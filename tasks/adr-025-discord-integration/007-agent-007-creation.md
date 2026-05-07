# Task 007: agent_007 Community Manager Creation (Phase 1.5)

**Status:** TODO
**Phase:** 1.5
**Effort:** ~30 lines config + system prompt file
**Depends on:** Task 006 (system prompt refactor)

## Description

Create agent_007 as a dedicated community manager agent for Discord. Separate from Ark — focused on public project information, multi-language support, and community interaction.

## Files

- `~/.dpc/agents/agent_007/config.json` — agent configuration
- `~/.dpc/agents/agent_007/memory/system_prompt.md` — community manager system prompt
- `~/.dpc/config.ini` — `[discord] agent_id = agent_007` routing

## Implementation

- Create agent_007 sandbox directory structure
- Write config.json with name "007", appropriate model settings
- Write system_prompt.md: community manager role, multi-language (respond in user's language), knowledge of public repo docs
- Knowledge scope: `docs/`, `README.md`, `ROADMAP.md`, `CHANGELOG.md`, `VISION.md` (public only)
- Configure Discord routing: `discord.agent_id = agent_007` in config.ini
- Agent must NOT have access to private files (backlog.md, protocol-13.md, memory/)

## Done criteria

- agent_007 visible in sidebar agent list
- Discord @mention routes to agent_007 (not Ark)
- agent_007 responds using community manager system prompt
- agent_007 can answer questions about project docs
- agent_007 does NOT have access to internal/private files
