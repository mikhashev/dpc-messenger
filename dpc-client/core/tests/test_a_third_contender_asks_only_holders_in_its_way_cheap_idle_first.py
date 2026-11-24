"""ADR-052, Confirmation test 21 (Zcode #52, Ark #53; L11 "A third contender while two hold"; section
2.2 item 11). It needs S4's music lease, so it lands with S4.

Whisper holds the card beside a busy YuE2 job (L11's one shared cell, the reading faked to "fits"),
then a llama-server start comes: it meets two holders. It asks only holders in its way, the idle cheap
one first: the warm Whisper is asked to let go (an unload) at once, the busy music job is waited for
and never asked while busy, and once the job ends the idle engine is asked in turn, and the start goes
on with one resident. A holder that is not in the contender's way (a measured share for that
contender, faked through the supervisor's `_excludes`) is never asked.

Real supervisors faked at their seams (`test_one_local_engine_holds_the_card.py`), the real yue2.cpp
supervisor's lease (no child: its job's use is held open by hand), the real Whisper provider.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, swap, until  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of
from tests._yue2_helpers import make_yue2
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick", "card")


def fits(monkeypatch):
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (30000, 32623))


async def busy_music(tmp_path):
    """The yue2.cpp lease granted and busy with a job, as the runner holds it; asks are recorded."""
    engine = make_yue2(tmp_path / "dpc")
    asked = []
    real = engine._yield_card

    async def on_yield(why):
        asked.append((why, engine.busy))
        return await real(why)

    engine.card_lease.rebind(on_yield)
    over = asyncio.Event()

    async def job():
        async with engine.in_use("music job J", wait_s=BOUND):
            await over.wait()

    task = asyncio.create_task(job())
    await until(lambda: engine.busy and engine.card_lease.granted)
    return engine, asked, over, task


async def whisper_beside(monkeypatch):
    p = gpu_whisper(monkeypatch)
    assert await asyncio.wait_for(p.transcribe("d", purpose="dictation", wait_s=BOUND), BOUND) == {"text": "d"}
    assert lease_of(p).beside == "music:yue2" and lease_of(p).state == "running", "warm, idle, beside"
    return p


@pytest.mark.asyncio
async def test_the_idle_whisper_is_asked_first_and_the_busy_job_is_waited_for(tmp_path, monkeypatch):
    fits(monkeypatch)
    swap(True, 30)
    engine, asked, over, job = await busy_music(tmp_path)
    p = await whisper_beside(monkeypatch)
    assert set(holding()) == {"music:yue2", "whisper:turbo"}

    llama = _llama()
    start = asyncio.create_task(llama.ensure_running())
    await until(lambda: not lease_of(p).granted)
    assert p.unloads == 1, "the idle cheap holder let go first"
    await asyncio.sleep(0.1)
    assert asked == [], "the busy music job is waited for, never asked"
    assert not start.done() and holding() == ["music:yue2"]

    over.set()  # the job ends; its engine is idle (no child here: nothing to give)
    await asyncio.wait_for(job, BOUND)
    await asyncio.wait_for(start, BOUND)
    assert asked == [("yield for qwen27", False)]
    assert holding() == ["qwen27"], "one resident after the start"
    await nothing_left()


@pytest.mark.asyncio
async def test_a_holder_not_in_the_contenders_way_is_never_asked(tmp_path, monkeypatch):
    fits(monkeypatch)
    swap(True, 30)
    engine, asked, over, job = await busy_music(tmp_path)
    p = await whisper_beside(monkeypatch)

    llama = _llama()
    real_excludes = llama._excludes
    # A measured share for this contender, faked: Whisper is not in its way.
    llama._excludes = lambda other: False if getattr(other, "alias", "") == "whisper:turbo" else real_excludes(other)
    start = asyncio.create_task(llama.ensure_running())
    await asyncio.sleep(0.2)
    assert p.unloads == 0 and lease_of(p).granted, "never asked"
    assert not start.done()

    over.set()
    await asyncio.wait_for(job, BOUND)
    await asyncio.wait_for(start, BOUND)
    assert asked == [("yield for qwen27", False)] and p.unloads == 0
    assert set(holding()) == {"qwen27", "whisper:turbo"}
    lease_of(p).release("test")
    await nothing_left()
