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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

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

    def default_model(self) -> str:
        """Return the current DPC provider's model name."""
        try:
            provider = self._llm_manager.get_default_provider()
            if provider:
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
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Send chat request through DPC's LLMManager.

        Args:
            messages: List of message dicts with role/content
            model: Optional model override (ignored, uses DPC's configured provider)
            tools: Optional list of tool schemas (handled via prompt injection)
            reasoning_effort: Effort level (low/medium/high) - passed to provider if supported
            max_tokens: Max completion tokens

        Returns:
            (response_message, usage_dict) tuple in Ouroboros format
        """
        # Convert message list to prompt string for DPC providers
        prompt = self._messages_to_prompt(messages)

        # Inject tool descriptions if provided
        if tools:
            tool_descriptions = self._format_tools_for_prompt(tools)
            prompt = f"{tool_descriptions}\n\n{prompt}"

        # Get response from DPC's LLMManager
        provider = self._llm_manager.get_default_provider()
        if not provider:
            raise RuntimeError("No AI provider configured in DPC Messenger")

        # Call DPC provider
        try:
            response = await provider.generate_response(prompt)

            # Build response message in Ouroboros format
            response_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response,
            }

            # Parse for tool calls if tools were provided
            if tools:
                tool_calls = self._parse_tool_calls(response)
                if tool_calls:
                    response_msg["tool_calls"] = tool_calls

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
        """
        tool_calls = []

        # Find tool_call blocks
        pattern = r"```tool_call\s*\n(.*?)```"
        matches = re.findall(pattern, content, re.DOTALL | re.IGNORECASE)

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

            except json.JSONDecodeError as e:
                log.warning(f"Failed to parse tool call JSON: {match[:100]} - {e}")
                continue
            except Exception as e:
                log.warning(f"Error processing tool call: {e}")
                continue

        return tool_calls

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
