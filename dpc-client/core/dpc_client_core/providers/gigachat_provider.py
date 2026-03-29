# dpc_client_core/providers/gigachat_provider.py

import logging
from typing import Dict, Any

from .base import AIProvider

logger = logging.getLogger(__name__)


class GigaChatProvider(AIProvider):
    """
    GigaChat provider by Sberbank using the official gigachat SDK.

    Supports GigaChat-2-Pro, GigaChat-2-Max (vision), GigaChat-2-Lite.
    All models have 128K context window.

    SSL Certificate: Sberbank requires the Russian НУЦ Минцифры CA cert.
    Install once into the virtualenv's certifi bundle:
        curl -k "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" -w "\\n" >> $(python -m certifi)
    Or set ca_bundle_file in provider config to the cert path.

    Auth: GIGACHAT_CREDENTIALS environment variable (authorization key
    from https://developers.sber.ru/studio).

    Scope values:
        GIGACHAT_API_PERS  — personal/individual (free tier, default)
        GIGACHAT_API_B2B   — business
        GIGACHAT_API_CORP  — corporate
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from gigachat import GigaChat
            self._GigaChat = GigaChat
        except ImportError:
            raise RuntimeError(
                f"GigaChatProvider '{alias}': Install gigachat — "
                "run: poetry add gigachat"
            )
        credentials = config.get("api_key")
        if not credentials:
            import os
            credentials = os.getenv(config.get("api_key_env", "GIGACHAT_CREDENTIALS"))
        if not credentials:
            raise ValueError(
                f"GigaChatProvider '{alias}': No credentials found. "
                "Set GIGACHAT_CREDENTIALS or specify api_key_env in config."
            )
        self._client_kwargs: Dict[str, Any] = dict(
            credentials=credentials,
            scope=config.get("scope", "GIGACHAT_API_PERS"),
            model=self.model,
            verify_ssl_certs=config.get("verify_ssl", True),
        )
        ca_bundle = config.get("ca_bundle_file", "")
        if ca_bundle:
            self._client_kwargs["ca_bundle_file"] = ca_bundle
        logger.info(
            f"GigaChatProvider '{alias}': Initialized with model '{self.model}', "
            f"scope={self._client_kwargs['scope']}"
        )

    def supports_vision(self) -> bool:
        return "Max" in self.model  # Only GigaChat-2-Max supports vision

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            async with self._GigaChat(**self._client_kwargs) as client:
                response = await client.achat(prompt)
                return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"GigaChatProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        try:
            full_text = ""
            async with self._GigaChat(**self._client_kwargs) as client:
                async for chunk in client.astream(prompt):
                    text = chunk.choices[0].delta.content or ""
                    full_text += text
                    if on_chunk and text:
                        await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(
                f"GigaChatProvider '{self.alias}' streaming failed: {e}"
            ) from e

    async def close(self) -> None:
        pass  # Context manager handles cleanup per-request
