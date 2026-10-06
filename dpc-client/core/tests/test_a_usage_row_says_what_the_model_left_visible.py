"""A usage row says whether the model wrote anything visible.

On the local llama-server path `thinking_tokens` is an estimate bounded by the
completion, so a round whose reasoning the server cut at its budget read the
same on the ledger as a long visible answer. `content_chars` (the stripped
assistant text's length) and `tool_calls` (the calls in the response) are the
words `eval/loop/round_metrics.py` already uses for the two halves. None is a
half the writer did not see; 0 is measured and empty.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.node_ledger import NodeLedger, response_counts, usage_row

SEPT_1 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
MESSAGES = [{"role": "user", "content": "read the file"}]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read", "parameters": {"type": "object", "properties": {}},
}}]
PROSE = "  The file holds three functions.\n"


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="qwen_local", model="qwen", route="local",
        prompt_tokens=100, completion_tokens=40, thinking_tokens=40,
        counts_source="engine", started_at=SEPT_1, duration_s=1.0,
        billing="subscription", cost_amount=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


# --- the row ---------------------------------------------------------------------


def test_a_silent_tool_call_round_writes_zero_chars_and_one_call():
    row = _row(**response_counts("", [{"id": "c1"}]))
    assert (row["content_chars"], row["tool_calls"]) == (0, 1)


def test_a_prose_answer_writes_its_stripped_length_and_no_calls():
    row = _row(**response_counts(PROSE, []))
    assert (row["content_chars"], row["tool_calls"]) == (len(PROSE.strip()), 0)


def test_an_unseen_response_writes_null_never_zero():
    row = _row(**response_counts(None, None))
    assert row["content_chars"] is None and row["tool_calls"] is None
    # ... and a writer that passes nothing writes the same nulls.
    bare = _row()
    assert bare["content_chars"] is None and bare["tool_calls"] is None


def test_the_columns_stand_beside_the_thinking_count_they_qualify():
    keys = list(_row())
    assert keys.index("content_chars") == keys.index("thinking_source") + 1
    assert keys.index("tool_calls") == keys.index("content_chars") + 1


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True])
def test_a_value_that_is_not_a_count_is_refused(bad):
    with pytest.raises(ValueError, match="content_chars"):
        _row(content_chars=bad)
    with pytest.raises(ValueError, match="tool_calls"):
        _row(tool_calls=bad)


def test_a_row_written_before_the_columns_reads_them_as_null(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = _row()
    del old["content_chars"], old["tool_calls"]
    ledger.append(old)
    (row,) = list(ledger.rows())
    assert row["content_chars"] is None and row["tool_calls"] is None


# --- the agent path, end to end through the adapter --------------------------------


class _ToolProvider:
    """A provider on the native tool path, answering one fixed round."""

    alias = "qwen_local"
    model = "qwen"

    def __init__(self, content, calls):
        self._content, self._calls = content, calls

    async def generate_with_tools(self, messages, tools, **kwargs):
        return {
            "content": self._content,
            "tool_calls_raw": [SimpleNamespace(id=f"c{i}", name="read_file", input={}) for i in range(self._calls)],
            "thinking": "reasoning the server cut at its budget",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                      "reasoning_tokens": 40, "thinking_source": "estimated"},
        }

    def get_last_usage(self):
        return {}


def _adapter(provider, ledger):
    manager = SimpleNamespace(
        token_count_manager=None, providers={"qwen_local": provider},
        agent_provider=None, default_provider="qwen_local",
    )
    return DpcLlmAdapter(manager, provider_alias="qwen_local", caller="agent_test", ledger=ledger)


@pytest.mark.asyncio
async def test_the_adapter_tells_a_silent_tool_round_from_a_prose_answer(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")

    await _adapter(_ToolProvider("", 1), ledger).chat(MESSAGES, tools=TOOLS)
    await _adapter(_ToolProvider(PROSE, 0), ledger).chat(MESSAGES, tools=TOOLS)

    silent, prose = list(ledger.rows())
    assert (silent["content_chars"], silent["tool_calls"]) == (0, 1)
    assert (prose["content_chars"], prose["tool_calls"]) == (len(PROSE.strip()), 0)
    # The thinking count alone could not tell them apart.
    assert silent["thinking_tokens"] == prose["thinking_tokens"]


# --- the host: the llm_manager dict --------------------------------------------------


@pytest.mark.asyncio
async def test_a_served_query_without_a_tool_calls_key_leaves_that_column_null(tmp_path):
    """`llm_manager.query` returns the text and no `tool_calls` key: the row
    measures the one half and leaves the other null rather than 0."""
    from unittest.mock import AsyncMock

    from tests.test_p2p_coordinator import make_coordinator

    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={"type": "ollama", "model": "gemma3:27b"})}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": " ok ", "model": "gemma3:27b", "prompt_tokens": 12, "response_tokens": 3,
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    (row,) = list(coord._ledger.rows())
    assert row["content_chars"] == 2 and row["tool_calls"] is None
