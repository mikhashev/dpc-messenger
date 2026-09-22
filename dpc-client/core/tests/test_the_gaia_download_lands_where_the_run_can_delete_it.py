"""The gated split is downloaded into the run's own cache, or the run stops.

Measured 2026-09-23 on a `--limit 2` smoke: `resolve_hf_token` imported
`huggingface_hub` before `redirect_hub_into`, the library froze its cache path
at that import, and `hf_hub_download` wrote `metadata.level1.parquet` into the
machine's real cache. The log said «hub cache removed: True» — it had removed
the directory it meant to use — and the agent phase started with the answers
on disk. The run was then killed, so its decoys were never removed either.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402

MARKER = "datasets--gaia-benchmark--GAIA"


def _args(**over):
    base = dict(
        limit=1, with_files=False, provider_alias=None, model="fake-model",
        base_url="http://127.0.0.1:1", context_window=4096, temperature=0.0,
        reasoning_effort="low", auto_approve=False, json=None, keep=False,
        allow_reachable_gold=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class _Llm:
    async def shutdown(self):
        pass


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A machine whose `huggingface_hub` was imported before the redirect.

    `hf_hub_download` is replaced by a recorder that behaves like the real one
    on the point that matters: with no `cache_dir` it writes where the frozen
    constant points. No network, no model.
    """
    import huggingface_hub
    from huggingface_hub import constants
    import dpc_client_core.llm_manager as llm_mod
    import dpc_client_core.dpc_agent.agent as agent_mod

    machine_hub = tmp_path / "machine-hub"
    machine_hub.mkdir()
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(machine_hub))
    monkeypatch.setattr(constants, "HUGGINGFACE_HUB_CACHE", str(machine_hub))
    monkeypatch.setattr(constants, "HF_HOME", str(tmp_path / "machine-home"))
    monkeypatch.setattr(gaia.tempfile, "tempdir", str(tmp_path))
    results = tmp_path / "results"
    results.mkdir()

    calls = []
    state = {"honour_cache_dir": True}

    def _download(repo, filename, repo_type=None, token=None, cache_dir=None, **kw):
        calls.append(cache_dir)
        if not state["honour_cache_dir"]:
            root = machine_hub          # a download that went somewhere else entirely
        else:
            root = Path(cache_dir) if cache_dir else Path(constants.HF_HUB_CACHE)
        path = root / MARKER / "snapshots" / "rev1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"parquet")
        return str(path)

    parquet = types.ModuleType("pyarrow.parquet")
    parquet.read_table = lambda path: types.SimpleNamespace(
        to_pylist=lambda: [{"task_id": "t1", "Question": "q", "Final answer": "7"}])
    pyarrow = types.ModuleType("pyarrow")
    pyarrow.parquet = parquet
    monkeypatch.setitem(sys.modules, "pyarrow", pyarrow)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", parquet)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", _download)

    agents = []

    class _Agent:
        def __init__(self, *a, agent_root=None, **k):
            agents.append(agent_root)
            Path(agent_root).mkdir(parents=True, exist_ok=True)

    async def _one(*a, **k):
        return {"task_id": "t1", "gold_sha256": "x", "answer": "FINAL ANSWER: 7",
                "correct": True, "error": None, "had_attachment": False,
                "seconds": 1.0, "usage": {}}

    monkeypatch.setenv("HF_TOKEN", "test-token")
    for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_DATASETS_CACHE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gaia, "RESULTS_DIR", results)
    monkeypatch.setattr(gaia, "GOLD_ARCHIVE", tmp_path / "no-archive")
    monkeypatch.setattr(gaia, "hub_caches_in_effect", lambda: [machine_hub])
    monkeypatch.setattr(gaia.provenance, "snapshot", lambda **k: {})
    monkeypatch.setattr(llm_mod, "LLMManager", lambda *a, **k: _Llm())
    monkeypatch.setattr(agent_mod, "DpcAgent", _Agent)
    monkeypatch.setattr(agent_mod, "AgentConfig", lambda *a, **k: None)
    monkeypatch.setattr(gaia, "run_one", _one)
    return types.SimpleNamespace(hub=machine_hub, calls=calls, state=state,
                                 agents=agents, results=results, tmp=tmp_path)


def test_an_import_before_the_redirect_does_not_move_the_download(machine, capsys):
    gaia.resolve_hf_token()          # the import that froze the constant on 2026-09-23

    assert asyncio.run(gaia.main_async(_args())) == 0

    assert machine.calls and all(c and "dpc-gaia-" in c for c in machine.calls), machine.calls
    assert not (machine.hub / MARKER).exists(), "the split reached the machine's cache"
    assert gaia._DATASET_STATE["private_cache_removed"] is True
    assert "hub cache removed: True" in capsys.readouterr().out


def test_the_redirect_moves_the_constant_the_library_already_froze(machine, tmp_path):
    from huggingface_hub import constants

    private, previous = gaia.redirect_hub_into(tmp_path / "run")
    try:
        assert Path(constants.HF_HUB_CACHE) == private / "hub"
    finally:
        gaia.restore_hub(previous)
    assert str(tmp_path / "run") not in constants.HF_HUB_CACHE


def test_a_download_outside_the_private_cache_stops_before_anything_is_deleted(
        machine, capsys):
    machine.state["honour_cache_dir"] = False     # the library ignores where we asked

    assert asyncio.run(gaia.main_async(_args())) == gaia.CONTAMINATED_EXIT

    leaked = list((machine.hub / MARKER).rglob("metadata.level1.parquet"))
    assert leaked, "the stray copy is left for the operator, and named"
    out = capsys.readouterr().out
    assert "ABORTED" in out and str(leaked[0]) in out
    assert machine.agents == [], "no agent may start beside the answers"


def test_a_copy_that_appears_during_setup_stops_the_run_before_the_first_task(
        machine, monkeypatch, capsys):
    calls = {"n": 0}
    leak = machine.hub / MARKER

    def _reachable(*a, **k):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [leak]

    monkeypatch.setattr(gaia, "reachable_gold", _reachable)

    assert asyncio.run(gaia.main_async(_args())) == gaia.CONTAMINATED_EXIT

    assert calls["n"] == 2, "the second enumeration is the one that caught it"
    assert machine.agents == []
    assert "readable after setup" in capsys.readouterr().out


def test_copies_allowed_at_the_start_do_not_count_as_a_new_leak(machine, monkeypatch):
    known = machine.tmp / "archive-copy.json"
    monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [known])

    assert asyncio.run(gaia.main_async(_args(allow_reachable_gold=True))) == 0


# --- decoys a killed run left behind -----------------------------------------


def _decoy(where, note=gaia.CANARY_NOTE):
    where.mkdir(parents=True, exist_ok=True)
    path = where / gaia.CANARY_NAME
    path.write_text(json.dumps({"canary": "GAIA-CANARY-x", "note": note}), encoding="utf-8")
    return path


def test_only_a_file_carrying_the_harness_note_is_removed(tmp_path):
    ours = _decoy(tmp_path / "hub")
    theirs = _decoy(tmp_path / "other", note="somebody else's file")
    not_json = tmp_path / "third" / gaia.CANARY_NAME
    not_json.parent.mkdir()
    not_json.write_text("plain text", encoding="utf-8")

    removed = gaia.remove_stale_decoys(
        [tmp_path / "hub", tmp_path / "other", tmp_path / "third", tmp_path / "absent"],
        None)

    assert removed == [ours] and not ours.exists()
    assert theirs.exists() and not_json.exists()


def test_the_next_run_removes_what_a_killed_run_planted(machine, capsys):
    stale = [_decoy(machine.hub), _decoy(machine.results)]

    assert asyncio.run(gaia.main_async(_args())) == 0

    assert not any(p.exists() for p in stale)
    assert "removed 2 stale decoy(s)" in capsys.readouterr().out
