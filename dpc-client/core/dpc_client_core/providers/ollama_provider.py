# dpc_client_core/providers/ollama_provider.py

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Dict, Any, Optional, List

import ollama

from .base import AIProvider

logger = logging.getLogger(__name__)

# Sampling parameters passed through from providers.json to the Ollama API.
# Options must stay a plain dict: min_p is missing from the SDK Options model
# (ollama 0.6.2) and survives only the dict serialization path.
OLLAMA_SAMPLING_PARAMS = ["min_p", "presence_penalty", "repeat_penalty", "top_k", "top_p", "num_predict"]

# Vision-capable Ollama models (for auto-detection)
OLLAMA_VISION_MODELS = [
    "qwen3.5",          # Qwen3.5 family — all sizes (0.8b-122b) are natively multimodal
    "qwen3.6",          # Qwen3.6 family — natively multimodal
    "qwen3-vl",         # Qwen3-VL dedicated vision variants
    "llava",            # LLaVA variants
    "llama3.2-vision",  # Llama 3.2 vision models
    "ministral-3",      # Ministral 3 vision models (3b, 8b, 14b)
    "bakllava",         # BakLLaVA
    "moondream",        # Moondream
]

# Thinking/reasoning models (for auto-detection)
# These models perform extended reasoning before producing their final response
OLLAMA_THINKING_MODELS = [
    "deepseek-r1",      # DeepSeek R1 (all variants)
    "deepseek-reasoner",
    "qwen3",            # Qwen3 family (3b, 8b, 14b, 30b, 32b, 235b) — native think param
]


class OllamaProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        self.client = ollama.AsyncClient(host=config.get("host"))
        # The loop `self.client` belongs to. Captured here when there is one,
        # so the long-lived service loop keeps a reusable client; None means
        # the first request adopts whatever loop it runs on.
        try:
            self._own_loop: Optional[Any] = asyncio.get_running_loop()
        except RuntimeError:
            self._own_loop = None
        self._last_thinking: Optional[str] = None

    @asynccontextmanager
    async def _client(self):
        """Yield a client usable on the running loop, closing it if it is ours.

        An httpx pool belongs to the loop that opened it, and agent tools run
        each async call in a fresh loop that is closed afterwards
        (tools/registry.py execute). The previous version handled that by
        replacing `self.client` whenever the running loop changed — and
        dropping the old one **without closing it**, because its loop was
        already dead. Every such call leaked a client holding an open socket
        to Ollama with a read still outstanding. One of those sockets is what
        parked the process inside `IocpProactor.close()` at shutdown, where
        the wait is unbounded: the exit never completed and the process had
        to be killed.

        Now the shared client is only ever used on the loop it belongs to.
        Any other loop gets its own client and closes it before returning, on
        that same loop — so nothing outlives its pool and shutdown has no
        orphans to find. The cost is one connection per request from those
        loops, which against a local Ollama is nothing; the old cache bought
        no reuse there anyway, since each per-call loop rebuilt the client
        regardless.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._own_loop is None:
            self._own_loop = loop
        if loop is self._own_loop:
            yield self.client
            return
        client = ollama.AsyncClient(host=self.config.get("host"))
        try:
            yield client
        finally:
            try:
                await client.close()
            except Exception as exc:
                logger.debug(
                    "OllamaProvider '%s': closing per-call client failed: %s",
                    self.alias, exc,
                )

    def supports_vision(self) -> bool:
        """Check if this Ollama model supports vision/multimodal inputs."""
        return any(vm in self.model.lower() for vm in OLLAMA_VISION_MODELS)

    def supports_thinking(self) -> bool:
        """Check if this Ollama model is a thinking/reasoning model."""
        return any(tm in self.model.lower() for tm in OLLAMA_THINKING_MODELS)

    def _build_options(self, **kwargs) -> Optional[Dict[str, Any]]:
        options: Dict[str, Any] = {}
        if self.config.get("context_window"):
            options["num_ctx"] = self.config["context_window"]
        temp = kwargs.get("temperature", self.temperature)
        if temp != 0.7:
            options["temperature"] = temp
        for key in OLLAMA_SAMPLING_PARAMS:
            if key in self.config:
                options[key] = self.config[key]
        if options:
            logger.debug(f"OllamaProvider '{self.alias}': options={options}")
        return options or None

    async def generate_response(self, prompt: str, **kwargs) -> str:
        self._last_thinking = None  # clear from previous call
        try:
            message = {'role': 'user', 'content': prompt}

            options = self._build_options(**kwargs)

            # Timeout: configurable via providers.json "timeout" field (default 300s).
            # Large models (9B+) can take >60s for initial VRAM load on first query.
            timeout = self.config.get("timeout", 300.0)

            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.chat(
                        model=self.model,
                        messages=[message],
                        options=options,
                        think=True if self.supports_thinking() else None,
                    ),
                    timeout=timeout
                )
            self._last_thinking = response['message'].thinking
            content = response['message']['content']
            if not content and self._last_thinking:
                content = self._last_thinking
            return content
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama provider '{self.alias}' timed out after {timeout}s.")
        except Exception as e:
            raise RuntimeError(f"Ollama provider '{self.alias}' failed: {e}") from e

    def get_last_thinking(self) -> Optional[str]:
        """Return thinking content from the most recent generate_response call."""
        return self._last_thinking

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Ollama vision API using images parameter.
        Docs: https://docs.ollama.com/capabilities/vision

        Args:
            prompt: Text prompt
            images: List of dicts with keys:
                - path: str (file path)
                - base64: str (optional, base64 data)
                - mime_type: str (optional)
            **kwargs: Additional parameters (temperature, timeout, etc.)

        Returns:
            str: AI response text
        """
        self._last_thinking = None
        try:
            # Build image list (Ollama accepts paths or base64)
            image_inputs = []
            for img in images:
                if "base64" in img:
                    # Use base64 data if available
                    base64_data = img["base64"]
                    # Strip data URL prefix if present (data:image/png;base64,...)
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                    image_inputs.append(base64_data)
                elif "path" in img:
                    # Use file path (Ollama SDK handles reading)
                    image_inputs.append(str(img["path"]))
                else:
                    raise ValueError("Image must have 'path' or 'base64' key")

            # Build message with images
            message = {
                'role': 'user',
                'content': prompt,
                'images': image_inputs
            }

            options = self._build_options(**kwargs)

            # Vision queries may take longer; respect provider config timeout first
            timeout = kwargs.get("timeout", self.config.get("timeout", 300.0))

            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.chat(
                        model=self.model,
                        messages=[message],
                        options=options,
                        think=True if self.supports_thinking() else None,
                        # Keep the VL model resident for a bit so back-to-back agent
                        # QC calls don't cold-start a reload each time (was 0 =
                        # unload immediately). Configurable via providers.json
                        # vision_keep_alive. Unaffected by the per-call client: this
                        # is the model's residency in Ollama, not our connection.
                        keep_alive=self.config.get("vision_keep_alive", "1m"),
                    ),
                    timeout=timeout
                )
            self._last_thinking = response['message'].thinking
            content = response['message']['content']
            if not content and self._last_thinking:
                content = self._last_thinking
            return content
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama vision query '{self.alias}' timed out after {timeout}s.")
        except Exception as e:
            raise RuntimeError(f"Ollama vision API failed for '{self.alias}': {e}") from e

    @staticmethod
    def _anthropic_to_openai_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for t in tools:
            if "function" in t:
                out.append(t)
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    @staticmethod
    def _anthropic_to_openai_messages(system: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system:
            sys_text = system if isinstance(system, str) else "".join(
                b.get("text", "") for b in system if isinstance(b, dict)
            )
            if sys_text:
                out.append({"role": "system", "content": sys_text})
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            blocks = content if isinstance(content, list) else []
            if role == "assistant":
                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        text_parts.append(b.get("text", ""))
                    elif bt == "tool_use":
                        tool_calls.append({
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": b.get("input", {}),
                            },
                        })
                msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
                continue
            if role == "user":
                tool_results = [
                    b for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for tr in tool_results:
                        tr_content = tr.get("content", "")
                        if isinstance(tr_content, list):
                            tr_content = "".join(
                                b.get("text", "") for b in tr_content if isinstance(b, dict)
                            )
                        out.append({"role": "tool", "content": str(tr_content)})
                else:
                    text_parts = [
                        b.get("text", "") for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    out.append({"role": "user", "content": "".join(text_parts)})
                continue
            out.append({"role": role or "user", "content": json.dumps(blocks)})
        return out

    async def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Any = "",
        on_chunk: Optional[Any] = None,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        self._last_thinking = None
        ollama_messages = self._anthropic_to_openai_messages(system, messages)
        ollama_tools = self._anthropic_to_openai_tools(tools)

        options = self._build_options(**kwargs)
        timeout = self.config.get("timeout", 300.0)

        try:
            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.chat(
                        model=self.model,
                        messages=ollama_messages,
                        tools=ollama_tools,
                        options=options,
                        think=True if self.supports_thinking() else None,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama provider '{self.alias}' timed out after {timeout}s.")
        except Exception as e:
            raise RuntimeError(f"Ollama tool call '{self.alias}' failed: {e}") from e

        msg = response['message']
        self._last_thinking = getattr(msg, 'thinking', None)
        content = getattr(msg, 'content', None) or ''

        tool_calls_raw = []
        for tc in (getattr(msg, 'tool_calls', None) or []):
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
            tool_calls_raw.append(SimpleNamespace(
                id=getattr(tc, 'id', None) or f"call_{uuid.uuid4().hex[:8]}",
                name=tc.function.name,
                input=args or {},
            ))

        if not content and not tool_calls_raw and self._last_thinking:
            content = self._last_thinking
        if on_chunk and content:
            await on_chunk(content, conversation_id)

        prompt_tokens = getattr(response, 'prompt_eval_count', 0) or 0
        completion_tokens = getattr(response, 'eval_count', 0) or 0
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        return {
            "content": content,
            "tool_calls_raw": tool_calls_raw,
            "thinking": self._last_thinking,
            "usage": usage,
        }

    async def get_model_info(self) -> Dict[str, Any]:
        """Query Ollama for model information including parameters.

        Returns:
            Dict containing:
                - modelfile: Raw modelfile content
                - parameters: Model parameters string
                - num_ctx: Parsed context window size (or None)
                - details: Model details (family, parameter_size, etc.)
        """
        try:
            async with self._client() as client:
                response = await client.show(model=self.model)

            # Parse num_ctx from modelfile
            num_ctx = None
            modelfile = response.get('modelfile', '')
            if modelfile:
                num_ctx = self._parse_num_ctx_from_modelfile(modelfile)

            # Convert details to dict if it's a Pydantic model
            details = response.get('details')
            if details:
                # Handle Pydantic models (they have model_dump method)
                if hasattr(details, 'model_dump'):
                    details = details.model_dump(exclude_none=True)
                elif hasattr(details, 'dict'):
                    details = details.dict(exclude_none=True)
                elif isinstance(details, dict):
                    details = details
                else:
                    details = {}
            else:
                details = {}

            # Convert modified_at datetime to string if present
            modified_at = response.get('modified_at')
            if modified_at and hasattr(modified_at, 'isoformat'):
                modified_at = modified_at.isoformat()

            return {
                "modelfile": modelfile,
                "parameters": response.get('parameters', ''),
                "num_ctx": num_ctx,
                "details": details,
                "template": response.get('template', ''),
                "modified_at": modified_at,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to get model info for '{self.model}': {e}") from e

    @staticmethod
    def _parse_num_ctx_from_modelfile(modelfile: str) -> Optional[int]:
        """Extract num_ctx parameter from modelfile string.

        Args:
            modelfile: Raw modelfile content

        Returns:
            Context window size as integer, or None if not found
        """
        import re
        match = re.search(r'PARAMETER\s+num_ctx\s+(\d+)', modelfile, re.IGNORECASE)
        return int(match.group(1)) if match else None

    async def close(self) -> None:
        """Close the Ollama async client. Model stays loaded — Ollama manages
        VRAM via its own keep_alive TTL (default 5 min idle → auto-unload).

        Per-request clients are already closed by `_client()`, on the loop
        that opened them; this only releases the one built in __init__ (or
        one a caller swapped in). It must not raise: shutdown used to log
        `Error closing provider 'ollama_vision': Event loop is closed` here
        and move on, which was the visible half of the leak — the invisible
        half was every client replaced before it, never closed at all.
        """
        if hasattr(self.client, 'close'):
            try:
                await self.client.close()
            except Exception as exc:
                logger.debug(
                    "OllamaProvider '%s': close failed (%s) — the per-call "
                    "clients were already closed by their own loop",
                    self.alias, exc,
                )
                return
        logger.debug(f"OllamaProvider '{self.alias}': Client closed")
