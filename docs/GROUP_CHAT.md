# Group Chat (v0.19.0)

D-PC Messenger's group chat enables multi-participant communication with full feature parity to 1:1 P2P chat: text, files, voice messages, screenshots, voice transcription, knowledge commits, and session management.

## Architecture

Group chat is architecturally "fan-out the same message to N peers with a `group_id` envelope." Each member maintains their own copy of group metadata and conversation history.

### Data Model

Group metadata stored at `~/.dpc/groups/{group_id}.json`:

```json
{
  "group_id": "group-<uuid4>",
  "name": "Project Planning",
  "topic": "Sprint 5 planning",
  "created_by": "dpc-node-<creator>",
  "created_at": "2026-03-01T10:00:00Z",
  "members": ["dpc-node-self-abc", "dpc-node-alice-123", "dpc-node-bob-456"],
  "version": 1
}
```

- `group-` prefix distinguishes from `dpc-node-` peer IDs and `local_ai`
- Each member stores their own copy (eventual consistency via GROUP_SYNC)
- Version counter incremented on membership changes (highest wins on sync)

## Protocol Messages

| Command | Purpose |
|---------|---------|
| `GROUP_CREATE` | Share group metadata, invite members |
| `GROUP_TEXT` | Text message in group |
| `GROUP_LEAVE` | Notify departure |
| `GROUP_DELETE` | Creator deletes group (all members remove it) |
| `GROUP_SYNC` | Metadata reconciliation on connect (version check) |
| `GROUP_HISTORY_REQUEST` / `GROUP_HISTORY_RESPONSE` | Chat history sync on reconnect |

Files, voice, and screenshots use existing `FILE_OFFER` with optional `group_id` field. Each member receives an independent file transfer.

## Features

### Text Messaging
- Fan-out via `p2p_manager.send_message_to_peer()` to each connected member
- Deduplication via `{group_id}:{message_id}` compound key
- Sender name displayed in chat panel

### File Transfer
- `FILE_OFFER` payload includes `group_id`
- Each member accepts independently
- Completed transfers routed to group conversation via `group_file_received` event

### Voice Messages
- Same as P2P voice (WAV recording via Rust backend)
- Fan-out file transfers to each member
- Auto-transcribe works for group voice messages

### Screenshots
- Ctrl+V paste triggers `send_group_image()` fan-out
- Each member receives image via file transfer with `image_metadata`

### Voice Transcription
- First member to transcribe shares with all via `VOICE_TRANSCRIPTION` fan-out
- `_broadcast_voice_transcription()` sends to all connected group members when `group_id` is set

### Knowledge Commits
- "End Session & Save Knowledge" works for group conversations
- `ConversationMonitor` keyed by `group_id` tracks group conversation
- `consensus_manager` handles multi-party voting (devil's advocate for 3+ members)

### Session Reset
- "New Session" voting broadcast to all group members via `propose_new_session()`
- Group members list passed as participants

### Group Management
- **Create**: Name + topic + peer checklist via NewGroupDialog
- **Leave**: Member leaves, notification sent to remaining members
- **Delete**: Creator-only action, broadcasts GROUP_DELETE, all members clean up
- **Sync**: GROUP_SYNC on peer connect, highest version wins

## Backend Files

| File | Purpose |
|------|---------|
| `managers/group_manager.py` | GroupManager + GroupMetadata: CRUD, persistence |
| `message_handlers/group_handler.py` | 7 MessageHandler subclasses for group commands |
| `service.py` | Group API methods, fan-out wrappers, session/knowledge extensions |
| `file_transfer_manager.py` | `group_id` on FileTransfer, group routing |

## Frontend Files

| File | Purpose |
|------|---------|
| `coreService.ts` | Group stores, event handlers, API functions |
| `+page.svelte` | Group routing for all message types, notifications |
| `NewGroupDialog.svelte` | Group creation dialog |
| `Sidebar.svelte` | Group listing with member count, unread badges |
| `ChatPanel.svelte` | Sender name display for group messages |

## WebSocket Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `create_group_chat` | name, topic, member_node_ids | Create new group |
| `send_group_message` | group_id, text | Send text to group |
| `send_group_image` | group_id, image_base64, filename, text | Send screenshot |
| `send_group_voice_message` | group_id, audio_base64, duration_seconds, mime_type | Send voice |
| `send_group_file` | group_id, file_path | Send file |
| `get_groups` | (none) | List all groups |
| `leave_group` | group_id | Leave a group |
| `delete_group` | group_id | Delete group (creator-only) |

## Limitations

- **No offline delivery**: Messages only reach connected members (gossip integration deferred)
- **No typing indicators**: Deferred to future release
- **No invite codes**: Groups created by selecting connected peers
- **Auto-accept invites**: GROUP_CREATE auto-adds to sidebar (accept/decline dialog deferred)
