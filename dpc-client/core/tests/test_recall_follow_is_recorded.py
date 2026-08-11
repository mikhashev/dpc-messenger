"""A read can say whether a hint sent it, instead of being joined to one afterwards.

The old answer was a join by address and time, which credits a document the agent
found on its own and misses the one it tried and failed to open. Both external
reviewers asked for the same field independently; this is it.
"""
import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.active_recall import (
    _OFFERED_ADDRESSES,
    _build_access_counts,
    _remember_offered,
    followed_a_hint,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    _OFFERED_ADDRESSES.clear()
    yield
    _OFFERED_ADDRESSES.clear()


@pytest.fixture
def agent_root(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def test_a_turn_offered_nothing_says_nothing(tmp_path):
    """Neither a follow nor a refusal to follow — and counting it either way is
    what made the join wrong."""
    assert followed_a_hint("task-1", str(tmp_path / "doc.md")) is None


def test_a_read_of_an_offered_address_is_a_follow(tmp_path):
    offered = str(tmp_path / "knowledge" / "doc.md")
    _remember_offered("task-1", [offered, None])

    assert followed_a_hint("task-1", offered) is True
    assert followed_a_hint("task-1", str(tmp_path / "elsewhere.md")) is False
    assert followed_a_hint("task-2", offered) is None, "another turn was offered nothing"


def test_the_registry_does_not_grow_without_bound():
    for i in range(64):
        _remember_offered(f"task-{i}", [f"C:/x/{i}.md"])
    assert len(_OFFERED_ADDRESSES) <= 32
    assert followed_a_hint("task-0", "C:/x/0.md") is None, "the oldest turn is dropped"
    assert followed_a_hint("task-63", "C:/x/63.md") is True


def _read(agent_root, path, via_hint=None):
    entry = {"tool": "read_file", "ts": "2026-08-12T00:00:00Z", "args": {"path": path}}
    if via_hint is not None:
        entry["via_hint"] = via_hint
    with open(agent_root / "logs" / "tools.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_follows_are_counted_apart_from_reads(agent_root):
    """Every follow is a read; not every read is a follow. One column answering
    both is where the one-in-four figure came from."""
    doc = str(pathlib.Path("C:/store/doc.md"))
    _read(agent_root, doc, via_hint=True)
    _read(agent_root, doc, via_hint=False)
    _read(agent_root, doc)

    counts = _build_access_counts(agent_root)
    meta = {"source_file": "L6/doc.md", "source_path": doc}
    assert counts.reads_for(meta) == 3
    assert counts.follows_for(meta) == 1
