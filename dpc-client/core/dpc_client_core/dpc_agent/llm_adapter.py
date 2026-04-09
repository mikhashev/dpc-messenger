"""
DPC LLM Adapter - Bridges Ouroboros agent to DPC's LLMManager.

Uses DPC's existing AI providers (Ollama, OpenAI Compatible, Anthropic, ZAI, etc.)
instead of OpenRouter. This allows the embedded agent to use whatever AI provider
the user has configured in DPC Messenger.

Key differences from Ouroboros's LLMClient:
1. No OpenRouter API calls - uses DPC's LLMManager
2. Message list converted to prompt string (DPC providers expect string input)
3. Tool calling implemented via prompt injection (DPC providers don't natively support tools)
4. Budget tracking delegated to DPC's existing system
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ..llm_manager import LLMManager

log = logging.getLogger(__name__)


class DpcLlmAdapter:
    """
    Adapts DPC's LLMManager to the interface expected by the Ouroboros agent loop.

    This replaces the OpenRouter-based LLMClient from Ouroboros, allowing
    the embedded agent to use DPC's configured AI providers.
    """

    def __init__(
        self,
        llm_manager: "LLMManager",
        provider_alias: Optional[str] = None,
        compute_host: str = "",
    ):
        """
        Initialize the adapter.

        Args:
            llm_manager: DPC's LLMManager instance (injected from CoreService)
            provider_alias: Specific provider to use (overrides agent_provider/default_provider)
            compute_host: Optional remote peer node_id — routes all LLM calls to that peer
        """
        self._llm_manager = llm_manager
        self._provider_alias = provider_alias  # Per-agent provider override
        self._compute_host = compute_host  # Per-agent remote peer override
        self._default_model: Optional[str] = None
        # Reuse existing TokenCountManager for accurate token counting
        self._token_counter = getattr(llm_manager, 'token_count_manager', None)
        if self._token_counter is None:
            log.warning("TokenCountManager not available - using character estimation")

    def _get_agent_provider_alias(self) -> Optional[str]:
        """
        Get the provider alias to use for agent inference.

        Priority:
        1. Per-agent provider_alias (if set via constructor)
        2. agent_provider (if configured in LLMManager)
        3. default_provider (fallback)

        Returns:
            Provider alias string, or None if no provider configured
        """
        # v0.19.0+: Per-agent provider override (Phase 3)
        if self._provider_alias and self._provider_alias in self._llm_manager.providers:
            log.debug(f"Using per-agent provider: {self._provider_alias}")
            return self._provider_alias

        # v0.18.0+: Use agent_provider if configured
        agent_provider = getattr(self._llm_manager, 'agent_provider', None)
        if agent_provider and agent_provider in self._llm_manager.providers:
            log.debug(f"Using agent_provider: {agent_provider}")
            return agent_provider

        # Fallback to default_provider
        default_provider = self._llm_manager.default_provider
        if default_provider and default_provider in self._llm_manager.providers:
            log.debug(f"Using default_provider (fallback): {default_provider}")
            return default_provider

        return None

    def _get_background_provider_alias(self) -> Optional[str]:
        """
        Get the provider alias for background tasks (consciousness, evolution).

        Falls back to agent provider if no background_provider configured.
        """
        bg_provider = getattr(self._llm_manager, 'background_provider', None)
        if bg_provider and bg_provider in self._llm_manager.providers:
            log.debug(f"Using background_provider: {bg_provider}")
            return bg_provider
        # Fall back to normal agent provider
        return self._get_agent_provider_alias()

    def _get_agent_provider(self) -> Optional[Any]:
        """Get the agent's provider instance."""
        alias = self._get_agent_provider_alias()
        if alias:
            return self._llm_manager.providers.get(alias)
        return None

    def _agent_provider_supports_vision(self) -> bool:
        """
        Check if the agent's configured provider supports vision natively.

        Returns:
            True if provider supports vision, False otherwise
        """
        provider = self._get_agent_provider()
        if provider and hasattr(provider, 'supports_vision'):
            return provider.supports_vision()
        return False

    def default_model(self) -> str:
        """Return the current DPC provider's model name."""
        try:
            alias = self._get_agent_provider_alias()
            if alias:
                provider = self._llm_manager.providers[alias]
                return getattr(provider, "model", "dpc_default") or "dpc_default"
        except Exception as e:
            log.debug(f"Failed to get default model: {e}")
        return "dpc_default"

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 4096,
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        conversation_id: Optional[str] = None,
        background: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Send chat request through DPC's LLMManager.

        Args:
            messages: List of message dicts with role/content
            model: Optional model override (ignored, uses DPC's configured provider)
            tools: Optional list of tool schemas (handled via prompt injection)
            reasoning_effort: Effort level (low/medium/high) - passed to provider if supported
            max_tokens: Max completion tokens
            on_stream_chunk: Optional async callback for streaming: await on_stream_chunk(chunk, conversation_id)
            conversation_id: Optional conversation ID for streaming callbacks
            background: If True, use background_provider for consciousness/evolution tasks

        Returns:
            (response_message, usage_dict) tuple in Ouroboros format
        """
        # Check if user message contains images (vision query)
        user_images = self._extract_images_from_messages(messages)
        if user_images:
            log.debug(f"Vision query with {len(user_images)} images")

            # Two-tier vision handling:
            # 1. If agent's provider supports vision → use native vision support
            # 2. If not → pre-analyze with vision model, inject description
            if self._agent_provider_supports_vision():
                log.info("Agent provider supports vision - using native vision")
                # Get the agent's provider for native vision call
                alias = self._get_agent_provider_alias()
                if not alias:
                    raise RuntimeError("No AI provider configured in DPC Messenger")
                provider = self._llm_manager.providers[alias]
                # Use native vision support (passes images directly to provider)
                return await self._chat_with_native_vision(
                    provider, messages, user_images, tools, on_stream_chunk, conversation_id
                )
            else:
                log.info("Agent provider does not support vision - pre-analyzing image")
                # Get user's text message for context
                user_text = self._extract_user_text(messages)
                # Pre-analyze the image with a vision model
                description = await self._pre_analyze_image_for_agent(
                    user_images, user_text
                )
                # Inject description into messages as text context
                messages = self._inject_image_description_into_messages(messages, description)
                # Continue with normal text-based agent flow

        # Check for remote peer routing — per-agent compute_host takes priority over global peer_id
        dpc_agent_provider = self._llm_manager.providers.get("dpc_agent")
        effective_peer_id = self._compute_host or (
            getattr(dpc_agent_provider, 'peer_id', None) if dpc_agent_provider else None
        )
        if effective_peer_id:
            if self._compute_host:
                # Per-agent remote routing: build a context object from per-agent values
                from types import SimpleNamespace
                remote_ctx = SimpleNamespace(
                    peer_id=effective_peer_id,
                    remote_provider=self._provider_alias,
                    remote_model=getattr(dpc_agent_provider, 'remote_model', None) if dpc_agent_provider else None,
                    _service=getattr(dpc_agent_provider, '_service', None) if dpc_agent_provider else None,
                    timeout=getattr(dpc_agent_provider, 'timeout', 180) if dpc_agent_provider else 180,
                )
                log.debug(f"Routing to per-agent remote peer: {effective_peer_id} (provider={self._provider_alias})")
                return await self._chat_via_remote_peer(
                    remote_ctx, messages, tools, on_stream_chunk, conversation_id
                )
            else:
                # Global peer_id routing (legacy KISS approach)
                log.debug(f"Routing to remote peer: {effective_peer_id}")
                return await self._chat_via_remote_peer(
                    dpc_agent_provider, messages, tools, on_stream_chunk, conversation_id
                )

        # Get the agent's provider (or background provider for consciousness/evolution)
        alias = self._get_background_provider_alias() if background else self._get_agent_provider_alias()
        if not alias:
            raise RuntimeError("No AI provider configured in DPC Messenger (check agent_provider or default_provider)")
        provider = self._llm_manager.providers[alias]

        # Native tool calling path — use when provider supports it and tools are requested.
        # This eliminates the text-based tool injection pattern that causes GLM-4.7 to
        # hallucinate [TOOL RESULT]/[USER] sections (tool bypass behavior, ArXiv 2412.04141).
        if tools and hasattr(provider, "generate_with_tools"):
            log.debug("Using native tool calling path for provider '%s'", alias)
            try:
                return await self._chat_native_tools(
                    provider, messages, tools, on_stream_chunk, conversation_id
                )
            except Exception as e:
                log.warning(
                    "Native tool calling failed (%s), falling back to text injection path", e
                )

        # Local inference - text-only path (or pre-analyzed image description)
        # Convert message list to prompt string for DPC providers
        prompt = self._messages_to_prompt(messages)

        # Inject tool descriptions if provided
        if tools:
            log.debug(f"Injecting {len(tools)} tool descriptions into prompt")
            tool_descriptions = self._format_tools_for_prompt(tools)
            prompt = f"{tool_descriptions}\n\n{prompt}"

        # Call DPC provider - use streaming if available and callback provided
        try:
            if on_stream_chunk and hasattr(provider, 'generate_response_stream'):
                # Use streaming
                log.debug("Using streaming mode for LLM response")
                response = await provider.generate_response_stream(
                    prompt,
                    on_chunk=on_stream_chunk,
                    conversation_id=conversation_id,
                )
            else:
                # Non-streaming fallback
                response = await provider.generate_response(prompt)

            # Build response message in Ouroboros format
            response_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response,
            }

            # Capture thinking/reasoning if the provider produced it
            if hasattr(provider, 'get_last_thinking'):
                thinking = provider.get_last_thinking()
                if thinking:
                    response_msg["thinking"] = thinking

            # Parse for tool calls if tools were provided
            if tools:
                log.debug(f"Parsing tool calls from response (len={len(response)})")
                tool_calls = self._parse_tool_calls(response)
                log.debug(f"Parsed {len(tool_calls)} tool calls from response")
                if tool_calls:
                    response_msg["tool_calls"] = tool_calls
                    log.info(f"Found {len(tool_calls)} tool call(s): {[tc['function']['name'] for tc in tool_calls]}")

            # Count tokens accurately using TokenCountManager (reuse existing)
            model_name = self.default_model()
            if self._token_counter:
                prompt_tokens = self._token_counter.count_tokens(prompt, model_name)
                completion_tokens = self._token_counter.count_tokens(response, model_name)
            else:
                # Fallback to character estimation
                prompt_tokens = len(prompt) // 4
                completion_tokens = len(response) // 4

            usage: Dict[str, Any] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": 0.0,  # DPC tracks cost separately in its own system
            }

            return response_msg, usage

        except Exception as e:
            log.error(f"DPC LLM error: {e}")
            raise

    async def _chat_with_native_vision(
        self,
        provider: Any,
        messages: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        conversation_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Handle vision queries using the agent provider's native vision support.

        This is used when the agent's provider supports vision (Anthropic Claude 3+,
        Z.AI GLM-V models). Images are passed directly without pre-analysis.

        Args:
            provider: The vision-capable provider instance
            messages: List of message dicts with role/content
            images: List of image dicts with base64 and mime_type keys
            tools: Optional list of tool schemas
            on_stream_chunk: Optional streaming callback
            conversation_id: Optional conversation ID

        Returns:
            (response_message, usage_dict) tuple in Ouroboros format
        """
        # Build text prompt from messages (for the text part)
        prompt = self._messages_to_prompt(messages)

        # Inject tools if provided
        if tools:
            log.debug(f"Injecting {len(tools)} tool descriptions into vision prompt")
            tool_descriptions = self._format_tools_for_prompt(tools)
            prompt = f"{tool_descriptions}\n\n{prompt}"

        try:
            log.info(f"Using native vision support from provider '{provider.alias}'")

            # Call provider's generate_with_vision method
            response = await provider.generate_with_vision(
                prompt=prompt,
                images=images,
            )

            # Detect silent vision failure: some Anthropic-compatible endpoints (e.g. Z.AI)
            # accept base64 images, convert them to CDN URLs internally, but the underlying
            # model receives the JSON URL reference as text and cannot process it visually.
            # In that case the model explicitly says it cannot see the image.
            _vision_failure_phrases = [
                "cannot see image",
                "can't see image",
                "don't see an actual image",
                "don't see any image",
                "don't see the image",
                "didn't see any image",
                "no actual image",
                "no image embedded",
                "no image attached",
                "no image in the message",
                "json objects",
                "json format",
                "image as json",
                "passed as json",
                "image as a url",
                "provided as a url",
                "image was provided as",
                "no tool for image",
                "cannot analyze image",
                "cannot directly analyze",
                "no image analysis",
            ]
            response_lower = response.lower()
            if any(phrase in response_lower for phrase in _vision_failure_phrases):
                log.warning(
                    f"Native vision for provider '{provider.alias}' appears to have failed "
                    f"(model cannot process the image). Falling back to pre-analysis."
                )
                user_text = self._extract_user_text(messages)
                description = await self._pre_analyze_image_for_agent(images, user_text)
                messages = self._inject_image_description_into_messages(messages, description)
                # Re-build prompt with injected description and continue as text-only
                prompt = self._messages_to_prompt(messages)
                if tools:
                    tool_descriptions = self._format_tools_for_prompt(tools)
                    prompt = f"{tool_descriptions}\n\n{prompt}"
                response = await provider.generate_response(prompt)

            # Build response message in Ouroboros format
            response_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response,
            }

            # Parse for tool calls if tools were provided
            if tools:
                log.debug(f"Parsing tool calls from vision response (len={len(response)})")
                tool_calls = self._parse_tool_calls(response)
                if tool_calls:
                    response_msg["tool_calls"] = tool_calls
                    log.info(f"Found {len(tool_calls)} tool call(s) in vision response")

            # Count tokens
            model_name = self.default_model()
            if self._token_counter:
                prompt_tokens = self._token_counter.count_tokens(prompt, model_name)
                completion_tokens = self._token_counter.count_tokens(response, model_name)
            else:
                prompt_tokens = len(prompt) // 4
                completion_tokens = len(response) // 4

            usage: Dict[str, Any] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": 0.0,
            }

            return response_msg, usage

        except Exception as e:
            log.error(f"Native vision query error: {e}")
            raise

    async def _chat_native_tools(
        self,
        provider: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        conversation_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Native Anthropic SDK tool calling path.

        Converts Ouroboros-format messages/tools to Anthropic format, calls
        provider.generate_with_tools(), and converts the response back to
        Ouroboros format. Tool calls are returned as structured data — the model
        cannot hallucinate [TOOL RESULT]/[USER] sections because the API cleanly
        separates tool_use blocks from text content.
        """
        system, anthropic_messages = self._convert_messages_to_anthropic(messages)
        anthropic_tools = self._convert_tools_to_anthropic(tools)

        log.debug(
            "Native tool calling: %d messages → %d anthropic messages, %d tools",
            len(messages), len(anthropic_messages), len(anthropic_tools),
        )

        raw = await provider.generate_with_tools(
            messages=anthropic_messages,
            tools=anthropic_tools,
            system=system,
            on_chunk=on_stream_chunk,
            conversation_id=conversation_id,
        )

        # Convert Anthropic tool_use blocks → Ouroboros tool_calls format
        tool_calls = []
        for block in raw.get("tool_calls_raw", []):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                },
            })

        response_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": raw.get("content", ""),
        }
        if tool_calls:
            response_msg["tool_calls"] = tool_calls
            log.info("Native tool calling: %d tool call(s): %s", len(tool_calls), [tc["function"]["name"] for tc in tool_calls])
        if raw.get("thinking"):
            response_msg["thinking"] = raw["thinking"]

        usage = raw.get("usage") or {}
        if not usage.get("total_tokens"):
            # Fallback if provider didn't return usage
            model_name = self.default_model()
            if self._token_counter:
                prompt_tokens = self._token_counter.count_tokens(str(anthropic_messages), model_name)
                completion_tokens = self._token_counter.count_tokens(raw.get("content", ""), model_name)
            else:
                prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
                completion_tokens = len(raw.get("content", "")) // 4
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": 0.0,
            }
        else:
            usage.setdefault("cost", 0.0)

        return response_msg, usage

    @staticmethod
    def _convert_messages_to_anthropic(
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Convert Ouroboros message list → (system_str, anthropic_messages).

        Ouroboros uses role:"tool" for tool results; Anthropic expects them as
        role:"user" with tool_result content blocks. Consecutive tool results are
        batched into a single user message as required by the Anthropic API.
        """
        system = ""
        anthropic_messages: List[Dict[str, Any]] = []

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "system":
                system = msg.get("content", "")
                i += 1
                continue

            if role == "user":
                content = msg.get("content", "")
                # If content is already a list (e.g. vision), pass through
                anthropic_messages.append({"role": "user", "content": content})
                i += 1
                continue

            if role == "assistant":
                blocks: List[Dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in msg.get("tool_calls", []):
                    try:
                        input_data = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        input_data = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"tu_{i}"),
                        "name": tc["function"]["name"],
                        "input": input_data,
                    })
                # Anthropic requires at least one block
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                anthropic_messages.append({"role": "assistant", "content": blocks})
                i += 1
                continue

            if role == "tool":
                # Batch all consecutive tool results into one user message
                tool_results: List[Dict[str, Any]] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    tm = messages[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tm.get("tool_call_id", ""),
                        "content": str(tm.get("content", "")),
                    })
                    i += 1
                anthropic_messages.append({"role": "user", "content": tool_results})
                continue

            i += 1

        return system, anthropic_messages

    @staticmethod
    def _convert_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Ouroboros/OpenAI tool schemas → Anthropic tool format.

        OpenAI format: {"type":"function","function":{"name":...,"parameters":...}}
        Anthropic format: {"name":...,"description":...,"input_schema":...}
        """
        result = []
        for t in tools:
            func = t.get("function", t)  # unwrap if OpenAI-style wrapper present
            result.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    async def _chat_via_remote_peer(
        self,
        dpc_agent_provider: Any,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        conversation_id: Optional[str] = None,
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Route inference to remote peer when dpc_agent.peer_id is set.

        This implements the KISS approach - instead of creating a separate
        remote_peer provider, just add peer_id to dpc_agent config.

        Args:
            dpc_agent_provider: The DpcAgentProvider instance with peer_id set
            messages: List of message dicts with role/content
            tools: Optional list of tool schemas
            on_stream_chunk: Optional streaming callback
            conversation_id: Optional conversation ID
            images: Optional list of image dicts for vision queries

        Returns:
            (response_message, usage_dict) tuple in Ouroboros format
        """
        # Get CoreService from the provider (injected via set_service())
        service = getattr(dpc_agent_provider, '_service', None)
        if not service:
            raise RuntimeError("DpcAgentProvider missing CoreService reference - cannot route to remote peer")

        # Convert messages to prompt
        prompt = self._messages_to_prompt(messages)

        # Inject tools if provided
        if tools:
            log.debug(f"Injecting {len(tools)} tool descriptions into prompt for remote peer")
            tool_descriptions = self._format_tools_for_prompt(tools)
            prompt = f"{tool_descriptions}\n\n{prompt}"

        try:
            # Call remote inference via CoreService
            # Use configurable timeout from dpc_agent provider (default 180s)
            timeout = getattr(dpc_agent_provider, 'timeout', 180) or 180
            log.info(f"Routing agent inference to remote peer: {dpc_agent_provider.peer_id} (timeout={timeout}s)")
            result = await service._request_inference_from_peer(
                peer_id=dpc_agent_provider.peer_id,
                prompt=prompt,
                model=dpc_agent_provider.remote_model,
                provider=dpc_agent_provider.remote_provider,
                images=[],
                timeout=timeout
            )

            # Extract response text from result dict
            # _request_inference_from_peer returns: {"response": str, "tokens_used": int, ...}
            if isinstance(result, dict):
                response_text = result.get("response", "")
                remote_tokens = result.get("tokens_used")
                remote_prompt_tokens = result.get("prompt_tokens")
                remote_response_tokens = result.get("response_tokens")
            else:
                # Fallback if result is already a string (shouldn't happen but be safe)
                response_text = str(result) if result else ""
                remote_tokens = None
                remote_prompt_tokens = None
                remote_response_tokens = None

            # Build response message in Ouroboros format
            response_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response_text,
            }

            # Parse for tool calls if tools were provided
            if tools:
                log.debug(f"Parsing tool calls from remote response (len={len(response_text)})")
                tool_calls = self._parse_tool_calls(response_text)
                if tool_calls:
                    response_msg["tool_calls"] = tool_calls
                    log.info(f"Found {len(tool_calls)} tool call(s) from remote peer")

            # Use actual token counts from remote if available, otherwise count locally
            if remote_prompt_tokens and remote_response_tokens:
                usage: Dict[str, Any] = {
                    "prompt_tokens": remote_prompt_tokens,
                    "completion_tokens": remote_response_tokens,
                    "total_tokens": remote_tokens or (remote_prompt_tokens + remote_response_tokens),
                    "cost": 0.0,
                }
            elif self._token_counter:
                # Count locally using TokenCountManager
                model_name = self.default_model()
                prompt_tokens = self._token_counter.count_tokens(prompt, model_name)
                completion_tokens = self._token_counter.count_tokens(response_text, model_name)
                usage: Dict[str, Any] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cost": 0.0,
                }
            else:
                # Final fallback to character estimation
                usage: Dict[str, Any] = {
                    "prompt_tokens": len(prompt) // 4,
                    "completion_tokens": len(response_text) // 4,
                    "total_tokens": (len(prompt) + len(response_text)) // 4,
                    "cost": 0.0,
                }

            return response_msg, usage

        except Exception as e:
            log.error(f"Remote peer inference error: {e}")
            raise

    def _extract_images_from_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Extract image data from user messages.

        Parses multipart content to find image_url blocks and extracts
        base64-encoded image data.

        Args:
            messages: List of message dicts with role/content

        Returns:
            List of image dicts with base64 and mime_type keys
        """
        images = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            # Parse data URL
                            image_url = block.get("image_url", {}).get("url", "")
                            if image_url.startswith("data:"):
                                try:
                                    header, data = image_url.split(",", 1)
                                    # Extract mime type from header like "data:image/png;base64"
                                    mime_type = header.split(";")[0].split(":")[1]
                                    images.append({
                                        "base64": data,
                                        "mime_type": mime_type,
                                    })
                                except (ValueError, IndexError) as e:
                                    log.warning(f"Failed to parse image data URL: {e}")
        return images

    def _extract_user_text(self, messages: List[Dict[str, Any]]) -> str:
        """
        Extract text content from the last user message.

        Args:
            messages: List of message dicts with role/content

        Returns:
            Text content of the last user message, or empty string
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    # Extract text from multipart content
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    return "\n".join(text_parts)
        return ""

    async def _pre_analyze_image_for_agent(
        self,
        images: List[Dict[str, Any]],
        user_message: str,
    ) -> str:
        """
        Pre-analyze images using a vision model and return description.

        This is used when the agent's provider doesn't support vision natively.
        The description is injected into the messages so the agent can reason
        about visual content using its tools.

        Args:
            images: List of image dicts with base64 and mime_type keys
            user_message: The user's text message (for context)

        Returns:
            Text description of the image content
        """
        try:
            # Build analysis prompt
            analysis_prompt = (
                f"Analyze this image in detail. The user asked: {user_message}\n\n"
                "Provide a comprehensive description that includes:\n"
                "- What objects, text, or UI elements are visible\n"
                "- Any error messages or important text\n"
                "- Layout and structure if it's a screenshot\n"
                "- Any other relevant details for understanding the image"
            )

            log.info(f"Pre-analyzing {len(images)} image(s) for non-vision agent provider")

            # Use LLMManager.query() with images - auto-selects vision provider
            response_metadata = await self._llm_manager.query(
                prompt=analysis_prompt,
                provider_alias=None,  # Auto-select vision provider
                images=images,
                return_metadata=True,
            )

            description = response_metadata.get("response", "")
            log.debug(f"Image analysis complete ({len(description)} chars)")
            return description

        except Exception as e:
            log.error(f"Image pre-analysis failed: {e}")
            return f"[Image analysis failed: {e}]"

    def _inject_image_description_into_messages(
        self,
        messages: List[Dict[str, Any]],
        description: str,
    ) -> List[Dict[str, Any]]:
        """
        Inject image description into the last user message.

        This is used when the agent's provider doesn't support vision.
        The description is added as context so the agent can reason about it.

        Args:
            messages: List of message dicts
            description: Text description of the image

        Returns:
            Modified messages list with description injected
        """
        # Make a copy to avoid modifying original
        messages = list(messages)

        # Find the last user message and inject description
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content")

                # Build the enhanced content with image context
                enhanced_text = (
                    f"[The user has shared an image. Here is the visual analysis]:\n"
                    f"{description}\n\n"
                    f"[User's message]: "
                )

                if isinstance(content, str):
                    messages[i] = {
                        "role": "user",
                        "content": enhanced_text + content
                    }
                elif isinstance(content, list):
                    # Extract text from multipart content
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    original_text = "\n".join(text_parts)
                    messages[i] = {
                        "role": "user",
                        "content": enhanced_text + original_text
                    }
                break

        return messages

    def _messages_to_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """
        Convert message list to single prompt string for DPC providers.

        DPC's AI providers expect a prompt string, not a message list.
        This method preserves the structure by using role markers.
        """
        parts = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Handle multipart content (system messages with cache_control blocks)
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            text_parts.append(text)
                content = "\n\n".join(text_parts)

            # Skip empty content
            if not content or not str(content).strip():
                continue

            # Format based on role
            if role == "system":
                parts.append(f"[SYSTEM]\n{content}")
            elif role == "user":
                parts.append(f"[USER]\n{content}")
            elif role == "assistant":
                parts.append(f"[ASSISTANT]\n{content}")
            elif role == "tool":
                # Include tool results
                tool_call_id = msg.get("tool_call_id", "unknown")
                parts.append(f"[TOOL RESULT: {tool_call_id}]\n{content}")

        return "\n\n".join(parts)

    def _format_tools_for_prompt(self, tools: List[Dict[str, Any]]) -> str:
        """Format tool schemas as text descriptions for prompt injection."""
        lines = ["# Available Tools\n"]
        lines.append("You have access to the following tools. To use a tool, output a code block like:")
        lines.append("```tool_call")
        lines.append('{"name": "tool_name", "arguments": {"arg1": "value1"}}')
        lines.append("```")
        lines.append("")
        lines.append("**IMPORTANT**: Use ONLY the ```tool_call code block format above.")
        lines.append("Do NOT use <tool_call>, [tool_call], or any XML/HTML format.")
        lines.append("Do NOT add explanatory text before the tool call block — output the block directly.\n")

        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "unknown")
            desc = func.get("description", "No description available")
            lines.append(f"## {name}\n{desc}\n")

            params = func.get("parameters", {}).get("properties", {})
            required = func.get("parameters", {}).get("required", [])

            if params:
                lines.append("**Parameters:**")
                for pname, pspec in params.items():
                    ptype = pspec.get("type", "any")
                    pdesc = pspec.get("description", "")
                    req_marker = " (required)" if pname in required else ""
                    lines.append(f"  - `{pname}` ({ptype}){req_marker}: {pdesc}")
                lines.append("")

        return "\n".join(lines)

    def _parse_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        """
        Parse tool calls from response content.

        Expects format like:
        ```tool_call
        {"name": "repo_read", "arguments": {"path": "foo.py"}}
        ```

        Returns list of tool call dicts in OpenAI format.

        Note: Uses brace-balanced JSON parsing to handle content with embedded
        triple backticks (e.g., markdown code blocks inside the JSON content field).
        """
        tool_calls = []

        if not content:
            log.debug("Empty content passed to _parse_tool_calls")
            return tool_calls

        # Debug: log first 500 chars of content
        log.debug(f"Parsing content (len={len(content)}): {content[:500]!r}")

        # Find tool_call blocks using brace-balanced parsing
        # This handles cases where content contains triple backticks
        matches = self._extract_tool_call_json(content)
        log.debug(f"Found {len(matches)} tool_call JSON blocks")

        # Fallback: try simple regex if brace-balanced parsing found nothing
        if not matches and "tool_call" in content.lower():
            log.debug("Trying simple regex fallback")
            pattern = r"```tool_call\s*[\r]?\n(.*?)```"
            regex_matches = re.findall(pattern, content, re.DOTALL | re.IGNORECASE)
            if regex_matches:
                log.debug(f"Regex fallback found {len(regex_matches)} matches")
                matches.extend(regex_matches)

        # Last resort: try to find JSON objects that look like tool calls
        if not matches:
            # Look for JSON objects with "name" and "arguments" fields
            json_pattern = r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{[^}]*\}\s*\}'
            json_matches = re.findall(json_pattern, content, re.DOTALL)
            if json_matches:
                log.debug(f"Found {len(json_matches)} JSON tool call objects directly")
                matches = json_matches

        # 4th fallback: handle GLM-4.7 native XML format <tool_call>tool_name>{json}</tool_name>
        # GLM-4.7 sometimes outputs this format when it generates text before the tool call
        if not matches:
            glm_pattern = r'<tool_call>\s*([A-Za-z0-9_-]+)\s*>\s*(\{.*?\})\s*</[^>]+>'
            for tool_name, args_str in re.findall(glm_pattern, content, re.DOTALL):
                try:
                    args = json.loads(args_str)
                    normalized = json.dumps({"name": tool_name, "arguments": args}, ensure_ascii=False)
                    matches.append(normalized)
                    log.debug(f"GLM fallback: normalized <tool_call>{tool_name}> to standard format")
                except json.JSONDecodeError:
                    log.debug(f"GLM fallback: failed to parse args for <tool_call>{tool_name}")

        # 5th fallback: <tool_call>tool_name\n{json} (newline separator, no closing tag)
        # Seen when Z.AI text injection path produces this compact format
        if not matches:
            glm_newline_pattern = r'<tool_call>\s*([A-Za-z0-9_-]+)\s*\n(\{.*?\})(?:\s*</tool_call>)?'
            for tool_name, args_str in re.findall(glm_newline_pattern, content, re.DOTALL):
                try:
                    args = json.loads(args_str)
                    normalized = json.dumps({"name": tool_name, "arguments": args}, ensure_ascii=False)
                    matches.append(normalized)
                    log.debug(f"GLM newline fallback: parsed <tool_call>{tool_name}\\n{{json}}")
                except json.JSONDecodeError:
                    log.debug(f"GLM newline fallback: failed to parse args for <tool_call>{tool_name}")

        # 6th fallback: <tool_call>tool_name arguments={json} (equals sign separator)
        # Seen in GLM output: <tool_call>list_extended_sandbox_paths arguments={}
        # Multiple calls may be separated by --- on its own line
        if not matches:
            glm_equals_pattern = r'<tool_call>\s*([A-Za-z0-9_-]+)\s+arguments\s*=\s*(\{.*?\})'
            for tool_name, args_str in re.findall(glm_equals_pattern, content, re.DOTALL):
                try:
                    args = json.loads(args_str)
                    normalized = json.dumps({"name": tool_name, "arguments": args}, ensure_ascii=False)
                    matches.append(normalized)
                    log.debug(f"GLM equals fallback: parsed <tool_call>{tool_name} arguments={{...}}")
                except json.JSONDecodeError:
                    log.debug(f"GLM equals fallback: failed to parse args for <tool_call>{tool_name}")

        for i, match in enumerate(matches):
            try:
                # Clean up and parse JSON
                json_str = match.strip()
                data = json.loads(json_str)

                tool_calls.append({
                    "id": f"tc_{i}_{hash(json_str) % 10000:04d}",
                    "type": "function",
                    "function": {
                        "name": data.get("name", ""),
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False)
                    }
                })
                log.debug(f"Parsed tool call: {data.get('name', 'unknown')}")

            except json.JSONDecodeError as e:
                log.warning(f"Failed to parse tool call JSON: {match[:100]} - {e}")
                continue
            except Exception as e:
                log.warning(f"Error processing tool call: {e}")
                continue

        return tool_calls

    def _extract_tool_call_json(self, content: str) -> List[str]:
        """
        Extract JSON from tool_call blocks using brace-balanced parsing.

        This method handles the case where the JSON content field contains
        triple backticks (e.g., markdown code examples) which would break
        simple regex parsing.

        Args:
            content: Full response content

        Returns:
            List of JSON strings extracted from tool_call blocks
        """
        matches = []

        # Find all tool_call block starts
        # Pattern matches: ```tool_call followed by optional whitespace and newline
        block_start_pattern = re.compile(r'```tool_call\s*[\r]?\n', re.IGNORECASE)

        pos = 0
        while True:
            # Find next tool_call block start
            start_match = block_start_pattern.search(content, pos)
            if not start_match:
                break

            # Start of JSON content
            json_start = start_match.end()

            # Extract JSON using brace balancing
            json_str = self._extract_balanced_json(content, json_start)
            if json_str:
                matches.append(json_str)
                log.debug(f"Extracted JSON from tool_call block: {json_str[:100]}...")
                # Move position past the extracted content
                pos = json_start + len(json_str)
            else:
                # Failed to extract, move past this block start
                pos = json_start

        return matches

    def _extract_balanced_json(self, content: str, start_pos: int) -> Optional[str]:
        """
        Extract a complete JSON object starting at start_pos using brace balancing.

        This handles nested objects and strings properly, so it won't be confused
        by braces or backticks inside string values.

        Args:
            content: Full content string
            start_pos: Position to start extracting from

        Returns:
            Complete JSON string, or None if extraction failed
        """
        if start_pos >= len(content):
            return None

        # Skip leading whitespace
        while start_pos < len(content) and content[start_pos] in ' \t\n\r':
            start_pos += 1

        if start_pos >= len(content) or content[start_pos] != '{':
            return None

        depth = 0
        in_string = False
        escape_next = False
        pos = start_pos

        while pos < len(content):
            char = content[pos]

            if escape_next:
                escape_next = False
                pos += 1
                continue

            if char == '\\' and in_string:
                escape_next = True
                pos += 1
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                pos += 1
                continue

            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        # Found complete JSON object
                        return content[start_pos:pos + 1]

            pos += 1

        # Reached end without finding balanced JSON
        log.debug(f"Could not find balanced JSON starting at pos {start_pos}")
        return None

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Chat with tool support (convenience wrapper).

        Since DPC providers don't natively support function calling,
        we inject tool descriptions into the prompt and parse tool calls
        from the response.

        Args:
            messages: Message list
            tools: Tool schemas
            **kwargs: Additional arguments passed to chat()

        Returns:
            (response_message, usage_dict) tuple
        """
        return await self.chat(messages=messages, tools=tools, **kwargs)


def normalize_reasoning_effort(effort: str, default: str = "medium") -> str:
    """
    Normalize reasoning effort level.

    Maps various effort levels to standard values.
    """
    effort_lower = effort.lower().strip()

    # Map to standard values
    effort_map = {
        "low": "low",
        "minimal": "low",
        "fast": "low",
        "medium": "medium",
        "normal": "medium",
        "default": "medium",
        "high": "high",
        "thorough": "high",
        "deep": "high",
        "xhigh": "high",
        "extended": "high",
    }

    return effort_map.get(effort_lower, default)
