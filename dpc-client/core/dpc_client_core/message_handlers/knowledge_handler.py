"""Handlers for knowledge commit and context update commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class ContextUpdatedHandler(MessageHandler):
    """Handles CONTEXT_UPDATED messages (peer notifying of context changes)."""

    def __init__(self, service):
        super().__init__(service)
        # Deduplicate UI broadcasts: only emit peer_context_updated when hash actually changes
        self._last_broadcast_hash: Dict[str, str] = {}

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

        # Broadcast event to UI so "Updated" badge appears, but deduplicate:
        # if we already broadcast this exact hash for this peer, skip to avoid flooding
        # (can happen with leaked connections, reconnects, or relay fanout)
        if context_hash != self._last_broadcast_hash.get(sender_node_id):
            self._last_broadcast_hash[sender_node_id] = context_hash
            await self.service.local_api.broadcast_event("peer_context_updated", {
                "node_id": sender_node_id,
                "context_hash": context_hash
            })
        else:
            self.logger.debug(
                "Skipping duplicate peer_context_updated broadcast for %s (same hash)",
                sender_node_id[:20]
            )

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
        accepted = await self.service.consensus_manager.handle_proposal_message(
            sender_node_id, payload
        )
        # A proposal this node refused is not passed on in its name either.
        if not accepted:
            return None

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
        proposal_id = payload.get("proposal_id", "")
        voter_node_id, verdict = self._authenticate_voter(sender_node_id, payload)

        if verdict == "rejected":
            self.logger.warning(
                "Discarding VOTE_KNOWLEDGE_COMMIT relayed by %s: signature does not hold",
                sender_node_id[:20],
            )
            return None

        # Same policy as VOTE_NEW_SESSION, and for the same reason: a vote on a
        # commit becomes a signature on that commit, so crediting it to whoever
        # carried it would put the wrong node's name inside the record.
        if verdict in ("verified", "legacy"):
            await self.service.consensus_manager.handle_vote_message(
                sender_node_id, payload, voter_node_id=voter_node_id
            )
        else:
            self.logger.info(
                "Not counting %s knowledge vote from %s on %s — relaying it on",
                verdict, str(voter_node_id)[:20], str(proposal_id)[:8]
            )

        # Relay to group members that can't reach the voter directly (star topology)
        session = self.service.consensus_manager.sessions.get(proposal_id)
        conversation_id = (
            payload.get("conversation_id")
            or (session.proposal.conversation_id if session else "")
        )
        if conversation_id and conversation_id.startswith("group-"):
            dedup_key = f"kv:{proposal_id}:{voter_node_id}"
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
        if session and not self.service.consensus_manager.record_peer_result(
            session, sender_node_id, status
        ):
            return None

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


# The peers with an owner card up for an unjudged commit: one card per sender at a time,
# so a peer cannot stack cards by retransmitting (ADR-053 point 3).
_COMMIT_CARDS_PENDING: set = set()


# How many entries an owner card for a peer's unvoted commit lists, and how much of each.
COMMIT_CARD_ENTRIES = 5
COMMIT_CARD_ENTRY_CHARS = 120


def commit_entries_preview(entries: Any) -> str:
    """The first line of each of a commit's first entries, bounded, for the owner's card;
    "" for none. Entries past the bound are counted, not listed."""
    entries = list(entries or [])
    lines = []
    for entry in entries[:COMMIT_CARD_ENTRIES]:
        content = entry.get("content") if isinstance(entry, dict) else getattr(entry, "content", "")
        text = str(content or "").strip()
        first = text.splitlines()[0].strip() if text else ""
        if not first:
            continue
        if len(first) > COMMIT_CARD_ENTRY_CHARS:
            first = first[:COMMIT_CARD_ENTRY_CHARS - 1].rstrip() + "…"
        lines.append(f"- {first}")
    more = len(entries) - COMMIT_CARD_ENTRIES
    if more > 0:
        lines.append(f"(and {more} more)")
    return ("\nEntries:\n" + "\n".join(lines)) if lines else ""


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

    async def _owner_approves_unjudged_commit(self, commit: Any, sender_node_id: str) -> bool:
        """Ask the owner before applying a commit this node never voted on (ADR-053 point 3).

        The card is the schedule card's machinery, as the agents' owner cards are: it names
        the sending node, the chat, the topic and the summary, waits 60 s, and refuses at
        once when no interface client could show it. The wait runs on a worker thread, so
        the service loop keeps serving."""
        import asyncio
        import functools
        import pathlib
        from types import SimpleNamespace
        from ..dpc_agent.agent import ASKER_UNKNOWN, Asker
        from ..dpc_agent.owner_approval import clip
        from ..dpc_agent.tools.core import _await_schedule_approval

        commit_id = (getattr(commit, "commit_id", "") or "")[:12]
        if sender_node_id in _COMMIT_CARDS_PENDING:
            self.logger.warning(
                "APPLY_KNOWLEDGE_COMMIT %s from %s not applied: a card for this node's earlier "
                "commit is still waiting for the owner", commit_id, sender_node_id[:20])
            return False
        peer_name = ""
        name_of = getattr(self.service, "_peer_name", None)
        if callable(name_of):
            try:
                peer_name = name_of(sender_node_id) or ""
            except Exception:
                peer_name = ""
        peer_name = peer_name or sender_node_id[:16]
        loop = asyncio.get_running_loop()
        ctx = SimpleNamespace(
            dpc_service=self.service, _event_loop=loop,
            agent_root=pathlib.Path("knowledge"),
            _agent=SimpleNamespace(display_name=f"{peer_name}'s node", _run_gate=None),
            reply_telegram_chat_id="", conversation_id=getattr(commit, "conversation_id", "") or "",
            conversation_title="", current_task_id=None, turn_stop_event=None,
            asker=Asker(ASKER_UNKNOWN, peer_name, sender_node_id, is_owner=False, verified=True),
        )
        topic = getattr(commit, "topic", "") or "(no topic)"
        summary = getattr(commit, "summary", "") or ""
        headline = f"topic {topic}" + (f": {summary}" if summary else "") + f" (commit {commit_id})"
        # What would be written, not only its title (Zcode #154): the first line of each
        # entry, bounded, so the owner decides on the content.
        details = clip(headline + commit_entries_preview(getattr(commit, "entries", None)))
        _COMMIT_CARDS_PENDING.add(sender_node_id)
        try:
            approved, reason = await loop.run_in_executor(None, functools.partial(
                _await_schedule_approval, ctx, task_type="knowledge_commit", when="now",
                about=f"{peer_name} sent a knowledge commit this node did not vote on: {headline}",
                card={"tool": "APPLY_KNOWLEDGE_COMMIT", "details": details}))
        except Exception as e:
            approved, reason = False, f"the card could not be shown: {e}"
        finally:
            _COMMIT_CARDS_PENDING.discard(sender_node_id)
        if not approved:
            self.logger.warning(
                "APPLY_KNOWLEDGE_COMMIT %s from %s not applied: this node did not vote on it and "
                "the owner did not approve it (%s)", commit_id, sender_node_id[:20], reason)
        return bool(approved)

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

            # Only a commit of a chat this node shares with the sender, and one
            # that names this node, is ours to apply: the retransmit goes from
            # the proposer to a participant that never ACKed.
            me = self.service.p2p_manager.node_id
            resolve = getattr(self.service.consensus_manager, "peer_proposal_roster", None)
            roster = resolve(commit.conversation_id or "", sender_node_id) if resolve else None
            if (
                not roster or me not in roster or sender_node_id not in roster
                or me not in (commit.participants or ())
            ):
                self.logger.warning(
                    "Refused APPLY_KNOWLEDGE_COMMIT %s from %s for %s: not a chat this "
                    "node shares with the sender, or a commit that does not name this node",
                    commit_id[:12], sender_node_id, commit.conversation_id,
                )
                return None

            provenance = commit.verify_provenance()

            if provenance.is_rejected:
                self.logger.warning(
                    "Refused APPLY_KNOWLEDGE_COMMIT %s from %s: %s",
                    commit_id[:12], sender_node_id[:20], provenance.detail
                )
                return None

            for signer in provenance.unverifiable_signers:
                await self._ask_for_certificate(signer, sender_node_id)

            knowledge_service = getattr(self.service, "knowledge_service", None)
            judged_here = bool(
                knowledge_service
                and knowledge_service.judged_proposal(getattr(commit, "proposal_id", "") or "")
            )
            # ADR-053 point 3: a commit this node never voted on is applied only after the
            # owner says so; a refusal, no answer or nobody to ask leaves it unapplied, and
            # the peer may send it again.
            if not judged_here and not await self._owner_approves_unjudged_commit(
                    commit, sender_node_id):
                return None
            await self.service.consensus_manager._apply_commit(
                commit, origin=provenance.verdict, judged_here=judged_here
            )
            say = self.logger.info if provenance.verdict == "verified" else self.logger.warning
            say(
                "Applied commit %s from %s as %s — %s",
                commit_id[:12], sender_node_id[:20], provenance.verdict, provenance.detail
            )
        except Exception as e:
            self.logger.error(
                "Failed to apply commit %s via recovery path: %s",
                commit_id[:12], e, exc_info=True
            )

        return None
