"""A turn that is not the owner's opens no private file (ADR-048, step 3).

Observed live 2026-11-08: a Telegram turn that was not the owner's was refused by
memory_search and then read the agent's memory/scratchpad.md through search_files and
read_file without asking anybody. Mike's call, 2026-11-08 (review by Zcode, Johnny,
Ark): a stranger opens nothing in the agent's directory but its own turn's tool
results, no extended path and no shared knowledge; a group with another node opens no
memory and no mixed store, extended paths and shared knowledge as their marks say, and
every other file it opens is logged; the owner's turns are unchanged; run_shell is a
separate story and its gate (validate_extended_path) is not touched.
"""

from __future__ import annotations

import json
import logging
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.agent import (
    ALL_MEMORY_LAYERS, OTHER_NODE_TURN, OWNER_TURN, STRANGER_TURN, DpcAgent,
)
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.tools import archive as archive_mod
from dpc_client_core.dpc_agent.tools import core as core_mod
from dpc_client_core.dpc_agent.tools import git as git_mod
from dpc_client_core.dpc_agent.tools.core import (
    deduplicate_identity, knowledge_list, list_dir, list_extended_sandbox_paths, read_file,
    repo_delete, search_files, search_in_file, update_identity, update_scratchpad, write_file,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

PROFILE = "agent_x"
TURN = "20261109T010203Z-abcdef12"
SCRATCH = "SCRATCH-MARK-41c2"
WORK = "WORK-MARK-8e1d"
EXT = "EXT-MARK-2f70"
SHARED = "SHARED-MARK-c3a9"
RESULT = "RESULT-MARK-6b04"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc" / "knowledge").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))
    root = tmp_path / "agents" / PROFILE
    (root / "knowledge").mkdir(parents=True)
    Memory(root).ensure_files()
    (root / "memory" / "scratchpad.md").write_text(f"scratch {SCRATCH}", encoding="utf-8")
    (root / "knowledge" / "topic.md").write_text(f"topic {SCRATCH}", encoding="utf-8")
    for rel in ("state/memory_index/bm25.txt", "logs/tools.jsonl", "task_results/t.json",
                "knowledge_graph_export/g.txt", "config.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"mixed {SCRATCH}", encoding="utf-8")
    (root / "work").mkdir()
    (root / "work" / "notes.txt").write_text(f"work {WORK}", encoding="utf-8")
    for turn in (TURN, "20261108T000000Z-00000000"):
        (root / "state" / "tool_results" / turn).mkdir(parents=True)
        (root / "state" / "tool_results" / turn / "r1-0.txt").write_text(
            f"result {RESULT}", encoding="utf-8")
    ext_root = tmp_path / "projects" / "open-project"
    ext_root.mkdir(parents=True)
    (ext_root / "notes.md").write_text(f"ext {EXT}", encoding="utf-8")
    shared = home / ".dpc" / "knowledge" / "shared.md"
    shared.write_text(f"shared {SHARED}", encoding="utf-8")
    return SimpleNamespace(root=root, ext=ext_root / "notes.md", ext_root=ext_root,
                           shared=shared, tmp=tmp_path)


def _firewall(w, memory=None):
    rules = w.tmp / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {
            "human_knowledge_access": True,
            "memory": {"enabled": True, **(memory or {})},
            "sandbox_extensions": {"read_only": [str(w.ext_root)], "extended_read_enabled": True},
        }},
    }), encoding="utf-8")
    return ContextFirewall(rules)


def _ctx(w, cls, layers, source, memory=None, conversation_id="conv-1", reply_chat=None):
    fw = _firewall(w, memory)
    ctx = ToolContext(agent_root=w.root, firewall=fw, message_source=source,
                      turn_class=cls, open_layers=layers, conversation_id=conversation_id,
                      reply_telegram_chat_id=reply_chat)
    ctx.tool_output_turn = TURN
    agent = DpcAgent.__new__(DpcAgent)
    agent._firewall_profile = PROFILE
    ctx._agent = agent
    return ctx


def stranger(w, **kw):
    return _ctx(w, STRANGER_TURN, frozenset(), "telegram", **kw)


def mixed(w, layers=frozenset({"L6"}), **kw):
    return _ctx(w, OTHER_NODE_TURN, layers, "group", **kw)


def owner(w, **kw):
    return _ctx(w, OWNER_TURN, ALL_MEMORY_LAYERS, "ui", **kw)


def _refused(answer, *marks):
    assert answer.startswith("⚠️"), answer
    assert "not the owner's" in answer, answer
    for m in marks:
        assert m not in answer


# -- a stranger ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["memory/scratchpad.md", "work/notes.txt", "knowledge/topic.md",
                                  f"state/tool_results/20261108T000000Z-00000000/r1-0.txt"])
def test_a_stranger_opens_nothing_in_the_agent_directory(world, path):
    _refused(read_file(stranger(world), path), SCRATCH, WORK, RESULT)


def test_a_stranger_still_reads_its_own_turn_s_tool_results(world):
    assert RESULT in read_file(stranger(world), f"state/tool_results/{TURN}/r1-0.txt")


def test_a_stranger_lists_and_searches_nothing_in_the_agent_directory(world):
    _refused(list_dir(stranger(world), "."))
    _refused(search_files(stranger(world), "MARK"), SCRATCH, WORK)
    _refused(search_in_file(stranger(world), "MARK", "memory/scratchpad.md"), SCRATCH)
    _refused(knowledge_list(stranger(world)))


def test_a_stranger_reads_no_extended_path_and_no_shared_knowledge(world):
    _refused(read_file(stranger(world), str(world.ext)), EXT)
    _refused(read_file(stranger(world), str(world.shared)), SHARED)
    _refused(list_extended_sandbox_paths(stranger(world)), str(world.ext_root))


def test_a_stranger_writes_nothing_in_the_agent_directory(world):
    _refused(write_file(stranger(world), "work/new.txt", "x"))
    assert not (world.root / "work" / "new.txt").exists()


# -- a group with another node ------------------------------------------------

CLOSED_TO_A_MIXED_GROUP = ["memory/scratchpad.md", "MEMORY/scratchpad.md", "knowledge/topic.md",
                           "state/memory_index/bm25.txt", "logs/tools.jsonl",
                           "task_results/t.json", "knowledge_graph_export/g.txt", "config.json"]


@pytest.mark.parametrize("path", CLOSED_TO_A_MIXED_GROUP)
def test_a_mixed_group_opens_no_memory_and_no_mixed_store(world, path):
    _refused(read_file(mixed(world), path), SCRATCH)


def test_a_mixed_group_reads_a_work_file_and_it_is_logged(world, caplog):
    with caplog.at_level(logging.INFO, logger=core_mod.log.name):
        answer = read_file(mixed(world), "work/notes.txt")
    assert WORK in answer
    lines = [r.getMessage() for r in caplog.records]
    assert any("non-owner turn opened work/notes.txt" in m for m in lines), lines
    assert not any(WORK in m for m in lines)


def test_a_mixed_group_s_search_of_the_root_skips_and_counts_what_it_may_not_open(world):
    answer = search_files(mixed(world), "MARK")
    assert WORK in answer and SCRATCH not in answer and RESULT in answer
    # memory, knowledge, five mixed stores and the other turn's result
    assert "9 files skipped: this turn is not the owner's" in answer, answer


def test_a_mixed_group_s_recursive_listing_leaves_out_what_it_may_not_open(world):
    answer = list_dir(mixed(world), ".", recursive=True)
    assert "notes.txt" in answer and "scratchpad.md" not in answer and "bm25" not in answer
    assert "entries not listed" in answer


def test_l5_marked_shared_opens_the_memory_to_a_mixed_group_but_not_the_mixed_stores(world):
    ctx = mixed(world, layers=frozenset({"L5", "L6"}))
    assert SCRATCH in read_file(ctx, "memory/scratchpad.md")
    _refused(read_file(ctx, "logs/tools.jsonl"), SCRATCH)


def test_an_extended_path_is_closed_to_a_mixed_group_while_ext_is_private(world):
    _refused(read_file(mixed(world), str(world.ext)), EXT)
    _refused(list_extended_sandbox_paths(mixed(world)))
    assert EXT in read_file(mixed(world, layers=frozenset({"L6", "EXT"})), str(world.ext))


def test_shared_knowledge_opens_to_a_mixed_group_and_not_to_a_stranger(world):
    assert SHARED in read_file(mixed(world), str(world.shared))
    _refused(read_file(stranger(world), str(world.shared)), SHARED)
    _refused(read_file(mixed(world, layers=frozenset()), str(world.shared)), SHARED)


# -- writes -------------------------------------------------------------------


@pytest.mark.parametrize("call", [
    lambda c: update_scratchpad(c, "leak"),
    lambda c: update_identity(c, "values", "leak"),
    lambda c: deduplicate_identity(c),
    lambda c: write_file(c, "knowledge/new.md", "leak"),
    lambda c: write_file(c, "memory/scratchpad.md", "leak"),
    lambda c: repo_delete(c, "memory/scratchpad.md"),
    lambda c: repo_delete(c, "knowledge", recursive=True),
], ids=["scratchpad", "identity", "dedup-identity", "write-knowledge", "write-memory",
        "delete-memory", "delete-knowledge"])
@pytest.mark.parametrize("who", ["stranger", "mixed"])
def test_a_memory_write_is_refused_in_one_line(world, call, who):
    before = (world.root / "memory" / "scratchpad.md").read_text(encoding="utf-8")
    ctx = stranger(world) if who == "stranger" else mixed(world)
    answer = call(ctx)
    assert answer.startswith("⚠️") and "not the owner's" in answer and "\n" not in answer, answer
    assert (world.root / "memory" / "scratchpad.md").read_text(encoding="utf-8") == before
    assert (world.root / "knowledge" / "topic.md").exists()
    assert not (world.root / "knowledge" / "new.md").exists()


def test_l5_shared_lets_a_mixed_group_write_the_scratchpad(world):
    assert update_scratchpad(mixed(world, layers=frozenset({"L5", "L6"})), "ok").startswith("✓")


# -- the owner, unchanged -----------------------------------------------------


def test_the_owner_s_turn_opens_everything_as_before(world):
    ctx = owner(world)
    assert SCRATCH in read_file(ctx, "memory/scratchpad.md")
    assert SCRATCH in read_file(ctx, "logs/tools.jsonl")
    assert EXT in read_file(ctx, str(world.ext))
    assert SHARED in read_file(ctx, str(world.shared))
    answer = search_files(ctx, "MARK")
    assert SCRATCH in answer and "skipped: this turn" not in answer
    assert "scratchpad.md" in list_dir(ctx, ".", recursive=True)
    assert "topic" in knowledge_list(ctx)
    assert str(world.ext_root) in list_extended_sandbox_paths(ctx)
    assert update_scratchpad(ctx, "more").startswith("✓")


def test_a_bare_context_with_no_source_and_no_class_is_not_gated(world):
    """Tests and probes build one; DpcAgent.process always sets both (pinned below)."""
    ctx = ToolContext(agent_root=world.root)
    assert SCRATCH in read_file(ctx, "memory/scratchpad.md")


@pytest.mark.asyncio
async def test_every_turn_s_tool_context_carries_its_class_and_layers(world, monkeypatch):
    """The gate's legacy door (no class) must never be the one a real turn takes."""
    from dpc_client_core.dpc_agent import agent as agent_mod
    from dpc_client_core.dpc_agent import context as context_mod

    captured = {}

    class _Stop(Exception):
        pass

    def _set_context(ctx):
        captured["ctx"] = ctx
        raise _Stop

    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = world.root
    agent.memory = Memory(world.root)
    agent.skill_store = agent._embedding_provider = None
    agent._firewall = _firewall(world)
    agent._firewall_profile = PROFILE
    agent._service = SimpleNamespace(group_manager=None)
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent.tools = SimpleNamespace(set_context=_set_context)
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)
    for source, expected in ((None, STRANGER_TURN), ("telegram", STRANGER_TURN),
                             ("group", OTHER_NODE_TURN), ("ui", OWNER_TURN)):
        with pytest.raises(_Stop):
            await agent.process("hi", "conv-1", message_source=source)
        assert captured["ctx"].turn_class == expected
        assert captured["ctx"].open_layers is not None


# -- session archives ---------------------------------------------------------


def _archive_ctx(w, conversation_id, reply_chat):
    ctx = stranger(w, conversation_id=conversation_id, reply_chat=reply_chat)
    ctx.conversation_monitor = SimpleNamespace(
        _get_history_path=lambda: w.tmp / "conversations" / conversation_id / "history.json")
    return ctx


@pytest.mark.parametrize("tool", [
    lambda c: archive_mod.read_session_archive(c),
    lambda c: archive_mod.read_session_detail(c, "2026-11-01_session.json"),
    lambda c: archive_mod.search_session_archives(c, query="harbour"),
], ids=["read_session_archive", "read_session_detail", "search_session_archives"])
def test_a_stranger_reads_only_its_own_telegram_chat_s_archive(world, tool):
    own = tool(_archive_ctx(world, "telegram-123", "123"))
    assert "not the owner's" not in own, own
    for conv, chat in (("agent_x", "123"), ("telegram-999", "123"), ("group-x", None)):
        _refused(tool(_archive_ctx(world, conv, chat)))


def test_a_mixed_group_reads_its_own_group_archive_as_before(world):
    ctx = mixed(world, conversation_id="group-x")
    ctx.conversation_monitor = SimpleNamespace(
        _get_history_path=lambda: world.tmp / "conversations" / "group-x" / "history.json")
    assert "not the owner's" not in archive_mod.read_session_archive(ctx)


# -- git reads ----------------------------------------------------------------


def test_git_reads_of_the_agent_directory_are_closed_to_turns_that_are_not_the_owner_s(
        world, monkeypatch):
    ran = []
    monkeypatch.setattr(git_mod, "_run_git", lambda *a, **kw: ran.append(a) or
                        {"success": True, "output": "ok"})
    monkeypatch.setattr(git_mod, "_run_git_external", lambda *a, **kw: ran.append(a) or
                        {"success": True, "output": "ok"})
    _refused(git_mod.git_status(stranger(world)))
    _refused(git_mod.git_status(mixed(world)))
    _refused(git_mod.git_diff(mixed(world), "."))
    _refused(git_mod.git_log(mixed(world), "memory"))
    _refused(git_mod.git_log(stranger(world), "work"))
    _refused(git_mod.git_diff(mixed(world), ".", repo_path=str(world.ext_root)))
    assert not ran
    assert "ok" in git_mod.git_log(mixed(world), "work")
    assert "ok" in git_mod.git_status(owner(world))
    assert len(ran) == 2


# -- run_shell's gate is untouched --------------------------------------------


@pytest.mark.parametrize("source", ["ui", "external_agent", "telegram_owner", "group",
                                    "telegram", None, "discord"])
def test_validate_extended_path_answers_every_source_as_before(world, source):
    """run_shell's path gate calls validate_extended_path; ADR-048 does not change it."""
    cls = STRANGER_TURN if source in ("telegram", None, "discord") else OTHER_NODE_TURN
    ctx = _ctx(world, cls, frozenset(), source)
    assert ctx.validate_extended_path(str(world.root / "memory" / "scratchpad.md")) \
        == (world.root / "memory" / "scratchpad.md").resolve()
    if source == "discord":
        with pytest.raises(PermissionError):
            ctx.validate_extended_path(str(world.ext))
    else:
        assert ctx.validate_extended_path(str(world.ext)) == world.ext.resolve()
