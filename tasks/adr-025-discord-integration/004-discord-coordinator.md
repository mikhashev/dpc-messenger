# Task 004: DiscordBridge Coordinator (Phase 2)

**Status:** IN PROGRESS (S101)
**Phase:** 2
**Effort:** ~250 lines total
**Depends on:** Task 003

## Subtasks

### 004a: Coordinator + bidirectional routing — DONE (S101, `3a43283`)
- `coordinators/discord_coordinator.py` — NEW (141 lines)
- `discord_service.py` — refactored to delegate to coordinator
- Conversation linking: channel_id ↔ agent_id with persistence
- Discord → Agent: handle_mention() routing
- Agent → Discord: forward_to_discord() method

### 004b: Discord mirror to DPC group — DONE (S101, `49cb216`)
- Mirror all Discord messages to configured DPC group chat
- Config: `[discord] mirror_group_id` in config.ini
- Tested: messages appear in DPC "Discord General" group

### 004c: Mirror cleanup — DONE (S102, `440302e`)
- Clean raw Discord mentions (`<@ID>` → display name) before mirroring
- Echo Iris responses to mirror group via `_echo_response_to_mirror()`
- +21/-4 lines in discord_coordinator.py

### 004d: Per-user conversation history — DONE (S102, `71e53d4`)
- Discord user ID → DPC conversation_id mapping (`discord-user-{id}`)
- Each @Iris dialog = separate 1:1 session per Discord user
- Mirror group = observation layer, per-user history = canonical
- Persistence in discord_conversation_links.json
- +17/-2 lines in discord_coordinator.py

### 004e: Thread-per-conversation — TODO (nice-to-have)
- Create Discord thread for each session
- Thread title = first 100 chars of topic
- Auto-archive after 1h inactivity

### 004f: File/image forwarding — TODO (nice-to-have)
- discord.File for attachments in both directions

## Done criteria

- Messages flow bidirectionally between DPC and Discord
- Per-user conversation history maintained
- Mirror group shows all Discord activity including Iris responses
- Clean display (no raw Discord IDs)
