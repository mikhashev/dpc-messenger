"""Name the bot behind a Telegram getUpdates Conflict and count repeats.

python-telegram-bot's Updater logs a polling Conflict as a bare traceback that
does not say which bot it belongs to. DPC runs several pollers at once (the
owner bot plus one bridge per agent), so the line was useless for telling
"our own abandoned long-poll after a network drop" (one or two, then silence)
from "a second node polls this token" (repeats for as long as it runs).

``make_polling_error_callback`` returns the ``error_callback`` for
``Updater.start_polling``. Retry behaviour is untouched: PTB keeps retrying,
this only changes what is logged. The token is never logged.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque, Optional

log = logging.getLogger(__name__)
# Non-Conflict errors keep going where PTB's own default callback sends them,
# so the existing network-noise filters on this logger still apply.
_PTB_UPDATER_LOG = logging.getLogger("telegram.ext.Updater")

CONFLICT_WINDOW_SECONDS = 600.0
SECOND_POLLER_THRESHOLD = 5


class ConflictCounter:
    """Rolling count of Conflicts for one bot."""

    def __init__(
        self,
        window: float = CONFLICT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._clock = clock
        self._seen: Deque[float] = deque()

    def hit(self) -> int:
        now = self._clock()
        self._seen.append(now)
        while self._seen and now - self._seen[0] > self._window:
            self._seen.popleft()
        return len(self._seen)


def format_conflict_line(username: Optional[str], who: str, count: int,
                         threshold: int = SECOND_POLLER_THRESHOLD) -> str:
    line = (
        f"Telegram getUpdates Conflict for @{username or '?'} ({who}): another getUpdates "
        "on this token - either our own previous long-poll after a network drop, or a "
        f"second poller elsewhere; repeat {count} in the last 10 min"
    )
    if count >= threshold:
        line += " - likely a second instance polling this token (another node?)"
    return line


def make_polling_error_callback(
    get_username: Callable[[], Optional[str]],
    who: str,
    threshold: int = SECOND_POLLER_THRESHOLD,
    counter: Optional[ConflictCounter] = None,
) -> Callable[[Exception], None]:
    """Build the synchronous ``error_callback`` for ``Updater.start_polling``.

    ``get_username`` is called lazily (the username is only known after
    ``get_me``); ``who`` is a label such as "owner bot" or
    "agent bridge for agent_x".
    """
    from telegram.error import Conflict

    counter = counter or ConflictCounter()

    def _callback(exc: Exception) -> None:
        if not isinstance(exc, Conflict):
            _PTB_UPDATER_LOG.error("Exception happened while polling for updates.", exc_info=exc)
            return
        try:
            username = get_username()
        except Exception:
            username = None
        log.error(format_conflict_line(username, who, counter.hit(), threshold))

    return _callback
