# dpc_client_core/providers/gemini_provider.py

import os
import asyncio
import logging
from typing import Dict, Any, List

from .base import AIProvider

logger = logging.getLogger(__name__)


class GeminiProvider(AIProvider):
    """
    Google Gemini provider using the google-genai SDK.

    Supports all Gemini models (gemini-2.0-flash, gemini-1.5-pro, etc.)
    with native vision (all models are multimodal) and thinking support
    for gemini-2.0-flash-thinking-exp.

    Auth: GEMINI_API_KEY environment variable (Google AI Studio key).
    """

    THINKING_MODELS = ["gemini-2.0-flash-thinking-exp"]

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise RuntimeError(
                f"GeminiProvider '{alias}': Install google-genai — "
                "run: poetry add google-genai"
            )
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(
                f"GeminiProvider '{alias}': No API key found. "
                "Set GEMINI_API_KEY or specify api_key_env in config."
            )
        self._genai = genai
        self._types = genai_types
        self.client = genai.Client(api_key=api_key)
        logger.info(f"GeminiProvider '{alias}': Initialized with model '{self.model}'")

    def supports_vision(self) -> bool:
        return True  # All Gemini models are natively multimodal

    def supports_thinking(self) -> bool:
        return any(m in self.model for m in self.THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                )
            )
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(
                None,
                lambda: list(self.client.models.generate_content_stream(
                    model=self.model,
                    contents=prompt,
                ))
            )
            full_text = ""
            for chunk in chunks:
                text = chunk.text or ""
                full_text += text
                if on_chunk and text:
                    await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' streaming failed: {e}") from e

    async def generate_with_vision(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        **kwargs,
    ) -> str:
        parts = []
        for img in images:
            parts.append(
                self._types.Part.from_bytes(
                    data=img["data"],
                    mime_type=img.get("media_type", "image/jpeg"),
                )
            )
        parts.append(prompt)
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=parts,
                )
            )
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' vision failed: {e}") from e
