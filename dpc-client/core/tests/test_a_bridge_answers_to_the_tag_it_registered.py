"""The group bridge posts and listens as the tag it registered, not as every CC.

cc_group_chat_bridge.py is a standalone CLI, so it is loaded by file path and
pointed at a fake DPC_HOME: a node.id, one group with the two-node metadata
shape the backend writes (agents + agent_names), and an empty history.
"""

import importlib.util
import json
import re
import runpy
import sys
import types
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "cc_group_chat_bridge.py"
GROUP = "group-0a52389f2bb6"
NODE = "dpc-node-" + "a" * 32       # this machine, registered as CC_mike
OTHER = "dpc-node-" + "b" * 32      # the other machine, registered as CC_linux
NOBODY = "dpc-node-" + "c" * 32     # a member with nothing registered


def _write_home(home: Path, node_id: str, agents: dict, agent_names: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "node.id").write_text(node_id, encoding="utf-8")
    gdir = home / "conversations" / f"{GROUP}-1234"
    gdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "group_id": GROUP,
        "name": "1234",
        "members": [NODE, OTHER, NOBODY],
        "agents": agents,
        "agent_names": agent_names,
    }
    (gdir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (gdir / "history.json").write_text(json.dumps({"messages": []}), encoding="utf-8")


REAL_AGENTS = {NODE: ["ext:CC_mike"], OTHER: ["ext:CC_linux"]}
REAL_NAMES = {NODE: {"ext:CC_mike": "CC_mike"}, OTHER: {"ext:CC_linux": "CC_linux"}}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cc_group_chat_bridge_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "dpc"
    _write_home(home, NODE, REAL_AGENTS, REAL_NAMES)
    monkeypatch.setattr(mod, "DPC_HOME", home)
    monkeypatch.setattr(mod, "CONFIG_PATH", home / "config.ini")   # absent → "CC"
    return mod


def _set_node(bridge, node_id: str) -> None:
    (bridge.DPC_HOME / "node.id").write_text(node_id, encoding="utf-8")


# (i) registered tags come from this node's slot in metadata.json


def test_registered_tags_are_this_nodes_ext_entries(bridge):
    assert bridge._registered_tags(GROUP) == ["CC_mike"]


def test_registered_tags_are_empty_for_a_node_that_registered_nothing(bridge):
    _set_node(bridge, NOBODY)
    assert bridge._registered_tags(GROUP) == []


def test_registered_tags_are_empty_without_a_node_id(bridge):
    (bridge.DPC_HOME / "node.id").unlink()
    assert bridge._get_node_id() == ""
    assert bridge._registered_tags(GROUP) == []


def test_registered_tag_falls_back_to_the_bare_tag_without_agent_names(bridge):
    _write_home(bridge.DPC_HOME, NODE, REAL_AGENTS, {})
    assert bridge._registered_tags(GROUP) == ["CC_mike"]


# (ii) identity resolution: override → registered tag → cc_display_name


def test_identity_is_the_registered_tag(bridge):
    assert bridge._resolve_identity(GROUP, None) == "CC_mike"


def test_identity_is_the_override_when_given(bridge):
    assert bridge._resolve_identity(GROUP, "CC_other") == "CC_other"


def test_identity_is_cc_display_name_when_nothing_is_registered(bridge):
    _set_node(bridge, NOBODY)
    assert bridge._resolve_identity(GROUP, None) == "CC"


def test_several_tags_without_override_exit_2_for_send_and_all_match_for_read(bridge, capsys):
    _write_home(bridge.DPC_HOME, NODE,
                {NODE: ["ext:CC_mike", "ext:CC_night"]},
                {NODE: {"ext:CC_mike": "CC_mike", "ext:CC_night": "CC_night"}})
    assert bridge._identity_names(GROUP, None) == ["CC_mike", "CC_night"]
    with pytest.raises(SystemExit) as exc:
        bridge._resolve_identity(GROUP, None)
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "[ERROR] several tags registered on this node for group-0a52389f2bb6: CC_mike, CC_night" in out
    assert "--as" in out


# (iii) mentions match the tag as a whole word


def _msgs(*texts):
    return [{"sender_name": "Mike", "content": t} for t in texts]


def test_tag_mention_matches_whole_word_case_insensitively(bridge):
    hits = bridge.find_mentions(_msgs("@CC_mike привет", "@cc_mike, глянь"), names=["CC_mike"])
    assert [i for i, _ in hits] == [0, 1]


def test_tag_mention_does_not_match_bare_cc_or_a_longer_tag(bridge):
    hits = bridge.find_mentions(_msgs("@CC привет", "@CC_mike2 привет"), names=["CC_mike"])
    assert hits == []


def test_bare_cc_does_not_match_a_tagged_mention(bridge):
    assert bridge.find_mentions(_msgs("@CC_mike привет"), names=["CC"]) == []
    hits = bridge.find_mentions(_msgs("@CC привет", "@СС привет"), names=["CC"])
    assert [i for i, _ in hits] == [0, 1]


def test_cyrillic_alias_only_for_a_name_that_is_cc(bridge):
    assert bridge.find_mentions(_msgs("@СС привет"), names=["CC_mike"]) == []


def test_own_messages_are_skipped(bridge):
    own = [{"sender_name": "CC_mike", "content": "@CC_mike echo"}]
    assert bridge.find_mentions(own, names=["CC_mike"]) == []


def test_default_names_is_cc_display_name(bridge):
    hits = bridge.find_mentions(_msgs("@CC привет", "@CC_mike привет"))
    assert [i for i, _ in hits] == [0]


# (iv) the send command carries the resolved tag as agent_name


class _FakeWS:
    def __init__(self, sent):
        self.sent = sent
        self._replies = [json.dumps({"status": "OK"}), json.dumps({"status": "sent"})]

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return self._replies.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_websockets(bridge, monkeypatch):
    sent = []
    fake = types.ModuleType("websockets")
    fake.connect = lambda url: _FakeWS(sent)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake)
    (bridge.DPC_HOME / ".ws_token").write_text("tok", encoding="utf-8")
    return sent


def test_send_posts_as_the_registered_tag(bridge, fake_websockets, capsys):
    bridge.send_group_message_sync(GROUP, "hi")
    auth, cmd = fake_websockets
    assert auth["command"] == "auth"
    assert cmd["command"] == "send_group_agent_message"
    assert cmd["payload"] == {"group_id": GROUP, "agent_name": "CC_mike", "text": "hi"}
    out = capsys.readouterr().out
    assert "[INFO] posting as CC_mike" in out
    assert "[SENT] 2 chars → group group-0a52389f2bb6: sent" in out


def test_send_posts_as_cc_with_no_info_line_when_nothing_is_registered(bridge, fake_websockets, capsys):
    _set_node(bridge, NOBODY)
    bridge.send_group_message_sync(GROUP, "hi")
    assert fake_websockets[1]["payload"]["agent_name"] == "CC"
    assert "[INFO] posting as" not in capsys.readouterr().out


def test_send_honours_an_explicit_name(bridge, fake_websockets):
    bridge.send_group_message_sync(GROUP, "hi", "CC_night")
    assert fake_websockets[1]["payload"]["agent_name"] == "CC_night"


def test_build_send_command_is_the_backend_shape(bridge):
    cmd = bridge._build_send_command(GROUP, "CC_mike", "x")
    assert cmd["command"] == "send_group_agent_message"
    assert cmd["payload"] == {"group_id": GROUP, "agent_name": "CC_mike", "text": "x"}
    assert len(cmd["id"]) == 8


# (v) what the user reads names the resolved tag, never a hardcoded CC


def test_mentions_banner_names_the_tags_scanned_for(bridge):
    assert bridge._mentions_banner(["CC_mike"], 0) == "No mentions of @CC_mike found."
    assert bridge._mentions_banner(["CC_mike", "CC_night"], 3) == \
        "=== 3 mention(s) of @CC_mike, @CC_night ==="
    assert bridge._mentions_banner(["Fable"], 1) == "=== 1 mention(s) of @Fable ==="


def _run_cli(bridge, monkeypatch, capsys, *argv):
    """Run the bridge as __main__ against the fixture home; return stdout."""
    monkeypatch.setenv("DPC_HOME", str(bridge.DPC_HOME))
    monkeypatch.setattr(sys, "argv", ["cc_group_chat_bridge.py", *argv])
    runpy.run_path(str(BRIDGE), run_name="__main__")
    return capsys.readouterr().out


def test_mentions_output_names_the_registered_tag_and_keeps_the_parsed_line(bridge, monkeypatch, capsys):
    out = _run_cli(bridge, monkeypatch, capsys, "--group", GROUP, "--mentions")
    assert out == "[CC Group Bridge] 0 messages (last 10)\n\nNo mentions of @CC_mike found.\n"

    gdir = bridge._find_group_dir(GROUP)
    history = {"messages": [
        {"sender_name": "Mike", "content": "@CC привет", "timestamp": "2026-09-04T10:00:00"},
        {"sender_name": "Mike", "content": "@CC_mike глянь", "timestamp": "2026-09-04T10:00:01"},
    ]}
    (gdir / "history.json").write_text(json.dumps(history), encoding="utf-8")
    out = _run_cli(bridge, monkeypatch, capsys, "--group", GROUP, "--mentions")
    assert out == ("[CC Group Bridge] 2 messages (last 10)\n\n"
                   "=== 1 mention(s) of @CC_mike ===\n"
                   "  [2] 10:00:01 Mike: @CC_mike глянь\n")


def test_mentions_output_names_the_configured_name_when_nothing_is_registered(bridge, monkeypatch, capsys):
    _set_node(bridge, NOBODY)
    (bridge.DPC_HOME / "config.ini").write_text("[agent_chat]\ncc_display_name = Fable\n",
                                                encoding="utf-8")
    out = _run_cli(bridge, monkeypatch, capsys, "--group", GROUP, "--mentions")
    assert out.endswith("No mentions of @Fable found.\n")


def test_help_names_no_cc_outside_the_historical_identifiers(bridge):
    text = bridge._build_parser().format_help()
    assert "cc_display_name" in text          # the config key is allowed to keep its name
    scrubbed = re.sub(r"cc_display_name|cc_group_chat_bridge\.py|cc_group_mention", "", text)
    assert re.search(r"\bcc\b", scrubbed, re.IGNORECASE) is None, scrubbed
    assert "Claude Code" not in text
