# dpc_client_core/providers/github_models_provider.py

import logging
from typing import Dict, Any

from .base import AIProvider

logger = logging.getLogger(__name__)


class GitHubModelsProvider(AIProvider):
    """
    A retired provider kept for one release so that an existing alias of type
    `github_models` loads instead of taking the backend down with it.

    GitHub retired GitHub Models, the inference API included, on 2026-07-30
    (https://docs.github.com/en/github-models). The type is no longer offered
    in the provider editor and the class makes no request: it does not even
    need the `azure-ai-inference` SDK or a token any more. Each call raises
    `RETIRED_MESSAGE`, so the user finds out what to do at the moment the alias
    is used. Construction does not raise because `LLMManager` loads providers
    in one pass and a failure there is not isolated to the alias: a
    `RuntimeError` aborts the loading of every provider after it.

    The output count convention stays `unknown`: the page above documents no
    usage object, and no call is made.
    """

    RETIRED_MESSAGE = (
        "GitHub Models was retired by GitHub on 2026-07-30 (including the inference "
        "API), so provider alias '{alias}' of type github_models can no longer answer. "
        "Remove this alias from the provider settings and use another provider."
    )

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        logger.warning("%s", self._retired())

    def _retired(self) -> str:
        return self.RETIRED_MESSAGE.format(alias=self.alias)

    def supports_vision(self) -> bool:
        return False

    async def generate_response(self, prompt: str, **kwargs) -> str:
        raise RuntimeError(self._retired())

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
        reasoning_effort: str = None,
    ) -> str:
        raise RuntimeError(self._retired())

    async def close(self) -> None:
        pass  # holds no client
