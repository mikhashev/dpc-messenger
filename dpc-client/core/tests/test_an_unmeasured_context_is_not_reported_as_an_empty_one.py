"""A chat with history and no measured prompt size is not reported as 0% full.

The group monitor's token_stats on disk carry tokens_after_last_response = 0 by
design, and the agent's own figure lives in the service's memory, so the first
group turn after a restart read `context_usage_percent: 0` beside
`history_tokens: 5454` (Johnny, DPC Research, 2026-09-23) — and the DPC Project
group read 0 beside 71% of the window in history alone. That 0 sized Active Recall
as if the window were empty.
"""

import json

from dpc_client_core.dpc_agent.context import _build_runtime_section, context_usage_ratio
from dpc_client_core.managers.agent_manager import DpcAgentManager


class _Monitor:
    def __init__(self, tokens_after=0, messages=14):
        self._tokens_after_last_response = tokens_after
        self._tokens_after_last_response_at = None
        self.message_history = [{"role": "user", "content": "x"}] * messages

    def get_token_usage(self):
        return {"token_limit": 215040, "tokens_used": 152686}


class _Service:
    def get_group_agent_context(self, group_id, agent_id):
        return None  # the service's map is empty after a restart


def _manager(monitor, conv="group-abc"):
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr._agent_monitors = {conv: monitor}
    mgr.config = {}
    mgr.agent_id = "agent_johnny"
    mgr.service = _Service()
    mgr._last_used_agent = None
    return mgr


def test_the_session_state_does_not_claim_zero_percent_without_a_measurement():
    state = _manager(_Monitor()).get_session_state("group-abc")
    assert state["context_usage_percent"] is None


def test_the_runtime_block_prints_unknown_not_zero(tmp_path):
    state = _manager(_Monitor()).get_session_state("group-abc")
    text = _build_runtime_section(tmp_path, {"id": "c", "type": "chat"}, state)
    session = json.loads(text.split("\n\n", 1)[1])["session"]
    assert session["tokens_after_last_response"] is None
    assert session["context_usage_percent"] is None
    assert session["history_tokens"] == 152686


def test_recall_sizing_reads_the_history_floor_when_unmeasured():
    state = _manager(_Monitor()).get_session_state("group-abc")
    # 152686 / 215040 = 71% of the window in history alone: recall must not run "full".
    assert context_usage_ratio(state) >= 0.7


def test_a_measured_context_is_still_reported_as_measured(tmp_path):
    state = _manager(_Monitor(tokens_after=30788), conv="agent_johnny").get_session_state("agent_johnny")
    assert state["context_usage_percent"] == 14.32
    text = _build_runtime_section(tmp_path, {"id": "c", "type": "chat"}, state)
    session = json.loads(text.split("\n\n", 1)[1])["session"]
    assert session["tokens_after_last_response"] == 30788
    assert context_usage_ratio(state) == 0.1432


def test_no_session_state_reads_as_zero_for_recall():
    assert context_usage_ratio(None) == 0.0
