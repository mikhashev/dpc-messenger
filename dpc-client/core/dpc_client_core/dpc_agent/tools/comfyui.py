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
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional, Tuple

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


# ── /object_info schema (drives generic UI->API conversion) ──────────────────
# object_info is large (~MBs) and only changes when custom nodes are added or
# ComfyUI restarts, so memoize per api_url for the life of the process.
_OBJECT_INFO_CACHE: Dict[str, dict] = {}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


async def _fetch_object_info(client: httpx.AsyncClient, api_url: str) -> dict:
    """Fetch + memoize ComfyUI /object_info (map class_type -> node schema).

    Returns {} on failure so callers can fall back to the whitelist converter.
    """
    key = api_url.rstrip("/")
    cached = _OBJECT_INFO_CACHE.get(key)
    if cached:
        return cached
    try:
        resp = await client.get(f"{key}/object_info", timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data:
            _OBJECT_INFO_CACHE[key] = data
            return data
    except Exception as e:  # noqa: BLE001 - degraded mode, not fatal
        log.warning("comfyui _fetch_object_info failed: %s", e)
    return {}


async def _fetch_logs(client: httpx.AsyncClient, api_url: str, keyword: str = "", max_lines: int = 100) -> str:
    """Fetch ComfyUI server logs (GET /internal/logs), filter, tail, strip ANSI."""
    resp = await client.get(f"{api_url.rstrip('/')}/internal/logs")
    resp.raise_for_status()
    text = resp.text
    lines: List[str] = []
    try:
        parsed = resp.json()
        if isinstance(parsed, str):
            lines = parsed.split("\n")
        elif isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
            lines = [e.get("m", "") if isinstance(e, dict) else str(e) for e in parsed["entries"]]
        else:
            lines = text.split("\n")
    except Exception:  # noqa: BLE001 - non-JSON body, treat as raw text
        lines = text.split("\n")
    lines = [ln for ln in lines if ln]
    if keyword:
        kw = keyword.lower()
        lines = [ln for ln in lines if kw in ln.lower()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(_ANSI_RE.sub("", ln) for ln in lines)


# ── Schema-driven widget/link classification ─────────────────────────────────
# A COMBO input's type-spec[0] is a list of choices, caught by the isinstance
# check below. Scalar/upload widget types are named strings. KNOWN GAP: an
# exotic custom widget type whose type-spec[0] is a string NOT in this set would
# be misread as a link. IMAGEUPLOAD (LoadImage file picker) is included; if a
# future node uses another bare widget-type string, add it here.
_WIDGET_SCALAR_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "IMAGEUPLOAD"})
_SKIP_UI_TYPES = frozenset({"Reroute", "Note", "MarkdownNote", "PrimitiveNode"})


def _input_spec(node_def: dict, name: str):
    inp = node_def.get("input", {}) or {}
    return (inp.get("required") or {}).get(name) or (inp.get("optional") or {}).get(name)


def _is_widget_input(spec) -> bool:
    """A ComfyUI input is a widget (value) when its type-spec[0] is a list of
    choices (COMBO) or a scalar widget type; otherwise it's a link (MODEL/CLIP/…)."""
    if not spec:
        return False
    type_spec = spec[0]
    if isinstance(type_spec, list):
        return True
    return type_spec in _WIDGET_SCALAR_TYPES


def _has_control_after_generate(spec) -> bool:
    """seed/noise_seed carry a phantom 'fixed'/'randomize' entry in widgets_values
    that maps to no named input — detect it so we can skip that slot."""
    if not spec or len(spec) < 2 or not isinstance(spec[1], dict):
        return False
    return spec[1].get("control_after_generate") is True


def _ordered_input_names(node_def: dict) -> List[str]:
    names: List[str] = []
    order = node_def.get("input_order")
    if order:
        names += list(order.get("required", []) or [])
        names += list(order.get("optional", []) or [])
    else:
        inp = node_def.get("input", {}) or {}
        names += list((inp.get("required") or {}).keys())
        names += list((inp.get("optional") or {}).keys())
    return names


def _convert_ui_to_api_schema(wf: dict, object_info: dict) -> Tuple[dict, List[str]]:
    """Generic UI/graph -> API conversion driven by /object_info.

    Unlike the fixed-whitelist _convert_ui_to_api, this maps ANY installed node
    (incl. custom nodes) by reading its input schema: each input is classified
    widget-vs-link from object_info, widgets_values are mapped positionally to the
    widget inputs (skipping the phantom control_after_generate slot), and linked
    inputs come from the node's inputs[] array. Nodes absent from object_info are
    warned + skipped (never raised). Returns (api_graph, warnings).
    """
    nodes = wf.get("nodes", [])
    links = wf.get("links", [])
    if not nodes:
        return wf, []

    link_map: Dict[int, Tuple[str, int]] = {}
    for link in links:
        if isinstance(link, list) and len(link) >= 3:
            link_map[link[0]] = (str(link[1]), link[2])

    node_ids = {str(n.get("id")) for n in nodes}
    api: Dict[str, Any] = {}
    warnings: List[str] = []

    for node in nodes:
        nid = str(node.get("id"))
        ct = node.get("class_type") or node.get("type", "")
        if not ct or ct in _SKIP_UI_TYPES:
            continue
        node_def = object_info.get(ct)
        if not node_def:
            warnings.append(
                f"node {nid} ({ct}): not in /object_info — custom node not installed? skipped"
            )
            continue

        inputs: Dict[str, Any] = {}

        # Linked inputs first (by name), so a widget converted-to-input is detected below.
        for inp in node.get("inputs", []) or []:
            if isinstance(inp, dict) and inp.get("link") is not None:
                li = link_map.get(inp["link"])
                if li and li[0] in node_ids:
                    inputs[inp["name"]] = [li[0], li[1]]

        # Widget inputs positionally from widgets_values, skipping any already wired
        # as a link (converted-to-input widget carries no widgets_values slot).
        wv = node.get("widgets_values", []) or []
        wi = 0
        for name in _ordered_input_names(node_def):
            spec = _input_spec(node_def, name)
            if not _is_widget_input(spec):
                continue
            if name in inputs:  # converted to input (linked) — no widget value present
                continue
            if wi >= len(wv):
                break
            inputs[name] = wv[wi]
            wi += 1
            if _has_control_after_generate(spec) and wi < len(wv):
                wi += 1  # skip phantom control_after_generate value
        # NB: leftover widgets_values here is NOT a reliable "unknown widget type"
        # signal — a widget converted-to-input keeps its stale default in
        # widgets_values while we (correctly) take the link, so the slot is left
        # over benignly. The _WIDGET_SCALAR_TYPES known-gap stays a documented
        # limitation rather than a noisy runtime warning.

        api[nid] = {"class_type": ct, "inputs": inputs}
        title = node.get("title") or (node.get("_meta") or {}).get("title")
        if title and title != ct:
            api[nid]["_meta"] = {"title": title}

    return api, warnings


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


# Source-node fields (in priority order) that hold the prompt text when the
# positive CLIPTextEncode's `text` is fed by a link (e.g. SimplePromptBatcher).
_PROMPT_SOURCE_FIELDS = ("prompts", "text", "prompt", "string", "value")


def _inject_prompt(graph: dict, prompt: str) -> Optional[str]:
    """Inject `prompt` into the positive prompt source of an API-format graph.

    Handles two shapes:
    - positive CLIPTextEncode.text is a direct widget string -> set it;
    - positive CLIPTextEncode.text is a link -> follow one hop to the source node
      (SimplePromptBatcher, a text/primitive node, …) and set its first string
      field named prompts/text/prompt/string/value.

    Returns a warning string if the prompt could not be placed, else None.
    """
    target = None
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode":
            title = str((node.get("_meta") or {}).get("title", "")).lower()
            if "negative" in title:
                continue
            target = node
            break
    if target is None:
        return None  # no positive CLIPTextEncode — nothing to inject into

    inputs = target.get("inputs", {})
    text_val = inputs.get("text")

    # Direct widget string on the encoder.
    if isinstance(text_val, str):
        inputs["text"] = prompt
        return None

    # Linked source: [source_node_id, slot] -> trace one hop.
    if isinstance(text_val, list) and text_val:
        src = graph.get(str(text_val[0]))
        if not isinstance(src, dict):
            return f"prompt not injected: text link points to missing node {text_val[0]}"
        src_inputs = src.get("inputs", {})
        for key in _PROMPT_SOURCE_FIELDS:
            if isinstance(src_inputs.get(key), str):
                src_inputs[key] = prompt
                return None
        return (
            f"prompt not injected: source node {text_val[0]} "
            f"({src.get('class_type')}) has no string text field "
            f"(looked for {'/'.join(_PROMPT_SOURCE_FIELDS)})"
        )

    return None


def _set_save_prefix(graph: dict, save_prefix: str) -> Optional[str]:
    """Set filename_prefix on every save node in the API graph (SaveImage,
    TS_SaveSVGString, SaveVideo/SaveWEBM/SaveAnimatedWEBP, …). Lets sequential or
    batch runs write named files (icon_brain_….svg) instead of colliding on a
    shared default name. Returns a warning if no save node was found."""
    n = 0
    for node in graph.values():
        if isinstance(node, dict):
            inp = node.get("inputs")
            if isinstance(inp, dict) and "filename_prefix" in inp:
                inp["filename_prefix"] = save_prefix
                n += 1
    if n == 0:
        return "save_prefix given but no save node with a filename_prefix field was found"
    return None


def _effective_params(graph: dict) -> str:
    """Scan the final submitted graph for the LoadImage.image filename and the
    first frame-count field, duck-typed by input name (video node class_types
    differ across LTX/Wan/Bernini workflows, but all use a 'length' input) —
    same style as the filename_prefix scan in _set_save_prefix. Returns a
    ' | image=... | length=... frames' suffix, omitting a part not found.
    Makes a silently-stale image/length visible in the first Queued message
    instead of only after the render completes."""
    image_val = None
    length_val = None
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if image_val is None and node.get("class_type") == "LoadImage":
            img = inputs.get("image")
            if isinstance(img, str) and img:
                image_val = img
        if length_val is None and "length" in inputs:
            length_val = inputs.get("length")
    parts = []
    if image_val is not None:
        parts.append(f"image={image_val}")
    if length_val is not None:
        parts.append(f"length={length_val} frames")
    return " | " + " | ".join(parts) if parts else ""


def comfyui_submit(ctx: ToolContext, workflow: str = "", prompt: str = "", workflow_json: dict = None, save_prefix: str = "", api_url: str = DEFAULT_API_URL) -> str:
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

    async def _submit():
        graph = wf_data
        conv_warnings: List[str] = []
        try:
            client = _get_client()

            # UI/graph format -> API format. Prefer the generic /object_info-driven
            # converter (handles any installed custom node); fall back to the fixed
            # whitelist converter only if object_info is unreachable.
            if _is_ui_format(graph):
                object_info = await _fetch_object_info(client, api_url)
                if object_info:
                    graph, conv_warnings = _convert_ui_to_api_schema(graph, object_info)
                else:
                    try:
                        graph = _convert_ui_to_api(graph)
                    except ValueError as e:
                        return f"Error: {e}"

            # Inject prompt into the positive prompt source — direct CLIPTextEncode
            # widget, or trace the text link back to a batcher/text node.
            if prompt:
                inject_warn = _inject_prompt(graph, prompt)
                if inject_warn:
                    conv_warnings.append(inject_warn)

            # Override save-node filename_prefix so files are named, not colliding.
            if save_prefix:
                sp_warn = _set_save_prefix(graph, save_prefix)
                if sp_warn:
                    conv_warnings.append(sp_warn)

            wf_str_keys = {str(k): v for k, v in graph.items()}
            payload: Dict[str, Any] = {"prompt": wf_str_keys}
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
                if conv_warnings:
                    parts.append("Conversion warnings:\n  " + "\n  ".join(conv_warnings))
                return "\n".join(parts)

            prompt_id = data.get("prompt_id")
            if not prompt_id:
                return f"Error: ComfyUI response missing prompt_id: {data}"

            result = f"Queued. prompt_id={prompt_id}" + _effective_params(wf_str_keys)
            if node_errs:
                result += f"\nWarnings:\n{_format_node_errors(node_errs)}"
            if conv_warnings:
                result += "\nConversion warnings:\n  " + "\n  ".join(conv_warnings)
            return result
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
        except Exception as e:
            log.warning("comfyui_submit failed: %s", e)
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_submit(), loop)
    return future.result(timeout=45)


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


def _queue_item_label(item) -> tuple:
    """From a /queue entry ``[number, prompt_id, prompt, ...]`` return
    ``(prompt_id, label)`` identifying *which* job it is.

    The input frame (``LoadImage.image``, e.g. 'C3_00012.png') is preferred
    as the label because it differs per clip. The fallback is a save node's
    ``filename_prefix`` — detected by duck-typing (any node carrying that
    input), not a hardcoded class-name list, so new/custom save nodes are
    picked up without drift. Note the prefix is often a constant across
    clips (e.g. 'BerniniR_i2v_1.3B'), which is why the input frame wins.

    Returns ('', '') on any malformed item — the queue display degrades to
    counts rather than raising. The full prompt_id is preserved (not
    truncated) so the agent can feed it straight into comfyui_check/_wait.
    """
    prompt_id = ""
    input_label = ""
    save_label = ""
    try:
        if isinstance(item, (list, tuple)) and len(item) > 1:
            prompt_id = str(item[1])
        prompt = item[2] if isinstance(item, (list, tuple)) and len(item) > 2 else None
        if isinstance(prompt, dict):
            for node in prompt.values():
                if not isinstance(node, dict):
                    continue
                inputs = node.get("inputs", {})
                if not isinstance(inputs, dict):
                    continue
                if not input_label and node.get("class_type") == "LoadImage":
                    img = inputs.get("image")
                    if isinstance(img, str) and img:  # str = a filename, not a [node,slot] link
                        input_label = img
                elif not save_label and isinstance(inputs.get("filename_prefix"), str):
                    fp = inputs.get("filename_prefix")
                    if fp:
                        save_label = fp
    except Exception:
        pass
    return prompt_id, (input_label or save_label)


def comfyui_queue_status(ctx: ToolContext, api_url: str = DEFAULT_API_URL) -> str:
    """Check ComfyUI queue — counts plus what each task actually is.

    Reports the running/pending counts and, for every queued job, its full
    prompt_id and a label = its input frame (LoadImage.image, e.g.
    'C3_00012.png'), so the agent knows exactly which clip is rendering
    instead of guessing from bare counts. Fast (single GET, no WS) — for the
    live step X/Y + ETA of the running job, call comfyui_progress.
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
            for item in running:
                pid, label = _queue_item_label(item)
                lines.append(f"  RUNNING  {pid or '?'}  {label or '(unlabeled)'}")
            for item in pending:
                pid, label = _queue_item_label(item)
                lines.append(f"  PENDING  {pid or '?'}  {label or '(unlabeled)'}")
            if running:
                lines.append("  (call comfyui_progress for live step X/Y + ETA)")
            return "\n".join(lines)
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}."
        except Exception as e:
            return f"Error: {e}"

    future = asyncio.run_coroutine_threadsafe(_status(), loop)
    return future.result(timeout=10)


# Last progress sample per api_url: (step, max, loop_time, prompt_id). Lets a
# poll estimate sec/iteration + ETA across calls when a single snapshot only
# catches one step (common on slow renders). Loop time is monotonic and
# comparable across calls because the agent reuses one event loop. The
# prompt_id is stored so the cross-call rate can be REJECTED when the stored
# sample belongs to a different render (the queue advanced to the next clip
# between polls) — otherwise a huge wall gap / tiny step delta yields a bogus
# inflated s/it. Module-level and keyed only by api_url: two agents polling the
# same URL concurrently could race here — acceptable while one agent drives
# renders; revisit (per-agent state) if concurrent generation lands.
_LAST_PROGRESS: dict = {}


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec}s" if sec < 90 else f"{sec // 60}m{sec % 60:02d}s"


def _format_progress_timing(samples: list, api_url: str, prompt_id: str = "") -> str:
    """Build a 'timing:' line (step, sec/it, ETA) from progress samples.

    ``samples`` are (step, max, loop_time) tuples collected this call.
    Rate comes from within-call samples when >=2 are seen (always accurate:
    two real sampler steps timed in one call). The cross-call fallback (using
    the last stored sample) is trusted ONLY when that sample belongs to the
    SAME ``prompt_id`` — otherwise the two samples straddle a clip switch (the
    queue advanced to the next job: tail of the previous clip + VAE decode +
    model reload + partial next clip), and dividing that large wall gap by the
    tiny step delta produces a wildly inflated s/it (the ~250/384-per-step
    ghost that made a healthy 28 s/step render look "stuck"). A different or
    unknown prompt_id -> no cross-call rate (report step only, ask to re-poll).
    Returns '' when nothing is known.
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
        # Same render only: prev is (step, max, loop_time, prompt_id).
        if prev and len(prev) >= 4 and prompt_id and prev[3] == prompt_id:
            p_step, _, p_t, _ = prev
            if cur_step - p_step > 0 and cur_t - p_t > 0:
                rate = (cur_t - p_t) / (cur_step - p_step)
    _LAST_PROGRESS[api_url] = (cur_step, cur_max, cur_t, prompt_id)
    parts = [f"step {cur_step}/{cur_max}"]
    if rate:
        eta = max(0, (cur_max or 0) - cur_step) * rate
        parts.append(f"~{rate:.1f}s/it")
        parts.append(f"ETA ~{_fmt_dur(eta)}")
    else:
        parts.append("(poll again in a few s for s/it & ETA)")
    return "timing: " + " | ".join(parts)


def comfyui_progress(ctx: ToolContext, timeout: int = 10, api_url: str = DEFAULT_API_URL) -> str:
    """Report ComfyUI generation progress, waiting for a real sampler step.

    Slow models (Bernini ~27s/step) emit a 'progress' event only once per
    step, so a short fixed window would catch only the periodic status
    heartbeat (queue_remaining) and no step — useless to the agent. This
    keeps listening until it actually sees a progress step (or the run
    ends), then grabs one more sample for an s/it + ETA estimate. ``timeout``
    is the *minimum* listen window once a step is seen; a hard cap
    (HARD_CAP) bounds the wait so it never hangs even between slow steps.
    """
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available."
    timeout = max(3, min(timeout, 60))
    # Must exceed TWO slow steps (~28s each) so the accurate within-call
    # 2-sample rate can be captured in one call when the agent asks for a long
    # enough window (timeout ~= 60) — otherwise only 1 step fits and we fall
    # back to the (prompt_id-guarded) cross-call estimate.
    HARD_CAP = 65.0

    async def _progress():
        import websockets
        ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url.rstrip('/')}/ws?clientId=dpc-forge-progress"
        events = []
        prog_samples = []
        cur_prompt_id = ""  # which render these samples belong to
        try:
            async with websockets.connect(ws_url, close_timeout=3) as ws:
                start = asyncio.get_event_loop().time()
                while True:
                    elapsed = asyncio.get_event_loop().time() - start
                    # Stop when: hard cap hit (safety); we have a step AND the
                    # nominal window elapsed; or we have 2 samples (step+rate).
                    if elapsed >= HARD_CAP:
                        break
                    if prog_samples and elapsed >= timeout:
                        break
                    if len(prog_samples) >= 2:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(raw)
                        msg_type = data.get("type", "")
                        msg_data = data.get("data", {})
                        # ComfyUI tags most events with the prompt_id of the
                        # running job; capture it so the timing estimator can
                        # tell one clip's samples from the next.
                        pid = msg_data.get("prompt_id")
                        if pid:
                            cur_prompt_id = pid
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
                            # Heartbeat only — keep as context, not a stop signal.
                            events.append(f"queue_remaining={remaining_q}")
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            if not events:
                return f"Error: cannot connect to ComfyUI WebSocket: {e}"
            events.append(f"(connection closed: {e})")

        timing = _format_progress_timing(prog_samples, api_url, cur_prompt_id)
        if timing:
            # We have a real step/ETA: drop the heartbeat noise so the agent
            # sees the signal, not 'queue_remaining=1' lines, then append it.
            events = [e for e in events if not e.startswith("queue_remaining=")]
            events.append(timing)
        if not events:
            return (
                "No sampler step seen within the wait window — the running job "
                "may be between nodes (model load / VAE decode) or the queue is "
                "idle. Call again in a few seconds."
            )
        return "\n".join(events)

    future = asyncio.run_coroutine_threadsafe(_progress(), loop)
    return future.result(timeout=HARD_CAP + 10)


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
        # `-y` above already answers ffmpeg's only prompt, but the child still
        # inherits the service console; closing stdin costs nothing and keeps
        # the rule the same in all three places that spawn a process.
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=120, stdin=subprocess.DEVNULL,
        )
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


def _find_message(messages: list, event: str):
    """Return the data payload of the first [event, data] message, or None."""
    for m in messages:
        if isinstance(m, list) and m and m[0] == event:
            return m[1] if len(m) > 1 else {}
    return None


def _format_history_entry(prompt_id: str, entry: dict) -> str:
    """Parse a /history entry's status.messages into a readable diagnosis:
    status, duration, cached nodes, failed-node exception + traceback, outputs."""
    lines: List[str] = []
    status = entry.get("status", {}) or {}
    lines.append(f"## Execution {prompt_id}")
    lines.append(f"status={status.get('status_str', '?')} completed={status.get('completed')}")

    messages = status.get("messages", []) or []
    start = _find_message(messages, "execution_start")
    end = _find_message(messages, "execution_success") or _find_message(messages, "execution_error")
    if isinstance(start, dict) and isinstance(end, dict) and "timestamp" in start and "timestamp" in end:
        try:
            lines.append(f"duration={((end['timestamp'] - start['timestamp']) / 1000.0):.2f}s")
        except Exception:  # noqa: BLE001
            pass

    cached = _find_message(messages, "execution_cached")
    if isinstance(cached, dict) and cached.get("nodes"):
        lines.append(f"cached_nodes={', '.join(map(str, cached['nodes']))}")

    err = _find_message(messages, "execution_error")
    if isinstance(err, dict):
        lines.append("")
        lines.append("### Error")
        if err.get("node_id") is not None:
            lines.append(f"failed_node={err.get('node_id')} ({err.get('node_type', '?')})")
        if err.get("exception_type"):
            lines.append(f"exception_type={err['exception_type']}")
        if err.get("exception_message"):
            lines.append(f"exception={err['exception_message']}")
        tb = err.get("traceback")
        if isinstance(tb, list) and tb:
            lines.append("traceback:")
            lines.append("".join(tb))

    if _find_message(messages, "execution_interrupted") is not None:
        lines.append("(execution interrupted/cancelled)")

    outputs = _collect_outputs(entry)
    if outputs:
        lines.append(f"outputs={', '.join(outputs)}")
    return "\n".join(lines)


def comfyui_diagnose(ctx: ToolContext, prompt_id: str = "", include_logs: bool = True,
                     log_keyword: str = "", api_url: str = DEFAULT_API_URL) -> str:
    """Post-failure diagnosis: parse a prompt's execution history (exception,
    failed node, traceback, timing) + optionally tail the server logs. Call after
    a render fails — NOT in a poll loop (comfyui_check is the fast poller)."""
    loop = ctx.agent_event_loop
    if loop is None:
        return "Error: no event loop available (agent_event_loop not set)."

    async def _diag():
        out: List[str] = []
        client = _get_client()
        try:
            path = f"/history/{prompt_id}" if prompt_id else "/history"
            resp = await client.get(f"{api_url.rstrip('/')}{path}")
            resp.raise_for_status()
            hist = resp.json()
            if not hist:
                out.append(f"No history for prompt {prompt_id}." if prompt_id else "No execution history.")
            elif prompt_id and prompt_id in hist:
                out.append(_format_history_entry(prompt_id, hist[prompt_id]))
            else:
                pid, entry = list(hist.items())[-1]  # most recent execution
                out.append(_format_history_entry(pid, entry))
        except httpx.ConnectError:
            return f"Error: cannot connect to ComfyUI at {api_url}. Is it running?"
        except Exception as e:  # noqa: BLE001
            log.warning("comfyui_diagnose history failed: %s", e)
            out.append(f"Error reading history: {e}")

        if include_logs:
            try:
                logs = await _fetch_logs(client, api_url, log_keyword, 100)
                if logs:
                    header = "### Server logs" + (f" (filter='{log_keyword}')" if log_keyword else "")
                    out.append("")
                    out.append(header)
                    out.append(logs)
            except Exception as e:  # noqa: BLE001
                out.append(f"(log fetch failed: {e})")

        return "\n".join(out)

    future = asyncio.run_coroutine_threadsafe(_diag(), loop)
    return future.result(timeout=30)


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
                    "UI/graph-format workflows are converted to API format generically using "
                    "the server's /object_info schema, so custom nodes (Bernini-R, SVG nodes, "
                    "etc.) convert as long as they are installed; nodes missing from /object_info "
                    "are skipped with a warning. API-format graphs are submitted as-is. "
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
                        "save_prefix": {
                            "type": "string",
                            "description": (
                                "Optional. Overrides filename_prefix on every save node "
                                "(SaveImage/SaveVideo/TS_SaveSVGString/…) so outputs are named "
                                "(e.g. 'icon_brain' -> icon_brain_*.svg) instead of colliding on a "
                                "shared default. Use a distinct value per run for batch packs."
                            ),
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
            timeout_sec=45,
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
                    "Reports s/it + ETA. IMPORTANT for the s/it to be accurate: the "
                    "reported rate is a real per-step delta only when either two steps are "
                    "seen in one call, or the previous poll was of the SAME running job. "
                    "On slow models (>~22s/step, e.g. Bernini ~28s) pass timeout=60 so two "
                    "real steps are captured in a single call — a short poll catches one step "
                    "and, right after a clip finishes in a batch, cannot compute a trustworthy "
                    "rate (it says 'poll again') rather than emitting an inflated number."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "integer",
                            "description": "Seconds to listen for events (default 10, max 60). Use 60 on slow models to capture two steps and get an accurate s/it in one call.",
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
            timeout_sec=80,
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
        ToolEntry(
            name="comfyui_diagnose",
            schema={
                "name": "comfyui_diagnose",
                "description": (
                    "Diagnose a ComfyUI execution after it fails. Parses the prompt's "
                    "history into a readable report: status, duration, cached nodes, and on "
                    "failure the exception type/message, which node failed, and the Python "
                    "traceback. Optionally tails the server logs (keyword-filterable). Call "
                    "once after comfyui_check/comfyui_wait returns an error — not in a poll loop."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt_id": {
                            "type": "string",
                            "description": "prompt_id to diagnose. Omit to use the most recent execution.",
                        },
                        "include_logs": {
                            "type": "boolean",
                            "description": "Also tail the ComfyUI server logs (default true).",
                        },
                        "log_keyword": {
                            "type": "string",
                            "description": "Case-insensitive filter for log lines (e.g. 'error', 'VRAM', a node name).",
                        },
                        "api_url": {
                            "type": "string",
                            "description": f"ComfyUI API URL (default: {DEFAULT_API_URL}).",
                        },
                    },
                    "required": [],
                },
            },
            handler=comfyui_diagnose,
            is_code_tool=False,
            timeout_sec=30,
            is_core=False,
            default_enabled=False,
        ),
    ]
