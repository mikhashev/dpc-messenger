"""
DPC Agent — Image description tool (pull-based vision).

Lets an agent "look at" an image file on disk on demand — the pull-side
counterpart to a human pasting a screenshot into chat (push, wired via
send_group_image). Mirrors the transcribe_audio tool: reuses the file-access
gates and the configured vision provider (LLMManager.query with images=...).

Use cases: an agent inspects its own output — a rendered ComfyUI preview, an
exported chart, a rasterized SVG, a screenshot — and reasons over what it
actually sees ("does this icon match the prompt?", "what's the error here?").

Path access reuses the file-tool gates: relative → agent sandbox,
absolute → firewall extended-path check.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import List, Optional

from .core import _resolve_file_path
from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_MAX_IMAGE_MB = 20
_SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _resolve_vision_alias(ctx: ToolContext, llm_manager) -> Optional[str]:
    """Which provider gets the picture when the caller named no model.

    Mike's rule, 2026-09-18: if the model the agent runs can see, the image goes
    to it; otherwise to the global vision_provider. None means "global".
    A remote agent (compute_host or a dpc_agent peer) also returns None — this
    tool reads a local file and calls locally, and a peer's alias is not in the
    local registry.
    """
    adapter = getattr(getattr(ctx, "_agent", None), "llm", None)
    if adapter is None:
        return None

    dpc_agent_provider = getattr(llm_manager, "providers", {}).get("dpc_agent")
    if getattr(adapter, "_compute_host", None) or (
        dpc_agent_provider is not None and getattr(dpc_agent_provider, "peer_id", None)
    ):
        log.info("describe_image: agent runs on a remote peer, using the global vision provider")
        return None

    try:
        alias = adapter._get_agent_provider_alias()
        provider = llm_manager.providers.get(alias) if alias else None
        can_see = bool(provider and hasattr(provider, "supports_vision") and provider.supports_vision())
    except Exception as e:
        log.warning("describe_image: cannot read the agent's provider (%s); using the global one", e)
        return None

    if can_see:
        log.info("describe_image: agent provider '%s' supports vision, sending the image to it", alias)
        return alias
    log.info(
        "describe_image: agent provider '%s' has no vision, using the global vision provider",
        alias or "(none)",
    )
    return None


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "image/png")


async def describe_image(
    ctx: ToolContext,
    image_path: str,
    question: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Look at an image and return a description, or answer a question about it.

    Runs the configured local vision model over the image (offline, GPU, no
    API cost) and returns the result inline — unlike transcribe_audio, the
    description is meant to be reasoned over immediately, and is short enough
    (~1-3 KB) to return directly.

    Args:
        ctx: Tool context
        image_path: Relative (sandbox) or absolute (firewall-checked) path
        question: Optional specific question. Omit for a full description.
        model: Vision provider alias. Default: the agent's own model if it
            supports vision, else the global vision provider.

    Returns:
        JSON string with image_path, model, question, description
    """
    dpc_service = getattr(ctx, "dpc_service", None)
    llm_manager = getattr(dpc_service, "llm_manager", None) if dpc_service else None
    if llm_manager is None:
        return "⚠️ Vision unavailable: no LLM manager in this context."

    try:
        source = _resolve_file_path(ctx, image_path, require_write=False)
    except PermissionError as e:
        return f"⚠️ Access denied: {e}"

    if not source.exists():
        return f"⚠️ File not found: {image_path}"
    if not source.is_file():
        return f"⚠️ Not a file: {image_path}"
    if source.suffix.lower() not in _SUPPORTED_EXT:
        return (
            f"⚠️ Unsupported image type '{source.suffix}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXT))}"
        )

    size_mb = source.stat().st_size / (1024 * 1024)
    if size_mb > _MAX_IMAGE_MB:
        return f"⚠️ Image too large ({size_mb:.1f} MB > {_MAX_IMAGE_MB} MB)."

    try:
        image_b64 = base64.b64encode(source.read_bytes()).decode("ascii")
    except OSError as e:
        return f"⚠️ Failed to read image: {e}"

    mime_type = _guess_mime(source)

    if question and question.strip():
        prompt = (
            "Look at this image and answer the question precisely, grounding your "
            f"answer in what is actually visible.\n\nQuestion: {question.strip()}"
        )
    else:
        prompt = (
            "Analyze this image in detail. Provide a comprehensive description:\n"
            "- What objects, text, or UI elements are visible\n"
            "- Any error messages or important text\n"
            "- Layout and structure if it's a screenshot\n"
            "- Any other relevant details for understanding the image"
        )

    ctx.emit_progress(f"Looking at {source.name}...")

    # An explicit `model` is the caller's override; without one, 2026-09-18's rule applies.
    alias = model if model else _resolve_vision_alias(ctx, llm_manager)

    try:
        meta = await llm_manager.query(
            prompt=prompt,
            provider_alias=alias,  # None → auto-select the global vision provider
            images=[{"base64": image_b64, "mime_type": mime_type}],
            return_metadata=True,
        )
    except Exception as e:
        log.error("describe_image failed for %s: %s", source, e, exc_info=True)
        return f"⚠️ Vision query failed: {e}"

    if isinstance(meta, dict):
        description = meta.get("response", "") or ""
        used_model = meta.get("model", "unknown")
    else:
        description = str(meta)
        used_model = "unknown"

    if not description.strip():
        return "⚠️ Vision model returned no description."

    return json.dumps({
        "image_path": str(source),
        "model": used_model,
        "question": question or None,
        "description": description,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export vision tools for registry."""
    return [
        ToolEntry(
            name="describe_image",
            schema={
                "name": "describe_image",
                "description": (
                    "Look at an image file (png/jpg/jpeg/webp/gif/bmp) and get a text "
                    "description, or answer a specific question about it, using the local "
                    "vision model (offline, GPU, no API cost). Use this to inspect your own "
                    "output — a rendered ComfyUI preview, an exported chart, a rasterized "
                    "SVG, a screenshot — and reason over what is actually visible. The full "
                    "description is returned inline as JSON. This is the pull-side "
                    "counterpart to a human pasting a screenshot into chat."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": (
                                "Path to the image. Relative paths resolve to the agent "
                                "sandbox; absolute paths require extended-path read access "
                                "in the firewall."
                            ),
                        },
                        "question": {
                            "type": "string",
                            "description": (
                                "Optional specific question to answer about the image "
                                "(e.g. 'does this icon match a brain-with-circuits motif?'). "
                                "Omit for a full general description."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Optional vision provider alias override. By default the "
                                "image goes to your own model if it supports vision, "
                                "otherwise to the globally configured vision provider."
                            ),
                        },
                    },
                    "required": ["image_path"],
                },
            },
            handler=describe_image,
            timeout_sec=300,
            default_enabled=False,
        ),
    ]
