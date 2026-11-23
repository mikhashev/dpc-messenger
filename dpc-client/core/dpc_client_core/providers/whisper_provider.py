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

# Start figures per device until this provider measures its own (ADR-052 L10, L12). Ratios are audio
# seconds per wall second; `cold` is the first call after a load, `load_s` the load itself.
#   cuda, "GPU figures on speech": warm 15.9x (the slowest row of 16-43x), cold 8.8x (the measured
#     first call), load 12.8 s (the fresh-process cold load L13 counts).
#   cpu, L12's table: 300 s of audio in 306 s; its "load + first call ≈ 9 s" is all in `load_s`.
# A device absent here (mps, mlx) is unknown until measured.
_START_FIGURES: Dict[str, Dict[str, float]] = {
    "cuda": {"warm": 15.9, "cold": 8.8, "load_s": 12.8},
    "cpu": {"warm": 300.0 / 306.0, "cold": 300.0 / 306.0, "load_s": 9.0},
}
_FIGURE_KEYS = ("warm", "cold", "load_s")
# ADR-052 L4: the margin a person's door keeps for the transcription itself, `max(10 s, estimate)`.
MIN_MARGIN_S = 10.0
# ADR-052 L11: what a loaded Whisper holds on the card beyond its weights, MiB (torch reserved peak at
# batch 8 plus the CUDA context, less the weights; "GPU figures on speech"). A start value.
_WHISPER_OVERHEAD_MIB = 2900
# The words a Stop or a refusal adds about a Whisper holder (ADR-052 2.2 item 7).
_STOP_HINT = "It lets go when idle; Stop unloads it now."


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
             caller_left_s: Optional[float] = None, audio_s: Optional[float] = None) -> Dict[str, Any]:
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
    is what a later waiter's estimate counts for this call)."""
    if audio_s is None:
        audio_s = audio_seconds(audio_path)
    estimate = getattr(provider, "expected_seconds", None)
    expected_s = estimate(audio_s) if callable(estimate) else None
    wait_s = None if bound_s is None else max(0.0, float(bound_s))
    if caller_left_s is not None:
        margin = max(MIN_MARGIN_S, expected_s or 0.0)
        left = max(0.0, float(caller_left_s) - margin)
        wait_s = left if wait_s is None else min(wait_s, left)
    return {"purpose": purpose, "wait_s": wait_s, "expected_s": expected_s}


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
    return f"transcription queued behind {behind}, {when}"


def whisper_vram_mib(model_name: str) -> Optional[int]:
    """The declared peak of a loaded Whisper model in MiB (ADR-052 3.1 and L11): the weights from
    the measured size table plus `_WHISPER_OVERHEAD_MIB`, else None (unknown, never 0). Never asks
    the network (`model_sizes.measured_size_bytes`)."""
    from .model_sizes import measured_size_bytes

    size = measured_size_bytes(model_name)
    return None if not size else size // 2**20 + _WHISPER_OVERHEAD_MIB

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
        # ADR-052 S3a: the speed this provider measured, {device: {"warm"|"cold"|"load_s": s}}, over
        # `_START_FIGURES`; the device the model is loaded on now (a load may fall back to the CPU);
        # the calls since that load (0: the next one is cold); `auto` resolved for an estimate.
        self._measured: Dict[str, Dict[str, float]] = {}
        self._loaded_device: Optional[str] = None
        self._calls_since_load = 0
        self._estimate_device: Optional[str] = None
        # ADR-052 S3b: this alias's standing on the card, built on the first call that runs on a GPU
        # device (never for the CPU) and handed over in a providers reload; and the calls holding it
        # now, {token: (monotonic start, expected_s)}, shared with a successor, for its `eta_s`.
        self._card_lease: Optional[Any] = None
        self._card_runs: Dict[int, Any] = {}

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
                    logger.info("Forcing CPU mode for Whisper model")
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
        """A load finished on `device` (where the model really is): its time is recorded and the
        next call is the cold one."""
        self._loaded_device = device
        self._calls_since_load = 0
        seconds = _a_positive(load_s)
        if seconds is not None:
            self._measured.setdefault(device, {})["load_s"] = seconds

    def _note_run(self, device: Optional[str], audio_s: float, elapsed: float, *, cold: bool) -> None:
        """One transcription of `audio_s` took `elapsed` on `device`: the ratio replaces the cold or
        the warm figure. A ratio that cannot be computed is not recorded."""
        audio, wall = _a_positive(audio_s), _a_positive(elapsed)
        if device is None or audio is None or wall is None:
            return
        self._measured.setdefault(device, {})["cold" if cold else "warm"] = audio / wall

    def _device_in_use(self) -> Optional[str]:
        """The device the loaded model runs on, for recording a run."""
        if self.pipeline == "mlx":
            return "mlx"
        if self._loaded_device is not None:
            return self._loaded_device
        if self._detected_device is not None:
            return self._detected_device
        return None if self.device == "auto" else self.device

    def _route_device(self) -> str:
        """The device a call would run on now: the loaded model's, else the configured one; `auto`
        is detected once (it may import torch: call from a thread when that matters)."""
        if self.model_loaded:
            in_use = self._device_in_use()
            if in_use is not None:
                return in_use
        if self.device != "auto":
            return self.device
        if self._estimate_device is None:
            self._estimate_device = self._detected_device or self._detect_device()
        return self._estimate_device

    def speed_figures(self, device: str) -> Dict[str, Optional[float]]:
        """`warm`, `cold` and `load_s` for `device`: measured here, else the start figure, else None."""
        measured = self._measured.get(device, {})
        start = _START_FIGURES.get(device, {})
        return {key: measured.get(key, start.get(key)) for key in _FIGURE_KEYS}

    def expected_seconds(self, audio_s: Any, device: Optional[str] = None,
                         loaded: Optional[bool] = None) -> Optional[float]:
        """Wall seconds a transcription of `audio_s` would take, or None when unknown (ADR-052 L10,
        L13 point 2): the load when the model is not loaded, plus the audio divided by the cold ratio
        (the first call after a load) or the warm one, through `card_lease.seconds_from_ratio`.
        `device` and `loaded` default to what this provider is now."""
        owner = self._current_owner()
        if owner is not self:
            return owner.expected_seconds(audio_s, device, loaded)
        from ..managers.card_lease import seconds_from_ratio

        if loaded is None:
            loaded = self.model_loaded
        figures = self.speed_figures(device or self._route_device())
        cold = not loaded or self._calls_since_load == 0
        work = seconds_from_ratio(audio_s, figures["cold" if cold else "warm"])
        if work is None:
            return None
        if loaded:
            return work
        load = figures["load_s"]
        return None if load is None else load + work

    # -- the card lease (ADR-052 S3b) --

    async def _lease_for_the_card(self) -> Optional[Any]:
        """The lease a call holds the card under, or None when it runs on the CPU (ADR-052 3.1: a CPU
        device builds no lease; a model that fell back to the CPU routes there). The route's device
        is `_route_device`: an explicit device is used as is (no torch import), `auto` is detected
        once, in a thread."""
        if (self.device == "auto" and not self.model_loaded and self._estimate_device is None
                and self._detected_device is None):
            device = await asyncio.to_thread(self._route_device)
        else:
            device = self._route_device()
        if device == "cpu":
            return None
        return self._the_card_lease()

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
            eta_s=self._card_eta, stop_hint=_STOP_HINT,
        )
        return self._card_lease

    async def _yield_for_the_card(self, why: str) -> str:
        """The lease's `on_yield`: the object that owns the model now lets go."""
        return await self._current_owner().yield_card(why)

    def _card_eta(self) -> Optional[float]:
        """The lease's `eta_s`: the longest remaining expected time of the calls holding the card now,
        None when any is unknown or none runs (L10). An overrun reads as unknown (`_positive`)."""
        runs = list(self._current_owner()._card_runs.values())
        if not runs:
            return None
        now = time.monotonic()
        left = []
        for started, expected in runs:
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

    async def _load_under_the_lease(self, lease: Optional[Any]):
        """`_load_model` in its thread, the lease marked `starting` meanwhile. The caller holds
        `_load_lock`. Returns `_finish_thread`'s triple."""
        if lease is not None:
            lease.mark_starting()
        try:
            outcome = await self._finish_thread(self._load_model)
        finally:
            if lease is not None:
                lease.mark_running()
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
        lease = await self._lease_for_the_card()
        if lease is None:
            return await self._preload(None)
        from ..managers.card_lease import LeaseSkipped
        try:
            # A use, not a bare grant: busy while it loads, so no contender asks it to let go before
            # the load starts (a grant with no use is idle, L6).
            async with lease.use("preload", wait_s=0, background=True):
                await self._preload(lease)
        except LeaseSkipped as skipped:
            logger.info("Whisper preload skipped: %s", skipped)
            raise
        finally:
            self._let_go_of_an_empty_grant("the preload loaded nothing")

    async def _preload(self, lease: Optional[Any]) -> None:
        async with self._load_lock:
            if self.model_loaded:
                return
            self._refuse_if_retired()
            _, error, cancelled = await self._load_under_the_lease(lease)
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

    def _copy_state_to(self, new: "LocalWhisperProvider") -> None:
        if not new.model_loaded and self.model_loaded:
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
        if new.model_name == self.model_name:
            # The speed measured here is the same model's: `new` keeps what it measured itself.
            for device, figures in self._measured.items():
                kept = new._measured.setdefault(device, {})
                for key, value in figures.items():
                    kept.setdefault(key, value)
            if new.model_loaded and self.model_loaded and new._loaded_device is None:
                new._loaded_device = self._loaded_device
                new._calls_since_load = self._calls_since_load
        if new.model_loaded:
            self.model_loaded = False  # one owner of the shared pipeline: `new`
        self._successor = new
        self.retire()

    def retire(self) -> None:
        """This object is replaced (providers reload hands its model to the new one) or
        shut down: stop its timer and never schedule another."""
        self._retired = True
        self._cancel_idle_task()

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

    def _get_audio_duration(self, audio_path: str) -> float:
        try:
            import soundfile as sf
            info = sf.info(audio_path)
            return info.duration
        except Exception:
            return 0.0

    @staticmethod
    def _realtime_ratio(duration_seconds: float, elapsed: float) -> str:
        """Audio seconds per wall second, for the log line; "n/a" when the clock did not advance."""
        if elapsed <= 0:
            return "n/a"
        return f"{duration_seconds / elapsed:.1f}x"

    @staticmethod
    def _segments_from_chunks(chunks: Any, duration_seconds: float) -> List[Dict[str, Any]]:
        """Normalise HF pipeline chunks into {start, end, text} segments.

        The pipeline hands back ``{"timestamp": (start, end), "text": ...}`` per
        chunk and leaves ``end`` as None on the last one; that chunk runs to the
        end of the audio, so it gets the measured duration.
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
            if end is None:
                end = duration_seconds
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            segments.append({"start": float(start), "end": float(end), "text": text})
        return segments

    @staticmethod
    def _segments_from_mlx(raw_segments: Any, duration_seconds: float) -> List[Dict[str, Any]]:
        """Normalise mlx_whisper segments into the same {start, end, text} shape."""
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
            segments.append({"start": float(start), "end": float(end), "text": text})
        return segments

    async def transcribe(self, audio_path: str, release_if_loaded_here: bool = False,
                         language: Optional[str] = None, task: Optional[str] = None, *,
                         purpose: Optional[str] = None, wait_s: Optional[float] = None,
                         expected_s: Optional[float] = None,
                         on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
                         check: Optional[Callable[[], Awaitable[None]]] = None) -> Dict[str, Any]:
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
        point 3). A refusal is a `CardQueueRefusal` (L3). On the CPU none of this applies."""
        # A stale reference to a replaced object forwards to the object that replaced it,
        # so a use is counted and ended on one object.
        current = self._current_owner()
        if current is not self:
            return await current.transcribe(audio_path, release_if_loaded_here,
                                            language=language, task=task, purpose=purpose,
                                            wait_s=wait_s, expected_s=expected_s,
                                            on_wait=on_wait, check=check)
        self._refuse_if_retired()
        lease = await self._lease_for_the_card()
        if lease is None:
            return await self._transcribe_holding(audio_path, release_if_loaded_here, language, task, None)
        # S3d seam (ADR-052 L12, item 1a): the GPU/CPU choice goes HERE, before the lease is
        # asked; a call L12 sends to the CPU twin asks no lease and swaps nothing out. Until
        # then every GPU call asks the lease.
        what = purpose or "transcription"
        logger.debug("Whisper '%s': %s asks for the card, bound %s, expected %s", self.alias, what,
                     "none" if wait_s is None else f"{wait_s:g} s",
                     "unknown" if expected_s is None else f"{expected_s:.1f} s")
        token = object()
        try:
            async with lease.use(what, wait_s=wait_s, expected_s=expected_s, on_wait=on_wait,
                                 check=check):
                if check is not None:
                    await check()
                runs = self._current_owner()._card_runs
                runs[id(token)] = (time.monotonic(), _a_positive(expected_s))
                try:
                    return await self._transcribe_holding(audio_path, release_if_loaded_here,
                                                          language, task, lease)
                finally:
                    runs.pop(id(token), None)
        finally:
            self._let_go_of_an_empty_grant(f"{what} ended with nothing loaded")

    async def _transcribe_holding(self, audio_path: str, release_if_loaded_here: bool,
                                  language: Optional[str], task: Optional[str],
                                  lease: Optional[Any]) -> Dict[str, Any]:
        """The body of `transcribe` once the card is held (`lease`) or not needed (None)."""
        # The lock is taken even when the model is loaded, so the use is counted
        # atomically with the load and an unload in progress is waited for.
        async with self._load_lock:
            self._refuse_if_retired()
            loaded_here = not self.model_loaded
            if loaded_here:
                _, error, cancelled = await self._load_under_the_lease(lease)
                if error is not None:
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
        try:
            overrides = {}
            if language is not None:
                overrides["language"] = language
            if task is not None:
                overrides["task"] = task
            return await self._transcribe_loaded(audio_path, **overrides)
        finally:
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

    def _record_run(self, audio_s: float, elapsed: float) -> None:
        """Record a finished run on the object that owns the model now: the first since its load is
        the cold figure, every later one the warm figure."""
        owner = self._current_owner()
        owner._note_run(owner._device_in_use(), audio_s, elapsed, cold=owner._calls_since_load == 0)
        owner._calls_since_load += 1

    async def _transcribe_loaded(self, audio_path: str, language: Optional[str] = None,
                                 task: Optional[str] = None) -> Dict[str, Any]:
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
                    return mlx_whisper.transcribe(
                        audio_path,
                        path_or_hf_repo=self.model_name,
                        language=call_language if call_language != "auto" else None,
                        task=call_task
                    )

                result = await asyncio.to_thread(mlx_transcribe)

                elapsed = asyncio.get_event_loop().time() - start_time
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
                        result.get("segments"), duration_seconds
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

                # Adaptive batch_size for long audio
                adaptive_batch_size = self.batch_size
                if duration_seconds > 180:
                    adaptive_batch_size = max(1, self.batch_size // 8)
                    logger.info(f"Long audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")
                elif duration_seconds > 120:
                    adaptive_batch_size = max(1, self.batch_size // 4)
                    logger.info(f"Long audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")
                elif duration_seconds > 60:
                    adaptive_batch_size = max(1, self.batch_size // 2)
                    logger.info(f"Medium audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")

                # return_timestamps is passed at call time (the standard place for
                # it); the pipeline itself is built once and shared. Segment-level
                # timestamps give chunks of {"timestamp": (start, end), "text": ...}.
                result = await asyncio.to_thread(
                    self.pipeline,
                    audio_array,
                    batch_size=adaptive_batch_size,
                    return_timestamps=True,
                    **({"generate_kwargs": call_generate_kwargs} if call_generate_kwargs else {})
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

                segments = self._segments_from_chunks(chunks_list, duration_seconds)

                logger.info(f"Local transcription completed in {elapsed:.1f}s ({self._realtime_ratio(duration_seconds, elapsed)} real-time): {len(text)} chars")

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.debug(f"Cleared CUDA cache after transcription")

                gc.collect()
                logger.debug("Python garbage collection completed after transcription")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper",
                    "segments": segments,
                }

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
