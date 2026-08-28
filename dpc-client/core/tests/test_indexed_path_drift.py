"""Path drift is reported to a person, and repaired only when the person says so.

Reconcile already repairs the list on every read, which is why indexing has been
correct while `privacy_rules.json` has held 52 entries pointing at nothing. The file
is what the user reads, so the drift has to be visible; and one branch of the repair
is "drop", so an unmounted disk must not lose its root because a service felt tidy.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.service import CoreService


def _rules(tmp_path: pathlib.Path, live: str, indexed: list) -> pathlib.Path:
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({
        "dpc_agent": {
            "enabled": True,
            "sandbox_extensions": {
                "read_only": [live],
                "read_write": [],
                "indexed_paths": indexed,
            },
        },
    }), encoding="utf-8")
    return path


class _Service:
    """CoreService is large and its constructor does far more than this needs."""

    def __init__(self, firewall):
        self.firewall = firewall

    get_indexed_path_drift = CoreService.get_indexed_path_drift
    repair_indexed_paths = CoreService.repair_indexed_paths


@pytest.fixture
def drifted(tmp_path):
    live = tmp_path / "project"
    live.mkdir()
    rules_path = _rules(tmp_path, str(live), [str(live), str(tmp_path / "gone")])
    return _Service(ContextFirewall(rules_path)), rules_path, live


def test_the_drift_is_reported_with_a_count_a_banner_can_use(drifted):
    service, _, _ = drifted

    report = asyncio.run(service.get_indexed_path_drift())

    assert report["status"] == "ok"
    assert report["total"] == 1
    assert report["scopes"][0]["scope"] == "dpc_agent"
    assert report["scopes"][0]["dropped"] == 1


def test_reporting_does_not_touch_the_file(drifted):
    """The whole point of splitting report from repair: looking must not decide."""
    service, rules_path, _ = drifted
    before = rules_path.read_text(encoding="utf-8")

    asyncio.run(service.get_indexed_path_drift())

    assert rules_path.read_text(encoding="utf-8") == before


def test_repair_writes_the_list_the_reader_already_uses(drifted):
    service, rules_path, live = drifted

    result = asyncio.run(service.repair_indexed_paths())

    assert result["status"] == "ok" and result["repaired"] == 1
    saved = json.loads(rules_path.read_text(encoding="utf-8"))
    assert saved["dpc_agent"]["sandbox_extensions"]["indexed_paths"] == [str(live)]


def test_repair_reports_what_it_did_rather_than_only_how_much(drifted):
    service, _, _ = drifted

    result = asyncio.run(service.repair_indexed_paths())

    assert result["details"] and "gone" in result["details"][0]


def test_a_clean_config_is_left_alone(tmp_path):
    live = tmp_path / "project"
    live.mkdir()
    rules_path = _rules(tmp_path, str(live), [str(live)])
    service = _Service(ContextFirewall(rules_path))
    before = rules_path.read_text(encoding="utf-8")

    assert asyncio.run(service.get_indexed_path_drift())["total"] == 0
    assert asyncio.run(service.repair_indexed_paths())["repaired"] == 0
    assert rules_path.read_text(encoding="utf-8") == before


def test_repair_is_idempotent(drifted):
    service, _, _ = drifted

    assert asyncio.run(service.repair_indexed_paths())["repaired"] == 1
    assert asyncio.run(service.repair_indexed_paths())["repaired"] == 0


def test_without_a_firewall_both_answer_rather_than_raise():
    service = _Service(None)

    assert asyncio.run(service.get_indexed_path_drift())["status"] == "error"
    assert asyncio.run(service.repair_indexed_paths())["status"] == "error"
