"""Two changes to the long tier after attempt 3 (reviewers Ark and Zcode, 2026-10-06).

1. A `*_where` answer is also accepted inside the definition of any function the
   gold's own derivation names (Ark's rule): attempt 3 rejected claim 5 answered
   inside `forget_in_index`, the function whose call the derive tests for. Where
   an invariant is *written* (a docstring) is still not where it *runs*.
2. `long-control-burn`, a positive control for budget burn: no tools, no gold,
   never pass/fail, read on the burn axes only, with its own step-0 reading.

Every answer, round and module below is synthetic; no model, no network, no
real `~/.dpc`. The git tests re-derive spans from a `git archive` of HEAD.
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
import seed_history as S  # noqa: E402
import tasks_long as T  # noqa: E402

BUDGET = 10000
BURN = "long-control-burn"


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
    dest = tmp_path_factory.mktemp("relies-on") / "snapshot"
    T.snapshot_source(REPO_ROOT, T.resolve_commit(REPO_ROOT, "HEAD"), dest)
    return dest


@pytest.fixture
def tasks(tmp_path):
    return {t["id"]: t for t in T.tasks_for(tmp_path / "root")}


def _audit_answer(t, **where) -> str:
    """Every verdict right, every place inside its first accepted span, unless overridden."""
    lines = [f"{k}={v}" for k, v in t["gold"].items()]
    lines += [f"{k}={where.get(k, f'{s[0][0]}:{s[0][1]}')}" for k, s in t["gold_where"].items()]
    return "\n".join(lines)


# -- 1. Ark's rule -----------------------------------------------------------------

def test_claim_five_answered_inside_the_function_its_derivation_names_passes(tasks):
    t = tasks["long-audit-claims"]
    # Attempt 3, run 1: verdict partly (right), place inside forget_in_index.
    v = R.check(t, _audit_answer(t, claim_5_where="dpc_agent/indexing_pipeline.py:209"))
    assert v["passed"], v["why"]
    assert ["dpc_agent/indexing_pipeline.py", "forget_in_index"] in t["gold_relies_on"]["claim_5_where"]


def test_where_an_invariant_is_written_is_not_where_it_runs(tasks):
    t = tasks["long-audit-claims"]
    # Attempt 3, run 3: line 97 is a sentence of BM25Index's docstring stating the
    # invariant; the code that keeps it is `save` / `_rebuild`.
    v = R.check(t, _audit_answer(t, claim_1_where="dpc_agent/bm25_index.py:97"))
    assert not v["passed"]
    assert v["why"] == ["claim_1_where=dpc_agent/bm25_index.py:97 (outside every accepted span)"]


@needs_git
def test_the_snapshot_puts_line_97_in_the_class_docstring_and_209_in_forget_in_index(snapshot):
    import ast
    text = (snapshot / T.PKG / "dpc_agent" / "bm25_index.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(text))
               if isinstance(n, ast.ClassDef) and n.name == "BM25Index")
    doc = cls.body[0]
    assert isinstance(doc, ast.Expr) and doc.lineno <= 97 <= doc.end_lineno
    derived = T._derive_audit(snapshot)
    assert not R._in_spans("dpc_agent/bm25_index.py:97", derived["_where"]["claim_1_where"])
    assert R._in_spans("dpc_agent/indexing_pipeline.py:209", derived["_where"]["claim_5_where"])


def _synthetic_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    mod = src / T.PKG / "pkg"
    mod.mkdir(parents=True)
    (mod / "alpha.py").write_text(
        '"""A synthetic module."""\n'      # 1
        "\n"                                # 2
        "def caller():\n"                   # 3
        "    return helper()\n"             # 4
        "\n"                                # 5
        "\n"                                # 6
        "def helper():\n"                   # 7
        "    x = 1\n"                       # 8
        "    return x\n"                    # 9
        "\n"                                # 10
        "\n"                                # 11
        "class Box:\n"                      # 12
        "    def open(self):\n"             # 13
        "        return 2\n",               # 14
        encoding="utf-8")
    return src


def test_a_derive_that_names_a_function_makes_its_definition_acceptable_only_for_that_gold(tmp_path):
    src = _synthetic_src(tmp_path)
    explicit = {"a_where": [["pkg/alpha.py", 3, 4]], "b_where": [["pkg/alpha.py", 3, 4]]}
    spans = T.accepted_spans(src, explicit, {"a_where": [["pkg/alpha.py", "helper"],
                                                         ["pkg/alpha.py", "Box.open"]]})
    assert spans["a_where"] == [["pkg/alpha.py", 3, 4], ["pkg/alpha.py", 7, 9],
                                ["pkg/alpha.py", 13, 14]]
    assert spans["b_where"] == [["pkg/alpha.py", 3, 4]]

    task = {"expect_where": spans}
    assert R.check(task, "a_where=pkg/alpha.py:8\nb_where=pkg/alpha.py:4")["passed"]
    # The same line under the key whose derivation did not name `helper`.
    assert R.check(task, "a_where=pkg/alpha.py:8\nb_where=pkg/alpha.py:8")["why"] == [
        "b_where=pkg/alpha.py:8 (outside every accepted span)"]
    # Between two definitions is inside neither.
    assert not R.check(task, "a_where=pkg/alpha.py:11\nb_where=pkg/alpha.py:4")["passed"]


def test_the_unresolvable_control_accepts_the_function_that_builds_the_config_path(tasks):
    t = tasks["long-control-unresolvable"]
    a = t["gold_places"]["places"][0][0]
    assert ["dpc_agent/utils.py", "get_agent_config_path"] in t["gold_relies_on"]["evidence_b"]
    rel, lo, hi = next(s for s in t["gold_places"]["places"][1] if s[1] == 671)
    answer = f"settleable=no\nvalue=none\nevidence_a={a[0]}:{a[1]}\nevidence_b={rel}:{hi}"
    assert R.check(t, answer)["passed"]


def test_the_last_line_naming_every_guard_is_the_order_scored(tasks):
    t = tasks["long-guard-chain"]
    fields = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    notes = "I first saw ToolLimitGuard, then RoundLimitGuard further up."
    assert R.check(t, notes + "\n" + ", ".join(t["gold_order"]) + "\n" + fields)["passed"]
    swapped = ", ".join([t["gold_order"][1], t["gold_order"][0], *t["gold_order"][2:]])
    assert R.check(t, swapped + "\n" + fields)["why"] == ["wrong order"]


def test_a_seeded_row_keeps_place_and_path_values_for_a_later_rescore(tasks):
    t = tasks["long-audit-claims"]
    out = {"answer": "x", "answer_tail": "x"}
    S.redact_seeded_outcome(out, t, R._field_value, _audit_answer(t))
    assert out["answer_fields"]["claim_5_where"] == "dpc_agent/tools/core.py:435"
    assert "answer" not in out and "answer_tail" not in out


# -- 2. the burn control --------------------------------------------------------------

def test_the_burn_control_offers_no_tools_has_no_gold_and_is_never_scored(tasks):
    t = tasks[BURN]
    assert t["kind"] == M.KIND_BURN
    assert T.task_tools(t) == frozenset() and t["tools_needed"] == [] and t["files"] == []
    assert not T.is_scored(t)
    assert not [k for k in t if k.startswith(("gold", "expect", "derive"))]
    for answer in ("", "x_12=6664\nx_24=1164\nx_36=3768\nx_48=6572\ns=29", "nonsense"):
        assert R.check(t, answer) == {"passed": None, "why": []}
    assert all(T.is_scored(o) for i, o in tasks.items() if i != BURN)


def _rows(notes_per_round):
    rec = M.RoundRecorder()
    for i, n in enumerate(notes_per_round, 1):
        last = i == len(notes_per_round)
        rec.record({"content": "answer" if last else "", "thinking": f"step {i}"},
                   {"reasoning_tokens": n, "thinking_source": "engine", "completion_tokens": n + 200,
                    "prompt_tokens": 11000 + i}, 1.0)
    return rec.rows


def _burn_result(notes_per_round, repeat=1, budget=BUDGET):
    m = M.summarise_rounds(_rows(notes_per_round), budget)
    m.update(compaction_round=None, compactions=0, compaction_failures=0, rounds_to_completion=None)
    return {"id": BURN, "arm": "off", "repeat": repeat, "kind": M.KIND_BURN, "passed": None,
            "timed_out": False, "seconds": 1.0, "metrics": m}


def test_step0_reports_burn_yes_when_a_round_reaches_the_budget_and_no_at_half():
    yes = M.step0([_burn_result([9900])])
    assert yes["burn_control"]["produced"] is True
    assert M.burn_line(yes["burn_control"]).startswith(
        "burn produced: yes (max reasoning tokens 9900 of budget 10000")
    no = M.step0([_burn_result([5000]), _burn_result([3000, 4000], repeat=2)])
    assert no["burn_control"]["produced"] is False
    assert no["burn_control"]["runs"] == 2
    assert M.burn_line(no["burn_control"]).startswith(
        "burn produced: no (max reasoning tokens 5000 of budget 10000")
    # Never the incident symptom, and never pass/fail.
    assert yes["reproduced"] is None and "only the burn control ran" in yes["why"]
    assert any("burn produced: yes" in line for line in M.step0_task_lines(yes))
    # Without a budget it is not measured, not "no".
    assert M.step0([_burn_result([9900], budget=None)])["burn_control"]["produced"] is None


def test_the_step0_outcome_for_the_burn_control_gives_the_agreed_readings():
    burns = M.step0([_burn_result([9950])])["outcome"]
    assert burns["code"] == "burn-produced"
    assert burns["text"] == ("the instrument can produce burning; the open question is which "
                             "task shape makes the model think instead of read")
    flat = M.step0([_burn_result([4000])])["outcome"]
    assert flat["code"] == "burn-absent"
    assert flat["text"].startswith(
        "the instrument does not produce burning at all on this setup; further attempts measure "
        "nothing — record the card as 0 of N, not reproduced, flag stays off")
    # Beside other tasks: the burn reading first, theirs appended, the burn row
    # kept out of the incident tally.
    other = dict(_burn_result([1000]), id="long-audit-claims", kind="incident", passed=False)
    both = M.step0([_burn_result([4000]), other])
    assert both["outcome"]["code"] == "burn-absent" and both["outcome"]["tasks_code"] == "not-reproduced"
    assert list(both["by_task"]) == ["long-audit-claims"]


def test_an_unscored_row_is_absent_from_the_success_axis_not_failed():
    off, on = _burn_result([9900]), dict(_burn_result([100]), arm="on")
    assert M._value(off, "passed") is None
    assert "success  off=        -  on=        -" in "\n".join(M.verdict_lines([off, on]))


def test_the_burn_control_estimate_is_budget_plus_answer_at_the_decode_speeds():
    est = R.burn_time_estimate(3, BUDGET)
    assert est["tokens_per_run"] == BUDGET + R.BURN_ANSWER_TOKENS
    assert est["seconds_per_run"] == [153, 230]
    assert est["expected_min"] == [7.7, 11.5]
    assert R.burn_time_estimate(3, None) is None


# -- the burn control through production and through the runner -----------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".dpc").mkdir(parents=True)
    (h / ".dpc" / "providers.json").write_text(json.dumps({"providers": [{
        "alias": "qwen3.8 27b", "type": "llamacpp_server", "model": "m",
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


def test_the_burn_control_runs_one_model_turn_with_no_tool_offered_or_callable(home, tmp_path,
                                                                              monkeypatch):
    from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
    from dpc_client_core.dpc_agent.retrieval import factory

    monkeypatch.setattr(factory, "_derive_embedding_metadata", S._known_width)  # no bge-m3
    R.harness_agent_configs({})
    task = next(t for t in T.tasks_for(tmp_path / "root") if t["id"] == BURN)
    shared = R.benchmark_tools.benchmark_firewall(tmp_path, R.LOOP_PROFILE,
                                                  allowed=T.LONG_TIER_TOOLS)
    fw = R.task_firewall(task, tmp_path, shared, {})
    assert fw is not shared
    assert json.loads((tmp_path / "privacy_rules.json").read_text(encoding="utf-8")) \
        ["agent_profiles"][R.LOOP_PROFILE]["tools"]["read_file"] is True

    agent = DpcAgent(llm_manager=S._NoModel(), config=AgentConfig(max_rounds=5),
                     agent_root=tmp_path / "burn-root", firewall=fw,
                     firewall_profile=R.LOOP_PROFILE, provider_alias="qwen3.8 27b")
    calls = []

    async def chat(messages, **kwargs):
        calls.append(kwargs.get("tools"))
        return {"content": "x_48=1"}, {"reasoning_tokens": 9900, "completion_tokens": 9910}

    agent.llm.chat = chat
    answer = asyncio.run(agent.process(message=task["prompt"], conversation_id="eval-burn"))
    assert answer and len(calls) == 1
    assert not calls[0], f"tools offered to the burn control: {calls[0]}"
    assert "not in the allowed tools list" in agent.tools.execute("read_file", {"path": "x"})


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


class _Agent:
    seen = []

    def __init__(self, llm_manager, config, agent_root, firewall, firewall_profile, provider_alias):
        self.llm = self
        self.firewall = firewall

    async def chat(self, messages, **kw):
        return {"content": "x_48=1"}, {"reasoning_tokens": kw["notes"], "thinking_source": "engine",
                                       "completion_tokens": kw["notes"] + 10,
                                       "prompt_tokens": 11000}

    async def process(self, message, conversation_id, reasoning_effort=None, session_state=None):
        tools = self.firewall.get_agent_tools_map(R.LOOP_PROFILE) or {}
        _Agent.seen.append(sorted(k for k, v in tools.items() if v))
        await self.llm.chat([], notes=9900 if len(_Agent.seen) == 2 else 2000)
        return "x_48=1"


@needs_git
def test_a_burn_only_step0_is_unscored_and_prints_the_burn_reading(home, tmp_path, monkeypatch,
                                                                   capsys):
    import dpc_client_core.llm_manager as LM
    import dpc_client_core.dpc_agent.agent as AG
    _Agent.seen = []
    monkeypatch.setattr(LM, "LLMManager", _LLM)
    monkeypatch.setattr(AG, "DpcAgent", _Agent)
    monkeypatch.setattr(R, "_check_vram_or_refuse", lambda entry: None)
    out = tmp_path / "report.json"
    args = R.parse_args(["--step0-only", "--task-ids", BURN, "--repeats", "3",
                         "--json", str(out)])
    assert asyncio.run(R.main_async(args)) == 0
    report = json.loads(out.read_text(encoding="utf-8"))

    assert _Agent.seen == [[], [], []]  # no tool on in any of the three roots
    assert [r["passed"] for r in report["results"]] == [None, None, None]
    assert (report["scored_tasks"], report["unscored_tasks"], report["accuracy"]) == (0, 3, None)
    burn = report["step0"]["burn_control"]
    assert (burn["produced"], burn["runs_burned"], burn["max_reasoning_tokens"]) == (True, 1, 9900)
    printed = capsys.readouterr().out
    assert "burn produced: yes (max reasoning tokens 9900 of budget 10000" in printed
    assert "step-0 preflight, burn control: the instrument can produce burning" in printed
    assert "FAIL" not in printed and "pass " not in printed
