"""
Group chat manager for persistent group metadata.

Handles create, join, leave, delete, and sync operations for group chats.
Group metadata is stored as individual JSON files in ~/.dpc/groups/.
"""

import json
import logging
import uuid
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

    Storage: ~/.dpc/groups/{group_id}.json (one file per group)
    """

    MAX_MEMBERS = 20

    def __init__(self, dpc_home: Path, node_id: str):
        self.dpc_home = dpc_home
        self.node_id = node_id
        self.groups_dir = dpc_home / "groups"
        self._groups: Dict[str, GroupMetadata] = {}

    def load_from_disk(self):
        """Load all group metadata files from disk."""
        if not self.groups_dir.exists():
            logger.debug("Groups directory not found at %s, starting fresh", self.groups_dir)
            return

        loaded = 0
        for group_file in self.groups_dir.glob("*.json"):
            try:
                with open(group_file, "r") as f:
                    data = json.load(f)
                group = GroupMetadata.from_dict(data)
                self._groups[group.group_id] = group
                loaded += 1
            except Exception as e:
                logger.error("Error loading group file %s: %s", group_file, e)

        if loaded:
            logger.info("Loaded %d groups from disk", loaded)

    def _save_group(self, group_id: str):
        """Save a single group metadata file to disk."""
        group = self._groups.get(group_id)
        if not group:
            return

        try:
            self.groups_dir.mkdir(parents=True, exist_ok=True)
            group_file = self.groups_dir / f"{group_id}.json"
            with open(group_file, "w") as f:
                json.dump(group.to_dict(), f, indent=2)
            logger.debug("Saved group %s to disk", group_id)
        except Exception as e:
            logger.error("Error saving group %s: %s", group_id, e)

    def _delete_group_file(self, group_id: str):
        """Delete a group metadata file from disk."""
        try:
            group_file = self.groups_dir / f"{group_id}.json"
            if group_file.exists():
                group_file.unlink()
                logger.debug("Deleted group file for %s", group_id)
        except Exception as e:
            logger.error("Error deleting group file %s: %s", group_id, e)

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

    def handle_group_deleted(self, group_id: str):
        """Handle GROUP_DELETE from creator — remove local copy."""
        if group_id in self._groups:
            del self._groups[group_id]
            self._delete_group_file(group_id)
            logger.info("Removed group %s (deleted by creator)", group_id)
