"""
ADR-033 compaction bench on REAL autoresearch tool-history (archived group session).
Focus: does the flash summary preserve EXACT numbers/hashes/metrics (Ark's Q3)?

Run from dpc-client/core:  uv run python <scratchpad>/bench_compaction_archive.py
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
TRIALS = 3
MAX_INPUT_TOKENS = 100_000  # if old-history exceeds this, cap + flag (single-call infeasible)

ARCHIVE = (Path.home() / ".dpc" / "conversations" / "group-b88b65076b85-dpc-project"
           / "archive" / "2026" / "07" / "2026-07-14T13-22-14_reset_session.json")

PROMPT_TMPL = """You are compacting an AI research agent's own tool-call history in the middle of a task,
to free up context window. Below are the OLDEST tool rounds (the most recent rounds are kept verbatim
elsewhere). Produce a compact structured note that lets the agent continue without re-reading. This is
RESEARCH work: preserve EXACT numbers, metrics, commit hashes, file paths, identifiers, and error text
verbatim — never round or paraphrase a number. Use exactly these sections (omit only if truly empty):

Goal:
Progress:
Decisions:
Next:
Files:
Findings:
Errors:

--- OLD TOOL ROUNDS ({n} rounds) ---
{body}
--- END OLD TOOL ROUNDS ---

Structured compaction note:"""


def load_rounds(archive_path):
    data = json.load(open(archive_path, encoding="utf-8"))
    msgs = data.get("messages", data) if isinstance(data, dict) else data
    calls = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        for tc in (m.get("tool_calls") or []):
            calls.append({
                "round": tc.get("round", 0),
                "tool": tc.get("tool"),
                "input": tc.get("input"),
                "output": tc.get("output", ""),
                "is_error": tc.get("is_error"),
                "sender": m.get("sender_name"),
            })
    calls.sort(key=lambda c: c["round"])
    return calls


def serialize(calls):
    blocks = []
    for c in calls:
        err = " [ERROR]" if c.get("is_error") else ""
        blocks.append(
            f"[round {c['round']}] TOOL {c['tool']}{err}\n"
            f"  input: {c.get('input')}\n"
            f"  output: {c.get('output')}"
        )
    return "\n\n".join(blocks)


def extract_numbers(text):
    """Research-critical exact tokens: numbers, metrics like 1/5, hex hashes, file paths."""
    fracs = set(re.findall(r"\b\d+/\d+\b", text))
    nums = set(re.findall(r"\b\d+\.\d+\b", text))          # decimals (metrics)
    ints = set(re.findall(r"\b\d{2,}\b", text))            # multi-digit ints
    hexes = set(re.findall(r"\b[0-9a-f]{7,40}\b", text))   # commit hashes
    paths = set(re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}", text))
    ids = fracs | nums | hexes | {p for p in paths if len(p) > 4}
    ids |= {i for i in ints if len(i) >= 3}
    return {i for i in ids if len(i) >= 2}


async def main():
    lm = LLMManager()
    if FLASH_ALIAS not in lm.providers:
        print(f"[FATAL] provider '{FLASH_ALIAS}' not loaded")
        return
    win = lm.get_context_window(FLASH_MODEL)

    calls = load_rounds(ARCHIVE)
    round_ids = sorted({c["round"] for c in calls})
    old_ids = set(round_ids[:-KEEP_RECENT]) if len(round_ids) > KEEP_RECENT else set(round_ids)
    old_calls = [c for c in calls if c["round"] in old_ids]

    body = serialize(old_calls)
    body_tok = lm.count_tokens(body, FLASH_MODEL)

    capped = False
    if body_tok > MAX_INPUT_TOKENS:
        # keep proportional prefix to stay under a realistic single-call size
        ratio = MAX_INPUT_TOKENS / body_tok
        body = body[: int(len(body) * ratio)]
        capped = True

    prompt = PROMPT_TMPL.format(n=len(old_ids), body=body)
    in_tok = lm.count_tokens(prompt, FLASH_MODEL)

    print("=" * 70)
    print("BENCH-2: ADR-033 compaction on REAL autoresearch tool-history")
    print(f"source        : archive/2026/07/{ARCHIVE.name}")
    print(f"flash window  : {win:,} tokens")
    print(f"tool calls    : {len(calls)} across {len(round_ids)} rounds")
    print(f"old (summ.)   : {len(old_ids)} rounds / {len(old_calls)} calls, keep_recent={KEEP_RECENT}")
    print(f"FULL old-hist : {body_tok:,} tokens  ({'CAPPED to ~%d for the call' % MAX_INPUT_TOKENS if capped else 'fits in one call'})")
    print(f"prompt tokens : ~{in_tok:,}   (flash window {win:,})")
    if in_tok > win:
        print(f"  ** prompt EXCEEDS flash window by {in_tok-win:,} tokens — single-shot compaction impossible **")
    print("=" * 70)

    ids_src = extract_numbers(serialize(old_calls))  # fidelity vs FULL old-history, not capped

    latencies = []
    last = None
    for i in range(TRIALS):
        t0 = time.perf_counter()
        try:
            meta = await lm.query(prompt, provider_alias=FLASH_ALIAS, return_metadata=True)
        except Exception as e:
            print(f"[trial {i+1}] ERROR: {type(e).__name__}: {e}")
            continue
        dt = time.perf_counter() - t0
        latencies.append(dt)
        last = meta
        resp = meta.get("response", "") if isinstance(meta, dict) else str(meta)
        tok = meta.get("tokens_used") if isinstance(meta, dict) else None
        print(f"[trial {i+1}] latency={dt:.2f}s  tokens_used={tok}  out_chars={len(resp)}")

    if not latencies:
        print("[FATAL] all trials failed (likely context-window overflow — a finding in itself)")
        return

    latencies.sort()
    median = latencies[len(latencies) // 2]
    resp = last.get("response", "")
    tokens_used = last.get("tokens_used") or 0
    out_tok = lm.count_tokens(resp, FLASH_MODEL)
    cost = compute_cost_usd(FLASH_ALIAS, in_tok, out_tok, model=FLASH_MODEL)

    ids_kept = {i for i in ids_src if i in resp}
    fid = (len(ids_kept) / len(ids_src) * 100) if ids_src else 0.0

    print("=" * 70)
    print(f"LATENCY   : median {median:.2f}s  (min {latencies[0]:.2f} / max {latencies[-1]:.2f}), ADR timeout 10s")
    print(f"TOKENS    : ~{in_tok:,} in + ~{out_tok:,} out  (provider tokens_used={tokens_used:,})")
    print(f"COST/call : ${cost:.5f}")
    print(f"FIDELITY  : {len(ids_kept)}/{len(ids_src)} exact numbers/hashes/paths survived ({fid:.0f}%)")
    lost = sorted(ids_src - ids_kept)[:25]
    print(f"  lost (sample): {lost}")
    print("=" * 70)
    print("SUMMARY OUTPUT (eyeball fidelity on research numbers):")
    print("-" * 70)
    print(resp)
    print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
