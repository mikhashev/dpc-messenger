"""ADR-052 follow-up 9b (Mike, 2026-11-24: "everything idle with no queue may be unloaded"; Zcode's review
of S4, point 3): a music engine started by hand takes the card from whatever is idle on it, on the terms a
queued music job gets from the lease, and refuses (`card_busy`, naming the holder) only when a holder is
busy or has a queue behind it.

- An idle Whisper model is let go of (its lease's yield) and the engine starts.
- An idle Strata is swapped out when swapping is on, and the start is refused when it is off.
- A busy holder refuses at once, nothing asked.
- The other music engine is stopped only after the card is granted: a refused start has stopped nothing.

Real music servers over `_fake_yue2_server.py` and `_fake_acestep_server.py`, a real Strata supervisor faked
at its seams, fake Whisper leases (`_card_lease_helpers.py`).
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.music import commands
from tests._acestep_helpers import FakeAceStepServer
from tests._card_lease_helpers import BOUND, fast, holding, hold_open, nothing_left, swap, until, whisper  # noqa: F401
from tests._music_job_commands_helpers import Service, without_tools
from tests._yue2_helpers import FakeYue2Server, install_both, yue2_env
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "card")


def service_at(home, monkeypatch):
    without_tools(monkeypatch)
    monkeypatch.setattr(commands, "AceStepServer", FakeAceStepServer)
    monkeypatch.setattr(commands, "Yue2Server", FakeYue2Server)
    service = Service(home)
    service.settings.get = lambda section, key, fallback=None: "0" if key in ("acestep_port", "yue2_port") else fallback
    return service


@pytest.fixture
def service(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    return service_at(home, monkeypatch)


async def finish(service):
    await service.stop_music_jobs()
    await service.stop_music_library()


@pytest.mark.asyncio
async def test_an_idle_whisper_model_is_unloaded_for_a_start_by_hand(service):
    try:
        warm = whisper()
        await warm.lease.acquire("dictation", wait_s=BOUND)  # a warm grant: the model loaded, no call open
        started = await asyncio.wait_for(service.start_music_engine(), 30)
        assert started["status"] == "ok", started
        assert started["engine"]["state"] == "running"
        assert warm.yields and not warm.loaded, "Whisper was asked to let go and unloaded"
        assert holding() == ["music:yue2"]
    finally:
        await finish(service)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_strata_is_swapped_out_for_a_start_by_hand_when_swapping_is_on(service, tmp_path):
    try:
        strata = _strata(tmp_path)
        await asyncio.wait_for(strata.ensure_running(), BOUND)
        assert holding() == ["flash"]
        started = await asyncio.wait_for(service.start_music_engine(), 30)
        assert started["status"] == "ok", started
        assert holding() == ["music:yue2"]
        assert strata._proc is None, "Strata's child was stopped by the swap"
    finally:
        await finish(service)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_strata_refuses_a_start_by_hand_when_swapping_is_off(service, tmp_path):
    swap(False)
    try:
        strata = _strata(tmp_path)
        await asyncio.wait_for(strata.ensure_running(), BOUND)
        began = time.monotonic()
        refused = await asyncio.wait_for(service.start_music_engine(), BOUND)
        assert time.monotonic() - began < 1.0, "refused at once, not after a wait"
        assert refused["status"] == "error" and refused["code"] == "card_busy"
        assert "flash" in refused["message"]
        assert strata._proc is not None and holding() == ["flash"], "nothing was stopped"
    finally:
        await finish(service)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_busy_whisper_refuses_a_start_by_hand_and_the_idle_music_engine_keeps_running(service):
    try:
        assert (await service.start_music_engine(engine="acestep"))["status"] == "ok"
        ace = service._music_engine("acestep")
        assert ace.alive()
        # Whisper is on the card beside the idle engine only by force here: the refusal under test is
        # what a busy holder gives, whatever put it there, and the point is that the engine stays.
        busy = whisper()
        over = asyncio.Event()
        ace.card_lease.release("test makes room")
        holding_it = asyncio.create_task(hold_open(busy.lease, "dictation", over))
        await until(lambda: busy.lease.granted)
        began = time.monotonic()
        refused = await asyncio.wait_for(service.start_music_engine(), BOUND)
        assert time.monotonic() - began < 1.0
        assert refused["status"] == "error" and refused["code"] == "card_busy"
        assert "Whisper" in refused["message"] and not busy.yields, "a busy holder is never asked"
        assert ace.alive(), "the other music engine was not stopped by a refused start"
        over.set()
        await asyncio.wait_for(holding_it, BOUND)
    finally:
        await finish(service)


@pytest.mark.asyncio
async def test_a_refused_start_has_not_stopped_the_idle_other_music_engine(service, monkeypatch):
    try:
        assert (await service.start_music_engine(engine="acestep"))["status"] == "ok"
        ace, yue2 = service._music_engine("acestep"), service._music_engine("yue2")

        async def refuse(what, *, wait_s, **kw):
            raise LeaseWaitedTooLong("yue2.cpp waited at once for the card and was not granted it: test", "x", "y")
        monkeypatch.setattr(yue2.card_lease, "acquire", refuse)
        refused = await asyncio.wait_for(service.start_music_engine(), BOUND)
        assert refused["status"] == "error" and refused["code"] == "card_busy"
        assert ace.alive() and ace.card_lease.granted, "the idle engine on the card was left as it was"
    finally:
        await finish(service)


@pytest.mark.asyncio
async def test_the_idle_other_music_engine_is_stopped_once_the_card_is_granted(service):
    try:
        assert (await service.start_music_engine(engine="acestep"))["status"] == "ok"
        started = await asyncio.wait_for(service.start_music_engine(), 30)
        assert started["status"] == "ok", started
        assert not service._music_engine("acestep").alive() and service._music_engine("yue2").alive()
        assert holding() == ["music:yue2"]
    finally:
        await finish(service)


def test_a_refusal_that_waited_for_nobody_does_not_say_it_asked():
    # Zcode's S4 review, point 3: the text said "asking it to let go" though nothing was asked.
    lease = whisper().lease
    waiter = card_lease._LeaseWaiter(lease, "start", 0, False, None)
    _who, why = card_lease._why_it_waits(waiter, "holder", whisper("other").lease, "running", queued=False)
    assert "asking" not in why and "not asked" in why
    _who, why = card_lease._why_it_waits(waiter, "holder", whisper("other").lease, "running")
    assert why == "idle, asking it to let go"
