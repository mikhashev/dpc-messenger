"""A seeded long-tier run renders its history the way production does, refuses
to start shallow, and writes none of the seed's text into its results.

`eval/loop/run_loop_eval.py --seed-history PATH` (with `seed_history.py`) puts
a conversation an agent had already loaded in front of each long-tier task —
the depth the first step 0 lacked. The seed is private group chat, so a seeded
report may carry its path, sha256 and count and nothing it says. Every seed
here is synthetic and every home is the test's own: no model, no network, the
real `~/.dpc` is never opened. The run-through tests skip without git (the
long tier snapshots this repository).
"""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import run_loop_eval as R  # noqa: E402
import seed_history as S  # noqa: E402
import tasks_long as T  # noqa: E402

ALIAS = "qwen3.8 27b"
NODE = "dpc-node-0123456789abcdef0123456789abcdef"
OTHER_NODE = "dpc-node-fedcba9876543210fedcba9876543210"
MARKER = "ZQX-SEED-MARKER-7731"


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
    from dpc_client_core.dpc_agent import loop as loop_module
    monkeypatch.setattr(loop_module, "load_agent_config", loop_module.load_agent_config)
    return h


def _record(i, sender, sender_type, content, owner=None):
    rec = {"id": f"m{i}", "msg_index": i, "timestamp": f"2026-10-05T10:{i:02d}:00.000000+00:00",
           "sender_name": sender, "sender_type": sender_type, "role": "user", "content": content}
    if owner:
        rec["agent_owner"] = owner
    return rec


def _write_seed(path: Path, records) -> Path:
    path.write_text(json.dumps({
        "reader": {"agent_id": "agent_johnny_test", "display_name": "Johnny", "node_id": NODE},
        "messages": records,
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _small_records():
    return [
        _record(1, "Mike", "human", f"first question {MARKER} from the operator"),
        _record(2, "Johnny", "agent", f"my own earlier answer {MARKER}", owner=NODE),
        _record(3, "Ark", "agent", "a colleague on the same node", owner=NODE),
        # Same name, another node: not the reader, so a user turn.
        _record(4, "Johnny", "agent", "a namesake on another node", owner=OTHER_NODE),
        _record(5, "Mike", "human", "@Johnny the trigger the task replaces"),
    ]


def _probe_world(tmp_path):
    from _harness import benchmark_tools
    (tmp_path / "rules").mkdir(exist_ok=True)
    fw = benchmark_tools.benchmark_firewall(tmp_path / "rules", R.LOOP_PROFILE,
                                            allowed=R._allowed_tools("long"))
    return fw


# -- rendering --------------------------------------------------------------------

def test_the_seed_is_rendered_by_production_before_the_task_with_the_readers_turns_as_assistant(
        home, tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent import agent as agent_module
    from dpc_client_core.dpc_agent import context as context_module

    seed = S.load_seed(_write_seed(tmp_path / "seed.json", _small_records()), "Johnny")
    built, derived = [], []
    real_build = agent_module.build_llm_messages
    real_derive = context_module.derive_history_role

    def spy_build(**kw):
        built.append(kw)
        return real_build(**kw)

    def spy_derive(rec, reader, is_group):
        derived.append((rec["msg_index"], dict(reader)))
        return real_derive(rec, reader, is_group)

    monkeypatch.setattr(agent_module, "build_llm_messages", spy_build)
    monkeypatch.setattr(context_module, "derive_history_role", spy_derive)
    R.harness_agent_configs({})

    task_prompt = "Read the code and answer: first_truncation_round_idx=<value>"
    m = asyncio.run(S.probe_round1(
        tmp_path / "depth-01", firewall=_probe_world(tmp_path), profile=R.LOOP_PROFILE,
        alias=ALIAS, task_prompt=task_prompt, seed=seed, conversation_id="eval-t",
        session_state={"tokens_limit": 215040}, reasoning_effort="medium"))

    assert len(built) == 1, "process must build the request through build_llm_messages"
    assert [r["msg_index"] for r in built[0]["conversation_history"]] == [1, 2, 3, 4]
    assert built[0]["reader_identity"] == seed["reader"]
    assert [i for i, _ in derived] == [1, 2, 3, 4], "every history record goes through the role rule"
    assert all(reader["display_name"] == "Johnny" for _, reader in derived)

    msgs = m["messages"]
    assert msgs[0]["role"] == "system"
    history = msgs[1:-1]
    assert [h["role"] for h in history] == ["user", "assistant", "user", "user"]
    for rec, h in zip(seed["messages"][:4], history):
        assert h["content"] == context_module.history_prefix(rec) + rec["content"]
    last = msgs[-1]
    assert last["role"] == "user"
    trigger = seed["messages"][-1]
    assert last["content"].startswith(
        context_module.history_prefix({**trigger, "content": task_prompt}) + task_prompt)
    assert "the trigger the task replaces" not in json.dumps(msgs, ensure_ascii=False)
    assert (m["history_turns"], m["assistant_turns"]) == (4, 1)


def test_a_seed_without_the_readers_identity_is_refused(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"messages": _small_records()}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        S.load_seed(path, "Johnny")
    assert "no identity for reader 'Johnny'" in str(exc.value)


# -- the flag ---------------------------------------------------------------------

def test_seed_history_is_refused_on_the_easy_and_hard_tiers(tmp_path):
    seed = str(tmp_path / "seed.json")
    for argv in (["--tier", "easy"], ["--tier", "hard"], []):
        with pytest.raises(SystemExit):
            R.parse_args([*argv, "--seed-history", seed])
    args = R.parse_args(["--step0-only", "--seed-history", seed])
    assert (args.tier, args.seed_history, args.seed_reader) == ("long", seed, "Johnny")
    assert args.seed_depth_floor == 60_000
    assert R.parse_args(["--tier", "long"]).seed_history is None


# -- depth before any model --------------------------------------------------------

def _depth_args(floor=60_000):
    return Namespace(seed_depth_floor=floor, tasks=1, compaction="production")


def _entry():
    return {"alias": ALIAS, "type": "llamacpp_server", "context_window": 215040}


def test_the_dry_run_depth_estimate_refuses_below_the_floor_and_passes_above_it(home, tmp_path):
    configs = {}
    R.harness_agent_configs(configs)
    fw = _probe_world(tmp_path)

    shallow = S.load_seed(_write_seed(tmp_path / "shallow.json", _small_records()), "Johnny")
    with pytest.raises(SystemExit) as exc:
        asyncio.run(R.seed_depth_or_refuse(shallow, _depth_args(), _entry(), "medium",
                                           tmp_path / "w1", fw, configs))
    assert "BELOW THE STEP-0 FLOOR" in str(exc.value)

    # ~280 000 characters of history: ~70 000 tokens on the chars/4 estimator.
    deep_records = [_record(i, "Mike" if i % 2 else "Ark", "human" if i % 2 else "agent",
                            ("word " * 1400).strip(), owner=None if i % 2 else NODE)
                    for i in range(1, 41)] + [_record(41, "Mike", "human", "@Johnny go")]
    deep = S.load_seed(_write_seed(tmp_path / "deep.json", deep_records), "Johnny")
    info = asyncio.run(R.seed_depth_or_refuse(deep, _depth_args(), _entry(), "medium",
                                              tmp_path / "w2", fw, configs))
    row = info["depth_by_task"][0]
    assert row["round1_tokens_est"] >= 60_000 and row["below_floor"] is False
    assert row["history_turns"] == 40
    assert row["round1_tokens_est"] == row["prompt_tokens_est"] + row["tool_schema_tokens_est"]
    assert (info["sha256"], info["messages"], info["depth_floor"]) == (deep["sha256"], 41, 60_000)

    # The same shallow seed passes when the floor is lowered on purpose.
    low = asyncio.run(R.seed_depth_or_refuse(shallow, _depth_args(floor=100), _entry(), "medium",
                                             tmp_path / "w3", fw, configs))
    assert low["depth_by_task"][0]["below_floor"] is False
    assert not (home / ".dpc" / "agents" / "depth-01").exists(), "probe roots stay out of the home"


# -- a seeded run through main_async: provenance and privacy -----------------------

class _FakeProvider:
    model = "qwen3.8 27b Mythos"
    preserve_reasoning = None


class _FakeLLM:
    def __init__(self, config_path):
        doc = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.providers = {doc["default_provider"]: _FakeProvider()}

    def get_context_window(self, model):
        return 215040

    async def query(self, *a, **k):
        return "summary"

    async def shutdown(self):
        pass


class _EchoAdapter:
    async def chat(self, messages, **kw):
        # The notes open with the chat, as a model retelling its history would.
        return ({"content": "", "thinking": f"recalling {MARKER} from the history"},
                {"reasoning_tokens": 10500, "completion_tokens": 10510, "prompt_tokens": 70000})


class _EchoAgent:
    seen = []

    def __init__(self, llm_manager, config, agent_root, firewall, firewall_profile, provider_alias):
        self.llm = _EchoAdapter()

    async def process(self, message, conversation_id, reasoning_effort=None, session_state=None,
                      **seeded):
        _EchoAgent.seen.append(seeded)
        for _ in range(3):
            await self.llm.chat([], tools=None)
        history = seeded["conversation_monitor"].get_message_history()
        quoted = " ".join(r["content"] for r in history)
        return (f"As discussed: {quoted}\nRoundLimitGuard, ToolLimitGuard\n"
                "max_rounds_default=200\ntools_per_turn=25")


@needs_git
def test_a_seeded_report_carries_path_sha256_and_count_and_no_seed_text(home, tmp_path,
                                                                        monkeypatch):
    import dpc_client_core.dpc_agent.agent as AG
    import dpc_client_core.llm_manager as LM

    async def probe(root, **kw):
        return {"history_turns": 4, "assistant_turns": 1, "history_tokens_est": 50,
                "prompt_tokens_est": 70_000, "tool_schema_tokens_est": 1_000,
                "round1_tokens_est": 71_000, "messages": [{"content": MARKER}]}

    _EchoAgent.seen = []
    monkeypatch.setattr(LM, "LLMManager", _FakeLLM)
    monkeypatch.setattr(AG, "DpcAgent", _EchoAgent)
    monkeypatch.setattr(R, "_check_vram_or_refuse", lambda entry: None)
    monkeypatch.setattr(S, "probe_round1", probe)

    seed_path = _write_seed(tmp_path / "seed.json", _small_records())
    out = tmp_path / "out" / "report.json"
    args = R.parse_args(["--step0-only", "--tasks", "1", "--seed-history", str(seed_path),
                         "--json", str(out)])
    assert asyncio.run(R.main_async(args)) == 0

    kw = _EchoAgent.seen[0]
    assert kw["trigger_message_id"] == S.TASK_RECORD_ID
    assert kw["reader_identity"]["display_name"] == "Johnny"

    report = json.loads(out.read_text(encoding="utf-8"))
    prov = json.loads(out.with_suffix(".provenance.json").read_text(encoding="utf-8"))
    want = {"path": str(seed_path), "sha256": hashlib.sha256(seed_path.read_bytes()).hexdigest(),
            "messages": 5}
    for block in (report["seed_history"], prov["run_conditions"]["seed_history"]):
        assert {k: block[k] for k in want} == want
        assert block["depth_by_task"][0]["round1_tokens_est"] == 71_000

    row = report["results"][0]
    assert "answer" not in row and "answer_tail" not in row
    assert row["answer_fields"]["max_rounds_default"] == "200"
    assert row["per_round"][0]["note_opening"].startswith("sha256:")
    assert row["metrics"]["repeat_opening_share"] == pytest.approx(2 / 3, abs=1e-3)

    for f in out.parent.rglob("*"):
        if f.is_file():
            assert MARKER not in f.read_text(encoding="utf-8"), f"seed text reached {f.name}"
    results_dir = tmp_path / "eval-results"
    if results_dir.exists():
        for f in results_dir.rglob("*"):
            if f.is_file():
                assert MARKER not in f.read_text(encoding="utf-8", errors="replace")


def test_the_withholding_net_replaces_a_verbatim_quote_and_keeps_the_rest():
    seed = {"messages": [{"content": "x" * 10 + "a sentence long enough to be the chat itself" + "y" * 10}]}
    windows = S.seed_windows(seed)
    counter = [0]
    out = S.withhold_seed_text({"cmd": "echo 'a sentence long enough to be the chat itself'",
                                "ok": "grep -n round_idx loop.py", "n": 3}, windows, counter=counter)
    assert out["cmd"].startswith("[withheld:") and "chat itself" not in out["cmd"]
    assert out["ok"] == "grep -n round_idx loop.py" and out["n"] == 3
    assert counter == [1]


# -- the reworded compaction-ladder gold -------------------------------------------

@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    if not _git_works():
        pytest.skip("git archive of this repository unavailable")
    dest = tmp_path_factory.mktemp("seeded-long") / "snapshot"
    T.snapshot_source(REPO_ROOT, T.resolve_commit(REPO_ROOT, "HEAD"), dest)
    return dest


@needs_git
def test_the_reworded_compaction_ladder_gold_still_rederives_from_the_snapshot(snapshot, tmp_path):
    task = next(t for t in T.tasks_for(tmp_path / "r") if t["id"] == "long-compaction-ladder")
    assert "round_idx" in task["prompt"] and "first round number" not in task["prompt"]
    assert "first_truncation_round_idx=<value>" in task["prompt"]
    derived = task["derive"](snapshot)
    assert derived["_first_round_idx"] == 1
    assert derived["first_truncation_round_idx"] == task["gold"]["first_truncation_round_idx"] == 9

    # The derive rests on the loop handing its own round_idx to apply_compaction:
    # break that and the gold is reported unsupported, not silently kept.
    mutated = tmp_path / "mutated"
    shutil.copytree(snapshot, mutated)
    loop = mutated / T.PKG / "dpc_agent" / "loop.py"
    text = loop.read_text(encoding="utf-8")
    anchor = ("notify=lambda m: emit_progress(m, None, round_idx),\n"
              "                round_idx=round_idx,")
    assert text.count(anchor) == 1
    loop.write_text(text.replace(anchor, anchor.replace("round_idx=round_idx,",
                                                        "round_idx=round_idx - 1,")),
                    encoding="utf-8")
    bad = [r for r in T.verify_golds(mutated) if not r["ok"]]
    assert {(r["task"], r["key"]) for r in bad} == {("long-compaction-ladder", "*")}
