# dpc_client_core/providers/whisper_provider.py

import os
import logging
from typing import Dict, Any, Optional, List

from .base import AIProvider, ModelNotCachedError

logger = logging.getLogger(__name__)

ONNX_MODEL_DEFAULT = "onnx-community/whisper-large-v3-turbo"
QUANTIZATION_DEFAULT = "q4f16"

_ONNX_MODEL_MAP = {
    "openai/whisper-large-v3-turbo": "onnx-community/whisper-large-v3-turbo",
    "openai/whisper-large-v3": "onnx-community/whisper-large-v3",
    "openai/whisper-large-v2": "onnx-community/whisper-large-v2",
    "openai/whisper-medium": "onnx-community/whisper-medium",
    "openai/whisper-small": "onnx-community/whisper-small",
    "openai/whisper-base": "onnx-community/whisper-base",
    "openai/whisper-tiny": "onnx-community/whisper-tiny",
}


class LocalWhisperProvider(AIProvider):
    """
    Local Whisper transcription provider using ONNX Runtime via onnx-asr.

    Replaces the PyTorch-based implementation with a lightweight ONNX pipeline:
    - onnx-asr handles model loading, tokenization, mel spectrogram, decoding
    - Silero VAD for long audio chunking (>30s)
    - GPU via CUDAExecutionProvider (same onnxruntime-gpu as BGE-M3 embeddings)
    - No PyTorch, no transformers, no librosa dependencies

    Model: onnx-community/whisper-large-v3-turbo (Q4F16 ~749MB VRAM)
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        raw_model = config.get("model", ONNX_MODEL_DEFAULT)
        self.model_name = _ONNX_MODEL_MAP.get(raw_model, raw_model)
        self.quantization = config.get("quantization", QUANTIZATION_DEFAULT)
        self.language = config.get("language", "auto")
        self.task = config.get("task", "transcribe")
        self.use_vad = config.get("use_vad", True)
        self.lazy_loading = config.get("lazy_loading", True)

        self._asr_model = None
        self._vad = None
        self.model_loaded = False
        self._load_lock = None
        self._detected_device = None

        logger.info(
            "LocalWhisperProvider '%s' initialized (model=%s, quantization=%s)",
            alias, self.model_name, self.quantization,
        )

    def _get_providers(self) -> list:
        import onnxruntime as ort
        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            self._detected_device = "cuda"
            return [
                ("CUDAExecutionProvider", {
                    "arena_extend_strategy": "kSameAsRequested",
                    "cudnn_conv_algo_search": "DEFAULT",
                }),
                "CPUExecutionProvider",
            ]
        self._detected_device = "cpu"
        return ["CPUExecutionProvider"]

    def _load_model(self):
        if self.model_loaded:
            return

        import time
        import onnx_asr

        logger.info("Loading Whisper ONNX model '%s' (quantization=%s)...",
                     self.model_name, self.quantization)
        start_time = time.time()

        try:
            providers = self._get_providers()

            self._asr_model = onnx_asr.load_model(
                self.model_name,
                quantization=self.quantization,
                providers=providers,
            )

            if self.use_vad:
                self._vad = onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])
                self._asr_model = self._asr_model.with_vad(self._vad)
                logger.info("Silero VAD enabled for long audio support")

            self.model_loaded = True
            elapsed = time.time() - start_time
            logger.info("Whisper ONNX model loaded in %.1fs (device=%s, quantization=%s)",
                        elapsed, self._detected_device, self.quantization)

        except Exception as e:
            error_msg = str(e)
            if "No such file or directory" in error_msg or "does not exist" in error_msg:
                cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
                raise ModelNotCachedError(
                    model_name=self.model_name,
                    cache_path=cache_dir,
                    download_size_gb=1.0,
                ) from e
            logger.error("Failed to load Whisper ONNX model: %s", e, exc_info=True)
            raise RuntimeError(f"Failed to load Whisper ONNX model: {e}") from e

    def is_model_loaded(self) -> bool:
        return self.model_loaded

    async def ensure_model_loaded(self) -> None:
        if self.model_loaded:
            return

        if self._load_lock is None:
            import asyncio
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            if self.model_loaded:
                return
            import asyncio
            await asyncio.to_thread(self._load_model)

    def _unload_model(self) -> None:
        if not self.model_loaded:
            logger.info("Whisper ONNX model already unloaded")
            return

        logger.info("Unloading Whisper ONNX model '%s'...", self.model_name)

        try:
            del self._asr_model
            del self._vad
        except Exception:
            pass

        self._asr_model = None
        self._vad = None
        self.model_loaded = False

        import gc
        gc.collect()

        logger.info("Whisper ONNX model unloaded")

    async def unload_model_async(self) -> None:
        if self._load_lock is None:
            import asyncio
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            if not self.model_loaded:
                return
            import asyncio
            await asyncio.to_thread(self._unload_model)

    async def download_model_async(self, progress_callback=None) -> Dict[str, Any]:
        import asyncio

        logger.info("Downloading Whisper ONNX model '%s' (~1GB)...", self.model_name)

        if progress_callback:
            await progress_callback("Preparing download", 0.0)

        def _download():
            import onnx_asr
            try:
                providers = self._get_providers()
                model = onnx_asr.load_model(
                    self.model_name,
                    quantization=self.quantization,
                    providers=providers,
                )
                del model
                import gc
                gc.collect()

                return {
                    "success": True,
                    "message": f"Model '{self.model_name}' downloaded successfully",
                    "model_name": self.model_name,
                    "cache_path": os.path.expanduser("~/.cache/huggingface/hub"),
                }
            except Exception as e:
                logger.error("Failed to download Whisper ONNX model: %s", e, exc_info=True)
                return {
                    "success": False,
                    "message": f"Download failed: {e}",
                    "model_name": self.model_name,
                    "cache_path": os.path.expanduser("~/.cache/huggingface/hub"),
                }

        if progress_callback:
            await progress_callback("Downloading model files", 0.3)

        result = await asyncio.to_thread(_download)

        if progress_callback:
            await progress_callback(
                "Download complete" if result["success"] else "Download failed", 1.0,
            )

        return result

    async def transcribe(self, audio_path: str) -> Dict[str, Any]:
        import asyncio

        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        if not self.model_loaded:
            async with self._load_lock:
                if not self.model_loaded:
                    await asyncio.to_thread(self._load_model)

        logger.info("Transcribing audio with Whisper ONNX: %s", audio_path)
        start_time = asyncio.get_event_loop().time()

        try:
            result = await asyncio.to_thread(self._asr_model.recognize, audio_path)

            elapsed = asyncio.get_event_loop().time() - start_time
            text = result if isinstance(result, str) else str(result)
            text = text.strip()

            logger.info("ONNX transcription completed in %.1fs: %d chars", elapsed, len(text))

            return {
                "text": text,
                "language": self.language if self.language != "auto" else "auto",
                "duration": 0.0,
                "provider": "local_whisper_onnx",
            }

        except Exception as e:
            logger.error("ONNX transcription failed: %s", e, exc_info=True)
            raise RuntimeError(f"Whisper ONNX transcription failed: {e}") from e

    async def generate_response(self, prompt: str, **kwargs) -> str:
        raise NotImplementedError("LocalWhisperProvider does not support text generation")

    def supports_vision(self) -> bool:
        return False
