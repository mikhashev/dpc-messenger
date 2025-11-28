"""
Conversation Monitor - Phase 4.2

Monitors group chat conversations in real-time to detect knowledge-worthy content
and propose knowledge commits. Uses AI to analyze conversation patterns and
detect consensus signals.
"""

import asyncio
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

from dpc_protocol.pcm_core import PersonalContext, KnowledgeEntry, KnowledgeSource
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal


@dataclass
class Message:
    """Represents a chat message"""
    message_id: str
    conversation_id: str
    sender_node_id: str
    sender_name: str
    text: str
    timestamp: str


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
        auto_detect: bool = True  # Enable/disable automatic detection
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
        """
        self.conversation_id = conversation_id
        self.participants = participants
        self.llm_manager = llm_manager
        self.knowledge_threshold = knowledge_threshold
        self.settings = settings
        self.ai_query_func = ai_query_func  # Enables both local and remote inference
        self.auto_detect = auto_detect  # Controls automatic detection vs manual-only

        # Message buffer
        self.message_buffer: List[Message] = []
        self.knowledge_score: float = 0.0

        # Tracking
        self.proposals_created: int = 0
        self.last_analysis_time: Optional[str] = None

        # Token tracking (Phase 2)
        self.current_token_count: int = 0
        self.token_limit: int = 100000  # Default limit, will be updated per model
        self.token_warning_threshold: float = 0.8  # Warn at 80%

        # Conversation history tracking (Phase 7: Conversation History)
        self.message_history: List[Dict[str, str]] = []  # List of {"role": "user/assistant", "content": "..."}
        self.context_included: bool = False  # Flag: has context been sent in this conversation?
        self.context_hash: str = ""  # Hash of personal.json + device_context.json when last sent
        self.peer_context_hashes: Dict[str, str] = {}  # {node_id: context_hash} for peer contexts

        # Phase 7: Peer context caching (to avoid re-fetching unchanged contexts)
        self.peer_context_cache: Dict[str, Any] = {}  # {node_id: PersonalContext} cached peer contexts
        self.peer_device_context_cache: Dict[str, dict] = {}  # {node_id: device_context_dict} cached device contexts

        # Knowledge detection inference settings (supports both local and remote)
        self.compute_host: str | None = None  # Node ID for remote inference (None = local)
        self.model: str | None = None  # Model name for remote/local inference
        self.provider_alias: str | None = None  # Provider alias for local inference

    async def on_message(self, message: Message) -> Optional[KnowledgeCommitProposal]:
        """Process new message in conversation

        Args:
            message: New message to analyze

        Returns:
            KnowledgeCommitProposal if knowledge detected (when auto_detect=True), None otherwise
        """
        # Always buffer messages (even if auto_detect is disabled, for manual extraction)
        self.message_buffer.append(message)

        # Only run automatic detection if enabled
        if not self.auto_detect:
            return None

        # Analyze every 5 messages or if buffer gets large
        if len(self.message_buffer) >= 5:
            self.knowledge_score = await self._calculate_knowledge_score()
            self.last_analysis_time = datetime.utcnow().isoformat()

            # Check if conversation is knowledge-worthy
            if self.knowledge_score > self.knowledge_threshold:
                # Check for consensus signals
                if self._detect_consensus():
                    # Generate commit proposal
                    proposal = await self._generate_commit_proposal()

                    # Reset buffer
                    self.message_buffer = []
                    self.knowledge_score = 0.0
                    self.proposals_created += 1

                    return proposal

        return None

    async def generate_commit_proposal(self, force: bool = False) -> Optional[KnowledgeCommitProposal]:
        """Manually generate a knowledge commit proposal

        Args:
            force: If True, generate proposal even if below threshold

        Returns:
            KnowledgeCommitProposal if knowledge detected (or forced), None otherwise
        """
        if not self.message_buffer:
            return None

        # Calculate score if not already done
        if self.knowledge_score == 0.0:
            self.knowledge_score = await self._calculate_knowledge_score()
            self.last_analysis_time = datetime.utcnow().isoformat()

        # Check if we should generate proposal
        if force or self.knowledge_score > self.knowledge_threshold:
            # Generate proposal
            proposal = await self._generate_commit_proposal()

            # Reset buffer
            self.message_buffer = []
            self.knowledge_score = 0.0
            self.proposals_created += 1

            return proposal

        return None

    def update_inference_settings(self, compute_host: str | None = None, model: str | None = None, provider: str | None = None):
        """Update inference settings for knowledge detection

        Knowledge detection can use either local or remote inference:
        - If compute_host is None: Uses local LLM with provider_alias
        - If compute_host is set: Uses remote peer's LLM with specified model

        Args:
            compute_host: Node ID for remote inference (None = local)
            model: Model name for remote inference
            provider: Provider alias for local inference
        """
        self.compute_host = compute_host
        self.model = model
        self.provider_alias = provider

        if compute_host:
            print(f"[Monitor {self.conversation_id}] Knowledge detection: REMOTE inference on {compute_host}, model={model or 'default'}")
        else:
            print(f"[Monitor {self.conversation_id}] Knowledge detection: LOCAL inference, provider={provider or 'default'}")

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
            # Use AI query function if available (supports remote inference), otherwise use llm_manager (local only)
            if self.ai_query_func:
                result = await self.ai_query_func(
                    prompt=prompt,
                    compute_host=self.compute_host,
                    model=self.model,
                    provider=self.provider_alias
                )
                response = result["response"]
            else:
                # Fallback to direct llm_manager call (local only)
                response = await self.llm_manager.query(
                    prompt=prompt,
                    provider_alias=self.provider_alias
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
            print(f"Error calculating knowledge score: {e}")
            print(f"  LLM Response preview: {response[:200] if 'response' in locals() else 'N/A'}...")
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

    async def _generate_commit_proposal(self) -> KnowledgeCommitProposal:
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
        # Check if cultural perspectives are enabled
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
        messages_text = self._format_messages_for_analysis(self.message_buffer)

        # Build cultural context section (conditional)
        cultural_section = ""
        if cultural_perspectives_enabled:
            cultural_section = f"""
PARTICIPANTS' CULTURAL CONTEXTS:
{', '.join(cultural_contexts) if cultural_contexts else 'Not specified'}
"""

        # Build JSON format with conditional cultural fields
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
      "reasoning": "Why notable"
    }
  ],
  "cultural_perspectives": ["Western individualistic", "Eastern collective"],
  "alternatives": ["Alternative perspective 1"],
  "devil_advocate": "Critical analysis",
  "flagged_assumptions": ["Assumption if any"]
}"""
            rules_section = """RULES:
- Rate confidence 0.0-1.0 for each claim
- Mark cultural_context as "Universal" or "Context: [specific culture]"
- Include devil's advocate critique
- List alternative viewpoints
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
      "reasoning": "Why notable"
    }
  ],
  "alternatives": ["Alternative perspective 1"],
  "devil_advocate": "Critical analysis",
  "flagged_assumptions": ["Assumption if any"]
}"""
            rules_section = """RULES:
- Rate confidence 0.0-1.0 for each claim
- Include devil's advocate critique
- List alternative viewpoints
- Flag problematic assumptions"""

        # Build bias-resistant prompt
        prompt = f"""CRITICAL INSTRUCTION: You must respond with ONLY valid JSON. No explanations before or after. No markdown code blocks. Just raw JSON.
{cultural_section}
CONVERSATION:
{messages_text}

TASK: Extract structured knowledge with bias mitigation.

REQUIRED JSON FORMAT (output ONLY this, nothing else):
{json_format}

{rules_section}

DO NOT include any explanatory text. DO NOT use markdown. Output ONLY the JSON object."""

        try:
            # Use AI query function if available (supports remote inference), otherwise use llm_manager (local only)
            if self.ai_query_func:
                result = await self.ai_query_func(
                    prompt=prompt,
                    compute_host=self.compute_host,
                    model=self.model,
                    provider=self.provider_alias
                )
                response = result["response"]
            else:
                # Fallback to direct llm_manager call (local only)
                response = await self.llm_manager.query(
                    prompt=prompt,
                    provider_alias=self.provider_alias
                )

            # Try to extract and parse JSON from response (with repair attempts)
            import json
            import re

            result = None
            json_str = None

            # Attempt 1: Try to find JSON object in response (handles markdown wrapping)
            json_match = re.search(r'\{(?:[^{}]|\{[^{}]*\})*\}', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    # Attempt 2: Try repairing the extracted JSON
                    try:
                        repaired = self._repair_json(json_str)
                        result = json.loads(repaired)
                        print("[JSON Repair] Successfully repaired malformed JSON")
                    except json.JSONDecodeError as e:
                        print(f"[JSON Repair] Failed to repair extracted JSON: {e}")
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
                        print("[JSON Repair] Successfully repaired malformed response")
                    except json.JSONDecodeError as e:
                        # All attempts failed
                        raise ValueError(f"Failed to extract valid JSON after repair attempts: {e}")

            # Build knowledge entries
            entries = []
            for entry_data in result.get('entries', []):
                source = KnowledgeSource(
                    type="ai_summary",
                    conversation_id=self.conversation_id,
                    participants=[p['node_id'] for p in self.participants],
                    confidence_score=entry_data.get('confidence', 1.0),
                    sources_cited=entry_data.get('sources', []),
                    cultural_perspectives_considered=result.get('cultural_perspectives', [])
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
                    alternative_viewpoints=result.get('alternatives', [])
                )
                entries.append(entry)

            # Calculate average confidence
            avg_confidence = sum(e.confidence for e in entries) / len(entries) if entries else 1.0

            # Create proposal
            proposal = KnowledgeCommitProposal(
                conversation_id=self.conversation_id,
                topic=result.get('topic', 'conversation_summary'),
                summary=result.get('summary', 'Knowledge from group discussion'),
                entries=entries,
                participants=[p['node_id'] for p in self.participants],
                proposed_by='ai',
                cultural_perspectives=result.get('cultural_perspectives', []),
                alternatives=result.get('alternatives', []),
                flagged_assumptions=result.get('flagged_assumptions', []),
                devil_advocate=result.get('devil_advocate'),
                avg_confidence=avg_confidence,
                status='proposed'
            )

            return proposal

        except Exception as e:
            print(f"Error generating commit proposal: {e}")
            print(f"  LLM Response preview: {response[:300] if 'response' in locals() else 'N/A'}...")
            # Return empty proposal on error
            return KnowledgeCommitProposal(
                conversation_id=self.conversation_id,
                topic='error',
                summary='Failed to extract knowledge - LLM did not return valid JSON',
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
            timestamp = msg.timestamp.split('T')[1][:8] if 'T' in msg.timestamp else msg.timestamp
            lines.append(f"[{timestamp}] {msg.sender_name}: {msg.text}")
        return "\n".join(lines)

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

        Args:
            tokens: Total tokens in the conversation
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

    def add_message(self, role: str, content: str):
        """Add a message to the conversation history

        Args:
            role: 'user' or 'assistant'
            content: Message content
        """
        self.message_history.append({"role": role, "content": content})

    def get_message_history(self) -> List[Dict[str, str]]:
        """Get the full conversation history

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        return self.message_history.copy()

    def mark_context_included(self, context_hash: str):
        """Mark that context has been included in this conversation

        Args:
            context_hash: SHA256 hash of personal.json + device_context.json
        """
        self.context_included = True
        self.context_hash = context_hash

    def has_context_changed(self, new_hash: str) -> bool:
        """Check if context files have changed since last inclusion

        Args:
            new_hash: Current hash of context files

        Returns:
            True if context has changed
        """
        return new_hash != self.context_hash

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

    def reset_conversation(self):
        """Reset conversation history and context tracking (for "New Chat" button)"""
        self.message_history = []
        self.context_included = False
        self.context_hash = ""
        self.peer_context_hashes = {}
        self.current_token_count = 0
        self.message_buffer = []
        self.knowledge_score = 0.0
        # Phase 7: Also clear peer context caches
        self.peer_context_cache = {}
        self.peer_device_context_cache = {}

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
