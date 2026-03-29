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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .consensus_manager import ConsensusManager
from .conversation_monitor import ConversationMonitor, Message as ConvMessage
from dpc_protocol.pcm_core import PCMCore

logger = logging.getLogger(__name__)

NODE_KEY = "node.key"


class KnowledgeService:
    """Manages personal knowledge, consensus voting, and conversation monitoring.

    Owns:
    - pcm_core (PCMCore) — personal context model access
    - consensus_manager (ConsensusManager) — multi-party knowledge voting
    - conversation_monitors (shared dict ref) — per-conversation monitor registry
    - auto_knowledge_detection_enabled (bool) — global toggle

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
        send_ai_query: Callable,
        broadcast_to_peers: Callable,
        broadcast_to_group: Callable,
        compute_context_hash: Callable,
    ):
        # Owned state
        self.pcm_core = pcm_core
        self.consensus_manager = ConsensusManager(
            node_id=p2p_manager.node_id,
            pcm_core=self.pcm_core,
            vote_timeout_minutes=10,
        )
        self.auto_knowledge_detection_enabled: bool = False

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
        self._send_ai_query = send_ai_query
        self._broadcast_to_peers_func = broadcast_to_peers
        self._broadcast_to_group_func = broadcast_to_group
        self._compute_context_hash = compute_context_hash

        # Register all consensus callbacks on the manager we own
        self.consensus_manager.on_commit_applied = self._on_commit_applied
        self.consensus_manager.on_commit_signed = self._on_commit_signed
        self.consensus_manager.on_commit_ack = self._on_commit_ack
        self.consensus_manager.on_commit_apply_failed = self._on_commit_apply_failed
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
            "auto_knowledge_detection": self.auto_knowledge_detection_enabled,
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
    # Telegram bridge helper
    # ─────────────────────────────────────────────────────────────

    def _get_agent_telegram_bridge(self, conversation_id: str):
        """Return the AgentTelegramBridge for an agent conversation, or None."""
        if not conversation_id or not conversation_id.startswith("agent_"):
            return None
        dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
        if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
            mgr = dpc_agent_provider._managers.get(conversation_id)
            if mgr:
                return getattr(mgr, '_telegram_bridge', None)
        return None

    # ─────────────────────────────────────────────────────────────
    # Conversation monitor management
    # ─────────────────────────────────────────────────────────────

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
        if conversation_id not in self.conversation_monitors:
            participants = []

            if conversation_id == "local_ai" or conversation_id.startswith("ai_"):
                participants = [
                    {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"},
                    {"node_id": conversation_id, "name": "DPC Agent", "context": "ai_agent"},
                ]
            elif conversation_id.startswith("group-"):
                group = self.group_manager.get_group(conversation_id)
                if group:
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
                else:
                    participants = [
                        {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"}
                    ]
            else:
                participants = [
                    {"node_id": self.p2p_manager.node_id, "name": "User", "context": "local"},
                    {
                        "node_id": conversation_id,
                        "name": self.peer_metadata.get(conversation_id, {}).get("name", conversation_id),
                        "context": "peer",
                    },
                ]

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
                auto_detect=self.auto_knowledge_detection_enabled,
                instruction_set_name=instruction_set_name or self.instruction_set.default,
                display_name=display_name,
            )

            # Load persisted history from disk — only for group chats
            if conversation_id.startswith("group-"):
                if self.conversation_monitors[conversation_id].load_history():
                    logger.info(
                        "Loaded persisted history for group %s (%d messages)",
                        conversation_id,
                        len(self.conversation_monitors[conversation_id].message_history),
                    )

            logger.info(
                "Created conversation monitor for %s with %d participant(s) "
                "(auto_detect=%s, instruction_set=%s)",
                conversation_id,
                len(participants),
                self.auto_knowledge_detection_enabled,
                instruction_set_name or self.instruction_set.default,
            )

        return self.conversation_monitors[conversation_id]

    # ─────────────────────────────────────────────────────────────
    # Knowledge detection toggle
    # ─────────────────────────────────────────────────────────────

    async def toggle_auto_knowledge_detection(self, enabled: bool = None) -> Dict[str, Any]:
        """Toggle automatic knowledge detection on/off.

        UI Integration: Called when user toggles the auto-detection switch.

        Args:
            enabled: True to enable, False to disable, None to toggle current state

        Returns:
            Dict with status and current state
        """
        try:
            if enabled is None:
                self.auto_knowledge_detection_enabled = not self.auto_knowledge_detection_enabled
            else:
                self.auto_knowledge_detection_enabled = enabled

            state_text = "enabled" if self.auto_knowledge_detection_enabled else "disabled"
            logger.info("Auto knowledge detection %s", state_text)

            for monitor in self.conversation_monitors.values():
                monitor.auto_detect = self.auto_knowledge_detection_enabled

            return {
                "status": "success",
                "enabled": self.auto_knowledge_detection_enabled,
                "message": f"Automatic knowledge detection {state_text}",
            }
        except Exception as e:
            logger.error("Error toggling auto knowledge detection: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Knowledge commit voting flow
    # ─────────────────────────────────────────────────────────────

    async def vote_knowledge_commit(
        self,
        proposal_id: str,
        vote: str,
        comment: str = None,
    ) -> Dict[str, Any]:
        """Cast vote on a knowledge commit proposal.

        UI Integration: Called when user clicks approve/reject/request_changes
        in KnowledgeCommitDialog component.
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

            success = await self.consensus_manager.cast_vote(
                proposal_id=proposal_id,
                vote=vote,
                comment=comment,
                broadcast_func=broadcast_func,
            )

            if success and is_ai_chat:
                logger.info(
                    "User voted on AI chat proposal %s, triggering AI evaluation",
                    proposal_id,
                )
                asyncio.create_task(
                    self._ai_agent_vote_on_proposal(proposal_id, ai_agent_node_id)
                )

            if success:
                return {"status": "success", "message": f"Vote cast: {vote}"}
            else:
                return {
                    "status": "error",
                    "message": "Proposal not found or voting session expired",
                }
        except Exception as e:
            logger.error("Error voting on knowledge commit: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

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
                        agent_provider_alias = _mgr.config.get("provider_alias")
                        _monitor = _mgr._agent_monitors.get(conv_id)
                        if _monitor:
                            conversation_history = _monitor.full_conversation

            ai_decision = await self._evaluate_knowledge_proposal_for_ai_vote(
                proposal,
                provider_alias=agent_provider_alias,
                conversation_history=conversation_history,
            )

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
    ) -> Dict[str, Any]:
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
            logger.warning("AI vote evaluation returned invalid JSON: %s", e)
            return {
                "vote": "approve",
                "comment": "AI evaluation completed (defaulting to approve due to parsing issue)",
            }
        except Exception as e:
            logger.error("Error in AI vote evaluation: %s", e, exc_info=True)
            return {
                "vote": "approve",
                "comment": "AI evaluation encountered an error, defaulting to user's judgment",
            }

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
                if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
                    if conversation_id in dpc_agent_provider._managers:
                        agent_manager = dpc_agent_provider._managers[conversation_id]
                        if hasattr(agent_manager, '_agent_monitors'):
                            monitor = agent_manager._agent_monitors.get(conversation_id)
            if monitor is None:
                monitor = self._get_or_create_conversation_monitor(conversation_id)

            logger.info("End Session - attempting manual extraction for %s", conversation_id)
            logger.info(
                "Full conversation: %d messages (incremental buffer: %d), Score: %.2f",
                len(monitor.full_conversation),
                len(monitor.message_buffer),
                monitor.knowledge_score,
            )

            # For group conversations with no local AI: inject a compute peer so extraction works
            if conversation_id.startswith("group-"):
                host, _, _ = monitor._infer_inference_settings()
                if host is None:
                    group = self.group_manager.get_group(conversation_id)
                    if group:
                        for member_id in group.members:
                            if member_id == self.p2p_manager.node_id:
                                continue
                            if member_id in self.p2p_manager.peers:
                                monitor.set_inference_settings(
                                    compute_host=member_id,
                                    model=None,
                                    provider=None,
                                )
                                logger.info(
                                    "Group knowledge extraction: using compute from peer %s",
                                    member_id[:20]
                                )
                                break

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

    async def _on_commit_approved(self, commit) -> None:
        """Notify the UI that consensus was reached and the commit was approved."""
        try:
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

    async def _on_commit_rejected(self, proposal, votes: Dict[str, Any]) -> None:
        """Notify the UI that the proposal was rejected, including rejection reasons."""
        try:
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
                    logger.info(
                        "Clearing buffer for %s after commit approval",
                        commit.conversation_id,
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

            # For private conversations, reset after commit so context window counter clears
            conv_id = commit.conversation_id
            if conv_id and (
                conv_id == "local_ai"
                or conv_id.startswith("ai_")
                or conv_id.startswith("agent_")
                or conv_id.startswith("telegram-")
            ):
                logger.info(
                    "Resetting private conversation %s after knowledge commit applied",
                    conv_id,
                )
                await self.local_api.broadcast_event(
                    "conversation_reset", {"conversation_id": conv_id}
                )
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
            response = await agent_mgr.process_message(
                message=revision_prompt,
                conversation_id=proposal.conversation_id,
                include_context=False,
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
        """Broadcast KNOWLEDGE_COMMIT_RESULT to all participants."""
        message = {"command": "KNOWLEDGE_COMMIT_RESULT", "payload": result_payload}

        for node_id in participants:
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
