"""
Group chat manager for persistent group metadata.

Handles create, join, leave, delete, and sync operations for group chats.
Group metadata is stored as individual JSON files in ~/.dpc/conversations/{group_id}/.
Legacy location ~/.dpc/groups/ is supported for migration.
"""

import json
import logging
import uuid
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)


@dataclass
class GroupMetadata:
    """Metadata for a group chat."""
    group_id: str
    name: str
    topic: str = ""
    created_by: str = ""
    created_at: str = ""
    members: List[str] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GroupMetadata":
        return cls(
            group_id=data["group_id"],
            name=data["name"],
            topic=data.get("topic", ""),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", ""),
            members=data.get("members", []),
            version=data.get("version", 1),
        )


class GroupManager:
    """
    Manages group chat metadata: create, join, leave, delete, sync, persist.

    Storage (v0.21.0): ~/.dpc/conversations/{group_id}/metadata.json
    Legacy storage: ~/.dpc/groups/{group_id}.json (supported for migration)
    Deleted groups registry: ~/.dpc/groups/deleted_registry.json
    """

    MAX_MEMBERS = 20

    def __init__(self, dpc_home: Path, node_id: str):
        self.dpc_home = dpc_home
        self.node_id = node_id
        self.groups_dir = dpc_home / "groups"  # Legacy, kept for deleted_registry
        self.conversations_dir = dpc_home / "conversations"  # New unified location
        self._groups: Dict[str, GroupMetadata] = {}
        self._deleted_groups: Dict[str, Dict[str, str]] = {}  # group_id -> {deleted_at, deleted_by}
        self._deleted_registry_path = self.groups_dir / "deleted_registry.json"

    def _get_conversation_dir(self, group_id: str) -> Path:
        """Get the conversation folder path for a group.

        Args:
            group_id: Group ID

        Returns:
            Path to ~/.dpc/conversations/{group_id}/
        """
        return self.conversations_dir / group_id

    def _get_group_metadata_path(self, group_id: str) -> Path:
        """Get path to group metadata file.

        Args:
            group_id: Group ID

        Returns:
            Path to ~/.dpc/conversations/{group_id}/metadata.json
        """
        return self._get_conversation_dir(group_id) / "metadata.json"

    def _get_legacy_group_path(self, group_id: str) -> Path:
        """Get legacy path to group metadata file (for migration).

        Args:
            group_id: Group ID

        Returns:
            Path to ~/.dpc/groups/{group_id}.json
        """
        return self.groups_dir / f"{group_id}.json"

    def load_from_disk(self):
        """Load all group metadata files from disk.

        Loads from new location (~/.dpc/conversations/{group_id}/metadata.json).
        Also checks legacy location (~/.dpc/groups/{group_id}.json) and migrates.
        """
        loaded = 0
        migrated = 0

        # First, check for legacy groups that need migration
        if self.groups_dir.exists():
            for legacy_file in self.groups_dir.glob("*.json"):
                # Skip the deleted registry file
                if legacy_file.name == "deleted_registry.json":
                    continue

                try:
                    # Extract group_id from filename (e.g., "group-abc123.json" -> "group-abc123")
                    group_id = legacy_file.stem
                    new_path = self._get_group_metadata_path(group_id)

                    # If new location doesn't exist, migrate
                    if not new_path.exists():
                        logger.info("Migrating group %s from legacy location", group_id)
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(legacy_file), str(new_path))
                        migrated += 1

                    # Also migrate history file if it exists
                    legacy_history = self.groups_dir / f"{group_id}_history.json"
                    if legacy_history.exists():
                        new_history_path = self._get_conversation_dir(group_id) / "history.json"
                        if not new_history_path.exists():
                            shutil.move(str(legacy_history), str(new_history_path))
                            logger.debug("Migrated history file for group %s", group_id)

                except Exception as e:
                    logger.error("Error migrating group file %s: %s", legacy_file, e)

        # Load from new location
        if self.conversations_dir.exists():
            for conv_dir in self.conversations_dir.iterdir():
                # Only process directories that look like group IDs
                if not conv_dir.is_dir() or not conv_dir.name.startswith("group-"):
                    continue

                metadata_file = conv_dir / "metadata.json"
                if not metadata_file.exists():
                    continue

                try:
                    with open(metadata_file, "r") as f:
                        data = json.load(f)
                    group = GroupMetadata.from_dict(data)
                    self._groups[group.group_id] = group
                    loaded += 1
                except Exception as e:
                    logger.error("Error loading group file %s: %s", metadata_file, e)

        if loaded:
            logger.info("Loaded %d groups from disk", loaded)
        if migrated:
            logger.info("Migrated %d groups from legacy location", migrated)

        # Load deleted groups registry (v0.20.0) - kept in groups dir
        self._load_deleted_registry()

    def _save_group(self, group_id: str):
        """Save a single group metadata file to disk.

        Saves to ~/.dpc/conversations/{group_id}/metadata.json
        """
        group = self._groups.get(group_id)
        if not group:
            return

        try:
            metadata_path = self._get_group_metadata_path(group_id)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            with open(metadata_path, "w") as f:
                json.dump(group.to_dict(), f, indent=2)
            logger.debug("Saved group %s to %s", group_id, metadata_path)
        except Exception as e:
            logger.error("Error saving group %s: %s", group_id, e)

    def _delete_group_file(self, group_id: str):
        """Delete entire conversation folder for a group.

        This removes metadata, history, and all files received in this group.
        """
        try:
            # Delete entire conversation folder (v0.21.0)
            conv_dir = self._get_conversation_dir(group_id)
            if conv_dir.exists():
                shutil.rmtree(conv_dir)
                logger.info("Deleted conversation folder for group %s", group_id)

            # Also clean up legacy files if they still exist (shouldn't after migration)
            legacy_file = self._get_legacy_group_path(group_id)
            if legacy_file.exists():
                legacy_file.unlink()
                logger.debug("Deleted legacy group file for %s", group_id)

            legacy_history = self.groups_dir / f"{group_id}_history.json"
            if legacy_history.exists():
                legacy_history.unlink()
                logger.debug("Deleted legacy history file for group %s", group_id)

        except Exception as e:
            logger.error("Error deleting group files for %s: %s", group_id, e)

    # --- Deleted Groups Registry (v0.20.0) ---

    def _load_deleted_registry(self):
        """Load deleted groups registry from disk."""
        if not self._deleted_registry_path.exists():
            return

        try:
            with open(self._deleted_registry_path, "r") as f:
                data = json.load(f)
            self._deleted_groups = data.get("deleted_groups", {})
            if self._deleted_groups:
                logger.info("Loaded %d deleted group IDs from registry", len(self._deleted_groups))
        except Exception as e:
            logger.error("Error loading deleted registry: %s", e)

    def _save_deleted_registry(self):
        """Save deleted groups registry to disk."""
        try:
            self.groups_dir.mkdir(parents=True, exist_ok=True)
            with open(self._deleted_registry_path, "w") as f:
                json.dump({
                    "version": 1,
                    "deleted_groups": self._deleted_groups
                }, f, indent=2)
            logger.debug("Saved deleted groups registry (%d entries)", len(self._deleted_groups))
        except Exception as e:
            logger.error("Error saving deleted registry: %s", e)

    def mark_group_deleted(self, group_id: str, deleted_by: str):
        """Add a group to the deleted registry.

        Args:
            group_id: Group that was deleted
            deleted_by: Node ID of the user who deleted it
        """
        self._deleted_groups[group_id] = {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "deleted_by": deleted_by
        }
        self._save_deleted_registry()
        logger.info("Marked group %s as deleted by %s", group_id, deleted_by)

    def is_group_deleted(self, group_id: str) -> bool:
        """Check if a group has been deleted.

        Args:
            group_id: Group ID to check

        Returns:
            True if group is in deleted registry
        """
        return group_id in self._deleted_groups

    def get_deleted_group_ids(self) -> List[str]:
        """Get list of all deleted group IDs.

        Returns:
            List of group IDs that have been deleted
        """
        return list(self._deleted_groups.keys())

    def create_group(self, name: str, topic: str, member_node_ids: List[str]) -> GroupMetadata:
        """
        Create a new group chat.

        Args:
            name: Group display name
            topic: Group topic/description
            member_node_ids: List of member node IDs (creator is auto-included)

        Returns:
            Created GroupMetadata
        """
        group_id = f"group-{uuid.uuid4().hex[:12]}"

        # Ensure creator is in members
        members = list(member_node_ids)
        if self.node_id not in members:
            members.insert(0, self.node_id)

        if len(members) > self.MAX_MEMBERS:
            raise ValueError(f"Group cannot exceed {self.MAX_MEMBERS} members")

        group = GroupMetadata(
            group_id=group_id,
            name=name.strip(),
            topic=topic.strip(),
            created_by=self.node_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            members=members,
            version=1,
        )

        self._groups[group_id] = group
        self._save_group(group_id)
        logger.info("Created group '%s' (%s) with %d members", name, group_id, len(members))
        return group

    def get_group(self, group_id: str) -> Optional[GroupMetadata]:
        """Get group metadata by ID."""
        return self._groups.get(group_id)

    def get_all_groups(self) -> List[GroupMetadata]:
        """Get all groups this node belongs to."""
        return list(self._groups.values())

    def get_groups_for_peer(self, node_id: str) -> List[GroupMetadata]:
        """Get all groups that contain the given peer."""
        return [g for g in self._groups.values() if node_id in g.members]

    def add_member(self, group_id: str, node_id: str) -> Optional[GroupMetadata]:
        """
        Add a member to a group.

        Returns:
            Updated GroupMetadata, or None if group not found
        """
        group = self._groups.get(group_id)
        if not group:
            logger.warning("Cannot add member: group %s not found", group_id)
            return None

        if node_id in group.members:
            logger.debug("Node %s already in group %s", node_id, group_id)
            return group

        if len(group.members) >= self.MAX_MEMBERS:
            raise ValueError(f"Group cannot exceed {self.MAX_MEMBERS} members")

        group.members.append(node_id)
        group.version += 1
        self._save_group(group_id)
        logger.info("Added %s to group %s (v%d)", node_id, group_id, group.version)
        return group

    def remove_member(self, group_id: str, node_id: str) -> Optional[GroupMetadata]:
        """
        Remove a member from a group.

        Returns:
            Updated GroupMetadata, or None if group not found
        """
        group = self._groups.get(group_id)
        if not group:
            logger.warning("Cannot remove member: group %s not found", group_id)
            return None

        if node_id not in group.members:
            logger.debug("Node %s not in group %s", node_id, group_id)
            return group

        group.members.remove(node_id)
        group.version += 1
        self._save_group(group_id)
        logger.info("Removed %s from group %s (v%d)", node_id, group_id, group.version)
        return group

    def delete_group(self, group_id: str, requester_node_id: str) -> bool:
        """
        Delete a group (creator-only).

        Args:
            group_id: Group to delete
            requester_node_id: Node ID requesting deletion

        Returns:
            True if deleted, False if not found or unauthorized
        """
        group = self._groups.get(group_id)
        if not group:
            logger.warning("Cannot delete: group %s not found", group_id)
            return False

        if group.created_by != requester_node_id:
            logger.warning(
                "Unauthorized delete attempt on %s by %s (creator: %s)",
                group_id, requester_node_id, group.created_by,
            )
            return False

        del self._groups[group_id]
        self._delete_group_file(group_id)

        # v0.20.0: Add to deleted registry for offline member sync
        self.mark_group_deleted(group_id, requester_node_id)

        logger.info("Deleted group %s by creator %s", group_id, requester_node_id)
        return True

    def leave_group(self, group_id: str) -> bool:
        """
        Leave a group (removes self from members, deletes local copy).

        Returns:
            True if left successfully
        """
        group = self._groups.get(group_id)
        if not group:
            return False

        if self.node_id in group.members:
            group.members.remove(self.node_id)

        del self._groups[group_id]
        self._delete_group_file(group_id)
        logger.info("Left group %s", group_id)
        return True

    def apply_sync(self, remote_group: Dict[str, Any]) -> Optional[GroupMetadata]:
        """
        Apply a GROUP_SYNC from a remote peer. Highest version wins.

        Args:
            remote_group: Group metadata dict from remote peer

        Returns:
            Updated GroupMetadata if applied, None if local version is newer
        """
        remote = GroupMetadata.from_dict(remote_group)

        local = self._groups.get(remote.group_id)
        if local and local.version >= remote.version:
            logger.debug(
                "Ignoring GROUP_SYNC for %s: local v%d >= remote v%d",
                remote.group_id, local.version, remote.version,
            )
            return None

        # Accept remote version
        self._groups[remote.group_id] = remote
        self._save_group(remote.group_id)
        logger.info(
            "Applied GROUP_SYNC for %s (v%d -> v%d)",
            remote.group_id,
            local.version if local else 0,
            remote.version,
        )
        return remote

    def handle_group_deleted(self, group_id: str, deleted_by: str = None):
        """Handle GROUP_DELETE from creator — remove local copy.

        Args:
            group_id: Group that was deleted
            deleted_by: Node ID of the creator who deleted it (optional, for registry)
        """
        if group_id in self._groups:
            # Get creator info before deleting
            if not deleted_by:
                deleted_by = self._groups[group_id].created_by

            del self._groups[group_id]
            self._delete_group_file(group_id)

            # v0.20.0: Add to deleted registry
            if deleted_by:
                self.mark_group_deleted(group_id, deleted_by)

            logger.info("Removed group %s (deleted by creator)", group_id)
