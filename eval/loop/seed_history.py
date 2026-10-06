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
`redact_seeded_outcome` and `withhold_seed_text`). Only the seed's path, sha256
and record count leave this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    for i, rec in enumerate(records):
        if not isinstance(rec, dict) or not isinstance(rec.get("msg_index"), int):
            raise SeedError(f"--seed-history: record {i} carries no integer msg_index")
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
    }


def seed_record(seed: Dict[str, Any]) -> Dict[str, Any]:
    """What a report may say about a seed: where, which bytes, how many records."""
    return {"path": seed["path"], "sha256": seed["sha256"],
            "messages": len(seed["messages"]),
            "history_records": len(seed["messages"]) - 1,
            "reader": seed["reader"]["display_name"],
            "trigger": "replaced by the task text (index, time and sender kept)"}


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
                       reasoning_effort: Optional[str]) -> Dict[str, Any]:
    """Build the run's round-1 request through production code and measure it.

    A real `DpcAgent` on a throwaway root, with the adapter's `chat` replaced by
    a recorder that returns a final answer at once: `process` renders the seed,
    the loop calls `chat` once with the messages and tool schemas the model
    would get, and nothing is sent anywhere. Tokens are counted with the
    production estimator (`dpc_agent.utils.estimate_tokens`, chars / 4) — the
    one behind `cap_info["estimated_tokens_before"]`, which production logs as
    "Context size: estimated N" — plus the same estimator over the tool schemas,
    which that figure leaves out and the engine counts. No tokenizer is loaded.
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
