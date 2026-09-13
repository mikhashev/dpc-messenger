"""TWO-SERIES-CARRY-ONE-PAID-CALL-AND-THE-BURN-READER-STILL-READS-THE-OLD-ONE.

Decided (Mike's call, 2026-09-13, DPC Project group): the ledger is the
record. `CoreService.get_usage_summary` — the one instrument the ledger has
beyond `spent_today` (A-LEDGER-NOBODY-READS-IS-NOT-YET-AN-INSTRUMENT) — folds
`node_ledger.owner_rows`: this node's own vendor spend, `route == "local"`,
instead of every row the ledger holds. The `DeepSeek usage:` line
`_record_usage` writes (`providers/deepseek_provider.py`) stays a log line:
nothing here parses it, and nothing should have to.
"""

import logging
from datetime import datetime, timezone

import pytest

from dpc_client_core import node_ledger
from dpc_client_core.node_ledger import owner_rows, usage_row
from dpc_client_core.service import CoreService

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=100, completion_tokens=50, thinking_tokens=None,
        counts_source="engine", started_at=WHEN, duration_s=1.0,
        billing="pay_per_use", cost_usd=0.01,
    )
    fields.update(overrides)
    return usage_row(**fields)


class FakeCore:
    get_usage_summary = CoreService.get_usage_summary


async def _summary(**kwargs):
    return await FakeCore().get_usage_summary(**kwargs)


# --- owner_rows: the single predicate, route == "local" ---


def test_owner_rows_keeps_the_three_local_shapes():
    """The owner's own agent, an owner's own gateway client, and this node
    serving a peer on its own key — all three write `route="local"` and all
    three are this node's own spend."""
    rows = [
        _row(request_id="a", caller="agent_001", caller_kind="agent", route="local"),
        _row(request_id="g", caller="ide_key", caller_kind="gateway", route="local"),
        _row(request_id="p", caller="dpc-node-alice", caller_kind="peer", route="local", cost_usd=0.02),
    ]
    kept = list(owner_rows(rows))
    assert {r["request_id"] for r in kept} == {"a", "g", "p"}


def test_owner_rows_drops_a_call_routed_to_a_peer():
    """An agent asking a peer to run the call spends the peer's money, not
    this node's — `route == "peer"` is excluded regardless of `caller_kind`."""
    rows = [
        _row(request_id="a", caller="agent_001", caller_kind="agent", route="local", cost_usd=0.01),
        _row(request_id="p", caller="agent_001", caller_kind="agent", route="peer", cost_usd=None,
             billing="subscription"),
    ]
    kept = list(owner_rows(rows))
    assert [r["request_id"] for r in kept] == ["a"]


# --- the burn reader: get_usage_summary folds only the owner's rows ---


async def test_a_peer_served_call_counts_in_the_owner_figure():
    """Serving a peer on this node's own DeepSeek key is this node's own
    money (ADR-041 D3 amendment, 2026-09-13): the tariff charged to the guest
    is a separate number (`tariff_amount`), but `cost_usd` is what this node
    paid its vendor, and that belongs in the owner burn figure."""
    ledger = node_ledger.default_ledger()
    ledger.append(_row(request_id="host-served-peer", caller="dpc-node-alice",
                        caller_kind="peer", route="local", cost_usd=0.05))

    result = await _summary()

    assert result["row_count"] == 1
    assert result["by_caller"]["dpc-node-alice"]["cost_usd"] == pytest.approx(0.05)


async def test_an_agents_own_call_counts_once():
    ledger = node_ledger.default_ledger()
    ledger.append(_row(request_id="own-call", caller="agent_001", caller_kind="agent",
                        route="local", cost_usd=0.03))

    result = await _summary()

    assert result["row_count"] == 1
    assert result["by_caller"]["agent_001"]["cost_usd"] == pytest.approx(0.03)


async def test_a_call_routed_to_a_peer_does_not_count():
    """The requester's own row for a peer-routed call already carries
    `cost_usd=None` since `24f92837`; here it must not even reach `summarize`
    — the money left with the peer that ran it, and the row is excluded by
    `route`, not merely by an absent cost."""
    ledger = node_ledger.default_ledger()
    ledger.append(_row(request_id="asked-a-peer", caller="agent_001", caller_kind="agent",
                        route="peer", cost_usd=None, billing="subscription"))

    result = await _summary()

    assert result["row_count"] == 0
    assert result["by_caller"] == {}


async def test_a_peer_call_and_an_own_call_do_not_mix():
    """The scenario the board entry names: one call this node served for a
    peer and one of the owner's own. Both are the owner's own spend and must
    both count, kept apart by caller rather than folded into one figure."""
    ledger = node_ledger.default_ledger()
    ledger.append(_row(request_id="own", caller="agent_001", caller_kind="agent",
                        route="local", cost_usd=0.01))
    ledger.append(_row(request_id="served", caller="dpc-node-alice", caller_kind="peer",
                        route="local", cost_usd=0.05))

    result = await _summary()

    assert result["row_count"] == 2
    assert result["by_caller"]["agent_001"]["cost_usd"] == pytest.approx(0.01)
    assert result["by_caller"]["dpc-node-alice"]["cost_usd"] == pytest.approx(0.05)


# --- the log line is no longer a source of figures ---


async def test_the_figure_comes_from_owner_rows_and_nothing_else(monkeypatch, caplog):
    """If the ledger's own filter answered zero, the burn figure must drop to
    zero even though a `DeepSeek usage:` line and a real ledger row both
    exist — proving `get_usage_summary` has no second, independent path back
    to a count, such as parsing that line, that could paper over a broken or
    bypassed filter."""
    logger = logging.getLogger("dpc_client_core.providers.deepseek_provider")
    with caplog.at_level(logging.INFO):
        logger.info(
            "DeepSeek usage: alias=ds_flash conv=- prompt=100 (hit=0/miss=100), "
            "completion=50 (reasoning=0/content=50), tool_calls=0, effort=server-default, path=plain"
        )

    ledger = node_ledger.default_ledger()
    ledger.append(_row(request_id="real", caller="agent_001", caller_kind="agent",
                        route="local", cost_usd=0.01))

    monkeypatch.setattr(node_ledger, "owner_rows", lambda rows: iter([]))

    result = await _summary()

    assert result["row_count"] == 0
    assert any("DeepSeek usage" in r.getMessage() for r in caplog.records)
