"""Agent state as earlier versions wrote it, so migration paths can be tested on it.

Every defect of this repair had one shape. The tests were green, the first restart was
red, and the difference was that the tests built their state from scratch while
production is made of rows written by code that no longer exists. Four times in two
sessions: the addressing bug that lived 102 days behind hand-built metas; the S59
end-to-end test that repeated the mistake one layer down; the store that dropped
`source_path`; and the guard that read a node's stale layer label and so refused to
import the shared layer at all — 1818 warnings, caught by a restart rather than by the
suite that had just passed.

What a clean-state test asserts is that new code writes what new code reads. That is
worth asserting and it is not the question production asks. The question production
asks is what happens to the rows that are already there.

So the forms below are not invented. Each was measured on live state, and carries the
measurement and the change that stopped producing it:

    F1  graph node, pre-key scheme      214 of 214 KnowledgeFile nodes in agent_001's
                                        store as of 2026-05-17 — id `kf:<stem>`,
                                        `source_layer` "L5" whatever the real layer,
                                        properties exactly {path, size_bytes,
                                        file_mtime}. Superseded by f3c5d903 (key as id)
                                        and a4ee1813 (layer as a parameter).
    F2  access-log line, pre-address    7161 of 7170 lines in agent_001's
                                        knowledge_access.jsonl on 2026-08-01: no
                                        `addresses`, no `task_id`, and `files` holding
                                        bare names — 9642 bare entries against 11866
                                        keyed. Superseded by adaaaa45 (keys) and
                                        514d77eb (addresses).
    F3  _meta.json entry, pre-write     Entries with `access_count` but no
                                        `last_written`/`write_count`. Superseded by
                                        5ee3625a. Production carries none today (all
                                        seven agents migrated on first read, measured
                                        2026-08-01) — this form survives here and only
                                        here, which is the point: the migration code is
                                        still live and nothing else can exercise it.
    F4  stored chunk meta, pre-path     Vector/text rows written without `source_path`,
                                        the 102-day bug itself. Measured before the
                                        v4 rebuild: 11 occurrences against 4316
                                        `source_file`. Superseded by a2ddcdae.
    F5  index header, older marker      `key_format` naming a previous scheme. This is
                                        the form that *announces* the others; every
                                        agent carried `layer_addressed_v3` until
                                        2026-08-01 15:36.

Nothing here reproduces a whole agent. The forms are the ones we know by name because
each one has already broken something, and a fixture that grew to a production snapshot
would be both unmaintainable and full of Mike's private documents.
"""

from __future__ import annotations

import json
import pathlib
from typing import Dict, Iterable, List, Optional

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import GraphNode, NodeType

# The marker every agent carried before 2026-08-01. Deliberately a literal: writing
# `KEY_FORMAT` here would make the fixture agree with the code by construction and stop
# noticing the day someone forgets to bump it.
LEGACY_KEY_FORMAT = "layer_addressed_v3"
LEGACY_EMBEDDING_MODEL = "BAAI/bge-m3"


def legacy_graph_node(stem: str, *, size_bytes: int = 4868,
                      file_mtime: str = "2026-04-19T15:43:24.212575+00:00") -> GraphNode:
    """F1 — a KnowledgeFile node as the graph wrote it before keys.

    Three properties and no `source_path`, addressed by stem, and labelled L5 whether
    or not the document lived in the agent's own layer. All three are load-bearing: the
    missing field is what a hint needs for an address, the stem is what a seed lookup
    used to be cut down to match, and the label is what the guard in 3841d66d believed.
    """
    return GraphNode(
        node_id=f"kf:{stem}",
        node_type=NodeType.KNOWLEDGE_FILE,
        label=stem.replace("_", " ").replace("-", " ").title(),
        source_layer="L5",
        properties={
            "path": f"{stem}.md",
            "size_bytes": size_bytes,
            "file_mtime": file_mtime,
        },
    )


def legacy_access_log_line(files: Iterable[str], *, ts: str, mode: str = "full") -> str:
    """F2 — one injection as it was recorded before addresses and before keys."""
    return json.dumps({"ts": ts, "mode": mode, "files": list(files), "useful": None})


def write_legacy_access_log(agent_root: pathlib.Path, entries: List[dict]) -> pathlib.Path:
    """F2 — knowledge_access.jsonl holding only pre-address lines."""
    path = agent_root / "state" / "knowledge_access.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(legacy_access_log_line(e["files"], ts=e["ts"]) for e in entries) + "\n",
        encoding="utf-8",
    )
    return path


def legacy_meta_entry(*, access_count: int, last_accessed: str,
                      source_layer: str = "L5") -> dict:
    """F3 — a _meta.json entry from before reads and writes were told apart.

    Every one of these numbers was produced by a write: update_access had exactly one
    caller and it sat in write_file. The entry has no `last_written` and no
    `write_count`, which is what the migration keys on.
    """
    return {
        "last_accessed": last_accessed,
        "access_count": access_count,
        "last_verified": "",
        "tags": [],
        "summary": "",
        "source_layer": source_layer,
        "project": "",
        "stale": False,
    }


def write_legacy_meta_json(knowledge_dir: pathlib.Path, entries: Dict[str, dict]) -> pathlib.Path:
    path = knowledge_dir / "_meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def legacy_chunk_meta(source_file: str, *, source_layer: str = "L5",
                      heading: str = "Legacy") -> dict:
    """F4 — chunk meta as the Grafeo store handed it back before it kept source_path.

    The field is absent rather than empty, because that is how it came back: the reader
    enumerated the four properties the writer had stored, and this was not one of them.
    """
    return {
        "source_file": source_file,
        "source_layer": source_layer,
        "heading": heading,
        "char_count": 1234,
    }


def write_legacy_index_header(index_dir: pathlib.Path, *,
                              key_format: str = LEGACY_KEY_FORMAT,
                              model_name: str = LEGACY_EMBEDDING_MODEL,
                              file_hashes: Optional[Dict[str, str]] = None) -> pathlib.Path:
    """F5 — index_meta.json announcing a previous key scheme."""
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / "index_meta.json"
    path.write_text(json.dumps({
        "file_hashes": file_hashes if file_hashes is not None else {"alpha.md": "deadbeef"},
        "header": {"model_name": model_name, "key_format": key_format},
    }, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def legacy_agent_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """An agent directory in the state a previous version would have left behind.

    Files only — the graph is a separate fixture because it needs a backend and this
    one must stay usable by tests that never open it.
    """
    root = tmp_path / "agents" / "agent_legacy"
    (root / "state" / "memory_index").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    knowledge = root / "knowledge"
    knowledge.mkdir(parents=True)

    (knowledge / "alpha.md").write_text("# Alpha\nwritten long ago", encoding="utf-8")
    (knowledge / "beta.md").write_text("# Beta\nalso long ago", encoding="utf-8")

    write_legacy_access_log(root, [
        {"ts": "2026-04-20T11:51:15.921944+00:00", "files": ["alpha.md", "beta.md", "beta.md"]},
        {"ts": "2026-04-20T12:03:02.114000+00:00", "files": ["alpha.md"]},
    ])
    write_legacy_meta_json(knowledge, {
        "alpha.md": legacy_meta_entry(access_count=7, last_accessed="2026-04-20T11:00:00+00:00"),
        "beta.md": legacy_meta_entry(access_count=0, last_accessed=""),
    })
    write_legacy_index_header(root / "state" / "memory_index")
    return root
