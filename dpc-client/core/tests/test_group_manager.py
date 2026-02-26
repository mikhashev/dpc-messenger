"""Tests for GroupManager - group chat metadata CRUD and persistence."""

import json
import tempfile
from pathlib import Path
import pytest

from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata


@pytest.fixture
def tmp_dpc_home(tmp_path):
    """Create a temporary DPC home directory."""
    return tmp_path


@pytest.fixture
def manager(tmp_dpc_home):
    """Create a GroupManager with temp storage."""
    return GroupManager(tmp_dpc_home, node_id="dpc-node-self-abc")


class TestGroupMetadata:
    def test_to_dict_roundtrip(self):
        group = GroupMetadata(
            group_id="group-test123",
            name="Test Group",
            topic="Testing",
            created_by="dpc-node-self-abc",
            created_at="2026-03-01T10:00:00Z",
            members=["dpc-node-self-abc", "dpc-node-alice-123"],
            version=1,
        )
        d = group.to_dict()
        restored = GroupMetadata.from_dict(d)
        assert restored.group_id == group.group_id
        assert restored.name == group.name
        assert restored.topic == group.topic
        assert restored.members == group.members
        assert restored.version == group.version

    def test_from_dict_defaults(self):
        group = GroupMetadata.from_dict({"group_id": "group-x", "name": "X"})
        assert group.topic == ""
        assert group.members == []
        assert group.version == 1


class TestGroupManagerCreate:
    def test_create_group(self, manager):
        group = manager.create_group("Alpha", "Sprint planning", ["dpc-node-alice-123"])
        assert group.group_id.startswith("group-")
        assert group.name == "Alpha"
        assert group.topic == "Sprint planning"
        assert group.created_by == "dpc-node-self-abc"
        assert "dpc-node-self-abc" in group.members
        assert "dpc-node-alice-123" in group.members
        assert group.version == 1

    def test_creator_auto_included(self, manager):
        group = manager.create_group("Beta", "", ["dpc-node-bob-456"])
        assert group.members[0] == "dpc-node-self-abc"

    def test_creator_not_duplicated(self, manager):
        group = manager.create_group("Gamma", "", ["dpc-node-self-abc", "dpc-node-bob-456"])
        assert group.members.count("dpc-node-self-abc") == 1

    def test_max_members_enforced(self, manager):
        members = [f"dpc-node-peer-{i:03d}" for i in range(21)]
        with pytest.raises(ValueError, match="cannot exceed"):
            manager.create_group("Big", "", members)

    def test_name_stripped(self, manager):
        group = manager.create_group("  Spaces  ", "  topic  ", [])
        assert group.name == "Spaces"
        assert group.topic == "topic"


class TestGroupManagerCRUD:
    def test_get_group(self, manager):
        group = manager.create_group("Test", "", ["dpc-node-alice-123"])
        retrieved = manager.get_group(group.group_id)
        assert retrieved is not None
        assert retrieved.name == "Test"

    def test_get_group_not_found(self, manager):
        assert manager.get_group("group-nonexistent") is None

    def test_get_all_groups(self, manager):
        manager.create_group("A", "", [])
        manager.create_group("B", "", [])
        assert len(manager.get_all_groups()) == 2

    def test_get_groups_for_peer(self, manager):
        manager.create_group("A", "", ["dpc-node-alice-123"])
        manager.create_group("B", "", ["dpc-node-bob-456"])
        alice_groups = manager.get_groups_for_peer("dpc-node-alice-123")
        assert len(alice_groups) == 1
        assert alice_groups[0].name == "A"

    def test_add_member(self, manager):
        group = manager.create_group("Test", "", [])
        updated = manager.add_member(group.group_id, "dpc-node-alice-123")
        assert "dpc-node-alice-123" in updated.members
        assert updated.version == 2

    def test_add_member_already_present(self, manager):
        group = manager.create_group("Test", "", ["dpc-node-alice-123"])
        original_version = group.version
        updated = manager.add_member(group.group_id, "dpc-node-alice-123")
        assert updated.version == original_version  # No version bump

    def test_add_member_nonexistent_group(self, manager):
        assert manager.add_member("group-nope", "dpc-node-alice-123") is None

    def test_add_member_max_enforced(self, manager):
        members = [f"dpc-node-peer-{i:03d}" for i in range(19)]
        group = manager.create_group("Full", "", members)
        assert len(group.members) == 20  # 19 + creator
        with pytest.raises(ValueError, match="cannot exceed"):
            manager.add_member(group.group_id, "dpc-node-extra-999")

    def test_remove_member(self, manager):
        group = manager.create_group("Test", "", ["dpc-node-alice-123"])
        updated = manager.remove_member(group.group_id, "dpc-node-alice-123")
        assert "dpc-node-alice-123" not in updated.members
        assert updated.version == 2

    def test_remove_member_not_present(self, manager):
        group = manager.create_group("Test", "", [])
        original_version = group.version
        updated = manager.remove_member(group.group_id, "dpc-node-nobody-999")
        assert updated.version == original_version

    def test_remove_member_nonexistent_group(self, manager):
        assert manager.remove_member("group-nope", "dpc-node-alice-123") is None


class TestGroupManagerDelete:
    def test_delete_by_creator(self, manager):
        group = manager.create_group("Test", "", [])
        assert manager.delete_group(group.group_id, "dpc-node-self-abc") is True
        assert manager.get_group(group.group_id) is None

    def test_delete_unauthorized(self, manager):
        group = manager.create_group("Test", "", [])
        assert manager.delete_group(group.group_id, "dpc-node-hacker-666") is False
        assert manager.get_group(group.group_id) is not None

    def test_delete_nonexistent(self, manager):
        assert manager.delete_group("group-nope", "dpc-node-self-abc") is False

    def test_delete_removes_file(self, manager):
        group = manager.create_group("Test", "", [])
        group_file = manager.groups_dir / f"{group.group_id}.json"
        assert group_file.exists()
        manager.delete_group(group.group_id, "dpc-node-self-abc")
        assert not group_file.exists()


class TestGroupManagerLeave:
    def test_leave_group(self, manager):
        group = manager.create_group("Test", "", ["dpc-node-alice-123"])
        assert manager.leave_group(group.group_id) is True
        assert manager.get_group(group.group_id) is None

    def test_leave_nonexistent(self, manager):
        assert manager.leave_group("group-nope") is False

    def test_leave_removes_file(self, manager):
        group = manager.create_group("Test", "", [])
        group_file = manager.groups_dir / f"{group.group_id}.json"
        assert group_file.exists()
        manager.leave_group(group.group_id)
        assert not group_file.exists()


class TestGroupManagerPersistence:
    def test_save_and_load(self, tmp_dpc_home):
        manager1 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        group = manager1.create_group("Persist", "test topic", ["dpc-node-alice-123"])

        # Load in a new manager instance
        manager2 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        manager2.load_from_disk()

        loaded = manager2.get_group(group.group_id)
        assert loaded is not None
        assert loaded.name == "Persist"
        assert loaded.topic == "test topic"
        assert loaded.members == group.members
        assert loaded.version == 1

    def test_multiple_groups_persist(self, tmp_dpc_home):
        manager1 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        manager1.create_group("A", "", [])
        manager1.create_group("B", "", [])

        manager2 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        manager2.load_from_disk()
        assert len(manager2.get_all_groups()) == 2

    def test_version_persists_after_update(self, tmp_dpc_home):
        manager1 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        group = manager1.create_group("Test", "", [])
        manager1.add_member(group.group_id, "dpc-node-alice-123")

        manager2 = GroupManager(tmp_dpc_home, "dpc-node-self-abc")
        manager2.load_from_disk()
        loaded = manager2.get_group(group.group_id)
        assert loaded.version == 2


class TestGroupManagerSync:
    def test_apply_newer_version(self, manager):
        group = manager.create_group("Old", "", [])
        remote = group.to_dict()
        remote["version"] = 5
        remote["name"] = "Updated"
        remote["members"].append("dpc-node-new-999")

        result = manager.apply_sync(remote)
        assert result is not None
        assert result.name == "Updated"
        assert result.version == 5
        assert "dpc-node-new-999" in result.members

    def test_ignore_older_version(self, manager):
        group = manager.create_group("Current", "", [])
        manager.add_member(group.group_id, "dpc-node-alice-123")  # bumps to v2

        remote = group.to_dict()
        remote["version"] = 1  # older
        remote["name"] = "Stale"

        result = manager.apply_sync(remote)
        assert result is None
        assert manager.get_group(group.group_id).name == "Current"

    def test_apply_new_group_via_sync(self, manager):
        remote = {
            "group_id": "group-remote-xyz",
            "name": "Remote Group",
            "topic": "",
            "created_by": "dpc-node-alice-123",
            "created_at": "2026-03-01T10:00:00Z",
            "members": ["dpc-node-alice-123", "dpc-node-self-abc"],
            "version": 1,
        }
        result = manager.apply_sync(remote)
        assert result is not None
        assert manager.get_group("group-remote-xyz") is not None

    def test_handle_group_deleted(self, manager):
        group = manager.create_group("Doomed", "", [])
        manager.handle_group_deleted(group.group_id)
        assert manager.get_group(group.group_id) is None

    def test_handle_group_deleted_nonexistent(self, manager):
        # Should not raise
        manager.handle_group_deleted("group-nope")
