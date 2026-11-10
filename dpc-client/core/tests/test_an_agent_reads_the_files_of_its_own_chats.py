"""An agent reads the files sent into its own chats, and nothing else of the store.

THE-SCREENSHOTS-A-GROUP-STORES-ARE-OUTSIDE-EVERY-AGENT-SANDBOX (Mike, 2026-11-11):
pasted screenshots land in `~/.dpc/conversations/<conversation>/files/`, outside the
agent's directory, and no profile granted a read there, so describe_image on a picture
from the agent's own chat was refused. The default grant is read-only: the `files/` of
the agent's one-to-one chat and of each group whose metadata lists the agent under this
node. Not history.json, not other agents' chats, not groups listed under another node.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools.core import (
    _resolve_file_path, list_dir, read_file, write_file,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

AGENT = "agent_forge_7244b181"
OTHER_AGENT = "agent_001"
THIS_NODE = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_NODE = "dpc-node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    conv = home / ".dpc" / "conversations"
    root = home / ".dpc" / "agents" / AGENT
    root.mkdir(parents=True)

    def chat(name, text):
        files = conv / name / "files" / "screenshots"
        files.mkdir(parents=True)
        (files / "paste_1.png").write_bytes(b"\x89PNG\r\n")
        (conv / name / "files" / "note.txt").write_text(text, encoding="utf-8")
        (conv / name / "history.json").write_text(f"history {text}", encoding="utf-8")
        return conv / name

    def group(name, group_id, agents):
        d = chat(name, name)
        (d / "metadata.json").write_text(json.dumps(
            {"group_id": group_id, "agents": agents}), encoding="utf-8")
        return d

    w = SimpleNamespace(tmp=tmp_path, conv=conv, root=root)
    w.own = chat(AGENT, "OWN-1to1")
    w.other_agent = chat(OTHER_AGENT, "OTHER-1to1")
    w.listed = group("group-aaaa11112222-listed", "group-aaaa11112222",
                     {THIS_NODE: [AGENT, OTHER_AGENT], OTHER_NODE: ["agent_zzz"]})
    w.absent = group("group-bbbb11112222-absent", "group-bbbb11112222",
                     {THIS_NODE: [OTHER_AGENT]})
    w.elsewhere = group("group-cccc11112222-elsewhere", "group-cccc11112222",
                        {OTHER_NODE: [AGENT]})
    w.nometa = chat("group-dddd11112222-nometa", "NOMETA")
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {AGENT: {
            "sandbox_extensions": {"read_only": [], "extended_read_enabled": True,
                                   "extended_write_enabled": True}}},
    }), encoding="utf-8")
    w.firewall = ContextFirewall(rules)
    return w


def _ctx(w, source="ui"):
    ctx = ToolContext(agent_root=w.root, firewall=w.firewall, message_source=source)
    ctx._agent = SimpleNamespace(_firewall_profile=AGENT, _this_node_id=lambda: THIS_NODE)
    return ctx


def _refused(ctx, path, **kw):
    with pytest.raises(PermissionError):
        ctx.validate_extended_path(str(path), **kw)


def test_own_one_to_one_files_are_readable(world):
    ctx = _ctx(world)
    png = world.own / "files" / "screenshots" / "paste_1.png"
    assert ctx.validate_extended_path(str(png)) == png.resolve()
    assert _resolve_file_path(ctx, str(png)) == png.resolve()
    assert "OWN-1to1" in read_file(ctx, str(world.own / "files" / "note.txt"))
    listing = list_dir(ctx, str(world.own / "files"))
    assert "note.txt" in listing and "Access denied" not in listing


def test_another_agents_one_to_one_files_are_refused(world):
    ctx = _ctx(world)
    _refused(ctx, world.other_agent / "files" / "note.txt")
    assert "OTHER-1to1" not in read_file(ctx, str(world.other_agent / "files" / "note.txt"))


def test_own_group_files_are_readable_when_listed_under_this_node(world):
    ctx = _ctx(world)
    png = world.listed / "files" / "screenshots" / "paste_1.png"
    assert ctx.validate_extended_path(str(png)) == png.resolve()
    assert "group-aaaa11112222-listed" in read_file(ctx, str(world.listed / "files" / "note.txt"))


def test_a_group_that_does_not_list_the_agent_is_refused(world):
    ctx = _ctx(world)
    _refused(ctx, world.absent / "files" / "note.txt")
    _refused(ctx, world.nometa / "files" / "note.txt")


def test_a_group_that_lists_the_agent_under_another_node_is_refused(world):
    _refused(_ctx(world), world.elsewhere / "files" / "note.txt")


def test_an_unknown_node_id_fails_closed(world):
    ctx = _ctx(world)
    ctx._agent._this_node_id = lambda: None
    _refused(ctx, world.listed / "files" / "note.txt")
    # the 1:1 grant needs no node id
    ctx.validate_extended_path(str(world.own / "files" / "note.txt"))


def test_only_files_inside_the_chat_files_folder_are_open(world):
    ctx = _ctx(world)
    _refused(ctx, world.own / "history.json")
    _refused(ctx, world.listed / "history.json")
    _refused(ctx, world.listed / "metadata.json")
    _refused(ctx, world.conv)
    _refused(ctx, world.own)
    _refused(ctx, world.own / "files" / ".." / "history.json")
    assert "history OWN" not in read_file(ctx, str(world.own / "history.json"))


def test_a_link_in_files_that_leads_out_is_judged_by_where_it_leads(world):
    link = world.own / "files" / "to_history"
    try:
        link.symlink_to(world.other_agent / "history.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    _refused(_ctx(world), link)


def test_a_write_into_own_chat_files_is_refused(world):
    ctx = _ctx(world)
    target = world.own / "files" / "new.txt"
    _refused(ctx, target, require_write=True)
    _refused(ctx, world.listed / "files" / "note.txt", require_write=True)
    write_file(ctx, str(target), "x")
    assert not target.exists()


def test_a_discord_turn_still_reads_nothing_outside_the_sandbox(world):
    ctx = _ctx(world, source="discord")
    _refused(ctx, world.own / "files" / "note.txt")
    _refused(ctx, world.listed / "files" / "note.txt")
