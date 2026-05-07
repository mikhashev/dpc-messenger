# Task 001: Discord Bot Application Setup (Phase 1 prerequisite)

**Status:** DONE (S97)
**Phase:** 1
**Effort:** Zero code — manual setup
**Depends on:** Task 000

## Description

Create Discord Application and Bot in Developer Portal. Enable required intents.

## Steps

1. Create Application on discord.com/developers/applications
2. Create Bot user under the Application
3. Enable privileged intents: Message Content, Server Members (if needed)
4. Generate bot token
5. Create invite URL with permissions: Send Messages, Read Message History, Attach Files, Use Slash Commands
6. Invite bot to DPC server
7. Store bot token in environment variable `DISCORD_BOT_TOKEN`

## Done criteria

- Bot appears online in Discord server
- Bot token stored securely (env var, not in code)
- Message Content intent enabled
