"""A window nobody knows is said to be unknown, and no number stands in for it.

`get_context_window` used to answer 4096 for a model no table names, and a model with
"default" in its name got 4096 from the table itself. An AI chat then refused its next
message as full; the provider list and a group header printed the number; `history.json`
kept it past a restart. The agent turn already refused plainly (`39f2f4d4`, tested in
`test_an_agent_switched_to_a_local_alias_runs_on_that_engines_window.py`); this file is
every other reader, and the reads that knew an alias and went by its model name.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import ai_chats, service as service_mod
from dpc_client_core.dpc_agent import loop as loop_mod, sleep_pipeline as sleep_mod
from dpc_client_core.dpc_agent.context import CompactionState
from dpc_client_core.dpc_agent.sleep_pipeline import run_sleep
from dpc_client_core.gateway import Gateway
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.service import CoreService
from dpc_client_core.telegram_footer import build_stats_footer

from tests.test_a_peer_routed_query_carries_only_the_context_its_node_rules_allow import (
    _FakeService,
    _Stop,
)
from tests.test_a_pinned_agents_sleep_and_snapshot_models_run_on_its_peer import (
    _conversation,
    _isolated,  # noqa: F401  (autouse: the home is tmp_path in every test here)
)
from tests.test_an_agent_switched_to_a_local_alias_runs_on_that_engines_window import (
    AGENT,
    ALIAS,
    SERVER_WINDOW,
    _alias,
    _args,
    _no_supervisors_left,  # noqa: F401
    node,  # noqa: F401
)
from tests.test_a_group_post_carries_only_a_name_this_node_registered import _Service as _PostService
from tests.test_group_payload_matches_the_record import GROUP, _Service as _GroupService
from tests.test_p2p_coordinator import make_coordinator
from tests.test_strata_supervisor import _install, home  # noqa: F401

NO_TABLE = "no-table-names-this-model"
AI_CHAT = "ai_chat_0123abcd"
REMEMBERED = 4096


def _llm(tmp_path, *rows, default=None):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": list(rows), "default_provider": default}), encoding="utf-8")
    return LLMManager(path)


def _vendor(alias="vendor", model=NO_TABLE, **fields):
    return {"alias": alias, "type": "deepseek", "model": model, "api_key": "x", **fields}


# --- The two guesses ----------------------------------------------------------------


def test_a_model_no_table_names_has_no_window(tmp_path):
    llm = _llm(tmp_path, _vendor())

    assert llm.alias_context_window("vendor") is None
    assert llm.lookup_context_window(NO_TABLE) is None


def test_no_method_is_left_that_answers_with_a_number_for_a_model_it_does_not_know(tmp_path):
    assert not hasattr(_llm(tmp_path), "get_context_window")


@pytest.mark.parametrize("model", ["my-default-model", "default", "def"])
def test_a_model_whose_name_meets_the_word_default_is_not_given_a_window_for_it(tmp_path, model):
    llm = _llm(tmp_path, _vendor(model=model))

    assert llm.alias_context_window("vendor") is None
    assert llm.lookup_context_window(model) is None


def test_a_model_the_table_does_name_still_answers(tmp_path):
    assert _llm(tmp_path, _vendor(model="gpt-4o")).alias_context_window("vendor") == 128000


# --- What each reader does with it --------------------------------------------------


@pytest.mark.asyncio
async def test_a_query_on_an_unknown_window_reports_0_and_no_number(tmp_path):
    """0 is what `monitor.set_token_limit` takes as "nothing measures against this"."""
    llm = _llm(tmp_path, _vendor(), default="vendor")
    llm.providers["vendor"].generate_response = AsyncMock(return_value="ok")

    result = await llm.query("hello", provider_alias="vendor", return_metadata=True)

    assert result["model_max_tokens"] == 0


def _rows_service(llm):
    service = CoreService.__new__(CoreService)
    service.llm_manager = llm
    service.voice_service = SimpleNamespace(_provider_supports_voice=lambda provider: False)
    return service


def test_a_provider_row_says_nothing_for_an_unknown_window(tmp_path):
    llm = _llm(tmp_path, _vendor())

    rows = _rows_service(llm)._provider_rows(list(llm.providers.items()))

    assert rows[0]["alias"] == "vendor"
    assert rows[0]["context_window"] is None


def test_a_transcription_alias_has_no_window_and_no_warning_about_it(tmp_path, caplog):
    """76 warnings in one log file named nothing wrong, one per provider list."""
    llm = _llm(tmp_path, {"alias": "whisper", "type": "local_whisper", "model": "openai/whisper-large-v3-turbo"})

    rows = _rows_service(llm)._provider_rows(list(llm.providers.items()))

    assert rows[0]["context_window"] is None
    assert "Context window" not in caplog.text


def test_two_aliases_of_one_model_each_state_their_own_on_every_row(tmp_path):
    llm = _llm(tmp_path, _vendor("big", context_window=250000), _vendor("small", context_window=8192))
    service = _rows_service(llm)

    rows = {row["alias"]: row["context_window"]
            for row in service._provider_rows(list(llm.providers.items()))}
    assert rows == {"big": 250000, "small": 8192}

    assert service.build_p2p_provider_info("small", llm.providers["small"])["context_window"] == 8192
    gateway = SimpleNamespace(_core=SimpleNamespace(llm_manager=llm))
    assert Gateway.local_context_window(gateway, "small") == 8192
    assert Gateway.local_context_window(gateway, "big") == 250000


def test_a_local_alias_states_its_engines_window_to_a_peer(tmp_path, home):  # noqa: F811
    root = _install(tmp_path, args=_args())
    llm = _llm(tmp_path, _alias(root))

    row = _rows_service(llm).build_p2p_provider_info(ALIAS, llm.providers[ALIAS])

    assert row["context_window"] == SERVER_WINDOW


def test_a_peer_row_carries_the_key_with_null_as_dptp_requires(tmp_path):
    """DPTP §3.5: `context_window` (integer or null, required)."""
    llm = _llm(tmp_path, _vendor())

    row = _rows_service(llm).build_p2p_provider_info("vendor", llm.providers["vendor"])

    assert "context_window" in row and row["context_window"] is None


def test_a_gateway_row_says_nothing_for_an_unknown_window_or_an_unknown_alias(tmp_path):
    gateway = SimpleNamespace(_core=SimpleNamespace(llm_manager=_llm(tmp_path, _vendor())))

    assert Gateway.local_context_window(gateway, "vendor") is None
    assert Gateway.local_context_window(gateway, "nobody") is None
    no_manager = SimpleNamespace(_core=SimpleNamespace(llm_manager=None))
    assert Gateway.local_context_window(no_manager, "vendor") is None


@pytest.mark.asyncio
async def test_a_served_call_on_an_unknown_window_leaves_the_field_off_the_wire():
    """Optional in DPTP §3.4: a 0 there would read as a window of no size."""
    coordinator, service = make_coordinator()
    service.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "m", "provider": "ollama_local", "tokens_used": 2, "prompt_tokens": 1,
        "response_tokens": 1, "model_max_tokens": 0, "thinking": None, "thinking_tokens": None,
    })
    service.llm_manager.providers = {"ollama_local": SimpleNamespace(model="m", config={"type": "ollama"})}
    service.firewall.compute_serving_local = ["ollama_local"]
    service.firewall.can_request_inference.return_value = True

    await coordinator.handle_inference_request("peer-1", "r1", "hello", provider="ollama_local")

    payload = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert payload.get("status") != "error", payload
    assert "model_max_tokens" not in payload


def test_the_footer_says_it_in_words_and_divides_by_nothing():
    footer = build_stats_footer({
        "tokens_after_last_response": 33533, "history_tokens": 1000, "tokens_limit": 0, "messages_count": 4,
        "window_unknown": "The context window of 'x' is not known",
    })

    assert "window not known" in footer
    assert "33,533" in footer
    assert "%" not in footer and "16,384" not in footer


# --- The agent turn -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vendor_alias_is_not_told_to_start_an_engine_it_does_not_have(node, tmp_path):  # noqa: F811
    n = node(_vendor("no-engine"))
    await n.switch("no-engine")

    text = await n.send()

    assert "no-engine" in text and "Context Window" in text
    assert "engine" not in text.replace("no-engine", "")


@pytest.mark.asyncio
async def test_a_turn_on_a_vendor_alias_with_no_window_is_refused(node, tmp_path):  # noqa: F811
    """Zcode's reading of `39f2f4d4`, checked on the path it named: the promise holds here too."""
    n = node(_vendor("no-engine"))

    assert (await n.switch("no-engine"))["context_window"] is None
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 0


def _agent_service(n, *, cfg, peer_metadata=None):
    Path(n.config_path).write_text(json.dumps(cfg), encoding="utf-8")
    n.llm.providers["dpc_agent"]._managers.clear()
    n.service.peer_metadata = peer_metadata or {}
    return n.service


def test_a_peer_pinned_agent_reads_its_hosts_number_not_a_local_alias_of_that_name(node, tmp_path):  # noqa: F811
    """The manager and the adapter go to the host first; so does the header."""
    n = node(_vendor("same", context_window=1000))
    service = _agent_service(
        n, cfg={"provider_alias": "same", "compute_host": "dpc-node-a"},
        peer_metadata={"dpc-node-a": {"providers": [{"alias": "same", "context_window": 40000}]}})

    assert service._resolve_agent_token_limit(AGENT) == 40000


def test_an_agent_whose_alias_is_gone_reads_the_alias_the_adapter_falls_back_on(node, tmp_path):  # noqa: F811
    n = node(_vendor("other"))
    service = _agent_service(n, cfg={"provider_alias": "gone", "compute_host": ""})

    assert service._resolve_agent_token_limit(AGENT) == 1000000


# --- A number on disk is not a window ------------------------------------------------


def _agent_history(tmp_path, monkeypatch, limit=REMEMBERED):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path)
    history = tmp_path / "conversations" / AGENT / "history.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({
        "conversation_id": AGENT,
        "messages": [{"id": "m1", "role": "user", "content": "x" * 400}],
        "token_stats": {"token_limit": limit, "current_token_count": 33533},
    }), encoding="utf-8")


def _core_over(n):
    core = CoreService.__new__(CoreService)
    core.conversation_monitors = {}
    core.agent_service = n.service
    return core


@pytest.mark.asyncio
async def test_the_header_after_a_restart_reads_the_alias_not_the_history_file(node, tmp_path, monkeypatch):  # noqa: F811
    root = _install(tmp_path, args=_args())
    n = node(_alias(root), agent_config={"provider_alias": ALIAS, "compute_host": ""})
    n.llm.providers["dpc_agent"]._managers.clear()
    _agent_history(tmp_path, monkeypatch)

    result = await _core_over(n).get_conversation_history(AGENT)

    assert result["token_limit"] == SERVER_WINDOW
    assert result["tokens_used"] == 33533


@pytest.mark.asyncio
async def test_the_header_of_a_window_nobody_knows_says_0_not_what_the_file_remembers(node, tmp_path, monkeypatch):  # noqa: F811
    root = _install(tmp_path, args=_args(max_context=None))
    n = node(_alias(root), agent_config={"provider_alias": ALIAS, "compute_host": ""})
    n.llm.providers["dpc_agent"]._managers.clear()
    _agent_history(tmp_path, monkeypatch)

    assert (await _core_over(n).get_conversation_history(AGENT))["token_limit"] == 0


@pytest.mark.asyncio
async def test_the_header_of_a_running_chat_reads_the_alias_that_chat_picked(node, tmp_path, monkeypatch):  # noqa: F811
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, alias="big", context_window=250000), _alias(root, alias="small", context_window=8192),
             agent_config={"provider_alias": "big", "compute_host": ""})
    n.manager._chat_aliases[AGENT] = "small"
    _agent_history(tmp_path, monkeypatch, limit=250000)

    assert (await _core_over(n).get_conversation_history(AGENT))["token_limit"] == 8192


def _knowledge_service(llm, group=None):
    ks = KnowledgeService.__new__(KnowledgeService)
    ks.conversation_monitors = {}
    ks.p2p_manager = SimpleNamespace(node_id="dpc-node-" + "b" * 32)
    ks.llm_manager = llm
    ks.settings = None
    ks.instruction_set = SimpleNamespace(default="general")
    ks.group_manager = SimpleNamespace(get_group=lambda _id: group, _get_conversation_dir=lambda _id: None)
    ks.peer_metadata = {}
    ks._send_ai_query = None
    ks._announce_history_after_repair = lambda _id: None
    return ks


def _remembered_on_disk(monitor, limit=REMEMBERED):
    """What the file holds after a turn the old fallback measured."""
    monitor.add_message("user", "one")
    monitor.set_token_limit(limit)
    assert monitor.save_history()


def _ai_chat_restart(llm, record, limit=REMEMBERED):
    ai_chats.write_record(AI_CHAT, record)
    _remembered_on_disk(_knowledge_service(llm)._get_or_create_conversation_monitor(AI_CHAT), limit)
    return _knowledge_service(llm)._get_or_create_conversation_monitor(AI_CHAT)


def test_an_ai_chat_read_back_from_disk_is_measured_on_its_alias(tmp_path):
    llm = _llm(tmp_path, _vendor("big", context_window=250000))

    assert _ai_chat_restart(llm, {"name": "c", "provider": "big"}).token_limit == 250000


def test_an_ai_chat_on_a_window_nobody_knows_does_not_keep_the_files_number(tmp_path):
    llm = _llm(tmp_path, _vendor())

    assert _ai_chat_restart(llm, {"name": "c", "provider": "vendor"}).token_limit == 0


def test_an_ai_chat_a_peer_serves_keeps_the_last_reply_until_the_next(tmp_path):
    llm = _llm(tmp_path, _vendor("big", context_window=250000))

    monitor = _ai_chat_restart(llm, {"name": "c", "provider": "big", "compute_host": "dpc-node-a"})

    assert monitor.token_limit == REMEMBERED


def test_a_null_limit_on_disk_reads_as_unknown_and_no_reader_trips_on_it(tmp_path):
    """A peer's chat, because nothing sets its limit after the read: what the file says stands."""
    llm = _llm(tmp_path, _vendor("big", context_window=250000))

    monitor = _ai_chat_restart(llm, {"name": "c", "provider": "big", "compute_host": "dpc-node-a"}, limit=None)

    assert monitor.token_limit == 0
    assert monitor.get_token_usage()["usage_percent"] == 0.0
    assert monitor.should_suggest_extraction() is False


def test_a_file_with_no_limit_keeps_the_limit_the_monitor_holds(tmp_path):
    """The key absent is not the key null: this default stays what `.get` had."""
    llm = _llm(tmp_path, _vendor("big", context_window=250000))
    ai_chats.write_record(AI_CHAT, {"name": "c", "provider": "big", "compute_host": "dpc-node-a"})
    first = _knowledge_service(llm)._get_or_create_conversation_monitor(AI_CHAT)
    held = first.token_limit
    first.add_message("user", "one")
    assert first.save_history()
    path = next(Path(first._get_conversation_dir()).glob("history.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    del saved["token_stats"]["token_limit"]
    path.write_text(json.dumps(saved), encoding="utf-8")

    assert _knowledge_service(llm)._get_or_create_conversation_monitor(AI_CHAT).token_limit == held


def _group_restart(llm):
    group = SimpleNamespace(name="g", agents={}, members=[], group_id="group-1")
    first = _knowledge_service(llm, group)._get_or_create_conversation_monitor("group-1")
    _remembered_on_disk(first)
    return _knowledge_service(llm, group)._get_or_create_conversation_monitor("group-1")


def test_a_group_read_back_from_disk_reads_the_default_alias_when_no_agent_says(tmp_path):
    llm = _llm(tmp_path, _vendor("big", context_window=250000), default="big")

    assert _group_restart(llm).token_limit == 250000


def test_a_group_on_a_window_nobody_knows_does_not_keep_the_files_number(tmp_path):
    llm = _llm(tmp_path, _vendor(), default="vendor")

    assert _group_restart(llm).token_limit == 0


@pytest.mark.asyncio
async def test_a_group_message_on_a_window_nobody_knows_broadcasts_0_not_128000():
    service = _GroupService()
    service.monitor.get_token_usage = lambda: {"token_limit": 0}

    await CoreService.send_group_message(service, GROUP, "hello")

    event = next(p for name, p in service.local_api.events if name == "token_usage_updated")
    assert event["token_limit"] == 0


@pytest.mark.asyncio
async def test_a_single_node_group_reset_broadcasts_0_not_128000():
    events = []

    async def _broadcast(name, payload):
        events.append((name, payload))

    core = CoreService.__new__(CoreService)
    core.p2p_manager = SimpleNamespace(node_id="dpc-node-me")
    core.group_manager = SimpleNamespace(get_group=lambda _id: SimpleNamespace(members=["dpc-node-me"]))
    core.reset_conversation = AsyncMock(return_value={"status": "success"})
    core.local_api = SimpleNamespace(broadcast_event=_broadcast)
    monitor = SimpleNamespace(get_token_usage=lambda: {"token_limit": 0})
    core.knowledge_service = SimpleNamespace(_get_or_create_conversation_monitor=lambda _id: monitor)

    await core.propose_new_session("group-1")

    assert ("token_usage_updated", 0) in [(name, p.get("token_limit")) for name, p in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("default,expected", [("small", 8192), ("vendor", 0)])
async def test_a_group_history_answer_reads_the_default_alias_not_its_models_first_alias(tmp_path, default, expected):
    llm = _llm(tmp_path, _vendor("big", context_window=250000), _vendor("small", context_window=8192),
               _vendor("vendor", model="another-model-no-table-names"), default=default)
    conv_dir = tmp_path / "group-store"
    conv_dir.mkdir()
    (conv_dir / "history.json").write_text(json.dumps({"messages": [], "token_stats": {"token_limit": REMEMBERED}}),
                                           encoding="utf-8")
    core = CoreService.__new__(CoreService)
    core.conversation_monitors = {}
    core.llm_manager = llm
    core.group_manager = SimpleNamespace(_get_conversation_dir=lambda _id: conv_dir)

    assert (await core.get_conversation_history("group-1"))["token_limit"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("windows,expected", [({"big": 250000}, 250000), ({}, 0)])
async def test_an_agent_post_reads_the_default_alias_when_the_monitor_holds_no_limit(windows, expected):
    service = _PostService()
    service.monitor.get_token_usage = lambda: {"token_limit": 0}
    service.llm_manager = _QueryManager(windows, "big")

    await CoreService.send_group_agent_message(service, "group-1234", "CC", "an answer")

    event = next(p for name, p in service.local_api.events if name == "token_usage_updated")
    assert event["token_limit"] == expected


# --- A message in an AI chat ----------------------------------------------------------


class _QueryMonitor:
    """What `execute_ai_query` reads up to the model call and after it."""

    instruction_set_name = None
    peer_context_hashes = {}

    def __init__(self, token_limit, count):
        self.token_limit = token_limit
        self.current_token_count = count
        self.history = []

    def set_token_limit(self, limit):
        self.token_limit = limit

    def set_token_count(self, tokens):
        self.current_token_count = tokens

    def add_message(self, role, content, **fields):
        self.history.append({"role": role, "content": content})

    def save_history(self):
        return True

    def get_message_history(self):
        return list(self.history)

    def set_inference_settings(self, *args):
        pass

    def should_suggest_extraction(self):
        return False


class _QueryManager:
    """What `execute_ai_query` reads for the limit; `alias_context_window` itself is tested
    above. The orchestrator keeps the double it was built with, which records the call."""

    def __init__(self, windows, default):
        self.windows = windows
        self.default_provider = default
        self.token_count_manager = SimpleNamespace(validate_prompt=lambda **kw: (True, None))

    def alias_context_window(self, alias):
        return self.windows.get(alias)

    def get_active_model_name(self):
        return "m"


def _query_service(windows, default="vendor", *, limit=REMEMBERED, count=33533):
    service = _FakeService(None, None, None)
    service.llm_manager = _QueryManager(windows, default)
    service.monitor = _QueryMonitor(limit, count)
    return service


def _reached_the_model(service):
    return [where for where, _peer, _prompt in service.sent] == ["local"]


async def _ask(service, **kwargs):
    await service.execute_ai_query(command_id="c1", prompt="hello", include_context=False,
                                   conversation_id=AI_CHAT, **kwargs)
    return service.local_api.responses


@pytest.mark.asyncio
async def test_a_message_is_not_refused_on_the_number_the_history_file_remembered():
    """33,533 / 4,096 stood in front of the only call that could have corrected it."""
    service = _query_service({"vendor": 131072})

    responses = await _ask(service)

    assert _reached_the_model(service)
    assert service.monitor.token_limit == 131072
    assert not any("full" in str(r.get("payload")) for r in responses)


@pytest.mark.asyncio
async def test_a_message_on_a_window_nobody_knows_goes_to_the_model():
    service = _query_service({})

    await _ask(service)

    assert _reached_the_model(service)
    assert service.monitor.token_limit == 0


@pytest.mark.asyncio
async def test_a_message_is_measured_on_the_alias_it_goes_to():
    service = _query_service({"big": 250000, "small": 8192}, default="big", limit=250000, count=20000)

    responses = await _ask(service, provider="small")

    assert service.monitor.token_limit == 8192
    assert any("full" in str(r.get("payload")) for r in responses)
    assert not service.sent


@pytest.mark.asyncio
async def test_a_message_a_peer_serves_is_not_measured_on_a_local_alias():
    service = _query_service({"vendor": 131072}, limit=50000, count=10)
    service.send_ai_query = AsyncMock(side_effect=_Stop())

    await _ask(service, compute_host="dpc-node-a", provider="vendor")

    assert service.monitor.token_limit == 50000


@pytest.mark.asyncio
async def test_a_reply_from_a_peer_that_states_no_window_leaves_0_not_none():
    """DPTP has the field optional; None in the monitor is a TypeError at the next `> 0`."""
    service = _query_service({}, limit=50000, count=10)
    service.send_ai_query = AsyncMock(return_value={
        "response": "ok", "model": "m", "provider": "p", "compute_host": "dpc-node-a",
        "tokens_used": 2, "prompt_tokens": 1, "response_tokens": 1, "model_max_tokens": None,
        "thinking": None, "thinking_tokens": None,
    })

    responses = await _ask(service, compute_host="dpc-node-a", provider="p")

    assert service.monitor.token_limit == 0
    assert responses[-1]["status"] == "OK", responses[-1]
    assert responses[-1]["payload"]["token_limit"] == 0


# --- The auxiliary readers ------------------------------------------------------------


class _SleepManager:
    """Two aliases of one model: the name cannot tell them apart."""

    def __init__(self, windows, default):
        self.windows = windows
        self.providers = {alias: SimpleNamespace(model="one-model") for alias in windows}
        self.default_provider = default
        self.token_count_manager = None

    async def query(self, prompt, provider_alias=None, **kwargs):
        return "- a finding"

    def count_tokens(self, text, model):
        return len(text) // 4

    def alias_context_window(self, alias):
        return self.windows.get(alias)


def _budgets_seen(monkeypatch):
    """The window the synthesis budget is computed from."""
    seen = []
    original = sleep_mod._compute_synthesis_budget
    monkeypatch.setattr(sleep_mod, "_compute_synthesis_budget",
                        lambda window, overhead: seen.append(window) or original(window, overhead))
    return seen


@pytest.mark.asyncio
async def test_sleep_budgets_on_the_alias_it_runs_on(tmp_path, monkeypatch):
    seen = _budgets_seen(monkeypatch)

    await run_sleep(_conversation(tmp_path), _SleepManager({"big": 250000, "small": 32000}, "big"),
                    provider_alias="small")

    assert set(seen) == {32000}


@pytest.mark.asyncio
async def test_sleep_on_a_window_nobody_knows_uses_its_named_default(tmp_path, monkeypatch):
    seen = _budgets_seen(monkeypatch)

    await run_sleep(_conversation(tmp_path), _SleepManager({"small": None}, "small"), provider_alias="small")

    assert set(seen) == {128000}


class _CompactionSeen(Exception):
    """`CompactionState` was built; the loop need go no further."""


async def _compaction_window(tmp_path, monkeypatch, *, cfg, running, windows):
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: cfg)
    seen = {}

    def _state(config):
        seen.update(config)
        raise _CompactionSeen()

    monkeypatch.setattr(loop_mod, "CompactionState", _state)
    manager = SimpleNamespace(providers={alias: SimpleNamespace(model="one-model") for alias in windows},
                              alias_context_window=windows.get)
    llm = SimpleNamespace(_llm_manager=manager, _compute_host="", _caller="a", _provider_alias=running)
    with pytest.raises(_CompactionSeen):
        await loop_mod.run_llm_loop(
            messages=[{"role": "user", "content": "go"}],
            tools=SimpleNamespace(_ctx=None, schemas=lambda **kw: []), llm=llm,
            agent_root=tmp_path, emit_progress=lambda *a, **k: None, reasoning_effort=None,
            conversation_id="c", max_rounds=1)
    return seen.get("context_window")


@pytest.mark.asyncio
async def test_compaction_measures_on_the_alias_the_loop_runs_on(tmp_path, monkeypatch):
    window = await _compaction_window(tmp_path, monkeypatch, cfg={"provider_alias": "big"}, running="small",
                                      windows={"big": 250000, "small": 8192})

    assert window == 8192


@pytest.mark.asyncio
async def test_compaction_falls_back_on_the_configured_alias_when_the_adapter_names_none(tmp_path, monkeypatch):
    window = await _compaction_window(tmp_path, monkeypatch, cfg={"provider_alias": "big"}, running=None,
                                      windows={"big": 250000, "small": 8192})

    assert window == 250000


@pytest.mark.asyncio
async def test_compaction_on_a_window_nobody_knows_gets_none_and_does_not_crash(tmp_path, monkeypatch):
    window = await _compaction_window(tmp_path, monkeypatch, cfg={"provider_alias": "big"}, running="big",
                                      windows={"big": None})

    assert window is None
    assert CompactionState({"context_window": window}).window == 204800
