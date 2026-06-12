"""GROUP-GUARD-REAL-COUNT-BLIND: get_session_state must feed the agent's own
last real prompt count from the service group map for group conversations,
because the agent-side monitor reloads group history.json before every invoke
and the on-disk token_stats are zero by design (display-only counter)."""

from dpc_client_core.managers.agent_manager import DpcAgentManager


class FakeMonitor:
    def __init__(self, tokens_after=0, at=None):
        self._tokens_after_last_response = tokens_after
        self._tokens_after_last_response_at = at
        self.message_history = []

    def get_token_usage(self):
        return {"token_limit": 204800, "tokens_used": 5000}


class FakeService:
    def __init__(self, value=None):
        self._value = value
        self.calls = []

    def get_group_agent_context(self, group_id, agent_id):
        self.calls.append((group_id, agent_id))
        return self._value


def make_manager(conversation_id, monitor, service):
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr._agent_monitors = {conversation_id: monitor}
    mgr.config = {}
    mgr.agent_id = "agent_001"
    mgr.service = service
    mgr._last_used_agent = None
    return mgr


def test_group_uses_own_real_count_from_service_map():
    service = FakeService(value=(194827, 204800, "2026-06-12T11:04:47Z"))
    mgr = make_manager("group-abc", FakeMonitor(tokens_after=0), service)
    state = mgr.get_session_state("group-abc")
    assert state["tokens_after_last_response"] == 194827
    assert state["tokens_after_last_response_at"] == "2026-06-12T11:04:47Z"
    assert service.calls == [("group-abc", "agent_001")]


def test_group_keeps_monitor_value_when_map_empty():
    service = FakeService(value=None)
    mgr = make_manager("group-abc", FakeMonitor(tokens_after=0), service)
    state = mgr.get_session_state("group-abc")
    assert state["tokens_after_last_response"] == 0


def test_group_keeps_larger_monitor_value():
    service = FakeService(value=(100, 204800, "old"))
    mgr = make_manager("group-abc", FakeMonitor(tokens_after=150000, at="fresh"), service)
    state = mgr.get_session_state("group-abc")
    assert state["tokens_after_last_response"] == 150000
    assert state["tokens_after_last_response_at"] == "fresh"


def test_non_group_does_not_consult_map():
    service = FakeService(value=(194827, 204800, "ts"))
    mgr = make_manager("agent_001", FakeMonitor(tokens_after=42), service)
    state = mgr.get_session_state("agent_001")
    assert state["tokens_after_last_response"] == 42
    assert service.calls == []


def test_usage_percent_is_percent_not_ratio():
    service = FakeService(value=None)
    mgr = make_manager("agent_001", FakeMonitor(tokens_after=194827), service)
    state = mgr.get_session_state("agent_001")
    assert state["context_usage_percent"] == 95.13
    assert state["history_usage_percent"] == 2.44


def test_runtime_section_renders_context_breakdown(tmp_path):
    from dpc_client_core.dpc_agent.context import _build_runtime_section

    breakdown = [
        {"name": "system_prompt", "tokens": 12000},
        {"name": "Scratchpad", "tokens": 25000},
        {"name": "Active Recall (EXT/backlog.md)", "tokens": 30000},
    ]
    text = _build_runtime_section(
        tmp_path, {"id": "t1", "type": "chat"},
        session_state={
            "tokens_limit": 204800,
            "history_tokens": 5000,
            "tokens_after_last_response": 100000,
            "context_breakdown": breakdown,
        },
    )
    assert "context_breakdown" in text
    assert "Active Recall (EXT/backlog.md)" in text
    assert "25000" in text


def test_group_agent_context_attribution():
    from dpc_client_core.service import CoreService

    svc = CoreService.__new__(CoreService)
    svc._group_agent_context = {}
    svc.update_group_agent_context("group-x", "agent_001", 201844, 204800, "Ark")
    svc.update_group_agent_context("group-x", "agent_w", 16000, 204800, "Warren")

    worst = svc._worst_group_agent_context("group-x")
    assert worst[0] == 201844
    assert worst[3] == "Ark"

    agents = svc._group_agent_context_list("group-x")
    assert [a["name"] for a in agents] == ["Ark", "Warren"]
    assert agents[0]["percent"] == 98.6
    assert agents[1]["percent"] == 7.8


def test_runtime_section_without_breakdown(tmp_path):
    from dpc_client_core.dpc_agent.context import _build_runtime_section

    text = _build_runtime_section(
        tmp_path, {"id": "t1", "type": "chat"},
        session_state={"tokens_limit": 204800, "history_tokens": 5000},
    )
    assert "context_breakdown" not in text
