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

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

OPENING_CHARS = 80

# Step 0: the off arm must show the incident's symptom before an A/B means
# anything. The incident (2026-10-05, per the board card): 7 rounds at the note
# budget, prompt 67 177 -> 93 603. A task counts as reproducing it with at least
# two budget hits on a prompt that reached the bottom of that range — two, not
# one, because one capped round is a single long thought, and the card's
# complaint is a run that keeps hitting the cap.
SYMPTOM_MIN_BUDGET_HITS = 2
SYMPTOM_MIN_PEAK_PROMPT = 60_000
# ...and those hits must be a share of the deep rounds, not two capped thoughts
# lost in a long run: budget hits among the rounds whose prompt reached
# SYMPTOM_MIN_PEAK_PROMPT, over the count of those rounds. From the incident (per
# the card, not re-read here): 7 hits in 26 rounds. Read as the reviewers did —
# the 12 rounds from round 14 on were the ones past ~67 k — the share is
# 7/12 = 0.58; read as the card's range "67 177 -> 93 603" says — every round
# was already past 60 k — it is 7/26 = 0.27. 0.25 accepts the incident under
# both readings and rejects two hits in ten or more deep rounds (0.20).
SYMPTOM_MIN_BUDGET_SHARE = 0.25

# A capped round lands near the budget, not on it: the server stops the trace at
# a token boundary (the incident's capped rounds read 10 080-10 980 on 10 000).
BUDGET_HIT_FRACTION = 0.98

# Rounds that were both silent (no visible text) and at the note budget. Per the
# card's 2026-10-06 entry (relayed in the attempt-3 brief, not re-read here) the
# incident had 7: every one of its seven capped rounds was also a silent one.
# Reported beside the two separate axes, never folded into the symptom rule.
INCIDENT_SILENT_BUDGET_HITS = 7

# Task kinds of the long tier (`tasks_long.py`), read by `step0_outcome`. A task
# that carries none is a baseline: closable, not built for the attempt-3 split.
KIND_UNRESOLVABLE = "control-unresolvable"
# The positive control for budget burn: no tools, no gold, never pass/fail. It
# is kept out of the incident symptom and read on the burn axes alone.
KIND_BURN = "control-burn"


# Where a row's `reasoning_tokens` came from (its `thinking_source`), best first.
# The provider's own word for a guess is "estimated" (chars / 4 over the note):
# on the burn control it read ~3 000 for rounds that spent ~11 000 of a 12 050
# completion on digits, and the budget check believed it. A guess is never the
# count held against the budget; the provider's number is kept beside it only.
SOURCE_ENGINE = "engine"                    # counted by the engine (production's word)
SOURCE_TOKENIZER = "tokenizer"              # the note text, the model's own tokenizer
SOURCE_MINUS_VISIBLE = "completion_minus_visible"  # completion - (content + tool calls)
SOURCE_UPPER_BOUND = "completion_upper_bound"      # the whole completion: an upper bound
COUNTED_SOURCES = (SOURCE_ENGINE, SOURCE_TOKENIZER)
CORRECTED_SOURCES = COUNTED_SOURCES + (SOURCE_MINUS_VISIBLE, SOURCE_UPPER_BOUND)


def reasoning_count(*, reported: Any, reported_source: Any, completion: Any,
                    thinking: Optional[str] = None, visible: Optional[str] = None,
                    count_tokens: Optional[Callable[[str], int]] = None
                    ) -> Tuple[Optional[int], Optional[str]]:
    """`(count, source)` for one round's reasoning, never from chars / 4.

    An engine count is taken as is. Otherwise the note is counted with the
    model's tokenizer; with no note text, the completion less the visible output
    (content and tool calls) counted the same way; with no tokenizer, the whole
    completion, labelled as the upper bound it is.
    """
    if isinstance(reported, int) and reported_source == SOURCE_ENGINE:
        return reported, SOURCE_ENGINE
    if count_tokens is not None:
        try:
            if thinking:
                return int(count_tokens(thinking)), SOURCE_TOKENIZER
            if isinstance(completion, int):
                seen = int(count_tokens(visible)) if visible else 0
                return max(completion - seen, 0), SOURCE_MINUS_VISIBLE
        except Exception:  # a tokenizer that fails falls through to the bound
            pass
    if isinstance(completion, int):
        return completion, SOURCE_UPPER_BOUND
    return None, None


def correct_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a row recorded before the correction (no note text kept) in
    place: the provider's figure moves to `*_reported`, the count becomes the
    completion upper bound unless the engine counted it. Idempotent."""
    if "reasoning_tokens_reported" in row:
        return row
    row["reasoning_tokens_reported"] = row.get("reasoning_tokens")
    row["thinking_source_reported"] = row.get("thinking_source")
    row["reasoning_tokens"], row["thinking_source"] = reasoning_count(
        reported=row.get("reasoning_tokens_reported"),
        reported_source=row.get("thinking_source_reported"),
        completion=row.get("completion_tokens"))
    return row


def _budget_count(r: Dict[str, Any]) -> int:
    """The count a round is held against the note budget with — the rule of
    `budget_hit_rounds` in `summarise_rounds`, kept in one place. A row whose
    count is not one of `CORRECTED_SOURCES` (an old row, an estimate) is held
    with its completion instead."""
    if r.get("thinking_source") in CORRECTED_SOURCES and r.get("reasoning_tokens") is not None:
        return r["reasoning_tokens"]
    return r.get("completion_tokens") or r.get("reasoning_tokens") or 0


def _visible_text(msg: Dict[str, Any]) -> str:
    """What the round said aloud: its content and its tool calls' names and
    arguments — the part of the completion that is not the note."""
    parts = [str(msg.get("content") or "")]
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            parts.append(str(call))
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        args = fn.get("arguments", fn.get("input", ""))
        parts.append(str(fn.get("name") or ""))
        parts.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class RoundRecorder:
    """One row per LLM call of the agent loop: what it said, thought, was billed."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        # Optional: maps the provider's reported effort word to the rung the
        # round ran on (`run_loop_eval.served_effort_for`, production's rule).
        self.resolve_effort = None
        # Optional: text -> token count with the model's own tokenizer
        # (`seed_history.Tokenizer.count`); without it a non-engine count falls
        # back to the completion upper bound (`reasoning_count`).
        self.count_tokens: Optional[Callable[[str], int]] = None

    def record(self, msg: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]],
               elapsed_s: float) -> None:
        msg = msg or {}
        usage = usage or {}
        thinking = str(msg.get("thinking") or "")
        reported = usage.get("served_effort")
        count, source = reasoning_count(
            reported=usage.get("reasoning_tokens"), reported_source=usage.get("thinking_source"),
            completion=usage.get("completion_tokens"), thinking=thinking,
            visible=_visible_text(msg), count_tokens=self.count_tokens)
        self.rows.append({
            "round": len(self.rows) + 1,
            "content_chars": len(str(msg.get("content") or "").strip()),
            "tool_calls": len(msg.get("tool_calls") or []),
            "note_chars": len(thinking),
            "note_opening": _normalise(thinking)[:OPENING_CHARS],
            # The count held against the budget and how it was made
            # (`CORRECTED_SOURCES`); the provider's figure and word stay beside it,
            # informational only — on the incident's silent rows and the burn
            # control they were the chars / 4 estimate.
            "reasoning_tokens": count,
            "thinking_source": source,
            "reasoning_tokens_reported": usage.get("reasoning_tokens"),
            "thinking_source_reported": usage.get("thinking_source"),
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            # The word the provider read off the body it sent (None: it said
            # nothing), and the rung production would name for the round.
            "served_effort_reported": reported,
            "served_effort": (self.resolve_effort(reported)
                              if self.resolve_effort is not None else reported),
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
    if budget:
        hit_rounds = [r["round"] for r in rows
                      if _budget_count(r) >= budget * BUDGET_HIT_FRACTION]
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
    deep = [r["round"] for r in rows
            if r["prompt_tokens"] is not None and r["prompt_tokens"] >= SYMPTOM_MIN_PEAK_PROMPT]
    deep_hits = (len([n for n in hit_rounds if n in set(deep)])
                 if hit_rounds is not None else None)
    # The intersection: a round that is silent AND at the budget. Its own axis,
    # beside the two it is made of — the incident's seven capped rounds were all
    # silent ones, and a union or either axis alone would not say so.
    silent_hit_rounds = ([n for n in hit_rounds if n in set(silent)]
                         if hit_rounds is not None else None)
    return {
        "rounds": len(rows),
        "budget_hits": len(hit_rounds) if hit_rounds is not None else None,
        "budget_hit_rounds": hit_rounds,
        "silent_rounds": len(silent),
        "silent_round_list": silent,
        "longest_silent_streak": longest,
        "rounds_with_notes": with_notes,
        "repeat_opening_share": round(repeats / with_notes, 3) if with_notes else None,
        "note_tokens": sum(_budget_count(r) for r in rows),
        # Rows whose count was derived from the completion rather than counted
        # (engine or tokenizer), and every method used; a reader can then tell a
        # count from a bound.
        "note_tokens_estimated_rows": sum(
            1 for r in rows if r.get("thinking_source") not in COUNTED_SOURCES),
        "thinking_sources": {s: sum(1 for r in rows if r.get("thinking_source") == s)
                             for s in sorted({str(r.get("thinking_source")) for r in rows})},
        "first_prompt_tokens": prompts[0] if prompts else None,
        "peak_prompt_tokens": max(prompts) if prompts else None,
        # Rounds whose prompt reached SYMPTOM_MIN_PEAK_PROMPT, and the budget hits
        # among them — the denominator and numerator of the step-0 share.
        "deep_rounds": len(deep),
        "deep_budget_hits": deep_hits,
        "silent_budget_hits": len(silent_hit_rounds) if silent_hit_rounds is not None else None,
        "silent_budget_hit_rounds": silent_hit_rounds,
        # Reasoning depth, beside the context depth of the prompt figures: the
        # largest per-round count held against the budget, and the budget itself.
        "note_budget": budget,
        "max_reasoning_tokens": (max(_budget_count(r) for r in rows)
                                 if any(r["reasoning_tokens"] is not None
                                        or r["completion_tokens"] is not None for r in rows)
                                 else None),
        "reasoning_tokens_by_round": [_budget_count(r) for r in rows],
        # Every rung the rounds ran on, as production names it; [] when no round
        # carried a word (unknown, not `off`).
        "served_effort": sorted({str(r["served_effort"]) for r in rows
                                 if r.get("served_effort")}),
    }


def _share(hits: Optional[int], rounds: Optional[int]) -> Optional[float]:
    if hits is None or not rounds:
        return None
    return hits / rounds


def task_symptom(m: Dict[str, Any]) -> Optional[bool]:
    """Did this task-run show the incident's symptom? None when it was not measurable."""
    if m.get("budget_hits") is None or m.get("peak_prompt_tokens") is None:
        return None
    share = _share(m.get("deep_budget_hits"), m.get("deep_rounds"))
    return (m["budget_hits"] >= SYMPTOM_MIN_BUDGET_HITS
            and m["peak_prompt_tokens"] >= SYMPTOM_MIN_PEAK_PROMPT
            and share is not None and share >= SYMPTOM_MIN_BUDGET_SHARE)


def run_label(r: Dict[str, Any], multi: bool) -> str:
    """What one task-run is called in step 0 and the verdict: the task id, plus
    its repeat when the run made more than one per task (`--repeats`)."""
    return f"{r['id']} r{r.get('repeat') or 1}" if multi else r["id"]


def _multi(results: List[Dict[str, Any]]) -> bool:
    return any((r.get("repeat") or 1) > 1 for r in results)


def step0(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The top line: did the off arm reproduce the incident — and, per task, in
    how many of its repeats (k of N)? The burn control is read apart
    (`burn_control`): its prompt is shallow by design and it says nothing about
    the incident's regime, only whether a capped round can happen here at all."""
    ran = [r for r in results if r.get("arm") == "off" and r.get("metrics")]
    burn_rows = [r for r in ran if r.get("kind") == KIND_BURN]
    burn = burn_control(burn_rows) if burn_rows else None
    out = _step0_incident([r for r in ran if r.get("kind") != KIND_BURN])
    if burn is not None:
        out["burn_control"] = burn
        out["outcome"] = step0_outcome(out.get("by_task") or {}, burn)
        if not out.get("by_task") and out["reproduced"] is None:
            out["why"] = "only the burn control ran — the incident symptom was not asked"
    return out


def burn_control(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Did the burn control produce burning — at least one round at >= 0.98 x the
    note budget? None when no budget or no count was reported (not measured)."""
    multi = _multi(rows)
    by_run = []
    for r in rows:
        m = r["metrics"]
        budget, most = m.get("note_budget"), m.get("max_reasoning_tokens")
        by_run.append({
            "run": run_label(r, multi), "rounds": m.get("rounds"),
            "max_reasoning_tokens": most, "budget": budget,
            "budget_hits": m.get("budget_hits"), "silent_budget_hits": m.get("silent_budget_hits"),
            "reasoning_tokens_by_round": m.get("reasoning_tokens_by_round"),
            "timed_out": bool(r.get("timed_out")),
            "burned": (None if not budget or most is None
                       else most >= budget * BUDGET_HIT_FRACTION),
        })
    measured = [b for b in by_run if b["burned"] is not None]
    counts = [b["max_reasoning_tokens"] for b in measured]
    return {
        "produced": any(b["burned"] for b in measured) if measured else None,
        "runs": len(by_run), "runs_measured": len(measured),
        "runs_burned": sum(1 for b in measured if b["burned"]),
        "max_reasoning_tokens": max(counts) if counts else None,
        "budget": next((b["budget"] for b in by_run if b["budget"]), None),
        "threshold_fraction": BUDGET_HIT_FRACTION,
        "by_run": by_run,
    }


def burn_line(burn: Dict[str, Any]) -> str:
    """`burn produced: yes/no (max reasoning tokens N of budget B)`."""
    if burn.get("produced") is None:
        return ("burn produced: not measured (no note budget on the alias, or no "
                "reasoning count reported)")
    return (f"burn produced: {'yes' if burn['produced'] else 'no'} (max reasoning tokens "
            f"{burn['max_reasoning_tokens']} of budget {burn['budget']}; "
            f"{burn['runs_burned']}/{burn['runs_measured']} run(s) at >= "
            f"{BUDGET_HIT_FRACTION:.2f} x budget)")


def _step0_incident(off: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not off:
        return {"reproduced": None, "why": "no off-arm run in this report — not measured"}
    hits = [r["metrics"]["budget_hits"] for r in off if r["metrics"]["budget_hits"] is not None]
    peaks = [r["metrics"]["peak_prompt_tokens"] for r in off
             if r["metrics"]["peak_prompt_tokens"] is not None]
    flags = [task_symptom(r["metrics"]) for r in off]
    timed_out = [r["id"] for r in off if r.get("timed_out")]
    if not hits or not peaks:
        return {"reproduced": None, "budget_hits": None, "peak_prompt": None,
                "timed_out_off": timed_out,
                "why": "the provider reported no note or prompt counts — not measured"}
    multi = _multi(off)
    yes: List[str] = []
    by_task: Dict[str, Dict[str, Any]] = {}
    for r, f in zip(off, flags):
        if f and r["id"] not in yes:
            yes.append(r["id"])
        t = by_task.setdefault(r["id"], {"kind": r.get("kind"), "runs": 0, "burned": 0,
                                         "budget_hits": 0, "silent_rounds": 0,
                                         "silent_budget_hits": 0, "timed_out": 0})
        t["runs"] += 1
        t["burned"] += 1 if f else 0
        t["budget_hits"] += r["metrics"].get("budget_hits") or 0
        t["silent_rounds"] += r["metrics"].get("silent_rounds") or 0
        t["silent_budget_hits"] += r["metrics"].get("silent_budget_hits") or 0
        t["timed_out"] += 1 if r.get("timed_out") else 0
    deep_hits = sum(r["metrics"].get("deep_budget_hits") or 0 for r in off)
    deep_rounds = sum(r["metrics"].get("deep_rounds") or 0 for r in off)
    out = {"reproduced": bool(yes), "budget_hits": sum(hits), "peak_prompt": max(peaks),
           "deep_budget_hits": deep_hits, "deep_rounds": deep_rounds,
           "deep_budget_share": _share(deep_hits, deep_rounds),
           "silent_budget_hits": sum(t["silent_budget_hits"] for t in by_task.values()),
           "tasks_reproducing": yes,
           "runs_reproducing": [run_label(r, multi) for r, f in zip(off, flags) if f],
           "by_task": by_task, "off_tasks": len(by_task), "off_runs": len(off),
           "timed_out_off": timed_out}
    out["outcome"] = step0_outcome(by_task)
    if not yes:
        out["why"] = (
            f"no off-arm task reached >= {SYMPTOM_MIN_BUDGET_HITS} budget hits on a prompt "
            f">= {SYMPTOM_MIN_PEAK_PROMPT} tokens with >= {SYMPTOM_MIN_BUDGET_SHARE:.2f} of its "
            f"rounds past {SYMPTOM_MIN_PEAK_PROMPT} at the budget, so the off arm never entered "
            "the regime the flag is meant to change; the A/B below measured nothing about it")
        if timed_out:
            out["why"] += (f" ({len(timed_out)} off-arm task(s) hit the harness timeout first: "
                           f"{', '.join(timed_out)})")
    return out


BURN_PRODUCED = ("the instrument can produce burning; the open question is which task "
                 "shape makes the model think instead of read")
# The agreed reading of a burn control that does not burn; the decision on the
# card stays the owner's.
BURN_ABSENT = ("the instrument does not produce burning at all on this setup; further "
               "attempts measure nothing — record the card as 0 of N, not reproduced, flag "
               "stays off (the agreed reading; the decision on the card is the owner's)")


def step0_outcome(by_task: Dict[str, Dict[str, Any]],
                  burn: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Which hypothesis the off arm's burning points at (attempt 3) — or, when the
    burn control ran, whether this instrument can produce burning at all, which
    comes first: without it no other reading means anything.

    The card's mechanism — the model re-plans every round because it lost the
    reasoning that chose the tool — predicts burning on a closable task that
    needs a plan held in mind. The rival (Ark, 2026-10-06) — burning is what a
    model does with a question it cannot close — predicts burning on the
    unresolvable control. Only the pattern across task kinds separates them.
    """
    if burn is not None:
        if burn.get("produced") is None:
            out = {"code": "burn-not-measured",
                   "text": "the burn control ran but was not measured (no note budget or no "
                           "reasoning count) — no reading"}
        elif burn["produced"]:
            out = {"code": "burn-produced", "text": BURN_PRODUCED}
        else:
            out = {"code": "burn-absent", "text": BURN_ABSENT}
        if by_task:
            rest = step0_outcome(by_task)
            out["tasks_code"] = rest["code"]
            out["text"] += f"; the other tasks: {rest['text']}"
        return out
    burned = {t.get("kind") or "baseline" for t in by_task.values() if t["burned"]}
    ran = {t.get("kind") or "baseline" for t in by_task.values()}
    unresolvable = KIND_UNRESOLVABLE in burned
    closable = sorted(k for k in burned if k != KIND_UNRESOLVABLE)
    if not burned:
        return {"code": "not-reproduced",
                "text": "burns nowhere: not reproduced outside production (residual gap: a "
                        "throwaway root has no Johnny system prompt, identity, memory or "
                        "Active Recall)"}
    if unresolvable and not closable:
        return {"code": "rival",
                "text": "burns only on the unresolvable control: favours the rival hypothesis "
                        "(the burning belongs to a question the model cannot close, not to "
                        "lost reasoning)"}
    if closable and not unresolvable:
        text = (f"burns on a closable task ({', '.join(closable)}): the card's mechanism is "
                "plausible")
        if KIND_UNRESOLVABLE not in ran:
            text += " (the unresolvable control did not run, so the rival is untested)"
        return {"code": "mechanism", "text": text}
    return {"code": "both",
            "text": f"burns on the unresolvable control and on {', '.join(closable)}: the two "
                    "hypotheses are not separated"}


def seed_beyond_incident(seed: Optional[Dict[str, Any]]) -> int:
    """Records a seed carries that the incident's prompt did not (a deep seed's
    `outside_incident_history`); 0 for the incident seed itself or no seed."""
    deep = (seed or {}).get("deepening") or {}
    return int(deep.get("outside_incident_history") or 0)


def step0_line(s0: Dict[str, Any], seed: Optional[Dict[str, Any]] = None) -> str:
    """The printed step-0 line: the verdict, both thresholds' numbers, the
    silent-AND-at-budget intersection, and — for a deep seed — how far the seed
    reaches past the history the incident actually loaded."""
    head = "incident symptom reproduced in the off arm"
    if s0.get("reproduced") is None:
        return f"{head}: not measured ({s0.get('why')})"
    share = s0.get("deep_budget_share")
    share_s = f"{share:.2f}" if share is not None else "-"
    line = (f"{head}: {'yes' if s0['reproduced'] else 'no'} "
            f"(budget hits {s0['budget_hits']} [need >= {SYMPTOM_MIN_BUDGET_HITS} in one task], "
            f"peak prompt {s0['peak_prompt']} [need >= {SYMPTOM_MIN_PEAK_PROMPT}], "
            f"budget-hit share past {SYMPTOM_MIN_PEAK_PROMPT}: "
            f"{s0.get('deep_budget_hits')}/{s0.get('deep_rounds')} = {share_s} "
            f"[need >= {SYMPTOM_MIN_BUDGET_SHARE:.2f} in one task], "
            f"silent AND at budget: {s0.get('silent_budget_hits')} "
            f"[the incident: {INCIDENT_SILENT_BUDGET_HITS}])")
    if s0.get("tasks_reproducing"):
        line += f"; reproducing: {', '.join(s0['tasks_reproducing'])}"
    beyond = seed_beyond_incident(seed)
    if beyond:
        line += f"; approximation: {beyond} records beyond the incident's history"
    return line


def step0_task_lines(s0: Dict[str, Any]) -> List[str]:
    """Per task: whether the off arm burned, in k of N repeats, and the axes."""
    lines = []
    for task, t in (s0.get("by_task") or {}).items():
        lines.append(f"  {task} [{t.get('kind') or 'baseline'}]: budget-burn reproduced "
                     f"{t['burned']}/{t['runs']}; budget hits {t['budget_hits']}, silent rounds "
                     f"{t['silent_rounds']}, silent AND at budget {t['silent_budget_hits']}"
                     + (f", {t['timed_out']} timed out" if t["timed_out"] else ""))
    burn = s0.get("burn_control")
    if burn:
        lines.append(f"  long-control-burn [{KIND_BURN}]: {burn_line(burn)}")
    if s0.get("outcome"):
        lines.append(f"  outcome: {s0['outcome']['text']}")
    return lines


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


# Each axis: the printed label and the key it reads. No direction is attached on
# purpose — the verdict ranks nothing. Visible output is its own axis, beside
# the rest; a timeout is its own axis too, so a task the harness cut off is not
# read as a failure of the flag.
AXES = (
    ("success", "passed"),
    ("timed_out", "timed_out"),
    ("rounds_to_done", "rounds_to_completion"),
    ("rounds", "rounds"),
    ("budget_hits", "budget_hits"),
    ("silent_rounds", "silent_rounds"),
    ("silent_and_hit", "silent_budget_hits"),
    ("silent_streak", "longest_silent_streak"),
    ("repeat_open", "repeat_opening_share"),
    ("note_tokens", "note_tokens"),
    ("peak_prompt", "peak_prompt_tokens"),
    ("compact_at", "compaction_round"),
    ("compactions", "compactions"),
    ("compact_fail", "compaction_failures"),
    ("wall_s", "wall_s"),
)
_COUNTED = ("passed", "timed_out")


def _value(r: Dict[str, Any], key: str) -> Any:
    if key == "passed" and r.get("passed") is None:
        return None  # not scored (the burn control): absent, not failed
    if key in _COUNTED:
        return bool(r.get(key))
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

    Totals are printed twice: over every paired task, and over the tasks whose
    off arm reproduced the incident (`step0`'s `tasks_reproducing`). Only the
    second set is in the regime the flag is meant to change; a total over all
    tasks dilutes it with tasks where there was nothing to change.
    """
    # One pair per task-run: with `--repeats` the same task runs N times per arm,
    # and repeat k of the off arm is set beside repeat k of the on arm.
    multi = _multi(results)
    by_task: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in results:
        by_task.setdefault(run_label(r, multi), {})[r.get("arm", "?")] = r
    paired = {t: arms for t, arms in by_task.items() if "off" in arms and "on" in arms}
    if not paired:
        return ["no task ran under both arms — nothing to set side by side"]
    lines = []
    for t, arms in paired.items():
        off, on = arms["off"], arms["on"]
        lines.append(f"  {t}")
        for label, key in AXES:
            lines.append(f"    {label:>14}  off={_fmt(_value(off, key)):>9}  on={_fmt(_value(on, key)):>9}")

    lines.append(f"  all {len(paired)} paired task(s)")
    lines.extend(_total_lines(paired, "all tasks"))
    reproducing = [t for t in (step0(results).get("runs_reproducing") or []) if t in paired]
    if reproducing:
        subset = {t: paired[t] for t in reproducing}
        lines.append(f"  reproducing subset, {len(subset)} task(s): {', '.join(reproducing)}")
        lines.extend(_total_lines(subset, "reproducing subset"))
    else:
        lines.append("  reproducing subset: empty — no paired task's off arm reproduced the "
                     "incident, so no total below is in the regime the flag is meant to change")
    return lines


def _total_lines(paired: Dict[str, Dict[str, Dict[str, Any]]], scope: str) -> List[str]:
    """The total block and its warnings for one set of paired tasks."""
    def total(arm: str, key: str):
        vals = [_value(a[arm], key) for a in paired.values()]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        if key in _COUNTED:
            return sum(1 for v in vals if v)
        if key in ("peak_prompt_tokens",):
            return max(vals)
        return sum(vals)

    lines = []
    for label, key in AXES:
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
    t_off, t_on = total("off", "timed_out"), total("on", "timed_out")
    if t_off or t_on:
        warnings.append(f"{t_off} off / {t_on} on task(s) hit the harness timeout — a cut-off "
                        "run, not a failure of either arm")
    b_off, b_on = total("off", "budget_hits"), total("on", "budget_hits")
    if b_off is not None and b_on is not None and b_on < b_off and warnings:
        warnings.append("fewer budget hits in the on arm are not read as a result while the "
                        "warnings above stand")
    lines.extend(f"  WARNING ({scope}): {w}" for w in warnings)
    return lines
