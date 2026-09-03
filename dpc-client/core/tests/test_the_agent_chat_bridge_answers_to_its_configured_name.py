"""The 1:1 agent-chat bridge scans for and prints the configured display name, not CC.

cc_agent_bridge.py is a standalone CLI, loaded by file path and pointed at a fake
DPC_HOME. A 1:1 chat has no Group Settings registration, so the only identity is
[agent_chat] cc_display_name (fallback "CC"); the backend still stamps the sender
(AN-EXTERNAL-AGENT-HAS-NO-IDENTITY-IN-A-ONE-TO-ONE-CHAT), which is not tested here.
"""

import importlib.util
import re
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "cc_agent_bridge.py"


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cc_agent_bridge_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "dpc"
    home.mkdir()
    monkeypatch.setattr(mod, "DPC_HOME", home)
    monkeypatch.setattr(mod, "CONFIG_PATH", home / "config.ini")   # absent → "CC"
    return mod


def _configure(bridge, name: str) -> None:
    bridge.CONFIG_PATH.write_text(f"[agent_chat]\ncc_display_name = {name}\n", encoding="utf-8")


def _msgs(*texts):
    return [{"sender_name": "Mike", "content": t} for t in texts]


def test_display_name_is_cc_when_nothing_is_configured(bridge):
    assert bridge._configured_display_name() == "CC"
    assert bridge._get_cc_display_name() == "CC"     # the historical reader still answers


def test_display_name_is_the_configured_value(bridge):
    _configure(bridge, "Fable")
    assert bridge._configured_display_name() == "Fable"


def test_mentions_follow_the_configured_name(bridge):
    _configure(bridge, "Fable")
    hits = bridge.find_mentions(_msgs("@Fable привет", "@fable, глянь", "@CC привет"))
    assert [i for i, _ in hits] == [0, 1]


def test_cyrillic_alias_only_when_the_name_is_cc(bridge):
    assert [i for i, _ in bridge.find_mentions(_msgs("@СС привет"))] == [0]
    _configure(bridge, "Fable")
    assert bridge.find_mentions(_msgs("@СС привет")) == []


def test_own_messages_are_skipped(bridge):
    _configure(bridge, "Fable")
    assert bridge.find_mentions([{"sender_name": "Fable", "content": "@Fable echo"}]) == []


def test_mentions_banner_names_the_configured_name(bridge):
    assert bridge._mentions_banner("Fable", 2) == "=== 2 mention(s) of @Fable ==="
    assert bridge._mentions_banner("CC", 0) == "=== 0 mention(s) of @CC ==="


def test_help_names_no_cc_outside_the_historical_identifiers(bridge):
    text = bridge._build_parser().format_help()
    scrubbed = re.sub(r"cc_agent_bridge\.py", "", text)
    assert re.search(r"\bcc\b", scrubbed, re.IGNORECASE) is None, scrubbed
    assert "Claude Code" not in text
