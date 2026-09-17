"""A benchmark run must not leave its inference server holding the GPU.

The campaign will not start a run until the card is under 6 GB, so a server
that outlives the run it belonged to makes the queue wait for its own child.
Measured: 0 of 4 runs started on 2026-08-25, 1 of 4 on 2026-08-27, with
llama-server.exe still holding 29 302 MiB of 32 623 seven hours later.

The teardown existed the whole time — `LLMManager.shutdown()` walks providers
calling `close()`, and the llamacpp provider's `close()` drains and stops the
server. The eval called neither.
"""
import sys
import types
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402


class _FakeLlm:
    def __init__(self, *a, **k):
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1


class _FakeAgent:
    def __init__(self, *a, agent_root=None, **k):
        self.agent_root = Path(agent_root)
        # Whatever the loop wrote while the run was going: the thing the
        # cleanup used to take with it.
        logs = self.agent_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "tools.jsonl").write_text(
            '{"phase": "attempt", "tool": "read_file", "tool_call_id": "c1"}\n'
            '{"phase": "outcome", "tool": "read_file", "tool_call_id": "c1"}\n',
            encoding="utf-8",
        )


def _args(**over):
    base = dict(
        limit=1, with_files=False, provider_alias=None, model="fake-model",
        base_url="http://127.0.0.1:1", context_window=4096, temperature=0.0,
        reasoning_effort="high", auto_approve=False, json=None, keep=False,
        allow_reachable_gold=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _outcome():
    return {
        "task_id": "t1", "gold_sha256": "abc", "answer": "FINAL ANSWER: 7", "correct": True,
        "error": None, "had_attachment": False, "seconds": 1.0,
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                  "rounds": 1, "cost_usd": 0.0,
                  "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None},
    }


@pytest.fixture
def harness(monkeypatch):
    """The eval with its dataset, model and provenance replaced, nothing else."""
    import dpc_client_core.llm_manager as llm_mod
    import dpc_client_core.dpc_agent.agent as agent_mod

    made = []

    def _make_llm(*a, **k):
        llm = _FakeLlm()
        made.append(llm)
        return llm

    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(llm_mod, "LLMManager", _make_llm)
    monkeypatch.setattr(agent_mod, "DpcAgent", _FakeAgent)
    monkeypatch.setattr(agent_mod, "AgentConfig", lambda *a, **k: None)
    monkeypatch.setattr(gaia, "load_tasks", lambda *a, **k: [{"task_id": "t1"}])
    # This machine really does hold readable answers, and the run refuses to
    # start while it does. That gate has its own tests; these are about shutdown.
    monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [])
    monkeypatch.setattr(gaia.provenance, "snapshot", lambda **k: {})
    return made


def test_the_provider_is_shut_down_when_the_run_finishes(harness, monkeypatch):
    import asyncio

    async def _one(*a, **k):
        return _outcome()

    monkeypatch.setattr(gaia, "run_one", _one)
    assert asyncio.run(gaia.main_async(_args())) == 0

    (llm,) = harness
    assert llm.shutdown_calls == 1, "the server would outlive the run"


def test_the_provider_is_shut_down_when_a_task_raises(harness, monkeypatch):
    """A crashed run holds the card exactly as hard as a finished one."""
    import asyncio

    async def _boom(*a, **k):
        raise RuntimeError("model died mid-task")

    monkeypatch.setattr(gaia, "run_one", _boom)
    with pytest.raises(RuntimeError, match="model died mid-task"):
        asyncio.run(gaia.main_async(_args()))

    (llm,) = harness
    assert llm.shutdown_calls == 1


def test_a_failing_shutdown_does_not_lose_the_run(harness, monkeypatch, capsys):
    """The report is already written; a teardown error must not eat the exit code."""
    import asyncio

    async def _one(*a, **k):
        return _outcome()

    monkeypatch.setattr(gaia, "run_one", _one)

    class _BadLlm(_FakeLlm):
        async def shutdown(self):
            self.shutdown_calls += 1
            raise OSError("port already closed")

    import dpc_client_core.llm_manager as llm_mod
    made = []
    monkeypatch.setattr(llm_mod, "LLMManager",
                        lambda *a, **k: made.append(_BadLlm()) or made[-1])

    assert asyncio.run(gaia.main_async(_args())) == 0
    assert "provider shutdown failed" in capsys.readouterr().out


def test_the_agent_logs_survive_the_cleanup(harness, monkeypatch, tmp_path):
    """The ledger the run wrote is the evidence a night is supposed to leave."""
    import asyncio

    async def _one(*a, **k):
        return _outcome()

    monkeypatch.setattr(gaia, "run_one", _one)
    out = tmp_path / "run.json"
    assert asyncio.run(gaia.main_async(_args(json=str(out)))) == 0

    kept = tmp_path / "run.agent-logs" / "task-001" / "tools.jsonl"
    assert kept.is_file(), "the ledger went out with the workdir"
    assert '"phase": "attempt"' in kept.read_text(encoding="utf-8")


def test_the_cache_note_does_not_contradict_the_numbers_beside_it(harness, monkeypatch, tmp_path):
    """It claimed the local path reports nothing while all 53 tasks reported."""
    import asyncio
    import json as _json

    async def _one(*a, **k):
        row = _outcome()
        row["usage"]["prompt_cache_hit_tokens"] = 6_379_825
        return row

    monkeypatch.setattr(gaia, "run_one", _one)
    out = tmp_path / "run.json"
    asyncio.run(gaia.main_async(_args(json=str(out))))

    tokens = _json.loads(out.read_text(encoding="utf-8"))["tokens"]
    assert tokens["prompt_cache_hit"]["reported_by"] == 1
    assert "the local llama.cpp path does not" not in tokens["note"]
