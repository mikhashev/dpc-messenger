"""Requests wanting the card wait in one line, and nothing is forced (ADR-044, amendment
2026-11-02, the card queue: rules 3, 4, 5, 7, 8, 12, 13 and 15, and the three decisions that
closed what the rules left open).

Real supervisors and their real `ensure_running`, with the fakes of
`test_one_local_engine_holds_the_card.py`; real doors (`P2PCoordinator.handle_inference_request`,
`Gateway.complete`) and a real agent loop where the rule is about one. `_swap_clock` is faked
where a test says a time, so that a total time is asserted and not slept.
"""

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.run_gate import RunGate, get_run_gate, turn_waits_for
from dpc_client_core.gateway import GatewayError
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    LlamaServerError,
    LlamaServerSupervisor,
    card_held_for,
    card_holders,
    serving_a_peer,
)
from dpc_client_core.p2p_coordinator import _engine_unavailable_cause
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS, stop_local_engine
from dpc_client_core.settings import Settings

from tests.test_a_callers_run_of_calls_keeps_its_alias_busy_for_the_window import (
    PEER,
    _Clock,
    _gateway,
    _Looks,
    _pair,
    _peer_call,
    _peer_door,
    _Served,
)
from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    F,
    S,
    _agent,
    _alive,
    _another_request,
    _Engine,
    _finish,
    _held,
    _Manager,
    _untouched,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401
from tests.test_strata_is_offered_to_peers_on_the_owners_terms import _strata as _strata_provider

G = "the third alias"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", fake)
    return fake


@pytest.fixture
def swaps(monkeypatch):
    """Every swap said, as (from, to), in order."""
    said = []
    real = LlamaServerSupervisor._note_swap

    def noting(self, holder, *args, **kwargs):
        said.append((holder.alias, self.alias))
        real(self, holder, *args, **kwargs)

    monkeypatch.setattr(LlamaServerSupervisor, "_note_swap", noting)
    return said


@pytest.fixture(autouse=True)
def swap_on(settings, tmp_path, monkeypatch):
    settings.set_local_engine_swap(True, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


def _three(tmp_path):
    f, s, g = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S), _strata(tmp_path / "g", G)
    _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S], _ACTIVE_SUPERVISORS[G] = f, s, g
    return f, s, g


async def _request_on(sup, until=None, **who):
    """A request as a door makes one: accepted, standing on its alias, then asking for it."""
    async with card_held_for(sup.alias, "accepted_request", **who):
        props = await sup.ensure_running()
        if until is not None:
            await until.wait()
        return props


def _request(sup, until=None, **who) -> asyncio.Task:
    return _another_request(_request_on(sup, until, **who))


async def _until(predicate, timeout=5.0):
    async def looking():
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(looking(), timeout)


def _in_line() -> list:
    return [place.alias for place in base_mod._CARD_QUEUE]


def _claim_order(*sups) -> list:
    """The aliases as each first claims the card. A Strata `_launch` claims again; said once."""
    order = []

    for sup in sups:
        real = sup._claim_card

        def claiming(sup=sup, real=real):
            real()
            if not order or order[-1] != sup.alias:
                order.append(sup.alias)

        sup._claim_card = claiming
    return order


def _slow_stop(sup):
    """Hold `sup`'s stop until the test lets it go; `reached` says it got there."""
    reached, let_go = asyncio.Event(), asyncio.Event()
    real = sup._stop_child

    async def stopping():
        reached.set()
        await let_go.wait()
        await real()

    sup._stop_child = stopping
    return reached, let_go


class TestTheOrder:
    @pytest.mark.asyncio
    async def test_three_starts_race_and_are_served_in_the_order_they_came(self, tmp_path, swaps):
        f, s, g = _three(tmp_path)
        h = _strata(tmp_path / "h", "the fourth alias")
        _ACTIVE_SUPERVISORS[h.alias] = h
        await f.ensure_running()
        slot = f.call_slot()
        order = _claim_order(s, g, h)
        # The oldest is the slowest to reach the card: without a line the others pass it.
        real_clear = s._clear_unfinished_start

        async def slow_clear():
            await asyncio.sleep(0.08)
            await real_clear()

        s._clear_unfinished_start = slow_clear

        starts = [_another_request(x.ensure_running()) for x in (s, g, h)]
        try:
            await asyncio.sleep(0.02)
            async with slot:
                pass
            await asyncio.wait_for(asyncio.gather(*starts), 15)
        finally:
            await _finish(*starts)

        assert order == [S, G, h.alias]
        assert swaps == [(F, S), (S, G), (G, h.alias)]
        assert [x["alias"] for x in card_holders()] == [h.alias]

    @pytest.mark.asyncio
    async def test_a_request_accepted_after_a_place_was_taken_waits_behind_it_on_the_holders_own_alias(
        self, tmp_path, swaps,
    ):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        end_first = asyncio.Event()
        first = _request(f, end_first, conversation_id="first")
        await _until(lambda: _held() == {F: 1})
        start = _request(s)
        await _until(lambda: _in_line() == [S])

        later = _request(f)
        await _until(lambda: _in_line() == [S, F])
        await asyncio.sleep(0.03)
        try:
            assert not later.done(), "served ahead of the place it arrived behind"
            assert _held() == {F: 2, S: 1}
            _untouched(f)

            end_first.set()
            await asyncio.wait_for(start, 10)
            assert swaps[0] == (F, S), "the later request does not extend the holder's busy"

            await asyncio.wait_for(later, 10)
            assert swaps == [(F, S), (S, F)]
            assert _alive(f) and not _alive(s)
        finally:
            end_first.set()
            await _finish(first, start, later)

    @pytest.mark.asyncio
    async def test_what_stood_there_when_the_start_queued_is_served_and_is_waited_for(self, tmp_path, swaps):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        async with card_held_for(F, "accepted_request", conversation_id="running"):
            start = _request(s)
            await _until(lambda: _in_line() == [S])

            # A later round of the running request: not new.
            assert await asyncio.wait_for(f.ensure_running(), 2) == {"total_slots": 1}
            async with f.call_slot():
                pass
            await asyncio.sleep(0.03)
            assert not start.done() and swaps == []
        await asyncio.wait_for(start, 10)
        assert swaps == [(F, S)]

    @pytest.mark.asyncio
    async def test_a_request_older_than_the_place_is_served_on_an_alias_still_starting(self, tmp_path):
        # Otherwise it waits behind a place that waits for its hold: a wait on itself.
        f, s, _g = _three(tmp_path)
        from tests.test_one_local_engine_holds_the_card import _fake_child

        _fake_child(f, health_delay=0.1)
        starting = _request(f)
        await _until(lambda: card_holders() and card_holders()[0]["state"] == "starting")
        async with card_held_for(F, "accepted_request", conversation_id="older"):
            start = _request(s)
            await _until(lambda: _in_line() == [S])
            assert await asyncio.wait_for(f.ensure_running(), 5) == {"total_slots": 1}
            assert not start.done()
        await asyncio.wait_for(start, 10)
        await _finish(starting)

    @pytest.mark.asyncio
    async def test_a_waiter_behind_the_first_place_holds_no_start_lock_so_the_swap_is_not_stuck_on_it(
        self, tmp_path, swaps,
    ):
        # The queue is entered before `_start_lock`: a later request on the holder's alias holding
        # that lock would leave the first place's stop waiting for it until a deadline.
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        later = _request(f)
        await _until(lambda: _in_line() == [S, F])
        try:
            assert not f._start_lock.locked()
            async with slot:
                pass
            await asyncio.wait_for(start, 3)
            assert swaps[0] == (F, S)
        finally:
            await _finish(start, later)


class TestAPlace:
    @pytest.mark.asyncio
    async def test_one_alias_has_one_place_and_a_request_joins_it(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        one, two = _request(s), _request(s)
        try:
            await _until(lambda: len(base_mod._CARD_QUEUE) == 1 and len(base_mod._CARD_QUEUE[0].requests) == 2)
            assert _in_line() == [S]
        finally:
            await _finish(one, two)
            async with slot:
                pass
        assert base_mod._CARD_QUEUE == []

    @pytest.mark.asyncio
    async def test_the_place_stays_with_the_next_oldest_request_and_is_gone_only_when_empty(self, tmp_path, swaps):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        oldest, next_oldest = _request(s), _request(s)
        await _until(lambda: _in_line() == [S] and len(base_mod._CARD_QUEUE[0].requests) == 2)
        behind = _request(g)
        await _until(lambda: _in_line() == [S, G])
        try:
            oldest.cancel()
            await asyncio.wait([oldest])
            await asyncio.sleep(0.02)
            assert _in_line() == [S, G], "the place did not move: it passed to the next oldest"
            assert len(base_mod._CARD_QUEUE[0].requests) == 1

            async with slot:
                pass
            await asyncio.wait_for(next_oldest, 10)
            assert swaps[0] == (F, S)
            await asyncio.wait_for(behind, 10)
            assert _in_line() == []
        finally:
            await _finish(oldest, next_oldest, behind)

    @pytest.mark.asyncio
    async def test_only_the_first_place_acts(self, tmp_path, monkeypatch):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        marked_by = []
        real_begin = type(f)._begin_swap_drain

        def begin(self, new):
            marked_by.append((new.alias, _in_line()[0]))
            real_begin(self, new)

        monkeypatch.setattr(type(f), "_begin_swap_drain", begin)
        slot = f.call_slot()
        first, second = _request(s), _request(g)
        try:
            await _until(lambda: _in_line() == [S, G])
            async with slot:
                pass
            await asyncio.wait_for(asyncio.gather(first, second), 10)
        finally:
            await _finish(first, second)
        assert marked_by == [(S, S), (G, G)], "each mark was made by the first in line"

    @pytest.mark.asyncio
    async def test_five_starts_on_an_empty_card_claim_one_at_a_time_and_each_claims_as_first(self, tmp_path):
        # The race for the claim. Every claim is made first in line, with nothing awaited since
        # the last look: at the claim no other holder stands, whoever the loop ran before it.
        sups = [_strata(tmp_path / str(n), f"alias {n}") for n in range(5)]
        for sup in sups:
            _ACTIVE_SUPERVISORS[sup.alias] = sup
        seen = []

        for sup in sups:
            real = sup._claim_card

            def claiming(sup=sup, real=real):
                if sup not in base_mod._CARD_CLAIMS:
                    seen.append((sup.alias, _in_line()[:1], [h["alias"] for h in card_holders()]))
                real()

            sup._claim_card = claiming

        starts = [_another_request(sup.ensure_running()) for sup in sups]
        try:
            await asyncio.wait_for(asyncio.gather(*starts), 30)
        finally:
            await _finish(*starts)

        assert [alias for alias, _, _ in seen] == [sup.alias for sup in sups]
        assert all(first == [alias] and holders == [] for alias, first, holders in seen), seen
        assert len(card_holders()) == 1


class TestOneDeadline:
    @pytest.mark.asyncio
    async def test_it_is_counted_from_acceptance_and_not_from_each_round_of_the_swap(
        self, tmp_path, clock, settings, swaps,
    ):
        # Two holders in a row: the second gets what the first left, not a clock of its own.
        settings.set_local_engine_swap(True, 100, 0)
        one, two = _llama("llama one"), _llama("llama two")
        s = _strata(tmp_path / "s", S)
        for sup in (one, two, s):
            _ACTIVE_SUPERVISORS[sup.alias] = sup
        await one.ensure_running()
        await two.ensure_running()
        slot_one, slot_two = one.call_slot(), two.call_slot()
        accepted = clock.now
        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now = accepted + 60
            async with slot_one:
                pass
            await _until(lambda: swaps == [("llama one", S)])

            clock.now = accepted + 99
            await asyncio.sleep(0.03)
            assert not start.done()

            clock.now = accepted + 101
            with pytest.raises(base_mod.CardQueueRefusal, match="busy for 101 s"):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
        assert _alive(two) and two._draining is False and two._swap_for is None
        assert _in_line() == []
        async with slot_two:
            pass

    @pytest.mark.asyncio
    async def test_it_runs_from_the_request_s_acceptance_and_not_from_its_arrival_at_the_card(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()

        async def later_on_the_card():
            async with card_held_for(S, "accepted_request", conversation_id="c"):
                clock.now += 50  # the request did something else first
                await s.ensure_running()

        start = _another_request(later_on_the_card())
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 9
            await asyncio.sleep(0.02)
            assert not start.done()
            clock.now += 2
            with pytest.raises(base_mod.CardQueueRefusal, match="busy for 61 s"):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_the_setting_is_read_once(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            settings.set_local_engine_swap(True, 6000, 0)
            clock.now += 61
            with pytest.raises(base_mod.CardQueueRefusal):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_the_setting_is_read_at_acceptance_and_not_when_the_request_reaches_the_card(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()

        async def accepted_then_changed():
            async with card_held_for(S, "accepted_request", conversation_id="c"):
                settings.set_local_engine_swap(True, 6000, 0)
                await s.ensure_running()

        start = _another_request(accepted_then_changed())
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 61
            with pytest.raises(base_mod.CardQueueRefusal, match="at most 60 s"):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_before_the_mark_the_holder_is_untouched_and_the_refusal_says_who_why_and_the_setting(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        gate = get_run_gate("agent_johnny")
        waiting_for_a_person = asyncio.Event()

        async def a_turn_waiting_for_approval():
            async with gate.hold({"conversation_id": "johnny-chat"}):
                async with card_held_for(F, "accepted_request", agent_id="agent_johnny",
                                         conversation_id="johnny-chat"):
                    with turn_waits_for("approval"):
                        waiting_for_a_person.set()
                        await asyncio.Event().wait()

        turn = _another_request(a_turn_waiting_for_approval())
        await waiting_for_a_person.wait()
        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 60
            with pytest.raises(CardHeldError) as refused:
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start, turn)
        message = str(refused.value)
        assert isinstance(refused.value, base_mod.CardQueueRefusal)
        assert f"strata alias '{F}'" in message and "busy for 60 s" in message and "at most 60 s" in message
        assert "agent 'agent_johnny', conversation 'johnny-chat', wait_reason 'approval'" in message
        _untouched(f)
        assert base_mod.last_swap() is None

    @pytest.mark.asyncio
    async def test_after_the_mark_the_stop_runs_to_its_end_the_request_leaves_and_the_next_takes_the_card(
        self, tmp_path, clock, settings, swaps, monkeypatch,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        timeouts = []
        real_stop = base_mod.stop_by_hand

        async def recording(supervisor, timeout=None, reopen=True, why=None):
            timeouts.append(timeout)
            await real_stop(supervisor, timeout=timeout, reopen=reopen, why=why)

        monkeypatch.setattr(base_mod, "stop_by_hand", recording)
        reached, let_go = _slow_stop(f)
        start = _request(s)
        behind = None
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert f._swap_for == S
            settings.set_local_engine_swap(True, 6000, 0)  # read once: only the next one gets it
            behind = _request(g)
            await _until(lambda: _in_line() == [S, G])

            clock.now += 61
            with pytest.raises(base_mod.CardQueueRefusal, match="already stopping"):
                await asyncio.wait_for(start, 5)

            assert timeouts == [None], "the remainder is never drain's timeout: drain would stop it anyway"
            assert _alive(f), "the stop is not hurried by the refusal"
            assert f._swap_for == S, "and not cancelled either"
            assert _in_line() == [G] and not behind.done()

            let_go.set()
            await asyncio.wait_for(behind, 10)
        finally:
            let_go.set()
            await _finish(*[t for t in (start, behind) if t is not None])
        assert _alive(g) and not _alive(f) and not _alive(s)
        assert f._draining is False and f._swap_for is None, "no half-stopped mark is left behind"

    @pytest.mark.asyncio
    async def test_a_request_cancelled_after_the_mark_does_not_stop_the_stop_or_leave_a_mark(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        start = _request(s)
        await asyncio.wait_for(reached.wait(), 5)

        start.cancel()
        await asyncio.wait([start])
        assert start.cancelled() and f._swap_for == S and _in_line() == []
        let_go.set()
        await _until(lambda: not _alive(f))
        await _until(lambda: f._swap_for is None)
        assert f._draining is False

    @pytest.mark.asyncio
    async def test_a_stop_that_fails_leaves_no_mark_and_the_holder_serves_again(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()

        async def fails():
            raise LlamaServerError("the child could not be stopped")

        f._stop_child = fails
        with pytest.raises(LlamaServerError, match="could not be stopped"):
            await asyncio.wait_for(_request(s), 10)
        _untouched(f)
        assert _in_line() == []
        async with f.call_slot():
            pass

    @pytest.mark.asyncio
    async def test_a_retired_holder_is_left_closed_but_with_no_mark_for_anybody(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        _ACTIVE_SUPERVISORS[F] = _strata(tmp_path / "f2", F)  # a settings save replaced it
        await asyncio.wait_for(_request(s), 10)
        assert f._draining is True and f._swap_for is None

    @pytest.mark.asyncio
    async def test_a_request_past_its_deadline_is_refused_at_once_by_a_busy_holder_and_swaps_an_idle_one(
        self, tmp_path, clock, swaps,
    ):
        # An old request that lost its alias. Rule 5 bounds a wait; an idle holder asks for none.
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        async with card_held_for(S, "accepted_request", conversation_id="old"):
            clock.now += 1000
            slot = f.call_slot()
            with pytest.raises(base_mod.CardQueueRefusal, match="busy for 1000 s"):
                await asyncio.wait_for(s.ensure_running(), 5)
            _untouched(f)
            async with slot:
                pass

            assert await asyncio.wait_for(s.ensure_running(), 5) == {"total_slots": 1}
            assert swaps == [(F, S)]

    @pytest.mark.asyncio
    async def test_a_wait_of_zero_refuses_behind_a_place_at_once(self, tmp_path, settings):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        settings.set_local_engine_swap(True, 0, 0)
        try:
            with pytest.raises(base_mod.CardQueueRefusal, match="behind"):
                await asyncio.wait_for(_request(g), 2)
        finally:
            await _finish(start)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_holder_that_never_stops_cannot_hold_a_request_past_its_deadline(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        f._draining = True  # a stop for another reason, that goes on

        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            await asyncio.sleep(0.02)
            assert not start.done(), "decision 1: a place, not today's refusal"
            clock.now += 61
            with pytest.raises(base_mod.CardQueueRefusal, match=r"\(draining\)"):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)


class TestARequestThatCameWithNoTimeLeft:
    """`came_late`: `swap_wait_s` 0, or an old request that lost its alias. A holder that is not busy
    asks it for no wait, and what it then waits for is bounded by `_STOP_WITH_NO_WAIT_LEFT_S` and not
    by the setting. Its refusals name the bound it actually waited on."""

    @pytest.mark.asyncio
    async def test_the_stop_it_began_is_waited_for_up_to_the_limit_and_the_refusal_names_the_limit(
        self, tmp_path, clock, settings, swaps,
    ):
        settings.set_local_engine_swap(True, 0, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        start = _request(s)
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert f._swap_for == S
            clock.now += base_mod._STOP_WITH_NO_WAIT_LEFT_S - 1
            await asyncio.sleep(0.03)
            assert not start.done(), "the limit bounds this wait, not the setting's 0 s"
            clock.now += 1
            with pytest.raises(base_mod.CardQueueRefusal) as refused:
                await asyncio.wait_for(start, 5)
            assert _alive(f) and f._swap_for == S, "the stop goes on"
        finally:
            let_go.set()
            await _finish(start)
        message = str(refused.value)
        assert "already stopping" in message
        assert "the limit of 60 s on waiting for a stop" in message
        assert "in place of the Wait for a busy holder setting, 0 s" in message
        assert "the wait of 0 s" not in message
        await _until(lambda: f._swap_for is None)
        assert swaps == [(F, S)], "said once, whoever is left"

    @pytest.mark.asyncio
    async def test_an_earlier_child_that_does_not_let_go_is_refused_at_the_limit_in_the_limit_s_words(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 0, 0)
        _f, s, _g = _three(tmp_path)

        async def never_lets_go():
            await asyncio.Event().wait()

        s._clear_unfinished_start = never_lets_go
        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += base_mod._STOP_WITH_NO_WAIT_LEFT_S - 1
            await asyncio.sleep(0.03)
            assert not start.done()
            clock.now += 1
            with pytest.raises(base_mod.CardQueueRefusal) as refused:
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
        message = str(refused.value)
        assert "earlier child did not let go within the limit of 60 s on waiting for a stop" in message
        assert "it waited" not in message and "at most 0 s" not in message

    @pytest.mark.asyncio
    async def test_a_stop_of_its_own_alias_behind_the_start_lock_is_bounded_by_the_limit_and_said_so(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 0, 0)
        f, _s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        stopping = asyncio.ensure_future(base_mod.stop_by_hand(f))
        await asyncio.wait_for(reached.wait(), 5)
        back = _request(f)
        try:
            await asyncio.sleep(0.03)
            assert not back.done()
            clock.now += base_mod._STOP_WITH_NO_WAIT_LEFT_S
            with pytest.raises(base_mod.CardQueueRefusal, match="the limit of 60 s on waiting for a stop"):
                await asyncio.wait_for(back, 5)
        finally:
            let_go.set()
            await _finish(stopping, back)

    @pytest.mark.asyncio
    async def test_behind_a_place_it_is_refused_at_once_and_its_age_is_not_called_a_wait(
        self, tmp_path, clock,
    ):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        try:
            async with card_held_for(G, "accepted_request", conversation_id="old"):
                clock.now += 1000
                with pytest.raises(base_mod.CardQueueRefusal) as refused:
                    await asyncio.wait_for(g.ensure_running(), 5)
        finally:
            await _finish(start)
            async with slot:
                pass
        message = str(refused.value)
        assert "it came 1000 s after its acceptance, with no time left to wait for the card behind" in message
        assert "it waited" not in message
        assert "at most 60 s" in message, "here the bound is the setting: no stop was waited for"
        assert "the limit of" not in message

    @pytest.mark.asyncio
    async def test_a_request_with_time_left_still_names_the_setting_and_not_the_limit(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 40, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        start = _request(s)
        try:
            await asyncio.wait_for(reached.wait(), 5)
            clock.now += 40
            with pytest.raises(base_mod.CardQueueRefusal) as refused:
                await asyncio.wait_for(start, 5)
        finally:
            let_go.set()
            await _finish(start)
        message = str(refused.value)
        assert "the wait of 40 s (Settings: Wait for a busy holder) ran out" in message
        assert "the limit of" not in message


class TestAHolderStoppingForAnotherReason:
    @pytest.mark.asyncio
    async def test_a_request_arriving_during_that_stop_takes_a_place_and_gets_the_card(self, tmp_path, swaps):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        stopping = asyncio.ensure_future(base_mod.stop_by_hand(f))
        await asyncio.wait_for(reached.wait(), 5)

        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            await asyncio.sleep(0.02)
            assert not start.done()
            let_go.set()
            await asyncio.wait_for(stopping, 5)
            await asyncio.wait_for(start, 10)
        finally:
            let_go.set()
            await _finish(start, stopping)
        assert swaps == [], "someone else's stop freed it: not a swap"
        assert _alive(s)

    @pytest.mark.asyncio
    async def test_a_request_on_the_stopping_alias_itself_is_queued_and_not_refused(self, tmp_path, swaps):
        # The 16:25:06 refusal: an owner's request on the alias being stopped for another.
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        start = _request(s)
        await asyncio.wait_for(reached.wait(), 5)

        back_on_f = _request(f)
        try:
            await _until(lambda: _in_line() == [S, F])
            let_go.set()
            await asyncio.wait_for(start, 10)
            await asyncio.wait_for(back_on_f, 10)
        finally:
            let_go.set()
            await _finish(start, back_on_f)
        assert swaps == [(F, S), (S, F)]
        assert _alive(f)


class TestLeavingTheLine:
    @pytest.mark.asyncio
    async def test_a_supervisor_retired_while_it_waits_leaves_and_starts_no_child_nobody_holds(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        s._draining = True  # a settings save retired it
        with pytest.raises(base_mod.EngineDrainingError):
            await asyncio.wait_for(start, 5)
        assert not _alive(s) and _in_line() == []
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_cancel_leaves_and_the_next_place_acts(self, tmp_path, swaps):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first, second = _request(s), _request(g)
        await _until(lambda: _in_line() == [S, G])

        first.cancel()
        await asyncio.wait([first])
        assert _in_line() == [G]

        async with slot:
            pass
        await asyncio.wait_for(second, 10)
        assert swaps == [(F, G)]

    @pytest.mark.asyncio
    async def test_a_first_stop_press_takes_a_turn_out_of_the_line_and_the_turn_says_it_was_stopped(
        self, tmp_path, swaps,
    ):
        f, s, _g = _three(tmp_path)
        engines = {F: _Engine(f), S: _Engine(s)}
        await f.ensure_running()
        slot = f.call_slot()
        asked = []
        real = s.ensure_running

        async def counting():
            asked.append(1)
            return await real()

        s.ensure_running = counting
        agent = _agent(tmp_path, _Manager(engines), provider_alias=S)
        interrupt, kill = asyncio.Event(), asyncio.Event()
        turn = _another_request(agent.process("a task", "the-chat", stop_event=interrupt, kill_event=kill))
        await _until(lambda: _in_line() == [S])

        # The seam a Stop press goes through, from the UI and from Telegram alike.
        manager = DpcAgentManager.__new__(DpcAgentManager)
        manager._interrupt_events = {"the-chat": interrupt}
        manager._kill_events = {"the-chat": kill}
        assert manager._interrupt_loop("the-chat") is True

        answer = await asyncio.wait_for(turn, 10)
        assert "Stopped by user" in answer
        assert agent._last_trace["turn_end"] == "stopped"
        assert asked == [1], "the native path's fallback does not take the turn back into the line"
        assert _in_line() == [] and swaps == []
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_the_queue_s_cancel_touches_nothing_for_a_chat_not_in_line(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s, conversation_id="the-chat")
        await _until(lambda: _in_line() == [S])
        try:
            base_mod.leave_card_queue("nobody")
            base_mod.leave_card_queue("")
            await asyncio.sleep(0.02)
            assert _in_line() == [S] and not start.done()
        finally:
            await _finish(start)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_the_queue_s_cancel_is_not_the_run_gate_s(self, tmp_path):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s, conversation_id="the-chat")
        await _until(lambda: _in_line() == [S])
        try:
            assert run_gate.stop_waiting("the-chat") == []
            await asyncio.sleep(0.02)
            assert _in_line() == [S] and not start.done()
            base_mod.leave_card_queue("the-chat")
            with pytest.raises(base_mod.TakenOutOfTheCardQueue):
                await asyncio.wait_for(start, 5)
        finally:
            await _finish(start)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_stop_from_another_loop_changes_the_line_on_the_owner_loop(self, tmp_path, monkeypatch):
        # Telegram's /stop runs on the loop its bridge was started on, which is not always this one.
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s, conversation_id="the-chat")
        await _until(lambda: _in_line() == [S])
        threads = set()
        real = base_mod._QueuedRequest.leaves

        def leaves(self):
            threads.add(threading.get_ident())
            real(self)

        monkeypatch.setattr(base_mod._QueuedRequest, "leaves", leaves)

        async def stop_on_another_loop():
            base_mod.leave_card_queue("the-chat")

        try:
            await asyncio.to_thread(asyncio.run, stop_on_another_loop())
            with pytest.raises(base_mod.TakenOutOfTheCardQueue):
                await asyncio.wait_for(start, 5)
            assert threads == {threading.get_ident()}, "the line changed off the owner loop"
            assert _in_line() == []
        finally:
            await _finish(start)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_turn_the_queue_refuses_ends_failed_and_its_text_is_the_refusal_and_no_llm_error(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        engines = {F: _Engine(f), S: _Engine(s)}
        await f.ensure_running()
        slot = f.call_slot()
        agent = _agent(tmp_path, _Manager(engines), provider_alias=S)
        turn = _another_request(agent.process("a task", "the-chat"))
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 61
            answer = await asyncio.wait_for(turn, 10)
        finally:
            await _finish(turn)
        assert "LLM error" not in answer
        assert f"strata alias '{F}'" in answer and "busy for 61 s" in answer and "at most 60 s" in answer
        assert len(answer.splitlines()) == 1, "a failed record is replayed by its first line"
        assert agent._last_trace["turn_end"] == "failed"
        assert not agent._last_trace.get("stopped_by_user")
        assert engines[S].round == 1, "the native path's fallback does not take the turn back into the line"
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["strata", "llama-server"])
    async def test_a_request_on_a_retired_supervisor_is_refused_at_once_and_does_not_wait_out_its_deadline(
        self, tmp_path, engine,
    ):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first = _request(s)
        await _until(lambda: _in_line() == [S])
        retired = _strata(tmp_path / "r", "a retired alias") if engine == "strata" else _llama("a retired alias")
        _ACTIVE_SUPERVISORS["a retired alias"] = _llama("a retired alias")  # a settings save replaced it
        retired._draining = True
        try:
            with pytest.raises(base_mod.EngineDrainingError):
                await asyncio.wait_for(_request(retired), 2)
            assert _in_line() == [S], "and it took no place on its way out"
        finally:
            await _finish(first)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_request_on_a_closed_provider_s_supervisor_is_refused_at_once(self, tmp_path):
        # `close()` hands the registry entry back after its drain, so this one still counts as
        # configured, and nothing will reopen it.
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first = _request(s)
        await _until(lambda: _in_line() == [S])
        del _ACTIVE_SUPERVISORS[G]
        g._draining = True
        try:
            with pytest.raises(base_mod.EngineDrainingError):
                await asyncio.wait_for(_request(g), 2)
            assert _in_line() == [S]
        finally:
            await _finish(first)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_supervisor_retired_while_it_waits_behind_a_place_does_not_wait_out_its_deadline(self, tmp_path):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first, behind = _request(s), _request(g)
        await _until(lambda: _in_line() == [S, G])
        _ACTIVE_SUPERVISORS[G] = _strata(tmp_path / "g2", G)
        g._draining = True
        try:
            with pytest.raises(base_mod.EngineDrainingError):
                await asyncio.wait_for(behind, 2)
            assert _in_line() == [S]
        finally:
            await _finish(first, behind)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_request_on_an_alias_a_person_is_stopping_waits_for_the_reopen(self, tmp_path):
        # Draining too, and this one ends: `stop_by_hand` reopens it, so it is waited for.
        f, _s, _g = _three(tmp_path)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        stopping = asyncio.ensure_future(base_mod.stop_by_hand(f))
        await asyncio.wait_for(reached.wait(), 5)
        back = _request(f)
        try:
            await asyncio.sleep(0.03)
            assert not back.done()
            let_go.set()
            await asyncio.wait_for(stopping, 5)
            assert await asyncio.wait_for(back, 10) == {"total_slots": 1}
            assert _alive(f)
        finally:
            let_go.set()
            await _finish(stopping, back)

    def test_the_refusal_is_found_however_many_wraps_stand_over_it(self):
        refusal = base_mod.CardQueueRefusal("no card", S, "strata")
        error = refusal
        for n in range(8):
            wrap = RuntimeError(f"wrap {n}")
            wrap.__cause__ = error
            error = wrap
        assert base_mod.card_queue_refusal_in(error) is refusal
        assert base_mod.card_queue_refusal_in(RuntimeError("no refusal behind it")) is None

    def test_a_cycle_of_causes_ends_the_walk(self):
        one, two = RuntimeError("one"), RuntimeError("two")
        one.__cause__, two.__cause__ = two, one
        assert base_mod.card_queue_refusal_in(one) is None

    @pytest.mark.asyncio
    async def test_a_kill_cancels_the_wait_and_leaves_no_place(self, tmp_path):
        f, s, _g = _three(tmp_path)
        engines = {F: _Engine(f), S: _Engine(s)}
        await f.ensure_running()
        slot = f.call_slot()
        agent = _agent(tmp_path, _Manager(engines), provider_alias=S)
        kill = asyncio.Event()
        turn = _another_request(agent.process("a task", "the-chat", kill_event=kill))
        await _until(lambda: _in_line() == [S])

        kill.set()
        answer = await asyncio.wait_for(turn, 10)
        assert "Stopped by user" in answer
        assert agent._last_trace["turn_end"] == "killed"
        assert _in_line() == []
        async with slot:
            pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", ["first", "second"])
    async def test_stop_engine_on_a_queued_alias_takes_its_place_away(self, tmp_path, position):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first = _request(g if position == "second" else s)
        await _until(lambda: len(_in_line()) == 1)
        second = _request(s) if position == "second" else None
        if second is not None:
            await _until(lambda: S in _in_line())
        target = second if second is not None else first
        try:
            assert await asyncio.wait_for(stop_local_engine(S), 5) is True
            assert S not in _in_line()
            with pytest.raises(base_mod.CardQueueRefusal, match="Stop engine"):
                await asyncio.wait_for(target, 5)
            _untouched(f)
        finally:
            await _finish(*[t for t in (first, second) if t is not None])
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_holder_s_death_advances_the_line_and_it_never_waits_for_the_dead(self, tmp_path, swaps):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        hold = card_held_for(F, "accepted_request", conversation_id="stands")
        await hold.__aenter__()
        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            f._proc.returncode = 137
            await asyncio.wait_for(start, 5)
        finally:
            await hold.__aexit__(None, None, None)
            await _finish(start)
        assert swaps == [] and _alive(s)

    @pytest.mark.asyncio
    async def test_stop_engine_on_the_holder_advances_the_line_once_the_call_ends(self, tmp_path, swaps):
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        stopping = asyncio.ensure_future(stop_local_engine(F))
        try:
            await asyncio.sleep(0.02)
            assert not start.done() and _alive(f), "Stop engine does not cut a call either"
            async with slot:
                pass
            await asyncio.wait_for(stopping, 5)
            await asyncio.wait_for(start, 5)
        finally:
            await _finish(start, stopping)
        assert swaps == [] and _alive(s)


class TestTheTurnSaysSo:
    @pytest.mark.asyncio
    async def test_a_turn_in_the_line_reads_card_queue_and_then_model_call_again(self, tmp_path):
        assert "card_queue" in run_gate.TURN_WAIT_REASONS
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        gate = RunGate(agent_id="agent_queued")
        read = {}

        async def a_turn():
            async with gate.hold({"conversation_id": "c"}):
                with turn_waits_for("model_call"):
                    await s.ensure_running()
                    read["after"] = gate.turn["wait_reason"]

        turn = _another_request(a_turn())
        await _until(lambda: gate.turn is not None and gate.turn["wait_reason"] == "card_queue")
        async with slot:
            pass
        await asyncio.wait_for(turn, 10)
        assert read["after"] == "model_call"

    @pytest.mark.asyncio
    async def test_a_stop_from_another_task_leaves_the_turn_with_no_stale_wait_reason(self, tmp_path):
        # `card_queue` is entered from the waiting task and left from the Stop's.
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        gate = RunGate(agent_id="agent_stopped")
        read = {}

        async def a_turn():
            async with gate.hold({"conversation_id": "the-chat"}):
                with turn_waits_for("model_call"):
                    async with card_held_for(S, "accepted_request", agent_id="agent_stopped",
                                             conversation_id="the-chat"):
                        with pytest.raises(base_mod.TakenOutOfTheCardQueue):
                            await s.ensure_running()
                    read["in_the_model_call"] = gate.turn["wait_reason"]
                read["after_the_model_call"] = gate.turn["wait_reason"]

        async def a_stop_press():
            base_mod.leave_card_queue("the-chat")
            read["right_after_the_stop"] = gate.turn["wait_reason"]  # nothing has awaited since

        turn = _another_request(a_turn())
        try:
            await _until(lambda: gate.turn is not None and gate.turn["wait_reason"] == "card_queue")
            await _another_request(a_stop_press())
            await asyncio.wait_for(turn, 5)
        finally:
            await _finish(turn)
            async with slot:
                pass
        assert read == {"right_after_the_stop": "model_call", "in_the_model_call": "model_call",
                        "after_the_model_call": None}

    @pytest.mark.asyncio
    async def test_a_turn_that_passes_straight_through_never_reads_it(self, tmp_path):
        _f, s, _g = _three(tmp_path)
        gate = RunGate(agent_id="agent_straight")
        written = []
        real = gate._apply

        def apply(conversation_id, only_if, fields, turn):
            written.append(fields.get("wait_reason"))
            real(conversation_id, only_if, fields, turn)

        gate._apply = apply
        async with gate.hold({"conversation_id": "c"}):
            with turn_waits_for("model_call"):
                await s.ensure_running()
        assert "card_queue" not in written


class TestPeers:
    @pytest.mark.asyncio
    async def test_a_new_peer_while_a_place_waits_is_refused_and_takes_none_and_learns_no_ids(self, tmp_path):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        async with card_held_for(F, "accepted_request", agent_id="agent_johnny", conversation_id="johnny-chat"):
            start = _request(s, agent_id="agent_owner", conversation_id="the-owners-chat")
            await _until(lambda: _in_line() == [S])

            answer = await asyncio.wait_for(_peer_call(coord, svc), 5)

            assert answer["code"] == "provider_unavailable"
            assert answer.get("retry_after_sec") is None
            assert _in_line() == [S] and len(base_mod._CARD_QUEUE[0].requests) == 1
            for secret in ("agent_johnny", "johnny-chat", "agent_owner", "the-owners-chat"):
                assert secret not in answer["error"]
            _untouched(f)
            await _finish(start)

    @pytest.mark.asyncio
    async def test_an_open_window_s_call_is_served_while_a_place_waits(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 300, 120)
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        assert (await _peer_call(coord, svc)).get("response") == "described"

        start = _request(s)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 10
            assert (await asyncio.wait_for(_peer_call(coord, svc), 5)).get("response") == "described"
            other = await asyncio.wait_for(_peer_call(coord, svc, peer="peer-2"), 5)
            assert other["code"] == "provider_unavailable"
            _untouched(f)
        finally:
            await _finish(start)

    @pytest.mark.asyncio
    async def test_a_peer_call_is_never_a_place_even_on_an_empty_line(self, tmp_path):
        f, s, _engines = _pair(tmp_path)
        await f.ensure_running()
        with serving_a_peer(PEER):
            with pytest.raises(LlamaServerError):  # a stopped Strata alias: not started for a peer
                await asyncio.wait_for(s.ensure_running(), 5)
        assert _in_line() == []
        _untouched(f)
        llama = _llama("a llama alias")
        with serving_a_peer(PEER):
            with pytest.raises(CardHeldError, match="the card is held by"):  # the claim's refusal
                await asyncio.wait_for(llama.ensure_running(), 5)
        assert _in_line() == []
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_stranger_cannot_jump_an_empty_card_while_a_place_waits(self, tmp_path):
        # The card is empty for a moment between the holder's stop and the place's claim, and the
        # peer's own start (`start_for_peer`) runs unmarked: it asks `_card_held_error`.
        _f, s, g = _three(tmp_path)
        gate = asyncio.Event()
        real = s._clear_unfinished_start

        async def held_up():
            await gate.wait()
            await real()

        s._clear_unfinished_start = held_up
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        try:
            assert card_holders() == []
            assert s._card_held_error() is None, "the first place itself is no stranger"
            refusal = g._card_held_error()
            assert isinstance(refusal, base_mod.CardQueueRefusal)

            assert g.start_for_peer(PEER) is True
            await asyncio.wait_for(g.peer_start_task, 5)
            assert not _alive(g) and card_holders() == []
        finally:
            gate.set()
            await asyncio.wait_for(start, 5)
        assert [h["alias"] for h in card_holders()] == [S]

    @pytest.mark.asyncio
    async def test_the_policy_of_a_stopped_strata_alias_begins_no_start_while_a_place_waits(
        self, tmp_path, monkeypatch,
    ):
        # The real route of a peer's start: `StrataProvider.peer_refusal`, card free, VRAM enough.
        provider = _strata_provider(tmp_path / "p")
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: 30000)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: 32000)
        s = _strata(tmp_path / "s", S)
        _ACTIVE_SUPERVISORS[S] = s
        gate = asyncio.Event()
        real = s._clear_unfinished_start

        async def held_up():
            await gate.wait()
            await real()

        s._clear_unfinished_start = held_up
        start = _request(s, agent_id="agent_owner", conversation_id="the-owners-chat")
        await _until(lambda: _in_line() == [S])
        try:
            reason = await provider.peer_refusal(PEER)
            assert "does not start it now: a request on this node is waiting for the card" in reason
            assert S not in reason and "the-owners-chat" not in reason and "agent_owner" not in reason
            assert provider.supervisor.peer_start_task is None
        finally:
            gate.set()
            await asyncio.wait_for(start, 5)

    @pytest.mark.asyncio
    async def test_a_supervisor_already_holding_a_claim_is_no_stranger(self, tmp_path):
        # A Strata `_launch` claims again, and a place may have been taken since the first claim.
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        try:
            assert f._card_held_error() is None
        finally:
            await _finish(start)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_a_peer_meeting_the_mark_reads_provider_unavailable(self, tmp_path):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        reached, let_go = _slow_stop(f)
        start = _request(s)
        await asyncio.wait_for(reached.wait(), 5)
        try:
            with pytest.raises(base_mod.EngineDrainingError) as refused:
                f.call_slot()
            wrapped = RuntimeError("the provider's wrap")
            wrapped.__cause__ = refused.value
            assert _engine_unavailable_cause(wrapped, strata=False)

            answer = await asyncio.wait_for(_peer_call(coord, svc), 5)
            assert answer["code"] == "provider_unavailable"
        finally:
            let_go.set()
            await _finish(start)


class TestTheGateway:
    @pytest.mark.asyncio
    async def test_the_order_is_the_card_queue_then_the_semaphore_then_the_call(self, tmp_path, swaps):
        f, s, engines = _pair(tmp_path)
        lock = asyncio.Semaphore(1)
        gateway = _gateway(tmp_path, engines, inference_lock=lock)
        await f.ensure_running()
        await lock.acquire()  # a guest's call on a loaded alias, say

        call = asyncio.create_task(gateway.complete(S, "ping"))
        try:
            await _until(lambda: _alive(s))
            assert swaps == [(F, S)], "the card came first"
            await asyncio.sleep(0.02)
            assert not call.done(), "and the semaphore after it"
            assert _held() == {S: 1}, "standing on its alias from acceptance"

            lock.release()
            assert (await asyncio.wait_for(call, 10)).text == "described"
        finally:
            await _finish(call)
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_a_client_waiting_in_the_queue_holds_no_semaphore(self, tmp_path):
        f, s, engines = _pair(tmp_path)
        lock = asyncio.Semaphore(1)
        gateway = _gateway(tmp_path, engines, inference_lock=lock)
        await f.ensure_running()
        slot = f.call_slot()
        call = asyncio.create_task(gateway.complete(S, "ping"))
        try:
            await _until(lambda: _in_line() == [S])
            assert not lock.locked()
        finally:
            await _finish(call)
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_the_door_limit_stays_on_the_semaphore(self, tmp_path, monkeypatch):
        f, s, engines = _pair(tmp_path)
        lock = asyncio.Semaphore(1)
        gateway = _gateway(tmp_path, engines, inference_lock=lock)
        monkeypatch.setattr(gateway._core.settings, "get_remote_inference_timeout", lambda: 0.05, raising=False)
        await lock.acquire()
        with pytest.raises(GatewayError) as refused:
            await asyncio.wait_for(gateway.complete(F, "ping"), 10)
        assert refused.value.code == "card_busy" and refused.value.status == 503
        assert _alive(f) and _held() == {}

    @pytest.mark.asyncio
    async def test_a_queue_refusal_is_provider_unavailable_and_not_an_upstream_error(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()
        slot = f.call_slot()
        call = asyncio.create_task(gateway.complete(S, "ping"))
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 61
            with pytest.raises(GatewayError) as refused:
                await asyncio.wait_for(call, 5)
        finally:
            await _finish(call)
        assert refused.value.status == 503
        assert refused.value.code == "provider_unavailable"
        assert refused.value.retry_after_sec is None
        assert "busy for 61 s" in str(refused.value)
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_queue_refusal_met_inside_the_call_reads_the_same(self, tmp_path, settings):
        # The child went away while the client sat on the semaphore.
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        settings.set_local_engine_swap(True, 0, 0)
        await s.ensure_running()
        slot = s.call_slot()

        async def goes_for_the_card(prompt, **_):
            raise base_mod.CardQueueRefusal("no card", S, "strata")

        engines[F].generate_response = goes_for_the_card
        engines[F].supervisor = f
        f._proc = s._proc  # up, so the queue before the semaphore lets it through
        f.props = {"total_slots": 1}
        base_mod._CARD_CLAIMS.pop(s, None)
        try:
            with pytest.raises(GatewayError) as refused:
                await asyncio.wait_for(gateway.complete(F, "ping"), 5)
            assert refused.value.status == 503 and refused.value.code == "provider_unavailable"
        finally:
            f._proc = None
            async with slot:
                pass

    @pytest.mark.asyncio
    async def test_two_gateway_starts_and_a_peer_do_not_converge_they_are_served_one_after_the_other(
        self, tmp_path, swaps,
    ):
        # Johnny's objection: peers do not queue, so nothing waits on a semaphore holder that
        # waits in the queue; the two gateway starts wait in one line, not in one wait loop.
        f, s, engines = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        engines[G] = _Served(g)
        coord, svc = _peer_door(tmp_path, engines)
        gateway = _gateway(tmp_path, engines, inference_lock=coord._peer_inference_lock)
        await f.ensure_running()
        slot = f.call_slot()

        on_s = asyncio.create_task(gateway.complete(S, "ping"))
        await _until(lambda: _in_line() == [S])
        on_g = asyncio.create_task(gateway.complete(G, "ping"))
        await _until(lambda: _in_line() == [S, G])
        try:
            answer = await asyncio.wait_for(_peer_call(coord, svc), 5)
            assert answer["code"] == "provider_unavailable"
            assert not coord._peer_inference_lock.locked(), "the peer let go: nothing waits on it"

            async with slot:
                pass
            done = await asyncio.wait_for(asyncio.gather(on_s, on_g), 20)
        finally:
            await _finish(on_s, on_g)
        assert [c.text for c in done] == ["described", "described"]
        assert swaps == [(F, S), (S, G)]


class TestTheSwitchAndTheSafetyNet:
    @pytest.mark.asyncio
    async def test_with_the_swap_off_there_is_no_line_and_the_refusal_is_at_once(self, tmp_path, settings):
        settings.set_local_engine_swap(False, 300, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        with pytest.raises(CardHeldError) as refused:
            await asyncio.wait_for(_request(s), 2)
        assert not isinstance(refused.value, base_mod.CardQueueRefusal)
        assert "swapping is off" in str(refused.value)
        assert _in_line() == []

    @pytest.mark.asyncio
    async def test_with_the_swap_off_a_stranger_is_not_refused_for_a_place_left_behind(self, tmp_path, settings):
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        settings.set_local_engine_swap(False, 300, 0)
        with pytest.raises(CardHeldError) as refused:
            await asyncio.wait_for(start, 5)
        assert "swapping is off" in str(refused.value), "a switch thrown during the wait: today's refusal"
        assert _in_line() == []
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_the_rounds_run_out_with_a_warning_and_the_refusal_names_who_holds_it(
        self, tmp_path, monkeypatch, caplog,
    ):
        monkeypatch.setattr(base_mod, "_MAX_SWAP_ROUNDS", 1)
        one, two = _llama("llama one"), _llama("llama two")
        s = _strata(tmp_path / "s", S)
        for sup in (one, two, s):
            _ACTIVE_SUPERVISORS[sup.alias] = sup
        await one.ensure_running()
        await two.ensure_running()
        with caplog.at_level(logging.WARNING, logger=base_mod.logger.name):
            with pytest.raises(CardHeldError, match="llama two"):
                await asyncio.wait_for(_request(s), 5)
        assert any("safety net" in r.getMessage() for r in caplog.records)
        assert _in_line() == []

    @pytest.mark.asyncio
    async def test_no_warning_when_the_last_round_freed_the_card(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(base_mod, "_MAX_SWAP_ROUNDS", 1)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        with caplog.at_level(logging.WARNING, logger=base_mod.logger.name):
            await asyncio.wait_for(_request(s), 5)
        assert not any("safety net" in r.getMessage() for r in caplog.records)


class TestWhatABusyTestCounts:
    @pytest.mark.asyncio
    async def test_a_hold_counts_when_it_was_there_first_or_has_been_through_the_line(self, tmp_path):
        # A request served out of a place on the holder's alias stands there for the next place,
        # though it was accepted after that place was taken.
        f, s, g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        first = _request(s)
        await _until(lambda: _in_line() == [S])
        behind = _request(g)
        await _until(lambda: _in_line() == [S, G])
        end_joined = asyncio.Event()
        joined = _request(s, end_joined, conversation_id="joined")  # joins S's place, older than none
        await _until(lambda: len(base_mod._CARD_QUEUE[0].requests) == 2)
        try:
            async with slot:
                pass
            await asyncio.wait_for(first, 10)
            await _until(lambda: _in_line() == [G])
            await asyncio.sleep(0.03)
            assert not behind.done() and _alive(s), "the joined request was let through and stands on S"
            assert s._draining is False

            end_joined.set()
            await asyncio.wait_for(behind, 10)
        finally:
            end_joined.set()
            await _finish(first, behind, joined)

    @pytest.mark.asyncio
    async def test_a_waiting_start_reads_a_later_hold_as_nothing_and_the_refusal_does_not_name_it(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60, 0)
        f, s, _g = _three(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = _request(s)
        await _until(lambda: _in_line() == [S])
        end_later = asyncio.Event()

        async def a_later_request():
            async with card_held_for(F, "accepted_request", conversation_id="came-later"):
                await end_later.wait()

        later = _another_request(a_later_request())
        try:
            await _until(lambda: _held().get(F) == 1)
            looks = _Looks(f)
            assert await asyncio.wait_for(looks.next(), 5) is True  # the call in flight
            clock.now += 61
            with pytest.raises(base_mod.CardQueueRefusal) as refused:
                await asyncio.wait_for(start, 5)
        finally:
            end_later.set()
            await _finish(start, later)
        assert "came-later" not in str(refused.value)
        async with slot:
            pass
