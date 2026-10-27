"""A guest agent waits while its host's model loads (Mike's call, 2026-10-20).

When a guest's agent asks a peer host for inference and the host refuses with
`provider_loading` because its model is loading, the guest asks again by
itself, the person sees that the model is still loading while it waits, and
if it never comes up the person reads a notice, not an "LLM error".

`provider_unavailable` cannot mean "loading": the same word covers a card held
by the host's other engine and a launcher stopped after an engine failure, which
no wait ends. So a loading engine has its own refusal code, `provider_loading`
(DPTP v1.9, Mike's call 2026-10-20), and the guest waits only on that. A host
that says "loading" in the prose of a `provider_unavailable` is told once and
not retried; so is a code this guest has no word for.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_protocol.protocol import (
    REFUSAL_PROVIDER_LOADING,
    REFUSAL_PROVIDER_UNAVAILABLE,
    PeerRefused,
    create_remote_inference_response,
)
from dpc_client_core.dpc_agent import llm_adapter as adapter_mod
from dpc_client_core.dpc_agent.llm_adapter import (
    CALL_PROGRESS,
    PeerStillLoading,
    ask_peer_waiting_out_a_load,
)
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler
from tests.test_p2p_coordinator import make_coordinator
from tests.test_strata_is_offered_to_peers_on_the_owners_terms import (  # noqa: F401 — autouse fixture
    ENOUGH,
    PEER,
    _answer,
    _card,
    _clean_registry,
    _door,
    _running,
    _slow_start,
    _strata,
)
from tests.test_strata_supervisor import _FakeProc

LOADING_TEXT = "'flash' is loading (the Strata launcher is starting); try again shortly"


def _loading() -> PeerRefused:
    return PeerRefused(LOADING_TEXT, code=REFUSAL_PROVIDER_LOADING)


def _held() -> PeerRefused:
    return PeerRefused(
        "'flash' is not running, and a peer's request does not start it now: the card is held "
        "by llama-server alias 'qwen27'",
        code=REFUSAL_PROVIDER_UNAVAILABLE,
    )


@pytest.fixture
def fast_wait(monkeypatch):
    """The real schedule scaled down: a step of 0.01 s and a cap of 0.05 s."""
    monkeypatch.setattr(adapter_mod, "PEER_LOADING_RETRY_EVERY_S", 0.01)
    monkeypatch.setattr(adapter_mod, "PEER_LOADING_WAIT_CAP_S", 0.05)


class _Asks:
    """A host that answers each ask with the next item: an exception is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _progress_lines():
    lines = []
    token = CALL_PROGRESS.set(lines.append)
    return lines, token


# --- the host says provider_loading only while the engine starts -------------------


class TestTheHostSaysStarting:
    @pytest.mark.asyncio
    async def test_a_starting_launcher_is_refused_with_provider_loading(self, tmp_path):
        p = _strata(tmp_path)
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {}
        coord, svc, _seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_loading"
        assert "engine_state" not in payload

    @pytest.mark.asyncio
    async def test_a_start_a_peers_request_just_began_is_starting_too(self, tmp_path, monkeypatch):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        _started, release = _slow_start(p.supervisor)
        coord, svc, _seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")
        await asyncio.sleep(0)

        assert _answer(svc)["code"] == "provider_loading"
        release.set()
        await p.supervisor.peer_start_task

    @pytest.mark.asyncio
    async def test_a_failed_launcher_stays_provider_unavailable(self, tmp_path):
        p = _strata(tmp_path)
        _running(p)
        p.supervisor.failure = {"reason": "engine died"}
        coord, svc, _seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_unavailable"

    @pytest.mark.asyncio
    async def test_too_little_vram_to_start_stays_provider_unavailable(self, tmp_path, monkeypatch):
        p = _strata(tmp_path)
        _card(monkeypatch, free=4096)
        coord, svc, _seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert "not running" in payload["error"] and payload["code"] == "provider_unavailable"

    @pytest.mark.asyncio
    async def test_the_loading_refusal_is_logged_at_info_and_the_other_at_warning(
        self, tmp_path, caplog,
    ):
        coord_logger = "dpc_client_core.p2p_coordinator"
        p = _strata(tmp_path)
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {}
        coord, _svc, _seen = _door(tmp_path, p)
        with caplog.at_level(logging.INFO, logger=coord_logger):
            await coord.handle_inference_request(PEER, "req-1", "ping")
        refused = [r for r in caplog.records
                   if r.name == coord_logger and "Peer inference refused" in r.getMessage()]
        assert [r.levelno for r in refused] == [logging.INFO]

        caplog.clear()
        second = tmp_path / "second"
        second.mkdir()
        failed = _strata(second)
        _running(failed)
        failed.supervisor.failure = {"reason": "engine died"}
        coord, _svc, _seen = _door(second, failed)
        with caplog.at_level(logging.INFO, logger=coord_logger):
            await coord.handle_inference_request(PEER, "req-1", "ping")
        refused = [r for r in caplog.records
                   if r.name == coord_logger and "Peer inference refused" in r.getMessage()]
        assert [r.levelno for r in refused] == [logging.WARNING]

    def test_the_code_rides_on_an_error_only_and_no_engine_state_exists(self):
        served = create_remote_inference_response("r", response="ok", code="provider_loading")
        refused = create_remote_inference_response("r", error="no", code="provider_loading")

        assert "code" not in served["payload"]
        assert refused["payload"]["code"] == "provider_loading"
        assert "engine_state" not in refused["payload"]
        with pytest.raises(TypeError):
            create_remote_inference_response("r", error="no", engine_state="starting")


# --- the guest's handler carries the code ---------------------------------------------


async def _settle(payload: dict) -> BaseException:
    service = SimpleNamespace(_pending_inference_requests={}, _pending_inference_chunks={})
    future = asyncio.get_running_loop().create_future()
    service._pending_inference_requests["req-1"] = future
    await RemoteInferenceResponseHandler(service).handle("peer-1", payload)
    return future.exception()


@pytest.mark.asyncio
async def test_the_handler_carries_provider_loading_on_the_exception():
    error = await _settle(create_remote_inference_response(
        "req-1", error=LOADING_TEXT, code="provider_loading",
    )["payload"])

    assert isinstance(error, PeerRefused) and error.code == "provider_loading"


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [None, 1, True, ["provider_loading"]])
async def test_an_absent_or_non_string_code_is_no_code(sent):
    payload = {"request_id": "req-1", "status": "error", "error": LOADING_TEXT}
    if sent is not None:
        payload["code"] = sent

    error = await _settle(payload)

    assert isinstance(error, PeerRefused) and error.code == ""


@pytest.mark.asyncio
async def test_a_dropped_engine_state_field_beside_provider_unavailable_is_not_a_wait():
    """A host that sent the field this version replaced (`engine_state: starting`
    beside `provider_unavailable`) is read by its code alone: no wait."""
    payload = {"request_id": "req-1", "status": "error", "error": LOADING_TEXT,
               "code": "provider_unavailable", "engine_state": "starting"}

    error = await _settle(payload)

    assert isinstance(error, PeerRefused) and error.code == "provider_unavailable"
    assert not hasattr(error, "engine_state")


# --- the guest waits out a load, and nothing else -------------------------------------


@pytest.mark.asyncio
async def test_a_load_then_an_answer_is_one_answer_and_a_line_per_wait(fast_wait, caplog):
    ask = _Asks(_loading(), _loading(), {"response": "the answer"})
    lines, token = _progress_lines()
    try:
        with caplog.at_level(logging.INFO, logger=adapter_mod.__name__):
            result = await ask_peer_waiting_out_a_load(ask, "Linus")
    finally:
        CALL_PROGRESS.reset(token)

    assert result == {"response": "the answer"} and ask.calls == 3
    assert len(lines) == 2 and all("The model on Linus is loading, waiting" in l for l in lines)
    records = [r for r in caplog.records if r.name == adapter_mod.__name__]
    assert [r.levelno for r in records] == [logging.INFO, logging.INFO]
    assert all(r.exc_info is None for r in records)


@pytest.mark.asyncio
async def test_a_load_that_outlasts_the_cap_ends_in_still_loading(fast_wait, caplog):
    ask = _Asks(_loading())

    with caplog.at_level(logging.INFO, logger=adapter_mod.__name__):
        with pytest.raises(PeerStillLoading) as raised:
            await ask_peer_waiting_out_a_load(ask, "Linus")

    assert 2 <= ask.calls <= 6, "asked every step until the cap, then stopped"
    assert raised.value.host_name == "Linus" and raised.value.code == "provider_loading"
    last = [r for r in caplog.records if r.name == adapter_mod.__name__][-1]
    assert last.levelno == logging.WARNING and last.exc_info is None


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_a_load_is_raised_at_once(fast_wait):
    ask = _Asks(_held())
    lines, token = _progress_lines()
    try:
        with pytest.raises(PeerRefused) as raised:
            await ask_peer_waiting_out_a_load(ask, "Linus")
    finally:
        CALL_PROGRESS.reset(token)

    assert ask.calls == 1 and lines == []
    assert not isinstance(raised.value, PeerStillLoading)
    assert raised.value.host_name == "Linus"


@pytest.mark.asyncio
async def test_a_host_that_predates_the_code_is_not_retried_even_saying_loading(fast_wait):
    """Its prose says "loading"; the guest reads the code, never the prose."""
    ask = _Asks(PeerRefused(LOADING_TEXT, code="provider_unavailable"))

    with pytest.raises(PeerRefused):
        await ask_peer_waiting_out_a_load(ask, "Linus")

    assert ask.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", "provider_starting", "something_newer", "insufficient_quota"])
async def test_a_code_this_guest_has_no_word_for_is_read_as_absent_and_not_retried(fast_wait, code):
    """A newer host may name a loading state differently, or name another cause;
    either way the guest reads an unknown word as an absent one and tells its
    person once (glossary: refusal code)."""
    ask = _Asks(PeerRefused(LOADING_TEXT, code=code))

    with pytest.raises(PeerRefused) as raised:
        await ask_peer_waiting_out_a_load(ask, "Linus")

    assert ask.calls == 1 and not isinstance(raised.value, PeerStillLoading)


@pytest.mark.asyncio
async def test_a_cancelled_task_asks_no_more(monkeypatch):
    monkeypatch.setattr(adapter_mod, "PEER_LOADING_RETRY_EVERY_S", 0.05)
    monkeypatch.setattr(adapter_mod, "PEER_LOADING_WAIT_CAP_S", 10.0)
    ask = _Asks(_loading())

    task = asyncio.ensure_future(ask_peer_waiting_out_a_load(ask, "Linus"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    calls = ask.calls
    await asyncio.sleep(0.15)

    assert calls == 1 and ask.calls == 1


@pytest.mark.asyncio
async def test_the_agents_peer_route_waits_and_names_the_host(fast_wait):
    """Through `_chat_via_remote_peer`, with the host's name from the peer cache."""
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    adapter._llm_manager = SimpleNamespace(providers={})
    adapter._provider_alias = "peer_alias"
    adapter._token_counter = None
    adapter._default_model = "m"
    peer_id = "dpc-node-" + "b" * 32
    answers = [_loading(), {"response": "ok", "prompt_tokens": 1, "response_tokens": 1, "tokens_used": 2}]

    async def _request(**kwargs):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    service = SimpleNamespace(
        _request_inference_from_peer=_request,
        p2p_manager=SimpleNamespace(
            peers={},
            peer_cache=SimpleNamespace(get_peer=lambda nid: SimpleNamespace(display_name="Linus")),
        ),
    )
    ctx = SimpleNamespace(peer_id=peer_id, remote_model="qwen", remote_provider="qwen",
                          timeout=30, _service=service)
    lines, token = _progress_lines()
    try:
        msg, _usage = await adapter._chat_via_remote_peer(ctx, [{"role": "user", "content": "hi"}])
    finally:
        CALL_PROGRESS.reset(token)

    assert msg["content"] == "ok"
    assert len(lines) == 1 and "Linus" in lines[0]


# --- the loop: progress in the chat, a notice at the end, no LLM error ---------------


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


class _PeerLlm:
    """chat() as the adapter's peer route makes it: a wait over a host's answers."""

    def __init__(self, ask):
        self.ask = ask

    async def chat(self, messages, **kwargs):
        result = await ask_peer_waiting_out_a_load(self.ask, "Linus")
        return {"role": "assistant", "content": result["response"], "tool_calls": []}, {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0,
        }


@pytest.fixture
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


async def _run(llm, tmp_path, progress):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path,
        emit_progress=lambda msg, *a, **k: progress.append(msg),
        task_id="t1", kill_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_the_loop_shows_the_wait_and_returns_the_answer(fast_wait, _no_agent_config, tmp_path):
    progress = []
    llm = _PeerLlm(_Asks(_loading(), {"response": "the answer"}))

    text, _usage, trace = await _run(llm, tmp_path, progress)

    assert text == "the answer" and "peer_refused" not in trace
    assert sum("The model on Linus is loading, waiting" in m for m in progress) == 1


@pytest.mark.asyncio
async def test_a_load_that_never_ends_is_a_notice_not_an_llm_error(
    fast_wait, _no_agent_config, tmp_path, caplog,
):
    progress = []
    llm = _PeerLlm(_Asks(_loading()))

    with caplog.at_level(logging.INFO):
        text, _usage, trace = await _run(llm, tmp_path, progress)

    assert not text.startswith("⚠️") and "LLM error" not in text
    assert "still loading" in text and "Linus" in text
    assert trace["peer_refused"]["code"] == "provider_loading"
    assert sum("is loading, waiting" in m for m in progress) >= 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_a_load_is_a_notice_at_once(fast_wait, _no_agent_config, tmp_path, caplog):
    progress = []
    ask = _Asks(_held())

    with caplog.at_level(logging.INFO):
        text, _usage, trace = await _run(_PeerLlm(ask), tmp_path, progress)

    assert ask.calls == 1
    assert text.startswith("No answer this time: Linus did not take the request")
    assert trace["peer_refused"]["code"] == "provider_unavailable"
    assert not any("is loading, waiting" in m for m in progress)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- the guest's coordinator logs the host's answer, not a failure --------------------


@pytest.mark.asyncio
async def test_the_coordinator_logs_a_refusal_at_info_without_a_traceback(caplog):
    coord, svc = make_coordinator()

    async def _refuse(peer_id, message):
        request_id = message["payload"]["request_id"]
        svc._pending_inference_requests[request_id].set_exception(_loading())

    svc.p2p_manager.send_message_to_peer = AsyncMock(side_effect=_refuse)

    with caplog.at_level(logging.INFO, logger="dpc_client_core.p2p_coordinator"):
        with pytest.raises(PeerRefused):
            await coord.request_inference_from_peer("peer-1", "ping", timeout=5)

    records = [r for r in caplog.records if r.name == "dpc_client_core.p2p_coordinator"]
    assert records and all(r.levelno < logging.WARNING for r in records)
    assert all(r.exc_info is None for r in records)


# --- the notice closes the turn in the history, marked refused ------------------------
#
# Until 2026-10-27 it was kept out of the history, which left the person's message
# with nothing after it; now it is saved with turn_end "refused" (Mike's call).


@pytest.mark.asyncio
async def test_the_notice_is_saved_to_history_marked_refused(tmp_path):
    notice = "No answer this time: the model on Linus was still loading after 120 s."

    class FakeAgent:
        _last_usage = {}
        _last_trace = {}

        async def process(self, **kwargs):
            self._last_trace = {"peer_refused": {"code": "provider_loading", "notice": notice},
                                "turn_end": "refused"}
            return notice

    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = "agent_001"
    manager.firewall = MagicMock()
    manager.config = {}
    manager._agent = FakeAgent()
    manager._agents = {}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Ark"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = ""
    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id="dpc-node-me"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
    )
    monitor = MagicMock()
    monitor.message_history = []
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None

    result = await manager._process_message_guarded("hello", "agent_001", include_context=False)

    assert result == notice
    saved = [c.kwargs for c in monitor.add_message.call_args_list
             if c.kwargs.get("role") == "assistant"]
    assert len(saved) == 1
    assert saved[0]["content"] == notice and saved[0]["turn_end"] == "refused"
    toasts = [c for c in manager.service.local_api.broadcast_event.call_args_list
              if c.args and c.args[0] == "error_toast"]
    assert toasts == []
