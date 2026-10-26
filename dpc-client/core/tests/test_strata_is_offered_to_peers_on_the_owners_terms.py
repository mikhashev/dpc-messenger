"""Strata is offered to peers on the owner's terms (ADR-044 point 9).

Two decisions, Mike's call 2026-10-18:

* the alias a peer request naming none is served from is set explicitly
  (`compute.serving_alias`, one of `serving_local`); absent, the first entry
  is taken as before and the P2P door says once that it was list order;
* a Strata alias answers a peer 503 (`provider_loading` while it loads, DPTP
  v1.10; `provider_unavailable`) while it is not running. The answer is not
  retried by this node, and a `llama-server` alias is untouched by the Strata
  policy.

And one more, Mike's call 2026-10-20: a peer's request may begin the start of a
stopped launcher in the background — once, when the other engine does not hold
the card and the card's free VRAM is enough — and is answered "loading"
meanwhile. A failed launcher stays the owner's to reset.

And Mike's call of the same day, "let peers in": a peer is served while this
node's own conversations stay parked and while an owner call runs, because
every call served to a peer goes out with Strata's checkpoint switch off
(`strata_checkpoint: false`, the engine's `ckpt=0`) and so parks nothing. An
owner call is sent as before.

The door is checked twice over. `TestTheWholePath` runs a peer request through
a real `ContextFirewall` read from a rules file and a real `StrataProvider`
down to its `call_slot`; only the HTTP client under the provider is a stand-in.
The unit classes above it stub the firewall gate (`can_request_inference`) to
True, each with the reason in a comment: they test what comes after the gate,
and the gate itself is proven by the whole-path class.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import CardHeldError, LlamaServerSupervisor
from dpc_client_core.managers.strata_supervisor import (
    PeerStartRefused,
    peer_being_served,
    serving_a_peer,
)
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers import strata_provider as strata_mod
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from tests.test_p2p_coordinator import make_coordinator
from tests.test_strata_supervisor import _FakeProc, _install
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (  # noqa: E501
    _rows,
)

PEER = "peer-1"


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    # No test reads the real card: unreadable unless a test says otherwise,
    # which refuses a peer's start.
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: None)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    _ACTIVE_SUPERVISORS.clear()
    base_mod._CARD_CLAIMS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()
    base_mod._CARD_CLAIMS.clear()


def _strata(tmp_path) -> StrataProvider:
    return StrataProvider("flash", {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
    })


def _running(provider: StrataProvider, cache=None):
    """A launcher up and answering. `cache` is what its /metrics would report;
    the peer policy no longer reads it, and the tests say so."""
    sup = provider.supervisor
    sup._proc = _FakeProc()
    sup.props = {"modalities": {"vision": False}}
    metrics = {"conversation_cache": cache} if cache is not None else {}
    sup._get = AsyncMock(return_value=metrics)
    return sup


async def _owner_has_called(sup) -> None:
    """One owner call through the real slot, so what is parked can be ours."""
    async with sup.call_slot():
        pass


def _firewall(tmp_path, compute: dict) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    return ContextFirewall(rules)


PARKED = {"enabled": True, "budget_mib": 10240, "slots": 4, "parked": 1, "bytes": 3 << 30}
EMPTY = {"enabled": True, "budget_mib": 10240, "slots": 4, "parked": 0, "bytes": 0}


# --- the explicit upstream ------------------------------------------------------------


class TestTheDefaultIsChosenNotInferred:
    def test_an_explicit_serving_alias_wins_over_list_order(self, tmp_path):
        fw = _firewall(tmp_path, {"serving_local": ["llama", "flash"], "serving_alias": "flash"})
        assert fw.compute_serving_alias == "flash"
        assert fw.compute_serving_default == "flash"
        assert fw.compute_serving_local == ["llama", "flash"]

    def test_absent_it_is_the_first_entry_and_nothing_is_chosen(self, tmp_path):
        fw = _firewall(tmp_path, {"serving_local": ["llama", "flash"]})
        assert fw.compute_serving_alias == "llama"
        assert fw.compute_serving_default is None

    def test_one_outside_the_list_or_not_a_string_is_refused_on_save(self):
        ok, errors = ContextFirewall.validate_config(
            {"compute": {"serving_local": ["llama"], "serving_alias": "flash"}})
        assert ok is False and any("serving_alias" in e and "flash" in e for e in errors)
        ok, errors = ContextFirewall.validate_config(
            {"compute": {"serving_local": ["llama"], "serving_alias": 3}})
        assert ok is False and any("serving_alias" in e for e in errors)

    @pytest.mark.asyncio
    async def test_the_door_says_once_that_the_default_was_list_order(self, caplog):
        coord, svc = make_coordinator()
        # Gate stubbed: the line under test is written after it, from the
        # serving fields set below; the real gate is TestTheWholePath's.
        svc.firewall.can_request_inference.return_value = True
        svc.firewall.compute_serving_default = None
        svc.firewall.compute_serving_local = ["llama", "flash"]
        svc.firewall.compute_serving_alias = "llama"
        svc.firewall.compute_serving_vendor = []
        svc.llm_manager.providers = {"llama": SimpleNamespace(config={"type": "llamacpp_server"})}
        svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})

        with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
            await coord.handle_inference_request(PEER, "req-1", "ping")
            await coord.handle_inference_request(PEER, "req-2", "ping")
        lines = [r for r in caplog.records if "by list order" in r.getMessage()]
        assert len(lines) == 1 and "'llama'" in lines[0].getMessage()

    @pytest.mark.asyncio
    async def test_an_explicit_default_or_a_single_entry_says_nothing(self, caplog):
        for default, local in (("flash", ["llama", "flash"]), (None, ["llama"])):
            coord, svc = make_coordinator()
            # Gate stubbed, as in the test above.
            svc.firewall.can_request_inference.return_value = True
            svc.firewall.compute_serving_default = default
            svc.firewall.compute_serving_local = local
            svc.firewall.compute_serving_alias = default or local[0]
            svc.firewall.compute_serving_vendor = []
            svc.llm_manager.providers = {}
            svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})
            with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
                await coord.handle_inference_request(PEER, "req-1", "ping")
        assert not [r for r in caplog.records if "by list order" in r.getMessage()]


# --- the policy on the provider -------------------------------------------------------


class TestThePeerPolicy:
    @pytest.mark.asyncio
    async def test_starting_is_refused_as_loading(self, tmp_path):
        p = _strata(tmp_path)
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {}
        assert "loading" in await p.peer_refusal()


    @pytest.mark.asyncio
    async def test_draining_is_refused(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        p.supervisor._draining = True
        assert "stopping" in await p.peer_refusal()

    @pytest.mark.asyncio
    async def test_owner_conversations_parked_do_not_refuse_a_peer(self, tmp_path):
        """Mike's call 2026-10-20: a peer's call parks nothing (ckpt=0), so what
        is parked is no reason to refuse it, and the cache is not even read."""
        p = _strata(tmp_path)
        sup = _running(p, cache=PARKED)
        await _owner_has_called(sup)
        assert await p.peer_refusal(PEER) is None
        sup._get.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_metrics_endpoint_does_not_refuse_a_peer(self, tmp_path):
        p = _strata(tmp_path)
        sup = _running(p)
        await _owner_has_called(sup)
        sup._get = AsyncMock(side_effect=ConnectionError("refused"))
        assert await p.peer_refusal(PEER) is None

    @pytest.mark.asyncio
    async def test_an_owner_call_running_does_not_refuse_a_peer(self, tmp_path):
        """The engine serves one request at a time; the peer's waits behind
        ours there, rather than being refused here."""
        p = _strata(tmp_path)
        _running(p, cache=PARKED)
        async with p.supervisor.call_slot():
            assert await p.peer_refusal(PEER) is None


CARD_MIB = 32623  # this machine's card, nvidia-smi memory.total
ENOUGH = CARD_MIB - 400  # the desktop's share is well under the reserve


def _card(monkeypatch, free, total=CARD_MIB):
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: free)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: total)


def _held_by_llama_server(alias="qwen27"):
    holder = LlamaServerSupervisor(alias, {"gguf_path": "D:/models/q.gguf"})
    holder._proc = _FakeProc()
    holder.props = {"total_slots": 1}
    base_mod._CARD_CLAIMS[holder] = "claimed"
    return holder


def _slow_start(sup):
    """`ensure_running` that records the mark it ran under and loads until released."""
    started, release = [], asyncio.Event()

    async def ensure_running():
        started.append(peer_being_served())
        await release.wait()
        return {}

    sup.ensure_running = ensure_running
    return started, release


def _started_lines(caplog):
    return [r for r in caplog.records if "started the launcher" in r.getMessage()]


class TestAPeerStartsAStoppedLauncher:
    """Mike's call 2026-10-20: stopped, the card not held and VRAM enough — one
    background start, and the peer told "loading"; otherwise a named refusal."""

    @pytest.mark.asyncio
    async def test_the_card_free_starts_it_once_unmarked_and_the_peer_is_told_loading(
        self, tmp_path, monkeypatch, caplog,
    ):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        started, release = _slow_start(p.supervisor)

        with caplog.at_level(logging.INFO, logger=strata_mod.__name__):
            with serving_a_peer(PEER):
                reason = await p.peer_refusal(PEER)
            await asyncio.sleep(0)
            assert "loading" in reason
            assert started == [None], "one start, run as the owner's would be, not as the peer's call"
            assert await p.peer_refusal("peer-2") == reason, "a start under way answers loading"
            assert len(started) == 1
        lines = _started_lines(caplog)
        assert len(lines) == 1 and PEER in lines[0].getMessage()
        release.set()
        await p.supervisor.peer_start_task

    @pytest.mark.asyncio
    async def test_two_concurrent_peer_requests_begin_one_start(self, tmp_path, monkeypatch, caplog):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        started, release = _slow_start(p.supervisor)

        with caplog.at_level(logging.INFO, logger=strata_mod.__name__):
            answers = await asyncio.gather(p.peer_refusal(PEER), p.peer_refusal("peer-2"))
            await asyncio.sleep(0)
        assert all("loading" in a for a in answers)
        assert len(started) == 1 and len(_started_lines(caplog)) == 1
        release.set()
        await p.supervisor.peer_start_task

    @pytest.mark.asyncio
    async def test_the_card_held_by_the_other_engine_is_refused_and_not_started(self, tmp_path, monkeypatch):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        _held_by_llama_server("qwen27")
        started, _ = _slow_start(p.supervisor)

        reason = await p.peer_refusal(PEER)
        await asyncio.sleep(0)
        assert "does not start it now" in reason
        assert "the card is held by llama-server alias 'qwen27'" in reason
        assert started == [] and p.supervisor.peer_start_task is None

    @pytest.mark.asyncio
    async def test_too_little_free_vram_is_refused_and_not_started(self, tmp_path, monkeypatch):
        p = _strata(tmp_path)
        _card(monkeypatch, free=20000)
        started, _ = _slow_start(p.supervisor)

        reason = await p.peer_refusal(PEER)
        await asyncio.sleep(0)
        needed = CARD_MIB - base_mod._DESKTOP_RESERVE_MIB
        assert f"20000 MiB of VRAM free, {needed} MiB needed" in reason
        assert started == []

    @pytest.mark.asyncio
    async def test_an_unreadable_card_is_refused_and_not_started(self, tmp_path):
        p = _strata(tmp_path)  # the fixture's card reads None
        started, _ = _slow_start(p.supervisor)
        assert "free VRAM cannot be read" in await p.peer_refusal(PEER)
        await asyncio.sleep(0)
        assert started == []

    @pytest.mark.asyncio
    async def test_a_failed_launcher_stays_refused_and_is_not_started(self, tmp_path, monkeypatch):
        """After an engine death a person resets the launcher (point 3); a free card changes nothing."""
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        p.supervisor.failure = {"reason": "engine died"}
        started, _ = _slow_start(p.supervisor)

        reason = await p.peer_refusal(PEER)
        await asyncio.sleep(0)
        assert "not running" in reason and "engine failure" in reason
        assert started == []

    @pytest.mark.asyncio
    async def test_a_failed_background_start_is_logged_and_a_later_request_may_begin_another(
        self, tmp_path, monkeypatch, caplog,
    ):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        sup = p.supervisor
        sup.ensure_running = AsyncMock(side_effect=base_mod.LlamaServerError("no venv"))

        with caplog.at_level(logging.WARNING, logger="dpc_client_core.managers.strata_supervisor"):
            assert "loading" in await p.peer_refusal(PEER)
            await sup.peer_start_task
        assert any("begun for peer peer-1 failed" in r.getMessage() for r in caplog.records)
        assert "loading" in await p.peer_refusal(PEER)
        await sup.peer_start_task
        assert sup.ensure_running.await_count == 2


class TestTheSupervisorMarks:
    @pytest.mark.asyncio
    async def test_a_peer_never_starts_the_launcher(self, tmp_path):
        p = _strata(tmp_path)
        sup = p.supervisor
        launched = []

        async def _launch(*a, **k):
            launched.append(a)
            return {}

        sup._launch = _launch
        with serving_a_peer(PEER):
            with pytest.raises(PeerStartRefused, match="does not start it"):
                await sup.ensure_running()
        assert launched == []
        assert await sup.ensure_running() == {}, "the owner's call still starts it"
        assert len(launched) == 1

    @pytest.mark.asyncio
    async def test_a_peer_slot_is_taken_beside_an_owner_slot(self, tmp_path):
        """Nothing at the slot refuses a peer any longer: the race it guarded
        (an owner call beginning after the policy) cannot cost us a park."""
        sup = _strata(tmp_path).supervisor
        async with sup.call_slot():
            with serving_a_peer(PEER):
                async with sup.call_slot():
                    assert sup._in_flight == 2
        assert sup._in_flight == 0

    def test_a_draining_launcher_refuses_any_slot(self, tmp_path):
        sup = _strata(tmp_path).supervisor
        sup._draining = True
        with serving_a_peer(PEER):
            with pytest.raises(base_mod.LlamaServerError, match="draining"):
                sup.call_slot()

    def test_a_peer_call_is_not_retried(self):
        err = RuntimeError("service unavailable: HTTP status 503")
        assert StrataProvider._is_retryable(err) is True
        with serving_a_peer(PEER):
            assert StrataProvider._is_retryable(err) is False
        assert peer_being_served() is None


# --- the door -------------------------------------------------------------------------


def _door(tmp_path, provider, alias="flash"):
    coord, svc = make_coordinator()
    # Gate stubbed: these tests are about what happens after it lets a peer in
    # (the Strata policy and the call); TestTheWholePath runs the real gate.
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = alias
    svc.firewall.compute_serving_vendor = []
    svc.llm_manager.providers = {alias: provider}
    seen = []

    async def _query(*a, **k):
        seen.append(peer_being_served())
        return {"response": "pong", "model": "m"}

    svc.llm_manager.query = AsyncMock(side_effect=_query)
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc, seen


def _answer(svc) -> dict:
    message = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert message["command"] == "REMOTE_INFERENCE_RESPONSE"
    return message["payload"]


class TestTheDoor:
    @pytest.mark.asyncio
    async def test_strata_starting_is_a_503_and_nothing_runs(self, tmp_path):
        p = _strata(tmp_path)
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {}
        coord, svc, seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["status"] == "error" and payload["code"] == "provider_loading"
        assert "loading" in payload["error"] and "retry_after_sec" not in payload
        assert seen == [] and _rows(coord._ledger) == []

    @pytest.mark.asyncio
    async def test_strata_stopped_with_too_little_vram_is_a_503_and_no_start(self, tmp_path, monkeypatch):
        p = _strata(tmp_path)
        _card(monkeypatch, free=4096)
        launched = []
        p.supervisor._launch = AsyncMock(side_effect=lambda *a, **k: launched.append(a))
        coord, svc, seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_unavailable" and "not running" in payload["error"]
        assert "4096 MiB of VRAM free" in payload["error"]
        assert seen == [] and launched == []

    @pytest.mark.asyncio
    async def test_strata_stopped_with_the_card_free_is_a_503_loading_and_one_start(
        self, tmp_path, monkeypatch, caplog,
    ):
        p = _strata(tmp_path)
        _card(monkeypatch, free=ENOUGH)
        started, release = _slow_start(p.supervisor)
        coord, svc, seen = _door(tmp_path, p)

        with caplog.at_level(logging.INFO, logger=strata_mod.__name__):
            await coord.handle_inference_request(PEER, "req-1", "ping")
            await asyncio.sleep(0)

        payload = _answer(svc)
        assert payload["code"] == "provider_loading" and "loading" in payload["error"]
        assert "retry_after_sec" not in payload
        assert seen == [] and _rows(coord._ledger) == [], "the peer's call itself never ran"
        assert started == [None]
        assert PEER in _started_lines(caplog)[0].getMessage()
        release.set()
        await p.supervisor.peer_start_task

    @pytest.mark.asyncio
    async def test_owner_conversations_parked_the_peer_is_served(self, tmp_path):
        p = _strata(tmp_path)
        await _owner_has_called(_running(p, cache=PARKED))
        coord, svc, seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["status"] == "success" and payload["response"] == "pong"
        assert seen == [PEER]

    @pytest.mark.asyncio
    async def test_a_failed_launcher_is_a_503(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        p.supervisor.failure = {"reason": "engine died"}
        coord, svc, seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_unavailable" and "engine failure" in payload["error"]
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_free_engine_serves_the_peer_under_the_peer_mark(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        coord, svc, seen = _door(tmp_path, p)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["status"] == "success" and payload["response"] == "pong"
        assert seen == [PEER]
        assert peer_being_served() is None, "the mark ends with the call"

    @pytest.mark.asyncio
    async def test_a_launcher_gone_under_the_call_is_a_503_and_not_started(self, tmp_path):
        """Past the gate, the start itself refuses a peer, and the door names it."""
        p = _strata(tmp_path)
        launched = []
        p.supervisor._launch = AsyncMock(side_effect=lambda *a, **k: launched.append(a))
        coord, svc, _ = _door(tmp_path, p)
        # The policy stubbed to "served": this test is the launcher going away
        # after the policy let the peer in, which the stub stands for.
        coord._local_engine_refusal = AsyncMock(return_value=None)

        async def _query(*a, **k):
            await p.supervisor.ensure_running()

        svc.llm_manager.query = AsyncMock(side_effect=_query)
        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_unavailable" and "does not start it" in payload["error"]
        assert launched == []

    @pytest.mark.asyncio
    async def test_a_llama_server_upstream_is_untouched(self, tmp_path):
        llama = SimpleNamespace(config={"type": "llamacpp_server"})
        coord, svc, seen = _door(tmp_path, llama, alias="llama")

        await coord.handle_inference_request(PEER, "req-1", "ping")
        assert _answer(svc)["status"] == "success" and seen == [PEER]

        class _Http503(Exception):
            status_code = 503

        svc.llm_manager.query = AsyncMock(side_effect=RuntimeError("failed"))
        svc.llm_manager.query.side_effect.__cause__ = _Http503("busy")
        await coord.handle_inference_request(PEER, "req-2", "ping")
        assert "code" not in _answer(svc), "a llama-server failure keeps its old, uncoded form"

    @pytest.mark.asyncio
    async def test_a_card_held_by_the_other_engine_is_a_503_on_a_llama_server_alias(self, tmp_path):
        """CardHeldError under a peer call, on any local engine type: not a codeless 502."""
        llama = SimpleNamespace(config={"type": "llamacpp_server"})
        coord, svc, _ = _door(tmp_path, llama, alias="llama")
        held = CardHeldError(
            "llama-server[llama] cannot load: the card is held by strata alias 'flash' (claimed)",
            "flash", "strata",
        )
        failure = RuntimeError(f"llamacpp_server provider 'llama' failed: CardHeldError: {held}")
        failure.__cause__ = held
        svc.llm_manager.query = AsyncMock(side_effect=failure)

        await coord.handle_inference_request(PEER, "req-1", "ping")

        payload = _answer(svc)
        assert payload["code"] == "provider_unavailable"
        assert "strata alias 'flash'" in payload["error"]
        assert _rows(coord._ledger) == []

    @pytest.mark.asyncio
    async def test_a_card_held_on_a_strata_alias_is_a_503(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        coord, svc, _ = _door(tmp_path, p)
        held = CardHeldError(
            "strata[flash] cannot load: the card is held by llama-server alias 'qwen27'",
            "qwen27", "llama-server",
        )
        failure = RuntimeError("wrapped")
        failure.__cause__ = held
        svc.llm_manager.query = AsyncMock(side_effect=failure)

        await coord.handle_inference_request(PEER, "req-1", "ping")
        assert _answer(svc)["code"] == "provider_unavailable"


# --- the whole path, real gate --------------------------------------------------------


class _FakeClient:
    """The HTTP client under the provider: the only stand-in on the whole path."""

    def __init__(self):
        self.calls = 0
        self.bodies = []

        async def create(**params):
            self.calls += 1
            self.bodies.append(params)
            message = SimpleNamespace(content="pong", reasoning_content=None, tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None, model_extra={},
            )

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _whole_path(tmp_path, compute: dict, provider: StrataProvider):
    """A coordinator with a real ContextFirewall read from a rules file and the
    real provider; the router is reduced to handing the call to that provider."""
    coord, svc = make_coordinator()
    svc.firewall = _firewall(tmp_path, compute)
    providers = {"flash": provider}
    svc.llm_manager = MagicMock()
    svc.llm_manager.providers = providers

    async def _route(prompt, provider_alias=None, **kwargs):
        text = await providers[provider_alias].generate_response(prompt)
        return {"response": text, "model": provider.model}

    svc.llm_manager.query = AsyncMock(side_effect=_route)
    coord._ledger = NodeLedger(tmp_path / "ledger")
    client = _FakeClient()
    provider.supervisor.port = 5000
    provider._port = 5000
    provider._openai = client
    return coord, svc, client


SERVED_TO_PEER = {"enabled": True, "allow_nodes": [PEER], "serving_local": ["flash"]}


class TestTheWholePath:
    """Peer request -> real firewall gate -> Strata policy -> provider slot -> answer."""

    @pytest.mark.asyncio
    async def test_owner_idle_the_peer_is_served_with_checkpointing_off(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        coord, svc, client = _whole_path(tmp_path, SERVED_TO_PEER, p)

        await coord.handle_inference_request(PEER, "req-1", "ping", provider="flash")

        payload = _answer(svc)
        assert payload["status"] == "success" and payload["response"] == "pong"
        assert client.calls == 1
        assert client.bodies[0]["extra_body"]["strata_checkpoint"] is False

    @pytest.mark.asyncio
    async def test_owner_conversations_parked_and_an_owner_call_running_the_peer_is_served(
        self, tmp_path,
    ):
        """The case the old rule refused all day (THE-PEER-POLICY-REFUSES-EVERY-PEER-...)."""
        p = _strata(tmp_path)
        _running(p, cache=PARKED)
        coord, svc, client = _whole_path(tmp_path, SERVED_TO_PEER, p)

        async with p.supervisor.call_slot():  # an owner call in flight
            await coord.handle_inference_request(PEER, "req-1", "ping", provider="flash")

        payload = _answer(svc)
        assert payload["status"] == "success" and payload["response"] == "pong"
        assert client.calls == 1 and len(_rows(coord._ledger)) == 1
        assert client.bodies[0]["extra_body"]["strata_checkpoint"] is False

    @pytest.mark.asyncio
    async def test_an_owner_call_on_the_same_provider_keeps_checkpointing(self, tmp_path):
        p = _strata(tmp_path)
        _running(p, cache=PARKED)
        coord, svc, client = _whole_path(tmp_path, SERVED_TO_PEER, p)

        await coord.handle_inference_request(PEER, "req-1", "ping", provider="flash")
        await p.generate_response("the owner's own question")

        peer_body, owner_body = client.bodies
        assert peer_body["extra_body"]["strata_checkpoint"] is False
        assert "strata_checkpoint" not in owner_body["extra_body"]

    @pytest.mark.asyncio
    async def test_the_gate_refuses_a_strata_alias_not_in_serving_local(self, tmp_path):
        """This is the firewall gate, not the Strata policy: an alias the owner
        does not list is refused with the gate's own word, `not_allowed`, and
        the policy is never asked."""
        p = _strata(tmp_path)
        _running(p, cache=EMPTY)
        compute = {"enabled": True, "allow_nodes": [PEER], "serving_local": ["llama"]}
        coord, svc, client = _whole_path(tmp_path, compute, p)
        assert svc.firewall.can_request_inference(PEER) is True, "the peer itself is allowed"
        p.peer_refusal = AsyncMock(side_effect=AssertionError("the policy was asked"))

        await coord.handle_inference_request(PEER, "req-1", "ping", provider="flash")

        payload = _answer(svc)
        assert payload["status"] == "error" and payload["code"] == "not_allowed"
        assert client.calls == 0


# --- every path a peer call takes goes out with checkpointing off -----------------------


def _wired(tmp_path, **overrides):
    """The real provider over a fake HTTP client that keeps every body; the
    stream path's `create` answers chunks."""
    from tests.test_llamacpp_server_provider import _FakeSupervisor, _chat_resp, _fake_client
    from tests.test_strata_provider import _config

    p = StrataProvider("flash", _config(_install(tmp_path), **overrides))
    p.supervisor = _FakeSupervisor()
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="t", arguments="{}"))
    client, completions = _fake_client(_chat_resp(tool_calls=[tc]))
    plain = completions.create

    async def chunks():
        delta = SimpleNamespace(content="ok", reasoning_content=None)
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)

    async def create(**params):
        if params.get("stream"):
            completions.bodies.append(params)
            return chunks()
        return await plain(**params)

    completions.create = create

    async def _ensure():
        return client

    p._ensure = _ensure
    return p, completions


async def _every_path(p):
    await p.generate_response("hi")
    await p.generate_response_stream("hi")
    await p.generate_with_tools(
        [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
        [{"name": "t", "description": "", "input_schema": {"type": "object"}}],
    )
    await p.generate_with_vision("what is this?", [{"mime_type": "image/png", "base64": "iVBORw0KGgo="}])


class TestEveryPeerCallParksNothing:
    """Mike's call 2026-10-20: `strata_checkpoint: false` on every call served to
    a peer (Strata serve/server.py:1216-1219 turns it into the engine's ckpt=0)."""

    @pytest.mark.asyncio
    async def test_plain_stream_tools_and_vision_carry_it_for_a_peer(self, tmp_path):
        p, completions = _wired(tmp_path)
        with serving_a_peer(PEER):
            await _every_path(p)
        assert [b.get("stream", False) for b in completions.bodies] == [False, True, False, False]
        assert [b["extra_body"].get("strata_checkpoint") for b in completions.bodies] == [False] * 4
        # The vision call with nothing asked takes the thinking-off body that
        # `_build_extra_body` never sees; it carries the switch too.
        assert completions.bodies[3]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}

    @pytest.mark.asyncio
    async def test_vision_with_a_named_word_carries_it_too(self, tmp_path):
        p, completions = _wired(tmp_path, reasoning_effort="low")
        with serving_a_peer(PEER):
            await p.generate_with_vision("what?", [{"mime_type": "image/png", "base64": "iVBORw0KGgo="}])
        body = completions.bodies[0]["extra_body"]
        assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
        assert body["strata_checkpoint"] is False

    @pytest.mark.asyncio
    async def test_an_owner_call_on_any_path_does_not(self, tmp_path):
        p, completions = _wired(tmp_path)
        await _every_path(p)
        assert len(completions.bodies) == 4
        assert all("strata_checkpoint" not in b["extra_body"] for b in completions.bodies)
