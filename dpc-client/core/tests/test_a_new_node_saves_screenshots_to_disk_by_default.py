"""A new node keeps pasted screenshots on disk unless its owner turns it off.

With `save_screenshots_to_disk` false the chat showed no preview of a
screenshot, and false was what every fresh privacy_rules.json said
(Mike's call, 2026-10-25: on by default). An owner's explicit false stays.
"""
import json
from pathlib import Path

from dpc_client_core.firewall import ContextFirewall

EXAMPLE = Path(__file__).resolve().parents[2] / "privacy_rules.example.json"


def test_a_fresh_rules_file_saves_screenshots(tmp_path):
    path = tmp_path / "privacy_rules.json"
    ContextFirewall(path)
    rules = json.loads(path.read_text(encoding="utf-8"))
    assert rules["image_transfer"]["save_screenshots_to_disk"] is True


def test_the_example_rules_say_the_same(tmp_path):
    rules = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert rules["image_transfer"]["save_screenshots_to_disk"] is True


def test_an_owners_explicit_false_is_left_alone(tmp_path):
    path = tmp_path / "privacy_rules.json"
    ContextFirewall(path)
    rules = json.loads(path.read_text(encoding="utf-8"))
    rules["image_transfer"]["save_screenshots_to_disk"] = False
    path.write_text(json.dumps(rules, indent=2), encoding="utf-8")

    firewall = ContextFirewall(path)

    assert firewall.rules["image_transfer"]["save_screenshots_to_disk"] is False
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["image_transfer"]["save_screenshots_to_disk"] is False
