"""Handlers for chat history synchronization commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class RequestChatHistoryHandler(MessageHandler):
    """Handles REQUEST_CHAT_HISTORY command - peer requesting conversation history."""

    @property
    def command_name(self) -> str:
        return "REQUEST_CHAT_HISTORY"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REQUEST_CHAT_HISTORY message.

        Peer is requesting chat history (e.g., after reconnecting and losing local history).
        We export our conversation history and send it back.

        Args:
            sender_node_id: Node ID of requesting peer
            payload: Contains conversation_id, request_id

        Returns:
            None (sends CHAT_HISTORY_RESPONSE to peer)
        """
        conversation_id = payload.get("conversation_id")
        request_id = payload.get("request_id")

        self.logger.info(f"Chat history request from {sender_node_id} for {conversation_id} (request_id: {request_id})")

        # For group chats, use conversation_id; for 1:1, use sender_node_id
        monitor_key = conversation_id if conversation_id and conversation_id.startswith("group-") else sender_node_id
        conversation_monitor = self.service.conversation_monitors.get(monitor_key)

        if not conversation_monitor and monitor_key.startswith("group-"):
            if hasattr(self.service, 'knowledge_service') and self.service.knowledge_service:
                conversation_monitor = self.service.knowledge_service._get_or_create_conversation_monitor(monitor_key)

        if not conversation_monitor:
            self.logger.warning(f"No conversation monitor found for {monitor_key}")
            # Send empty response
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "CHAT_HISTORY_RESPONSE",
                "payload": {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "messages": [],
                    "total_count": 0
                }
            })
            return None

        # Export conversation history
        messages = conversation_monitor.export_history()

        self.logger.info(f"Sending {len(messages)} messages to {sender_node_id}")

        # Send history response
        await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
            "command": "CHAT_HISTORY_RESPONSE",
            "payload": {
                "conversation_id": conversation_id,
                "request_id": request_id,
                "messages": messages,
                "total_count": len(messages)
            }
        })

        return None


class ChatHistoryResponseHandler(MessageHandler):
    """Handles CHAT_HISTORY_RESPONSE command - receiving conversation history from peer."""

    @property
    def command_name(self) -> str:
        return "CHAT_HISTORY_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle CHAT_HISTORY_RESPONSE message.

        Receive conversation history from peer and restore it locally.

        Args:
            sender_node_id: Node ID of peer sending history
            payload: Contains conversation_id, request_id, messages, total_count

        Returns:
            None (broadcasts history_restored event to UI)
        """
        conversation_id = payload.get("conversation_id")
        request_id = payload.get("request_id")
        messages = payload.get("messages", [])
        total_count = payload.get("total_count", 0)

        self.logger.info(f"Received {total_count} messages from {sender_node_id} for {conversation_id} (request_id: {request_id})")

        # For group chats, use conversation_id from payload; for 1:1, use sender_node_id
        monitor_key = conversation_id if conversation_id and conversation_id.startswith("group-") else sender_node_id
        conversation_monitor = self.service.conversation_monitors.get(monitor_key)

        if not conversation_monitor and monitor_key.startswith("group-"):
            if hasattr(self.service, 'knowledge_service') and self.service.knowledge_service:
                conversation_monitor = self.service.knowledge_service._get_or_create_conversation_monitor(monitor_key)

        if not conversation_monitor:
            self.logger.warning(f"No conversation monitor found for {monitor_key} - cannot import history")
            return None

        # Import history into conversation monitor
        conversation_monitor.import_history(messages)

        # Broadcast to UI for display
        await self.service.local_api.broadcast_event("history_restored", {
            "conversation_id": monitor_key,
            "message_count": len(messages),
            "messages": messages
        })

        self.logger.info(f"Chat history restored: {len(messages)} messages for {monitor_key}")

        return None
