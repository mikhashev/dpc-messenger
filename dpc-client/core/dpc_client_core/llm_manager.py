# dpc-client/core/dpc-client_core/llm_manager.py

import os
import json
import asyncio
import logging
import base64
from pathlib import Path
from typing import Dict, Any, Optional, List

# Import client libraries
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
import ollama

logger = logging.getLogger(__name__)

# Token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available - token counting will use estimation for all models")

# --- Abstract Base Class for all Providers ---

class AIProvider:
    """Abstract base class for all AI providers."""
    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = config
        self.model = config.get("model")

    async def generate_response(self, prompt: str) -> str:
        """Generates a response from the AI model."""
        raise NotImplementedError

    def supports_vision(self) -> bool:
        """Returns True if this provider supports vision API (multimodal queries)."""
        return False

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Generates a response from the AI model with image inputs (vision API).

        Args:
            prompt: Text prompt
            images: List of image dicts with keys:
                - path: str (absolute path to image file)
                - mime_type: str (e.g., "image/png")
                - base64: str (optional, if already encoded)
            **kwargs: Additional parameters (temperature, max_tokens, etc.)

        Returns:
            str: AI response text

        Raises:
            NotImplementedError: If provider doesn't support vision
        """
        raise NotImplementedError(f"Vision API not implemented for {self.__class__.__name__}")

# --- Concrete Provider Implementations ---

# Vision-capable Ollama models (for auto-detection)
OLLAMA_VISION_MODELS = [
    "qwen3-vl",         # Qwen3-VL (all variants: 2b, 4b, 8b, 30b, etc.)
    "llava",            # LLaVA variants
    "llama3.2-vision",  # Llama 3.2 vision models
    "ministral-3",      # Ministral 3 vision models (3b, 8b, 14b)
    "bakllava",         # BakLLaVA
    "moondream",        # Moondream
]

class OllamaProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        self.client = ollama.AsyncClient(host=config.get("host"))

    def supports_vision(self) -> bool:
        """Check if this Ollama model supports vision/multimodal inputs."""
        return any(vm in self.model.lower() for vm in OLLAMA_VISION_MODELS)

    async def generate_response(self, prompt: str) -> str:
        try:
            message = {'role': 'user', 'content': prompt}

            # Build options dict for custom parameters
            options = {}
            if self.config.get("context_window"):
                options["num_ctx"] = self.config["context_window"]

            # Add a timeout to the request
            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model,
                    messages=[message],
                    options=options if options else None
                ),
                timeout=60.0 # 60 second timeout
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama provider '{self.alias}' timed out after 60 seconds.")
        except Exception as e:
            raise RuntimeError(f"Ollama provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Ollama vision API using images parameter.
        Docs: https://docs.ollama.com/capabilities/vision

        Args:
            prompt: Text prompt
            images: List of dicts with keys:
                - path: str (file path)
                - base64: str (optional, base64 data)
                - mime_type: str (optional)
            **kwargs: Additional parameters (temperature, timeout, etc.)

        Returns:
            str: AI response text
        """
        try:
            # Build image list (Ollama accepts paths or base64)
            image_inputs = []
            for img in images:
                if "base64" in img:
                    # Use base64 data if available
                    base64_data = img["base64"]
                    # Strip data URL prefix if present (data:image/png;base64,...)
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                    image_inputs.append(base64_data)
                elif "path" in img:
                    # Use file path (Ollama SDK handles reading)
                    image_inputs.append(str(img["path"]))
                else:
                    raise ValueError("Image must have 'path' or 'base64' key")

            # Build message with images
            message = {
                'role': 'user',
                'content': prompt,
                'images': image_inputs
            }

            # Build options dict for custom parameters
            options = {}
            if self.config.get("context_window"):
                options["num_ctx"] = self.config["context_window"]

            # Vision queries may take longer - use configurable timeout
            timeout = kwargs.get("timeout", 120.0)  # Default 120s for vision

            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model,
                    messages=[message],
                    options=options if options else None
                ),
                timeout=timeout
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama vision query '{self.alias}' timed out after {kwargs.get('timeout', 120)}s.")
        except Exception as e:
            raise RuntimeError(f"Ollama vision API failed for '{self.alias}': {e}") from e

    async def get_model_info(self) -> Dict[str, Any]:
        """Query Ollama for model information including parameters.

        Returns:
            Dict containing:
                - modelfile: Raw modelfile content
                - parameters: Model parameters string
                - num_ctx: Parsed context window size (or None)
                - details: Model details (family, parameter_size, etc.)
        """
        try:
            response = await self.client.show(model=self.model)

            # Parse num_ctx from modelfile
            num_ctx = None
            modelfile = response.get('modelfile', '')
            if modelfile:
                num_ctx = self._parse_num_ctx_from_modelfile(modelfile)

            # Convert details to dict if it's a Pydantic model
            details = response.get('details')
            if details:
                # Handle Pydantic models (they have model_dump method)
                if hasattr(details, 'model_dump'):
                    details = details.model_dump(exclude_none=True)
                elif hasattr(details, 'dict'):
                    details = details.dict(exclude_none=True)
                elif isinstance(details, dict):
                    details = details
                else:
                    details = {}
            else:
                details = {}

            # Convert modified_at datetime to string if present
            modified_at = response.get('modified_at')
            if modified_at and hasattr(modified_at, 'isoformat'):
                modified_at = modified_at.isoformat()

            return {
                "modelfile": modelfile,
                "parameters": response.get('parameters', ''),
                "num_ctx": num_ctx,
                "details": details,
                "template": response.get('template', ''),
                "modified_at": modified_at,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to get model info for '{self.model}': {e}") from e

    @staticmethod
    def _parse_num_ctx_from_modelfile(modelfile: str) -> Optional[int]:
        """Extract num_ctx parameter from modelfile string.

        Args:
            modelfile: Raw modelfile content

        Returns:
            Context window size as integer, or None if not found
        """
        import re
        match = re.search(r'PARAMETER\s+num_ctx\s+(\d+)', modelfile, re.IGNORECASE)
        return int(match.group(1)) if match else None

class OpenAICompatibleProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env")
            if api_key_env:
                api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found for OpenAI compatible provider '{self.alias}'")

        self.client = AsyncOpenAI(base_url=config.get("base_url"), api_key=api_key)

    def supports_vision(self) -> bool:
        """OpenAI vision models: gpt-4o, gpt-4-turbo, gpt-4o-mini"""
        vision_models = ["gpt-4o", "gpt-4-turbo", "gpt-4o-mini"]
        return any(vm in self.model for vm in vision_models)

    async def generate_response(self, prompt: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI compatible provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        OpenAI vision API using multimodal content arrays.
        Docs: https://platform.openai.com/docs/guides/vision
        """
        try:
            # Build multimodal message content
            content = [{"type": "text", "text": prompt}]

            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    # Strip data URL prefix if present
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                # OpenAI expects data URL format
                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_data}",
                        "detail": "high"  # or "low" for faster/cheaper processing
                    }
                })

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 4000)
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI vision API failed for '{self.alias}': {e}") from e

class AnthropicProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key_env = config.get("api_key_env")
        api_key = os.getenv(api_key_env) if api_key_env else None

        if not api_key:
            raise ValueError(f"API key environment variable not set for Anthropic provider '{self.alias}'")

        self.client = AsyncAnthropic(api_key=api_key)

        # Read max_tokens from config (optional, defaults to 4096  if not specified)
        # Set to None or omit from config to use model's maximum
        self.max_tokens = config.get("max_tokens", 4096)

    def supports_vision(self) -> bool:
        """Claude 3+ models support vision"""
        vision_models = ["claude-3", "claude-opus", "claude-sonnet", "claude-haiku"]
        return any(vm in self.model for vm in vision_models)

    async def generate_response(self, prompt: str) -> str:
        try:
            # Use configured max_tokens or default (Anthropic requires max_tokens parameter)
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens if self.max_tokens else 4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            raise RuntimeError(f"Anthropic provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Anthropic vision API using multimodal content blocks.
        Docs: https://docs.anthropic.com/claude/docs/vision
        """
        try:
            # Build multimodal content array
            content = []

            # Add images first
            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    # Strip data URL prefix if present
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64_data
                    }
                })

            # Add text prompt after images
            content.append({"type": "text", "text": prompt})

            response = await self.client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", self.max_tokens or 4096)
            )
            return response.content[0].text
        except Exception as e:
            raise RuntimeError(f"Anthropic vision API failed for '{self.alias}': {e}") from e

class ZaiProvider(AIProvider):
    """Z.AI provider for GLM models (GLM-4.7, GLM-4.6, GLM-4.5, etc.)"""
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # API key handling (supports both plaintext and env var)
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "ZAI_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found for Z.AI provider '{self.alias}'")

        # Initialize Z.AI client (synchronous SDK confirmed from docs)
        from zai import ZaiClient
        self.client = ZaiClient(api_key=api_key)

    def supports_vision(self) -> bool:
        """GLM vision models: models with 'v' suffix (glm-4.6v-flash, glm-4.5v, glm-4.0v)"""
        # Vision models confirmed from rate limits page
        vision_models = ["glm-4.6v", "glm-4.5v", "glm-4.0v", "glm-4v"]
        return any(vm in self.model.lower() for vm in vision_models)

    async def generate_response(self, prompt: str) -> str:
        """Generate text response using Z.AI GLM model"""
        try:
            # Z.AI SDK is synchronous, wrap in asyncio.to_thread()
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"Z.AI provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Z.AI vision API for GLM-V models (glm-4.6v-flash, glm-4.5v, glm-4.0v)
        Assuming OpenAI-compatible multimodal format (needs verification)
        """
        try:
            # Build multimodal message content (OpenAI-compatible format)
            content = [{"type": "text", "text": prompt}]

            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_data}",
                        "detail": "high"
                    }
                })

            # Z.AI SDK is synchronous, wrap in asyncio.to_thread()
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 4000)
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"Z.AI vision API failed for '{self.alias}': {e}") from e


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
        self.model_name = config.get("model", "openai/whisper-large-v3")
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
        self._load_lock = None  # Will be set to asyncio.Lock when needed

        logger.info(f"LocalWhisperProvider '{alias}' initialized (model={self.model_name}, device={self.device})")

    def _detect_device(self) -> str:
        """
        Auto-detect the best available device.

        Priority: MLX > CUDA > MPS > CPU

        Returns:
            Device string: 'mlx', 'cuda', 'mps', or 'cpu'
        """
        import platform

        # 1. Check for Apple MLX (Apple Silicon - M1/M2/M3/M4)
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            try:
                import mlx.core as mx
                logger.info("MLX detected (Apple Silicon) - Using Apple GPU for local transcription")
                return "mlx"
            except ImportError:
                logger.debug("MLX not available (install with: poetry install -E mlx)")

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
        Model is cached in self.pipeline for subsequent transcriptions.
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
            else:
                device = self.device

            # MLX path (Apple Silicon)
            if device == "mlx":
                try:
                    import mlx_whisper
                    logger.info("Loading Whisper model with MLX (Apple Silicon optimization)...")

                    # mlx-whisper loads model on first transcribe() call
                    # Store device info for later use
                    self.pipeline = "mlx"  # Marker for MLX mode
                    self.model_loaded = True

                    elapsed = time.time() - start_time
                    logger.info(f"MLX Whisper initialized in {elapsed:.1f} seconds (lazy model loading)")
                    return

                except ImportError as mlx_error:
                    logger.warning(f"MLX not available: {mlx_error} - Falling back to PyTorch")
                    device = "cpu"  # Fallback to CPU if MLX not installed

            # PyTorch path (CUDA/MPS/CPU)
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

            # Model dtype (float16 for GPU, float32 for CPU)
            torch_dtype = torch.float16 if device in ("cuda", "mps") else torch.float32

            # Load model with optimizations
            model_kwargs = {}
            if device == "cuda":
                if self.use_flash_attention:
                    model_kwargs["attn_implementation"] = "flash_attention_2"
                else:
                    model_kwargs["attn_implementation"] = "sdpa"

            model = AutoModelForSpeechSeq2Seq.from_pretrained(
                self.model_name,
                torch_dtype=torch_dtype,
                low_cpu_mem_usage=True,
                use_safetensors=True,
                **model_kwargs
            )

            # Move model to device with CUDA fallback handling
            try:
                model.to(device)
            except RuntimeError as e:
                if "NVIDIA" in str(e) or "CUDA" in str(e):
                    # CUDA initialization failed (no GPU or driver), force CPU
                    logger.warning(f"Failed to initialize {device}: {e}")
                    logger.info("Forcing CPU mode for Whisper model")
                    device = "cpu"
                    torch_dtype = torch.float32  # CPU needs float32
                    model.to(device)
                else:
                    raise

            # Load processor
            processor = AutoProcessor.from_pretrained(self.model_name)

            # Create pipeline with chunking for long-form audio
            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                chunk_length_s=self.chunk_length_s,
                batch_size=self.batch_size,
                torch_dtype=torch_dtype,
                device=device,
                generate_kwargs={
                    "language": self.language if self.language != "auto" else None,
                    "task": self.task
                }
            )

            # Apply torch.compile for 4.5x speedup (PyTorch 2.4+, CUDA only)
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
        """Check if Whisper model is loaded in memory."""
        return self.model_loaded

    async def ensure_model_loaded(self) -> None:
        """
        Ensure Whisper model is loaded, loading it if necessary.

        This method is safe to call multiple times (idempotent) and uses
        a lock to prevent concurrent loading attempts.
        """
        if self.model_loaded:
            return  # Already loaded

        # Initialize lock if needed (lazy init for asyncio compatibility)
        if self._load_lock is None:
            import asyncio
            self._load_lock = asyncio.Lock()

        # Acquire lock and double-check loaded status
        async with self._load_lock:
            if self.model_loaded:
                return  # Another task loaded it while we waited

            # Load model in thread pool (blocking operation)
            import asyncio
            await asyncio.to_thread(self._load_model)

    async def transcribe(self, audio_path: str) -> Dict[str, Any]:
        """
        Transcribe audio file using local Whisper model.

        Args:
            audio_path: Path to audio file (webm, wav, mp3, etc.)

        Returns:
            Dict with keys:
                - text: Transcription text
                - language: Detected language code (e.g., 'en', 'es')
                - duration: Audio duration in seconds
                - provider: 'local_whisper'

        Raises:
            RuntimeError: If transcription fails
        """
        import asyncio

        # Initialize lock on first use
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        # Load model if not already loaded (lazy loading)
        if not self.model_loaded:
            async with self._load_lock:
                # Double-check after acquiring lock
                if not self.model_loaded:
                    # Run synchronous model loading in thread pool
                    await asyncio.to_thread(self._load_model)

        try:
            # MLX path (Apple Silicon)
            if self.pipeline == "mlx":
                import mlx_whisper
                import librosa

                logger.info(f"Transcribing audio with MLX Whisper: model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                # Load audio for duration calculation
                audio_array, sample_rate = librosa.load(audio_path, sr=16000)
                duration_seconds = len(audio_array) / sample_rate

                # Run MLX transcription in thread pool
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

                # Load audio file
                audio_array, sample_rate = librosa.load(audio_path, sr=16000)  # Whisper requires 16kHz

                # Calculate duration
                duration_seconds = len(audio_array) / sample_rate

                logger.info(f"Transcribing audio with local Whisper: {duration_seconds:.1f}s, model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                # Run transcription in thread pool (I/O + CPU bound)
                result = await asyncio.to_thread(
                    self.pipeline,
                    audio_array,
                    batch_size=self.batch_size
                )

                elapsed = asyncio.get_event_loop().time() - start_time
                text = result.get("text", "").strip()

                # Try to extract language from result
                detected_language = "unknown"
                if result.get("chunks") and len(result["chunks"]) > 0:
                    detected_language = result["chunks"][0].get("language", "unknown")

                logger.info(f"Local transcription completed in {elapsed:.1f}s ({duration_seconds/elapsed:.1f}x real-time): {len(text)} chars")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper"
                }

        except Exception as e:
            logger.error(f"Local transcription failed: {e}", exc_info=True)
            raise RuntimeError(f"Local Whisper transcription failed: {e}") from e

    async def generate_response(self, prompt: str) -> str:
        """Not implemented - LocalWhisperProvider only supports transcription."""
        raise NotImplementedError("LocalWhisperProvider does not support text generation")

    def supports_vision(self) -> bool:
        """LocalWhisperProvider does not support vision."""
        return False


# --- The Manager Class ---

PROVIDER_MAP = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "zai": ZaiProvider,
    "local_whisper": LocalWhisperProvider,  # v0.13.1+: Local Whisper transcription
}

# Default context window sizes for common models (in tokens)
MODEL_CONTEXT_WINDOWS = {
    # Ollama models
    "llama3.1:8b": 131072,  # 128K tokens
    "llama3.1:13b": 131072,
    "llama3.1:70b": 131072,
    "llama3.2:1b": 131072,
    "llama3.2:3b": 131072,
    "mistral:7b": 8192,
    "mixtral:8x7b": 32768,
    "qwen2.5:7b": 32768,
    "deepseek-coder-v2:16b": 131072,
    "codellama:7b": 16384,

    # Ollama vision models
    "qwen3-vl:2b": 262144,     # 256K tokens
    "qwen3-vl:4b": 262144,
    "qwen3-vl:8b": 262144,
    "qwen3-vl:30b": 262144,
    "qwen3-vl:32b": 262144,
    "llama3.2-vision:11b": 131072,  # 128K tokens
    "llama3.2-vision:90b": 131072,
    "ministral-3:3b": 262144,   # 256K tokens
    "ministral-3:8b": 262144,
    "ministral-3:14b": 262144,
    "llava:7b": 4096,
    "llava:13b": 4096,
    "llava:34b": 4096,

    # OpenAI models
    "gpt-4": 8192,
    "gpt-4-32k": 32768,
    "gpt-4-turbo": 128000,
    "gpt-4o": 128000,
    "gpt-3.5-turbo": 16384,
    "gpt-3.5-turbo-16k": 16384,

    # Anthropic models
    "claude-3-opus-20240229": 200000,
    "claude-3-sonnet-20240229": 200000,
    "claude-3-haiku-20240307": 200000,
    "claude-3-5-sonnet-20240620": 200000,
    "claude-sonnet-4-5-20250929": 200000,
    "claude-haiku-4-5": 200000,  # Claude Haiku 4.5 (shorthand model name)
    "claude-opus-4-5": 200000,   # Claude Opus 4.5 (shorthand model name)

    # Z.AI models (GLM series) - from docs.z.ai
    "glm-4.7": 128000,  # 128K tokens (estimated)
    "glm-4.6": 128000,  # 128K tokens (estimated)
    "glm-4.6v-flash": 128000,  # Vision model
    "glm-4.5": 128000,  # 128K tokens (estimated)
    "glm-4.5v": 128000,  # Vision model
    "glm-4.5-air": 128000,
    "glm-4.5-airx": 128000,
    "glm-4.5-flash": 128000,
    "glm-4-plus": 128000,
    "glm-4.0v": 128000,  # Vision model
    "glm-4-128-0414-128k": 131072,  # 128K explicit in name
    "autoglm-phone-multilingal": 32768,  # Conservative estimate

    # Default fallback
    "default": 4096
}

class LLMManager:
    """
    Manages all configured AI providers.
    """
    def __init__(self, config_path: Path = Path.home() / ".dpc" / "providers.json"):
        self.config_path = config_path
        self.providers: Dict[str, AIProvider] = {}
        self.default_provider: str | None = None
        self.vision_provider: str | None = None  # Vision-specific provider for auto-selection
        self.voice_provider: str | None = None  # v0.13.0+: Voice transcription provider for auto-selection

        # Token counting manager (Phase 4 refactor - v0.12.1)
        from dpc_client_core.managers.token_count_manager import TokenCountManager
        self.token_count_manager = TokenCountManager()

        self._load_providers_from_config()

    def _ensure_config_exists(self):
        """Creates a default providers.json file if one doesn't exist."""
        if not self.config_path.exists():
            logger.warning("Provider config file not found at %s", self.config_path)
            logger.info("Creating a default template with a local Ollama provider")

            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            default_config = {
                "_comment": "AI Provider Configuration - Manage your local and cloud AI providers",
                "default_provider": "ollama_text",
                "vision_provider": "ollama_vision",
                "voice_provider": "local_whisper_large",  # v0.13.0+: Local Whisper or OpenAI-compatible
                "providers": [
                    {
                        "alias": "ollama_text",
                        "type": "ollama",
                        "model": "llama3.1:8b",
                        "host": "http://127.0.0.1:11434",
                        "context_window": 16384,
                        "_note": "Fast text model for regular chat queries"
                    },
                    {
                        "alias": "ollama_vision",
                        "type": "ollama",
                        "model": "qwen3-vl:8b",
                        "host": "http://127.0.0.1:11434",
                        "context_window": 16384,
                        "_note": "Vision model for image analysis"
                    },
                    {
                        "alias": "local_whisper_large",
                        "type": "local_whisper",
                        "model": "openai/whisper-large-v3",
                        "device": "auto",
                        "compile_model": False,
                        "use_flash_attention": False,
                        "chunk_length_s": 30,
                        "batch_size": 16,
                        "language": "auto",
                        "task": "transcribe",
                        "lazy_loading": True,
                        "_note": "Local Whisper transcription - GPU accelerated (CUDA, MLX)"
                    }
                ],
                "_examples": {
                    "_comment": "Example configurations - uncomment and add to providers array above",
                    "ollama_vision_alternatives": [
                        {
                            "alias": "ollama_qwen_vision",
                            "type": "ollama",
                            "model": "qwen3-vl:8b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 262144,
                            "_note": "Qwen3-VL 8B - excellent vision model (256K context)"
                        },
                        {
                            "alias": "ollama_ministral_vision",
                            "type": "ollama",
                            "model": "ministral-3:8b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 262144,
                            "_note": "Ministral 3 8B - fast vision model (256K context)"
                        }
                    ],
                    "ollama_small_models": [
                        {
                            "alias": "ollama_small",
                            "type": "ollama",
                            "model": "llama3.2:3b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 131072,
                            "_note": "Small model for resource-constrained systems (~2GB RAM)"
                        },
                        {
                            "alias": "ollama_tiny",
                            "type": "ollama",
                            "model": "llama3.2:1b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 131072,
                            "_note": "Tiny model for embedded devices (~1GB RAM)"
                        }
                    ],
                    "lm_studio": {
                        "alias": "lm_studio",
                        "type": "openai_compatible",
                        "model": "lmstudio-community/Meta-Llama-3-8B-Instruct-GGUF",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "api_key": "lm-studio",
                        "_note": "Local LM Studio - OpenAI-compatible API"
                    },
                    "openai": {
                        "alias": "gpt4o",
                        "type": "openai_compatible",
                        "model": "gpt-4o",
                        "base_url": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "context_window": 128000,
                        "_note": "OpenAI GPT-4o - powerful vision-capable model",
                        "_setup": "Set environment variable: export OPENAI_API_KEY='sk-...'"
                    },
                    "anthropic": [
                        {
                            "alias": "claude_sonnet",
                            "type": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_key_env": "ANTHROPIC_API_KEY",
                            "context_window": 200000,
                            "_note": "Claude Sonnet 4.5 - most capable (vision-capable, 200K context)",
                            "_setup": "Set environment variable: export ANTHROPIC_API_KEY='sk-ant-...'"
                        },
                        {
                            "alias": "claude_haiku",
                            "type": "anthropic",
                            "model": "claude-haiku-4-5",
                            "api_key_env": "ANTHROPIC_API_KEY",
                            "context_window": 200000,
                            "_note": "Claude Haiku 4.5 - fast and affordable (vision-capable, 200K context)"
                        }
                    ]
                },
                "_instructions": {
                    "default_provider": "Provider used for all text-only queries (no images)",
                    "vision_provider": "Provider used for image analysis queries (screenshots, photos, diagrams)",
                    "voice_provider": "v0.13.0+: Provider used for voice transcription (local_whisper or OpenAI-compatible)",
                    "model_installation": {
                        "ollama": "Install models: ollama pull llama3.1:8b && ollama pull qwen3-vl:8b",
                        "alternative_vision": "Other vision models: ollama pull qwen3-vl:8b OR ollama pull ministral-3:8b",
                        "small_models": "For low RAM: ollama pull llama3.2:3b (2GB) OR ollama pull llama3.2:1b (1GB)"
                    },
                    "supported_types": "ollama (local, free), openai_compatible (GPT, LM Studio), anthropic (Claude)",
                    "vision_capable_models": {
                        "ollama": "llama3.2-vision, qwen3-vl, ministral-3, llava (all sizes)",
                        "openai": "gpt-4o, gpt-4-turbo, gpt-4o-mini",
                        "anthropic": "claude-3+, claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5"
                    },
                    "context_windows": {
                        "128K": "llama3.1, llama3.2-vision, gpt-4o (efficient for most use cases)",
                        "256K": "qwen3-vl, ministral-3 (excellent for long documents)",
                        "200K": "claude-3+, claude-4.5 (best for complex analysis)"
                    },
                    "vram_requirements": {
                        "1GB": "llama3.2:1b (tiny, embedded GPUs)",
                        "2GB": "llama3.2:3b (small, budget GPUs)",
                        "8GB": "llama3.1:8b, qwen3-vl:8b, ministral-3:8b (recommended - RTX 3060)",
                        "12GB": "llama3.1:13b (RTX 3060 12GB, RTX 4060 Ti)",
                        "16GB": "llama3.2-vision:11b (RTX 4060 Ti 16GB, RTX 4080)",
                        "24GB+": "llama3.1:70b, llama3.2-vision:90b (RTX 4090, A5000, professional)"
                    },
                    "api_key_setup": {
                        "linux_mac": "Add to ~/.bashrc: export OPENAI_API_KEY='sk-...' && export ANTHROPIC_API_KEY='sk-ant-...'",
                        "windows_cmd": "setx OPENAI_API_KEY \"sk-...\" && setx ANTHROPIC_API_KEY \"sk-ant-...\"",
                        "windows_powershell": "$env:OPENAI_API_KEY='sk-...'; [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', 'sk-...', 'User')"
                    }
                }
            }

            with open(self.config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info("Default provider config created at %s", self.config_path)

    def _load_providers_from_config(self):
        """Reads the config file and initializes all defined providers."""
        self._ensure_config_exists()
        logger.info("Loading AI providers from %s", self.config_path)
        if not self.config_path.exists():
            logger.warning("Provider config file not found at %s - no providers loaded", self.config_path)
            return

        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)

            self.default_provider = config.get("default_provider")
            self.vision_provider = config.get("vision_provider")  # Load vision provider for auto-selection
            self.voice_provider = config.get("voice_provider")  # v0.13.0+: Load voice provider for auto-selection

            for provider_config in config.get("providers", []):
                alias = provider_config.get("alias")
                provider_type = provider_config.get("type")

                if not alias or not provider_type:
                    logger.warning("Skipping invalid provider config: %s", provider_config)
                    continue

                if provider_type in PROVIDER_MAP:
                    provider_class = PROVIDER_MAP[provider_type]
                    try:
                        self.providers[alias] = provider_class(alias, provider_config)
                        logger.info("Successfully loaded provider '%s' of type '%s'", alias, provider_type)
                    except (ValueError, KeyError) as e:
                        logger.error("Error loading provider '%s': %s", alias, e)
                else:
                    logger.warning("Unknown provider type '%s' for alias '%s'", provider_type, alias)

            if self.default_provider and self.default_provider not in self.providers:
                logger.warning("Default provider '%s' not found in loaded providers", self.default_provider)
                self.default_provider = None

        except Exception as e:
            logger.error("Error parsing provider config file: %s", e, exc_info=True)

    def save_config(self, config_dict: Dict[str, Any]):
        """
        Save provider configuration to JSON file and reload providers.

        Args:
            config_dict: Dictionary containing providers configuration
        """
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config_dict, f, indent=2)
            logger.info("Provider configuration saved to %s", self.config_path)

            # Reload providers
            self.providers.clear()
            self._load_providers_from_config()
        except Exception as e:
            logger.error("Error saving provider config: %s", e, exc_info=True)
            raise

    def get_active_model_name(self) -> str:
        """
        Returns the name of the currently active AI model.
        
        Returns:
            String like "llama3.1:8b" or None if no model is loaded
        """
        # Use default_provider (not active_provider)
        if not self.default_provider:
            return None
        
        # Get the provider object (not a dict, but an AIProvider instance)
        provider = self.providers.get(self.default_provider)
        if not provider:
            return None
        
        # Get the model name from the provider object
        model = provider.model
        if not model:
            return None
        
        # Get provider type from config
        provider_type = provider.config.get('type', '')
        
        # Format based on provider type
        if provider_type == 'ollama':
            return model  # e.g., "llama3.1:8b"
        elif provider_type == 'openai_compatible':
            return f"OpenAI {model}"
        elif provider_type == 'anthropic':
            return f"Claude {model}"
        else:
            return model

    def find_provider_by_model(self, model_name: str) -> str | None:
        """
        Find a provider alias by model name.

        Args:
            model_name: The model name to search for (e.g., "claude-haiku-4-5")

        Returns:
            Provider alias if found, None otherwise
        """
        for alias, provider in self.providers.items():
            if provider.model == model_name:
                return alias
        return None

    def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for a given model.

        REFACTORED (Phase 4 - v0.12.1): Delegates to TokenCountManager
        for better separation of concerns and centralized token counting logic.

        Uses:
        - tiktoken for OpenAI/Anthropic (accurate BPE)
        - HuggingFace transformers for Ollama (accurate model-specific)
        - Character estimation fallback (4 chars ≈ 1 token)

        Args:
            text: The text to count tokens for
            model: The model name (e.g., "gpt-4", "llama3.1:8b")

        Returns:
            Token count
        """
        return self.token_count_manager.count_tokens(text, model)

    def get_context_window(self, model: str) -> int:
        """
        Get the context window size for a given model.

        Priority:
        1. Check provider config (providers.toml) for context_window field
        2. Check hardcoded MODEL_CONTEXT_WINDOWS dict
        3. Return default if not found

        Args:
            model: The model name (e.g., "gpt-4", "llama3.1:8b")

        Returns:
            Context window size in tokens
        """
        # Phase 6: Check provider config first (providers.toml can override)
        for alias, provider in self.providers.items():
            if provider.model == model:
                # Check if provider config has context_window field
                context_window_config = provider.config.get('context_window')
                if context_window_config:
                    try:
                        return int(context_window_config)
                    except (ValueError, TypeError):
                        logger.warning("Invalid context_window value in provider '%s' config: %s",
                                     alias, context_window_config)

        # Check direct match in hardcoded defaults
        if model in MODEL_CONTEXT_WINDOWS:
            return MODEL_CONTEXT_WINDOWS[model]

        # Check for partial matches (e.g., "gpt-4" matches "gpt-4-0613")
        for known_model, window_size in MODEL_CONTEXT_WINDOWS.items():
            if known_model in model or model in known_model:
                return window_size

        # Return default
        logger.warning("Context window size unknown for model '%s' - using default: %d",
                      model, MODEL_CONTEXT_WINDOWS['default'])
        return MODEL_CONTEXT_WINDOWS["default"]

    async def query(self, prompt: str, provider_alias: str | None = None, return_metadata: bool = False,
                    images: Optional[List[Dict[str, Any]]] = None, **kwargs):
        """
        Routes a query to the specified provider, or auto-selects based on query type.

        Auto-selection logic (when provider_alias is None):
        - If images present and vision_provider configured → use vision_provider
        - If images present and no vision_provider → find first vision-capable provider
        - If no images → use default_provider

        Args:
            prompt: The prompt to send to the LLM
            provider_alias: Optional provider alias to use (overrides auto-selection)
            return_metadata: If True, returns dict with 'response', 'provider', 'model', 'tokens_used', 'model_max_tokens'. If False, returns just the response string.
            images: Optional list of image dicts for vision API (multimodal queries). Each dict should contain:
                - path: str (absolute path to image file)
                - mime_type: str (e.g., "image/png")
                - base64: str (optional, if already encoded)
            **kwargs: Additional parameters passed to vision API (temperature, max_tokens, etc.)

        Returns:
            str if return_metadata=False, dict if return_metadata=True
        """
        # Auto-select provider based on query type
        if provider_alias is None:
            if images:
                # Vision query: prefer vision_provider, fallback to first vision-capable
                if self.vision_provider and self.vision_provider in self.providers:
                    alias_to_use = self.vision_provider
                    logger.info("Auto-selected vision provider '%s' for image query", alias_to_use)
                else:
                    # Find first vision-capable provider
                    alias_to_use = None
                    for alias, provider in self.providers.items():
                        if provider.supports_vision():
                            alias_to_use = alias
                            logger.info("Auto-selected vision-capable provider '%s' (no vision_provider configured)", alias_to_use)
                            break

                    if not alias_to_use:
                        raise ValueError("No vision-capable provider found. Please configure a vision_provider or add a vision-capable model.")
            else:
                # Text-only query: use default provider
                alias_to_use = self.default_provider
        else:
            # Explicit provider specified
            alias_to_use = provider_alias

        if not alias_to_use:
            raise ValueError("No provider specified and no default provider is set.")

        if alias_to_use not in self.providers:
            raise ValueError(f"Provider '{alias_to_use}' is not configured or failed to load.")

        provider = self.providers[alias_to_use]

        # Check if vision is requested but provider doesn't support it
        if images:
            if not provider.supports_vision():
                raise ValueError(f"Provider '{alias_to_use}' (model: {provider.model}) does not support vision API. "
                               f"Use a vision-capable model like gpt-4o, gpt-4-turbo, or claude-3+.")
            logger.info("Routing vision query to provider '%s' with model '%s' (%d images)",
                       alias_to_use, provider.model, len(images))
            response = await provider.generate_with_vision(prompt, images, **kwargs)
        else:
            logger.info("Routing query to provider '%s' with model '%s'", alias_to_use, provider.model)
            response = await provider.generate_response(prompt)

        if return_metadata:
            # Count tokens in prompt and response
            prompt_tokens = self.count_tokens(prompt, provider.model)
            response_tokens = self.count_tokens(response, provider.model)
            total_tokens = prompt_tokens + response_tokens

            # Get model's context window
            context_window = self.get_context_window(provider.model)

            return {
                "response": response,
                "provider": alias_to_use,
                "model": provider.model,
                "tokens_used": total_tokens,
                "prompt_tokens": prompt_tokens,
                "response_tokens": response_tokens,
                "model_max_tokens": context_window,
                "vision_used": bool(images)  # NEW: Indicate if vision API was used
            }
        return response

# --- Self-testing block ---
async def main_test():
    logger.info("--- Testing LLMManager ---")

    # Create a dummy providers.json for testing
    dummy_config = {
        "default_provider": "local_ollama",
        "providers": [
            {
                "alias": "local_ollama",
                "type": "ollama",
                "model": "llama3.1:8b",
                "host": "http://127.0.0.1:11434"
            }
        ]
    }

    dpc_dir = Path.home() / ".dpc"
    dpc_dir.mkdir(exist_ok=True)
    test_config_path = dpc_dir / "providers.json"
    with open(test_config_path, 'w') as f:
        json.dump(dummy_config, f, indent=2)

    try:
        manager = LLMManager(config_path=test_config_path)

        if not manager.providers:
            logger.warning("No providers were loaded - cannot run test query")
            return

        logger.info("Testing query with default provider")
        response = await manager.query("What is the capital of France?")
        logger.info("Response: %s", response)

        logger.info("Testing query with specified provider")
        response = await manager.query("What is the capital of Germany?", provider_alias="local_ollama")
        logger.info("Response: %s", response)

    except Exception as e:
        logger.error("An error occurred during testing: %s", e, exc_info=True)
    finally:
        # Clean up the dummy config
        if test_config_path.exists():
            test_config_path.unlink()
        logger.info("--- Test finished ---")

if __name__ == '__main__':
    # To run this test:
    # 1. Make sure Ollama is running.
    # 2. Navigate to `dpc-client/core/`
    # 3. Run: `poetry run python dpc_client_core/llm_manager.py`
    asyncio.run(main_test())