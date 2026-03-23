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

import asyncio
import gc
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

# Shared ThreadPoolExecutor for tool execution (fixes memory leak from creating new executors)
# Using max_workers=4 allows parallel tool execution while limiting resource usage
_SHARED_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _get_shared_executor() -> ThreadPoolExecutor:
    """Get or create the shared ThreadPoolExecutor for tool execution."""
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is None:
        _SHARED_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dpc_agent_tool")
        log.debug("Created shared ThreadPoolExecutor for tool execution")
    return _SHARED_EXECUTOR


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


async def _execute_with_timeout(
    tools: "ToolRegistry",
    tc: Dict[str, Any],
    logs_dir: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
) -> Dict[str, Any]:
    """Execute a tool call with a hard timeout using shared executor.

    Uses asyncio.run_in_executor to avoid blocking the event loop,
    allowing other async operations (like handling other chats) to proceed.
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]

    # Use shared executor to avoid memory leak from creating new executors
    executor = _get_shared_executor()
    loop = asyncio.get_running_loop()

    try:
        # Use run_in_executor to avoid blocking the event loop
        result = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                _execute_single_tool,
                tools, tc, logs_dir, task_id
            ),
            timeout=timeout_sec
        )
        return result
    except asyncio.TimeoutError:
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
        # Don't shutdown the shared executor - it will be reused
        # Force garbage collection after each tool execution to prevent memory accumulation
        gc.collect()


async def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: "ToolRegistry",
    llm: DpcLlmAdapter,
    agent_root: pathlib.Path,
    emit_progress: Callable[[str, Optional[str], Optional[int]], None],
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    on_stream_chunk: Optional[Callable[[str, str], None]] = None,
    conversation_id: Optional[str] = None,
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
        on_stream_chunk: Optional async callback for streaming: await on_stream_chunk(chunk, conversation_id)
        conversation_id: Optional conversation ID for streaming callbacks

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
        "prompt_tokens": 0,        # cumulative across all rounds (for cost/billing)
        "first_prompt_tokens": 0,  # round-1 only — baseline context before tool results inflate it
        "last_prompt_tokens": 0,   # last round only — peak context size during this response
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "rounds": 0,
    }

    # Get tool schemas (use firewall whitelist, not just core tools)
    tool_schemas = tools.schemas(core_only=False, include_restricted=False)

    # Deduplication: track (tool_name, args_hash) → call count.
    # If the same call is repeated MAX_DUPLICATE_CALLS times without new information,
    # break the loop to prevent infinite repetition.
    MAX_DUPLICATE_CALLS = 5
    MAX_TOOL_CALLS_PER_TURN = 25  # Hard cap: if LLM emits more than this in one shot, it's looping
    _tool_call_counts: Dict[str, int] = {}

    round_idx = 0
    try:
        while True:
            round_idx += 1

            # Hard limit on rounds
            if round_idx > max_rounds:
                finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({max_rounds}). Consider breaking into smaller tasks."
                messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
                try:
                    final_msg, _ = await llm.chat(
                        messages,
                        tools=tool_schemas,
                        on_stream_chunk=on_stream_chunk,
                        conversation_id=conversation_id,
                    )
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
                msg, usage = await llm.chat(
                    messages,
                    tools=tool_schemas,
                    on_stream_chunk=on_stream_chunk,
                    conversation_id=conversation_id,
                )
                round_prompt_tokens = usage.get("prompt_tokens", 0)
                accumulated_usage["prompt_tokens"] += round_prompt_tokens
                if accumulated_usage["rounds"] == 0:  # first round, before increment
                    accumulated_usage["first_prompt_tokens"] = round_prompt_tokens
                accumulated_usage["last_prompt_tokens"] = round_prompt_tokens  # replace — tracks peak context
                accumulated_usage["completion_tokens"] += usage.get("completion_tokens", 0)
                accumulated_usage["total_tokens"] += usage.get("total_tokens", 0)
                accumulated_usage["cost"] += usage.get("cost", 0)
                accumulated_usage["rounds"] += 1
                # Carry forward thinking from each round (last non-empty thinking wins)
                if msg.get("thinking"):
                    accumulated_usage["thinking"] = msg["thinking"]
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
                if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
                    log.warning(
                        "LLM generated %d tool calls in one turn (limit: %d) — injecting stop signal",
                        len(tool_calls), MAX_TOOL_CALLS_PER_TURN,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[TOOL_LIMIT] You generated {len(tool_calls)} tool calls in a single turn, "
                            f"which exceeds the limit of {MAX_TOOL_CALLS_PER_TURN}. "
                            "Stop calling tools. Summarise what you know and give your final answer now."
                        ),
                    })
                    try:
                        final_msg, _ = await llm.chat(
                            messages,
                            tools=None,
                            on_stream_chunk=on_stream_chunk,
                            conversation_id=conversation_id,
                        )
                        if final_msg and final_msg.get("content"):
                            return final_msg["content"], accumulated_usage, llm_trace
                    except Exception:
                        log.warning("Failed to get response after tool-call-per-turn limit", exc_info=True)
                    return (
                        f"⚠️ Agent generated too many tool calls ({len(tool_calls)}) in one turn and was stopped.",
                        accumulated_usage,
                        llm_trace,
                    )
            elif content and "tool_call" in content.lower():
                log.warning(f"No tool_calls parsed but 'tool_call' found in content: {content[:200]!r}")

            # No tool calls — final response or empty-response retry
            if not tool_calls:
                if content and content.strip():
                    llm_trace["assistant_notes"].append(content.strip()[:320])
                    return content, accumulated_usage, llm_trace
                # LLM returned empty content (e.g. GLM thinking-only with no text).
                # Inject a re-prompt and retry once so the user gets a real answer.
                if round_idx == 1:
                    log.warning("LLM returned empty content with no tool calls — re-prompting for text response")
                    messages.append({
                        "role": "user",
                        "content": "Please provide your answer as text. Your previous response was empty.",
                    })
                    continue
                return "", accumulated_usage, llm_trace

            # Process tool calls
            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

            if content and content.strip():
                emit_progress(content.strip(), None, round_idx)
                llm_trace["assistant_notes"].append(content.strip()[:320])

            # Check for duplicate tool calls before executing.
            # If the same (tool, args) fingerprint has appeared MAX_DUPLICATE_CALLS
            # times the model is stuck in a loop — inject a stop signal.
            stuck_tools = []
            for tc in tool_calls:
                try:
                    raw_args = tc["function"].get("arguments", {})
                    args_key = json.dumps(raw_args, sort_keys=True) if isinstance(raw_args, dict) else str(raw_args)
                    fingerprint = f"{tc['function']['name']}::{args_key}"
                    _tool_call_counts[fingerprint] = _tool_call_counts.get(fingerprint, 0) + 1
                    if _tool_call_counts[fingerprint] >= MAX_DUPLICATE_CALLS:
                        stuck_tools.append(tc["function"]["name"])
                except Exception:
                    pass

            if stuck_tools:
                dedup_names = ", ".join(sorted(set(stuck_tools)))
                log.warning(
                    "Duplicate tool call limit reached for: %s — injecting stop signal", dedup_names
                )
                messages.append({
                    "role": "system",
                    "content": (
                        f"[LOOP_GUARD] You have called the following tool(s) with identical arguments "
                        f"{MAX_DUPLICATE_CALLS} or more times without new information: {dedup_names}. "
                        "Stop repeating these calls. Summarise what you know so far and give your final answer now."
                    )
                })
                try:
                    final_msg, _ = await llm.chat(
                        messages,
                        tools=None,
                        on_stream_chunk=on_stream_chunk,
                        conversation_id=conversation_id,
                    )
                    if final_msg and final_msg.get("content"):
                        return final_msg["content"], accumulated_usage, llm_trace
                except Exception:
                    log.warning("Failed to get response after loop guard", exc_info=True)
                return (
                    f"⚠️ Agent loop stopped: repeated calls to {dedup_names} without progress.",
                    accumulated_usage,
                    llm_trace,
                )

            # Execute tool calls
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                emit_progress(f"Executing {tool_name}...", tool_name, round_idx)
                timeout = tools.get_timeout(tc["function"]["name"])
                exec_result = await _execute_with_timeout(tools, tc, logs_dir, timeout, task_id)

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
                        final_msg, _ = await llm.chat(
                            messages,
                            tools=None,
                            on_stream_chunk=on_stream_chunk,
                            conversation_id=conversation_id,
                        )
                        if final_msg.get("content"):
                            return final_msg["content"], accumulated_usage, llm_trace
                    except Exception:
                        log.warning("Failed to get final response after budget limit", exc_info=True)
                    return finish_reason, accumulated_usage, llm_trace

    except Exception as e:
        log.error(f"Loop error: {e}", exc_info=True)
        return f"⚠️ Loop error: {e}", accumulated_usage, llm_trace
