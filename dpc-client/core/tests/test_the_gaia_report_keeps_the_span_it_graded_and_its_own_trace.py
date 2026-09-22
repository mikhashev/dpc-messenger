"""A GAIA report holds the answer it graded and the trace of the run that wrote it.

Found in review, 2026-09-23. The stored answer was its first 600 characters
while FINAL ANSWER sits at the end, so the graded span was missing from the
report and the canary and admission scans read that truncated copy.
`traces_carrying_gold` scanned the whole results tree before this run's ledger
was copied into it, so it named earlier nights and never this one.
`web_lookups` named every ledger "tools.jsonl" and flagged every attachment,
because an attachment is named after its task id.
"""

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402

TASK = "72e110e7-464c-453c-a309-90a95aed6538"


class _Agent:
    def __init__(self, text):
        self.text = text

    async def process(self, message, conversation_id):
        return self.text


class _SlowAgent:
    async def process(self, message, conversation_id):
        await asyncio.sleep(5)
        return "FINAL ANSWER: late"


def _row(gold="Guatemala"):
    return {"task_id": TASK, "Question": "q?", "Final answer": gold}


# --- the answer ---------------------------------------------------------------


def test_a_long_answer_keeps_the_span_it_was_graded_on():
    text = "reasoning " * 300 + "\nFINAL ANSWER: Guatemala"
    outcome = asyncio.run(gaia.run_one(_Agent(text), _row(), None))

    assert outcome["correct"] is True
    assert outcome["final_answer"] == "Guatemala"
    assert "FINAL ANSWER: Guatemala" in outcome["answer_tail"]
    assert "FINAL ANSWER" not in outcome["answer"], "the head alone is what used to be kept"
    assert outcome["answer_chars"] == len(text)
    assert outcome[gaia.FULL_ANSWER_KEY] == text


def test_a_short_answer_has_no_separate_tail():
    outcome = asyncio.run(gaia.run_one(_Agent("FINAL ANSWER: 7"), _row("7"), None))
    assert outcome["answer"] == "FINAL ANSWER: 7" and outcome["answer_tail"] == ""


def test_a_task_that_never_returns_is_recorded_as_a_timeout():
    outcome = asyncio.run(gaia.run_one(_SlowAgent(), _row(), None, timeout_seconds=0.05))

    assert outcome["correct"] is False
    assert outcome["error"].startswith("TimeoutError")


# --- the web ledger -------------------------------------------------------------


def _ledger(root, task_dir, text):
    d = root / task_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "tools.jsonl").write_text(text, encoding="utf-8")


def test_a_lookup_names_its_task_and_not_only_the_file_name(tmp_path):
    _ledger(tmp_path, "task-003", '{"args": {"query": "\\"%s\\" answer"}}\n' % TASK)

    hits = gaia.web_lookups(tmp_path, [TASK], task_of={"task-003": TASK})

    assert hits == [{"file": "task-003/tools.jsonl", "task": TASK,
                     "marker": TASK, "kind": "task_id_in_trace"}]


def test_the_tasks_own_attachment_is_not_a_lookup(tmp_path):
    _ledger(tmp_path, "task-001",
            '{"args": {"path": "C:\\\\t\\\\gaia-files\\\\%s.xlsx"}}\n' % TASK)

    assert gaia.web_lookups(tmp_path, [TASK]) == []


@pytest.mark.parametrize("url", [
    "https://huggingface.co/datasets/bstraehle/gaia/blob/main/x.jsonl",
    "https://huggingface.co/spaces/bstraehle/gaia-agent",
    "https://example.org/mirror/gaia_validation.jsonl",
])
def test_the_mirror_the_board_names_is_seen(tmp_path, url):
    _ledger(tmp_path, "task-001", '{"args": {"url": "%s"}}\n' % url)

    assert [h["kind"] for h in gaia.web_lookups(tmp_path, [])] == ["mirror"]


# --- the whole run ---------------------------------------------------------------


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
def isolated(monkeypatch, tmp_path):
    """The run with its dataset and model replaced, writing only under tmp_path."""
    import dpc_client_core.llm_manager as llm_mod
    import dpc_client_core.dpc_agent.agent as agent_mod

    ledger = {"text": '{"tool": "read_file"}\n'}

    class _FakeAgent:
        def __init__(self, *a, agent_root=None, **k):
            logs = Path(agent_root) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "tools.jsonl").write_text(ledger["text"], encoding="utf-8")

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(gaia, "RESULTS_DIR", results)
    monkeypatch.setattr(gaia, "hub_caches_in_effect", lambda: [])
    monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [])
    monkeypatch.setattr(gaia, "load_tasks", lambda *a, **k: [{"task_id": TASK}])
    monkeypatch.setattr(gaia.provenance, "snapshot", lambda **k: {})
    monkeypatch.setattr(llm_mod, "LLMManager", lambda *a, **k: _Llm())
    monkeypatch.setattr(agent_mod, "DpcAgent", _FakeAgent)
    monkeypatch.setattr(agent_mod, "AgentConfig", lambda *a, **k: None)
    return types.SimpleNamespace(results=results, ledger=ledger, out=tmp_path / "run.json")


def _outcome_with(full):
    async def _one(*a, **k):
        return {
            "task_id": TASK, "gold_sha256": "abc", "final_answer": "7",
            "answer": full[:600], "answer_tail": full[-600:], "answer_chars": len(full),
            gaia.FULL_ANSWER_KEY: full, "correct": True, "error": None,
            "had_attachment": False, "seconds": 1.0, "usage": {},
        }
    return _one


def test_the_canary_is_looked_for_in_the_whole_answer(isolated, monkeypatch):
    """The token past character 600 was invisible to a scan of the stored copy."""
    seen = {}

    # The token is minted inside the run, so the fake agent reads it from the bait.
    async def _one(agent, row, attachment, timeout_seconds=None):
        planted = json.loads((isolated.results / gaia.CANARY_NAME).read_text(encoding="utf-8"))
        seen["token"] = planted["canary"]
        full = "x" * 900 + " the key file says " + seen["token"] + "\nFINAL ANSWER: 7"
        return await _outcome_with(full)()

    monkeypatch.setattr(gaia, "run_one", _one)

    code = asyncio.run(gaia.main_async(_args(json=str(isolated.out))))

    report = json.loads(isolated.out.read_text(encoding="utf-8"))
    assert code == gaia.CONTAMINATED_EXIT
    assert report["canary"]["seen_in_answers"] == [TASK]
    assert seen["token"] not in report["results"][0]["answer"], "stored copy is truncated"
    assert gaia.FULL_ANSWER_KEY not in report["results"][0], "the full text stays in memory"


def test_traces_carrying_gold_sees_this_run_and_only_this_run(isolated, monkeypatch):
    old = isolated.results / "20260830-old.agent-logs" / "task-009"
    old.mkdir(parents=True)
    (old / "tools.jsonl").write_text('{"out": "Final answer: 3"}', encoding="utf-8")
    monkeypatch.setattr(gaia, "run_one", _outcome_with("FINAL ANSWER: 7"))

    asyncio.run(gaia.main_async(_args(json=str(isolated.out))))
    clean = json.loads(isolated.out.read_text(encoding="utf-8"))["traces_carrying_gold"]

    isolated.ledger["text"] = '{"out": "columns: task_id, Final answer"}\n'
    monkeypatch.setenv("HF_TOKEN", "test-token")   # the first run dropped it, by design
    asyncio.run(gaia.main_async(_args(json=str(isolated.out))))
    dirty = json.loads(isolated.out.read_text(encoding="utf-8"))["traces_carrying_gold"]

    assert clean == [], "an earlier night's ledger is not this run's"
    assert dirty == ["task-001/tools.jsonl"]


def test_a_missing_alias_stops_the_run_before_the_dataset_is_fetched(
        isolated, monkeypatch, tmp_path):
    fetched = []
    monkeypatch.setattr(gaia, "load_tasks", lambda *a, **k: fetched.append(1) or [])
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    (home / ".dpc" / "providers.json").write_text(
        json.dumps({"providers": [{"alias": "qwen3.8 27b", "type": "llamacpp_server"}]}),
        encoding="utf-8")
    monkeypatch.setattr(gaia.Path, "home", classmethod(lambda cls: home))

    with pytest.raises(SystemExit) as exc:
        asyncio.run(gaia.main_async(_args(provider_alias="qwen3.8 27b Mythos", model=None)))

    assert "qwen3.8 27b" in str(exc.value) and "Available" in str(exc.value)
    assert fetched == []


def test_the_report_names_the_model_by_file_not_by_label(isolated, monkeypatch):
    monkeypatch.setattr(gaia, "run_one", _outcome_with("FINAL ANSWER: 7"))
    monkeypatch.setattr(gaia.provenance, "model_files", lambda entry, cache=None: {
        "gguf": {"path": "C:/m/Qwen-IQ4_XS.gguf", "sha256": "53adc4bbed67044d" + "0" * 48}})

    asyncio.run(gaia.main_async(_args(json=str(isolated.out))))
    report = json.loads(isolated.out.read_text(encoding="utf-8"))

    assert report["model_file"] == "Qwen-IQ4_XS.gguf sha256:53adc4bbed67044d"
    assert "model" not in report, "a bare label is what was stale"


# --- the credential ---------------------------------------------------------------


class TestTheToken:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        for var in ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HF_HUB_DISABLE_IMPLICIT_TOKEN",
                    "HF_TOKEN_PATH", "PIP_REQUIRE_VIRTUALENV"):
            monkeypatch.delenv(var, raising=False)

    def test_the_environment_wins(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "from-env")
        assert gaia.resolve_hf_token() == ("from-env", "env:HF_TOKEN")

    def test_the_stored_login_is_used_when_nothing_is_exported(self, monkeypatch):
        import huggingface_hub
        monkeypatch.setattr(huggingface_hub, "get_token", lambda: "stored")
        assert gaia.resolve_hf_token() == ("stored", "huggingface_hub stored token")

    def test_no_token_anywhere_is_none(self, monkeypatch):
        import huggingface_hub
        monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
        assert gaia.resolve_hf_token() == (None, "none")

    def test_the_stored_token_is_fenced_off_from_what_the_agent_starts(self, tmp_path):
        """Dropping the env var meant little once the file on disk was a fallback."""
        before = gaia.fence_stored_token(tmp_path)

        assert os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
        assert not Path(os.environ["HF_TOKEN_PATH"]).exists()
        gaia.restore_env(before)
        assert "HF_TOKEN_PATH" not in os.environ

    def test_pip_refuses_an_interpreter_that_is_not_a_venv(self):
        before = gaia.fence_operator_interpreters()
        assert os.environ["PIP_REQUIRE_VIRTUALENV"] == "true"
        gaia.restore_env(before)
        assert "PIP_REQUIRE_VIRTUALENV" not in os.environ
