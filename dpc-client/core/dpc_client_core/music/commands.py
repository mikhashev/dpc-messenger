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
  timestamps (`return_timestamps=True`, a9e2c8c3). When `[local_transcription] max_duration_seconds`
  is set (> 0), tracks are checked by probed duration instead of file size (0 = unlimited);
  when empty/unset, the legacy `[local_transcription] max_file_size_mb` (default 25) cap
  applies. A model
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

**Lossy export** (`music/export.py`, Mike #210 and #171; the design is CC's proposal [P]); presets
`mp3-v0` (default), `mp3-320` and `opus-160` (Opus 160 kbps in an Ogg `.opus` file for messengers):
`export_music_track {track_id, preset?, limit_true_peak?}`, `export_music_playlist {playlist_id, preset?,
limit_true_peak?}` (`limit_true_peak` defaults to true, the -1 dBTP limit Mike decided to keep; false makes
a separate, unlimited export) and
`get_music_export_info {track_id?}` and `delete_music_export {export_id | path}`. The source file is always the library's own path for the
track id; the UI never sends a path. The root is `[music] export_dir`
(`DPC_MUSIC_EXPORT_DIR`), by default `<DPC home>/exports/music`, outside every watched folder;
`get_music_export_info` says when a chosen root is inside one. A finished single export is
broadcast as `music_export_done {track_id, path}`, a playlist as `{playlist_id, path}` (the
folder). Every change to what the library lists is also broadcast, coalesced to at most one
per `DEFAULT_CHANGE_DELAY_S` window, as `music_library_changed {version, reasons, track_ids,
track_ids_truncated}`; the UI re-reads on it instead of polling. The take's measured true peak (`loudness.true_peak_dbtp`) decides whether the -1 dBTP
limiter runs; its sidecar and stored lyrics go into the file's tags (ID3 in an MP3, Vorbis comments in an Opus).
**For the agent tools** (music tools part 1, 2026-11-24; the tools call these on `CoreService`, they
are not on the UI door, except that `propose_music_job` and `cancel_music_job` take an optional
`requested_by`): `propose_music_job(..., requested_by={kind: "agent", agent_id, conversation_id?,
turn_id?})` records who asked and applies the limits `[music] agent_queue_max` (default 2) and
`turn_jobs_max` (default 1), read on every call with those defaults, so `settings.py` is unchanged;
`cancel_music_job(job_id, requested_by=...)` lets an agent cancel only its own job (`not_your_job`);
`cancel_music_jobs_of_turn(turn_id)` cancels a stopped turn's still-queued jobs; `get_music_take(take_id)`
resolves a take to its file path and facts without the job's request. A caller that sends no
`requested_by` is the person.
"Stop X" on a job waiting for the card is the existing `stop_local_engine` command with the
holder's alias from `job.card_wait.holders`; nothing here stops another engine.

**The describer of a same-style song** (`music/describe.py`, Mike 2026-11-26 option 3 "both"): the audio alias
`[music] describe_alias` names (`DPC_MUSIC_DESCRIBE_ALIAS`; unset means `music_flamingo` when that alias is a loaded
provider, `none` turns it off) describes a source for Same style. `describe_music_track {track_id, refresh?}` is the
form's "Describe source": it decodes the track's file, asks the describer once (or answers the description kept for
the same audio hash, unless `refresh`), keeps it by the hash and answers `{description, suggestion}` (the style line
and the Keep values the form fills, for the person to edit before Create). A same-style job on yue2.cpp asks the same
function through the runner and reuses that kept description. Never a rank: it describes.

**Lyrics preparation** (`prepare_music_lyrics {text, language?, choices?, engine?}`, music/lyrics_prep.py): read-only,
changes nothing, stores nothing. It answers `{report}`: syllables per line, a meter check per section, the
audit of the author's U+0301 marks, the stress status of every Russian word, the marked text, and the two
`records` the Create form puts in `params.lyrics_prep` (the one for the plain text, the one for the marked text
when the engine takes marks). The Russian stress dictionary is read from
`<model files folder>/lyrics/stress/ru/forms.sqlite` (`[local_engines] model_files_dir`, default `<DPC home>/models`);
without it the answer says `dictionary.state: "missing"` and the meter and audit still work.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import wave
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..local_api import slow_command
from . import job_schemas as js
from .acestep import AceStepServer, read_config
from .comments import ORIGINAL_VERSION_ID, MusicComments
from .capabilities import EngineStatus, MusicCapabilities
from .engine_base import MusicEngineError
from .engines import MusicEngines
from .export import (
    DEFAULT_PRESET, PRESETS, ExportError, ExportSource, MusicExporter, SkippedTrack, default_export_root,
    read_take_sidecar,
)
from .jobs import DEFAULT_AGENT_QUEUE_MAX, DEFAULT_TURN_JOBS_MAX, MusicJobError, MusicJobStore
from .library import DEFAULT_POLL_INTERVAL_S, MusicLibrary, MusicLibraryError
from .loudness import LoudnessError, measure_loudness
from .qa import qa_summary
from .lineage import LineageError, build_lineage, write_lineage_files
from .store import music_dir_of
from .lyrics import MusicLyrics
from . import lyrics_align
from . import lyrics_prep
from . import provenance as mp_provenance
from .peaks import PeaksError, get_peaks
from .runner import MusicJobRunner
from . import describe as mp_describe
from .stream import DEFAULT_STREAM_PORT, MusicStreamServer
from .yue2 import Yue2Server, read_config as read_yue2_config

LOCAL_WHISPER_TYPE = "local_whisper"
DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB = 25


def _positive_finite_seconds(value: Any) -> Optional[float]:
    """Return `value` as a finite float > 0 (never bool), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _non_negative_finite_number(value: Any) -> Optional[float]:
    """Return `value` as a finite float >= 0 (never bool), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return (0.0 if number == 0 else number) if math.isfinite(number) and number >= 0 else None


def read_local_transcription_max_duration_seconds(settings: Any) -> Optional[float]:
    """Read `[local_transcription] max_duration_seconds` from `settings`, returning None when
    unset, missing, mocked with a non-numeric return value, or invalid."""
    getter = getattr(settings, "get_local_transcription_max_duration_seconds", None)
    if not callable(getter):
        return None
    try:
        raw = getter()
    except Exception:
        logger.warning("transcription: reading max_duration_seconds failed; falling back to max_file_size_mb")
        return None
    if raw is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return _non_negative_finite_number(raw)


def read_local_transcription_max_file_size_mb(
    settings: Any, default: int = DEFAULT_LOCAL_TRANSCRIPTION_MAX_MB
) -> float:
    """Read the deprecated `[local_transcription] max_file_size_mb` cap from `settings`."""
    getter = getattr(settings, "get_local_transcription_max_file_size_mb", None)
    if callable(getter):
        try:
            raw = getter()
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                parsed = _non_negative_finite_number(raw)
                if parsed is not None:
                    return parsed
        except Exception:
            logger.warning("transcription: reading max_file_size_mb failed; using %s", default)
    return float(default)


def probe_audio_duration_seconds(path: Path) -> Optional[float]:
    """Probe audio duration in seconds without touching `whisper_provider.audio_seconds` more than
    once, falling back to pure-Python FLAC STREAMINFO / WAV headers when ffprobe and soundfile
    cannot read the file."""
    from ..providers import whisper_provider as wp
    from .identity import read_flac_streaminfo

    duration = _positive_finite_seconds(wp.audio_seconds(str(path)))
    if duration is not None:
        return duration
    try:
        flac = read_flac_streaminfo(path)
        if flac is not None:
            duration = _positive_finite_seconds(flac.duration_s)
            if duration is not None:
                return duration
    except Exception:
        pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
                if rate > 0 and frames > 0:
                    return _positive_finite_seconds(frames / rate)
        except Exception:
            pass
    return None


def evaluate_local_transcription_limit(
    settings: Any, path: Path, size_mb: float, *, declared_s: Any = None
) -> Dict[str, Any]:
    """Decide whether `path` fits the local Whisper limit configured on `settings`.

    When `[local_transcription] max_duration_seconds` is set:
    - `0`: unlimited (no file-size or duration gate).
    - `> 0`: compares probed duration (falling back to `declared_s` if unprobeable) against the
      duration cap; falls back to legacy `max_file_size_mb` only if duration cannot be determined.
    When unset/empty/invalid: applies the legacy `max_file_size_mb` (default 25 MB) check."""
    max_duration_s = read_local_transcription_max_duration_seconds(settings)
    probed_once = False
    probed_s: Optional[float] = None

    if max_duration_s is not None:
        if max_duration_s == 0:
            return {
                "allowed": True,
                "mode": "unlimited",
                "probed_once": False,
                "probed_s": None,
                "duration_s": None,
                "limit": 0.0,
                "code": None,
                "message": None,
            }
        probed_once = True
        probed_s = probe_audio_duration_seconds(path)
        effective_s = probed_s if probed_s is not None else _positive_finite_seconds(declared_s)
        if effective_s is not None:
            if effective_s > max_duration_s:
                return {
                    "allowed": False,
                    "mode": "duration",
                    "probed_once": True,
                    "probed_s": probed_s,
                    "duration_s": effective_s,
                    "limit": max_duration_s,
                    "code": "too_long",
                    "message": (
                        f"the track is {round(effective_s, 1):g} s, over the local transcription "
                        f"limit of {max_duration_s:g} s ([local_transcription] max_duration_seconds)"
                    ),
                }
            return {
                "allowed": True,
                "mode": "duration",
                "probed_once": True,
                "probed_s": probed_s,
                "duration_s": effective_s,
                "limit": max_duration_s,
                "code": None,
                "message": None,
            }
        logger.warning(
            "Could not probe audio duration for %s; falling back to [local_transcription] max_file_size_mb",
            path,
        )

    limit_mb = read_local_transcription_max_file_size_mb(settings)
    if size_mb > limit_mb:
        return {
            "allowed": False,
            "mode": "file_size",
            "probed_once": probed_once,
            "probed_s": probed_s,
            "duration_s": None,
            "limit": limit_mb,
            "code": "too_large",
            "message": (
                f"the file is {size_mb:.1f} MB, over the local transcription limit of {limit_mb:g} MB "
                f"([local_transcription] max_file_size_mb)"
            ),
        }
    return {
        "allowed": True,
        "mode": "file_size",
        "probed_once": probed_once,
        "probed_s": probed_s,
        "duration_s": None,
        "limit": limit_mb,
        "code": None,
        "message": None,
    }

logger = logging.getLogger(__name__)

DEFAULT_LRCLIB_ENABLED = True
# The longest lyrics `prepare_music_lyrics` takes (a long song is a few thousand characters).
MAX_PREP_TEXT_CHARS = 50000
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


def _opt_flag(value: Any, field: str, default: bool) -> bool:
    """An optional boolean command parameter (`default` when it is absent or null); anything but a
    JSON boolean is `invalid_params`."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise MusicLibraryError("invalid_params", f"{field} must be true or false")
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

    def _music_positive_int(self, key: str, default: int) -> int:
        """`[music] key` as an integer of at least 1, else `default` with a warning."""
        raw = self._music_config_value(key, str(default))
        try:
            value = int(str(raw).strip())
            if value >= 1:
                return value
        except (TypeError, ValueError):
            pass
        logger.warning("[music] %s %r is not an integer of at least 1; using %s", key, raw, default)
        return default

    def _music_agent_queue_max(self) -> int:
        """`[music] agent_queue_max` (default 2): an agent's jobs queued or running at once."""
        return self._music_positive_int("agent_queue_max", DEFAULT_AGENT_QUEUE_MAX)

    def _music_turn_jobs_max(self) -> int:
        """`[music] turn_jobs_max` (default 1): an agent's jobs per turn."""
        return self._music_positive_int("turn_jobs_max", DEFAULT_TURN_JOBS_MAX)

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
            self._music_library_instance.on_changed = self._music_library_changed
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

    async def _music_run_local_whisper(
        self,
        provider: Any,
        path: Path,
        *,
        purpose: str = "music Transcribe",
        bound_s: Optional[float] = None,
        trigger_download: bool = True,
        respect_size_limit: bool = True,
        non_evicting: bool = False,
    ) -> Dict[str, Any]:
        alias = getattr(provider, "alias", None) or LOCAL_WHISPER_TYPE
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError as exc:
            raise MusicLibraryError("no_file", f"the track's file cannot be read: {exc}") from exc
        ask_audio_s: Optional[float] = None
        if respect_size_limit:
            decision = await asyncio.to_thread(evaluate_local_transcription_limit, self.settings, path, size_mb)
            if not decision["allowed"]:
                raise MusicLibraryError(decision["code"], decision["message"])
            # Reuse the probe from `evaluate_local_transcription_limit` so `audio_seconds` runs at most once.
            ask_audio_s = (
                (decision["probed_s"] if decision["probed_s"] is not None else 0.0)
                if decision["probed_once"]
                else None
            )
        from ..providers.whisper_provider import (
            LocalWhisperProvider,
            asks_for_the_card,
            card_ask,
            card_wait_words,
            transcribe_with_route,
        )
        from .lyrics import show_card_wait

        if (
            not trigger_download
            and isinstance(provider, LocalWhisperProvider)
            and not getattr(provider, "model_loaded", False)
        ):
            from ..providers.model_sizes import is_model_cached

            if not await asyncio.to_thread(is_model_cached, getattr(provider, "model_name", "")):
                raise MusicLibraryError(
                    "model_not_downloaded",
                    f"Model '{provider.model_name}' is not downloaded yet.",
                )

        if non_evicting:
            if isinstance(provider, LocalWhisperProvider) and await asyncio.to_thread(asks_for_the_card, provider):
                from ..managers import card_lease

                snap = card_lease.queue_snapshot()
                if not snap.get("free") or snap.get("waiters"):
                    raise MusicLibraryError(
                        "card_busy",
                        "the GPU card is held or waited on; skipping background auto-align to avoid evicting warm engines",
                    )
            # Zero wait prevents Lease.use from asking any idle music engine to yield on a race.
            bound_s = 0.0

        # ADR-052 L4: explicit click (`bound_s=None`) waits until cancelled; background auto-align uses zero wait.
        ask = await asyncio.to_thread(
            card_ask, provider, purpose, str(path), bound_s=bound_s, audio_s=ask_audio_s
        )
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
                if trigger_download:
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

    async def _music_transcribe_with_whisper(self, provider: Any, path: Path) -> Dict[str, Any]:
        return await self._music_run_local_whisper(provider, path)

    async def _music_transcribe_for_align(self, path: Path) -> Dict[str, Any]:
        """Local-Whisper-only transcription for manual `align_music_lyrics`: never uploads to a
        remote or cloud provider, and bypasses the 25 MB cloud-API file size gate."""
        whisper, _alias = self._music_pick_transcriber()
        if whisper is None:
            raise MusicLibraryError(
                "transcription_unavailable",
                "alignment requires a local Whisper provider (remote or cloud providers are not used)",
            )
        return await self._music_run_local_whisper(
            whisper,
            path,
            purpose="music Align",
            bound_s=None,
            trigger_download=True,
            respect_size_limit=False,
        )

    def _music_auto_align_enabled(self) -> bool:
        fn = getattr(self.settings, "get_music_auto_align_lyrics", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                logger.warning("music: reading get_music_auto_align_lyrics failed; defaulting to True")
                return True
        raw = str(self._music_config_value("auto_align_lyrics", "true")).strip().lower()
        return raw not in _FALSE

    async def _music_auto_align_take(
        self,
        *,
        track_id: str,
        path: Path,
        lyrics: Any,
        duration_s: Optional[float] = None,
        replay: Optional[Mapping[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> None:
        """Best-effort forced alignment after a vocal take finishes and exits its engine card lease.
        Never fails or blocks take storage, skips when queued jobs exist, and never evicts warm engines."""
        try:
            if not self._music_auto_align_enabled():
                return
            runner = self._music_runner_instance
            if runner is not None and await asyncio.to_thread(runner.has_pending_jobs, exclude_job_id=job_id):
                logger.debug("music: auto-align skipped for %s (queued/running music jobs present)", track_id)
                return
            lines = MusicLyrics._known_lines_from_input(lyrics)
            if not lyrics_align.has_alignable_words(lines):
                return
            whisper, _alias = self._music_pick_transcriber()
            if whisper is None:
                logger.debug("music: auto-align skipped for %s (no local Whisper provider)", track_id)
                return
            anchors = lyrics_align.anchors_from_yue2_result(replay) if isinstance(replay, Mapping) else None

            async def _run_whisper(audio_path: Path) -> Dict[str, Any]:
                return await self._music_run_local_whisper(
                    whisper,
                    audio_path,
                    purpose="music Auto-align",
                    bound_s=0.0,
                    trigger_download=False,
                    respect_size_limit=False,
                    non_evicting=True,
                )

            lyr = await self._music_lyrics()
            res = await lyr.align(
                track_id,
                lyrics_text=lines,
                section_anchors=anchors,
                duration_s=duration_s,
                transcriber=_run_whisper,
                min_matches_required=True,
            )
            stats = res.get("alignment") or {}
            logger.info(
                "music: auto-aligned generated take %s (%s/%s words matched, sidecar=%s, synced_tag=%s)",
                track_id,
                stats.get("matched_words"),
                stats.get("total_lyric_words"),
                res.get("sidecar_written"),
                res.get("synced_tag_written"),
            )
        except MusicLibraryError as exc:
            logger.info("music: auto-align skipped for %s (%s: %s)", track_id, exc.code, exc.message)
        except Exception as exc:
            logger.warning("music: auto-align failed for %s (%s); keeping unsynced lyrics", track_id, exc)

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
                                                                     default_engine=engines.default,
                                                                     describer_probe=self._music_describer_status))
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

    def _music_library_changed(self, payload: Dict[str, Any]) -> None:
        self._music_broadcast("music_library_changed", payload)

    def _music_engine_changed(self, status: EngineStatus) -> None:
        self._music_broadcast("music_engine_status", {"engine": status.as_dict()})

    def _music_job_changed(self, job: Dict[str, Any]) -> None:
        self._music_broadcast("music_job_updated", {"job": job})

    def _music_wake_runner(self) -> None:
        if self._music_runner_instance is not None:
            self._music_runner_instance.wake()

    def _music_provenance_node_id(self) -> Optional[str]:
        """This node's public id for a take's provenance record (`node_id`), or None when the person
        turned `[music] provenance_node_id` off. Only the id peers see (`p2p_manager.node_id`), never key
        or token material; the lossy export withholds it (`provenance.withhold_node_id`)."""
        if str(self._music_config_value("provenance_node_id", "true")).strip().lower() in _FALSE:
            return None
        node_id = getattr(getattr(self, "p2p_manager", None), "node_id", None)
        return node_id if isinstance(node_id, str) else None

    async def start_music_jobs(self) -> None:
        """Start the job runner (it resumes queued jobs); called at service start. A
        failure is logged and leaves jobs queued."""
        mp_provenance.set_node_id_source(self._music_provenance_node_id)
        try:
            store = await self._music_jobs()
            if self._music_runner_instance is None:
                self._music_runner_instance = MusicJobRunner(
                    store, self._music_engine_set(), self._music, dpc_home=self.settings.dpc_home_dir,
                    on_job_change=self._music_job_changed, lyrics_get=self._music_stored_lyrics,
                    lyrics_save=self._music_save_take_lyrics, describer=self._music_describe_for_job,
                    auto_align=self._music_auto_align_take)
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

    async def _music_job_holds_the_card(self, job_id: str) -> bool:
        """Whether job `job_id` has the card and is using its engine: not while it is still `queued`
        (it waits in the card line, so it holds nothing), and not while a running job asks for the
        card again (its `card_wait` is set). An unreadable job counts as holding it."""
        try:
            store = await self._music_jobs()
            job = await asyncio.to_thread(store.get_job, job_id)
        except Exception:
            return True
        return job.get("state") != js.QUEUED and not job.get("card_wait")

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
        named (`card_busy`); the person stops that holder first. A refusal before the grant
        (`engine_busy` from the first check, `card_busy`) stops nothing; once the lease has let an
        idle holder go, a later refusal (the switch lock taken, or the other engine made busy, in
        the short window before `exclusive`) leaves that holder unloaded. Slow: it waits for the
        holders to let go and for /health."""
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
            # idle one is left running, and stopped below once the card is granted, so a start refused
            # here or at the card step has stopped nothing. (A refusal from `exclusive` below, in the
            # window after the grant, can follow an idle holder the lease already let go.)
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
        job then fails). A job that only waits for the card holds nothing on the engine, so it
        does not block the stop (the idle engine it waits behind is what the person stops)."""
        try:
            chosen = self._music_engine(engine)
            busy = self._music_engine_job(chosen.KEY)
            if busy and force is not True and await self._music_job_holds_the_card(busy):
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
            # The report probes the engines and reads their files: off the loop.
            return {"status": "ok", "capabilities": await asyncio.to_thread(store.capabilities.report)}
        except Exception as exc:
            return _error(exc)

    # -- the describer of a same-style song (music/describe.py) -------------------------------

    def _music_describe_alias(self) -> Tuple[Optional[str], str]:
        """`(alias, reason)`: the alias `[music] describe_alias` names, or `music_flamingo` when unset and loaded;
        `(None, why)` when there is none, it is turned off, or the named alias is not a loaded provider."""
        raw = self._music_config_value("describe_alias", "auto")
        text = str(raw).strip() if raw is not None else "auto"
        providers = dict(getattr(getattr(self, "llm_manager", None), "providers", None) or {})
        if text.lower() in mp_describe.DESCRIBE_OFF_WORDS:
            return None, "the describer is turned off ([music] describe_alias none)"
        if text.lower() == "auto":
            if mp_describe.DEFAULT_DESCRIBE_ALIAS in providers:
                return mp_describe.DEFAULT_DESCRIBE_ALIAS, f"{mp_describe.DEFAULT_DESCRIBE_ALIAS} (the default)"
            return None, (f"no describer: [music] describe_alias is unset and no {mp_describe.DEFAULT_DESCRIBE_ALIAS} "
                          "provider is loaded")
        if text not in providers:
            return None, f"the describe alias {text!r} is not a loaded provider"
        return text, f"{text} ([music] describe_alias)"

    def _music_describer(self) -> Tuple[Any, str]:
        """The describer's provider and alias, or `DescribeError` naming why there is none."""
        alias, reason = self._music_describe_alias()
        if alias is None:
            raise mp_describe.DescribeError("no_describer", reason)
        provider = dict(getattr(getattr(self, "llm_manager", None), "providers", None) or {}).get(alias)
        supports = getattr(provider, "supports_audio", None)
        try:
            ok = bool(callable(supports) and supports())
        except Exception:
            ok = False
        if not ok:
            raise mp_describe.DescribeError("no_audio", f"the describe alias {alias!r} takes no audio (a llama-server "
                                                        "alias with mmproj_kind audio is the describer's engine)")
        return provider, alias

    def _music_describer_status(self) -> Dict[str, Any]:
        """The capability report's `features.describe` (`MusicCapabilities.describer_status`)."""
        try:
            _provider, alias = self._music_describer()
        except mp_describe.DescribeError as exc:
            return {"status": "unsupported", "alias": None, "reason": exc.message}
        return {"status": "supported", "alias": alias,
                "reason": f"{alias} describes the source (genre, tags, instruments, vocals, tempo, key, time "
                          "signature, mood); it never ranks a take"}

    async def _music_describe_source(self, path: Path, content_hash: Optional[str], source_s: float, work_dir: Path,
                                     *, refresh: bool = False) -> Dict[str, Any]:
        """A `done` description of the audio at `path` (`describe.describe_audio`): the one kept for its hash unless
        `refresh`, else asked of the describer and kept. One at a time. `DescribeError` when it cannot be made."""
        home = self.settings.dpc_home_dir
        lock = self.__dict__.setdefault("_music_describe_lock", asyncio.Lock())
        async with lock:
            if not refresh:
                kept = await asyncio.to_thread(mp_describe.read_cached, home, content_hash)
                if kept is not None:
                    return {**kept, "cached": True}
            provider, alias = self._music_describer()
            doc = await mp_describe.describe_audio(provider, alias, Path(path), source_s=source_s,
                                                   content_hash=content_hash, work_dir=work_dir)
            if content_hash:
                try:
                    await asyncio.to_thread(mp_describe.write_cached, home, doc)
                except OSError:
                    logger.warning("music: the description of %s was not kept", content_hash, exc_info=True)
            return {**doc, "cached": False}

    async def _music_describe_for_job(self, path: Path, content_hash: Optional[str], source_s: float,
                                      work_dir: Path) -> Dict[str, Any]:
        """The runner's describer (`similar._describe_for_job`): a description, or the record of why there is none
        (`skipped` without a describer, `failed` when it ran and failed); never raises."""
        try:
            return await self._music_describe_source(path, content_hash, source_s, work_dir)
        except mp_describe.DescribeError as exc:
            if exc.code in ("no_describer", "no_audio"):
                return mp_describe.skipped(exc.message, content_hash)
            alias, _why = self._music_describe_alias()
            return mp_describe.failed(exc.message, alias, content_hash)

    @slow_command
    async def describe_music_track(self, track_id: Any = None, refresh: Any = False) -> Dict[str, Any]:
        """The form's "Describe source": `{track_id, description, suggestion: {caption, keep}}`, where `description`
        is the describer's record (`describe.py`: `answer`, `alias`, `model`, `windows`, `cached`) and `suggestion`
        what the form fills (the style line, and each Keep value the description gives). Errors: `invalid_params`,
        `invalid_source` (no such track, its file missing), `track_not_ready` (still being hashed),
        `no_describer`, `no_audio`, `decode_failed`, `failed` (the answer was not the schema's)."""
        try:
            tid = _need_str(track_id, "track_id", "invalid_params")
            if not isinstance(refresh, bool):
                raise MusicJobError("invalid_params", "refresh must be true or false", field="refresh")
            library = await self._music()
            try:
                entry = library.get_entry(tid)
            except MusicLibraryError:
                raise MusicJobError("invalid_source", f"no library track {tid!r}", field="track_id")
            path = library.get_track_path(tid)
            if entry.get("missing") or path is None or not Path(path).is_file():
                raise MusicJobError("invalid_source", "the track's file is missing; relink or rescan it first",
                                    field="track_id")
            content_hash = entry.get("content_hash")
            if not content_hash:
                raise MusicJobError("track_not_ready", "the track's audio is still being hashed; describe it again in "
                                                       "a moment", field="track_id")
            source_s = mp_describe.seconds_or_none(entry.get("duration_s"))
            if source_s is None:
                raise MusicJobError("track_not_ready", "the track's length is not known yet", field="track_id")
            work = self.settings.dpc_home_dir / "temp" / "music-describe"
            doc = await self._music_describe_source(Path(path), content_hash, source_s, work, refresh=refresh)
            answer = doc.get("answer") or {}
            keep = {k: v for k in js.KEEP_FROM_SOURCE_KEYS
                    if (v := mp_describe.keep_value(k, answer)) is not None}
            return {"status": "ok", "track_id": tid, "description": doc,
                    "suggestion": {"caption": answer.get("style") or "", "keep": keep}}
        except mp_describe.DescribeError as exc:
            return _error(MusicJobError(exc.code, exc.message))
        except Exception as exc:
            return _error(exc)

    async def _music_edit_source(self, track_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """What the job store needs of an edit's track (`MusicJobStore.create_job` `source`): its kind,
        audio hash and length, from the library; None when the id is not a track."""
        if not track_id:
            return None
        library = await self._music()
        try:
            entry = library.get_entry(track_id)
        except MusicLibraryError:
            raise MusicJobError("invalid_source", f"no library track {track_id!r}", field="track_id")
        if entry.get("missing"):
            raise MusicJobError("invalid_source", "the track's file is missing; relink or rescan it first",
                                field="track_id")
        return {"kind": (entry.get("source") or {}).get("kind"), "content_hash": entry.get("content_hash"),
                "duration_s": entry.get("duration_s"), "title": entry.get("title")}

    async def _music_edit_comment(self, strs: Dict[str, Any], region: Any, requested_by: Any) -> None:
        """An edit proposed without a comment (an agent's request, or the UI sending the form) gets
        one: the region comment with the action and the text, on the original version, by the person
        or the agent that asked. `strs` is completed in place (`comment_id`, `base_version_id`,
        `prompt`)."""
        if strs.get("base_version_id") is None:
            strs["base_version_id"] = ORIGINAL_VERSION_ID
        if strs.get("comment_id") is not None or not isinstance(region, dict):
            return
        rb = requested_by if isinstance(requested_by, dict) else {}
        author = ({"kind": "agent", "name": str(rb.get("agent_id") or "agent")[:80]}
                  if rb.get("kind") == js.REQUESTER_AGENT else None)
        text = strs.get("prompt") or f"{strs.get('action')} (proposed without a note)"
        comment = await (await self._music_comments()).add(
            strs["track_id"], region.get("t_start"), region.get("t_end"), text, strs.get("action"), author=author)
        strs["comment_id"] = comment["id"]
        strs["prompt"] = comment["text"]
        self._music_broadcast("music_comments_updated", {"track_id": strs["track_id"]})

    async def _music_stored_lyrics(self, track_id: str) -> Optional[Dict[str, Any]]:
        """The track's stored lyrics (`mp.lyrics/1`) for an edit, or None; never asks the internet."""
        try:
            answer = await (await self._music_lyrics()).get(track_id)
        except Exception:
            return None
        lyrics = answer.get("lyrics") if isinstance(answer, dict) else None
        return lyrics if isinstance(lyrics, dict) else None

    async def _music_song_lyrics(self, track_id: str) -> List[Tuple[str, Optional[float]]]:
        """The track's stored lyrics as `(text, start)` lines, empty lines trimmed at both ends (as the params
        keep them), `[]` when it has none stored. A store that cannot be read is `lyrics_unreadable`, never
        taken for an absence (Zcode #188, finding 4)."""
        try:
            answer = await (await self._music_lyrics()).get(track_id)
        except Exception as exc:
            raise MusicJobError("lyrics_unreadable", f"the track's stored lyrics could not be read: {exc}"[:300],
                                field="lyrics")
        lyrics = answer.get("lyrics") if isinstance(answer, dict) else None
        pairs: List[Tuple[str, Optional[float]]] = []
        for ln in (lyrics or {}).get("lines") or [] if isinstance(lyrics, dict) else []:
            if isinstance(ln, dict):
                t = ln.get("t_start")
                ok = isinstance(t, (int, float)) and not isinstance(t, bool) and t >= 0
                pairs.append((str(ln.get("text") or "").rstrip("\r\n"), float(t) if ok else None))
        while pairs and not pairs[-1][0].strip():
            pairs.pop()
        while pairs and not pairs[0][0].strip():
            pairs.pop(0)
        return pairs

    async def _music_song_params(self, job_type: str, params: Dict[str, Any], track_id: Optional[str]) -> Dict[str, Any]:
        """A song job's params with the track's stored lyrics filled in where they are reused and not
        given (an agent may name `lyrics_mode: reuse` alone). A cover that does not name its lyrics mode
        reuses them when the track has some, else goes without (the core refuses `missing_lyrics` where
        the engine needs them). Lines given under `reuse` must be the stored ones, else
        `reuse_lyrics_differ` (Zcode #188, finding 1: the take inherits the source's licence only for its
        own lyrics); `lyrics_source` and a yue2 cover's `lyrics_times` are this door's to fill."""
        out = dict(params)
        out.pop("lyrics_source", None)
        out.pop("lyrics_times", None)
        mode = out.get("lyrics_mode", js.SONG_DEFAULT_LYRICS_MODE.get(job_type))
        if mode != "reuse" or not track_id:
            return out
        pairs = await self._music_song_lyrics(track_id)
        stored = [text for text, _t in pairs]
        if out.get("lyrics") is not None:
            given = out["lyrics"].splitlines() if isinstance(out["lyrics"], str) else out["lyrics"]
            if isinstance(given, list) and all(isinstance(x, str) for x in given):
                given = [x.rstrip("\r\n") for x in given]
                while given and not given[-1].strip():
                    given.pop()
                while given and not given[0].strip():
                    given.pop(0)
                if given != stored:
                    raise MusicJobError("reuse_lyrics_differ", "lyrics_mode reuse sends the track's own stored lyrics; "
                                        "the lines given differ from them (leave lyrics out to reuse the stored ones)",
                                        field="lyrics")
        if any(t.strip() for t in stored):
            out["lyrics"] = stored
            out["lyrics_source"] = "stored"
            engine = out.get("engine", js.SONG_DEFAULT_ENGINE.get(job_type))
            if job_type == js.COVER and engine == js.ENGINE_YUE2 and any(t is not None for _x, t in pairs):
                out["lyrics_times"] = [t for _x, t in pairs]
        elif job_type == js.COVER and "lyrics_mode" not in out:
            out["lyrics_mode"] = "none"
        return out

    async def _music_save_take_lyrics(self, track_id: str, lrc_text: str) -> None:
        """A cut-with-shift take's re-timed lyrics, stored as its own (decision 27)."""
        await (await self._music_lyrics()).save_manual(track_id, lrc_text)

    @slow_command
    async def propose_music_job(self, type: Any = None, params: Any = None, track_id: Any = None,
                                base_version_id: Any = None, comment_id: Any = None, action: Any = None,
                                region: Any = None, prompt: Any = None,
                                idempotency_key: Any = None, requested_by: Any = None,
                                request: Any = None) -> Dict[str, Any]:
        """Propose a job. `requested_by` (`{kind: "person"|"agent", agent_id?, conversation_id?,
        turn_id?}`) says who asks; without it the person does (the UI door sends none). An
        agent's request is limited (`requester_queue_full`, `turn_job_limit`: `[music]
        agent_queue_max`, default 2, and `turn_jobs_max`, default 1, read on every call) and,
        with a `turn_id` and no `idempotency_key`, repeats return the same job.

        An `edit` (`track_id`, `action`, `region`, `prompt`) is checked against the library track
        (`listen_only_source`, `track_not_ready`); without a `comment_id` the region comment is made
        first, and `base_version_id` defaults to the original version. A song job (`generate_similar`,
        `cover`, `generate_from_lyrics`: `similar.py`) names its source `track_id`; the library's facts
        and, where they are reused, the stored lyrics fill its params."""
        try:
            job_type = _need_str(type, "type", "invalid_type")
            if not isinstance(params, dict):
                raise _bad("invalid_params", "params", "an object")
            if region is not None and not isinstance(region, dict):
                raise _bad("invalid_params", "region", "an object or null")
            strs = {k: _opt_str(v, k, "invalid_params") for k, v in (
                ("track_id", track_id), ("base_version_id", base_version_id), ("comment_id", comment_id),
                ("action", action), ("prompt", prompt), ("idempotency_key", idempotency_key))}
            if requested_by is not None and not isinstance(requested_by, dict):
                raise _bad("invalid_params", "requested_by", "an object or null")
            if request is not None and not isinstance(request, dict):
                raise _bad("invalid_params", "request", "an object or null")
            store = await self._music_jobs()
            queue_max, turn_max = self._music_agent_queue_max(), self._music_turn_jobs_max()
            source = None
            if job_type == "edit":
                source = await self._music_edit_source(strs.get("track_id"))
                if strs.get("comment_id") is None:
                    # Every check that can refuse runs before the comment is made, so a refusal leaves none.
                    probe = dict(strs, comment_id="cmt_" + "0" * 16,
                                 base_version_id=strs.get("base_version_id") or ORIGINAL_VERSION_ID)
                    checked = await asyncio.to_thread(lambda: store.check_job(
                        job_type, params, region=region, requested_by=requested_by, request=request,
                        source=source, agent_queue_max=queue_max, turn_jobs_max=turn_max, **probe))
                    if checked.get("repeat"):
                        return {"status": "ok", "job": checked["repeat"]}
                await self._music_edit_comment(strs, region, requested_by)
            elif job_type in js.SONG_TYPES:
                source = await self._music_edit_source(strs.get("track_id"))
                strs["base_version_id"] = strs.get("base_version_id") or ORIGINAL_VERSION_ID
                params = await self._music_song_params(job_type, params, strs.get("track_id"))
            job = await asyncio.to_thread(lambda: store.create_job(
                job_type, params, region=region, requested_by=requested_by, request=request, source=source,
                agent_queue_max=queue_max, turn_jobs_max=turn_max, **strs))
            if job.get("state") == "queued":
                self._music_wake_runner()
            self._music_job_changed(job)
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def check_music_job(self, type: Any = None, params: Any = None,
                              idempotency_key: Any = None, requested_by: Any = None, track_id: Any = None,
                              action: Any = None, region: Any = None, prompt: Any = None,
                              request: Any = None) -> Dict[str, Any]:
        """What `propose_music_job` with these arguments would answer, without making a job, for
        the agent runtime (music tools follow-up 12): `{repeat: job|null, params}`, or the error
        `propose_music_job` would give, and `unsupported` (with the reason) where it would make
        the job `unsupported`. Only a request that passes is put to the person for approval. An
        `edit` names its `track_id`, `action` and `region`; no comment is made by a check."""
        try:
            job_type = _need_str(type, "type", "invalid_type")
            if not isinstance(params, dict):
                raise _bad("invalid_params", "params", "an object")
            key = _opt_str(idempotency_key, "idempotency_key", "invalid_params")
            if requested_by is not None and not isinstance(requested_by, dict):
                raise _bad("invalid_params", "requested_by", "an object or null")
            if request is not None and not isinstance(request, dict):
                raise _bad("invalid_params", "request", "an object or null")
            if region is not None and not isinstance(region, dict):
                raise _bad("invalid_params", "region", "an object or null")
            store = await self._music_jobs()
            queue_max, turn_max = self._music_agent_queue_max(), self._music_turn_jobs_max()
            edit: Dict[str, Any] = {}
            if job_type == "edit":
                tid = _opt_str(track_id, "track_id", "invalid_params")
                edit = {"track_id": tid, "action": _opt_str(action, "action", "invalid_params"), "region": region,
                        "prompt": _opt_str(prompt, "prompt", "invalid_params"),
                        "comment_id": "cmt_" + "0" * 16, "base_version_id": ORIGINAL_VERSION_ID,
                        "source": await self._music_edit_source(tid)}
            elif job_type in js.SONG_TYPES:
                tid = _opt_str(track_id, "track_id", "invalid_params")
                edit = {"track_id": tid, "base_version_id": ORIGINAL_VERSION_ID,
                        "source": await self._music_edit_source(tid)}
                params = await self._music_song_params(job_type, params, tid)
            answer = await asyncio.to_thread(lambda: store.check_job(
                job_type, params, idempotency_key=key, requested_by=requested_by, request=request,
                agent_queue_max=queue_max, turn_jobs_max=turn_max, **edit))
            return {"status": "ok", **answer}
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
    async def cancel_music_job(self, job_id: Any = None, reason: Any = None,
                               requested_by: Any = None) -> Dict[str, Any]:
        """Cancel a job. Without `requested_by` (the UI door) it is the person's Cancel and takes
        any job. With an agent's `requested_by` only a job that agent proposed is cancelled;
        another's is refused `not_your_job`."""
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            why = _opt_str(reason, "reason", "invalid_reason")
            if requested_by is not None and not isinstance(requested_by, dict):
                raise _bad("invalid_params", "requested_by", "an object or null")
            store = await self._music_jobs()
            job = await asyncio.to_thread(lambda: store.cancel_job(jid, why, requested_by=requested_by))
            self._music_wake_runner()
            self._music_job_changed(job)
            return {"status": "ok", "job": job}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def cancel_music_jobs_of_turn(self, turn_id: Any = None, agent_id: Any = None) -> Dict[str, Any]:
        """Stop of a turn: cancel the jobs agents proposed in `turn_id` (of `agent_id` when given)
        that are still `queued`. A running job is left alone. Answers `{cancelled: [job, ...]}`,
        empty when nothing was queued. For the agent runtime, not the UI door."""
        try:
            tid = _need_str(turn_id, "turn_id", "invalid_params")
            aid = _opt_str(agent_id, "agent_id", "invalid_params")
            store = await self._music_jobs()
            jobs = await asyncio.to_thread(lambda: store.cancel_jobs_of_turn(tid, agent_id=aid))
            for job in jobs:
                self._music_job_changed(job)
            if jobs:
                self._music_wake_runner()
            return {"status": "ok", "turn_id": tid, "cancelled": jobs}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def get_music_take(self, take_id: Any = None) -> Dict[str, Any]:
        """A take by its id, for a caller that has no access to the files (ADR-051 decision D):
        where its file is and what is known about it, and nothing of the request that made it
        (no prompt, lyrics, params or payload; those live in the job). `take`: `take_id`,
        `job_id`, `track_id` (the source track, null for a new-track take), `library_track_id`,
        `kind`, `variant`, `state` (the take's decision: pending, accepted or rejected),
        `job_state`, `library_hidden` (true/false, null without a library entry), `path`
        (absolute, null when the URI is not a local file), `file_exists`,
        `duration_s`, `sample_rate`, `channels`, `content_hash`, `loudness`, `qa` (the stage-1
        `mp.qa/1`, null when not every field could be measured), `qa_report` (its gates, null on a
        take made before QA existed) and `qa_summary` (`{verdict, failed, not_measured, text}`,
        `qa.qa_summary`; read-only). Errors: `not_found`."""
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            store = await self._music_jobs()

            def read() -> tuple:
                take = store.get_take(tid)
                return take, store.get_job(take["job_id"])["state"]
            take, job_state = await asyncio.to_thread(read)
            file = take.get("file") or {}
            path = path_of_file_uri(file.get("uri"))
            exists = await asyncio.to_thread(lambda: path is not None and path.is_file())
            # Whether the take's library entry is hidden (a Discard hides it, decision 35): true,
            # false, or null when the take has no entry or the library does not know it.
            library_hidden = None
            if take.get("library_track_id"):
                try:
                    entry = (await self._music()).get_entry(take["library_track_id"])
                    library_hidden = bool(entry.get("hidden"))
                except Exception:
                    logger.debug("music: the library entry of take %s was not read", tid, exc_info=True)
                    library_hidden = None
            return {"status": "ok", "take": {
                "take_id": take["id"], "job_id": take["job_id"], "track_id": take.get("track_id"),
                "library_track_id": take.get("library_track_id"), "library_hidden": library_hidden,
                "kind": take["kind"],
                "variant": take.get("variant"), "state": take["decision"], "job_state": job_state,
                "path": str(path) if path is not None else None, "file_exists": exists,
                "duration_s": file.get("duration_s"), "sample_rate": file.get("sample_rate"),
                "channels": file.get("channels"), "content_hash": file.get("content_hash"),
                "loudness": take.get("loudness"), "qa": take.get("qa"), "qa_report": take.get("qa_report"),
                "qa_summary": qa_summary(take)}}
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

    async def _music_lineage(self, track_id: Any, job_id: Any) -> Dict[str, Any]:
        """The `mp.lineage/1` document for a track or job (`lineage.py`); read-only."""
        tid = _opt_str(track_id, "track_id", "invalid_params") or None
        jid = _opt_str(job_id, "job_id", "invalid_params") or None
        if not tid and not jid:
            raise _bad("invalid_params", "track_id", "given, or a job_id")
        store = await self._music_jobs()
        library = await self._music()

        def entry_of(lib_id: str) -> Optional[Dict[str, Any]]:
            try:
                return library.get_entry(lib_id)
            except MusicLibraryError:
                return None

        def build() -> Dict[str, Any]:
            jobs = store.list_jobs()
            takes = [t for j in jobs for t in store.list_takes(j["id"])]
            return build_lineage(
                jobs,
                takes,
                track_id=tid,
                job_id=jid,
                entry_of=entry_of,
                dpc_home_dir=self.settings.dpc_home_dir,
            )
        try:
            doc = await asyncio.to_thread(build)
        except LineageError as exc:
            raise MusicJobError(exc.code, exc.message)
        if tid and not doc["stats"]["jobs"]:
            library.get_entry(tid)   # a track the library does not know: unknown_track
        return doc

    @slow_command
    async def get_music_lineage(self, track_id: Any = None, job_id: Any = None) -> Dict[str, Any]:
        """The song lineage graph (`mp.lineage/1`, `lineage.py`) of the job chain holding a library
        track (`track_id`) or a job (`job_id`): the takes and edit siblings as nodes, what each
        was made from as edges, and the person's Keep / Discard (with the reason) as events. Read-only;
        answers `{lineage}`. A track no job explains is a graph of one source node. Errors:
        `invalid_params`, `not_found` (a job), `unknown_track`."""
        try:
            return {"status": "ok", "lineage": await self._music_lineage(track_id, job_id)}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def write_music_lineage_files(self, track_id: Any = None, job_id: Any = None) -> Dict[str, Any]:
        """On demand, write `lineage.json` and a static `lineage.html` for the same graph into the
        generated folder of the job it is asked for (or the track's job): `{lineage, files: {json,
        html}}`. It changes nothing else. Errors as `get_music_lineage`, and `no_job_folder` for a
        track with no job of its own (an imported file)."""
        try:
            doc = await self._music_lineage(track_id, job_id)
            focus = next((n for n in doc["nodes"] if n["id"] == doc["root"]["node_id"]), None)
            folder_job = (focus or {}).get("job_id")
            if not folder_job:
                raise MusicJobError("no_job_folder", "this track has no job folder of its own to write beside")
            folder = music_dir_of(self.settings.dpc_home_dir) / "generated" / folder_job
            files = await asyncio.to_thread(write_lineage_files, doc, folder)
            return {"status": "ok", "lineage": doc, "files": files}
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

    # -- lyrics preparation (music/lyrics_prep.py): read-only ------------------------------

    def _music_model_files_dir(self) -> Path:
        """The model files folder: `settings.get_model_files_dir()`, else `<DPC home>/models`."""
        fn = getattr(self.settings, "get_model_files_dir", None)
        if callable(fn):
            try:
                return Path(fn())
            except Exception:
                logger.warning("music: reading the model files folder failed; using <DPC home>/models")
        return Path(self.settings.dpc_home_dir) / "models"

    @slow_command
    async def prepare_music_lyrics(self, text: Any = None, language: Any = None, choices: Any = None,
                                   engine: Any = None) -> Dict[str, Any]:
        """The lyrics preparation report on `text` (module docstring above). The first call
        opens the 120 MB dictionary and reads its sha256, so the work runs in a thread."""
        try:
            if not isinstance(text, str):
                raise _bad("invalid_text", "text", "a string")
            if len(text) > MAX_PREP_TEXT_CHARS:
                raise _bad("text_too_long", "text", f"at most {MAX_PREP_TEXT_CHARS} characters")
            lang = _opt_str(language, "language", "invalid_language") or "auto"
            if choices is not None and not (isinstance(choices, dict)
                                            and all(isinstance(k, str) for k in choices)):
                raise _bad("invalid_choices", "choices", "an object mapping a word key to one of its options")
            eng = _opt_str(engine, "engine", "invalid_engine")
            path = lyrics_prep.default_dictionary_path(self._music_model_files_dir())

            def work() -> Dict[str, Any]:
                dictionary, state, message = lyrics_prep.open_dictionary(path)
                return lyrics_prep.prepare_lyrics(
                    text, lang, dictionary=dictionary, dictionary_state=state, dictionary_message=message,
                    choices=choices, engine=eng)

            return {"status": "ok", "report": await asyncio.to_thread(work)}
        except Exception as exc:
            return _error(exc)

    # -- listening notes: timecoded comments (`mp.comment/1`, music/comments.py) ----------

    async def _music_comments(self) -> MusicComments:
        library = await self._music()
        if self._music_comments_instance is None or self._music_comments_instance.library is not library:
            # A library set from outside (`_music` builds both together otherwise) gets its comments here.
            self._music_comments_instance = MusicComments(self.settings.dpc_home_dir, library)
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

    async def get_music_lyrics_transcript_raw(self, track_id: str) -> Dict[str, Any]:
        """Read only: the last Transcribe's kept unfiltered transcript for the track and the
        filter's report over it, including the dropped lines (`MusicLyrics.raw_transcript`),
        so the lyrics panel can show them after the run's own answer is gone. `present` is
        false when no Transcribe has run on this track. Writes and sends nothing."""
        try:
            return {"status": "ok", **(await (await self._music_lyrics()).raw_transcript(track_id))}
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

    @slow_command
    async def align_music_lyrics(self, track_id: str, overwrite_manual: bool = False) -> Dict[str, Any]:
        """Force-align existing known/unsynced lyrics of `track_id` against its audio using local
        Whisper word timestamps, keeping exact verbatim lyric text, storing `source: "aligned"`
        in DPC home, and returning `{status: "ok", lyrics, applied, lrc, alignment}`."""
        try:
            lyrics = await self._music_lyrics()
            use_transcriber = (
                self._music_transcribe_for_align
                if getattr(lyrics.transcriber, "__func__", None) is MusicCommandsMixin._music_transcribe_audio
                else lyrics.transcriber
            )
            answer = await lyrics.align(
                track_id,
                overwrite_manual=overwrite_manual is True,
                transcriber=use_transcriber,
            )
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
        synced_lines: Optional[List[Dict[str, Any]]] = None
        lyrics_source: Optional[str] = None
        try:
            stored = await (await self._music_lyrics()).load_stored(entry["id"])
        except Exception:
            stored = None
        if stored:
            lines = [str(line.get("text") or "") for line in stored.get("lines") or []]
            lyrics_text = "\n".join(lines).strip() or None
            lyrics_lang = stored.get("language")
            lyrics_source = stored.get("source")
            if stored.get("synced") is not False:
                timed = [ln for ln in (stored.get("lines") or [])
                         if isinstance(ln, dict) and ln.get("t_start") is not None]
                if timed:
                    synced_lines = list(stored.get("lines") or [])
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
            true_peak_dbtp=tp, true_peak_basis=basis, take_record=take_record, origin=entry.get("origin"),
            synced_lines=synced_lines, lyrics_source=lyrics_source)

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
    async def export_music_track(self, track_id: Any = None, preset: Any = None,
                                 limit_true_peak: Any = None) -> Dict[str, Any]:
        """Export one library track as MP3 or Opus (`music/export.py`): `{status, track_id, path, name,
        already_exported, relinked, preset, limit_true_peak, limited_db, true_peak_out_dbtp}`. The same
        track, preset and `limit_true_peak` again answers the existing file; `limit_true_peak` is true
        unless the person switched it off. Slow: ffmpeg encodes and measures.
        Errors: `unknown_track`, `no_file`, `not_hashed`, `invalid_preset`, `invalid_params`, `no_ffmpeg`,
        `no_mp3_encoder`, `no_opus_encoder`, `encode_failed`, `invalid_export_dir`."""
        try:
            tid = _need_str(track_id, "track_id", "invalid_params")
            chosen = MusicExporter.check_preset(_opt_str(preset, "preset", "invalid_preset"))
            limit = _opt_flag(limit_true_peak, "limit_true_peak", True)
            library = await self._music()
            entry = library.get_entry(tid)
            source = await self._music_export_source(library, entry)
            exporter = self._music_exporter()
            answer = await asyncio.to_thread(exporter.export_track, source, chosen, limit)
        except Exception as exc:
            return _error(exc)
        if not answer["already_exported"] or answer["relinked"]:
            self._music_broadcast("music_export_done", {"track_id": tid, "path": answer["path"]})
            library.notify_changed("export", [tid])
        return {"status": "ok", **answer}

    @slow_command
    async def export_music_playlist(self, playlist_id: Any = None, preset: Any = None,
                                    limit_true_peak: Any = None) -> Dict[str, Any]:
        """Export a playlist as MP3 or Opus into `<root>/<Playlist>/NN - <Artist> - <Title>.<ext>`: each
        track is exported once as a single (or found), then linked or copied in; hidden and
        missing tracks are skipped. `{status, folder, tracks: [{track_id, status, path |
        reason}], removed, kept}`. Errors: `unknown_playlist`, `smart_list`,
        `empty_playlist`, and the single-track ones."""
        try:
            pid = _need_str(playlist_id, "playlist_id", "invalid_params")
            chosen = MusicExporter.check_preset(_opt_str(preset, "preset", "invalid_preset"))
            limit = _opt_flag(limit_true_peak, "limit_true_peak", True)
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
            answer = await asyncio.to_thread(exporter.export_playlist, pid, playlist["name"], items, chosen,
                                             done.append, limit)
        except Exception as exc:
            return _error(exc)
        for single in done:
            if not single["already_exported"] or single["relinked"]:
                self._music_broadcast("music_export_done", {"track_id": single["track_id"], "path": single["path"]})
                library.notify_changed("export", [single["track_id"]])
        self._music_broadcast("music_export_done", {"playlist_id": pid, "path": answer["folder"]})
        return {"status": "ok", "playlist_id": pid, **answer}

    @slow_command
    async def get_music_export_info(self, track_id: Any = None) -> Dict[str, Any]:
        """Where exports go and what exists: `{export_root, default_root, inside_watched_folder,
        watched_folder, presets, default_preset, exports: {track_id: [{preset, path, name,
        exported_at, limited_db, true_peak_out_dbtp}]}}`, for one track with `track_id`, else
        for every track. Only exports whose file is there now are listed; each carries `export_id`
        (what `delete_music_export` takes), `size_bytes` and `playlists`: the playlist-folder files
        of the same song, `[{playlist_id, playlist_name, folder, path, name, how: hardlink|copy}]`."""
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
                    "default_preset": DEFAULT_PRESET, "limit_true_peak_default": True, "exports": exports}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def delete_music_export(self, export_id: Any = None, path: Any = None) -> Dict[str, Any]:
        """Delete one single MP3 export's file and its index entry: `export_id` (from
        `get_music_export_info`) or `path`, exactly one. Only a file the export index lists and
        that lies inside the export root can go. `{status, export_id, path, name, preset, format,
        deleted, file_missing, playlist_copies}`; `playlist_copies` lists the playlist-folder files
        of the same song, which stay. Errors: `invalid_params`, `unknown_export`,
        `outside_export_root`, `file_in_use` (Windows), `permission_denied`, `delete_failed`."""
        try:
            eid = _opt_str(export_id, "export_id", "invalid_params")
            raw_path = _opt_str(path, "path", "invalid_params")
            exporter = self._music_exporter()
            answer = await asyncio.to_thread(exporter.delete_export, export_id=eid or None, path=raw_path or None)
            library = await self._music()
        except Exception as exc:
            return _error(exc)
        library.notify_changed("export", [answer["track_id"]] if answer.get("track_id") else [])
        return {"status": "ok", **answer}


# The commands the UI door may call (every one is in `local_api.ALLOWED_COMMANDS`). Not listed on
# purpose: `cancel_music_jobs_of_turn`, `get_music_take` and `check_music_job`, for the agent tools only, which call
# them on `CoreService` directly and are kept off the UI allowlist.
MUSIC_COMMANDS = (
    "list_music_folders",
    "add_music_folder",
    "remove_music_folder",
    "rescan_music_folder",
    "list_music_library",
    "get_music_track",
    "get_music_track_peaks",
    "get_music_lineage",
    "write_music_lineage_files",
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
    "get_music_lyrics_transcript_raw",
    "save_music_lyrics",
    "lookup_music_lyrics_lrclib",
    "transcribe_music_lyrics",
    "align_music_lyrics",
    "get_music_stream_info",
    "list_music_comments",
    "add_music_comment",
    "update_music_comment",
    "delete_music_comment",
    "prepare_music_lyrics",
    "export_music_track",
    "export_music_playlist",
    "get_music_export_info",
    "delete_music_export",
)
