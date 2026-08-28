"""Stopping the leak is not the same as undoing it.

The gate that decides whether an agent may hold the shared layer was never asked about
the rows a broken gate had already written. Those rows outlive the fix, and nothing in
the ordinary indexing path removes them: the incremental pass diffs against
`index_meta.json`, which never knew about them either.

So the purge is checked on what it actually does to a store — one agent allowed the
layer, one denied it, both holding shared rows and one of their own. The denied store
must lose exactly its shared rows; the allowed store must lose nothing; and the denied
agent's own document must survive, because a purge that takes the sandbox with it is
not a narrower failure than the leak.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from dpc_client_core.dpc_agent.index_keys import l5_key, l6_key
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem, VectorAddItem
from dpc_client_core.dpc_agent.retrieval.native import NativeTextIndex, NativeVectorIndex
from dpc_client_core.service import CoreService

DIM = 384


class _Firewall:
    def __init__(self, denied):
        self._denied = set(denied)

    def can_agent_access_context(self, context_type, profile_name=None):
        return profile_name not in self._denied


class _Service:
    """Only `firewall` is reachable from the method under test."""

    def __init__(self, firewall):
        self.firewall = firewall


def _vec(i):
    v = np.zeros(DIM, dtype=np.float32)
    v[i % DIM] = 1.0
    return v


def _stock(agent_root, l6_dir, shared, own_name):
    index_dir = agent_root / "state" / "memory_index"
    index_dir.mkdir(parents=True)
    vec, txt = NativeVectorIndex(index_dir, dimensions=DIM), NativeTextIndex(index_dir)

    own = agent_root / "knowledge" / own_name
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("# Own\nsandbox layer", encoding="utf-8")
    keys = [l5_key(own, agent_root / "knowledge")]
    keys += [l6_key(p, l6_dir) for p in shared]

    for i, key in enumerate(keys):
        meta = {"source_file": key, "heading": key, "char_count": 10,
                "source_layer": "L6" if key.startswith("L6/") else "L5",
                "source_path": key}
        vec.add([VectorAddItem(vector=_vec(i), meta=meta)])
        txt.add([TextAddItem(text=f"body of {key}", meta=meta)])
    vec.save()
    txt.save()
    return index_dir


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "dpc"
    l6_dir = home / "knowledge"
    l6_dir.mkdir(parents=True)
    shared = []
    for name in ("alpha_commit-1.md", "beta_commit-2.md", "gamma_commit-3.md"):
        p = l6_dir / name
        p.write_text(f"# {name}\nshared layer", encoding="utf-8")
        shared.append(p)

    agents = home / "agents"
    for agent_id, own in (("agent_allowed", "mine-a.md"), ("agent_denied", "mine-d.md")):
        _stock(agents / agent_id, l6_dir, shared, own)

    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", home)
    return {"home": home, "agents": agents, "shared": shared}


def _run(denied):
    return asyncio.run(
        CoreService.purge_denied_shared_knowledge(_Service(_Firewall(denied))))


def _keys_in_store(agent_root):
    meta = agent_root / "state" / "memory_index" / "index_meta.json"
    chunks = json.loads(meta.read_text(encoding="utf-8"))["chunks"]
    return {c.get("source_file") for c in chunks}


def test_denied_agent_loses_the_shared_layer_and_keeps_its_own(world):
    result = _run({"agent_denied"})

    assert result["status"] == "ok"
    assert result["shared_documents"] == 3
    denied = next(a for a in result["agents"] if a["agent_id"] == "agent_denied")
    assert denied["gate"] == "closed"
    assert denied["removed_vectors"] == 3
    assert denied["removed_text"] == 3

    left = _keys_in_store(world["agents"] / "agent_denied")
    assert not any(k.startswith("L6/") for k in left)
    assert left == {"knowledge/mine-d.md"}


def test_allowed_agent_is_not_touched(world):
    _run({"agent_denied"})

    left = _keys_in_store(world["agents"] / "agent_allowed")
    assert sum(k.startswith("L6/") for k in left) == 3
    assert "knowledge/mine-a.md" in left


def test_a_gate_that_cannot_be_asked_counts_as_denied(world):
    """Fail-closed, the same reading the indexing path uses."""
    result = asyncio.run(CoreService.purge_denied_shared_knowledge(_Service(None)))

    assert result["status"] == "ok"
    assert all(a["gate"] == "closed" for a in result["agents"])
    for agent_id in ("agent_allowed", "agent_denied"):
        assert not any(k.startswith("L6/") for k in _keys_in_store(world["agents"] / agent_id))
