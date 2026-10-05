"""Per-round measurement for the loop eval, and the A/B verdict built on it.

Lifted from `dpc-client/core/tests/perf/run_reasoning_carry_ab.py`, the first
harness for the `preserve_reasoning` question, which is kept until this one has
run. Its metric code was sound; its setup was not — it ran on a real agent root
with no approver and no copy of the code it asked about, so both arms spent 40
rounds on refused shell calls and measured the approval gate. The counters are
the same here; the world they run in is `run_loop_eval.py`'s.

Nothing in this file loads a model. Every number is read from what the provider
returned for a round (`msg`, `usage`) or from the loop's own log records.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

OPENING_CHARS = 80

# Step 0: the off arm must show the incident's symptom before an A/B means
# anything. The incident (2026-10-05, per the board card): 7 rounds at the note
# budget, prompt 67 177 -> 93 603. A task counts as reproducing it with at least
# two budget hits on a prompt that reached the bottom of that range — two, not
# one, because one capped round is a single long thought, and the card's
# complaint is a run that keeps hitting the cap.
SYMPTOM_MIN_BUDGET_HITS = 2
SYMPTOM_MIN_PEAK_PROMPT = 60_000

# A capped round lands near the budget, not on it: the server stops the trace at
# a token boundary (the incident's capped rounds read 10 080-10 980 on 10 000).
BUDGET_HIT_FRACTION = 0.98


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class RoundRecorder:
    """One row per LLM call of the agent loop: what it said, thought, was billed."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def record(self, msg: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]],
               elapsed_s: float) -> None:
        msg = msg or {}
        usage = usage or {}
        thinking = str(msg.get("thinking") or "")
        self.rows.append({
            "round": len(self.rows) + 1,
            "content_chars": len(str(msg.get("content") or "").strip()),
            "tool_calls": len(msg.get("tool_calls") or []),
            "note_chars": len(thinking),
            "note_opening": _normalise(thinking)[:OPENING_CHARS],
            "reasoning_tokens": usage.get("reasoning_tokens"),
            # Counted by the engine or estimated by the provider; on the incident's
            # silent rows it was the estimate (`split=estimated`).
            "thinking_source": usage.get("thinking_source"),
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "elapsed_s": round(elapsed_s, 2),
        })

    def wrap(self, adapter) -> None:
        """Record every `adapter.chat` call the loop makes, on this instance only."""
        import time
        chat = adapter.chat

        async def recording_chat(messages, **kwargs):
            started = time.perf_counter()
            msg, usage = await chat(messages, **kwargs)
            self.record(msg, usage, time.perf_counter() - started)
            return msg, usage

        adapter.chat = recording_chat


class CompactionLog(logging.Handler):
    """The loop's own compaction records: the round each pass ran in, and failures.

    Read from `ADR-033 compaction: round=N` (a DEBUG record in
    `dpc_agent/context.py:apply_compaction`, the one place that names the round)
    rather than inferred from a prompt that shrank, which a short tool result can
    also cause.
    """

    LOGGER = "dpc_client_core.dpc_agent.context"

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.rounds: List[int] = []
        self.failures = 0

    def emit(self, record: logging.LogRecord) -> None:
        msg = str(record.msg)
        if msg.startswith("ADR-033 compaction: round=") and record.args:
            self.rounds.append(int(record.args[0]))
        elif msg.startswith("Compaction failed ("):
            self.failures += 1

    def __enter__(self) -> "CompactionLog":
        logger = logging.getLogger(self.LOGGER)
        self._saved_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        logger = logging.getLogger(self.LOGGER)
        logger.removeHandler(self)
        logger.setLevel(self._saved_level)


class QueryCounter:
    """Counts `LLMManager.query` calls — in a loop run, the compaction summariser's.

    Those calls do not go through the adapter's `chat`, so they are not rounds;
    their time is inside the task's wall time and is reported beside it.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seconds = 0.0

    def wrap(self, manager) -> None:
        import time
        query = manager.query

        async def counting_query(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await query(*args, **kwargs)
            finally:
                self.calls += 1
                self.seconds += time.perf_counter() - started

        manager.query = counting_query

    def take(self) -> Dict[str, Any]:
        out = {"summariser_calls": self.calls, "summariser_s": round(self.seconds, 1)}
        self.calls, self.seconds = 0, 0.0
        return out


def summarise_rounds(rows: List[Dict[str, Any]], budget: Optional[int]) -> Dict[str, Any]:
    """The counters the card asks for, from one task-run's rows.

    `budget` is the alias's `reasoning_budget_tokens`; without one, budget hits are
    None (not measured), never 0.
    """
    def notes(r):
        return r["reasoning_tokens"] if r["reasoning_tokens"] is not None else r["note_chars"] // 4

    if budget:
        hit_rounds = [r["round"] for r in rows
                      if (r["reasoning_tokens"] or r["completion_tokens"] or 0)
                      >= budget * BUDGET_HIT_FRACTION]
    else:
        hit_rounds = None
    silent = [r["round"] for r in rows if not r["content_chars"]]
    longest, run = 0, 0
    for r in rows:
        run = run + 1 if not r["content_chars"] else 0
        longest = max(longest, run)
    openings: Dict[str, int] = {}
    repeats = with_notes = 0
    for r in rows:
        if not r["note_opening"]:
            continue
        with_notes += 1
        if r["note_opening"] in openings:
            repeats += 1
        openings[r["note_opening"]] = openings.get(r["note_opening"], 0) + 1
    prompts = [r["prompt_tokens"] for r in rows if r["prompt_tokens"] is not None]
    return {
        "rounds": len(rows),
        "budget_hits": len(hit_rounds) if hit_rounds is not None else None,
        "budget_hit_rounds": hit_rounds,
        "silent_rounds": len(silent),
        "silent_round_list": silent,
        "longest_silent_streak": longest,
        "rounds_with_notes": with_notes,
        "repeat_opening_share": round(repeats / with_notes, 3) if with_notes else None,
        "note_tokens": sum(notes(r) for r in rows),
        # Which rows' note count is the engine's and which ours (chars / 4) or the
        # provider's estimate; a reader can then tell a count from a guess.
        "note_tokens_estimated_rows": sum(
            1 for r in rows if r["reasoning_tokens"] is None
            or str(r.get("thinking_source") or "").startswith("estimat")),
        "first_prompt_tokens": prompts[0] if prompts else None,
        "peak_prompt_tokens": max(prompts) if prompts else None,
    }


def task_symptom(m: Dict[str, Any]) -> Optional[bool]:
    """Did this task-run show the incident's symptom? None when it was not measurable."""
    if m.get("budget_hits") is None or m.get("peak_prompt_tokens") is None:
        return None
    return (m["budget_hits"] >= SYMPTOM_MIN_BUDGET_HITS
            and m["peak_prompt_tokens"] >= SYMPTOM_MIN_PEAK_PROMPT)


def step0(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The top line: did the off arm reproduce the incident?"""
    off = [r for r in results if r.get("arm") == "off" and r.get("metrics")]
    if not off:
        return {"reproduced": None, "why": "no off-arm run in this report — not measured"}
    hits = [r["metrics"]["budget_hits"] for r in off if r["metrics"]["budget_hits"] is not None]
    peaks = [r["metrics"]["peak_prompt_tokens"] for r in off
             if r["metrics"]["peak_prompt_tokens"] is not None]
    flags = [task_symptom(r["metrics"]) for r in off]
    if not hits or not peaks:
        return {"reproduced": None, "budget_hits": None, "peak_prompt": None,
                "why": "the provider reported no note or prompt counts — not measured"}
    yes = [r["id"] for r, f in zip(off, flags) if f]
    out = {"reproduced": bool(yes), "budget_hits": sum(hits), "peak_prompt": max(peaks),
           "tasks_reproducing": yes, "off_tasks": len(off)}
    if not yes:
        out["why"] = (
            f"no off-arm task reached >= {SYMPTOM_MIN_BUDGET_HITS} budget hits on a prompt "
            f">= {SYMPTOM_MIN_PEAK_PROMPT} tokens, so the off arm never entered the regime "
            "the flag is meant to change; the A/B below measured nothing about it")
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


# Each axis, and which direction is better when read alone. None: no direction —
# printed, not compared. Visible output is its own axis, beside the rest.
AXES = (
    ("success", "passed", +1),
    ("rounds_to_done", "rounds_to_completion", -1),
    ("rounds", "rounds", -1),
    ("budget_hits", "budget_hits", -1),
    ("silent_rounds", "silent_rounds", -1),
    ("silent_streak", "longest_silent_streak", -1),
    ("repeat_open", "repeat_opening_share", -1),
    ("note_tokens", "note_tokens", -1),
    ("peak_prompt", "peak_prompt_tokens", -1),
    ("compact_at", "compaction_round", None),
    ("compactions", "compactions", None),
    ("compact_fail", "compaction_failures", -1),
    ("wall_s", "wall_s", -1),
)


def _value(r: Dict[str, Any], key: str) -> Any:
    if key == "passed":
        return r.get("passed")
    if key == "wall_s":
        return r.get("seconds")
    return (r.get("metrics") or {}).get(key)


def verdict_lines(results: List[Dict[str, Any]]) -> List[str]:
    """Both sides of every trade, side by side, per task and in total. No ranking.

    A reviewer reads more silence as a warning, not a win: a sample of
    2026-10-05 had the flag-on arm silent in 33 of 41 rounds against 0 of 41
    (per the brief for this instrument; that sample's data was not re-read
    here). So the lines below never declare an arm better on budget hits when it
    went silent more or compacted earlier — they say so instead.
    """
    by_task: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in results:
        by_task.setdefault(r["id"], {})[r.get("arm", "?")] = r
    paired = {t: arms for t, arms in by_task.items() if "off" in arms and "on" in arms}
    if not paired:
        return ["no task ran under both arms — nothing to set side by side"]
    lines = []
    for t, arms in paired.items():
        off, on = arms["off"], arms["on"]
        lines.append(f"  {t}")
        for label, key, _ in AXES:
            lines.append(f"    {label:>14}  off={_fmt(_value(off, key)):>9}  on={_fmt(_value(on, key)):>9}")

    def total(arm: str, key: str):
        vals = [_value(a[arm], key) for a in paired.values()]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        if key == "passed":
            return sum(1 for v in vals if v)
        if key in ("peak_prompt_tokens",):
            return max(vals)
        return sum(vals)

    lines.append(f"  all {len(paired)} paired task(s)")
    for label, key, _ in AXES:
        if key in ("compaction_round", "repeat_opening_share", "longest_silent_streak",
                   "rounds_to_completion"):
            continue
        lines.append(f"    {label:>14}  off={_fmt(total('off', key)):>9}  on={_fmt(total('on', key)):>9}")
    # rounds to completion only over tasks both arms finished: a failed task has
    # no rounds-to-completion, and averaging over different sets compares nothing.
    both_done = [a for a in paired.values()
                 if _value(a["off"], "rounds_to_completion") is not None
                 and _value(a["on"], "rounds_to_completion") is not None]
    if both_done:
        o = sum(_value(a["off"], "rounds_to_completion") for a in both_done) / len(both_done)
        n = sum(_value(a["on"], "rounds_to_completion") for a in both_done) / len(both_done)
        lines.append(f"    {'rounds_to_done':>14}  off={o:9.1f}  on={n:9.1f}  "
                     f"(mean over the {len(both_done)} task(s) both arms finished)")
    else:
        lines.append(f"    {'rounds_to_done':>14}  no task finished in both arms — not comparable")

    warnings = []
    off_silent, on_silent = total("off", "silent_rounds"), total("on", "silent_rounds")
    off_rounds, on_rounds = total("off", "rounds"), total("on", "rounds")
    if None not in (off_silent, on_silent, off_rounds, on_rounds) and off_rounds and on_rounds:
        so, sn = off_silent / off_rounds, on_silent / on_rounds
        lines.append(f"    {'silent_share':>14}  off={so:9.3f}  on={sn:9.3f}  "
                     f"({off_silent}/{off_rounds} vs {on_silent}/{on_rounds})")
        if sn > so:
            warnings.append("the on arm went silent in a larger share of rounds — a warning, "
                            "not a win, whatever the budget hits say")
    c_off = [_value(a["off"], "compaction_round") for a in paired.values()]
    c_on = [_value(a["on"], "compaction_round") for a in paired.values()]
    earlier = sum(1 for o, n in zip(c_off, c_on)
                  if n is not None and (o is None or n < o))
    if earlier:
        warnings.append(f"the on arm compacted earlier (or only it compacted) in {earlier} task(s): "
                        "its later rounds ran on summaries the off arm still had verbatim")
    b_off, b_on = total("off", "budget_hits"), total("on", "budget_hits")
    if b_off is not None and b_on is not None and b_on < b_off and warnings:
        warnings.append("fewer budget hits in the on arm are not read as a result while the "
                        "warnings above stand")
    lines.extend(f"  WARNING: {w}" for w in warnings)
    return lines
