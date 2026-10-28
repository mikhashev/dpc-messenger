"""One page of a chat's live history, for `get_conversation_history`.

A chat used to receive its whole live history in one local-API message, which
grew with the chat (5.1 MiB for one group after the tool-output clip of
`tool_detail.py`). The UI now asks for the newest page when a chat opens and
for the page before its oldest message when the reader scrolls to the top.

The cursor, `before_message_id`, is a history record's `id` — never
`msg_index`, which `rechain()` renumbers. Records are taken in the order the
history is presented in (`_chronological_key`), and a page is cut by count
(`limit`) and by the bytes the local API would send for it, whichever comes
first; `has_older` says whether anything older is left. A cursor that is not in
the live history (reset, archived, merged away) is an error the pager stops on:
answering it with the newest page would loop for ever.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from .conversation_monitor import _chronological_key

# Messages in a page when the caller names no limit.
DEFAULT_LIMIT = 100
# The largest limit accepted. The byte budget below is what bounds an answer;
# at the ~1 KiB a plain chat message takes as JSON, 500 messages already reach
# it, so a larger count would only make the backend sort and measure more.
MAX_LIMIT = 500
# Serialized messages per page, measured as local_api sends them (json.dumps,
# ASCII-escaped, so characters are bytes). Well under the 4 MiB warning bound.
PAGE_BYTES = 512 * 1024


class HistoryPageError(ValueError):
    """A page request the pager must stop on."""


def checked_limit(limit: Any) -> int:
    """The page size to use; `None` is the default, anything not positive is refused."""
    if limit is None:
        return DEFAULT_LIMIT
    # bool is an int in Python; `true` from the UI is not a page size.
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise HistoryPageError(f"limit must be a whole number of messages, got {limit!r}")
    if limit <= 0:
        raise HistoryPageError(f"limit must be at least 1, got {limit} (there is no 'all')")
    if limit > MAX_LIMIT:
        raise HistoryPageError(f"limit must be at most {MAX_LIMIT}, got {limit}")
    return limit


def checked_cursor(before_message_id: Any) -> Optional[str]:
    if before_message_id is None:
        return None
    if not isinstance(before_message_id, str) or not before_message_id:
        raise HistoryPageError(f"before_message_id must be a record id, got {before_message_id!r}")
    return before_message_id


def record_id(record: Dict[str, Any]) -> Optional[str]:
    return record.get("id") or record.get("message_id")


def _json_size(message: Dict[str, Any]) -> int:
    return len(json.dumps(message))


def page_of(
    records: List[Dict[str, Any]],
    before_message_id: Optional[str],
    limit: int,
    render: Callable[[Dict[str, Any]], Dict[str, Any]] = lambda m: m,
    budget: int = PAGE_BYTES,
) -> Tuple[List[Dict[str, Any]], bool]:
    """The page ending just before `before_message_id` (newest when None) and `has_older`.

    `render` turns a record into what the UI is sent; only the records of the
    page are rendered, and the byte budget is counted on what it returns. A page
    holds at least one message, however large.
    """
    ordered = [m for _, m in sorted(enumerate(records), key=_chronological_key)]
    end = len(ordered)
    if before_message_id is not None:
        end = next((i for i, m in enumerate(ordered) if record_id(m) == before_message_id), -1)
        if end < 0:
            raise HistoryPageError(
                f"before_message_id {before_message_id!r} is not in the live history"
            )
    page: List[Dict[str, Any]] = []
    used = 0
    start = end
    while start > 0 and len(page) < limit:
        rendered = render(ordered[start - 1])
        size = _json_size(rendered) + 2  # the ", " between list items
        if page and used + size > budget:
            break
        page.append(rendered)
        used += size
        start -= 1
    page.reverse()
    return page, start > 0
