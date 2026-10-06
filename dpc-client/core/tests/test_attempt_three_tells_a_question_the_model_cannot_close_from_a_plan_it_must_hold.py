"""Attempt 3 of the long tier's step 0: an incident-shaped audit task, Ark's
control pair, the silent-AND-at-budget intersection, repeats reported as k of N,
and the effort a round actually ran on.

The golds are re-derived from a `git archive` of HEAD, as a run does. Every
answer and round below is synthetic; no model, no network, no real `~/.dpc`.
"""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import round_metrics as M  # noqa: E402
import run_loop_eval as R  # noqa: E402
import tasks_long as T  # noqa: E402

ALIAS = "qwen3.8 27b"
NEW = ("long-audit-claims", "long-control-unresolvable", "long-control-multistep")
BUDGET = 10000


def _git_works() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return out.returncode == 0


needs_git = pytest.mark.skipif(not _git_works(), reason="git archive of this repository unavailable")


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    if not _git_works():
        pytest.skip("git archive of this repository unavailable")
    dest = tmp_path_factory.mktemp("attempt3") / "snapshot"
    T.snapshot_source(REPO_ROOT, T.resolve_commit(REPO_ROOT, "HEAD"), dest)
    return dest


@pytest.fixture
def tasks(tmp_path):
    return {t["id"]: t for t in T.tasks_for(tmp_path / "root")}


def _mutate(src: Path, rel: str, old: str, new: str) -> None:
    p = src / T.PKG / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation anchor {old!r} is not unique in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


# -- golds -----------------------------------------------------------------------

@needs_git
def test_every_attempt_three_gold_rederives_from_the_head_snapshot(snapshot):
    rows = [r for r in T.verify_golds(snapshot) if r["task"] in NEW]
    keys = {(r["task"], r["key"]) for r in rows}
    assert not [r for r in rows if not r["ok"]], [r for r in rows if not r["ok"]]
    assert ("long-audit-claims", "claim_5_where") in keys
    assert ("long-control-unresolvable", "places") in keys
    assert ("long-control-multistep", "meta_module") in keys


@needs_git
def test_a_mutated_snapshot_fails_exactly_the_mutated_golds(snapshot, tmp_path):
    mutated = tmp_path / "mutated"
    shutil.copytree(snapshot, mutated)
    _mutate(mutated, "managers/agent_manager.py",
            '"""Re-read settings from firewall after UI save."""\n        pass',
            '"""Re-read settings from firewall after UI save."""\n        self._reload()')
    _mutate(mutated, "dpc_agent/tools/core.py",
            "filename = Path(path).name", "filename = Path(path).as_posix()")
    _mutate(mutated, "dpc_agent/indexing_pipeline.py", "hexdigest()[:16]", "hexdigest()[:20]")

    assert {(r["task"], r["key"]) for r in T.verify_golds(mutated) if not r["ok"]} == {
        ("long-audit-claims", "claim_2"),
        ("long-audit-claims", "claim_4"),
        ("long-control-multistep", "hash_hex_chars"),
    }


def test_the_refuted_claim_is_not_the_holds_answer_and_an_agreeing_audit_fails(tasks):
    t = tasks["long-audit-claims"]
    assert t["gold"]["claim_1"] == "fixed" != "holds"
    assert set(t["gold"].values()) == {"holds", "fixed", "partly"}
    wheres = [f"{k}={s[0][0]}:{s[0][1]}" for k, s in t["gold_where"].items()]
    agree = "\n".join([f"claim_{i}=holds" for i in range(1, 6)] + wheres)
    v = R.check(t, agree)
    assert not v["passed"]
    assert {w.split("=")[0] for w in v["why"]} == {"claim_1", "claim_3", "claim_5"}


def test_a_where_value_is_scored_by_file_and_span(tasks):
    t = tasks["long-audit-claims"]
    good = {k: str(v) for k, v in t["gold"].items()}
    good.update({k: f"{s[0][0]}:{s[0][1]}" for k, s in t["gold_where"].items()})

    def answer(**over):
        return "\n".join(f"{k}={over.get(k, v)}" for k, v in good.items())

    rel, lo, hi = t["gold_where"]["claim_2_where"][0]
    absolute = "C:\\tmp\\root\\src\\dpc_client_core\\" + rel.replace("/", "\\")
    assert R.check(t, answer())["passed"]
    assert R.check(t, answer(claim_2_where=f"{absolute}:{hi}"))["passed"]
    assert not R.check(t, answer(claim_2_where=f"{rel}:{hi + 1}"))["passed"]
    assert not R.check(t, answer(claim_2_where=f"dpc_agent/loop.py:{lo}"))["passed"]


# -- Ark's control pair --------------------------------------------------------------

def test_the_unresolvable_control_passes_cannot_settle_with_both_places(tasks):
    t = tasks["long-control-unresolvable"]
    (a,), (b, _) = t["gold_places"]["places"]
    place_a, place_b = f"{a[0]}:{a[1]}", f"{b[0]}:{b[2]}"

    settled_no = f"settleable=no\nvalue=none\nevidence_a={place_a}\nevidence_b={place_b}"
    either_order = f"settleable=no\nvalue=none\nevidence_a={place_b}\nevidence_b={place_a}"
    confident = f"settleable=yes\nvalue=172032\nevidence_a={place_a}\nevidence_b={place_b}"
    one_place = f"settleable=no\nvalue=none\nevidence_a={place_a}\nevidence_b={place_a}"

    assert R.check(t, settled_no)["passed"]
    assert R.check(t, either_order)["passed"]
    assert R.check(t, confident)["why"] == ["settleable=yes (want no)"]
    assert not R.check(t, one_place)["passed"]


def test_the_multistep_control_reads_module_paths_by_suffix(tasks):
    t = tasks["long-control-multistep"]
    fields = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    assert R.check(t, fields + "\nkey_module=dpc_client_core.dpc_agent.index_keys"
                              "\nmeta_module=C:\\x\\src\\dpc_client_core\\dpc_agent\\index_meta.py")["passed"]
    v = R.check(t, fields + "\nkey_module=dpc_agent/index_meta.py\nmeta_module=dpc_agent/index_meta.py")
    assert v["why"] == ["key_module=dpc_agent/index_meta.py (want dpc_agent/index_keys.py)"]


# -- the intersection axis -----------------------------------------------------------

def _rows(n, hits, silent=(), first_prompt=67000, peak=93603):
    rec = M.RoundRecorder()
    for i in range(1, n + 1):
        notes = 10500 if i in hits else 1500
        rec.record({"content": "" if i in silent else "text", "thinking": f"plan {i}"},
                   {"reasoning_tokens": notes, "completion_tokens": notes + 100,
                    "prompt_tokens": first_prompt + (peak - first_prompt) * i // n}, 1.0)
    return rec.rows


INCIDENT_HITS = {14, 18, 19, 20, 22, 24, 25}


def test_the_intersection_counts_only_rounds_both_silent_and_at_the_budget():
    incident = M.summarise_rounds(_rows(26, INCIDENT_HITS, silent=set(range(14, 27)) - {16}), BUDGET)
    assert incident["silent_budget_hits"] == M.INCIDENT_SILENT_BUDGET_HITS == 7
    assert incident["silent_budget_hit_rounds"] == sorted(INCIDENT_HITS)

    # Hits only on talking rounds, silence only on uncapped ones: the union is 8,
    # each axis alone is 4, the intersection is 0.
    apart = M.summarise_rounds(_rows(8, {1, 2, 3, 4}, silent={5, 6, 7, 8}), BUDGET)
    assert (apart["budget_hits"], apart["silent_rounds"], apart["silent_budget_hits"]) == (4, 4, 0)
    assert M.summarise_rounds(_rows(8, {1}, silent={1}), None)["silent_budget_hits"] is None


def _result(id_, metrics, arm="off", repeat=1, kind=None):
    m = dict(metrics, compaction_round=None, compactions=0, compaction_failures=0,
             rounds_to_completion=metrics["rounds"])
    return {"id": id_, "arm": arm, "repeat": repeat, "kind": kind, "passed": True,
            "timed_out": False, "seconds": 1.0, "metrics": m}


BURN = M.summarise_rounds(_rows(26, INCIDENT_HITS, silent=set(range(14, 27)) - {16}), BUDGET)
FLAT = M.summarise_rounds(_rows(5, set(), first_prompt=70000, peak=80000), BUDGET)


def test_step0_prints_the_intersection_beside_the_separate_axes():
    line = M.step0_line(M.step0([_result("a", BURN)]))
    assert "budget hits 7" in line
    assert "silent AND at budget: 7 [the incident: 7]" in line
    verdict = "\n".join(M.verdict_lines([_result("a", BURN), _result("a", FLAT, arm="on")]))
    assert "silent_and_hit  off=        7  on=        0" in verdict


# -- repeats -----------------------------------------------------------------------------

def test_step0_reports_reproduction_as_k_of_n_per_task():
    s0 = M.step0([_result("x", BURN, repeat=1, kind="incident"),
                  _result("x", FLAT, repeat=2, kind="incident"),
                  _result("x", BURN, repeat=3, kind="incident")])
    assert (s0["by_task"]["x"]["burned"], s0["by_task"]["x"]["runs"]) == (2, 3)
    assert s0["runs_reproducing"] == ["x r1", "x r3"]
    assert any("x [incident]: budget-burn reproduced 2/3" in line for line in M.step0_task_lines(s0))


@pytest.mark.parametrize("burning, code", [
    ({"long-control-unresolvable"}, "rival"),
    ({"long-control-multistep"}, "mechanism"),
    ({"long-audit-claims"}, "mechanism"),
    ({"long-control-unresolvable", "long-audit-claims"}, "both"),
    (set(), "not-reproduced"),
])
def test_the_outcome_says_which_hypothesis_the_burning_points_at(tasks, burning, code):
    rows = [_result(i, BURN if i in burning else FLAT, kind=tasks[i]["kind"]) for i in NEW]
    out = M.step0(rows)["outcome"]
    assert out["code"] == code
    if code == "not-reproduced":
        assert "residual gap" in out["text"] and "Johnny system prompt" in out["text"]


def test_the_step0_line_names_how_far_a_deep_seed_reaches_past_the_incident():
    s0 = M.step0([_result("a", FLAT)])
    deep = {"deepening": {"outside_incident_history": 73}}
    assert "approximation: 73 records beyond the incident's history" in M.step0_line(s0, deep)
    assert "approximation" not in M.step0_line(s0, {"deepening": None})


def test_repeats_and_task_ids_are_long_tier_selections():
    args = R.parse_args(["--step0-only", "--task-ids", ",".join(NEW), "--repeats", "3"])
    assert (args.tier, args.tasks, args.repeats) == ("long", None, 3)
    assert R.parse_args(["--step0-only"]).tasks == 2
    for bad in (["--repeats", "0", "--tier", "long"], ["--repeats", "2"], ["--task-ids", "x"]):
        with pytest.raises(SystemExit):
            R.parse_args(bad)
    with pytest.raises(SystemExit):
        R.select_long_tasks(T.tasks_for(Path("r")), R.parse_args(["--tier", "long",
                                                                   "--task-ids", "nope"]))


def test_the_run_time_estimate_is_tasks_times_repeats_times_arms():
    est = R.run_time_estimate(3, 3, 1, seeded=True, timeout_min=45.0)
    assert est == {"task_runs": 9, "seconds_per_run": 120, "expected_min": 18.0,
                   "ceiling_min": 405.0}


# -- through main_async, stubbed -------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".dpc").mkdir(parents=True)
    (h / ".dpc" / "providers.json").write_text(json.dumps({"providers": [{
        "alias": ALIAS, "type": "llamacpp_server", "model": "m",
        "gguf_path": str(h / "model.gguf"), "context_window": 215040,
        "reasoning_budget_tokens": BUDGET,
    }]}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("DPC_EVAL_RESULTS", str(tmp_path / "eval-results"))
    from dpc_client_core.dpc_agent import loop as loop_module
    monkeypatch.setattr(loop_module, "load_agent_config", loop_module.load_agent_config)
    return h


class _Provider:
    model = "m"
    preserve_reasoning = None


class _LLM:
    def __init__(self, config_path):
        doc = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.providers = {doc["default_provider"]: _Provider()}

    def get_context_window(self, model):
        return 215040

    async def query(self, *a, **k):
        return "summary"

    async def shutdown(self):
        pass


# Two silent rounds at the budget on a 70-80 k prompt, then an answer.
_ROUNDS = [("", 10500, 70000), ("", 10400, 80000), ("done", 300, 81000)]


class _Adapter:
    async def chat(self, messages, **kw):
        content, notes, prompt = _ROUNDS[kw.pop("_n")]
        return ({"content": content, "thinking": "the plan"},
                {"reasoning_tokens": notes, "completion_tokens": notes + 10, "prompt_tokens": prompt})


class _Agent:
    roots = []

    def __init__(self, llm_manager, config, agent_root, firewall, firewall_profile, provider_alias):
        self.llm = _Adapter()
        self.root = agent_root

    async def process(self, message, conversation_id, reasoning_effort=None, session_state=None):
        _Agent.roots.append((self.root.name,
                             (self.root / "src" / "dpc_client_core").is_dir()))
        for n in range(len(_ROUNDS)):
            await self.llm.chat([], _n=n)
        return "nothing"


@pytest.fixture
def stubbed(monkeypatch):
    import dpc_client_core.llm_manager as LM
    import dpc_client_core.dpc_agent.agent as AG
    _Agent.roots = []
    monkeypatch.setattr(LM, "LLMManager", _LLM)
    monkeypatch.setattr(AG, "DpcAgent", _Agent)
    monkeypatch.setattr(R, "_check_vram_or_refuse", lambda entry: None)


@needs_git
def test_repeats_run_in_fresh_roots_and_step0_reports_k_of_n(home, stubbed, tmp_path, capsys):
    out = tmp_path / "report.json"
    args = R.parse_args(["--step0-only", "--task-ids",
                         "long-control-multistep,long-control-unresolvable",
                         "--repeats", "2", "--json", str(out)])
    assert asyncio.run(R.main_async(args)) == 0
    report = json.loads(out.read_text(encoding="utf-8"))

    roots = [r for r, _ in _Agent.roots]
    assert roots == ["long-11-r1-off", "long-11-r2-off", "long-10-r1-off", "long-10-r2-off"]
    assert len(set(roots)) == 4 and all(has_src for _, has_src in _Agent.roots)
    assert [(r["id"], r["repeat"]) for r in report["results"]] == [
        ("long-control-multistep", 1), ("long-control-multistep", 2),
        ("long-control-unresolvable", 1), ("long-control-unresolvable", 2)]
    by_task = report["step0"]["by_task"]
    assert {k: (v["burned"], v["runs"], v["silent_budget_hits"]) for k, v in by_task.items()} == {
        "long-control-multistep": (2, 2, 4), "long-control-unresolvable": (2, 2, 4)}
    assert report["step0"]["outcome"]["code"] == "both"
    printed = capsys.readouterr().out
    assert "long-control-unresolvable [control-unresolvable]: budget-burn reproduced 2/2" in printed
    assert "silent AND at budget: 8 [the incident: 7]" in printed


@needs_git
def test_the_provenance_names_the_requested_and_the_served_effort(home, stubbed, tmp_path):
    out = tmp_path / "report.json"
    args = R.parse_args(["--step0-only", "--task-ids", "long-audit-claims", "--json", str(out)])
    assert asyncio.run(R.main_async(args)) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    prov = out.with_suffix(".provenance.json").read_text(encoding="utf-8")

    assert report["reasoning_effort_requested"] == R.LONG_EFFORT
    assert report["served_effort"] == [R.LONG_EFFORT]
    assert report["alias_reasoning_effort_default"] is None
    assert {row["served_effort"] for row in report["results"][0]["per_round"]} == {R.LONG_EFFORT}
    assert "reasoning_effort_sent" not in json.dumps(report) and "reasoning_effort_sent" not in prov
    assert '"served_effort"' in prov and '"reasoning_effort_requested"' in prov


def test_served_effort_takes_the_providers_word_first_and_none_without_a_channel():
    class Mute:
        model = "m"

        def reasoning_words_served(self):
            return []

    llm = type("L", (), {"providers": {ALIAS: _Provider(), "mute": Mute()}})()
    assert R.served_effort_for(llm, ALIAS, "medium", "low") == "low"
    assert R.served_effort_for(llm, ALIAS, "medium", None) == "medium"
    assert R.served_effort_for(llm, "mute", "medium", None) is None
