"""Handlers for group chat commands."""

import hashlib
import time
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from . import MessageHandler
from ..conversation_monitor import Message as ConvMessage


class GroupCreateHandler(MessageHandler):
    """Handles GROUP_CREATE messages (group invite from creator)."""

    @property
    def command_name(self) -> str:
        return "GROUP_CREATE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_CREATE message.

        Creator is inviting this node to a new group. Store metadata locally
        and notify UI for acceptance.

        Args:
            sender_node_id: Node ID of group creator
            payload: Group metadata dict (group_id, name, topic, members, etc.)
        """
        group_id = payload.get("group_id")
        name = payload.get("name", "")

        self.logger.info(
            "Received GROUP_CREATE from %s: group=%s name='%s'",
            sender_node_id[:20], group_id, name
        )

        # Apply via sync (creates local copy)
        group = self.service.group_manager.apply_sync(payload)
        if group:
            # Notify UI of new group invite
            await self.service.local_api.broadcast_event("group_invite_received", {
                "group_id": group.group_id,
                "name": group.name,
                "topic": group.topic,
                "created_by": group.created_by,
                "creator_name": self.service.peer_metadata.get(
                    sender_node_id, {}
                ).get("name", sender_node_id),
                "members": group.members,
            })

        return None


class GroupTextHandler(MessageHandler):
    """Handles GROUP_TEXT messages (text messages in group chat)."""

    @property
    def command_name(self) -> str:
        return "GROUP_TEXT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_TEXT message.

        Routes message to group conversation, deduplicates, broadcasts to UI,
        and feeds to conversation monitor for knowledge extraction.

        Args:
            sender_node_id: Node ID of message sender
            payload: Contains group_id, text, sender_name, message_id (v0.20.0+)
        """
        group_id = payload.get("group_id")
        text = payload.get("text")
        sender_name = payload.get("sender_name", sender_node_id)

        # v0.20.0: Use sender-provided message_id if available, else generate for backwards compat
        message_id = payload.get("message_id")
        if not message_id:
            # Fallback for older clients
            message_id = hashlib.sha256(
                f"{group_id}:{sender_node_id}:{text}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]

        # Deduplication
        dedup_key = f"{group_id}:{message_id}"
        if dedup_key in self.service._processed_message_ids:
            self.logger.debug("Duplicate group message from %s, skipping", sender_node_id)
            return None

        self.service._processed_message_ids.add(dedup_key)

        # Clean up old IDs
        if len(self.service._processed_message_ids) > self.service._max_processed_ids:
            to_remove = list(self.service._processed_message_ids)[:self.service._max_processed_ids // 2]
            for mid in to_remove:
                self.service._processed_message_ids.discard(mid)

        # Relay to group members the sender may not be directly connected to.
        # In a star topology (B↔A↔C, but B↛C), A must relay B's message to C.
        # Dedup key already set above prevents relay loops.
        group = self.service.group_manager.get_group(group_id)
        if group:
            relay_msg = {"command": "GROUP_TEXT", "payload": payload}
            for member_id in group.members:
                if member_id == self.service.p2p_manager.node_id:
                    continue  # Skip self
                if member_id == sender_node_id:
                    continue  # Skip original sender
                if member_id in self.service.p2p_manager.peers:
                    try:
                        await self.service.p2p_manager.send_message_to_peer(member_id, relay_msg)
                        self.logger.debug(
                            "Relayed GROUP_TEXT %s from %s to %s",
                            message_id[:8], sender_node_id[:20], member_id[:20]
                        )
                    except Exception as e:
                        self.logger.error(
                            "Failed to relay group message to %s: %s", member_id[:20], e
                        )

        # Use sender-provided timestamp if available (v0.20.0)
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

        # Broadcast to UI
        await self.service.local_api.broadcast_event("group_text_received", {
            "group_id": group_id,
            "sender_node_id": sender_node_id,
            "sender_name": sender_name,
            "text": text,
            "message_id": message_id,
            "timestamp": timestamp,
            "mentions": payload.get("mentions", []),
        })

        # Feed to conversation monitor for knowledge extraction
        try:
            monitor = self.service._get_or_create_conversation_monitor(group_id)

            conv_message = ConvMessage(
                message_id=message_id,
                conversation_id=group_id,
                sender_node_id=sender_node_id,
                sender_name=sender_name,
                text=text,
                timestamp=timestamp,  # v0.20.0: Use sender-provided timestamp
            )

            proposal = await monitor.on_message(conv_message)

            if self.service.auto_knowledge_detection_enabled and proposal:
                self.logger.info("Knowledge proposal generated for group %s", group_id)
                await self.service.local_api.broadcast_event(
                    "knowledge_commit_proposed",
                    proposal.to_dict()
                )
                await self.service.consensus_manager.propose_commit(
                    proposal=proposal,
                    broadcast_func=self.service._broadcast_to_peers,
                )
        except Exception as e:
            self.logger.error("Error in group conversation monitoring: %s", e, exc_info=True)

        return None


class GroupLeaveHandler(MessageHandler):
    """Handles GROUP_LEAVE messages (member departing group)."""

    @property
    def command_name(self) -> str:
        return "GROUP_LEAVE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_LEAVE message.

        Remove departing member from local group metadata.

        Args:
            sender_node_id: Node ID of member leaving
            payload: Contains group_id
        """
        group_id = payload.get("group_id")

        self.logger.info(
            "Received GROUP_LEAVE from %s for group %s",
            sender_node_id[:20], group_id
        )

        group = self.service.group_manager.remove_member(group_id, sender_node_id)
        if group:
            await self.service.local_api.broadcast_event("group_member_left", {
                "group_id": group_id,
                "node_id": sender_node_id,
                "member_name": self.service.peer_metadata.get(
                    sender_node_id, {}
                ).get("name", sender_node_id),
                "remaining_members": group.members,
            })

        return None


class GroupDeleteHandler(MessageHandler):
    """Handles GROUP_DELETE messages (creator deleting group)."""

    @property
    def command_name(self) -> str:
        return "GROUP_DELETE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_DELETE message.

        Creator deleted the group. Remove local copy and clean up.

        Args:
            sender_node_id: Node ID of creator who deleted
            payload: Contains group_id
        """
        group_id = payload.get("group_id")

        self.logger.info(
            "Received GROUP_DELETE from %s for group %s",
            sender_node_id[:20], group_id
        )

        # Verify sender is the creator
        group = self.service.group_manager.get_group(group_id)
        if group and group.created_by != sender_node_id:
            self.logger.warning(
                "Ignoring GROUP_DELETE from non-creator %s (creator: %s)",
                sender_node_id, group.created_by
            )
            return None

        # Capture group name before deletion for UI notification
        group_name = group.name if group else group_id

        # Remove local group data
        self.service.group_manager.handle_group_deleted(group_id)

        # Clean up conversation monitor
        if group_id in self.service.conversation_monitors:
            del self.service.conversation_monitors[group_id]

        # Notify UI
        await self.service.local_api.broadcast_event("group_deleted", {
            "group_id": group_id,
            "deleted_by": sender_node_id,
            "group_name": group_name,
        })

        return None


class GroupSyncHandler(MessageHandler):
    """Handles GROUP_SYNC messages (metadata reconciliation on connect)."""

    @property
    def command_name(self) -> str:
        return "GROUP_SYNC"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_SYNC message.

        Reconcile group metadata with remote peer. Highest version wins.

        Args:
            sender_node_id: Node ID of sync source
            payload: Group metadata dict with version
        """
        group_id = payload.get("group_id")
        remote_version = payload.get("version", 0)

        self.logger.info(
            "Received GROUP_SYNC from %s for group %s (v%d)",
            sender_node_id[:20], group_id, remote_version
        )

        result = self.service.group_manager.apply_sync(payload)
        if result:
            # Notify UI of updated group
            await self.service.local_api.broadcast_event("group_updated", {
                "group_id": result.group_id,
                "name": result.name,
                "topic": result.topic,
                "members": result.members,
                "version": result.version,
            })

        return None


class GroupHistoryRequestHandler(MessageHandler):
    """Handles GROUP_HISTORY_REQUEST messages (peer requesting group chat history)."""

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_REQUEST message.

        Send our group conversation history to the requesting peer.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains group_id
        """
        group_id = payload.get("group_id")

        self.logger.info(
            "Received GROUP_HISTORY_REQUEST from %s for group %s",
            sender_node_id[:20], group_id
        )

        # Get conversation monitor for this group
        monitor = self.service.conversation_monitors.get(group_id)
        if not monitor:
            self.logger.debug("No conversation history for group %s", group_id)
            return None

        # Export history and send back
        history = monitor.export_history() if hasattr(monitor, "export_history") else []
        await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
            "command": "GROUP_HISTORY_RESPONSE",
            "payload": {
                "group_id": group_id,
                "history": history,
            }
        })

        return None


class GroupHistoryResponseHandler(MessageHandler):
    """Handles GROUP_HISTORY_RESPONSE messages (receiving group chat history)."""

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_RESPONSE message.

        Import received group conversation history.

        Args:
            sender_node_id: Node ID of history provider
            payload: Contains group_id and history array
        """
        group_id = payload.get("group_id")
        history = payload.get("history", [])

        self.logger.info(
            "Received GROUP_HISTORY_RESPONSE from %s for group %s (%d messages)",
            sender_node_id[:20], group_id, len(history)
        )

        if not history:
            return None

        monitor = self.service._get_or_create_conversation_monitor(group_id)

        # v0.20.0: Use merge_history instead of import_history
        # This handles duplicates and saves to disk
        if hasattr(monitor, "merge_history"):
            added = monitor.merge_history(history)
            self.logger.info("Merged %d new messages into group %s history", added, group_id)
        elif hasattr(monitor, "import_history"):
            # Fallback for older monitors
            monitor.import_history(history)

        # Notify UI to refresh chat
        await self.service.local_api.broadcast_event("group_history_synced", {
            "group_id": group_id,
            "message_count": len(history),
        })

        return None


class GroupHistoryStatusHandler(MessageHandler):
    """Handles GROUP_HISTORY_STATUS messages (v0.20.0 hash-based sync).

    Exchange history hashes to determine if sync is needed.
    """

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_STATUS"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_STATUS message.

        Compare hashes and request history if peer has newer/different messages.

        Args:
            sender_node_id: Node ID of peer
            payload: Contains group_id, history_hash, message_count
        """
        group_id = payload.get("group_id")
        remote_hash = payload.get("history_hash", "")
        remote_count = payload.get("message_count", 0)
        is_reply = payload.get("is_reply", False)

        self.logger.debug(
            "Received GROUP_HISTORY_STATUS from %s for group %s (hash=%s, count=%d)",
            sender_node_id[:20], group_id, remote_hash[:16], remote_count
        )

        # Get local monitor
        monitor = self.service.conversation_monitors.get(group_id)

        # Compute local hash
        local_hash = monitor.compute_history_hash() if monitor and hasattr(monitor, "compute_history_hash") else "sha256:empty"
        local_count = len(monitor.message_history) if monitor else 0

        # Reply only to the initiating STATUS (not to replies), to prevent infinite ping-pong.
        # A sends STATUS → B replies once with is_reply=True → A does NOT reply again.
        if not is_reply:
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "GROUP_HISTORY_STATUS",
                "payload": {
                    "group_id": group_id,
                    "history_hash": local_hash,
                    "message_count": local_count,
                    "is_reply": True,
                }
            })

        # If hashes differ and peer has more messages, request history
        if remote_hash != local_hash and remote_count > local_count:
            self.logger.info(
                "Requesting history sync for group %s (local: %d, remote: %d)",
                group_id, local_count, remote_count
            )
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "GROUP_HISTORY_REQUEST",
                "payload": {"group_id": group_id}
            })

        return None


class GroupDeletedStatusHandler(MessageHandler):
    """Handles GROUP_DELETED_STATUS messages (v0.20.0 offline deletion notification).

    Exchange deleted group IDs to ensure eventually consistent deletion.
    """

    @property
    def command_name(self) -> str:
        return "GROUP_DELETED_STATUS"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_DELETED_STATUS message.

        Check if we have any groups that were deleted by the peer and remove them.

        Args:
            sender_node_id: Node ID of peer
            payload: Contains deleted_groups list
        """
        deleted_groups = payload.get("deleted_groups", [])

        if not deleted_groups:
            return None

        self.logger.debug(
            "Received GROUP_DELETED_STATUS from %s with %d deleted groups",
            sender_node_id[:20], len(deleted_groups)
        )

        # Check if we have any of the deleted groups
        removed_count = 0
        for group_id in deleted_groups:
            group = self.service.group_manager.get_group(group_id)
            if group:
                # Verify the deleter was the creator
                if group.created_by == sender_node_id:
                    self.logger.info(
                        "Removing locally deleted group %s (deleted by creator %s)",
                        group_id, sender_node_id[:20]
                    )
                    group_name = group.name

                    # Remove local group data
                    self.service.group_manager.handle_group_deleted(group_id)

                    # Clean up conversation monitor
                    if group_id in self.service.conversation_monitors:
                        del self.service.conversation_monitors[group_id]

                    # Notify UI
                    await self.service.local_api.broadcast_event("group_deleted", {
                        "group_id": group_id,
                        "deleted_by": sender_node_id,
                        "group_name": group_name,
                    })
                    removed_count += 1

        if removed_count > 0:
            self.logger.info("Removed %d groups based on deleted status from %s", removed_count, sender_node_id[:20])

        return None
