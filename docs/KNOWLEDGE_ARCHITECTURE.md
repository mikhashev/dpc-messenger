# DPC Messenger: Knowledge Architecture & Context Management

**Version:** 2.0
**Date:** November 2025
**Status:** Partial Implementation (v0.8.0)

---

## Implementation Status (v0.8.0)

### Completed Features (Phase 1 - Federated MVP)

| Phase | Feature | Status | Evidence |
|-------|---------|--------|----------|
| 1 | Enhanced PCM Structure | Complete | `dpc-protocol/dpc_protocol/pcm_core.py` |
| 2 | Instruction Processing | Partial | Instructions exist, bias mitigation pending |
| 3 | Markdown Files | Not Started | Planned for Phase 2 |
| 4 | Knowledge Commit Protocol | Partial | Consensus voting complete, git-like commits pending |
| 5 | Context Retrieval | Not Started | @-mentions planned for Phase 2 |
| 6 | Self-Improvement Tracking | Partial | Effectiveness tracking complete |
| 7 | Cognitive Bias Mitigation | Not Started | Planned for Phase 2 |
| 8 | Collaborative Knowledge Building | Complete | Multi-peer consensus voting with devil's advocate |

**What Works in v0.8.0:**
- Basic PCM structure with profile, knowledge, preferences
- Consensus voting system (`consensus_manager.py`)
- Multi-peer knowledge approval workflow
- Devil's advocate mechanism (force_dissent)
- Effectiveness tracking (`effectiveness_tracker.py`)
- Personal context sharing with firewall rules

**What's Next (Phase 2 - Team Collaboration):**
- Markdown knowledge files (Claude Code-style)
- @-mention context retrieval
- Semantic search across team contexts
- Git-like knowledge commit history
- Full cognitive bias mitigation
- Group chat knowledge extraction

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Learning from Modern AI Systems](#learning-from-modern-ai-systems)
3. [Core Architecture](#core-architecture)
4. [Cognitive Bias Mitigation](#cognitive-bias-mitigation)
5. [Personal Context Structure](#personal-context-structure)
6. [Knowledge Commit Protocol](#knowledge-commit-protocol)
7. [AI as Knowledge Curator](#ai-as-knowledge-curator)
8. [Implementation Patterns](#implementation-patterns)
9. [Migration Path](#migration-path)

---

## 1. Executive Summary

### The Vision

DPC Messenger transforms ephemeral conversations into permanent, versioned knowledge through AI-curated **Knowledge Commits**. Inspired by:

- **Git**: Version control for knowledge with commit messages and provenance
- **Claude Code**: Persistent context across sessions via structured files
- **Cursor/Windsurf**: Semantic context retrieval with @-mentions
- **Personal Context Manager**: Instruction-driven AI personalization
- **Academic Research**: Cognitive bias mitigation in LLM interactions

### Key Differentiators

| System | Context Model | Collaboration | Bias Mitigation | Versioning |
|--------|--------------|---------------|-----------------|------------|
| **Claude Code** | `.claude/` markdown files | ❌ Single user | ❌ None | Via git |
| **Cursor** | Codebase indexing | ❌ Single user | ❌ None | None |
| **ChatGPT Memory** | Auto-collected, cloud | ❌ Single user | ❌ None | None |
| **Personal Context Manager** | JSON + instructions | ❌ Single user | ⚠️ Manual | Manual or Via git |
| **DPC Messenger** | **PCM + Instructions** | ✅ **Multi-user consensus** | ✅ **Built-in** | ✅ **Git-like** |

### Core Innovation

**Transactional Knowledge Building:** Conversations are atomic transactions that either commit knowledge or are discarded, with multi-party consensus and AI curation guided by bias-resistant instructions.

---

## 2. Learning from Modern AI Systems

### 2.1 Claude Code's Context System

**What Claude Code Does Well:**

```
Project Structure:
.claude/
├── context.md          # Project overview, decisions
├── progress.md         # Current tasks, blockers
├── architecture.md     # System design
└── conventions.md      # Code style, patterns
```

**Strengths:**
- ✅ Human-readable (markdown)
- ✅ Persistent across sessions
- ✅ Git-versionable
- ✅ Explicit file structure
- ✅ AI can read/write autonomously

**Limitations:**
- ❌ Single user only
- ❌ No instruction blocks for AI behavior
- ❌ No bias mitigation
- ❌ No collaborative consensus
- ❌ Code-specific (not general knowledge)

**What DPC Adopts:**
- File-based persistence
- Markdown for human readability
- Autonomous AI updates (with approval)
- Clear separation of concerns (multiple files)

### 2.2 Cursor/Windsurf Context Patterns

**What They Do Well:**

```python
# @-mention syntax for explicit context
@file:utils.py
@folder:src/components
@symbol:DatabaseConnection
@docs
@web
```

**Strengths:**
- ✅ Explicit context selection
- ✅ Semantic search over codebase
- ✅ Multi-file context aggregation
- ✅ Smart relevance ranking

**Limitations:**
- ❌ No long-term memory (session-based)
- ❌ No versioning of retrieved context
- ❌ No collaborative features
- ❌ No instruction blocks

**What DPC Adopts:**
- `@user:alice` syntax for context selection
- `@topic:python_learning` for specific knowledge
- Semantic search over personal contexts
- Relevance ranking for knowledge retrieval

### 2.3 Personal Context Manager (PCM) Principles

**What PCM Does Well:**

```json
{
  "data": { /* user knowledge */ },
  "instruction": {
    "primary": "How AI should use this data",
    "context_update": "When to suggest changes",
    "verification_protocol": "Require evidence",
    "adaptive_approach": "Adjust to user needs"
  }
}
```

**Strengths:**
- ✅ Explicit AI instructions
- ✅ User-controlled updates
- ✅ Structured data formats
- ✅ Privacy controls
- ✅ Self-improvement tracking

**Limitations:**
- ❌ Single user only
- ❌ No collaborative knowledge building
- ❌ No consensus mechanism
- ❌ No conversation-to-knowledge automation

**What DPC Adopts:**
- Instruction blocks for AI behavior
- Structured JSON + markdown hybrid
- User control over updates
- Self-improvement metrics

### 2.4 Cognitive Bias Research Insights

From [cognitive bias guide](https://github.com/mikhashev/personal-context-manager/blob/main/docs/cognitive%20bias%20in%20AI%20and%20LLMs%20a%20user's%20guide.md):

**Key Biases to Mitigate:**

1. **Status Quo Bias**: AI favors existing solutions when mentioned
2. **Framing Effect**: Same question, different framing → different answers
3. **Anchoring Bias**: Earlier context inappropriately influences later decisions
4. **Cultural Bias**: Western individualistic values overrepresented
5. **Primacy Effect**: AI favors information presented first

**What DPC Adopts:**

```python
# Built-in bias mitigation in instruction blocks
{
  "instruction": {
    "bias_awareness": {
      "require_multi_perspective": true,
      "challenge_status_quo": true,
      "cultural_sensitivity": "Consider non-Western approaches",
      "framing_neutrality": "Present options without preference"
    }
  }
}
```

---

## 3. Core Architecture

### 3.1 Layered Context Model

```
┌─────────────────────────────────────────────────────────────┐
│                     User Interface Layer                    │
│  (Chat, Context Viewer, Commit Approval, Knowledge Graph)  │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────┴───────────────────────────────────────┐
│                  Context Management Layer                    │
│  - Context Aggregation (local + peers)                      │
│  - Instruction Processing                                    │
│  - Bias Mitigation Engine                                    │
│  - Semantic Search & Retrieval                               │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────┴───────────────────────────────────────┐
│                 Knowledge Curation Layer                     │
│  - Conversation Monitoring                                   │
│  - AI Summarization                                          │
│  - Knowledge Commit Proposals                                │
│  - Consensus Coordination                                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────┴───────────────────────────────────────┐
│                   Storage & Sync Layer                       │
│  - Local PCM Files (~/.dpc/personal.json)                   │
│  - Context Cache (in-memory + optional disk)                │
│  - Version History (git-like commits)                        │
│  - Peer Context Storage (ephemeral by default)              │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 File Structure

```
~/.dpc/
├── personal.json                 # Main PCM file (JSON + instructions)
├── knowledge/                    # Topic-specific markdown files
│   ├── python_learning.md
│   ├── game_design_philosophy.md
│   └── project_crystal_caverns.md
├── .knowledge_history/           # Git-like commit history
│   ├── commits.json
│   └── snapshots/
├── instructions.yaml             # Global AI behavior rules
├── cognitive_profile.json        # Learning preferences, biases
└── privacy_rules.json                 # Firewall rules (existing)
```

**Rationale:**
- **Hybrid format**: JSON for structured data, markdown for human-readable knowledge
- **Separation**: Instructions separate from data (easier to share/update)
- **Versioning**: Explicit history directory (like `.git/`)
- **Modularity**: Topic files can be shared independently

---

## 4. Cognitive Bias Mitigation

### 4.1 Bias-Resistant Instruction Template

```yaml
# instructions.yaml
ai_behavior:
  bias_mitigation:
    enabled: true
    strategies:
      - multi_perspective_analysis
      - status_quo_challenge
      - cultural_sensitivity
      - framing_neutrality
      - evidence_requirement

  prompt_preprocessing:
    detect_status_quo_bias: true
    neutralize_framing: true
    remove_anchoring: true

  response_validation:
    require_alternatives: 3  # Always provide 3+ options
    cultural_perspectives:
      - Western individualistic
      - Eastern collective
      - Indigenous holistic
    challenge_own_recommendations: true  # Devil's advocate

knowledge_curation:
  consensus_rules:
    require_multi_cultural_validation: true
    avoid_groupthink:
      force_dissent: true  # One person must argue opposite view
      anonymous_voting: true

  commit_review:
    bias_checklist:
      - "Does this favor Western values?"
      - "Are there cultural alternatives?"
      - "Is this based on stereotypes?"
      - "Would this work in non-Western contexts?"
```

### 4.2 Multi-Perspective Prompting

**Built into AI query assembly:**

```python
def _assemble_bias_resistant_prompt(contexts, query, instructions):
    """Build prompt with explicit bias mitigation"""

    system_instruction = f"""
You are a knowledge curator with strong bias-awareness training.

BIAS MITIGATION RULES:
1. Challenge Status Quo: Always question existing approaches
2. Multi-Cultural: Consider perspectives from at least 3 cultures
3. Framing Neutrality: Present options without preference
4. Evidence-Based: Require citations and reasoning
5. Devil's Advocate: Argue against your initial recommendation

COGNITIVE BIASES TO AVOID:
- Status quo bias (favoring current methods)
- Anchoring (overweighting earlier information)
- Cultural bias (Western-centric solutions)
- Groupthink (consensus without critical evaluation)
- Primacy effect (favoring first-presented options)

CONTEXT INTERPRETATION INSTRUCTIONS:
{format_instructions(instructions)}

When proposing knowledge commits:
- Verify information from multiple sources
- Include confidence scores (0.0-1.0)
- Flag culturally-specific assumptions
- Suggest alternative interpretations
"""

    # Add cultural context prompts
    for source_id, context in contexts.items():
        cultural_context = context.cognitive_profile.get('cultural_background')
        if cultural_context:
            system_instruction += f"\n[{source_id}] Cultural Context: {cultural_context}"

    return system_instruction + format_contexts(contexts) + query
```

### 4.3 Consensus with Required Dissent

**Prevent groupthink in knowledge commits:**

```python
@dataclass
class KnowledgeCommit:
    commit_id: str
    summary: str
    proposed_entries: Dict[str, List[KnowledgeEntry]]

    # Bias mitigation fields
    cultural_perspectives_considered: List[str]  # ["Western", "Eastern", "Indigenous"]
    alternative_interpretations: List[str]
    confidence_score: float  # AI's confidence
    sources_cited: List[str]

    # Required dissent for bias mitigation
    dissenting_opinion_required: bool = True
    dissenting_opinion: Optional[str] = None
    devil_advocate_analysis: Optional[str] = None

async def require_dissent_before_commit(commit: KnowledgeCommit, participants: List[str]):
    """Force at least one person to argue against the proposal"""

    if len(participants) < 2:
        return  # Can't require dissent with solo user

    # AI automatically generates devil's advocate view
    commit.devil_advocate_analysis = await ai_generate_dissenting_view(commit)

    # Randomly assign one participant as "required dissenter"
    dissenter = random.choice(participants)

    await notify_participant(dissenter, {
        "role": "devil_advocate",
        "task": "Please critique this proposal and identify weaknesses",
        "commit": commit,
        "ai_dissent": commit.devil_advocate_analysis
    })
```

---

## 5. Personal Context Structure

### 5.1 Enhanced PCM Schema

```python
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime

@dataclass
class BiasAwareness:
    """Tracks user's known biases and mitigation strategies"""
    known_biases: List[str] = field(default_factory=list)  # ["confirmation_bias", "status_quo_bias"]
    mitigation_strategies: Dict[str, str] = field(default_factory=dict)
    cultural_blind_spots: List[str] = field(default_factory=list)
    preferred_perspectives: List[str] = field(default_factory=list)  # ["pragmatic", "systems_thinking"]

@dataclass
class InstructionBlock:
    """AI behavior instructions - from Personal Context Manager"""

    # Core instructions
    primary: str = "Use this context to provide personalized assistance"
    context_update: str = "Suggest updates when new insights emerge"
    verification_protocol: str = "Provide reasoning and evidence"

    # Learning support (from self-education template)
    learning_support: Dict[str, str] = field(default_factory=lambda: {
        "explanations": "Connect to existing knowledge",
        "practice": "Generate active recall questions",
        "metacognition": "Help reflect on learning process",
        "connections": "Identify relationships between concepts"
    })

    # Bias mitigation (NEW - from cognitive bias research)
    bias_mitigation: Dict[str, Any] = field(default_factory=lambda: {
        "require_multi_perspective": True,
        "challenge_status_quo": True,
        "cultural_sensitivity": "Consider non-Western approaches",
        "framing_neutrality": True,
        "evidence_requirement": "citations_preferred"
    })

    # Collaboration (DPC-specific)
    collaboration_mode: Literal["individual", "group", "public"] = "individual"
    consensus_required: bool = True
    ai_curation_enabled: bool = True
    dissent_encouraged: bool = True  # NEW: Require devil's advocate

@dataclass
class CognitiveProfile:
    """User's learning style and cognitive preferences"""

    # Learning characteristics
    memory_strengths: List[str] = field(default_factory=list)
    memory_challenges: List[str] = field(default_factory=list)
    optimal_learning_times: Dict[str, List[str]] = field(default_factory=dict)
    attention_span: Dict[str, str] = field(default_factory=dict)

    # Cultural context (NEW - for bias mitigation)
    cultural_background: str = ""  # "Eastern collective", "Western individualistic", etc.
    cultural_values: List[str] = field(default_factory=list)
    communication_norms: Dict[str, str] = field(default_factory=dict)

    # Bias awareness (NEW)
    bias_profile: Optional[BiasAwareness] = None

@dataclass
class KnowledgeSource:
    """Provenance tracking for knowledge entries"""

    type: Literal["conversation", "ai_summary", "manual_edit", "import", "consensus"]
    conversation_id: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Consensus tracking
    consensus_status: Literal["draft", "approved", "rejected"] = "draft"
    approved_by: List[str] = field(default_factory=list)
    dissenting_opinions: List[str] = field(default_factory=list)  # NEW
    commit_id: Optional[str] = None

    # Bias tracking (NEW)
    cultural_perspectives_considered: List[str] = field(default_factory=list)
    confidence_score: float = 1.0
    sources_cited: List[str] = field(default_factory=list)

@dataclass
class KnowledgeEntry:
    """Individual knowledge item with full provenance and bias tracking"""

    content: str
    tags: List[str] = field(default_factory=list)
    source: Optional[KnowledgeSource] = None

    # AI metadata
    confidence: float = 1.0
    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Self-improvement metrics (from PCM)
    usage_count: int = 0
    effectiveness_score: float = 1.0
    review_due: Optional[str] = None  # Spaced repetition

    # Bias flags (NEW)
    cultural_specific: bool = False
    requires_context: List[str] = field(default_factory=list)  # ["Western workplace", "individualistic culture"]
    alternative_viewpoints: List[str] = field(default_factory=list)

@dataclass
class Topic:
    """Knowledge topic with versioning and learning metadata"""

    summary: str
    entries: List[KnowledgeEntry] = field(default_factory=list)

    # References
    key_books: List[Book] = field(default_factory=list)
    preferred_authors: List[str] = field(default_factory=list)

    # Learning metadata
    mastery_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    learning_strategies: List[str] = field(default_factory=list)

    # Versioning
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_modified: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Linked markdown file (NEW - from Claude Code pattern)
    markdown_file: Optional[str] = None  # e.g., "knowledge/python_learning.md"

@dataclass
class PersonalContext:
    """Complete Personal Context Model - integrating all systems"""

    # Core data (DPC original)
    profile: Profile
    knowledge: Dict[str, Topic] = field(default_factory=dict)
    preferences: Optional[Preferences] = None

    # Instructions (from PCM)
    instruction: InstructionBlock = field(default_factory=InstructionBlock)

    # Cognitive profile (from self-education template + bias awareness)
    cognitive_profile: Optional[CognitiveProfile] = None

    # Git-like versioning
    version: int = 1
    last_commit_id: Optional[str] = None
    last_commit_message: Optional[str] = None
    last_commit_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    commit_history: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=lambda: {
        "created": datetime.utcnow().isoformat(),
        "last_updated": datetime.utcnow().isoformat(),
        "storage": "local",
        "format_version": "2.0"
    })
```

### 5.2 Hybrid Format: JSON + Markdown

**Main structure (personal.json):**
```json
{
  "profile": { "name": "Alice", "description": "Game designer" },
  "knowledge": {
    "game_design": {
      "summary": "Game design philosophy and patterns",
      "markdown_file": "knowledge/game_design_philosophy.md",
      "entries": [],
      "version": 5
    }
  },
  "instruction": {
    "primary": "Use my design philosophy when suggesting ideas",
    "bias_mitigation": {
      "require_multi_perspective": true,
      "cultural_sensitivity": "Consider accessibility and diverse player backgrounds"
    }
  },
  "cognitive_profile": {
    "cultural_background": "Western individualistic with awareness of collectivist design",
    "bias_profile": {
      "known_biases": ["sunk_cost_fallacy", "novelty_bias"],
      "mitigation_strategies": {
        "sunk_cost": "Always evaluate current value, not past investment"
      }
    }
  }
}
```

**Linked markdown file (knowledge/game_design_philosophy.md):**
```markdown
# Game Design Philosophy

## Core Principles

### Environmental Storytelling over Exposition
*Source: Team discussion 2025-01-10 (commit-abc123)*
*Confidence: 1.0*
*Cultural Context: Universal - tested across cultures*

Players discover narrative through level design and environmental clues rather than explicit text/dialogue.

**Examples:**
- Crystal refraction puzzles reveal ancient civilization's light worship
- Broken tools hint at technological decline

**Alternative Approaches:**
- Explicit narrative (visual novels, text-heavy RPGs)
- Audio logs/collectibles (BioShock, Gone Home)

**When This Doesn't Work:**
- Complex sci-fi lore requiring exposition
- Games targeting younger audiences needing guidance

---

## Design Patterns

### Puzzle Design
*Last updated: 2025-01-12*
*Version: 3*

...
```

**Benefits of Hybrid Approach:**
- **JSON**: Structured, machine-parseable metadata
- **Markdown**: Human-readable, git-friendly, easy to edit
- **Best of both**: AI can parse JSON, humans can read/edit markdown
- **Like Claude Code**: Familiar to developers

---

## 6. Knowledge Commit Protocol

### 6.1 Commit Message Structure

**Inspired by git commits:**

```
Type: Brief summary (max 72 chars)

Detailed description of what knowledge was added/changed.

Cultural Perspectives Considered:
- Western individualistic
- Eastern collective
- Indigenous holistic

Confidence: 0.85
Sources: [conversation-xyz, user-manual-edit]

Participants: alice, bob
Consensus: Unanimous (2/2)
Dissent: None raised

Commit-ID: commit-abc123
Parent: commit-xyz789
```

### 6.2 Knowledge Commit Workflow

```python
# 1. AI Monitors Conversation
async def monitor_group_chat_for_knowledge(
    conversation_history: List[Message],
    participants: List[User]
) -> Optional[KnowledgeCommitProposal]:
    """AI analyzes conversation for knowledge-worthy content"""

    # Detect knowledge triggers
    if not detect_substantive_discussion(conversation_history):
        return None

    if not detect_consensus_signals(conversation_history):
        return None

    # Build bias-resistant summarization prompt
    summary_prompt = f"""
Analyze the following conversation and extract structured knowledge.

BIAS MITIGATION REQUIREMENTS:
1. Identify cultural assumptions in the discussion
2. Suggest alternative perspectives from at least 2 other cultures
3. Rate your confidence (0.0-1.0) in each extracted fact
4. Flag any statements that might be culturally specific
5. Provide sources/reasoning for claims

PARTICIPANTS:
{format_participant_contexts(participants)}

CONVERSATION:
{format_messages(conversation_history)}

Extract:
- Topic name
- Summary (one sentence)
- Knowledge entries (structured facts/decisions)
- Cultural perspectives considered
- Confidence scores
- Alternative viewpoints

Output JSON.
"""

    extracted = await llm.generate(summary_prompt)

    # Build commit proposal
    proposal = KnowledgeCommitProposal(
        summary=extracted['summary'],
        entries=extracted['entries'],
        participants=[p.node_id for p in participants],
        cultural_perspectives=extracted['cultural_perspectives'],
        confidence_scores=extracted['confidence_scores'],
        alternatives=extracted['alternatives']
    )

    # Generate devil's advocate critique
    proposal.devil_advocate = await generate_dissenting_view(proposal)

    return proposal


# 2. Present to Group with Bias Info
async def present_commit_proposal(proposal: KnowledgeCommitProposal):
    """Show proposal to all participants with bias transparency"""

    ui_message = f"""
📝 **Knowledge Commit Proposed**

**Summary:** {proposal.summary}

**Proposed Knowledge:**
{format_entries(proposal.entries)}

**Cultural Perspectives Considered:**
{', '.join(proposal.cultural_perspectives)}

**AI Confidence:** {proposal.avg_confidence:.0%}

**Alternative Viewpoints:**
{format_alternatives(proposal.alternatives)}

**🔴 Devil's Advocate (Required Critical Analysis):**
{proposal.devil_advocate}

---

**Review Checklist:**
- [ ] Is this culturally neutral or are assumptions flagged?
- [ ] Are confidence scores reasonable?
- [ ] Are alternative views represented?
- [ ] Would this work in different cultural contexts?

[Approve] [Request Changes] [Reject]
"""

    await broadcast_to_participants(ui_message, proposal.participants)


# 3. Consensus with Required Dissent
async def collect_votes(proposal: KnowledgeCommitProposal):
    """Collect votes with forced dissent to prevent groupthink"""

    votes = {}

    # If 3+ participants, randomly assign devil's advocate
    if len(proposal.participants) >= 3:
        devil = random.choice(proposal.participants)
        await notify_participant(devil, {
            "role": "required_dissenter",
            "instruction": "You must critique this proposal (prevents groupthink)",
            "ai_critique": proposal.devil_advocate
        })

    # Collect votes
    for participant_id in proposal.participants:
        vote = await wait_for_vote(participant_id, timeout=300)  # 5 min
        votes[participant_id] = vote

    # Check consensus
    if all(v == "approve" for v in votes.values()):
        await apply_commit(proposal)
    elif any(v == "request_changes" for v in votes.values()):
        await revise_commit(proposal, votes)
    else:
        await reject_commit(proposal, votes)


# 4. Apply Commit to All Participants
async def apply_commit(proposal: KnowledgeCommitProposal):
    """Apply approved commit to each participant's PCM"""

    commit_id = f"commit-{uuid.uuid4().hex[:8]}"

    for participant_id in proposal.participants:
        context = load_context(participant_id)

        # Add topic or update existing
        topic_name = proposal.topic
        if topic_name not in context.knowledge:
            # Create new topic
            context.knowledge[topic_name] = Topic(
                summary=proposal.summary,
                entries=proposal.entries,
                version=1
            )

            # Create markdown file
            md_path = f"knowledge/{topic_name}.md"
            create_markdown_file(md_path, proposal)
            context.knowledge[topic_name].markdown_file = md_path
        else:
            # Update existing topic
            topic = context.knowledge[topic_name]
            topic.entries.extend(proposal.entries)
            topic.version += 1
            topic.last_modified = datetime.utcnow().isoformat()

            # Update markdown file
            update_markdown_file(topic.markdown_file, proposal)

        # Update PCM metadata
        context.version += 1
        context.last_commit_id = commit_id
        context.last_commit_message = proposal.summary
        context.commit_history.append({
            "commit_id": commit_id,
            "timestamp": datetime.utcnow().isoformat(),
            "message": proposal.summary,
            "participants": proposal.participants,
            "consensus": "unanimous"
        })

        # Save
        pcm_core = PCMCore()
        pcm_core.save_context(context)

    # Notify success
    await broadcast_to_participants(
        f"✅ Knowledge committed: {commit_id}\n{proposal.summary}",
        proposal.participants
    )
```

### 6.3 Diff Visualization (Like Git)

**UI shows proposed changes:**

```diff
Topic: game_design_philosophy
Version: 4 → 5

+ ## Quest Design Principles
+ *Source: Team brainstorm 2025-01-13*
+ *Participants: alice, bob*
+ *Confidence: 0.90*
+
+ **Crystal Corruption Quest:**
+ Environmental puzzle using crystal refraction mechanics.
+ Approved by both designers after playtesting discussion.
+
+ **Cultural Note:** Puzzle-based gameplay universal across cultures,
+ but tutorial clarity may need localization.
+
+ **Alternative Approaches:**
+ - Combat-focused quest (rejected - doesn't fit level theme)
+ - Dialogue-heavy quest (considered for future area)

Metadata changes:
- last_modified: 2025-01-12 → 2025-01-13
- version: 4 → 5
- commit_history: +1 entry
```

---

## 7. AI as Knowledge Curator

### 7.1 Conversation Monitoring

**AI runs in background during group chats:**

```python
class ConversationMonitor:
    def __init__(self, conversation_id: str, participants: List[User]):
        self.conversation_id = conversation_id
        self.participants = participants
        self.message_buffer = []
        self.knowledge_score = 0.0  # How knowledge-worthy is this conversation?

    async def on_message(self, message: Message):
        """Called for each message in group chat"""
        self.message_buffer.append(message)

        # Update knowledge score
        self.knowledge_score = await self._calculate_knowledge_score()

        # Check if conversation is knowledge-worthy
        if self.knowledge_score > 0.7:  # Threshold
            # Check for consensus signals
            if self._detect_consensus():
                # Propose knowledge commit
                proposal = await self._generate_commit_proposal()
                await present_commit_proposal(proposal)

                # Reset
                self.message_buffer = []
                self.knowledge_score = 0.0

    async def _calculate_knowledge_score(self) -> float:
        """Calculate if conversation contains substantive knowledge"""

        # Use LLM to score conversation
        prompt = f"""
Analyze this conversation segment for knowledge-worthiness.

Score 0.0-1.0 based on:
- Substantive information (facts, decisions, insights): +0.3
- Multiple perspectives discussed: +0.2
- Consensus reached: +0.2
- Actionable conclusions: +0.2
- Novel ideas vs casual chat: +0.1

MESSAGES:
{format_messages(self.message_buffer[-10:])}  # Last 10 messages

Output JSON: {{"score": 0.0-1.0, "reasoning": "..."}}
"""

        result = await llm.generate(prompt)
        return result['score']

    def _detect_consensus(self) -> bool:
        """Detect if group has reached agreement"""

        # Look for consensus signals
        signals = [
            "sounds good",
            "agreed",
            "let's go with",
            "I'm on board",
            "works for me",
            "approved",
            "✅"
        ]

        recent_messages = self.message_buffer[-5:]
        consensus_count = sum(
            1 for msg in recent_messages
            if any(signal in msg.text.lower() for signal in signals)
        )

        # Need majority of participants to express agreement
        threshold = len(self.participants) * 0.6
        return consensus_count >= threshold
```

### 7.2 Bias-Resistant Summarization

**AI generates summaries with explicit bias mitigation:**

```python
async def generate_commit_proposal(
    conversation: List[Message],
    participants: List[User]
) -> KnowledgeCommitProposal:
    """Generate knowledge commit with bias awareness"""

    # Load participants' cultural contexts
    cultural_contexts = []
    for participant in participants:
        ctx = load_context(participant.node_id)
        if ctx.cognitive_profile and ctx.cognitive_profile.cultural_background:
            cultural_contexts.append(ctx.cognitive_profile.cultural_background)

    # Build bias-resistant prompt
    prompt = f"""
You are a knowledge curator trained in cognitive bias mitigation.

PARTICIPANTS' CULTURAL CONTEXTS:
{', '.join(cultural_contexts)}

CONVERSATION:
{format_messages(conversation)}

TASK: Extract structured knowledge following these STRICT RULES:

1. BIAS MITIGATION:
   - Identify cultural assumptions in the discussion
   - Provide alternative perspectives from non-represented cultures
   - Flag statements that may only apply in specific contexts
   - Rate confidence (0.0-1.0) for each claim
   - Cite sources (participant names, external refs if mentioned)

2. MULTI-PERSPECTIVE REQUIREMENT:
   - Consider at least 3 cultural perspectives:
     * Western individualistic
     * Eastern collective
     * Indigenous/holistic
   - For each knowledge entry, state: "Universal" or "Context: [culture]"

3. EVIDENCE REQUIREMENT:
   - Every claim needs reasoning
   - Flag opinions vs facts
   - Note where participants disagreed

4. DEVIL'S ADVOCATE:
   - Generate critique of your own summary
   - Identify potential weaknesses
   - Suggest what might be missing

OUTPUT FORMAT (JSON):
{{
  "topic": "brief_topic_name",
  "summary": "One sentence overview",
  "entries": [
    {{
      "content": "Knowledge statement",
      "tags": ["tag1", "tag2"],
      "confidence": 0.0-1.0,
      "cultural_context": "Universal" or "Context: Western workplace",
      "sources": ["alice", "bob"],
      "reasoning": "Why this is notable"
    }}
  ],
  "cultural_perspectives": ["Western individualistic", "..."],
  "alternatives": [
    "Alternative perspective 1",
    "Alternative perspective 2"
  ],
  "devil_advocate": "Critical analysis of this summary",
  "flagged_assumptions": ["Assumption 1", "..."]
}}
"""

    result = await llm.generate(prompt)

    # Build proposal
    proposal = KnowledgeCommitProposal(
        conversation_id=conversation[0].conversation_id,
        topic=result['topic'],
        summary=result['summary'],
        entries=[
            KnowledgeEntry(
                content=e['content'],
                tags=e['tags'],
                source=KnowledgeSource(
                    type="ai_summary",
                    conversation_id=conversation[0].conversation_id,
                    participants=[p.node_id for p in participants],
                    confidence_score=e['confidence'],
                    sources_cited=e['sources']
                ),
                confidence=e['confidence'],
                cultural_specific=e['cultural_context'] != "Universal",
                requires_context=[e['cultural_context']] if e['cultural_context'] != "Universal" else []
            )
            for e in result['entries']
        ],
        participants=[p.node_id for p in participants],
        cultural_perspectives=result['cultural_perspectives'],
        alternatives=result['alternatives'],
        devil_advocate=result['devil_advocate'],
        flagged_assumptions=result['flagged_assumptions']
    )

    return proposal
```

### 7.3 Self-Improvement Feedback Loop

**Track effectiveness of knowledge commits:**

```python
@dataclass
class CommitEffectivenessMetrics:
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
    confidence_after_usage: float = 1.0  # Updated based on feedback
    cultural_applicability: Dict[str, int] = field(default_factory=dict)  # {"Western": 5, "Eastern": 3}

async def track_commit_effectiveness(
    commit_id: str,
    event: Literal["referenced", "edited", "helpful", "unhelpful"]
):
    """Update metrics when knowledge is used"""

    metrics = load_metrics(commit_id)

    if event == "referenced":
        metrics.times_referenced += 1
        metrics.last_accessed = datetime.utcnow().isoformat()
    elif event == "edited":
        metrics.times_edited += 1
        # Edits suggest knowledge wasn't quite right
        metrics.confidence_after_usage *= 0.9
    elif event == "helpful":
        metrics.helpful_count += 1
    elif event == "unhelpful":
        metrics.unhelpful_count += 1

    # Recalculate effectiveness
    total_feedback = metrics.helpful_count + metrics.unhelpful_count
    if total_feedback > 0:
        metrics.effectiveness_score = metrics.helpful_count / total_feedback

    save_metrics(metrics)

    # If effectiveness is low, suggest revision
    if metrics.effectiveness_score < 0.5 and total_feedback >= 5:
        await suggest_knowledge_revision(commit_id, metrics)
```

---

## 8. Implementation Patterns

### 8.1 Context Retrieval with @-mentions

**Inspired by Cursor/Windsurf:**

```python
# In chat:
# "How should we design the boss fight? @user:bob @topic:game_design:boss_patterns"

class ContextRetriever:
    def parse_mentions(self, message: str) -> Dict[str, List[str]]:
        """Parse @-mentions in message"""
        mentions = {
            'users': [],
            'topics': [],
            'files': []
        }

        # Regex patterns
        user_pattern = r'@user:(\w+)'
        topic_pattern = r'@topic:(\w+)(?::(\w+))?'
        file_pattern = r'@file:([\w/\.]+)'

        mentions['users'] = re.findall(user_pattern, message)
        mentions['topics'] = re.findall(topic_pattern, message)
        mentions['files'] = re.findall(file_pattern, message)

        return mentions

    async def retrieve_contexts(self, mentions: Dict[str, List[str]]) -> Dict[str, Any]:
        """Fetch all mentioned contexts"""
        contexts = {}

        # User contexts
        for user_id in mentions['users']:
            if user_id in self.p2p_manager.peers:
                ctx = await self.request_context_from_peer(user_id, query="full")
                contexts[f"user:{user_id}"] = ctx

        # Topic contexts (semantic search)
        for topic_match in mentions['topics']:
            topic_name = topic_match[0]
            subtopic = topic_match[1] if len(topic_match) > 1 else None

            # Search across all users' topics
            results = await self.semantic_search_topics(topic_name, subtopic)
            contexts[f"topic:{topic_name}"] = results

        # Files (if mentioned)
        for file_path in mentions['files']:
            content = await self.read_knowledge_file(file_path)
            contexts[f"file:{file_path}"] = content

        return contexts
```

### 8.2 Semantic Search

**Find relevant knowledge across contexts:**

```python
class SemanticContextSearch:
    def __init__(self):
        # Use sentence-transformers for embeddings
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.index = {}  # node_id -> {topic: embedding}

    async def index_context(self, node_id: str, context: PersonalContext):
        """Create embeddings for all topics"""
        embeddings = {}

        for topic_name, topic in context.knowledge.items():
            # Combine summary + entries for embedding
            text = f"{topic.summary} " + " ".join(
                entry.content for entry in topic.entries
            )

            embedding = self.model.encode(text)
            embeddings[topic_name] = embedding

        self.index[node_id] = embeddings

    async def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """Find most relevant topics across all indexed contexts"""
        query_embedding = self.model.encode(query)

        results = []
        for node_id, topics in self.index.items():
            for topic_name, topic_embedding in topics.items():
                similarity = cosine_similarity(query_embedding, topic_embedding)
                results.append(SearchResult(
                    node_id=node_id,
                    topic=topic_name,
                    similarity=similarity
                ))

        # Sort by relevance
        results.sort(key=lambda r: r.similarity, reverse=True)
        return results[:top_k]
```

### 8.3 Markdown File Management

**Like Claude Code's `.claud/` directory:**

```python
class MarkdownKnowledgeManager:
    def __init__(self, knowledge_dir: Path):
        self.knowledge_dir = knowledge_dir
        self.knowledge_dir.mkdir(exist_ok=True)

    def create_topic_file(self, topic: Topic, commit: KnowledgeCommitProposal) -> Path:
        """Create markdown file for new topic"""

        # Sanitize filename
        filename = re.sub(r'[^\w\-]', '_', topic.summary[:50].lower())
        filepath = self.knowledge_dir / f"{filename}.md"

        # Build markdown content
        content = f"""# {topic.summary}

*Created: {topic.created_at}*
*Version: {topic.version}*
*Commit: {commit.commit_id}*

## Overview

{commit.summary}

---

## Knowledge Entries

"""

        for entry in commit.entries:
            content += f"""
### {entry.tags[0] if entry.tags else 'Entry'}

*Confidence: {entry.confidence:.0%}*
*Source: {', '.join(entry.source.sources_cited)}*
*Cultural Context: {', '.join(entry.requires_context) if entry.requires_context else 'Universal'}*

{entry.content}

"""

            if entry.alternative_viewpoints:
                content += f"""
**Alternative Perspectives:**
{chr(10).join(f"- {alt}" for alt in entry.alternative_viewpoints)}

"""

        content += f"""
---

## Cultural Perspectives Considered

{chr(10).join(f"- {p}" for p in commit.cultural_perspectives)}

## Metadata

- **Participants:** {', '.join(commit.participants)}
- **Consensus:** {commit.consensus_status}
- **AI Confidence:** {commit.avg_confidence:.0%}
"""

        filepath.write_text(content, encoding='utf-8')
        return filepath

    def update_topic_file(self, filepath: Path, new_entries: List[KnowledgeEntry]):
        """Append new entries to existing markdown file"""

        current_content = filepath.read_text(encoding='utf-8')

        # Find "## Knowledge Entries" section
        insert_pos = current_content.find("## Knowledge Entries")
        if insert_pos == -1:
            # Fallback: append to end
            insert_pos = len(current_content)
        else:
            # Find next ## heading or end of file
            next_section = current_content.find("\n## ", insert_pos + 1)
            if next_section == -1:
                insert_pos = len(current_content)
            else:
                insert_pos = next_section

        # Build new entry markdown
        new_content = ""
        for entry in new_entries:
            new_content += f"""
### {entry.tags[0] if entry.tags else 'Entry'}

*Added: {entry.last_updated}*
*Confidence: {entry.confidence:.0%}*

{entry.content}

"""

        # Insert
        updated = (
            current_content[:insert_pos] +
            new_content +
            current_content[insert_pos:]
        )

        filepath.write_text(updated, encoding='utf-8')
```

---

## 9. Implementation Roadmap

### Phase 1: Enhanced PCM Structure (v0.8.0 - COMPLETE)

**Goal:** Update data structures without breaking existing functionality

**Completed Tasks:**
1. Added `InstructionBlock`, `BiasAwareness`, enhanced `KnowledgeEntry` to `pcm_core.py`
2. Implemented backward compatibility for old `personal.json` files
3. Created v2 PCM schema with structured metadata
4. Updated `PCMCore.save_context()` to write new format
5. Added device context collection (`device_context.json`)

**Deliverable:** v2 PCM schema operational in production

---

### Phase 2: Instruction Processing (v0.8.0 - PARTIAL)

**Goal:** Make AI respect instruction blocks

**Completed Tasks:**
1. Basic instruction processing in `service.py`
2. Personal context instructions sent to AI
3. Multi-perspective analysis (devil's advocate mechanism)

**Pending Tasks:**
1. Full bias mitigation prompts based on `instruction.bias_mitigation`
2. Cultural sensitivity requirements
3. Evidence requirement enforcement
4. Framing neutrality detection

**Deliverable:** Partial - basic instructions work, advanced bias mitigation pending

---

### Phase 3: Markdown Files + Hybrid Storage (Phase 2.1 - Month 2-3, 2026)

**Goal:** Implement Claude Code-style markdown knowledge files

**Timeline:** Q1 2026 (Month 2-3)

**Tasks:**
1. Create `MarkdownKnowledgeManager` class
2. Add `~/.dpc/knowledge/` directory structure
3. Implement markdown file generation from knowledge commits
4. Update PCM to link JSON ↔ markdown (bidirectional sync)
5. Add markdown file syncing on context updates
6. Build UI markdown viewer/editor
7. Support @-file mentions for markdown knowledge

**Integration with Team Collaboration:**
- Shared markdown files for team knowledge
- Version control integration (git-compatible)
- Conflict resolution for concurrent edits
- Markdown export for external tools

**Deliverable:** Human-readable, git-versionable knowledge files

---

### Phase 4: Knowledge Commit Protocol (v0.8.0 - PARTIAL, Enhanced in Phase 2.2 - Month 4-5, 2026)

**Goal:** Complete git-like knowledge commit system

**Completed (v0.8.0):**
1. Consensus voting system (`consensus_manager.py`)
2. Multi-peer knowledge approval workflow
3. Devil's advocate mechanism (required dissent)
4. Basic knowledge entry structure

**Pending (Phase 2.2 - Month 4-5, 2026):**
1. Git-like commit history (`.knowledge_history/commits.json`)
2. Commit messages and provenance tracking
3. AI-generated commit summaries with bias mitigation
4. ConversationMonitor for automatic knowledge extraction
5. Commit diff visualization
6. Rollback/revert functionality
7. Branch/merge for experimental knowledge

**Integration with Team Collaboration:**
- Team-wide knowledge commits (2-20 members)
- Async consensus voting (email/webhook notifications)
- Knowledge contribution metrics (gamification)
- Commit changelog for team visibility

**Deliverable:** Full git-style knowledge versioning with team workflows

---

### Phase 5: Context Retrieval & Semantic Search (Phase 2.2 - Month 4-6, 2026)

**Goal:** Add @-mention syntax and semantic search across team contexts

**Timeline:** Q2 2026 (Month 4-6)

**Tasks:**
1. Implement `ContextRetriever` with @-mention parsing
   - `@user:alice` - retrieve peer's full context
   - `@topic:game_design` - search all team members' knowledge
   - `@file:knowledge/python.md` - include specific markdown file
   - `@group:frontend_team` - aggregate contexts from node group
2. Add semantic search with sentence-transformers
3. Index all local + peer contexts on connection
4. Update UI to support @-mentions (autocomplete dropdown)
5. Build relevance ranking algorithm (TF-IDF + embeddings)
6. Cache peer contexts for offline search

**Integration with Team Collaboration:**
- Real-time context indexing on knowledge commits
- Team-wide semantic search (query across 20 peers)
- Privacy-aware search (respects firewall rules)
- Search result provenance (who contributed what)

**Deliverable:** Smart, privacy-aware context retrieval for teams

---

### Phase 6: Self-Improvement Tracking (Phase 2.3 - Month 6-7, 2026)

**Goal:** Track effectiveness and suggest improvements

**Timeline:** Q2-Q3 2026 (Month 6-7)

**Completed (v0.8.0):**
1. Basic effectiveness tracking (`effectiveness_tracker.py`)
2. Token usage and cost tracking

**Pending:**
1. `CommitEffectivenessMetrics` storage (per-commit usage analytics)
2. Spaced repetition scheduling for knowledge review
3. Effectiveness dashboard UI (which knowledge gets used most?)
4. AI suggestions for low-scoring knowledge
5. Knowledge deprecation warnings (outdated context detection)
6. Team analytics (most valuable contributors)

**Integration with Team Collaboration:**
- Team knowledge quality metrics
- Identify experts (who contributes most-used knowledge?)
- Suggest knowledge gaps (what questions can't we answer?)
- Usage heatmaps (which topics are queried most?)

**Deliverable:** Data-driven knowledge quality system

---

### Phase 7: Cognitive Bias Mitigation (Phase 2.4 - Month 7-9, 2026)

**Goal:** Full bias-resistant AI curation

**Timeline:** Q3 2026 (Month 7-9)

**Tasks:**
1. Implement multi-cultural perspective prompting
2. Add cultural context tracking per user
3. Build bias detection in AI responses
4. Implement forced alternative viewpoints
5. Add confidence scoring with cultural applicability
6. Create bias audit logs
7. Build bias mitigation dashboard

**Integration with Team Collaboration:**
- Cultural diversity metrics for team knowledge
- Cross-cultural validation requirements
- Bias warnings when team lacks diversity
- Alternative perspective suggestions

**Deliverable:** Bias-aware collaborative knowledge system

---

### Phase 8: Group Chat Knowledge Extraction (Phase 2.1 - Month 1-3, 2026)

**Goal:** Automatic knowledge capture from team conversations

**Timeline:** Q1-Q2 2026 (Month 1-3, parallel with Phase 3)

**Tasks:**
1. Implement `ConversationMonitor` for group chats
2. Add knowledge-worthiness scoring algorithm
3. Build AI-powered summarization pipeline
4. Create real-time knowledge commit proposals
5. Implement consensus detection (agreement signals)
6. Add UI for in-chat commit approval
7. Support threaded knowledge discussions

**Integration with Team Collaboration:**
- Multi-party group chat support (3-20 participants)
- Async knowledge approval (notifications)
- Knowledge attribution (who said what?)
- Conversation context linking (why was this committed?)

**Deliverable:** Seamless conversation → knowledge transformation

---

## Updated Timeline (2026)

**Q1 2026 (Months 1-3):**
- Phase 3: Markdown Files + Hybrid Storage
- Phase 8: Group Chat Knowledge Extraction (parallel)

**Q2 2026 (Months 4-6):**
- Phase 4: Git-like Knowledge Commits (complete)
- Phase 5: Context Retrieval & Semantic Search

**Q3 2026 (Months 6-9):**
- Phase 6: Self-Improvement Tracking
- Phase 7: Cognitive Bias Mitigation

**Total Duration:** 9 months (aligned with Phase 2 main roadmap)

---

## Conclusion

This architecture combines the best practices from:

- **Claude Code**: File-based persistence, markdown readability
- **Cursor/Windsurf**: @-mentions, semantic search
- **Personal Context Manager**: Instruction blocks, structured data
- **Cognitive Bias Research**: Multi-perspective analysis, dissent requirements
- **Git**: Versioning, commit messages, provenance

The result is a **collaborative knowledge building system** where conversations transform into permanent, versioned, bias-resistant knowledge through AI curation and multi-party consensus.

---

## Current Status (v0.8.0)

**Production Ready:**
- Basic PCM structure with profile, knowledge, preferences
- Multi-peer consensus voting system
- Devil's advocate mechanism (anti-groupthink)
- Effectiveness tracking for AI responses
- Personal context sharing with firewall controls

**Partial Implementation:**
- Instruction processing (basic, no full bias mitigation)
- Knowledge commit protocol (voting works, git-like history pending)

**Planned for Phase 2 (2026):**
- Markdown knowledge files (Claude Code-style)
- @-mention context retrieval
- Semantic search across team contexts
- Git-like commit history and diffs
- Full cognitive bias mitigation
- Automatic knowledge extraction from group chats

---

## Phase 2 Focus: Small Team Collaboration (2-20 Members + AIs)

The knowledge architecture roadmap is now aligned with **Phase 2: Team Collaboration + Disaster Resilience** (Q1-Q3 2026):

**Primary Goal:** Enable small teams (2-20 members + AIs) to build shared knowledge through:
- Real-time collaborative knowledge commits
- Privacy-aware context sharing (firewall rules)
- Git-compatible versioning (markdown files)
- Semantic search across team knowledge
- Bias-resistant AI curation

**Why Small Teams First?**
- Test and refine knowledge commit workflows
- Validate consensus mechanisms at scale (2-20 peers)
- Build tooling for effective team collaboration
- Gather feedback before mass-market scaling (Phase 3)

**Integration with Disaster Resilience:**
- Knowledge commits work offline (DHT-based peer discovery)
- Markdown files sync over pluggable transports (WebRTC, WebSocket-TLS, HTTP tunneling)
- No Hub dependency for knowledge sharing
- Mesh-compatible (WiFi Mesh, Meshtastic, Starlink)

---

## Next Steps (Q1 2026)

**Immediate (Month 1):**
1. Update documentation (README.md, ROADMAP.md, KNOWLEDGE_ARCHITECTURE.md, CHANGELOG.md)
2. Complete Stage 1 planning for Phase 2 implementation
3. Design UI mockups for markdown knowledge viewer/editor
4. Prototype @-mention autocomplete system

**Month 2-3:**
1. Implement markdown knowledge files (Phase 3)
2. Build group chat knowledge extraction (Phase 8)
3. Add @-mention context retrieval prototype
4. Test with 3-5 person team (dogfooding)

**Month 4-6:**
1. Complete git-like commit history (Phase 4)
2. Launch semantic search across team contexts (Phase 5)
3. Beta test with 10-20 person teams
4. Gather effectiveness metrics

---

## Open Questions

1. **Markdown Format:** CommonMark vs GitHub Flavored Markdown vs custom extensions?
2. **Knowledge Commit Threshold:** What score triggers AI to propose a commit (currently 0.7)?
3. **Conflict Resolution:** How do we handle knowledge conflicts between team members?
4. **Search Privacy:** Should semantic search respect per-field firewall rules or per-topic rules?
5. **Offline Sync:** Git-style merge or CRDT-based conflict-free sync for markdown files?
6. **UI/UX:** Web UI for knowledge graph visualization or CLI-first?

See [ROADMAP.md](../ROADMAP.md) for complete Phase 2 implementation plan.
