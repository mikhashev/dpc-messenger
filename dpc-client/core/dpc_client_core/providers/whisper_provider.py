# dpc_client_core/providers/whisper_provider.py

import asyncio
import gc
import math
import os
import logging
import threading
import time
from typing import Awaitable, Callable, Dict, Any, Optional, List

from .base import AIProvider, ModelNotCachedError
from .model_sizes import model_size_bytes

# The revision model_sizes.py's measured row is keyed on.
WHISPER_MODEL_REVISION = "41f01f3fe87f28c78e2fbf8b568835947dd65ed9"

logger = logging.getLogger(__name__)

# What a missing package of the optional `voice-memory` extra tells the user.
_VOICE_MEMORY_HINT = (
    "local Whisper transcription needs the voice-memory extra (not installed); "
    "from dpc-client/core run: uv sync --extra voice-memory --inexact"
)


DEFAULT_IDLE_UNLOAD_S = 300.0
# How long shutdown waits for running transcriptions before it unloads anyway.
SHUTDOWN_DRAIN_S = 10.0

# mlx-whisper's `hallucination_silence_threshold` for the music path (`with_detail`): a segment that
# sits in more than this much silence around a suspect word is skipped.
MUSIC_HALLUCINATION_SILENCE_S = 2.0
# The HF pipeline leaves the end of the LAST word (or chunk) as None. On the music path that end is
# estimated from the words themselves instead of being the end of the file: a song with an outro
# would otherwise get a last word held for the whole outro and its line split (follow-up 11B).
# The median length of the words that did get an end, held to these bounds; the default when none did.
OPEN_WORD_DEFAULT_S = 0.5
OPEN_WORD_MIN_S = 0.2
OPEN_WORD_MAX_S = 1.5
# The same for an open last SEGMENT (the word-timestamp fallback): half a second per word, held to
# these bounds, so a last line of a few words is never stretched over an instrumental outro. A
# segment this length is below the lyrics filter's 7 s line limit, so the limit never fires on it.
OPEN_SEGMENT_PER_WORD_S = 0.5
OPEN_SEGMENT_MIN_S = 1.0
OPEN_SEGMENT_MAX_S = 6.0

# The adaptive batch descent of `_transcribe_loaded`: over each length (audio seconds) the batch is
# divided by its divisor. The warm figures are kept by the same bands (ADR-052 S3, Johnny #24-2), so
# a band is a batch size: 16 up to 60 s, 8 up to 120 s, 4 up to 180 s, 2 over 180 s (batch 16).
_BATCH_DESCENT = ((180.0, 8), (120.0, 4), (60.0, 2))
_LENGTH_BANDS = ("<=60", "<=120", "<=180", ">180")


def length_band(audio_s: Any) -> Optional[str]:
    """The band of `_LENGTH_BANDS` an audio length falls in, on `_BATCH_DESCENT`'s thresholds (60 s
    itself is "<=60", as the descent keeps batch 16 at 60 s), or None for an unknown length."""
    seconds = _a_positive(audio_s)
    if seconds is None:
        return None
    for (over, _), band in zip(_BATCH_DESCENT, _LENGTH_BANDS[:0:-1]):  # >180, <=180, <=120
        if seconds > over:
            return band
    return "<=60"


# Start figures per device until this provider measures its own (ADR-052 L10, L12). `warm` is the
# real-time ratio (audio s per wall s) and `penalty_s` the seconds a first call after a load adds on
# top of `audio / warm`, both by length band; `load_first_s` is a model's first load on a device in
# this process, `load_reload_s` any later load of it there (`_LOADED_WEIGHTS`). Sources (ADR-052,
# "GPU figures on speech" for cuda, L12's table for cpu):
#   cuda warm: the slowest clip's median per band: en_30 21.1x, ru_118 27.4x, en_300 15.9x; <=180 is
#     estimated from the batch-2 figure (batch 4 was never measured).
#   cuda penalty_s 1.0 in every band: ru_10's first call after the load, +0.91 s, rounded up. Only
#     the <=60 band is measured; the bands over 60 s carry ru_10's figure, where the log shows a
#     first call of 118 s and longer within run-to-run spread (≈0), so they err long by about 1 s.
#   cuda load_first_s 12.8 (a fresh process), load_reload_s 0.8 (the backend's reload after an
#     unload, 0.76-0.83 s; Forge's 0.6-0.9 s is a direct `from_pretrained` outside the backend, not
#     the `pipeline(...)` path).
#   cpu warm: 0.91x (10 s in 11 s), 1.15x (120 s in 104 s), 0.98x (300 s in 306 s; <=180 takes it).
#     The carrier is L12's table: numbers reported in chat, slower than the repo's own measurement
#     (`ideas/measurements/whisper-gpu-bench-2026-11-22/cpu_run.log`: ru_30 1.5x, ru_118 1.1x) and
#     Forge's runs (2.36x / 1.53x / 1.42x at 30 / 118 / 300 s). Kept as conservative seeds: L12
#     picks the twin less often than it could. The bands are not monotonic (<=120 1.15x against
#     <=180 0.98x), so an estimate jumps at 120/121 s by the table, not by data (≈19 s at 120 s).
#   cpu penalty_s 0: L12's "load + first call ≈ 9 s" is all in load_first_s 9.0; load_reload_s 1.0
#     is the running backend's log, "Whisper model loaded in 1.0 seconds (device=cpu)". The 9.0 is
#     a cold process's: with torch already imported the twin loaded in 1.09 s (Forge #60).
# A device absent here (mps, mlx) is unknown until measured.
_START_FIGURES: Dict[str, Dict[str, Any]] = {
    "cuda": {"warm": {"<=60": 21.1, "<=120": 27.4, "<=180": 15.9, ">180": 15.9},
             "penalty_s": {band: 1.0 for band in _LENGTH_BANDS},
             "load_first_s": 12.8, "load_reload_s": 0.8},
    "cpu": {"warm": {"<=60": 10.0 / 11.0, "<=120": 120.0 / 104.0, "<=180": 300.0 / 306.0,
                     ">180": 300.0 / 306.0},
            "penalty_s": {band: 0.0 for band in _LENGTH_BANDS},
            "load_first_s": 9.0, "load_reload_s": 1.0},
}
# The (device, model) pairs a Whisper load has run on in this process (any provider object): the next
# load of that model on that device is a reload. Per process because a providers reload builds new
# objects; per device and model because the reload figure is a second read of the same weights, which
# neither the twin's load (for the GPU) nor another model's load is. A pair not loaded yet reads the
# first-load figure, which errs long where the imports are already paid (ADR-052 S3 follow-ups 6, 7).
_LOADED_WEIGHTS: set = set()
# A batch-descent length (the estimate's) and the decoded length further apart than this are logged
# (S3 follow-up 6, Ark #49-3): the warm figure is recorded against the decoded one.
_LENGTH_MISMATCH_S = 1.0
# ADR-052 L4: the margin a person's door keeps for the transcription itself, `max(10 s, estimate)`.
MIN_MARGIN_S = 10.0
# ADR-052 L11 (S3e): what a loaded Whisper holds on the card beyond its weights while it decodes, MiB,
# by the call's length band: torch's reserved peak plus the CUDA context (485 MiB, the larger figure,
# Strata resident) less the 1547 MiB of turbo fp16 weights, rounded up to 100. `gpu_run1.log`
# (2026-11-22, empty card): <=60 s (batch 16) 1984-2618 reserved, the 2618 of ru_60 (two 30 s chunks
# in one batch); <=120 s (batch 8) 3840-3948; >180 s (batch 2) 2660. The peak follows the chunks
# decoded at once (1, 2 or 4), so <=180 s (batch 4, never measured) is **estimated** at the batch-8
# figure, four chunks at once. Start values until Forge's live run measures each band beside strata.exe.
_WHISPER_OVERHEAD_BY_BAND_MIB = {"<=60": 1600, "<=120": 2900, "<=180": 2900, ">180": 1600}
# The worst band: the lease's own `vram_mib` (its row) and the figure for a call of unknown length.
_WHISPER_OVERHEAD_MIB = max(_WHISPER_OVERHEAD_BY_BAND_MIB.values())
# The words a Stop or a refusal adds about a Whisper holder (ADR-052 2.2 item 7).
_STOP_HINT = "It lets go when idle; Stop unloads it now."

# ADR-052 L12 (slice S3d): the CPU twin of a GPU alias. Its alias is the GPU alias plus this suffix;
# it lives beside its GPU alias, never in `providers.json` nor in the providers manager's list.
CPU_TWIN_SUFFIX = "@cpu"
# The twin's own idle unload (L12, "Residency": short, proposed 60 s; Mike #71). A start value.
TWIN_IDLE_UNLOAD_S = 60.0
# L12, "Enough RAM": a cold CPU route is taken only when available host RAM covers the twin plus a
# margin. Both are read only here; the values are kept. What they stand for (Forge's measurement,
# 2026-11-23, this machine, CUDA hidden, 4 runs): the twin loads fp32 weights, 3086 MiB (not the
# 1543 of fp16); from an empty process `import torch` adds 656 MiB, the weights 3101, the pipeline
# 19, the transcription 914-1355 by batch; total RSS +4527 MiB (30 s, batch 16), +5125 (118 s,
# batch 4), +4688 (300 s, batch 2). So `_TWIN_RAM_MIB` 4096 is about the import plus the weights,
# and `_RAM_MARGIN_MIB` 1024 is about the transcription's activations: the "margin" is the second
# half of the load, not headroom. 5120 MiB in all covers every measured run but 118 s at batch 4.
_TWIN_RAM_MIB = 4096
_RAM_MARGIN_MIB = 1024
# L12's three settings, `[local_transcription]` (settings.py reads the file first): the code defaults
# when no source is registered (a provider built outside the service). Item 9 (2026-11-23): cpu_max_s
# 600 and the factor 1.0; cpu_max_audio_s 300 (Mike #71).
DEFAULT_CPU_MAX_S = 600.0
DEFAULT_CPU_MAX_AUDIO_S = 300.0
DEFAULT_CPU_CONTENTION_FACTOR = 1.0
_CPU_ROUTE_KEYS = {"cpu_max_s": DEFAULT_CPU_MAX_S, "cpu_max_audio_s": DEFAULT_CPU_MAX_AUDIO_S,
                   "cpu_contention_factor": DEFAULT_CPU_CONTENTION_FACTOR}
# The two caps whose 0 turns the CPU route off (Mike, 2026-11-24, Q3), as `idle_unload_s` 0 is never
# and `queue_wait_s` 0 is do not wait. The factor is a multiplier, not a switch.
_CPU_ROUTE_SWITCHES = ("cpu_max_s", "cpu_max_audio_s")
_cpu_route_source: Optional[Callable[[], Dict[str, Any]]] = None
# ADR-052 L12 point 3 (Mike, 2026-11-24, Q2): the local Whisper transcriptions running on the CPU now
# (a CPU twin's, or any local Whisper whose model runs on the CPU), by call, holding the alias. Each
# is CPU contention for the next choice.
_CPU_RUNS: Dict[int, str] = {}


def cpu_transcriptions_running() -> List[str]:
    """The aliases of the local Whisper transcriptions running on the CPU now, one per call, in the
    order they began (L12 point 3)."""
    return list(_CPU_RUNS.values())


def set_cpu_route_source(source: Optional[Callable[[], Dict[str, Any]]]) -> None:
    """Where L12 reads `cpu_max_s`, `cpu_max_audio_s` and `cpu_contention_factor` (a dict of them). A
    getter, read on every choice, so a settings change applies without a restart."""
    global _cpu_route_source
    _cpu_route_source = source


def cpu_route_settings() -> Dict[str, float]:
    """L12's three numbers: the registered source's, each one that is not a figure above 0 replaced
    by the code default (logged). `cpu_max_s` or `cpu_max_audio_s` at 0 stays 0: the CPU route off
    (Mike, 2026-11-24)."""
    values = dict(_CPU_ROUTE_KEYS)
    source = _cpu_route_source
    if source is None:
        return values
    try:
        read = source() or {}
    except Exception:
        logger.warning("Whisper: L12's settings are unreadable; using the defaults", exc_info=True)
        return values
    for key, default in _CPU_ROUTE_KEYS.items():
        if key in _CPU_ROUTE_SWITCHES and _a_zero(read.get(key)):
            values[key] = 0.0
            continue
        figure = _a_positive(read.get(key))
        if figure is None:
            if key in read:
                logger.warning("Whisper: [local_transcription] %s %r is not a figure above 0; using %g",
                               key, read.get(key), default)
            continue
        values[key] = figure
    return values


def available_ram_mib() -> Optional[float]:
    """Available host RAM in MiB (`psutil.virtual_memory().available`, the one instrument of L12's
    gate, log and UI), or None when it cannot be read."""
    try:
        import psutil
        return _a_positive(psutil.virtual_memory().available / 2**20)
    except Exception:
        logger.debug("Whisper: the host's available RAM could not be read", exc_info=True)
        return None


def cpu_ram_check() -> tuple:
    """(ok, words): whether available host RAM covers a CPU Whisper (`_TWIN_RAM_MIB`) plus the margin
    (ADR-052 L12, "Enough RAM"). RAM that cannot be read does not cover it: a route that cannot be
    shown to fit is not taken."""
    need = _TWIN_RAM_MIB + _RAM_MARGIN_MIB
    available = available_ram_mib()
    if available is None:
        return False, f"the host's available RAM could not be read ({need} MiB needed)"
    if available < need:
        return False, f"{available:.0f} MiB of host RAM available, {need} MiB needed"
    return True, f"{available:.0f} MiB of host RAM available, {need} MiB needed"


def is_cpu_twin(provider: Any) -> bool:
    """Whether `provider` is a GPU alias's CPU twin (ADR-052 L12): never a default, never preloaded."""
    return getattr(provider, "is_cpu_twin", False) is True


class RoutedToCpu(Exception):
    """L12 sent this call to the GPU alias's CPU twin (ADR-052 3.1, "Selection"). Not a refusal:
    nothing waited and nothing was asked of the card. `transcribe_with_route` catches it and calls
    `twin` with the same arguments. `decision` is what L12 read; `if_it_fails` is set for an agent's
    tool inside its own turn on a local alias (section 3.5), whose twin failure is refused at once."""

    def __init__(self, twin: "LocalWhisperProvider", decision: Dict[str, Any],
                 if_it_fails: Optional[Callable[[str], Exception]] = None):
        super().__init__(f"routed to the CPU twin '{twin.alias}'")
        self.twin = twin
        self.decision = decision
        self.if_it_fails = if_it_fails


class TwinDidNotLoad(RuntimeError):
    """The CPU twin's load failed (its RAM check, a load error) before any audio was processed."""


async def transcribe_with_route(provider: Any, audio_path: str, release_if_loaded_here: bool = False,
                                **kwargs: Any) -> Dict[str, Any]:
    """The selector every door calls instead of `provider.transcribe` (ADR-052 3.1, L12): the call
    as asked, and when the GPU alias's L12 says CPU (`RoutedToCpu`), the CPU twin with the same
    `language`, `task`, length and `release_if_loaded_here`. The card's own arguments (`purpose`,
    `wait_s`, `expected_s`, `on_wait`) have no use there; `check` runs once before the twin starts, as
    it would at a grant. No switch in mid-flight (Zcode #69), but a twin that did not load has not
    flown (Johnny #63-5): that call goes to the card's line (`cpu_route=False`), or inside an agent's
    own turn (3.5, `if_it_fails`) is refused at once. A failure while transcribing is the call's.

    The door's `wait_s` is a deadline from this call's start (Zcode #88-4): the line gets what is left
    of it after L12's choice and the twin's failed load, never the whole bound again."""
    # Passed only when set, as each door passed it before the selector.
    released = {"release_if_loaded_here": True} if release_if_loaded_here else {}
    bound_s = kwargs.get("wait_s")
    deadline = None if bound_s is None else time.monotonic() + max(0.0, float(bound_s))
    try:
        return await provider.transcribe(audio_path, **released, **kwargs)
    except RoutedToCpu as routed:
        twin = routed.twin
        check = kwargs.get("check")
        if check is not None:
            await check()
        try:
            result = await twin.transcribe(audio_path, release_if_loaded_here,
                                           language=kwargs.get("language"), task=kwargs.get("task"),
                                           audio_s=kwargs.get("audio_s"),
                                           with_detail=bool(kwargs.get("with_detail")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if routed.if_it_fails is not None:
                raise routed.if_it_fails(str(exc) or type(exc).__name__) from exc
            if not isinstance(exc, TwinDidNotLoad):
                raise
            logger.info("Whisper '%s': the CPU twin did not load (%s); %s goes to the line for the card",
                        getattr(provider, "alias", provider), exc, kwargs.get("purpose") or "the call")
        else:
            return {**(result or {}), "device": "cpu"}
    if deadline is not None:
        kwargs["wait_s"] = max(0.0, deadline - time.monotonic())
    return await provider.transcribe(audio_path, **released, **kwargs, cpu_route=False)


def _a_zero(value: Any) -> bool:
    """Whether `value` is the number 0 (a figure or its text), never a bool."""
    if isinstance(value, bool):
        return False
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def _a_positive(value: Any) -> Optional[float]:
    """A finite figure above 0, else None (unknown, never 0: ADR-052 L10)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def audio_seconds(audio_path: str) -> Optional[float]:
    """The length of an audio file, or None when it cannot be read (ADR-052 L10: "the duration is
    ffprobe's"). ffprobe when it is on PATH, read as `service.measure_recording_seconds` reads it;
    else soundfile, which reads WAV, FLAC and Ogg but not WebM. 0 is unknown. Blocking."""
    try:
        from pathlib import Path
        from ..music.identity import MediaTools, probe_metadata

        tools = MediaTools.discover()
        if tools.ffprobe:
            seconds = _a_positive(probe_metadata(Path(audio_path), tools).get("duration_s"))
            if seconds is not None:
                return seconds
    except Exception:
        logger.debug("Whisper: ffprobe could not read the length of %s", audio_path, exc_info=True)
    try:
        import soundfile as sf
        return _a_positive(sf.info(audio_path).duration)
    except Exception:
        return None


def card_ask(provider: Any, purpose: str, audio_path: str, *, bound_s: Optional[float] = None,
             caller_left_s: Optional[float] = None, audio_s: Optional[float] = None,
             declared_s: Any = None) -> Dict[str, Any]:
    """What a door passes to `transcribe` for the card (ADR-052 L4, the S3 row): `purpose`, its
    `wait_s` and the transcription's `expected_s`. Blocking (it may run ffprobe): call it in a thread.

    `expected_s` is the provider's own estimate for this file (`expected_seconds`), None when unknown
    or when the provider keeps no figures. `wait_s` follows L4, "the queue's deadline is never longer
    than its caller's": `min(caller_left_s - margin, bound_s)`, the margin being `max(10 s,
    expected_s)` (10 s when the estimate is unknown). Either side None drops out; both None is no
    deadline (`wait_s=None`, until cancelled). A peer's door passes neither: the lease line gives a
    peer what is left of its 120 s trip (`card_lease.PEER_TRIP_S`, L13).

    Slice S3a builds the ask and the doors pass it; since S3b `transcribe` hands these three to
    `CardLease.use` (`purpose` names the use, `wait_s` bounds the wait in the lease line, `expected_s`
    is what a later waiter's estimate counts for this call).

    The length is `audio_s` when the door has it, else the probe's (`audio_seconds`); when the probe
    reads none (no ffprobe, and soundfile does not read WebM, the recorder's default) it is
    `declared_s`, the recorder's own `duration_seconds` (ADR-052 L10: "ffprobe's, else the
    recorder's declared one"; Johnny #24-3). The probe wins; the declared length is the fallback.

    One length by value (Johnny #30-6): when the length is known (probe or declared), the ask
    carries it as `audio_s`, whether or not the provider keeps an estimator (Ark #44-2), and the call's batch descent and its recorded run read that same number
    instead of the decoded samples' (which may differ by a fraction of a second across a band
    boundary), so the band the estimate read is the band the call runs and records in."""
    if audio_s is None:
        audio_s = audio_seconds(audio_path)
    if _a_positive(audio_s) is None:
        audio_s = _a_positive(declared_s)
    estimate = getattr(provider, "expected_seconds", None)
    expected_s = estimate(audio_s) if callable(estimate) else None
    wait_s = None if bound_s is None else max(0.0, float(bound_s))
    if caller_left_s is not None:
        margin = max(MIN_MARGIN_S, expected_s or 0.0)
        left = max(0.0, float(caller_left_s) - margin)
        wait_s = left if wait_s is None else min(wait_s, left)
    ask: Dict[str, Any] = {"purpose": purpose, "wait_s": wait_s, "expected_s": expected_s}
    if _a_positive(audio_s) is not None:
        ask["audio_s"] = float(audio_s)
    return ask


def asks_for_the_card(provider: Any) -> bool:
    """Whether a transcription on `provider` would ask this node's card lease (ADR-052 3.1, queueing
    condition 2, slice S3c): a local Whisper whose route is not the CPU. A cloud provider, a
    `device: cpu` alias and a model that fell back to the CPU do not.

    An `auto` device not detected yet is detected here, as the call itself would route it
    (`_route_device`: torch's CUDA check, no model load; Johnny #30-7), so an unloaded `auto` alias
    on a machine with no GPU is not skipped after a refusal. Only a detection that fails counts as a
    card's, the side a cascade the card has just refused must err on. It may import torch: blocking,
    call it in a thread.

    A question writes nothing (Ark #44-2, S3d): a detection made here is not kept; the call keeps its
    own (`_route_device(remember=True)` in `_route_for_the_card` and `expected_seconds`), and this
    reads it once there is one."""
    if (getattr(provider, "config", None) or {}).get("type") != "local_whisper":
        return False
    current = provider._current_owner() if hasattr(provider, "_current_owner") else provider
    if getattr(current, "model_loaded", False):
        in_use = current._device_in_use()
        if in_use is not None:
            return in_use != "cpu"
    device = getattr(current, "device", "auto")
    if device == "auto":
        device = getattr(current, "_estimate_device", None) or getattr(current, "_detected_device", None)
    if device is None:
        route = getattr(current, "_route_device", None)
        try:
            device = route(remember=False) if callable(route) else None
        except Exception:
            logger.debug("Whisper: the device of %s could not be detected; counted as the card's",
                         getattr(current, "alias", current), exc_info=True)
            device = None
        if device is None:
            return True
    return device != "cpu"


def card_wait_words(estimate: Optional[Dict[str, Any]]) -> str:
    """A person's line for a transcription waiting for the card (ADR-052 section 4): "transcription
    queued behind yue2.cpp (music job J), about 6 min" or "..., time unknown". From the lease's wait
    estimate (L10), which the doors' `on_wait` receives."""
    estimate = estimate or {}
    behind = ", ".join(str(name) for name in estimate.get("behind") or []) or "another job on the card"
    wait_s = _a_positive(estimate.get("wait_s")) if estimate.get("known") else None
    if wait_s is None:
        when = "time unknown"
    elif wait_s < 90:
        when = f"about {max(1, round(wait_s))} s"
    else:
        when = f"about {round(wait_s / 60)} min"
    if wait_s is not None and estimate.get("upper_bound"):
        # An engine's yield is bounded by its stop (never measured): say "at most", as the
        # music engine line does.
        when = f"at most {when}"
    return f"transcription queued behind {behind}, {when}"


def whisper_vram_mib(model_name: str, audio_s: Any = None) -> Optional[int]:
    """The declared peak of a loaded Whisper model in MiB (ADR-052 3.1 and L11): the weights from
    the measured size table plus the overhead of the call's length band (`length_band`, the batch
    descent's), the worst band's `_WHISPER_OVERHEAD_MIB` when the length is unknown, as for the
    lease's own row; else None (unknown, never 0). Never asks the network
    (`model_sizes.measured_size_bytes`). A CPU device holds no lease and has no such figure."""
    from .model_sizes import measured_size_bytes

    size = measured_size_bytes(model_name)
    if not size:
        return None
    band = length_band(audio_s)
    return size // 2**20 + (_WHISPER_OVERHEAD_MIB if band is None else _WHISPER_OVERHEAD_BY_BAND_MIB[band])


def _shares_with_music(holder: Any) -> bool:
    """L11's one cell (decision 6): a Whisper lease may be granted beside a busy music lease, never
    beside another Whisper, an embedder or a supervisor (Mike #68)."""
    return getattr(holder, "kind", None) == "music"


_idle_unload_source: Optional[Callable[[], float]] = None


def set_idle_unload_source(source: Optional[Callable[[], float]]) -> None:
    """Where providers read `[local_transcription] idle_unload_s` (seconds, 0 = never).
    A getter, read on every schedule, so a settings change applies without a restart."""
    global _idle_unload_source
    _idle_unload_source = source


_unloaded_listener: Optional[Callable[[str, str, str], None]] = None


def set_unloaded_listener(listener: Optional[Callable[[str, str, str], None]]) -> None:
    """Called as `listener(alias, model_name, reason)` after every unload (idle, deferred,
    released after a run, switch, shutdown), so the UI can drop its "model is warm" state.
    Injected by the service; the provider never imports the broadcaster."""
    global _unloaded_listener
    _unloaded_listener = listener


class LocalWhisperProvider(AIProvider):
    """
    Local Whisper transcription provider with multi-platform GPU support.

    Supports:
    - openai/whisper-large-v3 (1.55B params, 99 languages, MIT license)
    - **MLX** acceleration (Apple Silicon - M1/M2/M3/M4)
    - **CUDA** acceleration with torch.compile (NVIDIA GPUs)
    - **MPS** acceleration (macOS Metal Performance Shaders)
    - Flash Attention 2 (optional, 20% additional speedup for CUDA)
    - Chunked long-form transcription (speed vs accuracy trade-off)
    - Lazy loading (model loads on first transcription request)

    Performance:
    - NVIDIA RTX 3060 (CUDA): ~10-13x real-time, ~3GB VRAM
    - Apple M1/M2 (MLX): ~10-15x real-time, unified memory
    - CPU: ~1-2x real-time, ~6GB RAM

    Reference: https://huggingface.co/openai/whisper-large-v3
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Model configuration
        self.model_name = config.get("model", "openai/whisper-large-v3-turbo")
        self.device = config.get("device", "auto")  # 'mlx', 'cuda', 'mps', 'cpu', or 'auto'
        self.compile_model = config.get("compile_model", True)
        self.use_flash_attention = config.get("use_flash_attention", False)
        self.chunk_length_s = config.get("chunk_length_s", 30)
        self.batch_size = config.get("batch_size", 16)
        self.language = config.get("language", "auto")
        self.task = config.get("task", "transcribe")  # 'transcribe' or 'translate'
        self.lazy_loading = config.get("lazy_loading", True)

        # Whisper invents filler on non-speech audio: subtitle credits, applause,
        # or a token looped dozens of times. These are its own guards against that
        # — a segment that trips a threshold is re-decoded at the next temperature.
        # Measured on a 30-min recording: repetition loops 86/36/29 -> 0, +16% time.
        self.temperature_fallback = tuple(
            config.get("temperature_fallback", (0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
        )
        self.compression_ratio_threshold = config.get("compression_ratio_threshold", 1.35)
        self.logprob_threshold = config.get("logprob_threshold", -1.0)
        self.no_speech_threshold = config.get("no_speech_threshold", 0.6)
        self.condition_on_prev_tokens = config.get("condition_on_prev_tokens", False)

        # Model state
        self.pipeline = None
        self.model_loaded = False
        self._load_lock = asyncio.Lock()
        self._detected_device = None
        # Transcriptions running on the loaded model now: counted under `_load_lock`
        # by `transcribe`, so `unload_if_idle` never pulls the model from under one.
        self._active_transcriptions = 0
        # Set while no transcription runs; shutdown waits on it.
        self._drained = asyncio.Event()
        self._drained.set()
        # An unload asked for while a transcription ran: done when the last one ends.
        self._unload_when_idle = False
        # The one pending idle/deferred unload task. `_idle_epoch` changes on every
        # transcription start or end, so a task that wakes late sees it is stale.
        self._idle_task: Optional["asyncio.Task"] = None
        self._idle_epoch = 0
        self._unloading = False  # an unload runs in its thread now (held under `_load_lock`)
        self._unload_reason = "requested"  # the reason a deferred unload reports
        self._retired = False  # replaced by a reloaded provider object, or shut down
        # The object that took over this one's model in a providers reload: a
        # transcription still running here is counted and ended on that object.
        self._successor: Optional["LocalWhisperProvider"] = None
        # ADR-052 S3a: the speed this provider measured, over `_START_FIGURES` and in its shape, and
        # the (device, key) of the last load measured; the device the model is loaded on now (a load
        # may fall back to the CPU); the calls since that load (0: the next one is the first, which
        # pays `penalty_s`); `auto` resolved for an estimate.
        self._measured: Dict[str, Dict[str, Any]] = {}
        self._last_load: Optional[tuple] = None
        self._loaded_device: Optional[str] = None
        self._calls_since_load = 0
        self._estimate_device: Optional[str] = None
        # ADR-052 S3b: this alias's standing on the card, built on the first call that runs on a GPU
        # device (never for the CPU) and handed over in a providers reload; the calls holding it now,
        # {token: (monotonic start, expected_s, audio_s)}, and the load running under it now,
        # {"since": monotonic start, "device": d} or empty, both shared with a successor, for its
        # `eta_s`.
        self._card_lease: Optional[Any] = None
        self._card_runs: Dict[int, Any] = {}
        self._card_load: Dict[str, Any] = {}
        # ADR-052 L12 (S3d): `is_cpu_twin` marks a GPU alias's CPU twin (built by `_the_cpu_twin`, its
        # config naming the GPU alias in `_cpu_twin_of`); `_cpu_twin` is this GPU alias's twin, built
        # on the first call L12 routes there and handed over in a providers reload; `last_route` is
        # L12's last choice for this alias, as logged.
        self.is_cpu_twin = bool(config.get("_cpu_twin_of"))
        self._cpu_twin: Optional["LocalWhisperProvider"] = None
        self.last_route: Optional[Dict[str, Any]] = None

        logger.info(f"LocalWhisperProvider '{alias}' initialized (model={self.model_name}, device={self.device})")

    def _build_generate_kwargs(self) -> Dict[str, Any]:
        """Assemble generate_kwargs: language/task plus the anti-hallucination guards.

        temperature follows the provider-wide sentinel (base.py defaults it to 0.7,
        meaning "user did not set it") — unset keeps the fallback ladder, an explicit
        value pins a single temperature and disables the ladder.
        """
        kwargs: Dict[str, Any] = {
            "language": self.language if self.language != "auto" else None,
            "task": self.task,
        }

        if self.temperature != 0.7:
            kwargs["temperature"] = self.temperature
        elif self.temperature_fallback:
            kwargs["temperature"] = self.temperature_fallback

        for key in (
            "compression_ratio_threshold",
            "logprob_threshold",
            "no_speech_threshold",
            "condition_on_prev_tokens",
        ):
            value = getattr(self, key)
            if value is not None:
                kwargs[key] = value

        return kwargs

    def _detect_device(self) -> str:
        """
        Auto-detect the best available device.

        Priority: MLX > CUDA > MPS > CPU
        """
        import platform

        # 1. Check for Apple MLX (Apple Silicon - M1/M2/M3/M4)
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            try:
                import mlx.core as mx
                logger.info("MLX detected (Apple Silicon) - Using Apple GPU for local transcription")
                return "mlx"
            except ImportError:
                logger.debug("MLX not available (install with: uv sync --extra mlx --inexact)")

        # 2. Check for CUDA (NVIDIA GPUs)
        try:
            import torch
            if torch.cuda.is_available():
                device_name = torch.cuda.get_device_name(0)
                logger.info(f"CUDA detected: {device_name} - Using NVIDIA GPU for local transcription")
                return "cuda"
        except ImportError:
            logger.debug("PyTorch not available for CUDA detection")

        # 3. Check for MPS (macOS Metal Performance Shaders)
        if platform.system() == "Darwin":
            try:
                import torch
                if torch.backends.mps.is_available():
                    logger.info("MPS detected - Using macOS Metal GPU for local transcription")
                    return "mps"
            except (ImportError, AttributeError):
                logger.debug("MPS not available")

        # 4. Fallback to CPU
        logger.info("No GPU detected - Using CPU for local transcription (slower)")
        return "cpu"

    def _load_model(self):
        """
        Load Whisper model lazily on first use.

        Uses mlx-whisper for MLX devices (Apple Silicon) or
        transformers.pipeline for PyTorch devices (CUDA/MPS/CPU).
        """
        if self.model_loaded:
            return
        if self.is_cpu_twin:
            # L12's RAM check where the twin loads: the seam's is a snapshot (ADR-052 "Enough RAM").
            room, words = cpu_ram_check()
            if not room:
                raise RuntimeError(f"the CPU twin '{self.alias}' does not load: {words}")

        import time
        logger.info(f"Loading Whisper model '{self.model_name}' (this may take a minute on first use)...")
        start_time = time.time()

        try:
            # Determine device
            if self.device == "auto":
                device = self._detect_device()
                self._detected_device = device
            else:
                device = self.device
                self._detected_device = device

            # MLX path (Apple Silicon)
            if device == "mlx":
                try:
                    import mlx_whisper
                    logger.info("Loading Whisper model with MLX (Apple Silicon optimization)...")

                    self.pipeline = "mlx"
                    self.model_loaded = True

                    elapsed = time.time() - start_time
                    self._note_load("mlx", elapsed)
                    logger.info(f"MLX Whisper initialized in {elapsed:.1f} seconds (lazy model loading)")
                    return

                except ImportError as mlx_error:
                    logger.warning(f"MLX not available: {mlx_error} - Falling back to PyTorch")
                    device = "cpu"

            # PyTorch path (CUDA/MPS/CPU)
            try:
                import torch
                from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
            except ImportError as e:
                raise ImportError(f"{_VOICE_MEMORY_HINT} ({e})") from e
            from .model_sizes import hf_cache_path

            cache_dir = hf_cache_path()
            os.makedirs(cache_dir, exist_ok=True)

            torch_dtype = torch.float16 if device in ("cuda", "mps") else torch.float32

            model_kwargs = {}
            if device == "cuda":
                if self.use_flash_attention:
                    model_kwargs["attn_implementation"] = "flash_attention_2"
                else:
                    model_kwargs["attn_implementation"] = "sdpa"

            try:
                model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    self.model_name,
                    torch_dtype=torch_dtype,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    cache_dir=cache_dir,
                    local_files_only=True,
                    **model_kwargs
                )
            except OSError as e:
                if "local_files_only" in str(e) or "offline mode" in str(e).lower():
                    revision = self.model_name == "openai/whisper-large-v3-turbo" and WHISPER_MODEL_REVISION or None
                    size_bytes, size_source = model_size_bytes(self.model_name, revision)
                    logger.warning(
                        f"Whisper model '{self.model_name}' not found in cache. "
                        f"User will be prompted to download."
                    )
                    raise ModelNotCachedError(
                        model_name=self.model_name,
                        cache_path=cache_dir,
                        revision=revision,
                        size_bytes=size_bytes,
                        size_source=size_source,
                    ) from e
                else:
                    raise

            try:
                model.to(device)
            except (RuntimeError, AssertionError) as e:
                if "NVIDIA" in str(e) or "CUDA" in str(e) or "not compiled" in str(e).lower():
                    logger.warning(f"Failed to initialize {device}: {e}")
                    # A CPU route L12 never approved (ADR-052 3.1, Ark #30): it passes L12's RAM
                    # check, or the call is refused with the reason.
                    room, words = cpu_ram_check()
                    if not room:
                        raise RuntimeError(f"the load on {device} failed and the CPU cannot take the "
                                           f"model instead ({words})") from e
                    logger.info("Forcing CPU mode for Whisper model (%s)", words)
                    device = "cpu"
                    torch_dtype = torch.float32
                    model.to(device)
                else:
                    raise

            processor = AutoProcessor.from_pretrained(
                self.model_name,
                cache_dir=cache_dir,
                local_files_only=True
            )

            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                chunk_length_s=self.chunk_length_s,
                batch_size=self.batch_size,
                dtype=torch_dtype,
                device=device,
                generate_kwargs=self._build_generate_kwargs(),
                ignore_warning=True
            )

            if self.compile_model and device == "cuda":
                # torch.compile is an optional 4.5x speedup. Wrap it in its own try:
                # if the installed torch/dynamo is incompatible (e.g. torch 2.11 dropped
                # functorch.compile.min_cut_rematerialization_partition, breaking
                # torch._dynamo import chain), we must NOT take down model loading —
                # the model is already loaded above and runs fine uncompiled. Without
                # this guard, a compile failure aborts local transcription entirely and
                # the user has no fallback if the OpenAI key is also unset.
                try:
                    logger.info("Applying torch.compile optimization (4.5x speedup)...")
                    self.pipeline.model = torch.compile(self.pipeline.model)
                except Exception as compile_err:
                    logger.warning(
                        "torch.compile failed (%s) — continuing with uncompiled model. "
                        "Local transcription will work, just slower.", compile_err
                    )

            self.model_loaded = True
            elapsed = time.time() - start_time
            self._note_load(device, elapsed)
            logger.info(f"Whisper model loaded in {elapsed:.1f} seconds (device={device})")

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}", exc_info=True)
            raise RuntimeError(f"Failed to load local Whisper model: {e}") from e

    def is_model_loaded(self) -> bool:
        return self.model_loaded

    # -- the speed figures (ADR-052 S3a: the producer of `expected_s`) --

    def _note_load(self, device: str, load_s: float) -> None:
        """A load finished on `device` (where the model really is): its time is recorded as this
        model's first load on that device in this process or as a reload (`_LOADED_WEIGHTS`), and the
        next call is the first after a load."""
        self._loaded_device = device
        self._calls_since_load = 0
        key = "load_reload_s" if (device, self.model_name) in _LOADED_WEIGHTS else "load_first_s"
        _LOADED_WEIGHTS.add((device, self.model_name))
        seconds = _a_positive(load_s)
        if seconds is not None:
            self._measured.setdefault(device, {})[key] = seconds
            self._last_load = (device, key)

    def _note_run(self, device: Optional[str], audio_s: float, elapsed: float, *, first: bool) -> None:
        """One transcription of `audio_s` took `elapsed` on `device`. A later call replaces the warm
        ratio of its own length band only. The first call after a load records the `penalty_s` of
        its own length band: what it took beyond `audio / warm` of that band, at least 0 (Johnny
        #24-1: the first call costs seconds, not a ratio; Johnny #41-2: by band, so a long first
        call leaves a short one's penalty). A figure that cannot be computed is not recorded."""
        audio, wall, band = _a_positive(audio_s), _a_positive(elapsed), length_band(audio_s)
        if device is None or audio is None or wall is None or band is None:
            return
        if first:
            warm = self.speed_figures(device)["warm"].get(band)
            if warm is not None:
                penalty = self._measured.setdefault(device, {}).setdefault("penalty_s", {})
                penalty[band] = max(0.0, wall - audio / warm)
            return
        self._measured.setdefault(device, {}).setdefault("warm", {})[band] = audio / wall

    def _device_in_use(self) -> Optional[str]:
        """The device the loaded model runs on, for recording a run."""
        if self.pipeline == "mlx":
            return "mlx"
        if self._loaded_device is not None:
            return self._loaded_device
        if self._detected_device is not None:
            return self._detected_device
        return None if self.device == "auto" else self.device

    def _loads_on_the_cpu(self, device: Optional[str]) -> bool:
        """Whether a load about to run is known to run on the CPU, without detecting anything: the
        route's device when it is given, else the twin or an alias configured `cpu`. An `auto` alias with
        no route is not known (detecting it may import torch) and counts once the model is up."""
        if self.pipeline == "mlx":
            return False
        if device is not None:
            return device == "cpu"
        return self.is_cpu_twin or self.device == "cpu"

    def _route_device(self, remember: bool = True) -> str:
        """The device a call would run on now: the loaded model's, else the configured one; `auto`
        is detected once (it may import torch: call from a thread when that matters).

        Only a detection that returned is kept, and only with `remember` (Ark #44-2, S3d): the call's
        own paths (`_route_for_the_card`, `expected_seconds` from the door's `card_ask`) keep it; a
        question (`asks_for_the_card`) passes False and leaves the provider as it found it."""
        if self.model_loaded:
            in_use = self._device_in_use()
            if in_use is not None:
                return in_use
        if self.device != "auto":
            return self.device
        known = self._estimate_device or self._detected_device
        if known is not None:
            return known
        detected = self._detect_device()
        if remember:
            self._estimate_device = detected
        return detected

    def speed_figures(self, device: str) -> Dict[str, Any]:
        """`warm` and `penalty_s` (each by length band), `load_first_s` and `load_reload_s` for
        `device`: each figure measured here, else the start figure, else absent (a band) or None
        (unknown). `load_s` is the one the next load on `device` costs: the reload once this model
        has loaded on `device` in this process, else the first load."""
        measured = self._measured.get(device, {})
        start = _START_FIGURES.get(device, {})
        figures: Dict[str, Any] = {
            key: {**start.get(key, {}), **measured.get(key, {})} for key in ("warm", "penalty_s")}
        for key in ("load_first_s", "load_reload_s"):
            figures[key] = measured.get(key, start.get(key))
        loaded = (device, self.model_name) in _LOADED_WEIGHTS
        figures["load_s"] = figures["load_reload_s" if loaded else "load_first_s"]
        return figures

    @staticmethod
    def _load_left(figures: Dict[str, Any], elapsed: float) -> Optional[float]:
        """What is left of a load that has run `elapsed` seconds, from a device's `speed_figures`:
        `load_s` less `elapsed`. A reload past its figure falls back to the device's first-load
        figure (S3 follow-up 6): an overrun reload is most likely reading the weights from disk
        again, which the first load also did, imports aside, so that figure is the slower load this
        device is known for. Past every figure, None (L10: unknown, never a guess)."""
        ceilings = [figures["load_s"]]
        if figures["load_s"] == figures["load_reload_s"] and figures["load_first_s"] is not None:
            ceilings.append(figures["load_first_s"])
        for figure in ceilings:
            if figure is not None and figure - elapsed > 0:
                return figure - elapsed
        return None

    def _first_call_seconds(self, audio_s: Any, device: str) -> Optional[float]:
        """The first call after a load on `device`: `audio / warm[band] + penalty_s[band]`, or None."""
        from ..managers.card_lease import seconds_from_ratio

        figures = self.speed_figures(device)
        band = length_band(audio_s)
        work = None if band is None else seconds_from_ratio(audio_s, figures["warm"].get(band))
        penalty = None if band is None else figures["penalty_s"].get(band)
        return None if work is None or penalty is None else work + penalty

    def expected_seconds(self, audio_s: Any, device: Optional[str] = None,
                         loaded: Optional[bool] = None) -> Optional[float]:
        """Wall seconds a transcription of `audio_s` would take, or None when unknown (ADR-052 L10,
        L13 point 2): `load_s` when the model is not loaded (the first load of this process or a
        reload, `speed_figures`), plus the audio divided by the warm ratio of its length band
        (`card_lease.seconds_from_ratio`), plus that band's `penalty_s` for the first call after a
        load. `device` and `loaded` default to what this provider is now."""
        owner = self._current_owner()
        if owner is not self:
            return owner.expected_seconds(audio_s, device, loaded)
        from ..managers.card_lease import seconds_from_ratio

        if loaded is None:
            loaded = self.model_loaded
        figures = self.speed_figures(device or self._route_device())
        band = length_band(audio_s)
        work = None if band is None else seconds_from_ratio(audio_s, figures["warm"].get(band))
        if work is None:
            return None
        if not loaded or self._calls_since_load == 0:
            penalty = figures["penalty_s"].get(band)
            if penalty is None:
                return None
            work += penalty
        if loaded:
            return work
        load = figures["load_s"]
        return None if load is None else load + work

    # -- the card lease (ADR-052 S3b) --

    def _would_detect(self) -> bool:
        """Whether `_route_device` would detect the device now (and so may import torch)."""
        if self.model_loaded and self._device_in_use() is not None:
            return False
        return self.device == "auto" and self._estimate_device is None and self._detected_device is None

    async def _route_for_the_card(self) -> tuple:
        """`(device, lease)`: the device a call routes on (`_route_device`) and the lease it holds the
        card under, None when that device is the CPU (ADR-052 3.1: a CPU device builds no lease; a
        model that fell back to the CPU routes there). An explicit device is used as is (no torch
        import); a detection runs in a thread, never on the loop (Forge #48-4), and is kept. The
        device is the one L12's estimates read; it is carried into `_load_under_the_lease`, so the
        ETA during a load reads the same device (Ark #49-2)."""
        if self._would_detect():
            device = await asyncio.to_thread(self._route_device)
        else:
            device = self._route_device()
        return device, (None if device == "cpu" else self._the_card_lease())

    def _the_card_lease(self) -> Any:
        """This alias's lease, built once: holder `whisper:<alias>`, a cheap yield (an unload), and
        `waits_for_engines` (item 1a, decided 2026-11-23: with the swap on and nothing waiting, an
        idle local engine is swapped out by the owner-start rule, as for music)."""
        if self._card_lease is not None:
            return self._card_lease
        from ..managers.card_lease import CardLease, lease_named

        holder = f"whisper:{self.alias}"
        known = lease_named(holder)
        if known is not None and (known.granted or known._uses or known._waiters()):
            # A retired object of this alias left it standing (no hand-over): one lease per alias.
            known.rebind(self._yield_for_the_card)
            self._card_lease = known
            return known
        self._card_lease = CardLease(
            holder, kind="transcription", engine="whisper",
            display=f"Whisper ({self.model_name.rsplit('/', 1)[-1]})",
            vram_mib=whisper_vram_mib(self.model_name), yield_cost="cheap",
            on_yield=self._yield_for_the_card, waits_for_engines=True,
            shares_with=_shares_with_music, eta_s=self._card_eta, stop_hint=_STOP_HINT,
        )
        return self._card_lease

    async def _yield_for_the_card(self, why: str) -> str:
        """The lease's `on_yield`: the object that owns the model now lets go."""
        return await self._current_owner().yield_card(why)

    # -- the CPU twin and L12 (ADR-052 S3d) --

    def _the_cpu_twin(self) -> "LocalWhisperProvider":
        """This GPU alias's CPU twin, built once (ADR-052 3.1, "The CPU route"): a `local_whisper`
        provider `<alias>@cpu`, the same model and settings on `device: cpu`, its own load lock, idle
        timer (`TWIN_IDLE_UNLOAD_S`, or the GPU alias's `cpu_idle_unload_s`) and speed figures, no
        lease. Not in the providers manager's list, so nothing reads it as a configured alias."""
        if self._cpu_twin is None:
            config = {key: value for key, value in self.config.items()
                      if key not in ("idle_unload_s", "let_peers_use_cpu_twin", "cpu_idle_unload_s")}
            config.update(alias=f"{self.alias}{CPU_TWIN_SUFFIX}", device="cpu", compile_model=False,
                          _cpu_twin_of=self.alias,
                          idle_unload_s=self.config.get("cpu_idle_unload_s", TWIN_IDLE_UNLOAD_S))
            self._cpu_twin = LocalWhisperProvider(config["alias"], config)
        return self._cpu_twin

    def _hand_the_twin_to(self, new: Optional["LocalWhisperProvider"]) -> None:
        """A providers reload: the twin goes to `new`'s twin the way the providers manager carries a
        GPU alias (load lock shared, a loaded model carried unless its unload runs, `hand_over_to`).
        With no `new` (retired), or one that takes no twin (a CPU device) or another model, the old
        twin is retired and its model unloaded."""
        old = self._cpu_twin
        self._cpu_twin = None
        if old is None:
            return
        if new is not None and new.device != "cpu" and new.model_name == old.model_name:
            twin = new._the_cpu_twin()
            twin._load_lock = old._load_lock
            if old.model_loaded and not old._unloading:
                twin.pipeline = old.pipeline
                twin.model_loaded = True
                twin._detected_device = old._detected_device
            old.hand_over_to(twin)
            twin.schedule_idle_unload()
            return
        old.retire()
        if old.model_loaded:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("Whisper '%s': the CPU twin of a replaced alias stays loaded (no event "
                               "loop to unload it on)", old.alias)
                return
            loop.create_task(old.unload_model_async("alias replaced"))

    def _lets_peers_use_the_twin(self) -> bool:
        """The owner's optional `let_peers_use_cpu_twin` on the GPU alias (Mike #71, 6C: off by
        default; S5 puts it beside the device selector)."""
        value = self.config.get("let_peers_use_cpu_twin", False)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return value is True or (type(value) is int and value == 1)  # JSON's 1, as the string "1"

    def _note_route(self, decision: Dict[str, Any]) -> None:
        """L12's choice, observable: kept as `last_route` and logged at INFO (DEBUG on a free card)."""
        self.last_route = decision
        where = {"cpu": "CPU twin", "gpu": "GPU"}.get(decision["route"], "line for the card")
        logger.log(logging.DEBUG if decision.get("free") else logging.INFO,
                   "Whisper '%s': L12 sends %s to the %s: %s", self.alias, decision["what"], where,
                   decision["why"])

    async def _choose_route(self, lease: Any, what: str, expected_s: Optional[float],
                            audio_s: Optional[float], vram_mib: Optional[int] = None) -> Optional[RoutedToCpu]:
        """ADR-052 L12 at the seam before the lease is asked (item 1a): `RoutedToCpu` for the CPU
        twin, None for the card (the GPU at once, or the line). Raises `LeaseWouldWaitOnItself` for an
        agent's tool inside its own turn on a local alias the twin cannot take (section 3.5). Nothing
        here waits, takes a place or asks a holder. The checks run in L12's order."""
        from ..managers.card_lease import PEER_TRIP_S, LeaseWouldWaitOnItself, route_view
        from ..managers.llama_server_supervisor import peer_being_served

        view = await route_view(lease, expected_s=expected_s, vram_mib=vram_mib)
        length = _a_positive(audio_s)
        fit = view.get("fit")
        decision: Dict[str, Any] = {"what": what, "free": view["free"], "audio_s": length,
                                    "behind": view["behind"], "vram_mib": vram_mib,
                                    "l11": None if fit is None else fit["outcome"]}

        def to(route: str, why: str) -> None:
            if fit is not None and fit["outcome"] != "fits":
                why = f"{why} (L11 {fit['outcome']}: {fit['why']})"
            decision.update(route=route, why=why)
            self._note_route(decision)

        if view["free"]:  # L12 point 1
            to("gpu", "the card is free")
            return None
        if fit is not None and fit["outcome"] == "fits":  # L12 point 1, L11's one shared cell (S3e)
            to("gpu", f"L11 fits it beside {', '.join(view['behind'])}: {fit['why']}")
            return None
        settings = cpu_route_settings()
        cap = settings["cpu_max_audio_s"]
        # 0 turns the CPU route off (Mike, 2026-11-24, Q3): every call L12 is asked about goes to the
        # line, and inside an agent's own turn it is refused at once (3.5).
        off = next((key for key in _CPU_ROUTE_SWITCHES if settings[key] == 0), None)
        if view["self_wait"] is not None:  # section 3.5: the cap and the RAM check, no comparison
            alias, engine = view["self_wait"]

            def refused(reason: str) -> Exception:
                refusal = LeaseWouldWaitOnItself(
                    f"Not transcribed: the CPU could not take it ({reason}), and this turn holds the "
                    f"card (the local model '{alias}'), so nothing was queued.", alias, engine)
                logger.info("card: %s refused: %s", what, refusal)
                return refusal

            if off is not None:
                to("line", f"CPU route off: {off} is 0; inside its own turn on '{alias}': refused")
                refusal = LeaseWouldWaitOnItself(
                    f"Not transcribed: this turn runs on the local model '{alias}', which holds the "
                    f"card, and the CPU route is off ([local_transcription] {off} is 0), so nothing "
                    "was queued.", alias, engine)
                logger.info("card: %s refused: %s", what, refusal)
                raise refusal
            if length is None or length > cap:
                too_long = (f"A file over {cap:g} s needs the card" if length is not None else
                            f"The length of this file cannot be read, and a file over {cap:g} s needs the card")
                to("line", f"inside its own turn on '{alias}', {length or 'unknown'} s of audio: refused")
                refusal = LeaseWouldWaitOnItself(
                    f"Not transcribed: this turn runs on the local model '{alias}', which holds the "
                    f"card. {too_long}, so nothing was queued.", alias, engine)
                logger.info("card: %s refused: %s", what, refusal)
                raise refusal
            twin = self._the_cpu_twin()
            if not twin.model_loaded:
                room, words = cpu_ram_check()
                if not room:
                    to("line", f"inside its own turn on '{alias}', the twin is cold and {words}: refused")
                    raise refused(words)
            to("cpu", f"inside its own turn on '{alias}', {length:g} s of audio within {cap:g} s")
            return RoutedToCpu(twin, decision, if_it_fails=refused)
        if off is not None:
            to("line", f"CPU route off: {off} is 0")
            return None
        peer = peer_being_served() is not None
        if peer and not self._lets_peers_use_the_twin():
            to("line", "a peer's call takes no CPU route (Mike #71)")
            return None
        if length is None:  # L12 point 2
            to("line", "the length is unknown, so the CPU's time is too")
            return None
        if length > cap:
            to("line", f"{length:g} s of audio is over cpu_max_audio_s {cap:g} s")
            return None
        # Point 3 (Mike, 2026-11-24, Q2): whatever small work computes on the CPU now, each named:
        # a resident child (Strata or llama-server, a claim on the card) that is busy by its own test,
        # the embedding model and GLiNER while they compute on the CPU (`route_view`'s
        # `cpu_contention`, from `card_lease._cpu_contention`), and a local Whisper transcription
        # already running on the CPU (added here). A llama-server kept off the card (`--device none`)
        # is not counted: `_cpu_contention` has no branch for an engine off the card.
        contention = list(view["cpu_contention"])
        contention += [f"a CPU transcription on '{alias}'" for alias in cpu_transcriptions_running()]
        decision["contention"] = contention
        factor = settings["cpu_contention_factor"] if contention else 1.0
        twin = self._the_cpu_twin()
        cpu_s = twin.expected_seconds(length)
        if cpu_s is None:
            to("line", "the twin's time is unknown")
            return None
        cpu_s *= factor
        # Point 4: the GPU's finish is the wait plus its own work; any unknown part is long.
        gpu_work = _a_positive(expected_s) or self.expected_seconds(length)
        wait_s = view["wait_s"]
        gpu_s = None if wait_s is None or gpu_work is None else wait_s + gpu_work
        cold = not twin.model_loaded
        decision.update(cpu_s=cpu_s, gpu_s=gpu_s, factor=factor, cold=cold, gpu_compared=gpu_s is not None)
        # Behind an engine that stays (item 1a) nothing of the GPU is estimated: the line must not
        # read as a comparison, nor a factor of 1 as a penalty.
        if view["engine_stays"]:
            gpu_words = "not compared: its wait is not estimated behind a local engine on the card (item 1a)"
        else:
            gpu_words = "long (unknown)" if gpu_s is None else f"≈{gpu_s:.0f} s"
        if not contention:
            busy = ""
        elif factor == 1.0:
            busy = f", {' and '.join(contention)}, cpu_contention_factor 1, no penalty"
        else:
            busy = f", x{factor:g} for {' and '.join(contention)}"
        compared = f"CPU ≈{cpu_s:.0f} s ({'cold' if cold else 'warm'}{busy}) against the GPU {gpu_words}"
        if gpu_s is not None and cpu_s >= gpu_s:
            to("line", f"{compared}: the GPU is sooner")
            return None
        if cpu_s > settings["cpu_max_s"]:
            to("line", f"{compared}: over cpu_max_s {settings['cpu_max_s']:g} s")
            return None
        if peer and cpu_s > PEER_TRIP_S:
            to("line", f"{compared}: over the guest's {PEER_TRIP_S:g} s (L13)")
            return None
        if cold:
            room, words = cpu_ram_check()
            if not room:
                to("line", f"{compared}, but the twin is cold and {words}")
                return None
        to("cpu", compared)
        return RoutedToCpu(twin, decision)

    def _card_eta(self) -> Optional[float]:
        """The lease's `eta_s`: the longest remaining expected time of the calls holding the card now,
        None when any is unknown or nothing runs or loads (L10). An overrun reads as unknown
        (`_positive`).

        While a load runs under the lease (S3 follow-up 4, Ark #40-1, Johnny #41-5) it is the load's
        remaining time, from its start and the load figure of the device the call routed on (the
        one L12 read, follow-up 6), plus the longest first-call work of the calls waiting for it
        (none for a preload). A call whose length is not known counts its own `expected_s -
        elapsed` instead, and one with neither leaves it unknown; that call and the load are
        compared (max), never added, since its `expected_s` already holds the load when the model
        was not loaded at its ask (Forge #48-5, read with the code: no double count). A reload past
        its figure falls back to the first-load figure, and a load past that is unknown
        (`_load_left`)."""
        owner = self._current_owner()
        runs = list(owner._card_runs.values())
        load = dict(owner._card_load)
        now = time.monotonic()
        if load:
            load_left = owner._load_left(owner.speed_figures(load["device"]), now - load["since"])
            if load_left is None:
                return None
            left = [load_left]
            for started, expected, audio_s in runs:
                work = None if audio_s is None else owner._first_call_seconds(audio_s, load["device"])
                if work is not None:
                    left.append(load_left + work)
                elif expected is not None:
                    left.append(expected - (now - started))
                else:
                    return None
            return max(left)
        if not runs:
            return None
        left = []
        for started, expected, _audio_s in runs:
            if expected is None:
                return None
            left.append(expected - (now - started))
        return max(left)

    def _holds_the_card(self) -> bool:
        """Whether a model sits on a GPU device now (a model that fell back to the CPU does not)."""
        owner = self._current_owner()
        return owner.model_loaded and owner._device_in_use() != "cpu"

    def _let_go_of_an_empty_grant(self, why: str) -> None:
        """A grant with no use open and no model on the card is released (ADR-052 3.1, "warm means
        granted with 0 uses"): a failed load, a call that ended before loading, an unload that ran
        while no use was open. Decided on the owner loop after the uses closed before it."""
        lease = self._current_owner()._card_lease or self._card_lease
        if lease is not None:
            lease.release_when_empty(why, self._holds_the_card)

    def _off_the_card_after_a_load(self, lease: Optional[Any]) -> None:
        """A load that fell back to the CPU releases the lease (ADR-052 3.1, Ark #13 new #3): the
        model lives in host RAM and the call runs without a card hold. L12's RAM check for this route
        is S3d's."""
        if lease is not None and self.model_loaded and self._loaded_device == "cpu":
            logger.warning("Whisper '%s': the load fell back to the CPU; the card is let go", self.alias)
            lease.release("the load fell back to the CPU")

    async def _load_under_the_lease(self, lease: Optional[Any], device: Optional[str] = None):
        """`_load_model` in its thread, the lease marked `starting` meanwhile. The caller holds
        `_load_lock`. Returns `_finish_thread`'s triple.

        `device` is the route's (`_route_for_the_card`), the one L12 estimated on: the load's ETA
        reads its figures, and nothing here detects a device on the loop (Forge #48-4, Ark #49-2;
        with no `device` given it is read in a thread). A model that loaded on another device is
        logged at WARNING: the ETA during the load read the other device's figures."""
        if lease is not None:
            if device is None:
                device = await asyncio.to_thread(self._route_device)
            self._card_load.update(since=time.monotonic(), device=device)
            lease.mark_starting()
        try:
            outcome = await self._finish_thread(self._load_model)
        finally:
            if lease is not None:
                self._card_load.clear()
                lease.mark_running()
        if (lease is not None and outcome[1] is None and self.model_loaded
                and self._loaded_device is not None and self._loaded_device != device):
            logger.warning("Whisper '%s': the load was estimated on %s and ran on %s; the ETA during "
                           "the load read %s's figures", self.alias, device, self._loaded_device, device)
        self._off_the_card_after_a_load(lease)
        return outcome

    async def yield_card(self, why: str) -> str:
        """Let go of the card for a contender (ADR-052 L6, 3.1), under `_load_lock`: nothing loaded
        is `nothing_to_give`; a transcription running is `refused`; else the model is unloaded, and
        the answer is `let_go` when it is gone, `could_not_let_go` when it is still loaded (the
        unload's thread never ran). Not gated by `swap_when_idle` (item 2)."""
        owner = self._current_owner()
        if owner is not self:
            return await owner.yield_card(why)
        async with self._load_lock:
            if not self.model_loaded:
                return "nothing_to_give"
            if self._active_transcriptions > 0:
                return "refused"
            self._cancel_idle_task()
            try:
                await self._unload_locked("card_yield", why)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Whisper '%s': the unload for the card did not run (%s); the model "
                               "and the card are kept", self.alias, exc)
            return "could_not_let_go" if self.model_loaded else "let_go"

    async def ensure_model_loaded(self) -> None:
        """The preload. On a GPU device it asks for the card as a background call (ADR-052 3.1,
        rule 10): it never waits or swaps anything out; when the card is not free it raises
        `LeaseSkipped` and the model stays lazy."""
        owner = self._current_owner()
        if owner is not self:
            return await owner.ensure_model_loaded()
        self._refuse_if_retired()
        if self.model_loaded:
            return
        device, lease = await self._route_for_the_card()
        if lease is None:
            return await self._preload(None)
        from ..managers.card_lease import LeaseSkipped
        try:
            # A use, not a bare grant: busy while it loads, so no contender asks it to let go before
            # the load starts (a grant with no use is idle, L6).
            async with lease.use("preload", wait_s=0, background=True):
                await self._preload(lease, device)
        except LeaseSkipped as skipped:
            logger.info("Whisper preload skipped: %s", skipped)
            raise
        finally:
            self._let_go_of_an_empty_grant("the preload loaded nothing")

    async def _preload(self, lease: Optional[Any], device: Optional[str] = None) -> None:
        async with self._load_lock:
            if self.model_loaded:
                return
            self._refuse_if_retired()
            _, error, cancelled = await self._load_under_the_lease(lease, device)
            if error is not None:
                raise error
            self._settle_finished_load()
            if cancelled:
                raise asyncio.CancelledError

    def _unload_model(self) -> Optional[bool]:
        """Free the model. False when the release raised (the reference is dropped anyway)."""
        if not self.model_loaded:
            logger.info("Whisper model already unloaded")
            return

        logger.info(f"Unloading Whisper model '{self.model_name}' from memory...")

        try:
            if self.pipeline == "mlx":
                if hasattr(self, 'whisper_model'):
                    del self.whisper_model
                self._release_mlx_model()
                logger.info("MLX Whisper model references cleared")

            elif self.pipeline is not None:
                import torch

                device = None
                if hasattr(self.pipeline, 'model') and hasattr(self.pipeline.model, 'device'):
                    device = str(self.pipeline.model.device)

                if device and 'cuda' in device:
                    logger.info(f"Moving Whisper model from {device} to CPU...")
                    self.pipeline.model.to('cpu')
                elif device and 'mps' in device:
                    logger.info(f"Moving Whisper model from {device} to CPU...")
                    self.pipeline.model.to('cpu')

                del self.pipeline

                gc.collect()

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.info("CUDA cache cleared, ~3GB VRAM freed")
                elif torch.backends.mps.is_available():
                    logger.info("MPS memory freed")
                else:
                    logger.info("CPU memory freed")

            self.pipeline = None
            self.model_loaded = False

            logger.info(f"Whisper model '{self.model_name}' unloaded successfully")
            return True

        except Exception as e:
            logger.error(f"Error unloading Whisper model: {e}", exc_info=True)
            self.pipeline = None
            self.model_loaded = False
            return False

    @staticmethod
    def _release_mlx_model() -> None:
        """Drop mlx_whisper's own cached model (it keeps the last one loaded in
        `ModelHolder`), then MLX's buffer cache. Best effort: either may be absent in
        a given mlx / mlx_whisper version, and neither exists off Apple Silicon."""
        try:
            from mlx_whisper.transcribe import ModelHolder
        except Exception:
            ModelHolder = None
        if ModelHolder is not None:
            if hasattr(ModelHolder, "model"):
                ModelHolder.model = None
            if hasattr(ModelHolder, "model_path"):
                ModelHolder.model_path = None
        gc.collect()
        try:
            import mlx.core as mx
        except Exception:
            return
        clear = getattr(mx, "clear_cache", None) or getattr(getattr(mx, "metal", None), "clear_cache", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                logger.debug("MLX clear_cache failed", exc_info=True)

    def _notify_unloaded(self, reason: str) -> None:
        listener = _unloaded_listener
        if listener is None:
            return
        try:
            listener(self.alias, self.model_name, reason)
        except Exception:
            logger.debug("whisper unload listener failed", exc_info=True)

    async def _unload_locked(self, reason: str, detail: str = "") -> bool:
        """Unload now. The caller holds `_load_lock`. Logs success or failure distinctly
        and tells the listener; True when the model is no longer loaded."""
        was_loaded = self.model_loaded
        self._unloading = True
        try:
            released, error, cancelled = await self._finish_thread(self._unload_model)
        finally:
            self._unloading = False
            # ADR-052 3.1: the card is let go on every unload that ran, read from state, not from the
            # path: a model still loaded (the thread never started) keeps it. A use still open keeps
            # it too (it is about to load); a yield releases by its own outcome.
            self._let_go_of_an_empty_grant(f"unloaded: {reason}")
        if error is not None:
            raise error
        if not was_loaded:
            if cancelled:
                raise asyncio.CancelledError
            return True  # nothing was loaded here: not a release, nothing to announce
        if released is False:
            logger.error("Whisper '%s' (%s): the unload failed (%s); the model reference was dropped "
                         "but its memory may not be free", self.alias, self.model_name, reason)
        else:
            logger.info("Whisper '%s' (%s) unloaded: %s", self.alias, self.model_name, detail or reason)
        self._notify_unloaded(reason)
        if cancelled:
            raise asyncio.CancelledError
        return not self.model_loaded

    @staticmethod
    async def _finish_thread(fn):
        """Run `fn` in a worker thread and return only once the thread has really ended,
        even when the caller or the worker task is cancelled (a thread cannot be stopped,
        and the lock must not be released under it). Returns (result, error, cancelled);
        `cancelled` is True only for a cancel of the caller, which re-raises it once its
        bookkeeping is done."""
        guard = threading.Lock()
        state = {"started": False, "abandoned": False, "result": None, "error": None}
        ended = threading.Event()

        def run():
            with guard:
                if state["abandoned"]:
                    ended.set()
                    return
                state["started"] = True
            try:
                state["result"] = fn()
            except BaseException as exc:
                state["error"] = exc
            finally:
                ended.set()

        worker = asyncio.ensure_future(asyncio.to_thread(run))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if not worker.cancelled():
                    cancelled = True
        if worker.cancelled():
            with guard:
                if not state["started"]:
                    state["abandoned"] = True  # never started, and `run` will now do nothing
            if state["abandoned"]:
                if cancelled:
                    raise asyncio.CancelledError
                return None, RuntimeError("the worker was cancelled before its thread started"), False
        while not ended.is_set():
            try:
                await asyncio.to_thread(ended.wait, 0.05)
            except asyncio.CancelledError:
                cancelled = True
        return state["result"], state["error"], cancelled

    async def unload_if_idle(self, reason: str = "released") -> bool:
        """Unload the model unless a transcription is using it now. True when it was
        unloaded by this call. Holds `_load_lock`, so a transcription that starts
        meanwhile waits and then loads the model again."""
        owner = self._current_owner()
        if owner is not self:
            return await owner.unload_if_idle(reason)
        async with self._load_lock:
            if not self.model_loaded:
                return False
            if self._active_transcriptions > 0:
                logger.info("Whisper model '%s' kept loaded: %d transcription(s) still running",
                            self.model_name, self._active_transcriptions)
                return False
            self._cancel_idle_task()
            return await self._unload_locked(reason)

    async def unload_model_async(self, reason: str = "requested") -> None:
        """Unload the model, never under a running transcription: when one runs, the
        unload is deferred and happens as soon as the last one ends."""
        owner = self._current_owner()
        if owner is not self:
            return await owner.unload_model_async(reason)
        async with self._load_lock:
            if not self.model_loaded:
                logger.debug("Whisper model not loaded, skipping unload")
                self._unload_when_idle = False
                return
            if self._active_transcriptions > 0:
                self._unload_when_idle = True
                self._unload_reason = reason
                logger.info("Whisper model '%s' unload deferred: %d transcription(s) running",
                            self.model_name, self._active_transcriptions)
                return
            self._unload_when_idle = False
            self._cancel_idle_task()
            await self._unload_locked(reason)

    async def shutdown_unload(self, timeout: float = SHUTDOWN_DRAIN_S) -> None:
        """Shutdown path, `timeout` s in total: running transcriptions get 80% of it to
        finish, then the model is unloaded (logged) so the card is free before the process
        exits. A transcription still running at that point is unloaded under, as logged."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            await asyncio.wait_for(self._drained.wait(), timeout * 0.8)
        except asyncio.TimeoutError:
            logger.warning("Whisper '%s': %d transcription(s) still running after %g s at shutdown; "
                           "unloading anyway", self.alias, self._active_transcriptions, timeout * 0.8)
        self.retire()

        async def unload_under_lock():
            async with self._load_lock:
                if self.model_loaded:
                    await self._unload_locked("shutdown")

        job = asyncio.ensure_future(unload_under_lock())

        def report_late_outcome(done):
            if done.cancelled():
                return
            if done.exception() is not None:
                logger.error("Whisper '%s': the shutdown unload failed: %s", self.alias, done.exception())
            elif not self.model_loaded and timed_out:
                logger.warning("Whisper '%s': the model finished unloading after the shutdown bound",
                               self.alias)

        timed_out = False
        job.add_done_callback(report_late_outcome)
        try:
            await asyncio.wait_for(asyncio.shield(job), max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            timed_out = True
            if not self._unloading:
                job.cancel()
            logger.error("Whisper '%s': not unloaded within the %g s shutdown bound; "
                         "the model is left loaded at exit", self.alias, timeout)
            return
        if self.model_loaded:
            logger.error("Whisper '%s': the model is still loaded at exit", self.alias)
        self._let_go_of_an_empty_grant("shutdown")

    # -- idle self-unload ------------------------------------------------------------------

    def _idle_unload_s(self) -> float:
        """Seconds of idleness before the unload; 0 = never. The provider's own
        `idle_unload_s` wins, then the registered settings source; with neither (a
        provider built outside the service) the timer is off."""
        raw = self.config.get("idle_unload_s")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                logger.warning("Whisper '%s': idle_unload_s %r is not a number; using the setting",
                               self.alias, raw)
        if _idle_unload_source is None:
            return 0.0
        try:
            return max(0.0, float(_idle_unload_source()))
        except Exception:
            logger.warning("Whisper '%s': [local_transcription] idle_unload_s is unreadable; using %g s",
                           self.alias, DEFAULT_IDLE_UNLOAD_S)
            return DEFAULT_IDLE_UNLOAD_S

    def _cancel_idle_task(self) -> None:
        """Drop the pending timer. A task already unloading is left to finish: cancelling
        it would free the load lock while the unload still runs in its thread."""
        self._idle_epoch += 1
        task, self._idle_task = self._idle_task, None
        if task is not None and not self._unloading and not task.done():
            task.cancel()

    def schedule_idle_unload(self) -> None:
        """(Re)start the idle timer for the loaded model, or the deferred unload at once.
        One task per provider; a new call replaces the pending one. No-op when the
        provider is retired, nothing is loaded or no event loop runs."""
        if self._retired:
            return
        self._cancel_idle_task()
        if not self.model_loaded or self._active_transcriptions > 0:
            return
        delay = 0.0 if self._unload_when_idle else self._idle_unload_s()
        if not self._unload_when_idle and delay <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._idle_unload_after(delay, self._idle_epoch))
        task.add_done_callback(self._forget_idle_task)
        self._idle_task = task

    async def _idle_unload_after(self, delay: float, epoch: int) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._load_lock:
                if (epoch != self._idle_epoch or self._retired or not self.model_loaded
                        or self._active_transcriptions > 0):
                    return
                deferred, self._unload_when_idle = self._unload_when_idle, False
                if deferred:
                    await self._unload_locked(self._unload_reason,
                                              f"{self._unload_reason}, deferred until its transcription ended")
                else:
                    await self._unload_locked("idle", f"idle for {delay:g} s")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Whisper '%s': idle unload failed", self.alias)

    def _forget_idle_task(self, task: "asyncio.Task") -> None:
        if self._idle_task is task:
            self._idle_task = None

    # -- providers reload ------------------------------------------------------------------

    def _current_owner(self) -> "LocalWhisperProvider":
        owner = self
        while owner._successor is not None:
            owner = owner._successor
        return owner

    def _refuse_if_retired(self) -> None:
        """A retired object with no successor (its alias was removed, or the app is
        shutting down) must never load or transcribe: nothing would track the model."""
        if self._retired and self._successor is None:
            raise RuntimeError(f"Whisper provider '{self.alias}' was removed or shut down")

    def _carry_figures_to(self, new: "LocalWhisperProvider", *, fresh_load: bool) -> None:
        """The speed measured here goes to `new` when it is the same model's (ADR-052 S3a): `new`
        keeps a figure it measured itself, band by band for `warm` and `penalty_s`, except, with
        `fresh_load`, the load this object has just measured (first or reload, on its device), which
        is newer than anything `new` holds."""
        if new.model_name != self.model_name:
            return
        for device, figures in self._measured.items():
            kept = new._measured.setdefault(device, {})
            for key, value in figures.items():
                if key in ("warm", "penalty_s"):
                    for band, figure in value.items():
                        kept.setdefault(key, {}).setdefault(band, figure)
                else:
                    kept.setdefault(key, value)
        if fresh_load and self._last_load is not None:
            device, key = self._last_load
            seconds = self._measured.get(device, {}).get(key)
            if seconds is not None:
                new._measured.setdefault(device, {})[key] = seconds

    def _copy_state_to(self, new: "LocalWhisperProvider") -> None:
        if not new.model_loaded and self.model_loaded:
            # Zcode #22-2: a load that finished here after the hand-over measured its time here.
            self._carry_figures_to(new, fresh_load=True)
            new.pipeline = self.pipeline
            new.model_loaded = True
            new._detected_device = self._detected_device
            new._loaded_device = self._loaded_device
            new._calls_since_load = self._calls_since_load
            self.model_loaded = False  # one owner of the shared pipeline: `new`

    def _settle_finished_load(self) -> None:
        """A load just finished under the lock. When a reload replaced this object meanwhile,
        the successor takes the model; either way the idle timer is armed for it."""
        owner = self._current_owner()
        if owner is not self:
            self._copy_state_to(owner)
        owner.schedule_idle_unload()

    def hand_over_to(self, new: "LocalWhisperProvider") -> None:
        """Providers reload: `new` replaces this object. Transcriptions still running here
        count on `new` from now on, so its idle timer or a deferred unload waits for
        them; the model state is carried by the reload, and calls on this object are
        forwarded to `new`."""
        if new is self:
            return
        new._active_transcriptions += self._active_transcriptions
        self._active_transcriptions = 0
        if new._active_transcriptions > 0:
            new._drained.clear()
            new._cancel_idle_task()
        if self._unload_when_idle:
            new._unload_when_idle = True
            new._unload_reason = self._unload_reason
        # ADR-052 3.1: one lease follows the model and is never released by a hand-over.
        lease = self._card_lease
        if lease is not None:
            new._card_lease = lease
            lease.rebind(new._yield_for_the_card)
            from ..managers import card_lease as _card_lease
            _card_lease._CARD_LEASES[lease.holder] = lease
        self._card_runs.update(new._card_runs)
        new._card_runs = self._card_runs
        self._card_load.update(new._card_load)
        new._card_load = self._card_load
        self._carry_figures_to(new, fresh_load=False)
        if new.model_name == self.model_name:
            if new.model_loaded and self.model_loaded and new._loaded_device is None:
                new._loaded_device = self._loaded_device
                new._calls_since_load = self._calls_since_load
        if new.model_loaded:
            self.model_loaded = False  # one owner of the shared pipeline: `new`
        self._hand_the_twin_to(new)
        self._successor = new
        self.retire()

    def retire(self) -> None:
        """This object is replaced (providers reload hands its model to the new one) or
        shut down: stop its timer and never schedule another. A CPU twin still here (no hand-over
        took it) is retired and unloaded."""
        self._retired = True
        self._cancel_idle_task()
        self._hand_the_twin_to(None)

    async def shutdown(self) -> None:
        self.retire()

    async def download_model_async(self, progress_callback=None) -> Dict[str, Any]:
        logger.info(f"Starting download of Whisper model '{self.model_name}'...")

        if progress_callback:
            await progress_callback("Preparing download", 0.0)

        def _download():
            try:
                import torch
                from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
            except ImportError as e:
                logger.error("%s (%s)", _VOICE_MEMORY_HINT, e)
                return {
                    "success": False,
                    "message": f"Download failed: {_VOICE_MEMORY_HINT}",
                    "model_name": self.model_name,
                    "cache_path": None,
                }
            from .model_sizes import hf_cache_path

            cache_dir = hf_cache_path()
            os.makedirs(cache_dir, exist_ok=True)

            try:
                if self.device == "auto":
                    device = self._detect_device()
                else:
                    device = self.device

                torch_dtype = torch.float16 if device in ("cuda", "mps") else torch.float32

                logger.info(f"Downloading model from HuggingFace: {self.model_name}")

                model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    self.model_name,
                    torch_dtype=torch_dtype,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    cache_dir=cache_dir
                )

                logger.info(f"Downloading processor config from HuggingFace: {self.model_name}")

                processor = AutoProcessor.from_pretrained(
                    self.model_name,
                    cache_dir=cache_dir
                )

                logger.info(f"Model and processor downloaded successfully to {cache_dir}")

                del model
                del processor
                gc.collect()

                return {
                    "success": True,
                    "message": f"Model '{self.model_name}' downloaded successfully",
                    "model_name": self.model_name,
                    "cache_path": cache_dir
                }

            except Exception as e:
                logger.error(f"Failed to download Whisper model: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"Download failed: {str(e)}",
                    "model_name": self.model_name,
                    "cache_path": cache_dir
                }

        if progress_callback:
            await progress_callback("Downloading model files", 0.3)

        result = await asyncio.to_thread(_download)

        if progress_callback:
            await progress_callback("Download complete" if result["success"] else "Download failed", 1.0)

        return result

    @staticmethod
    def _realtime_ratio(duration_seconds: float, elapsed: float) -> str:
        """Audio seconds per wall second, for the log line; "n/a" when the clock did not advance."""
        if elapsed <= 0:
            return "n/a"
        return f"{duration_seconds / elapsed:.1f}x"

    @staticmethod
    def _segments_from_chunks(chunks: Any, duration_seconds: float,
                              bounded_last: bool = False) -> List[Dict[str, Any]]:
        """Normalise HF pipeline chunks into {start, end, text} segments.

        The pipeline hands back ``{"timestamp": (start, end), "text": ...}`` per
        chunk and leaves ``end`` as None on the last one; that chunk runs to the
        end of the audio, so it gets the measured duration. With `bounded_last` (the music
        path's `with_detail` fallback only) it gets a length estimated from its words instead,
        see `_open_segment_end`: dictation keeps the end of the audio.
        """
        segments: List[Dict[str, Any]] = []
        if not chunks:
            return segments
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            timestamp = chunk.get("timestamp") or (None, None)
            try:
                start, end = timestamp[0], timestamp[1]
            except (TypeError, IndexError):
                start, end = None, None
            if start is None:
                start = segments[-1]["end"] if segments else 0.0
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            if end is None:
                end = (LocalWhisperProvider._open_segment_end(float(start), text, duration_seconds)
                       if bounded_last else duration_seconds)
            segments.append({"start": float(start), "end": float(end), "text": text})
        return segments

    @staticmethod
    def _open_segment_end(start: float, text: str, duration_seconds: float) -> float:
        """An end for a last segment the pipeline left open: `OPEN_SEGMENT_PER_WORD_S` per word held
        to `OPEN_SEGMENT_MIN_S`..`OPEN_SEGMENT_MAX_S`, never past the end of the audio and never
        before the start. An estimate, not an alignment."""
        length = min(max(OPEN_SEGMENT_PER_WORD_S * len(text.split()), OPEN_SEGMENT_MIN_S), OPEN_SEGMENT_MAX_S)
        return max(start, min(start + length, duration_seconds))

    @staticmethod
    def _words_from_chunks(chunks: Any, duration_seconds: float) -> List[Dict[str, Any]]:
        """HF pipeline word chunks (`return_timestamps="word"`) as `{word, start, end}`. A missing end
        (the last word) is NOT the end of the audio: a song with an outro would hold its last word,
        and so its line, for the whole outro. It is the start plus the median length of the words
        that did get an end, held to `OPEN_WORD_MIN_S`..`OPEN_WORD_MAX_S` (`OPEN_WORD_DEFAULT_S`
        when none did), and never past the end of the audio. An estimate, not an alignment."""
        parsed: List[tuple] = []
        for chunk in chunks or []:
            if not isinstance(chunk, dict):
                continue
            text = (chunk.get("text") or "").strip()
            timestamp = chunk.get("timestamp") or (None, None)
            try:
                start, end = timestamp[0], timestamp[1]
            except (TypeError, IndexError):
                start, end = None, None
            if not text or start is None:
                continue
            parsed.append((text, float(start), None if end is None else float(end)))
        known = sorted(end - start for _, start, end in parsed if end is not None and end > start)
        if known:
            middle = len(known) // 2
            median = known[middle] if len(known) % 2 else (known[middle - 1] + known[middle]) / 2
            open_s = min(max(median, OPEN_WORD_MIN_S), OPEN_WORD_MAX_S)
        else:
            open_s = OPEN_WORD_DEFAULT_S
        words: List[Dict[str, Any]] = []
        for text, start, end in parsed:
            if end is None:
                end = max(start, min(start + open_s, duration_seconds))
            words.append({"word": text, "start": start, "end": end})
        return words

    @staticmethod
    def _segments_from_mlx(raw_segments: Any, duration_seconds: float,
                           detail: bool = False) -> List[Dict[str, Any]]:
        """Normalise mlx_whisper segments into the same {start, end, text} shape. With `detail`
        each segment also keeps `no_speech_prob`, `avg_logprob`, `compression_ratio` and `words`."""
        segments: List[Dict[str, Any]] = []
        if not raw_segments:
            return segments
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = segment.get("start")
            end = segment.get("end")
            if start is None:
                start = segments[-1]["end"] if segments else 0.0
            if end is None:
                end = duration_seconds
            item: Dict[str, Any] = {"start": float(start), "end": float(end), "text": text}
            if detail:
                for key in ("no_speech_prob", "avg_logprob", "compression_ratio"):
                    figure = segment.get(key)
                    if isinstance(figure, (int, float)) and not isinstance(figure, bool):
                        item[key] = float(figure)
                words = []
                for w in segment.get("words") or []:
                    if isinstance(w, dict) and isinstance(w.get("start"), (int, float)) \
                            and isinstance(w.get("end"), (int, float)) and str(w.get("word") or "").strip():
                        words.append({"word": str(w["word"]).strip(), "start": float(w["start"]),
                                      "end": float(w["end"])})
                if words:
                    item["words"] = words
            segments.append(item)
        return segments

    async def transcribe(self, audio_path: str, release_if_loaded_here: bool = False,
                         language: Optional[str] = None, task: Optional[str] = None, *,
                         purpose: Optional[str] = None, wait_s: Optional[float] = None,
                         expected_s: Optional[float] = None,
                         on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
                         check: Optional[Callable[[], Awaitable[None]]] = None,
                         audio_s: Optional[float] = None, cpu_route: bool = True,
                         with_detail: bool = False) -> Dict[str, Any]:
        """Transcribe `audio_path`. With `release_if_loaded_here`, a model this very call
        had to load is unloaded as the call ends, unless another transcription is using
        it (the decision is made under the load lock, so it cannot race).

        `language` ("auto" = detect) and `task` apply to this call only; None keeps the
        provider's configured value. Nothing here mutates the shared provider. So do
        `purpose`, `wait_s` and `expected_s`, the door's ask for the card (`card_ask`).

        On a GPU device the call holds the card lease (ADR-052 S3b, section 3.1): its use is
        taken before `_load_lock` (the lease, then the lock), waiting in the lease line at
        most `wait_s` (None: until cancelled or `check` raises), and `on_wait` is told the
        wait estimate (L10) whenever who it waits for changes. `check` runs on every round of
        the wait and once more at the grant, before anything loads (a peer that left, L13
        point 3). A refusal is a `CardQueueRefusal` (L3). On the CPU none of this applies.

        `audio_s` is the length the door's estimate was made from (`card_ask`); when given, the
        batch descent reads it rather than the decoded samples' (Johnny #30-6), so the call runs the
        batch its estimate assumed. The recorded run reads the decoded length, the one its elapsed
        time was measured on (S3 follow-up 6, Ark #49-3).

        `with_detail` (the music player's Transcribe only) asks for word timestamps and, where the
        backend reports them, per-segment confidence, with the backend's hallucination options
        (`_transcribe_loaded`). It changes the decode and the answer, never the route or the lease.

        `cpu_route=False` skips L12 and asks the card: `transcribe_with_route` passes it after the CPU
        twin failed to load before any audio was processed (Johnny #63-5, S3e)."""
        # A stale reference to a replaced object forwards to the object that replaced it,
        # so a use is counted and ended on one object.
        current = self._current_owner()
        if current is not self:
            return await current.transcribe(audio_path, release_if_loaded_here,
                                            language=language, task=task, purpose=purpose,
                                            wait_s=wait_s, expected_s=expected_s,
                                            on_wait=on_wait, check=check, audio_s=audio_s,
                                            cpu_route=cpu_route, with_detail=with_detail)
        self._refuse_if_retired()
        device, lease = await self._route_for_the_card()
        if lease is None:
            return await self._transcribe_holding(audio_path, release_if_loaded_here, language, task, None,
                                                  audio_s=audio_s, with_detail=with_detail)
        what = purpose or "transcription"
        # L11 (S3e): this call's declared peak, by its length band (the worst band when unknown).
        peak = whisper_vram_mib(self.model_name, audio_s)
        # ADR-052 L12, item 1a (S3d): the GPU/CPU choice, before the lease is asked. A call sent to
        # the CPU twin asks no lease and swaps nothing out; the caller's `transcribe_with_route` runs
        # it there. A call sent to the line stays there (no switch in mid-flight).
        if cpu_route:
            routed = await self._choose_route(lease, what, expected_s, audio_s, peak)
            if routed is not None:
                raise routed
        logger.debug("Whisper '%s': %s asks for the card, bound %s, expected %s", self.alias, what,
                     "none" if wait_s is None else f"{wait_s:g} s",
                     "unknown" if expected_s is None else f"{expected_s:.1f} s")
        token = object()
        try:
            async with lease.use(what, wait_s=wait_s, expected_s=expected_s, on_wait=on_wait,
                                 check=check, vram_mib=peak):
                if check is not None:
                    await check()
                runs = self._current_owner()._card_runs
                runs[id(token)] = (time.monotonic(), _a_positive(expected_s), _a_positive(audio_s))
                try:
                    return await self._transcribe_holding(audio_path, release_if_loaded_here,
                                                          language, task, lease, audio_s=audio_s,
                                                          device=device, with_detail=with_detail)
                finally:
                    runs.pop(id(token), None)
        finally:
            self._let_go_of_an_empty_grant(f"{what} ended with nothing loaded")

    async def _transcribe_holding(self, audio_path: str, release_if_loaded_here: bool,
                                  language: Optional[str], task: Optional[str],
                                  lease: Optional[Any], audio_s: Optional[float] = None,
                                  device: Optional[str] = None, with_detail: bool = False) -> Dict[str, Any]:
        """The body of `transcribe` once the card is held (`lease`) or not needed (None); `device` is
        the route's, for a load under the lease."""
        # The lock is taken even when the model is loaded, so the use is counted
        # atomically with the load and an unload in progress is waited for.
        run = object()
        try:
            async with self._load_lock:
                self._refuse_if_retired()
                loaded_here = not self.model_loaded
                if loaded_here:
                    if self._loads_on_the_cpu(device):
                        # L12 point 3: a load on the CPU is CPU contention too, counted from before it
                        # starts (Zcode #85, minor), under the lock: a call that waits for it is not.
                        _CPU_RUNS[id(run)] = self.alias
                    _, error, cancelled = await self._load_under_the_lease(lease, device)
                    if error is not None:
                        if self.is_cpu_twin and not isinstance(error, ModelNotCachedError):
                            raise TwinDidNotLoad(str(error) or type(error).__name__) from error
                        raise error
                    if cancelled:
                        self._settle_finished_load()
                        raise asyncio.CancelledError
                # A reload may have replaced this object while it waited or loaded: count on
                # the replacement, which holds the shared model state.
                counted = self._current_owner()
                if counted is not self:
                    self._copy_state_to(counted)
                counted._active_transcriptions += 1
                counted._drained.clear()
                counted._cancel_idle_task()
        except BaseException:
            _CPU_RUNS.pop(id(run), None)  # a load that failed, was cancelled or was refused counts no more
            raise
        try:
            # The device the model ended up on decides, whatever the load was expected to run on.
            if counted._device_in_use() == "cpu":
                _CPU_RUNS[id(run)] = counted.alias  # L12 point 3: CPU contention while it runs
            else:
                _CPU_RUNS.pop(id(run), None)
            overrides = {}
            if language is not None:
                overrides["language"] = language
            if task is not None:
                overrides["task"] = task
            if _a_positive(audio_s) is not None:
                overrides["audio_s"] = audio_s
            if with_detail:
                overrides["with_detail"] = True
            return await self._transcribe_loaded(audio_path, **overrides)
        finally:
            _CPU_RUNS.pop(id(run), None)
            owner = counted._current_owner()
            if owner._active_transcriptions > 0:
                owner._active_transcriptions -= 1
            else:
                logger.error("Whisper '%s' (object %x, called on %x): a transcription ended with no "
                             "use counted; the counter is kept at 0", owner.alias, id(owner), id(self))
            if owner._active_transcriptions == 0:
                owner._drained.set()
                try:
                    if release_if_loaded_here and loaded_here:
                        try:
                            await owner.unload_if_idle("released")
                        except Exception:
                            logger.exception("Whisper '%s': releasing the model after the run failed",
                                             owner.alias)
                finally:
                    owner.schedule_idle_unload()  # also when this await was cancelled

    def _note_length_mismatch(self, audio_s: Optional[float], decoded_s: float) -> None:
        """A WARNING when the estimate's length and the decoded one differ by more than
        `_LENGTH_MISMATCH_S` (Ark #49-3): the batch followed the first, the record follows the
        second."""
        estimated = _a_positive(audio_s)
        if estimated is not None and abs(estimated - decoded_s) > _LENGTH_MISMATCH_S:
            logger.warning("Whisper '%s': the estimate read %.1f s of audio and the decode gives %.1f s; "
                           "the batch follows the estimate, the warm figure the decode", self.alias,
                           estimated, decoded_s)

    def _record_run(self, audio_s: float, elapsed: float) -> None:
        """Record a finished run on the object that owns the model now: the first since its load
        gives `penalty_s`, every later one the warm figure of its length band. `audio_s` is the
        decoded length the run was measured on (S3 follow-up 6)."""
        owner = self._current_owner()
        owner._note_run(owner._device_in_use(), audio_s, elapsed, first=owner._calls_since_load == 0)
        owner._calls_since_load += 1

    async def _transcribe_loaded(self, audio_path: str, language: Optional[str] = None,
                                 task: Optional[str] = None, audio_s: Optional[float] = None,
                                 with_detail: bool = False) -> Dict[str, Any]:
        # Per-call values, resolved once: the MLX thread and the HF generate call both use
        # these locals, never `self.language` / `self.task`, which another call may change.
        call_language = self.language if language is None else language
        call_task = self.task if task is None else task
        call_generate_kwargs: Dict[str, Any] = {}
        if language is not None:
            call_generate_kwargs["language"] = None if language == "auto" else language
        if task is not None:
            call_generate_kwargs["task"] = task
        # Clear CUDA cache before transcription to free fragmented memory
        # (MLX transcribes without torch, so its absence is not an error here)
        try:
            import torch
        except ImportError:
            torch = None
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
            free_mem_gb = torch.cuda.mem_get_info()[0] / 1024**3
            logger.debug(f"Cleared CUDA cache before transcription. Free memory: {free_mem_gb:.2f} GB")

        try:
            # MLX path (Apple Silicon)
            if self.pipeline == "mlx":
                import mlx_whisper
                try:
                    import librosa
                except ImportError as e:
                    raise ImportError(f"{_VOICE_MEMORY_HINT} ({e})") from e

                logger.info(f"Transcribing audio with MLX Whisper: model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                audio_array, sample_rate = librosa.load(audio_path, sr=16000)
                duration_seconds = len(audio_array) / sample_rate

                def mlx_transcribe():
                    detail_options = {
                        "word_timestamps": True, "condition_on_previous_text": False,
                        "hallucination_silence_threshold": MUSIC_HALLUCINATION_SILENCE_S,
                    } if with_detail else {}
                    return mlx_whisper.transcribe(
                        audio_path,
                        path_or_hf_repo=self.model_name,
                        language=call_language if call_language != "auto" else None,
                        task=call_task,
                        **detail_options
                    )

                result = await asyncio.to_thread(mlx_transcribe)

                elapsed = asyncio.get_event_loop().time() - start_time
                # The measurement is the decoded length's (Ark #49-3); MLX has no batch descent.
                self._note_length_mismatch(audio_s, duration_seconds)
                self._record_run(duration_seconds, elapsed)
                text = result.get("text", "").strip()
                detected_language = result.get("language", "unknown")

                logger.info(f"MLX transcription completed in {elapsed:.1f}s ({self._realtime_ratio(duration_seconds, elapsed)} real-time): {len(text)} chars")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper_mlx",
                    "segments": self._segments_from_mlx(
                        result.get("segments"), duration_seconds, detail=with_detail
                    ),
                }

            # PyTorch path (CUDA/MPS/CPU)
            else:
                try:
                    import librosa
                except ImportError as e:
                    raise ImportError(f"{_VOICE_MEMORY_HINT} ({e})") from e

                try:
                    audio_array, sample_rate = librosa.load(audio_path, sr=16000)
                except Exception as load_error:
                    logger.error(f"Failed to load audio file with librosa: {load_error}")
                    raise RuntimeError(f"Failed to load audio file '{audio_path}'. The file may be corrupted or in an unsupported format.") from load_error

                duration_seconds = len(audio_array) / sample_rate

                if duration_seconds == 0:
                    logger.error(f"Audio file appears to be empty or corrupted: {audio_path}")
                    raise RuntimeError(f"Audio file '{audio_path}' appears to be empty or corrupted. Loaded {len(audio_array)} samples at {sample_rate}Hz.")

                logger.info(f"Transcribing audio with local Whisper: {duration_seconds:.1f}s, model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                # One length by value (Johnny #30-6): the band the door's estimate read, when it read
                # one, is the band this call runs (batch size), as its estimate assumed; else the
                # decoded length. The run is recorded against the decoded length (Ark #49-3).
                run_s = _a_positive(audio_s) or duration_seconds
                self._note_length_mismatch(audio_s, duration_seconds)

                # Adaptive batch_size for long audio
                # (`_BATCH_DESCENT`, whose thresholds are also the warm figures' length bands)
                adaptive_batch_size = self.batch_size
                for over, divisor in _BATCH_DESCENT:
                    if run_s > over:
                        adaptive_batch_size = max(1, self.batch_size // divisor)
                        size = "Long" if over > 60 else "Medium"
                        logger.info(f"{size} audio ({run_s:.0f}s), reducing batch_size: "
                                    f"{self.batch_size} → {adaptive_batch_size}")
                        break

                # return_timestamps is passed at call time (the standard place for
                # it); the pipeline itself is built once and shared. Segment-level
                # timestamps give chunks of {"timestamp": (start, end), "text": ...}.
                # `with_detail`: one chunk per word and no segments (the lyrics code regroups them);
                # a model or transformers that refuses word timestamps falls back to segments and
                # the answer says `words_unavailable`.
                word_mode = bool(with_detail)
                words_unavailable = False
                pipeline_kwargs = {"generate_kwargs": call_generate_kwargs} if call_generate_kwargs else {}
                try:
                    result = await asyncio.to_thread(
                        self.pipeline,
                        audio_array,
                        batch_size=adaptive_batch_size,
                        return_timestamps="word" if word_mode else True,
                        **pipeline_kwargs
                    )
                except Exception as word_error:
                    if not word_mode or "CUDA out of memory" in str(word_error):
                        raise
                    logger.warning("Whisper '%s': word timestamps failed (%s); falling back to segment "
                                   "timestamps", self.alias, word_error)
                    word_mode, words_unavailable = False, True
                    result = await asyncio.to_thread(
                        self.pipeline,
                        audio_array,
                        batch_size=adaptive_batch_size,
                        return_timestamps=True,
                        **pipeline_kwargs
                    )

                elapsed = asyncio.get_event_loop().time() - start_time
                self._record_run(duration_seconds, elapsed)
                text = result.get("text", "").strip()

                detected_language = "unknown"
                chunks = result.get("chunks")
                chunks_list: List[Any] = []
                if chunks:
                    chunks_list = list(chunks) if hasattr(chunks, '__iter__') else [chunks]
                    for chunk in chunks_list:
                        if isinstance(chunk, dict) and chunk.get("language"):
                            detected_language = chunk["language"]
                            break
                if detected_language == "unknown" and isinstance(result.get("language"), str):
                    detected_language = result["language"]

                words: List[Dict[str, Any]] = []
                if word_mode:
                    words = self._words_from_chunks(chunks_list, duration_seconds)
                    segments: List[Dict[str, Any]] = []
                else:
                    segments = self._segments_from_chunks(chunks_list, duration_seconds,
                                                          bounded_last=with_detail)

                logger.info(f"Local transcription completed in {elapsed:.1f}s ({self._realtime_ratio(duration_seconds, elapsed)} real-time): {len(text)} chars")

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.debug(f"Cleared CUDA cache after transcription")

                gc.collect()
                logger.debug("Python garbage collection completed after transcription")

                answer = {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper",
                    "segments": segments,
                }
                if word_mode:
                    answer["words"] = words
                if words_unavailable:
                    answer["words_unavailable"] = True
                return answer

        except Exception as e:
            error_msg = str(e)

            if "CUDA out of memory" in error_msg:
                logger.warning(f"GPU OOM for {duration_seconds:.0f}s audio (need more VRAM or use turbo model)")

                raise RuntimeError(
                    f"Voice message too long for available GPU VRAM ({duration_seconds:.0f}s). "
                    f"Try: 1) Use 'openai/whisper-large-v3-turbo' model (less VRAM), "
                    f"2) Send shorter voice messages, or 3) Close other GPU applications"
                ) from e
            else:
                logger.error(f"Local transcription failed: {e}", exc_info=True)
                raise RuntimeError(f"Local Whisper transcription failed: {e}") from e

    async def generate_response(self, prompt: str, **kwargs) -> str:
        raise NotImplementedError("LocalWhisperProvider does not support text generation")

    def supports_vision(self) -> bool:
        return False
