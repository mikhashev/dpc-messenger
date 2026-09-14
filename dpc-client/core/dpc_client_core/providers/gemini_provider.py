# dpc_client_core/providers/gemini_provider.py

import os
import asyncio
import base64
import logging
from typing import Dict, Any, List

from .base import AIProvider, image_base64

logger = logging.getLogger(__name__)


class GeminiProvider(AIProvider):
    """
    Google Gemini provider using the google-genai SDK.

    Supports all Gemini models (gemini-2.0-flash, gemini-1.5-pro, etc.)
    with native vision (all models are multimodal) and thinking support
    for gemini-2.0-flash-thinking-exp.

    Auth: GEMINI_API_KEY environment variable (Google AI Studio key).

    Output count convention (`excludes`), from the UsageMetadata definitions at
    https://ai.google.dev/api/generate-content: `candidatesTokenCount` is "Total
    number of tokens across all the generated response candidates",
    `thoughtsTokenCount` is "Number of tokens of thoughts for thinking models",
    and `totalTokenCount` is "Total token count for the generation request
    (prompt + thoughts + response candidates)" — three addends, so the thoughts
    are beside the candidates and not inside them. The thinking page says the
    same about the bill: "When thinking is turned on, response pricing is the sum
    of output tokens and thinking tokens"
    (https://ai.google.dev/gemini-api/docs/thinking).
    """

    DECLARED_OUTPUT_INCLUDES_THINKING = "excludes"

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

    def _usage_from(self, response: Any) -> Dict[str, Any]:
        """The vendor's token accounting for one response, in this project's shape.

        `completion_tokens` is the candidates count alone, which under this
        vendor's convention is the answer without its thoughts; a tariff that
        charges the output adds `reasoning_tokens` to it rather than trusting it
        to be the whole. `total_tokens` is the vendor's own sum of the three.
        """
        u = getattr(response, "usage_metadata", None)
        if u is None:
            return {}
        prompt = getattr(u, "prompt_token_count", 0) or 0
        candidates = getattr(u, "candidates_token_count", 0) or 0
        thoughts = getattr(u, "thoughts_token_count", 0) or 0
        cached = getattr(u, "cached_content_token_count", 0) or 0
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": candidates,
            "reasoning_tokens": thoughts,
            "content_tokens": candidates,
            "total_tokens": getattr(u, "total_token_count", 0) or (prompt + candidates + thoughts),
            "cache_read_input_tokens": cached,
            "output_includes_thinking": self.DECLARED_OUTPUT_INCLUDES_THINKING,
        }
        if thoughts:
            usage["thinking_source"] = "engine"
        return usage

    def _log_usage(self, usage: Dict[str, Any], path: str) -> Dict[str, Any]:
        """Record and announce what the call just cost, and hand the dict back.

        Takes the dict rather than the response, because on the streaming path
        the totals belong to the last chunk that carried them, not to one object.
        """
        self._record_last_usage(usage)
        if usage:
            logger.info(
                "Gemini usage: alias=%s model=%s prompt=%d (cache_read=%d), "
                "candidates=%d, thoughts=%d, path=%s",
                self.alias, self.model, usage["prompt_tokens"],
                usage["cache_read_input_tokens"], usage["completion_tokens"],
                usage["reasoning_tokens"], path,
            )
        return usage

    def supports_vision(self) -> bool:
        return True  # All Gemini models are natively multimodal

    def supports_thinking(self) -> bool:
        return any(m in self.model for m in self.THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        # Cleared before the call, so a failure leaves no previous call's counts
        # for `get_last_usage()` to hand out as this one's.
        self._record_last_usage(None)
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                )
            )
            self._log_usage(self._usage_from(response), "plain")
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
        reasoning_effort: str = None,
    ) -> str:
        # Accepted so every provider answers the same call, and not forwarded:
        # whether this API has an equivalent has not been checked. Named rather
        # than swallowed by **kwargs, so the next reader sees it is a decision.
        self._record_last_usage(None)
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
            last_usage: Dict[str, Any] = {}
            for chunk in chunks:
                text = chunk.text or ""
                full_text += text
                # Each chunk restates the running totals; the last one that
                # carries them is the whole call.
                chunk_usage = self._usage_from(chunk)
                if chunk_usage:
                    last_usage = chunk_usage
                if on_chunk and text:
                    await on_chunk(text, conversation_id)
            self._log_usage(last_usage, "plain-stream")
            return full_text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' streaming failed: {e}") from e

    async def generate_with_vision(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        **kwargs,
    ) -> str:
        self._record_last_usage(None)
        # `Part.from_bytes` is typed `data: bytes` and the SDK offers no
        # from-base64 constructor, so the guard's base64 is decoded here. Not
        # left undecoded: `Blob` does accept base64 text, through pydantic's
        # val_json_bytes rather than the signature, and refuses a bad one in
        # pydantic's words instead of ours.
        parts = []
        for img in images:
            parts.append(
                self._types.Part.from_bytes(
                    data=base64.b64decode(image_base64(img, self.alias)),
                    mime_type=img.get("mime_type", "image/jpeg"),
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
            self._log_usage(self._usage_from(response), "vision")
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' vision failed: {e}") from e
