"""Two rules that hold today by coincidence, pinned so they hold by construction.

Neither of these was firing. Both were found by reading the code against its own
justification, and both are the same species as the defects that did fire: a rule
whose stated reason is narrower than its behaviour, and a helper whose caller happens
to protect it. Coincidence is not an invariant, and the day it stops being true is not
a day anyone will be watching.
"""

from __future__ import annotations

import pathlib

import pytest

from dpc_client_core.dpc_agent.index_keys import build_ext_roots, ext_key
from dpc_client_core.dpc_agent.tools.core import _is_shared_knowledge_read


# --------------------------------------------------------------------------
# The shared-layer read gate admits what the indexer admits, and no more
# --------------------------------------------------------------------------

class _Firewall:
    def can_agent_access_context(self, context_type, profile_name=None):
        return True


class _Ctx:
    def __init__(self):
        self.firewall = _Firewall()


@pytest.fixture
def shared_layer(tmp_path, monkeypatch):
    home = tmp_path / "dpc_home"
    knowledge = home / "knowledge"
    (knowledge / "nested").mkdir(parents=True)
    (knowledge / "note.md").write_text("top level", encoding="utf-8")
    (knowledge / "notes.txt").write_text("not markdown", encoding="utf-8")
    (knowledge / "nested" / "deep.md").write_text("one level down", encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(home))
    return knowledge


def test_a_top_level_markdown_file_is_admitted(shared_layer):
    """What the gate is for: the agent was offered this document, so it can open it."""
    assert _is_shared_knowledge_read(_Ctx(), str(shared_layer / "note.md"))


def test_a_file_in_a_subdirectory_is_not(shared_layer):
    """The indexer walks the directory without descending, so nothing down here was
    ever offered — and the gate's whole justification is 'honour what put it in the
    index'."""
    assert not _is_shared_knowledge_read(_Ctx(), str(shared_layer / "nested" / "deep.md"))


def test_a_non_markdown_file_is_not(shared_layer):
    assert not _is_shared_knowledge_read(_Ctx(), str(shared_layer / "notes.txt"))


def test_a_path_outside_the_shared_layer_is_not(shared_layer, tmp_path):
    assert not _is_shared_knowledge_read(_Ctx(), str(tmp_path / "elsewhere.md"))


def test_the_extension_check_is_case_insensitive(shared_layer):
    upper = shared_layer / "SHOUTED.MD"
    upper.write_text("still markdown", encoding="utf-8")

    assert _is_shared_knowledge_read(_Ctx(), str(upper))


def test_a_refusal_says_which_rule_refused(shared_layer, caplog):
    """Silence here is what let the gate drift from its reason in the first place."""
    with caplog.at_level("INFO"):
        _is_shared_knowledge_read(_Ctx(), str(shared_layer / "nested" / "deep.md"))

    assert any("shared knowledge read refused" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# A root that does not exist cannot rename the keys of one that does
# --------------------------------------------------------------------------

def test_a_dead_root_does_not_lengthen_a_live_root_s_tail(tmp_path):
    """The failure it prevents: `.../gone/project` is absent, so `is_dir()` is False
    and its *parent* becomes a base. That parent then competes for tails with the live
    root beside it, both lengthen to stay distinct, and every key under the live root
    is renamed — a rebuild nobody asked for, on documents that did not change."""
    live = tmp_path / "workspace" / "project"
    live.mkdir(parents=True)
    (live / "README.md").write_text("x", encoding="utf-8")
    dead = tmp_path / "workspace" / "project" / "gone" / "project"

    roots = build_ext_roots([str(live), str(dead)])

    assert [r.base for r in roots] == [live]
    assert ext_key(live / "README.md", roots) == "EXT/project/README.md"


def test_the_same_keys_come_out_with_the_dead_root_absent(tmp_path):
    """The point of the rule: adding a path that indexes nothing changes nothing."""
    live = tmp_path / "workspace" / "project"
    live.mkdir(parents=True)
    dead = tmp_path / "nowhere" / "project"

    with_dead = build_ext_roots([str(live), str(dead)])
    without = build_ext_roots([str(live)])

    assert [(r.base, r.tail) for r in with_dead] == [(r.base, r.tail) for r in without]


def test_a_dead_root_is_named_in_the_log(tmp_path, caplog):
    live = tmp_path / "live"
    live.mkdir()
    with caplog.at_level("WARNING"):
        build_ext_roots([str(live), str(tmp_path / "absent")])

    assert any("does not exist" in r.getMessage() for r in caplog.records)


def test_a_root_configured_as_a_single_file_still_works(tmp_path):
    """`is_dir()` is False for a real file too, and there the parent *is* the right
    base — the existence check must not confuse the two cases."""
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "spec.md"
    target.write_text("x", encoding="utf-8")

    roots = build_ext_roots([str(target)])

    assert [r.base for r in roots] == [root]
    assert ext_key(target, roots) == "EXT/docs/spec.md"


# --------------------------------------------------------------------------
# Platform behaviour of the path comparison, stated because it is asymmetric
# --------------------------------------------------------------------------

def test_path_comparison_follows_the_platform_not_a_convention():
    import os

    from dpc_client_core.dpc_agent.index_keys import _norm

    same = _norm("Backlog.md") == _norm("backlog.md")
    assert same is (os.path.normcase("A") == "a")
