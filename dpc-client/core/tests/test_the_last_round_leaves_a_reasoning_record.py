"""The turn that ends a run is the one a reader opens, and it left no trace.

`reasoning.jsonl` was appended inside the tool-call branch, so a round that
called nothing wrote nothing — and the round that called nothing is the final
answer, including the blank one that started this. The record is now written
on that branch too, with an empty tool list and a flag saying whether an
answer came with it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.loop import _detect_reasoning_quality, _is_answerless
from dpc_client_core.dpc_agent.utils import append_jsonl, utc_now_iso


def _record(thinking: str, content: str, logs_dir: Path) -> dict:
    """What the loop writes on a round that called no tool."""
    quality = _detect_reasoning_quality(thinking, [])
    quality["ts"] = utc_now_iso()
    quality["round"] = 1
    quality["task_id"] = "chat-test"
    quality["answered"] = not _is_answerless(content)
    append_jsonl(logs_dir / "reasoning.jsonl", quality)
    return quality


class TestTheRecordOfALastRound:
    def test_a_blank_answer_is_recorded_as_unanswered(self, tmp_path):
        record = _record("thought about it at length" * 20, "[#74 | 06:42:57 | Johnny]", tmp_path)

        assert record["answered"] is False
        assert record["tools"] == []

    def test_a_real_answer_is_recorded_as_answered(self, tmp_path):
        record = _record("first I check the log", "Here is what the log says", tmp_path)

        assert record["answered"] is True

    def test_the_line_lands_in_the_ledger_a_reader_opens(self, tmp_path):
        _record("thinking", "an answer", tmp_path)

        written = (tmp_path / "reasoning.jsonl").read_text(encoding="utf-8").strip()
        assert json.loads(written)["task_id"] == "chat-test"
