"""The small CPU work that counts as contention for the CPU twin of Whisper (ADR-052 L12 point 3).

Mike, 2026-11-24: only small CPU work counts, the CPU transcriptions (the twin or an alias with
`device: cpu`, counted by the Whisper provider), the embedding model and GLiNER; a heavy engine kept off
the card is not counted, nobody would run it there. The embedding model and GLiNER have no busy signal of
their own (a call into a library, from a worker thread), so each wraps its compute call in
`computing_on_the_cpu`, and the card lease's `_cpu_contention` reads `computing_now`. A call that runs on
a GPU is not CPU work and passes `on_cpu=False`; the wrapper then does nothing.

Thread-safe (the callers are worker threads), stdlib only, the same on Windows, Linux and macOS.
"""

import contextlib
import itertools
import threading
from typing import Dict, Iterator, List

_lock = threading.Lock()
_running: Dict[int, str] = {}
_tokens = itertools.count(1)


@contextlib.contextmanager
def computing_on_the_cpu(name: str, *, on_cpu: bool = True) -> Iterator[None]:
    """Count `name` as computing on the CPU for the body, however it ends. `on_cpu` False counts nothing."""
    if not on_cpu:
        yield
        return
    with _lock:
        token = next(_tokens)
        _running[token] = name
    try:
        yield
    finally:
        with _lock:
            _running.pop(token, None)


def computing_now() -> List[str]:
    """The names computing on the CPU now, one per call, in the order they began."""
    with _lock:
        return [_running[token] for token in sorted(_running)]
