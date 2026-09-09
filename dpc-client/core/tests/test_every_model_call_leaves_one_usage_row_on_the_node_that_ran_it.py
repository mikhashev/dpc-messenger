"""Every model call leaves one usage row on the node that ran it (ADR-041 D3).

An agent's call existed only inside a per-task aggregate; a peer's call was
written nowhere, and on a paid alias landed in the owner's burn series wearing
nobody's name. Now the adapter writes one row per `chat()` and the coordinator
one per served request, each priced at the moment of the call by the node that
made it and never re-priced.
"""

import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.pricing import compute_cost_usd
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers.base import AIProvider
from tests.test_p2p_coordinator import make_coordinator

D3_COLUMNS = (
    "request_id", "caller", "caller_kind", "alias", "model", "route",
    "prompt_tokens", "completion_tokens", "thinking_tokens", "counts_source",
    "started_at", "duration_s", "billing", "cost_usd",
)
MESSAGES = [{"role": "user", "content": "x" * 4000}]
PEER = "dpc-node-" + "b" * 32


class _PricedProvider:
    """Reports its own usage, as DeepSeek does; the adapter prices the report."""

    alias = "ds_flash"
    model = "deepseek-v4-flash"

    async def generate_response(self, prompt, **kwargs):
        return "short answer"

    def get_last_usage(self):
        return {
            "prompt_tokens": 1000, "completion_tokens": 900, "total_tokens": 1900,
            "reasoning_tokens": 850,
        }


class _SilentProvider(AIProvider):
    """Reports nothing, so the adapter counts for itself."""

    def __init__(self):
        super().__init__("ds_flash", {"type": "ollama", "model": "deepseek-v4-flash"})

    async def generate_response(self, prompt, **kwargs):
        return "short answer"


def _adapter(provider, ledger, **kwargs):
    manager = SimpleNamespace(
        token_count_manager=None, providers={"ds_flash": provider},
        agent_provider=None, default_provider="ds_flash",
    )
    return DpcLlmAdapter(manager, provider_alias="ds_flash", caller="agent_test", ledger=ledger, **kwargs)


# --- the agent path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_call_leaves_one_row_with_every_column_of_d3(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    adapter = _adapter(_PricedProvider(), ledger)

    _msg, usage = await adapter.chat(MESSAGES, task_id="task-1", conversation_id="conv-1")

    (row,) = list(ledger.rows())
    assert all(column in row for column in D3_COLUMNS)
    assert row["caller"] == "agent_test" and row["caller_kind"] == "agent"
    assert row["route"] == "local"
    assert row["alias"] == "ds_flash" and row["model"] == "deepseek-v4-flash"
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 900
    assert row["thinking_tokens"] == 850
    assert row["counts_source"] == "engine"
    assert row["billing"] == "pay_per_use"
    assert row["cost_usd"] > 0 and row["cost_usd"] == pytest.approx(usage["cost"])
    assert row["task_id"] == "task-1" and row["conversation_id"] == "conv-1"
    uuid.UUID(row["request_id"])
    started = datetime.fromisoformat(row["started_at"])
    assert started.utcoffset() is not None and started.utcoffset().total_seconds() == 0
    assert row["duration_s"] >= 0


@pytest.mark.asyncio
async def test_a_tasks_rows_add_up_to_the_cost_its_caller_accumulated(tmp_path):
    """D3's consistency rule: the sum of a task's rows is the task's cost. Two
    tasks interleaved, so the join key is what tells them apart."""
    ledger = NodeLedger(tmp_path / "ledger")
    adapter = _adapter(_PricedProvider(), ledger)
    accumulated = {"task-A": 0.0, "task-B": 0.0}

    for task in ("task-A", "task-B", "task-A", "task-A"):
        _msg, usage = await adapter.chat(MESSAGES, task_id=task)
        accumulated[task] += usage["cost"]  # what run_llm_loop does per round

    rows = list(ledger.rows())
    assert len(rows) == 4
    for task, total in accumulated.items():
        assert total > 0
        assert sum(r["cost_usd"] for r in rows if r["task_id"] == task) == pytest.approx(total)


@pytest.mark.asyncio
async def test_counts_source_says_who_counted(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")

    await _adapter(_PricedProvider(), ledger).chat(MESSAGES)
    await _adapter(_SilentProvider(), ledger).chat(MESSAGES)

    reported, counted = list(ledger.rows())
    assert reported["counts_source"] == "engine"
    assert counted["counts_source"] == "ours"
    assert counted["prompt_tokens"] > 0


@pytest.mark.asyncio
async def test_a_call_routed_to_a_peer_is_this_agents_row_marked_peer(tmp_path):
    """This node's row says the agent here called and a peer ran it; the peer
    writes its own row under this node's name."""
    ledger = NodeLedger(tmp_path / "ledger")
    service = SimpleNamespace(_request_inference_from_peer=AsyncMock(return_value={
        "response": "from afar", "prompt_tokens": 40, "response_tokens": 12,
        "tokens_used": 52, "model": "qwen-on-the-peer", "cost_usd": 0.0,
    }))
    adapter = _adapter(_PricedProvider(), ledger, compute_host=PEER)
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )

    _msg, usage = await adapter.chat(MESSAGES, task_id="task-remote")

    (row,) = list(ledger.rows())
    assert row["route"] == "peer"
    assert row["caller"] == "agent_test" and row["caller_kind"] == "agent"
    assert row["alias"] == "ds_flash" and row["model"] == "qwen-on-the-peer"
    assert row["counts_source"] == "engine"
    assert row["prompt_tokens"] == 40 and row["completion_tokens"] == 12
    assert row["cost_usd"] == pytest.approx(usage["cost"])


def test_the_loop_hands_its_task_id_to_every_round(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent.loop import run_llm_loop

    # The loop reads an agent config keyed by the root directory's name, and
    # the resolver creates ~/.dpc/agents/<name>/ as a side effect of the read.
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {})
    seen = []

    class _Llm:
        async def chat(self, messages, **kwargs):
            seen.append(kwargs.get("task_id"))
            return {"content": "done", "tool_calls": []}, {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0,
            }

    class _Tools:
        _ctx = None

        def schemas(self, core_only=False, include_restricted=False):
            return []

    asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "hi"}], tools=_Tools(), llm=_Llm(),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="task-7",
    ))

    assert seen == ["task-7"]


@pytest.mark.asyncio
async def test_the_guard_stop_final_call_still_names_its_task():
    from dpc_client_core.dpc_agent.hooks import HookRegistry
    from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop

    llm = SimpleNamespace(chat=AsyncMock(return_value=({"role": "assistant", "content": "bye"}, {})))

    await _finalize_after_guard_stop(
        HookRegistry(), [], llm, None, "conv-1", {}, {}, fallback_reason="stopped", task_id="task-9",
    )

    assert llm.chat.call_args.kwargs["task_id"] == "task-9"


# --- the peer path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_served_peer_call_is_written_under_the_peers_name_with_the_wires_request_id(tmp_path):
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = "deepseek_pro"
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "deepseek-v4-pro", "provider": "deepseek_pro",
        "prompt_tokens": 1000, "response_tokens": 500, "thinking_tokens": 200, "tokens_used": 1500,
    })

    await coord.handle_inference_request("peer-1", "req-from-the-wire", "hello")

    (row,) = list(coord._ledger.rows())
    assert row["caller"] == "peer-1" and row["caller_kind"] == "peer"
    assert row["request_id"] == "req-from-the-wire"
    assert row["alias"] == "deepseek_pro" and row["model"] == "deepseek-v4-pro"
    assert row["route"] == "local" and row["counts_source"] == "ours"
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 500
    assert row["thinking_tokens"] == 200
    assert row["billing"] == "pay_per_use"
    # Priced once, at the moment the call was made: pricing that moment again
    # gives the row's number, whatever hour this test runs at.
    at = datetime.fromisoformat(row["started_at"])
    assert row["cost_usd"] > 0
    assert row["cost_usd"] == pytest.approx(
        compute_cost_usd("deepseek_pro", 1000, 500, model="deepseek-v4-pro", at=at)
    )
    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["cost_usd"] == pytest.approx(row["cost_usd"])


@pytest.mark.asyncio
async def test_a_served_call_on_a_local_alias_costs_the_host_nothing_and_says_so(tmp_path):
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "gemma3:27b", "prompt_tokens": 1200, "response_tokens": 300,
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    (row,) = list(coord._ledger.rows())
    assert row["alias"] == "ollama_local" and row["billing"] == "subscription"
    assert row["cost_usd"] == 0.0
    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_the_hosts_price_reaches_the_requester_only_when_the_host_sent_it():
    from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler

    service = MagicMock()
    service._pending_inference_requests = {}
    handler = RemoteInferenceResponseHandler(service)
    loop = asyncio.get_running_loop()
    priced, unpriced = loop.create_future(), loop.create_future()
    service._pending_inference_requests.update({"req-priced": priced, "req-unpriced": unpriced})

    await handler.handle(PEER, {
        "request_id": "req-priced", "status": "success", "response": "ok", "cost_usd": 0.0041,
    })
    await handler.handle(PEER, {"request_id": "req-unpriced", "status": "success", "response": "ok"})

    assert priced.result()["cost_usd"] == 0.0041
    assert "cost_usd" not in unpriced.result()
