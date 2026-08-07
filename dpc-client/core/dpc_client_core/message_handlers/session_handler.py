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

        # Relay to group members that can't reach the proposer directly (star topology)
        conversation_id = payload.get("conversation_id", "")
        if conversation_id.startswith("group-"):
            dedup_key = f"ses:{proposal_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "PROPOSE_NEW_SESSION", payload, sender_node_id, conversation_id
                )

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

        # Relay to group members that can't reach the voter directly (star topology)
        session = self.service.session_manager.active_sessions.get(proposal_id)
        conversation_id = session.proposal.conversation_id if session else ""
        if conversation_id and conversation_id.startswith("group-"):
            dedup_key = f"sev:{proposal_id}:{sender_node_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "VOTE_NEW_SESSION", payload, sender_node_id, conversation_id
                )

        return None


class NewSessionResultHandler(MessageHandler):
    """Handles NEW_SESSION_RESULT messages (voting outcome notification)."""

    @property
    def command_name(self) -> str:
        return "NEW_SESSION_RESULT"

    @staticmethod
    def _refuse_reason(session, sender_node_id: str, conversation_id: str) -> Optional[str]:
        """Why this result may not be acted on, or None if it may.

        Membership rather than "must be the initiator": in a star the far edge
        hears the result **relayed** by the middle node, so demanding the
        initiator would refuse the legitimate relay and break New Session
        exactly where it works today.
        """
        if session is None:
            return "no local voting session for that proposal"
        proposal = getattr(session, "proposal", None)
        if proposal is None:
            return "session carries no proposal"
        if conversation_id != getattr(proposal, "conversation_id", None):
            return "names a different conversation than the vote it claims"
        if sender_node_id not in (getattr(proposal, "participants", None) or set()):
            return "sender did not take part in that vote"
        return None

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

        # A result is an instruction to destroy history, so it has to be one we
        # can place. This used to clear first and look for the session after,
        # which meant any peer that could reach us erased any conversation it
        # cared to name. The gate is made of what this node already knows, so it
        # needs neither signatures nor the ADR-038 marker to stand up today.
        session = self.service.session_manager.get_session(proposal_id)
        refusal = self._refuse_reason(session, sender_node_id, conversation_id)
        if refusal:
            self.logger.warning(
                "Refusing NEW_SESSION_RESULT from %s for %s: %s",
                sender_node_id[:20], str(conversation_id)[:20], refusal
            )
            # Not passed to the UI and not relayed: a result we will not act on
            # is not one we should repeat to anyone else.
            return None

        # If approved and clear_history flag set: clear local conversation
        if result == "approved" and clear_history:
            self.logger.info("Clearing local conversation history for %s", conversation_id[:20])
            monitor = self.service._get_or_create_conversation_monitor(conversation_id)
            # This node's own archive settings, the same ones the initiator
            # applies to itself. Called bare, it used the defaults instead, so a
            # node configured not to archive archived anyway and a node with a
            # retention limit ignored it.
            firewall = getattr(self.service, "firewall", None)
            preserve, max_sessions = (
                firewall.get_history_settings(conversation_id) if firewall else (True, 0)
            )
            monitor.reset_conversation(preserve=preserve, max_sessions=max_sessions)
            self.service._group_agent_context.pop(conversation_id, None)

        # Update session manager (if session exists)
        if session:
            # Remove from active sessions (finalized)
            if proposal_id in self.service.session_manager.active_sessions:
                del self.service.session_manager.active_sessions[proposal_id]
            self.logger.debug("Removed finalized session %s from active sessions", proposal_id[:8])

        # Broadcast event to UI (add sender_node_id for frontend conversation lookup)
        ui_payload = {**payload, "sender_node_id": sender_node_id}
        await self.service.local_api.broadcast_event("new_session_result", ui_payload)

        # Relay to group members that can't reach the result sender directly (star topology)
        if conversation_id and conversation_id.startswith("group-"):
            dedup_key = f"ser:{proposal_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "NEW_SESSION_RESULT", payload, sender_node_id, conversation_id
                )

        return None
