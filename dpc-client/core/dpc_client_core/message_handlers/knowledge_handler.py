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

        # Relay to group members that can't reach the proposer directly (star topology)
        conversation_id = payload.get("conversation_id", "")
        if conversation_id.startswith("group-"):
            proposal_id = payload.get("proposal_id", "")
            dedup_key = f"kc:{proposal_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "PROPOSE_KNOWLEDGE_COMMIT", payload, sender_node_id, conversation_id
                )

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

        # Relay to group members that can't reach the voter directly (star topology)
        proposal_id = payload.get("proposal_id", "")
        session = self.service.consensus_manager.sessions.get(proposal_id)
        conversation_id = session.proposal.conversation_id if session else ""
        if conversation_id and conversation_id.startswith("group-"):
            dedup_key = f"kv:{proposal_id}:{sender_node_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "VOTE_KNOWLEDGE_COMMIT", payload, sender_node_id, conversation_id
                )

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

        # Relay to group members that can't reach the result sender directly (star topology)
        conversation_id = session.proposal.conversation_id if session else ""
        if conversation_id and conversation_id.startswith("group-"):
            dedup_key = f"kr:{proposal_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group(
                    "KNOWLEDGE_COMMIT_RESULT", payload, sender_node_id, conversation_id
                )

        return None


class CommitSignedHandler(MessageHandler):
    """Handles COMMIT_SIGNED messages — a peer broadcasting their signature for an applied commit."""

    @property
    def command_name(self) -> str:
        return "COMMIT_SIGNED"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        A remote peer has applied the same commit and is sharing their RSA-PSS signature.

        Payload fields:
            commit_id   (str) — the commit this signature covers
            commit_hash (str) — deterministic hash the signature was made over
            node_id     (str) — signer's node_id (should match sender_node_id)
            signature   (str) — base64-encoded RSA-PSS signature

        The handler verifies the signature and, if valid, appends it to the
        markdown frontmatter so the file accumulates multi-party attestations.
        """
        commit_id = payload.get("commit_id", "")
        commit_hash = payload.get("commit_hash", "")
        signer_node_id = payload.get("node_id", sender_node_id)
        signature_b64 = payload.get("signature", "")

        if not all([commit_id, commit_hash, signature_b64]):
            self.logger.warning("COMMIT_SIGNED from %s missing fields", sender_node_id[:20])
            return None

        if not hasattr(self.service, 'consensus_manager') or self.service.consensus_manager is None:
            return None

        ok = await self.service.consensus_manager.record_commit_signature(
            commit_id, commit_hash, signer_node_id, signature_b64
        )
        if ok:
            self.logger.info(
                "Stored COMMIT_SIGNED from %s for commit %s",
                signer_node_id[:20], commit_id[:12]
            )

        # Relay to group members — find group via active sessions (sender may be a participant)
        dedup_key = f"cs:{commit_id}:{signer_node_id}"
        if dedup_key not in self.service._processed_message_ids:
            self.service._processed_message_ids.add(dedup_key)
            for sess in self.service.consensus_manager.sessions.values():
                conv_id = getattr(sess.proposal, 'conversation_id', '')
                if (conv_id and conv_id.startswith("group-")
                        and sender_node_id in getattr(sess.proposal, 'participants', [])):
                    await self._relay_to_group("COMMIT_SIGNED", payload, sender_node_id, conv_id)
                    break

        return None


class CommitAckHandler(MessageHandler):
    """Handles COMMIT_ACK — a peer confirming they successfully applied a commit.

    This closes Gap 3: _apply_commit failures are no longer silent.  Each node
    broadcasts COMMIT_ACK after a successful apply; the proposer (and all peers)
    track which participants have confirmed convergence.
    """

    @property
    def command_name(self) -> str:
        return "COMMIT_ACK"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Payload fields:
            commit_id    (str)       — commit that was applied
            node_id      (str)       — node that applied it (should match sender_node_id)
            participants (List[str]) — expected participants (for completion tracking)
        """
        commit_id = payload.get("commit_id", "")
        ack_node_id = payload.get("node_id", sender_node_id)
        participants = payload.get("participants", [])

        if not commit_id:
            self.logger.warning("COMMIT_ACK from %s missing commit_id", sender_node_id[:20])
            return None

        if not hasattr(self.service, 'consensus_manager') or self.service.consensus_manager is None:
            return None

        self.service.consensus_manager.record_commit_ack(commit_id, ack_node_id, participants)

        # Relay to other participants the sender can't reach directly
        dedup_key = f"ca:{commit_id}:{ack_node_id}"
        if dedup_key not in self.service._processed_message_ids:
            self.service._processed_message_ids.add(dedup_key)
            if participants:
                relay_msg = {"command": "COMMIT_ACK", "payload": payload}
                for p_id in participants:
                    if p_id == self.service.p2p_manager.node_id:
                        continue
                    if p_id == sender_node_id:
                        continue
                    if p_id in self.service.p2p_manager.peers:
                        try:
                            await self.service.p2p_manager.send_message_to_peer(p_id, relay_msg)
                            self.logger.debug("Relayed COMMIT_ACK to %s", p_id[:20])
                        except Exception as e:
                            self.logger.error("Failed to relay COMMIT_ACK to %s: %s", p_id[:20], e)

        return None


class ApplyKnowledgeCommitHandler(MessageHandler):
    """Handles APPLY_KNOWLEDGE_COMMIT — the recovery/retransmit path (Gap 3 + Gap 5).

    Previously a dead protocol message, now wired as the explicit recovery mechanism:
    if a node failed to apply a commit during finalization (disk error, crash, power
    loss, etc.) and subsequently misses the COMMIT_ACK window, a peer can retransmit
    the finalized commit object via APPLY_KNOWLEDGE_COMMIT.

    This handler applies the commit idempotently — it first checks whether the commit
    is already present in the local commit history, and only applies if missing.

    Future enhancement: after a COMMIT_ACK timeout, the proposer can automatically
    retransmit via this command to nodes that did not ACK.
    """

    @property
    def command_name(self) -> str:
        return "APPLY_KNOWLEDGE_COMMIT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Payload: serialized KnowledgeCommit dict (from KnowledgeCommit.to_dict()).
        """
        from dpc_protocol.knowledge_commit import KnowledgeCommit

        commit_id = payload.get("commit_id", "")
        if not commit_id:
            self.logger.warning("APPLY_KNOWLEDGE_COMMIT from %s missing commit_id", sender_node_id[:20])
            return None

        if not hasattr(self.service, 'consensus_manager') or self.service.consensus_manager is None:
            return None

        # Idempotency check: skip if already in commit history
        try:
            context = self.service.pcm_core.load_context()
            for entry in context.commit_history:
                if entry.get('commit_id') == commit_id:
                    self.logger.debug(
                        "APPLY_KNOWLEDGE_COMMIT: commit %s already applied locally, ignoring",
                        commit_id[:12]
                    )
                    return None
        except Exception:
            pass  # If context unreadable, proceed to apply attempt

        try:
            commit = KnowledgeCommit.from_dict(payload)
            await self.service.consensus_manager._apply_commit(commit)
            self.logger.info(
                "Applied commit %s via APPLY_KNOWLEDGE_COMMIT recovery path (sent by %s)",
                commit_id[:12], sender_node_id[:20]
            )
        except Exception as e:
            self.logger.error(
                "Failed to apply commit %s via recovery path: %s",
                commit_id[:12], e, exc_info=True
            )

        return None
