"""
DPC Agent — Audio transcription tool.

Exposes the local Whisper provider (providers/whisper_provider.py) as an
agent tool so agents can transcribe arbitrary audio files on disk. The
voice-message and Telegram paths already use the same provider; this is
the "agent transcribes a file it found" surface.

Path access reuses the file-tool gates: relative → agent sandbox,
absolute → firewall extended-path check.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from .core import _resolve_file_path
from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_WHISPER_PROVIDER_TYPE = "local_whisper"
_PREVIEW_CHARS = 300


def _whisper_providers(ctx: ToolContext) -> dict:
    dpc_service = getattr(ctx, "dpc_service", None)
    llm_manager = getattr(dpc_service, "llm_manager", None) if dpc_service else None
    providers = getattr(llm_manager, "providers", None) or {}
    return {
        alias: p
        for alias, p in providers.items()
        if getattr(p, "config", {}).get("type") == _WHISPER_PROVIDER_TYPE
    }


def _pick_provider(ctx: ToolContext, model: Optional[str]) -> tuple[Optional[Any], Optional[str]]:
    candidates = _whisper_providers(ctx)
    if not candidates:
        return None, (
            "No local Whisper provider configured. Add a provider with "
            'type "local_whisper" in ~/.dpc/providers.json.'
        )
    if not model:
        alias = sorted(candidates)[0]
        return candidates[alias], None

    for alias in sorted(candidates):
        provider = candidates[alias]
        if model in (alias, getattr(provider, "model_name", "")):
            return provider, None

    available = ", ".join(
        f"{alias} ({getattr(p, 'model_name', '?')})" for alias, p in sorted(candidates.items())
    )
    return None, f"No Whisper provider matches '{model}'. Available: {available}"


def _output_target(ctx: ToolContext, audio_path: Path, output_path: Optional[str]) -> Path:
    if output_path:
        target = _resolve_file_path(ctx, output_path, require_write=True)
    else:
        target = ctx.repo_path(f"transcripts/{audio_path.stem}.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


async def transcribe_audio_file(
    ctx: ToolContext,
    audio_path: str,
    model: Optional[str] = None,
    language: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """
    Transcribe an audio file with the local Whisper model.

    Writes the transcript to a .txt file and returns its path plus metadata.
    The text is not returned inline — a 30-minute recording is ~18 KB.

    Args:
        ctx: Tool context
        audio_path: Relative (sandbox) or absolute (firewall-checked) path
        model: Provider alias or model name. Default: first configured
               local_whisper provider.
        language: ISO code (e.g. "ru", "en"). Default: provider config,
                  usually auto-detect.
        output_path: Where to write the .txt. Default: transcripts/<name>.txt
                     in the agent sandbox.

    Returns:
        JSON string with output_path, language, duration_seconds, chars, preview
    """
    try:
        source = _resolve_file_path(ctx, audio_path, require_write=False)
    except PermissionError as e:
        return f"⚠️ Access denied: {e}"

    if not source.exists():
        return f"⚠️ File not found: {audio_path}"
    if not source.is_file():
        return f"⚠️ Not a file: {audio_path}"

    provider, error = _pick_provider(ctx, model)
    if provider is None:
        return f"⚠️ {error}"

    previous_language = None
    if language:
        previous_language = getattr(provider, "language", None)
        provider.language = language

    ctx.emit_progress(f"Transcribing {source.name}...")

    try:
        result = await provider.transcribe(str(source))
    except Exception as e:
        log.error("Transcription failed for %s: %s", source, e, exc_info=True)
        return f"⚠️ Transcription failed: {e}"
    finally:
        if previous_language is not None:
            provider.language = previous_language

    text = (result or {}).get("text", "") or ""
    if not text.strip():
        return "⚠️ Transcription produced no text — the file may contain no speech."

    try:
        target = _output_target(ctx, source, output_path)
        target.write_text(text, encoding="utf-8")
    except PermissionError as e:
        return f"⚠️ Access denied writing transcript: {e}"
    except OSError as e:
        return f"⚠️ Failed to write transcript: {e}"

    return json.dumps({
        "output_path": str(target),
        "language": result.get("language", "unknown"),
        "duration_seconds": round(result.get("duration", 0) or 0, 1),
        "chars": len(text),
        "model": getattr(provider, "model_name", "unknown"),
        "preview": text[:_PREVIEW_CHARS],
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export transcription tools for registry."""
    return [
        ToolEntry(
            name="transcribe_audio_file",
            schema={
                "name": "transcribe_audio_file",
                "description": (
                    "Transcribe an audio file (wav/mp3/ogg/webm/mp4/opus) to text "
                    "using the local Whisper model. Runs offline on the GPU, no API "
                    "cost. Writes the transcript to a .txt file and returns its path "
                    "plus a short preview — the full text is NOT returned inline "
                    "(a 30-minute recording is ~18 KB). Read the returned path with "
                    "read_file to work with the text. "
                    "Note: on non-speech stretches (pauses, silence, applause) Whisper "
                    "hallucinates filler — subtitle credits ('Субтитры создавал …') and "
                    "repeated 'АПЛОДИСМЕНТЫ'. Measured on a 30-min recording: 3 such "
                    "blocks, all mid-file at conversational pauses, not at the edges. "
                    "Treat these as artifacts, not content, wherever they appear."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "audio_path": {
                            "type": "string",
                            "description": (
                                "Path to the audio file. Relative paths resolve to the "
                                "agent sandbox; absolute paths require extended-path "
                                "read access in the firewall."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Provider alias or model name to use. Defaults to the "
                                "first configured local Whisper provider."
                            ),
                        },
                        "language": {
                            "type": "string",
                            "description": (
                                "ISO language code such as 'ru' or 'en'. Omit to use "
                                "the provider default (usually auto-detect)."
                            ),
                        },
                        "output_path": {
                            "type": "string",
                            "description": (
                                "Where to write the transcript. Defaults to "
                                "transcripts/<name>.txt in the agent sandbox. An "
                                "absolute path requires extended-path write access."
                            ),
                        },
                    },
                    "required": ["audio_path"]
                }
            },
            handler=transcribe_audio_file,
            timeout_sec=1800,
            default_enabled=False,
        ),
    ]
