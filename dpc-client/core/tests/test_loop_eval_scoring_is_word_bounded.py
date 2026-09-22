"""`THE-LOOP-HAS-NEVER-BEEN-SCORED-ON-A-TASK-IT-ACTUALLY-DOES` (backlog.md):
substring containment let an answer of `14` satisfy a gold of `4`. These
tests pin the word-boundary replacement in `eval/loop/run_loop_eval.py`
directly against fixed strings — no model, no network, no VRAM.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LOOP_DIR = REPO_ROOT / "eval" / "loop"
sys.path.insert(0, str(LOOP_DIR))

from run_loop_eval import check, tasks_for, build_fixture  # noqa: E402
import tasks_hard  # noqa: E402


def _task(id_, **fields):
    return {"id": id_, "prompt": "", **fields}


def test_a_longer_number_no_longer_satisfies_a_shorter_gold():
    task = _task("t", expect_in_answer=["4"])
    assert check(task, "There are 14 non-empty lines")["passed"] is False
    assert check(task, "4")["passed"] is True
    assert check(task, "The count is 4.")["passed"] is True


def test_nine_is_not_satisfied_by_nineteen():
    task = _task("t", expect_in_answer=["9"])
    assert check(task, "19")["passed"] is False
    assert check(task, "9")["passed"] is True


def test_reject_list_is_also_word_bounded():
    # A reject needle must not fire on a longer number that merely contains it.
    task = _task("t", reject_in_answer=["30", "60"])
    assert check(task, "It is neither 30 nor 60")["passed"] is False
    assert check(task, "1300")["passed"] is True  # contains "30" as a substring, not a token

    invent = _task("t", expect_in_answer=["45"], reject_in_answer=["30", "60"])
    assert check(invent, "45")["passed"] is True


def test_expect_ordered_still_finds_word_bounded_positions():
    task = _task("t", expect_ordered=["empty.log", "config.txt", "notes.md"])
    ok_answer = "empty.log, config.txt, notes.md"
    bad_order = "notes.md, config.txt, empty.log"
    assert check(task, ok_answer)["passed"] is True
    assert check(task, bad_order)["passed"] is False


def test_expect_file_keeps_exact_substring_semantics(tmp_path):
    target = tmp_path / "result.txt"
    target.write_text("acknowledged and more", encoding="utf-8")
    task = _task("t", expect_file={"path": str(target), "contains": "acknowledged"})
    assert check(task, "")["passed"] is True


def test_real_gold_answers_for_every_easy_task_still_pass(tmp_path):
    build_fixture(tmp_path)
    for task in tasks_for(tmp_path):
        gold = " ".join(task.get("expect_in_answer", []))
        if task["id"] == "write-a-file":
            Path(task["expect_file"]["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(task["expect_file"]["path"]).write_text("acknowledged", encoding="utf-8")
            gold = "done"
        result = check(task, gold)
        assert result["passed"], (task["id"], result["why"])


def test_real_gold_answers_for_every_hard_task_still_pass(tmp_path):
    tasks_hard.build_fixture(tmp_path)
    for task in tasks_hard.tasks_for(tmp_path):
        if "expect_file" in task:
            wf = task["expect_file"]
            Path(wf["path"]).parent.mkdir(parents=True, exist_ok=True)
            body = wf["contains"]
            for keep in wf.get("still_contains", []):
                body += "\n" + keep
            Path(wf["path"]).write_text(body, encoding="utf-8")
            gold = "done"
        elif "expect_ordered" in task:
            gold = ", ".join(task["expect_ordered"])
        else:
            gold = " ".join(task.get("expect_in_answer", []))
        result = check(task, gold)
        assert result["passed"], (task["id"], result["why"])
