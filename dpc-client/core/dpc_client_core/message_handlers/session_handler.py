"""Handlers for conversation session management commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class ProposeNewSessionHandler(MessageHandler):
    """Handles PROPOSE_NEW_SESSION messages (peer proposing to end conversation)."""

    @property
    def command_name(self) -> str:
        return "PROPOSE_NEW_SESSION"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle PROPOSE_NEW_SESSION message.

        Peer is proposing to end current conversation and start fresh.
        Forward to session manager for voting.

        Args:
            sender_node_id: Node ID of proposer
            payload: Contains session proposal data (conversation_id, proposal_id, etc.)
        """
        proposal_id = payload.get("proposal_id")

        self.logger.info(
            "Received PROPOSE_NEW_SESSION from %s: proposal=%s",
            sender_node_id[:20],
            proposal_id[:8] if proposal_id else "none"
        )

        # Forward to session manager
        await self.service.session_manager.handle_proposal_message(sender_node_id, payload)

        return None


class VoteNewSessionHandler(MessageHandler):
    """Handles VOTE_NEW_SESSION messages (peer voting on session proposal)."""

    @property
    def command_name(self) -> str:
        return "VOTE_NEW_SESSION"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle VOTE_NEW_SESSION message.

        Peer is voting on a session proposal.
        Forward to session manager for tally.

        Args:
            sender_node_id: Node ID of voter
            payload: Contains vote data (proposal_id, vote)
        """
        proposal_id = payload.get("proposal_id")
        vote = payload.get("vote")

        self.logger.info(
            "Received VOTE_NEW_SESSION from %s: proposal=%s, vote=%s",
            sender_node_id[:20],
            proposal_id[:8] if proposal_id else "none",
            "approve" if vote else "reject"
        )

        # Forward to session manager
        await self.service.session_manager.handle_vote_message(sender_node_id, payload)

        return None


class NewSessionResultHandler(MessageHandler):
    """Handles NEW_SESSION_RESULT messages (voting outcome notification)."""

    @property
    def command_name(self) -> str:
        return "NEW_SESSION_RESULT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle voting result notification from peer.

        Updates local session status and notifies UI.
        If approved, clears local conversation history.

        Args:
            sender_node_id: Node ID of the node that finalized voting
            payload: Contains voting result data (proposal_id, result, clear_history, etc.)
        """
        proposal_id = payload.get("proposal_id")
        result = payload.get("result")
        clear_history = payload.get("clear_history", False)
        conversation_id = payload.get("conversation_id")

        self.logger.info(
            "Received NEW_SESSION_RESULT from %s: proposal=%s, result=%s, clear=%s",
            sender_node_id[:20],
            proposal_id[:8] if proposal_id else "none",
            result,
            clear_history
        )

        # If approved and clear_history flag set: clear local conversation
        if result == "approved" and clear_history:
            self.logger.info("Clearing local conversation history for %s", conversation_id[:20])
            monitor = self.service._get_or_create_conversation_monitor(conversation_id)
            monitor.reset_conversation()

        # Update session manager (if session exists)
        session = self.service.session_manager.get_session(proposal_id)
        if session:
            # Remove from active sessions (finalized)
            if proposal_id in self.service.session_manager.active_sessions:
                del self.service.session_manager.active_sessions[proposal_id]
            self.logger.debug("Removed finalized session %s from active sessions", proposal_id[:8])

        # Broadcast event to UI (add sender_node_id for frontend conversation lookup)
        ui_payload = {**payload, "sender_node_id": sender_node_id}
        await self.service.local_api.broadcast_event("new_session_result", ui_payload)

        return None
