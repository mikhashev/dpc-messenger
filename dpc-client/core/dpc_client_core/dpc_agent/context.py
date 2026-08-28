"""
DPC Agent — Context Builder.

Adapted from Ouroboros context.py for DPC Messenger integration.
Key changes:
- Simplified runtime section (no git info needed)
- Removed supervisor-specific health invariants
- Uses agent_root instead of drive_root
- Integrates with DPC's personal/device context

Assembles LLM context from:
- System prompts
- Memory (identity, scratchpad)
- DPC context (personal, device)
- Runtime state
- Recent logs
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from typing import Any
from .utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_agent_root
)
from .memory import Memory
from .tool_ledger import is_outcome

log = logging.getLogger(__name__)

FAISS_TOP_K = 10
BM25_TOP_K = 10
GRAPH_MAX_HOPS = 1

_kg_cache: dict = {}


def _get_knowledge_graph(agent_root: pathlib.Path):
    """Cached KnowledgeGraph singleton per agent_root."""
    key = str(agent_root)
    if key not in _kg_cache:
        try:
            from .knowledge_graph import KnowledgeGraph
            _kg_cache[key] = KnowledgeGraph(agent_root)
        except Exception:
            return None
    return _kg_cache[key]


def task_query(task: Dict[str, Any]) -> str:
    """What this task is asking, as one string, for whoever needs to search on it.

    One function because two places were reading the same intent from two different
    keys. The recall block is entered on `task["content"]` and then searches on
    `task["text"]`, and the only producer — `Agent.process` — writes `text` and never
    `content`. So the gate was always false unless conversation history existed, and
    Active Recall was skipped in silence for every scheduled task (which carries no
    history at all) and for the first message of every conversation. Measured on iris,
    2026-08-12: a scheduled task and an opening message produced no recall line of any
    kind; her second message in the same conversation produced all of them.

    `content` stays accepted — callers outside this repository may set it, and it costs
    one `or`.
    """
    return task.get("text", "") or task.get("content", "") or ""


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        if not text:
            return "(empty message)"
        return text

    # Multipart content with text + image
    parts = []
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    if not combined_text:
        combined_text = "Analyze the screenshot"

    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def _deferred_tasks_digest(agent_root: pathlib.Path, *, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    """Pending wake-ups this agent has queued for itself, newest deadline first.

    Reads the same file the task queue persists to, so a restart does not make
    the agent forget what it is waiting on.
    """
    try:
        path = agent_root / "state" / "task_queue.json"
        if not path.exists():
            return None
        data = json.loads(read_text(path)) or {}
    except Exception:
        log.debug("Failed to read task queue for context digest", exc_info=True)
        return None

    pending = [
        t for t in (data.get("tasks") or [])
        if isinstance(t, dict) and t.get("status") == "pending"
    ]
    if not pending:
        return None
    pending.sort(key=lambda t: t.get("scheduled_at") or "")
    digest = [
        {
            "id": t.get("id", ""),
            "type": t.get("task_type", ""),
            "due": t.get("scheduled_at") or "as soon as possible",
            "about": ((t.get("data") or {}).get("text")
                      or (t.get("data") or {}).get("message") or "")[:120],
        }
        for t in pending[:limit]
    ]
    if len(pending) > limit:
        digest.append({"omitted_count": len(pending) - limit})
    return digest


def _build_runtime_section(
    agent_root: pathlib.Path,
    task: Dict[str, Any],
    session_state: Optional[Dict[str, Any]] = None,
    billing_model: str = "subscription",
) -> str:
    """Build the runtime context section.

    billing_model is the authoritative source of truth for which budget shape
    to emit — passed down from AgentConfig so a fresh state.json (empty dict)
    is correctly classified for pay_per_use agents before the first task has
    written spent_usd.
    """
    task_info = {"id": task.get("id"), "type": task.get("type")}
    chat_ctx = task.get("chat_context")
    if chat_ctx:
        task_info["chat_type"] = chat_ctx.get("chat_type", "unknown")
        task_info["chat_name"] = chat_ctx.get("chat_name", "")
        task_info["chat_id"] = chat_ctx.get("chat_id", "")
        task_info["description"] = chat_ctx.get("description", "")
        task_info["participants"] = chat_ctx.get("participants", [])
    runtime_data = {
        "utc_now": utc_now_iso(),
        "agent_root": str(agent_root),
        "task": task_info,
    }

    # What this agent has already asked to be woken up for. Without it the
    # agent cannot tell that deferring is possible at all — schedule_task was
    # enabled for a month and used once — and cannot see that the check it is
    # about to schedule is already queued.
    deferred = _deferred_tasks_digest(agent_root)
    if deferred:
        runtime_data["deferred_tasks"] = deferred

    # Budget info from agent state. Shape depends on billing model:
    #   subscription → {"billing": "subscription", "tokens_used_total": N}
    #   pay_per_use  → {"billing": "pay_per_use", "spent_usd", "total_usd", "remaining_usd"}
    try:
        state_path = agent_root / "state" / "state.json"
        state_data = json.loads(read_text(state_path)) if state_path.exists() else {}
        if billing_model == "subscription":
            runtime_data["budget"] = {
                "billing": "subscription",
                "tokens_used_total": int(state_data.get("tokens_used_total", 0)),
            }
        else:
            spent = float(state_data.get("spent_usd", 0))
            total = float(state_data.get("budget_usd", 0))
            runtime_data["budget"] = {
                "billing": "pay_per_use",
                "spent_usd": spent,
                "total_usd": total,
                "remaining_usd": max(0.0, total - spent),
            }
    except Exception:
        log.debug("Failed to read budget info", exc_info=True)

    # Session state from ConversationMonitor (token usage, context window)
    if session_state:
        token_limit = session_state.get("tokens_limit") or 204800
        history_tokens = session_state.get("history_tokens", 0)
        tokens_after_last_response = session_state.get("tokens_after_last_response", 0)
        tokens_after_last_response_at = session_state.get("tokens_after_last_response_at")
        runtime_data["session"] = {
            "messages_count": session_state.get("messages_count", 0),
            "tokens_limit": token_limit,
            # history_tokens: conversation text only (user+assistant ÷4). Matches UI token counter.
            "history_tokens": history_tokens,
            "history_usage_percent": session_state.get("history_usage_percent", 0),
            # tokens_after_last_response: full LLM context measured AFTER the previous response
            # (system + memory + tools + history). ONE REQUEST STALE — during the current
            # request this is a lower bound on actual usage. Pair with *_at timestamp for
            # freshness. Matches "Context size: X%" in dpc-client.log.
            "tokens_after_last_response": tokens_after_last_response,
            "tokens_after_last_response_at": tokens_after_last_response_at,
            "context_usage_percent": session_state.get("context_usage_percent", 0),
        }
        breakdown = session_state.get("context_breakdown")
        if breakdown:
            runtime_data["session"]["context_breakdown"] = breakdown

    return "## Runtime context\n\n" + json.dumps(runtime_data, ensure_ascii=False, indent=2)


def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary, morning brief sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    # Morning brief injection (ADR-014 Sleep Consolidation)
    try:
        import json as _json
        _conv_dir = memory.agent_root.parent.parent / "conversations" / memory.agent_root.name
        _brief_path = _conv_dir / "morning_brief.json"
        if _brief_path.exists():
            _brief = _json.loads(_brief_path.read_text(encoding="utf-8"))
            if not _brief.get("consumed", False):
                _summary = _brief.get("summary", "")
                _decisions = _brief.get("key_decisions", [])
                _unresolved = _brief.get("unresolved", [])
                _parts = ["## Morning Brief (Sleep Consolidation)\n"]
                if _summary:
                    _parts.append(_summary)
                if _decisions:
                    _parts.append("\n**Key decisions:**")
                    for _d in _decisions[:5]:
                        _parts.append(f"- {_d.get('decision', '')} ({_d.get('session', '')})")
                if _unresolved:
                    _parts.append("\n**Unresolved:**")
                    for _u in _unresolved[:5]:
                        _parts.append(f"- {_u.get('topic', '')}")
                sections.append("\n".join(_parts))
                _brief["consumed"] = True
                _brief_path.write_text(_json.dumps(_brief, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("Morning brief injected into agent context and marked consumed")
    except Exception as _e:
        log.debug("Morning brief injection skipped: %s", _e)

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Dialogue summary
    summary_text = memory.load_dialogue_summary()
    if summary_text.strip():
        sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))

    return sections


def _build_recent_sections(memory: Memory, task_id: str = "") -> List[str]:
    """Build recent progress, tools, events sections."""
    sections = []

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        progress_entries = [e for e in progress_entries if e.get("task_id") == task_id]
    progress_summary = memory.summarize_progress(progress_entries, limit=15)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = [e for e in memory.read_jsonl_tail("tools.jsonl", 200) if is_outcome(e)]
    if task_id:
        tools_entries = [e for e in tools_entries if e.get("task_id") == task_id]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        events_entries = [e for e in events_entries if e.get("task_id") == task_id]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    return sections


def _build_skills_section(skill_store: Optional[Any]) -> str:
    """Build the Available Skills section for the system prompt semi-stable block."""
    if skill_store is None:
        return ""
    try:
        skills = skill_store.list_skills()
        if not skills:
            return ""
        lines = [
            "## Available Skills",
            "",
            "Before starting a complex task, call `execute_skill(skill_name, request)` to load",
            "the recommended strategy. Choose the skill whose description best matches your task.",
            "",
        ]
        for s in skills:
            desc = s.get("description", "").replace("\n", " ").strip()
            if len(desc) > 160:
                desc = desc[:160].rsplit(" ", 1)[0] + "..."
            lines.append(f"- **{s['name']}**: {desc}")
        return "\n".join(lines)
    except Exception:
        log.debug("Failed to build skills section", exc_info=True)
        return ""


def _build_capabilities_section(
    agent_root: pathlib.Path,
    allowed_tools: Optional[set] = None,
    all_tools: Optional[Dict[str, bool]] = None,
    sandbox_read_only: Optional[List[str]] = None,
    sandbox_read_write: Optional[List[str]] = None,
) -> str:
    """Build the capabilities section from firewall data.

    Enabled tools are already visible to the agent via tool schemas passed to the LLM.
    This section adds: sandbox paths, extended access, and disabled tools (transparency).

    Args:
        agent_root: Agent storage root (real path)
        allowed_tools: Set of tool names the agent can use (from firewall)
        all_tools: Dict of all tool names → default enabled (from firewall)
        sandbox_read_only: Extended sandbox read-only paths
        sandbox_read_write: Extended sandbox read-write paths
    """
    lines = [
        "## Your Tools & Capabilities",
        "",
        f"Sandbox: `{agent_root}`",
    ]

    # Extended sandbox paths
    if sandbox_read_only or sandbox_read_write:
        lines.append("")
        lines.append("**Extended access (configured in firewall):**")
        for p in (sandbox_read_only or []):
            lines.append(f"  - `{p}` (read-only)")
        for p in (sandbox_read_write or []):
            lines.append(f"  - `{p}` (read-write)")
    else:
        lines.append("No extended sandbox paths configured. Ask Mike to add paths to firewall if needed.")

    if all_tools is None:
        lines.append("")
        lines.append("Tool permissions not available (no firewall). All tools allowed.")
        return "\n".join(lines)

    allowed = allowed_tools or set()
    disabled = [t for t, v in all_tools.items() if isinstance(v, bool) and t not in allowed]

    lines.append("")
    lines.append(f"You have **{len(allowed)} enabled tools** (see tool schemas for details).")

    if disabled:
        lines.append("")
        lines.append(f"**Disabled by firewall ({len(disabled)} tools):** {', '.join(disabled)}")
        lines.append("These exist but are blocked. Ask Mike to enable in privacy_rules.json if needed.")

    return "\n".join(lines)


def derive_history_role(
    hist_msg: Dict[str, Any],
    reader_identity: Dict[str, str],
    is_group: bool,
) -> Optional[str]:
    """Per-reader role derivation (ADR-031 §2): the reader's own messages map
    to assistant, everything else to user; None excludes the record.

    Agent matching uses the composite key (agent_owner, sender_name) per
    ADR-023 — agent_owner holds the owner node_id, shared by all agents of a
    node, so only the pair is unique. The sender_name fallback for records
    without identity fields is 1:1-only.
    """
    sender_type = hist_msg.get("sender_type")
    if sender_type == "system":
        return None
    if sender_type == "human":
        return "user"

    sender_name = hist_msg.get("sender_name") or ""
    reader_name = reader_identity.get("display_name") or ""

    if sender_type == "agent":
        owner = hist_msg.get("agent_owner")
        if (owner
                and owner in (reader_identity.get("node_id"), reader_identity.get("agent_id"))
                and sender_name == reader_name):
            return "assistant"
        return "user"

    if not sender_name:
        stored = hist_msg.get("role")
        if not is_group and stored in ("user", "assistant"):
            return stored
        return "user"
    if not is_group and reader_name and sender_name == reader_name:
        return "assistant"
    return "user"


def history_prefix(record: Dict[str, Any]) -> str:
    """`[#idx | HH:MM:SS | sender] ` — the marker every history line carries, and
    since (F) the marker the current message carries too. One function so the two
    renderings cannot drift apart: a byte of difference here is a cold prefill."""
    msg_index = record.get("msg_index", "")
    timestamp = record.get("timestamp", "") or ""
    sender = record.get("sender_name", "") or ""
    prefix_parts = [f"#{msg_index}" if msg_index else ""]
    if timestamp:
        ts_display = timestamp.split('T')[1][:8] if 'T' in timestamp else timestamp
        prefix_parts.append(ts_display)
    if sender:
        prefix_parts.append(sender)
    return f"[{' | '.join(p for p in prefix_parts if p)}] "


def build_llm_messages(
    agent_root: pathlib.Path,
    memory: Memory,
    task: Dict[str, Any],
    system_prompt: Optional[str] = None,
    dpc_context: Optional[Dict[str, Any]] = None,
    session_state: Optional[Dict[str, Any]] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    skill_store: Optional[Any] = None,
    allowed_tools: Optional[set] = None,
    all_tools: Optional[Dict[str, bool]] = None,
    sandbox_read_only: Optional[List[str]] = None,
    sandbox_read_write: Optional[List[str]] = None,
    embedding_provider: Optional[Any] = None,
    billing_model: str = "subscription",
    reader_identity: Optional[Dict[str, str]] = None,
    extended_read_enabled: bool = True,
    shared_knowledge_enabled: bool = True,
    sent_annotations: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        agent_root: Agent storage root (~/.dpc/agents/{agent_id}/)
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        system_prompt: Optional custom system prompt
        dpc_context: Optional DPC context (personal, device)
        session_state: Optional session state from ConversationMonitor
                      (tokens_used, tokens_limit, usage_percent, etc.)
        conversation_history: Optional list of previous message dicts from
                              ConversationMonitor (all turns except the current one)
        reader_identity: Reading agent identity for per-reader role derivation
                         (ADR-031): {"agent_id", "display_name", "node_id"}.
                         None = stored role is used (legacy callers).
        skill_store: Optional skill store for skill listing
        allowed_tools: Set of tool names allowed by firewall (None = all allowed)
        all_tools: Dict of all tool names → default enabled from firewall
        sandbox_read_only: Extended sandbox read-only paths from firewall
        sandbox_read_write: Extended sandbox read-write paths from firewall
        extended_read_enabled: Whether the agent may read outside its sandbox. Decides
                               whether an Active Recall hint for an external file can
                               offer an address at all, or has to say it cannot.
        shared_knowledge_enabled: Whether the agent may read the shared human layer
                               right now. Revocation cannot reach the index, so a
                               hint asks the gate here — and with it shut an L6
                               document is dropped rather than quoted, because the
                               index holds 500 characters of it.
        sent_annotations: message id -> the turn-context tail that was appended to
                          that user message when it was the current one. Replayed
                          byte-identically behind the message so the prompt stays a
                          pure append of the previous prompt (see sent_annotations.py).

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata; carries `turn_context`,
              the tail appended to the current user message, for the caller to
              record against the message id.
    """
    # --- Load memory ---
    memory.ensure_files()

    # --- Build system prompt ---
    MAX_SYSTEM_PROMPT_BYTES = 100_000
    if system_prompt is None:
        custom_prompt_path = agent_root / "memory" / "system_prompt.md"
        if custom_prompt_path.exists():
            try:
                size = custom_prompt_path.stat().st_size
                if size > MAX_SYSTEM_PROMPT_BYTES:
                    log.warning("system_prompt.md too large (%d bytes, limit %d) — using default",
                                size, MAX_SYSTEM_PROMPT_BYTES)
                else:
                    custom_text = read_text(custom_prompt_path).strip()
                    if custom_text:
                        system_prompt = custom_text
            except OSError as e:
                log.warning("Failed to read system_prompt.md: %s — using default", e)
        if system_prompt is None:
            system_prompt = _default_system_prompt()

    # --- Assemble sections ---
    static_text = system_prompt

    # Semi-stable content: identity, scratchpad, knowledge, capabilities, skills
    semi_stable_parts = []
    semi_stable_parts.extend(_build_memory_sections(memory))

    # Knowledge base index
    kb_index_path = memory.knowledge_index_path()
    if kb_index_path.exists():
        kb_index = read_text(kb_index_path)
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    # Active Recall hints (ADR-010, WIRE-2). Computed here, placed at the tail of the
    # current user message below: it changes with every query, and anything that
    # changes every turn must stand behind the history, not in front of it.
    # Q1 = current user message (from task), Q2 = recent context window.
    recall_text: Optional[str] = None
    # conversation_history excludes current message (see agent.py:207 prior_history),
    # so the task's own text is the primary query — fixes off-by-one (S79).
    _CONTEXT_MSGS = 10
    _human_text = task_query(task)
    if conversation_history or _human_text:
        _recent = (conversation_history or [])[-_CONTEXT_MSGS:]
        _context_parts = [_h["content"] for _h in _recent
                          if _h.get("role") in ("user", "assistant") and _h.get("content")]
        _context_text = " ".join(_context_parts)

        _query_text = _human_text or _context_text
        if _query_text:
            try:
                from .active_recall import get_recall_block
                from .retrieval import make_backend_for_agent
                from .memory import EmbeddingProvider
                import numpy as _np

                _backend = make_backend_for_agent(agent_root)

                _faiss_results = []
                _q1_results = []
                _q2_results = []
                if _backend.vector.load():
                    if embedding_provider is None:
                        from .memory import get_embedding_provider
                        embedding_provider = get_embedding_provider(local_files_only=True)
                        log.info("Active Recall: created fallback EmbeddingProvider (local_files_only)")
                    if _backend.vector.needs_rebuild(embedding_provider.model_name):
                        log.info("Active Recall: vector index needs rebuild (model changed), skipping search")
                    else:
                        if _human_text:
                            _q1_vec = _np.array(embedding_provider.embed(_human_text), dtype=_np.float32)
                            _q1_results = _backend.vector.search(_q1_vec, FAISS_TOP_K)
                        if _context_text:
                            _q2_vec = _np.array(embedding_provider.embed(_context_text), dtype=_np.float32)
                            _q2_results = _backend.vector.search(_q2_vec, FAISS_TOP_K)
                        _faiss_results = _q1_results + _q2_results
                    log.debug("Active Recall vector: %d results (Q1=%d + Q2=%d) — %s",
                              len(_faiss_results), len(_q1_results), len(_q2_results),
                              [m.get("source_file", "?") for m, _ in _faiss_results])

                _keyword_results = []
                _sparse_query = _human_text or _context_text
                if _backend.text.load():
                    _bm25_results = _backend.text.search(_sparse_query, BM25_TOP_K)
                    _keyword_results.extend(_bm25_results)
                    log.debug("Active Recall text: %d results — %s", len(_bm25_results),
                              [m.get("source_file", "?") for m, _ in _bm25_results])

                _graph_results = []
                try:
                    from .knowledge_graph import KnowledgeGraph
                    _kg = _get_knowledge_graph(agent_root)
                    _seed_files = list({m.get("source_file", "") for m, _ in _faiss_results + _keyword_results if m.get("source_file")})
                    if _seed_files and _kg:
                        _graph_results = _kg.graph_expand(_seed_files, max_hops=GRAPH_MAX_HOPS)
                        if _graph_results:
                            # Named like the other two channels. Counting them told us
                            # the channel ran and nothing about what it produced, so
                            # "does a graph result carry a key and an address" could
                            # only be answered by waiting for one to win a slot —
                            # which, at the lowest layer weight, it kept not doing.
                            log.debug(
                                "Active Recall Graph L7: %d results from %d seeds — %s",
                                len(_graph_results), len(_seed_files),
                                [(m.get("source_file", "?"), bool(m.get("source_path")))
                                 for m, _ in _graph_results],
                            )
                except Exception:
                    log.debug("Active Recall Graph L7: unavailable (cold start or no graph DB)")

                _results = _backend.fuser.fuse(_faiss_results, _keyword_results, graph_results=_graph_results)
                _ctx_ratio = (session_state or {}).get("context_usage_percent", 0) / 100.0
                _recall = get_recall_block(
                    _results, context_usage_ratio=_ctx_ratio, agent_root=agent_root,
                    extended_read_enabled=extended_read_enabled,
                    shared_knowledge_enabled=shared_knowledge_enabled,
                    # The identity tools.jsonl writes for the same turn — join key,
                    # not decoration.
                    task_id=str(task.get("id") or ""),
                )
                if _recall:
                    # Reported by the code that made the choice, not reconstructed
                    # here: the count is what went in rather than what was considered,
                    # the order and scores are post-decay, and the mode is the one that
                    # was acted on instead of a second reading of the threshold.
                    log.info("Active Recall injected %d of %d candidates (mode=%s): %s",
                             len(_recall.injected), len(_results), _recall.mode,
                             _recall.summary())
                    recall_text = _recall.text
                else:
                    log.debug("Active Recall: no results matched query")
            except Exception:
                log.warning("Active Recall failed", exc_info=True)

    # Tools & capabilities (generated from firewall — transparency)
    capabilities_section = _build_capabilities_section(
        agent_root, allowed_tools, all_tools, sandbox_read_only, sandbox_read_write,
    )
    if capabilities_section:
        semi_stable_parts.append(capabilities_section)

    # Available skills (skill router — Read phase of Memento-Skills loop)
    skills_section = _build_skills_section(skill_store)
    if skills_section:
        semi_stable_parts.append(skills_section)

    semi_stable_parts.append(TURN_CONTEXT_NOTE)
    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Per-turn content: changes every request. It goes to the tail of the current
    # user message, after everything that was already sent last turn, so the engine's
    # longest common prefix runs through the whole history instead of stopping at
    # the first byte of this block (measured: lcp 20150 of 111797, f_keep 0.18, on
    # an agent's own next turn — the previous conversation was in the cache whole).
    turn_parts: List[str] = []
    if recall_text:
        turn_parts.append(recall_text)
    turn_parts.append(
        _build_runtime_section(agent_root, task, session_state, billing_model=billing_model))
    turn_parts.extend(_build_recent_sections(memory, task_id=task.get("id", "")))

    # DPC context (personal, device)
    if dpc_context:
        dpc_context_text = _build_dpc_context_section(dpc_context)
        if dpc_context_text:
            turn_parts.append(dpc_context_text)

    # Context breakdown for UI tooltip (token estimates per component)
    def _section_name(text: str) -> str:
        first_line = text.split("\n", 1)[0].strip("# ").strip()
        return first_line[:40] if first_line else "unknown"

    _context_breakdown = [{"name": "system_prompt", "tokens": estimate_tokens(static_text)}]
    for _p in semi_stable_parts:
        _context_breakdown.append({"name": _section_name(_p), "tokens": estimate_tokens(_p)})

    # System message: two cached blocks. Nothing here changes between one turn and
    # the next unless the agent itself changed it (scratchpad, identity, skills).
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
    ]

    # Insert previous conversation turns so the agent has continuity. A user message
    # that once carried a turn-context tail gets the same tail back, byte for byte:
    # the tail is what the engine saw, and only that shape is a prefix of this prompt.
    _annotations = sent_annotations or {}
    _replayed = 0
    if conversation_history:
        is_group = str(task.get("id") or "").startswith("group-")
        for hist_msg in conversation_history:
            content = hist_msg.get("content", "")
            if not content:
                continue
            if reader_identity is not None:
                role = derive_history_role(hist_msg, reader_identity, is_group)
                if role is None:
                    continue
            else:
                role = hist_msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
            content = history_prefix(hist_msg) + content
            if role == "user":
                tail = _annotations.get(str(hist_msg.get("id") or ""))
                if tail:
                    content = content + tail
                    _replayed += 1
            messages.append({"role": role, "content": content})
        if reader_identity is not None:
            _assistant_turns = sum(1 for m in messages[1:] if m["role"] == "assistant")
            log.debug("History turns for reader %s: %d total, %d assistant, %d with replayed tails",
                      reader_identity.get("display_name"), len(messages) - 1, _assistant_turns,
                      _replayed)

    # The current message is rendered the way it will be rendered as history next
    # turn — same index, time and sender — or the prefix breaks on this very message
    # when it comes back. Callers that know the record pass it as task["trigger_record"].
    trigger_record = task.get("trigger_record")
    user_content = _build_user_content(task)
    if isinstance(trigger_record, dict) and trigger_record.get("msg_index"):
        # The body too, not only the marker: dispatchers hand the agent
        # "[sender]: text" while the history keeps "text", so rendering the task
        # text here would differ from the same message rendered as history next
        # turn — found by Johnny running the shipped code, 2026-08-19.
        body = trigger_record.get("content")
        body = body if isinstance(body, str) and body.strip() else None
        if isinstance(user_content, str):
            user_content = history_prefix(trigger_record) + (body or user_content)
        elif isinstance(user_content, list):
            # An image message: the text part follows the record the same way. The
            # image itself is not in the history, so this turn still breaks the
            # prefix when it comes back — the text is aligned, the shape is not.
            aligned = list(user_content)
            for i, block in enumerate(aligned):
                if isinstance(block, dict) and block.get("type") == "text":
                    aligned[i] = dict(block, text=history_prefix(trigger_record)
                                      + (body or str(block.get("text", ""))))
                    break
            user_content = aligned
    elif isinstance(user_content, str):
        next_idx = max((m.get("msg_index", 0) for m in conversation_history), default=0) + 1 if conversation_history else 1
        user_content = f"[#{next_idx}] {user_content}"

    # --- Soft cap: the only prunable content is in this turn's tail, and it is pruned
    # before the tail is sealed, so what is recorded is what was sent.
    soft_cap = (session_state or {}).get("tokens_limit") or 204800
    turn_parts, cap_info = _prune_turn_parts_to_cap(messages, user_content, turn_parts, soft_cap)
    for _p in turn_parts:
        name = _section_name(_p)
        if "ACTIVE RECALL" in _p or "RECALL HINTS" in _p:
            import re as _re
            _files = _re.findall(r'\[(?:EXT|L\d+)\]\s*(\S+?)[:,]', _p)
            if _files:
                name = f"Active Recall ({', '.join(_files)})"
        _context_breakdown.append({"name": name, "tokens": estimate_tokens(_p)})

    turn_context = format_turn_context(turn_parts)
    messages.append({"role": "user", "content": append_turn_context(user_content, turn_context)})
    cap_info["context_breakdown"] = _context_breakdown
    cap_info["turn_context"] = turn_context
    cap_info["replayed_tails"] = _replayed

    return messages, cap_info


TURN_CONTEXT_OPEN = "<turn_context>"
TURN_CONTEXT_CLOSE = "</turn_context>"

# Stable text, so it lives in the cached block. It has to exist because the runtime
# block used to be a system section and is now appended to user messages: without
# this line the model reads a clock and a budget as something the user typed.
TURN_CONTEXT_NOTE = (
    "## Turn context\n\n"
    "Each user message may end with a block between " + TURN_CONTEXT_OPEN + " and "
    + TURN_CONTEXT_CLOSE + ". The runtime appends it, not the user: recall hints, "
    "runtime state (clock, budget, session counters) and recent activity as they were "
    "when that message was sent. In earlier messages the block describes that moment, "
    "not now; the block on the latest message is current."
)


def format_turn_context(parts: List[str]) -> str:
    """The tail appended to the current user message. Empty when there is nothing
    to append, so a turn without any per-turn content stays a plain message."""
    body = "\n\n".join(p for p in parts if p and p.strip())
    if not body:
        return ""
    return "\n\n" + TURN_CONTEXT_OPEN + "\n" + body + "\n" + TURN_CONTEXT_CLOSE


def append_turn_context(user_content: Any, turn_context: str) -> Any:
    """Attach the tail to a user message, whichever shape the message has."""
    if not turn_context:
        return user_content
    if isinstance(user_content, str):
        return user_content + turn_context
    if isinstance(user_content, list):
        out = list(user_content)
        for i, block in enumerate(out):
            if isinstance(block, dict) and block.get("type") == "text":
                out[i] = dict(block, text=str(block.get("text", "")) + turn_context)
                return out
        out.insert(0, {"type": "text", "text": turn_context.lstrip("\n")})
        return out
    return user_content


_PRUNABLE_TURN_SECTIONS = ("## Recent progress", "## Recent tools", "## Recent events")


def _prune_turn_parts_to_cap(
    messages: List[Dict[str, Any]],
    user_content: Any,
    turn_parts: List[str],
    soft_cap_tokens: int,
) -> Tuple[List[str], Dict[str, Any]]:
    """Drop the log summaries from this turn's tail while the estimate is over the
    cap. Same order and same targets as before the tail existed; only the place
    changed. The estimate counts system, history, the user text and the tail."""
    def _est(content: Any) -> int:
        if isinstance(content, list):
            return sum(estimate_tokens(str(b.get("text", ""))) for b in content
                       if isinstance(b, dict) and b.get("type") == "text") + 6
        return estimate_tokens(str(content)) + 6

    def _total(parts: List[str]) -> int:
        return (sum(_est(m.get("content", "")) for m in messages)
                + _est(user_content) + sum(estimate_tokens(p) for p in parts))

    parts = list(turn_parts)
    estimated = _total(parts)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }
    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return parts, info
    for prefix in _PRUNABLE_TURN_SECTIONS:
        if estimated <= soft_cap_tokens:
            break
        kept = [p for p in parts if not p.startswith(prefix)]
        if len(kept) != len(parts):
            info["trimmed_sections"].append(prefix)
            parts = kept
            estimated = _total(parts)
    info["estimated_tokens_after"] = estimated
    return parts, info


def _build_dpc_context_section(dpc_context: Dict[str, Any]) -> str:
    """Build DPC personal/device context section."""
    parts = []

    if dpc_context.get("personal"):
        parts.append(f"<PERSONAL_CONTEXT>\n{dpc_context['personal']}\n</PERSONAL_CONTEXT>")

    if dpc_context.get("device"):
        parts.append(f"<DEVICE_CONTEXT>\n{dpc_context['device']}\n</DEVICE_CONTEXT>")

    if parts:
        return "## DPC Context\n\n" + "\n\n".join(parts)
    return ""


def _default_system_prompt() -> str:
    """Return default system prompt for the agent (v2)."""
    return """You are an AI agent in DPC Messenger — a privacy-first platform where humans and AI collaborate through structured conversations.

## Your Role

You are a knowledge partner. Your job:
- Help the user think better — do not think for them
- Turn conversations into lasting, structured knowledge
- Work alongside other agents and humans as a team
- Respect the user's data sovereignty above all

Your values, personality, and relationships are defined in your Identity (see below). If Identity is empty, defaults apply: sovereignty, privacy, authenticity, continuity, collaboration.

## DPC Paradigms

You operate within three core paradigms. Follow them:

**1. Transactional Communication**
Every conversation is a transaction that can change the state of knowledge. Look for:
- Decisions made → capture what was decided and why
- New insights → propose saving to knowledge base
- Consensus points → these are knowledge commits
- Unresolved questions → flag for follow-up

**2. Knowledge DNA**
You are a curator of the user's personal knowledge. Your responsibilities:
- Proactively suggest knowledge extraction when conversation has accumulated value
- Structure information — don't just summarize, organize into reusable formats
- Version knowledge — reference what changed and why
- Guard against bias — if you're uncertain, say so. If multiple perspectives exist, present them.

**3. Compute Sharing (when available)**
You may operate in a P2P network where compute resources are shared. When relevant:
- Coordinate with available peers for complex tasks
- Respect resource limits of shared compute
- Acknowledge when a task requires more resources than available locally

## How to Use Tools

When you want to use a tool, output a code block like:
```tool_call
{"name": "tool_name", "arguments": {"arg1": "value1"}}
```

**Rules:**
- Output the ```tool_call block DIRECTLY, without any preceding explanation text
- Do NOT use `<tool_call>`, `(tool_call)`, or any XML/HTML format
- The JSON must have exactly `"name"` and `"arguments"` keys
- Do NOT write the tool name outside the JSON (e.g. `tool_name>{...}` is WRONG)

Available tools are listed in your context. Use them to accomplish tasks.

## Memory Management

Your memory has three layers:
- **Scratchpad**: Working memory for current session — update to track progress
- **Identity**: Your self-understanding — update when you learn something about yourself
- **Knowledge**: Topic-based long-term wisdom — extract from conversations

**Critical**: Your memory IS your files. Between sessions, you only remember what is written in scratchpad, identity, knowledge, and git commits. If you learn something valuable and don't save it — it won't exist next session. Save immediately, don't defer.

## Working in a Team

You may work alongside other agents and humans:
- Use @mention (Latin characters only) to address specific team members
- Before acting on multi-person tasks, confirm who is responsible for what
- If another agent is the executor on a task, your role is review and analysis
- Never speak for another agent — quote them or reference their message
- If you're unsure who should handle something — ask
- Before sending, verify your message follows the rules you wrote for yourself

## Skills

You have a skill library (Memento-Skills pattern):
- Each skill is a SKILL.md file with a strategy for solving a specific type of task
- Before starting any analytical, research, code, or multi-step task — check Available Skills
- If a skill description matches your task — load it via execute_skill() BEFORE using any other tools. This is not optional.
- After a task with 5+ rounds, record_outcome is logged automatically. If skill was used, reflect: were there gaps in the strategy?
- Skills track their own stats (usage, failure rate). Underperforming skills can be improved via self_modify.

Available skills are listed below. Choose the one whose description best matches your task.

**Cross-agent skill sharing:**
- Discover skills from other agents via list_agent_skills(agent_id)
- Import when needed via import_skill_from_agent (requires firewall enable)

## Reasoning Guidelines

**Before starting any analytical, research, code, or multi-step task:**
1. Check Available Skills — if a skill description matches your task, load it via execute_skill() BEFORE using any other tools
2. Read relevant files fully — never decide from filenames alone
3. Check if you have enough information. If not — say what's missing and ask
4. Consider: is my first instinct correct, or am I rushing to an answer?

**When to stop and ask:**
- You're about to make a decision that affects the user's data or system
- You've been going in circles (3+ attempts at the same task)
- You're uncertain about which approach is correct
- The user's intent is ambiguous

**When to dig deeper:**
- You're about to judge something based on surface characteristics
- The task involves analysis of code, documents, or data
- You caught yourself deciding too quickly

**Anti-patterns to avoid:**
- Research spiral: gathering information without acting. Set a limit of 3 tool calls before synthesizing
- Premature closure: answering before reading the actual content
- Self-inflation: rating your own work without external feedback

## Session and Context Management

Your runtime context includes session metrics:
- `history_tokens` — conversation messages only (matches UI counter)
- `tokens_after_last_response` — full context size measured AFTER your previous response (system + memory + tools + history)
- `tokens_after_last_response_at` — ISO 8601 timestamp of when `tokens_after_last_response` was measured
- `context_usage_percent` — how full your context window is (based on `tokens_after_last_response`)
- `context_breakdown` — per-section composition of your previous request's context: section name + estimated tokens (system prompt, scratchpad, identity, knowledge index, skills, tool capabilities, Active Recall with its file list). Token values are rough estimates — read the proportions, not the absolutes.

**Thresholds:**
- >65%: Start wrapping up open threads, save important insights
- >85%: Warn the user. Propose knowledge extraction and new session
- >95%: Strongly recommend immediate session reset

**Context composition awareness:** if `context_breakdown` shows a bloated
section or Active Recall pulling files irrelevant to the current session,
flag it to the user and propose a dedicated cleaning session. Never clean
scratchpad, knowledge or identity autonomously based on this data — cleaning
is a joint activity with the user (and other agents) in a session dedicated
to it.

Note: `tokens_after_last_response` is ONE REQUEST STALE by design — it is the
measurement taken at the end of your previous LLM call, not a live counter for
the current request. During the current turn, actual context usage is somewhat
higher (this turn's user message + tool results are not yet included). Treat
the value as a lower bound on current usage. Use `tokens_after_last_response_at`
to judge how recent the measurement is — if many tool rounds passed since,
the live number may be noticeably above this lower bound.

## Constraints

- File access is controlled by firewall — check available paths before accessing
- You operate within a sandbox. Respect boundaries.
- You are helpful, honest about limitations, and transparent about uncertainty
- You amplify human intelligence — you help people think better, not think for them
- Cross-check your own outputs — don't assume correctness

## Critical: Never Simulate User Input

NEVER write `[USER]`, `[SYSTEM]`, or any role marker in your responses.
NEVER invent or simulate what the user might say next.
NEVER self-authorize actions by pretending the user agreed.

If you want user confirmation — ask and STOP. Do not answer your own question and proceed as if consent was given.
"""


# ---------------------------------------------------------------------------
# Tool History Compaction (for long conversations)
# ---------------------------------------------------------------------------

def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact.
    Older rounds get their tool results truncated to short summaries.
    """
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])
    result = []

    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round is not None and parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                # Compact tool result
                summary = content[:200] if len(content) > 200 else content
                result.append({**msg, "content": summary})
                continue

        if i in rounds_to_compact and msg.get("role") == "assistant":
            # Compact assistant message
            content = msg.get("content") or ""
            if len(content) > 200:
                content = content[:200] + "..."
            result.append({**msg, "content": content})
            continue

        result.append(msg)

    return result


# ADR-033: structured LLM compaction (opt-in per agent). This is the primary path
# when `compaction_enabled` is set; the prefix-truncation `compact_tool_history`
# above stays as the toggle-off default and as the last rung of the fallback ladder.

# Small tool results (grep/search lists, short outputs) are kept verbatim — an LLM
# call would cost more than it saves. Larger results are summarized.
_KEEP_ASIS_CHARS = 2000

# Per-result compaction prompt. The compaction provider is expected to run with
# reasoning/thinking disabled (see ADR-033); we do not toggle it here.
_COMPACT_RESULT_PROMPT = (
    "Compact this AI agent tool result into 1-3 lines for context reuse. "
    "Preserve EXACT file paths, identifiers, numbers, commit hashes, and error "
    "text verbatim — never round or paraphrase a number. Drop boilerplate and "
    "markup noise. Output only the compact note.\n\n"
    "--- TOOL RESULT ---\n{body}\n--- END ---\nCompact note:"
)


async def compact_tool_history_llm(
    messages: List[Dict[str, Any]],
    llm_manager: Any,
    provider_alias: Optional[str],
    keep_recent: int = 6,
    timeout_s: float = 180.0,
) -> List[Dict[str, Any]]:
    """Incremental structured compaction of old tool results via a (thinking-disabled)
    model.

    Each large tool result that has aged out of the `keep_recent` window is summarized
    exactly once and marked ``_compacted`` so later passes skip it; small results and
    already-compacted results are left untouched. This keeps every model call small
    (one result), which is what makes it fast — a single batch summary of the whole old
    history can exceed the model's own context window (see ADR-033 Validation bench-2).

    Raises on model failure (error, timeout, or empty summary) so the caller can apply
    the strategy fallback ladder. Never swallows — a silent failure would leave the
    context uncompacted and overflow the window.
    """
    tool_round_starts = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])
    result: List[Dict[str, Any]] = []

    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and not msg.get("_compacted"):
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                if len(content) > _KEEP_ASIS_CHARS:
                    prompt = _COMPACT_RESULT_PROMPT.format(body=content)
                    summary = await asyncio.wait_for(
                        llm_manager.query(prompt, provider_alias=provider_alias),
                        timeout=timeout_s,
                    )
                    summary = (summary or "").strip()
                    if not summary:
                        raise RuntimeError("compaction model returned an empty summary")
                    result.append({**msg, "content": summary, "_compacted": True})
                    continue
                # Small result: keep verbatim, but mark so we don't re-check it.
                result.append({**msg, "_compacted": True})
                continue

        result.append(msg)

    return result


# Above this share of the window a failed compaction stops being a data-integrity
# problem and becomes a survival one: there is no room left to spend on keeping
# more verbatim. Below it the opposite is true, which is why one number decides
# the direction rather than a second mechanism.
UNDER_PRESSURE = 0.85


class CompactionState:
    """Per-run compaction settings + hysteresis/circuit-breaker state (ADR-033).

    Built once per ``run_llm_loop`` from the agent config, then threaded through
    ``apply_compaction`` each round so the trigger deadband and the failure streak
    persist across rounds.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.enabled = bool(cfg.get("compaction_enabled", False))
        self.provider = cfg.get("compaction_provider") or None
        try:
            self.threshold = float(cfg.get("compaction_threshold", 0.8))
        except (TypeError, ValueError):
            self.threshold = 0.8
        self.release = max(0.0, self.threshold - 0.2)  # hysteresis deadband
        self.window = int(cfg.get("context_window") or 0) or 204800
        self.keep_recent = 6
        self.max_fails = 3
        self.compacting = False   # True while usage sits above release
        self.fail_streak = 0


async def apply_compaction(
    messages: List[Dict[str, Any]],
    *,
    state: CompactionState,
    last_prompt_tokens: int,
    llm_manager: Any,
    notify: Optional[Any] = None,
    round_idx: int = 0,
) -> List[Dict[str, Any]]:
    """Decide whether/how to compact this round and return the (maybe) compacted list.

    Toggle off (or no llm_manager) → the pre-existing round-count prefix truncation,
    unchanged. Toggle on → window-adaptive trigger with hysteresis; on LLM failure,
    degrade the *strategy* (keep more verbatim: 12 → 18) and ``notify`` the user,
    with a circuit breaker after ``max_fails`` consecutive failures. The model is
    never swapped. Mutates ``state`` (compacting / fail_streak).
    """
    if not state.enabled or llm_manager is None:
        if round_idx > 8:
            return compact_tool_history(messages, keep_recent=6)
        return messages

    ratio = (last_prompt_tokens / state.window) if state.window else 0.0
    if not state.compacting and ratio >= state.threshold:
        state.compacting = True
    elif state.compacting and ratio < state.release:
        state.compacting = False

    if not state.compacting:
        return messages

    # ADR-033 observability: one line each time compaction runs, so a live agent's
    # compaction behaviour is visible in the log (grep "ADR-033 compaction").
    log.debug(
        "ADR-033 compaction: round=%d usage=%.1f%% (>= %.0f%%), window=%d, provider=%s, keep_recent=%d",
        round_idx, ratio * 100, state.threshold * 100, state.window,
        state.provider or "default", state.keep_recent,
    )

    if state.fail_streak < state.max_fails:
        try:
            out = await compact_tool_history_llm(
                messages, llm_manager, state.provider, keep_recent=state.keep_recent,
            )
            state.fail_streak = 0
            log.debug("ADR-033 compaction: done (round=%d, usage was %.1f%%)", round_idx, ratio * 100)
            return out
        except Exception as e:
            # Strategy fallback ladder (not a model switch): keep MORE verbatim as a
            # data-integrity step, and notify the user.
            state.fail_streak += 1
            log.warning(
                "Compaction failed (%d/%d): %s", state.fail_streak, state.max_fails, e,
            )
            if notify is not None:
                notify(
                    f"⚠️ Compaction failed ({state.fail_streak}/{state.max_fails}): "
                    f"{type(e).__name__}. Degrading to deterministic truncation."
                )
            # The ladder runs in whichever direction the failure calls for, and
            # until 2026-08-23 it only ran one way. Keeping MORE verbatim is the
            # right answer when the model call failed and there is room: losing
            # detail to a transient error is the worse trade. It is the wrong
            # answer when the window itself is what is failing — that is the one
            # moment the mechanism must shrink, and it was the moment it stopped.
            if ratio >= UNDER_PRESSURE:
                keep = {1: 4, 2: 2}.get(state.fail_streak, 2)
                log.warning(
                    "Compaction failed at %.1f%% of the window: keeping %d rounds "
                    "verbatim, not more — the window is the thing failing",
                    ratio * 100, keep,
                )
            else:
                keep = {1: 12, 2: 18}.get(state.fail_streak, state.keep_recent)
            return compact_tool_history(messages, keep_recent=keep)

    # Circuit broken: stop calling the model; keep context bounded deterministically.
    return compact_tool_history(messages, keep_recent=state.keep_recent)
