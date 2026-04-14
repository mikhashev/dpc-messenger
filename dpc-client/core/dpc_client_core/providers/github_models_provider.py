# dpc_client_core/providers/github_models_provider.py

import os
import asyncio
import logging
from typing import Dict, Any, List

from .base import AIProvider

logger = logging.getLogger(__name__)


class GitHubModelsProvider(AIProvider):
    """
    GitHub Models provider using the azure-ai-inference SDK.

    Provides access to GPT-4o, Llama, Phi, Mistral and other models
    hosted at https://models.inference.ai.azure.com using a GitHub
    Personal Access Token (models:read permission required).

    Free tier: 15 RPM / 150 RPD (low-complexity models),
               10 RPM / 50 RPD (high-complexity models).

    Auth: GITHUB_TOKEN environment variable.
    """

    ENDPOINT = "https://models.inference.ai.azure.com"
    VISION_MODELS = ["gpt-4o", "llama-3.2-11b-vision"]

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential
            self._UserMessage = None  # lazy import in methods
        except ImportError:
            raise RuntimeError(
                f"GitHubModelsProvider '{alias}': Install azure-ai-inference — "
                "run: poetry add azure-ai-inference"
            )
        token = config.get("api_key")
        if not token:
            token = os.getenv(config.get("api_key_env", "GITHUB_TOKEN"))
        if not token:
            raise ValueError(
                f"GitHubModelsProvider '{alias}': No token found. "
                "Set GITHUB_TOKEN or specify api_key_env in config."
            )
        self.client = ChatCompletionsClient(
            endpoint=self.ENDPOINT,
            credential=AzureKeyCredential(token),
        )
        logger.info(
            f"GitHubModelsProvider '{alias}': Initialized with model '{self.model}' "
            f"at {self.ENDPOINT}"
        )

    def supports_vision(self) -> bool:
        return any(m in self.model for m in self.VISION_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        from azure.ai.inference.models import UserMessage
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.complete(
                    messages=[UserMessage(prompt)],
                    model=self.model,
                    temperature=self.temperature,
                )
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"GitHubModelsProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        from azure.ai.inference.models import UserMessage

        def _stream():
            result = []
            with self.client.complete(
                messages=[UserMessage(prompt)],
                model=self.model,
                temperature=self.temperature,
                stream=True,
            ) as stream:
                for update in stream:
                    if update.choices and update.choices[0].delta:
                        result.append(update.choices[0].delta.content or "")
            return result

        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(None, _stream)
            full_text = ""
            for text in chunks:
                full_text += text
                if on_chunk and text:
                    await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(
                f"GitHubModelsProvider '{self.alias}' streaming failed: {e}"
            ) from e

    async def close(self) -> None:
        self.client.close()
        logger.debug(f"GitHubModelsProvider '{self.alias}': Client closed")
