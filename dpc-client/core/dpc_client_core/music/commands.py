"""The `local_api` front door of the music library (ADR-051 D11).

`MusicCommandsMixin` is mixed into `CoreService`; `local_api` dispatches a command by
`getattr(core_service, command)`, so each `async def` here is one command, and each
must also be in `local_api.ALLOWED_COMMANDS`. The agent door (`music_*` tools) will
call the same `MusicLibrary` methods.

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
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..local_api import slow_command
from .library import DEFAULT_POLL_INTERVAL_S, MusicLibrary, MusicLibraryError
from .lyrics import MusicLyrics

LOCAL_WHISPER_TYPE = "local_whisper"
DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB = 25

logger = logging.getLogger(__name__)

DEFAULT_LRCLIB_ENABLED = True
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, MusicLibraryError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    logger.exception("music command failed")
    return {"status": "error", "code": "internal", "message": str(exc)}


class MusicCommandsMixin:
    """Music library commands for `CoreService`."""

    _music_library_instance: Optional[MusicLibrary] = None
    _music_lyrics_instance: Optional[MusicLyrics] = None

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
    "list_music_playlists",
    "create_music_playlist",
    "update_music_playlist",
    "delete_music_playlist",
    "get_music_lyrics",
    "save_music_lyrics",
    "lookup_music_lyrics_lrclib",
    "transcribe_music_lyrics",
)
