"""
Knowledge Commit Protocol - Phase 4

Implements git-like knowledge commits with consensus and bias mitigation.
Inspired by Personal Context Manager and cognitive bias research.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

from .pcm_core import KnowledgeEntry, KnowledgeSource


@dataclass
class KnowledgeCommitProposal:
    """Proposed knowledge commit awaiting consensus approval

    Similar to git commits, but requires multi-party approval and
    includes bias mitigation metadata.
    """

    # Basic info
    proposal_id: str = field(default_factory=lambda: f"proposal-{uuid.uuid4().hex[:8]}")
    conversation_id: str = ""
    topic: str = ""  # Topic name this commit applies to
    summary: str = ""  # One-line summary (like git commit message)

    # Knowledge content
    entries: List[KnowledgeEntry] = field(default_factory=list)

    # Participants and consensus
    participants: List[str] = field(default_factory=list)  # Node IDs
    proposed_by: str = "ai"  # "ai" or node_id
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Bias mitigation tracking (Phase 2 integration)
    cultural_perspectives: List[str] = field(default_factory=list)  # ["Western", "Eastern", etc.]
    alternatives: List[str] = field(default_factory=list)  # Alternative interpretations
    flagged_assumptions: List[str] = field(default_factory=list)  # Identified cultural assumptions

    # Required dissent (anti-groupthink)
    devil_advocate: Optional[str] = None  # AI-generated critique
    required_dissenter: Optional[str] = None  # Randomly assigned dissenter
    dissenting_opinions: List[Dict[str, str]] = field(default_factory=list)  # [{node_id: opinion}]

    # AI confidence
    avg_confidence: float = 1.0  # Average confidence across entries

    # Voting status
    status: Literal["proposed", "voting", "approved", "rejected", "revised"] = "proposed"
    votes: Dict[str, Literal["approve", "reject", "request_changes"]] = field(default_factory=dict)
    vote_deadline: Optional[str] = None  # ISO timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'KnowledgeCommitProposal':
        """Create from dictionary"""
        # Handle nested KnowledgeEntry objects
        entries_data = data.get('entries', [])
        entries = []
        for entry_data in entries_data:
            source_data = entry_data.get('source')
            source = KnowledgeSource(**source_data) if source_data else None
            entries.append(KnowledgeEntry(
                content=entry_data.get('content', ''),
                tags=entry_data.get('tags', []),
                source=source,
                confidence=entry_data.get('confidence', 1.0),
                last_updated=entry_data.get('last_updated', datetime.utcnow().isoformat()),
                edited_by=entry_data.get('edited_by'),  # Phase 5 - inline editing
                edited_at=entry_data.get('edited_at'),  # Phase 5 - inline editing
                usage_count=entry_data.get('usage_count', 0),
                effectiveness_score=entry_data.get('effectiveness_score', 1.0),
                review_due=entry_data.get('review_due'),
                cultural_specific=entry_data.get('cultural_specific', False),
                requires_context=entry_data.get('requires_context', []),
                alternative_viewpoints=entry_data.get('alternative_viewpoints', [])
            ))

        return cls(
            proposal_id=data.get('proposal_id', f"proposal-{uuid.uuid4().hex[:8]}"),
            conversation_id=data.get('conversation_id', ''),
            topic=data.get('topic', ''),
            summary=data.get('summary', ''),
            entries=entries,
            participants=data.get('participants', []),
            proposed_by=data.get('proposed_by', 'ai'),
            timestamp=data.get('timestamp', datetime.utcnow().isoformat()),
            cultural_perspectives=data.get('cultural_perspectives', []),
            alternatives=data.get('alternatives', []),
            flagged_assumptions=data.get('flagged_assumptions', []),
            devil_advocate=data.get('devil_advocate'),
            required_dissenter=data.get('required_dissenter'),
            dissenting_opinions=data.get('dissenting_opinions', []),
            avg_confidence=data.get('avg_confidence', 1.0),
            status=data.get('status', 'proposed'),
            votes=data.get('votes', {}),
            vote_deadline=data.get('vote_deadline')
        )


@dataclass
class CommitVote:
    """Individual vote on a knowledge commit proposal"""

    proposal_id: str
    voter_node_id: str
    vote: Literal["approve", "reject", "request_changes"]
    comment: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Dissent tracking (if this voter was assigned as devil's advocate)
    is_required_dissent: bool = False


@dataclass
class KnowledgeCommit:
    """Finalized knowledge commit (after consensus approval)

    This is the immutable record that gets added to commit history.
    """

    # Commit identification
    commit_id: str = field(default_factory=lambda: f"commit-{uuid.uuid4().hex[:8]}")
    parent_commit_id: Optional[str] = None

    # Commit message (git-style)
    summary: str = ""  # One-line summary
    description: str = ""  # Detailed description

    # Content
    topic: str = ""
    entries: List[KnowledgeEntry] = field(default_factory=list)

    # Provenance
    conversation_id: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Consensus tracking
    consensus_type: Literal["unanimous", "majority", "disputed"] = "unanimous"
    approved_by: List[str] = field(default_factory=list)
    rejected_by: List[str] = field(default_factory=list)

    # Bias mitigation metadata
    cultural_perspectives_considered: List[str] = field(default_factory=list)
    confidence_score: float = 1.0
    sources_cited: List[str] = field(default_factory=list)
    dissenting_opinion: Optional[str] = None  # Preserved dissent for historical record

    # Cryptographic integrity (Phase 8)
    commit_hash: Optional[str] = None  # Full SHA256 hash (64 chars)
    signatures: Dict[str, str] = field(default_factory=dict)  # node_id -> base64 signature

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for commit history storage"""
        return asdict(self)

    def compute_hash(self) -> str:
        """
        Compute hash-based commit ID.

        This creates a content-addressable commit ID based on the
        SHA256 hash of the commit content. Same content = same hash
        across all devices.

        Sets:
            self.commit_hash = full SHA256 hash (64 chars)
            self.commit_id = "commit-{hash[:16]}"

        Returns:
            Full hash (64 chars)
        """
        from .commit_integrity import compute_commit_hash

        self.commit_hash = compute_commit_hash(self)
        self.commit_id = f"commit-{self.commit_hash[:16]}"

        return self.commit_hash

    def sign(self, node_id: str, private_key: rsa.RSAPrivateKey):
        """
        Sign this commit with node's private key.

        Args:
            node_id: Node identifier (e.g., "dpc-node-abc123")
            private_key: RSA private key for signing

        Raises:
            ValueError: If commit_hash is not computed yet
        """
        from .commit_integrity import CommitSigner

        if not self.commit_hash:
            raise ValueError("Must compute hash before signing (call compute_hash() first)")

        signer = CommitSigner(node_id, private_key)
        self.signatures[node_id] = signer.sign_commit(self.commit_hash)

    def verify_signatures(self) -> bool:
        """
        Verify all signatures in this commit.

        Returns:
            True if all signatures are valid, False otherwise
        """
        from .commit_integrity import CommitSigner

        if not self.commit_hash:
            return False

        for node_id, signature in self.signatures.items():
            if not CommitSigner.verify_signature(node_id, self.commit_hash, signature):
                return False

        return True

    def verify_hash(self) -> bool:
        """
        Verify commit hash matches content.

        Returns:
            True if hash is valid
        """
        from .commit_integrity import verify_commit_hash
        return verify_commit_hash(self)

    def format_commit_message(self) -> str:
        """Format as git-style commit message

        Returns:
            Formatted commit message
        """
        lines = []

        # Header
        lines.append(f"Topic: {self.topic}")
        lines.append(f"Summary: {self.summary}")
        lines.append("")

        # Description
        if self.description:
            lines.append(self.description)
            lines.append("")

        # Bias mitigation metadata
        if self.cultural_perspectives_considered:
            lines.append("Cultural Perspectives Considered:")
            for perspective in self.cultural_perspectives_considered:
                lines.append(f"- {perspective}")
            lines.append("")

        # Confidence and sources
        lines.append(f"Confidence: {self.confidence_score:.0%}")
        if self.sources_cited:
            lines.append(f"Sources: {', '.join(self.sources_cited)}")
        lines.append("")

        # Consensus info
        lines.append(f"Participants: {', '.join(self.participants)}")
        lines.append(f"Consensus: {self.consensus_type.title()} ({len(self.approved_by)}/{len(self.participants)})")

        if self.dissenting_opinion:
            lines.append("")
            lines.append("Dissent Recorded:")
            lines.append(self.dissenting_opinion)

        lines.append("")
        lines.append(f"Commit-ID: {self.commit_id}")
        if self.parent_commit_id:
            lines.append(f"Parent: {self.parent_commit_id}")

        return "\n".join(lines)


# Protocol message types for P2P communication

@dataclass
class ProposeKnowledgeCommitMessage:
    """Message to propose a knowledge commit to peers"""

    command: str = "PROPOSE_KNOWLEDGE_COMMIT"
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, proposal: KnowledgeCommitProposal) -> 'ProposeKnowledgeCommitMessage':
        """Create message from proposal"""
        return cls(payload=proposal.to_dict())


@dataclass
class VoteKnowledgeCommitMessage:
    """Message to vote on a knowledge commit proposal"""

    command: str = "VOTE_KNOWLEDGE_COMMIT"
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, vote: CommitVote) -> 'VoteKnowledgeCommitMessage':
        """Create message from vote"""
        return cls(payload=asdict(vote))


@dataclass
class ApplyKnowledgeCommitMessage:
    """Message to apply approved commit to all participants"""

    command: str = "APPLY_KNOWLEDGE_COMMIT"
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, commit: KnowledgeCommit) -> 'ApplyKnowledgeCommitMessage':
        """Create message from commit"""
        return cls(payload=commit.to_dict())


@dataclass
class RequestCommitRevisionMessage:
    """Message to request changes to a commit proposal"""

    command: str = "REQUEST_COMMIT_REVISION"
    payload: Dict[str, Any] = field(default_factory=dict)  # {proposal_id, requested_changes, requester_id}


# Commit effectiveness tracking (Phase 6 preview)

@dataclass
class CommitEffectivenessMetrics:
    """Track how useful a commit was over time"""

    commit_id: str

    # Usage tracking
    times_referenced: int = 0
    times_edited: int = 0
    last_accessed: Optional[str] = None

    # User feedback
    helpful_count: int = 0
    unhelpful_count: int = 0
    effectiveness_score: float = 0.0

    # Quality indicators
    confidence_at_commit: float = 1.0
    confidence_after_usage: float = 1.0  # Adjusted based on feedback
    cultural_applicability: Dict[str, int] = field(default_factory=dict)  # {"Western": 5, "Eastern": 3}

    def update_effectiveness(self) -> None:
        """Recalculate effectiveness score based on feedback"""
        total_feedback = self.helpful_count + self.unhelpful_count
        if total_feedback > 0:
            self.effectiveness_score = self.helpful_count / total_feedback


# Example usage
if __name__ == '__main__':
    logger.info("Knowledge Commit Protocol Demo")

    # 1. Create a knowledge entry
    entry = KnowledgeEntry(
        content="Environmental storytelling is more powerful than explicit exposition in game design.",
        tags=["game_design", "narrative", "environmental_storytelling"],
        confidence=0.90,
        source=KnowledgeSource(
            type="ai_summary",
            conversation_id="conv-abc123",
            participants=["alice", "bob"],
            cultural_perspectives_considered=["Western", "Eastern"],
            confidence_score=0.90
        ),
        alternative_viewpoints=[
            "Explicit narrative works better for complex lore (visual novels, RPGs)",
            "Audio logs provide good middle ground (BioShock, Gone Home)"
        ]
    )

    # 2. Create a commit proposal
    proposal = KnowledgeCommitProposal(
        conversation_id="conv-abc123",
        topic="game_design_philosophy",
        summary="Add environmental storytelling principle from team discussion",
        entries=[entry],
        participants=["alice", "bob"],
        proposed_by="ai",
        cultural_perspectives=["Western", "Eastern", "Indigenous"],
        alternatives=["Dialogue-heavy approach", "Environmental + audio logs"],
        avg_confidence=0.90,
        devil_advocate="This principle may not apply to story-driven games requiring complex narrative exposition."
    )

    logger.info("1. Proposal Created:")
    logger.info("   ID: %s", proposal.proposal_id)
    logger.info("   Topic: %s", proposal.topic)
    logger.info("   Summary: %s", proposal.summary)
    logger.info("   Participants: %s", ', '.join(proposal.participants))
    logger.info("   Confidence: %.0f%%", proposal.avg_confidence * 100)
    logger.info("   Devil's Advocate: %s...", proposal.devil_advocate[:80])

    # 3. Simulate voting
    vote1 = CommitVote(
        proposal_id=proposal.proposal_id,
        voter_node_id="alice",
        vote="approve",
        comment="Agreed, this aligns with our design philosophy"
    )

    vote2 = CommitVote(
        proposal_id=proposal.proposal_id,
        voter_node_id="bob",
        vote="approve",
        comment="Yes, but we should document exceptions"
    )

    proposal.votes = {"alice": "approve", "bob": "approve"}
    proposal.status = "approved"

    logger.info("2. Votes Cast:")
    logger.info("   Alice: %s - %s", vote1.vote, vote1.comment)
    logger.info("   Bob: %s - %s", vote2.vote, vote2.comment)
    logger.info("   Status: %s", proposal.status)

    # 4. Create finalized commit
    commit = KnowledgeCommit(
        commit_id=f"commit-{uuid.uuid4().hex[:8]}",
        summary=proposal.summary,
        description="Team brainstorming session on narrative design patterns.",
        topic=proposal.topic,
        entries=proposal.entries,
        conversation_id=proposal.conversation_id,
        participants=proposal.participants,
        consensus_type="unanimous",
        approved_by=["alice", "bob"],
        cultural_perspectives_considered=proposal.cultural_perspectives,
        confidence_score=proposal.avg_confidence,
        sources_cited=["alice", "bob", "Team discussion"],
        dissenting_opinion=proposal.devil_advocate
    )

    logger.info("3. Finalized Commit:")
    logger.info(commit.format_commit_message())

    # 5. Protocol messages
    propose_msg = ProposeKnowledgeCommitMessage.create(proposal)
    logger.info("4. Protocol Message: %s", propose_msg.command)
    logger.info("   Payload keys: %s", list(propose_msg.payload.keys()))

    # 6. Effectiveness tracking
    metrics = CommitEffectivenessMetrics(commit_id=commit.commit_id)
    metrics.helpful_count = 5
    metrics.unhelpful_count = 1
    metrics.update_effectiveness()

    logger.info("5. Effectiveness Metrics:")
    logger.info("   Helpful: %d", metrics.helpful_count)
    logger.info("   Unhelpful: %d", metrics.unhelpful_count)
    logger.info("   Effectiveness: %.0f%%", metrics.effectiveness_score * 100)

    logger.info("Demo Complete")
