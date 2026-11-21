# dpc_client_core/providers/whisper_provider.py

import asyncio
import gc
import os
import logging
from typing import Callable, Dict, Any, Optional, List

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
            logger.info(f"Whisper model loaded in {elapsed:.1f} seconds (device={device})")

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}", exc_info=True)
            raise RuntimeError(f"Failed to load local Whisper model: {e}") from e

    def is_model_loaded(self) -> bool:
        return self.model_loaded

    async def ensure_model_loaded(self) -> None:
        owner = self._current_owner()
        if owner is not self:
            return await owner.ensure_model_loaded()
        self._refuse_if_retired()
        if self.model_loaded:
            return
        async with self._load_lock:
            if self.model_loaded:
                return
            self._refuse_if_retired()
            await asyncio.to_thread(self._load_model)
            successor = self._current_owner()
            if successor is not self:  # a reload handed this object over while it loaded
                self._copy_state_to(successor)
            self.schedule_idle_unload()  # a preload nobody uses must not hold the card

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
        self._unloading = True
        try:
            released = await asyncio.to_thread(self._unload_model)
        finally:
            self._unloading = False
        if released is False:
            logger.error("Whisper '%s' (%s): the unload failed (%s); the model reference was dropped "
                         "but its memory may not be free", self.alias, self.model_name, reason)
        else:
            logger.info("Whisper '%s' (%s) unloaded: %s", self.alias, self.model_name, detail or reason)
        self._notify_unloaded(reason)
        return not self.model_loaded

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
        """Shutdown path: wait up to `timeout` s in total for running transcriptions and
        the load lock, then unload the model anyway (logged) so the card is free before
        the process exits."""
        self.retire()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            await asyncio.wait_for(self._drained.wait(), max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            logger.warning("Whisper '%s': %d transcription(s) still running after %g s at shutdown; "
                           "unloading anyway", self.alias, self._active_transcriptions, timeout)
        try:
            await asyncio.wait_for(self._load_lock.acquire(), max(0.5, deadline - loop.time()))
        except asyncio.TimeoutError:
            logger.error("Whisper '%s': the load lock was not free within the %g s shutdown bound; "
                         "the model is left loaded at exit", self.alias, timeout)
            return
        try:
            if self.model_loaded:
                await self._unload_locked("shutdown")
        finally:
            self._load_lock.release()
        if self.model_loaded:
            logger.error("Whisper '%s': the model is still loaded at exit", self.alias)

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

    async def transcribe(self, audio_path: str, release_if_loaded_here: bool = False) -> Dict[str, Any]:
        """Transcribe `audio_path`. With `release_if_loaded_here`, a model this very call
        had to load is unloaded as the call ends, unless another transcription is using
        it (the decision is made under the load lock, so it cannot race)."""
        # A stale reference to a replaced object forwards to the object that replaced it,
        # so a use is counted and ended on one object.
        current = self._current_owner()
        if current is not self:
            return await current.transcribe(audio_path, release_if_loaded_here)
        self._refuse_if_retired()
        # The lock is taken even when the model is loaded, so the use is counted
        # atomically with the load and an unload in progress is waited for.
        async with self._load_lock:
            self._refuse_if_retired()
            loaded_here = not self.model_loaded
            if loaded_here:
                await asyncio.to_thread(self._load_model)
            # A reload may have replaced this object while it waited or loaded: count on
            # the replacement, which holds the shared model state.
            counted = self._current_owner()
            if counted is not self:
                self._copy_state_to(counted)
            counted._active_transcriptions += 1
            counted._drained.clear()
            counted._cancel_idle_task()
        try:
            return await self._transcribe_loaded(audio_path)
        finally:
            owner = counted._current_owner()
            if owner._active_transcriptions > 0:
                owner._active_transcriptions -= 1
            else:
                logger.error("Whisper '%s': a transcription ended with no use counted; "
                             "the counter is kept at 0", owner.alias)
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

    async def _transcribe_loaded(self, audio_path: str) -> Dict[str, Any]:
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
                        language=self.language if self.language != "auto" else None,
                        task=self.task
                    )

                result = await asyncio.to_thread(mlx_transcribe)

                elapsed = asyncio.get_event_loop().time() - start_time
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
                    return_timestamps=True
                )

                elapsed = asyncio.get_event_loop().time() - start_time
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
