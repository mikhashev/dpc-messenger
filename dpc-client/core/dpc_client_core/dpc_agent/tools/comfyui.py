"""
DPC Agent — ComfyUI tools (comfyui_submit, comfyui_check, comfyui_wait).

HTTP client tools for submitting workflows to a local ComfyUI server
and retrieving results. Transport layer for Phase 1 Forge spike.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

import httpx

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8188"
POLL_INTERVAL_SEC = 3
POLL_MAX_ITERATIONS = 120

_client: Optional[httpx.AsyncClient] = None
_client_lock = threading.Lock()


def _get_client() -> httpx.AsyncClient:
    """Lazy-init module-level httpx client for connection reuse."""
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=30.0)
        return _client


def _collect_outputs(entry: dict) -> List[str]:
    """Extract all output file paths from a ComfyUI history entry."""
    outputs = []
    for _node_id, node_output in entry.get("outputs", {}).items():
        for key in ("images", "videos", "audio", "gifs"):
            for item in node_output.get(key, []):
                filename = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                if subfolder:
                    outputs.append(f"{subfolder}/{filename}")
                else:
                    outputs.append(filename)
    return outputs


def comfyui_submit(ctx: ToolContext, workflow_json: dict, api_url: str = DEFAULT_API_URL) -> str:
    """Submit a ComfyUI workflow JSON for queued execution."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."

    async def _submit():
        prompt = {str(k): v for k, v in workflow_json.items()}
        payload: Dict[str, Any] = {"prompt": prompt}
        try:
            client = _get_client()
            resp = await client.post(f"{api_url.rstrip('/')}/prompt", json=payload)
            resp.raise_for_status()
            data = resp.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                return f"Error: ComfyUI response missing prompt_id: {data}"
            return f"Queued. prompt_id={prompt_id}"
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            return f"Error: ComfyUI returned {e.response.status_code}: {body}"
        except Exception as e:
            log.warning("comfyui_submit failed: %s", e)
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_submit(), loop)
    return future.result(timeout=35)


def comfyui_check(ctx: ToolContext, prompt_id: str, api_url: str = DEFAULT_API_URL) -> str:
    """Single non-blocking status check. Returns pending/done/error immediately."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."

    async def _check():
        try:
            client = _get_client()
            resp = await client.get(f"{api_url.rstrip('/')}/history/{prompt_id}")
            resp.raise_for_status()
            data = resp.json()
            if prompt_id not in data:
                return "pending"

            entry = data[prompt_id]
            status_info = entry.get("status", {})
            status_str = status_info.get("status_str", "")

            if status_str == "error":
                msgs = status_info.get("messages", [])
                return f"Error: workflow failed: {msgs}"

            if status_str != "success":
                return f"pending (status={status_str})"

            outputs = _collect_outputs(entry)
            if not outputs:
                return "Done but no output files found in workflow result."
            return "Done. outputs=" + ", ".join(outputs)

        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
        except Exception as e:
            log.warning("comfyui_check failed: %s", e)
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_check(), loop)
    return future.result(timeout=15)


def comfyui_wait(ctx: ToolContext, prompt_id: str, timeout: int = 300, api_url: str = DEFAULT_API_URL) -> str:
    """Blocking wait for workflow completion. Agent controls timeout."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."
    timeout = max(30, min(timeout, 600))
    max_iters = timeout // POLL_INTERVAL_SEC

    async def _wait():
        try:
            client = _get_client()
            for i in range(max_iters):
                resp = await client.get(f"{api_url.rstrip('/')}/history/{prompt_id}")
                resp.raise_for_status()
                data = resp.json()
                if prompt_id not in data:
                    if i < max_iters - 1:
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue
                    return f"Error: timeout waiting for ComfyUI ({timeout}s)."

                entry = data[prompt_id]
                status_info = entry.get("status", {})
                status_str = status_info.get("status_str", "")

                if status_str == "error":
                    msgs = status_info.get("messages", [])
                    return f"Error: workflow failed: {msgs}"

                if status_str != "success":
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                outputs = _collect_outputs(entry)
                if not outputs:
                    return "Done but no output files found in workflow result."
                return "Done. outputs=" + ", ".join(outputs)

        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
        except Exception as e:
            log.warning("comfyui_wait failed: %s", e)
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_wait(), loop)
    return future.result(timeout=timeout + 10)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="comfyui_submit",
            schema={
                "name": "comfyui_submit",
                "description": (
                    "Submit a ComfyUI workflow JSON to the local ComfyUI HTTP API. "
                    "Returns a prompt_id for checking status. ComfyUI must be running."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workflow_json": {
                            "type": "object",
                            "description": "ComfyUI workflow as a JSON object (node graph).",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": ["workflow_json"],
                },
            },
            handler=comfyui_submit,
            is_code_tool=False,
            timeout_sec=35,
            is_core=False,
            default_enabled=False,
        ),
        ToolEntry(
            name="comfyui_check",
            schema={
                "name": "comfyui_check",
                "description": (
                    "Non-blocking single check of ComfyUI workflow status. "
                    "Returns 'pending', 'Done. outputs=...' or 'Error: ...'. "
                    "Call repeatedly to poll, or use comfyui_wait for blocking."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt_id": {
                            "type": "string",
                            "description": "The prompt_id returned by comfyui_submit.",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": ["prompt_id"],
                },
            },
            handler=comfyui_check,
            is_code_tool=False,
            timeout_sec=15,
            is_core=False,
            default_enabled=False,
        ),
        ToolEntry(
            name="comfyui_wait",
            schema={
                "name": "comfyui_wait",
                "description": (
                    "Blocking wait for ComfyUI workflow completion. "
                    "Returns output filenames on success. "
                    "Use timeout parameter to control wait time (default 300s, max 600s). "
                    "Use comfyui_check for non-blocking status checks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt_id": {
                            "type": "string",
                            "description": "The prompt_id returned by comfyui_submit.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max seconds to wait (default 300, min 30, max 600). Use 180 for warm start, 420 for cold/heavy tasks.",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": ["prompt_id"],
                },
            },
            handler=comfyui_wait,
            is_code_tool=False,
            timeout_sec=610,
            is_core=False,
            default_enabled=False,
        ),
    ]
