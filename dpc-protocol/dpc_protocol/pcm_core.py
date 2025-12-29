# dpc-protocol\dpc_protocol\pcm_core.py

import json
import logging
from pathlib import Path
from .crypto import DPC_HOME_DIR
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Consensus tracking
    consensus_status: Literal["draft", "approved", "rejected"] = "draft"
    approved_by: List[str] = field(default_factory=list)
    dissenting_opinions: List[str] = field(default_factory=list)
    commit_id: Optional[str] = None

    # Bias tracking
    cultural_perspectives_considered: List[str] = field(default_factory=list)
    confidence_score: float = 1.0
    sources_cited: List[str] = field(default_factory=list)

    # Extraction metadata (for ai_summary type)
    extraction_model: Optional[str] = None  # Model used for extraction (e.g., "claude-haiku-4-5")
    extraction_host: Optional[str] = None  # Compute host ("local" or node_id)

@dataclass
class KnowledgeEntry:
    """Individual knowledge item with full provenance and bias tracking"""

    content: str
    tags: List[str] = field(default_factory=list)
    source: Optional[KnowledgeSource] = None

    # AI metadata
    confidence: float = 1.0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_modified: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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

    # Metadata (NEW for v2.0 - multi-set support)
    name: str = "General Purpose"
    description: str = "Default instructions for general conversations"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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
class InstructionSet:
    """Container for multiple named instruction sets (v2.0)"""

    schema_version: str = "2.0"
    default: str = "general"  # Default instruction set key
    sets: Dict[str, InstructionBlock] = field(default_factory=dict)

    def get_default(self) -> Optional[InstructionBlock]:
        """Get the default instruction set"""
        return self.sets.get(self.default, self.sets.get("general"))

    def get_set(self, name: str) -> Optional[InstructionBlock]:
        """Get a specific instruction set by name"""
        return self.sets.get(name)

    def create_set(self, key: str, name: str, description: str = "") -> InstructionBlock:
        """Create a new empty instruction set"""
        instruction_block = InstructionBlock(
            name=name,
            description=description,
            created=datetime.now(timezone.utc).isoformat(),
            last_updated=datetime.now(timezone.utc).isoformat()
        )
        self.sets[key] = instruction_block
        return instruction_block

    def delete_set(self, key: str) -> bool:
        """Delete an instruction set (protect 'general')"""
        if key == "general":
            logger.warning("Cannot delete the 'general' instruction set")
            return False
        if key not in self.sets:
            logger.warning(f"Instruction set '{key}' not found")
            return False
        del self.sets[key]
        # If deleted set was default, reset to 'general'
        if self.default == key:
            self.default = "general"
        return True

    def rename_set(self, old_key: str, new_key: str, new_name: str) -> bool:
        """Rename an instruction set"""
        if old_key == "general":
            logger.warning("Cannot rename the 'general' instruction set")
            return False
        if old_key not in self.sets:
            logger.warning(f"Instruction set '{old_key}' not found")
            return False
        if new_key in self.sets and new_key != old_key:
            logger.warning(f"Instruction set '{new_key}' already exists")
            return False

        # Update the instruction block
        instruction_block = self.sets[old_key]
        instruction_block.name = new_name
        instruction_block.last_updated = datetime.now(timezone.utc).isoformat()

        # Move to new key if key changed
        if new_key != old_key:
            self.sets[new_key] = instruction_block
            del self.sets[old_key]
            # Update default if needed
            if self.default == old_key:
                self.default = new_key

        return True

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

    # v2.0 enhancements - Cognitive profile (from self-education template + bias awareness)
    cognitive_profile: Optional[CognitiveProfile] = None

    # v2.0 enhancements - Git-like versioning
    version: int = 1
    last_commit_id: Optional[str] = None
    last_commit_message: Optional[str] = None
    last_commit_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    commit_history: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=lambda: {
        "created": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "storage": "local",
        "format_version": "2.0"
    })

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalContext':
        """Creates a PersonalContext instance from a dictionary (v2.0 format)."""
        # Load metadata
        metadata = data.get('metadata', {})

        # Load core v1 fields
        # Handle None values (can occur when firewall filters fields)
        profile_data = data.get('profile')
        if profile_data:
            profile = Profile(**profile_data)
        else:
            # Create empty profile if blocked by firewall
            profile = Profile(name="[Restricted]", description="[Access denied by firewall]")

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
                    last_updated=entry_data.get('last_updated', datetime.now(timezone.utc).isoformat()),
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
                'created_at': topic_content.get('created_at', datetime.now(timezone.utc).isoformat()),
                'last_modified': topic_content.get('last_modified', datetime.now(timezone.utc).isoformat()),
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

        # Load v2 fields
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
        last_commit_timestamp = data.get('last_commit_timestamp', datetime.now(timezone.utc).isoformat())
        commit_history = data.get('commit_history', [])

        return cls(
            profile=profile,
            knowledge=knowledge,
            preferences=preferences,
            cognitive_profile=cognitive_profile,
            version=version,
            last_commit_id=last_commit_id,
            last_commit_message=last_commit_message,
            last_commit_timestamp=last_commit_timestamp,
            commit_history=commit_history,
            metadata=metadata if metadata else {
                "created": datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
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
            logger.warning("Context file not found at %s", self.file_path)
            logger.info("Creating a default template")

            # Create parent directory if it doesn't exist
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

            template_profile = Profile(
                name="New User",
                description="My personal context for D-PC.",
                core_values=[]
            )
            template_context = PersonalContext(profile=template_profile)
            self.save_context(template_context)
            logger.info("Template context file created at %s", self.file_path)

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
        context.metadata['last_updated'] = datetime.now(timezone.utc).isoformat()
        context.metadata['format_version'] = '2.0'

        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(context), f, indent=2, ensure_ascii=False)

    def create_template(self, overwrite: bool = False):
        if self.file_path.exists() and not overwrite:
            logger.warning("File %s already exists. Use --overwrite to replace it", self.file_path)
            return

        template_profile = Profile(
            name="Anonymous",
            description="A person interested in learning and exploring new ideas.",
            core_values=["Learning", "Curiosity"]
        )
        template_context = PersonalContext(profile=template_profile)

        self.save_context(template_context)
        logger.info("Template context file created at %s", self.file_path)


# --- 3. Instructions Management (separate from personal.json) ---

class InstructionSetManager:
    """Manages loading, saving, and operations on instruction sets (v2.0)"""

    def __init__(self, config_dir: Path | None = None):
        """
        Initialize the instruction set manager.

        Args:
            config_dir: Directory containing instructions.json. Defaults to ~/.dpc/
        """
        if config_dir is None:
            config_dir = DPC_HOME_DIR
        else:
            config_dir = Path(config_dir)

        self.instructions_file = config_dir / "instructions.json"
        self.instruction_set: Optional[InstructionSet] = None

    def load(self) -> InstructionSet:
        """
        Load instruction sets from file, create default if missing.

        Returns:
            InstructionSet instance
        """
        if not self.instructions_file.exists():
            logger.info("Instructions file not found at %s. Creating default instruction set", self.instructions_file)
            return self._create_default()

        try:
            with open(self.instructions_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Load v2.0 format directly (no migration needed since no existing users)
            instruction_set = self._load_from_dict(data)
            self.instruction_set = instruction_set
            return instruction_set

        except Exception as e:
            logger.error("Error loading instructions from %s: %s", self.instructions_file, e, exc_info=True)
            logger.info("Creating default instruction set")
            return self._create_default()

    def _load_from_dict(self, data: dict) -> InstructionSet:
        """Load InstructionSet from dictionary data"""
        schema_version = data.get('schema_version', '2.0')
        default = data.get('default', 'general')
        sets_data = data.get('sets', {})

        # Convert each set dict to InstructionBlock
        sets = {}
        for key, inst_data in sets_data.items():
            sets[key] = InstructionBlock(
                name=inst_data.get('name', 'General Purpose'),
                description=inst_data.get('description', ''),
                created=inst_data.get('created', datetime.now(timezone.utc).isoformat()),
                last_updated=inst_data.get('last_updated', datetime.now(timezone.utc).isoformat()),
                primary=inst_data.get('primary', InstructionBlock().primary),
                context_update=inst_data.get('context_update', InstructionBlock().context_update),
                verification_protocol=inst_data.get('verification_protocol', InstructionBlock().verification_protocol),
                learning_support=inst_data.get('learning_support', InstructionBlock().learning_support),
                bias_mitigation=inst_data.get('bias_mitigation', InstructionBlock().bias_mitigation),
                collaboration_mode=inst_data.get('collaboration_mode', 'individual'),
                consensus_required=inst_data.get('consensus_required', True),
                ai_curation_enabled=inst_data.get('ai_curation_enabled', True),
                dissent_encouraged=inst_data.get('dissent_encouraged', True)
            )

        return InstructionSet(
            schema_version=schema_version,
            default=default,
            sets=sets
        )

    def _create_default(self) -> InstructionSet:
        """Create default instruction set with 'general' set"""
        default_instruction = InstructionBlock(
            name="General Purpose",
            description="Default instructions for general conversations",
            created=datetime.now(timezone.utc).isoformat(),
            last_updated=datetime.now(timezone.utc).isoformat()
        )

        instruction_set = InstructionSet(
            schema_version="2.0",
            default="general",
            sets={"general": default_instruction}
        )

        self.instruction_set = instruction_set
        self.save(instruction_set)
        return instruction_set

    def save(self, instruction_set: InstructionSet):
        """
        Save instruction sets to file.

        Args:
            instruction_set: InstructionSet instance to save
        """
        # Ensure parent directory exists
        self.instructions_file.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict
        data = {
            "schema_version": instruction_set.schema_version,
            "default": instruction_set.default,
            "sets": {
                key: asdict(inst)
                for key, inst in instruction_set.sets.items()
            }
        }

        with open(self.instructions_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info("Instructions saved to %s (%d sets)", self.instructions_file, len(instruction_set.sets))
        self.instruction_set = instruction_set

    def import_template(self, template_file: Path, set_key: str, set_name: str):
        """
        Import instruction template from personal-context-manager format.

        Args:
            template_file: Path to template JSON file
            set_key: Key for the new instruction set (e.g., "learning-math")
            set_name: Display name for the instruction set
        """
        try:
            with open(template_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Extract ai_rules.instruction block
            if "ai_rules" in data and "instruction" in data["ai_rules"]:
                instruction_data = data["ai_rules"]["instruction"]
            elif "instruction" in data:
                instruction_data = data["instruction"]
            else:
                instruction_data = data  # Assume top-level is instruction

            # Create InstructionBlock
            instruction_block = InstructionBlock(
                name=set_name,
                description=instruction_data.get("description", ""),
                primary=instruction_data.get("primary", InstructionBlock().primary),
                context_update=instruction_data.get("context_update", InstructionBlock().context_update),
                verification_protocol=instruction_data.get("verification_protocol", InstructionBlock().verification_protocol),
                learning_support=instruction_data.get("learning_support", InstructionBlock().learning_support),
                bias_mitigation=instruction_data.get("bias_mitigation", InstructionBlock().bias_mitigation),
                collaboration_mode=instruction_data.get("collaboration_mode", "individual"),
                consensus_required=instruction_data.get("consensus_required", True),
                ai_curation_enabled=instruction_data.get("ai_curation_enabled", True),
                dissent_encouraged=instruction_data.get("dissent_encouraged", True)
            )

            # Add to instruction set
            if self.instruction_set is None:
                self.instruction_set = self.load()

            self.instruction_set.sets[set_key] = instruction_block
            self.save(self.instruction_set)

            logger.info("Imported template from %s as '%s'", template_file, set_key)

        except Exception as e:
            logger.error("Error importing template from %s: %s", template_file, e, exc_info=True)
            raise


# --- Legacy functions for backward compatibility ---

def load_instructions(file_path: Path | None = None) -> InstructionBlock:
    """
    Load AI instructions from instructions.json (legacy function).
    Returns the default instruction set from the v2.0 format.

    Args:
        file_path: Path to instructions.json. Defaults to ~/.dpc/instructions.json

    Returns:
        InstructionBlock instance
    """
    manager = InstructionSetManager(file_path.parent if file_path else None)
    instruction_set = manager.load()
    return instruction_set.get_default() or InstructionBlock()


def save_instructions(instructions: InstructionBlock, file_path: Path | None = None):
    """
    Save AI instructions to instructions.json (legacy function).
    Saves as the default instruction set in v2.0 format.

    Args:
        instructions: InstructionBlock instance to save
        file_path: Path to instructions.json. Defaults to ~/.dpc/instructions.json
    """
    manager = InstructionSetManager(file_path.parent if file_path else None)
    instruction_set = InstructionSet(
        schema_version="2.0",
        default="general",
        sets={"general": instructions}
    )
    manager.save(instruction_set)


# --- 4. Usage example (for self-testing the module) ---

if __name__ == '__main__':
    logger.info("Testing PCMCore")

    test_file = "test_context.json"
    core = PCMCore(test_file)

    logger.info("1. Creating template file")
    core.create_template(overwrite=True)

    logger.info("2. Loading context from file")
    try:
        my_context = core.load_context()
        logger.info("Successfully loaded context for user: %s", my_context.profile.name)
        logger.info("Knowledge topics: %s", list(my_context.knowledge.keys()))
    except Exception as e:
        logger.error("Error loading context: %s", e, exc_info=True)

    logger.info("3. Modifying and saving context")
    if 'my_context' in locals():
        new_topic = Topic(
            summary="I'm a beginner in Python programming.",
            key_books=[Book(title="Automate the Boring Stuff with Python", rating=5)]
        )
        my_context.knowledge["python_programming"] = new_topic

        core.save_context(my_context)
        logger.info("Context updated and saved")

        reloaded_context = core.load_context()
        logger.info("Reloaded context. New topics: %s", list(reloaded_context.knowledge.keys()))

    Path(test_file).unlink()
    logger.info("Test finished, %s cleaned up", test_file)