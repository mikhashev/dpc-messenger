"""Handlers for knowledge commit and context update commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class ContextUpdatedHandler(MessageHandler):
    """Handles CONTEXT_UPDATED messages (peer notifying of context changes)."""

    @property
    def command_name(self) -> str:
        return "CONTEXT_UPDATED"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle CONTEXT_UPDATED message (Phase 7).

        Peer notifies that their personal context has changed.
        Invalidates cached peer context in all conversation monitors.

        Args:
            sender_node_id: Node ID of peer with updated context
            payload: Contains "context_hash" field with new hash
        """
        context_hash = payload.get("context_hash")
        self.logger.info(
            "Received CONTEXT_UPDATED from %s (hash: %s)",
            sender_node_id[:20],
            context_hash[:8] if context_hash else 'none'
        )

        # Invalidate cached peer context in all conversation monitors
        for monitor in self.service.conversation_monitors.values():
            monitor.invalidate_peer_context_cache(sender_node_id)
            self.logger.debug(
                "Invalidated cache for %s in conversation %s",
                sender_node_id[:20],
                monitor.conversation_id
            )

        # Broadcast event to UI so "Updated" badge appears
        await self.service.local_api.broadcast_event("peer_context_updated", {
            "node_id": sender_node_id,
            "context_hash": context_hash
        })

        return None


class ProposeKnowledgeCommitHandler(MessageHandler):
    """Handles PROPOSE_KNOWLEDGE_COMMIT messages (peer proposing knowledge commit)."""

    @property
    def command_name(self) -> str:
        return "PROPOSE_KNOWLEDGE_COMMIT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle PROPOSE_KNOWLEDGE_COMMIT message.

        Peer is proposing a knowledge commit for collaborative knowledge building.
        Forward to consensus manager for voting.

        Args:
            sender_node_id: Node ID of proposer
            payload: Contains commit proposal data
        """
        await self.service.consensus_manager.handle_proposal_message(sender_node_id, payload)
        return None


class VoteKnowledgeCommitHandler(MessageHandler):
    """Handles VOTE_KNOWLEDGE_COMMIT messages (peer voting on knowledge commit)."""

    @property
    def command_name(self) -> str:
        return "VOTE_KNOWLEDGE_COMMIT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle VOTE_KNOWLEDGE_COMMIT message.

        Peer is voting on a knowledge commit proposal.
        Forward to consensus manager for tally.

        Args:
            sender_node_id: Node ID of voter
            payload: Contains vote data (commit_id, vote)
        """
        await self.service.consensus_manager.handle_vote_message(sender_node_id, payload)
        return None


class KnowledgeCommitResultHandler(MessageHandler):
    """Handles KNOWLEDGE_COMMIT_RESULT messages (voting outcome notification)."""

    @property
    def command_name(self) -> str:
        return "KNOWLEDGE_COMMIT_RESULT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle voting result notification from peer.

        Updates local session status and notifies UI.

        Args:
            sender_node_id: Node ID of the node that finalized voting
            payload: Contains voting result data (status, vote_tally, votes, etc.)
        """
        proposal_id = payload.get("proposal_id")
        status = payload.get("status")

        self.logger.info(
            "Received KNOWLEDGE_COMMIT_RESULT from %s: proposal=%s, status=%s",
            sender_node_id[:20],
            proposal_id,
            status
        )

        # Update consensus manager session (if exists)
        session = self.service.consensus_manager.get_session(proposal_id)
        if session:
            session.status = status
            self.logger.debug("Updated local session status to: %s", status)

        # Broadcast event to UI
        await self.service.local_api.broadcast_event("knowledge_commit_result", payload)

        return None
