"""The group bridge posts and listens as the tag it registered, not as every CC.

cc_group_chat_bridge.py is a standalone CLI, so it is loaded by file path and
pointed at a fake DPC_HOME: a node.id, one group with the two-node metadata
shape the backend writes (agents + agent_names), and an empty history.
"""

import importlib.util
import json
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
