"""The `local_api` front door of the music library (ADR-051 D11).

`MusicCommandsMixin` is mixed into `CoreService`; `local_api` dispatches a command by
`getattr(core_service, command)`, so each `async def` here is one command, and each
must also be in `local_api.ALLOWED_COMMANDS`. The agent door (`music_*` tools) will
call the same `MusicLibrary` methods; the job commands (propose, keep, discard, ...) have no
agent tool (ADR-051 Q1: agents get no keep/discard).

Every answer is `{"status": "ok", ...}` or `{"status": "error", "code", "message"}`.

The mixin needs `self.settings` (a `Settings`); the library lives under
`settings.dpc_home_dir / "music"`. The poll interval is `[music] poll_interval_s`
(`DPC_MUSIC_POLL_INTERVAL_S`), read with a default so `settings.py` is unchanged.
Likewise the LRCLIB lookup of the lyrics chain is `[music] lrclib_enabled`
(`DPC_MUSIC_LRCLIB_ENABLED`, default `true`), read on every lookup; even when on, only
the explicit `lookup_music_lyrics_lrclib` sends a request.

**Transcribe** (`transcribe_music_lyrics`, the person's "Transcribe" click) reaches DPC's
existing transcription the way a voice message does, with the same provider choice:

- the voice provider in `providers.json` (`voice_provider`, `llm_manager.voice_provider`)
  when it names a loaded provider; without one, the first `local_whisper` provider when
  `[local_transcription] enabled` (default on);
- a `local_whisper` provider is called as `provider.transcribe(path, release_if_loaded_here=True)`, the call
  `service.transcribe_audio` and the agent's `transcribe_audio_file` make: the provider
  loads its own model under its own lock, and its result carries Whisper's segment
  timestamps (`return_timestamps=True`, a9e2c8c3). The file must be within
  `[local_transcription] max_file_size_mb` (default 25), as for a voice message. A model
  that is not downloaded yet raises the same download-consent dialog
  (`model_download_service.maybe_emit_required`) and the click answers
  `model_not_downloaded`. A local failure is reported, not retried elsewhere: the
  voice path's OpenAI and peer fallbacks would send a whole song off this machine
  without the person having picked that provider;
- any other choice (an OpenAI-compatible or `remote:` voice provider, or no local
  Whisper) goes through `service.transcribe_audio(file_path=...)` itself, with its own
  limits (`[voice_messages] max_size_mb`, default 10) and fallbacks. That path answers
  text only, so the lyrics come back unsynced (`synced: false`).

Whisper is an in-process model, not a local engine, so the ADR-044 card queue does not
gate it; the provider's lock and its CUDA out-of-memory message are the gates it has.

**Unload after the run** (Mike, 2026-11-20): the person picks CPU or GPU in the provider's
`device`; whichever it is, a Transcribe run that loaded the model unloads it when the run
ends, with success or an error (`provider.transcribe(..., release_if_loaded_here=True)`,
which decides "loaded here" under its own lock and then calls `unload_if_idle()`: on CUDA the weights go to the
CPU, are dropped and `torch.cuda.empty_cache()` frees the ~3 GB; on CPU the pipeline is
dropped; on MLX mlx_whisper's cached model is dropped). A model that was already loaded
when the run started (voice auto-transcribe keeps it warm; `[local_transcription]
idle_unload_s` frees it later) stays loaded, and so does one
another transcription is using at that moment: the provider counts its running
transcriptions under its load lock and refuses the unload while any is running.

**The track stream** (`get_music_stream_info`, ADR-051 N8 decided as option (i), D28):
`start_music_stream` opens `music/stream.py`'s loopback listener on `[music] stream_port`
(default 9996), which serves a library track's bytes by `track_id` with HTTP `Range`, a
per-process token and an `Origin` allowlist. The command hands the UI `{base_url, token, ranges_linux}`.

**The engines and the runner** (ADR-051 D30, D35; SPEC §12 decisions 31-34, 40): two
supervisors, `music/yue2.py` for yue2.cpp, the default engine (Mike #198; settings
`[music] yue2_binary`, `yue2_models_dir`, `yue2_backbone`, `yue2_port`, `yue2_idle_stop_s`,
`yue2_adapters_dir`, `yue2_device`, `yue2_max_seq`), and `music/acestep.py` for acestep.cpp
(`acestep_binary`, `acestep_models_dir`, `acestep_port`, `acestep_idle_stop_s`,
`acestep_device`), held by `music/engines.py` (`MusicEngines`), which keeps one music engine
on the card at a time. Their `status()` are the job store's engine probes, so the
capabilities report the real state and a `generate_from_text` job is queued only when the
engine it names is installed. `music/runner.py` runs queued jobs one at a time;
`start_music_jobs` starts it at service start (resuming queued jobs) and `stop_music_jobs`
stops it and both servers at shutdown. Each change the runner makes is broadcast as
`music_job_updated` `{job}`, and each engine state change as `music_engine_status`
`{engine}` (with `engine.key`); `get_music_job` answers the same state for a UI that polls.
The engine commands (`get_music_engine_status`, `start_music_engine`, `stop_music_engine`)
take an optional `engine` (`yue2` or `acestep`); without it they act on the default engine,
yue2 (before 2026-11-21 there was only acestep, and a call without `engine` meant it).
The person's own changes (propose, cancel, keep, discard, none of these) are broadcast the
same way, so a second window sees them.

**MP3 export** (`music/export.py`, Mike #210; the design is CC's proposal [P]):
`export_music_track {track_id, preset?}`, `export_music_playlist {playlist_id, preset?}` and
`get_music_export_info {track_id?}`. The source file is always the library's own path for the
track id; the UI never sends a path. The root is `[music] export_dir`
(`DPC_MUSIC_EXPORT_DIR`), by default `<DPC home>/exports/music`, outside every watched folder;
`get_music_export_info` says when a chosen root is inside one. A finished single export is
broadcast as `music_export_done {track_id, path}`, a playlist as `{playlist_id, path}` (the
folder). The take's measured true peak (`loudness.true_peak_dbtp`) decides whether the -1 dBTP
limiter runs; its sidecar and stored lyrics go into the MP3's tags.
"Stop X" on a job waiting for the card is the existing `stop_local_engine` command with the
holder's alias from `job.card_wait.holders`; nothing here stops another engine.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..local_api import slow_command
from . import job_schemas as js
from .acestep import AceStepServer, read_config
from .comments import MusicComments
from .capabilities import EngineStatus, MusicCapabilities
from .engine_base import MusicEngineError
from .engines import MusicEngines
from .export import (
    DEFAULT_PRESET, PRESETS, ExportError, ExportSource, MusicExporter, SkippedTrack, default_export_root,
    read_take_sidecar,
)
from .jobs import MusicJobError, MusicJobStore
from .library import DEFAULT_POLL_INTERVAL_S, MusicLibrary, MusicLibraryError
from .loudness import LoudnessError, measure_loudness
from .lyrics import MusicLyrics
from .peaks import PeaksError, get_peaks
from .runner import MusicJobRunner
from .stream import DEFAULT_STREAM_PORT, MusicStreamServer
from .yue2 import Yue2Server, read_config as read_yue2_config

LOCAL_WHISPER_TYPE = "local_whisper"
DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB = 25

logger = logging.getLogger(__name__)

DEFAULT_LRCLIB_ENABLED = True
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, MusicLibraryError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    if isinstance(exc, MusicJobError):
        return {"status": "error", "code": exc.code, "message": exc.message, "field": exc.field}
    if isinstance(exc, MusicEngineError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    if isinstance(exc, ExportError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    logger.exception("music command failed")
    return {"status": "error", "code": "internal", "message": str(exc)}


_UNSET: Any = object()


def _bad(code: str, field: str, what: str) -> MusicJobError:
    return MusicJobError(code, f"{field} must be {what}", field=field)


def _need_str(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(code, field, "a non-empty string")
    return value


def _opt_str(value: Any, field: str, code: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad(code, field, "a string or null")
    return value


def path_of_file_uri(uri: Any) -> Optional[Path]:
    """The local path of a `file:` URI (`Path.as_uri()`'s form on every platform:
    `file:///C:/x/y.flac` on Windows, `file:///home/x/y.flac` elsewhere), or None."""
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = ("//" if os.sep == "/" else "\\\\") + parsed.netloc + path
    return Path(path)


def _opt_str_list(value: Any, field: str, code: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad(code, field, "a list of strings or null")
    return value


class MusicCommandsMixin:
    """Music library commands for `CoreService`."""

    _music_library_instance: Optional[MusicLibrary] = None
    _music_lyrics_instance: Optional[MusicLyrics] = None
    _music_comments_instance: Optional[MusicComments] = None
    _music_jobs_instance: Optional[MusicJobStore] = None
    _music_jobs_lock: Optional[asyncio.Lock] = None
    _music_engines_instance: Optional[MusicEngines] = None
    _music_runner_instance: Optional[MusicJobRunner] = None

    def _music_poll_interval_s(self) -> float:
        raw = self._music_config_value("poll_interval_s", str(DEFAULT_POLL_INTERVAL_S))
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("[music] poll_interval_s %r is not a number; using %s", raw, DEFAULT_POLL_INTERVAL_S)
            return DEFAULT_POLL_INTERVAL_S

    async def _music(self) -> MusicLibrary:
        if self._music_library_instance is None:
            self._music_library_instance = MusicLibrary(
                self.settings.dpc_home_dir, poll_interval_s=self._music_poll_interval_s())
            # The comments follow an entry the library folds into another.
            comments = MusicComments(self.settings.dpc_home_dir, self._music_library_instance)
            self._music_comments_instance = comments
            self._music_library_instance.on_merged = comments.merge
            # The scan skips the files the MP3 export index lists (export.py).
            self._music_library_instance.exclude_path = self._music_export_excluded
        await self._music_library_instance.start()
        return self._music_library_instance

    def _music_lrclib_enabled(self) -> bool:
        """`[music] lrclib_enabled`: whether the lyrics chain may ask lrclib.net."""
        raw = self._music_config_value("lrclib_enabled", "true" if DEFAULT_LRCLIB_ENABLED else "false")
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        logger.warning("[music] lrclib_enabled %r is not a boolean; using %s", raw, DEFAULT_LRCLIB_ENABLED)
        return DEFAULT_LRCLIB_ENABLED

    async def _music_lyrics(self) -> MusicLyrics:
        library = await self._music()
        if self._music_lyrics_instance is None or self._music_lyrics_instance.library is not library:
            self._music_lyrics_instance = MusicLyrics(
                self.settings.dpc_home_dir, library, lrclib_enabled=self._music_lrclib_enabled,
                transcriber=self._music_transcribe_audio)
        return self._music_lyrics_instance

    # -- the transcriber of the lyrics' Transcribe -----------------------------------

    def _music_settings_value(self, getter: str, default: Any) -> Any:
        fn = getattr(self.settings, getter, None)
        if not callable(fn):
            return default
        try:
            return fn()
        except Exception:
            logger.warning("music: reading %s failed; using %r", getter, default)
            return default

    def _music_config_value(self, key: str, default: str) -> Any:
        """`[music] key` from the settings, or `default` when the settings object has no
        `get` (a start-up stand-in) or the read fails."""
        fn = getattr(self.settings, "get", None)
        if not callable(fn):
            return default
        try:
            return fn("music", key, default)
        except Exception:
            logger.warning("music: reading [music] %s failed; using %r", key, default)
            return default

    def _music_pick_transcriber(self) -> tuple:
        """`(whisper provider or None, alias for transcribe_audio or None)`: the voice
        path's choice (see the module docstring)."""
        llm = getattr(self, "llm_manager", None)
        providers = dict(getattr(llm, "providers", None) or {})
        chosen = getattr(llm, "voice_provider", None)
        chosen = chosen.strip() if isinstance(chosen, str) and chosen.strip() else None
        if chosen and chosen.startswith("local:"):
            chosen = chosen[len("local:"):]
        if chosen and chosen.startswith("remote:"):
            return None, chosen
        if chosen and chosen in providers:
            provider = providers[chosen]
            if (getattr(provider, "config", None) or {}).get("type") == LOCAL_WHISPER_TYPE:
                return provider, None
            return None, chosen  # transcribe_audio says whether this type can transcribe
        if self._music_settings_value("get_local_transcription_enabled", True):
            for provider in providers.values():
                if (getattr(provider, "config", None) or {}).get("type") == LOCAL_WHISPER_TYPE:
                    return provider, None
        return None, None

    async def _music_transcribe_audio(self, path: Path) -> Dict[str, Any]:
        """One transcription of a track's file, for `MusicLyrics.transcribe`."""
        whisper, alias = self._music_pick_transcriber()
        if whisper is not None:
            return await self._music_transcribe_with_whisper(whisper, path)
        transcribe_audio = getattr(self, "transcribe_audio", None)
        if transcribe_audio is None:
            raise MusicLibraryError(
                "transcription_unavailable",
                "no transcription provider: add a local_whisper provider (or an OpenAI-compatible one) "
                "in ~/.dpc/providers.json")
        result = await transcribe_audio(file_path=str(path), provider_alias=alias)
        if not isinstance(result, dict) or result.get("status") != "success":
            message = result.get("error") if isinstance(result, dict) else None
            if isinstance(result, dict) and result.get("model_not_cached"):
                raise MusicLibraryError("model_not_downloaded", str(message or "the model is not downloaded yet"))
            raise MusicLibraryError("transcription_failed", str(message or "the transcription failed"))
        return {"text": result.get("text") or "", "language": result.get("language"),
                "duration": result.get("duration"), "provider": result.get("provider") or alias,
                "model": None, "segments": [], "fallback_reason": result.get("fallback_reason")}

    async def _music_transcribe_with_whisper(self, provider: Any, path: Path) -> Dict[str, Any]:
        alias = getattr(provider, "alias", None) or LOCAL_WHISPER_TYPE
        limit_mb = self._music_settings_value("get_local_transcription_max_file_size_mb",
                                              DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB)
        try:
            limit = float(limit_mb)
        except (TypeError, ValueError):
            limit = float(DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB)
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError as exc:
            raise MusicLibraryError("no_file", f"the track's file cannot be read: {exc}") from exc
        if size_mb > limit:
            raise MusicLibraryError(
                "too_large",
                f"the file is {size_mb:.1f} MB, over the local transcription limit of {limit:g} MB "
                f"([local_transcription] max_file_size_mb)")
        from ..providers.whisper_provider import card_ask, card_wait_words, transcribe_with_route
        from .lyrics import show_card_wait
        # ADR-052 L4: a person's click with no bound of its own; it waits until cancelled.
        ask = await asyncio.to_thread(card_ask, provider, "music Transcribe", str(path))
        ask["on_wait"] = lambda estimate: show_card_wait({
            "text": card_wait_words(estimate), "behind": list(estimate.get("behind") or []),
            "wait_s": estimate.get("wait_s"), "known": bool(estimate.get("known"))})
        ask["with_detail"] = True  # word timestamps and the backend's hallucination options; see lyrics_filter
        try:
            # A model this run loads is released when the run ends (decided under the
            # provider's lock); a warm one stays, and the idle timer frees it in time.
            result = await transcribe_with_route(provider, str(path), release_if_loaded_here=True, **ask)
        except Exception as exc:
            from ..providers.base import ModelNotCachedError
            missing = exc if isinstance(exc, ModelNotCachedError) else (
                exc.__cause__ if isinstance(exc.__cause__, ModelNotCachedError) else None)
            if missing is not None:
                downloads = getattr(self, "model_download_service", None)
                if downloads is not None:
                    try:
                        await downloads.maybe_emit_required(
                            missing.model_name, revision=missing.revision, purpose="Lyrics transcription",
                            consequence_if_declined="Lyrics will not be transcribed.", provider_alias=alias)
                    except Exception:
                        logger.exception("music: asking for the Whisper download failed")
                raise MusicLibraryError(
                    "model_not_downloaded",
                    f"Model '{missing.model_name}' is not downloaded yet. Confirm the download in the "
                    f"D-PC Messenger app, then press Transcribe again.") from exc
            raise MusicLibraryError("transcription_failed", f"local Whisper ({alias}): {exc}") from exc
        if not isinstance(result, dict):
            raise MusicLibraryError("transcription_failed", f"local Whisper ({alias}) gave no result")
        return {**result, "provider": result.get("provider") or LOCAL_WHISPER_TYPE,
                "model": getattr(provider, "model_name", None) or result.get("model")}

    async def start_music_library(self) -> None:
        """Load the library and start watching its folders; called at service start."""
        try:
            await self._music()
        except Exception:
            logger.exception("music: the library did not start")

    async def stop_music_library(self) -> None:
        """Stop polling and hashing and write what is pending; called at shutdown."""
        if self._music_library_instance is not None:
            await self._music_library_instance.stop()

    # -- the track stream (ADR-051 N8, D28) ------------------------------------------

    _music_stream_instance: Optional[MusicStreamServer] = None
    _music_stream_problem: Optional[str] = None

    def _music_stream_port(self) -> int:
        raw = self._music_config_value("stream_port", str(DEFAULT_STREAM_PORT))
        try:
            port = int(str(raw).strip())
            if 0 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
        logger.warning("[music] stream_port %r is not a port; using %d", raw, DEFAULT_STREAM_PORT)
        return DEFAULT_STREAM_PORT

    def _music_stream_ranges_linux(self) -> bool:
        """`[music] stream_ranges_linux`: the Linux UI asks the stream for `Range` only when
        true; the default (false) asks for `ranges=0`, a plain whole-file 200."""
        raw = self._music_config_value("stream_ranges_linux", "false")
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _music_track_path(self, track_id: str) -> Optional[Path]:
        library = self._music_library_instance
        return library.get_track_path(track_id) if library is not None else None

    async def start_music_stream(self) -> None:
        """Open the loopback track stream (`music/stream.py`); called at service start,
        after the library. A taken port is logged and leaves the stream off: the UI is
        told `stream_unavailable` and falls back. Never raises."""
        if self._music_stream_instance is not None and self._music_stream_instance.is_running:
            return
        port = self._music_stream_port()
        server = MusicStreamServer(self._music_track_path, port=port)
        try:
            await server.start()
        except OSError as exc:
            self._music_stream_problem = (
                f"the music track stream could not listen on 127.0.0.1:{port} ({exc}); "
                f"free the port or set [music] stream_port (the app's CSP media-src must name the same port)")
            logger.error("music: %s", self._music_stream_problem)
            return
        except Exception as exc:
            self._music_stream_problem = f"the music track stream did not start: {exc}"
            logger.exception("music: the track stream did not start")
            return
        self._music_stream_instance = server
        self._music_stream_problem = None

    async def stop_music_stream(self) -> None:
        """Close the track stream; called at shutdown."""
        server, self._music_stream_instance = self._music_stream_instance, None
        if server is not None:
            await server.stop()

    async def get_music_stream_info(self) -> Dict[str, Any]:
        """Where the webview reads a track's bytes: `{base_url, token, ranges_linux}`; a track's URL is
        `{base_url}/music/track/{track_id}?t={token}`. Answered only through this
        authenticated door, so the token never reaches another page. `stream_unavailable`
        when the listener is not running (the UI falls back)."""
        server = self._music_stream_instance
        if server is None or not server.is_running:
            return {"status": "error", "code": "stream_unavailable",
                    "message": self._music_stream_problem or "the music track stream is not running"}
        return {"status": "ok", **server.info(), "ranges_linux": self._music_stream_ranges_linux()}

    # -- folders -----------------------------------------------------------------

    async def list_music_folders(self) -> Dict[str, Any]:
        try:
            return {"status": "ok", "folders": (await self._music()).list_folders()}
        except Exception as exc:
            return _error(exc)

    async def add_music_folder(self, path: str, watch: bool = True) -> Dict[str, Any]:
        """Add a watched folder; its first scan starts in the background."""
        try:
            folder = await (await self._music()).add_folder(path, watch=watch)
            return {"status": "ok", "folder": folder}
        except Exception as exc:
            return _error(exc)

    async def remove_music_folder(self, folder_id: str) -> Dict[str, Any]:
        """Remove a watched folder and its entries; the files are never touched."""
        try:
            removed = await (await self._music()).remove_folder(folder_id)
            return {"status": "ok", "removed_tracks": removed}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def rescan_music_folder(self, folder_id: Optional[str] = None) -> Dict[str, Any]:
        """Rescan one folder, or every folder without `folder_id`. Returns when the walk
        is reconciled; hashing of new or changed files goes on in the background."""
        try:
            library = await self._music()
            if folder_id is None:
                return {"status": "ok", "folders": await library.rescan_all()}
            return {"status": "ok", "folder": await library.rescan_folder(folder_id)}
        except Exception as exc:
            return _error(exc)

    # -- library -----------------------------------------------------------------

    async def list_music_library(self, folder_id: Optional[str] = None, include_missing: bool = True) -> Dict[str, Any]:
        try:
            entries = (await self._music()).list_entries(folder_id=folder_id, include_missing=include_missing)
            return {"status": "ok", "entries": entries}
        except Exception as exc:
            return _error(exc)

    async def hide_music_track(self, track_id: str) -> Dict[str, Any]:
        """Hide a track: reversible, the file is untouched."""
        try:
            return {"status": "ok", "entry": await (await self._music()).set_hidden(track_id, True)}
        except Exception as exc:
            return _error(exc)

    async def unhide_music_track(self, track_id: str) -> Dict[str, Any]:
        try:
            return {"status": "ok", "entry": await (await self._music()).set_hidden(track_id, False)}
        except Exception as exc:
            return _error(exc)

    async def remove_missing_music_tracks(self, track_id: Optional[str] = None) -> Dict[str, Any]:
        """Drop one missing entry (`track_id`), or all of them, from the library and
        the playlists. A present entry is refused."""
        try:
            return {"status": "ok", "removed": await (await self._music()).remove_missing(track_id)}
        except Exception as exc:
            return _error(exc)

    async def delete_generated_music_file(self, track_id: str) -> Dict[str, Any]:
        """Delete a DPC-made file under `music/generated/` (and its .json sidecar) and
        drop its entry. Anything else is refused; this is not Keep / Discard."""
        try:
            return {"status": "ok", **await (await self._music()).delete_generated_file(track_id)}
        except Exception as exc:
            return _error(exc)

    # -- jobs and takes (no agent tool: ADR-051 Q1) ----------------------------------

    async def _music_jobs(self) -> MusicJobStore:
        """The lazily built job store; two first calls open it once."""
        if self._music_jobs_instance is not None:
            return self._music_jobs_instance
        if self._music_jobs_lock is None:
            self._music_jobs_lock = asyncio.Lock()
        async with self._music_jobs_lock:
            if self._music_jobs_instance is None:
                engines = self._music_engine_set()
                store = MusicJobStore(self.settings.dpc_home_dir,
                                      capabilities=MusicCapabilities(engines=dict(engines.items()),
                                                                     default_engine=engines.default))
                await asyncio.to_thread(store.open)
                self._music_jobs_instance = store
        return self._music_jobs_instance

    # -- the engine and the runner (ADR-051 D30) --------------------------------------

    def _music_engine_set(self) -> MusicEngines:
        """Both music engine supervisors, built on first use; each re-reads its settings
        before each start."""
        if self._music_engines_instance is None:
            home = self.settings.dpc_home_dir

            def ace_config():
                return read_config(self.settings.get, home)

            def yue2_config():
                return read_yue2_config(self.settings.get, home)
            self._music_engines_instance = MusicEngines({
                js.ENGINE_YUE2: Yue2Server(yue2_config(), dpc_home=home, config_source=yue2_config,
                                           on_change=self._music_engine_changed),
                js.ENGINE_ACESTEP: AceStepServer(ace_config(), dpc_home=home, config_source=ace_config,
                                                 on_change=self._music_engine_changed),
            }, default=js.DEFAULT_FROM_TEXT_ENGINE)
        return self._music_engines_instance

    def _music_engine(self, engine: Any = None) -> Any:
        """One engine's supervisor: `engine` is `yue2` or `acestep`, None the default
        (`invalid_engine` for anything else)."""
        if engine is not None and not isinstance(engine, str):
            raise MusicEngineError("invalid_engine", "engine must be a string: yue2 or acestep")
        return self._music_engine_set().get(engine)

    def _music_broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        """Send an event to the UI when one is connected; never raises."""
        local_api = getattr(self, "local_api", None)
        broadcast = getattr(local_api, "broadcast_event", None)
        if broadcast is None:
            return
        try:
            asyncio.get_running_loop().create_task(broadcast(event, payload))
        except RuntimeError:
            pass  # no running loop: nobody to tell
        except Exception:
            logger.exception("music: broadcasting %s failed", event)

    def _music_engine_changed(self, status: EngineStatus) -> None:
        self._music_broadcast("music_engine_status", {"engine": status.as_dict()})

    def _music_job_changed(self, job: Dict[str, Any]) -> None:
        self._music_broadcast("music_job_updated", {"job": job})

    def _music_wake_runner(self) -> None:
        if self._music_runner_instance is not None:
            self._music_runner_instance.wake()

    async def start_music_jobs(self) -> None:
        """Start the job runner (it resumes queued jobs); called at service start. A
        failure is logged and leaves jobs queued."""
        try:
            store = await self._music_jobs()
            if self._music_runner_instance is None:
                self._music_runner_instance = MusicJobRunner(
                    store, self._music_engine_set(), self._music, dpc_home=self.settings.dpc_home_dir,
                    on_job_change=self._music_job_changed)
            self._music_runner_instance.start()
        except Exception:
            logger.exception("music: the job runner did not start")

    async def stop_music_jobs(self) -> None:
        """Stop the runner and both engine servers; called at shutdown. A job still
        running is settled by the store's restart rule at the next start."""
        runner = self._music_runner_instance
        if runner is not None:
            await runner.stop()
        if self._music_engines_instance is not None:
            await self._music_engines_instance.shutdown()

    def _music_engine_job(self, key: str) -> Optional[str]:
        """The job the runner is running on engine `key`, or None."""
        runner = self._music_runner_instance
        if runner is None or not runner.current_job_id:
            return None
        if runner.current_engine not in (None, key):
            return None
        return runner.current_job_id

    async def get_music_engine_status(self, engine: Any = None) -> Dict[str, Any]:
        """An engine's state (`engine`: yue2 or acestep; the default when omitted), where its
        files are, every engine's state under `engines`, and the job the runner holds. Who
        holds the GPU card is the local-engine status command's (owner-only) answer, and a
        waiting job's `card_wait` names the holder."""
        try:
            chosen = self._music_engine(engine)
            engines = self._music_engine_set()
            runner = self._music_runner_instance
            return {"status": "ok", "engine": chosen.details(),
                    "engines": {k: e.details() for k, e in engines.items()},
                    "default_engine": engines.default,
                    "runner": {"running": bool(runner and runner.running),
                               "current_job_id": runner.current_job_id if runner else None,
                               "current_engine": runner.current_engine if runner else None}}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def start_music_engine(self, engine: Any = None, backbone: Any = None) -> Dict[str, Any]:
        """Start an engine by hand (`engine`: yue2 or acestep; the default when omitted;
        also the way out of an `error` state). For yue2, `backbone` (`stock` or `rs-tw`)
        picks the backbone; without it the running one, else `[music] yue2_backbone`. Only
        one music engine is on the card at a time: the other is stopped when idle, and the
        start is refused (`engine_busy`) while it runs a job. Anything idle with no queue behind
        it may be unloaded for the start, on the terms a queued music job gets from the card
        lease (ADR-052 S4, Mike's call 2026-11-24): an idle Whisper model, an idle local LLM
        engine when swapping is on. A busy holder, or one with a queue, refuses it at once,
        named (`card_busy`); the person stops that holder first. Nothing is stopped for a
        start that is then refused. Slow: it waits for the holders to let go and for /health."""
        try:
            chosen = self._music_engine(engine)
            if backbone is not None and chosen.KEY != js.ENGINE_YUE2:
                raise MusicEngineError("invalid_params", "backbone is a yue2 option")
            busy = self._music_engine_job(chosen.KEY)
            running_backbone = getattr(chosen, "running_backbone", None)
            if busy and backbone is not None and running_backbone and backbone != running_backbone:
                raise MusicEngineError("engine_busy", f"music job {busy} is using {chosen.DISPLAY} with the "
                                                      f"{running_backbone} backbone; cancel it first")
            engines = self._music_engine_set()
            # The other music engine first, as a check only: a busy one refuses (`engine_busy`); an
            # idle one is left running, and stopped below once the card is granted, so a refused
            # start has stopped nothing.
            engines.refuse_while_switching(chosen.KEY)
            # Then the card (ADR-052 section 3.2): asked for before any lock. It is refused at once,
            # naming the holder (`card_busy`), when a holder is busy or has a queue behind it; when
            # every holder is idle with no queue the lease lets them go (an idle Whisper model, the
            # other music engine, a local LLM engine with swapping on) and waits for that, bounded.
            # A grant the runner's job holds on this engine is its own and is granted again.
            await chosen._card_for_a_start("start by hand", yields_idle=True)
            try:
                async with engines.exclusive(chosen.KEY, wait=False):
                    if chosen.KEY == js.ENGINE_YUE2:
                        await chosen.start(backbone=backbone)
                    else:
                        await chosen.start()
            finally:
                # A start by hand that brought no child up keeps no grant.
                chosen.card_lease.release_when_empty("a start by hand that did not start", holds=chosen.alive)
            return {"status": "ok", "engine": chosen.details()}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def stop_music_engine(self, engine: Any = None, force: Any = False) -> Dict[str, Any]:
        """Stop an engine by hand to free the card (`engine`: yue2 or acestep; the default
        when omitted). Refused (`engine_busy`) while a job runs on it, unless `force` (the
        job then fails)."""
        try:
            chosen = self._music_engine(engine)
            busy = self._music_engine_job(chosen.KEY)
            if busy and force is not True:
                return {"status": "error", "code": "engine_busy",
                        "message": f"music job {busy} is using {chosen.DISPLAY}; cancel it first, or pass force"}
            stopped = await chosen.stop("ui_stop")
            return {"status": "ok", "stopped": stopped, "engine": chosen.details()}
        except Exception as exc:
            return _error(exc)

    async def _music_hide_for_discard(self, take: Dict[str, Any]) -> Dict[str, Any]:
        """Decision 35: Discard removes the library entry (hides it); the file stays. A
        failure here never undoes the take's decision."""
        track_id = take.get("library_track_id")
        if not track_id:
            return {"library_hidden": False, "library_reason": "the take has no library entry"}
        try:
            await (await self._music()).set_hidden(track_id, True)
            return {"library_hidden": True}
        except MusicLibraryError as exc:
            return {"library_hidden": False, "library_reason": exc.message}
        except Exception as exc:
            logger.exception("music: hiding the discarded take's library entry failed")
            return {"library_hidden": False, "library_reason": str(exc)}

    @slow_command
    async def get_music_capabilities(self) -> Dict[str, Any]:
        try:
            store = await self._music_jobs()
            return {"status": "ok", "capabilities": store.capabilities.report()}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def propose_music_job(self, type: Any = None, params: Any = None, track_id: Any = None,
                                base_version_id: Any = None, comment_id: Any = None, action: Any = None,
                                region: Any = None, prompt: Any = None,
                                idempotency_key: Any = None) -> Dict[str, Any]:
        try:
            job_type = _need_str(type, "type", "invalid_type")
            if not isinstance(params, dict):
                raise _bad("invalid_params", "params", "an object")
            if region is not None and not isinstance(region, dict):
                raise _bad("invalid_params", "region", "an object or null")
            strs = {k: _opt_str(v, k, "invalid_params") for k, v in (
                ("track_id", track_id), ("base_version_id", base_version_id), ("comment_id", comment_id),
                ("action", action), ("prompt", prompt), ("idempotency_key", idempotency_key))}
            store = await self._music_jobs()
            job = await asyncio.to_thread(lambda: store.create_job(job_type, params, region=region, **strs))
            if job.get("state") == "queued":
                self._music_wake_runner()
            self._music_job_changed(job)
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def get_music_job(self, job_id: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            store = await self._music_jobs()
            return {"status": "ok", "job": await asyncio.to_thread(store.get_job, jid)}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def list_music_jobs(self, states: Any = None, job_type: Any = None) -> Dict[str, Any]:
        try:
            st = _opt_str_list(states, "states", "invalid_states")
            jt = _opt_str(job_type, "job_type", "invalid_type")
            store = await self._music_jobs()
            jobs = await asyncio.to_thread(lambda: store.list_jobs(states=st, job_type=jt))
            return {"status": "ok", "jobs": jobs}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def cancel_music_job(self, job_id: Any = None, reason: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            why = _opt_str(reason, "reason", "invalid_reason")
            store = await self._music_jobs()
            job = await asyncio.to_thread(store.cancel_job, jid, why)
            self._music_wake_runner()
            self._music_job_changed(job)
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def list_music_takes(self, job_id: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            store = await self._music_jobs()
            return {"status": "ok", "takes": await asyncio.to_thread(store.list_takes, jid)}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def keep_music_take(self, take_id: Any = None) -> Dict[str, Any]:
        """Keep: the take is the person's. The library is not touched."""
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            store = await self._music_jobs()
            take = await asyncio.to_thread(store.keep_take, tid)
            job = await asyncio.to_thread(store.get_job, take["job_id"])
            self._music_job_changed(job)
            return {"status": "ok", "take": take, "job": job}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def discard_music_take(self, take_id: Any = None, reason_text: Any = None,
                                 reason_class: Any = None) -> Dict[str, Any]:
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            text = _opt_str(reason_text, "reason_text", "invalid_reason")
            cls = _opt_str(reason_class, "reason_class", "invalid_reason")
            store = await self._music_jobs()
            take = await asyncio.to_thread(lambda: store.discard_take(tid, reason_text=text, reason_class=cls))
            job = await asyncio.to_thread(store.get_job, take["job_id"])
            hidden = await self._music_hide_for_discard(take)
            self._music_job_changed(job)
            return {"status": "ok", "take": take, "job": job, **hidden}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def reject_all_music_takes(self, job_id: Any = None, reason_text: Any = None,
                                     reason_class: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            text = _opt_str(reason_text, "reason_text", "invalid_reason")
            cls = _opt_str(reason_class, "reason_class", "invalid_reason")
            store = await self._music_jobs()
            job = await asyncio.to_thread(lambda: store.reject_all_takes(jid, reason_text=text, reason_class=cls))
            takes = await asyncio.to_thread(store.list_takes, jid)
            for take in takes:
                if take.get("decision") == "rejected":
                    take.update(await self._music_hide_for_discard(take))
            self._music_job_changed(job)
            return {"status": "ok", "job": job, "takes": takes}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def measure_music_take_loudness(self, take_id: Any = None, force: Any = False) -> Dict[str, Any]:
        """Measure a take's integrated loudness, LRA and true peak with ffmpeg ebur128
        (`loudness.py`) and record it on the take, for the level-matched A/B. A take that
        has a measurement answers it as it is unless `force`. Slow: ffmpeg reads the
        whole file. Errors: `not_found`, `measure_failed` (no ffmpeg, no file, no summary)."""
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            store = await self._music_jobs()
            take = await asyncio.to_thread(store.get_take, tid)
            if take.get("loudness") and force is not True:
                return {"status": "ok", "take": take, "measured": False}
            path = path_of_file_uri(take["file"]["uri"])
            if path is None:
                raise MusicJobError("measure_failed", f"take {tid} has no local file ({take['file']['uri']!r})")
            try:
                loudness = await asyncio.to_thread(measure_loudness, path)
            except LoudnessError as exc:
                raise MusicJobError("measure_failed", str(exc))
            take = await asyncio.to_thread(store.set_take_loudness, tid, loudness)
            return {"status": "ok", "take": take, "measured": True}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def list_music_discard_reasons(self, prefix: Any = None, limit: Any = None) -> Dict[str, Any]:
        """The Discard-reason dictionary: what the person typed, most used first."""
        try:
            pre = _opt_str(prefix, "prefix", "invalid_params")
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                raise _bad("invalid_params", "limit", "a positive integer")
            store = await self._music_jobs()
            reasons = await asyncio.to_thread(lambda: store.list_reasons(pre, 50 if limit is None else limit))
            return {"status": "ok", "reasons": reasons}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def set_music_discard_reason_class(self, key: Any = None, reason_class: Any = None) -> Dict[str, Any]:
        """Attach a failure class to a dictionary entry (null clears it)."""
        try:
            k = _need_str(key, "key", "invalid_params")
            cls = _opt_str(reason_class, "reason_class", "invalid_params")
            store = await self._music_jobs()
            reason = await asyncio.to_thread(store.set_reason_class, k, cls)
            return {"status": "ok", "reason": reason}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def get_music_track(self, track_id: str) -> Dict[str, Any]:
        """The library entry and its `mp.track/1` (null while the duration is unknown).
        Slow: it waits on ffprobe for the duration, sample rate and channels."""
        try:
            return {"status": "ok", **(await (await self._music()).get_track(track_id))}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def get_music_track_peaks(self, track_id: Any = None, n: Any = None) -> Dict[str, Any]:
        """The waveform peaks of a library track (`peaks.py`): `{status, peaks, duration_s,
        cached}`. The file is the library's own path for `track_id`; the UI never sends
        one. Slow: the first request decodes the track with ffmpeg, later ones read
        `<DPC home>/music/peaks/<content_hash>.json`. Errors: `unknown_track`, `no_file`,
        `no_ffmpeg`, `decode_failed`, `timeout`, `invalid_params`."""
        try:
            library = await self._music()
            entry = library.get_entry(_need_str(track_id, "track_id", "invalid_params"))
            path = library.get_track_path(entry["id"])
            try:
                doc = await asyncio.to_thread(
                    get_peaks, path, entry.get("content_hash"), self.settings.dpc_home_dir, n, library._tools)
            except PeaksError as exc:
                return {"status": "error", "code": exc.code, "message": exc.message}
            return {"status": "ok", "peaks": doc["peaks"], "duration_s": doc["duration_s"],
                    "n": doc["n"], "cached": doc["cached"]}
        except Exception as exc:
            return _error(exc)

    # -- playlists ---------------------------------------------------------------

    async def list_music_playlists(self) -> Dict[str, Any]:
        try:
            return {"status": "ok", "playlists": (await self._music()).list_playlists()}
        except Exception as exc:
            return _error(exc)

    async def create_music_playlist(self, name: str, track_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        try:
            return {"status": "ok", "playlist": await (await self._music()).create_playlist(name, track_ids)}
        except Exception as exc:
            return _error(exc)

    async def update_music_playlist(self, playlist_id: str, name: Optional[str] = None,
                                    track_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Rename and/or set the full ordered `track_ids` (a reorder is one call)."""
        try:
            playlist = await (await self._music()).update_playlist(playlist_id, name=name, track_ids=track_ids)
            return {"status": "ok", "playlist": playlist}
        except Exception as exc:
            return _error(exc)

    async def delete_music_playlist(self, playlist_id: str) -> Dict[str, Any]:
        try:
            await (await self._music()).delete_playlist(playlist_id)
            return {"status": "ok", "deleted": playlist_id}
        except Exception as exc:
            return _error(exc)

    # -- listening notes: timecoded comments (`mp.comment/1`, music/comments.py) ----------

    async def _music_comments(self) -> MusicComments:
        await self._music()
        assert self._music_comments_instance is not None
        return self._music_comments_instance

    @slow_command
    async def list_music_comments(self, track_id: Any = None) -> Dict[str, Any]:
        """The track's comments, by start time. Keyed by the library entry id."""
        try:
            return {"status": "ok", "track_id": track_id, "comments": await (await self._music_comments()).list(track_id)}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def add_music_comment(self, track_id: Any = None, start_s: Any = None, end_s: Any = None,
                                text: Any = None, action: Any = None) -> Dict[str, Any]:
        """Add a point (`end_s` null) or region comment. `action` is `note` (the default);
        the other SPEC actions answer `action_unsupported`. Broadcasts `music_comments_updated`."""
        try:
            comment = await (await self._music_comments()).add(track_id, start_s, end_s, text, action)
        except Exception as exc:
            return _error(exc)
        self._music_broadcast("music_comments_updated", {"track_id": track_id})
        return {"status": "ok", "comment": comment}

    @slow_command
    async def update_music_comment(self, track_id: Any = None, comment_id: Any = None, start_s: Any = _UNSET,
                                   end_s: Any = _UNSET, text: Any = _UNSET, action: Any = _UNSET) -> Dict[str, Any]:
        """Change a comment; a field not passed stays, `end_s: null` makes it a point."""
        fields = {k: v for k, v in (("start_s", start_s), ("end_s", end_s), ("text", text), ("action", action))
                  if v is not _UNSET}
        try:
            comment = await (await self._music_comments()).update(track_id, comment_id, **fields)
        except Exception as exc:
            return _error(exc)
        self._music_broadcast("music_comments_updated", {"track_id": track_id})
        return {"status": "ok", "comment": comment}

    @slow_command
    async def delete_music_comment(self, track_id: Any = None, comment_id: Any = None) -> Dict[str, Any]:
        try:
            await (await self._music_comments()).delete(track_id, comment_id)
        except Exception as exc:
            return _error(exc)
        self._music_broadcast("music_comments_updated", {"track_id": track_id})
        return {"status": "ok", "deleted": comment_id}

    # -- lyrics ------------------------------------------------------------------

    @slow_command
    async def get_music_lyrics(self, track_id: str, refresh: bool = False) -> Dict[str, Any]:
        """The track's lyrics. Stored ones come back as they are; without them (or with
        `refresh`) the chain runs: .lrc sidecar, embedded tags, the local LRCLIB cache.
        It never sends a request (the player calls it on every track change); `chain`
        says what each step found, `lrclib` a cached LRCLIB outcome when there is one,
        `transcribe` the track's last Transcribe run in this process (or null). It never
        transcribes."""
        try:
            return {"status": "ok", **(await (await self._music_lyrics()).get(track_id, refresh=bool(refresh)))}
        except Exception as exc:
            return _error(exc)

    async def save_music_lyrics(self, track_id: str, lrc_text: str) -> Dict[str, Any]:
        """Store the person's LRC as `source: "manual"` under the DPC home."""
        try:
            return {"status": "ok", **(await (await self._music_lyrics()).save_manual(track_id, lrc_text))}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def lookup_music_lyrics_lrclib(self, track_id: str, artist: Optional[str] = None,
                                         title: Optional[str] = None, album: Optional[str] = None,
                                         replace: bool = False,
                                         overwrite_manual: bool = False) -> Dict[str, Any]:
        """The explicit "Find on LRCLIB", the only command that sends anything to
        lrclib.net (artist, title, album and duration). Stores the result when nothing
        is stored or `replace`; otherwise it comes back as `candidate`. Over manual or
        edited lyrics `replace` answers `overwrite_manual_required` unless
        `overwrite_manual` is passed too (the player asks the person first)."""
        try:
            lyrics = await self._music_lyrics()
            answer = await lyrics.lookup_lrclib(track_id, artist=artist, title=title, album=album,
                                                replace=bool(replace),
                                                overwrite_manual=overwrite_manual is True)
            return {"status": "ok", **answer}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def transcribe_music_lyrics(self, track_id: str, overwrite_manual: bool = False) -> Dict[str, Any]:
        """The explicit "Transcribe": DPC's transcription on the track's file (the voice
        provider choice, see the module docstring), its segments stored as timed lyrics
        with `source: "transcribe"`; text without segments as unsynced lines. Over manual
        or edited lyrics it answers `overwrite_manual_required` unless `overwrite_manual`
        is true (the player asks the person first). `get_music_lyrics` never runs it; it
        reports the run in `transcribe` while it goes on and after it ends."""
        try:
            lyrics = await self._music_lyrics()
            answer = await lyrics.transcribe(track_id, overwrite_manual=overwrite_manual is True)
            return {"status": "ok", **answer}
        except Exception as exc:
            return _error(exc)

    # -- MP3 export (music/export.py, Mike #210) -----------------------------------------

    _music_exporter_instance: Optional[MusicExporter] = None

    def _music_export_root(self) -> Path:
        """`[music] export_dir`, or `<DPC home>/exports/music`. Refused (`invalid_export_dir`)
        when it is not absolute or is a filesystem root."""
        raw = str(self._music_config_value("export_dir", "") or "").strip()
        if not raw:
            return default_export_root(self.settings.dpc_home_dir)
        chosen = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not chosen.is_absolute():
            raise ExportError("invalid_export_dir", f"[music] export_dir must be an absolute path: {raw}")
        chosen = Path(os.path.normpath(chosen))
        if chosen.parent == chosen:
            raise ExportError("invalid_export_dir", f"[music] export_dir cannot be a filesystem root: {chosen}")
        return chosen

    def _music_exporter(self) -> MusicExporter:
        root = self._music_export_root()
        current = self._music_exporter_instance
        if current is None or current.root != root:
            self._music_exporter_instance = MusicExporter(root)
        return self._music_exporter_instance

    def _music_export_excluded(self, path: str) -> Optional[str]:
        """The library scan's question: is this walked file an export the index lists?"""
        try:
            return self._music_exporter().is_export_path(path)
        except ExportError:
            return None

    async def _music_take_of(self, entry: Dict[str, Any]) -> tuple:
        """`(take, job)` of a generated library entry, from the job its lineage names, or
        `(None, None)`."""
        lineage = (entry.get("source") or {}).get("lineage") or {}
        job_id = lineage.get("job_id")
        if entry.get("origin") != "generated" or not isinstance(job_id, str) or not job_id:
            return None, None
        try:
            store = await self._music_jobs()
            takes = await asyncio.to_thread(store.list_takes, job_id)
            job = await asyncio.to_thread(store.get_job, job_id)
        except Exception as exc:
            logger.info("music export: the take of %s is not known (%s)", entry.get("id"), exc)
            return None, None
        uri = (entry.get("source") or {}).get("uri")
        take = next((t for t in takes if t.get("library_track_id") == entry["id"]), None)
        if take is None:
            take = next((t for t in takes if (t.get("file") or {}).get("uri") == uri), None)
        return take, job

    async def _music_export_source(self, library: MusicLibrary, entry: Dict[str, Any]) -> ExportSource:
        """What the exporter needs for one entry, resolved from the library by id: its file,
        content hash, names, stored lyrics, and for a generated take its sidecar and
        measured true peak."""
        title = entry.get("title") or entry["id"]
        path = library.get_track_path(entry["id"])
        if path is None:
            raise ExportError("no_file", f"{title!r} has no file now (it is missing)")
        if entry.get("hash_state") != "done" or not entry.get("content_hash"):
            raise ExportError("not_hashed", f"{title!r} is still being identified; try again in a moment")
        lyrics_text: Optional[str] = None
        lyrics_lang: Optional[str] = None
        try:
            stored = await (await self._music_lyrics()).load_stored(entry["id"])
        except Exception:
            stored = None
        if stored:
            lines = [str(line.get("text") or "") for line in stored.get("lines") or []]
            lyrics_text = "\n".join(lines).strip() or None
            lyrics_lang = stored.get("language")
        take, job = await self._music_take_of(entry)
        take_record = await asyncio.to_thread(read_take_sidecar, path) if entry.get("origin") == "generated" else None
        tp = basis = None
        loudness = (take or {}).get("loudness") or {}
        if isinstance(loudness.get("true_peak_dbtp"), (int, float)):
            tp, basis = float(loudness["true_peak_dbtp"]), "the take's ebur128 measurement (loudness.py)"
        if lyrics_text is None and job is not None:
            sent = (job.get("params") or {}).get("lyrics")
            if isinstance(sent, str) and sent.strip() and sent.strip() != "[Instrumental]":
                lyrics_text = sent.strip()
        return ExportSource(
            track_id=entry["id"], path=path, content_hash=entry["content_hash"], title=title,
            artist=entry.get("artist"), album=entry.get("album"), lyrics=lyrics_text, lyrics_language=lyrics_lang,
            true_peak_dbtp=tp, true_peak_basis=basis, take_record=take_record, origin=entry.get("origin"))

    @staticmethod
    def _music_watched_folder_of(library: MusicLibrary, root: Path) -> Optional[str]:
        """The watched folder that holds `root` (or is it), or None."""
        from .library import _is_inside, path_key
        key = path_key(str(root))
        for folder in library.list_folders():
            if _is_inside(key, path_key(folder["path"])):
                return folder["path"]
        return None

    @slow_command
    async def export_music_track(self, track_id: Any = None, preset: Any = None) -> Dict[str, Any]:
        """Export one library track as MP3 (`music/export.py`): `{status, track_id, path, name,
        already_exported, relinked, preset, limited_db, true_peak_out_dbtp}`. The same track
        and preset again answers the existing file. Slow: ffmpeg encodes and measures.
        Errors: `unknown_track`, `no_file`, `not_hashed`, `invalid_preset`, `no_ffmpeg`,
        `no_mp3_encoder`, `encode_failed`, `invalid_export_dir`."""
        try:
            tid = _need_str(track_id, "track_id", "invalid_params")
            chosen = MusicExporter.check_preset(_opt_str(preset, "preset", "invalid_preset"))
            library = await self._music()
            entry = library.get_entry(tid)
            source = await self._music_export_source(library, entry)
            exporter = self._music_exporter()
            answer = await asyncio.to_thread(exporter.export_track, source, chosen)
        except Exception as exc:
            return _error(exc)
        if not answer["already_exported"] or answer["relinked"]:
            self._music_broadcast("music_export_done", {"track_id": tid, "path": answer["path"]})
        return {"status": "ok", **answer}

    @slow_command
    async def export_music_playlist(self, playlist_id: Any = None, preset: Any = None) -> Dict[str, Any]:
        """Export a playlist as MP3 into `<root>/<Playlist>/NN - <Artist> - <Title>.mp3`: each
        track is exported once as a single (or found), then linked or copied in; hidden and
        missing tracks are skipped. `{status, folder, tracks: [{track_id, status, path |
        reason}], removed, kept}`. Errors: `unknown_playlist`, `smart_list`,
        `empty_playlist`, and the single-track ones."""
        try:
            pid = _need_str(playlist_id, "playlist_id", "invalid_params")
            chosen = MusicExporter.check_preset(_opt_str(preset, "preset", "invalid_preset"))
            library = await self._music()
            playlist = next((p for p in library.list_playlists() if p["id"] == pid), None)
            if playlist is None:
                raise MusicLibraryError("unknown_playlist", f"no playlist {pid}")
            if playlist.get("kind") != "user":
                raise MusicLibraryError("smart_list", "a smart list is not exported; make a playlist of its tracks")
            items: List[Any] = []
            for tid in playlist["track_ids"]:
                try:
                    entry = library.get_entry(tid)
                except MusicLibraryError:
                    items.append(SkippedTrack(tid, None, "not in the library", "unknown_track"))
                    continue
                if entry.get("hidden"):
                    items.append(SkippedTrack(tid, entry.get("title"), "hidden", "hidden"))
                    continue
                try:
                    items.append(await self._music_export_source(library, entry))
                except ExportError as exc:
                    items.append(SkippedTrack(tid, entry.get("title"), exc.message, exc.code))
            exporter = self._music_exporter()
            done: List[Dict[str, Any]] = []
            answer = await asyncio.to_thread(exporter.export_playlist, pid, playlist["name"], items, chosen, done.append)
        except Exception as exc:
            return _error(exc)
        for single in done:
            if not single["already_exported"] or single["relinked"]:
                self._music_broadcast("music_export_done", {"track_id": single["track_id"], "path": single["path"]})
        self._music_broadcast("music_export_done", {"playlist_id": pid, "path": answer["folder"]})
        return {"status": "ok", "playlist_id": pid, **answer}

    @slow_command
    async def get_music_export_info(self, track_id: Any = None) -> Dict[str, Any]:
        """Where exports go and what exists: `{export_root, default_root, inside_watched_folder,
        watched_folder, presets, default_preset, exports: {track_id: [{preset, path, name,
        exported_at, limited_db, true_peak_out_dbtp}]}}`, for one track with `track_id`, else
        for every track. Only exports whose file is there now are listed."""
        try:
            tid = _opt_str(track_id, "track_id", "invalid_params")
            library = await self._music()
            entries = [library.get_entry(tid)] if tid is not None else library.list_entries()
            hashes: Dict[str, List[str]] = {}
            for e in entries:
                if e.get("content_hash"):
                    hashes.setdefault(e["content_hash"], []).append(e["id"])
            exporter = self._music_exporter()
            exports = await asyncio.to_thread(exporter.exports_for, hashes)
            watched = self._music_watched_folder_of(library, exporter.root)
            return {"status": "ok", "export_root": str(exporter.root),
                    "default_root": str(default_export_root(self.settings.dpc_home_dir)),
                    "inside_watched_folder": watched is not None, "watched_folder": watched,
                    "presets": [{"id": k, "label": v["label"], "format": v["format"]} for k, v in PRESETS.items()],
                    "default_preset": DEFAULT_PRESET, "exports": exports}
        except Exception as exc:
            return _error(exc)


MUSIC_COMMANDS = (
    "list_music_folders",
    "add_music_folder",
    "remove_music_folder",
    "rescan_music_folder",
    "list_music_library",
    "get_music_track",
    "get_music_track_peaks",
    "hide_music_track",
    "unhide_music_track",
    "remove_missing_music_tracks",
    "delete_generated_music_file",
    "get_music_capabilities",
    "get_music_engine_status",
    "start_music_engine",
    "stop_music_engine",
    "propose_music_job",
    "get_music_job",
    "list_music_jobs",
    "cancel_music_job",
    "list_music_takes",
    "keep_music_take",
    "discard_music_take",
    "reject_all_music_takes",
    "measure_music_take_loudness",
    "list_music_discard_reasons",
    "set_music_discard_reason_class",
    "list_music_playlists",
    "create_music_playlist",
    "update_music_playlist",
    "delete_music_playlist",
    "get_music_lyrics",
    "save_music_lyrics",
    "lookup_music_lyrics_lrclib",
    "transcribe_music_lyrics",
    "get_music_stream_info",
    "list_music_comments",
    "add_music_comment",
    "update_music_comment",
    "delete_music_comment",
    "export_music_track",
    "export_music_playlist",
    "get_music_export_info",
)
