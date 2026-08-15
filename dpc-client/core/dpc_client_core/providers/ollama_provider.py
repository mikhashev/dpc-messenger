# dpc_client_core/providers/ollama_provider.py

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

import ollama

from .base import AIProvider, normalize_reasoning_effort

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

# What the daemon says a model can do, keyed by model name. Both lists above
# are a copy of knowledge Ollama already has, and a copy goes stale the day a
# model is published: muse-glimmer is vision- and thinking-capable and appears
# in neither. So ask, and keep the lists for a daemon too old to answer.
# Cached process-wide because the answer cannot change without the model being
# pulled again; measured 2026-08-13: 72 ms on the first call, 3 ms after.
# The whole answer is kept rather than the capability list alone: the same
# response also carries the model's own sampling defaults, and reading them
# from a second call would double a question the daemon has already answered.
_MODEL_INFO: Dict[str, Any] = {}

# The question is asked from paths that run on the event loop. Against a
# local daemon it costs milliseconds, but `host` may be another machine, and
# a black-hole address with no timeout hangs on the SYN for as long as the OS
# allows — measured: with this timeout the same call raises ConnectTimeout in
# 1.01 s. This is the deadline, not an expectation.
_CAPABILITY_TIMEOUT_SECONDS = 2.0


def _reported_capabilities(model: str, host: Optional[str]) -> Optional[frozenset]:
    """Capabilities as Ollama reports them, or None if it could not be asked.

    Three answers, and they must stay three. A frozenset is what the daemon
    said — empty means it answered and named nothing. None means nobody
    could tell us, either because the daemon is unreachable or because it is
    old enough not to carry the field at all, and then the substring lists
    below decide. Folding the third case into an empty set would quietly
    strip vision from a model that has it."""
    info = _describe(model, host)
    if info is None:
        return None
    reported = getattr(info, "capabilities", None)
    return None if reported is None else frozenset(reported)


def _describe(model: str, host: Optional[str]) -> Optional[Any]:
    """The daemon's whole answer about a model, or None if it could not be
    asked. One call per model per process."""
    if model in _MODEL_INFO:
        return _MODEL_INFO[model]
    try:
        info = ollama.Client(
            host=host, timeout=_CAPABILITY_TIMEOUT_SECONDS
        ).show(model)
    except Exception as e:
        # Not cached: a daemon that is down now may be up on the next call.
        logger.debug("Ollama could not describe %s: %s", model, e)
        return None
    _MODEL_INFO[model] = info
    return info


def _model_default(model: str, host: Optional[str], key: str) -> Optional[str]:
    """A sampling default from the model's own Modelfile, as the daemon reports
    it, or None if it does not name one.

    `/api/show` returns these as the text of the PARAMETER lines — one
    `name<spaces>value` per line — which is why this parses rather than
    indexes. Only the value is returned, and as a string: it is for a log
    line, and rounding it into a float would let `1` come back as `1.0` and
    read as a number we chose."""
    info = _describe(model, host)
    text = getattr(info, "parameters", None) if info is not None else None
    if not text:
        return None
    for line in text.splitlines():
        name, _, value = line.strip().partition(" ")
        if name == key and value.strip():
            return value.strip()
    return None


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
        # Said once per provider, not once per round: both are properties of the
        # (model, alias) pair, so repeating them per call would bury the log
        # without telling anyone anything new.
        self._effort_clamped_logged = False
        self._effort_ignored_logged = False
        self._effort_unknown_logged = False
        self._temperature_override_logged = False

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
        """Whether this model takes images — the daemon's answer if there is
        one, the name list only when there is not."""
        caps = _reported_capabilities(self.model, self.config.get("host"))
        if caps is not None:
            return "vision" in caps
        return any(vm in self.model.lower() for vm in OLLAMA_VISION_MODELS)

    def supports_thinking(self) -> bool:
        """Whether this model can reason before answering. Can, not should —
        see `_think_flag`."""
        caps = _reported_capabilities(self.model, self.config.get("host"))
        if caps is not None:
            return "thinking" in caps
        return any(tm in self.model.lower() for tm in OLLAMA_THINKING_MODELS)

    def _think_flag(self, effort: Optional[str] = None) -> Optional[Union[bool, str]]:
        """What to send as `think`: the per-call effort if there is a usable
        one, else the configuration, else the capability.

        A model that *can* think is not a model that *should* on every call:
        qwen3.5:9b spent a whole QC verdict reasoning and returned nothing —
        83,693 characters of thinking, `done_reason=length`, empty content.
        Until now the capability decided alone, and the only way to say no
        was to keep the model out of a hardcoded list.

        This is the one place `think` is decided; it used to be three copies
        at the call sites. The per-call effort selector promised here has now
        arrived and outranks the configuration, which outranks the
        capability — a second source deciding the same flag is how the two
        lists above came to disagree with the daemon.

        Two things the daemon taught us in the measuring, both load-bearing:

        `max` is sent as `high`. On qwen3.8 with a fixed seed the two produce
        byte-identical traces, so nothing is lost; and the Python SDK types the
        field as `Literal['low','medium','high']`, so `max` dies in pydantic
        before a request leaves the process. The clamp is therefore both free
        and required. If a future model ever separates them, the log line below
        is how anyone will find out the downgrade was happening.

        A level sent to a model that cannot think is **refused**, not ignored:
        `400 "<model> does not support thinking"`. So the effort is dropped for
        such a model rather than passed on — a group-scoped effort must not be
        able to kill every call an agent makes because of the model it sits on.
        `think=False` is the only value every model accepts."""
        level = normalize_reasoning_effort(effort)
        if level is not None:
            if self.supports_thinking():
                if level == "max":
                    if not self._effort_clamped_logged:
                        logger.info(
                            "OllamaProvider '%s': effort 'max' sent as 'high' — the "
                            "daemon treats them alike on this model and the SDK "
                            "cannot express 'max'.", self.alias,
                        )
                        self._effort_clamped_logged = True
                    return "high"
                return level
            if not self._effort_ignored_logged:
                logger.info(
                    "OllamaProvider '%s': reasoning_effort='%s' ignored — %s does "
                    "not report thinking, and a level would be refused with a 400.",
                    self.alias, level, self.model,
                )
                self._effort_ignored_logged = True
        elif (effort or "").strip():
            # A word arrived and it is not one we know. Falling through to the
            # configuration is the right behaviour — guessing at a level would be
            # the very substitution this vocabulary exists to stop — but doing it
            # silently is not: `none` and `minimal` are real DeepSeek words that a
            # group's stored effort can carry to an Ollama agent, and without this
            # line they would read as "the operator chose the configured value".
            # The empty string is not this case: it is the header's own "Config".
            if not self._effort_unknown_logged:
                logger.info(
                    "OllamaProvider '%s': reasoning_effort=%r is not a level this "
                    "provider knows — using the configured value instead.",
                    self.alias, effort,
                )
                self._effort_unknown_logged = True
        configured = self.config.get("think")
        if configured is not None:
            return bool(configured)
        return True if self.supports_thinking() else None

    def _warn_if_temperature_overrides_the_model(self, sent: Any) -> None:
        """Say once that a configured temperature is displacing the model's own.

        This board found five of seven Ollama aliases carrying `temperature:
        0.7` that nobody had chosen — the number the old code used as a
        sentinel for "unset", written into the config file by the editor's own
        placeholder. They now reach the daemon, and 0.7 against a Modelfile
        that asks for 1 is a quieter model than its author shipped. Nothing is
        changed here: the operator's number is sent, and the log names both so
        the choice can be seen rather than inherited.

        Deliberately not gated on thinking. The decided package narrowed this
        to thinking-on calls by analogy with DeepSeek, where the vendor
        documents the field as ignored while reasoning; on Ollama no such
        claim exists and none was measured, so the informative event is the
        override itself, which is exactly as unintended on a model that cannot
        think."""
        if self._temperature_override_logged:
            return
        default = _model_default(self.model, self.config.get("host"), "temperature")
        if default is None or str(sent) == default:
            return
        self._temperature_override_logged = True
        logger.info(
            "OllamaProvider '%s': sending temperature=%s; %s asks for %s in its "
            "own Modelfile. Clear the field in the providers editor to run at "
            "the model's default.",
            self.alias, sent, self.model, default,
        )

    def _build_options(self, **kwargs) -> Optional[Dict[str, Any]]:
        options: Dict[str, Any] = {}
        if self.config.get("context_window"):
            options["num_ctx"] = self.config["context_window"]
        # Send a temperature when somebody chose one. The previous rule
        # skipped the value 0.7 to mean "unset", which made a configured 0.7
        # indistinguishable from silence: five of the seven Ollama providers
        # on this machine ask for 0.7 in providers.json and were running at
        # the model's own default instead — 1.0 on the two that were checked.
        temp = kwargs.get("temperature", self.config.get("temperature"))
        if temp is not None:
            options["temperature"] = temp
            self._warn_if_temperature_overrides_the_model(temp)
        for key in OLLAMA_SAMPLING_PARAMS:
            if key in self.config:
                options[key] = self.config[key]
        # `think` is logged beside the options because it is the one parameter
        # whose effect cannot be read back from the answer: a model may reason
        # after being told not to, and without this line there is no way to
        # tell that from the flag never having been sent.
        if options:
            logger.debug(
                "OllamaProvider '%s': options=%s think=%s",
                self.alias, options, self._think_flag(kwargs.get("reasoning_effort")),
            )
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
                        think=self._think_flag(kwargs.get("reasoning_effort")),
                    ),
                    timeout=timeout
                )
            self._last_thinking = response['message'].thinking
            content = response['message']['content']
            # A chat answer is read by a person who can see it is reasoning, and the
            # alternative here is a blank message — so this path keeps the fallback
            # and says in the log that it fired. The vision path does not: see the
            # note there for why the same trade goes the other way.
            if not content and self._last_thinking:
                logger.warning(
                    "OllamaProvider '%s': no answer, returning %d characters of "
                    "reasoning in its place.", self.alias, len(self._last_thinking),
                )
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
                        think=self._think_flag(kwargs.get("reasoning_effort")),
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
            # No fallback to the reasoning here, unlike the two text paths. What a
            # vision call returns is read as a description of what is in the image —
            # a transcription, a QC verdict — and reasoning handed back in that slot
            # is a plausible lie in the one place a plausible lie is worst. Measured
            # 2026-08-13: a page whose reasoning ran the token budget out returned
            # 0 characters of content beside 13,788 of thinking; with the fallback,
            # that arrives at the caller as the answer. Empty is the honest report,
            # and `describe_image` already says "returned no description" for it.
            if not content and self._last_thinking:
                logger.warning(
                    "OllamaProvider '%s': vision call produced %d characters of "
                    "reasoning and no answer — reporting empty rather than passing "
                    "the reasoning off as the answer.",
                    self.alias, len(self._last_thinking),
                )
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
                        think=self._think_flag(kwargs.get("reasoning_effort")),
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

        # Same trade as the chat path: an agent round with neither an answer nor a
        # tool call is a dead round, and the reasoning is better than nothing to
        # continue from. Logged, so the round can be told apart afterwards.
        if not content and not tool_calls_raw and self._last_thinking:
            logger.warning(
                "OllamaProvider '%s': round produced neither an answer nor a tool "
                "call, continuing on %d characters of reasoning.",
                self.alias, len(self._last_thinking),
            )
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
