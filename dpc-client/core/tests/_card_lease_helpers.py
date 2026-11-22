"""Fake card-lease clients for the ADR-052 tests: no GPU, no model, every wait bounded.

A `FakeClient` holds one `CardLease`. Its `on_yield` records each ask with the lease's state at that
moment, waits on an `asyncio.Event` gate when the test gives one, and answers the outcome the test
chose ("let_go" frees its "model"). `swap` binds a fake settings object, so the swap switch and
`swap_wait_s` are what the test says, and `fast` makes the line poll and re-ask quickly.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import CardLease

# Every await in these tests is bounded by this (ADR-052, Confirmation).
BOUND = 5


class FakeClient:
    """One GPU client with a lease. `outcome` is what its yield answers, or a callable returning it;
    `gate`, when set, holds the yield until the test opens it."""

    def __init__(self, holder: str, *, kind: str = "transcription", engine: str = "whisper",
                 display: Optional[str] = None, yield_cost: str = "cheap", outcome: Any = "let_go",
                 gate: Optional[asyncio.Event] = None, eta: Optional[float] = None,
                 vram_mib: Optional[int] = None, stop_hint: str = ""):
        self.outcome = outcome
        self.gate = gate
        self.eta = eta
        self.loaded = True
        self.yields: List[Tuple[str, str]] = []
        self.lease = CardLease(
            holder, kind=kind, engine=engine, display=display or holder, vram_mib=vram_mib,
            yield_cost=yield_cost, on_yield=self.on_yield, eta_s=lambda: self.eta, stop_hint=stop_hint,
        )

    async def on_yield(self, why: str) -> str:
        self.yields.append((why, self.lease.state))
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.outcome() if callable(self.outcome) else self.outcome
        if outcome == "let_go":
            self.loaded = False
        return outcome


def whisper(alias: str = "turbo", **kw: Any) -> FakeClient:
    return FakeClient(f"whisper:{alias}", kind="transcription", engine="whisper",
                      display=f"Whisper ({alias})", yield_cost="cheap", **kw)


def music(key: str = "yue2", **kw: Any) -> FakeClient:
    return FakeClient(f"music:{key}", kind="music", engine=f"{key}.cpp", display=f"{key}.cpp",
                      yield_cost="engine", vram_mib=5600, **kw)


def embedder(**kw: Any) -> FakeClient:
    return FakeClient("embedder:BAAI/bge-m3", kind="embedding", engine="embedder",
                      display="Embedding model (bge-m3)", yield_cost="cheap", **kw)


def swap(on: bool = True, wait_s: int = 300, remote_timeout: int = 1200) -> None:
    base_mod.bind_local_engine_settings(SimpleNamespace(
        get_swap_when_idle=lambda: on, get_swap_wait_s=lambda: wait_s, get_caller_idle_s=lambda: 0,
        get_remote_inference_timeout=lambda: remote_timeout,
    ))


@pytest.fixture
def fast(monkeypatch):
    """The line polls every 5 ms and asks a holder again after 20 ms; nothing is left behind."""
    monkeypatch.setattr(card_lease, "_LEASE_POLL_S", 0.005)
    monkeypatch.setattr(card_lease, "_YIELD_RETRY_S", 0.02)
    swap(True)
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


async def until(condition: Callable[[], Any], bound: float = BOUND) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), bound)


def waiting() -> List[str]:
    """The holder names in the lease line, in order."""
    return [row["alias"] for row in base_mod.card_queue() if "until_cancelled" in row]


def holding() -> List[str]:
    return [row["alias"] for row in base_mod.card_holders()]


async def nothing_left() -> None:
    """No yield still running and nobody in the line (ADR-052 Risks: assert no pending tasks)."""
    await until(lambda: not card_lease._YIELDS)
    assert card_lease._LEASE_LINE == []


async def hold_open(lease: CardLease, what: str, release: asyncio.Event, **kw: Any) -> None:
    """A use that stays open until `release` is set."""
    kw.setdefault("wait_s", BOUND)
    async with lease.use(what, **kw):
        await release.wait()
