"""
P2P Coordinator - Coordinates P2P connection lifecycle and request handling.

Extracted from service.py as part of Pre-Phase 2 refactoring (Priority 2).
This coordinator provides a clean API layer between CoreService and P2PManager.
Expanded in Phase C Step 5 with incoming P2P request handlers.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import websockets

from .firewall import SERVING_LOCAL_KEY, AppliedTariff, onward_sharing_refusal
from .node_ledger import NodeLedger, default_ledger, tariff_amount_for, usage_row

logger = logging.getLogger(__name__)


class EffortRefused(ValueError):
    """A guest asked this node to think in a word its serving alias has no rung
    for. Its message lists the words the alias does know — the ones its menu row
    advertises — and reaches the guest as the inference error response, before
    any inference has run and before any usage row exists.
    """


class P2PCoordinator:
    """Coordinates P2P connection lifecycle, messaging, and request handling."""

    def __init__(self, service, ledger: Optional[NodeLedger] = None):
        """
        Initialize P2PCoordinator with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, etc.)
            ledger: Where a served peer call's usage row goes; default is the
                node's own ledger
        """
        self.service = service
        self.p2p_manager = service.p2p_manager
        self.hub_client = service.hub_client
        self._ledger = ledger
        # One peer generation at a time on the shared alias. The full queue with
        # priorities and a remote-share cap is D4-β of ADR-040; this is the half
        # that keeps two peers from paging the resident model out between them.
        self._peer_inference_lock = asyncio.Semaphore(1)

    async def connect_via_uri(self, uri: str):
        """
        Connect to peer using dpc:// URI (Direct TLS).

        Supports local network and external IP connections.

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter
        """
        from dpc_protocol.utils import parse_dpc_uri

        logger.info("Orchestrating direct connection to %s", uri)

        # Parse the URI to extract host, port, and node_id
        host, port, target_node_id = parse_dpc_uri(uri)

        # Use connect_directly from P2PManager
        await self.p2p_manager.connect_directly(host, port, target_node_id)

    async def connect_via_hub(self, node_id: str):
        """
        Connect to peer via Hub using WebRTC (with NAT traversal).

        Args:
            node_id: Target peer's node ID

        Raises:
            ConnectionError: If Hub is not connected
        """
        logger.info("Orchestrating WebRTC connection to %s via Hub", node_id)

        # Check if Hub is connected
        if not self.hub_client.websocket or self.hub_client.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Not connected to Hub. Cannot establish WebRTC connection.")

        # Use WebRTC connection via Hub
        await self.p2p_manager.connect_via_hub(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def disconnect(self, node_id: str):
        """
        Disconnect from peer.

        Args:
            node_id: Peer's node ID to disconnect from
        """
        await self.p2p_manager.shutdown_peer_connection(node_id)

    async def test_port_connectivity(self, uri: str) -> dict:
        """
        Test port connectivity before attempting full connection.

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter

        Returns:
            Dict with keys:
            - success (bool): Whether port is accessible
            - message (str): Diagnostic message
            - host (str): Target host
            - port (int): Target port
            - node_id (str): Target node ID
        """
        from dpc_protocol.utils import parse_dpc_uri

        # Parse the URI to extract host and port
        host, port, target_node_id = parse_dpc_uri(uri)

        # Test port connectivity
        success, message = await self.p2p_manager.test_port_connectivity(host, port)

        return {
            "success": success,
            "message": message,
            "host": host,
            "port": port,
            "node_id": target_node_id
        }

    async def send_message(self, target_node_id: str, text: str):
        """
        Send text message to connected peer.

        Args:
            target_node_id: Peer's node ID
            text: Message text

        Raises:
            Exception: If sending fails
        """
        logger.debug("Sending text message to %s: %s", target_node_id, text)

        message = {
            "command": "SEND_TEXT",
            "payload": {
                "text": text
            }
        }

        try:
            await self.p2p_manager.send_message_to_peer(target_node_id, message)
        except Exception as e:
            logger.error("Error sending message to %s: %s", target_node_id, e, exc_info=True)
            raise

    def get_connected_peers(self) -> List[str]:
        """
        Get list of connected peer node IDs.

        Returns:
            List of node IDs currently connected
        """
        return list(self.p2p_manager.peers.keys())

    async def broadcast_to_peers(self, message: dict):
        """
        Broadcast message to all connected peers.

        Used by ConsensusManager for votes and proposals.

        Args:
            message: Message dict to broadcast
        """
        for peer_id in self.get_connected_peers():
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, message)
            except Exception as e:
                logger.warning("Failed to broadcast to %s: %s", peer_id, e)

    # ─────────────────────────────────────────────────────────────
    # Incoming P2P request handlers (Phase C Step 5 Batch 1)
    # ─────────────────────────────────────────────────────────────

    def _effort_for_peer(
        self, peer_id: str, requested: str, serving_alias: str,
    ) -> Optional[str]:
        """The rung a served call runs on: the guest's word under this node's cap,
        or, where the guest chose nothing, what this node's own configuration runs
        at. `EffortRefused` for a word the alias has no rung for.

        The vocabulary is the alias's own where its model named its words and the
        shared scale where it did not; `off` is the foot of every scale and always
        reachable. A word the guest chose is sent to the provider; the host's own
        configured word is not, because the alias already holds it and what would
        travel from here is this node's normalisation of it — a downgrade on a
        vendor whose ladder has more words than ours.

        None is «no word describes this call», which is not `off`: an alias with no
        effort channel, or a configured ceiling this node cannot read, where the
        call runs at the host's default and nothing here knows its name.
        `GatewayServer._served_effort` is the same rule at the other door.

        What a guest that asks for nothing is served is
        `effective_reasoning_default`, the same helper the alias's menu row quotes
        as `reasoning_default` — so the rung the row promises is the rung the door
        serves. Until 2026-09-14 the two were separate sentences and disagreed:
        the row said `xhigh`, the template's default, while the door served the
        configured `low`.
        """
        from .providers.base import (
            REASONING_EFFORTS,
            REASONING_OFF,
            declared_reasoning_words,
            effective_reasoning_default,
        )

        provider = self._provider_for_alias(serving_alias)
        words, _template_default = declared_reasoning_words(provider)
        configured = self._configured_effort_for_alias(serving_alias)
        asked = (requested or "").strip()

        if not asked:
            rung = effective_reasoning_default(provider)
            if rung is None and configured:
                logger.info(
                    "Peer %s chose no reasoning effort; %s is configured as %r, which is not "
                    "a word this alias knows — serving this node's default, under no name",
                    peer_id, serving_alias, configured,
                )
            return rung

        wanted = self._rung_of(provider, asked)
        if wanted is None:
            known = ", ".join(words) if words else ", ".join((REASONING_OFF,) + REASONING_EFFORTS)
            source = "the words its own model named" if words else "the shared scale"
            raise EffortRefused(
                f"This node serves '{serving_alias}' at efforts {known} — {source} — and "
                f"'{asked}' reaches none of them; ask for one of those, or send the request "
                "without an effort"
            )

        if not configured:
            return wanted
        cap = self._rung_of(provider, configured)
        if cap is None:
            # A ceiling this node stated and cannot read back. Serving the
            # guest's wish would be fail-open, so the call takes the host's
            # default and the row names nothing rather than a word nobody applied.
            logger.info(
                "Peer %s asked for reasoning effort %s; %s is configured as %r, which is "
                "not a word this alias knows — serving this node's default rather than the "
                "peer's request", peer_id, wanted, serving_alias, configured,
            )
            return None

        served = self._lower_rung(wanted, cap, words)
        if served is None:
            logger.info(
                "Peer %s asked for reasoning effort %s and %s is capped at %s: the two sit "
                "on ladders this node cannot rank together — serving this node's default",
                peer_id, wanted, serving_alias, cap,
            )
            return None
        if served != wanted:
            logger.info(
                "Peer %s asked for reasoning effort %s; this node caps %s at %s, serving %s",
                peer_id, wanted, serving_alias, cap, served,
            )
        return served

    @staticmethod
    def _lower_rung(wanted: str, cap: str, words: Optional[List[str]]) -> Optional[str]:
        """The lower of two rungs, or None when the two cannot be ranked.

        The alias's own ladder first, in the order its model named it; the shared
        scale otherwise, which is where `xhigh` and `max` meet. Neither: a ceiling
        this node cannot apply, which is not an open door.
        """
        from .providers.base import REASONING_EFFORTS, REASONING_OFF, normalize_reasoning_effort

        if words:
            ladder = (REASONING_OFF,) + tuple(words)
            if wanted in ladder and cap in ladder:
                return wanted if ladder.index(wanted) <= ladder.index(cap) else cap
        shared = (REASONING_OFF,) + REASONING_EFFORTS
        low, high = normalize_reasoning_effort(wanted), normalize_reasoning_effort(cap)
        if low is None or high is None:
            return None
        return wanted if shared.index(low) <= shared.index(high) else cap

    @staticmethod
    def _rung_of(provider: Any, word: str) -> Optional[str]:
        """The rung `word` names on this alias, or None when it names none.

        A provider whose ladder is its model's own answers for itself; the rest
        are the shared scale. An answer that is not a word is not an answer.
        """
        from .providers.base import normalize_reasoning_effort, reasoning_word_for

        if provider is None:
            return normalize_reasoning_effort(word)
        rung = reasoning_word_for(provider, word)
        return rung if isinstance(rung, str) and rung else None

    def _provider_for_alias(self, alias: str) -> Optional[Any]:
        """The loaded provider behind an alias, or None. The registry is a dict or
        it is nothing: a stand-in answering every attribute would be read here as
        a model with a ladder."""
        manager = getattr(self.service, "llm_manager", None)
        providers = getattr(manager, "providers", None)
        return providers.get(alias) if isinstance(providers, dict) else None

    def _configured_effort_for_alias(self, alias: str) -> str:
        """The effort this node configured for the alias it serves peers from.

        Read off the built provider's own `config` (`AIProvider.__init__` keeps
        the providers.json entry there), because that is the only place the
        alias's configured effort exists at run time — there is no separate
        table of configs to consult.
        """
        config = self._provider_config(alias)
        return config.get("reasoning_effort") if config else None

    def _provider_config(self, alias: str) -> Optional[Dict[str, Any]]:
        """The providers.json entry of a loaded alias, or None when the alias
        is not loaded or its provider keeps no dict there."""
        config = getattr(self._provider_for_alias(alias), "config", None)
        return config if isinstance(config, dict) else None

    def _provider_type(self, alias: str) -> Optional[str]:
        """The provider `type` of a loaded alias — what `gateway.provider_types`
        reads for every alias, read here for the one the door serves."""
        config = self._provider_config(alias)
        return config.get("type") if config else None

    def _tariff_for_call(
        self, serving_alias: str, peer_id: str, started_at: datetime,
    ) -> Optional[AppliedTariff]:
        """What this peer is charged for this alias at this moment, or None.

        None is «nothing declared» — the v1 gift — and so is a firewall that
        cannot answer: a tariff that fails to resolve must not turn a served
        answer into a refusal, and an unpriced row is the honest record of it.
        """
        firewall = getattr(self.service, "firewall", None)
        try:
            return firewall.tariff_for(serving_alias, peer_id=peer_id, at=started_at)
        except Exception:
            logger.error(
                "The tariff for %s served to %s could not be resolved; the call is recorded "
                "as unpriced", serving_alias, peer_id, exc_info=True,
            )
            return None

    def _record_peer_call(
        self,
        *,
        peer_id: str,
        request_id: str,
        serving_alias: str,
        result: Dict[str, Any],
        model: Optional[str],
        started_at: datetime,
        duration_s: float,
        served_effort: Optional[str] = None,
    ) -> tuple[str, Optional[AppliedTariff], Optional[float]]:
        """Price a served call once, at the moment it was made, and write its
        usage row under the peer's name (ADR-041 D3), naming the effort it ran
        at and whether the transport proved the name the row is written under.

        Two prices, and only one of them leaves this node. `cost_usd` is what
        the call cost us — a vendor's dollars, or zero for our own card — and
        stays on this row. The owner's tariff is what the guest is charged, is
        resolved for this peer at `started_at`, and travels: returned here as
        `(billing, tariff, tariff_amount)` for the response to carry.

        A row that cannot be built is logged and does not fail the answer: the
        tokens have already been generated and paid for.
        """
        from .dpc_agent.pricing import compute_cost_usd, get_billing_model
        from .p2p_manager import peer_proof

        proved, connection_type = peer_proof(getattr(self.p2p_manager, "peers", None), peer_id)
        billing = get_billing_model(serving_alias, model)
        cost_usd = compute_cost_usd(
            serving_alias,
            result.get("prompt_tokens") or 0,
            result.get("response_tokens") or 0,
            model=model,
            at=started_at,
        )
        output_includes_thinking = result.get("output_includes_thinking", "unknown")
        tariff = self._tariff_for_call(serving_alias, peer_id, started_at)
        tariff_amount = tariff_amount_for(
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("response_tokens"),
            thinking_tokens=result.get("thinking_tokens"),
            output_includes_thinking=output_includes_thinking,
            tariff_in=tariff.in_per_1m if tariff else None,
            tariff_out=tariff.out_per_1m if tariff else None,
        )
        try:
            row = usage_row(
                request_id=request_id,
                caller=peer_id,
                caller_kind="peer",
                alias=serving_alias,
                model=model,
                route="local",
                prompt_tokens=result.get("prompt_tokens"),
                completion_tokens=result.get("response_tokens"),
                thinking_tokens=result.get("thinking_tokens"),
                counts_source=result.get("counts_source", "ours"),
                output_includes_thinking=output_includes_thinking,
                served_effort=served_effort,
                peer_proved=proved,
                peer_connection_type=connection_type,
                started_at=started_at,
                duration_s=duration_s,
                billing=billing,
                cost_usd=cost_usd,
                tariff_in=tariff.in_per_1m if tariff else None,
                tariff_out=tariff.out_per_1m if tariff else None,
                tariff_currency=tariff.currency if tariff else None,
                tariff_at=tariff.at if tariff else None,
                tariff_amount=tariff_amount,
            )
        except Exception:
            logger.error(
                "Usage row for peer %s request %s was not built", peer_id, request_id, exc_info=True
            )
            return billing, tariff, tariff_amount
        (self._ledger or default_ledger()).append(row)
        return billing, tariff, tariff_amount

    async def handle_inference_request(self, peer_id: str, request_id: str, prompt: str, model: str = None, provider: str = None, images: list = None, reasoning_effort: str = None):
        """Handle incoming remote inference request from a peer."""
        from dpc_protocol.protocol import create_remote_inference_response
        from .p2p_manager import peer_proof

        logger.debug("Handling inference request from %s (request_id: %s, images: %s)", peer_id, request_id, "yes" if images else "no")

        # Identity before every other gate (ADR-041 D2). `peer_id` is the name
        # the firewall admits on, the ledger writes under and a quota counts
        # against, and only the direct tier proved it: WebRTC takes it from the
        # Hub's signal, relay from our own intention, gossip from an envelope
        # field. This request reaches here over any of them, so the tier is
        # asked here — before `can_request_inference`, before the serving alias
        # is read, before any provider call. The order matters beyond the cost:
        # a sender the transport could not prove must learn nothing about this
        # node's alias list, and both gates below answer about aliases (D7
        # part 1, D4-0). No usage row either — a refused call is not a call.
        proved, connection_type = peer_proof(getattr(self.p2p_manager, "peers", None), peer_id)
        if proved is not True:
            over = "no connection of record" if connection_type is None else repr(connection_type)
            logger.warning(
                "Peer inference refused for %s: the request arrived over %s, and the peer's "
                "key is proved only on direct TLS (ADR-041 D2)", peer_id, over,
            )
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=(
                    "This node serves peer inference only over a connection whose key is "
                    "proved — direct TLS, where the peer's key has been proved (ADR-041 D2). "
                    f"This request arrived over {over}."
                ),
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        # The alias the peer named is evidence for the gate, never an instruction
        # to the router (ADR-040 D4-0).
        if not self.service.firewall.can_request_inference(peer_id, model, provider=provider):
            denied_for = " for model {}".format(model) if model else ""
            denied_for += " via provider {}".format(provider) if provider else ""
            logger.warning("Access denied: %s cannot request inference%s", peer_id, denied_for)
            error_response = create_remote_inference_response(
                request_id=request_id,
                error="Access denied: You are not authorized to request inference" + denied_for
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        # What this node serves peers from is the host's choice and nobody else's.
        # Unset means we serve nobody: refused out loud, rather than falling back
        # to whatever the router would have picked — which on a box with a paid
        # default_provider means the host pays for a stranger's tokens.
        serving_alias = self.service.firewall.compute_serving_alias
        if not serving_alias:
            logger.warning(
                "Peer inference refused for %s: this node designates no compute.serving_alias", peer_id
            )
            error_response = create_remote_inference_response(
                request_id=request_id,
                error="This node shares no compute: no serving alias is configured",
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        # What is shared is not shared onward (ADR-041 D7 part 1). The gateway
        # classifies its lists against the registry on every request; the rules
        # loader cannot, because no registry exists when they are parsed, so
        # this door asks the same predicate here, before the router and before
        # any usage row: an alias that is somebody else's model is not served.
        refusal = onward_sharing_refusal(SERVING_LOCAL_KEY, serving_alias, self._provider_type(serving_alias))
        if refusal:
            logger.warning("Peer inference refused for %s: %s", peer_id, refusal)
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=f"This node cannot serve '{serving_alias}' to a peer: {refusal}",
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        # Before the call and before any row: a word this alias has no rung for
        # is answered with the words it has, not served at whatever the model
        # would have done with silence.
        try:
            served_effort = self._effort_for_peer(peer_id, reasoning_effort, serving_alias)
        except EffortRefused as refusal:
            logger.warning("Peer inference refused for %s: %s", peer_id, refusal)
            error_response = create_remote_inference_response(request_id=request_id, error=str(refusal))
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        try:
            logger.info("Running inference for %s (requested model: %s, requested provider: %s, serving alias: %s)",
                        peer_id, model or 'default', provider or 'default', serving_alias)

            # Only a word the guest chose travels to the provider: this node's
            # own configured effort is already inside the alias.
            query_kwargs = (
                {"reasoning_effort": served_effort}
                if served_effort and (reasoning_effort or "").strip() else {}
            )

            async with self._peer_inference_lock:
                # Clocked inside the lock: the wait is not part of the call, and
                # the price depends on the hour the call is made (ADR-041 D3).
                started_at = datetime.now(timezone.utc)
                clock = time.monotonic()
                result = await self.service.llm_manager.query(
                    prompt, provider_alias=serving_alias, images=images,
                    return_metadata=True, **query_kwargs,
                )
            duration_s = time.monotonic() - clock
            logger.info("Inference completed successfully for %s", peer_id)

            actual_model = result.get("model", model)
            # What the door decided to serve is a claim about the alias; what
            # the provider reports is the rung its entry point ran. The guest
            # and both rows get the second where there is one.
            ran_effort = result.get("provider_served_effort") or served_effort
            billing, tariff, tariff_amount = self._record_peer_call(
                peer_id=peer_id, request_id=request_id, serving_alias=serving_alias,
                result=result, model=actual_model, started_at=started_at,
                duration_s=duration_s, served_effort=ran_effort,
            )
            # The usage row above is the record of this call (ADR-041 D3): a
            # peer's request belongs to no agent, so no events.jsonl carries it,
            # and on a paid alias the vendor's own usage line lands in the
            # owner's burn series wearing nobody's name. This line is a log line.
            # `counts` says whose numbers these are, because the two differ by
            # more than rounding: an engine counts the template and the image,
            # our own recount sees the visible text alone.
            logger.info(
                "Peer inference served: peer=%s alias=%s model=%s effort=%s "
                "prompt_tokens=%s response_tokens=%s counts=%s",
                peer_id, serving_alias, actual_model, ran_effort or "unnamed",
                result.get("prompt_tokens"), result.get("response_tokens"),
                result.get("counts_source", "ours"),
            )
            success_response = create_remote_inference_response(
                request_id=request_id,
                response=result["response"],
                tokens_used=result.get("tokens_used"),
                prompt_tokens=result.get("prompt_tokens"),
                response_tokens=result.get("response_tokens"),
                model_max_tokens=result.get("model_max_tokens"),
                model=actual_model,
                provider=result.get("provider"),
                thinking=result.get("thinking"),
                thinking_tokens=result.get("thinking_tokens"),
                tariff_in=tariff.in_per_1m if tariff else None,
                tariff_out=tariff.out_per_1m if tariff else None,
                tariff_currency=tariff.currency if tariff else None,
                tariff_at=tariff.at.isoformat() if tariff else None,
                tariff_amount=tariff_amount,
                billing=billing,
                output_includes_thinking=result.get("output_includes_thinking"),
                # The rung this call ran on, asked for or not: the guest's only
                # way to check the depth it paid for against the depth it asked for.
                served_effort=ran_effort,
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)
            logger.debug("Sent inference result to %s", peer_id)

        except Exception as e:
            logger.error("Inference failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_inference_response(request_id=request_id, error=str(e))
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending inference error response to %s: %s", peer_id, send_err, exc_info=True)

    async def handle_transcription_request(self, peer_id: str, request_id: str, audio_base64: str, mime_type: str, model: str = None, provider: str = None, language: str = "auto", task: str = "transcribe"):
        """Handle incoming remote transcription request from a peer."""
        from dpc_protocol.protocol import create_remote_transcription_response
        import base64
        import tempfile
        import os

        logger.debug("Handling transcription request from %s (request_id: %s, mime_type: %s)", peer_id, request_id, mime_type)

        if not self.service.firewall.can_request_transcription(peer_id, model):
            logger.warning("Access denied: %s cannot request transcription%s", peer_id, f" for model {model}" if model else "")
            error_response = create_remote_transcription_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request transcription" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending transcription error response to %s: %s", peer_id, e, exc_info=True)
            return

        temp_audio_path = None
        try:
            logger.info("Running transcription for %s (model: %s, provider: %s, language: %s, task: %s)",
                       peer_id, model or 'default', provider or 'default', language, task)

            audio_data = base64.b64decode(audio_base64)
            ext_map = {
                "audio/webm": ".webm", "audio/opus": ".opus", "audio/ogg": ".ogg",
                "audio/wav": ".wav", "audio/mp3": ".mp3", "audio/mp4": ".mp4", "audio/mpeg": ".mp3"
            }
            file_ext = ext_map.get(mime_type, ".webm")

            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_audio_path = temp_file.name
                temp_file.write(audio_data)

            provider_alias_to_use = provider
            if model and not provider:
                found_alias = self.service.llm_manager.find_provider_by_model(model)
                if found_alias:
                    provider_alias_to_use = found_alias
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            provider_instance = self.service.llm_manager.providers.get(provider_alias_to_use)
            if not hasattr(provider_instance, 'transcribe'):
                raise ValueError(f"Provider '{provider_alias_to_use}' does not support transcription")

            if language != "auto":
                provider_instance.language = language
            if task:
                provider_instance.task = task

            result = await provider_instance.transcribe(temp_audio_path)
            logger.info("Transcription completed successfully for %s", peer_id)

            success_response = create_remote_transcription_response(
                request_id=request_id,
                text=result.get("text", ""),
                language=result.get("language"),
                duration_seconds=result.get("duration"),
                provider=result.get("provider") or provider_alias_to_use
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)

        except Exception as e:
            logger.error("Transcription failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_transcription_response(request_id=request_id, error=str(e))
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending transcription error response to %s: %s", peer_id, send_err, exc_info=True)
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.unlink(temp_audio_path)
                except Exception as cleanup_err:
                    logger.warning("Failed to delete temporary audio file %s: %s", temp_audio_path, cleanup_err)

    async def handle_get_providers_request(self, peer_id: str):
        """Handle GET_PROVIDERS request — send available providers filtered by firewall."""
        from dpc_protocol.protocol import create_providers_response

        logger.debug("Handling GET_PROVIDERS request from %s", peer_id)

        has_compute_access = self.service.firewall.can_request_inference(peer_id)
        has_transcription_access = self.service.firewall.can_request_transcription(peer_id)

        if not has_compute_access and not has_transcription_access:
            logger.warning("Access denied: %s cannot access compute or transcription resources", peer_id)
            response = create_providers_response([])
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, response)
            except Exception as e:
                logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)
            return

        all_providers = [
            self.service.build_p2p_provider_info(alias, provider)
            for alias, provider in self.service.llm_manager.providers.items()
        ]

        filtered_providers = []
        for provider_info in all_providers:
            provider_type = provider_info["type"]
            model = provider_info["model"]
            if provider_type == "local_whisper":
                if has_transcription_access and self.service.firewall.can_request_transcription(peer_id, model):
                    filtered_providers.append(provider_info)
            else:
                # Offer only what we will actually serve (ADR-040 D4-0). Offering
                # the rest both invites a request the gate now refuses and tells a
                # peer which paid accounts this node holds.
                if (has_compute_access
                        and provider_info["alias"] == self.service.firewall.compute_serving_alias
                        and self.service.firewall.can_request_inference(peer_id, model)):
                    filtered_providers.append(provider_info)

        logger.debug("Sending %d providers to %s (filtered from %d total)",
                    len(filtered_providers), peer_id[:20], len(all_providers))

        # Say the quiet part once. Compute sharing on, the peer allowed, and the
        # answer still carries no inference provider — because `serving_alias`
        # is what designates one and it is empty by default (D4-0: the host
        # allocates, not the caller). Until this line the only trace was the
        # DEBUG count above, so a person who had switched sharing on and added
        # the peer to a group saw a peer offering nothing and no reason for it.
        if has_compute_access and not self.service.firewall.compute_serving_alias:
            logger.info(
                "Compute sharing is enabled and %s is allowed, but no compute.serving_alias "
                "is designated — no inference provider is offered. Set it in the firewall "
                "rules (Compute Sharing) to name the one alias peers are served from.",
                peer_id[:20],
            )

        response = create_providers_response(filtered_providers)
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
        except Exception as e:
            logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)

    async def handle_providers_response(self, peer_id: str, providers: list):
        """Handle PROVIDERS_RESPONSE — store in metadata and resolve pending requests."""
        logger.debug("Received %d providers from %s", len(providers), peer_id)

        if peer_id not in self.service.peer_metadata:
            self.service.peer_metadata[peer_id] = {}
        self.service.peer_metadata[peer_id]["providers"] = providers

        pending_future = self.service._pending_providers_requests.pop(peer_id, None)
        if pending_future and not pending_future.done():
            pending_future.set_result(providers)
            logger.debug("Resolved pending providers request for %s", peer_id)

        await self.service.local_api.broadcast_event("peer_providers_updated", {
            "node_id": peer_id,
            "providers": providers
        })

    # ─────────────────────────────────────────────────────────────
    # File transfer coordination (Phase C Step 5 Batch 2)
    # ─────────────────────────────────────────────────────────────

    async def send_file(self, node_id: str, file_path: str, file_size_bytes: int = None):
        """Send a file to a peer via P2P file transfer."""
        from pathlib import Path

        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        transfer_id = await self.service.file_transfer_manager.send_file(node_id, file)

        size_bytes = file.stat().st_size
        return {
            "transfer_id": transfer_id,
            "status": "pending",
            "filename": file.name,
            "size_bytes": size_bytes
        }

    async def accept_file_transfer(self, transfer_id: str):
        """Accept an incoming file transfer offer."""
        transfer = self.service.file_transfer_manager.active_transfers.get(transfer_id)
        if not transfer:
            raise ValueError(f"Unknown transfer: {transfer_id}")
        if transfer.direction != "download":
            raise ValueError(f"Transfer {transfer_id} is not a download")

        await self.p2p_manager.send_message_to_peer(transfer.node_id, {
            "command": "FILE_ACCEPT",
            "payload": {"transfer_id": transfer_id}
        })
        return {"transfer_id": transfer_id, "status": "accepted"}

    async def cancel_file_transfer(self, transfer_id: str, reason: str = "user_cancelled"):
        """Cancel an active file transfer."""
        transfer = self.service.file_transfer_manager.active_transfers.get(transfer_id)
        await self.service.file_transfer_manager.cancel_transfer(transfer_id, reason)

        if transfer:
            await self.service.local_api.broadcast_event("file_transfer_cancelled", {
                "transfer_id": transfer_id,
                "node_id": transfer.node_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": reason,
                "status": "cancelled"
            })
        return {"transfer_id": transfer_id, "status": "cancelled", "reason": reason}

    # ─────────────────────────────────────────────────────────────
    # Outgoing P2P requests (Phase C Step 5 Batch 3)
    # ─────────────────────────────────────────────────────────────

    async def request_inference_from_peer(self, peer_id: str, prompt: str, model: str = None, provider: str = None, images: list = None, reasoning_effort: str = None, timeout: float = 1200.0) -> str:
        """Request remote inference from a specific peer."""
        import uuid
        from dpc_protocol.protocol import create_remote_inference_request

        logger.debug("Requesting inference from peer: %s (images: %s)", peer_id, 'yes' if images else 'no')

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            request_id = str(uuid.uuid4())
            response_future = asyncio.Future()
            self.service._pending_inference_requests[request_id] = response_future

            request_message = create_remote_inference_request(
                request_id=request_id, prompt=prompt,
                model=model, provider=provider, images=images,
                reasoning_effort=reasoning_effort,
            )
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("Received inference result from %s", peer_id)
                return result
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for inference from %s", peer_id)
                raise TimeoutError(f"Inference request to {peer_id} timed out after {timeout}s")
            finally:
                self.service._pending_inference_requests.pop(request_id, None)
        except Exception as e:
            logger.error("Error requesting inference from %s: %s", peer_id, e, exc_info=True)
            raise

    async def request_transcription_from_peer(
        self, peer_id: str, audio_base64: str, mime_type: str,
        model: str = None, provider: str = None, language: str = "auto",
        task: str = "transcribe", timeout: float = 120.0
    ) -> Dict[str, any]:
        """Request remote transcription from a specific peer."""
        import uuid
        from dpc_protocol.protocol import create_remote_transcription_request

        logger.debug("Requesting transcription from peer: %s (mime_type: %s, language: %s)", peer_id, mime_type, language)

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            request_id = str(uuid.uuid4())
            response_future = asyncio.Future()
            self.service._pending_transcription_requests[request_id] = response_future

            request_message = create_remote_transcription_request(
                request_id=request_id, audio_base64=audio_base64,
                mime_type=mime_type, model=model, provider=provider,
                language=language, task=task
            )
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("Received transcription result from %s: %d chars", peer_id, len(result.get("text", "")))
                return result
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for transcription from %s", peer_id)
                raise TimeoutError(f"Transcription request to {peer_id} timed out after {timeout}s")
            finally:
                self.service._pending_transcription_requests.pop(request_id, None)
        except Exception as e:
            logger.error("Error requesting transcription from %s: %s", peer_id, e, exc_info=True)
            raise

    async def aggregate_contexts(self, query: str, peer_ids: list = None) -> dict:
        """Aggregate contexts from local user and connected peers."""
        from dpc_protocol.pcm_core import PersonalContext

        contexts = {}
        contexts[self.p2p_manager.node_id] = self.p2p_manager.local_context

        if peer_ids is None:
            peer_ids = list(self.p2p_manager.peers.keys())

        if peer_ids:
            tasks = [self.service._request_context_from_peer(peer_id, query) for peer_id in peer_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for peer_id, result in zip(peer_ids, results):
                if isinstance(result, PersonalContext):
                    contexts[peer_id] = result
                elif result is not None:
                    logger.error("Error getting context from %s: %s", peer_id, result)

        return contexts
