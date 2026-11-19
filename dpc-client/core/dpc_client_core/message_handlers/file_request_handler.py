"""Handlers for FILE_REQUEST and FILE_REQUEST_DECLINED (managers/file_request.py)."""

from typing import Any, Dict, Optional

from . import MessageHandler
from ..managers.file_request import FILE_REQUEST, FILE_REQUEST_DECLINED


class FileRequestHandler(MessageHandler):
    """A peer asks this node to offer it a file it has a record of and not the bytes."""

    @property
    def command_name(self) -> str:
        return FILE_REQUEST

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        if not isinstance(payload, dict):
            return None
        await self.service.file_transfer_manager.file_requests.handle_request(sender_node_id, payload)
        return None


class FileRequestDeclinedHandler(MessageHandler):
    """A peer answered this node's FILE_REQUEST: it will not offer the file."""

    @property
    def command_name(self) -> str:
        return FILE_REQUEST_DECLINED

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        if not isinstance(payload, dict):
            return None
        await self.service.file_transfer_manager.file_requests.handle_declined(sender_node_id, payload)
        return None
