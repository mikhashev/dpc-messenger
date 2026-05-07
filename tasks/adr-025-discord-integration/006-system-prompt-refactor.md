# Task 006: System Prompt Refactor (Phase 1.5)

**Status:** TODO
**Phase:** 1.5
**Effort:** ~10 lines in context.py
**Depends on:** None (infrastructure for Task 007)

## Description

Add per-agent system prompt override via `memory/system_prompt.md` in each agent's sandbox directory. If the file exists, its content replaces the default hardcoded system prompt. If absent, the default from code is used. Existing agents (Ark) are unaffected unless they create the file.

## Files

- `dpc_client_core/dpc_agent/context.py` — read `memory/system_prompt.md` in `build_llm_messages`, use as Block 1 override

## Implementation

- In `build_llm_messages` (around line 539), check for `memory/system_prompt.md` in agent sandbox
- Sandbox path: `build_llm_messages` already receives `self.agent` which has `self.agent.sandbox_dir` (Path). Read `self.agent.sandbox_dir / "memory" / "system_prompt.md"`.
- If file exists and is non-empty, use its content as the system prompt (Block 1)
- If file does not exist, fall back to `_default_system_prompt()` (current behavior)
- No changes to existing agents needed

## Done criteria

- Agent with `memory/system_prompt.md` uses custom prompt
- Agent without the file uses default prompt (no regression)
- Ark behavior unchanged (no system_prompt.md in his sandbox)
