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
- a `local_whisper` provider is called as `provider.transcribe(path)`, the call
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
ends, with success or an error (`provider.unload_if_idle()`: on CUDA the weights go to the
CPU, are dropped and `torch.cuda.empty_cache()` frees the ~3 GB; on CPU the pipeline is
dropped; on MLX mlx_whisper's cached model is dropped). A model that was already loaded
when the run started (voice auto-transcribe keeps it warm) stays loaded, and so does one
another transcription is using at that moment: the provider counts its running
transcriptions under its load lock and refuses the unload while any is running.

**The track stream** (`get_music_stream_info`, ADR-051 N8 decided as option (i), D28):
`start_music_stream` opens `music/stream.py`'s loopback listener on `[music] stream_port`
(default 9996), which serves a library track's bytes by `track_id` with HTTP `Range`, a
per-process token and an `Origin` allowlist. The command hands the UI `{base_url, token, ranges_linux}`.

**The engine and the runner** (ADR-051 D30, SPEC §12 decisions 31-34): `music/acestep.py`
supervises the acestep.cpp server (settings `[music] acestep_binary`, `acestep_models_dir`,
`acestep_port`, `acestep_idle_stop_s`, `acestep_device`), and its `status()` is the job
store's engine probe, so the capabilities report the real state and a `generate_from_text`
job is queued only when the engine is installed. `music/runner.py` runs queued jobs one at a
time; `start_music_jobs` starts it at service start (resuming queued jobs) and
`stop_music_jobs` stops it and the server at shutdown. Each change the runner makes is
broadcast as `music_job_updated` `{job}`, and each engine state change as
`music_engine_status` `{engine}`; `get_music_job` answers the same state for a UI that polls.
"Stop X" on a job waiting for the card is the existing `stop_local_engine` command with the
holder's alias from `job.card_wait.holders`; nothing here stops another engine.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..local_api import slow_command
from .acestep import AceStepError, AceStepServer, read_config
from .capabilities import EngineStatus, MusicCapabilities
from .jobs import MusicJobError, MusicJobStore
from .library import DEFAULT_POLL_INTERVAL_S, MusicLibrary, MusicLibraryError
from .lyrics import MusicLyrics
from .runner import MusicJobRunner
from .stream import DEFAULT_STREAM_PORT, MusicStreamServer

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
    if isinstance(exc, AceStepError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    logger.exception("music command failed")
    return {"status": "error", "code": "internal", "message": str(exc)}


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
    _music_jobs_instance: Optional[MusicJobStore] = None
    _music_jobs_lock: Optional[asyncio.Lock] = None
    _music_engine_instance: Optional[AceStepServer] = None
    _music_runner_instance: Optional[MusicJobRunner] = None

    def _music_poll_interval_s(self) -> float:
        raw = self.settings.get("music", "poll_interval_s", str(DEFAULT_POLL_INTERVAL_S))
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("[music] poll_interval_s %r is not a number; using %s", raw, DEFAULT_POLL_INTERVAL_S)
            return DEFAULT_POLL_INTERVAL_S

    async def _music(self) -> MusicLibrary:
        if self._music_library_instance is None:
            self._music_library_instance = MusicLibrary(
                self.settings.dpc_home_dir, poll_interval_s=self._music_poll_interval_s())
        await self._music_library_instance.start()
        return self._music_library_instance

    def _music_lrclib_enabled(self) -> bool:
        """`[music] lrclib_enabled`: whether the lyrics chain may ask lrclib.net."""
        raw = self.settings.get("music", "lrclib_enabled", "true" if DEFAULT_LRCLIB_ENABLED else "false")
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
        loaded_before = self._music_whisper_loaded(provider)
        try:
            result = await provider.transcribe(str(path))
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
        finally:
            # Success, an error or a cancel: the run is over, so a model it loaded goes.
            await self._music_unload_whisper(provider, loaded_before)
        if not isinstance(result, dict):
            raise MusicLibraryError("transcription_failed", f"local Whisper ({alias}) gave no result")
        return {**result, "provider": result.get("provider") or LOCAL_WHISPER_TYPE,
                "model": getattr(provider, "model_name", None) or result.get("model")}

    @staticmethod
    def _music_whisper_loaded(provider: Any) -> bool:
        is_loaded = getattr(provider, "is_model_loaded", None)
        if not callable(is_loaded):
            return True  # cannot tell: treat as someone else's, never unload it
        try:
            return bool(is_loaded())
        except Exception:
            return True

    async def _music_unload_whisper(self, provider: Any, loaded_before: bool) -> None:
        """Unload the model after a run that loaded it, unless it is in use (see the
        module docstring). A failed unload is logged; it never changes the run's answer."""
        if loaded_before:
            return
        unload = getattr(provider, "unload_if_idle", None)
        if not callable(unload):
            return
        try:
            if await unload():
                logger.info("music: unloaded the Whisper model after the Transcribe run")
        except Exception:
            logger.exception("music: unloading the Whisper model after Transcribe failed")

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
        raw = self.settings.get("music", "stream_port", str(DEFAULT_STREAM_PORT))
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
        raw = self.settings.get("music", "stream_ranges_linux", "false")
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
                store = MusicJobStore(self.settings.dpc_home_dir,
                                      capabilities=MusicCapabilities(self._music_engine().status))
                await asyncio.to_thread(store.open)
                self._music_jobs_instance = store
        return self._music_jobs_instance

    # -- the engine and the runner (ADR-051 D30) --------------------------------------

    def _music_engine(self) -> AceStepServer:
        """The acestep.cpp supervisor, built on first use; its settings are re-read before
        each start."""
        if self._music_engine_instance is None:
            home = self.settings.dpc_home_dir

            def config():
                return read_config(self.settings.get, home)
            self._music_engine_instance = AceStepServer(config(), dpc_home=home, config_source=config,
                                                        on_change=self._music_engine_changed)
        return self._music_engine_instance

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
                    store, self._music_engine(), self._music, dpc_home=self.settings.dpc_home_dir,
                    on_job_change=self._music_job_changed)
            self._music_runner_instance.start()
        except Exception:
            logger.exception("music: the job runner did not start")

    async def stop_music_jobs(self) -> None:
        """Stop the runner and the acestep.cpp server; called at shutdown. A job still
        running is settled by the store's restart rule at the next start."""
        runner = self._music_runner_instance
        if runner is not None:
            await runner.stop()
        if self._music_engine_instance is not None:
            await self._music_engine_instance.shutdown()

    async def get_music_engine_status(self) -> Dict[str, Any]:
        """The engine's state, where its files are, and the job the runner holds. Who holds
        the GPU card is the local-engine status command's (owner-only) answer, and a waiting
        job's `card_wait` names the holder."""
        try:
            engine = self._music_engine()
            runner = self._music_runner_instance
            return {"status": "ok", "engine": engine.details(),
                    "runner": {"running": bool(runner and runner.running),
                               "current_job_id": runner.current_job_id if runner else None}}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def start_music_engine(self) -> Dict[str, Any]:
        """Start the acestep.cpp server by hand (also the way out of an `error` state).
        Slow: it waits for the server's /health."""
        try:
            engine = self._music_engine()
            await engine.start()
            return {"status": "ok", "engine": engine.details()}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def stop_music_engine(self, force: Any = False) -> Dict[str, Any]:
        """Stop the acestep.cpp server by hand to free the card. Refused (`engine_busy`)
        while a job runs on it, unless `force` (the job then fails)."""
        try:
            engine = self._music_engine()
            runner = self._music_runner_instance
            busy = runner.current_job_id if runner else None
            if busy and force is not True:
                return {"status": "error", "code": "engine_busy",
                        "message": f"music job {busy} is using the engine; cancel it first, or pass force"}
            stopped = await engine.stop("ui_stop")
            return {"status": "ok", "stopped": stopped, "engine": engine.details()}
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

    async def get_music_capabilities(self) -> Dict[str, Any]:
        try:
            store = await self._music_jobs()
            return {"status": "ok", "capabilities": store.capabilities.report()}
        except Exception as exc:
            return _error(exc)

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
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    async def get_music_job(self, job_id: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            store = await self._music_jobs()
            return {"status": "ok", "job": await asyncio.to_thread(store.get_job, jid)}
        except Exception as exc:
            return _error(exc)

    async def list_music_jobs(self, states: Any = None, job_type: Any = None) -> Dict[str, Any]:
        try:
            st = _opt_str_list(states, "states", "invalid_states")
            jt = _opt_str(job_type, "job_type", "invalid_type")
            store = await self._music_jobs()
            jobs = await asyncio.to_thread(lambda: store.list_jobs(states=st, job_type=jt))
            return {"status": "ok", "jobs": jobs}
        except Exception as exc:
            return _error(exc)

    async def cancel_music_job(self, job_id: Any = None, reason: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            why = _opt_str(reason, "reason", "invalid_reason")
            store = await self._music_jobs()
            job = await asyncio.to_thread(store.cancel_job, jid, why)
            self._music_wake_runner()
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    async def list_music_takes(self, job_id: Any = None) -> Dict[str, Any]:
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            store = await self._music_jobs()
            return {"status": "ok", "takes": await asyncio.to_thread(store.list_takes, jid)}
        except Exception as exc:
            return _error(exc)

    async def keep_music_take(self, take_id: Any = None) -> Dict[str, Any]:
        """Keep: the take is the person's. The library is not touched."""
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            store = await self._music_jobs()
            take = await asyncio.to_thread(store.keep_take, tid)
            job = await asyncio.to_thread(store.get_job, take["job_id"])
            return {"status": "ok", "take": take, "job": job}
        except Exception as exc:
            return _error(exc)

    async def discard_music_take(self, take_id: Any = None, reason_text: Any = None,
                                 reason_class: Any = None) -> Dict[str, Any]:
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            text = _opt_str(reason_text, "reason_text", "invalid_reason")
            cls = _opt_str(reason_class, "reason_class", "invalid_reason")
            store = await self._music_jobs()
            take = await asyncio.to_thread(lambda: store.discard_take(tid, reason_text=text, reason_class=cls))
            job = await asyncio.to_thread(store.get_job, take["job_id"])
            return {"status": "ok", "take": take, "job": job, **await self._music_hide_for_discard(take)}
        except Exception as exc:
            return _error(exc)

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
            return {"status": "ok", "job": job, "takes": takes}
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


MUSIC_COMMANDS = (
    "list_music_folders",
    "add_music_folder",
    "remove_music_folder",
    "rescan_music_folder",
    "list_music_library",
    "get_music_track",
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
    "list_music_playlists",
    "create_music_playlist",
    "update_music_playlist",
    "delete_music_playlist",
    "get_music_lyrics",
    "save_music_lyrics",
    "lookup_music_lyrics_lrclib",
    "transcribe_music_lyrics",
    "get_music_stream_info",
)
