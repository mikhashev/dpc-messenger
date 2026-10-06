"""Seed a long-tier task with a conversation the agent had already loaded.

Step 0 of 2026-10-06 (`long-qwen3.8_27b-step0-20261006-114852.json`) did not
reproduce the incident: both off-arm tasks finished in 5-6 rounds on a 16-24 k
peak prompt. The incident's round 1 was already 67 177 tokens, and part of that
was the group history the agent loaded with the task — 43 records, 42 of them
rendered as history turns ("History turns for reader Johnny: 42 total, 1
assistant", `dpc-client.log.1`, 2026-10-05 17:54:53). A fresh root starts near
10 k. This module puts such a history in front of a long-tier task.

**Rendered by production, not here.** The seed reaches `DpcAgent.process`
through the arguments production passes (`agent_manager.py`, the call with
`conversation_monitor=` and `reader_identity=`): a stand-in monitor serving the
seed's records, the reader's identity, and the trigger's id. `process` then runs
`select_prior_history` and `build_llm_messages`, whose `derive_history_role`
turns the reader's own records into assistant turns and everything else into
user turns, each prefixed by `history_prefix`. Nothing in this file formats a
turn.

**The task replaces the trigger.** The seed's last record is the message that
started the incident's task. Here its body is the code-reading task; its index,
time and sender stay, so the prompt has the incident's shape — 42 history turns,
then one user turn — and the agent is not handed a question the task does not
score (keeping both would put two user turns at the end, and the model may answer
the chat instead).

**Privacy.** A seed is private chat. It lives outside the repository
(`~/.dpc/eval-results/loop/seeds/`), is never copied into a report, and a seeded
run's report drops or digests every field the model writes (see
`redact_seeded_outcome` and `withhold_seed_text`). Only the seed's path, sha256,
record counts and source ranges leave this module.

**Engine scale.** The step-0 floor is in engine prompt tokens; chars / 4 reads
this conversation low (incident: 41 012 estimated, 67 177 counted). The probe
counts with `llama-tokenize` beside the pinned `llama-server` (`vocab_only`, no
weights, no GPU), and falls back to the incident's ratio, labelled calibrated.

**Deep seeds** (`deepen`) prepend whole records from earlier sessions, newest
first, to a target count. Production would not have loaded them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# The trigger's stand-in id: no real record carries it, so `select_prior_history`
# drops exactly this one record from the history and nothing else.
TASK_RECORD_ID = "eval-long-task"

# A string in a seeded report that shares this many consecutive characters with
# any seed record is withheld. Long enough that a file name or a short code
# token shared by the chat and the task does not trip it.
WITHHOLD_WINDOW = 40


class SeedError(SystemExit):
    """A seed file the harness refuses — raised before anything is loaded."""


def load_seed(path: Path, reader_name: str) -> Dict[str, Any]:
    """Read a seed file: its records, the reader's identity, sha256 and count.

    The file is `{"reader": {...} | "readers": [...], "messages": [records]}`,
    records as `history.json` stores them. The reader's identity
    (`agent_id`, `display_name`, `node_id` — the dict `agent_manager` builds) has
    to be in the file: it is what makes the reader's own records assistant turns,
    and guessing it would render a different prompt in silence.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise SeedError(f"--seed-history: no file at {path}")
    raw = path.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SeedError(f"--seed-history: {path} is not JSON ({exc})")
    records = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(records, list) or len(records) < 2:
        raise SeedError(f"--seed-history: {path} holds no records list of two or more "
                        "(history turns, then the trigger the task replaces)")
    deepening = doc.get("deepening") if isinstance(doc.get("deepening"), dict) else None
    prepended = int((deepening or {}).get("prepended_records") or 0)
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise SeedError(f"--seed-history: record {i} is not an object")
        if i < prepended:
            # Records from before the incident's session carry no index: the
            # trigger's index bounds the history (`select_prior_history` drops
            # every integer index at or above it), and theirs ran from 1 again.
            if rec.get("msg_index") is not None:
                raise SeedError(f"--seed-history: prepended record {i} keeps a msg_index; "
                                "an earlier session's index would collide with the trigger's")
        elif not isinstance(rec.get("msg_index"), int):
            raise SeedError(f"--seed-history: record {i} carries no integer msg_index")
    if prepended >= len(records) - 1:
        raise SeedError(f"--seed-history: {path} declares {prepended} prepended records of "
                        f"{len(records)}; the incident's own records must follow them")
    readers = doc.get("readers") or ([doc["reader"]] if doc.get("reader") else [])
    reader = next((r for r in readers if r.get("display_name") == reader_name), None)
    if reader is None:
        raise SeedError(f"--seed-history: {path} carries no identity for reader "
                        f"{reader_name!r} (has: {[r.get('display_name') for r in readers]})")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "messages": records,
        "reader": {"agent_id": str(reader.get("agent_id") or ""),
                   "display_name": reader_name,
                   "node_id": str(reader.get("node_id") or "")},
        "deepening": deepening,
    }


# The keys of a deep seed's `deepening` block a report may carry: paths, hashes,
# ranges and counts. Anything else in the block stays in the file.
DEEPENING_REPORT_KEYS = ("base_seed", "prepended_records", "outside_incident_history",
                         "skipped_by_renderer", "sources", "target_engine_tokens",
                         "engine_method", "production_would_not_have_loaded_these")


def seed_record(seed: Dict[str, Any]) -> Dict[str, Any]:
    """What a report may say about a seed: where, which bytes, how many records,
    and for a deep seed which files and index ranges the extra records came from."""
    out = {"path": seed["path"], "sha256": seed["sha256"],
           "messages": len(seed["messages"]),
           "history_records": len(seed["messages"]) - 1,
           "reader": seed["reader"]["display_name"],
           "trigger": "replaced by the task text (index, time and sender kept)"}
    deep = seed.get("deepening")
    if deep:
        out["deepening"] = {k: copy.deepcopy(deep[k]) for k in DEEPENING_REPORT_KEYS if k in deep}
    return out


class SeedMonitor:
    """The one `ConversationMonitor` method `DpcAgent.process` reads history from.

    `process` also hands the monitor to `ToolContext`, where only the
    `archive` tools read it; none of them is in `LONG_TIER_TOOLS`.
    """

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records

    def get_message_history(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._records)


def seeded_process_kwargs(seed: Dict[str, Any], task_prompt: str) -> Dict[str, Any]:
    """The production arguments that carry the seed into `DpcAgent.process`."""
    *history, trigger = seed["messages"]
    task_record = {
        "id": TASK_RECORD_ID,
        "msg_index": trigger["msg_index"],
        "timestamp": trigger.get("timestamp"),
        "sender_name": trigger.get("sender_name"),
        "sender_type": trigger.get("sender_type"),
        "role": "user",
        "content": task_prompt,
    }
    return {
        "conversation_monitor": SeedMonitor([*history, task_record]),
        "reader_identity": dict(seed["reader"]),
        "trigger_message_id": TASK_RECORD_ID,
    }


def roles_agree_for_group_and_direct(seed: Dict[str, Any]) -> List[int]:
    """msg_index of every record whose role differs between a group id and a 1:1 id.

    `build_llm_messages` passes `is_group` from the task id; the eval's ids are
    not `group-…` (a group id would also switch off `run_shell` through the group
    tool restriction, changing the tool set against unseeded runs). The flag
    only matters for records without `sender_type`; this is the check that the
    seed has none whose role it would change.
    """
    from dpc_client_core.dpc_agent.context import derive_history_role
    return [r["msg_index"] for r in seed["messages"][:-1]
            if derive_history_role(r, seed["reader"], True)
            != derive_history_role(r, seed["reader"], False)]


# -- engine scale -----------------------------------------------------------------

# The incident's round 1 by two counts, both read from `dpc-client.log.1`:
# production's estimator (chars/4, tool schemas excluded) and the engine's
# `prompt_tokens` (52 tool schemas included).
INCIDENT_ROUND1_ESTIMATED = 41_012
INCIDENT_ROUND1_COUNTED = 67_177
CALIBRATION = INCIDENT_ROUND1_COUNTED / INCIDENT_ROUND1_ESTIMATED

METHOD_TOKENIZER = "tokenizer"
METHOD_CALIBRATED = "calibrated"


class Tokenizer:
    """The model's own tokenizer through `llama-tokenize`, vocabulary only.

    llama.cpp's tokenize tool opens the GGUF with `vocab_only` and skips every
    tensor; CUDA is hidden so the process cannot open a GPU context. Text goes
    in on stdin, so nothing private is written to disk.
    """

    def __init__(self, binary: Path, gguf: Path, timeout_s: float = 120.0) -> None:
        self.binary = Path(binary)
        self.gguf = Path(gguf)
        self.timeout_s = timeout_s

    def describe(self) -> str:
        return f"{self.binary.name} --vocab-only on {self.gguf.name}"

    def count(self, text: str) -> int:
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")
        out = subprocess.run(
            [str(self.binary), "-m", str(self.gguf), "--stdin", "--ids", "--show-count",
             "--no-bos", "--log-disable"],
            input=text.encode("utf-8"), capture_output=True, env=env, timeout=self.timeout_s)
        found = re.search(rb"Total number of tokens: (\d+)", out.stdout)
        if out.returncode != 0 or found is None:
            raise RuntimeError(f"{self.binary.name} exited {out.returncode} without a count")
        return int(found.group(1))


def tokenizer_beside(server_binary: Optional[Path], gguf: Optional[Path]) -> Optional[Tokenizer]:
    """`llama-tokenize` from the directory of the pinned `llama-server`, or None."""
    if not server_binary or not gguf or not Path(gguf).is_file():
        return None
    for name in ("llama-tokenize.exe", "llama-tokenize"):
        candidate = Path(server_binary).parent / name
        if candidate.is_file():
            return Tokenizer(candidate, Path(gguf))
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(str(b.get("text", "")) for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    return "" if content is None else str(content)


def chatml_turn(role: str, content: Any) -> str:
    return f"<|im_start|>{role}\n{_text_of(content)}<|im_end|>\n"


def render_for_count(messages: Sequence[Dict[str, Any]], tools: Optional[list]) -> str:
    """The request as the engine's chat template lays it out, near enough to count.

    ChatML framing per message and the tool schemas as one JSON line each inside
    `<tools>` in the system turn; the template's own fixed wording around the
    tools is not reproduced.
    """
    parts: List[str] = []
    for i, m in enumerate(messages):
        text = _text_of(m.get("content"))
        if m.get("tool_calls"):
            text += "\n" + json.dumps(m["tool_calls"], ensure_ascii=False)
        if i == 0 and m.get("role") == "system" and tools:
            text += "\n\n<tools>\n" + "\n".join(json.dumps(t, ensure_ascii=False)
                                                for t in tools) + "\n</tools>"
        parts.append(chatml_turn(str(m.get("role") or "user"), text))
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


def engine_scale(request_est: int, messages: Sequence[Dict[str, Any]], tools: Optional[list],
                 tokenizer: Optional[Tokenizer]) -> Dict[str, Any]:
    """Round 1 in engine tokens: counted when a tokenizer is at hand, else calibrated.

    The calibrated figure applies the incident's ratio to the request estimate
    without tool schemas, the quantity the ratio was taken over; the incident's
    52 schemas are inside the ratio, so with fewer schemas it reads high.
    """
    if tokenizer is not None:
        return {"engine_tokens": tokenizer.count(render_for_count(messages, tools)),
                "engine_method": METHOD_TOKENIZER, "engine_counter": tokenizer.describe()}
    return {"engine_tokens": round(request_est * CALIBRATION),
            "engine_method": METHOD_CALIBRATED,
            "engine_counter": (f"chars/4 request estimate x {INCIDENT_ROUND1_COUNTED}/"
                               f"{INCIDENT_ROUND1_ESTIMATED} (the incident's ratio; not measured)")}


# -- deep seeds -------------------------------------------------------------------

def renders(record: Dict[str, Any], reader: Dict[str, str]) -> bool:
    """Whether `build_llm_messages` would emit a turn for this record at all."""
    from dpc_client_core.dpc_agent.context import derive_history_role
    return bool(record.get("content")) and derive_history_role(record, reader, False) is not None


def record_cost_text(record: Dict[str, Any], reader: Dict[str, str]) -> str:
    """One history record as the engine reads it: role framing, prefix, body."""
    from dpc_client_core.dpc_agent.context import derive_history_role, history_prefix
    role = derive_history_role(record, reader, False)
    return chatml_turn(role, history_prefix(record) + str(record.get("content") or ""))


def prepend_form(record: Dict[str, Any]) -> Dict[str, Any]:
    """An earlier session's record as a deep seed carries it: whole, its own index
    moved to `source_msg_index` (that session counted from 1 again, and
    `select_prior_history` drops every index at or above the trigger's)."""
    out = copy.deepcopy(record)
    out["source_msg_index"] = out.get("msg_index")
    out["msg_index"] = None
    return out


def deepen(base_records: List[Dict[str, Any]],
           sessions: Sequence[Tuple[str, List[Dict[str, Any]]]],
           reader: Dict[str, str], need_tokens: int,
           cost: Callable[[Dict[str, Any]], int]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Whole records from `sessions` (oldest session first), taken newest first
    backwards until their cost reaches `need_tokens`, then put in front of
    `base_records` in chronological order. The base is not touched.

    Returns the new records list and the provenance: per source the index range
    taken, counts, and how many taken records the renderer skips anyway.
    """
    taken: List[Tuple[str, Dict[str, Any]]] = []
    total = 0
    skipped = 0
    for label, records in reversed(list(sessions)):
        for rec in reversed(records):
            if total >= need_tokens:
                break
            taken.append((label, rec))
            if renders(rec, reader):
                total += cost(prepend_form(rec))
            else:
                skipped += 1
        if total >= need_tokens:
            break
    taken.reverse()
    sources: List[Dict[str, Any]] = []
    for label, rec in taken:
        if not sources or sources[-1]["file"] != label:
            sources.append({"file": label, "records": 0,
                            "msg_index_from": rec.get("msg_index"), "msg_index_to": None})
        sources[-1]["records"] += 1
        sources[-1]["msg_index_to"] = rec.get("msg_index")
    prepended = [prepend_form(rec) for _, rec in taken]
    return [*prepended, *copy.deepcopy(base_records)], {
        "prepended_records": len(prepended),
        "outside_incident_history": len(prepended),
        "skipped_by_renderer": skipped,
        "prepended_cost_tokens": total,
        "reached": total >= need_tokens,
        "sources": sources,
    }


# -- the round-1 depth probe ------------------------------------------------------

class _NoModel:
    """Stands where the LLMManager stands; holds no provider, loads nothing."""

    providers: Dict[str, Any] = {}
    token_count_manager = None

    def get_context_window(self, model: str) -> int:  # pragma: no cover - not reached
        return 0


def _known_width(agent_config: dict):
    """`factory._derive_embedding_metadata`'s not-cached branch, without the load."""
    from dpc_client_core.dpc_agent.memory import KNOWN_EMBEDDING_DIMENSIONS
    from dpc_client_core.dpc_agent.memory_config import get_memory_config
    name = get_memory_config(agent_config).embedding_model
    return name, KNOWN_EMBEDDING_DIMENSIONS.get(name, 384)


async def probe_round1(root: Path, *, firewall, profile: str, alias: Optional[str],
                       task_prompt: str, seed: Dict[str, Any], conversation_id: str,
                       session_state: Optional[Dict[str, Any]],
                       reasoning_effort: Optional[str],
                       tokenizer: Optional[Tokenizer] = None) -> Dict[str, Any]:
    """Build the run's round-1 request through production code and measure it.

    A real `DpcAgent` on a throwaway root, with the adapter's `chat` replaced by
    a recorder that returns a final answer at once: `process` renders the seed,
    the loop calls `chat` once with the messages and tool schemas the model
    would get, and nothing is sent anywhere. Two sizes come back: production's
    estimator (`estimate_tokens`, chars / 4, the figure behind "Context size:
    estimated N", plus the same over the tool schemas), and the engine-scale
    figure the floor is compared with (`engine_scale`).
    """
    from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
    from dpc_client_core.dpc_agent.utils import estimate_tokens

    agent = DpcAgent(llm_manager=_NoModel(), config=AgentConfig(max_rounds=1),
                     agent_root=root, firewall=firewall, firewall_profile=profile,
                     provider_alias=alias)
    seen: Dict[str, Any] = {}

    async def record_first_call(messages, **kwargs):
        if "messages" not in seen:
            seen["messages"] = copy.deepcopy(messages)
            seen["tools"] = kwargs.get("tools")
        return {"content": "(depth probe: no model was called)"}, {}

    agent.llm.chat = record_first_call
    # Active Recall asks the retrieval factory for the embedding width, and the
    # factory loads the embedding model to answer (`_derive_embedding_metadata`,
    # observed on the first probe of 2026-10-06: bge-m3 loaded by a dry run).
    # The probe root has no index, so recall finds nothing either way; the
    # factory's own not-cached answer (the known width) is served instead, and
    # the request is byte-for-byte what a loaded model would have left it.
    from dpc_client_core.dpc_agent.retrieval import factory
    loading = factory._derive_embedding_metadata
    factory._derive_embedding_metadata = _known_width
    try:
        await agent.process(message=task_prompt, conversation_id=conversation_id,
                            session_state=session_state, reasoning_effort=reasoning_effort,
                            **seeded_process_kwargs(seed, task_prompt))
    finally:
        factory._derive_embedding_metadata = loading
    messages = seen.get("messages") or []
    middle = messages[1:-1]
    cap = (agent._last_cap_info or {}).get("estimated_tokens_before", 0)
    tools = estimate_tokens(json.dumps(seen.get("tools") or [], ensure_ascii=False))
    return {
        "history_turns": len(middle),
        "assistant_turns": sum(1 for m in middle if m.get("role") == "assistant"),
        "history_tokens_est": sum(estimate_tokens(str(m.get("content", ""))) for m in middle),
        "prompt_tokens_est": cap,
        "tool_schema_tokens_est": tools,
        "round1_tokens_est": cap + tools,
        **engine_scale(cap, messages, seen.get("tools"), tokenizer),
        "messages": messages,  # for the caller's checks; never written to a report
    }


# -- keeping the seed out of the results --------------------------------------------

def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def redact_seeded_outcome(outcome: Dict[str, Any], task: Dict[str, Any],
                          field_value, answer: str) -> Dict[str, Any]:
    """Drop what the model wrote from one result row of a seeded run.

    The answer can quote or retell the chat it was handed, so its text goes;
    the scored values stay (`answer_fields`, read by the scorer's own
    `field_value`), with the answer's length. Note openings become digests:
    `repeat_opening_share` compares them for equality only, and was computed
    before this runs. Errors are cut to their type by `run_one` itself.
    """
    answer = answer or ""  # the whole answer: the row's own copy is cut to 400
    outcome.pop("answer", None)
    outcome.pop("answer_tail", None)
    lowered = answer.lower()
    outcome["answer_chars"] = len(answer)
    outcome["answer_fields"] = {k: field_value(k, lowered)
                                for k in (task.get("expect_fields") or {})}
    for row in outcome.get("per_round") or []:
        if row.get("note_opening"):
            row["note_opening"] = _digest(row["note_opening"])
    return outcome


def seed_windows(seed: Dict[str, Any], width: int = WITHHOLD_WINDOW) -> set:
    """Every `width`-character window of every seed record's content."""
    out = set()
    for rec in seed["messages"]:
        text = str(rec.get("content") or "")
        out.update(text[i:i + width] for i in range(0, max(0, len(text) - width + 1)))
    return out


def withhold_seed_text(obj: Any, windows: set, width: int = WITHHOLD_WINDOW,
                       counter: Optional[List[int]] = None) -> Any:
    """Replace any string sharing a `width`-character run with the seed.

    The net under `redact_seeded_outcome`: shell commands in the approver's
    summary and anything else the model typed are kept unless they carry the
    chat verbatim. A paraphrase passes this net — which is why the answer text
    is dropped outright rather than left to it.
    """
    if counter is None:
        counter = [0]
    if isinstance(obj, str):
        if len(obj) >= width and any(obj[i:i + width] in windows
                                     for i in range(len(obj) - width + 1)):
            counter[0] += 1
            return f"[withheld: shares {width}+ characters with the seed, {_digest(obj)}]"
        return obj
    if isinstance(obj, list):
        return [withhold_seed_text(v, windows, width, counter) for v in obj]
    if isinstance(obj, dict):
        return {k: withhold_seed_text(v, windows, width, counter) for k, v in obj.items()}
    return obj
