# Task 003: AgentDiscordBridge (Phase 1)

**Status:** DONE (S97, commit 30aeea9) — slash commands cancelled per S97 decision
**Phase:** 1
**Effort:** ~200 lines
**Depends on:** Task 002

## Description

Create managers/agent_discord_bridge.py — agent events to Discord, Discord messages to agent tasks, slash commands. Mirrors AgentTelegramBridge.

## Files

- `dpc_client_core/managers/agent_discord_bridge.py` — NEW (~200 lines)
- `dpc_client_core/discord_service.py` — wire bridge to service

## Implementation

- Subscribe to AgentEventEmitter (same events as Telegram bridge)
- Event formatting: sleep_started/completed, morning_brief, task events
- @mention routing: when bot @mentioned in allowed channel → create agent task
- Slash commands: /ask, /status, /sleep, /extract_knowledge, /help
- Morning brief: post to morning_brief_channel_id on wakeup
- Rate limit awareness: discord.py handles automatically

## Done criteria

- Ark responds to @mention in #ark channel
- Slash commands work: /status shows agent state, /ask sends query
- Morning brief posts to configured channel after sleep
- Sleep/event notifications appear in Discord
