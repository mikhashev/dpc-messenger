# dpc_client_core/providers/whisper_provider.py

import asyncio
import gc
import os
import logging
from typing import Dict, Any, Optional, List

from .base import AIProvider, ModelNotCachedError

logger = logging.getLogger(__name__)


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

        # Model state
        self.pipeline = None
        self.model_loaded = False
        self._load_lock = asyncio.Lock()
        self._detected_device = None

        logger.info(f"LocalWhisperProvider '{alias}' initialized (model={self.model_name}, device={self.device})")

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
                logger.debug("MLX not available (install with: uv sync --extra mlx)")

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
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
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
                    logger.warning(
                        f"Whisper model '{self.model_name}' not found in cache. "
                        f"Download required (~3GB). User will be prompted."
                    )
                    raise ModelNotCachedError(
                        model_name=self.model_name,
                        cache_path=cache_dir,
                        download_size_gb=3.0
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
                generate_kwargs={
                    "language": self.language if self.language != "auto" else None,
                    "task": self.task
                },
                ignore_warning=True
            )

            if self.compile_model and device == "cuda":
                logger.info("Applying torch.compile optimization (4.5x speedup)...")
                self.pipeline.model = torch.compile(self.pipeline.model)

            self.model_loaded = True
            elapsed = time.time() - start_time
            logger.info(f"Whisper model loaded in {elapsed:.1f} seconds (device={device})")

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}", exc_info=True)
            raise RuntimeError(f"Failed to load local Whisper model: {e}") from e

    def is_model_loaded(self) -> bool:
        return self.model_loaded

    async def ensure_model_loaded(self) -> None:
        if self.model_loaded:
            return
        async with self._load_lock:
            if self.model_loaded:
                return
            await asyncio.to_thread(self._load_model)

    def _unload_model(self) -> None:
        if not self.model_loaded:
            logger.info("Whisper model already unloaded")
            return

        logger.info(f"Unloading Whisper model '{self.model_name}' from memory...")

        try:
            if self.pipeline == "mlx":
                if hasattr(self, 'whisper_model'):
                    del self.whisper_model
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

        except Exception as e:
            logger.error(f"Error unloading Whisper model: {e}", exc_info=True)
            self.pipeline = None
            self.model_loaded = False

    async def unload_model_async(self) -> None:
        async with self._load_lock:
            if not self.model_loaded:
                logger.debug("Whisper model not loaded, skipping unload")
                return
            await asyncio.to_thread(self._unload_model)
            logger.info("Whisper model unloaded (async)")

    async def download_model_async(self, progress_callback=None) -> Dict[str, Any]:
        logger.info(f"Starting download of Whisper model '{self.model_name}' (~3GB)...")

        if progress_callback:
            await progress_callback("Preparing download", 0.0)

        def _download():
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
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
                    "message": f"Model '{self.model_name}' downloaded successfully (~3GB)",
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

    async def transcribe(self, audio_path: str) -> Dict[str, Any]:
        if not self.model_loaded:
            async with self._load_lock:
                if not self.model_loaded:
                    await asyncio.to_thread(self._load_model)

        # Clear CUDA cache before transcription to free fragmented memory
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            free_mem_gb = torch.cuda.mem_get_info()[0] / 1024**3
            logger.debug(f"Cleared CUDA cache before transcription. Free memory: {free_mem_gb:.2f} GB")

        try:
            # MLX path (Apple Silicon)
            if self.pipeline == "mlx":
                import mlx_whisper
                import librosa

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

                logger.info(f"MLX transcription completed in {elapsed:.1f}s ({duration_seconds/elapsed:.1f}x real-time): {len(text)} chars")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper_mlx"
                }

            # PyTorch path (CUDA/MPS/CPU)
            else:
                import librosa

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

                result = await asyncio.to_thread(
                    self.pipeline,
                    audio_array,
                    batch_size=adaptive_batch_size
                )

                elapsed = asyncio.get_event_loop().time() - start_time
                text = result.get("text", "").strip()

                detected_language = "unknown"
                chunks = result.get("chunks")
                if chunks:
                    chunks_list = list(chunks) if hasattr(chunks, '__iter__') else chunks
                    if len(chunks_list) > 0:
                        detected_language = chunks_list[0].get("language", "unknown")

                logger.info(f"Local transcription completed in {elapsed:.1f}s ({duration_seconds/elapsed:.1f}x real-time): {len(text)} chars")

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.debug(f"Cleared CUDA cache after transcription")

                gc.collect()
                logger.debug("Python garbage collection completed after transcription")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper"
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
