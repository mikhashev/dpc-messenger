"""
ADR-033 bench-4: is the latency floor the MODEL (reasoning) or the input?
Run the SAME per-round compaction on LOCAL fast models and compare to flash.
Tests the real lever (model choice, Mike's #3), not batching.

Run from dpc-client/core:  uv run python <scratchpad>/bench_compaction_local.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CORE = Path(r"C:\Users\mikha\Documents\dpc-messenger\dpc-client\core")
sys.path.insert(0, str(CORE))

from dpc_client_core.llm_manager import LLMManager

# candidate fast local models (first that loads wins); flash for contrast
LOCAL_CANDIDATES = ["lfm2.5_local", "ollama_text", "qwen3.5:9b"]
ARCHIVE = (Path.home() / ".dpc" / "conversations" / "group-b88b65076b85-dpc-project"
           / "archive" / "2026" / "07" / "2026-07-14T13-22-14_reset_session.json")

ROUND_PROMPT = """Compact this single tool round from an AI research agent into 1-3 lines.
Preserve EXACT numbers, metrics, commit hashes, file paths, and error text verbatim.
Drop boilerplate/markup/URL noise. Output only the compact note.

--- TOOL ROUND {r} ---
{body}
--- END ---
Compact note:"""


def load_rounds(p):
    data = json.load(open(p, encoding="utf-8"))
    msgs = data.get("messages", data) if isinstance(data, dict) else data
    by_round = {}
    for m in msgs:
        if isinstance(m, dict):
            for tc in (m.get("tool_calls") or []):
                by_round.setdefault(tc.get("round", 0), []).append(tc)
    return by_round


def serialize(calls):
    return "\n".join(
        f"TOOL {c.get('tool')}\n  input: {c.get('input')}\n  output: {c.get('output')}"
        for c in calls
    )


async def main():
    lm = LLMManager()
    local = next((a for a in LOCAL_CANDIDATES if a in lm.providers), None)
    if not local:
        print(f"[FATAL] none of {LOCAL_CANDIDATES} in providers: {list(lm.providers)}")
        return
    print(f"local model under test: {local} (model={lm.providers[local].model})")
    print(f"flash contrast        : deepseek_flash (reasoning)")
    print("=" * 70)

    by_round = load_rounds(ARCHIVE)
    # 3 representative rounds: small, medium, large (by input size)
    sized = sorted(((lm.count_tokens(serialize(v), "x"), r, v) for r, v in by_round.items()))
    picks = [sized[len(sized) // 6], sized[len(sized) // 2], sized[-3]]  # small / mid / large-ish

    for tok, r, calls in picks:
        body = serialize(calls)
        if tok > 30000:  # cap huge ones so a local model doesn't choke
            body = body[: int(len(body) * 30000 / tok)]
        prompt = ROUND_PROMPT.format(r=r, body=body)
        eff_tok = min(tok, 30000)
        for alias in (local, "deepseek_flash"):
            t0 = time.perf_counter()
            try:
                resp = await lm.query(prompt, provider_alias=alias)
                dt = time.perf_counter() - t0
                resp = (resp or "").strip()
                print(f"[round {r:>2} ~{eff_tok:>5}tok] {alias:<16} {dt:6.1f}s  out={len(resp):>4}ch  :: {resp[:90].replace(chr(10),' ')}")
            except Exception as e:
                dt = time.perf_counter() - t0
                print(f"[round {r:>2} ~{eff_tok:>5}tok] {alias:<16} {dt:6.1f}s  ERROR {type(e).__name__}: {e}")
        print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
