"""
Conversation Monitor - Phase 4.2

Monitors group chat conversations in real-time to detect knowledge-worthy content
and propose knowledge commits. Uses AI to analyze conversation patterns and
detect consensus signals.
"""

import asyncio
import logging
import json
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass
from datetime import datetime, timezone

from dpc_protocol.pcm_core import PersonalContext, KnowledgeEntry, KnowledgeSource
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Represents a chat message"""
    message_id: str
    conversation_id: str
    sender_node_id: str
    sender_name: str
    text: str
    timestamp: str
    attachment_transfer_id: Optional[str] = None  # Link to attachment transfer (v0.14.0)


class ConversationMonitor:
    """Monitors conversations for knowledge-worthy content

    Runs in background during group chats, analyzing messages for:
    - Substantive information (facts, decisions, insights)
    - Multiple perspectives being discussed
    - Consensus being reached
    - Novel ideas vs casual chat

    When detected, proposes knowledge commits to participants.
    """

    def __init__(
        self,
        conversation_id: str,
        participants: List[Dict[str, str]],  # [{node_id, name, context}]
        llm_manager,  # LLMManager instance
        knowledge_threshold: float = 0.7,  # Minimum score to propose commit
        settings = None,  # Settings instance (optional, for config like cultural_perspectives_enabled)
        ai_query_func = None,  # Callable for AI queries (supports both local and remote inference)
        auto_detect: bool = True,  # Enable/disable automatic detection
        instruction_set_name: str = "general",  # NEW: Which instruction set to use for this conversation
        display_name: str = None,  # Human-readable name appended to folder (e.g. "Work", "Mike MacOS")
    ):
        """Initialize conversation monitor

        Args:
            conversation_id: Unique conversation identifier
            participants: List of participant info dicts
            llm_manager: LLMManager instance for AI analysis (used if ai_query_func is None)
            knowledge_threshold: Score threshold (0.0-1.0) to trigger commit proposal
            settings: Settings instance for configuration (optional)
            ai_query_func: Optional callable for AI queries. Signature: async (prompt, compute_host, model, provider) -> dict
                          If provided, enables remote inference for knowledge detection.
            auto_detect: If True, automatically detect and propose commits. If False, only buffer messages for manual extraction.
            instruction_set_name: Key of the instruction set to use for AI queries in this conversation (default: "general")
            display_name: Optional human-readable label appended to the conversation folder name for easy navigation
        """
        self.conversation_id = conversation_id
        self.display_name = display_name
        self.participants = participants
        self.llm_manager = llm_manager
        self.knowledge_threshold = knowledge_threshold
        self.settings = settings
        self.ai_query_func = ai_query_func  # Enables both local and remote inference
        self.auto_detect = auto_detect  # Controls automatic detection vs manual-only
        self.instruction_set_name = instruction_set_name  # NEW: Track instruction set for this conversation

        # Message buffer
        self.message_buffer: List[Message] = []  # Cleared after each extraction (for incremental auto-detect)
        self.full_conversation: List[Message] = []  # Never cleared (for manual "End Session" extraction)
        self.knowledge_score: float = 0.0

        # Flag to prevent concurrent knowledge extraction (e.g., auto-detect + end_session running simultaneously).
        # Note: not a proper lock — just a best-effort guard.
        self._extracting: bool = False

        # Tracking
        self.proposals_created: int = 0
        self.last_analysis_time: Optional[str] = None

        # Token tracking (Phase 2)
        self.current_token_count: int = 0
        self.token_limit: int = 100000  # Default limit, will be updated per model
        self.token_warning_threshold: float = 0.8  # Warn at 80%

        # Conversation history tracking (Phase 7: Conversation History)
        self.message_history: List[Dict[str, str]] = []  # List of {"role": "user/assistant", "content": "..."}
        self.message_ids: Set[str] = set()  # Track unique message IDs for deduplication
        self._history_dirty: bool = False  # Track unsaved changes
        self.peer_context_hashes: Dict[str, str] = {}  # {node_id: context_hash} for peer cache invalidation

        # Phase 7: Peer context caching (to avoid re-fetching unchanged contexts)
        self.peer_context_cache: Dict[str, Any] = {}  # {node_id: PersonalContext} cached peer contexts
        self.peer_device_context_cache: Dict[str, dict] = {}  # {node_id: device_context_dict} cached device contexts

        # Track last used compute settings for knowledge extraction
        self.last_compute_host: str | None = None  # Track last compute host used
        self.last_model: str | None = None  # Track last model used
        self.last_provider: str | None = None  # Track last provider used

        # Conversation type classification (v0.9.3)
        self.conversation_type: str = "general"  # Detected type: task/technical/decision/general

    def set_inference_settings(self, compute_host: str | None, model: str | None, provider: str | None):
        """Update inference settings (called after each AI query in this conversation).

        Args:
            compute_host: Node ID for remote inference (None = local)
            model: Model name
            provider: Provider alias
        """
        self.last_compute_host = compute_host
        self.last_model = model
        self.last_provider = provider
        logger.debug(
            "Monitor %s: Updated inference settings (compute_host=%s, model=%s, provider=%s)",
            self.conversation_id,
            compute_host or "local",
            model or "default",
            provider or "default"
        )

    async def on_message(self, message: Message) -> Optional[KnowledgeCommitProposal]:
        """Process new message in conversation

        Args:
            message: New message to analyze

        Returns:
            KnowledgeCommitProposal if knowledge detected (when auto_detect=True), None otherwise
        """
        # Always buffer messages (even if auto_detect is disabled, for manual extraction)
        self.message_buffer.append(message)  # For incremental auto-detection
        self.full_conversation.append(message)  # For manual "End Session" extraction

        # Add to message_history for chat history sync (v0.11.2)
        # Determine role based on sender
        role = "user"  # Default to user
        for participant in self.participants:
            if participant["node_id"] == message.sender_node_id:
                if participant.get("context") == "peer":
                    role = "peer"
                break

        # Extract metadata for proper chat history display (v0.15.3)
        # Use ISO format timestamp from message
        timestamp = message.timestamp if hasattr(message, 'timestamp') else None
        sender_node_id = message.sender_node_id
        sender_name = message.sender_name
        # Extract attachments if present (dynamic attribute for Telegram messages)
        attachments = getattr(message, 'attachments', None)

        self.add_message(role, message.text, attachments=attachments,
                        timestamp=timestamp, sender_node_id=sender_node_id,
                        sender_name=sender_name)
        logger.debug(f"Added message to history: role={role}, text_len={len(message.text)}")

        # Only run automatic detection if enabled
        if not self.auto_detect:
            return None

        # Analyze every 5 messages or if buffer gets large
        if len(self.message_buffer) >= 5:
            self.knowledge_score = await self._calculate_knowledge_score()
            self.last_analysis_time = datetime.now(timezone.utc).isoformat()

            # Check if conversation is knowledge-worthy
            if self.knowledge_score > self.knowledge_threshold:
                # Check for consensus signals
                if self._detect_consensus():
                    # Generate commit proposal (auto-threshold path)
                    proposal = await self._generate_commit_proposal(
                        proposed_by="auto_monitor", initiated_by="auto_monitor"
                    )

                    # DON'T reset buffer yet - wait for all peers to approve (v0.15.1 fix)
                    # Buffer will be cleared by consensus_manager when proposal is approved
                    self.proposals_created += 1

                    return proposal

        return None

    async def generate_commit_proposal(
        self,
        force: bool = False,
        proposed_by: str = "auto_monitor",
        initiated_by: str = "auto_monitor",
    ) -> Optional[KnowledgeCommitProposal]:
        """Manually generate a knowledge commit proposal

        Args:
            force: If True, generate proposal even if below threshold
            proposed_by: Agent ID or node_id that triggered extraction
            initiated_by: How extraction was triggered: "auto_monitor", "agent_tool",
                          "telegram", or "user_request"

        Returns:
            KnowledgeCommitProposal if knowledge detected (or forced), None otherwise
        """
        if not self.message_buffer:
            return None

        # Best-effort guard against concurrent extractions (e.g., auto-detect racing
        # with end_session).
        if self._extracting:
            logger.warning(
                "generate_commit_proposal called while extraction already in progress for %s — skipping",
                self.conversation_id,
            )
            return "EXTRACTION_IN_PROGRESS"

        self._extracting = True
        try:
            # Calculate score only when needed for the threshold check.
            # Skip when force=True — score is irrelevant and the extra LLM call
            # doubles the extraction time (1-2 min wasted).
            if not force and self.knowledge_score == 0.0:
                self.knowledge_score = await self._calculate_knowledge_score()
                self.last_analysis_time = datetime.now(timezone.utc).isoformat()

            # Check if we should generate proposal
            if force or self.knowledge_score > self.knowledge_threshold:
                try:
                    # Generate proposal
                    proposal = await self._generate_commit_proposal(
                        proposed_by=proposed_by, initiated_by=initiated_by
                    )

                    # DON'T reset buffer yet - wait for all peers to approve (v0.15.1 fix)
                    # Buffer will be cleared by consensus_manager when proposal is approved
                    if proposal is not None:
                        self.proposals_created += 1

                    return proposal
                except Exception as e:
                    # Log error but DON'T clear buffer to allow retry (v0.14.0 fix)
                    logger.error(f"Error generating proposal (buffer preserved for retry): {e}", exc_info=True)
                    raise  # Re-raise so caller knows it failed

            return None
        finally:
            self._extracting = False

    def _infer_inference_settings(self) -> tuple[str | None, str | None, str | None]:
        """Infer inference settings when not explicitly tracked.

        Priority order:
        1. Local inference (if default provider configured)
        2. Remote inference (fallback for peer conversations)

        Returns:
            (compute_host, model, provider) tuple
        """
        # PRIORITY 1: Check if local inference is available (default provider configured)
        if self.llm_manager and self.llm_manager.default_provider:
            logger.info("Monitor %s: Using local inference for knowledge extraction (default provider available)",
                       self.conversation_id)
            return (None, None, None)  # Local inference with default provider

        # PRIORITY 2: For peer conversations, try using peer compute as fallback
        if self.conversation_id.startswith("dpc-node-"):
            logger.info("Monitor %s: Will attempt peer compute for knowledge extraction (no local provider, compute_host=%s)",
                       self.conversation_id, self.conversation_id)
            return (self.conversation_id, self.last_model, None)  # Peer compute fallback

        # PRIORITY 2b: Use last known compute host (works for group and P2P conversations)
        if self.last_compute_host:
            logger.info("Monitor %s: Using last_compute_host %s for knowledge extraction",
                       self.conversation_id, self.last_compute_host)
            return (self.last_compute_host, self.last_model, self.last_provider)

        # PRIORITY 3: Final fallback - try local anyway (will fail gracefully if no providers)
        logger.warning("Monitor %s: No inference provider available for knowledge extraction",
                      self.conversation_id)
        return (None, None, None)

    def _detect_conversation_type(self) -> str:
        """Detect conversation type from message content and participant context.

        Uses simple keyword matching on message text. Checks both full_conversation
        (for manual extraction) and message_buffer (for auto-detection).

        Returns:
            Conversation type: "task", "technical", "decision", "general", or
            "self_reflection_candidate" (requires async LLM confirmation before use)
        """
        # Use full_conversation if available (manual extraction), otherwise message_buffer
        messages = self.full_conversation if self.full_conversation else self.message_buffer

        # Combine all message text for analysis
        all_text = " ".join([msg.text.lower() for msg in messages])

        # Task/Planning keywords (highest priority - user's primary use case)
        task_keywords = [
            "task", "deadline", "assigned", "project", "deliverable",
            "milestone", "schedule", "priority", "estimate", "sprint",
            "ticket", "issue", "todo", "action item", "owner"
        ]

        # Technical keywords
        technical_keywords = [
            "code", "implementation", "architecture", "refactor", "bug",
            "api", "database", "function", "class", "module", "library",
            "framework", "algorithm", "performance", "optimization", "test"
        ]

        # Decision keywords
        decision_keywords = [
            "decide", "decision", "choose", "option", "alternative",
            "propose", "vote", "approve", "reject", "consensus",
            "recommend", "suggest", "evaluate", "tradeoff", "pros and cons"
        ]

        # Self-reflection keywords (multi-word phrases for low false-positive rate)
        # Single words like "reflect", "analyze", "behavior" are too common in technical contexts
        self_reflection_keywords = [
            "self-reflection", "self reflection", "retrospective",
            "my habit", "my behavior", "my routine", "my pattern",
            "i tend to", "reflecting on myself", "who am i",
            "my values", "my progress", "lessons learned",
            "context anxiety", "self-analysis"
        ]

        # Count keyword matches
        task_count = sum(1 for kw in task_keywords if kw in all_text)
        technical_count = sum(1 for kw in technical_keywords if kw in all_text)
        decision_count = sum(1 for kw in decision_keywords if kw in all_text)
        self_reflection_count = sum(1 for kw in self_reflection_keywords if kw in all_text)

        # Self-reflection candidate: requires async LLM confirmation (handled in _generate_commit_proposal)
        if self_reflection_count > 0:
            return "self_reflection_candidate"

        # Determine type based on highest count (task has priority for ties)
        if task_count >= technical_count and task_count >= decision_count and task_count > 0:
            return "task"
        elif technical_count > task_count and technical_count >= decision_count:
            return "technical"
        elif decision_count > 0:
            return "decision"
        else:
            return "general"

    async def _is_self_reflection_conversation(self, messages_text: str) -> bool:
        """Use LLM to confirm whether a conversation is a self-reflection.

        Only called when self_reflection keywords matched (pre-filter). Prevents
        false positives where technical language coincidentally matches phrases like
        "this reflects the architecture decision".

        Returns:
            True if LLM confirms self-reflection, False otherwise (falls back to general)
        """
        logger.debug("Monitor %s: Running LLM self-reflection confirmation (keyword pre-filter matched)",
                     self.conversation_id)

        prompt = f"""Analyze this conversation. Is it a SELF-REFLECTION?

SELF-REFLECTION: Discussion of personal behaviors, habits, routines, identity,
emotional patterns, self-performance review ("I tend to...", "My habit is...",
"Who am I", "My values").

NOT self-reflection: Technical discussions (even with "analyze", "reflect"),
task planning (even with "my goals"), external decisions.

Return ONLY: "yes" or "no"

Conversation:
{messages_text}"""

        try:
            result = await asyncio.wait_for(
                self.llm_manager.query(prompt=prompt),
                timeout=10.0  # Don't block extraction if LLM is slow
            )
            confirmed = result.strip().lower().startswith("yes")
            logger.debug("Monitor %s: Self-reflection LLM confirmation: %s",
                         self.conversation_id, confirmed)
            return confirmed
        except asyncio.TimeoutError:
            logger.warning("Monitor %s: Self-reflection LLM confirmation timed out — falling back to general",
                           self.conversation_id)
            return False
        except Exception as e:
            logger.warning("Monitor %s: Self-reflection LLM confirmation failed (%s) — falling back to general",
                           self.conversation_id, e)
            return False

    def _get_task_extraction_prompt(self, messages_text: str, cultural_section: str) -> str:
        """Build extraction prompt optimized for task/planning conversations.

        Focuses on: task names, assignments, deadlines, status, decisions.
        """
        json_format = """{
  "topic": "concise_task_or_project_name",
  "summary": "One sentence: who is doing what, by when",
  "entries": [
    {
      "content": "Task name, assignment, deadline, or status fact",
      "tags": ["task_assignment", "deadline", "status"],
      "confidence": 0.9,
      "sources": ["participant_name"],
      "reasoning": "Direct statement or agreed commitment",
      "alternatives": []
    }
  ],
  "devil_advocate": "Any risks or unclear requirements",
  "flagged_assumptions": ["Implicit assumptions about scope or timeline"]
}"""

        rules_section = """EXTRACTION RULES FOR TASK CONVERSATIONS:
1. PRIMARY FOCUS: Extract the main task/project NAME (often in first few messages)
2. WHO: Identify who is assigned (mentioned by name or "you"/"I")
3. WHEN: Extract ALL time references (deadlines, start dates, milestones)
4. STATUS: Capture acceptance/rejection ("yes I can", "will take it", "cannot do")
5. SCOPE: Note any deliverables or requirements mentioned
6. CONFIDENCE: Rate 0.9+ for explicit statements, 0.7 for implicit agreements
7. IGNORE: Procedural questions about availability unless they lead to commitment
8. TOPIC: Use task/project NAME, not meta-conversation labels like "deadline inquiry"

EXAMPLE GOOD EXTRACTION:
Topic: "Core Service Refactoring"
Entry 1: "Mike Windows assigned to core service refactoring task"
Entry 2: "Deadline: 3 days from start (tomorrow as start date)"
Entry 3: "Status: Accepted ('yes I can')"

AVOID: Topics like "Task Deadline Inquiry", "Availability Check"
"""

        return f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.

CONVERSATION TYPE: Task/Planning (formal work coordination)
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract task assignments, deadlines, and commitments. Focus on FACTS, not procedural questions.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

    def _get_technical_extraction_prompt(self, messages_text: str, cultural_section: str) -> str:
        """Build extraction prompt optimized for technical discussions.

        Focuses on: architecture, implementation details, technical decisions.
        """
        json_format = """{
  "topic": "technical_topic_name",
  "summary": "One sentence technical summary",
  "entries": [
    {
      "content": "Technical fact, decision, or implementation detail",
      "tags": ["architecture", "implementation", "technical_decision"],
      "confidence": 0.8,
      "sources": ["participant_name"],
      "reasoning": "Technical rationale or evidence",
      "alternatives": ["Alternative technical approach specific to THIS entry"]
    }
  ],
  "devil_advocate": "Technical risks or tradeoffs",
  "flagged_assumptions": ["Technical assumptions to validate"]
}"""

        rules_section = """EXTRACTION RULES FOR TECHNICAL CONVERSATIONS:
1. Focus on: Architecture, implementation details, technical decisions
2. Capture rationale: WHY decisions were made (not just what was decided)
3. Alternative approaches: List other options discussed
4. Tradeoffs: Document pros/cons of chosen approach
5. Technical risks: Flag potential issues or unknowns
6. Confidence: Higher for tested/proven solutions, lower for experimental
"""

        return f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.

CONVERSATION TYPE: Technical Discussion
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract technical knowledge with architectural rationale and tradeoffs.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

    def _get_decision_extraction_prompt(self, messages_text: str, cultural_section: str) -> str:
        """Build extraction prompt optimized for decision-making conversations.

        Focuses on: options evaluated, decision made, consensus reached.
        """
        json_format = """{
  "topic": "decision_or_proposal_topic",
  "summary": "One sentence: what was decided and why",
  "entries": [
    {
      "content": "Decision, option evaluated, or consensus point",
      "tags": ["decision", "option_evaluation", "consensus"],
      "confidence": 0.85,
      "sources": ["participant_name"],
      "reasoning": "Evidence or criteria used for decision",
      "alternatives": ["Options that were NOT chosen and why, specific to THIS entry"]
    }
  ],
  "devil_advocate": "Counter-arguments or dissenting views",
  "flagged_assumptions": ["Assumptions underlying the decision"]
}"""

        rules_section = """EXTRACTION RULES FOR DECISION CONVERSATIONS:
1. DECISION: State clearly what was decided
2. OPTIONS: List all alternatives evaluated (chosen and rejected)
3. CRITERIA: Capture decision criteria or evaluation factors
4. CONSENSUS: Note level of agreement (unanimous vs. majority)
5. DISSENT: Preserve any dissenting opinions or concerns
6. NEXT STEPS: Extract any follow-up actions decided
"""

        return f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.

CONVERSATION TYPE: Decision Making
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract decision made, options evaluated, and consensus reached.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

    def _get_general_extraction_prompt(self, messages_text: str, cultural_section: str) -> str:
        """Build extraction prompt for general conversations (fallback).

        Uses current v0.9.2 prompt logic (existing implementation).
        """
        # Check if cultural perspectives are enabled
        cultural_perspectives_enabled = False
        if self.settings:
            cultural_perspectives_enabled = self.settings.get_cultural_perspectives_enabled()

        if cultural_perspectives_enabled:
            json_format = """{
  "topic": "brief_topic_name",
  "summary": "One sentence overview",
  "entries": [
    {
      "content": "Knowledge statement",
      "tags": ["tag1", "tag2"],
      "confidence": 0.8,
      "cultural_context": "Universal",
      "sources": ["participant_name"],
      "reasoning": "Why notable",
      "alternatives": ["Alternative perspective specific to THIS entry"]
    }
  ],
  "cultural_perspectives": ["Western individualistic", "Eastern collective"],
  "devil_advocate": "Critical analysis",
  "flagged_assumptions": ["Assumption if any"]
}"""
            rules_section = """RULES:
- Rate confidence 0.0-1.0 for each claim
- Mark cultural_context as "Universal" or "Context: [specific culture]"
- Include devil's advocate critique (one per commit)
- List alternative viewpoints PER ENTRY (each entry gets its own unique alternatives)
- Flag cultural assumptions"""
        else:
            json_format = """{
  "topic": "brief_topic_name",
  "summary": "One sentence overview",
  "entries": [
    {
      "content": "Knowledge statement",
      "tags": ["tag1", "tag2"],
      "confidence": 0.8,
      "sources": ["participant_name"],
      "reasoning": "Why notable",
      "alternatives": ["Alternative perspective specific to THIS entry"]
    }
  ],
  "devil_advocate": "Critical analysis of the overall extraction",
  "flagged_assumptions": ["Assumption if any"]
}"""
            rules_section = """RULES:
- Rate confidence 0.0-1.0 for each claim
- Include devil's advocate critique (one per commit, not per entry)
- List alternative viewpoints PER ENTRY (each entry gets its own unique alternatives)
- Flag problematic assumptions"""

        return f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract structured knowledge with bias mitigation.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

    def _get_self_reflection_extraction_prompt(self, messages_text: str, cultural_section: str) -> str:
        """Build extraction prompt optimized for self-reflection conversations.

        Focuses on: behavioral patterns, habits, identity insights, improvement areas.
        Key difference from general: devil's advocate critiques insights, not the process.
        """
        json_format = """{
  "topic": "reflection_topic_name",
  "summary": "One sentence: what was reflected on and the key realization",
  "entries": [
    {
      "content": "Specific behavior, habit, pattern, or identity insight",
      "tags": ["behavior_pattern", "habit", "identity", "emotion", "goal"],
      "confidence": 0.8,
      "sources": ["participant_name"],
      "reasoning": "Context or evidence supporting this insight"
    }
  ],
  "behavioral_patterns": ["Recurring behaviors or habits identified"],
  "identity_insights": ["Insights about values, goals, or self-perception"],
  "improvement_areas": ["Areas mentioned for growth or change"],
  "devil_advocate": "Critique the INSIGHTS themselves — what assumptions might be wrong, what context might be missing — but do NOT question whether reflection was warranted",
  "flagged_assumptions": ["Statements presented as facts that might be projections"]
}"""

        rules_section = """EXTRACTION RULES FOR SELF-REFLECTION CONVERSATIONS:
1. BEHAVIORS: Capture specific recurring behaviors ("I tend to work until 3 AM")
2. HABITS: Extract named habits, routines, or patterns
3. IDENTITY: Note values, self-perception, "who am I" explorations
4. NUANCE: Self-reflection is inherently uncertain — preserve "maybe", "I think", "seems like"
5. IMPROVEMENT: What the person wants to change, grow, or explore
6. DEVIL'S ADVOCATE — CRITICAL RULE:
   - CORRECT: "This insight assumes X is a problem, but what if it's a coping strategy?"
   - CORRECT: "The conclusion may overlook Y factor"
   - WRONG: "Why reflect on this?" / "Trigger threshold too low" / "This doesn't warrant analysis"
   - The devil's advocate should challenge the SUBSTANCE of insights, never the act of reflecting
7. CONFIDENCE: Lower (0.6-0.75) for uncertain self-assessments, higher (0.85+) for clearly stated patterns
"""

        return f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.

CONVERSATION TYPE: Self-Reflection (personal behavior, habits, identity analysis)
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract behavioral patterns, identity insights, and improvement areas from self-reflection.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

    async def _calculate_knowledge_score(self) -> float:
        """Calculate knowledge-worthiness score for conversation segment

        Uses LLM to score based on:
        - Substantive information (facts, decisions, insights): +0.3
        - Multiple perspectives discussed: +0.2
        - Consensus reached: +0.2
        - Actionable conclusions: +0.2
        - Novel ideas vs casual chat: +0.1

        Returns:
            Score from 0.0 to 1.0
        """
        # Format messages for analysis
        messages_text = self._format_messages_for_analysis(self.message_buffer[-10:])

        prompt = f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations, no markdown, no code blocks - just raw JSON.

Task: Analyze this conversation for knowledge-worthiness.

Score 0.0-1.0 based on:
- Substantive information (facts, decisions, insights): +0.3
- Multiple perspectives discussed: +0.2
- Consensus reached: +0.2
- Actionable conclusions: +0.2
- Novel ideas vs casual chat: +0.1

MESSAGES:
{messages_text}

REQUIRED OUTPUT FORMAT (raw JSON only):
{{"score": 0.75, "reasoning": "brief explanation"}}

DO NOT include any text before or after the JSON. DO NOT use markdown code blocks. DO NOT explain your analysis outside the JSON."""

        try:
            # Infer inference settings (uses tracked settings if available, otherwise infers from conversation type)
            compute_host, model, provider = self._infer_inference_settings()

            # Use AI query function if available (supports remote inference), otherwise use llm_manager (local only)
            response = None
            primary_error = None
            if self.ai_query_func:
                try:
                    result = await self.ai_query_func(
                        prompt=prompt,
                        compute_host=compute_host,
                        model=model,
                        provider=provider
                    )
                    response = result["response"]
                except Exception as primary_error:
                    # Try fallback: if primary was remote, try local; if primary was local, try remote
                    fallback_attempted = False

                    # Case 1: Remote failed, try local as fallback
                    if compute_host and self.llm_manager and self.llm_manager.default_provider:
                        logger.warning("Remote inference failed, trying local inference as fallback: %s", primary_error)
                        fallback_attempted = True
                        try:
                            result = await self.ai_query_func(
                                prompt=prompt,
                                compute_host=None,  # Force local inference
                                model=None,
                                provider=None
                            )
                            response = result["response"]
                            primary_error = None  # Success! Clear error
                        except Exception as local_error:
                            logger.error("Local inference fallback also failed: %s", local_error)

                    # Case 2: Local failed (or wasn't configured), try remote as fallback for peer conversations
                    elif not compute_host and self.conversation_id.startswith("dpc-node-"):
                        logger.warning("Local inference failed, trying remote inference as fallback: %s", primary_error)
                        fallback_attempted = True
                        try:
                            result = await self.ai_query_func(
                                prompt=prompt,
                                compute_host=self.conversation_id,  # Try peer compute
                                model=self.last_model,
                                provider=None
                            )
                            response = result["response"]
                            primary_error = None  # Success! Clear error
                        except Exception as remote_error:
                            logger.error("Remote inference fallback also failed: %s", remote_error)

                    # If no fallback attempted or both failed, raise original error
                    if primary_error:
                        if not fallback_attempted:
                            logger.error("Inference failed and no fallback available")
                        else:
                            logger.error("Both primary and fallback inference failed")
                        raise primary_error
            else:
                # Fallback to direct llm_manager call (local only)
                response = await self.llm_manager.query(
                    prompt=prompt,
                    provider_alias=provider
                )

            # Try to extract JSON if wrapped in markdown or text
            import json
            import re

            # Try to find JSON in the response
            json_match = re.search(r'\{[^{}]*"score"[^{}]*\}', response)
            if json_match:
                json_str = json_match.group(0)
                result = json.loads(json_str)
                return float(result.get('score', 0.0))
            else:
                # Try parsing the whole response
                result = json.loads(response.strip())
                return float(result.get('score', 0.0))
        except Exception as e:
            logger.error("Error calculating knowledge score: %s", e, exc_info=True)
            logger.error("  LLM Response preview: %s...", response[:200] if 'response' in locals() and response is not None and isinstance(response, str) else 'N/A')
            return 0.0

    def _detect_consensus(self) -> bool:
        """Detect if group has reached agreement

        Looks for consensus signals in recent messages:
        - "sounds good", "agreed", "let's go with"
        - "I'm on board", "works for me"
        - "approved", "✅"

        Returns:
            True if consensus detected, False otherwise
        """
        consensus_signals = [
            "sounds good",
            "agreed",
            "agree",
            "let's go with",
            "i'm on board",
            "works for me",
            "approved",
            "✅",
            "👍",
            "yes",
            "that works",
            "makes sense",
            "good idea"
        ]

        recent_messages = self.message_buffer[-5:]
        consensus_count = 0

        for msg in recent_messages:
            text_lower = msg.text.lower()
            if any(signal in text_lower for signal in consensus_signals):
                consensus_count += 1

        # Need majority of participants to express agreement
        threshold = len(self.participants) * 0.6
        return consensus_count >= threshold

    def _extract_json_object(self, text: str) -> Optional[str]:
        """Extract the first balanced JSON object from text, handling arbitrary nesting.

        Unlike a simple regex, this correctly handles 3+ levels of nesting so that
        the outermost JSON object is always returned, not an inner fragment.
        """
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i, char in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if char == '\\' and in_string:
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None  # Unbalanced braces

    def _repair_json(self, json_str: str) -> str:
        """Attempt to repair common JSON malformations from LLM responses.

        Handles:
        - Missing closing braces
        - Trailing commas
        - Missing commas between array/object elements
        - Markdown code block wrappers

        Args:
            json_str: Potentially malformed JSON string

        Returns:
            Repaired JSON string (best effort)
        """
        import re

        # Remove markdown code blocks
        json_str = re.sub(r'```(?:json)?\s*', '', json_str)
        json_str = json_str.strip()

        # Remove trailing commas before closing brackets/braces
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

        # Count opening and closing braces
        open_braces = json_str.count('{')
        close_braces = json_str.count('}')
        open_brackets = json_str.count('[')
        close_brackets = json_str.count(']')

        # Add missing closing braces
        if open_braces > close_braces:
            json_str += '}' * (open_braces - close_braces)

        # Add missing closing brackets
        if open_brackets > close_brackets:
            json_str += ']' * (open_brackets - close_brackets)

        # Try to fix missing commas between array elements (heuristic)
        # Example: ["item1" "item2"] -> ["item1", "item2"]
        json_str = re.sub(r'"\s+"', '", "', json_str)

        # Try to fix missing commas between object properties
        # Example: {"a": 1 "b": 2} -> {"a": 1, "b": 2}
        json_str = re.sub(r'([}\]0-9"])\s+"', r'\1, "', json_str)

        return json_str

    async def _generate_commit_proposal(
        self,
        proposed_by: str = "auto_monitor",
        initiated_by: str = "auto_monitor",
    ) -> KnowledgeCommitProposal:
        """Generate knowledge commit proposal from conversation

        Uses bias-aware prompting to extract structured knowledge with:
        - Multi-perspective analysis
        - Cultural assumptions flagged (if enabled)
        - Alternative viewpoints
        - Confidence scores
        - Devil's advocate critique

        Returns:
            KnowledgeCommitProposal object
        """
        # Detect conversation type (v0.9.3)
        self.conversation_type = self._detect_conversation_type()
        logger.info("Monitor %s: Detected conversation type: %s",
                   self.conversation_id, self.conversation_type)

        # Get cultural perspectives setting
        cultural_perspectives_enabled = False
        if self.settings:
            cultural_perspectives_enabled = self.settings.get_cultural_perspectives_enabled()

        # Load participant cultural contexts (only if enabled)
        cultural_contexts = []
        if cultural_perspectives_enabled:
            for participant in self.participants:
                if 'context' in participant:
                    context = participant['context']
                    if hasattr(context, 'cognitive_profile') and context.cognitive_profile:
                        if context.cognitive_profile.cultural_background:
                            cultural_contexts.append(context.cognitive_profile.cultural_background)

        # Format conversation
        # Use full_conversation for manual extraction (includes all messages)
        # Use message_buffer only for automatic incremental extraction
        messages_to_analyze = self.full_conversation if self.full_conversation else self.message_buffer
        messages_text = self._format_messages_for_analysis(messages_to_analyze)

        # Extract voice transcriptions from message_history (v0.13.2+)
        # Includes transcribed text for knowledge extraction
        transcriptions_text = self._extract_transcriptions_from_history()
        messages_text += transcriptions_text

        # Build cultural context section (conditional)
        cultural_section = ""
        if cultural_perspectives_enabled:
            cultural_section = f"""
PARTICIPANTS' CULTURAL CONTEXTS:
{', '.join(cultural_contexts) if cultural_contexts else 'Not specified'}
"""

        # LLM confirmation for self-reflection candidate (keyword pre-filter ran in _detect_conversation_type)
        if self.conversation_type == "self_reflection_candidate":
            confirmed = await self._is_self_reflection_conversation(messages_text)
            self.conversation_type = "self_reflection" if confirmed else "general"

        # Select type-specific prompt builder (v0.9.3)
        if self.conversation_type == "task":
            prompt = self._get_task_extraction_prompt(messages_text, cultural_section)
        elif self.conversation_type == "technical":
            prompt = self._get_technical_extraction_prompt(messages_text, cultural_section)
        elif self.conversation_type == "decision":
            prompt = self._get_decision_extraction_prompt(messages_text, cultural_section)
        elif self.conversation_type == "self_reflection":
            prompt = self._get_self_reflection_extraction_prompt(messages_text, cultural_section)
        else:  # general (fallback)
            prompt = self._get_general_extraction_prompt(messages_text, cultural_section)

        try:
            # Infer inference settings (uses tracked settings if available, otherwise infers from conversation type)
            compute_host, model, provider = self._infer_inference_settings()

            # Use AI query function if available (supports remote inference), otherwise use llm_manager (local only)
            inference_result = None  # Save for metadata extraction
            response = None
            primary_error = None
            if self.ai_query_func:
                try:
                    inference_result = await self.ai_query_func(
                        prompt=prompt,
                        compute_host=compute_host,
                        model=model,
                        provider=provider
                    )
                    response = inference_result["response"]
                except Exception as primary_error:
                    # Try fallback: if primary was remote, try local; if primary was local, try remote
                    fallback_attempted = False

                    # Case 1: Remote failed, try local as fallback
                    if compute_host and self.llm_manager and self.llm_manager.default_provider:
                        logger.warning("Remote inference failed, trying local inference as fallback: %s", primary_error)
                        fallback_attempted = True
                        try:
                            inference_result = await self.ai_query_func(
                                prompt=prompt,
                                compute_host=None,  # Force local inference
                                model=None,
                                provider=None
                            )
                            response = inference_result["response"]
                            primary_error = None  # Success! Clear error
                        except Exception as local_error:
                            logger.error("Local inference fallback also failed: %s", local_error)

                    # Case 2: Local failed (or wasn't configured), try remote as fallback
                    # Works for peer conversations (dpc-node-) and group conversations (last_compute_host set)
                    elif not compute_host and (self.conversation_id.startswith("dpc-node-") or self.last_compute_host):
                        remote_host = self.last_compute_host or self.conversation_id
                        logger.warning("Local inference failed, trying remote compute %s as fallback: %s", remote_host[:20], primary_error)
                        fallback_attempted = True
                        try:
                            inference_result = await self.ai_query_func(
                                prompt=prompt,
                                compute_host=remote_host,
                                model=self.last_model,
                                provider=None
                            )
                            response = inference_result["response"]
                            primary_error = None  # Success! Clear error
                        except Exception as remote_error:
                            logger.error("Remote inference fallback also failed: %s", remote_error)

                    # If no fallback attempted or both failed, raise original error
                    if primary_error:
                        if not fallback_attempted:
                            logger.error("Inference failed and no fallback available")
                        else:
                            logger.error("Both primary and fallback inference failed")
                        raise primary_error
            else:
                # Fallback to direct llm_manager call (local only)
                response = await self.llm_manager.query(
                    prompt=prompt,
                    provider_alias=provider
                )

            # Try to extract and parse JSON from response (with repair attempts)
            import json
            import re

            result = None
            json_str = None

            # Attempt 1: Extract outermost JSON object with balanced-bracket parser.
            # Handles arbitrary nesting depth — the old regex only handled 2 levels and
            # would capture an inner fragment (e.g. the bias section) when the LLM wraps
            # its output in a 3-level structure like {"analysis": {"entries": [...]}}.
            json_str = self._extract_json_object(response)
            if json_str:
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    # Attempt 2: Try repairing the extracted JSON
                    try:
                        repaired = self._repair_json(json_str)
                        result = json.loads(repaired)
                        logger.debug("Successfully repaired malformed JSON")
                    except json.JSONDecodeError as e:
                        logger.warning("Failed to repair extracted JSON: %s", e)
                        result = None

            # Attempt 3: Try parsing whole response
            if result is None:
                try:
                    result = json.loads(response.strip())
                except json.JSONDecodeError:
                    # Attempt 4: Try repairing the whole response
                    try:
                        repaired = self._repair_json(response.strip())
                        result = json.loads(repaired)
                        logger.debug("Successfully repaired malformed response")
                    except json.JSONDecodeError as e:
                        # All attempts failed
                        raise ValueError(f"Failed to extract valid JSON after repair attempts: {e}")

            # Determine extraction model and host from inference result
            extraction_model_name = model  # From _infer_inference_settings()
            if not extraction_model_name and inference_result:
                # Try to extract from inference result dict (has model, provider, compute_host)
                extraction_model_name = inference_result.get('model')

            extraction_host_name = "local" if not compute_host else compute_host

            # Build knowledge entries
            if not result.get('entries'):
                logger.warning(
                    "Knowledge extraction returned no entries for %s. "
                    "Result keys: %s | Response preview: %.300s",
                    self.conversation_id,
                    list(result.keys()),
                    response,
                )
            entries = []
            for entry_data in result.get('entries', []):
                source = KnowledgeSource(
                    type="ai_summary",
                    conversation_id=self.conversation_id,
                    participants=[p['node_id'] for p in self.participants],
                    confidence_score=entry_data.get('confidence', 1.0),
                    sources_cited=entry_data.get('sources', []),
                    cultural_perspectives_considered=result.get('cultural_perspectives', []),
                    extraction_model=extraction_model_name,
                    extraction_host=extraction_host_name
                )

                # Handle cultural context (only if enabled)
                cultural_context = entry_data.get('cultural_context', 'Universal') if cultural_perspectives_enabled else 'Universal'
                entry = KnowledgeEntry(
                    content=entry_data.get('content', ''),
                    tags=entry_data.get('tags', []),
                    source=source,
                    confidence=entry_data.get('confidence', 1.0),
                    cultural_specific=(cultural_context != 'Universal') if cultural_perspectives_enabled else False,
                    requires_context=[cultural_context] if (cultural_perspectives_enabled and cultural_context != 'Universal') else [],
                    # Per-entry alternatives (S34 fix). Fallback to commit-level for backward compat.
                    alternative_viewpoints=entry_data.get('alternatives', []) or result.get('alternatives', [])
                )
                entries.append(entry)

            # Calculate average confidence
            avg_confidence = sum(e.confidence for e in entries) / len(entries) if entries else 1.0

            # Sanitize alternatives (convert objects to strings if needed)
            raw_alternatives = result.get('alternatives', [])
            alternatives = []
            for alt in raw_alternatives:
                if isinstance(alt, str):
                    alternatives.append(alt)
                elif isinstance(alt, dict):
                    # Extract string from dict (common AI response pattern)
                    alternatives.append(alt.get('description') or alt.get('text') or alt.get('content') or str(alt))
                else:
                    alternatives.append(str(alt))

            # Sanitize devil_advocate (convert object to string if needed)
            raw_devil_advocate = result.get('devil_advocate')
            if raw_devil_advocate is None:
                devil_advocate = None
            elif isinstance(raw_devil_advocate, str):
                devil_advocate = raw_devil_advocate
            elif isinstance(raw_devil_advocate, dict):
                # Extract string from dict (common AI response pattern)
                devil_advocate = (raw_devil_advocate.get('critique') or
                                 raw_devil_advocate.get('analysis') or
                                 raw_devil_advocate.get('text') or
                                 raw_devil_advocate.get('content') or
                                 str(raw_devil_advocate))
            else:
                devil_advocate = str(raw_devil_advocate)

            # Create proposal (extraction_model_name and extraction_host_name already determined above)
            proposal = KnowledgeCommitProposal(
                conversation_id=self.conversation_id,
                topic=result.get('topic', 'conversation_summary'),
                summary=result.get('summary', 'Knowledge from group discussion'),
                entries=entries,
                participants=[p['node_id'] for p in self.participants],
                proposed_by=proposed_by,
                initiated_by=initiated_by,
                cultural_perspectives=result.get('cultural_perspectives', []),
                alternatives=alternatives,
                flagged_assumptions=result.get('flagged_assumptions', []),
                devil_advocate=devil_advocate,
                avg_confidence=avg_confidence,
                extraction_model=extraction_model_name,
                extraction_host=extraction_host_name,
                status='proposed'
            )

            return proposal

        except Exception as e:
            logger.error("Error generating commit proposal: %s", e, exc_info=True)
            logger.error("  LLM Response preview: %s...", response[:300] if 'response' in locals() and response is not None and isinstance(response, str) else 'N/A')

            # Determine error message based on exception type
            error_msg = 'Failed to extract knowledge'
            error_str = str(e).lower()

            if 'access denied' in error_str or 'not authorized' in error_str:
                error_msg += ' - Remote inference denied and no local provider available'
            elif 'not connected' in error_str or 'connection' in error_str:
                error_msg += ' - Service not available (check if Ollama/AI provider is running)'
            elif 'no providers configured' in error_str or 'provider not found' in error_str:
                error_msg += ' - No AI provider configured. Add a provider in Settings'
            elif 'connection refused' in error_str or 'timeout' in error_str:
                error_msg += ' - AI service not responding (is Ollama running?)'
            elif 'model not found' in error_str:
                error_msg += ' - Model not available. Check your AI provider configuration'
            elif response:
                error_msg += ' - LLM did not return valid JSON'
            else:
                error_msg += f' - {str(e)[:100]}'

            # Return empty proposal on error
            return KnowledgeCommitProposal(
                conversation_id=self.conversation_id,
                topic='error',
                summary=error_msg,
                participants=[p['node_id'] for p in self.participants]
            )

    def _format_messages_for_analysis(self, messages: List[Message]) -> str:
        """Format messages as text for LLM analysis

        Args:
            messages: List of messages to format

        Returns:
            Formatted string
        """
        lines = []
        for msg in messages:
            # Handle both string and datetime timestamp formats
            if isinstance(msg.timestamp, str):
                timestamp = msg.timestamp.split('T')[1][:8] if 'T' in msg.timestamp else msg.timestamp
            else:
                # datetime object - format as HH:MM:SS
                timestamp = msg.timestamp.strftime('%H:%M:%S')
            lines.append(f"[{timestamp}] {msg.sender_name}: {msg.text}")
        return "\n".join(lines)

    def _extract_transcriptions_from_history(self) -> str:
        """Extract voice transcription text from message_history

        Scans message_history for voice attachments with transcriptions and
        formats them for inclusion in knowledge extraction prompts.

        Returns:
            Formatted string with voice transcriptions, or empty string if none found
        """
        transcriptions = []

        for msg in self.message_history:
            attachments = msg.get("attachments", [])
            for attachment in attachments:
                if attachment.get("type") == "voice":
                    transcription = attachment.get("transcription")
                    if transcription and transcription.get("text"):
                        # Format: "Voice message from [sender]: [transcription text]"
                        role = msg.get("role", "user")
                        sender_label = "You" if role == "user" else "Peer"

                        text = transcription["text"]
                        provider = transcription.get("provider", "unknown")
                        confidence = transcription.get("confidence", 0.0)

                        # Format with metadata for context
                        transcriptions.append(
                            f"{sender_label} (voice message, transcribed by {provider}, "
                            f"confidence: {confidence:.2f}): {text}"
                        )

        if transcriptions:
            return "\n\nVOICE MESSAGE TRANSCRIPTIONS:\n" + "\n".join(transcriptions)
        else:
            return ""

    def reset(self):
        """Reset monitor state"""
        self.message_buffer = []
        self.knowledge_score = 0.0

    def get_stats(self) -> Dict[str, Any]:
        """Get monitor statistics

        Returns:
            Dictionary with stats
        """
        return {
            'conversation_id': self.conversation_id,
            'participants': len(self.participants),
            'messages_buffered': len(self.message_buffer),
            'current_knowledge_score': self.knowledge_score,
            'proposals_created': self.proposals_created,
            'last_analysis': self.last_analysis_time
        }

    # --- Token Tracking Methods (Phase 2) ---

    def set_token_limit(self, limit: int):
        """Set the token limit for this conversation

        Args:
            limit: Maximum tokens for the model's context window
        """
        self.token_limit = limit

    def update_token_count(self, tokens: int):
        """Update the current token count

        Args:
            tokens: Number of tokens to add to the count
        """
        self.current_token_count += tokens

    def set_token_count(self, tokens: int):
        """Set the current token count (replaces instead of adding)

        IMPORTANT: Use this method for prompt tokens, NOT update_token_count(),
        to avoid double-counting conversation history!

        The prompt_tokens from LLM already includes:
        - System instructions
        - Personal/device contexts
        - FULL conversation history
        - Current query

        So we REPLACE the count, not ADD to it.

        Args:
            tokens: Total prompt tokens (from LLM metadata)
        """
        self.current_token_count = tokens

    def get_token_usage(self) -> Dict[str, Any]:
        """Get current token usage statistics

        Returns:
            Dictionary with token usage info
        """
        usage_percent = self.current_token_count / self.token_limit if self.token_limit > 0 else 0.0
        return {
            'conversation_id': self.conversation_id,
            'tokens_used': self.current_token_count,
            'token_limit': self.token_limit,
            'usage_percent': usage_percent,
            'should_warn': usage_percent >= self.token_warning_threshold
        }

    def should_suggest_extraction(self, threshold: Optional[float] = None) -> bool:
        """Check if knowledge extraction should be suggested based on token usage

        Args:
            threshold: Optional custom threshold (uses default if None)

        Returns:
            True if token usage exceeds threshold
        """
        effective_threshold = threshold if threshold is not None else self.token_warning_threshold
        usage_percent = self.current_token_count / self.token_limit if self.token_limit > 0 else 0.0
        return usage_percent >= effective_threshold

    def reset_token_count(self):
        """Reset token count (after knowledge extraction)"""
        self.current_token_count = 0

    # --- Conversation History Methods (Phase 7) ---

    def add_message(self, role: str, content: str, attachments: Optional[List[Dict[str, Any]]] = None,
                    timestamp: Optional[str] = None, sender_node_id: Optional[str] = None,
                    sender_name: Optional[str] = None, message_id: Optional[str] = None,
                    thinking: Optional[str] = None, streaming_raw: Optional[str] = None,
                    source: Optional[str] = None):
        """Add a message to the conversation history

        Args:
            role: 'user' or 'assistant' (or 'peer')
            content: Message content
            attachments: Optional list of attachment metadata dicts
                Example: [{"type": "file", "filename": "...", "size_bytes": 123, ...}]
            timestamp: Optional ISO format timestamp (e.g., "2026-01-19T12:34:56Z")
            sender_node_id: Optional sender node ID (for proper attribution in chat history)
            sender_name: Optional sender display name
            message_id: Optional unique message ID (auto-generated if not provided)
            thinking: Optional extended chain-of-thought from reasoning models (persisted to history.json)
            streaming_raw: Optional full streamed text output (persisted to history.json, for UI restore)

        Note: This also adds to message_buffer and full_conversation for knowledge extraction.
        thinking and streaming_raw are stored in history.json but excluded from knowledge extraction.
        """
        import uuid

        # Generate unique message ID if not provided (v0.20.0)
        if not message_id:
            message_id = str(uuid.uuid4())

        # Add to message_history (for chat history sync)
        message_dict = {"id": message_id, "role": role, "content": content}
        if attachments:
            message_dict["attachments"] = attachments
        if timestamp:
            message_dict["timestamp"] = timestamp
        if sender_node_id:
            message_dict["sender_node_id"] = sender_node_id
        if sender_name:
            message_dict["sender_name"] = sender_name
        if thinking:
            message_dict["thinking"] = thinking
        if streaming_raw:
            message_dict["streaming_raw"] = streaming_raw
        if source:
            message_dict["source"] = source

        # Track message ID for deduplication (v0.20.0)
        self.message_ids.add(message_id)
        self._history_dirty = True

        self.message_history.append(message_dict)

        # Also add to knowledge extraction buffers (v0.13.2 fix for voice messages)
        # Map role to sender info (use different variable names to avoid shadowing)
        if role == "user":
            # User is the local node (first participant is always self)
            km_sender_node_id = self.participants[0]["node_id"] if self.participants else "local"
            km_sender_name = self.participants[0]["name"] if self.participants else "You"
        else:  # role == "assistant" or "peer"
            # Assistant/peer is the conversation partner (second participant)
            km_sender_node_id = self.participants[1]["node_id"] if len(self.participants) > 1 else "peer"
            km_sender_name = self.participants[1]["name"] if len(self.participants) > 1 else "Peer"

        # Create Message object for knowledge extraction (reuse the same message_id)
        message_obj = Message(
            message_id=message_id,
            conversation_id=self.conversation_id,
            sender_node_id=km_sender_node_id,
            sender_name=km_sender_name,
            text=content,
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            attachment_transfer_id=attachments[0].get("transfer_id") if attachments else None  # v0.14.0
        )

        self.message_buffer.append(message_obj)
        self.full_conversation.append(message_obj)

    def get_message_history(self) -> List[Dict[str, str]]:
        """Get the full conversation history

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        return self.message_history.copy()

    def update_peer_context_hash(self, node_id: str, context_hash: str):
        """Update the stored hash for a peer's context

        Args:
            node_id: Peer node identifier
            context_hash: SHA256 hash of peer's context
        """
        self.peer_context_hashes[node_id] = context_hash

    def has_peer_context_changed(self, node_id: str, new_hash: str) -> bool:
        """Check if a peer's context has changed

        Args:
            node_id: Peer node identifier
            new_hash: Current hash of peer's context

        Returns:
            True if peer context has changed or is new
        """
        old_hash = self.peer_context_hashes.get(node_id, "")
        return new_hash != old_hash

    def _archive_current_session(self, reason: str = "reset", max_sessions: int = 0) -> int:
        """Archive the current history.json to archive/ before clearing.

        Writes to ~/.dpc/conversations/{conversation_id}/archive/{timestamp}_{reason}_session.json
        Keeps all archives by default; when max_sessions > 0, prunes oldest
        beyond that cap.

        Args:
            reason: Label for why the session was archived (e.g. "reset", "new_session")
            max_sessions: Maximum archives to retain. 0 (default) = unlimited.

        Returns:
            Number of archives in the folder after archiving (0 if skipped or error).
        """
        if not self.message_history:
            return 0

        path = self._get_history_path()
        if not path.exists():
            return 0

        try:
            now = datetime.now(timezone.utc)
            ts = now.strftime("%Y-%m-%dT%H-%M-%S")
            # ADR-008: YYYY/MM subdirectory layout for scalable archive storage
            archive_base = path.parent / "archive"
            archive_dir = archive_base / now.strftime("%Y") / now.strftime("%m")
            archive_dir.mkdir(parents=True, exist_ok=True)

            archive_path = archive_dir / f"{ts}_{reason}_session.json"

            # Read current file content and inject archive metadata
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["archived_at"] = datetime.now(timezone.utc).isoformat()
            data["session_reason"] = reason

            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"Archived session to {archive_path}")

            # Generate incremental session digest (derived data — can be rebuilt from archives)
            self._generate_session_digest(data, archive_path)

            # Prune oldest archives beyond max_sessions (rglob for YYYY/MM layout).
            # ARCH-19: max_sessions == 0 means unlimited retention — skip prune.
            archives = sorted(archive_base.rglob("*_session.json"))
            if max_sessions > 0 and len(archives) > max_sessions:
                for old in archives[: len(archives) - max_sessions]:
                    old.unlink()
                    logger.debug(f"Pruned old archive: {old.name}")
                    # Clean up empty YYYY/MM dirs after pruning
                    try:
                        old.parent.rmdir()  # only removes if empty
                    except OSError:
                        pass

            return len(list(archive_base.rglob("*_session.json")))

        except Exception as e:
            logger.warning(f"Failed to archive session for {self.conversation_id}: {e}")
            return 0

    def _generate_session_digest(self, session_data: Dict[str, Any], archive_path: Path) -> None:
        """Generate an incremental session digest and append to digest.jsonl.

        Extracts structured metadata from the archived session via parsing (no LLM).
        The digest is derived data — if lost, it can be rebuilt from session archives.

        Args:
            session_data: The archived session JSON data.
            archive_path: Path to the archive file (for logging).
        """
        try:
            messages = session_data.get("messages", [])
            if not messages:
                return

            # Parse timestamps for duration
            timestamps = [m.get("timestamp", "") for m in messages if m.get("timestamp")]
            duration_mins = 0.0
            if len(timestamps) >= 2:
                try:
                    first = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
                    last = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
                    duration_mins = round((last - first).total_seconds() / 60, 1)
                except (ValueError, TypeError):
                    pass

            # Count tool calls from tools.jsonl (agent log) by timestamp range
            tool_stats: Dict[str, int] = {}
            tool_durations: Dict[str, list] = {}
            if timestamps and self.conversation_id.startswith("agent_"):
                try:
                    tools_path = (
                        Path.home() / ".dpc" / "agents"
                        / self.conversation_id / "logs" / "tools.jsonl"
                    )
                    if tools_path.exists():
                        first_ts = timestamps[0]
                        last_ts = timestamps[-1]
                        with open(tools_path, encoding="utf-8") as tf:
                            for line in tf:
                                try:
                                    entry = json.loads(line.strip())
                                    ts = entry.get("ts", "")
                                    if first_ts <= ts <= last_ts or entry.get("session_id") == self.conversation_id:
                                        name = entry.get("tool", "unknown")
                                        tool_stats[name] = tool_stats.get(name, 0) + 1
                                        dur = entry.get("duration_ms")
                                        if dur is not None:
                                            tool_durations.setdefault(name, []).append(dur)
                                except (json.JSONDecodeError, TypeError):
                                    continue
                except Exception as e:
                    logger.debug(f"Could not read tools.jsonl for digest: {e}")

            # Extract basic topics from user messages (first 5 user messages as topic hints)
            user_messages = [m.get("content", "")[:100] for m in messages
                            if m.get("role") == "user" and m.get("content")]

            # Compute avg duration per tool
            tool_avg_duration: Dict[str, int] = {}
            for name, durations in tool_durations.items():
                if durations:
                    tool_avg_duration[name] = round(sum(durations) / len(durations))

            digest_entry = {
                "date": session_data.get("archived_at", datetime.now(timezone.utc).isoformat()),
                "session_id": self.conversation_id,
                "message_count": len(messages),
                "duration_mins": duration_mins,
                "tool_stats": tool_stats,
                "tool_avg_duration_ms": tool_avg_duration,
                "user_message_previews": user_messages[:5],
                "archive_file": archive_path.name,
            }

            # Write to digest.jsonl in the conversation directory (next to archive/)
            # Use explicit conversation dir instead of parent chain (ADR-008: archive_path
            # is now inside YYYY/MM subdirs, so parent.parent would be wrong)
            conversation_dir = self._get_history_path().parent
            digest_path = conversation_dir / "digest.jsonl"
            with open(digest_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(digest_entry, ensure_ascii=False) + "\n")

            logger.info(f"Session digest appended to {digest_path}")

        except Exception as e:
            # Digest failure must not block archival
            logger.warning(f"Failed to generate session digest: {e}")

    def reset_conversation(self, preserve: bool = True, max_sessions: int = 0) -> int:
        """Reset conversation history and context tracking (for "New Chat" button).

        Args:
            preserve: If True, archive the current session before clearing.
            max_sessions: Max archives to keep. 0 (default) = unlimited.

        Returns:
            Archive count after reset (0 if preserve=False or nothing was archived).
        """
        # Archive before wiping
        archive_count = self._archive_current_session(reason="reset", max_sessions=max_sessions) if preserve else 0

        self.message_history = []
        self.message_ids = set()
        self._history_dirty = False
        self.peer_context_hashes = {}
        self.current_token_count = 0
        self._last_context_estimated = 0  # reset so token counter shows 0 on fresh session
        self.message_buffer = []
        self.knowledge_score = 0.0
        # Clear peer context caches
        self.peer_context_cache = {}
        self.peer_device_context_cache = {}

        # Delete persisted history file (v0.20.0)
        path = self._get_history_path()
        if path.exists():
            try:
                path.unlink()
                logger.info(f"Deleted history file on reset: {path}")
            except Exception as e:
                logger.error(f"Failed to delete history file {path}: {e}")

        return archive_count

    def _remap_attachment_paths(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remap file paths in attachments from peer's filesystem to local filesystem.

        When importing chat history, voice/file attachments may have file_path
        that points to the peer's filesystem (e.g., C:\\Users\\...\\file.webm on Windows).
        This method checks if the file exists locally and remaps the path.

        Args:
            attachments: List of attachment dicts (may contain file_path)

        Returns:
            List of attachment dicts with remapped file_path (if file exists locally)
        """
        import os
        from pathlib import Path

        # Construct local files directory: ~/.dpc/conversations/{peer_id}/files/
        dpc_home = Path.home() / ".dpc"
        local_files_dir = dpc_home / "conversations" / self.conversation_id / "files"

        remapped = []
        for attachment in attachments:
            # Make a copy to avoid modifying original
            att = dict(attachment)

            # Check if this attachment has a file_path (voice/file attachments)
            if "file_path" in att and att["file_path"]:
                peer_path = att["file_path"]

                # Extract filename from peer's path (cross-platform)
                # Handle both Unix (/) and Windows (\) separators
                filename = os.path.basename(peer_path.replace("\\", "/"))

                # Construct local path: ~/.dpc/conversations/{peer_id}/files/{filename}
                local_file_path = local_files_dir / filename

                # Check if file exists locally
                if local_file_path.exists():
                    # Replace with local path
                    att["file_path"] = str(local_file_path)
                    logger.debug(f"Remapped attachment path: {peer_path} -> {local_file_path}")
                else:
                    # File doesn't exist locally - keep peer's path but log warning
                    logger.warning(
                        f"Voice/file attachment not found locally: {filename}. "
                        f"Expected at {local_file_path}. Voice message may not play."
                    )
                    # Keep the peer's path (will fail to play, but preserves history)

            remapped.append(att)

        return remapped

    def export_history(self) -> List[Dict[str, Any]]:
        """Export conversation history for syncing with peer

        Returns history in serializable format with timestamps added.
        No message limit - returns full history.

        Returns:
            List of message dicts with 'role', 'content', 'timestamp', 'attachments'
        """
        exported = []
        for msg in self.message_history:
            exported_msg = {
                "id": msg.get("id"),  # Preserve ID so merge_history can deduplicate
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
            if "attachments" in msg:
                exported_msg["attachments"] = msg["attachments"]
            if "sender_node_id" in msg:
                exported_msg["sender_node_id"] = msg["sender_node_id"]
            if "sender_name" in msg:
                exported_msg["sender_name"] = msg["sender_name"]
            exported.append(exported_msg)

        logger.info(f"Exported {len(exported)} messages from conversation history")
        return exported

    def import_history(self, messages: List[Dict[str, Any]]):
        """Import conversation history received from peer

        Replaces current history with received messages.
        Used when reconnecting to restore lost history.

        Args:
            messages: List of message dicts from export_history()
        """
        if not messages:
            logger.info("No messages to import")
            return

        # Replace all three message stores (v0.14.0 fix)
        self.message_history = []
        self.message_buffer = []
        self.full_conversation = []

        import uuid

        for msg in messages:
            # 1. Add to message_history (original format)
            imported_msg = {
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            }
            if "attachments" in msg:
                # Fix file paths in attachments (convert peer's paths to local paths)
                imported_msg["attachments"] = self._remap_attachment_paths(msg["attachments"])
            self.message_history.append(imported_msg)

            # 2. Also create Message objects for extraction buffers (v0.14.0 fix)
            # Map role to sender info
            role = msg.get("role", "user")
            if role == "user":
                # User is the local node (first participant is always self)
                sender_node_id = self.participants[0]["node_id"] if self.participants else "local"
                sender_name = self.participants[0]["name"] if self.participants else "You"
            else:  # role == "assistant" or "peer"
                # Assistant/peer is the conversation partner (second participant)
                sender_node_id = self.participants[1]["node_id"] if len(self.participants) > 1 else "peer"
                sender_name = self.participants[1]["name"] if len(self.participants) > 1 else "Peer"

            # Create Message object (same as add_message() does)
            message_obj = Message(
                message_id=str(uuid.uuid4()),
                conversation_id=self.conversation_id,
                sender_node_id=sender_node_id,
                sender_name=sender_name,
                text=msg.get("content", ""),
                timestamp=msg.get("timestamp", datetime.now(timezone.utc).isoformat())
            )

            # Add to both extraction buffers
            self.message_buffer.append(message_obj)
            self.full_conversation.append(message_obj)

        logger.info(f"Imported {len(messages)} messages into all conversation buffers")

    # Phase 7: Peer context cache management methods
    def cache_peer_context(self, node_id: str, context: Any, device_context: dict = None):
        """Cache peer's personal context and device context locally

        Args:
            node_id: Peer's node ID
            context: PersonalContext object
            device_context: Device context dict (optional)
        """
        self.peer_context_cache[node_id] = context
        if device_context:
            self.peer_device_context_cache[node_id] = device_context

    def get_cached_peer_context(self, node_id: str) -> Optional[Any]:
        """Get cached peer context if available

        Args:
            node_id: Peer's node ID

        Returns:
            PersonalContext object or None if not cached
        """
        return self.peer_context_cache.get(node_id)

    def get_cached_peer_device_context(self, node_id: str) -> Optional[dict]:
        """Get cached peer device context if available

        Args:
            node_id: Peer's node ID

        Returns:
            Device context dict or None if not cached
        """
        return self.peer_device_context_cache.get(node_id)

    def invalidate_peer_context_cache(self, node_id: str):
        """Invalidate cached peer context (when peer notifies of change)

        Args:
            node_id: Peer's node ID
        """
        if node_id in self.peer_context_cache:
            del self.peer_context_cache[node_id]
        if node_id in self.peer_device_context_cache:
            del self.peer_device_context_cache[node_id]
        # Also clear the hash so context will be re-included on next query
        if node_id in self.peer_context_hashes:
            del self.peer_context_hashes[node_id]

    # --- Conversation History Persistence (v0.21.0: Unified storage) ---

    @staticmethod
    def _slugify(name: str) -> str:
        """Convert a display name to a filesystem-safe slug."""
        import re
        slug = name.lower()
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'\s+', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')
        return slug[:20]

    def _get_folder_name(self) -> str:
        """Return the folder name for this conversation, with display_name suffix if set."""
        if self.display_name:
            slug = self._slugify(self.display_name)
            if slug:
                return f"{self.conversation_id}-{slug}"
        return self.conversation_id

    def _get_conversation_dir(self) -> Path:
        """Get the conversation folder path.

        Returns:
            Path to ~/.dpc/conversations/{conversation_id}-{slug}/
            Falls back to ~/.dpc/conversations/{conversation_id}/ if no display_name.
            Auto-migrates old unnamed folder to new named folder on first access.
        """
        base = Path.home() / ".dpc" / "conversations"
        folder = self._get_folder_name()
        new_dir = base / folder
        old_dir = base / self.conversation_id
        if folder != self.conversation_id and old_dir.exists() and not new_dir.exists():
            try:
                old_dir.rename(new_dir)
                logger.info("Renamed conversation folder: %s → %s", old_dir.name, new_dir.name)
            except Exception as e:
                logger.warning("Could not rename conversation folder %s: %s", old_dir.name, e)
                return old_dir
        return new_dir

    def _get_history_path(self) -> Path:
        """Get path to history file for this conversation

        Returns:
            Path to ~/.dpc/conversations/{conversation_id}/history.json
        """
        return self._get_conversation_dir() / "history.json"

    def _get_settings_path(self) -> Path:
        """Get path to per-conversation settings file.

        Returns:
            Path to ~/.dpc/conversations/{conversation_id}/settings.json
        """
        return self._get_conversation_dir() / "settings.json"

    def _is_group_conversation(self) -> bool:
        """Check if this is a group conversation.

        Returns:
            True if conversation_id starts with 'group-' or 'agent_' (both persist history)
        """
        return self.conversation_id.startswith("group-") or self.conversation_id.startswith("agent_")

    def _load_conversation_settings(self) -> Dict[str, Any]:
        """Load per-conversation settings from disk.

        Returns:
            Settings dict with defaults if file doesn't exist
        """
        settings_path = self._get_settings_path()
        defaults = {
            "conversation_id": self.conversation_id,
            "conversation_type": "group" if self._is_group_conversation() else "p2p",
            "persist_history": self._is_group_conversation(),  # Groups persist by default, P2P don't
            "created_at": datetime.now(timezone.utc).isoformat(),
            "peer_display_name": None
        }

        if not settings_path.exists():
            return defaults

        try:
            with open(settings_path, encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults (defaults provide missing keys)
            return {**defaults, **data}
        except Exception as e:
            logger.warning(f"Failed to load conversation settings: {e}, using defaults")
            return defaults

    def _save_conversation_settings(self, settings: Dict[str, Any]) -> bool:
        """Save per-conversation settings to disk.

        Args:
            settings: Settings dict to save

        Returns:
            True if saved successfully
        """
        settings_path = self._get_settings_path()
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            logger.debug(f"Saved conversation settings to {settings_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save conversation settings: {e}")
            return False

    @property
    def persist_history(self) -> bool:
        """Check if history should be persisted for this conversation.

        Returns:
            True if history should be saved to disk
        """
        settings = self._load_conversation_settings()
        return settings.get("persist_history", self._is_group_conversation())

    def set_persist_history(self, persist: bool) -> bool:
        """Set whether history should be persisted for this conversation.

        Args:
            persist: True to persist history, False for ephemeral

        Returns:
            True if setting was saved successfully
        """
        settings = self._load_conversation_settings()
        settings["persist_history"] = persist
        settings["last_modified"] = datetime.now(timezone.utc).isoformat()
        return self._save_conversation_settings(settings)

    def compute_history_hash(self) -> str:
        """Compute SHA256 hash of current message history

        Uses sorted message IDs + timestamps for deterministic hashing.

        Returns:
            Hash string like "sha256:abc123..." or "sha256:empty" if no messages
        """
        if not self.message_history:
            return "sha256:empty"

        # Sort by timestamp, then by message ID for determinism
        sorted_msgs = sorted(
            self.message_history,
            key=lambda m: (m.get("timestamp", ""), m.get("id", ""))
        )

        # Hash the concatenated IDs and timestamps
        data = "|".join(
            f"{m.get('id', '?')}:{m.get('timestamp', '?')}"
            for m in sorted_msgs
        )
        return "sha256:" + hashlib.sha256(data.encode()).hexdigest()[:16]

    def save_history(self) -> bool:
        """Persist message history to disk

        Saves to ~/.dpc/conversations/{conversation_id}/history.json
        Only saves if persist_history setting is True for this conversation.

        Returns:
            True if saved successfully (or skipped due to settings), False on error
        """
        # Check if history should be persisted for this conversation
        if not self.persist_history:
            logger.debug(f"Skipping history save for {self.conversation_id} (persist_history=False)")
            return True  # Not an error, just skipped

        path = self._get_history_path()
        try:
            # Ensure directory exists
            path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "conversation_id": self.conversation_id,
                "version": 1,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "message_count": len(self.message_history),
                "history_hash": self.compute_history_hash(),
                "token_stats": {
                    "current_token_count": self.current_token_count,
                    "token_limit": self.token_limit,
                    "context_estimated": getattr(self, '_last_context_estimated', 0),
                },
                "messages": self.message_history
            }

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            self._history_dirty = False
            logger.info(f"Saved {len(self.message_history)} messages to {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to save history to {path}: {e}")
            return False

    def load_history(self) -> bool:
        """Load message history from disk

        Loads from ~/.dpc/conversations/{conversation_id}/history.json
        Also checks legacy path ~/.dpc/groups/{conversation_id}_history.json for migration.

        Returns:
            True if loaded successfully, False if file doesn't exist or on error
        """
        path = self._get_history_path()

        # Check for legacy path (migration support)
        legacy_path = Path.home() / ".dpc" / "groups" / f"{self.conversation_id}_history.json"
        if not path.exists() and legacy_path.exists():
            logger.info(f"Migrating history from legacy path: {legacy_path}")
            try:
                # Ensure new directory exists
                path.parent.mkdir(parents=True, exist_ok=True)
                # Move file to new location
                legacy_path.rename(path)
                logger.info(f"Migrated history to {path}")
            except Exception as e:
                logger.warning(f"Failed to migrate history, reading from legacy path: {e}")
                path = legacy_path

        if not path.exists():
            logger.debug(f"No history file found at {path}")
            return False

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])
            self.message_history = messages

            # Rebuild message_ids set for deduplication
            self.message_ids = {
                m.get("id") for m in messages
                if m.get("id")
            }

            # Restore token stats so the UI token counter shows correct values after restart
            token_stats = data.get("token_stats", {})
            if token_stats:
                self.current_token_count = token_stats.get("current_token_count", self.current_token_count)
                self.token_limit = token_stats.get("token_limit", self.token_limit)
                self._last_context_estimated = token_stats.get("context_estimated", 0)

            self._history_dirty = False
            logger.info(f"Loaded {len(messages)} messages from {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to load history from {path}: {e}")
            return False

    def rebuild_extraction_buffers_from_history(self) -> int:
        """Rebuild full_conversation and message_buffer from message_history.

        Called after load_history() to restore the knowledge-extraction buffers
        that are NOT persisted to disk. Without this, end_session extraction
        only sees messages from the current in-memory session, missing all
        historical messages loaded from disk.

        Only adds messages that are not already in full_conversation (by message_id),
        so it is safe to call even when some messages were added via add_message().

        Returns:
            Number of messages added to full_conversation
        """
        import uuid as _uuid
        existing_ids = {msg.message_id for msg in self.full_conversation}
        added = 0
        for msg in self.message_history:
            msg_id = msg.get("id", "")
            if msg_id and msg_id in existing_ids:
                continue  # already present
            message_obj = Message(
                message_id=msg_id or str(_uuid.uuid4()),
                conversation_id=self.conversation_id,
                sender_node_id=msg.get("sender_node_id", "local"),
                sender_name=msg.get("sender_name", msg.get("role", "user").capitalize()),
                text=msg.get("content", ""),
                timestamp=msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
            )
            self.full_conversation.append(message_obj)
            self.message_buffer.append(message_obj)
            if msg_id:
                existing_ids.add(msg_id)
            added += 1
        if added:
            logger.debug(f"Rebuilt extraction buffers: added {added} historical messages for {self.conversation_id}")
        return added

    def add_message_with_id(self, message: Dict[str, Any]) -> bool:
        """Add a message to history with duplicate detection

        Unlike add_message(), this method:
        - Checks for duplicate message IDs
        - Sets the dirty flag for auto-save
        - Returns whether the message was actually added

        Args:
            message: Message dict with 'id', 'role', 'content', etc.

        Returns:
            True if message was added, False if duplicate
        """
        msg_id = message.get("id")

        # Check for duplicate
        if msg_id and msg_id in self.message_ids:
            logger.debug(f"Skipping duplicate message: {msg_id}")
            return False

        # Track ID
        if msg_id:
            self.message_ids.add(msg_id)

        # Add to history
        self.message_history.append(message)
        self._history_dirty = True

        return True

    def merge_history(self, remote_messages: List[Dict[str, Any]]) -> int:
        """Merge remote messages with local history

        Unlike import_history(), this method:
        - Keeps existing local messages
        - Only adds new messages (by ID)
        - Saves to disk if any messages were added

        Args:
            remote_messages: List of message dicts from peer

        Returns:
            Count of new messages added
        """
        added = 0
        for msg in remote_messages:
            if self.add_message_with_id(msg):
                added += 1

        if added > 0:
            self.save_history()
            logger.info(f"Merged {added} new messages into conversation history")

        return added

    def clear_history(self, preserve: bool = True, max_sessions: int = 0) -> int:
        """Clear all message history and delete persisted file.

        Called when a new session is approved.

        Args:
            preserve: If True, archive the current session before clearing.
            max_sessions: Max archives to keep. 0 (default) = unlimited.

        Returns:
            Archive count after clearing (0 if preserve=False or nothing was archived).
        """
        # Archive before wiping
        archive_count = self._archive_current_session(reason="new_session", max_sessions=max_sessions) if preserve else 0

        self.message_history = []
        self.message_ids = set()
        self._history_dirty = False

        # Delete persisted file
        path = self._get_history_path()
        if path.exists():
            try:
                path.unlink()
                logger.info(f"Deleted history file: {path}")
            except Exception as e:
                logger.error(f"Failed to delete history file {path}: {e}")

        return archive_count

    def delete_conversation_folder(self) -> bool:
        """Delete the entire conversation folder including history, settings, and files.

        This is a complete deletion - used when leaving a group or deleting a conversation.

        Returns:
            True if folder was deleted or didn't exist, False on error
        """
        import shutil

        conv_dir = self._get_conversation_dir()
        if not conv_dir.exists():
            logger.debug(f"Conversation folder doesn't exist: {conv_dir}")
            return True

        try:
            shutil.rmtree(conv_dir)
            logger.info(f"Deleted conversation folder: {conv_dir}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete conversation folder {conv_dir}: {e}")
            return False


# Example usage
if __name__ == '__main__':
    print("=== ConversationMonitor Demo ===\n")

    # Mock LLM manager
    class MockLLMManager:
        async def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 500) -> str:
            # Simulate LLM response
            if "knowledge-worthiness" in prompt:
                return '{"score": 0.85, "reasoning": "Substantive discussion about game design with consensus"}'
            elif "Extract structured knowledge" in prompt:
                return '''{
                    "topic": "game_design_philosophy",
                    "summary": "Environmental storytelling is powerful for player immersion",
                    "entries": [
                        {
                            "content": "Environmental storytelling allows players to discover narrative through exploration rather than explicit exposition.",
                            "tags": ["game_design", "narrative"],
                            "confidence": 0.90,
                            "cultural_context": "Universal",
                            "sources": ["alice", "bob"],
                            "reasoning": "Both participants agreed and cited examples"
                        }
                    ],
                    "cultural_perspectives": ["Western", "Eastern"],
                    "alternatives": ["Dialogue-heavy approach", "Audio logs"],
                    "devil_advocate": "May not work for complex lore-heavy games",
                    "flagged_assumptions": ["Assumes players enjoy exploration"]
                }'''
            return "{}"

    async def demo():
        participants = [
            {'node_id': 'alice', 'name': 'Alice', 'context': None},
            {'node_id': 'bob', 'name': 'Bob', 'context': None}
        ]

        monitor = ConversationMonitor(
            conversation_id='conv-demo',
            participants=participants,
            llm_manager=MockLLMManager(),
            knowledge_threshold=0.7
        )

        print("1. Creating conversation monitor")
        print(f"   Participants: {[p['name'] for p in participants]}")
        print(f"   Threshold: {monitor.knowledge_threshold}")
        print()

        # Simulate conversation
        messages = [
            Message('m1', 'conv-demo', 'alice', 'Alice', 'What do you think about environmental storytelling?', '2025-01-14T10:00:00'),
            Message('m2', 'conv-demo', 'bob', 'Bob', 'I think it\'s really powerful for immersion', '2025-01-14T10:00:15'),
            Message('m3', 'conv-demo', 'alice', 'Alice', 'Yeah, players discover the narrative themselves', '2025-01-14T10:00:30'),
            Message('m4', 'conv-demo', 'bob', 'Bob', 'Better than explicit exposition in many cases', '2025-01-14T10:00:45'),
            Message('m5', 'conv-demo', 'alice', 'Alice', 'Agreed! Let\'s use this approach', '2025-01-14T10:01:00')
        ]

        print("2. Simulating conversation:")
        for msg in messages:
            print(f"   [{msg.sender_name}] {msg.text}")
            proposal = await monitor.on_message(msg)
            if proposal:
                print(f"\n3. Knowledge commit proposed!")
                print(f"   Topic: {proposal.topic}")
                print(f"   Summary: {proposal.summary}")
                print(f"   Entries: {len(proposal.entries)}")
                print(f"   Confidence: {proposal.avg_confidence:.0%}")
                print(f"   Devil's Advocate: {proposal.devil_advocate}")
                break

        print(f"\n4. Monitor stats:")
        stats = monitor.get_stats()
        for key, value in stats.items():
            print(f"   {key}: {value}")

    asyncio.run(demo())
    print("\n=== Demo Complete ===")
