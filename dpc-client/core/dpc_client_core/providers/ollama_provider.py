# dpc_client_core/providers/ollama_provider.py

import asyncio
import logging
from typing import Dict, Any, Optional, List

import ollama

from .base import AIProvider

logger = logging.getLogger(__name__)

# Vision-capable Ollama models (for auto-detection)
OLLAMA_VISION_MODELS = [
    "qwen3-vl",         # Qwen3-VL (all variants: 2b, 4b, 8b, 30b, etc.)
    "llava",            # LLaVA variants
    "llama3.2-vision",  # Llama 3.2 vision models
    "ministral-3",      # Ministral 3 vision models (3b, 8b, 14b)
    "bakllava",         # BakLLaVA
    "moondream",        # Moondream
]

# Thinking/reasoning models (for auto-detection)
# These models perform extended reasoning before producing their final response
OLLAMA_THINKING_MODELS = [
    "deepseek-r1",      # DeepSeek R1 (all variants)
    "deepseek-reasoner",
]


class OllamaProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        self.client = ollama.AsyncClient(host=config.get("host"))

    def supports_vision(self) -> bool:
        """Check if this Ollama model supports vision/multimodal inputs."""
        return any(vm in self.model.lower() for vm in OLLAMA_VISION_MODELS)

    def supports_thinking(self) -> bool:
        """Check if this Ollama model is a thinking/reasoning model."""
        return any(tm in self.model.lower() for tm in OLLAMA_THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            message = {'role': 'user', 'content': prompt}

            # Build options dict for custom parameters
            options = {}
            if self.config.get("context_window"):
                options["num_ctx"] = self.config["context_window"]

            # Add temperature if specified (use kwargs override or config default)
            temp = kwargs.get("temperature", self.temperature)
            if temp != 0.7:  # Only pass if non-default to avoid unnecessary API params
                options["temperature"] = temp

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

    async def close(self) -> None:
        """Close the Ollama async client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"OllamaProvider '{self.alias}': Client closed")
