"""
Inference Orchestrator - Coordinates AI inference execution (local and remote).

Extracted from service.py as part of Pre-Phase 2 refactoring (Priority 2).
This coordinator handles the core inference logic while CoreService retains
orchestration of contexts, conversation history, and token tracking.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class InferenceOrchestrator:
    """Coordinates local and remote AI inference execution."""

    def __init__(self, service):
        """
        Initialize InferenceOrchestrator with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, etc.)
        """
        self.service = service
        self.llm_manager = service.llm_manager
        self.p2p_manager = service.p2p_manager

    async def execute_inference(
        self,
        prompt: str,
        compute_host: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        images: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Execute AI inference (local or remote).

        Args:
            prompt: The prompt to send to the AI
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use
            images: Optional list of image dicts for vision queries (Phase 2: Remote Vision)

        Returns:
            Dict with 'response', 'model', 'provider', 'compute_host' keys
            and optionally 'tokens_used', 'model_max_tokens', etc.

        Raises:
            ValueError: If compute_host is specified but peer is not connected
            RuntimeError: If inference fails
        """
        logger.info("Inference - compute_host: %s, model: %s, images: %s",
                   compute_host or 'local', model or 'default', 'yes' if images else 'no')

        if compute_host:
            return await self._execute_remote_inference(
                compute_host=compute_host,
                prompt=prompt,
                model=model,
                provider=provider,
                images=images
            )
        else:
            return await self._execute_local_inference(
                prompt=prompt,
                provider=provider,
                images=images
            )

    async def _execute_local_inference(
        self,
        prompt: str,
        provider: Optional[str] = None,
        images: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Execute local inference using llm_manager.

        Args:
            prompt: The prompt to send to the AI
            provider: Optional provider alias to use
            images: Optional list of image dicts for vision queries (Phase 2: Remote Vision)

        Returns:
            Dict with 'response', 'model', 'provider', 'compute_host', and token metadata

        Raises:
            RuntimeError: If local inference fails
        """
        try:
            result = await self.llm_manager.query(
                prompt,
                provider_alias=provider,
                images=images,
                return_metadata=True
            )
            result['compute_host'] = 'local'
            return result
        except Exception as e:
            logger.error("Local inference failed: %s", e, exc_info=True)
            raise RuntimeError(f"Local inference failed: {e}") from e

    async def _execute_remote_inference(
        self,
        compute_host: str,
        prompt: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        images: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Execute remote inference on peer node.

        Args:
            compute_host: Node ID of peer to use for inference
            prompt: The prompt to send to the AI
            model: Optional model name to use
            provider: Optional provider alias to use
            images: Optional list of image dicts for vision queries (Phase 2: Remote Vision)

        Returns:
            Dict with 'response', 'model', 'provider', 'compute_host', and token metadata

        Raises:
            ValueError: If compute_host is not connected
            RuntimeError: If remote inference fails
        """
        try:
            result_data = await self.service._request_inference_from_peer(
                peer_id=compute_host,
                prompt=prompt,
                model=model,
                provider=provider,
                images=images
            )

            # result_data is a dict with response and token metadata
            return {
                "response": result_data.get("response") if isinstance(result_data, dict) else result_data,
                "model": model or provider or "unknown",  # Use provider alias as model name if model not specified
                "provider": provider or "unknown",
                "compute_host": compute_host,
                # Include token metadata if available
                "tokens_used": result_data.get("tokens_used") if isinstance(result_data, dict) else None,
                "model_max_tokens": result_data.get("model_max_tokens") if isinstance(result_data, dict) else None,
                "prompt_tokens": result_data.get("prompt_tokens") if isinstance(result_data, dict) else None,
                "response_tokens": result_data.get("response_tokens") if isinstance(result_data, dict) else None
            }
        except ConnectionError as e:
            raise ValueError(f"Compute host {compute_host} is not connected") from e
        except Exception as e:
            logger.error("Remote inference failed: %s", e, exc_info=True)
            raise RuntimeError(f"Remote inference failed: {e}") from e

    def assemble_prompt(
        self,
        contexts: Dict[str, Any],
        clean_prompt: str,
        device_context: Optional[Dict] = None,
        peer_device_contexts: Optional[Dict] = None,
        message_history: Optional[list] = None,
        include_full_context: bool = True
    ) -> str:
        """
        Assemble the final prompt for the LLM with instruction processing.

        Phase 2: Incorporates InstructionBlock and bias mitigation from PCM v2.0
        Phase 7: Supports conversation history and context optimization

        Args:
            contexts: Dict mapping node_id -> PersonalContext (e.g., {'local': context_obj})
            clean_prompt: The raw user query
            device_context: Optional local device context dict
            peer_device_contexts: Optional dict mapping node_id -> device context
            message_history: Optional list of previous conversation messages
            include_full_context: If True, include full contexts; otherwise, history only

        Returns:
            Final assembled prompt string with contexts and history
        """
        # Delegate to CoreService's _assemble_final_prompt for now
        # (This method is complex and depends on PersonalContext structure)
        return self.service._assemble_final_prompt(
            contexts=contexts,
            clean_prompt=clean_prompt,
            device_context=device_context,
            peer_device_contexts=peer_device_contexts,
            message_history=message_history,
            include_full_context=include_full_context
        )
