# ADR-009: Knowledge Extraction Flows Redesign

**Status:** Accepted (S33, 2026-04-13)
**Date:** 2026-04-11 (drafted), 2026-04-13 (decided)
**Authors:** Mike (direction), CC (drafted from S28 discussion), Ark (co-analyst)
**Session:** S28 — archeology and redesign. S33 — final discussion and decision.
**Supersedes/extends:** [ADR-004 Knowledge Extraction Review](004-knowledge-extraction-review.md)
**Related:** [docs/KNOWLEDGE_ARCHITECTURE.md](../KNOWLEDGE_ARCHITECTURE.md) Phase 8

---

## Context

### Origin (PCM, March 2025)

Knowledge extraction in DPC Messenger descends from Mike's pre-DPC experiments with AI assistants via a single personal context JSON file. The original mechanism is preserved in the reference file `C:\Users\mike\Documents\Context\example_context.json`:

- A JSON file containing `personal_context` (sections with personal data) and an `instruction` block that tells the AI how to work with it.
- The instruction included a command: **"Завершение сессии"** — upon which the AI would collect what it learned during the session into `context_updates.updates`, show the updated file, and Mike would manually save.
- **No multi-perspective fields.** No `alternative_viewpoints`, no `cultural_perspectives`, no `devil_advocate_analysis`. Just `updates` entries with `updated_by` metadata (e.g. `"Grok 3"`) as a simple audit trail.
- One user, one AI, one JSON file. Solo bias-mitigation was not part of the design.

### Multi-perspective fields added later

The multi-perspective fields entered DPC Messenger from a separate source:
`C:\Users\mike\personal-context-manager\docs\cognitive-bias-ai-llms-user-guide.md` — Mike's own user guide on working with cognitive biases in LLMs.

- §5.2 "Multi-Perspective Validation" / "The Three-Frame Method" → `alternative_viewpoints`
- §3 "Cultural and Language Biases" / "The Cultural Lens Method" → `cultural_perspectives`
- §5.2 "The Devil's Advocate Approach" → `devil_advocate_analysis`

These were **user-level methods** ("ask the AI from multiple angles, check across cultures, have the AI argue against itself") that got **operationalized as schema fields** in DPC's `KnowledgeCommit` structure sometime after DPC launch. The schema fields were designed to be filled by **LLM self-reflection** — the AI generates its own alternative viewpoints as part of extraction.

### DPC era evolution

- **November 2025 — DPC Messenger launched.** Inherited from PCM: `personal.json` schema, the "Завершение сессии" concept (renamed to "End Session and Extract Knowledge"), conversation monitoring + auto-detection from the UserTesting methodology (May 2025).
- **Multi-perspective fields added to `KnowledgeCommit` schema** (sometime between Nov 2025 and Feb 2026 — exact commit not verified).
- **P2P voting added on top via `consensus_manager`** — multi-party approval, crypto signatures, Devil's Advocate as a required dissenter role when `participants >= 3`.
- **Feb 21, 2026** — first verified `.md` commit in `~/.dpc/knowledge/` (`ai_agent_integration_into_dpc_messenger_commit-ae975ad9f955c0f3.md`). Empirical verification: **template-collapse already present in this file** — all 10 entries share identical `alternative_viewpoints` about "integration paths" and "collaboration styles" regardless of entry topic. The LLM-self-reflection mechanism was never producing genuinely varied outputs.
- **Agent chat flow** added via `dpc_agent/tools/core.py:extract_knowledge()` for agent self-learning.
- **`aba4195` (Mar 25, 2026)** — agent `extract_knowledge` tool triggered the voting flow through `consensus_manager.propose_commit()`, unifying agent and human extraction paths.
- **`aed5401` (Apr 1, 2026)** — added `proposal_id` to `KnowledgeCommit` for linking agent-initiated commits to the voting flow.
- **`287606c` (Apr 3, 2026)** — **REVERTED** the voting trigger on the agent side. Reason: agent `extract_knowledge` consumed `ConversationMonitor.message_buffer`, causing Human's End Session to find 0 entries, and producing surprise voting dialogs while the user was still working.

### Current state (post-revert)

Three independent extraction paths with zero coordination:

| Path | Triggered by | Saves to | Schema | Index |
|---|---|---|---|---|
| Local AI chat | Extract Knowledge button | `~/.dpc/knowledge/*.md` | Full KnowledgeCommit with multi-perspective | N/A (store has no index) |
| P2P chat | Extract Knowledge button (any peer) + multi-party voting | `~/.dpc/knowledge/*.md` | Same, signed by all approvers | N/A |
| Agent chat (Mike side) | Extract Knowledge button | **Bypassed** — `service.py:4258-4260` clears history without extraction | — | — |
| Agent chat (Ark side) | `extract_knowledge()` tool call | `~/.dpc/agents/agent_001/knowledge/*.json` | Stripped (topic/summary/entries[content,tags,confidence]/participants/timestamps) | `_index.md` exists but is NOT updated |
| CC memory | CC internal decision | `~/.claude/projects/.../memory/*.md` | Frontmatter (name/description/type) + MEMORY.md index | MEMORY.md (manually updated) |

---

## Problem

### P1: Template-collapse in Human knowledge store (pre-existing, not a regression)

The `alternative_viewpoints`, `cultural_perspectives`, and `devil_advocate_analysis` fields are filled by LLM self-reflection from the extraction prompt. In solo contexts (Local AI chat, agent chat) there is no second participant with an actual differing viewpoint — the LLM generates "alternative" content from its own output, producing generic templates that are **identical across unrelated entries** within the same commit.

**Empirically verified** (S28):
- First `.md` commit (2026-02-21): 10 entries, **all with identical** alternative_viewpoints about integration paths
- Latest `.md` commit (2026-04-11): 7 entries, **all with identical** alternative_viewpoints about v0.21 positioning
- `cultural_perspectives` arrays are **always empty**
- `devil_advocate_analysis` field is **absent** from saved structure even though the schema defines it

This is not erosion under load — it was always there. The mechanism was designed for user-level methods (a human asking the AI from multiple angles), not for automated batch extraction at session end.

### P2: Agent store index desync

`dpc_agent/tools/core.py:extract_knowledge()` writes to `~/.dpc/agents/agent_001/knowledge/` but does NOT update `_index.md`. As of 2026-04-11:
- `_index.md` contains 36 entries (old manually curated `.md` files from the initial period)
- The directory contains 25+ additional `.json` files from the post-revert tool flow (Apr 5 → Apr 11)
- Ark's `knowledge_read()` tool relies on the index → cannot find its own recent extractions via index (filesystem scan still works, but is not the default recall path)

### P3: Hardcoded `.json` format in agent tool

`dpc_agent/tools/core.py:1753`:

```python
filename = f"{safe_topic}_{timestamp}.json"
...
with open(filepath, "w", encoding="utf-8") as f:
    json.dump(knowledge_entry, f, indent=2, ensure_ascii=False)
```

The `.json` extension is hardcoded. No architectural rationale — this was a quick implementation post-revert. The rest of the knowledge ecosystem uses `.md` (both Mike's store and the older curated files in the agent store). JSON is less human-readable and inconsistent with the rest of the system.

### P4: No coordination between the three knowledge streams

Mike's store, agent's store, and CC's memory are each maintained independently. In a three-way session (Mike + Ark + CC), each participant saves different knowledge to a different location in a different format — and no participant has visibility into what the others saved. Integration across the three streams is not part of this ADR's scope, but is noted as an open question.

### Clarification: Extract Knowledge button already works in agent chat

Initial drafts of this ADR (S28) incorrectly claimed that `service.py:4258-4260` bypassed Extract Knowledge for agent chats. **That was wrong.** Verified during S28 review:

- `service.py:4258-4260` is part of `propose_new_session` — it handles the **New Session** button (session reset voting). The `agent_*` / `ai_*` / `telegram-*` branch routes these to simple history reset because there is no peer to vote with, NOT because extraction is bypassed.
- Extract Knowledge button has its own handler: `knowledge_service.end_conversation_session()` (file `knowledge_service.py:566-705`). Its docstring explicitly says: *"UI Integration: Called when user clicks 'End Session & Save Knowledge' button"* (old name of the Extract Knowledge button).
- For `conversation_id.startswith("agent_")`, that handler fetches the monitor via `dpc_agent_provider.get_manager()` (lines 578-585), calls `generate_commit_proposal(force=True, ...)` (line 618), broadcasts `knowledge_commit_proposed` to the UI voting panel (line 647), and runs solo voting via `consensus_manager.propose_commit(..., _no_op_broadcast)` (lines 652-669) since the chat is marked private.

**Conclusion:** Mike's Extract Knowledge button already functions correctly in agent chat via the Local-AI-chat-equivalent flow. No code change required on Mike's side. The only real problems are P1 (template-collapse), P2 (index desync), and P3 (hardcoded `.json`).

---

## Decision

### Two independent extraction paths in agent chat

Agent chat (Mike + Ark) will have two parallel extraction mechanisms, both operating on the same conversation but writing to separate stores without cross-linking:

#### Path A: Mike's extraction (button-initiated)

- Mike presses the **Extract Knowledge** button in the agent chat UI.
- Backend handles it via `knowledge_service.end_conversation_session()` — the same handler used for Local AI chat. Flow is already correct (see "Clarification" note above).
- **Result:** crypto-signed `.md` file in `~/.dpc/knowledge/` with Mike's signature. Participant list includes `dpc-node-...` (Mike) and `agent_001` (Ark, as session participant — same as already happens in recent files).
- **Voting:** solo (only Mike is a voter since Ark has no crypto identity). Agent chat is marked as a private conversation; consensus runs without peer broadcast.
- **No change required.** This path already functions. This ADR documents it for design-trail completeness.

#### Path B: Ark's extraction (write_file + discipline) — DECIDED S33

**S33 Decision (2026-04-13):** The `extract_knowledge()` tool is **removed**. Ark saves knowledge using the existing `write_file()` tool — the same tool used for scratchpad, identity, and skills. Knowledge saving is a **discipline and session-closing ritual** (P18), not a separate automated mechanism.

**What this means:**
- Ark decides autonomously what is worth saving and writes `.md` files via `write_file()` to `~/.dpc/agents/agent_001/knowledge/`.
- Ark also updates `~/.dpc/agents/agent_001/knowledge/_index.md` after each write.
- No separate LLM extraction call — Ark formulates knowledge himself, like CC does with auto-memory.
- Trigger: session-closing ritual (P18 Session Closing Protocol) + ad-hoc during session when something is clearly valuable.
- Ark is the sole decision maker for his own store. No voting, no shared consensus with Mike.

**Why remove extract_knowledge() instead of fixing it:**
- The tool ran a redundant LLM call on the same conversation Ark already participated in — overhead without added value.
- Template-collapse (P1) was a direct consequence of LLM self-reflection without external input.
- The `_index.md` desync (P2) and `.json` hardcoding (P3) were bugs in a tool that shouldn't exist.
- Mike's rationale (S33 msg [96]): "сохранение знаний это у нас ритуал в конце сессии и дисциплина" — discipline, not automation.
- CC already works this way (auto-memory) — proven pattern.

### The two paths are fully independent

- **No shared voting.** Mike does not approve Ark's saves; Ark does not approve Mike's saves.
- **No cross-store linking.** No `proposal_id` bridge between agent store and Mike store. No commit-chain between them.
- **No schema unification.** Mike's store keeps the full `KnowledgeCommit` schema (including multi-perspective fields, for now — see P1 open question). Agent store keeps the simpler tool format.
- **No visibility requirement** from one participant into the other's extraction decisions. Mike sees his own proposal in the UI (voting panel), Ark sees only the return value of his own tool call.

This mirrors the relationship in Local AI chat where the AI provider is not a "voter" — it's just the entity the user is talking to. In agent chat, Ark is closer to "a second participant with his own memory" than "a peer in a shared knowledge consensus".

### Format: `.md` for both stores

- Mike's store: already `.md` (no change).
- Agent store: must be `.md`, not `.json`. The exact markdown layout (frontmatter fields, body structure) is left to the mechanism decision (O5) — this ADR only locks in the file extension and human-readability constraint.

---

## Non-goals

The following are explicitly **not part of this ADR**:

1. **Linking agent extractions to Mike's voting flow.** This was attempted in `aba4195` → `aed5401` → reverted in `287606c`. The original failure modes (buffer consumption, surprise voting dialogs, UX breakage) were never addressed. Re-attempting linking without first solving those problems is out of scope here.
2. **Multi-party voting between agent and human.** Ark has no crypto identity. Creating one requires ADR-level work on agent identity, not extraction flow.
3. **Unified knowledge store across participants.** Each participant keeps their own store. Cross-participant discovery (e.g., "show me what Ark saved from today's session in Mike's UI") is a separate UX feature, not a prerequisite for this fix.
4. **Fixing template-collapse.** See open questions below — this is a separate decision, because the fix depends on a design choice (remove the fields, add anti-repetition, or accept the template quality).
5. **CC's memory store.** CC uses Claude Code's native auto-memory mechanism. Not part of DPC extraction redesign.
6. **Auto-detection behavior.** The `auto_detect_knowledge_in_conversations` option exists in UI and continues to work as a background trigger. This ADR does not change auto-detection behavior. All decisions apply to the button-initiated and tool-initiated paths only.

---

## Consequences

### Positive

- **Ark's future extractions will be discoverable through `_index.md`.** Once the mechanism is decided and implemented, index sync is required — no more "agent can't find what it just saved" via the default recall path.
- **Agent store becomes human-readable.** `.md` format lets Mike peek at what Ark saved without parsing JSON.
- **Architectural clarity.** The design choice is explicit: each participant is owner of their own extraction decisions, no forced consensus across participant types.
- **Mike's Extract Knowledge path is documented, not changed.** The ADR captures why the current behavior is correct and leaves it alone — protects against future "helpful" refactors that might inadvertently break it.
- **ADR trail for future debugging.** If someone touches the agent extraction mechanism in the future, this ADR explains the WHAT constraints that must be preserved even as the HOW evolves.

### Negative / Costs

- **Template-collapse continues in Mike's store** until P1 is addressed separately. Accepting mechanically-generated alternative_viewpoints as the default for now.
- **Migration cost** for 25+ existing `.json` files in the agent store. Either convert to `.md` (one-shot migration) or leave as historical artifacts. See open question O2.
- **Old `_index.md`** (36 entries referring to old manually-curated `.md` files) needs to be rebuilt to reflect the current state. See open question O3.
- **No shared session knowledge artifact.** Mike saves his version, Ark saves his version, and there is no single place showing "here's what was decided in session S28 as a joint record". The three-way asymmetry identified in P5 remains.

### Risks

- **Mechanism decision may change the shape of everything.** Since the HOW of Ark's extraction is not locked in by this ADR, the migration strategy and file formats may need revision once the mechanism is decided. This ADR deliberately avoids committing to a specific code path to prevent locking in the wrong implementation.
- **Migration script bugs:** whatever migration script ends up converting the old `.json` files touches existing data in place. Must be idempotent and reversible. Deferred until O5 resolves.

---

## Open Questions (deferred to separate decisions)

### O1: Template-collapse in Human store

Three options:
- **(a)** Remove `alternative_viewpoints` / `cultural_perspectives` / `devil_advocate_analysis` from solo-context extraction. Honest about the field being meaningless without real multi-participant input.
- **(b)** Keep fields, add anti-repetition via feeding the LLM previous `alternative_viewpoints` of the same topic as "already said, generate new ones". Preserves the PCM cognitive-bias methodology spirit but requires memory of previous outputs.
- **(c)** Keep fields as-is, accept template quality, treat them as aspirational metadata.

**Recommendation (CC):** (a) for solo/agent contexts (Local AI chat, agent chat), (b) for P2P multi-human contexts (where the fields have real data sources).

**Deferred:** needs Mike's decision. Not blocking the current ADR — the fix can ship without touching template-collapse.

### O2: Migration of 25+ existing `.json` files in agent store

Options:
- **(a)** One-shot migration script that converts each `.json` to `.md` format and adds entries to `_index.md`. Idempotent — re-running is safe.
- **(b)** Leave existing `.json` files as historical artifacts; only new extractions use `.md`. Downside: mixed format in one directory, confusing forever.

**Recommendation:** (a). One-time cost, clean state afterward.

### O3: Full rebuild of `_index.md`

After the migration in O2, `_index.md` needs to be rebuilt from filesystem scan to reflect all current files (old curated `.md` + migrated `.md` from former `.json`). Idempotent filesystem-walk script.

**Recommendation:** combine with O2 into a single migration pass.

### O4: Agent-initiated extraction heuristic

Ark currently calls `extract_knowledge()` when he decides something is worth saving (subjective intuition). This is not well-defined. Future work could add:
- Explicit triggers (e.g., after a tool sequence of certain length, after a Decision verb from Mike, after a certain number of insights in scratchpad)
- Quality metrics (e.g., how "substantive" was this session block)
- Integration with Ark's consciousness/evolution systems

**Deferred:** not in scope for this ADR. Current ad-hoc trigger is acceptable.

### O5: Agent extraction mechanism (the HOW question) — RESOLVED S33

**Resolved (2026-04-13, S33 msg [96]):** Remove `extract_knowledge()` tool entirely. Ark uses `write_file()` to save knowledge as `.md` + updates `_index.md`. Discipline and session-closing ritual (P18), not a separate automated mechanism.

This was option **(e)** — not on the original table: **no separate extraction mechanism at all**. The agent writes knowledge the same way it writes scratchpad — via the general-purpose file tool. The extraction "mechanism" is Ark's own judgment about what's worth saving, exercised through the same write_file tool used for all other agent file operations.

---

## Implementation Plan

This ADR locks only the WHAT (format, location, index, independence, Mike-side behavior unchanged). Because the HOW of Ark's extraction is deferred to O5, the implementation plan is intentionally minimal at this stage.

### Step 0: ADR review and approval (this document)

- CC drafts (this file).
- Ark reviews — checks historical accuracy, flags missed cases.
- Mike approves — makes final Decision in P13 sense.
- Revision if needed.

### Step 1: Mechanism decision for Path B (O5)

Before any code is touched, a separate decision session must resolve O5 — what mechanism Ark uses to extract knowledge, what payload/schema the tool writes, and whether the current `dpc_agent/tools/core.py:extract_knowledge()` is the foundation or needs to be replaced. The output of this step is either a new ADR or a direct verbal Decision captured in a session archive.

**Blocking:** all code steps below wait for this.

### Step 2: Agent tool implementation (blocked by Step 1)

Once O5 is resolved:
- Implement the chosen mechanism to produce `.md` output into `~/.dpc/agents/agent_001/knowledge/`.
- Update `~/.dpc/agents/agent_001/knowledge/_index.md` after each new file.
- Leave Mike's path (`knowledge_service.end_conversation_session`) untouched.

**Scope:** to be estimated once O5 is resolved — the shape of the change depends on the chosen mechanism.

### Step 3: Migration of existing 25+ `.json` files (blocked by Step 1)

Once the target `.md` layout is defined by O5, a one-shot migration script converts the existing `.json` files and rebuilds `_index.md` from a filesystem scan. Idempotent, reversible.

**Scope:** deferred until target format is decided.

### Step 4: Verification (manual test sequence)

After Steps 2 and 3, manual test:

1. **Mike's path (baseline — should be unchanged):** Press Extract Knowledge button in this agent chat. Verify proposal appears, approve, and a `.md` file appears in `~/.dpc/knowledge/` with Mike's crypto signature. This is to confirm that Steps 2-3 did not break the already-working Mike-side flow.
2. **Ark's path (new):** Ark invokes extraction. Verify `.md` file created in `~/.dpc/agents/agent_001/knowledge/`, new entry added to `_index.md`, Ark can recall via `knowledge_read()` using the index path.
3. **Migration check:** all old `.json` files converted to `.md`, index rebuilt, no orphans.

**Exit criteria:** both paths produce their respective files; indexes are consistent; no regressions in Human extraction for Local AI chat, P2P chat, or agent chat.

### Step 5: (Deferred) Template-collapse decision

Depends on O1. Orthogonal to Steps 1-4. Tracked as separate decision.

---

## References

### Source material
- `C:\Users\mike\Documents\Context\example_context.json` — PCM origin JSON, proves simple `updates` mechanism without multi-perspective fields
- `C:\Users\mike\personal-context-manager\docs\cognitive-bias-ai-llms-user-guide.md` — Mike's cognitive bias user guide, source of multi-perspective field concepts
- `ideas/cc-mike-research/optimized-crunching-rabin.md` — DPC archeology (Phase 1-5 chronology)

### Code locations
- `dpc-client/core/dpc_client_core/dpc_agent/tools/core.py:1664-1795` — `extract_knowledge` tool implementation
- `dpc-client/core/dpc_client_core/dpc_agent/tools/core.py:1753` — the hardcoded `.json` filename
- `dpc-client/core/dpc_client_core/dpc_agent/tools/core.py:1778-1782` — comment explaining the post-`287606c` design
- `dpc-client/core/dpc_client_core/service.py:4258-4260` — the bypass for agent/AI/Telegram chats
- `dpc-client/core/dpc_client_core/conversation_monitor.py:925-1004` — `_generate_commit_proposal` (shared entry point)
- `dpc-client/core/dpc_client_core/conversation_monitor.py:176-199` — `process_message` auto-trigger path (not used in this ADR but noted)
- `dpc-client/core/dpc_client_core/session_manager.py:89-290` — `propose_new_session` (Voting B / session-reset flow, separate from extraction)

### Git history
- `aba4195` (2026-03-25) — agent extract_knowledge triggered voting via consensus_manager
- `aed5401` (2026-04-01) — added `proposal_id` to KnowledgeCommit for linking
- `287606c` (2026-04-03) — REVERTED voting trigger for agent, broke Human End Session; root cause of current post-revert state

### Related ADRs
- [ADR-004 Knowledge Extraction Review](004-knowledge-extraction-review.md) — predecessor. Q4 asked "Is the Devil's Advocate mechanism adding real value or just mechanical dissent?" — S28 provides a **partial** empirical answer for the saved-field side: the `devil_advocate_analysis` field produces template-collapsed content (same text across unrelated entries in one commit, verified on both earliest and latest files). The **voting-role** Devil's Advocate mechanism (required dissenter in sessions with `participants >= 3`) has rarely fired in practice because most sessions are 1-2 participants — so S28 does not directly test the voting role. Template-collapse in the saved field is **consistent with** the "mechanical" hypothesis but does not close Q4 for the voting role.
- [ADR-007 Hooks & Middleware Infrastructure](007-hooks-middleware.md) — Phase 0 infrastructure; unrelated but shows the ADR format convention used here.

### Session archive
- Session S28 (2026-04-11) — this discussion, covering PCM archeology, three-chat-type analysis, design choice convergence, implementation plan agreement. Knowledge extraction for this session itself should produce one commit per the new flow once Steps 1-3 are deployed.

---

## Decision log (fill during review)

- [x] CC drafted (2026-04-11, this document)
- [x] Ark reviewed round 1 (flagged Q4 wording + duplication false positive)
- [x] Mike reviewed round 1 (flagged P3 wrong handler + mechanism over-prescription)
- [x] CC revised (removed P3, removed mechanism prescription from Path B, added O5, rewrote Implementation Plan)
- [x] S33 Discussion (2026-04-13): 7-layer memory model recalled, extract_knowledge vs write_file compared, Mike decided to remove extract_knowledge
- [x] O5 resolved (S33 msg [96]): remove extract_knowledge, use write_file + discipline
- [x] CC updated ADR with S33 decisions (Path B rewritten, O5 marked resolved)
- [ ] Ark reviewed round 2 (pending — this update)
- [ ] Mike approved (final)
- [ ] Step 2 (remove extract_knowledge from code) committed
- [ ] Step 3 (migration .json → .md) completed
- [ ] Merged to main
- [ ] Step 4 (verification) complete
- [ ] Step 5 (template-collapse decision) opened as separate ADR
