---
adr: 031
title: "Derive LLM roles per-reader from participant identity; single-writer conversation history"
status: accepted
date: 2026-06-10
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: [ADR-006, ADR-023]
related: [ADR-022, ADR-025, ADR-026]
supersedes: []
session: S199
---

# ADR-031: Participant Roles in LLM Payload — Per-Reader Derivation and Single-Writer History

## Context and Problem Statement

A live context-overflow incident (S198) and its follow-up audit (S199, backlog item GROUP-HISTORY-LLM-PAYLOAD) traced the full chain "group chat message → history.json → LLM payload" and found three defects, all reproduced on live data during the audit session itself:

1. **Duplicate trigger.** The triggering message reaches the LLM twice: once from disk history (written by the service-side monitor on receipt) and once as the current prompt (re-added by the agent-side monitor in `process_message`, then appended again by `build_llm_messages`). For long Cyrillic messages this doubles an already-undercounted token cost.
2. **Two-writer race.** Two independent `ConversationMonitor` objects — service-side (`service.py` group handlers) and agent-side (`agent_manager.py` per-conversation monitor) — write the same `history.json` with no coordination. The later writer clobbers the earlier one's records; transient duplicates can be absorbed into *another* agent's view of the history mid-race.
3. **Flat stored role.** `ConversationMonitor.on_message()` assigns `role="user"` to every incoming message regardless of `sender_type`. Verified on live data: 13/13 messages in a fresh group history carried `role="user"`, including 7 with `sender_type="agent"`. Agents never see their own prior messages as `assistant` — no user/assistant alternation reaches the model.

The root issue behind defect 3 is conceptual, not mechanical: **in a multi-party history, "assistant" is a relative role.** For agent A, A's messages are `assistant` and everyone else's are `user`; for agent B the same records map the other way. A single `role` field stored absolutely in a shared file cannot be correct for N agents reading the same history.

ADR-006 (Participant Identity Model) already established the right abstraction in April 2026: three orthogonal identity dimensions (author / participant_type / source) with LLM role mapping as a **translation layer** — "LLM constraints must not leak into UI rendering or internal message storage." Its implementation was deferred with an explicit resume trigger: *"when the quick-fix reveals new edge cases that can't be patched without the proper model."* That trigger has now fired. ADR-023 added the group participant schema (`sender_type`, `sender_node_id`, `agent_owner`) but did not design the history→LLM assembly layer; the two monitor code paths grew independently.

This ADR resolves both: it implements ADR-006's translation layer for all chat types and fixes the write-ownership gap left open by ADR-023.

## Decision Drivers

- **Correctness for N readers:** any number of agents must be able to read one shared history and each see itself as `assistant`.
- **Context economy:** the group path is the hottest path (every agent invoke); duplicate triggers directly amplify context overflow (S198 incident: real 198K tokens at an estimated 128K).
- **History integrity:** concurrent writers must not clobber or cross-contaminate records; an agent must never absorb another invoke's transient state.
- **Coverage of all chat types:** 1:1 agent chat, 1:1 P2P peer chat, local AI chat, local group chat, bridged group chat (Discord), Telegram bridge, and future multi-node groups (ADR-023 Phase 3) must share one model.
- **Bridges are sources, not models:** Telegram/Discord participants enter the same schema with a different `source`, per ADR-006.
- **Backward compatibility:** 1:1 UI paths currently detect agents via stored `role === "assistant"`; group/sync UI paths already use `sender_type`. Existing on-disk histories must keep working without a data migration.
- **Message chain integrity:** `chain_hash` currently includes `role` in its input; any storage change must not silently break MSG-CHAIN verification.

## Decision

**Store identity, derive roles.** Conversation history is a transport format carrying participant identity only; the LLM `role` is computed per-reader at payload-assembly time. Group histories get a single writer.

### 1. History storage: identity-only transport format (all chat types)

Each message stores (existing fields, now canonical):

```json
{
  "id": "uuid",
  "msg_index": 42,
  "sender_node_id": "dpc-node-...",
  "sender_name": "Mike",
  "sender_type": "human",
  "agent_owner": null,
  "source": "node | telegram | discord | vscode-bridge",
  "content": "text",
  "timestamp": "2026-06-10T...",
  "chain_hash": "...", "content_hash": "...", "signature": "..."
}
```

- `sender_type` enum follows ADR-006: `human | agent | system`. External bot accounts arriving via bridges are `agent` with the bridge as `source` (no fourth type; see Open Questions).
- `agent_owner` identifies the owning agent for `sender_type="agent"` (composite identity `(agent_owner, sender_name)` per ADR-023).
- The stored `role` field **loses authority** but **keeps being written exactly as today** (non-authoritative compat value) in all chat types. Payload assembly no longer reads it for groups — see §2. Physical removal of the field is deferred to a future format version once the fleet is upgraded.

  *Why keep writing it (S199 verification of Mike's cross-node questions):* (a) **P2P history sync** — old-build peers' payload assembly filters records by `role in (user, assistant)`; a role-less record merged from a new node would silently vanish from the old peer's agent context. Writing the compat value preserves today's behavior on old nodes bit-for-bit. (b) **Chain format** — `chain_hash` input keeps its v1 shape, so the v2-marker machinery is **not needed now**; it becomes the documented plan for the eventual field removal, not part of this ADR's implementation. (c) UI 1:1 paths keep working untouched until T5. Net: zero storage-format change in this ADR; only the *reader* changes.

### 2. Translation layer: per-reader role derivation at payload build

One function in the context-assembly path (`dpc_agent/context.py:build_llm_messages`) derives roles for the *reading* agent:

```
own message     (sender_type == "agent" and agent_owner == reader_agent_id) → role = "assistant", content as-is
everything else (humans, other agents, peers, bridge users)                 → role = "user", content = "[{sender_name}]: {content}"
system records                                                              → excluded from history turns (system prompt is Block 1-3 territory)
```

This applies to **all chat types**. In a 1:1 agent chat the derivation degenerates exactly to today's behavior (the agent's messages → `assistant`, the human's → `user`), so back-compat holds by construction. The legacy `"peer"` role in P2P chats folds into the same rule (peer = not-mine → `user` with name prefix).

Legacy records without `agent_owner` (e.g., past agent-side assistant writes that stored `sender_node_id = conversation_id`) match via fallback `sender_name == reader agent display name` — **in 1:1 conversations only**, where exactly one agent exists and the match is unambiguous. In group histories, records without `agent_owner` stay `user` (conservative: misattributing another participant's message as the reader's own output is worse than losing one alternation turn). *(Scope per Ark review R3.)*

**Chain integrity (former Q3, revised after S199 cross-node verification):** since the `role` field keeps being written as a compat value (§1), the `chain_hash` v1 input shape is **unchanged by this ADR** — no chain work needed in T4. The versioned format (`v2` marker, per Ark review R1; empty-slot rejected as fragile) is recorded here as the **agreed mechanism for the future field removal**: when `role` is physically dropped, new records prefix the chain input with a `v2` marker and records without the marker verify as v1. *Code note:* the existing verifier builds its expected input with `get("role", "")`, so even role-less records verify today with an empty slot; the marker is chosen for explicitness when that day comes.

### 3. Single-writer ownership for group histories

The **service-side monitor is the only writer** of a group's `history.json`. It already receives every message from every source (UI, P2P, agent responses, CC bridge, Discord coordinator) and assigns `msg_index`/`chain_hash` at one point.

- Agent-side monitors become **read-only consumers** for `group-*` conversation IDs: they `load_history()` before an invoke but never `add_message`/`save_history` on the group file.
- *Precedent (code-verified, S199 sweep):* the 1:1 agent-chat path already works this way — `service.py` `execute_ai_query` writes the user message into the agent's monitor and calls `process_message(..., _skip_history=True)`, so the skip mechanism is proven in production. The difference: in 1:1 both writes go through the **same monitor object** (no race by construction); the group fix extends the same contract across the two-monitor boundary.
- The **incoming P2P path** (`group_handler.py`, remote nodes' messages) already routes through the service-side monitor — correct owner — but currently (a) builds its `ConvMessage` without `sender_type`/`agent_owner`, so remote messages lose identity fields on the receiving node, and (b) never calls `save_history()`, so a remote message persists only when the next local write happens to save. Both fixed in T3 (propagate identity fields from the payload into the stored record; explicit save).
- `_invoke_agent_in_group` no longer relies on the agent re-adding the trigger: the trigger is already on disk. The agent's response returns to `send_group_agent_message`, which is a service-side write.
- The "exclude current message from prior history" step switches from the positional `full_history[:-1]` slice to **dedup by `message_id`** against the current trigger. The positional slice is exactly what breaks under concurrency (it can cut a different participant's message that landed mid-invoke and leave the duplicate in).

### 4. Bridges (Telegram, Discord) are sources — with different topologies

Bridge participants enter the same storage schema (`source="telegram"` / `source="discord"`), but the two bridges map onto **different chat types** (verified in code, S199):

- **Telegram — relay into 1:1.** The bot relays each Telegram chat into a dedicated conversation (`agent-{agent_id}` when linked to an agent, else `telegram-{chat_id}`), single-participant monitor, sender identity = the Telegram user's name (`telegram_coordinator._get_or_create_conversation_id`). This is the 1:1 model: one writer, absolute roles, derivation degenerates to current behavior. No group involvement today.
- **Discord — per-user 1:1 plus group mirror.** Each Discord user gets their own agent conversation (`discord-user-{discord_user_id}`, `discord_coordinator.get_user_conversation_id`) where the actual LLM exchange happens — again the 1:1 model. Separately, `mirror_to_dpc_group` copies Discord messages into the configured DPC mirror group through the **service-side monitor** with synthetic identity (`sender_node_id="discord-{user_id}"`, `sender_name="{name} (Discord)"`, `sender_type="human"`, `message_id="discord-{message.id}"`), and the agent's reply is echoed via `send_group_agent_message`. The mirror is therefore a **group-type history** and inherits §1–§3 in full: per-reader derivation and single-writer ownership apply to it like to any other group.

Net: bridges do not introduce a third role model. Telegram resolves to the 1:1 case; Discord resolves to the 1:1 case (per-user) plus the group case (mirror). The synthetic identity fields the Discord mirror already writes are exactly what derivation needs. When bridged groups or multi-node groups (ADR-023 Phase 3) land, they inherit §1–§3 unchanged because the model never depended on which transport delivered the message.

### 5. UI migration: identity-based sender detection

All UI sender/agent detection flows through `mapBackendMessage`/`mapMessageSender` using identity fields (`sender_type`, `sender_node_id`, `agent_owner`), removing the remaining `role === "assistant"` detection in 1:1 reload paths (`AgentPanel.svelte`, `+page.svelte` history reload). This absorbs the existing backlog item MESSAGEMAPPER-1:1-RELOAD-REFACTOR. Group/sync panels already use `sender_type` and need no behavioral change.

### Rationale

- **Why derivation instead of fixing the stored value:** mapping `sender_type` → `role` at write time (e.g., agents → `assistant`) is still an absolute role and is wrong the moment a second agent reads the history — *every* agent would see *all* agents' messages as its own output. Only per-reader derivation is correct for N readers; this is ADR-006's "only ONE agent per conversation can be role: assistant (the responding model)" constraint, generalized.
- **Why single-writer instead of locking:** file locks would serialize writes but keep two divergent in-memory states that still clobber each other logically (lost updates survive locking). One owner with one in-memory state removes the class of bug; agents have no need to write group history once the trigger-duplication path is gone.
- **Why no data migration:** identity fields (`sender_type` since ADR-023, `sender_name` always) are already present in existing histories. Derivation works retroactively at read time — old flat-`user` group histories produce correct alternation immediately, without rewriting files or breaking `chain_hash` on existing records.

## Considered Options

- **Option A — Hotfix only (`_skip_history=True` on the group invoke path):** stops the duplicate trigger, leaves the race and flat roles.
- **Option B — Store role per reader:** N role values per message (one per agent). Storage explosion, schema churn on every agent add/remove.
- **Option C — Fix role at write time (`sender_type="agent"` → `"assistant"`):** still an absolute role; wrong for every reader except the message's author. Also rewrites chain-hashed records.
- **Option D — Identity-only storage + per-reader derivation + single writer (chosen):** correct for N readers, no data migration, removes the race class, implements the already-accepted ADR-006 model.

### Pros and Cons of the Options

#### Option A — hotfix skip-flag
- Good: smallest diff, immediate token relief.
- Bad: race remains (cross-contamination between agents); flat role remains (no self-continuity); `[:-1]` slice still cuts the wrong message under concurrency — with the skip-flag it would cut a *legitimate* last message.

#### Option B — per-reader stored roles
- Good: assembly becomes a lookup.
- Bad: O(N agents) storage per message; every membership change invalidates stored roles; chain hashing over mutable maps; solves nothing A/D don't.

#### Option C — write-time role from sender_type
- Good: one-line change in `on_message`.
- Bad: semantically wrong for multi-agent groups (all agents' messages become `assistant` for every reader); requires migrating existing records; keeps two writers.

#### Option D — chosen
- Good: correct by construction for any reader count; retroactive on existing data; deletes the race class; aligns storage with ADR-006's accepted model; 1:1 behavior unchanged by derivation.
- Neutral: payload assembly takes the reader's identity as an input (it already receives the agent instance today).
- Bad: touches the hottest path (needs the verification matrix below); UI migration spans several Svelte components; stored `role` lingers in 1:1 histories until §5 completes.

## Consequences

- **Positive:** agents regain self-continuity in groups (own messages as `assistant`); trigger tokens stop doubling; group histories stop clobbering each other; one model covers all current and planned chat types including multi-node groups; GROUP-HISTORY-LLM-PAYLOAD (all three defects) closes.
- **Negative:** the `role` key persists in storage as a semantically hollow compat value for groups (documented, not removed) — a reader unaware of this ADR could mistake it for authoritative; physical removal is deferred to a future format version with the v2 chain marker.
- **Neutral:** `chain_hash` inputs for *new* group messages change shape (versioned, no `role` component) — chain verification stays per-record-forward, existing records keep their original hashes valid. P2P peer-chat histories see a behavioral improvement: records stored with the legacy `role="peer"` are silently **dropped** from agent payloads today (the assembly filter passes only user/assistant); under derivation they become `user` turns and reach the model.
- **Found during verification, tracked separately (not blockers for this ADR):** (a) the `on_message` path **double-appends** every message to the knowledge-extraction buffers — once directly with real sender identity, once via `add_message`'s synthetic copy whose sender is guessed from `role` (wrong in groups: everything maps to participants[0]) — extraction sees each message twice and the auto-detect threshold fires early; (b) morning-brief posting falls back to the agent **folder id** as display name when the group's `agent_names` map is empty (live in S199 history: `sender_name="agent_001"`), reinforcing why name-based reader matching is 1:1-only (R3); (c) the CC bridge writes 1:1 records with `sender_node_id="cc"` and no `sender_type` — harmless for derivation (CC is never the reader) but in scope for the UI identity migration (T5).

## Confirmation

- [ ] Group invoke payload contains the trigger exactly once (log the assembled message count vs history length).
- [ ] In a group with ≥2 agents, each agent's payload shows its *own* prior messages as `assistant` and the other agent's as `user` with `[Name]:` prefix (capture two consecutive invokes, diff the payloads).
- [ ] A second participant's message landing mid-invoke is neither dropped from the next payload nor duplicated (race test: send during agent processing).
- [ ] Group `history.json` message count is monotonically increasing across a multi-agent session (no clobber: write counter assertions in service-side monitor logs).
- [ ] 1:1 agent chat payload keeps correct user/assistant **alternation** (semantic check: the agent's own prior messages arrive as `assistant`, never flat-user) AND is payload-equivalent pre/post change for the same history (token-count equivalence; byte-identical where prefixes match). *(Reworded per Ark review R2 — format equality alone does not prove role semantics.)*
- [ ] UI renders sender attribution correctly in 1:1 and group chats after the `role`-detection removal (manual pass per ADR-006 rendering table).
- [ ] MSG-CHAIN verification on an existing group history behaves identically pre/post change (storage format untouched per §1 revision; this criterion guards against accidental write-path drift).
- [ ] P2P history sync round-trip with an old-build peer: records merge in both directions, no message loss in either side's agent payload (Q5 verification).

## Scope

Backend:
- `dpc_client_core/conversation_monitor.py` — `role` write path unchanged (compat value per §1); document the field as non-authoritative for groups in the docstring; no chain-input change in this ADR.
- `dpc_client_core/service.py` — group handlers (`send_group_message`, `send_group_agent_message`, `_invoke_agent_in_group`) become the single write path; pass reader identity to the invoke.
- `dpc_client_core/managers/agent_manager.py` — `process_message` group path: monitor becomes read-only (no `add_message`/`save_history` for `group-*`).
- `dpc_client_core/dpc_agent/agent.py` — `process()` prior-history selection: replace the positional `full_history[:-1]` slice with `message_id` dedup against the current trigger (the slice is the concurrency hole named in Context defect 1/2; flagged by Ark review as a critical change point).
- `dpc_client_core/dpc_agent/context.py` — `build_llm_messages` per-reader role derivation (single translation function).
- `dpc_client_core/message_handlers/group_handler.py` — propagate `sender_type`/`agent_owner` from the P2P payload into the stored record; explicit `save_history()` after `on_message` (found in S199 verification sweep).
- `dpc_client_core/coordinators/telegram_coordinator.py`, `coordinators/discord_coordinator.py` — ensure `source` is populated; no model change.
- CC bridge write path (`service.py` agent-chat CC handler) — add `sender_type="agent"` to CC records (UI migration prerequisite, T5).

Frontend:
- `src/lib/utils/messageMapper.ts` — canonical identity-based sender mapping (absorbs MESSAGEMAPPER-1:1-RELOAD-REFACTOR).
- `src/lib/panels/AgentPanel.svelte`, `src/routes/+page.svelte` — replace `role === 'assistant'` agent detection with mapper.
- `src/lib/panels/HistorySyncPanel.svelte`, `src/lib/panels/ChatHistorySyncPanel.svelte` — drop `role`-based fallbacks once identity fields are guaranteed.

Docs:
- ADR-006 — add addendum: implementation resumed via ADR-031.
- ADR-023 — cross-reference: history→LLM assembly layer defined here.
- `docs/GROUP_CHAT.md` — covered by existing GROUP-CHAT-DOCS-AUDIT backlog item.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| T1 Translation layer in `build_llm_messages` (per-reader derivation + legacy fallbacks) | Pending | — |
| T2 Trigger dedup by `message_id` (replaces positional slice) | Pending | — |
| T3 Single-writer: agent-side monitor read-only for groups | Pending | — |
| T4 `group_handler` identity propagation + explicit save; P2P sync compat check (role write stays as-is per §1 revision) | Pending | — |
| T5 UI migration to identity-based mapping | Pending | — |
| T6 Verification matrix (Confirmation checklist) on live multi-agent group | Pending | — |

Suggested order: T1+T2 first (token relief + correctness, no storage change), then T3+T4 (write ownership), then T5 (UI), T6 throughout.

## Open Questions

- ~~**Q1:** Reader-matching for legacy agent records without `agent_owner`~~ — **RESOLVED (Ark review R3):** `sender_name` fallback in 1:1 only; group records without `agent_owner` stay `user`. See Decision §2.
- ~~**Q2:** `sender_type` enum~~ — **RESOLVED (Mike, S199):** keep ADR-006's `human | agent | system` plus the `source` dimension. External bridge bots are `agent` with `source="telegram-bot"` / `source="discord-webhook"`; no fourth type (taxonomic drift without need).
- ~~**Q3:** `chain_hash` input shape for role-less records~~ — **RESOLVED (Ark review R1):** versioned chain format (`v2` marker), empty-slot rejected. Prototype in T4: @CC, review: @Ark. See Decision §2.
- **Q4:** Multi-node groups (ADR-023 Phase 3): remote agents' `agent_owner` is only unique per node — confirm composite `(sender_node_id, agent_owner)` as the reader-matching key for cross-node histories. — design check at Phase 3 start
- ~~**Q5:** CHAT_HISTORY_REQUEST/RESPONSE P2P sync with peers running older builds~~ — **RESOLVED (S199 code verification, triggered by Mike's cross-node question):** `merge_history` is role-agnostic — dedup by message `id`, per-message signature over `content_hash` whose input (`id|sender_node_id|content|timestamp`) **does not include `role`**, records appended verbatim via `add_message_with_id`. With the compat `role` value still written (§1), both directions are bit-compatible with old builds; nothing changes on the wire. Remote inference (compute sharing) is likewise unaffected: `RemotePeerProvider.generate_response` sends a locally-assembled flat prompt string over REMOTE_INFERENCE_REQUEST — roles are consumed at local assembly time and never cross the wire. *(Pre-existing, not ADR-caused: merged remote records keep the remote node's `chain_hash`, so the local chain verifier may warn on merged group histories — same behavior before and after this ADR.)*

## Authors

Workflow roles per Protocol 13:

- **Mike** (project owner, deciding node) — Decision; framed the core question ("roles are our abstraction; cover all chat types"), pointed to ADR-006.
- **CC** (Claude Code, external agent via VSCode bridge) — Investigation (full chain trace, live-data verification, UI field inventory, bridge topology check), draft.
- **Ark** (DPC embedded agent, agent_001) — Independent verification of all three defects, role-model analysis (alternation/self-continuity rationale), fix-direction options, review R1–R4.

## References

- [ADR-006](006-participant-model.md) — participant identity model (three dimensions, translation-layer principle, multi-agent role constraint).
- [ADR-023](023-group-chat-participant-model.md) — group participant schema, mention routing, Phase 3 multi-node outlook.
- [ADR-022](022-multi-agent-safety-governance.md) — per-agent identity referenced by quotas/governance.
- [ADR-025](025-discord-integration.md), [ADR-026](026-public-agent-guardrails.md) — bridge integrations covered by the `source` dimension.
- backlog.md `GROUP-HISTORY-LLM-PAYLOAD` (S198/S199) — defect evidence and origin; `AGENT-EMPTY-RESPONSE-RETRY` — the overflow incident that triggered the audit (fixed separately in `6a75701`).
