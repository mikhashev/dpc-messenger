"""ADR-052, Confirmation test 39 (slice S2; section 2.4): a card lease that swaps Strata out reads the
parked count before the stop and says what it costs.

Two parked conversations: the count is read before the stop; the INFO line and the toast say the
parked conversations are dropped, and `last_swap.parked_dropped` is true. A count of 0: not said. A
count that cannot be read within the launcher's 3 s: said as if some went. A lease that yields
(Whisper, music) parks nothing, so a start that takes the card from it says no parked words. With
the swap off nothing is stopped.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests._card_lease_helpers import BOUND, fast, holding, music, nothing_left, swap, until, waiting, whisper  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")

DROPPED = "; its parked conversations are dropped"


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    yield
    base_mod.bind_model_download_service(None)


@pytest.fixture
def toasts():
    sent = []

    class Api:
        async def broadcast_event(self, name, payload):
            sent.append((name, payload))

    base_mod.bind_model_download_service(SimpleNamespace(local_api=Api()))
    return sent


async def strata_with(tmp_path, parked):
    """A running Strata whose launcher reports `parked` (a number), or never answers (`None`)."""
    strata = _strata(tmp_path)
    await strata.ensure_running()
    events = []
    real_get, real_stop = strata._get, strata._stop_child

    async def get(path):
        if path == "/metrics":
            events.append("parked read")
            if parked is None:
                raise asyncio.TimeoutError("the launcher did not answer")
            return {"conversation_cache": {"slots": 2, "parked": parked, "bytes": 1}}
        return await real_get(path)

    async def stop_child():
        events.append("stopped")
        await real_stop()

    strata._get = get
    strata._stop_child = stop_child
    return strata, events


async def music_takes_the_card(caplog):
    m = music(waits_for_engines=True)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(m.lease.acquire("music job J", wait_s=None), BOUND)
        await asyncio.gather(*list(base_mod._SWAP_TOASTS))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]
    m.lease.release("job done")
    return lines


class TestTheParkedCount:
    @pytest.mark.asyncio
    async def test_two_parked_are_read_before_the_stop_and_said_dropped(self, tmp_path, caplog, toasts):
        _strata_sup, events = await strata_with(tmp_path, 2)
        lines = await music_takes_the_card(caplog)

        assert events == ["parked read", "stopped"]
        assert lines == [f"swapped 'flash' out for 'music:yue2'{DROPPED}"]
        assert [(name, p["title"]) for name, p in toasts] == [("notice_toast", "Local engine swapped")]
        assert toasts[0][1]["message"].endswith(DROPPED)
        assert base_mod.last_swap()["parked_dropped"] is True
        await nothing_left()

    @pytest.mark.asyncio
    async def test_none_parked_is_not_said(self, tmp_path, caplog, toasts):
        _strata_sup, events = await strata_with(tmp_path, 0)
        lines = await music_takes_the_card(caplog)

        assert events == ["parked read", "stopped"]
        assert lines == ["swapped 'flash' out for 'music:yue2'"]
        assert "parked" not in toasts[0][1]["message"]
        assert base_mod.last_swap()["parked_dropped"] is False
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_count_that_cannot_be_read_is_said_as_if_some_went(self, tmp_path, caplog):
        _strata_sup, events = await strata_with(tmp_path, None)
        lines = await music_takes_the_card(caplog)

        assert events == ["parked read", "stopped"]
        assert lines == [f"swapped 'flash' out for 'music:yue2'{DROPPED}"]
        assert base_mod.last_swap()["parked_dropped"] is True
        await nothing_left()

    @pytest.mark.asyncio
    async def test_with_the_swap_off_nothing_is_stopped_or_read(self, tmp_path):
        swap(False)
        strata, events = await strata_with(tmp_path, 2)
        m = music(waits_for_engines=True)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.05)
        assert events == [] and holding() == ["flash"] and base_mod.last_swap() is None
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await nothing_left()


class TestALeaseThatYieldsParksNothing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("client", [whisper, music], ids=["whisper", "music"])
    async def test_a_start_that_takes_the_card_from_a_lease_says_no_parked_words(self, tmp_path, client, caplog,
                                                                                 toasts):
        c = client()
        await c.lease.acquire("preload", wait_s=BOUND)
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(_strata(tmp_path).ensure_running(), BOUND)
            await asyncio.gather(*list(base_mod._SWAP_TOASTS))

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]
        assert lines == [f"swapped '{c.lease.alias}' out for 'flash'"]
        assert base_mod.last_swap()["parked_dropped"] is False
        assert [(name, p["title"]) for name, p in toasts] == [("notice_toast", "Card handed over")]
        await nothing_left()
