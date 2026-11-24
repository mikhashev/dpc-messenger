"""Follow-up 11C (Ark #24-1, Forge #26 F3): a start by hand that waits for idle holders to let go of the card
is no longer a silent await, and the wait of whoever stands behind an idle music engine has a bound.

- A start by hand (`start_music_engine` -> `_card_for_a_start(yields_idle=True)`) waits up to `swap_wait_s`
  (1800 s on Mike's machine). It passes `on_wait`, so the engine's status carries `card_wait`
  (`{text, behind, wait_s, known, upper_bound}`) while it waits, is broadcast as `music_engine_status`,
  and is cleared the moment the grant (or the refusal) ends the wait.
- A music engine's yield (a stop: the signal, `STOP_GRACE_S`, the kill, `STOP_GRACE_S` again) has no
  measured figure, but a bound: `yield_bound_s = 2 * STOP_GRACE_S`. The estimate counts it and says it
  is an upper bound (`upper_bound`); a cheap holder's one second is not one.
- A start that is not by hand shows no wait (it never waits).
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.music import commands
from dpc_client_core.music.engine_base import STOP_GRACE_S, card_wait_words_for_a_start
from tests._acestep_helpers import FakeAceStepServer
from tests._card_lease_helpers import BOUND, fast, music, nothing_left, until, whisper  # noqa: F401
from tests._music_job_commands_helpers import Service, without_tools
from tests._yue2_helpers import FakeYue2Server, install_both, yue2_env
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "card")


def _row(alias):
    return next(row for row in base_mod.card_queue() if row["alias"] == alias)


def _service(home, monkeypatch):
    without_tools(monkeypatch)
    monkeypatch.setattr(commands, "AceStepServer", FakeAceStepServer)
    monkeypatch.setattr(commands, "Yue2Server", FakeYue2Server)
    service = Service(home)
    service.settings.get = lambda section, key, fallback=None: "0" if key in ("acestep_port", "yue2_port") else fallback
    events = []
    service._music_broadcast = lambda event, payload: events.append((event, payload))
    return service, events


@pytest.fixture
def service(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    return _service(home, monkeypatch)


def _waits(events):
    """The `card_wait` of each `music_engine_status` event, in order."""
    return [payload["engine"]["card_wait"] for event, payload in events if event == "music_engine_status"]


def test_a_music_engines_yield_is_bounded_by_two_stop_graces_and_the_words_say_at_most():
    assert STOP_GRACE_S == 15.0
    words = card_wait_words_for_a_start("yue2.cpp", {
        "wait_s": 30.0, "known": True, "behind": ["acestep.cpp"], "upper_bound": True})
    assert words == "yue2.cpp is waiting for the card: acestep.cpp is letting go, at most about 30 s"
    assert card_wait_words_for_a_start("yue2.cpp", {"wait_s": 1.0, "known": True, "behind": ["Whisper (turbo)"]}) \
        == "yue2.cpp is waiting for the card: Whisper (turbo) is letting go, about 1 s"
    assert card_wait_words_for_a_start("yue2.cpp", {"wait_s": 400.0, "known": True, "behind": ["x"]}) \
        .endswith("about 7 min")
    assert card_wait_words_for_a_start("yue2.cpp", {"wait_s": None, "known": False, "behind": []}) == \
        "yue2.cpp is waiting for the card: another job on the card is letting go, time unknown"


@pytest.mark.asyncio
async def test_the_estimate_behind_an_idle_music_engine_is_its_stop_bound_and_says_so(service):
    svc, _events = service
    engine = svc._music_engine("acestep")
    assert engine.card_lease.yield_bound_s == 2 * STOP_GRACE_S == 30.0
    assert engine.card_lease.yield_cost == "engine"
    gate = asyncio.Event()
    idle_music, contender = music("acestep", gate=gate), whisper()
    idle_music.lease.yield_bound_s = 2 * STOP_GRACE_S   # what the engine's own lease declares
    await idle_music.lease.acquire("preload", wait_s=BOUND)
    asking = asyncio.create_task(contender.lease.acquire("dictation", wait_s=BOUND))
    await until(lambda: idle_music.yields)
    assert _row("whisper:turbo")["wait"] == {
        "wait_s": 30.0, "known": True, "behind": ["acestep.cpp"], "where": "yielding", "upper_bound": True}
    gate.set()
    await asyncio.wait_for(asking, BOUND)
    contender.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_music_lease_with_no_bound_keeps_an_unknown_yield():
    gate = asyncio.Event()
    idle_music, contender = music(gate=gate), whisper()
    await idle_music.lease.acquire("preload", wait_s=BOUND)
    asking = asyncio.create_task(contender.lease.acquire("dictation", wait_s=BOUND))
    await until(lambda: idle_music.yields)
    wait = _row("whisper:turbo")["wait"]
    assert wait["wait_s"] is None and wait["known"] is False and "upper_bound" not in wait
    gate.set()
    await asyncio.wait_for(asking, BOUND)
    contender.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_start_by_hand_that_waits_for_a_whisper_model_shows_its_wait_and_clears_it(service):
    svc, events = service
    gate = asyncio.Event()
    warm = whisper(gate=gate)
    await warm.lease.acquire("dictation", wait_s=BOUND)   # a warm grant: idle, with no queue behind it
    try:
        starting = asyncio.create_task(svc.start_music_engine())
        await until(lambda: warm.yields)
        await until(lambda: any(_waits(events)))
        yue2 = svc._music_engine("yue2")
        shown = yue2.details()["card_wait"]
        assert shown["behind"] == ["Whisper (turbo)"] and shown["known"] is True and shown["wait_s"] == 1.0
        assert shown["upper_bound"] is False
        assert shown["text"] == "yue2.cpp is waiting for the card: Whisper (turbo) is letting go, about 1 s"
        assert yue2.details()["state"] == "stopped", "no child yet: the state is not touched by the wait"
        assert not starting.done(), "the click is still waiting for the holder"

        gate.set()
        started = await asyncio.wait_for(starting, 30)
        assert started["status"] == "ok" and started["engine"]["card_wait"] is None
        waits = _waits(events)
        assert waits[0]["text"] == shown["text"], "the first event of the start is the wait"
        # Every later event of this start carries the key, empty: the UI merges events, so a missing key
        # would leave the row up.
        assert waits[-1] is None and yue2.details()["card_wait"] is None
        assert all("card_wait" in payload["engine"] for event, payload in events if event == "music_engine_status")
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()


@pytest.mark.asyncio
async def test_a_start_by_hand_behind_the_other_music_engine_says_at_most_thirty_seconds(service):
    svc, events = service
    try:
        assert (await svc.start_music_engine(engine="acestep"))["status"] == "ok"
        ace = svc._music_engine("acestep")
        gate = asyncio.Event()
        real_yield = ace.card_lease._on_yield

        async def slow_yield(why):
            await gate.wait()
            return await real_yield(why)
        ace.card_lease._on_yield = slow_yield
        del events[:]
        starting = asyncio.create_task(svc.start_music_engine(engine="yue2"))
        await until(lambda: any(_waits(events)))
        shown = next(w for w in _waits(events) if w)
        assert shown["behind"] == ["acestep.cpp"] and shown["wait_s"] == 30.0 and shown["upper_bound"] is True
        assert shown["text"].endswith("acestep.cpp is letting go, at most about 30 s"), shown["text"]
        gate.set()
        started = await asyncio.wait_for(starting, 30)
        assert started["status"] == "ok" and _waits(events)[-1] is None
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()


@pytest.mark.asyncio
async def test_a_start_on_a_free_card_shows_no_wait_and_a_refused_start_clears_none_it_never_set(service):
    svc, events = service
    try:
        started = await asyncio.wait_for(svc.start_music_engine(), 30)
        assert started["status"] == "ok"
        assert not any(_waits(events)), "a free card: nothing to show"
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()


@pytest.mark.asyncio
async def test_a_start_by_hand_that_is_cancelled_while_it_waits_leaves_no_wait_row(service):
    svc, events = service
    gate = asyncio.Event()
    warm = whisper(gate=gate)
    await warm.lease.acquire("dictation", wait_s=BOUND)
    try:
        starting = asyncio.create_task(svc.start_music_engine())
        await until(lambda: any(_waits(events)))
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert svc._music_engine("yue2").details()["card_wait"] is None
        assert _waits(events)[-1] is None
    finally:
        gate.set()
        warm.lease.release("test")
        await svc.stop_music_jobs()
        await svc.stop_music_library()
    await nothing_left()


def test_the_lease_refuses_a_bound_that_is_not_a_positive_number():
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            card_lease.CardLease("x:bad", kind="music", engine="x", display="x", vram_mib=None,
                                 yield_cost="engine", on_yield=lambda why: None, yield_bound_s=bad)
