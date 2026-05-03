# ADR-023: Group Chat Participant Model

**Status:** Proposed
**Date:** 2026-05-03
**Authors:** CC (draft), Ark (review)
**Origin:** S89 group chat dogfooding discussion

## Context

Group chat dogfooding (S88-S89) revealed that the current group chat implementation lacks participant identity infrastructure. All messages store only `sender_name` without distinguishing humans from agents. Agents are not explicitly registered as group participants — they participate implicitly through node-level config.

Mike's four questions (S89 group chat #46):
1. How does a user choose which agents join a group?
2. How should group history work (vs agent 1:1 chat)?
3. No context counter visible in group chat
4. What are the roles in group chat?

## Decision

### Participant Schema

Each message in group history includes:

| Field | Type | Description |
|---|---|---|
| `sender_type` | `"human" \| "agent"` | Participant kind |
| `sender_name` | `string` | Display name |
| `sender_node_id` | `string` | Node that sent the message (always node_id) |
| `agent_owner` | `string \| null` | Owner node_id for agents, null for humans |

Identity: for humans, `sender_node_id` is sufficient. For agents, identity is the composite `(agent_owner, sender_name)` — globally unique across nodes.

**Backward compatibility:** Messages without `sender_type` field are treated as `"human"` (all pre-ADR-023 messages).

### Per-Group Agent Configuration

`metadata.json` extended with:

```json
{
  "group_id": "group-...",
  "name": "DPC Project",
  "members": ["dpc-node-..."],
  "agents": {
    "dpc-node-...": ["agent_001"]
  }
}
```

`agents` maps node_id to list of agent_ids allowed in this group. Agents not listed are excluded from @mention routing.

**Note:** This format works for Phase 2 (single-node). Phase 3 (multi-node) will likely require richer format with agent display names, since remote nodes don't know each other's agent_ids. Format change expected at Phase 3.

### @Mention Routing

When a group message contains `@AgentName`, routing checks:
1. Is `AgentName` listed in `metadata.agents` for this group? If not — skip.
2. Route to the agent's node for processing.
3. Agent response posted back via `send_group_agent_message`.

### UI: Group Settings

Group Settings dialog (existing modal) extended with "Agents" section below Members:
- List of node's agents with checkboxes (enabled/disabled per group)
- Agent display name + model info shown

### UI: Message Display

- Agent messages show "(agent)" badge after sender name
- Human messages show no badge (default)
- In multi-node groups: show node owner name for agents from other nodes

### Context Counter

- Show total group history size in tokens (same position as agent chat counter)
- Computed by `conversation_monitor` on each `save_history()` (already tracks `current_token_count`)
- Per-agent context window usage shown on hover/tooltip

### History Management

- Full history sent to agent (no artificial limits) — already implemented (S89)
- Context window naturally limits what fits
- New Session clears group history (existing behavior)

## Migration

| Phase | Scope | Status |
|---|---|---|
| Phase 1 | Single-node, implicit roles, `sender_name` only | DONE (S89) |
| Phase 2 | `sender_type` in messages, agent badge in UI, metadata `agents` field | NEXT |
| Phase 3 | Multi-node, explicit participant model, cross-node agent visibility | FUTURE |

## Relationship to Other ADRs

- **ADR-022** (Safety Governance): ADR-023 provides the participant identity layer that ADR-022's per-agent quotas and governance log reference. ADR-022 Layer 1 (per-node permission) maps to the `agents` field in metadata.
- **ADR-006** (Participant Model): ADR-023 extends the participant concept from P2P 1:1 to group chat with agent awareness.

## Consequences

- Messages become self-describing (human vs agent distinction survives restart and export)
- Group Settings becomes the single place for agent management per group
- Multi-node groups will need agents field synchronized across nodes (Phase 3)
