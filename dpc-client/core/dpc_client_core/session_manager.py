"""
Session Manager - Mutual New Session Approval

Manages new session proposals and voting for collaborative conversation resets.
Requires mutual consent before clearing conversation history.
"""

import asyncio
import logging
import uuid
import time
from typing import Dict, Optional, Set, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class NewSessionProposal:
    """Represents a new session proposal initiated by a peer."""
    proposal_id: str           # UUID
    initiator_node_id: str
    conversation_id: str
    timestamp: str             # ISO 8601
    participants: Set[str]     # All node_ids in conversation
    votes: Dict[str, bool] = field(default_factory=dict)  # node_id → approve(True)/reject(False)
    deadline: float = 0.0      # time.time() + 60 seconds


@dataclass
class VotingSession:
    """Tracks voting state for a new session proposal."""
    proposal: NewSessionProposal
    is_initiator: bool
    timeout_task: Optional[asyncio.Task] = None

    def is_complete(self) -> bool:
        """Check if all participants have voted."""
        return len(self.proposal.votes) == len(self.proposal.participants)

    def is_approved(self) -> bool:
        """
        Check if proposal is approved based on voting rules.

        P2P (2 participants): Unanimous approval required
        Multi-party (3+): Majority approval (>50%)
        """
        if not self.proposal.votes:
            return False

        total_votes = len(self.proposal.votes)
        approve_votes = sum(1 for v in self.proposal.votes.values() if v)

        if len(self.proposal.participants) == 2:
            # P2P: require unanimous approval
            return approve_votes == 2 and total_votes == 2
        else:
            # Multi-party: require majority
            return approve_votes > (total_votes / 2)


class NewSessionProposalManager:
    """
    Manages new session proposals and voting lifecycle.

    Features:
    - Democratic voting (wait for all votes or timeout)
    - 60-second timeout per proposal
    - Duplicate proposal prevention
    - Automatic initiator vote as "approve"
    """

    def __init__(self, core_service):
        """
        Initialize session proposal manager.

        Args:
            core_service: Reference to CoreService for sending messages and accessing components
        """
        self.core_service = core_service
        self.active_sessions: Dict[str, VotingSession] = {}  # proposal_id → session
        self.logger = logging.getLogger(__name__)

        # Callbacks for notifications
        self.on_proposal_received: Optional[Callable] = None  # Called when peer proposal arrives
        self.on_result_broadcast: Optional[Callable] = None   # Called to broadcast voting results

    async def propose_new_session(
        self,
        conversation_id: str,
        participants: Set[str]
    ) -> Dict[str, Any]:
        """
        Initiate a new session proposal.

        Args:
            conversation_id: The conversation to reset
            participants: Set of node_ids participating in conversation

        Returns:
            Dict with status and proposal_id

        Raises:
            ValueError: If a proposal is already pending for this conversation
        """
        # Check for existing proposal
        existing = self.get_pending_proposal(conversation_id)
        if existing:
            raise ValueError(f"Proposal already pending for conversation {conversation_id}")

        # Generate proposal
        proposal_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        deadline = time.time() + 60  # 60 seconds from now

        proposal = NewSessionProposal(
            proposal_id=proposal_id,
            initiator_node_id=self.core_service.p2p_manager.node_id,
            conversation_id=conversation_id,
            timestamp=timestamp,
            participants=participants,
            votes={},
            deadline=deadline
        )

        # Auto-vote approve for initiator
        proposal.votes[self.core_service.p2p_manager.node_id] = True

        # Create voting session
        session = VotingSession(
            proposal=proposal,
            is_initiator=True,
            timeout_task=None
        )

        # Store session
        self.active_sessions[proposal_id] = session

        # Start timeout task
        session.timeout_task = asyncio.create_task(
            self._handle_timeout(proposal_id)
        )

        self.logger.info(
            "Created new session proposal %s for conversation %s with %d participants",
            proposal_id[:8],
            conversation_id[:20],
            len(participants)
        )

        # Broadcast PROPOSE_NEW_SESSION to all participants (except self)
        await self._broadcast_proposal(proposal)

        return {
            "status": "success",
            "proposal_id": proposal_id,
            "message": "New session proposal sent to peers"
        }

    async def record_vote(
        self,
        proposal_id: str,
        voter_node_id: str,
        approve: bool
    ) -> None:
        """
        Record a vote for a proposal and check if voting is complete.

        Args:
            proposal_id: UUID of the proposal
            voter_node_id: Node ID of the voter
            approve: True for approve, False for reject
        """
        session = self.active_sessions.get(proposal_id)
        if not session:
            self.logger.warning("Vote received for unknown proposal %s", proposal_id[:8])
            return

        # Record vote
        session.proposal.votes[voter_node_id] = approve

        vote_str = "approve" if approve else "reject"
        self.logger.info(
            "Recorded vote from %s: %s (total: %d/%d)",
            voter_node_id[:20],
            vote_str,
            len(session.proposal.votes),
            len(session.proposal.participants)
        )

        # Check if voting complete (all votes received)
        if session.is_complete():
            self.logger.info("All votes received for proposal %s, finalizing...", proposal_id[:8])
            await self._finalize_proposal(proposal_id)

    async def _finalize_proposal(self, proposal_id: str) -> None:
        """
        Tally votes and broadcast result.

        Args:
            proposal_id: UUID of the proposal to finalize
        """
        session = self.active_sessions.get(proposal_id)
        if not session:
            self.logger.warning("Cannot finalize unknown proposal %s", proposal_id[:8])
            return

        proposal = session.proposal

        # Cancel timeout task
        if session.timeout_task and not session.timeout_task.done():
            session.timeout_task.cancel()
            try:
                await session.timeout_task
            except asyncio.CancelledError:
                pass

        # Tally votes
        total_votes = len(proposal.votes)
        approve_votes = sum(1 for v in proposal.votes.values() if v)
        reject_votes = total_votes - approve_votes

        # Determine result
        is_approved = session.is_approved()
        result = "approved" if is_approved else "rejected"

        self.logger.info(
            "Finalized proposal %s: %s (approve: %d, reject: %d, total: %d)",
            proposal_id[:8],
            result,
            approve_votes,
            reject_votes,
            total_votes
        )

        # Prepare result payload
        result_payload = {
            "proposal_id": proposal_id,
            "conversation_id": proposal.conversation_id,
            "result": result,
            "clear_history": is_approved,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vote_tally": {
                "approve": approve_votes,
                "reject": reject_votes,
                "total": total_votes
            }
        }

        # Extract peer's node_id (conversations are keyed by peer's node_id)
        peer_node_id = next((p for p in proposal.participants if p != self.core_service.p2p_manager.node_id), None)

        # If approved: clear local history (for all participants)
        if is_approved:
            self.logger.info("Proposal approved, clearing local conversation history for %s", peer_node_id[:20] if peer_node_id else "unknown")
            monitor = self.core_service._get_or_create_conversation_monitor(peer_node_id)
            monitor.reset_conversation()

        # Broadcast result to all participants
        if self.on_result_broadcast:
            await self.on_result_broadcast(result_payload, list(proposal.participants))

        # Emit event to UI for initiator (add peer's node_id for frontend lookup)
        ui_payload = {**result_payload, "sender_node_id": peer_node_id}
        await self.core_service.local_api.broadcast_event(
            "new_session_result",
            ui_payload
        )

        # Remove from active sessions
        del self.active_sessions[proposal_id]

    async def _handle_timeout(self, proposal_id: str) -> None:
        """
        Handle timeout expiration (60 seconds).

        Args:
            proposal_id: UUID of the proposal that timed out
        """
        await asyncio.sleep(60)  # Wait 60 seconds

        session = self.active_sessions.get(proposal_id)
        if not session:
            # Already finalized
            return

        self.logger.info(
            "Proposal %s timed out with %d/%d votes",
            proposal_id[:8],
            len(session.proposal.votes),
            len(session.proposal.participants)
        )

        # Finalize with current votes
        await self._finalize_proposal(proposal_id)

    async def _broadcast_proposal(self, proposal: NewSessionProposal) -> None:
        """
        Broadcast PROPOSE_NEW_SESSION to all participants (except initiator).

        Args:
            proposal: The proposal to broadcast
        """
        message = {
            "command": "PROPOSE_NEW_SESSION",
            "payload": {
                "proposal_id": proposal.proposal_id,
                "initiator_node_id": proposal.initiator_node_id,
                "conversation_id": proposal.conversation_id,
                "timestamp": proposal.timestamp,
                "participants": list(proposal.participants)
            }
        }

        # Send to all participants except self
        for node_id in proposal.participants:
            if node_id == self.core_service.p2p_manager.node_id:
                continue

            if node_id in self.core_service.p2p_manager.peers:
                try:
                    await self.core_service.p2p_manager.send_message_to_peer(node_id, message)
                    self.logger.debug("Sent PROPOSE_NEW_SESSION to %s", node_id[:20])
                except Exception as e:
                    self.logger.error("Error sending proposal to %s: %s", node_id[:20], e)

    async def handle_proposal_message(self, sender_node_id: str, payload: Dict[str, Any]) -> None:
        """
        Handle incoming PROPOSE_NEW_SESSION message from peer.

        Args:
            sender_node_id: Node ID of the sender
            payload: Proposal payload
        """
        proposal_id = payload.get("proposal_id")
        conversation_id = payload.get("conversation_id")

        self.logger.info(
            "Received new session proposal %s from %s for conversation %s",
            proposal_id[:8],
            sender_node_id[:20],
            conversation_id[:20]
        )

        # Create local proposal object
        proposal = NewSessionProposal(
            proposal_id=proposal_id,
            initiator_node_id=payload.get("initiator_node_id"),
            conversation_id=conversation_id,
            timestamp=payload.get("timestamp"),
            participants=set(payload.get("participants", [])),
            votes={payload.get("initiator_node_id"): True},  # Initiator already voted approve
            deadline=time.time() + 60
        )

        # Create voting session (not initiator)
        session = VotingSession(
            proposal=proposal,
            is_initiator=False,
            timeout_task=asyncio.create_task(self._handle_timeout(proposal_id))
        )

        self.active_sessions[proposal_id] = session

        # Notify UI via callback
        if self.on_proposal_received:
            await self.on_proposal_received(payload)

    async def handle_vote_message(self, sender_node_id: str, payload: Dict[str, Any]) -> None:
        """
        Handle incoming VOTE_NEW_SESSION message from peer.

        Args:
            sender_node_id: Node ID of the voter
            payload: Vote payload
        """
        proposal_id = payload.get("proposal_id")
        vote = payload.get("vote")  # True = approve, False = reject

        self.logger.info(
            "Received vote from %s: %s",
            sender_node_id[:20],
            "approve" if vote else "reject"
        )

        # Record vote
        await self.record_vote(proposal_id, sender_node_id, vote)

    def get_pending_proposal(self, conversation_id: str) -> Optional[NewSessionProposal]:
        """
        Get pending proposal for a conversation (for UI button disabling).

        Args:
            conversation_id: The conversation ID to check

        Returns:
            NewSessionProposal if one exists, None otherwise
        """
        for session in self.active_sessions.values():
            if session.proposal.conversation_id == conversation_id:
                return session.proposal
        return None

    def get_session(self, proposal_id: str) -> Optional[VotingSession]:
        """
        Get voting session by proposal ID.

        Args:
            proposal_id: UUID of the proposal

        Returns:
            VotingSession if found, None otherwise
        """
        return self.active_sessions.get(proposal_id)
