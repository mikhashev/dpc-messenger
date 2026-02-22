"""
DPC Agent — LLM Tool Loop.

Adapted from Ouroboros loop.py for DPC Messenger integration.
Key changes:
- Uses DpcLlmAdapter instead of OpenRouter
- Removed supervisor event emission
- Simplified budget tracking
- No pricing fetching (DPC handles that)

Core loop: send messages to LLM, execute tool calls, repeat until final response.
"""

from __future__ import annotations

import json
import logging
import pathlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .utils import (
    utc_now_iso, append_jsonl, truncate_for_log,
    sanitize_tool_args_for_log, sanitize_tool_result_for_log, get_agent_root
)
from .context import compact_tool_history
from .llm_adapter import DpcLlmAdapter

if TYPE_CHECKING:
    from .tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# Default configuration
DEFAULT_MAX_ROUNDS = 200
DEFAULT_TIMEOUT_SEC = 120


def _truncate_tool_result(result: Any) -> str:
    """Hard-cap tool result string to 15000 characters."""
    result_str = str(result)
    if len(result_str) <= 15000:
        return result_str
    return result_str[:15000] + f"\n... (truncated from {len(result_str)} chars)"


def _execute_single_tool(
    tools: "ToolRegistry",
    tc: Dict[str, Any],
    logs_dir: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return result info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    # Execute tool
    is_error = False
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        is_error = True
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(logs_dir / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "tool_error",
            "task_id": task_id,
            "tool": fn_name,
            "args": args_for_log,
            "error": repr(e),
        })

    # Log tool execution
    append_jsonl(logs_dir / "tools.jsonl", {
        "ts": utc_now_iso(),
        "tool": fn_name,
        "task_id": task_id,
        "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = is_error or str(result).startswith("⚠️")

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
    }


def _execute_with_timeout(
    tools: "ToolRegistry",
    tc: Dict[str, Any],
    logs_dir: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
) -> Dict[str, Any]:
    """Execute a tool call with a hard timeout."""
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_execute_single_tool, tools, tc, logs_dir, task_id)
        try:
            return future.result(timeout=timeout_sec)
        except TimeoutError:
            result = f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit."
            append_jsonl(logs_dir / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "tool_timeout",
                "tool": fn_name,
                "timeout_sec": timeout_sec,
            })
            return {
                "tool_call_id": tool_call_id,
                "fn_name": fn_name,
                "result": result,
                "is_error": True,
                "args_for_log": {},
            }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: "ToolRegistry",
    llm: DpcLlmAdapter,
    agent_root: pathlib.Path,
    emit_progress: Callable[[str, Optional[str], Optional[int]], None],
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM-with-tools loop.

    Sends messages to LLM, executes tool calls, repeats until final response.

    Args:
        messages: Initial message list
        tools: Tool registry
        llm: LLM adapter
        agent_root: Agent storage root
        emit_progress: Callback for progress updates
        task_id: Task identifier for logging
        budget_remaining_usd: Optional budget limit
        max_rounds: Maximum LLM rounds before stopping

    Returns:
        (final_text, accumulated_usage, llm_trace) tuple
    """
    logs_dir = agent_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    llm_trace: Dict[str, Any] = {
        "assistant_notes": [],
        "tool_calls": [],
    }
    accumulated_usage: Dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "rounds": 0,
    }

    # Get tool schemas
    tool_schemas = tools.schemas(core_only=True, include_restricted=False)

    round_idx = 0
    try:
        while True:
            round_idx += 1

            # Hard limit on rounds
            if round_idx > max_rounds:
                finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({max_rounds}). Consider breaking into smaller tasks."
                messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
                try:
                    final_msg, _ = await llm.chat(messages, tools=tool_schemas)
                    if final_msg.get("content"):
                        return final_msg["content"], accumulated_usage, llm_trace
                except Exception:
                    log.warning("Failed to get final response after round limit", exc_info=True)
                return finish_reason, accumulated_usage, llm_trace

            # Compact old tool history when needed
            if round_idx > 8:
                messages = compact_tool_history(messages, keep_recent=6)

            # --- LLM call ---
            try:
                msg, usage = await llm.chat(messages, tools=tool_schemas)
                accumulated_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
                accumulated_usage["completion_tokens"] += usage.get("completion_tokens", 0)
                accumulated_usage["total_tokens"] += usage.get("total_tokens", 0)
                accumulated_usage["cost"] += usage.get("cost", 0)
                accumulated_usage["rounds"] += 1
            except Exception as e:
                log.error(f"LLM error: {e}", exc_info=True)
                return f"⚠️ LLM error: {e}", accumulated_usage, llm_trace

            # Handle empty response
            if msg is None:
                return "⚠️ No response from LLM", accumulated_usage, llm_trace

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            log.debug(f"LLM response: tool_calls={len(tool_calls)}, content_len={len(content) if content else 0}")
            if tool_calls:
                log.info(f"Processing {len(tool_calls)} tool call(s)")
            elif content and "tool_call" in content.lower():
                log.warning(f"No tool_calls parsed but 'tool_call' found in content: {content[:200]!r}")

            # No tool calls — final response
            if not tool_calls:
                if content and content.strip():
                    llm_trace["assistant_notes"].append(content.strip()[:320])
                return content or "", accumulated_usage, llm_trace

            # Process tool calls
            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

            if content and content.strip():
                emit_progress(content.strip(), None, round_num + 1)
                llm_trace["assistant_notes"].append(content.strip()[:320])

            # Execute tool calls
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                emit_progress(f"Executing {tool_name}...", tool_name, round_num + 1)
                timeout = tools.get_timeout(tc["function"]["name"])
                exec_result = _execute_with_timeout(tools, tc, logs_dir, timeout, task_id)

                truncated_result = _truncate_tool_result(exec_result["result"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": exec_result["tool_call_id"],
                    "content": truncated_result,
                })

                llm_trace["tool_calls"].append({
                    "tool": exec_result["fn_name"],
                    "args": exec_result["args_for_log"],
                    "result": truncate_for_log(exec_result["result"], 700),
                    "is_error": exec_result["is_error"],
                })

            # --- Budget guard ---
            if budget_remaining_usd is not None:
                task_cost = accumulated_usage.get("cost", 0)
                if budget_remaining_usd > 0 and task_cost > budget_remaining_usd * 0.5:
                    finish_reason = f"Task spent ${task_cost:.3f} (>50% of budget ${budget_remaining_usd:.2f})."
                    messages.append({"role": "system", "content": f"[BUDGET_LIMIT] {finish_reason} Give your final response now."})
                    try:
                        final_msg, _ = await llm.chat(messages, tools=None)
                        if final_msg.get("content"):
                            return final_msg["content"], accumulated_usage, llm_trace
                    except Exception:
                        log.warning("Failed to get final response after budget limit", exc_info=True)
                    return finish_reason, accumulated_usage, llm_trace

    except Exception as e:
        log.error(f"Loop error: {e}", exc_info=True)
        return f"⚠️ Loop error: {e}", accumulated_usage, llm_trace
