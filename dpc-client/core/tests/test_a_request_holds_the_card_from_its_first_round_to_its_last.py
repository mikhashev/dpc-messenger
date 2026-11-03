"""A request holds the card from its first round to its last (ADR-044, amendment 2026-11-02,
the card queue: rules 1, 2, 9, 14 and 16; the queue itself is in
`test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced.py`).

The replay is the 16:23-16:25 sequence of that amendment, with `swap_when_idle` forced true.
Real `DpcAgent.process`, `run_llm_loop` and adapter, real tool threads, real supervisors with
the fakes of `test_one_local_engine_holds_the_card.py`. Only the provider is a double, and it
uses a supervisor as the real ones do: `ensure_running`, then the call inside `call_slot`.
"""

import asyncio
import contextvars
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter, local_alias_of
from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.tools.registry import ToolEntry
from dpc_client_core.inference_orchestrator import InferenceOrchestrator
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import card_holders
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.settings import Settings

from tests.test_one_local_engine_holds_the_card import _fake_child, _strata, card  # noqa: F401

# The two aliases of the log.
F = "strata flash-next"
S = "qwen3.8-flash-next-iq3_s Strata"
LAST_ROUND = 6


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def swap_on(settings, tmp_path, monkeypatch):
    """HEAD with the swap forced on, as the amendment's rule 16 asks; quick polling."""
    settings.set_local_engine_swap(True, 60)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.01)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # No embedding model is loaded for the prompt's memory section.
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


def _text_of(messages) -> str:
    """The text of the Anthropic-shape turns a provider is handed."""
    out = []
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"text": content}]
        out.extend(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
    return "\n".join(out)


class _Engine:
    """A provider over a real supervisor. Each `generate_with_tools` is one round: it asks
    for the tool until `rounds` are done. `before_round[n]`, when set, is awaited before
    round n touches the supervisor, which is the gap between two calls of a loop."""

    model = "m"

    def __init__(self, supervisor, rounds=1, tool="probe"):
        self.supervisor = supervisor
        self.rounds = rounds
        self.tool = tool
        self.round = 0
        self.before_round = {}
        self.in_the_call = None
        self.saw = []  # the text of what each round was sent

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                  conversation_id=None, **_):
        self.round += 1
        n = self.round
        self.saw.append(_text_of(messages))
        if n in self.before_round:
            await self.before_round[n]()
        await self.supervisor.ensure_running()
        async with self.supervisor.call_slot():
            if self.in_the_call is not None:
                await self.in_the_call()
            usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
            if n >= self.rounds:
                return {"content": "the answer", "usage": usage}
            call = SimpleNamespace(id=f"call-{n}", name=self.tool, input={"n": n})
            return {"content": "", "tool_calls_raw": [call], "usage": usage}

    async def generate_response(self, prompt, **_):
        await self.supervisor.ensure_running()
        async with self.supervisor.call_slot():
            if self.in_the_call is not None:
                await self.in_the_call()
            return "described"


class _Vendor:
    """A vendor's alias: no supervisor, no card."""

    model = "m"
    held_during_the_call = None

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                  conversation_id=None, **_):
        self.held_during_the_call = _held()
        return {"content": "the answer", "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}

    async def generate_response(self, prompt, **_):
        self.held_during_the_call = _held()
        return "the answer"


class _Manager:
    """What the agent, its adapter and the orchestrator read of the LLMManager."""

    token_count_manager = None
    agent_provider = None
    vision_provider = None
    _alias_for_query = LLMManager._alias_for_query

    def __init__(self, providers, default_provider=None):
        self.providers = providers
        self.default_provider = default_provider

    def alias_context_window(self, alias):
        return 128000

    async def query(self, prompt, provider_alias=None, images=None, return_metadata=False, **_):
        alias = self._alias_for_query(provider_alias, bool(images))
        text = await self.providers[alias].generate_response(prompt)
        return {"response": text, "provider": alias} if return_metadata else text


def _agent(tmp_path, manager, provider_alias=F, compute_host="", name="agent_johnny") -> DpcAgent:
    agent = DpcAgent(llm_manager=manager, config=AgentConfig(max_rounds=30),
                     agent_root=tmp_path / "agents" / name, provider_alias=provider_alias,
                     compute_host=compute_host)
    _tool(agent, "probe", lambda ctx, n=0: f"step {n}")
    return agent


def _orchestrator(manager) -> InferenceOrchestrator:
    return InferenceOrchestrator(SimpleNamespace(llm_manager=manager, p2p_manager=None))


def _pair(tmp_path, rounds=LAST_ROUND):
    """Two Strata aliases and a manager over them; nothing is started."""
    f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
    _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
    engines = {F: _Engine(f, rounds=rounds), S: _Engine(s)}
    return f, s, engines, _Manager(engines)


def _alive(sup) -> bool:
    return sup._proc is not None and sup._proc.returncode is None


def _held():
    """The registry as {alias: how many requests stand on it}."""
    return {alias: len(holds) for alias, holds in base_mod._CARD_HOLDS.items()}


def _untouched(sup):
    assert _alive(sup) and sup._draining is False and sup._swap_for is None


def _another_request(coro) -> asyncio.Task:
    """`coro` as a request of its own: a task made here would inherit this one's hold and
    be taken for it, which is what rule 9 asks of a request's own tool."""
    return asyncio.get_running_loop().create_task(coro, context=contextvars.Context())


def _tool(agent, name, handler):
    agent.tools.register(ToolEntry(
        name=name,
        schema={"name": name, "description": "A test's tool.",
                "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        handler=handler,
        default_enabled=True,
    ))


class _Gap:
    """A pause between two rounds: `reached` is set when the loop gets there."""

    def __init__(self):
        self.reached = asyncio.Event()
        self.go_on = asyncio.Event()

    async def __call__(self):
        self.reached.set()
        await self.go_on.wait()


async def _finish(*tasks):
    """Nothing is left running on the test's loop, whatever the assertions said."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestTheReplay:
    @pytest.mark.asyncio
    async def test_a_swap_does_not_cut_a_loop_between_its_rounds_and_the_other_alias_waits_for_its_end(
        self, tmp_path,
    ):
        f, s, engines, manager = _pair(tmp_path)
        await s.ensure_running()  # 16:23, the other alias is on the card
        gap = _Gap()
        engines[F].before_round[LAST_ROUND] = gap

        request = asyncio.ensure_future(_agent(tmp_path, manager).process("a task", "johnny-chat"))
        other = None
        try:
            # 16:23:06 the request on F swaps S out; 16:24:04-29 its rounds run.
            await asyncio.wait_for(gap.reached.wait(), 30)
            assert engines[F].round == LAST_ROUND and _alive(f) and not _alive(s)
            swap_in = base_mod.last_swap()
            assert (swap_in["from"], swap_in["to"]) == (S, F)

            # 16:24:34, between round 5 and round 6: the owner's request on S.
            other = asyncio.ensure_future(_orchestrator(manager).execute_inference("hello", provider=S))
            await asyncio.sleep(0.15)

            seen = {
                "marked_for": f._swap_for, "draining": f._draining, "child_alive": _alive(f),
                "last_swap": base_mod.last_swap(), "other_is_done": other.done(),
            }
            assert seen == {
                "marked_for": None, "draining": False, "child_alive": True,
                "last_swap": swap_in, "other_is_done": False,
            }

            # Round 6 is served, on the engine it ran on.
            gap.go_on.set()
            assert await asyncio.wait_for(request, 30) == "the answer"
            assert engines[F].round == LAST_ROUND

            # The request on F has ended: S takes the card.
            answer = await asyncio.wait_for(other, 30)
            assert answer["response"] == "described" and answer["provider"] == S
            assert not _alive(f) and _alive(s)
            assert [h["alias"] for h in card_holders()] == [S]
            swap_back = base_mod.last_swap()
            assert (swap_back["from"], swap_back["to"]) == (F, S)
        finally:
            gap.go_on.set()
            await _finish(*[t for t in (request, other) if t is not None])

    @pytest.mark.asyncio
    async def test_the_is_stopping_refusal_gives_way_to_queueing_and_the_total_time_is_bounded(
        self, tmp_path, monkeypatch,
    ):
        # Rule 16's other two assertions, on the log's own turn of events: S is being stopped to
        # free the card for F when the owner's request on S arrives (16:25:06). It is queued and
        # not refused, F's loop runs to its end, and the card goes back once: two swaps in all.
        f, s, engines, manager = _pair(tmp_path, rounds=3)
        swaps = []
        real_note = type(f)._note_swap

        def noting(self, holder, *args, **kwargs):
            swaps.append((holder.alias, self.alias))
            real_note(self, holder, *args, **kwargs)

        monkeypatch.setattr(type(f), "_note_swap", noting)
        await s.ensure_running()
        real_stop = s._stop_child
        in_the_stop, let_it_stop = asyncio.Event(), asyncio.Event()

        async def slow_stop():
            in_the_stop.set()
            await let_it_stop.wait()
            await real_stop()

        s._stop_child = slow_stop
        request = asyncio.ensure_future(_agent(tmp_path, manager).process("a task", "johnny-chat"))
        other = None
        try:
            await asyncio.wait_for(in_the_stop.wait(), 30)
            assert s._swap_for == F

            started = time.monotonic()
            other = asyncio.ensure_future(_orchestrator(manager).execute_inference("hello", provider=S))
            await asyncio.sleep(0.05)
            assert not other.done(), "queued, and not refused with 'is stopping'"

            let_it_stop.set()
            assert await asyncio.wait_for(request, 30) == "the answer"
            answer = await asyncio.wait_for(other, 30)
            total = time.monotonic() - started
        finally:
            let_it_stop.set()
            await _finish(*[t for t in (request, other) if t is not None])

        assert answer["response"] == "described" and answer["provider"] == S
        assert swaps == [(S, F), (F, S)], "exactly two swaps: nothing traded the card back and forth"
        assert engines[F].round == 3
        assert total < 60, "within the request's one deadline (swap_wait_s is 60 here)"
        assert _alive(s) and not _alive(f)


class TestAHoldEndsHoweverTheRequestEnds:
    @pytest.mark.asyncio
    async def test_a_kill_in_the_middle_of_a_call_lets_go(self, tmp_path):
        f, s, engines, manager = _pair(tmp_path)
        in_the_call = asyncio.Event()

        async def hang():
            in_the_call.set()
            await asyncio.Event().wait()

        engines[F].in_the_call = hang
        kill = asyncio.Event()
        request = asyncio.ensure_future(
            _agent(tmp_path, manager).process("a task", "chat", kill_event=kill)
        )
        try:
            await asyncio.wait_for(in_the_call.wait(), 30)
            assert _held() == {F: 1}

            kill.set()
            await asyncio.wait_for(request, 30)

            assert _held() == {}
            assert f._in_flight == 0
            assert await asyncio.wait_for(s.ensure_running(), 10) == {"total_slots": 1}
            assert not _alive(f), "nothing stands on it any more, so it is swapped out"
        finally:
            await _finish(request)

    @pytest.mark.asyncio
    async def test_a_cancel_between_two_rounds_lets_go(self, tmp_path):
        f, s, engines, manager = _pair(tmp_path)
        gap = _Gap()
        engines[F].before_round[2] = gap
        request = asyncio.ensure_future(_agent(tmp_path, manager).process("a task", "chat"))
        try:
            await asyncio.wait_for(gap.reached.wait(), 30)
            assert _held() == {F: 1}

            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            assert _held() == {}
        finally:
            await _finish(request)

    @pytest.mark.asyncio
    async def test_an_exception_out_of_the_loop_lets_go(self, tmp_path, monkeypatch):
        _f, _s, _engines, manager = _pair(tmp_path)
        seen = []

        async def the_loop_raises(**_):
            seen.append(_held())
            raise RuntimeError("the loop broke")

        monkeypatch.setattr(agent_mod, "run_llm_loop", the_loop_raises)

        with pytest.raises(RuntimeError, match="the loop broke"):
            await _agent(tmp_path, manager).process("a task", "chat")

        assert seen == [{F: 1}]
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_a_dying_task_closed_from_another_context_still_lets_go(self):
        hold = base_mod.card_held_for(F, "accepted_request", conversation_id="c")
        await hold.__aenter__()
        assert _held() == {F: 1}

        with pytest.raises(ValueError, match="different Context"):
            await _another_request(hold.__aexit__(None, None, None))

        assert _held() == {}


class TestWhatAHoldIs:
    @pytest.mark.asyncio
    async def test_a_hold_creates_no_claim(self, tmp_path):
        f, s, _engines, _manager = _pair(tmp_path)
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            assert card_holders() == []
            assert await asyncio.wait_for(s.ensure_running(), 10) == {"total_slots": 1}
            assert f._proc is None and [h["alias"] for h in card_holders()] == [S]

    @pytest.mark.asyncio
    async def test_a_dead_child_holds_nothing_whoever_stands_on_its_alias(self, tmp_path):
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            f._proc.returncode = 137

            assert await asyncio.wait_for(s.ensure_running(), 10) == {"total_slots": 1}

            assert [h["alias"] for h in card_holders()] == [S]
            assert base_mod.last_swap() is None, "nothing was swapped out: the child had died"

    def test_a_kind_this_commit_does_not_have_is_refused(self):
        # Kinds are checked against a list, and rule 11 keeps `sleep` off it.
        async def enter():
            async with base_mod.card_held_for(F, "sleep"):
                pass

        with pytest.raises(ValueError, match="sleep"):
            asyncio.run(enter())
        assert _held() == {}

    def test_a_misspelt_name_for_the_request_is_a_type_error(self):
        async def enter():
            async with base_mod.card_held_for(F, "accepted_request", agent="agent_johnny"):
                pass

        with pytest.raises(TypeError, match="agent"):
            asyncio.run(enter())
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_the_busy_refusal_names_who_stands_on_the_alias(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 0)
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", agent_id="agent_johnny", conversation_id="johnny-chat"):
            async with base_mod.card_held_for(F, "accepted_request", conversation_id="the-owners-chat"):
                async with base_mod.card_held_for(F, "accepted_request"):
                    with pytest.raises(base_mod.CardHeldError) as refused:
                        await asyncio.wait_for(_another_request(s.ensure_running()), 10)
        assert str(refused.value).endswith(
            "serves as before. In use by agent 'agent_johnny', conversation 'johnny-chat'; "
            "conversation 'the-owners-chat'."
        )

    @pytest.mark.asyncio
    async def test_a_busy_holder_with_no_hold_names_nobody(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 0)
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with f.call_slot():
            with pytest.raises(base_mod.CardHeldError) as refused:
                await asyncio.wait_for(s.ensure_running(), 10)
        assert str(refused.value).endswith("serves as before.")

    @pytest.mark.asyncio
    async def test_a_wait_past_the_setting_refuses_and_the_holder_stays_as_it_was(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 1)
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            with pytest.raises(base_mod.CardHeldError, match="busy for 1 s"):
                await asyncio.wait_for(_another_request(s.ensure_running()), 10)
            _untouched(f)

    @pytest.mark.asyncio
    async def test_a_wait_of_zero_refuses_a_held_holder_at_once(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 0)
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            with pytest.raises(base_mod.CardHeldError, match="busy for 0 s"):
                await asyncio.wait_for(_another_request(s.ensure_running()), 10)
            _untouched(f)

    @pytest.mark.asyncio
    async def test_the_mark_comes_only_once_nothing_stands_there_with_no_await_since_the_check(
        self, tmp_path, monkeypatch,
    ):
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        real_begin = type(f)._begin_swap_drain
        at_the_mark = []

        def begin(self, other):
            at_the_mark.append((self.alias, self._in_flight, _held()))
            real_begin(self, other)

        monkeypatch.setattr(type(f), "_begin_swap_drain", begin)
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            start = _another_request(s.ensure_running())
            await asyncio.sleep(0.05)
            assert at_the_mark == [] and not start.done()
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}
        assert at_the_mark == [(F, 0, {})]

    @pytest.mark.asyncio
    async def test_a_hold_taken_while_a_start_waits_does_not_keep_it_waiting(self, tmp_path):
        # Turned over by the card queue's rule 4: a waiting start waits for what stood there when
        # it queued, no more. It read "keeps it waiting" until the queue landed.
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        first = asyncio.Event()

        async def a_request(until):
            async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
                first.set()
                await until.wait()

        end_first, end_second = asyncio.Event(), asyncio.Event()
        one = asyncio.ensure_future(a_request(end_first))
        await first.wait()
        start = asyncio.ensure_future(s.ensure_running())
        await asyncio.sleep(0.03)
        assert not start.done()
        two = asyncio.ensure_future(a_request(end_second))
        await asyncio.sleep(0.03)
        assert _held() == {F: 2}
        assert not start.done(), "the first request still stands there"

        end_first.set()
        await one
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}
        assert not _alive(f), "the later hold did not extend the holder's busy"

        end_second.set()
        await two

    @pytest.mark.asyncio
    async def test_a_holder_still_starting_is_busy_and_is_not_marked(self, tmp_path):
        f, s, _engines, _manager = _pair(tmp_path)
        _fake_child(f, health_delay=0.1)
        starting = asyncio.ensure_future(f.ensure_running())
        await asyncio.sleep(0.02)
        assert [h["state"] for h in card_holders()] == ["starting"]

        start = asyncio.ensure_future(s.ensure_running())
        await asyncio.sleep(0.03)
        assert f._draining is False and f._swap_for is None and not start.done()

        assert await asyncio.wait_for(starting, 10) == {"total_slots": 1}, "its own start is not cut"
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}
        assert not _alive(f)

    @pytest.mark.asyncio
    async def test_a_stop_by_hand_landing_during_the_wait_gives_the_waiting_start_the_card(self, tmp_path):
        # A busy holder is no longer marked, so a Stop is the first mark it gets; the start
        # that was already waiting goes on waiting and is not told "draining" (rule 12's
        # "it never waits for the dead" keeps the place; the place itself is the queue's).
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        start = asyncio.ensure_future(s.ensure_running())
        await asyncio.sleep(0.03)

        stopping = asyncio.ensure_future(base_mod.stop_by_hand(f))
        await asyncio.sleep(0.03)
        assert f._draining is True and f._swap_for is None and not start.done()

        async with slot:
            pass
        await asyncio.wait_for(stopping, 10)
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}
        assert base_mod.last_swap() is None, "someone else's stop freed it: not a swap"

    @pytest.mark.asyncio
    async def test_a_hold_survives_a_supervisor_replaced_by_a_settings_save(self, tmp_path):
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            replacement = _strata(tmp_path / "f2", F)
            replacement.supersedes(asyncio.ensure_future(f.drain(timeout=None)))
            _ACTIVE_SUPERVISORS[F] = replacement
            await replacement.ensure_running()
            assert not _alive(f) and _alive(replacement)

            start = _another_request(s.ensure_running())
            await asyncio.sleep(0.05)
            assert not start.done()
            _untouched(replacement)
            assert _held() == {F: 1}
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}
        assert not _alive(replacement)


class TestAnAliasSwitch:
    @pytest.mark.asyncio
    async def test_the_hold_follows_the_switch_and_the_old_alias_is_free_to_go(self, tmp_path):
        f, g = _strata(tmp_path / "f", F), _strata(tmp_path / "g", "the other")
        _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS["the other"] = f, g
        engines = {F: _Engine(f, rounds=2, tool="switch"), "the other": _Engine(g, rounds=1)}
        agent = _agent(tmp_path, _Manager(engines))
        _tool(agent, "switch", lambda ctx, n=0: agent.llm.set_provider_alias("the other") or "switched")
        seen = []

        async def note():
            seen.append(_held())

        engines[F].before_round[1] = note
        engines["the other"].before_round[1] = note

        answer = await asyncio.wait_for(agent.process("a task", "chat"), 30)

        assert answer == "the answer"
        assert seen == [{F: 1}, {"the other": 1}], "one alias at a time"
        assert (base_mod.last_swap()["from"], base_mod.last_swap()["to"]) == (F, "the other")
        assert not _alive(f) and _alive(g)
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_a_switch_to_a_vendor_lets_go(self, tmp_path):
        f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
        _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
        vendor = _Vendor()
        engines = {F: _Engine(f, rounds=2, tool="switch"), S: _Engine(s), "cloud": vendor}
        agent = _agent(tmp_path, _Manager(engines))
        _tool(agent, "switch", lambda ctx, n=0: agent.llm.set_provider_alias("cloud") or "switched")

        assert await asyncio.wait_for(agent.process("a task", "chat"), 30) == "the answer"

        assert vendor.held_during_the_call == {}

    @pytest.mark.asyncio
    async def test_another_adapter_s_call_inside_the_request_does_not_move_its_hold(self, tmp_path):
        # An auxiliary model (compaction, a pinned agent's sleep) runs its own adapter.
        f, _s, engines, manager = _pair(tmp_path)
        vendor = _Vendor()
        engines["cloud"] = vendor
        aux = DpcLlmAdapter(manager, provider_alias="cloud")
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            await aux.chat([{"role": "user", "content": "hello"}])
            assert vendor.held_during_the_call == {F: 1}
            assert _held() == {F: 1}


class TestFromInside:
    """Rule 9: a request holding an excluding alias that asks for another would wait on
    itself, so it is refused at once, in the form of the card-held refusal."""

    @pytest.mark.asyncio
    async def test_a_tool_on_its_thread_and_its_own_loop_is_refused_at_once(self, tmp_path):
        # The real route: a pool thread, `ToolRegistry.execute`'s private loop, and the
        # supervisor's lifecycle back on the owner loop. Waiting would take 60 s.
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        f, s, engines, manager = _pair(tmp_path, rounds=2)
        engines[F].tool = "describe"
        refused = []

        async def describe(ctx, n=0):
            try:
                await engines[S].generate_response("what is in the picture")
            except base_mod.LlamaServerError as e:
                refused.append(e)
            return "no description"

        agent = _agent(tmp_path, manager)
        _tool(agent, "describe", describe)

        assert await asyncio.wait_for(agent.process("a task", "chat"), 20) == "the answer"

        assert len(refused) == 1 and isinstance(refused[0], base_mod.CardHeldError)
        message = str(refused[0])
        assert f"strata alias '{F}'" in message and "would wait on itself" in message
        assert refused[0].holder_alias == F
        assert base_mod.last_swap() is None and s._proc is None
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_tool_asking_for_the_request_s_own_alias_is_served(self, tmp_path):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        f, _s, engines, manager = _pair(tmp_path, rounds=2)
        engines[F].tool = "describe"
        answers = []

        async def describe(ctx, n=0):
            answers.append(await engines[F].generate_response("what is in the picture"))
            return "a description"

        agent = _agent(tmp_path, manager)
        _tool(agent, "describe", describe)

        assert await asyncio.wait_for(agent.process("a task", "chat"), 20) == "the answer"
        assert answers == ["described"]
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_request_holding_no_alias_waits_like_any_start(self, tmp_path):
        # "A request holding no alias (a cloud agent) queues as a normal request."
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        async with base_mod.card_held_for(None, "accepted_request", conversation_id="cloud"):
            start = asyncio.ensure_future(s.ensure_running())
            await asyncio.sleep(0.05)
            assert not start.done() and _held() == {}
        async with slot:
            pass
        assert await asyncio.wait_for(start, 10) == {"total_slots": 1}

    @pytest.mark.asyncio
    async def test_a_tool_thread_that_outlived_its_request_speaks_for_nobody(self, tmp_path):
        # After a Kill the thread runs on with its hold: that hold moves nothing and
        # refuses nothing.
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()
        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            left_behind = base_mod.current_card_hold()

        with base_mod.carrying_card_hold(left_behind):
            base_mod.card_hold_follows(S)
            assert _held() == {}
            assert await asyncio.wait_for(s.ensure_running(), 10) == {"total_slots": 1}
        assert not _alive(f), "a live holder with nobody on it, swapped out as any other"


class _Eye(_Engine):
    """A provider that can see, over a real supervisor, as a vision provider is."""

    def __init__(self, supervisor):
        super().__init__(supervisor)
        self.looks = 0

    def supports_vision(self):
        return True

    async def generate_with_vision(self, prompt, images, **_):
        self.looks += 1
        await self.supervisor.ensure_running()
        async with self.supervisor.call_slot():
            return "a cat on a keyboard"


class _QueryingManager(_Manager):
    """`LLMManager.query` itself: the image route through `_alias_for_query`, independent of
    this machine's providers.json."""

    query = LLMManager.query

    def count_tokens(self, text, model):
        return len(text) // 4


PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
         "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
A, B = "the agent alias", "the vision alias"


def _picture_pair(tmp_path, rounds=1):
    """Agent alias A cannot see; `vision_provider` is B; each excludes the other."""
    a, b = _strata(tmp_path / "a", A), _strata(tmp_path / "b", B)
    _ACTIVE_SUPERVISORS[A], _ACTIVE_SUPERVISORS[B] = a, b
    eyes, blind = _Eye(b), _Engine(a, rounds=rounds)
    manager = _QueryingManager({A: blind, B: eyes})
    manager.vision_provider = B
    return a, b, blind, eyes, manager


async def _a_picture_turn(tmp_path, manager):
    agent = _agent(tmp_path, manager, provider_alias=A)
    answer = await asyncio.wait_for(
        agent.process("what is this", "chat", image_base64=PIXEL, image_mime="image/png"), 20,
    )
    return agent, answer


class TestAPictureOnATurnOnAnotherAlias:
    """The route to `_pre_analyze_image_for_agent`: `_chat` finds an image in the turn, the
    agent's alias cannot see, and the query goes out with no alias, so `_alias_for_query`
    takes `vision_provider`. `_pre_analyze_image_for_agent` catches what it is refused and
    the turn goes on without the look, told that it has not seen the image."""

    @pytest.mark.asyncio
    async def test_with_the_agent_on_the_card_the_look_is_refused_at_once_and_the_turn_goes_on(self, tmp_path):
        a, b, blind, eyes, manager = _picture_pair(tmp_path)
        await a.ensure_running()

        started = time.monotonic()
        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert time.monotonic() - started < 10, "swap_wait_s is 60: it did not wait on itself"
        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert eyes.looks == 1 and b._proc is None
        sent = blind.saw[0]
        assert "The visual analysis failed (" in sent and "so you have not seen it" in sent
        assert f"the card is held by strata alias '{A}' (running)" in sent
        assert "would wait on itself" in sent
        assert base_mod.last_swap() is None
        _untouched(a)

    @pytest.mark.asyncio
    async def test_the_refusal_is_asked_once_and_every_round_reads_it(self, tmp_path):
        # A is Strata here, so its description does not borrow the card (rule 9), and the refusal
        # is kept for the turn as a description would be.
        a, _b, blind, eyes, manager = _picture_pair(tmp_path, rounds=3)
        await a.ensure_running()

        _agent_, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer"
        assert eyes.looks == 1
        assert all("The visual analysis failed (" in sent for sent in blind.saw)
        _untouched(a)

    @pytest.mark.asyncio
    async def test_with_the_agent_off_the_card_the_first_round_sees_and_the_next_keeps_it(self, tmp_path):
        # The first look starts B, as nothing holds the card; A's own start then swaps the
        # idle B out; from round 2 A is on the card. The description of round 1 is kept for the
        # turn (rule 9), so round 2 does not ask, and does not meet the refusal.
        a, b, blind, eyes, manager = _picture_pair(tmp_path, rounds=2)

        _agent_, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer"
        assert all("a cat on a keyboard" in sent for sent in blind.saw)
        assert not any("The visual analysis failed (" in sent for sent in blind.saw)
        swap = base_mod.last_swap()
        assert (swap["from"], swap["to"]) == (B, A)
        assert eyes.looks == 1 and _alive(a) and not _alive(b)

    @pytest.mark.asyncio
    async def test_with_the_swap_off_the_look_meets_today_s_refusal_word_for_word(self, tmp_path, settings):
        settings.set_local_engine_swap(False, 300)
        a, b, blind, eyes, manager = _picture_pair(tmp_path)
        await a.ensure_running()

        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert answer == "the answer" and agent._last_trace.get("turn_end") is None
        assert eyes.looks == 1 and b._proc is None
        todays = (
            f"strata[{B}] cannot load: the card is held by strata alias '{A}' (running). "
            "strata and strata cannot hold the card together and DPC did not swap them here "
            f"(swapping is off, the start serves a peer, or '{A}' is already stopping). Stop '{A}' "
            f"from the providers settings to free the card, or switch this agent to '{A}'. Or tick "
            "'Swap local models when the card holder is idle' in Providers > Local engines, and DPC "
            "stops an idle holder for you."
        )
        assert todays in blind.saw[0]
        assert base_mod.last_swap() is None
        assert a._draining is False and a._swap_for is None and _alive(a)

    @pytest.mark.asyncio
    async def test_with_the_swap_off_and_the_agent_off_the_card_the_look_takes_the_card_and_the_turn_fails(
        self, tmp_path, settings,
    ):
        # Not this route's refusal: the look succeeds, and the agent's own start is what is
        # refused. Today's behaviour, kept here so that a change to it is seen.
        settings.set_local_engine_swap(False, 300)
        a, b, _blind, eyes, manager = _picture_pair(tmp_path)

        agent, answer = await _a_picture_turn(tmp_path, manager)

        assert eyes.looks == 1 and _alive(b) and a._proc is None
        assert f"the card is held by strata alias '{B}'" in answer
        assert agent._last_trace.get("turn_end") == "failed"


class TestWithTheSwapOff:
    @pytest.mark.asyncio
    async def test_a_hold_changes_nothing_and_the_refusal_is_today_s_word_for_word(self, tmp_path, settings):
        settings.set_local_engine_swap(False, 300)
        f, s, _engines, _manager = _pair(tmp_path)
        await f.ensure_running()

        with pytest.raises(base_mod.CardHeldError) as no_hold:
            await s.ensure_running()

        async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
            with pytest.raises(base_mod.CardHeldError) as holding_it:
                await asyncio.wait_for(s.ensure_running(), 5)

        assert str(holding_it.value) == str(no_hold.value)
        assert "swapping is off" in str(no_hold.value) and "Swap local models" in str(no_hold.value)
        _untouched(f)
        assert base_mod.last_swap() is None

    @pytest.mark.asyncio
    async def test_nothing_is_left_on_the_registry_after_a_request_runs(self, tmp_path, settings):
        settings.set_local_engine_swap(False, 300)
        f, _s, _engines, manager = _pair(tmp_path, rounds=2)

        assert await asyncio.wait_for(_agent(tmp_path, manager).process("a task", "chat"), 30) == "the answer"

        assert _held() == {}
        _untouched(f)


class TestARouteThatLeavesThisNode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("pinned_how", ["compute_host", "global_peer"])
    async def test_an_agent_on_a_peer_never_marks_the_alias_it_would_fall_back_to(self, tmp_path, pinned_how):
        # Muse's shape: its alias is absent here, and `_get_agent_provider_alias` falls
        # back to the node's default, which it never runs on.
        f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
        _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
        providers = {F: _Engine(f), S: _Engine(s)}
        compute_host = "dpc-node-" + "e" * 32
        if pinned_how == "global_peer":
            providers["dpc_agent"] = SimpleNamespace(peer_id=compute_host)
            compute_host = ""
        manager = _Manager(providers, default_provider=F)
        await f.ensure_running()
        agent = _agent(tmp_path, manager, provider_alias="a peer's alias", compute_host=compute_host)
        assert agent.llm._get_agent_provider_alias() == F, "the fallback this test is about"
        assert agent.llm.local_alias() is None
        during = {}

        async def chat(messages, **_):
            during["held"] = _held()
            during["start"] = await asyncio.wait_for(s.ensure_running(), 10)
            return {"content": "from the peer"}, {"prompt_tokens": 1, "completion_tokens": 1}

        agent.llm.chat = chat

        assert await asyncio.wait_for(agent.process("a task", "chat"), 30) == "from the peer"

        assert during == {"held": {}, "start": {"total_slots": 1}}
        assert not _alive(f), "it was idle: nothing stood on it"

    def test_a_vendor_alias_has_no_card(self, tmp_path):
        manager = _Manager({"cloud": _Vendor()})
        assert _agent(tmp_path, manager, provider_alias="cloud").llm.local_alias() is None

    def test_a_double_with_no_local_alias_stands_on_nothing(self):
        assert local_alias_of(SimpleNamespace()) is None


class TestTheOwnersChat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider, images, default, vision", [
        (F, None, None, None),  # the alias named
        (None, None, F, None),  # the default
        (None, [{"base64": "x", "mime_type": "image/png"}], S, F),  # the vision provider
    ])
    async def test_the_request_stands_on_the_alias_query_will_use(self, tmp_path, provider, images, default, vision):
        _f, _s, engines, manager = _pair(tmp_path)
        manager.default_provider = default
        manager.vision_provider = vision
        seen = []

        async def note():
            seen.append(_held())

        engines[F].in_the_call = note

        answer = await asyncio.wait_for(
            _orchestrator(manager).execute_inference("hello", provider=provider, images=images), 10,
        )

        assert answer["response"] == "described" and answer["compute_host"] == "local"
        assert seen == [{F: 1}]
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_a_failed_call_lets_go_and_says_what_it_said_before(self, tmp_path):
        _f, _s, engines, manager = _pair(tmp_path)

        async def broken():
            raise ValueError("the engine broke")

        engines[F].in_the_call = broken

        with pytest.raises(RuntimeError, match="Local inference failed: the engine broke"):
            await asyncio.wait_for(_orchestrator(manager).execute_inference("hello", provider=F), 10)
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_an_alias_query_refuses_is_reported_by_query_as_before(self, tmp_path):
        _f, _s, _engines, manager = _pair(tmp_path)
        with pytest.raises(RuntimeError, match="Local inference failed: Provider 'nobody' is not configured"):
            await _orchestrator(manager).execute_inference("hello", provider="nobody")
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_a_vendor_alias_stands_on_nothing(self, tmp_path):
        vendor = _Vendor()
        manager = _Manager({"cloud": vendor})
        await _orchestrator(manager).execute_inference("hello", provider="cloud")
        assert vendor.held_during_the_call == {}


class TestTheOwnerLoop:
    @pytest.mark.asyncio
    async def test_a_hold_entered_on_another_loop_is_written_on_the_owner_loop(self, monkeypatch):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        here = threading.get_ident()
        written_on = []
        real = base_mod._hold_moves_to

        def recording(hold, alias):
            written_on.append(threading.get_ident())
            real(hold, alias)

        monkeypatch.setattr(base_mod, "_hold_moves_to", recording)
        seen = []

        async def from_a_tool_s_own_loop():
            assert threading.get_ident() != here
            async with base_mod.card_held_for(F, "accepted_request", conversation_id="c"):
                await asyncio.sleep(0.05)
                seen.append(_held())
                base_mod.card_hold_follows(S)
                await asyncio.sleep(0.05)
                seen.append(_held())

        await asyncio.to_thread(asyncio.run, from_a_tool_s_own_loop())
        await asyncio.sleep(0)

        assert seen == [{F: 1}, {S: 1}]
        assert _held() == {}
        assert set(written_on) == {here}
