# Task 004: DiscordBridge Coordinator (Phase 2)

**Status:** TODO
**Phase:** 2
**Effort:** ~150 lines
**Depends on:** Task 003

## Description

Create coordinators/discord_coordinator.py — bidirectional DPC ↔ Discord message routing and conversation linking. Mirrors TelegramCoordinator.

## Files

- `dpc_client_core/coordinators/discord_coordinator.py` — NEW (~150 lines)
- `dpc_client_core/discord_service.py` — wire coordinator

## Implementation

- Conversation linking: Discord channel_id ↔ DPC conversation_id
- DPC → Discord: forward P2P messages to linked Discord channel
- Discord → DPC: forward Discord messages to linked DPC conversation
- File/image forwarding: discord.File for attachments
- Thread-per-conversation: create Discord thread for each session
- Thread title = first 100 chars of session topic
- Auto-archive after 1h inactivity (Discord default)

## Done criteria

- Messages flow bidirectionally between DPC conversation and Discord channel
- Files/images forwarded in both directions
- New conversation creates Discord thread
- Thread auto-archives after inactivity
