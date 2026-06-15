"""
DPC Agent — ComfyUI tools (comfyui_submit, comfyui_check, comfyui_wait, comfyui_convert).

HTTP client tools for submitting workflows to a local ComfyUI server
and retrieving results. Transport layer for Phase 1 Forge spike.
comfyui_convert wraps ffmpeg for WEBP→MP4 conversion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import subprocess
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


def _format_node_errors(node_errors: dict) -> str:
    """Format ComfyUI node_errors into a readable string for the agent."""
    lines = []
    for node_id, errs in node_errors.items():
        cls = errs.get("class_type", node_id)
        for err in errs.get("errors", []):
            msg = err.get("message", str(err))
            lines.append(f"  node {node_id} ({cls}): {msg}")
    return "\n".join(lines) if lines else str(node_errors)


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


# Node class_types whose widgets_values this converter knows how to map to
# named API inputs. A UI-format node OUTSIDE this set that carries
# widgets_values would silently lose them (e.g. LoadImage's image filename),
# producing an invalid graph. _convert_ui_to_api raises instead of dropping —
# the caller should export such workflows in API format ("Save (API Format)").
_HANDLED_WIDGET_TYPES = {
    "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
    "EmptyLatentImage", "EmptySD3LatentImage", "EmptyHunyuanLatentVideo",
    "KSampler", "SaveImage", "SaveAnimatedWEBP", "CreateVideo",
    "SaveVideo", "SaveWEBM",
}


def _convert_ui_to_api(wf: dict) -> dict:
    """Convert ComfyUI UI/graph format (nodes+links) to API prompt format.

    Raises ValueError if the graph contains node types whose widget values
    cannot be mapped (the converter only knows a fixed set; custom nodes like
    Bernini-R are unsupported). Callers should export those in API format.
    """
    nodes = wf.get("nodes", [])
    links = wf.get("links", [])
    if not nodes:
        return wf

    link_map = {}
    for link in links:
        link_id, src_node, src_slot = link[0], link[1], link[2]
        link_map[link_id] = (str(src_node), src_slot)

    api = {}
    unconvertible = set()
    for node in nodes:
        nid = str(node["id"])
        ct = node.get("class_type") or node.get("type", "")
        wv = node.get("widgets_values", [])
        inp_defs = node.get("inputs", [])

        if wv and ct not in _HANDLED_WIDGET_TYPES:
            unconvertible.add(ct)

        inputs = {}
        for inp in inp_defs:
            if isinstance(inp, dict) and inp.get("link") is not None:
                link_id = inp["link"]
                if link_id in link_map:
                    inputs[inp["name"]] = list(link_map[link_id])

        if ct == "UNETLoader":
            inputs["unet_name"] = wv[0] if len(wv) > 0 else ""
            inputs["weight_dtype"] = wv[1] if len(wv) > 1 else "default"
        elif ct == "CLIPLoader":
            inputs["clip_name"] = wv[0] if len(wv) > 0 else ""
            inputs["type"] = wv[1] if len(wv) > 1 else "stable_diffusion"
        elif ct == "VAELoader":
            inputs["vae_name"] = wv[0] if len(wv) > 0 else ""
        elif ct == "CLIPTextEncode":
            inputs["text"] = wv[0] if len(wv) > 0 else ""
        elif ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs["width"] = wv[0] if len(wv) > 0 else 512
            inputs["height"] = wv[1] if len(wv) > 1 else 512
            inputs["batch_size"] = wv[2] if len(wv) > 2 else 1
        elif ct == "EmptyHunyuanLatentVideo":
            inputs["width"] = wv[0] if len(wv) > 0 else 832
            inputs["height"] = wv[1] if len(wv) > 1 else 480
            inputs["length"] = wv[2] if len(wv) > 2 else 41
            inputs["batch_size"] = wv[3] if len(wv) > 3 else 1
        elif ct == "KSampler":
            inputs["seed"] = wv[0] if len(wv) > 0 else 0
            if len(wv) > 1 and isinstance(wv[1], str):
                inputs["control_after_generate"] = wv[1]
            inputs["steps"] = wv[2] if len(wv) > 2 else 20
            inputs["cfg"] = wv[3] if len(wv) > 3 else 7.0
            inputs["sampler_name"] = wv[4] if len(wv) > 4 else "euler"
            inputs["scheduler"] = wv[5] if len(wv) > 5 else "normal"
            inputs["denoise"] = wv[6] if len(wv) > 6 else 1.0
        elif ct == "SaveImage":
            inputs["filename_prefix"] = wv[0] if len(wv) > 0 else "ComfyUI"
        elif ct == "SaveAnimatedWEBP":
            inputs["filename_prefix"] = wv[0] if len(wv) > 0 else "ComfyUI"
            inputs["fps"] = wv[1] if len(wv) > 1 else 6.0
            inputs["lossless"] = wv[2] if len(wv) > 2 else True
            inputs["quality"] = wv[3] if len(wv) > 3 else 80
            inputs["method"] = wv[4] if len(wv) > 4 else "default"
        elif ct == "CreateVideo":
            inputs["fps"] = wv[0] if len(wv) > 0 else 8.0
        elif ct == "SaveVideo":
            inputs["filename_prefix"] = wv[0] if len(wv) > 0 else "video/ComfyUI"
            inputs["format"] = wv[1] if len(wv) > 1 else "auto"
            inputs["codec"] = wv[2] if len(wv) > 2 else "auto"
        elif ct == "SaveWEBM":
            inputs["filename_prefix"] = wv[0] if len(wv) > 0 else "ComfyUI"
            inputs["codec"] = wv[1] if len(wv) > 1 else "vp9"
            inputs["fps"] = wv[2] if len(wv) > 2 else 24.0
            inputs["crf"] = wv[3] if len(wv) > 3 else 32.0

        api[nid] = {"class_type": ct, "inputs": inputs}

    if unconvertible:
        raise ValueError(
            "UI-format workflow contains node types this tool cannot convert "
            "(their widget values would be lost): "
            + ", ".join(sorted(unconvertible))
            + ". Re-export it in API format via ComfyUI 'Save (API Format)' and "
            "submit that file (or pass it as workflow_json) — API-format graphs "
            "are submitted as-is with no lossy conversion."
        )

    return api


def _is_ui_format(wf: dict) -> bool:
    """Detect if workflow is in UI/graph format (has nodes array) vs API format."""
    return "nodes" in wf and isinstance(wf["nodes"], list)


def _resolve_workflow_path(ctx: ToolContext, workflow: str) -> Optional[pathlib.Path]:
    """Resolve a workflow filename to an existing path.

    Order: (1) absolute path as given, (2) path as given relative to cwd,
    (3) under <agent_root>/comfy-ui-workflows/. Returns None if not found.
    Absolute paths let agents reference workflows kept outside the sandbox
    (e.g. a project repo's comfy-workflows/ dir).
    """
    p = pathlib.Path(workflow)
    if p.is_absolute() and p.exists():
        return p
    if p.exists():
        return p
    if ctx.agent_root:
        candidate = pathlib.Path(ctx.agent_root) / "comfy-ui-workflows" / workflow
        if candidate.exists():
            return candidate
    return None


def comfyui_submit(ctx: ToolContext, workflow: str = "", prompt: str = "", workflow_json: dict = None, api_url: str = DEFAULT_API_URL) -> str:
    """Submit a ComfyUI workflow. Pass workflow filename + prompt, or raw workflow_json."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."

    wf_data = workflow_json if not workflow else None
    if workflow:
        wf_path = _resolve_workflow_path(ctx, workflow)
        if wf_path is None:
            return (
                f"Error: workflow file not found: {workflow} "
                f"(searched: absolute path, cwd, <agent_root>/comfy-ui-workflows/). "
                f"Pass an absolute path for workflows kept elsewhere."
            )
        try:
            wf_data = json.loads(wf_path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"Error reading workflow: {e}"

    if not wf_data:
        return "Error: provide either workflow (filename) or workflow_json (dict)."

    if _is_ui_format(wf_data):
        try:
            wf_data = _convert_ui_to_api(wf_data)
        except ValueError as e:
            return f"Error: {e}"

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
            data = resp.json()

            err = data.get("error")
            node_errs = data.get("node_errors")
            if err or (resp.status_code >= 400):
                parts = [f"Error: ComfyUI validation failed (HTTP {resp.status_code})"]
                if err:
                    parts.append(f"  {err.get('type', '')}: {err.get('message', str(err))}")
                if node_errs:
                    parts.append(_format_node_errors(node_errs))
                return "\n".join(parts)

            prompt_id = data.get("prompt_id")
            if not prompt_id:
                return f"Error: ComfyUI response missing prompt_id: {data}"

            result = f"Queued. prompt_id={prompt_id}"
            if node_errs:
                result += f"\nWarnings:\n{_format_node_errors(node_errs)}"
            return result
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
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


# Save-node class types whose filename_prefix is the most human-readable
# label for a queued job (e.g. "C2_ship_approaching"). Checked in order of
# preference is not needed — any match wins, one save node per graph in practice.
_SAVE_NODE_TYPES = {
    "BerniniRSaveVideo", "SaveVideo", "CreateVideo", "SaveWEBM",
    "SaveAnimatedWEBP", "VHS_VideoCombine", "SaveImage",
}


def _queue_item_label(item) -> tuple:
    """From a /queue entry ``[number, prompt_id, prompt, ...]`` return
    ``(prompt_id, label)`` where label is a save node's filename_prefix.

    Returns ('', '') on any malformed item — the queue display degrades to
    counts rather than raising. The full prompt_id is preserved (not
    truncated) so the agent can feed it straight into comfyui_check/_wait.
    """
    prompt_id = ""
    label = ""
    try:
        if isinstance(item, (list, tuple)) and len(item) > 1:
            prompt_id = str(item[1])
        prompt = item[2] if isinstance(item, (list, tuple)) and len(item) > 2 else None
        if isinstance(prompt, dict):
            for node in prompt.values():
                if not isinstance(node, dict):
                    continue
                if node.get("class_type") in _SAVE_NODE_TYPES:
                    fp = node.get("inputs", {}).get("filename_prefix")
                    if fp:
                        label = str(fp)
                        break
    except Exception:
        pass
    return prompt_id, label


async def _peek_progress_step(api_url: str, timeout: float = 3.0) -> str:
    """Best-effort: grab the latest progress step of the running job via WS.

    Returns 'step X/Y' or '' (never raises). Bounded by ``timeout`` so a
    queue poll stays fast even if the WS is unresponsive.
    """
    import websockets

    ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url.rstrip('/')}/ws?clientId=dpc-forge-queuepeek"
    latest = None
    try:
        async with websockets.connect(ws_url, close_timeout=2) as ws:
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 1.5))
                    d = json.loads(raw)
                    if d.get("type") == "progress":
                        md = d.get("data", {})
                        latest = (md.get("value", 0), md.get("max", 0))
                except asyncio.TimeoutError:
                    if latest is not None:
                        break
                    continue
    except Exception:
        return ""
    return f"step {latest[0]}/{latest[1]}" if latest else ""


def comfyui_queue_status(ctx: ToolContext, api_url: str = DEFAULT_API_URL) -> str:
    """Check ComfyUI queue — counts plus what each task actually is.

    Reports the running/pending counts and, for every queued job, its
    prompt_id and label (the save node's filename_prefix, e.g.
    'C2_ship_approaching'), so the agent knows exactly what is rendering
    instead of guessing from bare counts. The running job is annotated with
    a live 'step X/Y' progress sample when available.
    """
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available."

    async def _status():
        try:
            client = _get_client()
            resp = await client.get(f"{api_url.rstrip('/')}/queue")
            resp.raise_for_status()
            data = resp.json()
            running = data.get("queue_running", []) or []
            pending = data.get("queue_pending", []) or []
            lines = [f"running={len(running)}, pending={len(pending)}"]
            step_txt = await _peek_progress_step(api_url) if running else ""
            for i, item in enumerate(running):
                pid, label = _queue_item_label(item)
                extra = f"  [{step_txt}]" if (i == 0 and step_txt) else ""
                lines.append(f"  RUNNING  {pid or '?'}  {label or '(unlabeled)'}{extra}")
            for item in pending:
                pid, label = _queue_item_label(item)
                lines.append(f"  PENDING  {pid or '?'}  {label or '(unlabeled)'}")
            return "\n".join(lines)
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}."
        except Exception as e:
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_status(), loop)
    return future.result(timeout=15)


# Last progress sample per api_url: (step, max, loop_time). Lets a poll
# estimate sec/iteration + ETA across calls when a single snapshot only
# catches one step (common on slow renders). Loop time is monotonic and
# comparable across calls because the agent reuses one event loop.
# Module-level and keyed only by api_url: two agents polling the same URL
# concurrently could race here — acceptable while one agent drives renders;
# revisit (per-agent state) if concurrent generation lands.
_LAST_PROGRESS: dict = {}


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec}s" if sec < 90 else f"{sec // 60}m{sec % 60:02d}s"


def _format_progress_timing(samples: list, api_url: str) -> str:
    """Build a 'timing:' line (step, sec/it, ETA) from progress samples.

    ``samples`` are (step, max, loop_time) tuples collected this call.
    Rate comes from within-call samples when >=2 are seen, else from the
    last stored sample across calls. Returns '' when nothing is known.
    """
    if not samples:
        return ""
    cur_step, cur_max, cur_t = samples[-1]
    rate = None  # seconds per step
    if len(samples) >= 2:
        s0, _, t0 = samples[0]
        if cur_step - s0 > 0:
            rate = (cur_t - t0) / (cur_step - s0)
    if rate is None:
        prev = _LAST_PROGRESS.get(api_url)
        if prev:
            p_step, _, p_t = prev
            if cur_step - p_step > 0 and cur_t - p_t > 0:
                rate = (cur_t - p_t) / (cur_step - p_step)
    _LAST_PROGRESS[api_url] = (cur_step, cur_max, cur_t)
    parts = [f"step {cur_step}/{cur_max}"]
    if rate:
        eta = max(0, (cur_max or 0) - cur_step) * rate
        parts.append(f"~{rate:.1f}s/it")
        parts.append(f"ETA ~{_fmt_dur(eta)}")
    else:
        parts.append("(poll again in a few s for s/it & ETA)")
    return "timing: " + " | ".join(parts)


def comfyui_progress(ctx: ToolContext, timeout: int = 10, api_url: str = DEFAULT_API_URL) -> str:
    """One-shot WebSocket snapshot of ComfyUI generation progress."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available."
    timeout = max(3, min(timeout, 30))

    async def _progress():
        import websockets
        ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url.rstrip('/')}/ws?clientId=dpc-forge-progress"
        events = []
        prog_samples = []
        try:
            async with websockets.connect(ws_url, close_timeout=3) as ws:
                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
                        data = json.loads(raw)
                        msg_type = data.get("type", "")
                        msg_data = data.get("data", {})
                        if msg_type == "progress":
                            val = msg_data.get("value", 0)
                            mx = msg_data.get("max", 0)
                            prog_samples.append((val, mx, asyncio.get_event_loop().time()))
                            events.append(f"progress: step {val}/{mx}")
                        elif msg_type == "executing":
                            node = msg_data.get("node", "")
                            if node:
                                events.append(f"executing: node {node}")
                            else:
                                events.append("executing: done (all nodes finished)")
                                break
                        elif msg_type == "executed":
                            events.append(f"executed: node {msg_data.get('node', '?')}")
                        elif msg_type == "execution_error":
                            events.append(f"error: {msg_data.get('exception_message', str(msg_data))}")
                            break
                        elif msg_type == "status":
                            q = msg_data.get("status", {}).get("exec_info", {})
                            remaining_q = q.get("queue_remaining", 0)
                            events.append(f"queue_remaining={remaining_q}")
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            if not events:
                return f"Error: cannot connect to ComfyUI WebSocket: {e}"
            events.append(f"(connection closed: {e})")

        timing = _format_progress_timing(prog_samples, api_url)
        if timing:
            events.append(timing)
        if not events:
            return "No activity detected (ComfyUI idle or timeout too short)."
        return "\n".join(events)

    future = asyncio.run_coroutine_threadsafe(_progress(), loop)
    return future.result(timeout=timeout + 5)


def comfyui_convert(ctx: ToolContext, input_path: str, output_path: str = "", fps: int = 16, codec: str = "libx264") -> str:
    """Convert ComfyUI animated WEBP output to MP4 via Pillow frame extraction + ffmpeg."""
    import shutil
    import tempfile

    src = pathlib.Path(input_path)
    if not src.exists():
        return f"Error: input file not found: {input_path}"

    if not output_path:
        output_path = str(src.with_suffix(".mp4"))
    dst = pathlib.Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = None
    try:
        from PIL import Image
        img = Image.open(str(src))
        n_frames = getattr(img, "n_frames", 1)
        if n_frames < 2:
            return "Error: input is not an animated WEBP (single frame)."

        tmp_dir = tempfile.mkdtemp(prefix="comfyui_convert_")
        for i in range(n_frames):
            img.seek(i)
            frame = img.convert("RGB")
            frame.save(pathlib.Path(tmp_dir) / f"frame_{i:05d}.png")

        pattern = str(pathlib.Path(tmp_dir) / "frame_%05d.png")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", pattern,
            "-c:v", codec,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(dst),
        ]
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        if result.returncode != 0:
            stderr = result.stderr[-500:] if result.stderr else ""
            return f"Error: ffmpeg exit {result.returncode}: {stderr}"
        if not dst.exists():
            return "Error: ffmpeg completed but output file not found."
        size_mb = dst.stat().st_size / (1024 * 1024)
        return f"Done. output={dst}, size={size_mb:.1f}MB, frames={n_frames}, fps={fps}"
    except ImportError:
        return "Error: Pillow not installed (pip install Pillow)."
    except FileNotFoundError:
        return "Error: ffmpeg not found in PATH."
    except subprocess.TimeoutExpired:
        return "Error: ffmpeg timed out after 120s."
    except Exception as e:
        return f"Error: {e}"
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="comfyui_submit",
            schema={
                "name": "comfyui_submit",
                "description": (
                    "Submit a ComfyUI workflow for generation. "
                    "Pass workflow (filename or absolute path) + prompt (text); the tool "
                    "reads the file, injects prompt into the positive CLIPTextEncode node, submits. "
                    "For workflows with custom nodes (e.g. Bernini-R) export them in API format "
                    "via ComfyUI 'Save (API Format)' — API-format graphs are submitted as-is "
                    "(no lossy UI->API conversion). UI-format graphs with unsupported custom "
                    "nodes are rejected with a clear message rather than silently mangled. "
                    "Alternative: pass workflow_json directly (a dict already in API format)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": (
                                "Workflow JSON filename resolved under <agent_root>/comfy-ui-workflows/, "
                                "OR an absolute path to a workflow kept elsewhere "
                                "(e.g. a project repo's comfy-workflows/ dir)."
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Text prompt to inject into the positive CLIPTextEncode node.",
                        },
                        "workflow_json": {
                            "type": "object",
                            "description": "Raw ComfyUI workflow JSON in API format (submitted as-is).",
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
        ToolEntry(
            name="comfyui_progress",
            schema={
                "name": "comfyui_progress",
                "description": (
                    "One-shot WebSocket snapshot of ComfyUI generation progress. "
                    "Shows current step (e.g. step 5/20), which node is executing, "
                    "queue status, and errors. Use during long generations to check status. "
                    "Non-blocking — reads available events within timeout and returns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "integer",
                            "description": "Seconds to listen for events (default 10, max 30). Longer = more events captured.",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": [],
                },
            },
            handler=comfyui_progress,
            is_code_tool=False,
            timeout_sec=35,
            is_core=False,
            default_enabled=False,
        ),
        ToolEntry(
            name="comfyui_convert",
            schema={
                "name": "comfyui_convert",
                "description": (
                    "Convert ComfyUI animated WEBP output to MP4 via ffmpeg (fallback). "
                    "Use only if SaveVideo node is unavailable. Preferred path: SaveVideo in workflow. "
                    "Uses libx264 + yuv420p for universal compatibility."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input_path": {
                            "type": "string",
                            "description": "Path to animated WEBP file from ComfyUI output.",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Path for MP4 output. Default: same name with .mp4 extension.",
                        },
                        "fps": {
                            "type": "integer",
                            "description": "Output framerate (default: 16, matching ComfyUI SaveAnimatedWEBP).",
                        },
                        "codec": {
                            "type": "string",
                            "description": "Video codec (default: libx264). Use libx265 for smaller files.",
                        },
                    },
                    "required": ["input_path"],
                },
            },
            handler=comfyui_convert,
            is_code_tool=False,
            timeout_sec=120,
            is_core=False,
            default_enabled=False,
        ),
    ]
