"""The effort the host actually served reaches the guest and both usage rows.

The request carries `reasoning_effort` and the host clamps it to its own cap
(`P2PCoordinator._effort_for_peer`). Until now the clamped word went only into
the model call and an INFO line on the host: not into the response and not into
either ledger row, so a guest paying by the token for reasoning it asked for at
`low` and was served at `high`, or the reverse, had nothing to check the depth
against. `served_effort` is that word: on REMOTE_INFERENCE_RESPONSE (DPTP v1.7),
on the host's row and, copied from the wire, on the requester's row. Absent means
the host applied no effort control — not the same as `off`. A row written before
the column reads as None.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler
from dpc_client_core.node_ledger import NodeLedger, usage_row
from tests.test_p2p_coordinator import make_coordinator

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
PEER = "dpc-node-" + "c" * 32
SPEC = Path(__file__).resolve().parents[3] / "specs" / "dptp_v1.md"


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="qwen", model="qwen3:8b", route="local",
        prompt_tokens=8, completion_tokens=1, thinking_tokens=56,
        counts_source="ours", started_at=NOW, duration_s=1.0,
        billing="subscription", cost_usd=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


# --- the ledger row ---------------------------------------------------------------


def test_a_row_that_was_given_no_served_effort_says_none():
    assert _row()["served_effort"] is None


@pytest.mark.parametrize("word", ["off", "low", "medium", "high", "max"])
def test_the_served_effort_is_written_as_given(word):
    assert _row(served_effort=word)["served_effort"] == word


def test_a_served_effort_that_is_not_a_word_is_refused_not_written():
    with pytest.raises(ValueError, match="served_effort"):
        _row(served_effort=3)


def test_a_row_written_before_the_column_reads_as_none(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = {k: v for k, v in _row().items() if k != "served_effort"}
    ledger.append(old)

    (row,) = ledger.rows()
    assert "served_effort" in row and row["served_effort"] is None


# --- the host: the clamped word goes on the wire and on the host's own row -------------


def _host(tmp_path, configured_effort=None):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    config = {"reasoning_effort": configured_effort} if configured_effort else {}
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config=config)}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3:8b", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 1, "tokens_used": 9,
    })
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_a_guest_asking_max_of_a_host_capped_at_low_is_told_low(tmp_path):
    coord, svc = _host(tmp_path, configured_effort="low")

    await coord.handle_inference_request("peer-1", "req-1", "ping", reasoning_effort="max")

    assert svc.llm_manager.query.call_args.kwargs["reasoning_effort"] == "low"
    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["served_effort"] == "low"
    (row,) = coord._ledger.rows()
    assert row["served_effort"] == "low"


@pytest.mark.asyncio
async def test_a_host_that_applied_no_effort_control_sends_no_word_and_its_row_says_none(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    assert "reasoning_effort" not in svc.llm_manager.query.call_args.kwargs
    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["status"] == "success"
    assert "served_effort" not in sent["payload"]
    (row,) = coord._ledger.rows()
    assert row["served_effort"] is None


@pytest.mark.asyncio
async def test_a_refusal_carries_no_served_effort(tmp_path):
    coord, svc = _host(tmp_path, configured_effort="low")
    svc.firewall.can_request_inference.return_value = False

    await coord.handle_inference_request("peer-1", "req-1", "ping", reasoning_effort="max")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["status"] == "error"
    assert "served_effort" not in sent["payload"]


# --- the guest: the handler reads the word, and an old host leaves it absent -------------


@pytest.mark.asyncio
async def test_the_requester_reads_the_served_effort_off_the_wire_and_an_old_host_leaves_it_absent():
    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    loop = asyncio.get_running_loop()
    stated, silent = loop.create_future(), loop.create_future()
    handler.service._pending_inference_requests.update({"req-stated": stated, "req-silent": silent})

    await handler.handle("peer-1", {"request_id": "req-stated", "status": "success", "response": "ok",
                                    "served_effort": "low"})
    await handler.handle("peer-1", {"request_id": "req-silent", "status": "success", "response": "ok"})

    assert stated.result()["served_effort"] == "low"
    assert "served_effort" not in silent.result()


# --- the guest's row: the word from the wire, whether or not the host counted -----------


class _SilentProvider:
    alias = "qwen"
    model = "qwen3:8b"

    async def generate_response(self, prompt, **kwargs):
        return "unused"

    def get_last_usage(self):
        return {}


def _requester(tmp_path, answer):
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"qwen": _SilentProvider()},
        agent_provider=None, default_provider="qwen",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="qwen", caller="agent_test",
                            ledger=ledger, compute_host=PEER)
    service = SimpleNamespace(_request_inference_from_peer=AsyncMock(return_value=answer))
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )
    return adapter, ledger


COUNTED = {"request_id": "req-from-the-wire", "response": "from afar", "model": "qwen-on-the-peer",
           "prompt_tokens": 40, "response_tokens": 1, "tokens_used": 41}
UNCOUNTED = {"request_id": "req-from-the-wire", "response": "from afar", "model": "qwen-on-the-peer"}


@pytest.mark.parametrize("answer", [COUNTED, UNCOUNTED], ids=["host counted", "host did not count"])
@pytest.mark.asyncio
async def test_the_requesters_row_carries_the_word_the_host_sent(tmp_path, answer):
    adapter, ledger = _requester(tmp_path, dict(answer, served_effort="low"))

    _, usage = await adapter.chat([{"role": "user", "content": "x"}], reasoning_effort="max")

    assert usage["served_effort"] == "low"
    (row,) = ledger.rows()
    assert (row["route"], row["request_id"]) == ("peer", "req-from-the-wire")
    assert row["served_effort"] == "low"


@pytest.mark.asyncio
async def test_the_word_survives_the_whole_wire_from_the_factory_to_the_requesters_row(tmp_path):
    """The host's factory builds the frame, the guest's handler reads it, the
    adapter writes the row: one word, three hops, no hop allowed to drop it."""
    from dpc_protocol.protocol import create_remote_inference_response

    frame = create_remote_inference_response(
        "req-from-the-wire", response="from afar", model="qwen-on-the-peer",
        prompt_tokens=40, response_tokens=1, tokens_used=41, served_effort="low",
    )
    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    future = asyncio.get_running_loop().create_future()
    handler.service._pending_inference_requests["req-from-the-wire"] = future
    await handler.handle(PEER, frame["payload"])

    adapter, ledger = _requester(tmp_path, future.result())
    await adapter.chat([{"role": "user", "content": "x"}], reasoning_effort="max")

    (row,) = ledger.rows()
    assert (row["request_id"], row["served_effort"]) == ("req-from-the-wire", "low")


@pytest.mark.asyncio
async def test_an_old_host_leaves_the_requesters_row_at_none(tmp_path):
    adapter, ledger = _requester(tmp_path, dict(COUNTED))

    _, usage = await adapter.chat([{"role": "user", "content": "x"}], reasoning_effort="max")

    assert "served_effort" not in usage
    (row,) = ledger.rows()
    assert row["served_effort"] is None


# --- the spec: §3.4 names both words and no longer names an object nothing sends ---------


def _section_3_4():
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### 3.4 Remote AI Inference")
    return text[start:text.index("### 3.4.1", start)]


def _changelog_v1_7():
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### v1.7")
    return text[start:text.index("### v1.6", start)]


def test_section_3_4_lists_reasoning_effort_on_the_request_and_served_effort_on_the_response():
    section = _section_3_4()
    request, response = section.split("#### REMOTE_INFERENCE_RESPONSE")
    assert "`reasoning_effort` (string, optional" in request
    assert "`served_effort` (string, optional" in response


def test_section_3_4_no_longer_names_the_thinking_object_nothing_sends():
    section = _section_3_4()
    assert "budget_tokens" not in section
    assert "`thinking` (object" not in section


def test_the_v1_7_changelog_names_both_words_and_the_removal():
    changelog = _changelog_v1_7()
    assert "`reasoning_effort`" in changelog
    assert "`served_effort`" in changelog
    assert "never emitted or read" in changelog
