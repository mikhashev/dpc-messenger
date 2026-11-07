"""A revoked extended path has to leave the recall block on the next turn.

Recall asked only the global extended-read flag. The rows of an extended path stay in
the index until the next manager start, so after the owner removed a path from the
agent's grants the hint went on quoting the document's heading, excerpt and address
while read_file already refused that address. The grant of each candidate's own path
is now asked every turn, through the check read_file itself applies, before the slots
are counted.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as context_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.active_recall import get_recall_block
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.hybrid_search import SearchResult
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem
from dpc_client_core.dpc_agent.tools.core import read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

PROFILE = "agent_x"
QUERY = "harbour inventory ledger"
REVOKED_MARK = "REVOKED-BODY-MARK-41d2"
KEPT_MARK = "KEPT-BODY-MARK-8a07"
OWN_MARK = "OWN-BODY-MARK-c3e9"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))

    agent_root = tmp_path / "agents" / PROFILE
    (agent_root / "knowledge").mkdir(parents=True)
    Memory(agent_root).ensure_files()

    revoked_root = tmp_path / "projects" / "secret-project"
    kept_root = tmp_path / "projects" / "open-project"
    for root in (revoked_root, kept_root):
        root.mkdir(parents=True)
    revoked = revoked_root / "plan.md"
    kept = kept_root / "notes.md"
    own = agent_root / "knowledge" / "own-note.md"
    revoked.write_text(f"# Secret Plan\n{QUERY} {REVOKED_MARK}", encoding="utf-8")
    kept.write_text(f"# Open Notes\n{QUERY} {KEPT_MARK}", encoding="utf-8")
    own.write_text(f"# Own Note\n{QUERY} {OWN_MARK}", encoding="utf-8")

    rules = tmp_path / "privacy_rules.json"
    w = {"agent_root": agent_root, "rules": rules, "revoked_root": revoked_root,
         "kept_root": kept_root, "revoked": revoked, "kept": kept, "own": own}
    _write_rules(w, [revoked_root, kept_root])
    w["firewall"] = ContextFirewall(rules)
    return w


def _write_rules(w, read_only, extended_read=True):
    w["rules"].write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {"sandbox_extensions": {
            "read_only": [str(p) for p in read_only],
            "extended_read_enabled": extended_read,
        }}},
    }), encoding="utf-8")


def _revoke(w):
    """What a firewall save does: the file changes and the live object reloads."""
    _write_rules(w, [w["kept_root"]])
    ok, msg = w["firewall"].reload()
    assert ok, msg


def _meta(key, layer, path, heading):
    return {"source_file": key, "source_layer": layer, "source_path": str(path),
            "heading": heading, "text": path.read_text(encoding="utf-8")}


def _metas(w):
    return [
        _meta("EXT/secret-project/plan.md", "EXT", w["revoked"], "Secret Plan"),
        _meta("EXT/open-project/notes.md", "EXT", w["kept"], "Open Notes"),
        _meta("knowledge/own-note.md", "L5", w["own"], "Own Note"),
    ]


def _agent(w):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = w["agent_root"]
    agent.memory = Memory(w["agent_root"])
    agent.skill_store = agent._embedding_provider = None
    agent._firewall = w["firewall"]
    agent._firewall_profile = PROFILE
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    return agent


class _PromptBuilt(Exception):
    pass


async def _next_turn_tail(w, monkeypatch) -> str:
    """Run the agent's own process() up to the prompt, through the real
    build_llm_messages, and return the tail it would have sent."""
    real_build = context_mod.build_llm_messages
    real_backend = retrieval_mod.make_backend_for_agent
    seen = {}

    def _build(**kwargs):
        _messages, cap = real_build(**kwargs)
        seen["tail"] = cap.get("turn_context") or ""
        raise _PromptBuilt

    monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
    # No embedding model in a test: the text channel alone carries the candidates.
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=8))
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)

    with pytest.raises(_PromptBuilt):
        await _agent(w).process(QUERY, "conv-1")
    return seen["tail"]


def _index(w):
    backend = retrieval_mod.make_backend_for_agent(w["agent_root"], dimensions=8)
    backend.text.add([TextAddItem(text=m["text"], meta=m) for m in _metas(w)])
    backend.text.save()


def _granted(w):
    return _agent(w)._extended_path_allowed


def _result(meta, score=1.0):
    return SearchResult(chunk_meta=meta, score=score, source="hybrid")


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_a_path_makes_the_next_turn_carry_no_hint_for_it(world, monkeypatch):
    _index(world)

    before = await _next_turn_tail(world, monkeypatch)
    assert REVOKED_MARK in before, "the index row did not reach the block before revocation"

    _revoke(world)
    after = await _next_turn_tail(world, monkeypatch)

    assert REVOKED_MARK not in after, after
    assert "Secret Plan" not in after
    assert "secret-project" not in after
    assert str(world["revoked"]) not in after
    # And read_file agrees: the address the hint no longer prints is the one it refuses.
    ctx = ToolContext(agent_root=world["agent_root"], firewall=world["firewall"])
    ctx._agent = _agent(world)
    assert read_file(ctx, str(world["revoked"])).startswith("⚠️ Access denied")


@pytest.mark.asyncio
async def test_a_still_granted_path_and_the_own_layer_are_unaffected(world, monkeypatch):
    _index(world)
    _revoke(world)

    tail = await _next_turn_tail(world, monkeypatch)

    assert KEPT_MARK in tail
    assert f'read_file("{world["kept"]}")' in tail
    assert OWN_MARK in tail
    assert 'read_file("knowledge/own-note.md")' in tail


def test_the_grant_is_read_from_the_live_firewall_each_time(world):
    granted = _granted(world)
    assert granted(str(world["revoked"]))

    _revoke(world)

    assert not granted(str(world["revoked"]))
    assert granted(str(world["kept"]))


def test_a_revoked_document_does_not_take_a_slot(world):
    """Top-ranked and revoked: it must leave room for the three behind it."""
    _revoke(world)
    revoked, kept, own = _metas(world)
    second_own = dict(own, source_file="knowledge/second.md", heading="Second")
    results = [_result(revoked, 9.0), _result(kept, 3.0), _result(own, 2.0),
               _result(second_own, 1.0)]

    block = get_recall_block(results, agent_root=None, max_results=3,
                             extended_path_allowed=_granted(world))

    assert [r.chunk_meta["source_file"] for r in block.injected] == [
        "EXT/open-project/notes.md", "knowledge/own-note.md", "knowledge/second.md"]
    assert REVOKED_MARK not in block.text


def test_hints_mode_does_not_name_a_revoked_document_either(world):
    _revoke(world)
    revoked, kept, _own = _metas(world)

    block = get_recall_block([_result(revoked), _result(kept)], context_usage_ratio=0.6,
                             agent_root=None, extended_path_allowed=_granted(world))

    assert block.mode == "hints"
    assert "secret-project" not in block.text
    assert "open-project" in block.text


def test_non_extended_layers_ignore_the_path_grant(world, tmp_path):
    """L5 is addressed by its key and L6 by its own gate; a grant check that refuses
    everything must not touch either."""
    l6_dir = tmp_path / "home" / ".dpc" / "knowledge"
    l6_dir.mkdir(parents=True)
    l6 = l6_dir / "shared.md"
    l6.write_text("# Shared\nshared body", encoding="utf-8")
    _revoked, _kept, own = _metas(world)
    shared = _meta("L6/shared.md", "L6", l6, "Shared")

    block = get_recall_block([_result(own), _result(shared)], agent_root=None,
                             extended_path_allowed=lambda _p: False)

    assert [r.chunk_meta["source_file"] for r in block.injected] == [
        "knowledge/own-note.md", "L6/shared.md"]


def test_with_the_global_flag_off_a_granted_path_keeps_its_dead_end_hint(world):
    """The global toggle's behaviour is not this change's: a granted path still gets
    its slot and excerpt with no address."""
    _write_rules(world, [world["revoked_root"], world["kept_root"]], extended_read=False)
    assert world["firewall"].reload()[0]
    _revoked, kept, _own = _metas(world)

    block = get_recall_block([_result(kept)], agent_root=None,
                             extended_read_enabled=False,
                             extended_path_allowed=_granted(world))

    assert KEPT_MARK in block.text
    assert "extended path read access is off" in block.text
    assert len(block.injected) == 1


def test_an_outside_row_with_no_path_cannot_show_a_grant_and_is_dropped(world):
    no_path = {"source_file": "EXT/old/row.md", "source_layer": "EXT",
               "heading": "Old Row", "text": "old row body"}

    block = get_recall_block([_result(no_path)], agent_root=None,
                             extended_path_allowed=_granted(world))

    assert block.injected == []
    assert "old row body" not in block.text


def test_without_a_grant_check_nothing_is_filtered():
    """Callers that pass none (tests, tools) keep the old behaviour."""
    meta = {"source_file": "EXT/p/a.md", "source_layer": "EXT",
            "source_path": "/nowhere/a.md", "heading": "A", "text": "a body"}

    block = get_recall_block([_result(meta)], agent_root=None)

    assert len(block.injected) == 1
