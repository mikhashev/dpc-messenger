"""The loop eval's run conditions reach the agent they are meant for, and
nothing reaches the operator's `~/.dpc`.

`eval/loop/run_loop_eval.py` copies one alias out of `~/.dpc/providers.json`,
sets `preserve_reasoning` per arm on the copy, and serves each throwaway root
its compaction config from the harness. Until 2026-10-06 the checks for that
lived in an author's scratch directory; `--rounds` was parsed and never read.
These tests run `main_async` with the model, the agent and the VRAM gate
replaced, under a temporary home — no model, no network, no GPU, and the real
`~/.dpc` is never opened.
"""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import run_loop_eval as R  # noqa: E402

ALIAS = "qwen3.8 27b"


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


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home of the test's own, holding an operator providers file with one alias."""
    h = tmp_path / "home"
    (h / ".dpc").mkdir(parents=True)
    (h / ".dpc" / "providers.json").write_text(json.dumps({"providers": [{
        "alias": ALIAS, "type": "llamacpp_server", "model": "qwen3.8 27b Mythos",
        "gguf_path": str(h / "model.gguf"), "context_window": 215040,
        "reasoning_budget_tokens": 10000,
    }]}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("DPC_EVAL_RESULTS", str(tmp_path / "eval-results"))
    # main_async rebinds the loop's load_agent_config for the process; put the
    # original back after the test.
    from dpc_client_core.dpc_agent import loop as loop_module
    monkeypatch.setattr(loop_module, "load_agent_config", loop_module.load_agent_config)
    return h


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# -- the arm flag ----------------------------------------------------------------

def test_the_arm_flag_lands_on_copies_and_never_on_the_operator_file(home):
    operator = home / ".dpc" / "providers.json"
    before = _digest(operator)

    entry = R._provider_entry_for_alias(ALIAS)
    arms = R.arm_entries(entry, ("off", "on"))

    assert arms["off"]["preserve_reasoning"] is False
    assert arms["on"]["preserve_reasoning"] is True
    assert "preserve_reasoning" not in entry, "the source copy itself must stay as read"
    assert _digest(operator) == before
    assert "preserve_reasoning" not in operator.read_text(encoding="utf-8")


class _FakeProvider:
    model = "qwen3.8 27b Mythos"
    preserve_reasoning = None


class _FakeLLM:
    instances = []

    def __init__(self, config_path):
        doc = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.config_path = Path(config_path)
        self.written_entry = doc["providers"][0]
        self.providers = {doc["default_provider"]: _FakeProvider()}
        self.shut = False
        _FakeLLM.instances.append(self)

    def get_context_window(self, model):
        return 215040

    async def query(self, *a, **k):
        return "summary"

    async def shutdown(self):
        self.shut = True


# Three rounds per task: two at the 10 000 budget on a 70-80 k prompt, one answer.
_ROUNDS = [("", 10500, 70000), ("", 10400, 80000), ("done", 300, 81000)]


class _FakeAdapter:
    def __init__(self):
        self.n = 0

    async def chat(self, messages, **kw):
        content, notes, prompt = _ROUNDS[self.n % len(_ROUNDS)]
        self.n += 1
        return ({"content": content, "thinking": "the same plan again"},
                {"reasoning_tokens": notes, "thinking_source": "engine", "completion_tokens": notes + 10,
                 "prompt_tokens": prompt})


class _FakeAgent:
    seen = []

    def __init__(self, llm_manager, config, agent_root, firewall, firewall_profile, provider_alias):
        self.llm = _FakeAdapter()
        self.llm_manager = llm_manager
        self.config = config
        self.root = agent_root
        self.profile = firewall_profile
        self.alias = provider_alias

    async def process(self, message, conversation_id, reasoning_effort=None, session_state=None):
        from dpc_client_core.dpc_agent import loop as loop_module
        prov = self.llm_manager.providers[ALIAS]
        _FakeAgent.seen.append({
            "root": self.root.name,
            "preserve_reasoning": prov.preserve_reasoning,
            "effort": reasoning_effort,
            "session_state": session_state,
            "max_rounds": self.config.max_rounds,
            "profile": self.profile,
            "alias": self.alias,
            "snapshot_in_root": (self.root / "src" / "dpc_client_core" / "dpc_agent"
                                 / "loop.py").is_file(),
            "served_config": loop_module.load_agent_config(self.root.name),
        })
        for _ in _ROUNDS:
            await self.llm.chat([], tools=None)
        return "nothing useful"


@pytest.fixture
def stubbed(monkeypatch):
    import dpc_client_core.llm_manager as LM
    import dpc_client_core.dpc_agent.agent as AG
    _FakeLLM.instances = []
    _FakeAgent.seen = []
    monkeypatch.setattr(LM, "LLMManager", _FakeLLM)
    monkeypatch.setattr(AG, "DpcAgent", _FakeAgent)
    monkeypatch.setattr(R, "_check_vram_or_refuse", lambda entry: None)


def _run(tmp_path, *argv):
    out = tmp_path / "report.json"
    args = R.parse_args([*argv, "--json", str(out)])
    rc = asyncio.run(R.main_async(args))
    return rc, json.loads(out.read_text(encoding="utf-8"))


def test_both_arms_reach_the_live_provider_and_the_operator_file_is_untouched(home, stubbed, tmp_path):
    operator = home / ".dpc" / "providers.json"
    before = _digest(operator)

    rc, report = _run(tmp_path, "--tier", "easy", "--tasks", "1", "--preserve-reasoning", "both")

    assert rc == 0
    assert [s["preserve_reasoning"] for s in _FakeAgent.seen] == [False, True]
    llm = _FakeLLM.instances[0]
    assert llm.written_entry["preserve_reasoning"] is False, "the first arm's copy is what gets loaded"
    assert llm.config_path.parent != home / ".dpc", "the live providers file is the run's own"
    assert llm.shut, "the run must stop the provider it started"
    assert _digest(operator) == before
    assert report["operator_providers_unchanged"] is True
    assert report["preserve_reasoning_by_arm"] == {"off": False, "on": True}
    assert [r["arm"] for r in report["results"]] == ["off", "on"]


# -- --rounds --------------------------------------------------------------------

def test_rounds_is_parsed_under_both_spellings():
    assert R.parse_args(["--rounds", "7"]).rounds == 7
    assert R.parse_args(["--max-rounds", "9"]).rounds == 9
    assert R.parse_args([]).rounds is None


def test_each_tier_keeps_its_own_round_limit_when_rounds_is_absent():
    from dpc_client_core.dpc_agent.agent import AgentConfig
    assert R.resolve_max_rounds("long", None) == R.LONG_MAX_ROUNDS == 60
    assert R.resolve_max_rounds("easy", None) == AgentConfig().max_rounds
    assert R.resolve_max_rounds("hard", None) == AgentConfig().max_rounds
    assert R.resolve_max_rounds("hard", 5) == 5
    with pytest.raises(SystemExit):
        R.resolve_max_rounds("easy", 0)


def test_rounds_reaches_the_agents_max_rounds(home, stubbed, tmp_path):
    rc, report = _run(tmp_path, "--tier", "easy", "--tasks", "1", "--rounds", "7")

    assert rc == 0
    assert [s["max_rounds"] for s in _FakeAgent.seen] == [7]
    assert report["max_rounds"] == 7


def test_without_rounds_the_easy_tier_runs_under_agentconfigs_own_limit(home, stubbed, tmp_path):
    from dpc_client_core.dpc_agent.agent import AgentConfig
    rc, report = _run(tmp_path, "--tier", "easy", "--tasks", "1")

    assert rc == 0
    assert [s["max_rounds"] for s in _FakeAgent.seen] == [AgentConfig().max_rounds]
    assert report["max_rounds"] == AgentConfig().max_rounds


# -- the long tier through main_async ---------------------------------------------

@needs_git
def test_a_long_run_serves_snapshot_arm_config_and_limits_to_each_root(home, stubbed, tmp_path):
    rc, report = _run(tmp_path, "--tier", "long", "--tasks", "1", "--preserve-reasoning", "both")

    assert rc == 0
    seen = _FakeAgent.seen
    assert [s["root"] for s in seen] == ["long-01-off", "long-01-on"]
    assert [s["preserve_reasoning"] for s in seen] == [False, True]
    for s in seen:
        assert s["snapshot_in_root"], "the snapshot copy must be inside the task root"
        assert s["max_rounds"] == R.LONG_MAX_ROUNDS
        assert s["effort"] == R.LONG_EFFORT
        assert s["session_state"] == {"tokens_limit": 215040}
        assert s["alias"] == ALIAS
        assert s["served_config"] == R.long_compaction_config(
            ALIAS, "production", R.LONG_COMPACTION_THRESHOLD)
    assert report["step0"]["reproduced"] is True
    assert report["step0"]["tasks_reproducing"] == ["long-guard-chain"]
    assert any("reproducing subset, 1 task(s): long-guard-chain" in line
               for line in report["verdict"])
    # No task root got a directory in the home. `default` is not the harness's:
    # building any ContextFirewall constructs `ToolRegistry()` with no root,
    # which calls `get_agent_root("default")` (registry.py:378) and makes it —
    # observed 2026-10-06, outside eval/loop, left as it is.
    agents = home / ".dpc" / "agents"
    made = {p.name for p in agents.iterdir()} if agents.exists() else set()
    assert made <= {"default"}, f"task roots leaked into the home: {sorted(made)}"


def test_step0_only_is_the_long_tiers_off_arm_on_two_tasks():
    args = R.parse_args(["--step0-only"])
    assert (args.tier, args.preserve_reasoning, args.tasks) == ("long", "off", 2)
    assert R.parse_args(["--step0-only", "--tasks", "1"]).tasks == 1
    with pytest.raises(SystemExit):
        R.parse_args(["--step0-only", "--preserve-reasoning", "both"])
    with pytest.raises(SystemExit):
        R.parse_args(["--step0-only", "--tier", "hard"])
    assert R.parse_args([]).tier == "easy"


@needs_git
def test_the_step0_preflight_runs_one_arm_and_prints_the_verdict(home, stubbed, tmp_path, capsys):
    rc, report = _run(tmp_path, "--step0-only", "--tasks", "1")

    assert rc == 0
    assert [s["root"] for s in _FakeAgent.seen] == ["long-01-off"]
    assert report["step0_only"] is True and report["arms"] == ["off"]
    assert "verdict" not in report
    out = capsys.readouterr().out
    assert "incident symptom reproduced in the off arm: yes" in out
    assert "step-0 preflight: the off arm reached the incident's regime" in out


# -- load_agent_config, timeouts, the window ----------------------------------------

def test_the_harness_config_bypass_creates_no_directory_under_home(home):
    from dpc_client_core.dpc_agent import loop as loop_module
    from dpc_client_core.dpc_agent import utils

    cfg = R.long_compaction_config(ALIAS, "production", R.LONG_COMPACTION_THRESHOLD)
    R.harness_agent_configs({"long-01-off": cfg})

    assert loop_module.load_agent_config("long-01-off") == cfg
    assert loop_module.load_agent_config("some-other-root") == {}
    assert not (home / ".dpc" / "agents").exists()

    # Control: the function the harness replaces does create the directory, so
    # the assertion above can fail.
    utils.load_agent_config("control-root")
    assert (home / ".dpc" / "agents" / "control-root").is_dir()


class _SlowAgent:
    async def process(self, **kw):
        await asyncio.sleep(5)
        return "late"


def test_a_task_cut_by_the_timeout_is_reported_as_timed_out_not_failed_by_the_agent():
    task = {"id": "t", "prompt": "p", "expect_in_answer": ["late"]}
    out = asyncio.run(R.run_one(_SlowAgent(), task, timeout_s=0.05))

    assert out["timed_out"] is True
    assert out["passed"] is False
    assert any(w.startswith("timeout after") for w in out["why"])


def test_the_dry_run_refuses_an_alias_without_a_context_window(tmp_path):
    entry = {"alias": ALIAS, "type": "llamacpp_server", "model": "m"}
    args = R.parse_args(["--dry-run", "--tier", "long"])
    with pytest.raises(SystemExit) as exc:
        R._dry_run(entry, args, R.arm_entries(entry, ("off",)), None)
    assert "4096" in str(exc.value)

    assert R.require_context_window({**entry, "context_window": 215040}) == 215040
    for bad in (None, 0, "", "x"):
        with pytest.raises(SystemExit):
            R.require_context_window({**entry, "context_window": bad})
