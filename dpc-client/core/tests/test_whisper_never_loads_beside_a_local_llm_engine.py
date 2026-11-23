"""ADR-052, Confirmation test 19, the parts of slice S3b (Mike #68: "no, it must not load beside";
the matrix row "whisper load"; item 1a, decided 2026-11-23).

With Strata on the card a Whisper load never shares it. A busy Strata (a turn in progress) is waited
for, within the door's bound. An idle one, with the swap on and nothing waiting, is swapped out by
the owner-start rule, as for music (`swap_out_for_a_lease`): the live conversation saved first, the
parked count read and said. With the swap off the dictation waits and nothing is stopped. An agent's
own tool inside a turn on Strata is refused at once (`LeaseWouldWaitOnItself`). No "loaded beside"
line is ever written. L12's CPU twin, which takes a file up to 300 s first, is S3d's: until then
every file takes the line.

Real supervisors faked at their seams (`test_one_local_engine_holds_the_card.py`).
"""

import asyncio
import logging
import time

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong, LeaseWouldWaitOnItself
from dpc_client_core.managers.llama_server_supervisor import card_held_for

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, swap, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


def _no_beside_line(caplog):
    assert not [r for r in caplog.records if "beside" in r.getMessage()]


@pytest.mark.asyncio
async def test_an_idle_strata_is_swapped_out_for_a_dictation_with_the_swap_on(tmp_path, monkeypatch, caplog):
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    with caplog.at_level(logging.INFO):
        result = await asyncio.wait_for(p.transcribe("d", purpose="dictation", wait_s=BOUND), BOUND)
    assert result == {"text": "d"}
    assert events == ["parked read", "live saved", "stopped"], "the owner-start rule, as for music"
    assert strata._proc is None and holding() == ["whisper:turbo"]
    last = base_mod.last_swap()
    assert (last["from"], last["to"], last["to_engine"]) == ("flash", "whisper:turbo", "whisper")
    _no_beside_line(caplog)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_busy_strata_is_waited_for_and_never_shared(tmp_path, monkeypatch, caplog):
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    turn_over = asyncio.Event()

    async def turn():
        async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
            await turn_over.wait()

    on_strata = asyncio.create_task(turn())
    await until(lambda: base_mod._CARD_HOLDS.get("flash"))
    with caplog.at_level(logging.INFO):
        dictation = asyncio.create_task(p.transcribe("d", purpose="dictation", wait_s=BOUND))
        await until(lambda: waiting() == ["whisper:turbo"])
        await asyncio.sleep(0.05)
        assert p.loads == 0 and events == [] and holding() == ["flash"]
        turn_over.set()
        assert (await asyncio.wait_for(dictation, BOUND)) == {"text": "d"}
    await asyncio.wait_for(on_strata, BOUND)
    assert events == ["parked read", "live saved", "stopped"] and holding() == ["whisper:turbo"]
    _no_beside_line(caplog)
    await nothing_left()


@pytest.mark.asyncio
async def test_with_the_swap_off_the_dictation_waits_and_is_refused_at_its_bound(tmp_path, monkeypatch):
    swap(False)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    with pytest.raises(LeaseWaitedTooLong) as refused:
        await asyncio.wait_for(p.transcribe("d", purpose="dictation", wait_s=0.2), BOUND)
    assert "'flash'" in str(refused.value)
    assert events == [] and p.loads == 0 and holding() == ["flash"]
    assert not lease_of(p).granted
    await nothing_left()


@pytest.mark.asyncio
async def test_an_agents_tool_inside_its_own_turn_on_strata_is_refused_at_once(tmp_path, monkeypatch):
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    started = time.monotonic()
    async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
        with pytest.raises(LeaseWouldWaitOnItself) as refused:
            await asyncio.wait_for(p.transcribe("f", purpose="agent tool", wait_s=1790.0), BOUND)
    assert time.monotonic() - started < 0.5
    assert "'flash'" in str(refused.value)
    assert p.loads == 0 and events == [] and waiting() == []
    await nothing_left()
