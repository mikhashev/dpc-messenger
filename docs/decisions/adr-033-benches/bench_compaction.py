"""
ADR-033 compaction bench: measure latency / cost / fidelity of a structural
LLM summary of a real ~20-round agent tool-history on the flash tier.

Run from dpc-client/core:  uv run python <scratchpad>/bench_compaction.py
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

# Structural template from ADR-033 (Goal/Progress/Decisions/Next/Files/Findings/Errors)
PROMPT_TMPL = """You are compacting an AI agent's own tool-call history in the middle of a task,
to free up context window. Below are the OLDEST tool rounds (the most recent rounds are kept
verbatim elsewhere and are NOT shown). Produce a compact structured note that lets the agent
continue without re-reading. Preserve EXACT file paths, identifiers, numbers, and error text
verbatim — do not round or paraphrase them. Use exactly these sections (omit a section only if
truly empty):

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


def pick_task(tools_jsonl: Path, target=20):
    """Group tool-call log by task_id, return the (task_id, rows) whose round-count
    is closest to `target` (a realistic single-task tool loop)."""
    rows = [json.loads(l) for l in tools_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_task = {}
    for r in rows:
        by_task.setdefault(r.get("task_id"), []).append(r)
    best = None
    for tid, rs in by_task.items():
        rounds = {r.get("round") for r in rs}
        n = len(rounds)
        score = abs(n - target)
        if best is None or score < best[0]:
            best = (score, tid, rs, n)
    return best[1], best[2], best[3]


def serialize_rounds(rows):
    """Render tool rows as a realistic agent tool-history text, one block per round."""
    rows = sorted(rows, key=lambda r: (r.get("round", 0), r.get("ts", "")))
    blocks = []
    for r in rows:
        rnd = r.get("round")
        tool = r.get("tool")
        args = r.get("args")
        res = r.get("result_preview", "")
        err = " [ERROR]" if r.get("is_error") else ""
        try:
            args_s = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_s = str(args)
        blocks.append(
            f"[round {rnd}] TOOL {tool}{err}\n  args: {args_s}\n  result: {res}"
        )
    return "\n\n".join(blocks)


def extract_identifiers(text):
    """Pull exact tokens whose loss would matter: file paths, dotted names, numbers, hashes."""
    paths = set(re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}", text))
    nums = set(re.findall(r"\b\d[\d.,]{1,}\b", text))
    hexes = set(re.findall(r"\b[0-9a-f]{7,40}\b", text))
    ids = {p for p in paths if len(p) > 3} | nums | hexes
    # drop trivially common ones
    return {i for i in ids if len(i) >= 3}


async def main():
    lm = LLMManager()
    if FLASH_ALIAS not in lm.providers:
        print(f"[FATAL] provider '{FLASH_ALIAS}' not loaded. Have: {list(lm.providers)}")
        return

    # Prefer scout (varied web/file tools, ~182 calls); fall back to any agent with a tools log.
    candidates = [
        Path.home() / ".dpc" / "agents" / "agent_scout_b427566f" / "logs" / "tools.jsonl",
        Path.home() / ".dpc" / "agents" / "agent_muse_59ab1256" / "logs" / "tools.jsonl",
        Path.home() / ".dpc" / "agents" / "agent_pulse_fcf439b6" / "logs" / "tools.jsonl",
    ]
    tools_jsonl = next((c for c in candidates if c.exists()), None)
    if not tools_jsonl:
        print("[FATAL] no tools.jsonl found")
        return

    task_id, rows, n_rounds = pick_task(tools_jsonl, target=20)
    rows = sorted(rows, key=lambda r: (r.get("round", 0), r.get("ts", "")))
    round_ids = sorted({r.get("round") for r in rows})
    old_round_ids = set(round_ids[:-KEEP_RECENT]) if len(round_ids) > KEEP_RECENT else set(round_ids)
    old_rows = [r for r in rows if r.get("round") in old_round_ids]

    body = serialize_rounds(old_rows)
    prompt = PROMPT_TMPL.format(n=len(old_round_ids), body=body)

    # Baseline: what today's prefix-truncation keeps (content[:200] per old round)
    baseline_chars = sum(min(200, len(r.get("result_preview", ""))) for r in old_rows)

    in_tok_est = lm.count_tokens(prompt, FLASH_MODEL)

    print("=" * 70)
    print(f"BENCH: ADR-033 structural compaction on flash tier")
    print(f"source        : {tools_jsonl.parent.parent.name}/logs/tools.jsonl")
    print(f"task_id       : {task_id}")
    print(f"rounds total  : {n_rounds}  (old={len(old_round_ids)} summarized, keep_recent={KEEP_RECENT} verbatim)")
    print(f"old-rows chars: {len(body):,}   prompt chars: {len(prompt):,}")
    print(f"input tokens  : ~{in_tok_est:,} (count_tokens estimate)")
    print(f"baseline(200-char trunc) kept chars: {baseline_chars:,}")
    print("=" * 70)

    ids_src = extract_identifiers(body)

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
        print("[FATAL] all trials failed")
        return

    latencies.sort()
    median = latencies[len(latencies) // 2]
    resp = last.get("response", "")
    tokens_used = last.get("tokens_used") or 0
    out_tok_est = lm.count_tokens(resp, FLASH_MODEL)
    # tokens_used is total; approximate prompt vs completion for cost
    prompt_tok = in_tok_est
    completion_tok = out_tok_est
    cost = compute_cost_usd(FLASH_ALIAS, prompt_tok, completion_tok, model=FLASH_MODEL)

    # Fidelity: how many source identifiers survived verbatim in the summary
    ids_kept = {i for i in ids_src if i in resp}
    fid = (len(ids_kept) / len(ids_src) * 100) if ids_src else 0.0

    print("=" * 70)
    print(f"LATENCY   : median {median:.2f}s  (min {latencies[0]:.2f} / max {latencies[-1]:.2f}), timeout budget 10s")
    print(f"TOKENS    : ~{prompt_tok:,} in + ~{completion_tok:,} out  (provider tokens_used={tokens_used:,})")
    print(f"COST/call : ${cost:.5f}  (deepseek-v4-flash cache-miss 0.14 / out 0.28 per 1M)")
    print(f"COMPRESS  : {len(body):,} src chars -> {len(resp):,} summary chars  ({len(resp)/max(1,len(body))*100:.1f}%)")
    print(f"FIDELITY  : {len(ids_kept)}/{len(ids_src)} exact identifiers survived ({fid:.0f}%)")
    missing = sorted(ids_src - ids_kept)[:15]
    print(f"  lost (sample): {missing}")
    print("=" * 70)
    print("SUMMARY OUTPUT (for eyeball fidelity):")
    print("-" * 70)
    print(resp)
    print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
