"""The ids of messages this node has already handled, oldest first.

`CoreService._processed_message_ids` used to be a plain `set`, and each of
three handlers cut `list(the_set)[:half]` when it grew past the cap. A set has
no order, so "half" meant whichever keys hashed first: a key added a moment ago
was as likely to go as the oldest one, and a duplicate that arrived after that
was handled twice. This keeps insertion order and evicts from the front, in one
place.
"""

from collections import OrderedDict
from typing import Hashable, Iterator


class ProcessedMessageIds:
    """A set of ids that remembers the order they were added in and bounds itself.

    Offers the part of the `set` API the handlers use: `add`, `discard`,
    `in`, `len` and iteration. `add` is the only place that evicts: once the
    count passes `max_size`, the oldest half goes.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self.max_size = max_size
        self._ids: "OrderedDict[Hashable, None]" = OrderedDict()

    def add(self, key: Hashable) -> None:
        # A key already present keeps its place: it is the first sighting that
        # ages, as it did in the set this replaced.
        if key not in self._ids:
            self._ids[key] = None
        if len(self._ids) > self.max_size:
            self._evict_oldest_half()

    def discard(self, key: Hashable) -> None:
        self._ids.pop(key, None)

    def _evict_oldest_half(self) -> None:
        for _ in range(len(self._ids) // 2):
            self._ids.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def __iter__(self) -> Iterator[Hashable]:
        return iter(self._ids)
