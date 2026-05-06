# Task 002: DiscordService + DiscordBotManager (Phase 1)

**Status:** TODO
**Phase:** 1
**Effort:** ~150 lines
**Depends on:** Task 001

## Description

Create discord_service.py and managers/discord_manager.py — bot lifecycle, whitelist, basic message sending. Mirrors TelegramService + TelegramBotManager.

## Files

- `dpc_client_core/discord_service.py` — NEW (~50 lines)
- `dpc_client_core/managers/discord_manager.py` — NEW (~100 lines)
- `dpc_client_core/settings.py` — add [discord] config section
- `pyproject.toml` — add `discord.py>=2.7.0` dependency

## Implementation

- DiscordService: init, start(), stop(), injected into CoreService
- DiscordBotManager: discord.Client subclass, on_ready, on_message handler
- Whitelist: allowed_guild_ids + allowed_channel_ids from config.ini
- send_message(channel_id, text) helper
- Background asyncio task for bot.run()

## Done criteria

- Bot connects to Discord, shows online
- Whitelist filters messages from unauthorized channels
- send_message delivers text to specified channel
- Graceful shutdown on CoreService stop
