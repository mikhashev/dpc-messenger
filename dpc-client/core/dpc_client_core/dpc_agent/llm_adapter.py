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

    def __init__(self, llm_manager: "LLMManager"):
        """
        Initialize the adapter.

        Args:
            llm_manager: DPC's LLMManager instance (injected from CoreService)
        """
        self._llm_manager = llm_manager
        self._default_model: Optional[str] = None

    def _get_agent_provider_alias(self) -> Optional[str]:
        """
        Get the provider alias to use for agent inference.

        Priority:
        1. agent_provider (if configured)
        2. default_provider (fallback)

        Returns:
            Provider alias string, or None if no provider configured
        """
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

        Returns:
            (response_message, usage_dict) tuple in Ouroboros format
        """
        # Check if dpc_agent provider has peer_id (remote inference) - KISS approach
        dpc_agent_provider = self._llm_manager.providers.get("dpc_agent")
        if dpc_agent_provider and hasattr(dpc_agent_provider, 'peer_id') and dpc_agent_provider.peer_id:
            log.debug(f"Routing to remote peer: {dpc_agent_provider.peer_id}")
            return await self._chat_via_remote_peer(
                dpc_agent_provider, messages, tools, on_stream_chunk, conversation_id
            )

        # Local inference - existing logic
        # Convert message list to prompt string for DPC providers
        prompt = self._messages_to_prompt(messages)

        # Inject tool descriptions if provided
        if tools:
            log.debug(f"Injecting {len(tools)} tool descriptions into prompt")
            tool_descriptions = self._format_tools_for_prompt(tools)
            prompt = f"{tool_descriptions}\n\n{prompt}"

        # Get response from DPC's LLMManager
        # Use agent_provider if configured, otherwise fallback to default_provider
        alias = self._get_agent_provider_alias()
        if not alias:
            raise RuntimeError("No AI provider configured in DPC Messenger (check agent_provider or default_provider)")
        provider = self._llm_manager.providers[alias]

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

            # Parse for tool calls if tools were provided
            if tools:
                log.debug(f"Parsing tool calls from response (len={len(response)})")
                tool_calls = self._parse_tool_calls(response)
                log.debug(f"Parsed {len(tool_calls)} tool calls from response")
                if tool_calls:
                    response_msg["tool_calls"] = tool_calls
                    log.info(f"Found {len(tool_calls)} tool call(s): {[tc['function']['name'] for tc in tool_calls]}")

            # Estimate usage (DPC providers may not return token counts)
            usage: Dict[str, Any] = {
                "prompt_tokens": len(prompt) // 4,  # Rough estimate: ~4 chars per token
                "completion_tokens": len(response) // 4,
                "total_tokens": (len(prompt) + len(response)) // 4,
                "cost": 0.0,  # DPC tracks cost separately in its own system
            }

            return response_msg, usage

        except Exception as e:
            log.error(f"DPC LLM error: {e}")
            raise

    async def _chat_via_remote_peer(
        self,
        dpc_agent_provider: Any,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        conversation_id: Optional[str] = None,
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

            # Use actual token counts from remote if available, otherwise estimate
            if remote_prompt_tokens and remote_response_tokens:
                usage: Dict[str, Any] = {
                    "prompt_tokens": remote_prompt_tokens,
                    "completion_tokens": remote_response_tokens,
                    "total_tokens": remote_tokens or (remote_prompt_tokens + remote_response_tokens),
                    "cost": 0.0,
                }
            else:
                # Fallback estimation
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
        lines.append("```\n")

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
