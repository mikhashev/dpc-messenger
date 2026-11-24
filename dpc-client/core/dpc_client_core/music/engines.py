"""The music engines DPC supervises, and the rule that only one is on the card at a time
(ADR-051 D35; SPEC §12 decision 40).

`MusicEngines` holds one supervisor per engine word: `yue2` (yue2.cpp, the default, Mike
#198) and `acestep` (acestep.cpp). Each loads several GB onto the GPU (YuE2 Q8 peaks at
5.2-5.6 GB, acestep.cpp at about 9.3 GB), so two side by side would fight for the card.

**Mutual exclusion.** Every start of a music engine goes through `exclusive(key)`, which
holds one switch lock and, before the start, looks at every other music engine:

- one that is **busy** (a job holds it through `in_use()`) or **starting** is never
  stopped. The runner waits for it (`wait=True`, re-checking every `poll_s` and calling
  `check()`, which raises when the person cancels the waiting job); a start by hand
  (`start_music_engine`) is refused with `engine_busy` instead of hanging the command;
- one that is **alive and idle** is stopped (`stop("swap")`, the registry's word for a stop
  made because another start needed the card) and the start goes on.

The runner runs one job at a time and holds its own engine (`in_use`) from before this
switch to the end of the job, so a start by hand of the other engine during a job is
refused, and the idle stop never takes the engine from under a job.

Since ADR-052 S4 each engine also holds the card lease `music:<key>`, and every lease excludes
every other, so the runner's job meets a busy other engine in the lease line first (queued, its
`card_wait` naming it) and this switch then finds nothing to wait for; it stays, and agrees: its
stop of an idle other engine lets that engine's lease go. The other holders of the card (Strata,
llama-server, a Whisper model) are the same lease wait (decision 32, amended by Mike #70), with the
person's "Stop X".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional

from . import job_schemas as js
from .engine_base import MusicEngineError

logger = logging.getLogger(__name__)

DEFAULT_WAIT_POLL_S = 0.5


class MusicEngines:
    """The engine supervisors by word, with the default and the switch lock."""

    def __init__(self, engines: Mapping[str, Any], *, default: str = js.DEFAULT_FROM_TEXT_ENGINE,
                 poll_s: float = DEFAULT_WAIT_POLL_S):
        if not engines:
            raise ValueError("MusicEngines needs at least one engine")
        self._engines: Dict[str, Any] = dict(engines)
        self.default = default if default in self._engines else next(iter(self._engines))
        self.poll_s = poll_s
        self._lock: Optional[asyncio.Lock] = None

    @classmethod
    def of(cls, engines: Any, **kw) -> "MusicEngines":
        """A registry from a registry, a mapping, or one supervisor (by its `KEY`)."""
        if isinstance(engines, MusicEngines):
            return engines
        if isinstance(engines, Mapping):
            return cls(engines, **kw)
        key = getattr(engines, "KEY", None) or js.ENGINE_ACESTEP
        return cls({key: engines}, default=key, **kw)

    def keys(self) -> List[str]:
        return list(self._engines)

    def items(self):
        return self._engines.items()

    def find(self, key: Optional[str]) -> Optional[Any]:
        return self._engines.get(key or self.default)

    def get(self, key: Optional[str] = None) -> Any:
        """The supervisor of `key` (the default when None); `invalid_engine` otherwise."""
        engine = self.find(key)
        if engine is None:
            raise MusicEngineError("invalid_engine", f"engine must be one of {', '.join(self._engines)}, got {key!r}")
        return engine

    def others(self, key: str) -> List[Any]:
        return [e for k, e in self._engines.items() if k != key]

    def busy_other(self, key: str) -> Optional[Any]:
        """Another engine a job holds or that is starting, or None."""
        for e in self.others(key):
            if getattr(e, "busy", False) or getattr(e, "state", None) == "starting":
                return e
        return None

    def _the_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @contextlib.asynccontextmanager
    async def exclusive(self, key: str, *, wait: bool = True,
                        check: Optional[Callable[[], Awaitable[None]]] = None) -> AsyncIterator[Any]:
        """Make the card free of the other music engines for `key`, then hold the switch
        lock while the caller starts it (see the module docstring). Yields the supervisor."""
        engine = self.get(key)
        if not wait:
            # A start by hand never queues behind the runner's switch (which may be waiting
            # for a job on the other engine): it is refused at once.
            other = self.busy_other(key)
            if self._the_lock().locked() or other is not None:
                who = other.DISPLAY if other is not None else "another music engine start"
                raise MusicEngineError(
                    "engine_busy", f"{who} holds the card or is switching engines; only one music engine runs at "
                                   f"a time, so {engine.DISPLAY} cannot start now")
        async with self._the_lock():
            told = None
            while True:
                other = self.busy_other(key)
                if other is None:
                    break
                if not wait:
                    raise MusicEngineError(
                        "engine_busy", f"{other.DISPLAY} is busy with a job (or starting) and holds the card; only "
                                       f"one music engine runs at a time, so {engine.DISPLAY} waits for it")
                if told is not other:
                    logger.info("music: %s waits for %s to finish its job", engine.DISPLAY, other.DISPLAY)
                    told = other
                if check is not None:
                    await check()
                await asyncio.sleep(self.poll_s)
            for other in self.others(key):
                if other.alive():
                    logger.info("music: stopping the idle %s so %s can have the card", other.DISPLAY, engine.DISPLAY)
                    await other.stop("swap")
            yield engine

    async def shutdown(self) -> None:
        """Stop every engine; never raises."""
        for engine in self._engines.values():
            try:
                await engine.shutdown()
            except Exception:
                logger.exception("music: %s did not stop cleanly", getattr(engine, "DISPLAY", engine))

    def statuses(self) -> Dict[str, Any]:
        return {k: e.status() for k, e in self._engines.items()}
