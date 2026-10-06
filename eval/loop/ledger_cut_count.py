"""How often was a round's reasoning cut at the reasoning budget? A standing counter.

The board card `THE-MODEL-STARTS-EVERY-ROUND-WITHOUT-THE-REASONING-THAT-CHOSE-THE-TOOL`
stays open as a counter (Mike's call, 2026-10-06): the `preserve_reasoning` flag
(`2407f64f`) stays off, and this file says how often the case it would address
happens in production. It reads the node ledger, never the model: no model, no
service, no network, and no message content — a ledger row holds none, and
nothing here prints more than caller, task_id, alias and counts.

Every row of an alias whose providers.json entry carries `reasoning_budget_tokens`
(B; THR = 0.98) is one of:

- `no_cut` — completion < THR x B. Proof: on llama-server the completion holds
  every sampled token, reasoning included, so the reasoning was under the cap.
- `cut` — the engine counted the reasoning (`thinking_source` == engine) at
  >= THR x B on an alias whose budget is a hard cap; or the round was silent
  (`content_chars` == 0 and `tool_calls` == 0) and its completion >= B, so all
  of it was reasoning. The second form needs the two fields added to rows on
  2026-10-06; older rows lack them and cannot take it.
- `undetermined` — every other row at or above THR x B. On the local path the
  reasoning figure is `chars / 4` clamped to the completion, which read ~2 948
  for rounds the tokenizer proved cut at 10 000, so an estimate decides nothing.
  Inside it, the clamp band (thinking == completion, B <= completion <= 1.1 x B)
  is reported apart: the lower figure is `cut` + clamp band, the upper figure
  `cut` + all undetermined.

Rows of an alias without a budget are `not_applicable`; rows whose alias is not
in today's providers.json, and whose model maps to no single budget, are
`unmapped` — absent, never folded into zero.

An episode is a maximal run of consecutive rows over the line (lower or upper)
inside one (caller, task_id), ordered by `started_at`. A row with no task_id
cannot be ordered inside a task and is an episode of its own.

The budget is today's providers.json, not the one in force when a row was
written; it changed on 2026-10-01.

Run from `dpc-client/core`:

    uv run python ../../eval/loop/ledger_cut_count.py
    uv run python ../../eval/loop/ledger_cut_count.py --since 2026-09-29 --until 2026-10-06
    uv run python ../../eval/loop/ledger_cut_count.py --json

Exit code is 0 always: this is a reading, not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

THR = 0.98
CLAMP_BAND_TOP = 1.1
# Provider types whose `reasoning_budget_tokens` the server enforces as a cap:
# llama-server stops the trace at exactly the budget and still reports `stop`
# (burn control, 2026-10-06: a tokenizer count of exactly 10 000).
HARD_CAP_TYPES = frozenset({"llamacpp_server"})
# The node ledger's own partition pattern (`NodeLedger.partitions`): a backup
# such as `usage-2026-09.jsonl.before-nd-fix-2026-09-28` does not match it.
PARTITION_GLOB = "usage-????-??.jsonl"
BUDGET_NOTE = (
    "budget = reasoning_budget_tokens in today's providers.json, not the value in force "
    "when a row was written; it changed on 2026-10-01"
)

NO_CUT, CUT, UNDETERMINED = "no_cut", "cut", "undetermined"
NOT_APPLICABLE, UNMAPPED = "not_applicable", "unmapped"
CLASSES = (NO_CUT, CUT, UNDETERMINED, NOT_APPLICABLE, UNMAPPED)
BUDGETED = (NO_CUT, CUT, UNDETERMINED)


# --- budgets ------------------------------------------------------------------

def load_budgets(providers: Dict[str, Any]) -> Dict[str, Dict[str, Tuple[Optional[int], Optional[str]]]]:
    """{"alias": {alias: (budget, type)}, "model": {model: (budget, type)}}.

    A model is mapped only when every entry naming it agrees on one budget and
    one type; that is how a row of a retired alias still finds its budget.
    """
    by_alias: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    by_model: Dict[str, set] = defaultdict(set)
    for entry in providers.get("providers") or []:
        budget = entry.get("reasoning_budget_tokens")
        budget = int(budget) if isinstance(budget, (int, float)) and budget > 0 else None
        ptype = entry.get("type")
        if entry.get("alias") is not None:
            by_alias[entry["alias"]] = (budget, ptype)
        if entry.get("model"):
            by_model[entry["model"]].add((budget, ptype))
    single = {m: next(iter(v)) for m, v in by_model.items() if len(v) == 1}
    return {"alias": by_alias, "model": single}


def budget_for(row: Dict[str, Any], budgets) -> Tuple[Optional[int], Optional[str], str]:
    """(budget, provider type, how): how is alias, model or unmapped."""
    alias = row.get("alias")
    if alias in budgets["alias"]:
        b, t = budgets["alias"][alias]
        return b, t, "alias"
    model = row.get("model")
    if model in budgets["model"]:
        b, t = budgets["model"][model]
        return b, t, "model"
    return None, None, "unmapped"


# --- classification -------------------------------------------------------------

def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def classify(row: Dict[str, Any], budget: Optional[int], ptype: Optional[str], how: str) -> Tuple[str, bool]:
    """(class, in_clamp_band). The band is only ever true for `undetermined`."""
    if how == "unmapped":
        return UNMAPPED, False
    if not budget:
        return NOT_APPLICABLE, False
    comp = _int(row.get("completion_tokens"))
    if comp is None:
        return UNDETERMINED, False      # no completion: nothing proves either way
    if comp < THR * budget:
        return NO_CUT, False
    thinking = _int(row.get("thinking_tokens"))
    if (ptype in HARD_CAP_TYPES and row.get("thinking_source") == "engine"
            and thinking is not None and thinking >= THR * budget):
        return CUT, False
    # A silent round: no visible text and no tool call, so the whole completion
    # was reasoning. Null in either field means unknown, never zero.
    if (_int(row.get("content_chars")) == 0 and _int(row.get("tool_calls")) == 0
            and comp >= budget):
        return CUT, False
    clamp = thinking is not None and thinking == comp and budget <= comp <= CLAMP_BAND_TOP * budget
    return UNDETERMINED, clamp


# --- reading ----------------------------------------------------------------------

def parse_started_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def partitions(ledger_dir: Path) -> List[Path]:
    return sorted(ledger_dir.glob(PARTITION_GLOB))


def read_rows(paths: Iterable[Path], since: Optional[datetime], until: Optional[datetime],
              stats: Counter) -> Iterator[Dict[str, Any]]:
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    stats["unparsable_lines"] += 1
                    continue
                dt = parse_started_at(row.get("started_at"))
                if dt is None:
                    stats["rows_without_started_at"] += 1
                    continue
                if (since and dt < since) or (until and dt >= until):
                    continue
                row["_dt"] = dt
                yield row


# --- counting ---------------------------------------------------------------------

def _minutes(rows: Iterable[Dict[str, Any]]) -> float:
    return round(sum(float(r.get("duration_s") or 0) for r in rows) / 60.0, 1)


def _episodes(rows: List[Dict[str, Any]], pred) -> List[List[Dict[str, Any]]]:
    """Maximal runs of rows satisfying `pred`, within each (caller, task_id)."""
    seqs: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    out: List[List[Dict[str, Any]]] = []
    for r in rows:
        if r.get("task_id") is None:
            if pred(r):
                out.append([r])
        else:
            seqs[(r.get("caller"), r.get("task_id"))].append(r)
    for seq in seqs.values():
        seq.sort(key=lambda r: r["_dt"])
        cur: List[Dict[str, Any]] = []
        for r in seq:
            if pred(r):
                cur.append(r)
            elif cur:
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
    out.sort(key=lambda ep: ep[0]["_dt"])
    return out


def _is_lower(r):
    return r["_cls"] == CUT or (r["_cls"] == UNDETERMINED and r["_clamp"])


def _is_upper(r):
    return r["_cls"] in (CUT, UNDETERMINED)


def _episode_record(ep: List[Dict[str, Any]], tz) -> Dict[str, Any]:
    return {
        "caller": ep[0].get("caller"),
        "task_id": ep[0].get("task_id"),
        "start_local": ep[0]["_dt"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "last_start_local": ep[-1]["_dt"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "rows": len(ep),
        "minutes": _minutes(ep),
        "aliases": sorted({str(r.get("alias")) for r in ep}),
    }


def _figure(rows, pred, tz) -> Dict[str, Any]:
    sel = [r for r in rows if pred(r)]
    eps = _episodes(rows, pred)
    return {
        "rows": len(sel),
        "minutes": _minutes(sel),
        "episode_count": len(eps),
        "longest_run": max((len(e) for e in eps), default=0),
        "episodes": [_episode_record(e, tz) for e in eps],
    }


def _caller_summary(rows: List[Dict[str, Any]], tz) -> Dict[str, Any]:
    c = Counter(r["_cls"] for r in rows)
    budgeted = [r for r in rows if r["_cls"] in BUDGETED]
    lower, upper = _figure(rows, _is_lower, tz), _figure(rows, _is_upper, tz)
    return {
        "rows": len(rows),
        "rows_with_budget": len(budgeted),
        **{k: c[k] for k in CLASSES},
        "undetermined_clamp_band": sum(1 for r in rows if r["_cls"] == UNDETERMINED and r["_clamp"]),
        "rows_without_task_id": sum(1 for r in rows if r.get("task_id") is None),
        "minutes_over_budget": _minutes(r for r in rows if _is_upper(r)),
        "minutes_with_budget": _minutes(budgeted),
        "minutes_total": _minutes(rows),
        "lower": lower,
        "upper": upper,
    }


def count(rows: Iterable[Dict[str, Any]], budgets, tz=timezone.utc) -> Dict[str, Any]:
    rows = list(rows)
    mapping = Counter()
    for r in rows:
        b, t, how = budget_for(r, budgets)
        r["_cls"], r["_clamp"] = classify(r, b, t, how)
        mapping[(str(r.get("alias")), how, b, t)] += 1
    callers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        callers[str(r.get("caller"))].append(r)
    order = sorted(callers, key=lambda k: (-len(callers[k]), k))
    return {
        "budget_note": BUDGET_NOTE,
        "threshold": THR,
        "rows": len(rows),
        "first_started_at": min((r["_dt"] for r in rows), default=None),
        "last_started_at": max((r["_dt"] for r in rows), default=None),
        "mapping": [
            {"alias": a, "how": how, "budget": b, "type": t, "rows": n}
            for (a, how, b, t), n in sorted(mapping.items(), key=lambda kv: -kv[1])
        ],
        "callers": {k: _caller_summary(callers[k], tz) for k in order},
        "total": _caller_summary(rows, tz),
    }


# --- output -----------------------------------------------------------------------

def render(report: Dict[str, Any], window: str, files: List[str]) -> str:
    out = [
        "ledger_cut_count — rounds whose reasoning may have been cut at the budget",
        f"window: {window}",
        f"files: {', '.join(files) or '(none)'}",
        f"NOTE: {report['budget_note']}.",
        f"rows read: {report['rows']}  (first {report['first_started_at']}, last {report['last_started_at']})",
        "",
        "budget mapping (alias, how, budget, provider type): rows",
    ]
    for m in report["mapping"]:
        out.append(f"  {m['alias']!r} {m['how']} {m['budget']} {m['type']}: {m['rows']}")
    out += ["", "per caller (THR = 0.98 x budget; lower = cut + clamp band, upper = cut + all undetermined):"]
    header = (f"{'caller':40s} {'rows':>6s} {'budg':>6s} {'no_cut':>6s} {'cut':>4s} {'undet':>5s} "
              f"{'clamp':>5s} {'n/a':>6s} {'unmap':>5s} | {'lowR':>4s} {'lowE':>4s} {'lowRun':>6s} {'lowMin':>7s} "
              f"| {'upR':>4s} {'upE':>4s} {'upRun':>5s} {'upMin':>7s} | {'budgMin':>8s} {'totMin':>8s}")
    out.append(header)

    def line(name, s):
        lo, up = s["lower"], s["upper"]
        return (f"{name[:40]:40s} {s['rows']:6d} {s['rows_with_budget']:6d} {s[NO_CUT]:6d} {s[CUT]:4d} "
                f"{s[UNDETERMINED]:5d} {s['undetermined_clamp_band']:5d} {s[NOT_APPLICABLE]:6d} {s[UNMAPPED]:5d} | "
                f"{lo['rows']:4d} {lo['episode_count']:4d} {lo['longest_run']:6d} {lo['minutes']:7.1f} | "
                f"{up['rows']:4d} {up['episode_count']:4d} {up['longest_run']:5d} {up['minutes']:7.1f} | "
                f"{s['minutes_with_budget']:8.1f} {s['minutes_total']:8.1f}")

    for name, s in report["callers"].items():
        out.append(line(name, s))
    out.append(line("TOTAL", report["total"]))
    out.append("  (minutes over the budget = upMin; rows without task_id are episodes of their own: "
               f"{report['total']['rows_without_task_id']} rows)")
    for label in ("lower", "upper"):
        eps = report["total"][label]["episodes"]
        out += ["", f"{label} episodes ({len(eps)}), local time:"]
        for e in eps:
            out.append(f"  {e['start_local']} .. {e['last_start_local'][11:]}  {e['caller']}  task={e['task_id']}  "
                       f"rows={e['rows']}  min={e['minutes']:.1f}  aliases={e['aliases']}")
    return "\n".join(out)


def _jsonable(report: Dict[str, Any]) -> Dict[str, Any]:
    def conv(o):
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))
    return json.loads(json.dumps(report, default=conv))


def local_bounds(since: Optional[str], until: Optional[str], tz) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Local dates to a half-open UTC window; `until` is inclusive of its whole day."""
    lo = datetime.combine(date.fromisoformat(since), time(0), tz) if since else None
    hi = datetime.combine(date.fromisoformat(until) + timedelta(days=1), time(0), tz) if until else None
    return (lo.astimezone(timezone.utc) if lo else None, hi.astimezone(timezone.utc) if hi else None)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    home = Path.home() / ".dpc"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="first local date, YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", help="last local date, YYYY-MM-DD (inclusive)")
    ap.add_argument("--ledger-dir", type=Path, default=home / "ledger")
    ap.add_argument("--providers", type=Path, default=home / "providers.json")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    tz = datetime.now().astimezone().tzinfo
    try:
        since, until = local_bounds(args.since, args.until, tz)
        providers = json.loads(Path(args.providers).read_text(encoding="utf-8"))
        budgets = load_budgets(providers)
        paths = partitions(Path(args.ledger_dir))
        stats: Counter = Counter()
        report = count(read_rows(paths, since, until, stats), budgets, tz)
        report["skipped"] = dict(stats)
    except (OSError, ValueError) as exc:
        print(f"ledger_cut_count: could not read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    window = (f"local {args.since or 'start'} .. {args.until or 'end'} "
              f"(UTC {since.isoformat() if since else '-'} .. {until.isoformat() if until else '-'}, half-open)")
    files = [p.name for p in paths]
    if args.json:
        report = _jsonable(report)
        report.update({"window": window, "files": files})
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report, window, files))
        if report.get("skipped"):
            print(f"\nskipped: {report['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
