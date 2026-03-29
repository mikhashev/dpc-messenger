# dpc_client_core/providers/openai_provider.py

import os
import base64
import logging
from typing import Dict, Any, List

from openai import AsyncOpenAI

from .base import AIProvider, OPENAI_THINKING_MODELS

logger = logging.getLogger(__name__)


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

    def supports_thinking(self) -> bool:
        """Check if this is an OpenAI reasoning model (o1/o3 series)."""
        return any(tm in self.model.lower() for tm in OPENAI_THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            # Use kwargs override or config default for temperature
            temperature = kwargs.get("temperature", self.temperature)
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
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
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", 4000)
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI vision API failed for '{self.alias}': {e}") from e

    async def close(self) -> None:
        """Close the AsyncOpenAI client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"OpenAICompatibleProvider '{self.alias}': Client closed")
