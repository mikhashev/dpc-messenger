"""A prompt divergence line carries the `request_id` of its request's usage row.

The line named conversation, round and agent, the round restarts every turn,
and the row has no round, so 1079 lines in one dpc-client.log carried only 313
distinct keys and none could be joined to its row
(A-DIVERGENCE-LINE-AND-ITS-LEDGER-ROW-SHARE-NO-KEY). The adapter hands the id
of the row it wrote back in the call's usage, and the loop logs the line with
it once the call returns — one request, one join.

Cross-platform: pure asyncio, a fake provider, a ledger in tmp_path.
"""

import asyncio
import re
from types import SimpleNamespace

from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.node_ledger import NodeLedger

SCHEMAS = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]
LOGGER = "dpc_client_core.dpc_agent.prompt_divergence"
LINE = re.compile(r"prompt divergence: conv=\S+ round=(\d+) .* request_id=(\S+)$")


class _Provider:
    """Tool rounds until `rounds`, then a final answer; raises on call `fail_on`."""

    alias, model = "vendor", "vendor-model"

    def __init__(self, rounds, fail_on=None):
        self.rounds, self.fail_on, self.calls = rounds, fail_on, 0

    async def generate_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("vendor down")
        usage = {"prompt_tokens": 100 * self.calls, "completion_tokens": 5,
                 "total_tokens": 100 * self.calls + 5}
        if self.calls > self.rounds:
            return {"content": "final answer", "tool_calls_raw": [], "usage": usage}
        block = SimpleNamespace(id=f"c{self.calls}", name="probe", input={"n": self.calls})
        return {"content": "", "tool_calls_raw": [block], "usage": usage}

    def get_last_usage(self):
        return {}


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return SCHEMAS

    def get_timeout(self, name):
        return 10


async def _fake_execute(tools, tc, logs_dir, timeout, task_id, **kwargs):
    return {"result": "ok", "is_error": False, "fn_name": "probe",
            "tool_call_id": tc["id"], "args_for_log": {}, "duration_ms": 1}


def _run(tmp_path, monkeypatch, caplog, provider, *, conversation_id, max_rounds=20):
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {
        "compaction_enabled": False, "context_window": 10_000_000,
    })
    monkeypatch.setattr(loop_mod, "_execute_with_timeout", _fake_execute)
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"vendor": provider},
        agent_provider=None, default_provider="vendor",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="vendor", caller="agent_test", ledger=ledger)
    with caplog.at_level("INFO", logger=LOGGER):
        text, _usage, _trace = asyncio.run(run_llm_loop(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
            tools=_Tools(), llm=adapter, agent_root=tmp_path / "agent_test",
            emit_progress=lambda *a, **k: None, conversation_id=conversation_id,
            max_rounds=max_rounds,
        ))
    lines = [LINE.search(r.getMessage()) for r in caplog.records
             if r.name == LOGGER and "prompt divergence" in r.getMessage()]
    assert all(lines), [r.getMessage() for r in caplog.records]
    return text, [(int(m.group(1)), m.group(2)) for m in lines], list(ledger.rows())


def test_every_request_of_a_turn_logs_the_id_its_row_carries(tmp_path, monkeypatch, caplog):
    text, lines, rows = _run(tmp_path, monkeypatch, caplog, _Provider(rounds=2),
                             conversation_id="conv-join-rounds")

    assert text == "final answer"
    assert [r for r, _ in lines] == [1, 2, 3]
    assert [rid for _, rid in lines] == [row["request_id"] for row in rows]
    assert len({rid for _, rid in lines}) == 3


def test_the_finalising_call_after_a_guard_stop_is_joined_too(tmp_path, monkeypatch, caplog):
    # Tool rounds forever; the round guard stops at two and one more call writes the answer.
    text, lines, rows = _run(tmp_path, monkeypatch, caplog, _Provider(rounds=2),
                             conversation_id="conv-join-final", max_rounds=2)

    assert len(rows) == 3 and len(lines) == 3
    assert [rid for _, rid in lines] == [row["request_id"] for row in rows]


def test_a_call_that_raised_logs_its_line_with_no_row_to_name(tmp_path, monkeypatch, caplog):
    text, lines, rows = _run(tmp_path, monkeypatch, caplog, _Provider(rounds=5, fail_on=2),
                             conversation_id="conv-join-raised")

    assert text.startswith("⚠️ LLM error")
    assert len(rows) == 1
    assert lines == [(1, rows[0]["request_id"]), (2, "-")]
