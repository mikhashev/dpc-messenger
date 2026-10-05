"""A/B harness for `preserve_reasoning`: the same agent task, flag off then on.

The card THE-MODEL-STARTS-EVERY-ROUND-WITHOUT-THE-REASONING-THAT-CHOSE-THE-TOOL
leaves two honest hypotheses open and says reading code settles neither: the model
holds the thread better when it is shown the notes that chose the last call, or it
anchors on its own early reasoning and stops revising when a tool result
contradicts it. This script is the instrument for that question; it decides
nothing by itself.

**It is not to be run unattended.** It needs the live llama-server child and a free
card, and it writes usage rows to the node ledger like any other agent run.

Per run it prints, for one task:

    rounds                  how many LLM calls the loop made
    budget_hits             rounds whose note count reached the alias budget
    silent_rounds           rounds that emitted no visible text at all
    repeat_opening_share    share of note-bearing rounds whose notes open with the
                            same ~80 normalised characters as an earlier round —
                            the re-derivation this change exists to remove
    note_tokens             reasoning tokens over the whole run
    wall_s                  wall clock, first call to last

Run it against the child the service already has, so no second copy of the model
is loaded. Read the port from the client log's own start line:

    grep "llama-server\[" ~/.dpc/logs/dpc-client.log | tail -1
    # llama-server[qwen3.8 27b] starting on :NNNNN (binary=..., n_ctx=...)

then, from `dpc-client/core`:

    uv run python -m tests.perf.run_reasoning_carry_ab \\
        --port NNNNN \\
        --gguf D:/models/qwen3.8-27b-Q4_K_M.gguf \\
        --agent-root ~/.dpc/agents/agent_johnny_f309700d \\
        --task "<one multi-step question>" \\
        --reasoning-budget 10000 --effort medium \\
        --out ab_reasoning_carry.json

`--task-file` takes the question from a file instead. `--repeat N` runs the pair N
times; the card asks for 5 to 10 tasks, so the honest shape is one invocation per
task and a comparison across them — a single pair is one sample of a sampled
process, not an answer.

The two runs differ in exactly one config key. Everything else — the child, the
alias, the tool set, the effort, the budget, the question — is held equal, and the
run that goes second pays no cold start because the child is already up. The
history bytes do change when the flag is on, so the first `on` round after an `off`
round re-prefills: expect one cold prefill per switch and read `wall_s` with that
in mind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider

OPENING_CHARS = 80


class _LiveChild:
    """The supervisor's surface, pointed at a child that is already serving.

    Starting our own would load a second copy of a 30 GB model onto a card that
    cannot hold two, so this harness never spawns one: it borrows the port the
    service's child is on and takes no slot discipline beyond its own serialisation
    (the runs are sequential by construction).
    """

    def __init__(self, port: int):
        self.port = port
        self.props: Dict[str, Any] = {"total_slots": 1}

    async def ensure_running(self) -> Dict[str, Any]:
        return self.props

    def call_slot(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def drain(self, timeout: float = 0.0) -> None:
        return None

    async def stop(self) -> None:
        return None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class _Rounds:
    """One row per LLM call: what the model said, thought, and was billed for."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def record(self, msg: Dict[str, Any], usage: Dict[str, Any], elapsed_s: float) -> None:
        thinking = str(msg.get("thinking") or "")
        self.rows.append({
            "content_chars": len(str(msg.get("content") or "").strip()),
            "tool_calls": len(msg.get("tool_calls") or []),
            "note_chars": len(thinking),
            "note_opening": _normalise(thinking)[:OPENING_CHARS],
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "elapsed_s": round(elapsed_s, 2),
        })

    def report(self, budget: Optional[int], wall_s: float) -> Dict[str, Any]:
        rounds = len(self.rows)
        # The note count is what the provider reported; where it reported nothing,
        # the characters it produced divided by four. Both are named in the row, so
        # a reader can tell an engine count from our estimate.
        note_tokens = sum(
            r["reasoning_tokens"] if r["reasoning_tokens"] is not None
            else r["note_chars"] // 4
            for r in self.rows
        )
        if budget:
            # 0.98 rather than equality: the server stops the trace at a token
            # boundary, so a capped round lands near the budget, not on it.
            hits = sum(
                1 for r in self.rows
                if (r["reasoning_tokens"] or r["completion_tokens"] or 0) >= budget * 0.98
            )
        else:
            hits = 0
        openings: Dict[str, int] = {}
        repeats = 0
        with_notes = 0
        for r in self.rows:
            opening = r["note_opening"]
            if not opening:
                continue
            with_notes += 1
            if opening in openings:
                repeats += 1
            openings[opening] = openings.get(opening, 0) + 1
        return {
            "rounds": rounds,
            "budget_hits": hits,
            "silent_rounds": sum(1 for r in self.rows if not r["content_chars"]),
            "rounds_with_notes": with_notes,
            "repeat_opening_share": round(repeats / with_notes, 3) if with_notes else None,
            "note_tokens": note_tokens,
            "wall_s": round(wall_s, 1),
            "per_round": self.rows,
        }


def _provider(alias: str, args: argparse.Namespace, preserve: bool) -> LlamaServerProvider:
    config: Dict[str, Any] = {
        "type": "llamacpp_server",
        "gguf_path": args.gguf,
        "preserve_reasoning": preserve,
    }
    if args.reasoning_budget:
        config["reasoning_budget_tokens"] = args.reasoning_budget
    if args.context_window:
        config["context_window"] = args.context_window
    provider = LlamaServerProvider(alias, config)
    provider.supervisor = _LiveChild(args.port)
    return provider


def _adapter(provider: LlamaServerProvider, alias: str, window: int) -> DpcLlmAdapter:
    manager = SimpleNamespace(
        providers={alias: provider},
        token_count_manager=None,
        agent_provider=alias,
        default_provider=alias,
        get_context_window=lambda _model: window,
        get_active_model_name=lambda: getattr(provider, "model", alias),
    )
    return DpcLlmAdapter(manager, provider_alias=alias, caller="ab-reasoning-carry")


async def _one_run(args: argparse.Namespace, task: str, preserve: bool) -> Dict[str, Any]:
    alias = "ab_local_qwen"
    provider = _provider(alias, args, preserve)
    window = args.context_window or 215040
    llm = _adapter(provider, alias, window)
    rounds = _Rounds()

    chat = llm.chat

    async def recording_chat(messages, **kwargs):
        started = time.perf_counter()
        msg, usage = await chat(messages, **kwargs)
        rounds.record(msg, usage, time.perf_counter() - started)
        return msg, usage

    llm.chat = recording_chat  # type: ignore[method-assign]

    agent_root = Path(args.agent_root).expanduser()
    tools = ToolRegistry(agent_root=agent_root)
    tools.set_context(ToolContext(
        agent_root=agent_root,
        current_task_id="ab-reasoning-carry",
        current_task_type="chat",
    ))

    wall_started = time.perf_counter()
    answer, usage, _trace = await run_llm_loop(
        messages=[{"role": "user", "content": task}],
        tools=tools,
        llm=llm,
        agent_root=agent_root,
        emit_progress=lambda *a, **k: None,
        task_id="ab-reasoning-carry",
        max_rounds=args.max_rounds,
        reasoning_effort=args.effort,
        context_window=window,
    )
    wall_s = time.perf_counter() - wall_started
    await provider.close()

    report = rounds.report(args.reasoning_budget, wall_s)
    report["preserve_reasoning"] = preserve
    report["answer"] = answer
    report["loop_usage"] = usage
    return report


HEADER = (
    f"{'flag':>6}  {'rounds':>6}  {'budget_hits':>11}  {'silent':>6}  "
    f"{'repeat_open':>11}  {'note_tokens':>11}  {'wall_s':>8}"
)


def _line(report: Dict[str, Any]) -> str:
    share = report["repeat_opening_share"]
    return (
        f"{'on' if report['preserve_reasoning'] else 'off':>6}  "
        f"{report['rounds']:>6}  {report['budget_hits']:>11}  "
        f"{report['silent_rounds']:>6}  "
        f"{'-' if share is None else f'{share:.3f}':>11}  "
        f"{report['note_tokens']:>11}  {report['wall_s']:>8.1f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, required=True,
                        help="port of the llama-server child that is already serving")
    parser.add_argument("--gguf", required=True, help="gguf_path of that child's alias")
    parser.add_argument("--agent-root", required=True,
                        help="the agent whose tools and sandbox the task runs in")
    parser.add_argument("--task", help="the question, inline")
    parser.add_argument("--task-file", type=Path, help="the question, from a file")
    parser.add_argument("--reasoning-budget", type=int, default=None,
                        help="reasoning_budget_tokens for both runs; also the budget_hits threshold")
    parser.add_argument("--context-window", type=int, default=None)
    parser.add_argument("--effort", default=None, help="reasoning effort word for both runs")
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--repeat", type=int, default=1, help="how many off/on pairs to run")
    parser.add_argument("--out", type=Path, default=None, help="write every row as JSON here")
    args = parser.parse_args()

    if bool(args.task) == bool(args.task_file):
        parser.error("pass exactly one of --task / --task-file")
    task = args.task or args.task_file.read_text(encoding="utf-8")

    reports: List[Dict[str, Any]] = []
    print(HEADER)
    for _pair in range(args.repeat):
        for preserve in (False, True):
            report = asyncio.run(_one_run(args, task, preserve))
            reports.append(report)
            print(_line(report), flush=True)

    if args.out:
        args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"\nrows written to {args.out}")
    print(
        "\nOne pair is one sample. The card asks for 5 to 10 multi-step tasks and "
        "compares rounds-to-completion and success, which no counter here can judge "
        "— read the answers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
