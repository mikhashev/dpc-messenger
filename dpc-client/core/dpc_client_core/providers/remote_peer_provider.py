# dpc_client_core/providers/remote_peer_provider.py

import asyncio
import logging
from typing import Any, Dict, List, TYPE_CHECKING

from .base import AIProvider

if TYPE_CHECKING:
    from dpc_client_core.service import CoreService

logger = logging.getLogger(__name__)


class RemotePeerProvider(AIProvider):
    """
    Remote peer inference provider.

    This provider enables using a remote peer's AI model for inference,
    allowing users to leverage more powerful models on other machines
    or share compute resources within trusted peer groups.

    Configuration example (~/.dpc/providers.json):
    {
        "alias": "remote_alice_llama70b",
        "type": "remote_peer",
        "peer_id": "dpc-node-alice-123",
        "model": "llama3:70b",
        "provider": "ollama_text",  // Optional: remote provider alias
        "timeout": 60,
        "context_window": 131072
    }

    Security: Remote inference requests are controlled by the firewall
    in privacy_rules.json under the 'compute' section. The remote peer
    must have compute sharing enabled and the requester whitelisted.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Remote peer configuration
        self.peer_id = config.get("peer_id")
        self.model = config.get("model")  # Model to request on remote peer
        self.remote_provider = config.get("provider")  # Optional: specific provider on remote
        # 1200 s: the host's own DEFAULT_TIMEOUT_SECONDS (900) plus overhead.
        # At 60 s this provider abandoned nearly every real generation while
        # the host kept producing tokens for a caller that had left.
        self.timeout = config.get("timeout", 1200.0)

        if not self.peer_id:
            raise ValueError(f"RemotePeerProvider '{alias}' requires 'peer_id' in config")

        # CoreService reference (injected by LLMManager)
        self._service = None

        logger.info(f"RemotePeerProvider '{alias}' initialized (peer={self.peer_id}, "
                   f"model={self.model}, timeout={self.timeout}s)")

    def set_service(self, service: "CoreService") -> None:
        """
        Inject CoreService reference for remote inference.

        Called by CoreService during initialization.
        """
        self._service = service
        logger.debug(f"RemotePeerProvider '{self.alias}': CoreService injected")

    async def generate_response(
        self,
        prompt: str,
        conversation_id: str = None,
        images: list = None,
        **kwargs
    ) -> str:
        """
        Generate response using remote peer's AI model.

        Args:
            prompt: The prompt to send to the remote peer
            conversation_id: Optional conversation ID (not used for remote)
            images: Optional list of images for vision queries
            **kwargs: Additional arguments (ignored for remote)

        Returns:
            Response text from the remote peer's model

        Raises:
            RuntimeError: If CoreService not injected or remote inference fails
        """
        if not self._service:
            raise RuntimeError(f"RemotePeerProvider '{self.alias}': CoreService not injected")

        logger.info(f"RemotePeerProvider '{self.alias}': Requesting inference from peer {self.peer_id}")

        try:
            # Use CoreService's remote inference method
            response = await self._service._request_inference_from_peer(
                peer_id=self.peer_id,
                prompt=prompt,
                model=self.model,
                provider=self.remote_provider,
                images=images,
                timeout=self.timeout
            )
            return response
        except asyncio.TimeoutError:
            raise RuntimeError(f"Remote inference to peer {self.peer_id} timed out after {self.timeout}s")
        except Exception as e:
            logger.error(f"RemotePeerProvider '{self.alias}': Remote inference failed: {e}")
            raise RuntimeError(f"Remote inference failed: {e}")

    async def generate_with_vision(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        **kwargs
    ) -> str:
        """Vision entry point for llm_manager; the DPTP request carries images."""
        return await self.generate_response(prompt, images=images, **kwargs)

    def supports_vision(self) -> bool:
        """Whether the peer's own provider row for this alias claims vision.

        Unknown is answered as no: llm_manager refuses a no with a message
        naming the provider, while a yes it cannot honour becomes a swallowed
        failure that reaches the model as though it were an analysis.
        """
        if not self._service:
            logger.debug(
                f"RemotePeerProvider '{self.alias}': no vision claim: CoreService not injected"
            )
            return False

        if not self.remote_provider:
            logger.debug(
                f"RemotePeerProvider '{self.alias}': no vision claim: "
                f"config names no provider alias on peer {self.peer_id}"
            )
            return False

        peer_providers = getattr(self._service, "peer_metadata", {}).get(
            self.peer_id, {}
        ).get("providers", [])
        for p in peer_providers:
            if p.get("alias") == self.remote_provider:
                return bool(p.get("supports_vision"))

        logger.debug(
            f"RemotePeerProvider '{self.alias}': no vision claim: provider "
            f"'{self.remote_provider}' not among the {len(peer_providers)} "
            f"advertised by peer {self.peer_id}"
        )
        return False
