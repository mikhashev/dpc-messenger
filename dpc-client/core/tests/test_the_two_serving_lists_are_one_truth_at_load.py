"""privacy_rules.json names what this node serves in two lists, and one truth.

`compute.serving_local` and `compute.serving_vendor` replace the single
`serving_alias` (ADR-041 D5): the first is bounded by the card, the second by
a daily ceiling in `compute.vendor_quotas`, so a vendor alias with no ceiling
is a configuration error refused at load with a named reason, never a
default. The old key is still read — folded into `serving_local` with one
WARNING — and the P2P door keeps reading `compute_serving_alias`, which is
now the first local entry. Two keys that disagree are refused, not
reconciled. Classification by provider type is the firewall's rule too, and
an alias that is somebody else's model may stand in neither list (D7).
"""

import json
import logging
from pathlib import Path

import pytest

from dpc_client_core import provider_alias_refs
from dpc_client_core.firewall import ContextFirewall

LOCAL = "ollama_local"
VENDOR = "ds_flash"


def _firewall(tmp_path: Path, compute: dict) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    return ContextFirewall(rules)


def _refused(tmp_path: Path, compute: dict) -> str:
    with pytest.raises(ValueError) as refused:
        _firewall(tmp_path, compute)
    return str(refused.value)


# --- the fold ------------------------------------------------------------------------


def test_serving_alias_alone_is_folded_into_serving_local_with_one_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.firewall"):
        fw = _firewall(tmp_path, {"enabled": True, "serving_alias": LOCAL})
    assert fw.compute_serving_local == [LOCAL]
    assert fw.compute_serving_alias == LOCAL
    warnings = [r for r in caplog.records if "serving_alias" in r.getMessage() and "serving_local" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


def test_the_p2p_door_reads_the_first_local_entry_and_none_when_the_list_is_empty(tmp_path):
    fw = _firewall(tmp_path, {"serving_local": [LOCAL, "second_local"]})
    assert fw.compute_serving_alias == LOCAL
    assert _firewall(tmp_path, {"serving_local": []}).compute_serving_alias is None
    assert _firewall(tmp_path, {}).compute_serving_alias is None
    assert _firewall(tmp_path, {}).compute_serving_local == []


def test_both_keys_agreeing_is_read_without_a_fold(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.firewall"):
        fw = _firewall(tmp_path, {"serving_alias": LOCAL, "serving_local": [LOCAL]})
    assert fw.compute_serving_local == [LOCAL]
    assert not [r for r in caplog.records if "serving_local" in r.getMessage()]


def test_both_keys_disagreeing_is_refused_at_load_naming_both(tmp_path):
    reason = _refused(tmp_path, {"serving_alias": "old_alias", "serving_local": [LOCAL]})
    assert "old_alias" in reason and LOCAL in reason
    assert "serving_alias" in reason and "serving_local" in reason

    reason = _refused(tmp_path, {"serving_alias": "old_alias", "serving_local": []})
    assert "old_alias" in reason and "serving_local" in reason


def test_a_reload_that_introduces_a_disagreement_is_refused_and_the_old_rules_stand(tmp_path):
    fw = _firewall(tmp_path, {"serving_local": [LOCAL]})
    fw.access_file_path.write_text(
        json.dumps({"compute": {"serving_alias": "other", "serving_local": [LOCAL]}}), encoding="utf-8",
    )
    ok, message = fw.reload()
    assert ok is False and "other" in message and LOCAL in message
    assert fw.compute_serving_local == [LOCAL]


# --- (5) a vendor alias needs a ceiling ------------------------------------------------


def test_a_vendor_alias_without_a_quota_is_refused_at_load_with_the_reason(tmp_path):
    reason = _refused(tmp_path, {"serving_vendor": [VENDOR]})
    assert VENDOR in reason and "vendor_quotas" in reason and "serving_vendor" in reason

    reason = _refused(tmp_path, {"serving_vendor": [VENDOR], "vendor_quotas": {"another": 1.0}})
    assert VENDOR in reason and "vendor_quotas" in reason


def test_the_same_refusal_reaches_validate_config_so_the_ui_save_is_refused(tmp_path):
    ok, errors = ContextFirewall.validate_config({"compute": {"serving_vendor": [VENDOR]}})
    assert ok is False
    assert any(VENDOR in e and "vendor_quotas" in e for e in errors)


def test_quotas_are_read_as_dollars_per_day_per_caller_and_a_bad_quota_is_refused(tmp_path):
    fw = _firewall(tmp_path, {"serving_vendor": [VENDOR], "vendor_quotas": {VENDOR: 2, "_comment": "usd/day"}})
    assert fw.compute_vendor_quotas == {VENDOR: 2.0}
    assert fw.compute_serving_vendor == [VENDOR]

    for bad in ({VENDOR: "2"}, {VENDOR: -1}, {VENDOR: True}, {VENDOR: None}, [VENDOR]):
        reason = _refused(tmp_path, {"serving_vendor": [VENDOR], "vendor_quotas": bad})
        assert "vendor_quotas" in reason, bad


def test_an_alias_in_both_lists_and_a_list_that_is_not_a_list_are_refused(tmp_path):
    reason = _refused(tmp_path, {"serving_local": [LOCAL], "serving_vendor": [LOCAL], "vendor_quotas": {LOCAL: 1}})
    assert LOCAL in reason and "both" in reason

    for bad in ("ollama_local", [1], [""], {"a": 1}):
        reason = _refused(tmp_path, {"serving_local": bad})
        assert "serving_local" in reason, bad


# --- classification by provider type (D5, D7) ----------------------------------------


def test_the_lists_are_classified_by_provider_type_and_an_unloaded_alias_keeps_its_place(tmp_path):
    fw = _firewall(tmp_path, {"serving_local": [LOCAL, "not_loaded"], "serving_vendor": [VENDOR],
                              "vendor_quotas": {VENDOR: 1.5}})
    lists = fw.classify_serving_lists({LOCAL: "ollama", VENDOR: "deepseek", "paid_default": "anthropic"})
    assert lists.local == (LOCAL, "not_loaded")
    assert lists.vendor == (VENDOR,)
    assert lists.quotas == {VENDOR: 1.5}
    assert lists.owner_of(LOCAL) == "local" and lists.owner_of(VENDOR) == "vendor"
    assert lists.owner_of("paid_default") is None


@pytest.mark.parametrize("list_key, type_", [
    ("serving_local", "remote_peer"), ("serving_local", "dpc_agent"),
    ("serving_vendor", "remote_peer"), ("serving_vendor", "dpc_agent"),
])
def test_an_alias_that_is_itself_remote_is_refused_in_either_list(tmp_path, list_key, type_):
    fw = _firewall(tmp_path, {list_key: ["relay"], "vendor_quotas": {"relay": 1.0}})
    with pytest.raises(ValueError) as refused:
        fw.classify_serving_lists({"relay": type_})
    reason = str(refused.value)
    assert "relay" in reason and type_ in reason
    assert "shared onward" in reason, "refused for the D7 reason, not as an unclassified type"


def test_a_vendor_type_under_local_and_a_local_type_under_vendor_are_refused_by_name(tmp_path):
    fw = _firewall(tmp_path, {"serving_local": [VENDOR]})
    with pytest.raises(ValueError) as refused:
        fw.classify_serving_lists({VENDOR: "deepseek"})
    assert VENDOR in str(refused.value) and "deepseek" in str(refused.value) and "serving_vendor" in str(refused.value)

    fw = _firewall(tmp_path, {"serving_vendor": [LOCAL], "vendor_quotas": {LOCAL: 1.0}})
    with pytest.raises(ValueError) as refused:
        fw.classify_serving_lists({LOCAL: "ollama"})
    assert LOCAL in str(refused.value) and "ollama" in str(refused.value) and "serving_local" in str(refused.value)


def test_a_type_the_tables_do_not_know_is_refused_rather_than_guessed(tmp_path):
    fw = _firewall(tmp_path, {"serving_local": ["mystery"]})
    with pytest.raises(ValueError) as refused:
        fw.classify_serving_lists({"mystery": "brand_new_type"})
    assert "mystery" in str(refused.value) and "brand_new_type" in str(refused.value)


# --- a rename follows the lists, or the fold refuses the next load ---------------------


def test_renaming_an_alias_follows_both_lists_and_the_quota_key(tmp_path):
    home = tmp_path
    (home / "privacy_rules.json").write_text(json.dumps({
        "compute": {"serving_alias": LOCAL, "serving_local": [LOCAL], "serving_vendor": [VENDOR],
                    "vendor_quotas": {VENDOR: 1.0, "_comment": "usd/day"}},
    }), encoding="utf-8")

    found = provider_alias_refs.find_references(VENDOR, home)
    assert "privacy_rules.json:compute.serving_vendor" in found
    assert "privacy_rules.json:compute.vendor_quotas" in found

    provider_alias_refs.rename_references(LOCAL, "renamed_local", home)
    provider_alias_refs.rename_references(VENDOR, "renamed_vendor", home)
    compute = json.loads((home / "privacy_rules.json").read_text(encoding="utf-8"))["compute"]
    assert compute["serving_alias"] == "renamed_local"
    assert compute["serving_local"] == ["renamed_local"]
    assert compute["serving_vendor"] == ["renamed_vendor"]
    assert compute["vendor_quotas"] == {"renamed_vendor": 1.0, "_comment": "usd/day"}

    # And what was written loads: the fold sees the two keys agree.
    fw = ContextFirewall(home / "privacy_rules.json")
    assert fw.compute_serving_alias == "renamed_local"
