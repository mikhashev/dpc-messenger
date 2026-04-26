"""Handler for SEND_TEXT command - text messages between peers."""

from typing import Dict, Any, Optional
import hashlib
import time
from datetime import datetime, timezone
from . import MessageHandler
from ..conversation_monitor import Message as ConvMessage


class SendTextHandler(MessageHandler):
    """Handles SEND_TEXT messages (peer-to-peer text messages)."""

    @property
    def command_name(self) -> str:
        return "SEND_TEXT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle SEND_TEXT message.

        Features:
        - Message deduplication (prevents duplicate broadcasts)
        - Knowledge extraction (buffers messages for auto-detection)
        - UI notification (broadcasts to frontend)

        Args:
            sender_node_id: Node ID of sender
            payload: Contains "text" field with message content
        """
        text = payload.get("text")

        # Create unique message ID for deduplication
        message_id = hashlib.sha256(
            f"{sender_node_id}:{text}:{int(time.time() * 1000)}".encode()
        ).hexdigest()[:16]

        # Check if already processed
        if message_id in self.service._processed_message_ids:
            self.logger.debug("Duplicate message detected from %s, skipping", sender_node_id)
            return None

        # Add to processed set
        self.service._processed_message_ids.add(message_id)

        # Clean up old IDs
        if len(self.service._processed_message_ids) > self.service._max_processed_ids:
            to_remove = list(self.service._processed_message_ids)[:self.service._max_processed_ids // 2]
            for mid in to_remove:
                self.service._processed_message_ids.discard(mid)

        # Broadcast to UI with sender name
        sender_name = self.service.peer_metadata.get(sender_node_id, {}).get("name") or sender_node_id
        await self.service.local_api.broadcast_event("new_p2p_message", {
            "sender_node_id": sender_node_id,
            "sender_name": sender_name,
            "text": text,
            "message_id": message_id
        })

        # Feed to conversation monitor for knowledge extraction (Phase 4.2)
        # Always buffer messages (even when auto-detection is OFF) to enable manual extraction
        try:
            monitor = self.service._get_or_create_conversation_monitor(sender_node_id)

            # Create ConvMessage object for monitor
            conv_message = ConvMessage(
                message_id=message_id,
                conversation_id=sender_node_id,
                sender_node_id=sender_node_id,
                sender_name=self.service.peer_metadata.get(sender_node_id, {}).get("name", sender_node_id),
                text=text,
                timestamp=datetime.now(timezone.utc).isoformat()
            )

            # Buffer message for manual extraction
            await monitor.on_message(conv_message)
        except Exception as e:
            self.logger.error("Error in conversation monitoring: %s", e, exc_info=True)

        return None
