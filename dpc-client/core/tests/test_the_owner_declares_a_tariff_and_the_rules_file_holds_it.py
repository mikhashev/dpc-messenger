"""The owner declares a tariff in privacy_rules.json, and the rules file holds it.

ADR-041 D3, amendment of 2026-09-10: a shared alias has a price the owner sets —
`compute.serving_tariff`, dated entries per alias in `compute.currency` — and a
subset of the allowed peers, `compute.free_nodes` / `compute.free_groups`, who
get it at zero. Three states, distinct on purpose: `None` is «not declared»,
the gift; zero is «declared free»; more is paid. A free list distinguishes
inside a declared tariff and never stands in for one, so with nothing
declared a free peer gets `None` like everyone else. The block is validated
the way the serving lists are — at load and at save, by one function — so a
hand edit and the UI are refused with the same sentence.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from dpc_client_core import provider_alias_refs
from dpc_client_core.firewall import ISO_4217_CODES, AppliedTariff, ContextFirewall

LOCAL = "ollama_local"
VENDOR = "ds_flash"
ALICE = "dpc-node-alice-123"
BOB = "dpc-node-bob-456"
CAROL = "dpc-node-carol-789"

DECLARED = {
    "enabled": True,
    "currency": "RUB",
    "allow_nodes": [ALICE, BOB],
    "allow_groups": ["friends"],
    "free_nodes": [ALICE],
    "free_groups": ["friends"],
    "serving_local": [LOCAL],
    "serving_vendor": [VENDOR],
    "vendor_quotas": {VENDOR: 5.0},
    "serving_tariff": {
        "_comment": "per 1M tokens, in currency",
        LOCAL: [
            {"from": "2026-09-01", "in": 20, "out": 60},
            {"from": "2026-06-01", "in": 10, "out": 30},
        ],
        VENDOR: [{"from": "2026-08-15", "in": 100.5, "out": 400}],
    },
}
NODE_GROUPS = {"friends": [CAROL], "_comment": "peers"}


def _rules(compute: dict) -> dict:
    return {"compute": compute, "node_groups": NODE_GROUPS}


def _firewall(tmp_path: Path, compute: dict) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps(_rules(compute)), encoding="utf-8")
    return ContextFirewall(rules)


def _refused(tmp_path: Path, compute: dict) -> str:
    with pytest.raises(ValueError) as refused:
        _firewall(tmp_path, compute)
    return str(refused.value)


def _at(day: str) -> datetime:
    return datetime.fromisoformat(day + "T12:00:00+00:00")


# --- (1) a valid block loads and the resolver applies the newest entry ----------------


def test_a_valid_block_loads_and_the_newest_entry_at_or_before_the_date_applies(tmp_path):
    fw = _firewall(tmp_path, DECLARED)

    assert fw.compute_currency == "RUB"
    assert fw.compute_free_nodes == [ALICE]
    assert fw.compute_free_groups == ["friends"]
    assert set(fw.compute_serving_tariff) == {LOCAL, VENDOR}, "the comment key is not an alias"

    applied = fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10"))
    assert applied == AppliedTariff(in_per_1m=20.0, out_per_1m=60.0, currency="RUB", at=date(2026, 9, 1))

    # On the day an entry starts it already applies; the day before, the older one does.
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-01")).at == date(2026, 9, 1)
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-08-31")) == AppliedTariff(10.0, 30.0, "RUB", date(2026, 6, 1))
    assert fw.tariff_for(VENDOR, peer_id=BOB, at=_at("2026-09-10")) == AppliedTariff(100.5, 400.0, "RUB", date(2026, 8, 15))

    # The date is taken in UTC: 23:30 on the 31st in UTC+2 is still the 31st in UTC.
    late_local = datetime.fromisoformat("2026-09-01T00:30:00+02:00")
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=late_local).at == date(2026, 6, 1)
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=date(2026, 9, 1)).at == date(2026, 9, 1)


def test_the_applied_tariff_is_frozen_and_a_naive_moment_is_refused(tmp_path):
    fw = _firewall(tmp_path, DECLARED)
    applied = fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10"))
    with pytest.raises(Exception):
        applied.in_per_1m = 0.0  # type: ignore[misc]
    with pytest.raises(ValueError):
        fw.tariff_for(LOCAL, peer_id=BOB, at=datetime(2026, 9, 10))


# --- (2) before the earliest entry, and without a currency, nothing is declared --------


def test_a_call_before_the_earliest_entry_and_an_alias_without_entries_get_none(tmp_path):
    fw = _firewall(tmp_path, DECLARED)
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-05-31")) is None
    assert fw.tariff_for("unpriced_alias", peer_id=BOB, at=_at("2026-09-10")) is None


def test_without_a_currency_every_tariff_is_none_and_the_load_warns_once(tmp_path, caplog):
    undeclared = {k: v for k, v in DECLARED.items() if k != "currency"}
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.firewall"):
        fw = _firewall(tmp_path, undeclared)

    assert fw.compute_currency is None
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10")) is None
    assert fw.tariff_for(VENDOR, peer_id=BOB, at=_at("2026-09-10")) is None
    warned = [r for r in caplog.records if "currency" in r.getMessage() and "serving_tariff" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING

    # A node that declares no tariff at all has nothing to be warned about.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.firewall"):
        _firewall(tmp_path, {"enabled": True, "serving_local": [LOCAL]})
    assert not [r for r in caplog.records if "currency" in r.getMessage()]


# --- (3) a free peer gets zeros inside a declared tariff, and none outside one ---------


def test_a_free_node_and_a_member_of_a_free_group_get_zeros_with_the_currency_and_the_date(tmp_path):
    fw = _firewall(tmp_path, DECLARED)
    zeros = AppliedTariff(in_per_1m=0.0, out_per_1m=0.0, currency="RUB", at=date(2026, 9, 1))

    assert fw.tariff_for(LOCAL, peer_id=ALICE, at=_at("2026-09-10")) == zeros
    assert fw.tariff_for(LOCAL, peer_id=CAROL, at=_at("2026-09-10")) == zeros
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10")).in_per_1m == 20.0

    # Free is not a tariff of its own: before the first entry there is nothing to be free of.
    assert fw.tariff_for(LOCAL, peer_id=ALICE, at=_at("2026-05-31")) is None


def test_a_free_peer_gets_none_when_nothing_is_declared(tmp_path):
    no_tariff = {k: v for k, v in DECLARED.items() if k != "serving_tariff"}
    fw = _firewall(tmp_path, no_tariff)
    assert fw.tariff_for(LOCAL, peer_id=ALICE, at=_at("2026-09-10")) is None
    assert fw.tariff_for(LOCAL, peer_id=CAROL, at=_at("2026-09-10")) is None

    no_currency = {k: v for k, v in DECLARED.items() if k != "currency"}
    fw = _firewall(tmp_path, no_currency)
    assert fw.tariff_for(LOCAL, peer_id=ALICE, at=_at("2026-09-10")) is None


# --- (4) a free list is a subset of the allow list --------------------------------------


def test_a_free_node_outside_the_allow_list_is_refused_at_load_and_at_save_with_one_sentence(tmp_path):
    stranger = "dpc-node-stranger-000"
    compute = dict(DECLARED, free_nodes=[ALICE, stranger])
    reason = _refused(tmp_path, compute)
    assert stranger in reason and "free_nodes" in reason and "allow_nodes" in reason

    ok, errors = ContextFirewall.validate_config(_rules(compute))
    assert ok is False
    assert [e for e in errors if stranger in e] and any(e in reason for e in errors if stranger in e)

    reason = _refused(tmp_path, dict(DECLARED, free_groups=["friends", "family"]))
    assert "family" in reason and "free_groups" in reason and "allow_groups" in reason

    for bad in ("dpc-node-x", [1], {"a": 1}):
        reason = _refused(tmp_path, dict(DECLARED, free_nodes=bad))
        assert "free_nodes" in reason, bad


# --- (5) the currency is an ISO 4217 code, from the bundled table ----------------------


@pytest.mark.parametrize("bad", ["rub", "RUB ", "XYZ", "123", "руб", "", 840, ["RUB"]])
def test_a_string_that_is_not_an_iso_4217_code_is_refused_as_the_currency(tmp_path, bad):
    reason = _refused(tmp_path, dict(DECLARED, currency=bad))
    assert "currency" in reason and "ISO 4217" in reason


def test_an_explicit_null_is_not_declared_which_is_what_the_template_writes(tmp_path):
    fw = _firewall(tmp_path, dict(DECLARED, currency=None))
    assert fw.compute_currency is None
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10")) is None


@pytest.mark.parametrize("good", ["RUB", "USD", "EUR"])
def test_a_real_code_passes(tmp_path, good):
    assert _firewall(tmp_path, dict(DECLARED, currency=good)).compute_currency == good


def test_the_bundled_table_is_the_standard_and_not_a_shape():
    assert {"RUB", "USD", "EUR", "CNY", "GBP", "JPY"} <= ISO_4217_CODES
    assert "XYZ" not in ISO_4217_CODES and "XXX" not in ISO_4217_CODES
    assert 150 < len(ISO_4217_CODES) < 200
    assert all(len(code) == 3 and code.isupper() and code.isalpha() for code in ISO_4217_CODES)


# --- (6) a bad entry is refused naming the alias and the field --------------------------


@pytest.mark.parametrize("entries, field", [
    ([{"from": "2026-09-01", "in": -1, "out": 60}], "in"),
    ([{"from": "2026-09-01", "in": 20, "out": True}], "out"),
    ([{"from": "2026-09-01", "in": "20", "out": 60}], "in"),
    ([{"from": "2026-09-01", "in": 20}], "out"),
    ([{"from": "September 1st", "in": 20, "out": 60}], "from"),
    ([{"from": "2026-13-01", "in": 20, "out": 60}], "from"),
    ([{"from": "2026-9-1", "in": 20, "out": 60}], "from"),
    ([{"from": "2026-09-01", "in": 20, "out": 60}, {"from": "2026-09-01", "in": 1, "out": 1}], "from"),
    ([["2026-09-01", 20, 60]], "serving_tariff"),
    ({"from": "2026-09-01", "in": 20, "out": 60}, "serving_tariff"),
])
def test_a_bad_entry_is_refused_naming_the_alias_and_the_field(tmp_path, entries, field):
    compute = dict(DECLARED, serving_tariff={LOCAL: entries})
    reason = _refused(tmp_path, compute)
    assert LOCAL in reason and field in reason, reason

    ok, errors = ContextFirewall.validate_config(_rules(compute))
    assert ok is False and any(LOCAL in e and field in e for e in errors)


def test_a_tariff_for_an_alias_this_node_does_not_serve_is_a_warning_not_a_refusal(tmp_path, caplog):
    compute = dict(DECLARED, serving_tariff={"museum_piece": [{"from": "2026-01-01", "in": 1, "out": 1}]})
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.firewall"):
        fw = _firewall(tmp_path, compute)
    assert fw.tariff_for("museum_piece", peer_id=BOB, at=_at("2026-09-10")).in_per_1m == 1.0
    warned = [r for r in caplog.records if "museum_piece" in r.getMessage() and "serving_tariff" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING


def test_a_block_that_is_not_an_object_is_refused(tmp_path):
    for bad in ([], "x", 3):
        reason = _refused(tmp_path, dict(DECLARED, serving_tariff=bad))
        assert "serving_tariff" in reason, bad


# --- (7) a rename follows the tariff key -------------------------------------------------


def test_renaming_an_alias_follows_the_tariff_key_and_keeps_its_place(tmp_path):
    (tmp_path / "privacy_rules.json").write_text(json.dumps(_rules(DECLARED)), encoding="utf-8")

    found = provider_alias_refs.find_references(LOCAL, tmp_path)
    assert "privacy_rules.json:compute.serving_tariff" in found

    provider_alias_refs.rename_references(LOCAL, "renamed_local", tmp_path)
    compute = json.loads((tmp_path / "privacy_rules.json").read_text(encoding="utf-8"))["compute"]
    assert list(compute["serving_tariff"]) == ["_comment", "renamed_local", VENDOR]
    assert compute["serving_tariff"]["renamed_local"] == DECLARED["serving_tariff"][LOCAL]
    assert "renamed_local" not in provider_alias_refs.unresolved_references([LOCAL, VENDOR], tmp_path)

    fw = ContextFirewall(tmp_path / "privacy_rules.json")
    assert fw.tariff_for("renamed_local", peer_id=BOB, at=_at("2026-09-10")).in_per_1m == 20.0


# --- (8) a reload that introduces a refused block keeps the old rules --------------------


def test_a_reload_that_introduces_a_refused_block_is_refused_and_the_old_rules_stand(tmp_path):
    fw = _firewall(tmp_path, DECLARED)
    broken = dict(DECLARED, currency="rub", free_nodes=[ALICE, "dpc-node-stranger-000"])
    fw.access_file_path.write_text(json.dumps(_rules(broken)), encoding="utf-8")

    ok, message = fw.reload()

    assert ok is False and "currency" in message and "dpc-node-stranger-000" in message
    assert fw.compute_currency == "RUB" and fw.compute_free_nodes == [ALICE]
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10")).in_per_1m == 20.0


def test_the_default_template_declares_nothing_and_says_where_to(tmp_path):
    fw = ContextFirewall(tmp_path / "fresh" / "privacy_rules.json")
    compute = json.loads(fw.access_file_path.read_text(encoding="utf-8"))["compute"]
    assert compute["currency"] is None and compute["serving_tariff"] == {}
    assert compute["free_nodes"] == [] and compute["free_groups"] == []
    assert {"_currency", "_serving_tariff", "_free_nodes"} <= set(compute)
    assert fw.tariff_for(LOCAL, peer_id=BOB, at=_at("2026-09-10")) is None
