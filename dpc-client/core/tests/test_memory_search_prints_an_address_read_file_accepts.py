"""memory_search prints an address read_file accepts, not only the index key.

Board entry MEMORY-SEARCH-GIVES-THE-AGENT-A-KEY-AND-A-SCORE-AND-IGNORES-THE-MEMORY-SWITCH-
AND-REVOKED-PATHS, step 2. The key (`L6/<rel>`, `EXT/<root>/<rel>`) is the document's
identity in the index, not a path, and agents that guessed a path from it failed 6 of ~37
reads; on one agent a work folder literally named `L6/` made read_file("L6/x") open the wrong
file without a word. The result now carries the address Active Recall would print
(hint_address, with the gates recall asks), the heading, a short excerpt, the layer and the
channels that found it, under a header saying what was searched.
"""

from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

import numpy as np
import pytest

from dpc_client_core.dpc_agent import memory as memory_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.agent import OTHER_NODE_TURN
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem, VectorAddItem
from dpc_client_core.dpc_agent.tools.core import memory_search, read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.providers import model_sizes

PROFILE = "agent_x"
QUERY = "harbour inventory ledger"
OWN_MARK = "OWN-BODY-MARK-31aa"
SHARED_MARK = "SHARED-BODY-MARK-77de"
WORK_MARK = "WORK-FOLDER-MARK-5c20"
EXT_MARK = "EXT-BODY-MARK-90b4"

# BM25 gives a word in every document no weight; unrelated rows keep the query words rare.
FILLERS = [{"source_file": f"knowledge/filler-{i}.md", "source_layer": "L5",
            "heading": f"Filler {i}", "text": f"unrelated weather almanac page {i}"}
           for i in range(6)]
ADDRESS = re.compile(r'read_file\("(?P<path>[^"]+)"\)')


class _Provider:
    def __init__(self, model_name="test/model", dims=4, max_tokens=4096):
        self.model_name, self.dimensions, self.max_tokens = model_name, dims, max_tokens

    def embed(self, text):
        return [0.1] * self.dimensions


def _write_rules(w, extended_read=True, shared=True, memory=True):
    w["rules"].write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {
            "human_knowledge_access": shared,
            "sandbox_extensions": {"read_only": [str(w["ext_root"])],
                                   "extended_read_enabled": extended_read},
            "memory": {"enabled": memory, "active_recall": True},
        }},
    }), encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))

    root = tmp_path / "agents" / PROFILE
    (root / "knowledge").mkdir(parents=True)
    Memory(root).ensure_files()
    own = root / "knowledge" / "own-note.md"
    own.write_text(f"# Own Note\n{QUERY} {OWN_MARK}", encoding="utf-8")

    shared = home / ".dpc" / "knowledge" / "x.md"
    shared.parent.mkdir(parents=True)
    shared.write_text(f"# Shared X\n{QUERY} {SHARED_MARK}", encoding="utf-8")
    # A work folder named like the key prefix, holding another file of the same name.
    work = root / "L6" / "x.md"
    work.parent.mkdir()
    work.write_text(f"# Work X\n{WORK_MARK}", encoding="utf-8")

    ext_root = tmp_path / "projects" / "open-project"
    ext_root.mkdir(parents=True)
    ext = ext_root / "notes.md"
    ext.write_text(f"# Open Notes\n{QUERY} {EXT_MARK}", encoding="utf-8")

    w = {"root": root, "own": own, "shared": shared, "work": work, "ext": ext,
         "ext_root": ext_root, "rules": tmp_path / "privacy_rules.json"}
    _write_rules(w)
    w["firewall"] = ContextFirewall(w["rules"])

    real_backend = retrieval_mod.make_backend_for_agent
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda r, **kw: real_backend(r, dimensions=4))
    # No embedding model: the word channel alone answers, unless a test builds vectors.
    monkeypatch.setattr(memory_mod, "get_embedding_provider", lambda *a, **kw: _Provider())
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)
    w["real_backend"] = real_backend
    return w


def _meta(key, layer, path, heading, text=None):
    return {"source_file": key, "source_layer": layer, "source_path": str(path),
            "heading": heading,
            "text": text if text is not None else pathlib.Path(path).read_text(encoding="utf-8")}


def _metas(w):
    return [_meta("knowledge/own-note.md", "L5", w["own"], "Own Note"),
            _meta("L6/x.md", "L6", w["shared"], "Shared X"),
            _meta("EXT/open-project/notes.md", "EXT", w["ext"], "Open Notes")]


def _index(w, metas):
    backend = w["real_backend"](w["root"], dimensions=4)
    backend.text.add([TextAddItem(text=m["text"], meta=m) for m in metas + FILLERS])
    backend.text.save()


def _ctx(w, **kw):
    kw.setdefault("message_source", "ui")
    ctx = ToolContext(agent_root=w["root"], firewall=w["firewall"], **kw)
    ctx._agent = SimpleNamespace(_firewall_profile=PROFILE)
    return ctx


def _result_block(answer, key_fragment):
    """The lines of the one result whose first line mentions key_fragment or heading."""
    lines = answer.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\d+\. \[", line) and key_fragment in line:
            end = i + 1
            while end < len(lines) and not re.match(r"\d+\. \[", lines[end]):
                end += 1
            return lines[i:end]
    raise AssertionError(f"no result for {key_fragment!r} in:\n{answer}")


def _addresses(answer):
    return [m.group("path") for m in ADDRESS.finditer(answer)]


# -- the address ---------------------------------------------------------------


def test_an_l5_row_prints_its_key_as_the_address_and_no_second_name(world):
    _index(world, _metas(world))

    block = _result_block(memory_search(_ctx(world), QUERY, top_k=10), "Own Note")

    assert 'read_file("knowledge/own-note.md")' in block[0]
    assert not any(line.strip().startswith("key:") for line in block)


def test_an_l6_row_prints_the_source_path_not_the_key(world):
    _index(world, _metas(world))

    answer = memory_search(_ctx(world), QUERY, top_k=10)
    block = _result_block(answer, "Shared X")

    assert f'read_file("{world["shared"]}")' in block[0]
    assert 'read_file("L6/x.md")' not in answer
    assert any("key: L6/x.md" in line for line in block), block


def test_a_work_folder_named_l6_does_not_capture_the_printed_address(world):
    _index(world, _metas(world))
    ctx = _ctx(world)

    answer = memory_search(ctx, QUERY, top_k=10)
    address = next(a for a in _addresses(answer) if a.endswith("x.md"))

    assert SHARED_MARK in read_file(ctx, address)
    # The trap the card names: the key, taken for a path, opens the work file.
    assert WORK_MARK in read_file(ctx, "L6/x.md")


def test_an_ext_row_prints_the_source_path_while_extended_read_is_on(world):
    _index(world, _metas(world))

    block = _result_block(memory_search(_ctx(world), QUERY, top_k=10), "Open Notes")

    assert f'read_file("{world["ext"]}")' in block[0]
    assert any("key: EXT/open-project/notes.md" in line for line in block)


def test_an_ext_row_says_not_readable_when_extended_read_is_off(world):
    _index(world, _metas(world))
    _write_rules(world, extended_read=False)
    assert world["firewall"].reload()[0]

    # The per-path grant is what drops a row; with the global flag off the row stays and
    # says so, as in recall (the flag governs reaching the file, not seeing it).
    answer = memory_search(_ctx(world), QUERY, top_k=10)
    block = _result_block(answer, "Open Notes")

    assert "not readable from here — extended path read access is off" in block[0]
    assert "read_file(" not in block[0]
    assert EXT_MARK in block[-1], "the excerpt stays, as in recall"


def test_a_row_with_no_recorded_path_says_so_instead_of_blaming_a_toggle(world):
    old = {"source_file": "EXT/old/row.md", "source_layer": "EXT", "heading": "Old Row",
           "text": f"{QUERY} old row body"}
    _index(world, [old])
    ctx = ToolContext(agent_root=world["root"], firewall=None, message_source="ui")

    answer = memory_search(ctx, QUERY, top_k=10)

    assert "not readable from here — no path was recorded when it was indexed" in answer


def test_every_printed_address_opens_the_intended_file(world):
    _index(world, _metas(world))
    ctx = _ctx(world)

    answer = memory_search(ctx, QUERY, top_k=10)
    wanted = {"knowledge/own-note.md": OWN_MARK, str(world["shared"]): SHARED_MARK,
              str(world["ext"]): EXT_MARK}

    assert set(wanted) <= set(_addresses(answer)), answer
    for address, mark in wanted.items():
        text = read_file(ctx, address)
        assert mark in text, (address, text[:120])
    assert WORK_MARK not in "".join(read_file(ctx, a) for a in wanted)


# -- the header ----------------------------------------------------------------


def test_the_header_counts_documents_by_layer_and_names_the_channels(world):
    _index(world, _metas(world))

    lines = memory_search(_ctx(world), QUERY, top_k=10).splitlines()

    assert lines[0].startswith("Found ")
    assert lines[1] == ("Searched 9 documents (L5 7, L6 1, EXT 1) in layers L5, L6, EXT; "
                        "channels: text (vector skipped: embedding model not downloaded).")


def test_the_header_lists_both_channels_when_both_ran_and_each_row_says_who_found_it(
        world, monkeypatch):
    pytest.importorskip("faiss")
    cur = world["real_backend"](world["root"], dimensions=4, model_name="test/model")
    cur.vector.add([VectorAddItem(vector=np.ones(4, dtype=np.float32),
                                  meta=_metas(world)[0])])
    cur.vector._inner._header.max_tokens = 4096
    cur.vector.save()
    _index(world, _metas(world))
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)

    answer = memory_search(_ctx(world), QUERY, top_k=10)

    assert "channels: vector, text." in answer.splitlines()[1]
    assert "found by: vector+text)" in _result_block(answer, "Own Note")[0]
    assert "found by: text)" in _result_block(answer, "Shared X")[0]


def test_the_header_says_when_the_vector_channel_was_skipped_as_stale(world, monkeypatch):
    pytest.importorskip("faiss")
    old = world["real_backend"](world["root"], dimensions=4, model_name="test/old-model")
    old.vector.add([VectorAddItem(vector=np.ones(4, dtype=np.float32),
                                  meta=_metas(world)[0])])
    old.vector.save()
    _index(world, _metas(world))
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)
    monkeypatch.setattr(memory_mod, "get_embedding_provider",
                        lambda *a, **kw: _Provider(model_name="test/new-model"))

    lines = memory_search(_ctx(world), QUERY, top_k=10).splitlines()

    assert lines[0].startswith("Vector index is stale")
    assert "channels: text (vector skipped: stale index)." in lines[2]


def test_the_header_counts_only_the_layers_open_on_this_turn(world):
    _index(world, _metas(world))
    ctx = _ctx(world, turn_class=OTHER_NODE_TURN, open_layers=frozenset({"L6"}),
               message_source="group")

    answer = memory_search(ctx, QUERY, top_k=10)

    assert answer.splitlines()[1].startswith("Searched 1 documents (L6 1) in layers L6;")
    assert "knowledge/own-note.md" not in answer
    assert "open-project" not in answer
    assert "Found 1 results" in answer


def test_a_revoked_external_path_leaves_the_count_and_the_results(world):
    _index(world, _metas(world))
    world["rules"].write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {
            "human_knowledge_access": True,
            "sandbox_extensions": {"read_only": [str(world["root"] / "elsewhere")],
                                   "extended_read_enabled": True},
            "memory": {"enabled": True, "active_recall": True},
        }},
    }), encoding="utf-8")
    assert world["firewall"].reload()[0]

    answer = memory_search(_ctx(world), QUERY, top_k=10)

    assert "EXT 0" in answer.splitlines()[1]
    assert "open-project" not in answer


def test_a_closed_shared_knowledge_gate_drops_the_l6_row_and_its_content(world):
    _index(world, _metas(world))
    _write_rules(world, shared=False)
    assert world["firewall"].reload()[0]

    answer = memory_search(_ctx(world), QUERY, top_k=10)

    assert "L6/x.md" not in answer and "Shared X" not in answer and SHARED_MARK not in answer
    assert "in layers L5, EXT;" in answer.splitlines()[1]
    assert "knowledge/own-note.md" in answer


# -- the excerpt ---------------------------------------------------------------


def test_the_excerpt_is_one_line_and_capped(world):
    body = ("word \n\t line\r\n" * 80) + "TAIL-BEYOND-THE-CAP"
    meta = _meta("knowledge/long.md", "L5", world["own"], "Long", text=f"{QUERY} {body}")
    _index(world, [meta])

    answer = memory_search(_ctx(world), QUERY, top_k=10)
    block = _result_block(answer, "Long")
    excerpt = block[-1]

    assert "\r" not in answer and "\t" not in answer
    assert excerpt == "    " + " ".join(meta["text"][:200].split())
    assert len(excerpt) <= 4 + 200
    assert "TAIL-BEYOND-THE-CAP" not in answer


def test_the_heading_stands_for_a_row_without_one(world):
    meta = {"source_file": "knowledge/own-note.md", "source_layer": "L5",
            "source_path": str(world["own"]), "text": f"{QUERY} {OWN_MARK}"}
    _index(world, [meta])

    block = _result_block(memory_search(_ctx(world), QUERY, top_k=10), "knowledge/own-note.md")

    assert block[0].startswith("1. [L5] knowledge/own-note.md — ")


# -- what stayed -----------------------------------------------------------------


def test_the_refusals_are_unchanged(world):
    _index(world, _metas(world))

    assert memory_search(_ctx(world, turn_class=OTHER_NODE_TURN, open_layers=frozenset(),
                              message_source="group"), QUERY).startswith(
        "memory_search answers only the owner; this turn came from group")
    _write_rules(world, memory=False)
    assert world["firewall"].reload()[0]
    assert memory_search(_ctx(world), QUERY) == "Memory is off for this agent."


def test_the_top_k_cut_is_said_in_the_first_line(world, monkeypatch):
    from dpc_client_core.dpc_agent.retrieval.base import FusionResult
    _index(world, _metas(world))
    real = retrieval_mod.make_backend_for_agent

    def _backend(root, **kw):
        backend = real(root, **kw)
        backend.fuser = SimpleNamespace(fuse=lambda v, t: [
            FusionResult(chunk_meta=m, score=0.03 - i * 0.01, source="hybrid")
            for i, m in enumerate(_metas(world))])
        return backend

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _backend)

    answer = memory_search(_ctx(world), QUERY, top_k=1)

    assert answer.splitlines()[0] == f"Found 3 results for '{QUERY}' (showing 1):"
    assert len(_addresses(answer)) == 1
