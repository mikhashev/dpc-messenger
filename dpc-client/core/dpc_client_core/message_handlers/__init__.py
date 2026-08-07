"""
Message handlers for P2P commands.

This module provides a pluggable architecture for handling different P2P message types.
Each message handler is responsible for processing a specific command type.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class MessageHandler(ABC):
    """Base class for P2P message handlers."""

    def __init__(self, service):
        """
        Initialize handler with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, settings, etc.)

        Note: Handlers access service components dynamically (e.g., self.service.local_api)
        rather than storing references. This allows tests to mock components after initialization.
        """
        self.service = service
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle message from sender with given payload.

        Args:
            sender_node_id: Node ID of the message sender
            payload: Message payload (data after command field)

        Returns:
            Optional response data (for request-response patterns)
        """
        pass

    @property
    @abstractmethod
    def command_name(self) -> str:
        """Command name this handler responds to."""
        pass

    def _authenticate_voter(self, transport_node_id: str, payload: Dict[str, Any]):
        """Who cast this vote, and whether we may count it.

        Returns (voter_node_id, verdict) where verdict is one of:
          verified   — signature checks out; the vote is the claimed node's
          unverified — signed, but the voter's certificate is not cached, so
                       nothing can be checked. Not counted, still relayed: a
                       node that does hold the certificate must get its chance.
          legacy     — no signature fields, from a node that predates this.
                       Counted only when the transport peer *is* the claimed
                       voter, i.e. it arrived first-hand. A relayed unsigned
                       vote is dropped rather than credited to the relayer,
                       which is what used to happen.
          rejected   — signature present and wrong, or signed by someone other
                       than the claimed voter. Not counted, not relayed.

        ADR-036 §5 forbids attributing an unsigned relayed message to the
        identity it claims, so "take voter_node_id from the payload" on its own
        would have traded misattribution for forgery by anyone connected.
        """
        claimed = payload.get("voter_node_id")
        vote_hash = payload.get("vote_hash")
        signature = payload.get("signature")
        signer = payload.get("signer_node_id")

        if not (claimed and vote_hash and signature and signer):
            if claimed and claimed != transport_node_id:
                return claimed, "legacy_relayed"
            return transport_node_id, "legacy"

        from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

        if payload.get("vote_preimage_version") != VOTE_PREIMAGE_VERSION:
            # A preimage we cannot recompute — one version ahead or behind.
            # Treated as legacy rather than rejected: cutting off a neighbour
            # mid-upgrade is an outage, not a security decision.
            if claimed != transport_node_id:
                return claimed, "legacy_relayed"
            return transport_node_id, "legacy"

        if signer != claimed:
            return transport_node_id, "rejected"

        expected = vote_content_hash(
            proposal_id=payload.get("proposal_id"),
            conversation_id=payload.get("conversation_id"),
            voter_node_id=claimed,
            vote=payload.get("vote"),
            timestamp=payload.get("timestamp"),
        )
        if expected != vote_hash:
            return transport_node_id, "rejected"

        try:
            from dpc_protocol.commit_integrity import CommitSigner
            result = CommitSigner.verify_signature(signer, vote_hash, signature)
        except Exception as e:  # noqa: BLE001
            self.logger.warning("Vote signature check failed for %s: %s", str(claimed)[:20], e)
            return transport_node_id, "rejected"

        if result is False:
            return transport_node_id, "rejected"
        if result is None:
            return claimed, "unverified"
        return claimed, "verified"

    async def _ask_for_certificate(self, node_id: str, ask_node_id: str) -> None:
        """Ask the peer that handed us something for the signer's certificate.

        The one place where a missing certificate is noticed is the one place
        that knows who to ask: whoever relayed the message almost certainly has
        it, because they could check the signature we could not. Nothing here
        needs to trust them — an answer whose key does not hash to `node_id` is
        refused on arrival.

        Asked once per node until it arrives, so a chatty group does not turn a
        missing certificate into a flood.
        """
        if not node_id or not ask_node_id or node_id == ask_node_id:
            return
        if node_id == self.service.p2p_manager.node_id:
            return
        pending = getattr(self.service, "pending_certificate_requests", None)
        if pending is None:
            return
        if node_id in pending:
            return
        if ask_node_id not in self.service.p2p_manager.peers:
            return

        pending.add(node_id)
        try:
            await self.service.p2p_manager.send_message_to_peer(
                ask_node_id, {"command": "CERT_REQUEST", "payload": {"node_id": node_id}}
            )
            self.logger.info(
                "Asked %s for the certificate of %s", ask_node_id[:20], str(node_id)[:20]
            )
        except Exception as e:  # noqa: BLE001
            pending.discard(node_id)
            self.logger.debug("Could not ask %s for a certificate: %s", ask_node_id[:20], e)

    async def _relay_to_group(
        self, command: str, payload: Dict[str, Any],
        sender_node_id: str, group_id: str
    ) -> None:
        """Relay message to group members the sender can't reach directly (star topology)."""
        group = self.service.group_manager.get_group(group_id)
        if not group:
            return
        relay_msg = {"command": command, "payload": payload}
        for member_id in group.members:
            if member_id == self.service.p2p_manager.node_id:
                continue
            if member_id == sender_node_id:
                continue
            if member_id in self.service.p2p_manager.peers:
                try:
                    await self.service.p2p_manager.send_message_to_peer(member_id, relay_msg)
                    self.logger.debug("Relayed %s to %s", command, member_id[:20])
                except Exception as e:
                    self.logger.error("Failed to relay %s to %s: %s", command, member_id[:20], e)


__all__ = ["MessageHandler"]
