"""A request's description may borrow the card (ADR-044, amendment 2026-11-02, the card queue, rule 9,
second sentence, variant B).

The case: with the swap on, an agent whose alias cannot see and whose `vision_provider` excludes it
read "the visual analysis failed" each round. Now the description may give up the request's own
alias, through the queue, within the request's one deadline, on a stop that loses no parked
conversations, and it is kept for the turn. Nothing is forced, and everything else meets the refusal.

Real `DpcAgent.process`, `run_llm_loop` and adapter, real `LLMManager.query`, real supervisors with the
fakes of `test_one_local_engine_holds_the_card.py`. A is a llama-server alias that cannot see, B a
Strata alias that can: on this code that is the only pair a borrow can be, because two llama-server
children do not exclude each other and a Strata holder is not stopped.
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.tools import vision as vision_mod
from dpc_client_core.dpc_agent.tools.vision import describe_image
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    BackgroundCallSkipped,
    CardHeldError,
    CardQueueRefusal,
    LlamaServerError,
    LlamaServerSupervisor,
    TakenOutOfTheCardQueue,
    a_background_call,
    card_held_for,
    card_queue,
    leave_card_queue,
    serving_a_peer,
    take_card_queue_place,
)
from dpc_client_core.managers.strata_supervisor import PeerStartRefused
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

from tests.test_a_callers_run_of_calls_keeps_its_alias_busy_for_the_window import _Clock
from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    PIXEL,
    _agent,
    _alive,
    _another_request,
    _Engine,
    _Eye,
    _text_of,
    _Vendor,
    _finish,
    _held,
    _QueryingManager,
    _tool,
    _untouched,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401
from tests.test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced import _in_line, _request, _until

A, B, G = "the agent alias", "the vision alias", "another alias"
DESCRIPTION = "a cat on a keyboard"
FAILED = "The visual analysis failed ("


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


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
    assert base_mod._CARD_QUEUE == [], "a place outlived what it was taken for"
    assert base_mod._CARD_HOLDS == {}, "a hold outlived what it was taken for"


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


class _Looking(_Eye):
    """The vision alias over a real supervisor. `asked` counts every ask, a refused one too;
    `looks` only those that got the model. `during` is awaited inside the look."""

    during = None
    asked = 0

    async def generate_with_vision(self, prompt, images, **_):
        self.asked += 1
        await self.supervisor.ensure_running()
        async with self.supervisor.call_slot():
            self.looks += 1
            if self.during is not None:
                await self.during()
            return DESCRIPTION


def _engine_reports_parked(sup, parked):
    """The launcher's own `/metrics` as `strata_status` reads it."""

    async def get(path):
        if path == "/metrics":
            return {"engine": {"version": "0.1.46"}, "live": {"state": "loaded"},
                    "conversation_cache": {"enabled": True, "parked": parked}}
        return {"total_slots": 1}

    sup._get = get


def _render(agent, name="render.png", extra=b""):
    agent.agent_root.mkdir(parents=True, exist_ok=True)
    (agent.agent_root / name).write_bytes(base64.b64decode(PIXEL) + extra)
    return name


def _pair(tmp_path, rounds=1, holder=None):
    """A cannot see and holds nothing yet; `vision_provider` is B; they exclude each other."""
    a, b = holder or _llama(A), _strata(tmp_path / "b", B)
    _ACTIVE_SUPERVISORS[A], _ACTIVE_SUPERVISORS[B] = a, b
    blind, eyes = _Engine(a, rounds=rounds), _Looking(b)
    manager = _QueryingManager({A: blind, B: eyes})
    manager.vision_provider = B
    return a, b, blind, eyes, manager


async def _a_picture_turn(tmp_path, manager, conversation="chat", image=PIXEL):
    agent = _agent(tmp_path, manager, provider_alias=A)
    answer = await asyncio.wait_for(
        agent.process("what is this", conversation, image_base64=image, image_mime="image/png"), 20,
    )
    return agent, answer


class TestTheBorrow:
    @pytest.mark.asyncio
    async def test_a_llama_server_holder_is_stopped_for_the_description_and_the_turn_gets_it_back(
        self, tmp_path, swaps,
    ):
        a, b, blind, eyes, manager = _pair(tmp_path)
        await a.ensure_running()
        first_child = a._proc

        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert DESCRIPTION in blind.saw[0] and FAILED not in blind.saw[0]
        assert eyes.looks == 1
        assert swaps == [(A, B), (B, A)], "two swaps, each said once"
        assert _alive(a) and a._proc is not first_child and not _alive(b)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parked, dropped", [(0, False), (2, True), (None, True)])
    async def test_a_swap_says_parked_conversations_went_as_the_engine_reports_them(
        self, tmp_path, parked, dropped,
    ):
        # The launcher is asked just before its stop. Nothing readable is said as the class's word.
        a, b, _blind, _eyes, manager = _pair(tmp_path)
        _engine_reports_parked(b, parked)
        await a.ensure_running()

        await _a_picture_turn(tmp_path, manager)

        swap = base_mod.last_swap()
        assert (swap["from"], swap["to"], swap["parked_dropped"]) == (B, A, dropped)

    @pytest.mark.asyncio
    async def test_a_second_request_on_the_alias_is_busy_for_someone_else_and_is_waited_for(self, tmp_path, swaps):
        a, _b, blind, eyes, manager = _pair(tmp_path)
        await a.ensure_running()
        other_done = asyncio.Event()
        other = _request(a, other_done, agent_id="agent_2", conversation_id="chat_2")
        await _until(lambda: _held().get(A) == 1)

        turn = asyncio.ensure_future(_a_picture_turn(tmp_path, manager))
        await _until(lambda: _in_line() == [B])
        await asyncio.sleep(0.05)
        assert swaps == [] and eyes.asked == 1 and eyes.looks == 0
        _untouched(a)

        other_done.set()
        _agent_, answer = await turn
        assert answer == "the answer" and DESCRIPTION in blind.saw[0]
        assert swaps == [(A, B), (B, A)]
        await other

    @pytest.mark.asyncio
    async def test_a_second_request_that_outlasts_the_deadline_is_never_forced(self, tmp_path, settings, swaps):
        settings.set_local_engine_swap(True, 1, 0)
        a, b, blind, eyes, manager = _pair(tmp_path)
        await a.ensure_running()
        other_done = asyncio.Event()
        other = _request(a, other_done, agent_id="agent_2", conversation_id="chat_2")
        await _until(lambda: _held().get(A) == 1)

        try:
            agent, answer = await _a_picture_turn(tmp_path, manager)

            assert answer == "the answer" and agent._last_trace.get("turn_end") is None
            assert FAILED in blind.saw[0] and DESCRIPTION not in blind.saw[0]
            assert "agent 'agent_2'" in blind.saw[0], "the refusal names who is on the alias"
            assert swaps == [] and b._proc is None
            _untouched(a)
        finally:
            other_done.set()
            await other

    @pytest.mark.asyncio
    async def test_a_call_in_flight_on_the_alias_is_waited_for(self, tmp_path, swaps):
        a, _b, blind, _eyes, manager = _pair(tmp_path)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()

        turn = asyncio.ensure_future(_a_picture_turn(tmp_path, manager))
        await _until(lambda: _in_line() == [B])
        await asyncio.sleep(0.05)
        assert swaps == []

        await slot.__aexit__(None, None, None)
        await turn
        assert DESCRIPTION in blind.saw[0]


class TestWhatKeepsTheRefusal:
    """Each of these is today's refusal, at once, with the holder as it was."""

    @pytest.mark.asyncio
    async def test_a_strata_holder_is_not_stopped_and_the_refusal_says_why(self, tmp_path, swaps):
        strata_holder = _strata(tmp_path / "a", A)
        a, b, blind, eyes, manager = _pair(tmp_path, holder=strata_holder)
        await a.ensure_running()

        started = time.monotonic()
        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert time.monotonic() - started < 10, "swap_wait_s is 60: it did not wait"
        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert FAILED in blind.saw[0] and "would drop its parked conversations" in blind.saw[0]
        assert "would wait on itself" in blind.saw[0]
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_call_that_is_not_a_description_is_refused_as_before(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with pytest.raises(CardHeldError, match="would wait on itself"):
                await b.ensure_running()
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_turn_served_to_a_peer_does_not_borrow_for_a_strata_alias_it_may_not_start(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with serving_a_peer("dpc-node-guest"), base_mod.a_card_borrow():
                with pytest.raises(PeerStartRefused):
                    await b.ensure_running()
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_turn_served_to_a_peer_takes_no_place_and_stops_nothing_on_an_alias_it_may_start(
        self, tmp_path, swaps,
    ):
        # A llama-server child made to exclude, so that only the queue's own peer gate stands here:
        # with a Strata alias the start's "a peer does not start it" answers first. The holder is
        # busy (a request stands on it), so the peer's swap, allowed since the amendment of
        # 2026-11-06 on the owner's terms, is refused at once and in a peer's words.
        a, _b, _blind, _eyes, _manager = _pair(tmp_path)
        lone = _llama(G)
        lone.EXCLUSIVE_ON_THE_CARD = True
        _ACTIVE_SUPERVISORS[G] = lone
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with serving_a_peer("dpc-node-guest"), base_mod.a_card_borrow():
                with pytest.raises(CardHeldError, match="cannot load for a peer.*it is busy") as refused:
                    await asyncio.wait_for(lone.ensure_running(), 5)
            assert "conversation" not in str(refused.value) and "'c'" not in str(refused.value)
            assert _in_line() == []
        assert swaps == [] and lone._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_background_call_never_borrows(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with a_background_call("skill_reflection"), base_mod.a_card_borrow():
                with pytest.raises(BackgroundCallSkipped):
                    await b.ensure_running()
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_the_swap_off_is_today_s_refusal_word_for_word_each_round(self, tmp_path, settings, swaps):
        settings.set_local_engine_swap(False, 300)
        a, b, blind, eyes, manager = _pair(tmp_path, rounds=2)
        await a.ensure_running()

        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert eyes.asked == 2 and eyes.looks == 0, "nothing of this acts with the swap off, the keeping included"
        todays = (
            f"strata[{B}] cannot load: the card is held by llama-server alias '{A}' (running). "
            "strata and llama-server cannot hold the card together and DPC did not swap them here "
            f"(swapping is off, the start serves a peer, or '{A}' is already stopping). Stop '{A}' "
            f"from the providers settings to free the card, or switch this agent to '{A}'. Or tick "
            "'Swap local models when the card holder is idle' in Providers > Local engines, and DPC "
            "stops an idle holder for you."
        )
        assert todays in blind.saw[0] and todays in blind.saw[1]
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_place_already_waiting_is_a_wait_on_itself_and_is_refused_at_once(self, tmp_path, swaps):
        # That place waits for this very hold; behind it the description would wait for itself to
        # end, and ahead of it two swaps would be spent on a person's wait.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            waiting = _request(g)
            await _until(lambda: _in_line() == [G])
            started = time.monotonic()
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError, match="is waiting for it"):
                await b.ensure_running()
            assert time.monotonic() - started < 5
            assert swaps == [] and b._proc is None
            _untouched(a)
        await asyncio.wait_for(waiting, 10)
        assert swaps == [(A, G)]

    @pytest.mark.asyncio
    async def test_a_turn_past_its_deadline_does_not_start_a_borrow(self, tmp_path, monkeypatch, swaps):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            clock.now += 61
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError, match="time for the card has run out"):
                await b.ensure_running()
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("own_first", [True, False])
    async def test_another_child_on_the_card_keeps_the_refusal_and_nobody_is_stopped(self, tmp_path, swaps, own_first):
        # Whichever came first: stopping one and refusing on the other would be two losses.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        other = _llama("other llama")
        _ACTIVE_SUPERVISORS["other llama"] = other
        for child in ((a, other) if own_first else (other, a)):
            await child.ensure_running()

        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError) as raised:
                await asyncio.wait_for(b.ensure_running(), 5)

        message = str(raised.value)
        assert f"alias '{A}'" in message and "'other llama' is on the card too" in message
        assert "queue" not in message, "no place stands anywhere"
        assert swaps == [] and b._proc is None
        _untouched(a)
        _untouched(other)

    @pytest.mark.asyncio
    async def test_a_swap_wait_of_zero_never_borrows(self, tmp_path, settings, swaps):
        settings.set_local_engine_swap(True, 0, 0)
        a, b, blind, _eyes, manager = _pair(tmp_path)
        await a.ensure_running()

        _agent_, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and FAILED in blind.saw[0]
        assert swaps == [] and b._proc is None
        _untouched(a)


class TestTheOneDeadline:
    @pytest.mark.asyncio
    async def test_the_wait_for_a_busy_alias_ends_at_the_turn_s_deadline_and_not_a_new_one(
        self, tmp_path, monkeypatch, swaps,
    ):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        other_done = asyncio.Event()
        other = _request(a, other_done, conversation_id="chat_2")
        await _until(lambda: _held().get(A) == 1)
        try:
            async with card_held_for(A, "accepted_request", conversation_id="c"):
                clock.now += 50
                with base_mod.a_card_borrow():
                    asking = asyncio.ensure_future(b.ensure_running())
                    await _until(lambda: _in_line() == [B])
                    clock.now += 11  # past acceptance + 60; a clock begun at the place has 49 left
                    with pytest.raises(CardQueueRefusal, match="busy for 61 s"):
                        await asyncio.wait_for(asking, 5)
                assert _in_line() == [] and swaps == []
                _untouched(a)
        finally:
            other_done.set()
            await other

    @pytest.mark.asyncio
    async def test_a_request_behind_the_way_back_is_told_who_stands_on_the_borrowed_alias(
        self, tmp_path, monkeypatch, swaps,
    ):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", agent_id="agent_1", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            behind = _request(g)
            await _until(lambda: _in_line() == [A, G])
            clock.now += 61
            with pytest.raises(CardQueueRefusal, match="agent 'agent_1'"):
                await asyncio.wait_for(behind, 5)
            assert _in_line() == [A]


class TestAPersonsStop:
    """Rule 12: a person's Stop takes a turn out of the line, and it ends `stopped`."""

    @pytest.mark.asyncio
    async def test_a_stop_during_the_description_s_wait_ends_the_turn_with_no_model_call(self, tmp_path, swaps):
        a, _b, blind, _eyes, manager = _pair(tmp_path)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        turn = asyncio.ensure_future(_a_picture_turn(tmp_path, manager))
        await _until(lambda: _in_line() == [B])

        leave_card_queue("chat")
        agent, answer = await turn

        assert agent._last_trace.get("turn_end") == "stopped"
        assert blind.round == 0 and "Stopped by user" in answer
        assert swaps == []
        await slot.__aexit__(None, None, None)
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_stop_during_the_tool_s_wait_ends_the_turn_before_the_next_round(self, tmp_path, swaps):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, _b, blind, _eyes, manager = _pair(tmp_path, rounds=3)
        blind.tool = "look"
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)
        stop = asyncio.Event()
        answers = []

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            answers.append(await describe_image(ctx, name))
            return answers[-1]

        _tool(agent, "look", look)
        closing_calls = []
        real_closing = blind.generate_response

        async def counted(prompt, **kwargs):
            closing_calls.append(prompt)
            return await real_closing(prompt, **kwargs)

        blind.generate_response = counted
        turn = asyncio.ensure_future(asyncio.wait_for(agent.process("look", "chat", message_source="ui", stop_event=stop), 20))
        await _until(lambda: _in_line() == [B])

        stop.set()
        leave_card_queue("chat")
        answer = await turn

        assert agent._last_trace.get("turn_end") == "stopped" and "Stopped by user" in answer
        assert blind.round == 1 and closing_calls == [], "no further model call of any kind after the Stop"
        assert swaps == []
        await slot.__aexit__(None, None, None)
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_stop_while_the_way_back_stands_ends_the_turn_with_no_reload_for_a_summary(self, tmp_path, swaps):
        # Between two tools of a borrowing turn: the closing call would have to take the alias back.
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, b, blind, _eyes, manager = _pair(tmp_path, rounds=3)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)
        stop = asyncio.Event()

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            answer = await describe_image(ctx, name)
            stop.set()
            leave_card_queue("chat")
            return answer

        _tool(agent, "look", look)
        answer = await asyncio.wait_for(agent.process("look", "chat", message_source="ui", stop_event=stop), 20)

        assert agent._last_trace.get("turn_end") == "stopped" and "Stopped by user" in answer
        assert "No summary" not in answer, "no call was tried, and none failed"
        assert swaps == [(A, B)] and _alive(b) and not _alive(a)

    @pytest.mark.asyncio
    async def test_a_stop_with_no_request_in_line_keeps_the_closing_call(self, tmp_path, swaps):
        # The loop's contract for a Stop between rounds stands where the card is not in question.
        a, _b, blind, _eyes, manager = _pair(tmp_path, rounds=3)
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        stop = asyncio.Event()

        async def stop_between_rounds():
            stop.set()
            leave_card_queue("chat")

        blind.before_round[1] = stop_between_rounds
        answer = await asyncio.wait_for(agent.process("look", "chat", message_source="ui", stop_event=stop), 20)

        assert agent._last_trace.get("turn_end") == "stopped"
        assert "Stopped by user" not in answer, "the closing call wrote the answer"

    @pytest.mark.asyncio
    async def test_a_stop_is_not_kept_as_the_turn_s_description(self, tmp_path):
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            base_mod.keep_turn_description("k", "failed", failure=TakenOutOfTheCardQueue("stopped", A, ""))
            assert base_mod.turn_description("k") is None


class TestWhatOutlivesItsRequest:
    @pytest.mark.asyncio
    async def test_a_tool_s_wait_ends_before_the_tool_times_out_so_no_orphan_stops_the_alias(
        self, tmp_path, settings, monkeypatch, swaps,
    ):
        # `swap_wait_s` above the tool's timeout: a thread left in line would go on with this turn's
        # hold, and its own stand would not count, so it would stop the alias between two rounds.
        settings.set_local_engine_swap(True, 20, 0)
        monkeypatch.setattr(vision_mod, "_TOOL_TIMEOUT_S", 1)
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, b, blind, _eyes, manager = _pair(tmp_path, rounds=2)
        blind.tool = "look"
        await a.ensure_running()
        other_done = asyncio.Event()
        other = _request(a, other_done, agent_id="agent_2", conversation_id="chat_2")
        await _until(lambda: _held().get(A) == 1)
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)
        answers = []

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            answers.append(await describe_image(ctx, name))
            return answers[-1]

        _tool(agent, "look", look)
        agent.tools.get_timeout = lambda name: 3

        async def between_the_rounds():
            other_done.set()
            await asyncio.sleep(0.3)
            assert swaps == [], "nobody may stop the alias under this turn"

        blind.before_round[2] = between_the_rounds
        answer = await asyncio.wait_for(agent.process("look", "chat", message_source="ui"), 20)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert answers[0].startswith("⚠️ Vision query failed:"), "the tool's own failure, not its timeout"
        assert "the tool's own timeout" in answers[0]
        assert swaps == [] and b._proc is None
        await other
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_description_in_line_when_its_request_ended_stops_nothing(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                asking = asyncio.ensure_future(b.ensure_running())
            await _until(lambda: _in_line() == [B])
        await slot.__aexit__(None, None, None)

        with pytest.raises(CardQueueRefusal, match="has ended"):
            await asyncio.wait_for(asking, 5)
        assert swaps == [] and b._proc is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_description_asked_after_its_request_ended_loads_nothing(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            left_behind = base_mod.current_card_hold()

        with base_mod.carrying_card_hold(left_behind), base_mod.a_card_borrow():
            with pytest.raises(CardQueueRefusal, match="has ended"):
                await asyncio.wait_for(b.ensure_running(), 5)
        assert swaps == [] and b._proc is None
        _untouched(a)


class TestTheWayBack:
    @pytest.mark.asyncio
    async def test_a_look_that_outlasts_the_way_back_s_bound_ends_the_turn_in_words_naming_the_holder(
        self, tmp_path, settings, swaps,
    ):
        # The one written way a borrow can cost a turn (Mike's call, 2026-11-04).
        settings.set_local_engine_swap(True, 1, 0)
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, b, blind, eyes, manager = _pair(tmp_path, rounds=3)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)

        async def slow():
            await asyncio.sleep(3.5)

        eyes.during = slow

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            return await describe_image(ctx, name)

        _tool(agent, "look", look)
        agent.tools.get_timeout = lambda name: 0.8
        answer = await asyncio.wait_for(agent.process("look", "chat", message_source="ui"), 30)

        assert agent._last_trace.get("turn_end") == "failed"
        assert f"alias '{B}', busy for" in answer and "gave up" in answer
        assert "agent_johnny" not in answer, "a turn is not named as its own blocker"
        assert swaps[0] == (A, B)
        await asyncio.sleep(3)  # the thread's look ends on the test's own loop

    @pytest.mark.asyncio
    async def test_a_tool_timeout_during_the_look_does_not_cost_the_turn(self, tmp_path, settings, swaps):
        # The look is still in flight when the next round wants the alias back, past the first
        # swap's deadline; the way back's own bound starts then and covers what is left of it.
        settings.set_local_engine_swap(True, 2, 0)
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, b, blind, eyes, manager = _pair(tmp_path, rounds=3)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)

        async def slow():
            await asyncio.sleep(2.5)

        eyes.during = slow

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            return await describe_image(ctx, name)

        _tool(agent, "look", look)
        agent.tools.get_timeout = lambda name: 1.2
        answer = await asyncio.wait_for(agent.process("look", "chat", message_source="ui"), 30)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert blind.round == 3 and eyes.looks == 1
        assert swaps[:2] == [(A, B), (B, A)] and _alive(a)

    async def _an_older_request_on_the_borrowed_alias(self, b):
        """Accepted before the borrow on an alias with no child, and served on it during the borrow."""
        older_done, older_on_b = asyncio.Event(), asyncio.Event()

        async def older():
            async with card_held_for(B, "accepted_request", conversation_id="older"):
                await older_on_b.wait()
                await b.ensure_running()
                await older_done.wait()

        standing = _another_request(older())
        await _until(lambda: _held().get(B) == 1)
        return standing, older_on_b, older_done

    @pytest.mark.asyncio
    async def test_the_way_back_s_bound_starts_when_it_starts_and_not_at_acceptance(self, tmp_path, monkeypatch, swaps):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        standing, older_on_b, older_done = await self._an_older_request_on_the_borrowed_alias(b)
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            older_on_b.set()
            await asyncio.sleep(0.02)
            clock.now += 500  # long past acceptance + 60, with nothing started yet
            back = asyncio.ensure_future(a.ensure_running())
            await asyncio.sleep(0.05)
            clock.now += 59
            await asyncio.sleep(0.05)
            assert not back.done(), "59 of its own 60 s"
            assert swaps == [(A, B)]

            older_done.set()
            await asyncio.wait_for(back, 10)
            assert swaps == [(A, B), (B, A)]
        await standing

    @pytest.mark.asyncio
    async def test_the_way_back_s_refusal_names_who_stands_there_and_not_itself(self, tmp_path, monkeypatch, swaps):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        standing, older_on_b, older_done = await self._an_older_request_on_the_borrowed_alias(b)
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            older_on_b.set()
            await asyncio.sleep(0.02)
            back = asyncio.ensure_future(a.ensure_running())
            await asyncio.sleep(0.05)
            clock.now += 61

            with pytest.raises(CardQueueRefusal) as raised:
                await asyncio.wait_for(back, 5)

            message = str(raised.value)
            assert f"alias '{B}', busy for 61 s" in message and "conversation 'older'" in message
            assert "conversation 'c'" not in message and "gave up" in message
            assert swaps == [(A, B)] and _alive(b) and _in_line() == []
        older_done.set()
        await standing

    @pytest.mark.asyncio
    async def test_a_way_back_with_a_wait_of_zero_refuses_a_busy_child_at_once(self, tmp_path, settings, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            settings.set_local_engine_swap(True, 0, 0)  # read when the way back starts
            slot = b.call_slot()
            await slot.__aenter__()
            with pytest.raises(CardQueueRefusal, match="busy for 0 s"):
                await asyncio.wait_for(a.ensure_running(), 5)
            await slot.__aexit__(None, None, None)
        assert swaps == [(A, B)] and _alive(b)

    @pytest.mark.asyncio
    async def test_a_way_back_with_a_wait_of_zero_swaps_an_idle_child(self, tmp_path, settings, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            settings.set_local_engine_swap(True, 0, 0)  # read when the way back starts

            await asyncio.wait_for(a.ensure_running(), 5)

            assert _alive(a) and _in_line() == []
        assert swaps == [(A, B), (B, A)]

    @pytest.mark.asyncio
    async def test_a_person_s_stop_ends_a_way_back_that_waits(self, tmp_path, monkeypatch, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            slot = b.call_slot()
            await slot.__aenter__()
            back = asyncio.ensure_future(a.ensure_running())
            await asyncio.sleep(0.05)
            assert not back.done()
            leave_card_queue("c")
            with pytest.raises(TakenOutOfTheCardQueue):
                await asyncio.wait_for(back, 5)
            await slot.__aexit__(None, None, None)
        assert swaps == [(A, B)]

    @pytest.mark.asyncio
    async def test_the_way_back_s_own_stop_has_the_stop_limit_whatever_is_left_of_its_bound(
        self, tmp_path, settings, monkeypatch, swaps,
    ):
        # A bound of 5 s, shorter than a launcher takes to go: its own stop must not refuse it.
        settings.set_local_engine_swap(True, 5, 0)
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        go_on = asyncio.Event()
        real_stop = b._stop_child

        async def slow_stop():
            await go_on.wait()
            await real_stop()

        b._stop_child = slow_stop
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            asking = asyncio.ensure_future(a.ensure_running())
            await _until(lambda: b._draining)
            clock.now += 59
            await asyncio.sleep(0.05)
            assert not asking.done()
            go_on.set()
            await asyncio.wait_for(asking, 5)
            assert _alive(a) and _in_line() == []
        assert swaps == [(A, B), (B, A)]

    @pytest.mark.asyncio
    async def test_a_stop_slower_than_the_stop_limit_refuses_the_way_back_and_runs_to_its_end(
        self, tmp_path, monkeypatch, swaps,
    ):
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        go_on = asyncio.Event()
        real_stop = b._stop_child

        async def slow_stop():
            await go_on.wait()
            await real_stop()

        b._stop_child = slow_stop
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            asking = asyncio.ensure_future(a.ensure_running())
            await _until(lambda: b._draining)
            clock.now += 61
            with pytest.raises(CardQueueRefusal, match="the limit of 60 s on waiting for a stop"):
                await asyncio.wait_for(asking, 5)
            go_on.set()
            await _until(lambda: not _alive(b))
            await _until(lambda: not base_mod._SWAP_STOPS)
        assert swaps[1][0] == B, "the stop went on and was said"

    @pytest.mark.asyncio
    async def test_the_switch_thrown_off_during_a_borrow_does_not_cost_the_turn(self, tmp_path, settings, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            settings.set_local_engine_swap(False, 60, 0)

            await asyncio.wait_for(a.ensure_running(), 5)

            assert _alive(a) and not _alive(b) and _in_line() == []
        assert swaps == [(A, B), (B, A)]

    @pytest.mark.asyncio
    async def test_the_way_back_is_reported_with_no_time_until_it_starts_and_then_with_its_own(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", agent_id="agent_1", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            rows = card_queue()
            assert [(r["alias"], r["agent_id"], r["give_up_at"]) for r in rows] == [(A, "agent_1", None)]
            json.dumps(rows, allow_nan=False)

            slot = b.call_slot()
            await slot.__aenter__()
            back = asyncio.ensure_future(a.ensure_running())
            await _until(lambda: card_queue()[0]["give_up_at"] is not None)
            assert 55 < card_queue()[0]["give_up_at"] - time.time() <= 60
            await slot.__aexit__(None, None, None)
            await asyncio.wait_for(back, 10)

    @pytest.mark.asyncio
    async def test_a_younger_request_in_the_vision_place_stands_behind_the_way_back(self, tmp_path, swaps):
        # It joined the description's place while that waited for a busy alias. Served first it
        # could keep the borrowed alias from the turn, and park a conversation there.
        a, b, blind, _eyes, manager = _pair(tmp_path, rounds=2)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        turn = asyncio.ensure_future(_a_picture_turn(tmp_path, manager))
        await _until(lambda: _in_line() == [B])
        younger_done = asyncio.Event()
        younger = _request(b, younger_done, agent_id="agent_2", conversation_id="chat_2")
        await _until(lambda: len(card_queue()) == 2)
        await slot.__aexit__(None, None, None)

        agent, answer = await asyncio.wait_for(turn, 10)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert DESCRIPTION in blind.saw[0]
        await _until(lambda: len(swaps) == 3)
        assert swaps == [(A, B), (B, A), (A, B)], "the younger gets its alias after the turn"
        younger_done.set()
        await younger

    @pytest.mark.asyncio
    async def test_only_the_way_back_moves_and_a_younger_place_on_the_alias_stays_behind_an_older_one(
        self, tmp_path, swaps,
    ):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        younger_done = asyncio.Event()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                asking = asyncio.ensure_future(b.ensure_running())
                await _until(lambda: _in_line() == [B])
                older = _request(g, conversation_id="older")
                await _until(lambda: _in_line() == [B, G])
                younger = _request(a, younger_done, conversation_id="younger")
                await _until(lambda: _in_line() == [B, G, A])
                await slot.__aexit__(None, None, None)
                await asyncio.wait_for(asking, 10)
            assert [r["conversation_id"] for r in card_queue()] == ["c", "older", "younger"]
            await asyncio.wait_for(a.ensure_running(), 10)
            assert _in_line() == [G, A], "the younger did not come along with the way back"
        await asyncio.wait_for(older, 10)
        assert swaps[:3] == [(A, B), (B, A), (A, G)]
        younger_done.set()
        await asyncio.wait_for(younger, 10)

    @pytest.mark.asyncio
    async def test_a_younger_request_for_the_alias_during_the_borrow_does_not_join_the_way_back(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        younger_done = asyncio.Event()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            older = _request(g, conversation_id="older")
            await _until(lambda: _in_line() == [A, G])
            younger = _request(a, younger_done, conversation_id="younger")
            await _until(lambda: _in_line() == [A, G, A])
            await asyncio.wait_for(a.ensure_running(), 10)
            assert _in_line() == [G, A]
        await asyncio.wait_for(older, 10)
        younger_done.set()
        await asyncio.wait_for(younger, 10)

    @pytest.mark.asyncio
    async def test_a_request_that_came_during_the_borrow_stands_behind_the_way_back(self, tmp_path, swaps):
        a, b, blind, eyes, manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        looking, go_on = asyncio.Event(), asyncio.Event()

        async def during():
            looking.set()
            await go_on.wait()

        eyes.during = during
        turn = asyncio.ensure_future(_a_picture_turn(tmp_path, manager))
        await asyncio.wait_for(looking.wait(), 10)

        assert [row["alias"] for row in card_queue()] == [A], "the way back is in the line, and reported"
        other_done = asyncio.Event()
        other = _request(g, other_done, conversation_id="chat_2")
        await _until(lambda: _in_line() == [A, G])
        go_on.set()

        _agent_, answer = await turn
        assert answer == "the answer" and DESCRIPTION in blind.saw[0]
        await asyncio.wait_for(_until(lambda: not other.done() and len(swaps) == 3), 10)
        assert swaps == [(A, B), (B, A), (A, G)], "the turn had its alias before the one behind it"
        other_done.set()
        await other

    @pytest.mark.asyncio
    async def test_a_place_taken_while_the_description_waited_stands_behind_the_way_back_too(self, tmp_path, swaps):
        # That place waits for this request's stand on the borrowed alias: ahead of its way back
        # it would wait for a request waiting for it.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                asking = asyncio.ensure_future(b.ensure_running())
                await _until(lambda: _in_line() == [B])
                behind = _request(g)
                await _until(lambda: _in_line() == [B, G])
                await slot.__aexit__(None, None, None)
                await asyncio.wait_for(asking, 10)
            assert _in_line() == [A, G]
            await asyncio.wait_for(a.ensure_running(), 10)
            assert swaps == [(A, B), (B, A)], "its alias first, and the one behind it after"
        await asyncio.wait_for(behind, 10)
        assert swaps == [(A, B), (B, A), (A, G)]

    @pytest.mark.asyncio
    async def test_the_later_rounds_stay_on_the_alias_and_the_next_in_line_waits_for_the_turn(self, tmp_path, swaps):
        a, _b, blind, _eyes, manager = _pair(tmp_path, rounds=3)
        await a.ensure_running()

        await _a_picture_turn(tmp_path, manager)

        assert blind.round == 3 and swaps == [(A, B), (B, A)]

    @pytest.mark.asyncio
    async def test_a_description_that_fails_after_the_stop_leaves_the_turn_working_and_is_not_tried_again(
        self, tmp_path, swaps,
    ):
        a, b, blind, eyes, manager = _pair(tmp_path, rounds=3)
        await a.ensure_running()

        async def no_load(*_a, **_k):
            raise LlamaServerError("the launcher would not start")

        b._launch = no_load
        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert all(FAILED in sent and "would not start" in sent for sent in blind.saw)
        assert eyes.asked == 1 and eyes.looks == 0, "a second try would stop the alias again each round"
        assert swaps == [(A, B)], "B never held the card, so the way back stops nothing"
        assert _alive(a)
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_turn_that_ends_during_the_borrow_leaves_no_place_and_no_stand(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            assert _in_line() == [A] and _held() == {A: 1, B: 1}
        assert _in_line() == [] and _held() == {}
        assert swaps == [(A, B)] and _alive(b)

    @pytest.mark.asyncio
    async def test_a_second_description_on_the_borrowed_alias_needs_no_swap(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
                await asyncio.wait_for(b.ensure_running(), 10)
            assert swaps == [(A, B)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["strata", "llama-server"])
    async def test_a_third_alias_during_a_borrow_is_refused_and_does_not_wait_on_the_way_back(
        self, tmp_path, swaps, engine,
    ):
        # A compaction alias that normally sits beside the turn's llama-server alias meets this while
        # the Strata child is up. `apply_compaction` absorbs it as any summariser failure.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        g = _strata(tmp_path / "g", G) if engine == "strata" else _llama(G)
        _ACTIVE_SUPERVISORS[G] = g
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
                started = time.monotonic()
                with pytest.raises(CardHeldError):
                    await g.ensure_running()
                assert time.monotonic() - started < 5
        assert swaps == [(A, B)] and _alive(b)

    @pytest.mark.asyncio
    async def test_stop_engine_on_the_alias_takes_the_way_back_and_the_turn_is_told(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            take_card_queue_place(A)
            with pytest.raises(CardQueueRefusal, match="stopped by hand"):
                await a.ensure_running()
            assert _in_line() == [] and _held() == {A: 1}

    @pytest.mark.asyncio
    async def test_stop_engine_on_the_alias_during_the_first_swap_leaves_the_line_whole(self, tmp_path, swaps):
        # A younger request stands in the description's place. When the description leaves, it goes
        # behind a way back that Stop engine has already taken: it must stay in the line.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        go_on = asyncio.Event()
        real_stop = a._stop_child

        async def slow_stop():
            await go_on.wait()
            await real_stop()

        a._stop_child = slow_stop
        younger_done = asyncio.Event()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                asking = asyncio.ensure_future(b.ensure_running())
                await _until(lambda: _in_line() == [B])
                younger = _request(b, younger_done, conversation_id="younger")
                await _until(lambda: len(card_queue()) == 2)
                await slot.__aexit__(None, None, None)
                await _until(lambda: a._draining and _in_line() == [B, A])

                take_card_queue_place(A)
                assert _in_line() == [B]
                go_on.set()
                assert await asyncio.wait_for(asking, 10) == {"total_slots": 1}, "the description went on"
            await _until(lambda: _in_line() == [])
            await asyncio.wait_for(asyncio.sleep(0), 1)
        younger_done.set()
        assert await asyncio.wait_for(younger, 10) == {"total_slots": 1}, "nobody hung outside the line"
        assert _alive(b)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marked", [True, False])
    async def test_a_request_on_an_alias_already_stopping_is_told_it_is_stopping(self, tmp_path, swaps, marked):
        # It is not a wait on itself: the alias is going for another reason.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        a._begin_swap_drain(b)
        try:
            async with card_held_for(A, "accepted_request", conversation_id="c"):
                if marked:
                    with base_mod.a_card_borrow(), pytest.raises(CardHeldError) as raised:
                        await asyncio.wait_for(b.ensure_running(), 5)
                else:
                    with pytest.raises(CardHeldError) as raised:
                        await asyncio.wait_for(b.ensure_running(), 5)
        finally:
            a._end_swap_drain()
        message = str(raised.value)
        assert f"this request is running on '{A}', which is already draining" in message
        assert "would wait on itself" not in message
        assert swaps == []

    @pytest.mark.asyncio
    async def test_a_borrow_cut_short_leaves_the_vision_child_up_and_a_later_start_reads_who_holds_the_card(
        self, tmp_path, settings, swaps,
    ):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
        assert _alive(b) and not _alive(a), "nothing stops it when the turn ends"
        assert _in_line() == [] and _held() == {}

        settings.set_local_engine_swap(False, 60, 0)
        with pytest.raises(CardHeldError, match=f"the card is held by strata alias '{B}'"):
            await asyncio.wait_for(a.ensure_running(), 5)
        assert _alive(b)

        settings.set_local_engine_swap(True, 60, 0)
        await asyncio.wait_for(a.ensure_running(), 10)
        assert swaps == [(A, B), (B, A)] and _alive(a) and not _alive(b)

    @pytest.mark.asyncio
    async def test_a_person_s_stop_takes_the_way_back(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            leave_card_queue("c")
            with pytest.raises(TakenOutOfTheCardQueue):
                await a.ensure_running()

    @pytest.mark.asyncio
    async def test_a_way_back_past_the_deadline_still_gets_the_alias_from_an_idle_borrowed_child(
        self, tmp_path, monkeypatch, swaps,
    ):
        # The deadline bounds waiting, not the description's own work.
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            clock.now += 61
            await asyncio.wait_for(a.ensure_running(), 10)
            assert _in_line() == [] and _held() == {A: 1}
        assert swaps == [(A, B), (B, A)] and _alive(a)

    @pytest.mark.asyncio
    async def test_a_way_back_failing_once_does_not_leave_the_request_waiting_for_itself(self, tmp_path, swaps):
        # The adapter's text path asks again after the native path failed for a reason of its own.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            real = a._launch

            async def fails_once(*args, **kwargs):
                a._launch = real
                raise LlamaServerError("once")

            a._launch = fails_once
            with pytest.raises(LlamaServerError, match="once"):
                await asyncio.wait_for(a.ensure_running(), 10)
            await asyncio.wait_for(a.ensure_running(), 10)
            assert _alive(a) and _in_line() == [] and _held() == {A: 1}


class TestTheDescriptionIsKeptForTheTurn:
    @pytest.mark.asyncio
    async def test_it_is_described_once_across_the_rounds_and_again_in_the_next_turn(self, tmp_path, swaps):
        a, _b, blind, eyes, manager = _pair(tmp_path, rounds=3)
        await a.ensure_running()

        await _a_picture_turn(tmp_path, manager)

        assert eyes.looks == 1 and all(DESCRIPTION in sent for sent in blind.saw)
        assert swaps == [(A, B), (B, A)]

        blind.round = 0
        await _a_picture_turn(tmp_path, manager)

        assert eyes.looks == 2, "the turn's end dropped it"
        assert swaps == [(A, B), (B, A), (A, B), (B, A)]

    @pytest.mark.asyncio
    async def test_a_refusal_is_kept_for_the_turn_too(self, tmp_path, swaps):
        strata_holder = _strata(tmp_path / "a", A)
        a, _b, blind, eyes, manager = _pair(tmp_path, rounds=3, holder=strata_holder)
        await a.ensure_running()

        await _a_picture_turn(tmp_path, manager)

        assert eyes.asked == 1 and eyes.looks == 0 and all(FAILED in sent for sent in blind.saw)
        _untouched(a)

    @pytest.mark.asyncio
    async def test_a_vendor_s_passing_failure_is_asked_again_next_round(self, tmp_path):
        # No swap stands behind it, and a retry costs what it always did.
        a, _b, blind, eyes, manager = _pair(tmp_path, rounds=2)
        answers = iter([RuntimeError("a passing 500"), {"response": DESCRIPTION, "model": "m"}])

        async def query(prompt, provider_alias=None, images=None, return_metadata=False, **_):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        manager.query = query
        _agent_, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer"
        assert FAILED in blind.saw[0] and DESCRIPTION in blind.saw[1]

    @pytest.mark.asyncio
    async def test_any_failure_met_while_a_borrow_stands_is_kept_a_vendor_s_too(self, tmp_path, swaps):
        # The code cannot tell whose it is by then, and a retry on the borrowed alias is two swaps.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
            base_mod.keep_turn_description("k", (None, "a passing 500"), failure=RuntimeError("a passing 500"))
            assert base_mod.turn_description("k") == (None, "a passing 500")

    @pytest.mark.asyncio
    async def test_the_same_bytes_are_one_description_and_another_image_in_the_turn_is_another(self, tmp_path, swaps):
        # A file re-rendered between two calls is a new picture at the same path.
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, _b, blind, eyes, manager = _pair(tmp_path, rounds=4)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        name = _render(agent)
        bytes_in_each_round = iter([b"", b"", b"re-rendered"])

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            _render(agent, name, next(bytes_in_each_round))
            return await describe_image(ctx, name)

        _tool(agent, "look", look)
        await asyncio.wait_for(agent.process("look", "chat", message_source="ui"), 30)

        assert eyes.asked == 2 and eyes.looks == 2

    @pytest.mark.asyncio
    async def test_a_description_is_pinned_by_its_asker(self, tmp_path):
        image = [{"base64": PIXEL, "mime_type": "image/png"}]
        assert base_mod.description_key(image, "x") != base_mod.description_key(image, "y")
        assert base_mod.description_key(image, "x") == base_mod.description_key(list(image), "x")


class TestAStrataHolderTheRequestDoesNotStandOn:
    """A description never stops a Strata holder (Mike's call, 2026-11-04, until the session-files
    measurement), also where the request does not stand on it: the refusal in the words of the
    Strata-holder refusal, the turn goes on, and what is parked stays. Only a call marked as a
    description: an ordinary request's swap and a person's group image are as they were."""

    PARKED = "stopping it for a description would drop its parked conversations"

    def _a_strata_holder(self, tmp_path):
        holder = _strata(tmp_path / "s", "the strata holder")
        _ACTIVE_SUPERVISORS["the strata holder"] = holder
        return holder

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", [None, A], ids=["a cloud agent", "a turn whose alias has no child"])
    async def test_a_description_is_refused_and_the_strata_holder_stays(self, tmp_path, swaps, alias):
        _a, b, _blind, _eyes, _manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()
        started = time.monotonic()
        async with card_held_for(alias, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError) as raised:
                await asyncio.wait_for(b.ensure_running(), 10)
        assert time.monotonic() - started < 5, "swap_wait_s is 60: refused at once"
        message = str(raised.value)
        assert self.PARKED in message and "strata alias 'the strata holder'" in message
        assert "would wait on itself" not in message, "this request does not stand on it"
        assert swaps == [] and b._proc is None and _in_line() == []
        _untouched(holder)

    @pytest.mark.asyncio
    async def test_a_cloud_agent_s_turn_goes_on_with_a_failed_description(self, tmp_path, swaps):
        _a, b, _blind, eyes, manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()
        saw = []

        class Cloud(_Vendor):
            async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                          conversation_id=None, **_):
                saw.append(_text_of(messages))
                return await super().generate_with_tools(messages, tools, system, on_chunk, conversation_id)

        manager.providers["cloud"] = Cloud()
        agent = _agent(tmp_path, manager, provider_alias="cloud")

        answer = await asyncio.wait_for(
            agent.process("what is this", "chat", image_base64=PIXEL, image_mime="image/png"), 20,
        )

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert FAILED in saw[0] and self.PARKED in saw[0]
        assert eyes.asked == 1 and eyes.looks == 0
        assert swaps == [] and b._proc is None
        _untouched(holder)

    @pytest.mark.asyncio
    async def test_the_tool_comes_back_with_the_refusal_and_the_strata_holder_stays(self, tmp_path, swaps):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        _a, b, _blind, _eyes, manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()
        cloud = _Vendor()
        manager.providers["cloud"] = cloud
        agent = _agent(tmp_path, manager, provider_alias="cloud")
        name = _render(agent)
        service = SimpleNamespace(llm_manager=manager)

        async with card_held_for(None, "accepted_request", conversation_id="c"):
            answer = await describe_image(ToolContext(agent_root=agent.agent_root, dpc_service=service), name)

        assert answer.startswith("⚠️ Vision query failed:") and self.PARKED in answer
        assert swaps == [] and b._proc is None
        _untouched(holder)

    @pytest.mark.asyncio
    async def test_a_strata_alias_that_took_the_card_while_the_description_waited_is_not_stopped_either(
        self, tmp_path, swaps,
    ):
        # In line behind the Strata alias, with a llama-server child on the card when it asked.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        s = self._a_strata_holder(tmp_path)
        await a.ensure_running()
        slot = a.call_slot()
        await slot.__aenter__()
        waiting = _request(s, conversation_id="owner on strata")
        await _until(lambda: _in_line() == ["the strata holder"])

        async def a_cloud_description():
            async with card_held_for(None, "accepted_request", conversation_id="cloud"):
                with base_mod.a_card_borrow():
                    await b.ensure_running()

        asking = _another_request(a_cloud_description())
        await _until(lambda: _in_line() == ["the strata holder", B])
        await slot.__aexit__(None, None, None)
        await asyncio.wait_for(waiting, 10)

        with pytest.raises(CardHeldError, match=self.PARKED):
            await asyncio.wait_for(asking, 10)
        assert swaps == [(A, "the strata holder")] and b._proc is None
        _untouched(s)

    @pytest.mark.asyncio
    async def test_with_a_strata_holder_on_the_card_it_does_not_wait_in_line_to_be_refused(self, tmp_path, swaps):
        _a, b, _blind, _eyes, _manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        g = _strata(tmp_path / "g", G)
        _ACTIVE_SUPERVISORS[G] = g
        await holder.ensure_running()
        slot = holder.call_slot()
        await slot.__aenter__()
        waiting = _request(g, conversation_id="owner")
        await _until(lambda: _in_line() == [G])
        started = time.monotonic()
        async with card_held_for(None, "accepted_request", conversation_id="cloud"):
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError, match=self.PARKED):
                await asyncio.wait_for(b.ensure_running(), 10)
        assert time.monotonic() - started < 5 and _in_line() == [G]
        await slot.__aexit__(None, None, None)
        await asyncio.wait_for(waiting, 10)

    @pytest.mark.asyncio
    async def test_a_description_asking_for_the_alias_it_gave_up_still_takes_the_way_back(self, tmp_path, swaps):
        # The holder is Strata, and this is the request's own stand, which the rule does not refuse.
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(A, "accepted_request", conversation_id="c"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
                await asyncio.wait_for(a.ensure_running(), 10)
        assert swaps == [(A, B), (B, A)] and _alive(a)

    @pytest.mark.asyncio
    async def test_a_description_still_swaps_out_an_idle_llama_server_child_it_does_not_stand_on(self, tmp_path, swaps):
        a, b, _blind, _eyes, _manager = _pair(tmp_path)
        await a.ensure_running()
        async with card_held_for(None, "accepted_request", conversation_id="cloud"):
            with base_mod.a_card_borrow():
                await asyncio.wait_for(b.ensure_running(), 10)
        assert swaps == [(A, B)], "DROPS_PARKED_ON_STOP decides"

    @pytest.mark.asyncio
    async def test_with_the_swap_off_the_refusal_is_today_s(self, tmp_path, settings, swaps):
        settings.set_local_engine_swap(False, 300)
        _a, b, _blind, _eyes, _manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()
        async with card_held_for(None, "accepted_request", conversation_id="cloud"):
            with base_mod.a_card_borrow(), pytest.raises(CardHeldError, match="swapping is off") as raised:
                await asyncio.wait_for(b.ensure_running(), 5)
        assert self.PARKED not in str(raised.value)
        _untouched(holder)

    @pytest.mark.asyncio
    async def test_an_ordinary_request_still_swaps_out_an_idle_strata_holder(self, tmp_path, swaps):
        _a, b, _blind, _eyes, _manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()
        async with card_held_for(None, "accepted_request", conversation_id="cloud"):
            await asyncio.wait_for(b.ensure_running(), 10)
        assert swaps == [("the strata holder", B)] and base_mod.last_swap()["parked_dropped"] is True

    @pytest.mark.asyncio
    async def test_a_person_s_group_image_is_no_marked_description_and_still_swaps_it_out(self, tmp_path, swaps):
        # Not touched by the call: what a group image does in this case, written down.
        _a, b, _blind, _eyes, manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()

        described = await asyncio.wait_for(
            CoreService._describe_image_for_agents(SimpleNamespace(llm_manager=manager), PIXEL, "image/png"), 10,
        )

        assert described == DESCRIPTION
        assert swaps == [("the strata holder", B)] and base_mod.last_swap()["parked_dropped"] is True

    @pytest.mark.asyncio
    async def test_a_picture_turn_on_a_childless_alias_is_refused_the_look_and_its_own_round_still_swaps(
        self, tmp_path, swaps,
    ):
        # The description stops nothing; the turn's own model call is an ordinary request, not changed.
        a, b, blind, eyes, manager = _pair(tmp_path)
        holder = self._a_strata_holder(tmp_path)
        await holder.ensure_running()

        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert FAILED in blind.saw[0] and self.PARKED in blind.saw[0]
        assert eyes.looks == 0 and swaps == [("the strata holder", A)]
        assert _alive(a) and not _alive(holder)


class TestTheTool:
    """`describe_image` on the real route: a pool thread, the tool's private loop, and the
    supervisor's lifecycle back on the owner loop."""

    @pytest.mark.asyncio
    async def test_the_tool_borrows_and_the_same_question_is_answered_once_in_the_turn(self, tmp_path, swaps):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, b, blind, eyes, manager = _pair(tmp_path, rounds=4)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        (agent.agent_root).mkdir(parents=True, exist_ok=True)
        (agent.agent_root / "render.png").write_bytes(base64.b64decode(PIXEL))
        answers = []
        questions = iter(["what is it", "what is it", "which colour"])

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            answer = await describe_image(ctx, "render.png", question=next(questions))
            answers.append(answer)
            return answer

        _tool(agent, "look", look)
        answer = await asyncio.wait_for(agent.process("look at your render", "chat", message_source="ui"), 30)

        assert answer == "the answer"
        assert [json.loads(text)["description"] for text in answers] == [DESCRIPTION] * 3
        assert eyes.looks == 2, "the same image and question once; another question is another look"
        assert swaps == [(A, B), (B, A), (A, B), (B, A)]
        assert _alive(a) and not _alive(b)

    @pytest.mark.asyncio
    async def test_a_refused_look_comes_back_as_the_tool_s_failure_and_the_turn_goes_on(self, tmp_path, swaps):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        strata_holder = _strata(tmp_path / "a", A)
        a, _b, blind, eyes, manager = _pair(tmp_path, rounds=2, holder=strata_holder)
        blind.tool = "look"
        await a.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias=A)
        (agent.agent_root).mkdir(parents=True, exist_ok=True)
        (agent.agent_root / "render.png").write_bytes(base64.b64decode(PIXEL))
        answers = []

        async def look(ctx, n=0):
            ctx.dpc_service = SimpleNamespace(llm_manager=manager)
            answers.append(await describe_image(ctx, "render.png"))
            return answers[-1]

        _tool(agent, "look", look)
        answer = await asyncio.wait_for(agent.process("look at your render", "chat", message_source="ui"), 30)

        assert answer == "the answer"
        assert answers[0].startswith("⚠️ Vision query failed:") and "parked conversations" in answers[0]
        assert swaps == []
        _untouched(a)
        await _finish()
