"""THE-LEDGER-COUNTS-EVERY-SHARED-CALL-AND-NEITHER-SIDE-CAN-SEE-IT-IN-THE-UI.

One ledger, three series: what this node served to peers, what peers served
it, and what it ran for itself. `node_ledger.usage_by_role` folds them and
`CoreService.get_inference_usage` answers with them; `get_usage_summary`, the
owner's burn, is untouched beside it.

The four rows below are the four shapes a real partition holds — a served
call with a declared tariff, this node's own vendor call, a consumed call
whose amount the host computed, and a consumed call whose counts nobody could
price — so every assertion is about a row that can exist.
"""

import json
from datetime import datetime, timezone

import pytest

from dpc_client_core import node_ledger
from dpc_client_core.node_ledger import (
    NodeLedger,
    consumed_rows,
    own_rows,
    served_rows,
    summarize,
    tariff_amount_for,
    usage_by_role,
    usage_row,
)

SEPT_1 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
SEPT_2 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
AUG_31 = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)

ALICE = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32

TARIFF = {"tariff_in": 2.0, "tariff_out": 10.0, "tariff_currency": "EUR", "tariff_at": "2026-09-01"}


def _row(started_at=SEPT_1, **overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=100, completion_tokens=50, thinking_tokens=None,
        counts_source="engine", output_includes_thinking="excludes",
        started_at=started_at, duration_s=1.0,
        billing="pay_per_use", cost_usd=0.01,
    )
    fields.update(overrides)
    return usage_row(**fields)


def _amount(row_fields):
    """What the writer computes once, at the moment of the call."""
    return tariff_amount_for(
        prompt_tokens=row_fields["prompt_tokens"],
        completion_tokens=row_fields["completion_tokens"],
        thinking_tokens=row_fields.get("thinking_tokens"),
        output_includes_thinking=row_fields["output_includes_thinking"],
        tariff_in=TARIFF["tariff_in"], tariff_out=TARIFF["tariff_out"],
    )


SERVED_FIELDS = dict(
    request_id="served-1", caller=ALICE, caller_kind="peer",
    alias="ollama_local", model="qwen3:8b", route="local",
    prompt_tokens=1000, completion_tokens=500, thinking_tokens=None,
    counts_source="ours", output_includes_thinking="excludes",
    duration_s=4.0, billing="subscription", cost_usd=0.0,
    peer_proved=True, peer_connection_type="ipv4_direct", **TARIFF,
)
CONSUMED_FIELDS = dict(
    request_id="consumed-1", caller="agent_001", caller_kind="agent",
    alias="bob_glm", model="glm-4.7", route="peer", served_by=BOB,
    prompt_tokens=200, completion_tokens=100, thinking_tokens=None,
    counts_source="engine", output_includes_thinking="excludes",
    duration_s=2.0, billing="subscription", cost_usd=None,
    peer_proved=True, peer_connection_type="ipv4_direct", **TARIFF,
)

# 1000 x 2.0 + 500 x 10.0, per 1M: what Alice owes this node for the served call.
SERVED_AMOUNT = 0.007
# 200 x 2.0 + 100 x 10.0, per 1M: what this node owes Bob for the priced one.
CONSUMED_AMOUNT = 0.0014


def _four_rows():
    """The served call, this node's own vendor call, and two consumed ones —
    the second consumed row is a call whose counts carry no convention, so its
    host wrote the tariff group with a null amount."""
    served = usage_row(started_at=SEPT_1, tariff_amount=_amount(SERVED_FIELDS), **SERVED_FIELDS)
    own = _row(SEPT_1, request_id="own-1", cost_usd=0.02)
    consumed = usage_row(
        started_at=SEPT_1, tariff_amount=_amount(CONSUMED_FIELDS), **CONSUMED_FIELDS
    )
    unpriceable = usage_row(
        started_at=SEPT_2,
        **{**CONSUMED_FIELDS, "request_id": "consumed-2",
           "output_includes_thinking": "unknown", "tariff_amount": None},
    )
    return [served, own, consumed, unpriceable]


def _empty_role_entry() -> dict:
    return {
        "row_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0,
        "duration_s": 0.0,
        "counts_source": {"ours": 0, "engine": 0},
        "peer_proved": {"true": 0, "false": 0, "none": 0},
        "cost_usd": 0.0, "unpriced": 0,
        "tariff": {}, "tariff_unpriceable": 0, "untariffed": 0,
    }


# --- (1) the column the guest's row needed --------------------------------------------


def test_the_guests_row_names_the_host_that_served_it():
    row = usage_row(started_at=SEPT_1, **CONSUMED_FIELDS)

    assert row["served_by"] == BOB


def test_a_row_this_node_ran_itself_carries_no_host_column():
    """There is no other node to name, and a null would read as «served by
    nobody» rather than «served here»."""
    assert "served_by" not in _row(SEPT_1)


def test_a_host_that_is_not_a_node_id_is_refused_rather_than_written():
    for bad in (17, ["dpc-node-x"], {"node_id": BOB}):
        with pytest.raises(ValueError, match="served_by"):
            usage_row(started_at=SEPT_1, **{**CONSUMED_FIELDS, "served_by": bad})


def test_an_old_consumed_row_reads_the_column_as_unknown(tmp_path):
    """A partition written before the column: `rows()` fills it, and only on a
    consumed row — a local row never had a host to name."""
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.directory.mkdir(parents=True)
    legacy_consumed = {
        "request_id": "old-peer", "caller": "agent_002", "caller_kind": "agent",
        "alias": "bob_glm", "model": "glm-4.7", "route": "peer",
        "prompt_tokens": 10, "completion_tokens": 5, "thinking_tokens": None,
        "counts_source": "engine", "started_at": "2026-09-01T00:00:00+00:00",
        "duration_s": 0.5, "billing": "subscription", "cost_usd": None,
    }
    legacy_local = {**legacy_consumed, "request_id": "old-local", "route": "local",
                    "cost_usd": 0.001, "billing": "pay_per_use"}
    partition = ledger.partition_for(legacy_consumed["started_at"])
    partition.write_text(
        json.dumps(legacy_consumed) + "\n" + json.dumps(legacy_local) + "\n", encoding="utf-8"
    )

    by_id = {row["request_id"]: row for row in ledger.rows()}

    assert by_id["old-peer"]["served_by"] is None
    assert "served_by" not in by_id["old-local"]


# --- (2) the three filters ------------------------------------------------------------


def test_each_filter_takes_its_own_rows_and_no_others():
    served, own, consumed, unpriceable = _four_rows()
    rows = [served, own, consumed, unpriceable]

    assert [r["request_id"] for r in served_rows(rows)] == ["served-1"]
    assert [r["request_id"] for r in own_rows(rows)] == ["own-1"]
    assert [r["request_id"] for r in consumed_rows(rows)] == ["consumed-1", "consumed-2"]


def test_a_gateway_client_is_this_nodes_own_user_not_a_peer_it_serves():
    """The gateway door is local: its caller sits at this machine, so its row
    is the owner's own consumption however the id is spelled."""
    gateway = _row(SEPT_1, request_id="g1", caller="continue", caller_kind="gateway")

    assert [r["request_id"] for r in own_rows([gateway])] == ["g1"]
    assert list(served_rows([gateway])) == []


# --- (3) the three series -------------------------------------------------------------


def test_the_two_sides_of_a_shared_call_each_read_their_own_numbers():
    result = usage_by_role(_four_rows())

    # The owner's side: one peer, one alias, the tokens Alice spent and what
    # she owes for them. cost_usd is a real zero — a local model costs the
    # node nothing by construction — and `unpriced` stays 0 to say so.
    alice = {
        **_empty_role_entry(), "row_count": 1,
        "prompt_tokens": 1000, "completion_tokens": 500, "duration_s": 4.0,
        "counts_source": {"ours": 1, "engine": 0},
        "peer_proved": {"true": 1, "false": 0, "none": 0},
        "cost_usd": 0.0, "unpriced": 0,
        "tariff": {"EUR": {"amount": pytest.approx(SERVED_AMOUNT), "rows": 1}},
    }
    assert result["served"]["by_caller"] == {
        ALICE: {**alice, "by_alias": {"ollama_local": alice}},
    }
    assert result["served"]["by_alias"] == {"ollama_local": alice}

    # The guest's side: both consumed rows fold under one host and alias. The
    # priced one is summed, the one the host could not price is counted — never
    # added in as a zero, which would say the call was free.
    assert result["consumed"]["by_source"] == {
        f"remote:{BOB}:bob_glm": {
            **_empty_role_entry(), "row_count": 2,
            "prompt_tokens": 400, "completion_tokens": 200, "duration_s": 4.0,
            "counts_source": {"ours": 0, "engine": 2},
            "peer_proved": {"true": 2, "false": 0, "none": 0},
            "cost_usd": 0.0, "unpriced": 2,
            "tariff": {"EUR": {"amount": pytest.approx(CONSUMED_AMOUNT), "rows": 1}},
            "tariff_unpriceable": 1,
            "node_id": BOB, "alias": "bob_glm",
        },
    }

    # Neither side of a shared call: this node's own vendor spend, with no
    # tariff on it at all.
    assert result["own"]["by_alias"] == {
        "ds_flash": {
            **_empty_role_entry(), "row_count": 1,
            "prompt_tokens": 100, "completion_tokens": 50, "duration_s": 1.0,
            "counts_source": {"ours": 0, "engine": 1},
            "peer_proved": {"true": 0, "false": 0, "none": 1},
            "cost_usd": 0.02, "untariffed": 1,
        },
    }
    assert result["since"] is None and result["until"] is None


def test_an_empty_ledger_reads_as_three_empty_series():
    assert usage_by_role([]) == {
        "since": None, "until": None,
        "served": {"by_caller": {}, "by_alias": {}},
        "consumed": {"by_source": {}},
        "own": {"by_alias": {}},
    }


def _legacy_consumed(**extra):
    """A consumed row as every one written before the `served_by` column looks:
    the alias, and nothing saying whose alias it was."""
    fields = {k: v for k, v in CONSUMED_FIELDS.items() if k != "served_by"}
    fields.update(extra)
    return usage_row(started_at=SEPT_1, **fields, tariff_amount=_amount(CONSUMED_FIELDS))


def test_a_consumed_row_with_no_host_lands_under_an_unknown_host():
    """The key keeps the form every consumed key has and names the host `?`,
    which no node id can be; the group still echoes `node_id: None`, so the
    reader says «host not recorded» from the field, not from the key."""
    result = usage_by_role([_legacy_consumed()])

    assert list(result["consumed"]["by_source"]) == ["remote:?:bob_glm"]
    group = result["consumed"]["by_source"]["remote:?:bob_glm"]
    assert group["node_id"] is None and group["alias"] == "bob_glm"


def test_a_local_alias_and_a_legacy_peer_row_of_one_name_are_two_keys():
    """The collision this key was changed for: keyed by the bare alias, a row
    written before the column carried the very string an own row of that name
    uses, so a node that both serves and consumes `bob_glm` had one line for
    two things in any reader that holds one map of keys
    (A-CONSUMED-ROW-WITH-NO-SERVED-BY-IS-KEYED-BY-THE-BARE-ALIAS)."""
    mine = _row(SEPT_1, request_id="own-1", alias="bob_glm")

    result = usage_by_role([_legacy_consumed(), mine])

    consumed, own = result["consumed"]["by_source"], result["own"]["by_alias"]
    assert list(consumed) == ["remote:?:bob_glm"] and list(own) == ["bob_glm"]
    assert set(consumed).isdisjoint(own), "no key of one list can be a key of the other"
    assert consumed["remote:?:bob_glm"]["row_count"] == own["bob_glm"]["row_count"] == 1


def test_two_hosts_serving_the_same_alias_are_two_lines():
    first = usage_row(started_at=SEPT_1, **CONSUMED_FIELDS)
    second = usage_row(
        started_at=SEPT_1, **{**CONSUMED_FIELDS, "request_id": "c2", "served_by": ALICE}
    )

    result = usage_by_role([first, second])

    assert set(result["consumed"]["by_source"]) == {
        f"remote:{BOB}:bob_glm", f"remote:{ALICE}:bob_glm",
    }


def test_a_gift_is_counted_apart_from_a_tariff_of_zero():
    """`untariffed` is «nobody declared a rate»; a declared 0 is a price, and
    it lands in the money with an amount of 0.0."""
    gift = usage_row(
        started_at=SEPT_1, **{k: v for k, v in SERVED_FIELDS.items() if not k.startswith("tariff")}
    )
    free = usage_row(
        started_at=SEPT_1,
        **{**SERVED_FIELDS, "request_id": "free-1", "tariff_in": 0.0, "tariff_out": 0.0,
           "tariff_amount": 0.0},
    )

    served = usage_by_role([gift, free])["served"]["by_alias"]["ollama_local"]

    assert served["untariffed"] == 1
    assert served["tariff"] == {"EUR": {"amount": 0.0, "rows": 1}}
    assert served["tariff_unpriceable"] == 0


def test_a_window_excludes_a_row_outside_it_on_every_series():
    result = usage_by_role(
        _four_rows(), since="2026-09-02T00:00:00Z", until="2026-09-30T23:59:59Z"
    )

    assert result["served"]["by_caller"] == {} and result["own"]["by_alias"] == {}
    only = result["consumed"]["by_source"][f"remote:{BOB}:bob_glm"]
    assert only["row_count"] == 1 and only["tariff_unpriceable"] == 1
    assert only["tariff"] == {}
    assert result["since"] == "2026-09-02T00:00:00Z"


def test_a_malformed_bound_is_refused_not_silently_ignored():
    with pytest.raises(ValueError):
        usage_by_role(_four_rows(), since="last week")


# --- (4) the owner's burn reader is unchanged beside it -------------------------------


def test_the_burn_summary_still_answers_exactly_what_it_did():
    """`get_usage_summary` folds `owner_rows` — every local row, this node's
    dollars whoever asked — and the role reader adds no column to it."""
    rows = _four_rows()

    summary = summarize(node_ledger.owner_rows(rows))

    assert set(summary) == {"row_count", "since", "until", "by_caller", "by_alias", "by_month"}
    assert summary["row_count"] == 2
    assert set(summary["by_caller"]) == {ALICE, "agent_001"}
    assert set(summary["by_alias"]) == {"ollama_local", "ds_flash"}
    assert set(summary["by_caller"][ALICE]) == {
        "row_count", "prompt_tokens", "completion_tokens", "thinking_tokens",
        "cost_usd", "unpriced", "peer_proved", "output_includes_thinking",
    }


# --- (5) the command ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_local_api_answers_with_the_three_series(tmp_path, monkeypatch):
    from dpc_client_core.local_api import ALLOWED_COMMANDS, LocalApiServer
    from dpc_client_core.service import CoreService

    assert "get_inference_usage" in ALLOWED_COMMANDS

    ledger = node_ledger.default_ledger()
    for row in _four_rows():
        ledger.append(row)

    class FakeCore:
        get_inference_usage = CoreService.get_inference_usage

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "ok", "command": "get_inference_usage", "payload": {}}),
        json.dumps({"id": "month", "command": "get_inference_usage",
                    "payload": {"month": "2026-09"}}),
        json.dumps({"id": "bad-date", "command": "get_inference_usage",
                    "payload": {"since": "yesterday"}}),
        json.dumps({"id": "bad-month", "command": "get_inference_usage",
                    "payload": {"month": "September"}}),
    ])

    await server._handler(ws)
    answers = {m.get("id"): m for m in ws.sent if m.get("id")}

    ok = answers["ok"]["payload"]
    assert ok["status"] == "success"
    assert list(ok["served"]["by_caller"]) == [ALICE]
    assert list(ok["consumed"]["by_source"]) == [f"remote:{BOB}:bob_glm"]
    assert list(ok["own"]["by_alias"]) == ["ds_flash"]
    assert answers["month"]["payload"]["served"] == ok["served"]

    for bad, word in (("bad-date", "yesterday"), ("bad-month", "September")):
        payload = answers[bad]["payload"]
        assert payload["status"] == "error" and word in payload["message"]


class _FakeWS:
    """Feeds frames to the handler and records what it answers — the same fake
    used in tests/test_local_api.py."""

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
