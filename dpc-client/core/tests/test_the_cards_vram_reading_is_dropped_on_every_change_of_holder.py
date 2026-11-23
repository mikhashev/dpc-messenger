"""ADR-052 L11, "The cache, exactly" (Mike #11, item 13; slice S3e): the 30 s VRAM reading the agent
map shares stays, and a function clears its `at` and `free`, never `total`.

The card lease calls it when the card changes: a grant, a load that finished under the grant
(`mark_running`) and a release, so L11 never decides on a reading older than the last change of
holder. L11 calls it after a reading that came back None, so a failure is not served for 30 s. L11
reads through `commit_memory.read_vram_mib`, never by calling the two nvidia-smi helpers itself.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import llama_server_supervisor as lss
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")


@pytest.fixture(autouse=True)
def fresh_cache():
    cm._vram_reading.update(at=None, wall=None, free=None, total=None)
    yield
    cm._vram_reading.update(at=None, wall=None, free=None, total=None)


class _Card:
    def __init__(self, free=30000, total=32623):
        self.free, self.total = free, total
        self.free_calls = self.total_calls = 0

    def install(self, monkeypatch):
        def free(while_stopping=False):
            self.free_calls += 1
            return self.free

        def total():
            self.total_calls += 1
            return self.total

        monkeypatch.setattr(lss, "_free_vram_mib", free)
        monkeypatch.setattr(lss, "_total_vram_mib", total)
        return self


def _cached(free=1000, total=32623):
    cm._vram_reading.update(at=1.0, wall=2.0, free=free, total=total)


def test_forgetting_clears_at_and_free_and_keeps_total(monkeypatch):
    card = _Card().install(monkeypatch)
    assert cm.read_vram_mib(now=100.0) == (30000, 32623)
    cm.forget_vram_reading()
    assert (cm._vram_reading["at"], cm._vram_reading["wall"], cm._vram_reading["free"]) == (None, None, None)
    assert cm._vram_reading["total"] == 32623
    card.free = 12000
    assert cm.read_vram_mib(now=101.0) == (12000, 32623), "read again inside the window"
    assert (card.free_calls, card.total_calls) == (2, 1), "the total is never asked twice"


@pytest.mark.asyncio
async def test_a_grant_a_finished_load_and_a_release_each_drop_the_reading():
    w = whisper()
    _cached()
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
    assert cm._vram_reading["at"] is None and cm._vram_reading["total"] == 32623
    w.lease.mark_starting()
    _cached()
    w.lease.mark_running()
    assert cm._vram_reading["at"] is None
    _cached()
    w.lease.release("test")
    await until(lambda: not w.lease.granted)
    assert cm._vram_reading["at"] is None and cm._vram_reading["total"] == 32623
    await nothing_left()


@pytest.mark.asyncio
async def test_l11_reads_through_the_agent_maps_reader_and_drops_a_none_reading(monkeypatch):
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", card_lease._card_vram)
    card = _Card(free=None).install(monkeypatch)
    m = music()
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(m.lease, "music job J", over))
    await until(lambda: m.lease.granted)
    w = whisper("turbo", vram_mib=4447)
    w.lease.shares_with = lambda holder: holder.kind == "music"
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.05), BOUND)
    assert card.free_calls == 1, "L11 asked nvidia-smi through read_vram_mib once"
    assert cm._vram_reading["at"] is None, "a None reading is not served for the next 30 s"
    card.free = 30000
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
    assert w.lease.beside == "music:yue2" and card.free_calls == 2
    over.set()
    await asyncio.wait_for(task, BOUND)
    w.lease.release("test")
    m.lease.release("test")
    await nothing_left()
