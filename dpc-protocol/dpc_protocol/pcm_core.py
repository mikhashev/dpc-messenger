# dpc-protocol\dpc_protocol\pcm_core.py

import json
from pathlib import Path
from .crypto import DPC_HOME_DIR
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime

# --- 1. Data Structure Definitions (Data Structures) ---
# We use dataclasses for strict typing and convenience.
# This is our "contract" for what personal_context.json should look like.

@dataclass
class Book:
    title: str
    rating: int
    authors: Optional[List[str]] = field(default_factory=list)

# Forward declare for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    pass

@dataclass
class KnowledgeSource:
    """Provenance tracking for knowledge entries"""

    type: Literal["conversation", "ai_summary", "manual_edit", "import", "consensus"] = "manual_edit"
    conversation_id: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Consensus tracking
    consensus_status: Literal["draft", "approved", "rejected"] = "draft"
    approved_by: List[str] = field(default_factory=list)
    dissenting_opinions: List[str] = field(default_factory=list)
    commit_id: Optional[str] = None

    # Bias tracking
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

    # Bias flags
    cultural_specific: bool = False
    requires_context: List[str] = field(default_factory=list)  # ["Western workplace", "individualistic culture"]
    alternative_viewpoints: List[str] = field(default_factory=list)

@dataclass
class Topic:
    summary: str
    key_books: List[Book] = field(default_factory=list)
    preferred_authors: List[str] = field(default_factory=list)

    # v2.0 enhancements
    entries: List[KnowledgeEntry] = field(default_factory=list)
    mastery_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    learning_strategies: List[str] = field(default_factory=list)

    # Versioning
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_modified: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Linked markdown file (NEW - from Claude Code pattern)
    markdown_file: Optional[str] = None  # e.g., "knowledge/python_learning.md"

@dataclass
class Profile:
    name: str
    description: str
    core_values: List[str] = field(default_factory=list)

@dataclass
class Preferences:
    communication_style: str

# --- Enhanced Data Structures (v2.0) ---

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
class PersonalContext:
    """The main class representing the entire structure of personal_context.json (v2.0)"""
    profile: Profile
    knowledge: Dict[str, Topic] = field(default_factory=dict)
    preferences: Optional[Preferences] = None

    # v2.0 enhancements - Instructions (from PCM)
    instruction: InstructionBlock = field(default_factory=InstructionBlock)

    # v2.0 enhancements - Cognitive profile (from self-education template + bias awareness)
    cognitive_profile: Optional[CognitiveProfile] = None

    # v2.0 enhancements - Git-like versioning
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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalContext':
        """Creates a PersonalContext instance from a dictionary, handling nested structures.

        Supports both v1.0 and v2.0 formats for backward compatibility.
        """
        # Detect format version
        metadata = data.get('metadata', {})
        format_version = metadata.get('format_version', '1.0')

        # Load core v1 fields
        profile_data = data.get('profile', {})
        profile = Profile(**profile_data)

        preferences_data = data.get('preferences')
        preferences = Preferences(**preferences_data) if preferences_data else None

        # Load knowledge with v1/v2 compatibility
        knowledge_data = data.get('knowledge', {})
        knowledge = {}
        for topic_name, topic_content in knowledge_data.items():
            books = [Book(**book_data) for book_data in topic_content.get('key_books', [])]

            # v2 fields for KnowledgeEntry
            entries_data = topic_content.get('entries', [])
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
                    usage_count=entry_data.get('usage_count', 0),
                    effectiveness_score=entry_data.get('effectiveness_score', 1.0),
                    review_due=entry_data.get('review_due'),
                    cultural_specific=entry_data.get('cultural_specific', False),
                    requires_context=entry_data.get('requires_context', []),
                    alternative_viewpoints=entry_data.get('alternative_viewpoints', [])
                ))

            knowledge[topic_name] = Topic(
                summary=topic_content.get('summary', ''),
                key_books=books,
                preferred_authors=topic_content.get('preferred_authors', []),
                entries=entries,
                mastery_level=topic_content.get('mastery_level', 'beginner'),
                learning_strategies=topic_content.get('learning_strategies', []),
                version=topic_content.get('version', 1),
                created_at=topic_content.get('created_at', datetime.utcnow().isoformat()),
                last_modified=topic_content.get('last_modified', datetime.utcnow().isoformat()),
                markdown_file=topic_content.get('markdown_file')
            )

        # Load v2 fields if present (backward compatibility)
        instruction_data = data.get('instruction', {})
        instruction = InstructionBlock(**instruction_data) if instruction_data else InstructionBlock()

        cognitive_profile_data = data.get('cognitive_profile')
        cognitive_profile = None
        if cognitive_profile_data:
            bias_profile_data = cognitive_profile_data.get('bias_profile')
            bias_profile = BiasAwareness(**bias_profile_data) if bias_profile_data else None
            cognitive_profile = CognitiveProfile(
                memory_strengths=cognitive_profile_data.get('memory_strengths', []),
                memory_challenges=cognitive_profile_data.get('memory_challenges', []),
                optimal_learning_times=cognitive_profile_data.get('optimal_learning_times', {}),
                attention_span=cognitive_profile_data.get('attention_span', {}),
                cultural_background=cognitive_profile_data.get('cultural_background', ''),
                cultural_values=cognitive_profile_data.get('cultural_values', []),
                communication_norms=cognitive_profile_data.get('communication_norms', {}),
                bias_profile=bias_profile
            )

        # Load versioning fields
        version = data.get('version', 1)
        last_commit_id = data.get('last_commit_id')
        last_commit_message = data.get('last_commit_message')
        last_commit_timestamp = data.get('last_commit_timestamp', datetime.utcnow().isoformat())
        commit_history = data.get('commit_history', [])

        return cls(
            profile=profile,
            knowledge=knowledge,
            preferences=preferences,
            instruction=instruction,
            cognitive_profile=cognitive_profile,
            version=version,
            last_commit_id=last_commit_id,
            last_commit_message=last_commit_message,
            last_commit_timestamp=last_commit_timestamp,
            commit_history=commit_history,
            metadata=metadata if metadata else {
                "created": datetime.utcnow().isoformat(),
                "last_updated": datetime.utcnow().isoformat(),
                "storage": "local",
                "format_version": "2.0"
            }
        )

# --- 2. Class for managing the context file (Core Logic) ---

class PCMCore:
    """
    Handles all operations with the personal_context.json file:
    reading, writing, and template creation.
    """
    def __init__(self, file_path: str | Path | None = None):
        """
        Initializes the manager.
        If file_path is None, it defaults to 'personal.json' in the user's DPC home directory.
        """
        if file_path:
            self.file_path = Path(file_path)
        else:
            # Default to the main context file in the user's DPC dir
            # We will manage multiple contexts later, for now, one main file.
            self.file_path = DPC_HOME_DIR / "personal.json"

    def ensure_context_file_exists(self):
        """
        Checks if the context file exists. If not, creates a default template.
        """
        if not self.file_path.exists():
            print(f"Warning: Context file not found at {self.file_path}.")
            print("Creating a default template...")
            
            # Create parent directory if it doesn't exist
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            
            template_profile = Profile(
                name="New User",
                description="My personal context for D-PC.",
                core_values=[]
            )
            template_context = PersonalContext(profile=template_profile)
            self.save_context(template_context)
            print(f"Template context file created at {self.file_path}")

    def load_context(self) -> PersonalContext:
        """
        Loads, parses, and validates the context file.
        Ensures the file exists before trying to load it.
        """
        self.ensure_context_file_exists()
        
        with open(self.file_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
        
        return PersonalContext.from_dict(raw_data)

    def save_context(self, context: PersonalContext):
        """Save context to file, updating metadata timestamps"""
        # Update metadata timestamp
        context.metadata['last_updated'] = datetime.utcnow().isoformat()
        context.metadata['format_version'] = '2.0'

        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(context), f, indent=2, ensure_ascii=False)

    def create_template(self, overwrite: bool = False):
        if self.file_path.exists() and not overwrite:
            print(f"File {self.file_path} already exists. Use --overwrite to replace it.")
            return

        template_profile = Profile(
            name="Anonymous",
            description="A person interested in learning and exploring new ideas.",
            core_values=["Learning", "Curiosity"]
        )
        template_context = PersonalContext(profile=template_profile)
        
        self.save_context(template_context)
        print(f"Template context file created at {self.file_path}")


# --- 3. Usage example (for self-testing the module) ---

if __name__ == '__main__':
    print("--- Testing PCMCore ---")
    
    test_file = "test_context.json"
    core = PCMCore(test_file)

    print("\n1. Creating template file...")
    core.create_template(overwrite=True)

    print("\n2. Loading context from file...")
    try:
        my_context = core.load_context()
        print(f"   - Successfully loaded context for user: {my_context.profile.name}")
        print(f"   - Knowledge topics: {list(my_context.knowledge.keys())}")
    except Exception as e:
        print(f"   - Error loading context: {e}")

    print("\n3. Modifying and saving context...")
    if 'my_context' in locals():
        new_topic = Topic(
            summary="I'm a beginner in Python programming.",
            key_books=[Book(title="Automate the Boring Stuff with Python", rating=5)]
        )
        my_context.knowledge["python_programming"] = new_topic
        
        core.save_context(my_context)
        print("   - Context updated and saved.")

        reloaded_context = core.load_context()
        print(f"   - Reloaded context. New topics: {list(reloaded_context.knowledge.keys())}")
    
    Path(test_file).unlink()
    print(f"\n--- Test finished, {test_file} cleaned up. ---")