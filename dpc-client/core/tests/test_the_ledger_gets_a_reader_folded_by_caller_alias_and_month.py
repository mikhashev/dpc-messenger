"""A-LEDGER-NOBODY-READS-IS-NOT-YET-AN-INSTRUMENT: the ledger's first reader.

`node_ledger.summarize()` folds rows by `caller`, by `alias` and by month of
`started_at`; `CoreService.get_usage_summary` exposes it over the local API
the way `get_firewall_rules` exposes the firewall.
"""

import json
from datetime import datetime, timezone

import pytest

from dpc_client_core import node_ledger
from dpc_client_core.node_ledger import NodeLedger, summarize, usage_row

pytestmark = pytest.mark.asyncio

SEPT_1 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
SEPT_2 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
AUG_31 = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)


def _row(started_at, **overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=100, completion_tokens=50, thinking_tokens=None,
        counts_source="engine", started_at=started_at, duration_s=1.0,
        billing="pay_per_use", cost_usd=0.01,
    )
    fields.update(overrides)
    return usage_row(**fields)


def _empty_group() -> dict:
    return {
        "row_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0,
        "cost_usd": 0.0, "unpriced": 0,
        "peer_proved": {"true": 0, "false": 0, "none": 0},
        "output_includes_thinking": {"includes": 0, "excludes": 0, "unknown": 0},
    }


def test_an_empty_ledger_summarizes_to_empty_groups_and_zero_totals():
    result = summarize([])

    assert result == {
        "row_count": 0, "since": None, "until": None,
        "by_caller": {}, "by_alias": {}, "by_month": {},
    }


def test_three_rows_fold_exactly_by_caller_alias_and_month():
    """An agent's local row (priced), a proved peer row (unpriced, D3: null
    means nobody priced it) and an unproved gateway row (priced) — the split
    ADR-041 D2 exists to make visible."""
    agent_row = _row(
        SEPT_1, request_id="a1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", route="local", prompt_tokens=100, completion_tokens=50,
        cost_usd=0.01, peer_proved=None,
    )
    proved_peer_row = _row(
        SEPT_1, request_id="p1", caller="dpc-node-alice", caller_kind="peer",
        alias="ds_pro", route="peer", prompt_tokens=200, completion_tokens=100,
        cost_usd=None, billing="subscription", peer_proved=True,
        peer_connection_type="ipv4_direct",
    )
    unproved_gateway_row = _row(
        SEPT_1, request_id="g1", caller="dpc-node-mallory", caller_kind="gateway",
        alias="ds_flash", route="peer", prompt_tokens=300, completion_tokens=150,
        cost_usd=0.02, peer_proved=False, peer_connection_type="hub_webrtc",
    )
    rows = [agent_row, proved_peer_row, unproved_gateway_row]

    result = summarize(rows)

    assert result["row_count"] == 3
    assert set(result["by_caller"]) == {"agent_001", "dpc-node-alice", "dpc-node-mallory"}
    assert result["by_caller"]["agent_001"] == {
        **_empty_group(), "row_count": 1, "prompt_tokens": 100, "completion_tokens": 50,
        "cost_usd": 0.01, "unpriced": 0,
        "peer_proved": {"true": 0, "false": 0, "none": 1},
        "output_includes_thinking": {"includes": 0, "excludes": 0, "unknown": 1},
    }
    assert result["by_caller"]["dpc-node-alice"] == {
        **_empty_group(), "row_count": 1, "prompt_tokens": 200, "completion_tokens": 100,
        "cost_usd": 0.0, "unpriced": 1,
        "peer_proved": {"true": 1, "false": 0, "none": 0},
        "output_includes_thinking": {"includes": 0, "excludes": 0, "unknown": 1},
    }

    ds_flash = result["by_alias"]["ds_flash"]
    assert ds_flash["row_count"] == 2
    assert ds_flash["prompt_tokens"] == 400 and ds_flash["completion_tokens"] == 200
    assert ds_flash["cost_usd"] == pytest.approx(0.03)
    assert ds_flash["unpriced"] == 0
    assert ds_flash["peer_proved"] == {"true": 0, "false": 1, "none": 1}

    ds_pro = result["by_alias"]["ds_pro"]
    assert ds_pro == {
        **_empty_group(), "row_count": 1, "prompt_tokens": 200, "completion_tokens": 100,
        "cost_usd": 0.0, "unpriced": 1,
        "peer_proved": {"true": 1, "false": 0, "none": 0},
        "output_includes_thinking": {"includes": 0, "excludes": 0, "unknown": 1},
    }

    assert set(result["by_month"]) == {"2026-09"}
    assert result["by_month"]["2026-09"]["row_count"] == 3


def test_a_window_excludes_a_row_outside_it():
    inside = _row(SEPT_1, request_id="inside")
    outside = _row(AUG_31, request_id="outside")

    result = summarize([inside, outside], since="2026-09-01T00:00:00Z", until="2026-09-30T23:59:59Z")

    assert result["row_count"] == 1
    assert list(result["by_month"]) == ["2026-09"]
    assert result["by_month"]["2026-09"]["row_count"] == 1


def test_a_malformed_bound_is_refused_not_silently_ignored():
    with pytest.raises(ValueError):
        summarize([_row(SEPT_1)], since="yesterday")


def test_a_legacy_row_without_the_new_columns_counts_under_none(tmp_path):
    """A row written before `peer_proved`/`output_includes_thinking` existed
    — hand-written JSONL, the shape a partition from before those columns
    actually has. `NodeLedger.rows()` fills the defaults; `summarize` must
    fold what it fills, not what was never on disk."""
    ledger = NodeLedger(tmp_path / "ledger")
    legacy = {
        "request_id": "old-1", "caller": "agent_002", "caller_kind": "agent",
        "alias": "ds_flash", "model": "deepseek-v4-flash", "route": "local",
        "prompt_tokens": 10, "completion_tokens": 5, "thinking_tokens": None,
        "counts_source": "engine", "started_at": "2026-09-01T00:00:00+00:00",
        "duration_s": 0.5, "billing": "pay_per_use", "cost_usd": 0.001,
    }
    ledger.directory.mkdir(parents=True)
    partition = ledger.partition_for(legacy["started_at"])
    partition.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    result = summarize(ledger.rows())

    group = result["by_caller"]["agent_002"]
    assert group["peer_proved"] == {"true": 0, "false": 0, "none": 1}
    assert group["output_includes_thinking"] == {"includes": 0, "excludes": 0, "unknown": 1}


async def test_the_local_api_command_answers_and_refuses_a_malformed_date(tmp_path, monkeypatch):
    from dpc_client_core.local_api import ALLOWED_COMMANDS, LocalApiServer
    from dpc_client_core.service import CoreService

    assert "get_usage_summary" in ALLOWED_COMMANDS

    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(SEPT_1, request_id="one"))

    class FakeCore:
        get_usage_summary = CoreService.get_usage_summary

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "ok", "command": "get_usage_summary", "payload": {}}),
        json.dumps({"id": "bad", "command": "get_usage_summary",
                    "payload": {"since": "yesterday"}}),
    ])

    await server._handler(ws)

    ok = next(m for m in ws.sent if m.get("id") == "ok")
    assert ok["status"] == "OK"
    assert ok["payload"]["status"] == "success"
    assert ok["payload"]["row_count"] == 1

    bad = next(m for m in ws.sent if m.get("id") == "bad")
    assert bad["status"] == "OK"
    assert bad["payload"]["status"] == "error"
    assert "yesterday" in bad["payload"]["message"]


class _FakeWS:
    """Feeds frames to the handler and records what it answers — the same
    fake used in tests/test_local_api.py."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []

    async def recv(self):
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, *args, **kwargs):
        pass
