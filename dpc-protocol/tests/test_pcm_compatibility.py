"""
Tests for PCM format compatibility (v1.0 and v2.0)

This test suite ensures that:
1. v1.0 files can be loaded without errors
2. v1.0 files are automatically upgraded to v2.0 format
3. v2.0 files can be saved and loaded correctly
4. Backward compatibility is maintained
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from dpc_protocol.pcm_core import (
    PCMCore,
    PersonalContext,
    Profile,
    Topic,
    Book,
    KnowledgeEntry,
    KnowledgeSource,
    InstructionBlock,
    CognitiveProfile,
    BiasAwareness
)


class TestPCMBackwardCompatibility:
    """Test backward compatibility with v1.0 format"""

    def test_load_v1_minimal(self):
        """Test loading a minimal v1.0 format file"""
        v1_data = {
            "profile": {
                "name": "Test User",
                "description": "A test user"
            }
        }

        # Save v1 format to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v1_data, f)
            temp_path = Path(f.name)

        try:
            # Load using PCMCore
            core = PCMCore(temp_path)
            context = core.load_context()

            # Verify core fields loaded
            assert context.profile.name == "Test User"
            assert context.profile.description == "A test user"

            # Verify v2 fields have defaults
            assert isinstance(context.instruction, InstructionBlock)
            assert context.cognitive_profile is None
            assert context.version == 1
            assert context.commit_history == []
        finally:
            temp_path.unlink()

    def test_load_v1_with_knowledge(self):
        """Test loading v1.0 format with knowledge topics"""
        v1_data = {
            "profile": {
                "name": "Alice",
                "description": "Game designer",
                "core_values": ["creativity", "collaboration"]
            },
            "knowledge": {
                "python_programming": {
                    "summary": "Learning Python basics",
                    "key_books": [
                        {
                            "title": "Automate the Boring Stuff",
                            "rating": 5,
                            "authors": ["Al Sweigart"]
                        }
                    ],
                    "preferred_authors": ["Al Sweigart"]
                }
            },
            "preferences": {
                "communication_style": "casual"
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v1_data, f)
            temp_path = Path(f.name)

        try:
            core = PCMCore(temp_path)
            context = core.load_context()

            # Verify v1 fields
            assert context.profile.name == "Alice"
            assert len(context.knowledge) == 1
            assert "python_programming" in context.knowledge

            topic = context.knowledge["python_programming"]
            assert topic.summary == "Learning Python basics"
            assert len(topic.key_books) == 1
            assert topic.key_books[0].title == "Automate the Boring Stuff"

            # Verify v2 enhancements have defaults
            assert topic.entries == []
            assert topic.mastery_level == "beginner"
            assert topic.version == 1
            assert topic.markdown_file is None
        finally:
            temp_path.unlink()

    def test_save_upgrades_to_v2(self):
        """Test that saving a loaded v1 file upgrades it to v2"""
        v1_data = {
            "profile": {
                "name": "Bob",
                "description": "Developer"
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v1_data, f)
            temp_path = Path(f.name)

        try:
            # Load v1 and save
            core = PCMCore(temp_path)
            context = core.load_context()
            core.save_context(context)

            # Read raw JSON
            with open(temp_path, 'r') as f:
                saved_data = json.load(f)

            # Verify v2 format
            assert 'metadata' in saved_data
            assert saved_data['metadata']['format_version'] == '2.0'
            assert 'instruction' in saved_data
            assert 'version' in saved_data
            assert 'commit_history' in saved_data
        finally:
            temp_path.unlink()


class TestPCMV2Format:
    """Test v2.0 format features"""

    def test_create_v2_with_instructions(self):
        """Test creating a context with instruction blocks"""
        profile = Profile(name="Carol", description="AI researcher")
        instruction = InstructionBlock(
            primary="Focus on ML and AI topics",
            bias_mitigation={
                "require_multi_perspective": True,
                "challenge_status_quo": True
            }
        )

        context = PersonalContext(profile=profile, instruction=instruction)

        assert context.instruction.primary == "Focus on ML and AI topics"
        assert context.instruction.bias_mitigation["require_multi_perspective"] is True

    def test_create_v2_with_cognitive_profile(self):
        """Test creating a context with cognitive profile"""
        profile = Profile(name="David", description="Designer")

        bias_awareness = BiasAwareness(
            known_biases=["sunk_cost_fallacy"],
            mitigation_strategies={"sunk_cost": "Evaluate current value, not past investment"}
        )

        cognitive_profile = CognitiveProfile(
            cultural_background="Eastern collective",
            memory_strengths=["visual", "spatial"],
            bias_profile=bias_awareness
        )

        context = PersonalContext(
            profile=profile,
            cognitive_profile=cognitive_profile
        )

        assert context.cognitive_profile.cultural_background == "Eastern collective"
        assert len(context.cognitive_profile.bias_profile.known_biases) == 1

    def test_knowledge_entry_with_source(self):
        """Test creating knowledge entries with provenance"""
        source = KnowledgeSource(
            type="ai_summary",
            conversation_id="conv-123",
            participants=["alice", "bob"],
            confidence_score=0.85,
            cultural_perspectives_considered=["Western", "Eastern"]
        )

        entry = KnowledgeEntry(
            content="Environmental storytelling is powerful",
            tags=["game_design", "narrative"],
            source=source,
            confidence=0.9,
            cultural_specific=False,
            alternative_viewpoints=["Dialogue-heavy approach", "Audio logs"]
        )

        assert entry.content == "Environmental storytelling is powerful"
        assert entry.source.type == "ai_summary"
        assert entry.source.confidence_score == 0.85
        assert len(entry.alternative_viewpoints) == 2

    def test_topic_with_entries_and_versioning(self):
        """Test creating topics with knowledge entries and versioning"""
        entry1 = KnowledgeEntry(
            content="Use crystal refraction mechanics",
            tags=["puzzle_design"]
        )

        entry2 = KnowledgeEntry(
            content="Environmental cues > explicit tutorial",
            tags=["tutorial_design"]
        )

        topic = Topic(
            summary="Game design principles",
            entries=[entry1, entry2],
            mastery_level="intermediate",
            version=3,
            markdown_file="knowledge/game_design.md"
        )

        assert len(topic.entries) == 2
        assert topic.mastery_level == "intermediate"
        assert topic.version == 3
        assert topic.markdown_file == "knowledge/game_design.md"

    def test_commit_history_tracking(self):
        """Test git-like commit history"""
        profile = Profile(name="Eve", description="Writer")

        context = PersonalContext(profile=profile)

        # Add commit
        context.commit_history.append({
            "commit_id": "commit-abc123",
            "timestamp": datetime.utcnow().isoformat(),
            "message": "Added game design philosophy",
            "participants": ["eve", "frank"],
            "consensus": "unanimous"
        })

        context.version += 1
        context.last_commit_id = "commit-abc123"
        context.last_commit_message = "Added game design philosophy"

        assert context.version == 2
        assert len(context.commit_history) == 1
        assert context.commit_history[0]["consensus"] == "unanimous"

    def test_save_and_load_v2_roundtrip(self):
        """Test saving and loading a complete v2 context"""
        profile = Profile(name="Frank", description="Engineer")

        source = KnowledgeSource(
            type="manual_edit",
            confidence_score=1.0
        )

        entry = KnowledgeEntry(
            content="Test-driven development is essential",
            tags=["testing", "methodology"],
            source=source
        )

        topic = Topic(
            summary="Software engineering practices",
            entries=[entry],
            mastery_level="advanced",
            version=2
        )

        instruction = InstructionBlock(
            primary="Focus on software engineering best practices"
        )

        context = PersonalContext(
            profile=profile,
            knowledge={"software_engineering": topic},
            instruction=instruction,
            version=5,
            commit_history=[
                {
                    "commit_id": "commit-xyz789",
                    "message": "Initial commit",
                    "timestamp": datetime.utcnow().isoformat()
                }
            ]
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_path = Path(f.name)

        try:
            # Save
            core = PCMCore(temp_path)
            core.save_context(context)

            # Load
            loaded_context = core.load_context()

            # Verify all fields
            assert loaded_context.profile.name == "Frank"
            assert len(loaded_context.knowledge) == 1
            assert "software_engineering" in loaded_context.knowledge
            assert loaded_context.knowledge["software_engineering"].mastery_level == "advanced"
            assert loaded_context.instruction.primary == "Focus on software engineering best practices"
            assert loaded_context.version == 5
            assert len(loaded_context.commit_history) == 1
            assert loaded_context.metadata['format_version'] == '2.0'
        finally:
            temp_path.unlink()


class TestMigrationScenarios:
    """Test common migration scenarios"""

    def test_migrate_empty_v1_to_v2(self):
        """Test migrating minimal v1 file"""
        v1_data = {
            "profile": {
                "name": "Minimal User",
                "description": "Minimal context"
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v1_data, f)
            temp_path = Path(f.name)

        try:
            core = PCMCore(temp_path)
            context = core.load_context()
            core.save_context(context)

            # Verify migration
            with open(temp_path, 'r') as f:
                migrated = json.load(f)

            assert migrated['metadata']['format_version'] == '2.0'
            assert 'instruction' in migrated
            assert 'commit_history' in migrated
        finally:
            temp_path.unlink()

    def test_migrate_complex_v1_to_v2(self):
        """Test migrating complex v1 file with multiple topics"""
        v1_data = {
            "profile": {
                "name": "Complex User",
                "description": "User with many topics",
                "core_values": ["learning", "teaching"]
            },
            "knowledge": {
                "topic1": {
                    "summary": "Topic 1 summary",
                    "key_books": [{"title": "Book 1", "rating": 5}]
                },
                "topic2": {
                    "summary": "Topic 2 summary",
                    "preferred_authors": ["Author 1"]
                }
            },
            "preferences": {
                "communication_style": "formal"
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v1_data, f)
            temp_path = Path(f.name)

        try:
            core = PCMCore(temp_path)
            context = core.load_context()

            # Verify all v1 data preserved
            assert context.profile.name == "Complex User"
            assert len(context.knowledge) == 2
            assert "topic1" in context.knowledge
            assert "topic2" in context.knowledge
            assert context.preferences.communication_style == "formal"

            # Save as v2
            core.save_context(context)

            # Reload and verify
            context2 = core.load_context()
            assert context2.metadata['format_version'] == '2.0'
            assert len(context2.knowledge) == 2
            assert context2.knowledge["topic1"].summary == "Topic 1 summary"
        finally:
            temp_path.unlink()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
