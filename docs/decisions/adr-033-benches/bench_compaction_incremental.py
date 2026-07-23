"""
ADR-033 bench-3: INCREMENTAL compaction (the improvement).
Instead of one giant summary of all old rounds, compact each old round individually
as it ages out of keep_recent. Small tool results (grep/search lists) kept as-is (no LLM).
Measures: per-call latency distribution, total cost, final compacted size — vs single-shot.

Run from dpc-client/core:  uv run python <scratchpad>/bench_compaction_incremental.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CORE = Path(r"C:\Users\mikha\Documents\dpc-messenger\dpc-client\core")
sys.path.insert(0, str(CORE))

from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.dpc_agent.pricing import compute_cost_usd

FLASH_ALIAS = "deepseek_flash"
FLASH_MODEL = "deepseek-v4-flash"
KEEP_RECENT = 6
KEEP_ASIS_TOKENS = 500        # small results (grep/search lists) kept verbatim, no LLM call
PER_CALL_INPUT_CAP = 60_000   # a single monstrous result is still capped (rare worst case)

ARCHIVE = (Path.home() / ".dpc" / "conversations" / "group-b88b65076b85-dpc-project"
           / "archive" / "2026" / "07" / "2026-07-14T13-22-14_reset_session.json")

ROUND_PROMPT = """Compact this single tool round from an AI research agent into 1-3 lines.
Preserve EXACT numbers, metrics, commit hashes, file paths, and error text verbatim.
Drop boilerplate/markup/URL noise. Output only the compact note.

--- TOOL ROUND {r} ---
{body}
--- END ---
Compact note:"""


def load_rounds(archive_path):
    data = json.load(open(archive_path, encoding="utf-8"))
    msgs = data.get("messages", data) if isinstance(data, dict) else data
    by_round = {}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        for tc in (m.get("tool_calls") or []):
            by_round.setdefault(tc.get("round", 0), []).append(tc)
    return by_round


def serialize_round(calls):
    out = []
    for c in calls:
        err = " [ERROR]" if c.get("is_error") else ""
        out.append(f"TOOL {c.get('tool')}{err}\n  input: {c.get('input')}\n  output: {c.get('output')}")
    return "\n".join(out)


async def main():
    lm = LLMManager()
    if FLASH_ALIAS not in lm.providers:
        print(f"[FATAL] provider '{FLASH_ALIAS}' not loaded")
        return

    by_round = load_rounds(ARCHIVE)
    round_ids = sorted(by_round)
    old_ids = round_ids[:-KEEP_RECENT] if len(round_ids) > KEEP_RECENT else round_ids

    print("=" * 70)
    print("BENCH-3: INCREMENTAL compaction (per-round, the improvement)")
    print(f"source     : archive/2026/07/{ARCHIVE.name}")
    print(f"old rounds : {len(old_ids)} to compact incrementally (keep_recent={KEEP_RECENT})")
    print(f"rule       : results <{KEEP_ASIS_TOKENS} tok kept as-is (no LLM); larger -> 1 small call")
    print("=" * 70)

    per_call_latency = []
    total_cost = 0.0
    total_out_chars = 0
    kept_asis = 0
    llm_calls = 0
    biggest = (0, None)

    for r in old_ids:
        body = serialize_round(by_round[r])
        tok = lm.count_tokens(body, FLASH_MODEL)
        if tok < KEEP_ASIS_TOKENS:
            kept_asis += 1
            total_out_chars += len(body)  # kept verbatim
            print(f"[round {r:>2}] {tok:>6} tok  KEPT AS-IS (no call)")
            continue
        capped = ""
        if tok > PER_CALL_INPUT_CAP:
            body = body[: int(len(body) * PER_CALL_INPUT_CAP / tok)]
            capped = " [capped]"
        prompt = ROUND_PROMPT.format(r=r, body=body)
        in_tok = lm.count_tokens(prompt, FLASH_MODEL)
        t0 = time.perf_counter()
        try:
            meta = await lm.query(prompt, provider_alias=FLASH_ALIAS, return_metadata=True)
        except Exception as e:
            print(f"[round {r:>2}] {tok:>6} tok  ERROR {type(e).__name__}: {e}")
            continue
        dt = time.perf_counter() - t0
        resp = meta.get("response", "") if isinstance(meta, dict) else str(meta)
        out_tok = lm.count_tokens(resp, FLASH_MODEL)
        cost = compute_cost_usd(FLASH_ALIAS, in_tok, out_tok, model=FLASH_MODEL)
        per_call_latency.append(dt)
        total_cost += cost
        total_out_chars += len(resp)
        llm_calls += 1
        if in_tok > biggest[0]:
            biggest = (in_tok, r)
        print(f"[round {r:>2}] {tok:>6} tok in -> {dt:5.1f}s  out {len(resp):>4}ch  ${cost:.5f}{capped}")

    print("=" * 70)
    if per_call_latency:
        per_call_latency.sort()
        med = per_call_latency[len(per_call_latency) // 2]
        print(f"LLM calls      : {llm_calls}  (+ {kept_asis} rounds kept as-is, 0 cost/latency)")
        print(f"per-call latency: median {med:.1f}s  (min {per_call_latency[0]:.1f} / max {per_call_latency[-1]:.1f}s)")
        print(f"  under 10s timeout: {sum(1 for x in per_call_latency if x <= 10)}/{len(per_call_latency)}")
        print(f"biggest single call: {biggest[0]:,} tok (round {biggest[1]}) — the worst-case one result")
        print(f"TOTAL cost     : ${total_cost:.5f} across {llm_calls} calls (spread over session, not one spike)")
        print(f"final compacted: {total_out_chars:,} chars (fits easily in window)")
    print("=" * 70)
    print("CONTRAST vs single-shot (bench-2):")
    print("  single-shot: 1 call, 1.4M tok input (EXCEEDS 1M window), 30-60s, all-or-nothing")
    print(f"  incremental: {llm_calls} calls, each <= per-round size, per-call latency above, never exceeds window")


if __name__ == "__main__":
    asyncio.run(main())
