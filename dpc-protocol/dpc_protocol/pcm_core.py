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

    # Edit tracking (Phase 5 - inline editing with attribution)
    edited_by: Optional[str] = None  # peer_id or node_id who last edited
    edited_at: Optional[str] = None  # ISO timestamp of last edit

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
    # Core fields (always present)
    summary: str
    entries: List[KnowledgeEntry] = field(default_factory=list)
    mastery_level: Literal["beginner", "intermediate", "advanced"] = "beginner"

    # Versioning (core)
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_modified: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # v2.0 - Linked markdown file
    markdown_file: Optional[str] = None  # e.g., "knowledge/python_learning.md"
    commit_id: Optional[str] = None  # Hash-based commit ID

    # Optional fields (None by default - user adds when needed)
    key_books: Optional[List[Book]] = None
    preferred_authors: Optional[List[str]] = None
    learning_strategies: Optional[List[str]] = None

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

            # Core fields
            topic_kwargs = {
                'summary': topic_content.get('summary', ''),
                'entries': entries,
                'mastery_level': topic_content.get('mastery_level', 'beginner'),
                'version': topic_content.get('version', 1),
                'created_at': topic_content.get('created_at', datetime.utcnow().isoformat()),
                'last_modified': topic_content.get('last_modified', datetime.utcnow().isoformat()),
                'markdown_file': topic_content.get('markdown_file'),
                'commit_id': topic_content.get('commit_id')
            }

            # Optional fields - only include if present
            if 'key_books' in topic_content and topic_content['key_books']:
                topic_kwargs['key_books'] = [Book(**book_data) for book_data in topic_content['key_books']]

            if 'preferred_authors' in topic_content and topic_content['preferred_authors']:
                topic_kwargs['preferred_authors'] = topic_content['preferred_authors']

            if 'learning_strategies' in topic_content and topic_content['learning_strategies']:
                topic_kwargs['learning_strategies'] = topic_content['learning_strategies']

            knowledge[topic_name] = Topic(**topic_kwargs)

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


# --- 3. Instructions Management (separate from personal.json) ---

def load_instructions(file_path: Path | None = None) -> InstructionBlock:
    """
    Load AI instructions from instructions.json.

    Args:
        file_path: Path to instructions.json. Defaults to ~/.dpc/instructions.json

    Returns:
        InstructionBlock instance
    """
    if file_path is None:
        file_path = DPC_HOME_DIR / "instructions.json"
    else:
        file_path = Path(file_path)

    # If instructions.json doesn't exist, return default
    if not file_path.exists():
        print(f"Instructions file not found at {file_path}. Using default instructions.")
        return InstructionBlock()

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Handle nested bias_mitigation and learning_support dicts
        return InstructionBlock(
            primary=data.get('primary', InstructionBlock().primary),
            context_update=data.get('context_update', InstructionBlock().context_update),
            verification_protocol=data.get('verification_protocol', InstructionBlock().verification_protocol),
            learning_support=data.get('learning_support', InstructionBlock().learning_support),
            bias_mitigation=data.get('bias_mitigation', InstructionBlock().bias_mitigation),
            collaboration_mode=data.get('collaboration_mode', 'individual'),
            consensus_required=data.get('consensus_required', True),
            ai_curation_enabled=data.get('ai_curation_enabled', True),
            dissent_encouraged=data.get('dissent_encouraged', True)
        )
    except Exception as e:
        print(f"Error loading instructions from {file_path}: {e}")
        print("Using default instructions.")
        return InstructionBlock()


def save_instructions(instructions: InstructionBlock, file_path: Path | None = None):
    """
    Save AI instructions to instructions.json.

    Args:
        instructions: InstructionBlock instance to save
        file_path: Path to instructions.json. Defaults to ~/.dpc/instructions.json
    """
    if file_path is None:
        file_path = DPC_HOME_DIR / "instructions.json"
    else:
        file_path = Path(file_path)

    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to dict and save
    data = asdict(instructions)

    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Instructions saved to {file_path}")


def migrate_instructions_from_personal_context(
    personal_json_path: Path | None = None,
    instructions_json_path: Path | None = None
) -> bool:
    """
    Migrate instructions from personal.json to instructions.json.

    IDEMPOTENT: Safe to run multiple times.

    Args:
        personal_json_path: Path to personal.json. Defaults to ~/.dpc/personal.json
        instructions_json_path: Path to instructions.json. Defaults to ~/.dpc/instructions.json

    Returns:
        True if changes were made, False if no changes needed
    """
    if personal_json_path is None:
        personal_json_path = DPC_HOME_DIR / "personal.json"
    else:
        personal_json_path = Path(personal_json_path)

    if instructions_json_path is None:
        instructions_json_path = DPC_HOME_DIR / "instructions.json"
    else:
        instructions_json_path = Path(instructions_json_path)

    if not personal_json_path.exists():
        print("No personal.json found, skipping migration")
        return False

    try:
        with open(personal_json_path, 'r', encoding='utf-8') as f:
            personal_data = json.load(f)

        changes_made = False

        # CASE 1: instructions.json exists
        if instructions_json_path.exists():
            print(f"Instructions file exists at {instructions_json_path}")

            # Check if personal.json still has instruction field
            if 'instruction' in personal_data:
                print("  → Cleaning up legacy instruction field")

                # Remove instruction field
                del personal_data['instruction']
                changes_made = True

                # Add external_files reference
                if 'external_files' not in personal_data.get('metadata', {}):
                    personal_data.setdefault('metadata', {})['external_files'] = {}

                from datetime import datetime
                personal_data['metadata']['external_files']['instructions'] = {
                    "file": "instructions.json",
                    "description": "AI behavior instructions",
                    "last_updated": datetime.utcnow().isoformat()
                }

                print("  ✓ Removed instruction field")
                print("  ✓ Added external_files reference")
            else:
                print("  ✓ Already clean")
                return False

        # CASE 2: instructions.json doesn't exist
        else:
            if 'instruction' not in personal_data:
                print("No instruction field found")
                return False

            print("Migrating instructions to separate file...")

            # Extract instruction
            instruction_data = personal_data['instruction']

            # Create InstructionBlock from data
            instructions = InstructionBlock(
                primary=instruction_data.get('primary', InstructionBlock().primary),
                context_update=instruction_data.get('context_update', InstructionBlock().context_update),
                verification_protocol=instruction_data.get('verification_protocol', InstructionBlock().verification_protocol),
                learning_support=instruction_data.get('learning_support', InstructionBlock().learning_support),
                bias_mitigation=instruction_data.get('bias_mitigation', InstructionBlock().bias_mitigation),
                collaboration_mode=instruction_data.get('collaboration_mode', 'individual'),
                consensus_required=instruction_data.get('consensus_required', True),
                ai_curation_enabled=instruction_data.get('ai_curation_enabled', True),
                dissent_encouraged=instruction_data.get('dissent_encouraged', True)
            )

            # Save to instructions.json
            save_instructions(instructions, instructions_json_path)
            print(f"  ✓ Created {instructions_json_path}")

            # Remove from personal.json
            del personal_data['instruction']

            # Add external_files reference
            from datetime import datetime
            personal_data.setdefault('metadata', {})['external_files'] = {
                'instructions': {
                    "file": "instructions.json",
                    "description": "AI behavior instructions",
                    "last_updated": datetime.utcnow().isoformat()
                }
            }

            changes_made = True

        # Save cleaned personal.json
        if changes_made:
            # Backup original
            import shutil
            backup_path = personal_json_path.with_suffix('.json.backup')
            shutil.copy(personal_json_path, backup_path)
            print(f"  ✓ Backed up to {backup_path}")

            # Save cleaned version
            with open(personal_json_path, 'w', encoding='utf-8') as f:
                json.dump(personal_data, f, indent=2, ensure_ascii=False)

            print(f"  ✓ Updated {personal_json_path}")

        return changes_made

    except Exception as e:
        print(f"Error during migration: {e}")
        return False


# --- 4. Usage example (for self-testing the module) ---

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