"""
KnowledgeService — PCM/consensus lifecycle and conversation knowledge management.

Extracted from service.py as part of the Grand Refactoring (Phase 1b).
See docs/decisions/001-service-split.md for rationale.

Responsibilities:
- PCMCore wrapper (personal context load/save)
- ConsensusManager lifecycle and all consensus callbacks
- Conversation monitor creation and management
- Knowledge commit proposal, voting, and revision flows
- Auto-knowledge-detection toggle
- AI agent voting and proposal evaluation
- Broadcasting commit results and context-updated events to peers

NOT in scope (stays in service.py or other handlers):
- get_conversation_history — mixed concern (voice transcriptions + agent history)
- _resolve_agent_token_limit — generic agent helper
- send_ai_query — top-level inference orchestration (injected as callable)
- _broadcast_to_peers / _broadcast_to_group — general P2P helpers (injected)
- _compute_context_hash — uses CoreService device_context (injected as callable)
"""

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .consensus_manager import ConsensusManager
from .conversation_monitor import ConversationMonitor, Message as ConvMessage
from dpc_protocol.pcm_core import PCMCore

logger = logging.getLogger(__name__)

NODE_KEY = "node.key"

# Slice of the ten-minute voting deadline a deferred vote spends waiting for the
# records it asked for, before the person is told nobody answered.
PENDING_VOTE_TIMEOUT_SECONDS = 90


class KnowledgeService:
    """Manages personal knowledge, consensus voting, and conversation monitoring.

    Owns:
    - pcm_core (PCMCore) — personal context model access
    - consensus_manager (ConsensusManager) — multi-party knowledge voting
    - conversation_monitors (shared dict ref) — per-conversation monitor registry


    Injected dependencies:
    - llm_manager, local_api, p2p_manager, settings — shared service references
    - conversation_monitors, peer_metadata — shared dicts (CoreService owns)
    - group_manager, instruction_set — structural references
    - send_ai_query, broadcast_to_peers, broadcast_to_group, compute_context_hash — callables
    """

    def __init__(
        self,
        pcm_core: PCMCore,
        llm_manager,
        local_api,
        p2p_manager,
        settings,
        dpc_home_dir: Path,
        conversation_monitors: Dict,
        peer_metadata: Dict,
        group_manager,
        instruction_set,
        *,
        firewall=None,
        send_ai_query: Callable,
        broadcast_to_peers: Callable,
        broadcast_to_group: Callable,
        compute_context_hash: Callable,
        history_requests=None,
    ):
        # Owned state
        self.pcm_core = pcm_core
        self.consensus_manager = ConsensusManager(
            node_id=p2p_manager.node_id,
            pcm_core=self.pcm_core,
            vote_timeout_minutes=10,
        )
        # Shared references (same dict objects as CoreService)
        self.conversation_monitors = conversation_monitors
        self.peer_metadata = peer_metadata

        # Injected dependencies
        self.llm_manager = llm_manager
        self.local_api = local_api
        self.p2p_manager = p2p_manager
        self.settings = settings
        self.dpc_home_dir = dpc_home_dir
        self.group_manager = group_manager
        self.instruction_set = instruction_set
        # Needed to honour human_knowledge_access when a commit is indexed into the
        # agents. Nothing set it before, so the gate below read None and never fired:
        # `L6 reindex skipped` has zero occurrences in any log against 175 reindexes.
        self.firewall = firewall
        self._send_ai_query = send_ai_query
        self._broadcast_to_peers_func = broadcast_to_peers
        self._broadcast_to_group_func = broadcast_to_group
        self._compute_context_hash = compute_context_hash
        # The register of history requests this node has made. A response is
        # only merged if it answers one of them, so a vote that asks for the
        # records it is missing has to announce the request through it.
        self.history_requests = history_requests

        # proposal_id -> a vote that could not be cast because this history
        # does not hold the messages the proposal was extracted from, plus the
        # request sent for exactly those messages. See _defer_vote_for_missing_records.
        self._pending_votes: Dict[str, Dict[str, Any]] = {}

        # proposal_id -> did this node judge the text, or only abstain? Read by
        # the apply path, which signs a commit only for a proposal it judged.
        self._judged_proposals: Dict[str, bool] = {}

        # Results cache for agent store-and-poll (proposal_id → result dict)
        self.pending_results: Dict[str, Dict] = {}

        # Register all consensus callbacks on the manager we own
        self.consensus_manager.on_commit_applied = self._on_commit_applied
        self.consensus_manager.on_commit_signed = self._on_commit_signed
        self.consensus_manager.on_commit_ack = self._on_commit_ack
        self.consensus_manager.on_commit_apply_failed = self._on_commit_apply_failed
        self.consensus_manager.on_apply_retransmit = self._on_apply_retransmit
        self.consensus_manager.on_proposal_received = self._on_proposal_received_from_peer
        self.consensus_manager.on_result_broadcast = self._broadcast_commit_result
        self.consensus_manager.on_commit_revision_needed = self._on_commit_revision_needed
        self.consensus_manager.on_vote_received = self._on_vote_received
        self.consensus_manager.on_commit_approved = self._on_commit_approved
        self.consensus_manager.on_commit_rejected = self._on_commit_rejected

    # ─────────────────────────────────────────────────────────────
    # Runtime introspection
    # ─────────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Agent-readable snapshot of current knowledge service state."""
        active_sessions = len(getattr(self.consensus_manager, 'sessions', {}))
        return {
            "active_consensus_sessions": active_sessions,
            "conversation_monitors": len(self.conversation_monitors),
        }

    # ─────────────────────────────────────────────────────────────
    # P2P broadcast helpers (thin wrappers around injected callables)
    # ─────────────────────────────────────────────────────────────

    async def _broadcast_to_peers(self, message: Dict[str, Any]) -> None:
        await self._broadcast_to_peers_func(message)

    async def _broadcast_to_group(self, group_id: str, message: Dict[str, Any]) -> None:
        await self._broadcast_to_group_func(group_id, message)

    async def _broadcast_context_updated_to_peers(self, context_hash: str) -> None:
        """Broadcast CONTEXT_UPDATED to all connected peers after a commit is applied."""
        connected_peers = list(self.p2p_manager.peers.keys())
        if not connected_peers:
            logger.debug("No connected peers to notify of context update")
            return
        logger.info("Broadcasting CONTEXT_UPDATED to %d peer(s)", len(connected_peers))
        for peer_id in connected_peers:
            try:
                message = {
                    "command": "CONTEXT_UPDATED",
                    "payload": {
                        "node_id": self.p2p_manager.node_id,
                        "context_hash": context_hash,
                    },
                }
                await self.p2p_manager.send_message_to_peer(peer_id, message)
                logger.debug("Notified %s of context update", peer_id[:20])
            except Exception as e:
                logger.error("Error notifying %s of context update: %s", peer_id[:20], e, exc_info=True)

    # ─────────────────────────────────────────────────────────────
    # Personal context CRUD
    # ─────────────────────────────────────────────────────────────

    async def get_personal_context(self) -> Dict[str, Any]:
        """Load and return personal context for UI display."""
        try:
            import hashlib
            from dataclasses import asdict
            from dpc_protocol.markdown_manager import MarkdownKnowledgeManager

            context = self.pcm_core.load_context()
            markdown_manager = MarkdownKnowledgeManager()

            for topic_name, topic in context.knowledge.items():
                if topic.markdown_file:
                    filepath = self.dpc_home_dir / topic.markdown_file
                    if filepath.exists():
                        frontmatter, content = markdown_manager.parse_markdown_with_frontmatter(filepath)
                        if 'content_hash' in frontmatter:
                            actual_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
                            # str(): a 16-hex-digit hash that happens to contain no
                            # letters is valid YAML for an integer, and the parser
                            # obliges — so two of 265 commits compared str against int
                            # and "mismatched" against their own unchanged content.
                            if actual_hash != str(frontmatter['content_hash']):
                                logger.warning("Content hash mismatch for %s: %s", topic_name, frontmatter['commit_id'])
                        entries = markdown_manager.markdown_to_entries(content)
                        topic.entries = entries
                    else:
                        logger.warning("Markdown file not found: %s", topic.markdown_file)

            return {"status": "success", "context": asdict(context)}
        except Exception as e:
            logger.error("Error loading personal context: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_personal_context(self, context_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Save updated personal context from UI editor."""
        try:
            from dpc_protocol.pcm_core import PersonalContext
            from datetime import datetime, timezone

            current = self.pcm_core.load_context()
            if isinstance(current, dict):
                current = PersonalContext.from_dict(current)

            if "profile" in context_dict:
                current.profile.__dict__.update(context_dict["profile"])

            if isinstance(current.metadata, dict):
                current.metadata['last_updated'] = datetime.now(timezone.utc).isoformat()
            else:
                current.metadata.last_updated = datetime.now(timezone.utc).isoformat()

            self.pcm_core.save_context(current)

            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                self.p2p_manager.local_context = current
                if current.profile and current.profile.name:
                    self.p2p_manager.set_display_name(current.profile.name)
                    logger.info("Notifying connected peers of name change")
                    await self._notify_peers_of_name_change(current.profile.name)

            new_context_hash = self._compute_context_hash()
            await self.local_api.broadcast_event("personal_context_updated", {
                "message": "Personal context saved successfully",
                "context_hash": new_context_hash
            })

            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                await self._broadcast_context_updated_to_peers(new_context_hash)

            return {"status": "success", "message": "Personal context saved successfully"}
        except Exception as e:
            logger.error("Error saving personal context: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def reload_personal_context(self) -> Dict[str, Any]:
        """Reload personal context from disk."""
        try:
            from dataclasses import asdict

            context = self.pcm_core.load_context()

            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                self.p2p_manager.local_context = context
                if context.profile and context.profile.name:
                    self.p2p_manager.set_display_name(context.profile.name)
                    logger.info("Notifying connected peers of name change")
                    await self._notify_peers_of_name_change(context.profile.name)

            await self.local_api.broadcast_event("personal_context_reloaded", {
                "context": asdict(context)
            })

            return {
                "status": "success",
                "message": "Personal context reloaded from disk",
                "context": asdict(context)
            }
        except Exception as e:
            logger.error("Error reloading personal context: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _notify_peers_of_name_change(self, new_name: str):
        """Notify all connected peers of name change via HELLO message."""
        from dpc_protocol.protocol import create_hello_message

        connected_peers = list(self.p2p_manager.peers.keys())
        if not connected_peers:
            logger.debug("No connected peers to notify")
            return
        for peer_id in connected_peers:
            try:
                hello_msg = create_hello_message(self.p2p_manager.node_id, new_name)
                await self.p2p_manager.send_message_to_peer(peer_id, hello_msg)
                logger.debug("Notified %s of name change: %s", peer_id, new_name)
            except Exception as e:
                logger.error("Error notifying %s of name change: %s", peer_id, e, exc_info=True)

    # ─────────────────────────────────────────────────────────────
    # Telegram bridge helper
    # ─────────────────────────────────────────────────────────────

    def _get_agent_telegram_bridge(self, conversation_id: str):
        """Return the AgentTelegramBridge for an agent conversation, or None."""
        from .managers.agent_telegram_bridge import get_agent_telegram_bridge

        return get_agent_telegram_bridge(self.llm_manager, conversation_id)

    # ─────────────────────────────────────────────────────────────
    # Conversation monitor management
    # ─────────────────────────────────────────────────────────────

    def _build_participants(self, conversation_id: str) -> List[Dict[str, Any]]:
        """Who is in this conversation, as of now.

        Consensus counts votes against this list, so it has to describe the
        conversation at the moment it is asked — not at the moment the monitor
        happened to be constructed.
        """
        if conversation_id == "local_ai" or conversation_id.startswith("ai_"):
            return [
                {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"},
                {"node_id": conversation_id, "name": "DPC Agent", "context": "ai_agent"},
            ]

        if conversation_id.startswith("group-"):
            group = self.group_manager.get_group(conversation_id)
            if not group:
                return [
                    {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"}
                ]
            participants = []
            for member_id in group.members:
                if member_id == self.p2p_manager.node_id:
                    participants.append(
                        {"node_id": member_id, "name": "User", "context": "local"}
                    )
                else:
                    participants.append({
                        "node_id": member_id,
                        "name": self.peer_metadata.get(member_id, {}).get("name", member_id),
                        "context": "peer",
                    })
            return participants

        return [
            {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"},
            {
                "node_id": conversation_id,
                "name": self.peer_metadata.get(conversation_id, {}).get("name", conversation_id),
                "context": "peer",
            },
        ]

    def _announce_history_after_repair(self, group_id: str) -> None:
        """Tell connected members that this node's copy of a group just changed.

        Fires only when a consolidation actually recovered messages, so a node
        with nothing to repair stays silent. The peers answer with their own
        status, the existing author-digest comparison runs, and whatever either
        side is missing is fetched — the same path a reconnect would take, just
        without waiting for one.
        """
        monitor = self.conversation_monitors.get(group_id)
        summary = getattr(monitor, "last_consolidation", None) or {}
        if not summary.get("messages_added"):
            return

        group = self.group_manager.get_group(group_id)
        members = [m for m in (group.members if group else []) if m != self.p2p_manager.node_id]
        peers = [m for m in members if m in getattr(self.p2p_manager, "peers", {})]
        if not peers:
            logger.info(
                "Group %s repaired (%d message(s) recovered); no member connected to tell yet",
                group_id, summary["messages_added"],
            )
            return

        payload = {
            "group_id": group_id,
            "history_hash": monitor.compute_history_hash(),
            "message_count": len(monitor.message_history),
            "history_digest": monitor.history_digest(),
        }

        async def _send():
            for peer_id in peers:
                try:
                    await self.p2p_manager.send_message_to_peer(peer_id, {
                        "command": "GROUP_HISTORY_STATUS",
                        "payload": payload,
                    })
                except Exception as exc:
                    logger.debug("Could not announce repaired history to %s: %s", peer_id[:20], exc)

        try:
            asyncio.get_running_loop().create_task(_send())
            logger.info(
                "Group %s repaired (%d message(s) recovered); announced to %d connected member(s)",
                group_id, summary["messages_added"], len(peers),
            )
        except RuntimeError:
            # No loop here — the reconnect exchange will carry it instead.
            logger.info(
                "Group %s repaired (%d message(s) recovered); no event loop to announce on",
                group_id, summary["messages_added"],
            )

    def _get_or_create_conversation_monitor(
        self,
        conversation_id: str,
        instruction_set_name: str = None,
    ) -> ConversationMonitor:
        """Get or create a conversation monitor for a conversation/peer.

        Args:
            conversation_id: Identifier for the conversation (peer node_id or "local_ai")
            instruction_set_name: Optional instruction set name (defaults to instruction_set.default)

        Returns:
            ConversationMonitor instance
        """
        if conversation_id in self.conversation_monitors:
            monitor = self.conversation_monitors[conversation_id]
            # A group roster changes after the monitor exists — someone is
            # invited, someone leaves — but the list built at construction is
            # what consensus counts votes against. A monitor born before the
            # second node joined made every proposal claim one participant, so
            # the proposer's own vote was unanimity and the other person's
            # arrived too late to count. Re-read it; it is a dict lookup.
            if conversation_id.startswith("group-"):
                monitor.participants = self._build_participants(conversation_id)
            return monitor

        participants = self._build_participants(conversation_id)
        group = (
            self.group_manager.get_group(conversation_id)
            if conversation_id.startswith("group-")
            else None
        )

        # Determine display_name for readable folder suffix
        if conversation_id.startswith("group-"):
            display_name = group.name if group else None
        elif not conversation_id.startswith(("local_ai", "ai_", "agent-")):
            display_name = self.peer_metadata.get(conversation_id, {}).get("name") or None
        else:
            display_name = None

        self.conversation_monitors[conversation_id] = ConversationMonitor(
            conversation_id=conversation_id,
            participants=participants,
            llm_manager=self.llm_manager,
            knowledge_threshold=0.7,
            settings=self.settings,
            ai_query_func=self._send_ai_query,
            auto_detect=False,
            instruction_set_name=instruction_set_name or self.instruction_set.default,
            display_name=display_name,
        )

        # Load persisted history from disk — only for group chats
        if conversation_id.startswith("group-"):
            if self.conversation_monitors[conversation_id].load_history():
                self.conversation_monitors[conversation_id].rebuild_extraction_buffers_from_history()
                logger.info(
                    "Loaded persisted history for group %s (%d messages, extraction buffers rebuilt)",
                    conversation_id,
                    len(self.conversation_monitors[conversation_id].message_history),
                )
            # Loading may have folded a split store back together. Peers learn
            # about it now rather than at the next reconnect, which on a pair
            # that stays connected for days is the difference between "syncs
            # automatically" and "syncs when something else happens".
            self._announce_history_after_repair(conversation_id)
            # Set token_limit to max context window among agents in the group
            if group and self.llm_manager:
                max_ctx = 0
                node_id = getattr(self.p2p_manager, "node_id", None)
                from .service import EXTERNAL_AGENT_PREFIX
                for aid in group.agents.get(node_id, []):
                    if aid.startswith(EXTERNAL_AGENT_PREFIX):
                        continue
                    try:
                        from .dpc_agent.utils import load_agent_config
                        cfg = load_agent_config(aid) or {}
                        ctx = cfg.get("context_window", 0)
                        if not ctx:
                            pa = cfg.get("provider_alias")
                            if pa and pa in self.llm_manager.providers:
                                model = self.llm_manager.providers[pa].model
                                ctx = self.llm_manager.get_context_window(model) or 0
                        max_ctx = max(max_ctx, ctx)
                    except Exception:
                        pass
                if not max_ctx:
                    model = self.llm_manager.get_active_model_name()
                    max_ctx = self.llm_manager.get_context_window(model) or 0
                if max_ctx > 0:
                    self.conversation_monitors[conversation_id].set_token_limit(max_ctx)
                    logger.info("Group %s token_limit set to %d (max agent context)", conversation_id, max_ctx)

        logger.info(
            "Created conversation monitor for %s with %d participant(s) "
            "(instruction_set=%s)",
            conversation_id,
            len(participants),
            instruction_set_name or self.instruction_set.default,
        )

        return self.conversation_monitors[conversation_id]

    # ─────────────────────────────────────────────────────────────
    # Knowledge commit voting flow
    # ─────────────────────────────────────────────────────────────

    def _history_drift(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Refuse a vote cast on a different conversation than the proposal read.

        The proposal names every message the extraction read, by `content_hash`.
        A voter that holds all of them is judging the same text; one that is
        missing any of them would be approving knowledge drawn from something
        it has never seen.

        The check used to compare `(msg_index, chain_hash)` and refused every
        remote vote, because `chain_hash` is local by construction (ADR-037):
        it covers `role`, which differs per reader. Measured across the three
        nodes on 2026-08-07 — same messages, three chains, and both peers were
        refused at index 3. A check that exists and always says no is worse
        than none, because it looks like agreement.

        Returns an error payload when messages are missing, None otherwise
        (including when there is nothing to compare, which is not a mismatch).
        """
        session = self.consensus_manager.sessions.get(proposal_id)
        if session is None:
            return None
        window = getattr(session.proposal, "based_on_content_hashes", None)
        if not window:
            return None  # proposer predates the window anchor — nothing to verify

        monitor = self.conversation_monitors.get(session.proposal.conversation_id)
        if monitor is None:
            return None

        held = {
            m.get("content_hash") for m in monitor.message_history if m.get("content_hash")
        }
        missing = [h for h in window if h not in held]
        if not missing:
            return None

        logger.warning(
            "Vote on %s held back: %d of %d messages in the extraction window are "
            "missing from this history (%s%s)",
            proposal_id, len(missing), len(window),
            ", ".join(h[:12] for h in missing[:5]),
            ", ..." if len(missing) > 5 else "",
        )
        return {
            "status": "error",
            "reason": "history_drift",
            "missing_messages": len(missing),
            "missing_hashes": missing,
            "window_size": len(window),
            "message": (
                f"This proposal was extracted from messages you do not have "
                f"({len(missing)} of {len(window)} are missing here). Voting on it "
                f"would approve text you are not looking at."
            ),
        }

    async def vote_knowledge_commit(
        self,
        proposal_id: str,
        vote: str,
        comment: str = None,
        entries: list = None,
        summary: str = None,
        _allow_defer: bool = True,
    ) -> Dict[str, Any]:
        """Cast vote on a knowledge commit proposal.

        UI Integration: Called when user clicks approve/reject/request_changes
        in KnowledgeCommitDialog component.

        If entries/summary are provided (from UI edit mode), the proposal is
        updated before the vote is cast so the commit reflects user edits.
        """
        try:
            is_ai_chat = False
            ai_agent_node_id = None
            if proposal_id in self.consensus_manager.sessions:
                session = self.consensus_manager.sessions[proposal_id]
                conversation_id = session.proposal.conversation_id
                if conversation_id == "local_ai" or conversation_id.startswith("ai_"):
                    is_ai_chat = True
                    ai_agent_node_id = conversation_id
                elif conversation_id.startswith("agent_"):
                    is_ai_chat = True
                    ai_agent_node_id = conversation_id
                    dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                    if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
                        agent_mgr = dpc_agent_provider._managers.get(conversation_id)
                        if agent_mgr and agent_mgr.agent_id:
                            ai_agent_node_id = agent_mgr.agent_id

            broadcast_func = self._broadcast_to_peers
            if is_ai_chat:
                async def _no_op_broadcast(message: Dict[str, Any]) -> None:
                    pass
                broadcast_func = _no_op_broadcast

            # Apply user edits from UI edit mode before casting the vote.
            # This ensures the commit contains user-modified entries/summary.
            if proposal_id in self.consensus_manager.sessions:
                session = self.consensus_manager.sessions[proposal_id]
                if entries is not None:
                    from dpc_protocol.pcm_core import KnowledgeEntry as KE, KnowledgeSource as KS
                    rebuilt = []
                    for e in entries:
                        if isinstance(e, dict):
                            src = e.get("source", {})
                            if isinstance(src, dict):
                                src = KS(**{k: v for k, v in src.items() if k in KS.__dataclass_fields__})
                            rebuilt.append(KE(
                                content=e.get("content", ""),
                                tags=e.get("tags", []),
                                source=src,
                                confidence=e.get("confidence", 1.0),
                                alternative_viewpoints=e.get("alternative_viewpoints", []),
                                edited_by=e.get("edited_by"),
                                edited_at=e.get("edited_at"),
                            ))
                        else:
                            rebuilt.append(e)
                    session.proposal.entries = rebuilt
                if summary is not None:
                    session.proposal.summary = summary


            # Only a vote that asserts something about the text needs the text.
            # A refusal is «I do not sign this»; an abstention is «I cannot
            # judge this», which is the very state the guard detects.
            drift = None if vote in ("reject", "abstain") else self._history_drift(proposal_id)
            if drift:
                if _allow_defer:
                    return await self._defer_vote_for_missing_records(
                        proposal_id, drift, vote, comment
                    )
                return drift

            success = await self.consensus_manager.cast_vote(
                proposal_id=proposal_id,
                vote=vote,
                comment=comment,
                broadcast_func=broadcast_func,
            )

            if success and is_ai_chat and not conversation_id.startswith("agent_"):
                # ADR-009: agent chat = solo voting (Mike only).
                # Agent is not a voter — same as "AI provider is not a voter" in local_ai.
                logger.info(
                    "User voted on AI chat proposal %s, triggering AI evaluation",
                    proposal_id,
                )
                asyncio.create_task(
                    self._ai_agent_vote_on_proposal(proposal_id, ai_agent_node_id)
                )

            if success:
                # Whether this node judged the text decides whether it signs the
                # commit later: a signature is a judgement, not a receipt.
                self._judged_proposals[proposal_id] = vote != "abstain"
                return {"status": "success", "message": f"Vote cast: {vote}"}

            # "Not found or expired" was the answer to three different
            # situations, and the common one — the session closed seconds ago
            # because another node's vote already decided it — is the one a
            # person needs named. Reading "expired" about a proposal that is
            # still on screen explains nothing.
            session = self.consensus_manager.sessions.get(proposal_id)
            if session is None:
                return {"status": "error", "message": "Proposal not found"}
            if session.status != "voting":
                return {
                    "status": "error",
                    "reason": "already_decided",
                    "decided_as": session.status,
                    "message": (
                        f"Voting already closed ({session.status}) — your vote "
                        f"was not counted"
                    ),
                }
            return {"status": "error", "message": "Vote could not be cast"}
        except Exception as e:
            logger.error("Error voting on knowledge commit: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # -------------------------------------------------------------
    # A vote held back until the records it judges arrive
    # -------------------------------------------------------------

    def _offline_participants(self, conversation_id: str) -> Dict[str, str]:
        """Members of this group that are not connected right now, by name.

        Empty for anything that is not a group, and for a group of one: a lone
        member is the only voter and is trivially present.
        """
        if not str(conversation_id).startswith("group-"):
            return {}
        group = self.group_manager.get_group(conversation_id) if self.group_manager else None
        if not group:
            return {}
        me = getattr(self.p2p_manager, "node_id", None)
        others = [m for m in (group.members or []) if m != me]
        if not others:
            return {}
        connected = set(getattr(self.p2p_manager, "peers", None) or {})
        return {
            m: (self.peer_metadata.get(m, {}).get("name") or m[:20])
            for m in others
            if m not in connected
        }

    def judged_proposal(self, proposal_id: str) -> bool:
        """Did this node judge that proposal's text, rather than abstain?

        A proposal it never voted on counts as unjudged: silence is not a
        reading either.
        """
        return bool(getattr(self, "_judged_proposals", {}).get(proposal_id, False))

    def _pending_vote_store(self) -> Dict[str, Dict[str, Any]]:
        """The deferred votes, tolerating a service built without __init__."""
        store = getattr(self, "_pending_votes", None)
        if store is None:
            store = {}
            self._pending_votes = store
        return store

    def _peers_to_ask(self, session) -> List[str]:
        """Connected participants of this proposal, ourselves excluded.

        Falls back to every connected peer when the proposal names none we hold
        a connection to: any member of the group may hold the records.
        """
        p2p = getattr(self, "p2p_manager", None)
        if p2p is None:
            return []
        connected = set(getattr(p2p, "peers", None) or {})
        me = getattr(p2p, "node_id", None)
        participants = list(getattr(session.proposal, "participants", None) or [])
        asked = [n for n in participants if n != me and n in connected]
        if asked:
            return asked
        return [n for n in connected if n != me]

    async def _emit_vote_event(self, event: str, payload: Dict[str, Any]) -> None:
        api = getattr(self, "local_api", None)
        if api is None:
            return
        try:
            await api.broadcast_event(event, payload)
        except Exception as e:  # a UI that is not listening must not lose a vote
            logger.debug("Could not broadcast %s: %s", event, e)

    async def _defer_vote_for_missing_records(
        self,
        proposal_id: str,
        drift: Dict[str, Any],
        vote: str,
        comment: Optional[str],
    ) -> Dict[str, Any]:
        """Ask the peers for the messages this vote is missing, and hold it.

        The request names them by `content_hash`, so the answer is the
        extraction window rather than a whole history.
        `GroupHistoryResponseHandler` calls `retry_pending_votes` after every
        answer, including an empty one.

        Returns a `pending` result when the request went out, and the drift
        refusal itself when there is nobody to ask.
        """
        session = self.consensus_manager.sessions.get(proposal_id)
        conversation_id = (
            getattr(session.proposal, "conversation_id", "") if session is not None else ""
        )
        missing = list(drift.get("missing_hashes") or [])
        registry = getattr(self, "history_requests", None)
        peers = self._peers_to_ask(session) if session is not None else []

        if not (missing and peers and registry is not None
                and str(conversation_id).startswith("group-")):
            refused = dict(drift)
            refused["retry"] = "no_peer_to_ask"
            refused["message"] = drift.get("message", "") + (
                " No connected peer could be asked for them."
            )
            return refused

        store = self._pending_vote_store()
        previous = store.get(proposal_id)
        if previous is not None and previous.get("timeout_task") is not None:
            previous["timeout_task"].cancel()

        request_ids: List[str] = []
        asked: List[str] = []
        for peer in peers:
            request_id = uuid.uuid4().hex[:8]
            try:
                registry.note(peer, conversation_id, request_id)
                await self.p2p_manager.send_message_to_peer(peer, {
                    "command": "GROUP_HISTORY_REQUEST",
                    "payload": {
                        "group_id": conversation_id,
                        "content_hashes": missing,
                        "request_id": request_id,
                    },
                })
            except Exception as e:
                logger.warning(
                    "Could not ask %s for %d missing record(s): %s",
                    peer[:20], len(missing), e,
                )
                continue
            request_ids.append(request_id)
            asked.append(peer)

        if not asked:
            refused = dict(drift)
            refused["retry"] = "request_failed"
            return refused

        store[proposal_id] = {
            "proposal_id": proposal_id,
            "conversation_id": conversation_id,
            "vote": vote,
            "comment": comment,
            "missing": missing,
            "asked": asked,
            "request_ids": request_ids,
            "timeout_task": None,
        }
        store[proposal_id]["timeout_task"] = asyncio.create_task(
            self._deferred_vote_timed_out(proposal_id)
        )

        logger.info(
            "Vote %s on %s deferred: asked %d peer(s) for %d missing record(s)",
            vote, proposal_id, len(asked), len(missing),
        )
        held = {
            "proposal_id": proposal_id,
            "conversation_id": conversation_id,
            "vote": vote,
            "missing_messages": len(missing),
            "window_size": drift.get("window_size"),
            "asked_peers": asked,
            "message": (
                f"{len(missing)} of {drift.get('window_size')} messages this proposal "
                f"was read from are missing here. Asked {len(asked)} peer(s) for them; "
                f"your {vote} is cast as soon as they arrive."
            ),
        }
        await self._emit_vote_event("knowledge_vote_deferred", held)
        return dict(held, status="pending", reason="history_drift")

    async def retry_pending_votes(
        self,
        group_id: str,
        rejected: Optional[List[Dict[str, Any]]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        """Re-try the votes deferred on this group after a history answer.

        Args:
            group_id: the conversation the answer belonged to
            rejected: records the merge refused, from `last_merge_rejected`. A
                record that arrived and failed its own signature is a different
                verdict from one that never came, and the person is told which.
            request_id: the request the answer echoed, for the log only
        """
        store = self._pending_vote_store()
        waiting = [pid for pid, p in store.items() if p.get("conversation_id") == group_id]
        if not waiting:
            return

        for proposal_id in waiting:
            pending = store.pop(proposal_id, None)
            if pending is None:
                continue
            task = pending.get("timeout_task")
            if task is not None:
                task.cancel()

            drift = self._history_drift(proposal_id)
            if drift is None:
                logger.info(
                    "Records for %s arrived (request %s), casting the held %s",
                    proposal_id, request_id, pending["vote"],
                )
                result = await self.vote_knowledge_commit(
                    proposal_id,
                    pending["vote"],
                    pending.get("comment"),
                    _allow_defer=False,
                )
                await self._emit_vote_event("knowledge_vote_resolved", {
                    "proposal_id": proposal_id,
                    "conversation_id": group_id,
                    "vote": pending["vote"],
                    "status": result.get("status", "error"),
                    "reason": result.get("reason"),
                    "message": result.get("message", ""),
                })
                continue

            still_missing = set(drift.get("missing_hashes") or [])
            unverifiable = [
                r for r in (rejected or [])
                if r.get("content_hash") in still_missing
            ]
            if unverifiable:
                reason = "unverifiable_record"
                message = (
                    f"{len(unverifiable)} of the missing messages arrived and did not "
                    f"verify, so they were not stored. This proposal cannot be voted on "
                    f"here: it was read from text this node cannot confirm."
                )
            else:
                reason = "records_not_held"
                message = (
                    f"The peer answered without {len(still_missing)} of the messages this "
                    f"proposal was read from. Your {pending['vote']} was not cast."
                )
            logger.warning(
                "Deferred vote on %s becomes an abstention: %s (%d record(s) still missing)",
                proposal_id, reason, len(still_missing),
            )
            await self._abstain_with_reason(proposal_id, group_id, reason, message, extra={
                "missing_messages": len(still_missing),
                "unverifiable": [r.get("content_hash") for r in unverifiable],
            })

    async def _abstain_with_reason(
        self,
        proposal_id: str,
        conversation_id: Optional[str],
        reason: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Say «I cannot judge this» in the vote itself, not only in a log line.

        The records never arrived, or arrived and did not verify. Under a
        denominator counted over participants an abstention is what stops the
        commit, and it carries the reason it stopped it.
        """
        result = await self.vote_knowledge_commit(
            proposal_id, "abstain", message, _allow_defer=False
        )
        payload = {
            "proposal_id": proposal_id,
            "conversation_id": conversation_id,
            "vote": "abstain",
            "status": "error",
            "reason": reason,
            "message": message,
        }
        payload.update(extra or {})
        if result.get("status") != "success":
            payload["abstention_failed"] = result.get("message", "")
            logger.warning(
                "Could not record the abstention on %s: %s",
                proposal_id, result.get("message", ""),
            )
        await self._emit_vote_event("knowledge_vote_resolved", payload)

    async def _deferred_vote_timed_out(self, proposal_id: str) -> None:
        """Nobody answered in time: say so instead of holding the vote forever."""
        try:
            await asyncio.sleep(PENDING_VOTE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        pending = self._pending_vote_store().pop(proposal_id, None)
        if pending is None:
            return
        logger.warning(
            "Deferred vote on %s dropped: no answer from %s within %ds",
            proposal_id, ", ".join(x[:20] for x in pending.get("asked", [])),
            PENDING_VOTE_TIMEOUT_SECONDS,
        )
        await self._abstain_with_reason(
            proposal_id,
            pending.get("conversation_id"),
            "no_answer",
            (
                f"No peer sent the missing messages within "
                f"{PENDING_VOTE_TIMEOUT_SECONDS} seconds, so this node cannot judge "
                f"the proposal and abstains."
            ),
            extra={"missing_messages": len(pending.get("missing") or [])},
        )

    async def _ai_agent_vote_on_proposal(
        self, proposal_id: str, ai_agent_node_id: str
    ) -> None:
        """Have the AI agent evaluate and cast a vote on a knowledge proposal."""
        try:
            if proposal_id not in self.consensus_manager.sessions:
                logger.warning("AI vote: Proposal %s not found", proposal_id)
                return

            session = self.consensus_manager.sessions[proposal_id]
            proposal = session.proposal

            if ai_agent_node_id in session.votes:
                logger.info(
                    "AI agent %s already voted on proposal %s",
                    ai_agent_node_id, proposal_id,
                )
                return

            agent_provider_alias = None
            conversation_history = None
            conv_id = proposal.conversation_id
            if conv_id and conv_id.startswith("agent_"):
                dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
                    _mgr = dpc_agent_provider._managers.get(conv_id)
                    if _mgr:
                        # Resolve underlying LLM provider — NOT "dpc_agent" itself,
                        # which would route through agent.process_message() and pollute
                        # history.json with the voting prompt (#20/#10/#16).
                        _raw_alias = _mgr.config.get("provider_alias")
                        if _raw_alias == "dpc_agent" or not _raw_alias:
                            # Use the agent's actual LLM provider (agent_provider → default)
                            agent_provider_alias = (
                                getattr(self.llm_manager, "agent_provider", None)
                                or self.llm_manager.default_provider
                            )
                        else:
                            agent_provider_alias = _raw_alias
                        _monitor = _mgr._agent_monitors.get(conv_id)
                        if _monitor:
                            conversation_history = _monitor.full_conversation

            ai_decision = await self._evaluate_knowledge_proposal_for_ai_vote(
                proposal,
                provider_alias=agent_provider_alias,
                conversation_history=conversation_history,
            )

            if ai_decision is None:
                logger.warning(
                    "AI agent %s could not evaluate proposal %s — abstaining (no vote cast)",
                    ai_agent_node_id, proposal_id,
                )
                return

            logger.info(
                "AI agent %s voting on proposal %s: %s - %s",
                ai_agent_node_id,
                proposal_id,
                ai_decision["vote"],
                ai_decision.get("comment", ""),
            )

            async def _no_op(msg):
                pass

            original_node_id = self.consensus_manager.node_id
            self.consensus_manager.node_id = ai_agent_node_id
            try:
                await self.consensus_manager.cast_vote(
                    proposal_id=proposal_id,
                    vote=ai_decision["vote"],
                    comment=ai_decision.get("comment"),
                    broadcast_func=_no_op,
                )
            finally:
                self.consensus_manager.node_id = original_node_id

        except Exception as e:
            logger.error("Error in AI agent voting: %s", e, exc_info=True)

    async def _evaluate_knowledge_proposal_for_ai_vote(
        self,
        proposal,
        provider_alias: Optional[str] = None,
        conversation_history: Optional[list] = None,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate a knowledge proposal and return an AI vote decision."""
        try:
            entries_text = ""
            for i, entry in enumerate(proposal.entries, 1):
                entries_text += f"\n{i}. {entry.content} (confidence: {entry.confidence:.2f})"
                if hasattr(entry, 'tags') and entry.tags:
                    entries_text += f" [tags: {', '.join(entry.tags)}]"

            conversation_text = ""
            if conversation_history:
                lines = []
                for msg in conversation_history:
                    if hasattr(msg, 'sender_name'):
                        role = msg.sender_name.upper()
                        content = msg.text
                    else:
                        role = msg.get("role", "unknown").upper()
                        content = msg.get("content", "")
                    lines.append(f"{role}: {content}")
                conversation_text = (
                    "\n\n**Full Conversation (source of the extracted knowledge):**\n"
                    + "\n\n".join(lines)
                )

            prompt = f"""You extracted the following knowledge entries from the conversation below. Now review them and vote on whether they should be saved to the user's personal knowledge base.

**Proposal Topic:** {proposal.topic}
**Summary:** {proposal.summary}
**Average Confidence:** {proposal.avg_confidence:.2f}

**Knowledge Entries:**{entries_text}
{conversation_text}

**Your Task:**
Evaluate each entry against the actual conversation above:
1. **Accuracy**: Does this correctly reflect what was said or concluded in the conversation?
2. **Relevance**: Is this genuinely useful knowledge worth preserving long-term?
3. **Redundancy**: Does this duplicate or contradict existing common knowledge?
4. **Quality**: Is the entry clear, specific, and well-formulated?

**Voting Options:**
- `approve`: Entries accurately reflect the conversation and are worth saving
- `reject`: Entries are factually wrong, misrepresent the conversation, or are harmful
- `request_changes`: Entries have potential but need revision (explain what specifically)

**IMPORTANT:**
- Ground your evaluation in the conversation above — not just general world knowledge
- Personal preferences, technical conclusions, and learned insights are all valid
- If most entries are good but a few misrepresent the conversation, vote request_changes
- Only reject if entries are genuinely wrong or harmful

Respond in JSON format:
{{
    "vote": "approve" | "reject" | "request_changes",
    "comment": "Brief explanation of your decision (1-2 sentences)",
    "entry_feedback": [
        {{"index": 1, "assessment": "good" | "needs_work" | "problematic", "note": "optional note"}}
    ]
}}
"""

            logger.debug(
                "Evaluating knowledge proposal %s with provider=%s",
                proposal.proposal_id, provider_alias or "default",
            )
            response = await self.llm_manager.query(
                prompt=prompt,
                provider_alias=provider_alias,
                max_tokens=500,
                conversation_id=proposal.conversation_id,
            )

            response_text = response
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                decision = json.loads(json_match.group())
            else:
                decision = json.loads(response_text)

            vote = decision.get("vote", "approve")
            comment = decision.get("comment", "")

            entry_feedback = decision.get("entry_feedback", [])
            if entry_feedback and vote == "request_changes":
                feedback_notes = []
                for ef in entry_feedback:
                    if ef.get("assessment") != "good" and ef.get("note"):
                        feedback_notes.append(f"Entry {ef.get('index')}: {ef.get('note')}")
                if feedback_notes:
                    comment += " | " + "; ".join(feedback_notes[:3])

            return {"vote": vote, "comment": comment[:500]}

        except json.JSONDecodeError as e:
            logger.warning(
                "AI vote evaluation returned invalid JSON, agent will abstain: %s", e
            )
            return None
        except Exception as e:
            logger.error(
                "Error in AI vote evaluation, agent will abstain: %s", e, exc_info=True
            )
            return None

    # ─────────────────────────────────────────────────────────────
    # End conversation session + knowledge proposal flow
    # ─────────────────────────────────────────────────────────────

    async def end_conversation_session(
        self,
        conversation_id: str,
        initiated_by: str = "user_request",
    ) -> Dict[str, Any]:
        """Manually end a conversation session and extract knowledge.

        UI Integration: Called when user clicks "End Session & Save Knowledge" button.
        Also called by agent_telegram_bridge for /endsession command.
        """
        try:
            monitor = None
            if conversation_id.startswith("agent_"):
                dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                if dpc_agent_provider and hasattr(dpc_agent_provider, 'get_manager'):
                    try:
                        agent_manager = dpc_agent_provider.get_manager(conversation_id)
                        monitor = agent_manager._get_or_create_agent_monitor(conversation_id)
                    except Exception as _e:
                        logger.warning("Could not get agent manager for %s: %s", conversation_id, _e)
            if monitor is None:
                monitor = self._get_or_create_conversation_monitor(conversation_id)

            # `_extracting` on the monitor clears when the proposal is built,
            # which is minutes before the vote on it closes. A second extraction
            # started in that window produces two proposals over overlapping
            # history, each anchored to a different position — not a stale
            # proposal but two commits arguing over the same conversation.
            open_session = next(
                (
                    s for s in self.consensus_manager.sessions.values()
                    if s.proposal.conversation_id == conversation_id
                    and s.status == "voting"
                ),
                None,
            )
            if open_session is not None:
                logger.info(
                    "Refusing extraction for %s — proposal %s is still being voted on",
                    conversation_id, open_session.proposal.proposal_id,
                )
                return {
                    "status": "error",
                    "reason": "vote_in_progress",
                    "proposal_id": open_session.proposal.proposal_id,
                    "message": (
                        "A knowledge commit for this conversation is still being "
                        "voted on. Finish that vote before extracting again."
                    ),
                }

            # Everyone who has to vote must be able to. Under a denominator
            # counted over participants a missing member cannot be outvoted,
            # only waited for, so the vote would spend its ten minutes and end
            # as a timeout — after the extraction had already been paid for.
            offline = self._offline_participants(conversation_id)
            if offline:
                names = ", ".join(offline.values())
                logger.info(
                    "Refusing extraction for %s — %d participant(s) offline: %s",
                    conversation_id, len(offline), names,
                )
                return {
                    "status": "error",
                    "reason": "participants_offline",
                    "offline": list(offline),
                    "message": (
                        f"All participants must be online to vote on a knowledge "
                        f"commit. Offline: {names}"
                    ),
                }

            logger.info("End Session - attempting manual extraction for %s", conversation_id)
            logger.info(
                "Full conversation: %d messages (incremental buffer: %d), Score: %.2f",
                len(monitor.full_conversation),
                len(monitor.message_buffer),
                monitor.knowledge_score,
            )

            # A peer is not pre-injected here. It used to be, because the
            # resolver returned no host for a group; now the group resolves
            # through its own chain, and writing a peer in first would hand it
            # the primary route — and wipe the provenance while doing it. The
            # peer remains where it belongs: the `except` retry in the monitor.

            monitor.full_conversation = []
            monitor.message_buffer = []
            monitor.rebuild_extraction_buffers_from_history()
            logger.info(
                "Re-sourced extraction from history (%d messages) for %s",
                len(monitor.full_conversation),
                conversation_id,
            )

            proposal = await monitor.generate_commit_proposal(
                force=True,
                proposed_by=self.p2p_manager.node_id,
                initiated_by=initiated_by,
            )

            if proposal:
                logger.info("Knowledge proposal generated for %s", conversation_id)
                logger.info(
                    "Topic: %s, Entries: %d, Confidence: %.2f",
                    proposal.topic, len(proposal.entries), proposal.avg_confidence,
                )

                if len(proposal.entries) == 0:
                    logger.warning(
                        "Skipping empty knowledge proposal for %s "
                        "(0 entries — likely LLM format mismatch, see earlier warning for details)",
                        conversation_id,
                    )
                    await self.local_api.broadcast_event(
                        "knowledge_extraction_failed",
                        {
                            "conversation_id": conversation_id,
                            "reason": "no_entries",
                            "message": "Knowledge extraction returned 0 entries. Try again or check the AI provider.",
                        },
                    )
                    return

                # ADR-009: agent chat = solo voting (Mike only, agent is not a voter).
                # Remove agent from participants so consensus finalizes on Mike's vote alone.
                if conversation_id.startswith("agent_"):
                    user_node_id = self.p2p_manager.node_id
                    proposal.participants = [
                        p for p in proposal.participants if p == user_node_id
                    ]

                await self.local_api.broadcast_event(
                    "knowledge_commit_proposed",
                    proposal.to_dict(),
                )

                if (
                    conversation_id == "local_ai"
                    or conversation_id.startswith("ai_")
                    or conversation_id.startswith("telegram-")
                    or conversation_id.startswith("agent_")
                ):
                    logger.info(
                        "%s - private conversation, knowledge will not be shared with peers",
                        conversation_id,
                    )

                    async def _no_op_broadcast(message: Dict[str, Any]) -> None:
                        pass

                    await self.consensus_manager.propose_commit(
                        proposal=proposal,
                        broadcast_func=_no_op_broadcast,
                    )
                elif conversation_id.startswith("group-"):
                    # Extraction takes a minute or more, and a member can drop
                    # inside it. Opening a vote nobody can finish is the state
                    # this whole path exists to avoid.
                    left_meanwhile = self._offline_participants(conversation_id)
                    if left_meanwhile:
                        names = ", ".join(left_meanwhile.values())
                        logger.warning(
                            "Not opening a vote for %s — %s went offline during the extraction",
                            conversation_id, names,
                        )
                        await self.local_api.broadcast_event(
                            "knowledge_extraction_failed",
                            {
                                "conversation_id": conversation_id,
                                "reason": "participants_offline",
                                "message": (
                                    f"{names} went offline while the knowledge was being "
                                    f"extracted, so the vote was not opened. Try again when "
                                    f"everyone is back."
                                ),
                            },
                        )
                        return {
                            "status": "error",
                            "reason": "participants_offline",
                            "offline": list(left_meanwhile),
                            "message": f"Not opening a vote: {names} went offline",
                        }
                    logger.info(
                        "Group Chat - broadcasting knowledge proposal to group %s for consensus",
                        conversation_id,
                    )

                    async def _group_broadcast(message: Dict[str, Any], _gid=conversation_id) -> None:
                        await self._broadcast_to_group(_gid, message)

                    await self.consensus_manager.propose_commit(
                        proposal=proposal,
                        broadcast_func=_group_broadcast,
                    )
                else:
                    logger.info("Peer Chat - broadcasting knowledge proposal to peers for consensus")
                    await self.consensus_manager.propose_commit(
                        proposal=proposal,
                        broadcast_func=self._broadcast_to_peers,
                    )

                return {
                    "status": "success",
                    "message": "Knowledge proposal created",
                    "proposal_id": proposal.proposal_id,
                }
            else:
                logger.info(
                    "No proposal generated - buffer was empty or no knowledge detected"
                )
                return {
                    "status": "success",
                    "message": "No significant knowledge detected in conversation (buffer may be empty)",
                }
        except Exception as e:
            logger.error("Error ending conversation session: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Consensus callbacks (registered on self.consensus_manager in __init__)
    # ─────────────────────────────────────────────────────────────

    async def _on_proposal_received_from_peer(self, proposal) -> None:
        """Callback when knowledge proposal received from peer. Broadcasts to UI."""
        logger.info(
            "Broadcasting peer proposal to UI: %s (topic: %s)",
            proposal.proposal_id, proposal.topic,
        )
        await self.local_api.broadcast_event(
            "knowledge_commit_proposed",
            proposal.to_dict(),
        )

    async def _on_vote_received(self, vote) -> None:
        """Broadcast a received vote to the UI so the vote panel updates in real time."""
        try:
            await self.local_api.broadcast_event("knowledge_vote_received", {
                "proposal_id": vote.proposal_id,
                "voter_node_id": vote.voter_node_id,
                "voter_name": vote.voter_node_id,
                "vote": vote.vote,
                "comment": vote.comment or "",
                "is_required_dissent": vote.is_required_dissent,
            })
        except Exception as e:
            logger.error("Error in _on_vote_received: %s", e, exc_info=True)

    async def _reindex_commit_into_agents(self, markdown_file: str) -> None:
        """Add an approved commit to the index of each agent allowed the shared layer.

        Lifted out of the approval handler so the gate can be tested. Inline, it sat
        behind everything else that handler does, and the one branch whose whole job is
        to refuse never ran: `L6 reindex skipped` has zero occurrences across every log
        on this machine, against 175 reindexes.

        Async because each agent's mutation now runs on that agent's index writer, and
        this handler runs on the event loop: waiting on the writer here would stop
        every other coroutine for as long as that agent's queue is busy, which can be
        a full rebuild. Awaiting costs the same wall-clock for the commit and nothing
        for anybody else.
        """
        firewall = self.firewall
        commit_path = self.dpc_home_dir / markdown_file
        if not commit_path.exists():
            return
        dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
        if not (dpc_agent_provider and hasattr(dpc_agent_provider, "_managers")):
            return
        for agent_mgr in dpc_agent_provider._managers.values():
            # Fail closed. The previous form was `if firewall and not …`, so a missing
            # firewall disabled the gate instead of the indexing — a permission that
            # silently stops being asked is worse than one that refuses.
            if firewall is None:
                logger.warning("MEM-3.7: L6 reindex skipped for %s — no firewall to ask",
                               agent_mgr.agent_id)
                continue
            if not firewall.can_agent_access_context("knowledge", profile_name=agent_mgr.agent_id):
                logger.info("MEM-3.7: L6 reindex skipped for %s (human_knowledge_access disabled)",
                            agent_mgr.agent_id)
                continue
            agent = agent_mgr._agent
            if not (agent and getattr(agent, "_embedding_provider", None)):
                continue
            index_dir = agent_mgr.agent_root / "state" / "memory_index"
            if not index_dir.exists():
                continue
            from .dpc_agent.index_keys import l6_key
            from .dpc_agent.index_writer import write_index_async
            from .dpc_agent.indexing_pipeline import index_single_file
            from .dpc_agent.retrieval import make_backend_for_agent
            # Same key shape as the full rebuild, from the same helper, so this
            # incremental add replaces the rebuilt entry instead of sitting beside it.
            _l6_key = l6_key(commit_path, self.dpc_home_dir / "knowledge")

            def _add_commit(agent_root=agent_mgr.agent_root, provider=agent._embedding_provider):
                backend = make_backend_for_agent(agent_root)
                if not backend.vector.load():
                    return False
                backend.text.load()
                index_single_file(commit_path, provider, backend,
                                  source_layer="L6", source_file_key=_l6_key)
                backend.save()
                return True

            if await write_index_async(index_dir, _add_commit):
                logger.info("MEM-3.7: reindexed L6 commit %s for agent %s",
                            commit_path.name, agent_mgr.agent_id)
            else:
                logger.warning("MEM-3.7: L6 commit %s not indexed for %s — the index would not load",
                               commit_path.name, agent_mgr.agent_id)

    async def _on_commit_approved(self, commit) -> None:
        """Notify the UI that consensus was reached and the commit was approved."""
        try:
            # Store result for agent store-and-poll (get_proposal_result tool)
            from dpc_protocol.markdown_manager import MarkdownKnowledgeManager
            _mkm = MarkdownKnowledgeManager()
            safe_topic = _mkm.sanitize_filename(commit.topic or "knowledge")
            markdown_file = f"knowledge/{safe_topic}_{commit.commit_id}.md"
            self.pending_results[commit.proposal_id] = {
                "status": "approved",
                "commit_id": commit.commit_id,
                "topic": commit.topic,
                "markdown_file": markdown_file,
            }

            await self.local_api.broadcast_event("knowledge_commit_approved", {
                "commit_id": commit.commit_id,
                "topic": commit.topic,
                "summary": commit.summary,
                "approved_by": commit.approved_by,
                "vote_comments": commit.vote_comments,
            })
            bridge = self._get_agent_telegram_bridge(commit.conversation_id)
            if bridge:
                await bridge.notify_knowledge_result(
                    proposal_id=commit.commit_id,
                    status="approved",
                    topic=commit.topic,
                    vote_comments=commit.vote_comments,
                )
        except Exception as e:
            logger.error("Error in _on_commit_approved: %s", e, exc_info=True)

        # MEM-3.7 trigger #2: incremental reindex for Active Recall (L6)
        try:
            await self._reindex_commit_into_agents(markdown_file)
        except Exception as e:
            logger.warning("MEM-3.7 L6 reindex failed for commit %s: %s", commit.commit_id, e)

    async def _on_commit_rejected(self, proposal, votes: Dict[str, Any]) -> None:
        """Notify the UI that the proposal was rejected, including rejection reasons."""
        try:
            # Store result for agent store-and-poll
            self.pending_results[proposal.proposal_id] = {
                "status": "rejected",
                "commit_id": None,
                "topic": proposal.topic,
                "markdown_file": None,
            }

            rejection_comments = {
                v.voter_node_id: v.comment
                for v in votes.values()
                if v.vote == "reject" and v.comment
            }
            await self.local_api.broadcast_event("knowledge_commit_rejected", {
                "proposal_id": proposal.proposal_id,
                "topic": proposal.topic,
                "summary": proposal.summary,
                "rejected_by": [v.voter_node_id for v in votes.values() if v.vote == "reject"],
                "rejection_comments": rejection_comments,
            })
            bridge = self._get_agent_telegram_bridge(proposal.conversation_id)
            if bridge:
                await bridge.notify_knowledge_result(
                    proposal_id=proposal.proposal_id,
                    status="rejected",
                    topic=proposal.topic,
                    vote_comments=rejection_comments,
                )
        except Exception as e:
            logger.error("Error in _on_commit_rejected: %s", e, exc_info=True)

    async def _on_commit_applied(self, commit) -> None:
        """Reload local context and notify peers after a knowledge commit is applied."""
        try:
            logger.info(
                "Commit Applied - reloading local context after commit %s",
                commit.commit_id,
            )

            # Clear conversation monitor buffer for this conversation
            if commit.conversation_id:
                monitor = self._get_or_create_conversation_monitor(commit.conversation_id)
                if monitor.message_buffer:
                    # Name the buffer. "Clearing buffer for <conversation>" reads as
                    # "your conversation was cleared", and it is not: message_history,
                    # which the chat shows and syncs, is untouched here.
                    logger.info(
                        "Knowledge auto-detect buffer reset for %s after commit approval "
                        "(%d messages; chat history untouched)",
                        commit.conversation_id, len(monitor.message_buffer),
                    )
                    monitor.message_buffer = []
                    monitor.knowledge_score = 0.0

            # Reload context from disk
            context = self.pcm_core.load_context()

            # Update in P2PManager so context requests return latest data
            if self.p2p_manager:
                self.p2p_manager.local_context = context
                logger.info("Updated p2p_manager.local_context with new knowledge")

            # Compute new context hash (delegates to CoreService — uses device_context)
            new_context_hash = self._compute_context_hash()

            # Broadcast CONTEXT_UPDATED to all connected peers
            await self._broadcast_context_updated_to_peers(new_context_hash)

            # Emit event to UI
            await self.local_api.broadcast_event("personal_context_updated", {
                "message": f"Knowledge commit applied: {commit.topic}",
                "context_hash": new_context_hash,
            })

            # Knowledge extraction does NOT reset the conversation.
            # Chat and history remain — user explicitly starts New Session to clear.
            # Counter resets happen only via New Session (propose_new_session flow).
        except Exception as e:
            logger.error("Error in _on_commit_applied: %s", e, exc_info=True)
            import traceback
            traceback.print_exc()

    async def _on_commit_revision_needed(self, proposal, votes: Dict[str, Any]) -> None:
        """Callback when a knowledge commit proposal receives 'request_changes' votes."""
        try:
            change_requests = [
                {
                    "node_id": v.voter_node_id,
                    "comment": v.comment or "",
                    "is_required_dissent": v.is_required_dissent,
                }
                for v in votes.values()
                if v.vote == "request_changes" and v.comment
            ]

            logger.info(
                "Revision needed for proposal %s: %d change request(s)",
                proposal.proposal_id, len(change_requests),
            )
            for cr in change_requests:
                logger.info("  %s: %s", cr["node_id"][:20], cr["comment"][:120])

            await self.local_api.broadcast_event("knowledge_commit_revision_needed", {
                "proposal_id": proposal.proposal_id,
                "topic": proposal.topic,
                "summary": proposal.summary,
                "conversation_id": proposal.conversation_id,
                "change_requests": change_requests,
            })

            bridge = self._get_agent_telegram_bridge(proposal.conversation_id)
            if bridge:
                await bridge.notify_knowledge_result(
                    proposal_id=proposal.proposal_id,
                    status="revision_needed",
                    topic=proposal.topic,
                    change_requests=change_requests,
                )

            conv_id = proposal.conversation_id
            if conv_id and conv_id.startswith("agent_"):
                dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
                    agent_mgr = dpc_agent_provider._managers.get(conv_id)
                    if agent_mgr:
                        asyncio.create_task(
                            self._agent_auto_revise_proposal(agent_mgr, proposal, change_requests)
                        )
        except Exception as e:
            logger.error("Error in _on_commit_revision_needed: %s", e, exc_info=True)

    async def _agent_auto_revise_proposal(
        self, agent_mgr, proposal, change_requests: list
    ) -> None:
        """Ask the originating agent to revise its knowledge proposal based on change requests."""
        try:
            if not change_requests:
                return

            feedback_lines = "\n".join(
                f"- {cr['node_id']}: {cr['comment']}" for cr in change_requests
            )
            revision_prompt = (
                f"Your knowledge commit proposal for topic '{proposal.topic}' received change requests:\n\n"
                f"{feedback_lines}\n\n"
                f"Please revise the proposal entries to address the feedback. "
                f"Reply ONLY with a JSON object:\n"
                f'{{"summary": "...", "entries": [{{"content": "...", "confidence": 0.9, "tags": []}}]}}'
            )

            logger.info(
                "Asking agent %s to revise proposal %s",
                agent_mgr.agent_id, proposal.proposal_id,
            )
            # _skip_history=True: revision_prompt is a synthetic system message,
            # not real user input. Saving it to history.json would pollute Ark's
            # archive readers (archive.py role:user filter) and corrupt
            # introspection (ARCH-9 / S23). Same rationale as the voting bypass
            # at line 397-399 (#20/#10/#16) — synthetic prompts must not appear
            # as user turns in conversation history.
            response = await agent_mgr.process_message(
                message=revision_prompt,
                conversation_id=proposal.conversation_id,
                include_context=False,
                _skip_history=True,
            )

            if not response:
                logger.warning(
                    "Agent returned empty revision response for %s", proposal.proposal_id
                )
                return

            import re as _re
            json_match = _re.search(r'\{.*\}', response, _re.DOTALL)
            if not json_match:
                logger.warning(
                    "Agent revision response contained no JSON: %s", response[:200]
                )
                return

            revision = json.loads(json_match.group())
            updated_summary = revision.get("summary") or None
            raw_entries = revision.get("entries") or []

            if not raw_entries:
                logger.warning("Agent revision produced 0 entries for %s", proposal.proposal_id)
                return

            from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource
            updated_entries = []
            for raw in raw_entries:
                updated_entries.append(KnowledgeEntry(
                    content=raw.get("content", ""),
                    confidence=float(raw.get("confidence", 0.8)),
                    tags=raw.get("tags", []),
                    source=KnowledgeSource(
                        type="ai_summary",
                        conversation_id=proposal.conversation_id,
                    ),
                ))

            async def _no_op_broadcast(msg):
                pass

            ok = await self.consensus_manager.revise_proposal(
                proposal_id=proposal.proposal_id,
                updated_summary=updated_summary,
                updated_entries=updated_entries,
                broadcast_func=_no_op_broadcast,
            )

            if ok:
                logger.info(
                    "Agent %s resubmitted revised proposal %s",
                    agent_mgr.agent_id, proposal.proposal_id,
                )
                await self.local_api.broadcast_event(
                    "knowledge_commit_proposed", proposal.to_dict()
                )
            else:
                logger.warning(
                    "revise_proposal returned False for %s (wrong state?)",
                    proposal.proposal_id,
                )
        except json.JSONDecodeError as e:
            logger.warning(
                "Agent revision JSON parse error for %s: %s", proposal.proposal_id, e
            )
        except Exception as e:
            logger.error("Error in _agent_auto_revise_proposal: %s", e, exc_info=True)

    async def _on_commit_signed(self, commit) -> None:
        """Sign the commit with our private key and broadcast COMMIT_SIGNED to participants."""
        try:
            from dpc_protocol.commit_integrity import CommitSigner
            from cryptography.hazmat.primitives import serialization as _ser

            key_path = self.dpc_home_dir / NODE_KEY
            if not key_path.exists():
                logger.debug("_on_commit_signed: key file not found, skipping")
                return

            with open(key_path, 'rb') as f:
                private_key = _ser.load_pem_private_key(f.read(), password=None)

            signer = CommitSigner(self.p2p_manager.node_id, private_key)
            signature_b64 = signer.sign_commit(commit.commit_hash)

            payload = {
                "commit_id": commit.commit_id,
                "commit_hash": commit.commit_hash,
                "node_id": self.p2p_manager.node_id,
                "signature": signature_b64,
            }

            participants = commit.participants or []
            for peer_id in participants:
                if peer_id == self.p2p_manager.node_id:
                    continue
                try:
                    await self.p2p_manager.send_message_to_peer(
                        peer_id, {"command": "COMMIT_SIGNED", "payload": payload}
                    )
                    logger.debug(
                        "Sent COMMIT_SIGNED to %s for commit %s",
                        peer_id[:20], commit.commit_id[:12],
                    )
                except Exception as e:
                    logger.debug("Could not send COMMIT_SIGNED to %s: %s", peer_id[:20], e)
        except Exception as e:
            logger.error("Error in _on_commit_signed: %s", e, exc_info=True)

    async def _on_commit_ack(self, commit) -> None:
        """Broadcast COMMIT_ACK to all participants confirming this node applied the commit."""
        try:
            participants = commit.participants or []
            payload = {
                "commit_id": commit.commit_id,
                "node_id": self.p2p_manager.node_id,
                "participants": participants,
            }

            for peer_id in participants:
                if peer_id == self.p2p_manager.node_id:
                    continue
                try:
                    await self.p2p_manager.send_message_to_peer(
                        peer_id, {"command": "COMMIT_ACK", "payload": payload}
                    )
                    logger.debug(
                        "Sent COMMIT_ACK to %s for commit %s",
                        peer_id[:20], commit.commit_id[:12],
                    )
                except Exception as e:
                    logger.debug("Could not send COMMIT_ACK to %s: %s", peer_id[:20], e)
        except Exception as e:
            logger.error("Error in _on_commit_ack: %s", e, exc_info=True)

    async def _on_apply_retransmit(self, commit, node_ids: List[str]) -> None:
        """Hand the whole commit to participants that never ACKed it.

        The counterpart of `_on_commit_ack`: that one says «I applied it», this one
        answers the silence. A node that failed its own apply cannot ask for the
        commit — it does not know there is one — so the commit has to be pushed.
        """
        from dpc_protocol.knowledge_commit import ApplyKnowledgeCommitMessage

        message = ApplyKnowledgeCommitMessage.create(commit)
        for peer_id in node_ids:
            if peer_id == self.p2p_manager.node_id:
                continue
            try:
                await self.p2p_manager.send_message_to_peer(
                    peer_id, {"command": message.command, "payload": message.payload}
                )
                logger.info(
                    "Retransmitted commit %s to %s (no COMMIT_ACK within the window)",
                    commit.commit_id[:12], peer_id[:20],
                )
            except Exception as e:
                logger.warning(
                    "Could not retransmit commit %s to %s: %s",
                    commit.commit_id[:12], peer_id[:20], e,
                )

    async def _on_commit_apply_failed(self, commit, error_msg: str) -> None:
        """Surface apply failures to the UI."""
        logger.error(
            "Failed to apply knowledge commit %s: %s",
            getattr(commit, 'commit_id', '?'), error_msg,
        )
        await self.local_api.broadcast_event("knowledge_commit_apply_failed", {
            "topic": getattr(commit, 'topic', 'unknown'),
            "commit_id": getattr(commit, 'commit_id', None),
            "error": error_msg,
        })

    async def _broadcast_commit_result(
        self, result_payload: dict, participants: List[str]
    ) -> None:
        """Broadcast KNOWLEDGE_COMMIT_RESULT to remote participants and emit UI event.

        Solo-vote conversations (agent_*, local_ai, ai_*, telegram-*) reduce
        participants to [local_node_id] for voting. The local node is never in
        p2p_manager.peers, so iterating it produced a misleading "Broadcasted"
        log with zero recipients. Filter self out before iterating.
        """
        message = {"command": "KNOWLEDGE_COMMIT_RESULT", "payload": result_payload}
        local_node_id = self.p2p_manager.node_id
        remote_participants = [p for p in participants if p != local_node_id]

        for node_id in remote_participants:
            if node_id in self.p2p_manager.peers:
                try:
                    await self.p2p_manager.send_message_to_peer(node_id, message)
                    logger.info("Sent KNOWLEDGE_COMMIT_RESULT to %s", node_id[:20])
                except Exception as e:
                    logger.error(
                        "Failed to send result to %s: %s", node_id[:20], e, exc_info=True
                    )
            else:
                logger.debug(
                    "Participant %s not connected, skipping result broadcast", node_id[:20]
                )

        await self.local_api.broadcast_event("knowledge_commit_result", result_payload)

    # --- Session / Conversation Management ---

    async def reset_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Reset conversation history (internal, called after approval)."""
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            monitor.reset_conversation()
            logger.info("Reset Conversation - cleared history for %s", conversation_id)
            await self.local_api.broadcast_event(
                "conversation_reset", {"conversation_id": conversation_id}
            )
            return {"status": "success", "message": "Conversation reset successfully"}
        except Exception as e:
            logger.error("Error resetting conversation: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_conversation_settings(self, conversation_id: str) -> Dict[str, Any]:
        """Get per-conversation settings including history persistence."""
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            settings = monitor._load_conversation_settings()
            return {"status": "success", "settings": settings}
        except Exception as e:
            logger.error("Error getting conversation settings: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def set_conversation_persist_history(self, conversation_id: str, persist: bool) -> Dict[str, Any]:
        """Set whether to persist history for a conversation."""
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            success = monitor.set_persist_history(persist)
            if not success:
                return {"status": "error", "message": "Failed to save settings"}

            if persist and monitor.message_history:
                monitor.save_history()
            elif not persist:
                monitor.clear_history()

            await self.local_api.broadcast_event(
                "conversation_settings_changed",
                {"conversation_id": conversation_id, "persist_history": persist}
            )
            return {"status": "success", "persist_history": persist}
        except Exception as e:
            logger.error("Error setting conversation persistence: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def delete_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Delete an entire conversation including history, settings, and files."""
        try:
            if conversation_id.startswith("group-"):
                success = self.group_manager.leave_group(conversation_id)
                if not success:
                    return {"status": "error", "message": "Group not found or could not be deleted"}
            else:
                if conversation_id in self.conversation_monitors:
                    monitor = self.conversation_monitors[conversation_id]
                    monitor.delete_conversation_folder()
                    del self.conversation_monitors[conversation_id]
                else:
                    import shutil
                    conv_dir = Path.home() / ".dpc" / "conversations" / conversation_id
                    if conv_dir.exists():
                        shutil.rmtree(conv_dir)

            logger.info("Deleted conversation: %s", conversation_id)
            await self.local_api.broadcast_event(
                "conversation_deleted", {"conversation_id": conversation_id}
            )
            return {"status": "success", "message": "Conversation deleted successfully"}
        except Exception as e:
            logger.error("Error deleting conversation: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
