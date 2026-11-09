"""A group's session archive opens only to who witnessed it (ARCHIVE-GROUP-ACCESS).

Mike's call, 2026-11-09 (review by Johnny and Zcode): the writer records the group
(`group_id`, `members`, `version`) in every archive of a group conversation; an owner
turn reads its own conversation and, with `history.archive_include_groups` on, the
archives of the groups whose agents map lists this agent; a turn in a group with
another node reads that group only, and only sessions whose recorded members include
every current member; a stranger keeps its own Telegram chat. Both sides must allow
(agents map and flag), and a group whose metadata is missing, broken or lists no
agents is not included.
"""

from __future__ import annotations

import json
import logging
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.agent import (
    ALL_MEMORY_LAYERS, OTHER_NODE_TURN, OWNER_TURN, STRANGER_TURN, DpcAgent,
)
from dpc_client_core.dpc_agent.tools import archive
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.group_manager import GroupManager

THIS = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
LATE = "dpc-node-" + "c" * 32
AGENT = "agent_x"
SAME_NAME = "2026-11-01T10-00-00_reset_session.json"
UNRECORDED_NAME = "2026-11-02T10-00-00_reset_session.json"
FEWER_NAME = "2026-11-03T10-00-00_new_session_session.json"


# -- the writer ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


def _history(monitor: ConversationMonitor) -> None:
    record = {"id": "m1", "role": "user", "content": "hello", "timestamp": "2026-11-09T01:00:00Z"}
    monitor.message_history = [record]
    path = monitor._get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conversation_id": monitor.conversation_id, "version": 1,
                                "messages": [record]}), encoding="utf-8")


def _archives(monitor: ConversationMonitor) -> list:
    folder = monitor._get_history_path().parent / "archive"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.rglob("*_session.json"))]


@pytest.mark.parametrize("wipe", ["reset_conversation", "clear_history"])
def test_the_writer_records_the_group_at_reset_and_at_a_new_session(home, wipe):
    manager = GroupManager(home / ".dpc", THIS)
    group = manager.create_group("Work", "", [THIS, PEER])
    manager.set_node_agents(group.group_id, THIS, [AGENT])  # version moves to 2
    monitor = ConversationMonitor(group.group_id, [], None)
    _history(monitor)

    getattr(monitor, wipe)()

    [data] = _archives(monitor)
    assert data["group"] == {"group_id": group.group_id, "members": [THIS, PEER], "version": 2}
    assert data["session_reason"] == ("reset" if wipe == "reset_conversation" else "new_session")


def test_a_one_to_one_archive_gets_no_group_key(home):
    monitor = ConversationMonitor("agent_x", [], None)
    _history(monitor)
    monitor.reset_conversation()
    [data] = _archives(monitor)
    assert "group" not in data


@pytest.mark.parametrize("metadata", [None, "{not json", json.dumps({"group_id": "group-other",
                                                                      "members": [THIS]})],
                         ids=["missing", "broken", "another-group"])
def test_a_group_that_cannot_be_found_gets_nothing_extra_and_one_line(home, caplog, metadata):
    monitor = ConversationMonitor("group-123456789abc", [], None)
    _history(monitor)
    if metadata is not None:
        (monitor._get_history_path().parent / "metadata.json").write_text(metadata, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.conversation_monitor"):
        monitor.reset_conversation()
    [data] = _archives(monitor)
    assert "group" not in data
    lines = [r for r in caplog.records if "records no group" in r.getMessage()]
    assert len(lines) == 1


# -- the reader ---------------------------------------------------------------


def _session(conv_dir: pathlib.Path, name: str, text: str, group=None) -> None:
    month = conv_dir / "archive" / "2026" / "11"
    month.mkdir(parents=True, exist_ok=True)
    data = {"archived_at": "2026-11-01T10:00:00Z", "session_reason": "reset",
            "messages": [{"role": "user", "content": text, "timestamp": "2026-11-01T10:00:00Z"}]}
    if group is not None:
        data["group"] = group
    (month / name).write_text(json.dumps(data), encoding="utf-8")


def _meta(conv_dir: pathlib.Path, group_id: str, members, agents) -> None:
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / "metadata.json").write_text(json.dumps({
        "group_id": group_id, "name": group_id, "members": members, "agents": agents,
        "version": 3,
    }), encoding="utf-8")


def _record(group_id, members):
    return {"group_id": group_id, "members": members, "version": 3}


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "conversations"
    # The agent's own 1:1 conversation. Its file shares a name with one in group-aaa.
    _session(base / AGENT, SAME_NAME, "own line")
    # A group this agent is in, with a peer: one session witnessed by both, one with no
    # record, one held before the peer joined.
    aaa = base / "group-aaaaaaaaaaaa-work"
    _meta(aaa, "group-aaaaaaaaaaaa", [THIS, PEER], {THIS: [AGENT], PEER: ["agent_p"]})
    _session(aaa, SAME_NAME, "witnessed line", _record("group-aaaaaaaaaaaa", [THIS, PEER]))
    _session(aaa, UNRECORDED_NAME, "unrecorded line")
    _session(aaa, FEWER_NAME, "fewer line", _record("group-aaaaaaaaaaaa", [THIS]))
    # Groups this agent is not in, or whose metadata fails closed.
    bbb = base / "group-bbbbbbbbbbbb"
    _meta(bbb, "group-bbbbbbbbbbbb", [THIS, PEER], {THIS: ["agent_other"]})
    _session(bbb, SAME_NAME, "bbb line", _record("group-bbbbbbbbbbbb", [THIS, PEER]))
    _session(base / "group-cccccccccccc", SAME_NAME, "ccc line")  # no metadata.json
    ddd = base / "group-dddddddddddd"
    _session(ddd, SAME_NAME, "ddd line")
    (ddd / "metadata.json").write_text("{broken", encoding="utf-8")
    eee = base / "group-eeeeeeeeeeee"
    _meta(eee, "group-eeeeeeeeeeee", [THIS, PEER], {})
    _session(eee, SAME_NAME, "eee line")
    fff = base / "group-ffffffffffff"  # the same agent id, but another node's agent
    _meta(fff, "group-ffffffffffff", [THIS, PEER], {PEER: [AGENT]})
    _session(fff, SAME_NAME, "fff line")
    return SimpleNamespace(base=base, aaa=aaa, tmp=tmp_path)


def _firewall(w, include):
    history = {"preserve_on_reset": True}
    if include is not None:
        history["archive_include_groups"] = include
    rules = w.tmp / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {"enabled": True},
                                 "agent_profiles": {AGENT: {"history": history}}}),
                     encoding="utf-8")
    return ContextFirewall(rules)


def _ctx(w, cls, conversation_dir, conversation_id, include=True, source=None, chat=None):
    layers = {OWNER_TURN: ALL_MEMORY_LAYERS, OTHER_NODE_TURN: frozenset({"L6"}),
              STRANGER_TURN: frozenset()}[cls]
    source = source or {OWNER_TURN: "ui", OTHER_NODE_TURN: "group", STRANGER_TURN: "telegram"}[cls]
    ctx = ToolContext(agent_root=w.tmp / "agents" / AGENT, firewall=_firewall(w, include),
                      message_source=source, turn_class=cls, open_layers=layers,
                      conversation_id=conversation_id, reply_telegram_chat_id=chat)
    ctx.conversation_monitor = SimpleNamespace(
        conversation_id=conversation_id,
        _get_history_path=lambda: conversation_dir / "history.json")
    agent = DpcAgent.__new__(DpcAgent)
    agent._firewall_profile = AGENT
    agent._service = SimpleNamespace(group_manager=SimpleNamespace(node_id=THIS))
    ctx._agent = agent
    return ctx


def owner(w, include=True):
    return _ctx(w, OWNER_TURN, w.base / AGENT, AGENT, include)


def owner_in_group(w, include=True):
    return _ctx(w, OWNER_TURN, w.aaa, "group-aaaaaaaaaaaa", include)


def mixed(w, include=True):
    return _ctx(w, OTHER_NODE_TURN, w.aaa, "group-aaaaaaaaaaaa", include)


def _listed(ctx) -> dict:
    return json.loads(archive.read_session_archive(ctx, last_n=50))


def _texts(listing: dict) -> set:
    return {(s["conversation_id"], p) for s in listing["sessions"] for p in s["user_message_previews"]}


def _found(ctx, query="line") -> dict:
    return json.loads(archive.search_session_archives(ctx, query, last_n=50))


# owner turns


def test_owner_with_the_flag_off_reads_only_its_own_conversation(world):
    for include in (False, None, "yes"):  # absent and a non-bool both stay closed
        listing = _listed(owner(world, include))
        assert _texts(listing) == {(AGENT, "own line")}, include


def test_owner_with_the_flag_on_adds_the_groups_whose_agents_map_holds_this_agent(world):
    listing = _listed(owner(world))
    assert _texts(listing) == {
        (AGENT, "own line"),
        ("group-aaaaaaaaaaaa", "witnessed line"),
        ("group-aaaaaaaaaaaa", "unrecorded line"),  # no recorded members: owner turns only
        ("group-aaaaaaaaaaaa", "fewer line"),
    }
    assert "note" not in listing
    found = _found(owner(world))
    assert {(m["conversation_id"], m["snippet"]) for m in found["matches"]} == _texts(listing)


@pytest.mark.parametrize("line", ["bbb line", "ccc line", "ddd line", "eee line", "fff line"],
                         ids=["agent-not-in-map", "no-metadata", "broken-metadata",
                              "empty-agents-map", "same-id-under-another-node"])
def test_a_group_the_rule_does_not_open_stays_closed_to_the_owner(world, line):
    assert all(line != t for _, t in _texts(_listed(owner(world))))
    assert all(line not in m["snippet"] for m in _found(owner(world))["matches"])


def test_without_this_node_s_id_no_group_is_included(world):
    ctx = owner(world)
    ctx._agent._service = None
    assert _texts(_listed(ctx)) == {(AGENT, "own line")}


def test_owner_in_a_group_reads_that_group_whole_as_before(world):
    listing = _listed(owner_in_group(world, include=False))
    assert {t for _, t in _texts(listing)} == {"witnessed line", "unrecorded line", "fewer line"}
    assert "note" not in listing


# a turn in a group with another node


@pytest.mark.parametrize("include", [True, False])
def test_a_mixed_group_reads_only_sessions_every_current_member_witnessed(world, include):
    listing = _listed(mixed(world, include))
    assert _texts(listing) == {("group-aaaaaaaaaaaa", "witnessed line")}
    assert listing["total_archives"] == 1
    note = listing["note"]
    assert note.startswith("2 session(s) of this group exist but are not shown on this turn")
    assert "1 record no members" in note and "1 were held without every current member" in note
    assert "\n" not in note

    found = _found(mixed(world, include))
    assert [m["snippet"] for m in found["matches"]] == ["witnessed line"]
    assert found["note"] == note


def test_a_member_who_joined_later_closes_the_sessions_before_them(world):
    _meta(world.aaa, "group-aaaaaaaaaaaa", [THIS, PEER, LATE], {THIS: [AGENT]})
    listing = _listed(mixed(world))
    assert listing["sessions"] == []
    assert listing["note"].startswith("3 session(s) of this group exist")


@pytest.mark.parametrize("metadata", [None, "{broken", json.dumps({"group_id": "group-aaaaaaaaaaaa",
                                                                    "members": []})],
                         ids=["missing", "broken", "empty-roster"])
def test_a_mixed_group_without_known_members_opens_no_session(world, metadata):
    path = world.aaa / "metadata.json"
    path.unlink()
    if metadata is not None:
        path.write_text(metadata, encoding="utf-8")
    listing = _listed(mixed(world))
    assert listing["sessions"] == []
    assert "current members are not known" in listing["note"]
    answer = archive.read_session_detail(mixed(world), SAME_NAME)
    assert answer.startswith("⚠️") and "current members are not known" in answer


def test_a_mixed_group_never_reads_another_conversation(world):
    for cid in (AGENT, "group-bbbbbbbbbbbb"):
        answer = archive.read_session_detail(mixed(world), SAME_NAME, conversation_id=cid)
        assert answer == f"⚠️ Conversation {cid} is not open to session archive reads on this turn."


def test_a_mixed_group_cannot_open_a_skipped_session_by_name(world):
    for name, why in ((UNRECORDED_NAME, "records no members"),
                      (FEWER_NAME, "without every current member")):
        answer = archive.read_session_detail(mixed(world), name)
        assert answer.startswith("⚠️") and why in answer, answer
        assert "line" not in answer
    opened = json.loads(archive.read_session_detail(mixed(world), SAME_NAME))
    assert opened["conversation_id"] == "group-aaaaaaaaaaaa"
    assert opened["messages"][0]["content"] == "witnessed line"


def test_a_recorded_roster_of_another_group_does_not_count(world):
    _session(world.aaa, SAME_NAME, "witnessed line", _record("group-bbbbbbbbbbbb", [THIS, PEER]))
    assert _listed(mixed(world))["sessions"] == []


# read_session_detail and conversation_id


def test_detail_resolves_a_filename_inside_the_named_conversation_only(world):
    own = json.loads(archive.read_session_detail(owner(world), SAME_NAME))
    assert (own["conversation_id"], own["messages"][0]["content"]) == (AGENT, "own line")
    group = json.loads(archive.read_session_detail(
        owner(world), SAME_NAME, conversation_id="group-aaaaaaaaaaaa"))
    assert (group["conversation_id"], group["messages"][0]["content"]) == (
        "group-aaaaaaaaaaaa", "witnessed line")


@pytest.mark.parametrize("cid", ["group-bbbbbbbbbbbb", "group-cccccccccccc", "group-dddddddddddd",
                                 "group-eeeeeeeeeeee", "group-ffffffffffff", "../agent_x"])
def test_detail_refuses_a_conversation_the_rule_does_not_open(world, cid):
    answer = archive.read_session_detail(owner(world), SAME_NAME, conversation_id=cid)
    assert answer == f"⚠️ Conversation {cid} is not open to session archive reads on this turn."


def test_detail_refuses_a_group_when_the_flag_is_off(world):
    answer = archive.read_session_detail(owner(world, include=False), SAME_NAME,
                                         conversation_id="group-aaaaaaaaaaaa")
    assert answer.startswith("⚠️ Conversation group-aaaaaaaaaaaa is not open")


# a stranger


@pytest.mark.parametrize("include", [True, False])
def test_a_stranger_keeps_only_its_own_telegram_chat(world, include):
    _session(world.base / "telegram-42", SAME_NAME, "telegram line")
    own = _ctx(world, STRANGER_TURN, world.base / "telegram-42", "telegram-42", include, chat="42")
    assert _texts(_listed(own)) == {("telegram-42", "telegram line")}
    refused = archive.read_session_detail(own, SAME_NAME, conversation_id="group-aaaaaaaaaaaa")
    assert refused.startswith("⚠️ Conversation group-aaaaaaaaaaaa is not open")
    elsewhere = _ctx(world, STRANGER_TURN, world.aaa, "group-aaaaaaaaaaaa", include)
    for answer in (archive.read_session_archive(elsewhere),
                   archive.read_session_detail(elsewhere, SAME_NAME),
                   archive.search_session_archives(elsewhere, "line")):
        assert answer.startswith("⚠️ Session archives are not read on this turn")


# validation and the schema


@pytest.mark.parametrize("where", ["profile", "global"])
def test_validate_config_accepts_a_bool_only(where):
    def config(value):
        history = {"archive_include_groups": value}
        if where == "profile":
            return {"agent_profiles": {AGENT: {"history": history}}}
        return {"dpc_agent": {"history": history}}
    for ok in (True, False):
        valid, errors = ContextFirewall.validate_config(config(ok))
        assert valid, errors
    for bad in ("true", 1, None, []):
        valid, errors = ContextFirewall.validate_config(config(bad))
        assert not valid and any("archive_include_groups" in e for e in errors), bad


def test_the_detail_schema_names_conversation_id():
    [detail] = [t for t in archive.get_tools() if t.name == "read_session_detail"]
    props = detail.schema["parameters"]["properties"]
    assert props["conversation_id"]["type"] == "string"
    assert "conversation_id" not in detail.schema["parameters"]["required"]
    assert "conversation_id" in detail.schema["description"]
