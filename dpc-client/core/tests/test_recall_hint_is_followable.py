"""Whatever the hint prints, read_file must be able to open it.

This is the test that was missing. Active Recall printed
`call read_file("EXT/backlog.md")` for 102 days; `EXT/` does not exist relative to a
sandbox, so every follow failed — 28 attempts, 0 successes — while every unit test
around it passed, because each half was tested alone and nobody ran the join.

So these tests do the join: build the metas exactly as the indexer does, take the
string out of the rendered hint, and hand *that* to read_file.
"""
import pathlib
import re

import pytest

from dpc_client_core.dpc_agent.active_recall import format_recall_hints, hint_address
from dpc_client_core.dpc_agent.hybrid_search import SearchResult
from dpc_client_core.dpc_agent.index_keys import build_ext_roots, ext_key, l5_key, l6_key
from dpc_client_core.dpc_agent.tools.core import read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext


class _Firewall:
    """Only the three questions the read path actually asks."""

    def __init__(self, extended_read=True, knowledge=True, allowed=()):
        self._extended_read = extended_read
        self._knowledge = knowledge
        self._allowed = [str(p) for p in allowed]

    def get_extended_read_enabled(self, profile_name=None):
        return self._extended_read

    def get_extended_write_enabled(self, profile_name=None):
        return False

    def can_agent_access_context(self, context_type, profile_name=None):
        return self._knowledge if context_type == "knowledge" else False

    def is_extended_path_allowed(self, path, require_write=False, profile_name=None):
        return any(str(path).startswith(root) for root in self._allowed)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A sandbox, a shared knowledge layer, and one external root — as in production."""
    agent_root = tmp_path / "agents" / "agent_x"
    (agent_root / "knowledge").mkdir(parents=True)
    (agent_root / "knowledge" / "own-note.md").write_text("# Own\nsandbox layer", encoding="utf-8")

    dpc_home = tmp_path / "dpc"
    (dpc_home / "knowledge").mkdir(parents=True)
    (dpc_home / "knowledge" / "commit.md").write_text("# Commit\nshared layer", encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(dpc_home))

    ext_root = tmp_path / "projects" / "dpc-messenger"
    ext_root.mkdir(parents=True)
    (ext_root / "backlog.md").write_text("# Backlog\nexternal layer", encoding="utf-8")

    return {
        "agent_root": agent_root,
        "knowledge_dir": agent_root / "knowledge",
        "l6_dir": dpc_home / "knowledge",
        "ext_root": ext_root,
    }


def _meta(path: pathlib.Path, key: str, layer: str) -> dict:
    """Exactly the shape agent_manager stores."""
    return {"source_file": key, "source_layer": layer, "source_path": str(path),
            "heading": "H", "text": path.read_text(encoding="utf-8")[:500]}


def _metas(world):
    l5 = world["knowledge_dir"] / "own-note.md"
    l6 = world["l6_dir"] / "commit.md"
    ext = world["ext_root"] / "backlog.md"
    roots = build_ext_roots([str(world["ext_root"])])
    return [
        _meta(l5, l5_key(l5, world["knowledge_dir"]), "L5"),
        _meta(l6, l6_key(l6, world["l6_dir"]), "L6"),
        _meta(ext, ext_key(ext, roots), "EXT"),
    ]


def _addresses_from_hint(text: str):
    return re.findall(r'read_file\("([^"]+)"\)', text)


def _ctx(world, firewall):
    return ToolContext(agent_root=world["agent_root"], firewall=firewall)


def test_every_printed_address_opens(world):
    """The acceptance criterion, stated as one assertion per layer."""
    firewall = _Firewall(allowed=[str(world["ext_root"])])
    ctx = _ctx(world, firewall)
    results = [SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in _metas(world)]

    hint = format_recall_hints(results, max_results=3)
    addresses = _addresses_from_hint(hint)
    assert len(addresses) == 3, hint

    for address, expected in zip(addresses, ["sandbox layer", "shared layer", "external layer"]):
        content = read_file(ctx, address)
        assert not content.startswith("⚠️"), f"{address!r} -> {content}"
        assert expected in content


def test_a_layer_prefixed_key_is_not_a_path(world):
    """What the hint used to print, kept as a test so it cannot come back.

    `L6/` and `EXT/` are names of layers, not directories. Resolved against the
    sandbox — which is what read_file does with any relative path — they land on
    nothing, and the tool answers with a not-found that reads like the file is gone
    rather than like the address was never valid.
    """
    ctx = _ctx(world, _Firewall(allowed=[str(world["ext_root"])]))
    assert read_file(ctx, "L6/commit.md").startswith("⚠️")
    assert read_file(ctx, "EXT/backlog.md").startswith("⚠️")
    assert read_file(ctx, "EXT/dpc-messenger/backlog.md").startswith("⚠️")


def test_the_shared_layer_needs_no_extended_path_entry(world):
    """L6 is admitted to the index by the knowledge gate, so the read honours that gate.

    Without this, an agent could hold a document, be offered it, and be refused it —
    because a second, unrelated list never mentioned the directory.
    """
    firewall = _Firewall(allowed=[])  # nothing in the extended path list at all
    ctx = _ctx(world, firewall)
    l6 = world["l6_dir"] / "commit.md"
    assert "shared layer" in read_file(ctx, str(l6))


def test_revoked_knowledge_access_still_denies_the_shared_layer(world):
    firewall = _Firewall(knowledge=False, allowed=[])
    ctx = _ctx(world, firewall)
    l6 = world["l6_dir"] / "commit.md"
    assert read_file(ctx, str(l6)).startswith("⚠️")


def test_external_hint_says_so_instead_of_printing_a_dead_path(world):
    """A hint that cannot be followed must not look like one that can."""
    metas = _metas(world)
    results = [SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in metas]
    hint = format_recall_hints(results, max_results=3, extended_read_enabled=False)

    addresses = _addresses_from_hint(hint)
    assert all("projects" not in a for a in addresses)
    assert "extended path read access is off" in hint
    # The two layers that do not depend on that toggle are unaffected.
    assert len(addresses) == 2


def test_hint_carries_an_excerpt(world):
    """~28,900 injections delivered nothing but a filename and a broken path."""
    results = [SearchResult(chunk_meta=m, score=1.0, source="hybrid") for m in _metas(world)]
    hint = format_recall_hints(results, max_results=3)
    assert "external layer" in hint


def test_meta_without_source_path_offers_nothing(world):
    """An index built before source_path existed has no address to give."""
    assert hint_address({"source_file": "L6/old.md", "source_layer": "L6"}) is None


def test_sandbox_layer_address_is_the_key_itself(world):
    key = l5_key(world["knowledge_dir"] / "own-note.md", world["knowledge_dir"])
    assert hint_address({"source_file": key, "source_layer": "L5"}) == key
    assert key == "knowledge/own-note.md"
