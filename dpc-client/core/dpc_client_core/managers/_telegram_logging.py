"""Telegram logging filter — suppresses verbose stacktraces for transient network errors.

PTB's default handler prints full ~90-line stacktraces for every NetworkError
(DNS fail, read timeout mid-long-poll, connection drop). These are expected
on unreliable networks — PTB's retry loop already backs off — but they drown
out real errors in the log.

This filter detects NetworkError/TimedOut exceptions on telegram.* loggers
and rewrites them as single-line WARNINGs, throttled to one per exception
class per minute. InvalidToken and other non-transient errors pass through
unchanged.

Install once at startup via install_telegram_log_filter().

Origin: LOW-1 (S41, 2026-04-15) — variant A per backlog.
"""

from __future__ import annotations

import logging
import time

_TELEGRAM_LOGGERS = ("telegram.ext.Updater", "telegram.ext._application")
_THROTTLE_WINDOW = 60.0


class _TelegramNetworkErrorFilter(logging.Filter):
    """Downgrade transient network errors to throttled WARNINGs without stacktrace."""

    def __init__(self) -> None:
        super().__init__()
        self._last_seen: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return True
        try:
            from telegram.error import NetworkError, TimedOut
        except ImportError:
            return True
        if not isinstance(exc, (NetworkError, TimedOut)):
            return True

        exc_name = type(exc).__name__
        now = time.monotonic()
        last = self._last_seen.get(exc_name)
        if last is not None and (now - last) < _THROTTLE_WINDOW:
            return False

        self._last_seen[exc_name] = now
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.msg = f"Telegram network error ({exc_name}): {exc} (further occurrences suppressed for {int(_THROTTLE_WINDOW)}s)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


_installed = False


def install_telegram_log_filter() -> None:
    """Attach the filter to PTB loggers. Idempotent."""
    global _installed
    if _installed:
        return
    flt = _TelegramNetworkErrorFilter()
    for name in _TELEGRAM_LOGGERS:
        logging.getLogger(name).addFilter(flt)
    _installed = True
