# Group Chat (v0.26.0)

D-PC Messenger's group chat enables multi-participant communication with full feature parity to 1:1 P2P chat: text, files, voice messages, screenshots, voice transcription, knowledge commits, and session management.

Since v0.19.0 the model expanded substantially: **agents are first-class participants** (ADR-023), the LLM payload uses **per-reader role derivation** (ADR-031), `@Name`/`@all`/`@CC` mentions route to agents, and history reconciles **cross-node via a hash-based gate**.

## Architecture

Group chat is "fan-out the same message to N participants under a `group_id` envelope." Each member keeps its own copy of group metadata and conversation history (eventual consistency via `GROUP_SYNC`).

There are **two participant types**:
- **Humans** — P2P peer nodes.
- **Agents** — local LLM instances. Agents are **per-node**: each node decides which of its own agents participate in a given group. Agent messages are attributed with `is_agent=True` and a display name.

### Data Model

Group metadata stored at `~/.dpc/groups/{group_id}.json`:

```json
{
  "group_id": "group-<uuid4>",
  "name": "Project Planning",
  "topic": "Sprint 5 planning",
  "created_by": "dpc-node-<creator>",
  "created_at": "2026-03-01T10:00:00Z",
  "members": ["dpc-node-self-abc", "dpc-node-alice-123"],
  "agents": {
    "dpc-node-self-abc": ["agent_001", "agent_002"]
  },
  "agent_names": {
    "dpc-node-self-abc": { "agent_001": "Ark", "agent_002": "Warren" }
  },
  "version": 1
}
```

- `group-` prefix distinguishes from `dpc-node-` peer IDs and `local_ai`.
- `members` — human peer node IDs.
- `agents` — `{node_id → [agent_id]}`: which agents each node has assigned to this group (per-node opt-in).
- `agent_names` — `{node_id → {agent_id → display_name}}`: resolved names for UI and `@mention` matching.
- `version` — incremented on metadata changes; drives `GROUP_SYNC` convergence (highest version wins; equal-version divergence broken by a deterministic content-hash tie-break).

## Protocol Messages

| Command | Purpose |
|---------|---------|
| `GROUP_CREATE` | Share group metadata, invite members |
| `GROUP_TEXT` | Text message in group |
| `GROUP_LEAVE` | Notify departure |
| `GROUP_DELETE` | Creator deletes group (all members remove it) |
| `GROUP_DELETED_STATUS` | Exchange deleted group IDs on connect (notify of deletions that happened while offline) |
| `GROUP_SYNC` | Metadata reconciliation on connect — carries the full group dict (incl. `agents`/`agent_names`); version + content-hash tie-break |
| `GROUP_HISTORY_REQUEST` / `GROUP_HISTORY_RESPONSE` | Chat-history reconciliation (hash-based, bidirectional) |
| `CHAT_HISTORY_RESPONSE` | Full-history push to a newly added member on join |
| `cc_group_mention` *(local event)* | Broadcasts an `@CC` mention to the Claude Code CLI bridge |

Files, voice, and screenshots use the existing `FILE_OFFER` with an optional `group_id` field; each member runs an independent transfer.

## Agent Participation (ADR-023)

Agents are full group participants. This is the largest change since v0.19.0.

### Per-node agent assignment
Each node configures which of its **local** agents participate in a group (stored in `group.agents[node_id]`, names in `group.agent_names[node_id]`). Assignments propagate to peers via `GROUP_SYNC`. There is no cross-node agent sharing — an agent runs only on the node it belongs to.

### Mention routing
A human message is parsed for mentions (fenced code blocks are stripped first, so technical text doesn't trigger agents):
- `@AgentName` — invokes that specific agent.
- `@all` — fans out to **all** agents in the group. Human-sender only; agents cannot `@all` (anti-cascade).
- `@CC` — emits a `cc_group_mention` local event to the Claude Code bridge.

An agent's own name is excluded when matching mentions in its own message, preventing self-trigger loops.

### Per-group serialization
Agents in the same group run **sequentially** (one `asyncio.Lock` per `group_id`), so concurrent invocations don't clobber each other's progress or pile LLM load on one group. Different groups still run agents in parallel.

### Agent context tracking
The backend tracks each agent's real prompt size per group (prompt tokens, context-window limit, timestamp, display name). The UI token counter shows the worst agent's percentage of its **own** window, attributed by name (see TOKEN-COUNTER-AGENT-ATTRIBUTION).

## Per-Reader Role Derivation (ADR-031)

In a group with multiple agents, the same conversation needs **different role labels per reader**: a message authored by agent A is `assistant` to A but `user` to everyone else.

- Roles are derived **per-reader at payload-build time**, not stored per-message.
- When building the LLM payload for agent A: A's own past messages → `assistant`; all others (humans + other agents) → `user`.
- **Single-writer history:** group agent invocations use `_skip_history=True`; the group handler owns history writes centrally, avoiding the dual-writer races that corrupted roles pre-ADR-031.

See [docs/decisions/031-participant-roles-llm-payload.md](decisions/031-participant-roles-llm-payload.md).

## Cross-Node History Sync

On connect, nodes reconcile group history so all members converge.

- **Hash-based bidirectional gate:** each node advertises `{count, hash}` of its group history (hash = content digest of the message set). Matching hashes → in sync; otherwise both sides exchange missing messages. The old **count-based** gate was removed — two nodes can have the same count but different messages.
- **Disk as source of truth:** history is read from `history.json` on disk (not the in-memory monitor, which may be unloaded), and the disk fast-path exposes a stable `message_id` (normalised via `setdefault`) for dedup.
- **message_id dedup:** both disk- and monitor-paths expose `message_id`; the frontend merges by it, fixing the post-sync double-render (GROUP-HISTORY-UI-DOUBLE-LOAD).
- **GROUP_SYNC tie-break:** metadata convergence uses the version counter; equal versions resolve by deterministic content hash. Topic edits broadcast a `GROUP_SYNC` with an incremented version.

## Features

### Text Messaging
- Fan-out via `p2p_manager.send_message_to_peer()` to each connected member.
- Deduplication via `{group_id}:{message_id}` compound key.

### File Transfer / Voice / Screenshots / Voice Transcription
- `FILE_OFFER` carries `group_id`; each member accepts independently.
- Voice (WAV via Rust backend) and screenshots (Ctrl+V → `send_group_image()`) fan out as file transfers; auto-transcribe works for group voice.
- The first member to transcribe shares the result with all via `VOICE_TRANSCRIPTION` fan-out.

### Knowledge Commits
- "End Session & Save Knowledge" works for groups; a `ConversationMonitor` keyed by `group_id` tracks the conversation, and `consensus_manager` runs multi-party voting (devil's advocate for 3+ participants).

### Group Sleep & Morning Briefs
- A per-group **Sleep** button triggers sleep consolidation; agents post **morning briefs** into the group chat. `_delete_group_briefs` removes the previous briefs when new ones arrive.

### Group Metadata Injection
- Each agent invocation receives a `chat_context` describing the group: `chat_type: "group"`, `chat_name`, `chat_id`, `description` (topic), and a `participants` list with role labels (User / peer / agent).

### Session Reset
- "New Session" voting broadcasts to all members via `propose_new_session()`.

## Group Management

- **Create** — name + topic + peer checklist via NewGroupDialog.
- **Leave** — member leaves; remaining members notified.
- **Delete** — creator-only; broadcasts `GROUP_DELETE`; all members clean up, including the on-disk conversation directory (GROUP-DELETE-FOLDER-REMNANT fix). Peers that were offline learn of the deletion via `GROUP_DELETED_STATUS` on next connect.
- **Sync** — `GROUP_SYNC` on connect (version + content-hash tie-break); history reconciliation (hash-based); newly added members receive a full history push (`CHAT_HISTORY_RESPONSE`).

## Backend Files

| File | Purpose |
|------|---------|
| `managers/group_manager.py` | GroupManager + GroupMetadata: CRUD, persistence |
| `message_handlers/group_handler.py` | MessageHandler subclasses for group commands (mention routing, agent invocation, sync) |
| `service.py` | Group API, agent invocation + serialization, mention routing, history sync |
| `conversation_monitor.py` | Per-group monitors (lazy-loaded), disk-SSoT history read + `message_id` normalisation |
| `consensus_manager.py` | Multi-party knowledge voting (devil's advocate for 3+ participants) — shared with 1:1 |
| `session_manager.py` | New-session voting (`propose_new_session`) — shared with 1:1 |
| `file_transfer_manager.py` | `group_id` on FileTransfer, group routing |

## Frontend Files

| File | Purpose |
|------|---------|
| `coreService.ts` | Group stores, event handlers, API functions |
| `+page.svelte` | Group routing for all message types, notifications |
| `panels/GroupPanel.svelte` | Group view, @mention autocomplete, toasts |
| `NewGroupDialog.svelte` | Group creation dialog |
| `Sidebar.svelte` | Group listing, member count, unread badges, agent assignment |
| `ChatMessageList.svelte` | Sender / agent attribution |

## WebSocket Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `create_group_chat` | name, topic, member_node_ids | Create new group |
| `send_group_message` | group_id, text | Send text to group |
| `send_group_image` | group_id, image_base64, filename, text | Send screenshot |
| `send_group_voice_message` | group_id, audio_base64, duration_seconds, mime_type | Send voice |
| `send_group_file` | group_id, file_path | Send file |
| `send_group_agent_message` | group_id, agent_name, text | Post a message as a local agent (used by the CC bridge) |
| `update_group_topic` | group_id, topic | Update topic (creator-only; broadcasts `GROUP_SYNC`) |
| `add_group_member` | group_id, node_id | Add member + push full history |
| `remove_group_member` | group_id, node_id | Remove member; broadcast `GROUP_SYNC` |
| `get_groups` | (none) | List all groups |
| `leave_group` | group_id | Leave a group |
| `delete_group` | group_id | Delete group (creator-only) |

## Limitations

- **Agent `@all` is human-only** — agents cannot trigger cascading agent invocations.
- **No cross-node agent sharing** — an agent runs only on the node it is assigned to.
- **No offline delivery** — messages reach only connected members (gossip integration deferred).
- **No typing indicators**, **no invite codes** — deferred.
