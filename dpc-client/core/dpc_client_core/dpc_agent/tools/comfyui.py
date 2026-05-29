"""
DPC Agent — ComfyUI tools (comfyui_submit, comfyui_check, comfyui_wait).

HTTP client tools for submitting workflows to a local ComfyUI server
and retrieving results. Transport layer for Phase 1 Forge spike.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
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


def comfyui_submit(ctx: ToolContext, workflow: str = "", prompt: str = "", workflow_json: dict = None, api_url: str = DEFAULT_API_URL) -> str:
    """Submit a ComfyUI workflow. Pass workflow filename + prompt, or raw workflow_json."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."

    wf_data = workflow_json if not workflow else None
    if workflow:
        wf_dir = pathlib.Path(ctx.agent_root) / "comfy-ui-workflows" if ctx.agent_root else None
        if not wf_dir:
            return "Error: agent_root not set, cannot resolve workflow path."
        wf_path = wf_dir / workflow
        if not wf_path.exists():
            return f"Error: workflow file not found: {wf_path}"
        try:
            wf_data = json.loads(wf_path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"Error reading workflow: {e}"

    if not wf_data:
        return "Error: provide either workflow (filename) or workflow_json (dict)."

    if prompt:
        for node in wf_data.values():
            if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode":
                inputs = node.get("inputs", {})
                if inputs.get("text") and "negative" not in str(node.get("_meta", {}).get("title", "")).lower():
                    inputs["text"] = prompt
                    break

    async def _submit():
        wf_str_keys = {str(k): v for k, v in wf_data.items()}
        payload: Dict[str, Any] = {"prompt": wf_str_keys}
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


def comfyui_queue_status(ctx: ToolContext, api_url: str = DEFAULT_API_URL) -> str:
    """Check ComfyUI queue status — how many tasks pending/running."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available."

    async def _status():
        try:
            client = _get_client()
            resp = await client.get(f"{api_url.rstrip('/')}/queue")
            resp.raise_for_status()
            data = resp.json()
            running = len(data.get("queue_running", []))
            pending = len(data.get("queue_pending", []))
            return f"running={running}, pending={pending}"
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}."
        except Exception as e:
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_status(), loop)
    return future.result(timeout=10)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="comfyui_submit",
            schema={
                "name": "comfyui_submit",
                "description": (
                    "Submit a ComfyUI workflow for generation. "
                    "Preferred: pass workflow (filename from comfy-ui-workflows/) + prompt (text). "
                    "Tool reads file, injects prompt into CLIPTextEncode node, submits to ComfyUI. "
                    "Alternative: pass workflow_json directly (legacy, error-prone)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": "Workflow JSON filename from comfy-ui-workflows/ dir (e.g. 'wan22_ti2v_5B.json').",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Text prompt to inject into the positive CLIPTextEncode node.",
                        },
                        "workflow_json": {
                            "type": "object",
                            "description": "Raw ComfyUI workflow JSON (legacy — prefer workflow + prompt).",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": [],
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
        ToolEntry(
            name="comfyui_queue_status",
            schema={
                "name": "comfyui_queue_status",
                "description": (
                    "Check ComfyUI queue status — running and pending task counts. "
                    "Use before comfyui_submit to avoid OOM from concurrent generation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": [],
                },
            },
            handler=comfyui_queue_status,
            is_code_tool=False,
            timeout_sec=10,
            is_core=False,
            default_enabled=False,
        ),
    ]
