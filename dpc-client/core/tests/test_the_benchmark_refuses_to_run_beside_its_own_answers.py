"""A score is not worth reading while the answers are readable on the machine.

Measured 2026-08-28, the first night the tool trace survived: the agent wrote a
script into its own sandbox and ran it, and the script read the gold parquet out
of the hub cache. Both steps are Tier 0 and legitimate — `write_file` stays
inside the sandbox, `python x.py` names a path inside it — so no gate was ever
asked. The absolute path lived inside the file, where a lexical classifier
cannot look.

So this is not a better gate. It is the run refusing to start while a copy of
the answers sits somewhere a process can open, and putting the copy it needs
itself out of reach before the first task.

A run now records a digest of each answer rather than the answer, so it stops
seeding the next run's answer key. The check stays because reports and logs
written before that still carry the answers in full.
"""
import json
import sys
import types
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402


class TestFindingTheAnswers:
    def test_the_dataset_in_a_hub_cache_is_found(self, tmp_path):
        cache = tmp_path / "hub"
        (cache / "datasets--gaia-benchmark--GAIA").mkdir(parents=True)
        (cache / "models--something-else").mkdir()

        assert gaia.reachable_gold([cache]) == [cache / "datasets--gaia-benchmark--GAIA"]

    def test_an_earlier_report_is_an_answer_key_too(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "run.json").write_text(
            json.dumps({"results": [{"task_id": "t1", "gold": "7", "answer": "7"}]}),
            encoding="utf-8",
        )
        (results / "campaign.json").write_text(
            json.dumps({"runs": [{"name": "t0", "accuracy": 0.5}]}), encoding="utf-8"
        )

        assert gaia.reachable_gold([], results) == [results / "run.json"], (
            "the campaign summary carries no gold and must not be flagged"
        )

    def test_an_earlier_run_log_is_an_answer_key_too(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "run.log").write_text(
            "[ 1/53] MISS   29.2s  gold='17'                 got='FINAL ANSWER: 17000'\n",
            encoding="utf-8",
        )
        (results / "quiet.log").write_text("53 task(s) from GAIA L1\n", encoding="utf-8")

        assert gaia.reachable_gold([], results) == [results / "run.log"], (
            "the progress line prints the gold, and a log is the bigger of the two files"
        )

    def test_a_clean_machine_finds_nothing(self, tmp_path):
        (tmp_path / "hub").mkdir()
        assert gaia.reachable_gold([tmp_path / "hub"], tmp_path / "nowhere") == []

    def test_the_default_cache_is_always_consulted(self, monkeypatch):
        for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
            monkeypatch.delenv(var, raising=False)

        assert gaia.hub_caches_in_effect()[-1] == Path.home() / ".cache" / "huggingface" / "hub"

    def test_an_operator_set_home_is_consulted_first(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HF_HOME", str(tmp_path / "elsewhere"))
        for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
            monkeypatch.delenv(var, raising=False)

        caches = gaia.hub_caches_in_effect()

        assert caches[0] == tmp_path / "elsewhere" / "hub"
        assert Path.home() / ".cache" / "huggingface" / "hub" in caches


class TestWhatARunIsAllowedToWriteDown:
    def test_the_report_records_a_digest_and_never_the_answer(self):
        outcome = _outcome()

        assert "gold" not in outcome, "an answer on disk is next run's answer key"
        assert gaia.gold_fingerprint(" 7 ") == gaia.gold_fingerprint("7")

    def test_the_digest_is_not_reversible_into_an_answer_key(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "run.json").write_text(
            json.dumps({"results": [{"task_id": "t1",
                                     "gold_sha256": gaia.gold_fingerprint("7"),
                                     "answer": "7"}]}),
            encoding="utf-8",
        )

        assert gaia.reachable_gold([], results) == [], (
            "a report of digests must not block the next run, because it gives nothing away"
        )


class TestTheBenchmarkAgentsOwnRules:
    def test_the_run_never_reads_the_operators_privacy_rules(self, tmp_path):
        firewall = gaia.benchmark_firewall(tmp_path)

        assert firewall.access_file_path == tmp_path / "privacy_rules.json"
        assert firewall.access_file_path.is_file()

    def test_every_tool_stays_on_so_the_firewall_changes_only_the_paths(self, tmp_path):
        from dpc_client_core.dpc_agent.tools.registry import ToolRegistry

        firewall = gaia.benchmark_firewall(tmp_path)
        tools = firewall.get_agent_tools_map(gaia.BENCH_PROFILE)

        assert set(tools) == set(ToolRegistry()._entries)
        off = sorted(name for name, on in tools.items() if not on)
        assert off == [], (
            "the runs this score is compared with had every tool; dropping the web "
            "or the shell measures the tooling instead of the loop"
        )

    def test_the_repository_is_outside_the_benchmark_sandbox(self, tmp_path):
        firewall = gaia.benchmark_firewall(tmp_path)
        repo = str(Path(__file__).resolve().parents[3])

        assert not firewall.is_extended_path_allowed(repo, profile_name=gaia.BENCH_PROFILE)
        assert not firewall.is_extended_path_allowed(
            str(Path.home() / ".cache" / "huggingface"), profile_name=gaia.BENCH_PROFILE
        )


class TestTheCanary:
    def test_the_decoy_lands_where_an_agent_would_look(self, tmp_path):
        cache = tmp_path / "hub"
        cache.mkdir()
        results = tmp_path / "results"
        results.mkdir()

        planted = gaia.plant_canary([cache, tmp_path / "gone"], results, "TOKEN-1")

        assert planted == [cache / gaia.CANARY_NAME, results / gaia.CANARY_NAME]
        body = json.loads(planted[0].read_text(encoding="utf-8"))
        assert body["canary"] == "TOKEN-1"

    def test_the_decoy_is_not_itself_an_answer_key(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()

        gaia.plant_canary([], results, "TOKEN-1")

        assert gaia.reachable_gold([], results) == [], (
            "the bait must not block the next run the way a real report does"
        )

    def test_a_token_in_an_answer_is_a_read(self, tmp_path):
        results = [{"task_id": "t1", "answer": "the key says TOKEN-1"},
                   {"task_id": "t2", "answer": "42"}]

        verdict = gaia.canary_was_read("TOKEN-1", results, tmp_path / "logs")

        assert verdict["triggered"] is True
        assert verdict["seen_in_answers"] == ["t1"]
        assert verdict["seen_in_trace"] == []

    def test_a_token_in_the_tool_ledger_is_a_read_the_answers_never_show(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "tools.jsonl").write_text(
            '{"tool": "read_file", "output": "TOKEN-1"}\n', encoding="utf-8"
        )

        verdict = gaia.canary_was_read("TOKEN-1", [{"task_id": "t1", "answer": "42"}], logs)

        assert verdict["triggered"] is True
        assert verdict["seen_in_trace"] == ["tools.jsonl"]

    def test_an_untouched_decoy_says_so(self, tmp_path):
        verdict = gaia.canary_was_read("TOKEN-1", [{"task_id": "t1", "answer": "42"}], tmp_path)

        assert verdict["triggered"] is False


class TestTakingTheRunsOwnCopyOutOfReach:
    def test_the_hub_is_redirected_into_the_run(self, monkeypatch, tmp_path):
        import os

        for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_DATASETS_CACHE"):
            monkeypatch.delenv(var, raising=False)

        private, previous = gaia.redirect_hub_into(tmp_path)

        assert private == tmp_path / "hf"
        assert (private / "hub").is_dir()
        assert os.environ["HF_HOME"] == str(private)
        assert os.environ["HF_HUB_CACHE"] == str(private / "hub")
        assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(private / "hub")
        assert os.environ["HF_DATASETS_CACHE"] == str(private / "datasets")
        assert previous == {var: None for var in gaia._HUB_VARS}

    def test_the_machines_own_cache_comes_back_once_the_dataset_is_gone(
        self, monkeypatch, tmp_path
    ):
        import os

        monkeypatch.setenv("HF_HOME", str(tmp_path / "mine"))
        for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_DATASETS_CACHE"):
            monkeypatch.delenv(var, raising=False)

        _, previous = gaia.redirect_hub_into(tmp_path)
        gaia.restore_hub(previous)

        assert os.environ["HF_HOME"] == str(tmp_path / "mine")
        assert "HF_HUB_CACHE" not in os.environ, (
            "left pointing at the run's own cache, the agent re-downloads its "
            "embedding model instead of using the one on the machine"
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


def _outcome(*a, **k):
    return {
        "task_id": "t1", "gold_sha256": "abc", "answer": "7", "correct": True,
        "error": None, "had_attachment": False, "seconds": 1.0,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                  "rounds": 1, "cost_usd": 0.0,
                  "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None},
    }


class _Llm:
    async def shutdown(self):
        pass


class _Agent:
    def __init__(self, *a, agent_root=None, **k):
        Path(agent_root).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def runnable(monkeypatch):
    """Everything but the gold check replaced, so the check is what is tested."""
    import dpc_client_core.llm_manager as llm_mod
    import dpc_client_core.dpc_agent.agent as agent_mod

    async def _one(*a, **k):
        return _outcome()

    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(llm_mod, "LLMManager", lambda *a, **k: _Llm())
    monkeypatch.setattr(agent_mod, "DpcAgent", _Agent)
    monkeypatch.setattr(agent_mod, "AgentConfig", lambda *a, **k: None)
    monkeypatch.setattr(gaia, "load_tasks", lambda *a, **k: [{"task_id": "t1"}])
    monkeypatch.setattr(gaia.provenance, "snapshot", lambda **k: {})
    monkeypatch.setattr(gaia, "run_one", _one)


class TestTheRefusal:
    def test_the_run_refuses_while_the_answers_are_readable(self, runnable, monkeypatch, tmp_path):
        import asyncio

        monkeypatch.setattr(
            gaia, "reachable_gold",
            lambda *a, **k: [tmp_path / "hub" / "datasets--gaia-benchmark--GAIA"],
        )

        with pytest.raises(SystemExit) as exc:
            asyncio.run(gaia.main_async(_args()))

        assert "readable from this machine" in str(exc.value)
        assert "datasets--gaia-benchmark--GAIA" in str(exc.value)

    def test_the_flag_runs_anyway_and_the_run_records_that_it_was_contaminable(
        self, runnable, monkeypatch, tmp_path, capsys
    ):
        import asyncio

        monkeypatch.setattr(gaia, "reachable_gold",
                            lambda *a, **k: [tmp_path / "somewhere" / "gold.json"])

        assert asyncio.run(gaia.main_async(_args(allow_reachable_gold=True))) == 0

        assert "contaminable" in capsys.readouterr().out
        assert gaia._DATASET_STATE["gold_reachable_allowed"] is True
        assert gaia._DATASET_STATE["gold_reachable_at_start"]

    def test_every_task_gets_a_root_of_its_own(self, runnable, monkeypatch):
        import asyncio
        import dpc_client_core.dpc_agent.agent as agent_mod

        roots = []

        class _Recording(_Agent):
            def __init__(self, *a, agent_root=None, **k):
                super().__init__(*a, agent_root=agent_root, **k)
                roots.append(Path(agent_root))

        monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [])
        monkeypatch.setattr(agent_mod, "DpcAgent", _Recording)
        monkeypatch.setattr(gaia, "load_tasks",
                            lambda *a, **k: [{"task_id": "t1"}, {"task_id": "t2"}])

        assert asyncio.run(gaia.main_async(_args(limit=2))) == 0

        assert len(roots) == 2 and roots[0] != roots[1], (
            "one root for two tasks is how task 2 inherits task 1's scratchpad"
        )

    def test_a_clean_machine_runs_and_the_private_cache_is_gone(
        self, runnable, monkeypatch, capsys
    ):
        import asyncio

        monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [])

        assert asyncio.run(gaia.main_async(_args())) == 0

        assert gaia._DATASET_STATE["private_cache_removed"] is True
        assert "hub cache removed: True" in capsys.readouterr().out
