"""
Consensus Manager - Phase 4.3

Manages knowledge commit voting with required dissent to prevent groupthink.
Coordinates multi-party approval with devil's advocate mechanism.
"""

import asyncio
import logging
import random
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from dpc_protocol.knowledge_commit import (
    KnowledgeCommitProposal,
    CommitVote,
    KnowledgeCommit
)
from dpc_protocol.pcm_core import PCMCore, PersonalContext

logger = logging.getLogger(__name__)


@dataclass
class VotingSession:
    """Active voting session for a commit proposal"""
    proposal: KnowledgeCommitProposal
    votes: Dict[str, CommitVote] = None  # node_id -> vote
    required_dissenter: Optional[str] = None
    deadline: Optional[datetime] = None
    status: str = "voting"  # voting, approved, rejected, timeout

    def __post_init__(self):
        if self.votes is None:
            self.votes = {}


class ConsensusManager:
    """Manages consensus voting for knowledge commits

    Features:
    - Multi-party voting with configurable thresholds
    - Required dissent mechanism (anti-groupthink)
    - Devil's advocate assignment
    - Vote deadline management
    - Automatic commit application on approval
    """

    def __init__(
        self,
        node_id: str,
        pcm_core: PCMCore,
        vote_timeout_minutes: int = 10,
        consensus_threshold: float = 0.75  # 75% approval required
    ):
        """Initialize consensus manager

        Args:
            node_id: This node's identifier
            pcm_core: PCMCore instance for applying commits
            vote_timeout_minutes: Minutes until vote deadline
            consensus_threshold: Fraction of votes needed to approve (0.0-1.0)
        """
        self.node_id = node_id
        self.pcm_core = pcm_core
        self.vote_timeout_minutes = vote_timeout_minutes
        self.consensus_threshold = consensus_threshold

        # Active sessions
        self.sessions: Dict[str, VotingSession] = {}  # proposal_id -> session

        # Callbacks for notifications
        self.on_proposal_received: Optional[Callable] = None
        self.on_vote_received: Optional[Callable] = None
        self.on_commit_approved: Optional[Callable] = None
        self.on_commit_rejected: Optional[Callable] = None
        self.on_commit_applied: Optional[Callable] = None  # Called after commit is applied to personal.json

    async def propose_commit(
        self,
        proposal: KnowledgeCommitProposal,
        broadcast_func: Callable
    ) -> VotingSession:
        """Start voting on a new commit proposal

        Args:
            proposal: KnowledgeCommitProposal to vote on
            broadcast_func: Async function to broadcast proposal to peers

        Returns:
            VotingSession object
        """
        # Assign required dissenter if 3+ participants (anti-groupthink)
        if len(proposal.participants) >= 3:
            # Randomly assign one person as devil's advocate
            proposal.required_dissenter = random.choice(proposal.participants)

        # Set deadline
        deadline = datetime.utcnow() + timedelta(minutes=self.vote_timeout_minutes)
        proposal.vote_deadline = deadline.isoformat()
        proposal.status = 'voting'

        # Create session
        session = VotingSession(
            proposal=proposal,
            required_dissenter=proposal.required_dissenter,
            deadline=deadline
        )

        self.sessions[proposal.proposal_id] = session

        # Broadcast to participants
        await broadcast_func({
            'command': 'PROPOSE_KNOWLEDGE_COMMIT',
            'payload': proposal.to_dict()
        })

        # Start deadline timer
        asyncio.create_task(self._handle_vote_deadline(proposal.proposal_id))

        return session

    async def cast_vote(
        self,
        proposal_id: str,
        vote: str,  # "approve", "reject", "request_changes"
        comment: Optional[str] = None,
        broadcast_func: Optional[Callable] = None
    ) -> bool:
        """Cast a vote on a proposal

        Args:
            proposal_id: ID of proposal to vote on
            vote: Vote choice
            comment: Optional comment
            broadcast_func: Optional function to broadcast vote to peers

        Returns:
            True if vote was cast, False if session not found
        """
        if proposal_id not in self.sessions:
            return False

        session = self.sessions[proposal_id]

        # Check if this voter is required dissenter
        is_required_dissent = (self.node_id == session.required_dissenter)

        # Create vote object
        vote_obj = CommitVote(
            proposal_id=proposal_id,
            voter_node_id=self.node_id,
            vote=vote,
            comment=comment,
            is_required_dissent=is_required_dissent
        )

        # Record vote
        session.votes[self.node_id] = vote_obj

        # Broadcast vote if function provided
        if broadcast_func:
            await broadcast_func({
                'command': 'VOTE_KNOWLEDGE_COMMIT',
                'payload': asdict(vote_obj)
            })

        # Check if voting is complete
        if len(session.votes) == len(session.proposal.participants):
            await self._finalize_vote(session)

        # Trigger callback
        if self.on_vote_received:
            await self.on_vote_received(vote_obj)

        return True

    async def receive_vote(
        self,
        vote: CommitVote
    ) -> None:
        """Receive vote from peer

        Args:
            vote: CommitVote object from peer
        """
        proposal_id = vote.proposal_id

        if proposal_id not in self.sessions:
            logger.warning("Received vote for unknown proposal %s", proposal_id)
            return

        session = self.sessions[proposal_id]

        # Record vote
        session.votes[vote.voter_node_id] = vote

        # Check if voting is complete
        if len(session.votes) == len(session.proposal.participants):
            await self._finalize_vote(session)

        # Trigger callback
        if self.on_vote_received:
            await self.on_vote_received(vote)

    async def _finalize_vote(self, session: VotingSession) -> None:
        """Finalize voting and determine outcome

        Args:
            session: VotingSession to finalize
        """
        proposal = session.proposal
        votes = session.votes

        # Count votes
        approve_count = sum(1 for v in votes.values() if v.vote == "approve")
        reject_count = sum(1 for v in votes.values() if v.vote == "reject")
        change_count = sum(1 for v in votes.values() if v.vote == "request_changes")

        total_votes = len(votes)
        approval_rate = approve_count / total_votes if total_votes > 0 else 0

        # Determine outcome
        if approval_rate >= self.consensus_threshold:
            # Approved!
            session.status = "approved"
            proposal.status = "approved"

            # Create finalized commit
            commit = KnowledgeCommit(
                summary=proposal.summary,
                description=f"Approved by {approve_count}/{total_votes} participants",
                topic=proposal.topic,
                entries=proposal.entries,
                conversation_id=proposal.conversation_id,
                participants=proposal.participants,
                consensus_type="unanimous" if approval_rate == 1.0 else "majority",
                approved_by=[nid for nid, v in votes.items() if v.vote == "approve"],
                rejected_by=[nid for nid, v in votes.items() if v.vote == "reject"],
                cultural_perspectives_considered=proposal.cultural_perspectives,
                confidence_score=proposal.avg_confidence,
                sources_cited=[],  # Could extract from entries
                dissenting_opinion=proposal.devil_advocate
            )

            # Apply commit to local context
            await self._apply_commit(commit)

            # Trigger callback
            if self.on_commit_approved:
                await self.on_commit_approved(commit)

        elif reject_count > change_count:
            # Rejected
            session.status = "rejected"
            proposal.status = "rejected"

            if self.on_commit_rejected:
                await self.on_commit_rejected(proposal, votes)

        else:
            # Changes requested
            session.status = "revision_needed"
            proposal.status = "revised"

            # Could trigger revision workflow here

    async def _apply_commit(self, commit: KnowledgeCommit) -> None:
        """Apply approved commit to local PCM with cryptographic integrity

        Args:
            commit: KnowledgeCommit to apply
        """
        try:
            import hashlib
            from dpc_protocol.crypto import load_identity
            from cryptography.hazmat.primitives import serialization

            # Load current context
            context = self.pcm_core.load_context()

            # 1. Set parent commit (chain of trust)
            commit.parent_commit_id = context.last_commit_id

            # 2. Compute hash-based commit ID
            commit.compute_hash()  # Sets commit_hash and commit_id

            logger.info("Created commit %s (hash: %s...)", commit.commit_id, commit.commit_hash[:16])

            # 3. Sign commit with our private key
            node_id, key_path, cert_path = load_identity()

            with open(key_path, 'rb') as f:
                private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None
                )

            commit.sign(node_id, private_key)

            logger.info("Signed commit with %s", node_id)

            # 4. Add or update topic
            topic_name = commit.topic

            if topic_name in context.knowledge:
                # Update existing topic
                topic = context.knowledge[topic_name]
                topic.entries.extend(commit.entries)
                topic.version += 1
                topic.last_modified = datetime.utcnow().isoformat()
            else:
                # Create new topic
                from dpc_protocol.pcm_core import Topic
                context.knowledge[topic_name] = Topic(
                    summary=commit.summary,
                    entries=commit.entries,
                    version=1
                )

            topic = context.knowledge[topic_name]

            # 5. Update context metadata
            context.version += 1
            context.last_commit_id = commit.commit_id
            context.last_commit_message = commit.summary
            context.last_commit_timestamp = commit.timestamp

            # 6. Add to commit history with cryptographic fields
            context.commit_history.append({
                'commit_id': commit.commit_id,
                'commit_hash': commit.commit_hash,
                'timestamp': commit.timestamp,
                'message': commit.summary,
                'participants': commit.participants,
                'consensus': commit.consensus_type,
                'approved_by': commit.approved_by,
                'signatures': commit.signatures
            })

            # 7. Create versioned markdown file with frontmatter
            from dpc_protocol.markdown_manager import MarkdownKnowledgeManager

            markdown_manager = MarkdownKnowledgeManager()

            # Compute content hash for markdown
            markdown_content = markdown_manager.topic_to_markdown_content(topic)
            content_hash = hashlib.sha256(markdown_content.encode('utf-8')).hexdigest()[:16]

            # Create markdown with frontmatter
            safe_topic_name = markdown_manager.sanitize_filename(topic_name)
            markdown_filename = f"{safe_topic_name}_{commit.commit_id}.md"
            markdown_path = markdown_manager.knowledge_dir / markdown_filename

            frontmatter = {
                'topic': topic_name,
                'commit_id': commit.commit_id,
                'commit_hash': commit.commit_hash,
                'parent_commit': commit.parent_commit_id or "",
                'content_hash': content_hash,
                'timestamp': commit.timestamp,
                'version': topic.version,
                'author': node_id,
                'participants': commit.participants,
                'approved_by': commit.approved_by,
                'rejected_by': commit.rejected_by,
                'consensus': commit.consensus_type,
                'confidence_score': commit.confidence_score,
                'signatures': commit.signatures,
                'cultural_perspectives': commit.cultural_perspectives_considered
            }

            markdown_manager.write_markdown_with_frontmatter(
                markdown_path,
                frontmatter,
                markdown_content
            )

            # Update topic reference
            topic.markdown_file = f"knowledge/{markdown_filename}"
            topic.commit_id = commit.commit_id
            topic.entries = []  # Clear entries (markdown is source of truth)

            # 8. Save context
            self.pcm_core.save_context(context)

            logger.info("Applied commit: %s", commit.commit_id)
            logger.info("   Topic: %s", topic_name)
            logger.info("   Markdown: %s", markdown_filename)
            logger.info("   Signatures: %d", len(commit.signatures))

            # 9. Notify callback (for reloading p2p_manager.local_context and broadcasting CONTEXT_UPDATED)
            if self.on_commit_applied:
                await self.on_commit_applied(commit)

        except Exception as e:
            logger.error("Error applying commit: %s", e, exc_info=True)

    async def _handle_vote_deadline(self, proposal_id: str) -> None:
        """Handle vote deadline timeout

        Args:
            proposal_id: Proposal to check
        """
        # Wait until deadline
        if proposal_id not in self.sessions:
            return

        session = self.sessions[proposal_id]
        if session.deadline:
            now = datetime.utcnow()
            if session.deadline > now:
                wait_seconds = (session.deadline - now).total_seconds()
                await asyncio.sleep(wait_seconds)

        # Check if still voting
        if proposal_id in self.sessions:
            session = self.sessions[proposal_id]
            if session.status == "voting":
                # Timeout - finalize with current votes
                session.status = "timeout"
                await self._finalize_vote(session)

    def get_session(self, proposal_id: str) -> Optional[VotingSession]:
        """Get voting session by proposal ID

        Args:
            proposal_id: Proposal ID

        Returns:
            VotingSession or None
        """
        return self.sessions.get(proposal_id)

    def get_active_sessions(self) -> List[VotingSession]:
        """Get all active voting sessions

        Returns:
            List of VotingSession objects
        """
        return [s for s in self.sessions.values() if s.status == "voting"]

    def clear_old_sessions(self, max_age_hours: int = 24) -> int:
        """Clear old completed sessions

        Args:
            max_age_hours: Max age in hours

        Returns:
            Number of sessions cleared
        """
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        to_remove = []

        for pid, session in self.sessions.items():
            if session.deadline and session.deadline < cutoff:
                if session.status in ["approved", "rejected", "timeout"]:
                    to_remove.append(pid)

        for pid in to_remove:
            del self.sessions[pid]

        return len(to_remove)

    async def handle_proposal_message(self, sender_node_id: str, payload: Dict[str, Any]) -> None:
        """Handle PROPOSE_KNOWLEDGE_COMMIT message from peer

        Args:
            sender_node_id: Node ID of the proposer
            payload: Proposal payload (dict format)
        """
        try:
            # Reconstruct proposal from dict
            proposal = KnowledgeCommitProposal.from_dict(payload)

            # Create voting session
            session = VotingSession(
                proposal=proposal,
                required_dissenter=proposal.required_dissenter,
                deadline=datetime.fromisoformat(proposal.vote_deadline) if proposal.vote_deadline else None
            )

            self.sessions[proposal.proposal_id] = session

            logger.info("Received knowledge commit proposal from %s", sender_node_id)
            logger.info("  - Topic: %s", proposal.topic)
            logger.info("  - Entries: %d", len(proposal.entries))
            logger.info("  - Proposal ID: %s", proposal.proposal_id)

            # Notify callback if registered
            if self.on_proposal_received:
                await self.on_proposal_received(proposal)

        except Exception as e:
            logger.error("Error handling proposal message from %s: %s", sender_node_id, e, exc_info=True)

    async def handle_vote_message(self, sender_node_id: str, payload: Dict[str, Any]) -> None:
        """Handle VOTE_KNOWLEDGE_COMMIT message from peer

        Args:
            sender_node_id: Node ID of the voter
            payload: Vote payload (dict format)
        """
        try:
            # Reconstruct vote from dict
            vote = CommitVote(
                proposal_id=payload.get('proposal_id'),
                voter_node_id=sender_node_id,
                vote=payload.get('vote'),
                comment=payload.get('comment'),
                timestamp=payload.get('timestamp', datetime.utcnow().isoformat()),
                is_required_dissent=payload.get('is_required_dissent', False)
            )

            # Process vote
            await self.receive_vote(vote)

            logger.info("Received vote from %s: %s", sender_node_id, vote.vote)

            # Notify callback if registered
            if self.on_vote_received:
                await self.on_vote_received(vote)

        except Exception as e:
            logger.error("Error handling vote message from %s: %s", sender_node_id, e, exc_info=True)


# Example usage
if __name__ == '__main__':
    from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource

    async def demo():
        print("=== ConsensusManager Demo ===\n")

        # Mock PCMCore
        class MockPCMCore:
            def load_context(self):
                from dpc_protocol.pcm_core import Profile, PersonalContext
                return PersonalContext(
                    profile=Profile(name="Test", description="Test user")
                )

            def save_context(self, context):
                print(f"   [PCMCore] Saved context version {context.version}")

        # Mock broadcast function
        async def mock_broadcast(message):
            print(f"   [Broadcast] {message['command']}")

        # Create manager
        manager = ConsensusManager(
            node_id="alice",
            pcm_core=MockPCMCore(),
            vote_timeout_minutes=5,
            consensus_threshold=0.75
        )

        # Create proposal
        entry = KnowledgeEntry(
            content="Environmental storytelling is powerful",
            tags=["game_design"],
            confidence=0.90,
            source=KnowledgeSource(type="ai_summary")
        )

        proposal = KnowledgeCommitProposal(
            conversation_id="conv-demo",
            topic="game_design",
            summary="Add environmental storytelling principle",
            entries=[entry],
            participants=["alice", "bob", "charlie"],
            avg_confidence=0.90
        )

        print("1. Creating proposal:")
        print(f"   Topic: {proposal.topic}")
        print(f"   Participants: {', '.join(proposal.participants)}")
        print()

        # Start voting
        print("2. Starting voting session:")
        session = await manager.propose_commit(proposal, mock_broadcast)
        print(f"   Proposal ID: {proposal.proposal_id}")
        print(f"   Required dissenter: {session.required_dissenter}")
        print(f"   Deadline: {session.deadline}")
        print()

        # Cast votes
        print("3. Casting votes:")

        await manager.cast_vote(
            proposal_id=proposal.proposal_id,
            vote="approve",
            comment="Looks good!",
            broadcast_func=mock_broadcast
        )
        print(f"   [Alice] Voted: approve")

        # Simulate bob's vote
        bob_vote = CommitVote(
            proposal_id=proposal.proposal_id,
            voter_node_id="bob",
            vote="approve",
            comment="Agreed"
        )
        await manager.receive_vote(bob_vote)
        print(f"   [Bob] Voted: approve")

        # Simulate charlie's vote (required dissenter)
        charlie_vote = CommitVote(
            proposal_id=proposal.proposal_id,
            voter_node_id="charlie",
            vote="approve",
            comment="Good, but we should document exceptions",
            is_required_dissent=True
        )
        await manager.receive_vote(charlie_vote)
        print(f"   [Charlie] Voted: approve (as required dissenter)")
        print()

        # Check result
        print("4. Voting result:")
        session = manager.get_session(proposal.proposal_id)
        print(f"   Status: {session.status}")
        print(f"   Votes: {len(session.votes)}/{len(proposal.participants)}")
        print()

        # Stats
        print("5. Active sessions:")
        active = manager.get_active_sessions()
        print(f"   Count: {len(active)}")

    asyncio.run(demo())
    print("\n=== Demo Complete ===")
