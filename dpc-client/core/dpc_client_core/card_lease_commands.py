"""The card lease's queue as a `local_api` command (Mike, 2026-11-25: the queue is visible).

`CardLeaseCommandsMixin` is mixed into `CoreService`, as the music commands are: `local_api` dispatches
a command by `getattr(core_service, command)`, and `get_card_lease_status` is in
`local_api.ALLOWED_COMMANDS`. It is read-only and has no agent tool (an agent has `card_status`, which
leaves the owner's chats out); the Agent map reads it when it opens, and `card_lease_updated` pushes
every change after that.

The service starts the publisher (`managers.card_lease_state`) when it starts and stops it when it
stops: the file CC reads, `<dpc home>/state/card_lease.json`, and the push to the UI.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CardLeaseCommandsMixin:
    """Needs `self.settings` (its `dpc_home_dir`) and `self.local_api` (its `broadcast_event`)."""

    _card_lease_publisher: Optional[Any] = None

    async def get_card_lease_status(self) -> Dict[str, Any]:
        """Who holds the GPU card, who waits for it in order, and the last few grants and releases:
        `card_lease.queue_snapshot()` with `status: "success"`. Read-only; it waits for nothing."""
        from .managers import card_lease

        try:
            return {"status": "success", **card_lease.queue_snapshot()}
        except Exception as exc:
            logger.error("get_card_lease_status failed: %s", exc, exc_info=True)
            return {"status": "error", "message": f"The card queue could not be read: {exc}"}

    def _start_card_lease_state(self) -> None:
        """Begin writing the queue file and pushing the queue to the UI, on the running loop. A
        failure is logged and leaves the backend starting."""
        import asyncio

        try:
            from .managers.card_lease_state import CardLeaseStatePublisher, state_file_path

            local_api = getattr(self, "local_api", None)
            broadcast = getattr(local_api, "broadcast_event", None)
            publisher = CardLeaseStatePublisher(
                asyncio.get_running_loop(), state_file_path(self.settings.dpc_home_dir), broadcast)
            publisher.start()
            self._card_lease_publisher = publisher
        except Exception:
            logger.warning("The card queue file and push did not start", exc_info=True)

    def _stop_card_lease_state(self) -> None:
        publisher, self._card_lease_publisher = self._card_lease_publisher, None
        if publisher is not None:
            try:
                publisher.stop()
            except Exception:
                logger.debug("The card queue publisher did not stop cleanly", exc_info=True)
