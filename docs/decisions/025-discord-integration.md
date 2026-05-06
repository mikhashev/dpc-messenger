# ADR-025: Discord Integration

**Status:** Accepted
**Date:** 2026-05-06
**Session:** S97
**Authors:** Ark (research, architecture analysis), CC (research, implementation plan), Mike (direction, decision)
**Depends on:** None (standalone, mirrors Telegram pattern)

## Context

DPC Messenger has ~3000 GitHub clones but zero community feedback channel. Users try the project but have no way to ask questions, report issues, or connect with the team. Telegram integration (v0.14.0) proved the messaging bridge pattern works for agent interaction. Discord is the natural next step — larger developer community, richer features (threads, embeds, reactions).

### Goals

1. **Community feedback channel** — Discord server where cloners can discuss, report issues, ask questions
2. **Agent interaction** — Ark communicates with community members via Discord bot (same pattern as Telegram bridge)
3. **Reuse existing architecture** — mirror the Telegram 3-layer pattern, minimize new code

## Decision

### 1. Library: discord.py

discord.py v2.7+ (github.com/Rapptz/discord.py). Same async model as python-telegram-bot 21.x. Native slash commands, Gateway Intents, built-in rate limit handling.

### 2. Architecture: mirror Telegram bridge pattern

```
DiscordService (discord_service.py)
  ├── DiscordBotManager (managers/discord_manager.py) — lifecycle, whitelist, sending
  ├── DiscordBridge (coordinators/discord_coordinator.py) — message routing
  └── AgentDiscordBridge (managers/agent_discord_bridge.py) — agent interaction
```

Each component mirrors its Telegram counterpart:

| Discord | Telegram equivalent | Purpose |
|---------|-------------------|---------|
| DiscordService | TelegramService | Lifecycle, injected into CoreService |
| DiscordBotManager | TelegramBotManager | Bot init, whitelist, send helpers |
| DiscordBridge | TelegramCoordinator | Bidirectional DPC ↔ Discord routing |
| AgentDiscordBridge | AgentTelegramBridge | Agent events, commands, @mention routing |

### 3. Configuration

```ini
[discord]
enabled = false
bot_token_env = DISCORD_BOT_TOKEN
allowed_guild_ids = []
allowed_channel_ids = []
morning_brief_channel_id =
```

### 4. Gateway Intents

Required privileged intents:
- `message_content` — read message text (must enable in Discord Developer Portal)
- `guilds` — server awareness
- `dm_messages` — direct messages

**Important:** `message_content` is a privileged intent — without it the bot cannot read message text at all. Privileged intents are auto-approved for bots in <100 servers. DPC is personal-scale, no Discord verification needed. This is a Phase 1 prerequisite, not optional.

### 5. Agent interaction model

- Dedicated `#ark` channel for agent conversations
- **Whitelist enforcement:** Agent responds only in explicitly whitelisted channels (`allowed_channel_ids`) + DMs. @mention in non-whitelisted channels is silently ignored (same as Telegram `allowed_chat_ids` pattern)
- Slash commands: `/ask`, `/status`, `/sleep`, `/extract_knowledge`, `/help`
- Morning brief posted to configured `morning_brief_channel_id` (default: `#ark`) on wakeup, triggered by sleep consolidation pipeline
- Sleep/event notifications via Discord embeds

### 5a. Rate limit strategy

discord.py handles rate limits automatically via exponential backoff. Agent long messages (morning briefs, knowledge extraction) split into multiple messages with discord.py's built-in queue. No manual throttling needed for <5 msg/5sec typical agent output.

### 6. Key differences from Telegram

| Aspect | Telegram | Discord |
|--------|----------|---------|
| Connection | HTTP polling | WebSocket gateway (persistent) |
| Channels | Flat chats | Server → Channels → Threads |
| Rate limits | 30 msg/sec | 5 msg/5sec per channel |
| Formatting | Limited Markdown | Full Markdown + embeds |
| Commands | BotFather /command | Slash commands via API |
| Threading | None | Native threads (session boundaries) |
| Voice | Voice messages (file transfer) | Voice channels (real-time, deferred to v2) |

### 7. Scope estimate

~500-600 lines Python (3 files + config). 80% architecture reuse from Telegram bridge — main work is API call adaptation, not design.

Voice channels deferred to v2 — fundamentally different from Telegram voice messages (file transfer vs real-time streaming).

## Implementation Phases

### Phase 0: Community Server (zero code)
- Create Discord server "D-PC Messenger"
- Add invite link to README.md
- Add Discord badge to README.md
- Create channels: #general, #bugs, #ark, #announcements

### Phase 1: Bot + Agent Bridge (~400 lines)
- `discord_service.py` — lifecycle, CoreService injection
- `managers/discord_manager.py` — bot init, whitelist, message sending
- `managers/agent_discord_bridge.py` — agent events → Discord, Discord → agent tasks
- Slash commands: /ask, /status, /sleep, /help
- @mention routing for Ark (whitelisted channels only)
- `discord.py>=2.7.0` dependency in pyproject.toml
- **Prerequisite:** message_content privileged intent enabled in Discord Developer Portal

### Phase 2: Full Bridge (~200 lines)
- `coordinators/discord_coordinator.py` — bidirectional DPC ↔ Discord routing
- Conversation linking (channel_id ↔ conversation_id)
- File/image forwarding
- **Thread-per-conversation:** Discord thread created per conversation session. Thread title = session topic (first 100 chars). Thread auto-archives after 1 hour of inactivity (Discord default). New `/ask` in archived thread creates new thread.

### Phase 3: Rich Features (future)
- Discord embeds for structured messages (knowledge commits, morning briefs)
- Reaction-based feedback collection
- Voice channel integration (real-time audio)

## References

- discord.py: github.com/Rapptz/discord.py
- Telegram bridge: managers/telegram_manager.py, coordinators/telegram_coordinator.py, managers/agent_telegram_bridge.py
- ADR-016 pattern: same modular service → manager → coordinator architecture
