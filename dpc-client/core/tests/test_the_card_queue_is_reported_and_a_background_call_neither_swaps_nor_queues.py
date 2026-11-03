"""The queue's status, and rule 10 (ADR-044, amendment 2026-11-02, the card queue).

A person whose turn reads `wait_reason: card_queue` can see who stands where and until when, in
`local_engine_status`, and a peer cannot. A background call neither swaps nor queues: it runs on
a child already on the card, or it is skipped by name. With the swap off nothing of this acts.

Real supervisors and their real `ensure_running`, with the fakes of
`test_one_local_engine_holds_the_card.py`.
"""

import asyncio
import logging
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core import local_api
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.run_gate import RunGate
from dpc_client_core.dpc_agent.skill_reflection import SkillReflector
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    BackgroundCallSkipped,
    CardHeldError,
    EngineDrainingError,
    a_background_call,
    card_held_for,
    card_holders,
    card_queue,
    last_background_skip,
)
from dpc_client_core.providers import strata_provider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

from tests.test_a_callers_run_of_calls_keeps_its_alias_busy_for_the_window import _Clock
from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    F,
    S,
    _alive,
    _another_request,
    _finish,
    _Manager,
    _untouched,
)
from tests.test_a_group_image_reports_a_refused_description_and_still_routes_the_mention import (
    _make_self,
    _png_data_url,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401
from tests.test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced import _in_line, _request, _until

CORE = Path(base_mod.__file__).resolve().parents[1]
WHAT = "skill_reflection"
NOT_ON_THE_CARD = "is not on the card"
PLACE_WAITS = f"a request is waiting in the queue for the card for '{S}'"
CONFIRMATION = "self_reflection_confirmation"
# `"card_queue"` is also a word of another list, a turn's `wait_reason`: that line is not the status.
WAIT_REASONS = re.compile(r"^TURN_WAIT_REASONS = .*$", re.M)
STATUS_NAMES = (
    r"\blocal_engine_status\b", r"\b_who_stands_on\b", r"\bcard_queue\(\)", r"\blast_background_skip\(\)",
    r"""["']card_queue["']""", r"""["']last_background_skip["']""", r"""["']card_holders["']""",
)


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def bound(settings, monkeypatch, tmp_path):
    settings.set_local_engine_swap(True, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", fake)
    return fake


def _pair(tmp_path):
    f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
    _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
    return f, s


async def _background(sup, what=WHAT):
    with a_background_call(what):
        return await sup.ensure_running()


async def _a_place_waits_on(s, holder, tmp_path):
    """`holder` up and busy, and `s`'s request standing in line for the card."""
    await holder.ensure_running()
    slot = holder.call_slot()
    await slot.__aenter__()
    start = _request(s)
    await _until(lambda: _in_line() == [s.alias])
    return start, slot


class TestTheStatus:
    @pytest.mark.asyncio
    async def test_the_line_is_reported_in_order_with_who_it_runs_for_and_both_times(self, tmp_path, clock):
        f, s = _pair(tmp_path)
        g = _strata(tmp_path / "g", "third")
        _ACTIVE_SUPERVISORS["third"] = g
        start, slot = await _a_place_waits_on(s, f, tmp_path)
        other = _request(g, agent_id="agent_1", conversation_id="chat_1")
        behind = _request(g, conversation_id="chat_2")
        await _until(lambda: len(card_queue()) == 3)
        try:
            rows = card_queue()
            assert [(r["alias"], r["agent_id"], r["conversation_id"]) for r in rows] == [
                (S, None, None), ("third", "agent_1", "chat_1"), ("third", None, "chat_2"),
            ]
            for row in rows:
                assert set(row) == {"alias", "agent_id", "conversation_id", "queued_at", "give_up_at"}
                assert abs(row["queued_at"] - time.time()) < 30, "wall clock, as last_swap's at"
                assert row["give_up_at"] - row["queued_at"] == pytest.approx(60, abs=5)
        finally:
            await slot.__aexit__(None, None, None)
            await _finish(start, other, behind)

    @pytest.mark.asyncio
    async def test_the_time_to_give_up_counts_from_acceptance_and_the_time_queued_from_the_place(
        self, tmp_path, clock,
    ):
        f, s = _pair(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        await slot.__aenter__()

        async def accepted_then_late():
            async with card_held_for(S, "accepted_request", conversation_id="chat"):
                clock.now += 20
                await s.ensure_running()

        start = _another_request(accepted_then_late())
        await _until(lambda: _in_line() == [S])
        try:
            [row] = card_queue()
            assert row["give_up_at"] - row["queued_at"] == pytest.approx(40, abs=5)
            clock.now += 10
            [later] = card_queue()
            assert later["queued_at"] == row["queued_at"], "the time it took its place does not move"
            assert later["give_up_at"] == pytest.approx(row["give_up_at"] - 10, abs=5)
        finally:
            await slot.__aexit__(None, None, None)
            await _finish(start)

    @pytest.mark.asyncio
    async def test_local_engine_status_carries_the_line_every_holder_and_the_skips(self, tmp_path):
        f, s = _pair(tmp_path)
        start, slot = await _a_place_waits_on(s, f, tmp_path)
        with pytest.raises(BackgroundCallSkipped):
            await _background(f)
        try:
            status = await strata_provider.local_engine_status()
            assert [r["alias"] for r in status["card_queue"]] == [S]
            assert status["card_holders"] == card_holders() and status["card_holders"][0]["alias"] == F
            assert status["last_background_skip"][WHAT]["alias"] == F
        finally:
            await slot.__aexit__(None, None, None)
            await _finish(start)

    def test_the_status_has_empty_keys_with_nothing_to_report(self):
        assert card_queue() == [] and card_holders() == [] and last_background_skip() == {}


class TestOnlyTheOwnerReadsIt:
    def test_the_status_is_a_command_of_the_local_api_which_listens_on_this_machine_alone(self):
        source = (CORE / "local_api.py").read_text(encoding="utf-8")
        assert re.search(r'"local_engine_status",', source)
        default_host = local_api.LocalApiServer.__init__.__defaults__[0]
        assert default_host == "127.0.0.1"
        # ... and the one server the backend makes takes that default.
        made = [made for path in CORE.rglob("*.py") if "__pycache__" not in path.parts
                for made in re.findall(r"\bLocalApiServer\([^)]*\)", path.read_text(encoding="utf-8"))]
        assert made == ["LocalApiServer(core_service=self)"]

    def test_nothing_on_a_peers_path_names_the_status_or_what_it_reads(self):
        owners = {"local_api.py", "service.py", "strata_provider.py", "llama_server_supervisor.py"}
        readers = {}
        for path in CORE.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            source = WAIT_REASONS.sub("", path.read_text(encoding="utf-8"))
            for name in STATUS_NAMES:
                if re.search(name, source):
                    readers.setdefault(name, set()).add(path.name)
        # Non-vacuous: every pattern must find its owner, or it has stopped guarding anything.
        assert set(readers) == set(STATUS_NAMES)
        assert set().union(*readers.values()) == owners


class TestABackgroundCall:
    @pytest.mark.asyncio
    async def test_on_an_alias_off_the_card_it_is_skipped_and_starts_nothing_and_touches_no_holder(self, tmp_path):
        f, s = _pair(tmp_path)
        await f.ensure_running()

        with pytest.raises(BackgroundCallSkipped) as refused:
            await asyncio.wait_for(_background(s), 5)

        assert NOT_ON_THE_CARD in str(refused.value)
        assert s._proc is None and _in_line() == []
        _untouched(f)
        assert [h["alias"] for h in card_holders()] == [F]
        row = last_background_skip()[WHAT]
        assert row["alias"] == S and NOT_ON_THE_CARD in row["reason"]
        assert abs(row["at"] - time.time()) < 30

    @pytest.mark.asyncio
    async def test_on_a_stopped_alias_with_the_card_free_it_starts_nothing(self, tmp_path):
        _f, s = _pair(tmp_path)

        async def not_reached(request):
            raise AssertionError("a start offers a missing pin to the owner, for a call nobody waits on")

        s._start_if_needed = not_reached
        with pytest.raises(BackgroundCallSkipped):
            await _background(s)
        assert s._proc is None and card_holders() == []

    @pytest.mark.asyncio
    async def test_on_a_child_already_up_it_runs_and_leaves_no_skip(self, tmp_path):
        f, _s = _pair(tmp_path)
        props = await f.ensure_running()
        assert await _background(f) == props
        assert last_background_skip() == {}

    @pytest.mark.asyncio
    async def test_while_a_place_waits_it_does_not_go_ahead_and_takes_no_place_on_its_own_alias(self, tmp_path):
        f, s = _pair(tmp_path)
        start, slot = await _a_place_waits_on(s, f, tmp_path)
        try:
            with pytest.raises(BackgroundCallSkipped) as refused:
                await asyncio.wait_for(_background(f), 5)
            assert PLACE_WAITS in str(refused.value)
            assert _in_line() == [S]
            assert last_background_skip()[WHAT]["reason"] == PLACE_WAITS
            _untouched(f)
        finally:
            await slot.__aexit__(None, None, None)
            await _finish(start)

    @pytest.mark.asyncio
    async def test_a_child_that_went_after_the_door_is_not_started_again(self):
        sup = _llama()
        spawned = []
        await sup.ensure_running()
        real_spawn = sup._spawn

        async def spawn(cmd, env):
            spawned.append(cmd)
            return await real_spawn(cmd, env)

        sup._spawn = spawn
        await sup._start_lock.acquire()  # past the door, the call waits here
        call = asyncio.create_task(_background(sup))
        await asyncio.sleep(0.02)
        sup._proc.returncode = 1  # the child is gone, and the lock lets the call on
        sup._start_lock.release()

        with pytest.raises(BackgroundCallSkipped):
            await asyncio.wait_for(call, 5)
        assert spawned == []
        assert last_background_skip()[WHAT]["alias"] == sup.alias

    @pytest.mark.asyncio
    async def test_a_model_kept_off_the_card_has_no_card_to_be_on_and_starts_as_before(self, tmp_path):
        sup = _llama("cpu", extra_args=["--device", "none"])
        await _background(sup)
        assert _alive(sup) and last_background_skip() == {}
        await sup.stop()

    @pytest.mark.asyncio
    async def test_each_call_keeps_its_own_row(self, tmp_path):
        _f, s = _pair(tmp_path)
        for what in (CONFIRMATION, WHAT):
            with pytest.raises(BackgroundCallSkipped):
                await _background(s, what)
        assert set(last_background_skip()) == {CONFIRMATION, WHAT}

    @pytest.mark.asyncio
    async def test_a_place_waiting_is_the_reason_over_a_child_that_is_down(self, tmp_path):
        f, s = _pair(tmp_path)
        third = _strata(tmp_path / "third", "third")
        start, slot = await _a_place_waits_on(s, f, tmp_path)
        try:
            with pytest.raises(BackgroundCallSkipped):
                await _background(third)
            row = last_background_skip()[WHAT]
            assert row["alias"] == "third" and row["reason"] == PLACE_WAITS
        finally:
            await slot.__aexit__(None, None, None)
            await _finish(start)

    @pytest.mark.parametrize("what, follows", [
        (CONFIRMATION, "treats this conversation as general"),
        (WHAT, "the next long task that uses a skill reflects"),
    ])
    @pytest.mark.asyncio
    async def test_a_skip_says_what_follows_for_its_own_call_and_promises_no_retry(
        self, tmp_path, caplog, what, follows,
    ):
        _f, s = _pair(tmp_path)
        with caplog.at_level(logging.INFO), pytest.raises(BackgroundCallSkipped):
            await _background(s, what)
        assert follows in last_background_skip()[what]["what_follows"]
        assert follows in caplog.text
        assert "next cycle" not in caplog.text

    def test_a_name_nobody_declared_is_refused(self):
        with pytest.raises(ValueError):
            with a_background_call("anything"):
                pass

    @pytest.mark.asyncio
    async def test_a_skip_is_a_refusal_the_providers_do_not_retry(self):
        from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider

        assert LlamaServerProvider._is_retryable(BackgroundCallSkipped("x", "a", "strata")) is False


class TestADrainThatBeganPastTheDoor:
    """The door saw a child up; the drain came after it. A skip, not an engine's failure."""

    @pytest.mark.parametrize("make", [lambda tmp_path: _llama(), lambda tmp_path: _strata(tmp_path / "s", S)],
                             ids=["llama", "strata"])
    @pytest.mark.asyncio
    async def test_the_slot_meets_the_skip_and_takes_no_call_in_flight(self, tmp_path, make):
        sup = make(tmp_path)
        await sup.ensure_running()
        with a_background_call(WHAT):
            await sup.ensure_running()
            sup._draining = True
            with pytest.raises(BackgroundCallSkipped):
                sup.call_slot()
        assert sup._in_flight == 0
        row = last_background_skip()[WHAT]
        assert row["alias"] == sup.alias and row["reason"] == f"'{sup.alias}' is stopping"

    @pytest.mark.parametrize("make", [lambda tmp_path: _llama(), lambda tmp_path: _strata(tmp_path / "s", S)],
                             ids=["llama", "strata"])
    @pytest.mark.asyncio
    async def test_behind_the_start_lock_it_meets_the_skip_too(self, tmp_path, make):
        sup = make(tmp_path)
        await sup.ensure_running()
        await sup._start_lock.acquire()
        call = asyncio.create_task(_background(sup))
        await asyncio.sleep(0.02)
        sup._draining = True
        sup._start_lock.release()
        with pytest.raises(BackgroundCallSkipped):
            await asyncio.wait_for(call, 5)
        assert last_background_skip()[WHAT]["alias"] == sup.alias

    @pytest.mark.asyncio
    async def test_through_the_real_adapter_it_is_not_logged_as_an_error(self, tmp_path, caplog):
        _f, s = _pair(tmp_path)
        await s.ensure_running()

        class _Provider:
            model = "m"
            supervisor = s

            async def generate_response(self, prompt, **_):
                await s.ensure_running()
                s._draining = True  # past the door, on the way to the slot
                try:
                    async with s.call_slot():
                        return "answer"
                except Exception as e:
                    raise RuntimeError("llamacpp_server provider failed") from e

        adapter = DpcLlmAdapter(_Manager({S: _Provider()}, default_provider=S), provider_alias=S)

        with a_background_call(WHAT), caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError) as refused:
                await adapter.chat([{"role": "user", "content": "hi"}])

        assert isinstance(refused.value.__cause__, BackgroundCallSkipped)
        assert "DPC LLM error" not in caplog.text
        assert last_background_skip()[WHAT]["alias"] == S

    @pytest.mark.asyncio
    async def test_an_unmarked_call_still_meets_the_draining_refusal(self):
        sup = _llama()
        sup._draining = True
        with pytest.raises(EngineDrainingError):
            sup.call_slot()
        assert last_background_skip() == {}

    @pytest.mark.asyncio
    async def test_a_model_kept_off_the_card_has_no_card_to_be_skipped_from(self):
        sup = _llama("cpu", extra_args=["--device", "none"])
        sup._draining = True
        with a_background_call(WHAT), pytest.raises(EngineDrainingError):
            sup.call_slot()
        assert last_background_skip() == {}


class TestWithTheSwapOff:
    """The queue lives only while `swap_when_idle` is true: all of today's answers stand."""

    @pytest.fixture(autouse=True, params=["off", "unbound"])
    def swap_off(self, request, settings, bound):
        if request.param == "off":
            settings.set_local_engine_swap(False, 60, 0)
        else:
            base_mod.bind_local_engine_settings(None)

    @pytest.mark.asyncio
    async def test_it_starts_a_stopped_alias_as_today_and_nothing_is_recorded(self, tmp_path):
        _f, s = _pair(tmp_path)
        await _background(s)
        assert _alive(s) and last_background_skip() == {}

    @pytest.mark.asyncio
    async def test_a_held_card_refuses_it_as_today_and_nothing_is_recorded(self, tmp_path):
        f, s = _pair(tmp_path)
        await f.ensure_running()
        with pytest.raises(CardHeldError) as refused:
            await _background(s)
        assert not isinstance(refused.value, BackgroundCallSkipped)
        assert f"alias '{F}'" in str(refused.value)
        assert last_background_skip() == {}
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_drain_refuses_it_as_today_and_nothing_is_recorded(self):
        sup = _llama()
        sup._draining = True
        with a_background_call(WHAT), pytest.raises(EngineDrainingError):
            sup.call_slot()
        assert last_background_skip() == {}

    def test_the_status_reports_an_empty_line(self):
        assert card_queue() == []


class _QueryThatNeedsTheEngine:
    """What `LLMManager.query` and an adapter's `chat` do on the way to a local engine, with the
    provider's wrap around a refusal."""

    def __init__(self, sup, answer="yes"):
        self.sup = sup
        self.answer = answer
        self.asked = 0
        self.default_provider = sup.alias

    async def query(self, prompt=None, **_):
        self.asked += 1
        try:
            await self.sup.ensure_running()
        except Exception as e:
            raise RuntimeError(
                f"llamacpp_server vision failed for '{self.sup.alias}': {type(e).__name__}: {e}"
            ) from e
        return self.answer

    async def chat(self, messages, tools=None):
        self.asked += 1
        await self.sup.ensure_running()
        return {"content": ""}, {}


class TestTheCallSites:
    @pytest.mark.asyncio
    async def test_the_self_reflection_confirmation_answers_general_at_once_and_logs_no_failure(
        self, tmp_path, caplog,
    ):
        f, s = _pair(tmp_path)
        await f.ensure_running()
        monitor = ConversationMonitor.__new__(ConversationMonitor)
        monitor.conversation_id = "chat"
        monitor.llm_manager = _QueryThatNeedsTheEngine(s)

        with caplog.at_level(logging.INFO):
            confirmed = await asyncio.wait_for(monitor._is_self_reflection_conversation("text"), 5)

        assert confirmed is False and s._proc is None and _in_line() == []
        _untouched(f)
        assert last_background_skip()[CONFIRMATION]["alias"] == S
        assert "background call self_reflection_confirmation skipped" in caplog.text
        assert "confirmation failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_real_failure_of_the_confirmation_is_still_logged_as_one(self, caplog):
        monitor = ConversationMonitor.__new__(ConversationMonitor)
        monitor.conversation_id = "chat"
        monitor.llm_manager = SimpleNamespace(query=AsyncMock(side_effect=RuntimeError("the model is gone")))

        with caplog.at_level(logging.WARNING):
            assert await monitor._is_self_reflection_conversation("text") is False

        assert "confirmation failed (the model is gone)" in caplog.text

    @pytest.mark.asyncio
    async def test_the_self_reflection_confirmation_runs_on_the_alias_on_the_card(self, tmp_path):
        f, _s = _pair(tmp_path)
        await f.ensure_running()
        monitor = ConversationMonitor.__new__(ConversationMonitor)
        monitor.conversation_id = "chat"
        monitor.llm_manager = _QueryThatNeedsTheEngine(f)

        assert await monitor._is_self_reflection_conversation("text") is True
        assert last_background_skip() == {}

    @pytest.mark.asyncio
    async def test_skill_reflection_is_skipped_by_name_and_starts_nothing(self, tmp_path):
        f, s = _pair(tmp_path)
        await f.ensure_running()

        class _Store:
            def load_skill_body(self, name):
                return "## A section\nbody"

            def get_stats(self):
                return {}

        reflector = SkillReflector(_Store(), llm=_QueryThatNeedsTheEngine(s))

        await asyncio.wait_for(reflector.reflect_async("skill", "task", {"tool_calls": []}, {"rounds": 9}), 5)

        assert reflector.llm.asked == 1 and s._proc is None and _in_line() == []
        _untouched(f)
        assert last_background_skip()[WHAT]["alias"] == S

    @pytest.mark.asyncio
    async def test_through_the_real_adapter_and_a_providers_wrap_a_skip_is_not_logged_as_an_error(
        self, tmp_path, caplog,
    ):
        f, s = _pair(tmp_path)
        await f.ensure_running()

        class _Provider:
            model = "m"
            supervisor = s

            async def generate_response(self, prompt, **_):
                try:
                    await s.ensure_running()
                except Exception as e:
                    raise RuntimeError("Llama.cpp server failed") from e
                return "answer"

        adapter = DpcLlmAdapter(_Manager({S: _Provider()}, default_provider=S), provider_alias=S)

        with a_background_call(WHAT), caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError) as refused:
                await adapter.chat([{"role": "user", "content": "hi"}])

        assert isinstance(refused.value.__cause__, BackgroundCallSkipped)
        assert "DPC LLM error" not in caplog.text
        assert last_background_skip()[WHAT]["alias"] == S


VOTE = '{"vote": "approve", "comment": "accurate"}'


def _a_vote_on(llm):
    proposal = SimpleNamespace(proposal_id="p1", conversation_id="ai_chat", entries=[], topic="t", summary="s",
                               avg_confidence=0.9)
    service = KnowledgeService.__new__(KnowledgeService)
    service.llm_manager = llm
    service.consensus_manager = SimpleNamespace(
        sessions={"p1": SimpleNamespace(proposal=proposal, votes={})}, node_id="me",
        cast_vote=AsyncMock(return_value=True),
    )
    return service


class TestWhatStaysARequest:
    """Automatic, unmarked and with nobody waiting at the door, and still requests: with the swap
    on each stands in line and takes the card in its turn."""

    async def _in_line_then_served(self, f, s, request):
        await f.ensure_running()
        slot = f.call_slot()
        await slot.__aenter__()
        task = _another_request(request)
        try:
            await _until(lambda: _in_line() == [S])
            assert last_background_skip() == {}
            _untouched(f)
        finally:
            await slot.__aexit__(None, None, None)
        try:
            answer = await asyncio.wait_for(task, 10)
        finally:
            await _finish(task)
        assert _alive(s) and not _alive(f)
        return answer

    @pytest.mark.asyncio
    async def test_the_vote_takes_a_place_and_votes(self, tmp_path):
        f, s = _pair(tmp_path)
        service = _a_vote_on(_QueryThatNeedsTheEngine(s, VOTE))
        await self._in_line_then_served(f, s, service._ai_agent_vote_on_proposal("p1", "ai-node"))
        service.consensus_manager.cast_vote.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_group_image_takes_a_place_and_is_described(self, tmp_path):
        f, s = _pair(tmp_path)
        service = MagicMock(llm_manager=_QueryThatNeedsTheEngine(s, "a picture"))
        described = await self._in_line_then_served(
            f, s, CoreService._describe_image_for_agents(service, "b64", "image/png"),
        )
        assert described == "a picture"

    @pytest.mark.asyncio
    async def test_a_sleep_analysis_takes_a_place_and_answers(self, tmp_path):
        f, s = _pair(tmp_path)
        archive = tmp_path / "archive.json"
        archive.write_text(json.dumps({"messages": [{"role": "user", "content": "hello"}]}), encoding="utf-8")
        digest = {"source": "group_archive", "archive_path": str(archive), "archive_file": "a"}
        finding = await self._in_line_then_served(
            f, s, sleep_pipeline._analyze_single_session(digest, tmp_path, _QueryThatNeedsTheEngine(s, '{"x": 1}')),
        )
        assert finding["x"] == 1


class TestTheVoteAndTheImageNameTheirRefusal:
    @pytest.mark.asyncio
    async def test_a_refused_vote_is_one_line_naming_the_refusal_and_the_alias_and_casts_nothing(
        self, tmp_path, settings, caplog,
    ):
        settings.set_local_engine_swap(True, 0, 0)  # no wait: a busy holder refuses it at once
        f, s = _pair(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        await slot.__aenter__()
        service = _a_vote_on(_QueryThatNeedsTheEngine(s, VOTE))

        with caplog.at_level(logging.INFO, logger="dpc_client_core.knowledge_service"):
            await asyncio.wait_for(service._ai_agent_vote_on_proposal("p1", "ai-node"), 5)
        await slot.__aexit__(None, None, None)

        said = [r for r in caplog.records if r.name == "dpc_client_core.knowledge_service"]
        assert len(said) == 1, [r.getMessage() for r in said]
        line = said[0]
        assert line.levelno == logging.WARNING and line.exc_info is None
        message = line.getMessage()
        assert f"the card queue refused alias '{S}'" in message and "cannot load" in message
        assert "could not evaluate" not in message and "Error in AI" not in message
        service.consensus_manager.cast_vote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_model_that_fails_the_vote_still_reads_as_could_not_evaluate(self, caplog):
        llm = SimpleNamespace(default_provider="d", query=AsyncMock(side_effect=RuntimeError("the model is gone")))
        service = _a_vote_on(llm)

        with caplog.at_level(logging.WARNING):
            await service._ai_agent_vote_on_proposal("p1", "ai-node")

        assert "Error in AI vote evaluation" in caplog.text and "could not evaluate" in caplog.text
        assert "the card queue refused" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_refused_group_image_reaches_the_person_in_the_refusals_own_words(
        self, tmp_path, settings,
    ):
        settings.set_local_engine_swap(True, 0, 0)
        f, s = _pair(tmp_path)
        await f.ensure_running()
        slot = f.call_slot()
        await slot.__aenter__()
        service, _monitor = _make_self(tmp_path)
        service.llm_manager = _QueryThatNeedsTheEngine(s)
        service._describe_image_for_agents = lambda *args: CoreService._describe_image_for_agents(service, *args)

        result = await CoreService.send_group_image(
            service, "group-test", _png_data_url(), "shot.png", "caption", describe_for_agents=True,
        )
        await slot.__aexit__(None, None, None)

        assert result["status"] == "success" and result["vl_description_status"] == "failed"
        [warning] = result["warnings"]
        assert "cannot load" in warning and f"'{F}'" in warning
        assert "vision failed" not in warning and "CardQueueRefusal" not in warning


class TestTheTurn:
    @pytest.mark.asyncio
    async def test_a_mark_left_by_passes_leaves_a_model_call_set_in_between(self):
        """Johnny's case: `passes` is the happy path of every request that waited."""
        gate = RunGate(agent_id="agent_x")
        place = base_mod._CardPlace(S)
        async with gate.hold({"conversation_id": "c"}):
            request = base_mod._QueuedRequest(place)
            place.requests.append(request)
            base_mod._CARD_QUEUE.append(place)
            request.says_it_waits()
            assert gate.turn["wait_reason"] == "card_queue"
            gate.update_turn(wait_reason="model_call")

            request.passes()

            assert gate.turn["wait_reason"] == "model_call"
            assert _in_line() == []
