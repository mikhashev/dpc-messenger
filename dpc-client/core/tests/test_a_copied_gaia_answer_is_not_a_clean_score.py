"""A GAIA answer copied from a published answer key is reported, excluded and exits 3.

The run of 2026-09-23 (`20260923-0543`) scored 40/53 and 41/53 and exited 0,
while three and four of its correct answers had been read off pages that quote
the dataset (A-GAIA-AGENT-GOES-LOOKING-FOR-THE-ANSWER-KEY-AND-THE-RUN-COUNTS-
WHAT-IT-COPIES). The report's own detectors missed half of it
(A-MIRROR-PAGE-FULL-OF-GOLD…, 2026-09-23 note); one test per miss below. The
ledger rows are shaped like that night's, with no answer from the dataset in them.
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
import campaign  # noqa: E402

TASK = "72e110e7-464c-453c-a309-90a95aed6538"
OTHER = "a3fbeb63-0e8c-4a11-bff6-0e3b484c3e9c"
BLOCKED = "⚠️ BLOCKED_IN_BENCHMARK (fetch_json): answer-key source — x"


def _call(tool, args, preview, call_id="c1"):
    base = {"tool": tool, "tool_call_id": call_id, "task_id": "gaia-run-0123456789ab",
            "args": args, "round": 1}
    return (json.dumps({**base, "phase": "attempt"}) + "\n"
            + json.dumps({**base, "phase": "outcome", "result_preview": preview,
                          "is_error": preview.startswith("⚠️")}) + "\n")


def _ledger(root, task_dir, *rows):
    d = root / task_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "tools.jsonl").write_text("".join(rows), encoding="utf-8")
    return root


def _exposure(logs, correct=True, final="7", answer="FINAL ANSWER: 7", task=TASK):
    results = [{"task_id": task, "correct": correct, "final_answer": final}]
    return gaia.answer_key_exposure(logs, results, [{"task_id": task, "answer": answer,
                                                     "correct": correct}],
                                    task_of={"task-001": task})


def _kinds(got):
    return sorted({r["kind"] for t in got["answer_key_exposure"]["tasks"] for r in t["reasons"]})


# --- the mirror shapes the 2026-09-23 note lists as missed ------------------------


@pytest.mark.parametrize("url", [
    "https://huggingface.co/datasets/Kevin355/Who_and_When/raw/main/Who%26When/Hand-Crafted/33.json",
    "https://huggingface.co/spaces/olcapone/Final_Assignment/discussions/13/files",
    "https://github.com/example-org/harbor-index/blob/main/tasks/instruction.md",
    "https://hal.cs.princeton.edu/reliability/benchmark/gaia/analysis/",
    "https://raw.githubusercontent.com/patronus-ai/trail-benchmark/main/benchmarking/data/GAIA/4a8d.json",
])
def test_a_mirror_the_old_list_missed_is_seen(tmp_path, url):
    _ledger(tmp_path, "task-001", _call("fetch_json", {"url": url}, "{...}"))

    assert [h["kind"] for h in gaia.web_lookups(tmp_path, [])] == ["mirror"]


def test_the_datasets_own_discussion_page_exposes_the_task(tmp_path):
    """t0-xhigh/t0-low task-005: discussions/26 quotes the row."""
    url = "https://huggingface.co/datasets/gaia-benchmark/GAIA/discussions/26"
    _ledger(tmp_path, "task-001", _call("browse_page", {"url": url, "size": "m"}, "[page]"))

    got = _exposure(tmp_path)

    assert got["correct_clean"] == 0
    assert got["answer_key_exposure"]["copied"] == [TASK]
    assert got["answer_key_exposure"]["tasks"][0]["reasons"][0]["marker"] == url


def test_a_search_listing_that_shows_the_answer_exposes_the_task(tmp_path):
    """t0-low task-004: the discussions/11 snippet carried the answer itself."""
    listing = ("Search results for 'x' (2 found, backend=auto):\n\n"
               "  1. Ordinary page\n     https://example.org/a\n     nothing here\n\n"
               "  2. Question about task\n     https://huggingface.co/datasets/gaia-benchmark/"
               "GAIA/discussions/11\n     the answer is Zanzibar42 per the thread")
    _ledger(tmp_path, "task-001", _call("search_web", {"query": "bird species"}, listing))

    assert _kinds(_exposure(tmp_path, final="Zanzibar42")) == ["mirror_in_results"]


def test_a_search_listing_without_the_answer_is_not_exposure(tmp_path):
    """t0-xhigh 019/027, t0-low 046: Final_Assignment and Who_and_When pages quote
    GAIA questions and turn up in honest searches; their snippets did not carry
    the answer, and the audit counted those tasks clean."""
    listing = ("Search results for 'x' (1 found, backend=auto):\n\n"
               "  1. Space\n     https://huggingface.co/spaces/u/Final_Assignment/commit/ab\n"
               "     the question text only")
    _ledger(tmp_path, "task-001", _call("search_web", {"query": "bird species"}, listing))

    got = _exposure(tmp_path, final="Zanzibar42")

    assert got["answer_key_exposure"]["tasks"] == []
    assert got["correct_clean"] == 1


# --- the content markers ---------------------------------------------------------


@pytest.mark.parametrize("tool,preview,marker", [
    ("fetch_json", 'JSON object with 9 top-level keys [history, question, groundtruth, ...]',
     "groundtruth"),
    ("search_web", "Please provide your response in algebraic notation. Expected answer: …",
     "Expected answer"),
    ("browse_page", '{"Question": "...", \n    "Level": 1, \n    "Final answer": "…"}',
     '"Final answer"'),
])
def test_a_dataset_row_in_a_tool_result_exposes_the_task(tmp_path, tool, preview, marker):
    _ledger(tmp_path, "task-001", _call(tool, {"url": "https://example.org/p"}, preview))

    got = _exposure(tmp_path)

    reasons = got["answer_key_exposure"]["tasks"][0]["reasons"]
    assert {"kind": "gold_marker", "tool": tool, "marker": marker, "blocked": False} in reasons
    assert got["answer_key_exposure"]["copied"] == [TASK]


def test_the_tasks_own_prompt_words_are_not_a_marker(tmp_path):
    _ledger(tmp_path, "task-001",
            _call("browse_page", {"url": "https://example.org/p"},
                  "When you have determined the final answer, write FINAL ANSWER: x"))

    assert _exposure(tmp_path)["answer_key_exposure"]["tasks"] == []


# --- the answer that names its source --------------------------------------------


@pytest.mark.parametrize("sentence,phrase", [
    ("The official final answer is recorded there.", "official final answer"),
    ("This matches the official GAIA answer.", "official GAIA answer"),
    ("The golden answer in that file agrees.", "golden answer"),
    ("My result matches the dataset ground truth.", "dataset ground truth"),
    ("The analysis page lists the Expected answer for this task.", "Expected answer"),
])
def test_an_admission_the_old_list_missed_is_caught(sentence, phrase):
    found = gaia.answers_admitting_a_lookup(
        [{"task_id": "t", "answer": sentence + "\nFINAL ANSWER: 7", "correct": True}])

    assert [f["phrase"] for f in found] == [phrase]


def test_expected_answer_in_ordinary_prose_is_not_an_admission():
    assert gaia.answers_admitting_a_lookup(
        [{"task_id": "t", "answer": "The expected answer format is a number.\nFINAL ANSWER: 7",
          "correct": True}]) == []


# --- the query that names the benchmark ------------------------------------------


def test_a_query_naming_gaia_is_flagged(tmp_path):
    _ledger(tmp_path, "task-001", _call(
        "search_web", {"query": 'GAIA benchmark video question "highest number of bird species"'},
        "No results"))

    hits = gaia.web_lookups(tmp_path, [], task_of={"task-001": TASK})

    assert [(h["kind"], h["task"]) for h in hits] == [("gaia_query", TASK)]
    assert _kinds(_exposure(tmp_path)) == ["gaia_query"]


def test_an_ordinary_query_and_the_attachment_path_are_not(tmp_path):
    _ledger(tmp_path, "task-001",
            _call("search_web", {"query": "University of Leicester fish bag volume"}, "…"),
            _call("run_shell", {"command": r"type C:\t\dpc-gaia-23qubap5\task-001\gaia-files\x.txt"},
                  "ok", call_id="c2"))

    assert gaia.web_lookups(tmp_path, []) == []
    assert _exposure(tmp_path)["answer_key_exposure"]["tasks"] == []


# --- the false positive -----------------------------------------------------------


def test_the_tasks_attachment_in_a_powershell_error_is_not_a_lookup(tmp_path):
    """t0-low task-025: PowerShell cut the file name to `gaia-files/<id> ...`."""
    preview = ("[stderr]\nExpand-Archive : .pptx is not a supported archive file format.\n"
               "+ Expand-Archive -Path 'gaia-files/%s ...\n" % OTHER)
    _ledger(tmp_path, "task-001", _call("run_shell", {"command": "powershell -Command x"},
                                        preview))

    assert gaia.web_lookups(tmp_path, [OTHER]) == []


def test_a_task_id_in_a_query_is_still_a_lookup(tmp_path):
    _ledger(tmp_path, "task-001", _call("search_web", {"query": '"%s" answer' % OTHER}, "…"))

    assert [h["kind"] for h in gaia.web_lookups(tmp_path, [OTHER])] == ["task_id_in_trace"]


# --- refused versus reached ------------------------------------------------------


def test_a_refused_attempt_is_out_of_the_clean_score_but_is_not_a_copy(tmp_path):
    url = "https://huggingface.co/datasets/Kevin355/Who_and_When/raw/main/x.json"
    _ledger(tmp_path, "task-001", _call("fetch_json", {"url": url}, BLOCKED))

    got = _exposure(tmp_path)
    task = got["answer_key_exposure"]["tasks"][0]

    assert got["correct_clean"] == 0
    assert got["answer_key_exposure"]["exposed_correct"] == [TASK]
    assert task["reached"] is False and got["answer_key_exposure"]["copied"] == []
    assert not gaia.contaminated({"canary": {"triggered": False},
                                  "answer_key_exposure": got["answer_key_exposure"]})


def test_a_withheld_page_counts_as_refused(tmp_path):
    _ledger(tmp_path, "task-001", _call("browser_click", {"ref_or_selector": "@e3"},
                                        BLOCKED.replace("fetch_json", "browser_click")))

    task = _exposure(tmp_path)["answer_key_exposure"]["tasks"][0]

    assert [r["kind"] for r in task["reasons"]] == ["blocked_by_policy"]
    assert task["reached"] is False


def test_a_wrong_answer_can_be_exposed_without_contaminating(tmp_path):
    _ledger(tmp_path, "task-001", _call("fetch_json", {"url": "https://h/x"}, "groundtruth: …"))

    got = _exposure(tmp_path, correct=False)

    assert got["answer_key_exposure"]["exposed_correct"] == []
    assert not gaia.contaminated({"answer_key_exposure": got["answer_key_exposure"]})


def test_a_stored_report_is_rescored_by_task_directory(tmp_path):
    report = {"results": [
        {"task_id": OTHER, "correct": True, "final_answer": "7", "answer": "FINAL ANSWER: 7"},
        {"task_id": TASK, "correct": True, "final_answer": "9",
         "answer": "x" * 600, "answer_tail": "The official final answer is 9\nFINAL ANSWER: 9"},
    ]}
    _ledger(tmp_path, "task-001", _call("search_web", {"query": "fish"}, "…"))

    got = gaia.exposure_from_report(report, tmp_path)

    assert got["correct_clean"] == 1 and got["accuracy_clean"] == 0.5
    assert got["answer_key_exposure"]["copied"] == [TASK], "the admission sits in the tail"


# --- the whole run ------------------------------------------------------------------


def _args(**over):
    base = dict(limit=1, with_files=False, provider_alias=None, model="fake-model",
                base_url="http://127.0.0.1:1", context_window=4096, temperature=0.0,
                reasoning_effort="low", auto_approve=False, json=None, keep=False,
                allow_reachable_gold=False)
    base.update(over)
    return types.SimpleNamespace(**base)


class _Llm:
    async def shutdown(self):
        pass


class _Registry:
    def __init__(self):
        from dpc_client_core.dpc_agent.tools.registry import ToolEntry
        self._entries = {"search_web": ToolEntry(name="search_web", schema={},
                                                 handler=lambda ctx, query: "…")}

    def override_handler(self, name, handler):
        self._entries[name].handler = handler


@pytest.fixture
def run(monkeypatch, tmp_path):
    import dpc_client_core.llm_manager as llm_mod
    import dpc_client_core.dpc_agent.agent as agent_mod

    ledger = {"text": _call("read_file", {"path": "notes.txt"}, "ok")}
    agents = []

    class _FakeAgent:
        def __init__(self, *a, agent_root=None, **k):
            logs = Path(agent_root) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "tools.jsonl").write_text(ledger["text"], encoding="utf-8")
            self.tools = _Registry()
            agents.append(self)

    async def _one(*a, **k):
        return {"task_id": TASK, "gold_sha256": "abc", "final_answer": "7",
                "answer": "FINAL ANSWER: 7", "answer_tail": "", "answer_chars": 15,
                gaia.FULL_ANSWER_KEY: "FINAL ANSWER: 7", "correct": True, "error": None,
                "had_attachment": False, "seconds": 1.0, "usage": {}}

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(gaia, "RESULTS_DIR", results)
    monkeypatch.setattr(gaia, "hub_caches_in_effect", lambda: [])
    monkeypatch.setattr(gaia, "reachable_gold", lambda *a, **k: [])
    monkeypatch.setattr(gaia, "load_tasks", lambda *a, **k: [{"task_id": TASK}])
    monkeypatch.setattr(gaia.provenance, "snapshot", lambda **k: {})
    monkeypatch.setattr(gaia, "run_one", _one)
    monkeypatch.setattr(llm_mod, "LLMManager", lambda *a, **k: _Llm())
    monkeypatch.setattr(agent_mod, "DpcAgent", _FakeAgent)
    monkeypatch.setattr(agent_mod, "AgentConfig", lambda *a, **k: None)
    out = tmp_path / "run.json"

    def go():
        code = asyncio.run(gaia.main_async(_args(json=str(out))))
        return code, json.loads(out.read_text(encoding="utf-8"))
    return types.SimpleNamespace(go=go, ledger=ledger, agents=agents)


def test_a_clean_run_reports_both_numbers_and_exits_0(run):
    code, report = run.go()

    assert code == 0
    assert (report["correct"], report["correct_clean"], report["accuracy_clean"]) == (1, 1, 1.0)
    assert report["answer_key_exposure"]["tasks"] == []


def test_a_copied_answer_exits_3_with_the_task_named(run):
    """The exit the canary had to itself until 2026-09-23."""
    run.ledger["text"] = _call(
        "fetch_json",
        {"url": "https://huggingface.co/datasets/Kevin355/Who_and_When/raw/main/x/33.json"},
        'JSON object with 9 top-level keys [history, question, groundtruth, ...]')

    code, report = run.go()

    assert code == gaia.CONTAMINATED_EXIT
    assert report["canary"]["triggered"] is False
    assert report["accuracy"] == 1.0 and report["accuracy_clean"] == 0.0
    assert report["answer_key_exposure"]["copied"] == [TASK]


def test_the_run_installs_the_policy_on_each_agents_tools(run):
    code, report = run.go()

    assert getattr(run.agents[0].tools._entries["search_web"].handler,
                   "_answer_key_policy", None) is not None
    assert report["answer_key_policy"]["tools_wrapped_per_task"] == [1]
    assert report["answer_key_policy"]["refusals"] == 0


# --- the campaign ----------------------------------------------------------------


def test_the_campaign_says_why_a_run_is_contaminated_and_its_clean_score():
    record = {"exit_code": 3, **campaign.report_fields({
        "correct": 40, "tasks": 53, "accuracy": 0.755, "correct_clean": 37,
        "accuracy_clean": 0.698, "canary": {"triggered": False},
        "answer_key_exposure": {"copied": [TASK, OTHER], "exposed_tasks": 5},
    })}

    reason = campaign.contamination_reason(record)

    assert "2 correct answer(s) reached an answer key" in reason and TASK[:8] in reason
    assert "canary" not in reason
    assert campaign._clean(record) == "clean 37/53 = 0.698"
    assert campaign.campaign_exit([{**record, "minutes": 150}], False) == campaign.CONTAMINATED_EXIT
    assert not campaign.stops_the_queue({**record, "minutes": 1.0})


def test_a_report_from_before_the_policy_still_summarises():
    record = campaign.report_fields({"correct": 40, "tasks": 53, "accuracy": 0.755})

    assert campaign._clean(record) == "clean score not in the report"
    assert campaign.contamination_reason(record) == "the run exited 3 (report unread)"
