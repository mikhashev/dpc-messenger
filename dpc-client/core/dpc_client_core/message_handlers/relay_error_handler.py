"""
Relay Error Handler - Client-side ERROR routing for relay operations

The relay answers a bad RELAY_REGISTER or RELAY_MESSAGE with the generic
{"command": "ERROR", "payload": {"error": <code>, "message": ...}} — see
relay_register_handler.py and relay_message_handler.py for every code sent.
"ERROR" is not relay-specific; today this is the only sender of it (grep
confirms), but the handler still checks the payload's "error" code against
a known set before acting, rather than treating every ERROR as relay traffic.

Routing per code:
    not_volunteering            -> fails every pending RELAY_REGISTER on
                                    this relay connection (relay-wide)
    forward_failed, invalid_sender -> fails the oldest pending RELAY_MESSAGE
                                    forward on this relay connection (FIFO)
    invalid_request              -> ambiguous: relay_register_handler and
                                    relay_message_handler both use this code.
                                    Fails a pending register if one exists,
                                    else a pending forward.
    not_authorized                -> RELAY_DISCONNECT from a non-participant;
                                    logged only, nothing to settle client-side.
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)

#: The only codes the relay side ever sends (relay_register_handler.py,
#: relay_message_handler.py, relay_disconnect_handler.py). Anything else is
#: another subsystem's ERROR and is left untouched.
RELAY_ERROR_CODES = frozenset({
    "not_volunteering",
    "invalid_request",
    "invalid_sender",
    "forward_failed",
    "not_authorized",
})


class RelayErrorHandler(MessageHandler):
    """Handle ERROR frames that originate from relay-side handlers."""

    @property
    def command_name(self) -> str:
        return "ERROR"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        error_code = payload.get("error")
        message = payload.get("message", "")

        if error_code not in RELAY_ERROR_CODES:
            logger.debug(
                "ERROR from %s with code %r — not a relay error, ignoring",
                sender_node_id[:20], error_code
            )
            return

        relay_manager = getattr(self.service, 'relay_manager', None)
        if not relay_manager:
            logger.debug(
                "RelayManager not initialized — ignoring relay ERROR %s", error_code
            )
            return

        if error_code == "not_authorized":
            logger.warning(
                "Relay ERROR not_authorized from %s: %s (RELAY_DISCONNECT from a "
                "non-participant — nothing pending to settle)",
                sender_node_id[:20], message
            )
            return

        if error_code == "not_volunteering":
            failed = relay_manager.fail_pending_registers(sender_node_id, error_code, message)
            self._log_outcome("register", sender_node_id, error_code, message, failed)
            return

        if error_code in ("forward_failed", "invalid_sender"):
            failed = relay_manager.fail_pending_forward(sender_node_id, error_code, message)
            self._log_outcome("forward", sender_node_id, error_code, message, failed)
            return

        # invalid_request: shared by RELAY_REGISTER and RELAY_MESSAGE validation
        # failures. No field in the ERROR tells us which one it answers, so a
        # pending register is tried first, then a pending forward.
        failed = relay_manager.fail_pending_registers(sender_node_id, error_code, message)
        if not failed:
            ok = relay_manager.fail_pending_forward(sender_node_id, error_code, message)
            failed = 1 if ok else 0
        self._log_outcome("register/forward", sender_node_id, error_code, message, failed)

    def _log_outcome(self, kind: str, sender_node_id: str, error_code: str, message: str, failed) -> None:
        if failed:
            logger.warning(
                "Relay ERROR %s from %s failed %s pending %s: %s",
                error_code, sender_node_id[:20], failed, kind, message
            )
        else:
            logger.info(
                "Relay ERROR %s from %s — no pending %s to fail: %s",
                error_code, sender_node_id[:20], kind, message
            )
