"""
ADR-033 bench-5: is flash-WITHOUT-reasoning competitive with a local model?
Same rounds, three configs: flash(thinking ON) vs flash(thinking OFF) vs lfm2.5 local.
Answers the open question Mike raised (reasoning is a flag).

Run from dpc-client/core:  uv run python <scratchpad>/bench_compaction_nothink.py
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


async def timed(lm, alias, prompt):
    t0 = time.perf_counter()
    try:
        resp = await lm.query(prompt, provider_alias=alias)
        dt = time.perf_counter() - t0
        return dt, (resp or "").strip(), None
    except Exception as e:
        return time.perf_counter() - t0, "", f"{type(e).__name__}: {e}"


async def main():
    lm = LLMManager()
    local = next((a for a in LOCAL_CANDIDATES if a in lm.providers), None)
    flash = lm.providers.get("deepseek_flash")
    if not flash:
        print("[FATAL] deepseek_flash not loaded"); return

    by_round = load_rounds(ARCHIVE)
    sized = sorted(((lm.count_tokens(serialize(v), "x"), r, v) for r, v in by_round.items()))
    picks = [sized[len(sized) // 6], sized[len(sized) // 2], sized[-3]]

    print(f"local: {local} | flash: deepseek_flash (thinking on/off toggle)")
    print("=" * 78)
    for tok, r, calls in picks:
        body = serialize(calls)
        eff = min(tok, 30000)
        if tok > 30000:
            body = body[: int(len(body) * 30000 / tok)]
        prompt = ROUND_PROMPT.format(r=r, body=body)

        # flash thinking ON
        flash.thinking_enabled = True
        d_on, o_on, e_on = await timed(lm, "deepseek_flash", prompt)
        # flash thinking OFF
        flash.thinking_enabled = False
        d_off, o_off, e_off = await timed(lm, "deepseek_flash", prompt)
        flash.thinking_enabled = True  # restore
        # local
        d_loc, o_loc, e_loc = (await timed(lm, local, prompt)) if local else (0, "", "no local")

        print(f"round {r:>2}  ~{eff:>5} tok in")
        print(f"  flash thinking-ON : {d_on:6.1f}s  out={len(o_on):>4}ch  {('ERR '+e_on) if e_on else ''}")
        print(f"  flash thinking-OFF: {d_off:6.1f}s  out={len(o_off):>4}ch  {('ERR '+e_off) if e_off else ''}")
        print(f"  {local:<17}: {d_loc:6.1f}s  out={len(o_loc):>4}ch  {('ERR '+e_loc) if e_loc else ''}")
        print(f"    OFF sample :: {o_off[:120].replace(chr(10),' ')}")
        print("-" * 78)


if __name__ == "__main__":
    asyncio.run(main())
