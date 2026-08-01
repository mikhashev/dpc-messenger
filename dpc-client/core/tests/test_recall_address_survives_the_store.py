"""The address has to survive the store, not just the renderer.

`test_recall_hint_is_followable` builds its metas by hand and hands them straight to
the renderer. That covers indexer -> hint. It leaves out the leg in between: the meta
goes into a backend and comes back out, and a backend is free to keep only the fields
it knows about. Grafeo kept four and dropped `source_path`, so in production every
external hint resolved to nothing while those tests stayed green — the original defect
one layer down.

So this test writes through a real backend, reads back through its own search, and
follows whatever address comes out. Parametrized over both backends because parity is
the property that broke: native passes the whole meta dict through, Grafeo enumerates
properties, and only an enumerating backend can silently lose one.
"""
from __future__ import annotations

import re

import numpy as np
import pytest

from dpc_client_core.dpc_agent.active_recall import format_recall_hints
from dpc_client_core.dpc_agent.hybrid_search import SearchResult
from dpc_client_core.dpc_agent.index_keys import build_ext_roots, ext_key, l5_key
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem, VectorAddItem
from dpc_client_core.dpc_agent.retrieval.native import NativeTextIndex, NativeVectorIndex
from dpc_client_core.dpc_agent.tools.core import read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext

DIM = 8


class _Firewall:
    def __init__(self, allowed=()):
        self._allowed = [str(p) for p in allowed]

    def get_extended_read_enabled(self, profile_name=None):
        return True

    def get_extended_write_enabled(self, profile_name=None):
        return False

    def can_agent_access_context(self, context_type, profile_name=None):
        return context_type == "knowledge"

    def is_extended_path_allowed(self, path, require_write=False, profile_name=None):
        return any(str(path).startswith(root) for root in self._allowed)


@pytest.fixture
def world(tmp_path):
    agent_root = tmp_path / "agents" / "agent_x"
    (agent_root / "knowledge").mkdir(parents=True)
    (agent_root / "knowledge" / "own-note.md").write_text(
        "# Own\nsandbox layer", encoding="utf-8")

    ext_root = tmp_path / "projects" / "dpc-messenger"
    ext_root.mkdir(parents=True)
    (ext_root / "backlog.md").write_text("# Backlog\nexternal layer", encoding="utf-8")

    return {"agent_root": agent_root,
            "knowledge_dir": agent_root / "knowledge",
            "ext_root": ext_root}


def _metas(world):
    """Exactly the dicts agent_manager stores, for the two layers that differ."""
    l5 = world["knowledge_dir"] / "own-note.md"
    ext = world["ext_root"] / "backlog.md"
    roots = build_ext_roots([str(world["ext_root"])])
    return [
        {"source_file": l5_key(l5, world["knowledge_dir"]), "source_layer": "L5",
         "source_path": str(l5), "heading": "Own",
         "char_count": 20, "text": l5.read_text(encoding="utf-8")},
        {"source_file": ext_key(ext, roots), "source_layer": "EXT",
         "source_path": str(ext), "heading": "Backlog",
         "char_count": 24, "text": ext.read_text(encoding="utf-8")},
    ]


def _vector_index(kind, tmp_path):
    if kind == "native":
        return NativeVectorIndex(tmp_path / "vectors.faiss", dimensions=DIM)
    pytest.importorskip("grafeo")
    from dpc_client_core.dpc_agent.retrieval.grafeo import GrafeoVectorIndex
    return GrafeoVectorIndex(tmp_path / "store.grafeo", dimensions=DIM)


def _text_index(kind, tmp_path):
    if kind == "native":
        return NativeTextIndex(tmp_path / "bm25")
    pytest.importorskip("grafeo")
    from dpc_client_core.dpc_agent.retrieval.grafeo import GrafeoTextIndex
    return GrafeoTextIndex(tmp_path / "store.grafeo")


def _unit(seed: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[seed % DIM] = 1.0
    return v


def _addresses(hint: str):
    return re.findall(r'read_file\("([^"]+)"\)', hint)


@pytest.mark.parametrize("kind", ["native", "grafeo"])
def test_vector_search_returns_a_meta_that_still_addresses_the_file(kind, world, tmp_path):
    index = _vector_index(kind, tmp_path)
    metas = _metas(world)
    index.add([VectorAddItem(vector=_unit(i), meta=m) for i, m in enumerate(metas)])

    found = index.search(_unit(1), top_k=5)
    assert found, "search returned nothing — the write did not land"

    ctx = ToolContext(agent_root=world["agent_root"],
                      firewall=_Firewall(allowed=[str(world["ext_root"])]))
    for meta, _score in found:
        hint = format_recall_hints(
            [SearchResult(chunk_meta=meta, score=1.0, source="vector")], max_results=1)
        addresses = _addresses(hint)
        assert addresses, f"{kind}: no address for {meta.get('source_file')!r} — {hint}"
        content = read_file(ctx, addresses[0])
        assert not content.startswith("⚠️"), f"{kind}: {addresses[0]!r} -> {content}"


@pytest.mark.parametrize("kind", ["native", "grafeo"])
def test_text_search_returns_a_meta_that_still_addresses_the_file(kind, world, tmp_path):
    index = _text_index(kind, tmp_path)
    metas = _metas(world)
    index.add([TextAddItem(text=m["text"], meta=m) for m in metas])

    found = index.search("external layer backlog", top_k=5)
    assert found, "search returned nothing — the write did not land"

    ctx = ToolContext(agent_root=world["agent_root"],
                      firewall=_Firewall(allowed=[str(world["ext_root"])]))
    for meta, _score in found:
        hint = format_recall_hints(
            [SearchResult(chunk_meta=meta, score=1.0, source="text")], max_results=1)
        addresses = _addresses(hint)
        assert addresses, f"{kind}: no address for {meta.get('source_file')!r} — {hint}"
        content = read_file(ctx, addresses[0])
        assert not content.startswith("⚠️"), f"{kind}: {addresses[0]!r} -> {content}"


@pytest.mark.parametrize("kind", ["native", "grafeo"])
def test_the_field_the_address_is_built_from_survives_the_round_trip(kind, world, tmp_path):
    """Stated on the field itself, so a failure names the cause instead of the symptom."""
    index = _vector_index(kind, tmp_path)
    metas = _metas(world)
    index.add([VectorAddItem(vector=_unit(i), meta=m) for i, m in enumerate(metas)])

    by_key = {m["source_file"]: m for m, _ in index.search(_unit(1), top_k=5)}
    for original in metas:
        returned = by_key.get(original["source_file"])
        assert returned is not None, f"{kind}: {original['source_file']} not returned"
        assert returned.get("source_path") == original["source_path"], (
            f"{kind}: source_path lost in the store for {original['source_file']}")


def _read_access_log(agent_root):
    import json
    path = agent_root / "state" / "knowledge_access.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_the_log_records_the_addresses_that_were_printed(world, tmp_path):
    """Attribution is a string comparison, so the strings have to be the printed ones."""
    from dpc_client_core.dpc_agent.active_recall import get_recall_block, render_recall_hints

    metas = _metas(world)
    results = [SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in metas]
    agent_root = world["agent_root"]

    block = get_recall_block(results, context_usage_ratio=0.0, agent_root=agent_root,
                             task_id="group-b88b65076b85")
    printed = _addresses(block.text)
    entry = _read_access_log(agent_root)[-1]

    assert entry["task_id"] == "group-b88b65076b85"
    assert entry["addresses"] == printed, "logged addresses are not the ones in the block"
    assert len(entry["addresses"]) == len(entry["files"])


def test_an_unreachable_hint_is_recorded_as_a_null_address(world, tmp_path):
    """A slot the agent cannot follow has to be countable, not merely visible in the block.

    Written first against the graph channel's meta; that case now never reaches a slot
    (see `test_a_hint_with_neither_address_nor_text_gets_no_slot`), so the remaining —
    and the only honest — null is the external document offered while extended read is
    off: it says so, quotes the file, and records that no address was printed.
    """
    from dpc_client_core.dpc_agent.active_recall import get_recall_block

    ext_meta = [m for m in _metas(world) if m["source_layer"] == "EXT"][0]
    results = [SearchResult(chunk_meta=ext_meta, score=1.0, source="hybrid")]
    agent_root = world["agent_root"]

    get_recall_block(results, context_usage_ratio=0.0, agent_root=agent_root,
                     extended_read_enabled=False, task_id="t-1")
    entry = _read_access_log(agent_root)[-1]

    assert entry["addresses"] == [None]
    assert entry["files"] == [ext_meta["source_file"]]


def test_the_hints_mode_records_no_addresses_because_it_prints_none(world, tmp_path):
    from dpc_client_core.dpc_agent.active_recall import get_recall_block

    results = [SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in _metas(world)]
    agent_root = world["agent_root"]

    # Between CONTEXT_THRESHOLD_HINTS_ONLY (0.5) and CONTEXT_THRESHOLD_SKIP (0.7):
    # above 0.7 nothing is injected at all and there would be no entry to check.
    block = get_recall_block(results, context_usage_ratio=0.6, agent_root=agent_root,
                             task_id="t-2")
    assert block.mode == "hints"
    assert 'read_file("' not in block.text
    assert _read_access_log(agent_root)[-1]["addresses"] == []


def test_a_hint_with_neither_address_nor_text_gets_no_slot(world, tmp_path):
    """The graph channel's meta: nothing to open, nothing to read. It should not cost a slot."""
    from dpc_client_core.dpc_agent.active_recall import get_recall_block

    graph_meta = {"source_file": "graviton-knowledge-graph-framework.md",
                  "source_layer": "L7", "heading": "Graviton"}
    usable = _metas(world)
    results = [SearchResult(chunk_meta=graph_meta, score=9.0, source="graph")] + [
        SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in usable]

    block = get_recall_block(results, context_usage_ratio=0.0,
                             agent_root=world["agent_root"], task_id="t-3")

    assert "graviton" not in block.text, block.text
    assert len(block.injected) == len(usable)
    assert _read_access_log(world["agent_root"])[-1]["files"] == [m["source_file"] for m in usable]


def test_an_honest_dead_end_with_an_excerpt_keeps_its_slot(world, tmp_path):
    """Narrowness matters: 'no address' alone must not evict the EXT toggle-off case."""
    from dpc_client_core.dpc_agent.active_recall import get_recall_block, hint_address

    ext_meta = [m for m in _metas(world) if m["source_layer"] == "EXT"][0]
    assert hint_address(ext_meta, extended_read_enabled=False) is None
    assert ext_meta["text"]

    block = get_recall_block([SearchResult(chunk_meta=ext_meta, score=1.0, source="hybrid")],
                             context_usage_ratio=0.0, agent_root=world["agent_root"],
                             extended_read_enabled=False, task_id="t-4")

    assert ext_meta["source_file"] in block.text
    assert "external layer" in block.text
    assert len(block.injected) == 1
