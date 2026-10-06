"""The long tier: code-reading tasks deep enough to reach the 2026-10-05 incident.

Written for the `preserve_reasoning` A/B (board card
`THE-MODEL-STARTS-EVERY-ROUND-WITHOUT-THE-REASONING-THAT-CHOSE-THE-TOOL`). The
incident it has to reproduce before the A/B can mean anything: agent Johnny,
alias `qwen3.8 27b`, a 26-round run whose prompt grew 67 177 -> 93 603 tokens,
seven rounds at the ~10k note budget, eleven of the twelve rounds from 14 on with
no visible text (per the card's 2026-10-06 entry, read from the provider's usage
rows; not re-read here). The easy and hard tiers finish in two or three rounds on
a ~10k-token prompt and cannot get there.

**What the agent reads.** A snapshot of this repository's own source, taken with
`git archive` at the commit the run starts at and copied into each task's
throwaway root under `src/`. Copying is the only way: the eval approver never
answers a sandbox-boundary question (`_harness/auto_approve.py`), and the
repository is outside every task root. `git archive` reads the commit, not the
working tree, so a teammate's uncommitted edit cannot reach a gold.

**Why these files.** Each task names three or four files of 20-110 k characters
and asks for values that sit far apart in them, so an answer needs many reads.
The loop caps one tool result at 15 000 characters (`TOOL_RESULT_CHAR_CAP`,
`loop.py`), about 4 k tokens of code, and the system prompt of a throwaway root
with 32 tools is ~10.3 k tokens (Observed: the 1-round tasks of GAIA run
`20260924-0348-t0-low` started at 10 329-10 448 prompt tokens). Reaching 60 k
therefore needs about thirteen full-cap reads still in the history. **Not
verified**: whether the model reads that much rather than searching — the
search tools would let it answer from a few lines. Step 0 of the runner is what
finds out; a long tier that stays shallow is reported as such, not as a result.
It did stay shallow: the first step 0 (2026-10-06) peaked at 16-24 k in 5-6 rounds.
`--seed-history` (`seed_history.py`) is the answer under
test — the incident's own loaded history in front of each task, rendered by
production; the task text then stands where the incident's trigger stood.

**Scoring.** Deterministic, as in the rest of `eval/loop`: each task ends with
`key=value` lines checked by `expect_fields` (whole-token, numbers compared as
numbers), sometimes beside `expect_ordered` / `expect_in_answer`. No model
judges anything.

**Attempt 3** (2026-10-06) adds three tasks after the eight: an audit of five
claims to verdict (`long-audit-claims`, kind `incident`), a question the snapshot
cannot settle (`long-control-unresolvable`) and a closable chain of dependent
lookups (`long-control-multistep`). Their `where`/`evidence` values are scored
by span (`expect_where`, `expect_places`), module paths by suffix
(`expect_paths`); every span is recomputed by `derive` like any other gold,
and since attempt 3 a span also covers the definition of every function the
derive names (`accepted_spans`). `long-control-burn` holds no tools and no gold:
it is measured on the burn axes only, never pass/fail.

**Golds.** Each gold was written by reading the code at `GOLD_COMMIT`, and each
task carries a `derive(src)` that recomputes the same values mechanically (AST or
an anchored regex) from whatever snapshot the run copied. `verify_golds` runs
every derive against the snapshot before the first task, and in `--dry-run`; a
gold the snapshot no longer supports stops the run (exit 2) instead of grading
the model against code that moved. Two derivations — a reading and a parse —
agreeing is what a gold here means.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# The commit every gold below was written and checked against (dev, 2026-10-06).
GOLD_COMMIT = "57ba6879489cd65a55565484433f8a399a34237d"

# Repository paths copied into each task root. The prefix up to `dpc_client_core`
# is dropped, so the agent sees `src/dpc_client_core/...`.
SNAPSHOT_PREFIX = "dpc-client/core/"
SNAPSHOT_PATHS = (
    "dpc-client/core/dpc_client_core/dpc_agent",
    "dpc-client/core/dpc_client_core/managers/agent_manager.py",
    "dpc-client/core/dpc_client_core/providers/base.py",
    "dpc-client/core/dpc_client_core/providers/llamacpp_server_provider.py",
)

# The tools a long-tier task may hold — a subset of the benchmark allow list.
# No web: this repository is public, and a model that read it on GitHub would be
# answering from another commit. No memory or skills: the root is empty.
LONG_TIER_TOOLS = frozenset({
    "read_file", "list_dir", "search_files", "search_in_file", "run_shell",
    "update_scratchpad", "list_my_tools",
})

PKG = "dpc_client_core"


# -- snapshot ------------------------------------------------------------------

def resolve_commit(repo_root: Path, ref: str = "HEAD") -> str:
    out = subprocess.run(["git", "rev-parse", ref], cwd=str(repo_root),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def snapshot_source(repo_root: Path, commit: str, dest: Path) -> Dict[str, Any]:
    """Extract SNAPSHOT_PATHS at `commit` into `dest`, prefix dropped.

    `git archive` reads the commit object, so the shared working tree — another
    developer's uncommitted edits included — never reaches the snapshot.
    """
    blob = subprocess.run(
        ["git", "archive", "--format=tar", commit, "--", *SNAPSHOT_PATHS],
        cwd=str(repo_root), capture_output=True, check=True,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    files = 0
    size = 0
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if not name.startswith(SNAPSHOT_PREFIX) or ".." in Path(name).parts:
                raise RuntimeError(f"unexpected archive member {name!r}")
            target = dest / name[len(SNAPSHOT_PREFIX):]
            target.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(member).read()
            target.write_bytes(data)
            files += 1
            size += len(data)
    return {"commit": commit, "paths": list(SNAPSHOT_PATHS), "files": files, "bytes": size}


def build_fixture(root: Path, snapshot: Path) -> None:
    """Copy an already-extracted snapshot into one task root, as `root/src`."""
    import shutil
    shutil.copytree(snapshot, root / "src", dirs_exist_ok=False)


# -- mechanical derivation helpers ---------------------------------------------

def _read(src: Path, rel: str) -> str:
    return (src / PKG / rel).read_text(encoding="utf-8")


def _eval_node(node: ast.AST) -> Any:
    # Constants such as `256 * 1024` — arithmetic on literals, no names.
    return eval(compile(ast.Expression(node), "<const>", "eval"), {"__builtins__": {}})


def _module_const(text: str, name: str) -> Any:
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return _eval_node(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            return _eval_node(node.value)
    raise LookupError(f"module constant {name} not found")


def _class_body(text: str, cls: str) -> List[ast.stmt]:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return node.body
    raise LookupError(f"class {cls} not found")


def _class_const(text: str, cls: str, name: str) -> Any:
    for node in _class_body(text, cls):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return _eval_node(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            return _eval_node(node.value)
    raise LookupError(f"{cls}.{name} not found")


def _func(text: str, name: str, cls: Optional[str] = None):
    body = _class_body(text, cls) if cls else ast.parse(text).body
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(f"function {cls + '.' if cls else ''}{name} not found")


def _default(text: str, func: str, arg: str, cls: Optional[str] = None) -> Any:
    fn = _func(text, func, cls)
    a = fn.args
    positional = a.posonlyargs + a.args
    for p, d in zip(positional[len(positional) - len(a.defaults):], a.defaults):
        if p.arg == arg:
            return _eval_node(d)
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        if p.arg == arg and d is not None:
            return _eval_node(d)
    raise LookupError(f"default of {func}({arg}) not found")


def _segment(text: str, func: str, cls: Optional[str] = None) -> str:
    return ast.get_source_segment(text, _func(text, func, cls)) or ""


def _one(pattern: str, text: str, flags: int = 0) -> re.Match:
    found = list(re.finditer(pattern, text, flags))
    if len(found) != 1:
        raise LookupError(f"expected one match of {pattern!r}, found {len(found)}")
    return found[0]


def _num(s: str):
    return float(s) if "." in s else int(s)


# -- the tasks -------------------------------------------------------------------
#
# Each derive returns {field: value}. How each gold was checked by reading, at
# GOLD_COMMIT, is written above its task; the derive is the second, mechanical
# check, run against every snapshot.

def _derive_guards(src: Path) -> Dict[str, Any]:
    loop = _read(src, "dpc_agent/loop.py")
    guards = _read(src, "dpc_agent/guards.py")
    agent = _read(src, "dpc_agent/agent.py")
    order = re.findall(r"hooks\.register\((\w+)\(", _segment(loop, "run_llm_loop"))
    return {
        "_order": order,
        "max_rounds_default": _class_const(agent, "AgentConfig", "max_rounds"),
        "tools_per_turn": _default(guards, "__init__", "max_per_turn", "ToolLimitGuard"),
        "research_consecutive": _default(guards, "__init__", "max_consecutive", "ResearchLimitGuard"),
        "research_silent_total": _default(guards, "__init__", "max_silent_total", "ResearchLimitGuard"),
        "duplicate_calls": _default(guards, "__init__", "max_duplicate_calls", "LoopGuard"),
        "budget_fraction": _default(guards, "__init__", "max_fraction", "BudgetLimitGuard"),
        "context_ratio": _default(guards, "__init__", "ratio", "ContextLimitGuard"),
    }


def _derive_compaction(src: Path) -> Dict[str, Any]:
    loop = _read(src, "dpc_agent/loop.py")
    ctx = _read(src, "dpc_agent/context.py")
    apply_src = _segment(ctx, "apply_compaction")
    off = _one(r"if round_idx > (\d+):\s*\n\s*return compact_tool_history\(messages, keep_recent=(\d+)\)",
               apply_src)
    init = _segment(ctx, "__init__", "CompactionState")
    deadband = float(_one(r"self\.threshold - ([\d.]+)\)", init).group(1))
    ladder = _one(r"keep = \{1: (\d+), 2: (\d+)\}\.get\(state\.fail_streak, state\.keep_recent\)",
                  apply_src)
    # The question asks for the loop's own `round_idx`, so the gold is the
    # smallest value passing `round_idx > N` only if the loop hands that very
    # variable to apply_compaction, before the round's LLM call.
    run = _segment(loop, "run_llm_loop")
    start = _one(r"\n    round_idx = (\d+)\n", run)
    increment = run.index("round_idx += 1")
    call = run.index("messages = await apply_compaction(")
    llm_call = run.index("# --- LLM call ---")
    if not (start.start() < increment < call < llm_call
            and "round_idx=round_idx," in run[call:llm_call]):
        raise LookupError("round_idx is not incremented, then handed to apply_compaction, "
                          "before the round's LLM call")
    return {
        # `round_idx = 0`, then `+= 1` before the first call: the first round is 1.
        "_first_round_idx": int(start.group(1)) + 1,
        "first_truncation_round_idx": int(off.group(1)) + 1,
        "keep_recent": int(off.group(2)),
        "default_threshold": float(_one(r'cfg\.get\("compaction_threshold", ([\d.]+)\)', init).group(1)),
        "release_at_half": round(0.5 - deadband, 6),
        "max_fails": int(_one(r"self\.max_fails = (\d+)", init).group(1)),
        "summary_timeout": _default(ctx, "compact_tool_history_llm", "timeout_s"),
        "keep_after_first_failure": int(ladder.group(1)),
        "keep_after_second_failure": int(ladder.group(2)),
    }


def _derive_caps(src: Path) -> Dict[str, Any]:
    core = _read(src, "dpc_agent/tools/core.py")
    loop = _read(src, "dpc_agent/loop.py")
    shell = _read(src, "dpc_agent/tools/shell.py")
    rf = _one(r"truncate_limit = (\d+) if os\.path\.isabs\(path\) else (\d+)",
              _segment(core, "read_file"))
    ext = _one(r"fallback_truncate=(\d+)\)", _segment(core, "extended_path_read"))
    return {
        "read_absolute": int(rf.group(1)),
        "read_relative": int(rf.group(2)),
        "read_extended": int(ext.group(1)),
        "loop_result_cap": _module_const(loop, "TOOL_RESULT_CHAR_CAP"),
        "shell_stream_cap": _module_const(shell, "MAX_OUTPUT"),
        "shell_timeout": _default(shell, "run_shell", "timeout"),
        "approval_seconds": _module_const(shell, "APPROVAL_TTL_SECONDS"),
        "script_read_limit": _module_const(shell, "_SCRIPT_READ_LIMIT"),
    }


def _derive_retrieval(src: Path) -> Dict[str, Any]:
    bm25 = _read(src, "dpc_agent/bm25_index.py")
    pipe = _read(src, "dpc_agent/indexing_pipeline.py")
    mem = _read(src, "dpc_agent/memory.py")
    keys = _read(src, "dpc_agent/index_keys.py")
    detect = _segment(bm25, "_detect_script")
    stops = _segment(bm25, "_compute_corpus_stops", "BM25Index")
    model = _module_const(mem, "DEFAULT_EMBEDDING_MODEL")
    return {
        "max_df": _class_const(bm25, "BM25Index", "CORPUS_MAX_DF"),
        "min_docs_for_corpus_stops": int(_one(r"if len\(texts\) < (\d+):", stops).group(1)),
        "corpus_version": _module_const(bm25, "CORPUS_VERSION"),
        "script_sample_chars": int(_one(r"sample = text\[:(\d+)\]", detect).group(1)),
        "bigram_share": float(_one(r"cjk_count > len\(sample\) \* ([\d.]+)", detect).group(1)),
        "texts_file": _module_const(bm25, "TEXTS_FILE"),
        "embedding_model": model,
        "embedding_dim": _module_const(mem, "KNOWN_EMBEDDING_DIMENSIONS")[model],
        "key_format": _module_const(keys, "KEY_FORMAT"),
        "debounce_seconds": _module_const(pipe, "_DEBOUNCE_WINDOW"),
    }


def _tool_entries(core: str) -> List[Dict[str, Any]]:
    fn = _func(core, "get_tools")
    rows = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ToolEntry":
            kw = {k.arg: k.value for k in node.keywords}
            # schedule_task's timeout is `_SCHEDULE_APPROVAL_TTL_SECONDS + 30`, a
            # name this evaluator does not resolve; only read_file's is asked.
            try:
                timeout = _eval_node(kw["timeout_sec"]) if "timeout_sec" in kw else None
            except NameError:
                timeout = None
            rows.append({
                "name": _eval_node(kw["name"]),
                "default_enabled": _eval_node(kw["default_enabled"]) if "default_enabled" in kw else None,
                "timeout_sec": timeout,
            })
    return rows


def _derive_tools(src: Path) -> Dict[str, Any]:
    core = _read(src, "dpc_agent/tools/core.py")
    reg = _read(src, "dpc_agent/tools/registry.py")
    rows = _tool_entries(core)
    return {
        "core_tools": len(rows),
        "default_on": sum(1 for r in rows if r["default_enabled"] is True),
        "default_off": sum(1 for r in rows if r["default_enabled"] is not True),
        "read_file_timeout": next(r["timeout_sec"] for r in rows if r["name"] == "read_file"),
        "registry_default_enabled": _class_const(reg, "ToolEntry", "default_enabled"),
        "registry_default_timeout": _class_const(reg, "ToolEntry", "timeout_sec"),
        "_off_names": sorted(r["name"] for r in rows if r["default_enabled"] is not True),
    }


def _derive_effort(src: Path) -> Dict[str, Any]:
    am = _read(src, "managers/agent_manager.py")
    agent = _read(src, "dpc_agent/agent.py")
    ctx = _read(src, "dpc_agent/context.py")
    seg = _segment(am, "_resolve_reasoning_effort", "DpcAgentManager")
    labels = re.findall(r'return [\w.]+, "([a-z-]+)"', seg)
    # Order of the returns as written: call, group, agent-config, exception, none.
    return {
        "first_source": labels[0],
        "second_source": labels[1],
        "third_source": labels[2],
        "error_source": labels[3],
        "nothing_source": labels[4],
        "agent_window_fallback": int(_one(r'get\("tokens_limit"\) or (\d+)', agent).group(1)),
        "compaction_window_fallback": int(_one(
            r'int\(cfg\.get\("context_window"\) or 0\) or (\d+)',
            _segment(ctx, "__init__", "CompactionState")).group(1)),
    }


def _derive_headroom(src: Path) -> Dict[str, Any]:
    agent = _read(src, "dpc_agent/agent.py")
    ctx = _read(src, "dpc_agent/context.py")
    guards = _read(src, "dpc_agent/guards.py")
    m = _one(r"max\(int\(_ctx_window \* ([\d.]+)\),\s*min\(CONTEXT_ROUND_RESERVE_TOKENS, "
             r"int\(_ctx_window \* ([\d.]+)\)\)\)", agent)
    lo, hi = float(m.group(1)), float(m.group(2))
    cap = _module_const(agent, "CONTEXT_ROUND_RESERVE_TOKENS")

    def reserve(w: int) -> int:
        return max(int(w * lo), min(cap, int(w * hi)))

    deadband = float(_one(r"self\.threshold - ([\d.]+)\)",
                          _segment(ctx, "__init__", "CompactionState")).group(1))
    ratio = _default(guards, "__init__", "ratio", "ContextLimitGuard")
    w = 215040
    return {
        "reserve_215040": reserve(w),
        "reserve_32768": reserve(32768),
        "compaction_starts": int(w * 0.5),
        "compaction_releases": int(round(w * (0.5 - deadband))),
        "guard_stops": int(round(w * ratio)),
    }


def _derive_notes(src: Path) -> Dict[str, Any]:
    loop = _read(src, "dpc_agent/loop.py")
    base = _read(src, "providers/base.py")
    prov = _read(src, "providers/llamacpp_server_provider.py")
    ctx = _read(src, "dpc_agent/context.py")
    run = _segment(loop, "run_llm_loop")
    conv = _segment(base, "anthropic_to_openai_messages")
    final_return = run.index("return clean_content, accumulated_usage, llm_trace")
    append_at = run.index("messages.append(assistant_turn)")
    preserve_at = conv.index("if preserve_reasoning:")
    tool_calls_at = conv.rindex("if tool_calls:", 0, preserve_at)
    return {
        "loop_key": _one(r'assistant_turn\["(\w+)"\] = _round_notes', run).group(1),
        "wire_field": _one(r'msg\["(\w+)"\] = kept', conv).group(1),
        "flag_default": _one(r'config\.get\("preserve_reasoning", (\w+)\)', prov).group(1).lower(),
        "echo_default": str(_default(base, "anthropic_to_openai_messages", "reasoning_echo")).lower(),
        # The final answering round returns before the append, so it is never stored.
        "final_round_stored": "false" if final_return < append_at else "true",
        # The preserve branch sits inside `if tool_calls:` — tool-call turns only.
        "tool_call_turns_only": "true" if tool_calls_at < preserve_at else "false",
        "notes_kept_rounds": int(_one(r"self\.keep_recent = (\d+)",
                                      _segment(ctx, "__init__", "CompactionState")).group(1)),
    }


# -- attempt 3: the incident-shaped task and Ark's control pair --------------------
#
# The eight tasks above ask for constants; a model re-plans them cheaply, and two
# step 0s on them did not burn (2026-10-06). The incident's task was an audit:
# claims to check against code, some wrong, some needing two files and a finding
# carried from an earlier step. The control pair splits the two readings of a
# burn: a question the snapshot cannot settle (rival hypothesis), and a closable
# chain of dependent lookups (the card's mechanism). A `where` value is scored by
# span, not by one line: `claim_N_where=<file>:<line>` passes when the line falls
# inside any accepted span of that file, each span recomputed from the snapshot.

def _span(text: str, func: str, cls: Optional[str] = None) -> List[int]:
    fn = _func(text, func, cls)
    return [fn.lineno, fn.end_lineno]


def _definition_span(text: str, qualname: str) -> List[int]:
    """`func` or `Class.func` -> [first line, last line] of its definition."""
    cls, _, fn = qualname.rpartition(".")
    return _span(text, fn, cls or None)


# Ark's rule (2026-10-06, after attempt 3): a `*_where` answer is also accepted
# when it falls inside the definition of a function the gold's own derivation
# names. Attempt 3's claim 5 pointed inside `forget_in_index` — the very function
# whose call the derive tests for — and was rejected because only the two
# delete-path spans were listed. Each derive therefore declares, per key, the
# functions it relies on (`_relies_on`), and the accepted spans are the explicit
# ones plus those functions' definition spans, both recomputed from the snapshot.
# Only that key widens. A function the derive merely searches inside for a call
# is declared too; the one exception is named where it is made.

def accepted_spans(src: Path, explicit: Dict[str, List[List[Any]]],
                   relies_on: Dict[str, List[List[str]]]) -> Dict[str, List[List[Any]]]:
    """Per key: the explicit spans, then the definition span of every function
    that key's derivation names ([rel, qualname]), duplicates dropped."""
    texts: Dict[str, str] = {}
    out: Dict[str, List[List[Any]]] = {}
    for key in list(explicit) + [k for k in relies_on if k not in explicit]:
        spans = [list(s) for s in explicit.get(key, [])]
        for rel, qualname in relies_on.get(key, []):
            if rel not in texts:
                texts[rel] = _read(src, rel)
            span = [rel, *_definition_span(texts[rel], qualname)]
            if span not in spans:
                spans.append(span)
        out[key] = spans
    return out


def _const_line(text: str, name: str) -> int:
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.lineno
    raise LookupError(f"module constant {name} not found")


def _verdict(survivors: int, parts: int) -> str:
    """holds when every part of the claim is true of the code, fixed when none
    is, partly in between."""
    return "holds" if survivors == parts else ("fixed" if survivors == 0 else "partly")


def _derive_audit(src: Path) -> Dict[str, Any]:
    bm25 = _read(src, "dpc_agent/bm25_index.py")
    am = _read(src, "managers/agent_manager.py")
    core = _read(src, "dpc_agent/tools/core.py")
    pipe = _read(src, "dpc_agent/indexing_pipeline.py")
    mem = _read(src, "dpc_agent/memory.py")
    B, AM = "dpc_agent/bm25_index.py", "managers/agent_manager.py"
    C, P, M = "dpc_agent/tools/core.py", "dpc_agent/indexing_pipeline.py", "dpc_agent/memory.py"

    # Claim 1 — "BM25 keeps no full text; every rebuild can only use the preview".
    # Three facts would each make it true: no texts file, `save` not writing it,
    # `_rebuild` not preferring the stored text over `meta["text"]`.
    _module_const(bm25, "TEXTS_FILE")
    saves = "TEXTS_FILE" in _segment(bm25, "save", "BM25Index")
    prefers = re.search(r't if t is not None else m\.get\("text"',
                        _segment(bm25, "_rebuild", "BM25Index")) is not None
    claim1 = _verdict((not saves) + (not prefers), 2)

    # Claim 2 — "sync_firewall_settings does nothing".
    fn = _func(am, "sync_firewall_settings", "DpcAgentManager")
    body = [s for s in fn.body if not (isinstance(s, ast.Expr)
                                       and isinstance(getattr(s, "value", None), ast.Constant)
                                       and isinstance(s.value.value, str))]
    claim2 = "holds" if body and all(isinstance(s, ast.Pass) for s in body) else "fixed"

    # Claim 3 — "a knowledge write adds new index rows beside the old ones".
    # Untrue per channel when write_file goes through replace_file_in_index and
    # that function removes the key's rows in the channel before adding to it.
    write_seg = _segment(core, "write_file")
    replace = _segment(pipe, "replace_file_in_index")
    if "replace_file_in_index(" not in write_seg:
        appends = 2
    else:
        appends = sum(
            not (0 <= replace.find(f"backend.{ch}.remove_by_source(") < replace.find(f"backend.{ch}.add("))
            for ch in ("vector", "text"))
    claim3 = _verdict(appends, 2)

    # Claim 4 — "knowledge/a/notes.md and knowledge/b/notes.md share one entry of
    # one _meta.json": write_file names the registry by basename in knowledge/,
    # and the registry is a dict keyed by that name.
    by_name = (re.search(r"filename = Path\(path\)\.name\b", write_seg) is not None
               and 'knowledge_dir = ctx.agent_root / "knowledge"' in write_seg
               and "record_write(knowledge_dir, filename" in write_seg)
    keyed = ("all_meta[filename] = " in _segment(mem, "write_file_meta")
             and "write_file_meta(knowledge_dir, filename" in _segment(mem, "record_write"))
    claim4 = "holds" if by_name and keyed else "fixed"

    # Claim 5 — "after repo_delete both the index rows and the _meta.json entry
    # survive". Index rows go when the delete path reaches forget_in_index; the
    # registry entry goes only if the delete path touches the registry at all.
    delete = _segment(core, "repo_delete") + _segment(core, "_forget_knowledge_files")
    index_kept = "forget_in_index" not in delete
    meta_kept = not any(tok in delete for tok in
                        ("_meta.json", "write_all_meta(", "write_file_meta(", "read_all_meta("))
    claim5 = _verdict(index_kept + meta_kept, 2)

    write_span = [C, *_span(core, "write_file")]
    explicit = {
        "claim_1_where": [[B, _const_line(bm25, "TEXTS_FILE"), _const_line(bm25, "TEXTS_FILE")],
                          [B, *_span(bm25, "_full_texts", "BM25Index")],
                          [B, *_span(bm25, "_rebuild", "BM25Index")],
                          [B, *_span(bm25, "save", "BM25Index")]],
        "claim_2_where": [[AM, *_span(am, "sync_firewall_settings", "DpcAgentManager")]],
        "claim_3_where": [[P, *_span(pipe, "replace_file_in_index")], write_span],
        "claim_4_where": [write_span, [M, *_span(mem, "record_write")],
                          [M, *_span(mem, "write_file_meta")]],
        "claim_5_where": [[C, *_span(core, "repo_delete")],
                          [C, *_span(core, "_forget_knowledge_files")]],
    }
    # Every function each claim's lines above name. Claim 1's docstring is in the
    # class, not in a named function, so where the invariant is *written* stays
    # outside: where it runs is `save` / `_rebuild`.
    relies_on = {
        "claim_1_where": [[B, "BM25Index.save"], [B, "BM25Index._rebuild"]],
        "claim_2_where": [[AM, "DpcAgentManager.sync_firewall_settings"]],
        "claim_3_where": [[C, "write_file"], [P, "replace_file_in_index"]],
        "claim_4_where": [[C, "write_file"], [M, "record_write"], [M, "write_file_meta"]],
        "claim_5_where": [[C, "repo_delete"], [C, "_forget_knowledge_files"],
                          [P, "forget_in_index"], [M, "read_all_meta"],
                          [M, "write_all_meta"], [M, "write_file_meta"]],
    }
    return {
        "claim_1": claim1, "claim_2": claim2, "claim_3": claim3, "claim_4": claim4,
        "claim_5": claim5,
        "_relies_on": relies_on,
        "_where": accepted_spans(src, explicit, relies_on),
    }


def _derive_unresolvable(src: Path) -> Dict[str, Any]:
    ctx = _read(src, "dpc_agent/context.py")
    loop = _read(src, "dpc_agent/loop.py")
    utils = _read(src, "dpc_agent/utils.py")
    init = _segment(ctx, "__init__", "CompactionState")
    run = _segment(loop, "run_llm_loop")
    # Open exactly when the threshold is read from the per-agent config with a
    # default, the loop takes that config from load_agent_config, and that reads
    # a file under the agent's home — which no snapshot carries.
    defaulted = re.search(r'cfg\.get\("compaction_threshold", [\d.]+\)', init) is not None
    from_config = "_compaction_cfg = load_agent_config(agent_root.name)" in run \
        and "CompactionState(_compaction_cfg)" in run
    from_home = "get_agent_config_path(agent_id)" in _segment(utils, "load_agent_config")
    lines = loop.splitlines()
    load_line = next(i for i, l in enumerate(lines, 1)
                     if "_compaction_cfg = load_agent_config(agent_root.name)" in l)
    state_line = next(i for i, l in enumerate(lines, 1)
                      if "_compaction_state = CompactionState(_compaction_cfg)" in l)
    U = "dpc_agent/utils.py"
    # Named by this derive and declared (Ark's rule, see `accepted_spans`):
    # CompactionState.__init__, load_agent_config, get_agent_config_path — the
    # last is where the path under ~/.dpc/agents is built. Not declared:
    # run_llm_loop, searched only for the two lines that read the config; its
    # definition is hundreds of lines, and accepting it would accept any line of
    # the loop as the place the answer is left open.
    relies_on = {"evidence_a": [["dpc_agent/context.py", "CompactionState.__init__"]],
                 "evidence_b": [[U, "load_agent_config"], [U, "get_agent_config_path"]]}
    spans = accepted_spans(src, {
        "evidence_a": [["dpc_agent/context.py", *_span(ctx, "__init__", "CompactionState")]],
        # The try/except around the read starts two lines above it.
        "evidence_b": [["dpc_agent/loop.py", load_line - 2, state_line]],
    }, relies_on)
    return {
        "settleable": "no" if defaulted and from_config and from_home else "yes",
        "_relies_on": relies_on,
        "_places": [spans["evidence_a"], spans["evidence_b"]],
    }


def _derive_multistep(src: Path) -> Dict[str, Any]:
    core = _read(src, "dpc_agent/tools/core.py")
    keys = _read(src, "dpc_agent/index_keys.py")
    pipe = _read(src, "dpc_agent/indexing_pipeline.py")
    write_seg = _segment(core, "write_file")
    key_fn = _one(r"_l5_key = (\w+)\(file_path, ", write_seg).group(1)
    key_mod = _one(rf"from \.\.(\w+) import {key_fn}\b", write_seg).group(1)
    replace_fn = _one(r"return (\w+)\(\s*ctx\.agent_root, file_path, provider", write_seg).group(1)
    replace_mod = _one(rf"from \.\.(\w+) import {replace_fn}\b", write_seg).group(1)
    if (key_mod, replace_mod) != ("index_keys", "indexing_pipeline"):
        raise LookupError(f"the chain moved: {key_mod}.{key_fn}, {replace_mod}.{replace_fn}")
    l5 = _segment(keys, key_fn)
    prefix = _module_const(keys, "L5_PREFIX")
    if "_relative_posix(path, knowledge_dir)" in l5 \
            and "path.relative_to(base).as_posix()" in _segment(keys, "_relative_posix"):
        index_key = f"{prefix}/a/notes.md"
    else:
        index_key = f"{prefix}/notes.md"
    replace = _segment(pipe, replace_fn)
    if "document_meta(" not in replace or "record_file_hashes(" not in replace:
        raise LookupError(f"{replace_fn} no longer builds its row or records its hash")
    if '"text": excerpt' not in _segment(pipe, "document_meta"):
        raise LookupError("document_meta's preview is no longer document_fields' excerpt")
    hashes = _segment(pipe, "record_file_hashes")
    return {
        "key_function": key_fn,
        "key_module": f"dpc_agent/{key_mod}.py",
        "index_key": index_key,
        "replace_function": replace_fn,
        "preview_chars": int(_one(r"body\[:(\d+)\]", _segment(pipe, "document_fields")).group(1)),
        "hashes_file": _one(r'index_dir / "([\w.]+)"', hashes).group(1),
        "hashes_key": _one(r'doc\.get\("(\w+)"\)', hashes).group(1),
        "meta_module": "dpc_agent/"
                       + _one(r"from \.(\w+) import read_meta, write_meta", hashes).group(1) + ".py",
        "hash_hex_chars": int(_one(r"hexdigest\(\)\[:(\d+)\]", _segment(pipe, "doc_hash")).group(1)),
    }


def _fields_block(keys: List[str]) -> str:
    return "\n".join(f"{k}=<value>" for k in keys)


def tasks_for(root: Path) -> List[Dict[str, Any]]:
    """The long-tier tasks for one task root whose snapshot is at `root/src`."""
    s = root / "src" / PKG
    a = s / "dpc_agent"
    rule = ("Read the code itself to answer; do not guess from names. "
            "Give plain numbers (no thousands separators), true/false for yes/no "
            "questions, and end your answer with exactly these lines, one per value:\n")
    tasks: List[Dict[str, Any]] = []

    # Gold checked by reading: loop.py run_llm_loop registers RoundLimitGuard,
    # ToolLimitGuard, ResearchLimitGuard, LoopGuard, BudgetLimitGuard,
    # ContextLimitGuard in that order (loop.py:1057-1064 at GOLD_COMMIT);
    # guards.py __init__ defaults: max_per_turn 25 (:54), max_consecutive 15 and
    # max_silent_total 60 (:153), max_duplicate_calls 5 (:299), max_fraction 0.5
    # (:387), ratio 0.95 (:448); AgentConfig.max_rounds 200 (agent.py:110).
    keys = ["max_rounds_default", "tools_per_turn", "research_consecutive",
            "research_silent_total", "duplicate_calls", "budget_fraction", "context_ratio"]
    tasks.append({
        "id": "long-guard-chain",
        "files": ["dpc_agent/loop.py", "dpc_agent/guards.py", "dpc_agent/agent.py"],
        "derive": _derive_guards,
        "prompt": (
            f"The source of an agent loop is under {s}. In {a / 'loop.py'}, the function "
            f"run_llm_loop registers a series of guards. Name every guard class it registers, "
            f"in registration order, as one comma-separated line. Then, from {a / 'guards.py'} "
            f"and {a / 'agent.py'}, give: the default max_rounds in AgentConfig; ToolLimitGuard's "
            f"default per-turn tool limit; ResearchLimitGuard's default consecutive limit and its "
            f"default silent total; LoopGuard's default duplicate-call limit; BudgetLimitGuard's "
            f"default budget fraction; ContextLimitGuard's default ratio. " + rule + _fields_block(keys)
        ),
        "gold": {"max_rounds_default": 200, "tools_per_turn": 25, "research_consecutive": 15,
                 "research_silent_total": 60, "duplicate_calls": 5, "budget_fraction": 0.5,
                 "context_ratio": 0.95},
        "gold_order": ["RoundLimitGuard", "ToolLimitGuard", "ResearchLimitGuard", "LoopGuard",
                       "BudgetLimitGuard", "ContextLimitGuard"],
    })

    # Gold checked by reading context.py at GOLD_COMMIT: apply_compaction with the
    # toggle off does `if round_idx > 8: return compact_tool_history(messages,
    # keep_recent=6)` (:1329-1330) -> round_idx 9, keep 6. run_llm_loop sets
    # `round_idx = 0` and increments it at the top of each round, before
    # apply_compaction and the LLM call (loop.py:1099-1144 at HEAD 67d0ab41), so the
    # first round is round_idx 1 and round_idx 9 is the ninth call. Until
    # 2026-10-06 the question asked for "the first round number"; the step-0 run
    # answered 10, a reading the wording allowed (rounds counted from 0, or "after
    # round 9"). It now asks for the variable's value. CompactionState: threshold
    # default 0.8, release = threshold - 0.2 (-> 0.3 at 0.5), max_fails 3
    # (:1300-1306); compact_tool_history_llm timeout_s=180.0 (:1220); ladder
    # `{1: 12, 2: 18}` below UNDER_PRESSURE (:1384).
    keys = ["first_truncation_round_idx", "keep_recent", "default_threshold", "release_at_half",
            "max_fails", "summary_timeout", "keep_after_first_failure", "keep_after_second_failure"]
    tasks.append({
        "id": "long-compaction-ladder",
        "files": ["dpc_agent/loop.py", "dpc_agent/context.py"],
        "derive": _derive_compaction,
        "prompt": (
            f"The source of an agent loop is under {s}. Read {a / 'loop.py'} and "
            f"{a / 'context.py'} and answer about tool-history compaction. With compaction "
            f"disabled: the value of run_llm_loop's own variable round_idx (as the loop sets "
            f"and increments it, not a count of your own) in the first round whose LLM call "
            f"is sent with old tool history truncated, and how many recent tool rounds that "
            f"truncation keeps. With it enabled: the default "
            f"threshold; the usage ratio at which compaction stops again when the threshold is "
            f"0.5; how many consecutive summariser failures stop the summariser being called; "
            f"the summariser call's timeout in seconds; and, below the under-pressure ratio, how "
            f"many rounds are kept verbatim after the first and after the second consecutive "
            f"failure. " + rule + _fields_block(keys)
        ),
        "gold": {"first_truncation_round_idx": 9, "keep_recent": 6, "default_threshold": 0.8,
                 "release_at_half": 0.3, "max_fails": 3, "summary_timeout": 180,
                 "keep_after_first_failure": 12, "keep_after_second_failure": 18},
    })

    # Gold checked by reading at GOLD_COMMIT: tools/core.py read_file
    # `truncate_limit = 100000 if os.path.isabs(path) else 50000` (:243),
    # extended_path_read fallback_truncate=100000 (:1731); loop.py
    # TOOL_RESULT_CHAR_CAP = 15000 (:339); tools/shell.py MAX_OUTPUT = 50_000 (:50,
    # used per stream at :2431), run_shell(timeout=120) (:2513),
    # APPROVAL_TTL_SECONDS = 60 (:2248), _SCRIPT_READ_LIMIT = 256 * 1024 (:1949).
    keys = ["read_absolute", "read_relative", "read_extended", "loop_result_cap",
            "shell_stream_cap", "shell_timeout", "approval_seconds", "script_read_limit"]
    tasks.append({
        "id": "long-size-caps",
        "files": ["dpc_agent/tools/core.py", "dpc_agent/loop.py", "dpc_agent/tools/shell.py"],
        "derive": _derive_caps,
        "prompt": (
            f"The source of an agent's tools is under {a / 'tools'}, and its loop is "
            f"{a / 'loop.py'}. Find every size or time limit on what a tool hands back: "
            f"read_file's truncation for an absolute path and for a relative one, "
            f"extended_path_read's truncation, the loop's cap on any single tool result in "
            f"characters, run_shell's per-stream output cap in characters, run_shell's default "
            f"timeout in seconds, how long a shell approval request waits in seconds, and the "
            f"largest script file (in bytes) the shell gate will read. " + rule + _fields_block(keys)
        ),
        "gold": {"read_absolute": 100000, "read_relative": 50000, "read_extended": 100000,
                 "loop_result_cap": 15000, "shell_stream_cap": 50000, "shell_timeout": 120,
                 "approval_seconds": 60, "script_read_limit": 262144},
    })

    # Gold checked by reading at GOLD_COMMIT: bm25_index.py CORPUS_MAX_DF = 0.8
    # (BM25Index), `if len(texts) < 5: return frozenset()` in _compute_corpus_stops,
    # CORPUS_VERSION = 2, _detect_script samples text[:500] and compares against
    # len(sample) * 0.15, TEXTS_FILE = "bm25_texts.json"; memory.py
    # DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3" (:361) with dimension 1024 (:364);
    # index_keys.py KEY_FORMAT = "layer_addressed_v6" (:56); indexing_pipeline.py
    # _DEBOUNCE_WINDOW = 0.1 (:31).
    keys = ["max_df", "min_docs_for_corpus_stops", "corpus_version", "script_sample_chars",
            "bigram_share", "texts_file", "embedding_model", "embedding_dim", "key_format",
            "debounce_seconds"]
    tasks.append({
        "id": "long-retrieval-constants",
        "files": ["dpc_agent/bm25_index.py", "dpc_agent/indexing_pipeline.py",
                  "dpc_agent/memory.py", "dpc_agent/index_keys.py"],
        "derive": _derive_retrieval,
        "prompt": (
            f"The retrieval code of an agent is under {a}: {a / 'bm25_index.py'}, "
            f"{a / 'indexing_pipeline.py'}, {a / 'memory.py'} and {a / 'index_keys.py'}. Give: "
            f"the document-frequency share above which a word becomes a corpus stop word; the "
            f"fewest documents for which corpus stop words are computed at all; the BM25 texts "
            f"corpus version; how many leading characters script detection samples; the share "
            f"of CJK/Arabic/Thai characters above which bigram tokenisation is used; the name of "
            f"the BM25 full-texts file; the default embedding model name; its embedding "
            f"dimension; the index key format string; and the indexing debounce window in "
            f"seconds. " + rule + _fields_block(keys)
        ),
        "gold": {"max_df": 0.8, "min_docs_for_corpus_stops": 5, "corpus_version": 2,
                 "script_sample_chars": 500, "bigram_share": 0.15, "texts_file": "bm25_texts.json",
                 "embedding_model": "BAAI/bge-m3", "embedding_dim": 1024,
                 "key_format": "layer_addressed_v6", "debounce_seconds": 0.1},
    })

    # Gold checked by reading tools/core.py get_tools() at GOLD_COMMIT (:2095-2645),
    # entry by entry: 20 ToolEntry, 13 default_enabled=True, 7 False — repo_delete,
    # write_file, update_identity, deduplicate_identity, schedule_task,
    # register_task_type, unregister_task_type; read_file timeout_sec=30 (:2124);
    # registry.py ToolEntry defaults timeout_sec 120 (:309), default_enabled False (:311).
    keys = ["core_tools", "default_on", "default_off", "read_file_timeout",
            "registry_default_enabled", "registry_default_timeout"]
    off = ["repo_delete", "write_file", "update_identity", "deduplicate_identity",
           "schedule_task", "register_task_type", "unregister_task_type"]
    tasks.append({
        "id": "long-core-tool-defaults",
        "files": ["dpc_agent/tools/core.py", "dpc_agent/tools/registry.py"],
        "derive": _derive_tools,
        "prompt": (
            f"In {a / 'tools' / 'core.py'}, the function get_tools() returns a list of "
            f"ToolEntry objects. How many entries does it return, how many are enabled by "
            f"default and how many are not? Name every tool that is not enabled by default. "
            f"What is read_file's timeout in seconds? Then, from {a / 'tools' / 'registry.py'}, "
            f"what are ToolEntry's own defaults for default_enabled and for timeout_sec? "
            + rule + _fields_block(keys)
        ),
        "gold": {"core_tools": 20, "default_on": 13, "default_off": 7, "read_file_timeout": 30,
                 "registry_default_enabled": False, "registry_default_timeout": 120},
        "gold_names": off,
    })

    # Gold checked by reading at GOLD_COMMIT: agent_manager.py
    # _resolve_reasoning_effort (:938-972) returns "call" for a per-call value, then
    # "group" for a group room's own effort, then "agent-config", "exception" when
    # something raised, "none" when nobody answered; agent.py
    # `(session_state or {}).get("tokens_limit") or 204800` (:470); context.py
    # CompactionState `int(cfg.get("context_window") or 0) or 204800` (:1304).
    keys = ["first_source", "second_source", "third_source", "error_source",
            "nothing_source", "agent_window_fallback", "compaction_window_fallback"]
    tasks.append({
        "id": "long-effort-precedence",
        "files": ["managers/agent_manager.py", "dpc_agent/agent.py", "dpc_agent/context.py"],
        "derive": _derive_effort,
        "prompt": (
            f"In {s / 'managers' / 'agent_manager.py'}, the method _resolve_reasoning_effort "
            f"returns an effort and the label of the branch that decided it. Give the labels of "
            f"the first, second and third branches tried, the label returned when something "
            f"raised, and the label returned when nothing answered. Then: in "
            f"{a / 'agent.py'}, DpcAgent.process uses a context window when the session state "
            f"carries none — which number? And in {a / 'context.py'}, which window does "
            f"CompactionState fall back to when the config names none? "
            + rule + _fields_block(keys)
        ),
        "gold": {"first_source": "call", "second_source": "group", "third_source": "agent-config",
                 "error_source": "exception", "nothing_source": "none",
                 "agent_window_fallback": 204800, "compaction_window_fallback": 204800},
    })

    # Gold checked by reading and computing at GOLD_COMMIT: agent.py
    # CONTEXT_ROUND_RESERVE_TOKENS = 16384 (:48), reserve = max(int(w*0.05),
    # min(16384, int(w*0.2))) (:497-498): w=215040 -> max(10752, 16384) = 16384;
    # w=32768 -> max(1638, min(16384, 6553)) = 6553. context.py: starts at
    # threshold 0.5 -> 107520, releases at 0.5-0.2 -> 0.3*215040 = 64512.
    # guards.py ContextLimitGuard ratio 0.95 -> 204288.
    keys = ["reserve_215040", "reserve_32768", "compaction_starts", "compaction_releases",
            "guard_stops"]
    tasks.append({
        "id": "long-headroom-arithmetic",
        "files": ["dpc_agent/agent.py", "dpc_agent/context.py", "dpc_agent/guards.py"],
        "derive": _derive_headroom,
        "prompt": (
            f"Work out an agent's context headroom from its code: {a / 'agent.py'}, "
            f"{a / 'context.py'} and {a / 'guards.py'}. DpcAgent.process refuses a call when "
            f"the remaining window is below a reserve: compute that reserve in tokens for a "
            f"215040-token window and for a 32768-token window. With compaction enabled at "
            f"threshold 0.5 on a 215040-token window, at what prompt size in tokens does "
            f"compaction start, and below what size does it stop? At what prompt size in "
            f"tokens does ContextLimitGuard's default ratio stop the loop on that window? "
            + rule + _fields_block(keys)
        ),
        "gold": {"reserve_215040": 16384, "reserve_32768": 6553, "compaction_starts": 107520,
                 "compaction_releases": 64512, "guard_stops": 204288},
    })

    # Gold checked by reading at GOLD_COMMIT: loop.py stores `assistant_turn["thinking"]
    # = _round_notes` (:1321) and returns the final answer before
    # `messages.append(assistant_turn)` (:1272 vs :1322); providers/base.py writes
    # `msg["reasoning_content"] = kept` under `if preserve_reasoning:` inside
    # `if tool_calls:` (:592-597), with reasoning_echo=False as the signature default
    # (:492); llamacpp_server_provider.py `config.get("preserve_reasoning", False)`
    # (:223); context.py CompactionState.keep_recent = 6, the tail on which notes stay.
    keys = ["loop_key", "wire_field", "flag_default", "echo_default", "final_round_stored",
            "tool_call_turns_only", "notes_kept_rounds"]
    tasks.append({
        "id": "long-notes-carry-path",
        "files": ["dpc_agent/loop.py", "dpc_agent/llm_adapter.py", "providers/base.py",
                  "providers/llamacpp_server_provider.py", "dpc_agent/context.py"],
        "derive": _derive_notes,
        "prompt": (
            f"Trace how a tool round's reasoning notes travel from the agent loop to a local "
            f"llama-server, through {a / 'loop.py'}, {a / 'llm_adapter.py'}, "
            f"{s / 'providers' / 'base.py'} and {s / 'providers' / 'llamacpp_server_provider.py'}. "
            f"Give: the key under which the loop stores a round's notes on the assistant turn; "
            f"the field name they are sent under on the wire; the default of the provider's "
            f"preserve_reasoning setting; the default of the converter's reasoning_echo "
            f"parameter; whether the final answering round is stored in the history; whether "
            f"notes are sent only on turns that carry tool calls; and, from {a / 'context.py'}, "
            f"how many recent tool rounds keep their notes when compaction runs. "
            + rule + _fields_block(keys)
        ),
        "gold": {"loop_key": "thinking", "wire_field": "reasoning_content", "flag_default": "false",
                 "echo_default": "false", "final_round_stored": "false",
                 "tool_call_turns_only": "true", "notes_kept_rounds": 6},
    })

    # -- attempt 3 ---------------------------------------------------------------
    where_rule = (f"Give each place as a path relative to {s}, a colon and one line number, "
                  f"for example dpc_agent/loop.py:120. ")

    # Golds checked by reading at a5e49e7b (dpc_client_core as at GOLD_COMMIT):
    # 1 fixed, REFUTED by the code — bm25_index.py TEXTS_FILE (:73), written by save
    #   (:298, :329); _rebuild prefers the stored text over meta["text"] (:163).
    # 2 holds — agent_manager.py sync_firewall_settings is docstring + pass (:710-712).
    # 3 fixed — write_file -> replace_file_in_index (core.py :367, :379), which removes
    #   both channels' rows (:187-188) before adding (:194-195).
    # 4 holds — write_file keys the registry by Path(path).name in knowledge/ (:358-361);
    #   memory.py record_write -> write_file_meta, all_meta[filename] (:239, :199-202).
    # 5 partly — repo_delete -> _forget_knowledge_files -> forget_in_index drops the rows
    #   (core.py :407-470); nothing on that path touches the _meta.json of claim 4.
    claims = ["claim_1", "claim_2", "claim_3", "claim_4", "claim_5"]
    keys = [k for c in claims for k in (c, f"{c}_where")]
    tasks.append({
        "id": "long-audit-claims",
        "kind": "incident",
        "files": ["dpc_agent/bm25_index.py", "managers/agent_manager.py", "dpc_agent/tools/core.py",
                  "dpc_agent/indexing_pipeline.py", "dpc_agent/memory.py",
                  "dpc_agent/index_keys.py"],
        "derive": _derive_audit,
        "prompt": (
            f"An audit of an agent's memory code makes five claims about the source under {s}. "
            f"Check every claim against the code itself: do not trust the audit, and do not "
            f"guess from names. Answer each claim with exactly one word: holds (the code at "
            f"this snapshot does what the claim says), fixed (it does not — whether the code "
            f"was repaired or the claim was never true), or partly (one part of the claim is "
            f"true of the code and another part is not). For each claim also name the place in "
            f"the code that decides your verdict. " + where_rule +
            f"\nClaim 1. BM25Index in dpc_agent/bm25_index.py keeps no full text of its "
            f"documents, so every rebuild of its corpus can only use each row's "
            f"500-character preview, meta[\"text\"]."
            f"\nClaim 2. DpcAgentManager.sync_firewall_settings in managers/agent_manager.py "
            f"does nothing: it re-reads no settings."
            f"\nClaim 3. When write_file in dpc_agent/tools/core.py writes a relative path "
            f"under knowledge/ for an agent whose retrieval index exists and which has an "
            f"embedding provider, the file's new index rows are added beside its earlier "
            f"rows, which stay in the index."
            f"\nClaim 4. When write_file writes knowledge/a/notes.md and then "
            f"knowledge/b/notes.md, both writes update one and the same entry in one and the "
            f"same _meta.json file."
            f"\nClaim 5. When repo_delete in dpc_agent/tools/core.py deletes a file under "
            f"knowledge/ for an agent whose retrieval index exists, two kinds of state about "
            f"that file survive the call: its rows in the retrieval index, and its entry in "
            f"_meta.json.\n"
            f"End your answer with exactly these lines, one per value:\n" + _fields_block(keys)
        ),
        "gold": {"claim_1": "fixed", "claim_2": "holds", "claim_3": "fixed", "claim_4": "holds",
                 "claim_5": "partly"},
        "gold_where": {
            "claim_1_where": [["dpc_agent/bm25_index.py", 73, 73], ["dpc_agent/bm25_index.py", 123, 156],
                              ["dpc_agent/bm25_index.py", 158, 165], ["dpc_agent/bm25_index.py", 281, 336]],
            "claim_2_where": [["managers/agent_manager.py", 710, 712]],
            "claim_3_where": [["dpc_agent/indexing_pipeline.py", 170, 199],
                              ["dpc_agent/tools/core.py", 330, 399]],
            "claim_4_where": [["dpc_agent/tools/core.py", 330, 399], ["dpc_agent/memory.py", 218, 243],
                              ["dpc_agent/memory.py", 199, 202]],
            # The last four: Ark's rule — the definitions of forget_in_index (whose
            # call decides the rows half) and of the three registry functions the
            # derive checks the delete path never reaches (the _meta.json half).
            "claim_5_where": [["dpc_agent/tools/core.py", 435, 470],
                              ["dpc_agent/tools/core.py", 407, 432],
                              ["dpc_agent/indexing_pipeline.py", 202, 216],
                              ["dpc_agent/memory.py", 170, 185], ["dpc_agent/memory.py", 188, 190],
                              ["dpc_agent/memory.py", 199, 202]],
        },
        "gold_relies_on": {
            "claim_1_where": [["dpc_agent/bm25_index.py", "BM25Index.save"],
                              ["dpc_agent/bm25_index.py", "BM25Index._rebuild"]],
            "claim_2_where": [["managers/agent_manager.py", "DpcAgentManager.sync_firewall_settings"]],
            "claim_3_where": [["dpc_agent/tools/core.py", "write_file"],
                              ["dpc_agent/indexing_pipeline.py", "replace_file_in_index"]],
            "claim_4_where": [["dpc_agent/tools/core.py", "write_file"],
                              ["dpc_agent/memory.py", "record_write"],
                              ["dpc_agent/memory.py", "write_file_meta"]],
            "claim_5_where": [["dpc_agent/tools/core.py", "repo_delete"],
                              ["dpc_agent/tools/core.py", "_forget_knowledge_files"],
                              ["dpc_agent/indexing_pipeline.py", "forget_in_index"],
                              ["dpc_agent/memory.py", "read_all_meta"],
                              ["dpc_agent/memory.py", "write_all_meta"],
                              ["dpc_agent/memory.py", "write_file_meta"]],
        },
    })

    # Gold checked by reading at a5e49e7b: CompactionState.__init__ takes
    # cfg.get("compaction_threshold", 0.8) (context.py :1296-1308); the loop builds cfg
    # from load_agent_config(agent_root.name) (loop.py :1074-1097), which reads
    # ~/.dpc/agents/<id>/config.json (utils.py :676-688) — in no snapshot. Insufficient
    # evidence, not conflicting: 0.8 would give 172032, a config of 0.5 gives 107520.
    keys = ["settleable", "value", "evidence_a", "evidence_b"]
    tasks.append({
        "id": "long-control-unresolvable",
        "kind": "control-unresolvable",
        "files": ["dpc_agent/context.py", "dpc_agent/loop.py", "dpc_agent/utils.py"],
        "derive": _derive_unresolvable,
        "prompt": (
            f"The source of an agent loop is under {s}. An agent whose root directory is "
            f"named agent_7f3e runs with tool-history compaction enabled, on a model whose "
            f"context window the loop resolves as 215040 tokens. At what prompt size in "
            f"tokens does compaction first fire on that agent's runs? Answer from this code "
            f"snapshot alone; nothing outside {s} is available to you. If the code settles "
            f"it, write settleable=yes and the number as value. If it cannot be settled from "
            f"this code, write settleable=no and value=none, and name the two places in the "
            f"code between which the answer is left open, as evidence_a and evidence_b (in "
            f"either order). " + where_rule +
            "End your answer with exactly these lines, one per value:\n" + _fields_block(keys)
        ),
        "gold": {"settleable": "no"},
        "gold_places": {
            "keys": ["evidence_a", "evidence_b"],
            # utils.py 671-673 is get_agent_config_path (Ark's rule: named by the derive).
            "places": [[["dpc_agent/context.py", 1296, 1308]],
                       [["dpc_agent/loop.py", 1074, 1097], ["dpc_agent/utils.py", 676, 688],
                        ["dpc_agent/utils.py", 671, 673]]],
        },
        "gold_relies_on": {
            "evidence_a": [["dpc_agent/context.py", "CompactionState.__init__"]],
            "evidence_b": [["dpc_agent/utils.py", "load_agent_config"],
                           ["dpc_agent/utils.py", "get_agent_config_path"]],
        },
    })

    # Gold checked by reading at a5e49e7b, each step from the previous answer: core.py
    # write_file -> l5_key from ..index_keys (:369, :374) -> "knowledge/" + relative posix
    # (index_keys.py :58, :88-98) -> replace_file_in_index (core.py :367, :379) ->
    # document_meta's "text" = document_fields' body[:500] (indexing_pipeline.py :93,
    # :115) -> record_file_hashes: index_meta.json, "file_hashes", read_meta/write_meta
    # from .index_meta (:156-159) -> doc_hash hexdigest()[:16] (:98).
    keys = ["key_function", "key_module", "index_key", "replace_function", "preview_chars",
            "hashes_file", "hashes_key", "meta_module", "hash_hex_chars"]
    tasks.append({
        "id": "long-control-multistep",
        "kind": "control-multistep",
        "files": ["dpc_agent/tools/core.py", "dpc_agent/index_keys.py",
                  "dpc_agent/indexing_pipeline.py", "dpc_agent/index_meta.py"],
        "derive": _derive_multistep,
        "prompt": (
            f"Follow one chain through the source under {s}; each step starts where the "
            f"previous one ended. Read the code; do not guess from names. "
            f"(1) In {a / 'tools' / 'core.py'}, when write_file writes a relative path under "
            f"knowledge/: which function computes the key the file is indexed under, and in "
            f"which module is that function defined? "
            f"(2) In that module: which key does that function return for the file "
            f"knowledge/a/notes.md of an agent whose knowledge directory is the knowledge/ "
            f"directory of its root? "
            f"(3) Back in write_file: to which function is that key handed to update the index? "
            f"(4) In that function's module: at most how many characters of a document's body "
            f"does the stored row's preview, meta[\"text\"], hold? "
            f"(5) In which file, under which top-level key, does that function record the "
            f"document's hash, and which module provides the functions that read and write "
            f"that file? "
            f"(6) How many hexadecimal characters long is that hash? "
            f"Give modules as paths relative to {s}, for example dpc_agent/loop.py. "
            f"End your answer with exactly these lines, one per value:\n" + _fields_block(keys)
        ),
        "gold": {"key_function": "l5_key", "index_key": "knowledge/a/notes.md",
                 "replace_function": "replace_file_in_index", "preview_chars": 500,
                 "hashes_file": "index_meta.json", "hashes_key": "file_hashes",
                 "hash_hex_chars": 16},
        "gold_paths": {"key_module": "dpc_agent/index_keys.py",
                       "meta_module": "dpc_agent/index_meta.py"},
    })

    # The burn control: can this instrument produce a capped round at all? No tools
    # (the runner builds it a firewall allowing none), no gold, never pass/fail —
    # measured on the burn axes only (`round_metrics.burn_control`). The chain has
    # 48 dependent steps, each an exact square of a number below 10007 reduced mod
    # 10007, the rule picked by the value mod 4; no closed form and no cycle, and
    # one slip spoils every later value, so a careful model checks every step.
    burn_keys = ["x_12", "x_24", "x_36", "x_48", "s"]
    tasks.append({
        "id": "long-control-burn",
        "kind": "control-burn",
        "scored": False,
        "tools": frozenset(),
        "files": [],
        "prompt": (
            "No files, tools or lookups are available for this task; everything you need is "
            "below. Work it out by careful reasoning and exact arithmetic. Every step depends "
            "on the one before it, so a single slip changes every later value: check each "
            "step before moving on.\n"
            "Let p = 10007 (a prime). Define integers x_0, x_1, ..., x_48 with "
            "0 <= x_n < p by x_0 = 2026 and, for n = 0, 1, ..., 47, with r = x_n mod 4:\n"
            "- if r = 0: x_(n+1) = (x_n^2 + n) mod p\n"
            "- if r = 1: x_(n+1) = (3*x_n^2 + 7*n + 1) mod p\n"
            "- if r = 2: x_(n+1) = (x_n^2 + x_n + 5) mod p\n"
            "- if r = 3: x_(n+1) = (x_n * (n+2)^2 + 11) mod p\n"
            "Let s be how many of x_0, ..., x_48 are greater than 5003.\n"
            "End your answer with exactly these lines, one per value:\n"
            + _fields_block(burn_keys)
        ),
    })

    for t in tasks:
        t.setdefault("kind", "baseline")
        if "gold" not in t:
            t["tools_needed"] = []
            continue
        t["expect_fields"] = dict(t["gold"])
        for gold_key, expect_key in (("gold_where", "expect_where"), ("gold_paths", "expect_paths"),
                                     ("gold_places", "expect_places")):
            if gold_key in t:
                t[expect_key] = t[gold_key]
        if "gold_order" in t:
            t["expect_ordered"] = list(t["gold_order"])
        if "gold_names" in t:
            t["expect_in_answer"] = list(t["gold_names"])
        t["tools_needed"] = sorted({"read_file"})
    return tasks


def task_tools(task: Dict[str, Any]) -> frozenset:
    """The tools one long task may hold: its own set where it names one (the burn
    control: none), else the tier's."""
    tools = task.get("tools")
    return LONG_TIER_TOOLS if tools is None else frozenset(tools)


def is_scored(task: Dict[str, Any]) -> bool:
    """False for a task measured only on the burn axes, never pass/fail."""
    return task.get("scored", True) is not False


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).lower() == str(b).lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a) == str(b)


def verify_golds(snapshot: Path) -> List[Dict[str, Any]]:
    """Every gold against a mechanical derivation from `snapshot`. One row per value.

    `snapshot` is the extracted tree (the one holding `dpc_client_core/`). A
    derive that raises is a row with ok=False and the error — absent, not wrong.
    """
    rows: List[Dict[str, Any]] = []
    fake_root = snapshot.parent / "_gold_check_root"
    for t in tasks_for(fake_root):
        if "derive" not in t:
            continue  # the burn control has no gold to derive
        try:
            derived = t["derive"](snapshot)
        except Exception as exc:
            rows.append({"task": t["id"], "key": "*", "gold": None, "derived": None,
                         "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for k, g in t["gold"].items():
            d = derived.get(k)
            rows.append({"task": t["id"], "key": k, "gold": g, "derived": d, "ok": _same(g, d)})
        if "gold_order" in t:
            rows.append({"task": t["id"], "key": "order", "gold": t["gold_order"],
                         "derived": derived.get("_order"),
                         "ok": t["gold_order"] == derived.get("_order")})
        if "gold_names" in t:
            rows.append({"task": t["id"], "key": "off_names", "gold": sorted(t["gold_names"]),
                         "derived": derived.get("_off_names"),
                         "ok": sorted(t["gold_names"]) == derived.get("_off_names")})
        for k, g in (t.get("gold_where") or {}).items():
            d = (derived.get("_where") or {}).get(k)
            rows.append({"task": t["id"], "key": k, "gold": g, "derived": d, "ok": g == d})
        for k, g in (t.get("gold_relies_on") or {}).items():
            d = (derived.get("_relies_on") or {}).get(k)
            rows.append({"task": t["id"], "key": f"relies_on:{k}", "gold": g, "derived": d,
                         "ok": g == d})
        for k, g in (t.get("gold_paths") or {}).items():
            rows.append({"task": t["id"], "key": k, "gold": g, "derived": derived.get(k),
                         "ok": g == derived.get(k)})
        if "gold_places" in t:
            g = t["gold_places"]["places"]
            rows.append({"task": t["id"], "key": "places", "gold": g,
                         "derived": derived.get("_places"), "ok": g == derived.get("_places")})
        for rel in t["files"]:
            p = snapshot / PKG / rel
            rows.append({"task": t["id"], "key": f"file:{rel}", "gold": "exists",
                         "derived": p.stat().st_size if p.is_file() else None,
                         "ok": p.is_file()})
    return rows
