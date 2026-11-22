"""ADR-052, Confirmation test 5 (slice S2): llama-server and Strata wait for a busy card lease and
ask an idle one to let go, with the swap on and with it off (Mike #77 B: yielding a lease is not
gated by `swap_when_idle`).

A start whose way is blocked by a lease takes a place in the card queue (section 2.2 item 3); a busy
lease is never marked and never asked, it is waited for and named; an idle one is marked `draining`
before its `on_yield`, and the start goes on when it let go (item 5). The swap is said without
parked words, with the toast title "Card handed over"; `last_swap` names the lease. A lease that
refuses or could not let go is asked again a bounded number of times and then waited for until the
deadline; one that does not let go within the request's stop bound gives a refusal naming it. A
request standing on the lease through one of its open uses is refused at once (item 4). After a
start across a lease the card has one resident, never two (S2 acceptance, Forge 2.2). A person's
Stop engine reaches a lease by its holder name (item 8). Peers (L13): ADR-044's amendment of
2026-11-18 is not built, so an inference peer keeps today's refusal while a lease is busy, words a
foreign node may read; it asks an idle lease to let go only on the owner's terms.

Real supervisors through their real `ensure_running`, faked at their seams as in
`test_one_local_engine_holds_the_card.py`; lease clients from `_card_lease_helpers`.
"""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseBusy, LeaseWouldWaitOnItself
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    CardQueueRefusal,
    card_held_for,
    card_queue,
    serving_a_peer,
)
from dpc_client_core.providers.llamacpp_server_provider import stop_local_engine

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    hold_open,
    holding,
    music,
    nothing_left,
    swap,
    until,
    waiting,
    whisper,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")

PEER = "dpc-node-" + "b" * 32


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(base_mod, "_LEASE_ASK_AGAIN_S", 0.02)
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


def queued_starts():
    return [row["alias"] for row in card_queue() if "until_cancelled" not in row]


def swap_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(("swapped ", "stopped '"))]


async def warm(client):
    """A granted lease with nothing open: loaded and idle."""
    await asyncio.wait_for(client.lease.acquire("preload", wait_s=BOUND), BOUND)
    assert client.lease.state == "running"


class TestAnIdleLeaseIsAskedToLetGo:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("swap_on", [True, False], ids=["swap on", "swap off"])
    async def test_a_llama_server_start_asks_it_and_starts_with_one_resident(self, swap_on, caplog, toasts):
        swap(swap_on, 30)
        w = whisper()
        await warm(w)
        llama = _llama()

        with caplog.at_level(logging.INFO):
            props = await asyncio.wait_for(llama.ensure_running(), BOUND)
            await asyncio.gather(*list(base_mod._SWAP_TOASTS))

        assert props == {"total_slots": 1}
        assert w.yields == [("yield for qwen27", "draining")], "marked draining before on_yield"
        assert not w.lease.granted and not w.loaded
        assert holding() == ["qwen27"], "after a start across a lease the card has one resident"
        assert swap_lines(caplog) == ["swapped 'whisper:turbo' out for 'qwen27'"], "a lease parks nothing"
        last = base_mod.last_swap()
        assert (last["from"], last["to"], last["from_engine"]) == ("whisper:turbo", "qwen27", "whisper")
        assert last["parked_dropped"] is False and last["live_saved"] is None
        assert [(name, p["title"]) for name, p in toasts] == [("notice_toast", "Card handed over")]
        assert "parked" not in toasts[0][1]["message"]
        assert queued_starts() == []
        await nothing_left()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("swap_on", [True, False], ids=["swap on", "swap off"])
    async def test_a_strata_start_asks_it_too(self, tmp_path, swap_on):
        swap(swap_on, 30)
        m = music(waits_for_engines=True)
        await warm(m)
        strata = _strata(tmp_path)

        await asyncio.wait_for(strata.ensure_running(), BOUND)

        assert m.yields == [("yield for flash", "draining")]
        assert holding() == ["flash"]
        # A live Strata supervisor has no `gguf_path` (its install sizes the model): the lease path
        # never needs one.
        assert strata.config.get("gguf_path") is None
        await nothing_left()

    @pytest.mark.asyncio
    async def test_nothing_to_give_releases_and_the_start_goes_on_unsaid(self, caplog):
        w = whisper(outcome="nothing_to_give")
        await warm(w)
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert holding() == ["qwen27"]
        assert swap_lines(caplog) == [] and base_mod.last_swap() is None, "nothing was handed over"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_refusal_is_asked_again_and_the_start_waits_meanwhile(self):
        answers = iter(["refused", "could_not_let_go", "let_go"])
        w = whisper(outcome=lambda: next(answers))
        await warm(w)

        await asyncio.wait_for(_llama().ensure_running(), BOUND)

        assert [state for _why, state in w.yields] == ["draining"] * 3
        assert holding() == ["qwen27"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_lease_that_never_lets_go_is_waited_for_and_named_at_the_deadline(self):
        swap(True, 1)
        w = whisper(outcome="could_not_let_go")
        await warm(w)

        with pytest.raises(CardQueueRefusal) as refused:
            await asyncio.wait_for(_llama().ensure_running(), BOUND)

        assert len(w.yields) == base_mod._MAX_SWAP_ROUNDS, "asked a bounded number of times, then waited for"
        assert "'whisper:turbo'" in str(refused.value) and refused.value.holder_alias == "whisper:turbo"
        assert w.lease.granted and holding() == ["whisper:turbo"], "it kept its memory, and its grant"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_one_that_does_not_let_go_within_the_stop_bound_is_named_and_its_yield_goes_on(self):
        swap(True, 1)
        gate = asyncio.Event()
        w = whisper(gate=gate)
        await warm(w)

        with pytest.raises(CardQueueRefusal) as refused:
            await asyncio.wait_for(_llama().ensure_running(), BOUND)

        assert "'whisper:turbo' is already letting go" in str(refused.value)
        assert refused.value.holder_alias == "whisper:turbo"
        assert w.lease.state == "draining", "the mark stays until the yield has ended"
        gate.set()
        await until(lambda: not w.lease.granted)
        await nothing_left()
        assert queued_starts() == []


class TestABusyLeaseIsWaitedFor:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("swap_on", [True, False], ids=["swap on", "swap off"])
    async def test_it_is_never_asked_while_busy_and_the_start_goes_on_when_it_is_idle(self, swap_on):
        swap(swap_on, 30)
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        llama = _llama()

        start = asyncio.create_task(llama.ensure_running())
        await until(lambda: queued_starts() == ["qwen27"])
        await asyncio.sleep(0.05)
        assert w.yields == [] and not start.done() and llama._proc is None
        assert holding() == ["whisper:turbo"]

        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, start), BOUND)
        assert w.yields == [("yield for qwen27", "draining")]
        assert holding() == ["qwen27"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_at_the_deadline_the_refusal_names_the_lease_and_what_it_is_busy_with(self):
        swap(True, 1)
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        started = time.monotonic()
        with pytest.raises(CardQueueRefusal) as refused:
            await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert time.monotonic() - started >= 0.9

        message = str(refused.value)
        assert "whisper alias 'whisper:turbo'" in message and "In use by dictation." in message
        assert refused.value.holder_alias == "whisper:turbo" and refused.value.holder_engine == "whisper"
        assert w.yields == [] and holding() == ["whisper:turbo"]
        release.set()
        await asyncio.wait_for(dictation, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_new_use_does_not_jump_the_waiting_start(self):
        """L5 with a supervisor's place: while a start waits for the lease no new use is admitted;
        it waits behind the place, and then for the engine the start brought up."""
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation 1", release))
        await until(lambda: w.lease._uses)
        start = asyncio.create_task(_llama().ensure_running())
        await until(lambda: queued_starts() == ["qwen27"])

        second = asyncio.create_task(hold_open(w.lease, "dictation 2", asyncio.Event(), wait_s=None))
        await until(lambda: waiting() == ["whisper:turbo"])
        assert [u.what for u in w.lease._uses] == ["dictation 1"]

        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, start), BOUND)
        assert holding() == ["qwen27"] and waiting() == ["whisper:turbo"], "the use waits for the engine now"
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_request_that_uses_the_lease_itself_is_refused_at_once(self):
        w = whisper()
        async with card_held_for(None, "accepted_request", agent_id="agent", conversation_id="chat"):
            async with w.lease.use("agent 'agent' tool transcribe_audio_file", wait_s=BOUND):
                started = time.monotonic()
                with pytest.raises(LeaseWouldWaitOnItself) as refused:
                    await asyncio.wait_for(_llama().ensure_running(), BOUND)
                assert time.monotonic() - started < 0.5
        assert refused.value.holder_alias == "whisper:turbo"
        assert "transcribe_audio_file" in str(refused.value)
        assert queued_starts() == [] and w.yields == []
        await nothing_left()


class TestAPersonsStopEngineOnALease:
    @pytest.mark.asyncio
    async def test_an_idle_lease_lets_go_a_busy_one_says_why_and_a_refusal_is_not_no_child(self):
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        with pytest.raises(LeaseBusy, match="busy with dictation"):
            await stop_local_engine("whisper:turbo")
        release.set()
        await asyncio.wait_for(dictation, BOUND)

        assert await asyncio.wait_for(stop_local_engine("whisper:turbo"), BOUND) is True
        assert not w.lease.granted and w.yields == [("yield for Stop by hand", "draining")]
        assert await stop_local_engine("whisper:turbo") is False, "a lease not on the card: nothing to stop"

        w.outcome = "refused"
        await warm(w)
        with pytest.raises(CardHeldError, match="did not let go of the card"):
            await asyncio.wait_for(stop_local_engine("whisper:turbo"), BOUND)
        assert w.lease.granted
        await nothing_left()


class TestPeersKeepTodaysRefusalWhileTheAmendmentIsNotBuilt:
    @pytest.mark.asyncio
    async def test_a_peers_start_is_refused_at_once_by_a_busy_lease_in_words_for_a_foreign_node(self):
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        started = time.monotonic()
        with serving_a_peer(PEER):
            with pytest.raises(CardQueueRefusal) as refused:
                await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert time.monotonic() - started < 0.5
        message = str(refused.value)
        assert "right now" in message and "whisper" not in message.lower() and "dictation" not in message
        assert w.yields == [] and queued_starts() == []
        release.set()
        await asyncio.wait_for(dictation, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_idle_lease_lets_go_for_a_peer_on_the_owners_terms_only(self):
        swap(False, 30)
        w = whisper()
        await warm(w)
        with serving_a_peer(PEER):
            with pytest.raises(CardHeldError) as refused:
                await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert "whisper" not in str(refused.value).lower() and w.yields == [], "swap off: today's refusal"

        swap(True, 30)
        with serving_a_peer(PEER):
            await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert w.yields == [("yield for qwen27", "draining")] and holding() == ["qwen27"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_peer_does_not_go_ahead_of_an_owners_lease_waiter(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])

        with serving_a_peer(PEER):
            with pytest.raises(CardQueueRefusal, match="waiting for the card"):
                await asyncio.wait_for(_llama().ensure_running(), BOUND)
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, job), BOUND)
        assert holding() == ["music:yue2"]
        m.lease.release("done")
        await nothing_left()
