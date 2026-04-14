# CC Agent Chat Monitor — Public Cron Prompt Template

A generic Claude Code cron prompt for monitoring a DPC agent chat and
responding to `@CC` mentions via the bridge. Use this as a starting
point when setting up Claude Code integration in your own project.

The internal variant the project maintainers run
([`cc_cron_prompt.md`](./cc_cron_prompt.md)) includes team-specific
references (Protocol 13, agent named `Ark`). This public template
drops those so you can adopt the pattern cleanly in any context.

## How to use

1. Decide which agent Claude Code should monitor. Use the display name
   from `~/.dpc/agents/<folder>/config.json:name` (for example `Ark`,
   or any custom name you set). Folder ids like `agent_001` also work.
2. In the prompt below, replace every `<agent>` with your agent name.
   Quote names with spaces: `--conversation-id "My Agent"`.
3. In Claude Code, create a recurring cron (schedule: `every 1 minute`)
   and paste the resulting prompt.
4. The cron is session-only — recreate it after reopening the IDE.

## Prompt template

```
Check DPC agent chat. Run: cd <path-to-dpc-client-core> && python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions are found, read context and respond via: python cc_agent_bridge.py --send "response text" --conversation-id <agent>. Keep responses in markdown formatting. Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). If no actionable mentions, do nothing and don't report.
```

`<path-to-dpc-client-core>` is the absolute path to the
`dpc-client/core` directory inside your clone of dpc-messenger (or
wherever you have `cc_agent_bridge.py` available).

## Customizing

- **Response style.** The line `Keep responses in markdown formatting`
  is the minimum. Add project-specific conventions here (length
  limits, tone, banned phrasings) rather than stuffing them into every
  response.
- **What counts as actionable.** The distinction between a direct
  `@CC` question and a passing mention is intentional — it avoids
  chatter. Tighten or loosen it to taste (for example, ignore
  mentions that end with a period, only respond to questions ending
  with `?`, etc.).
- **Mentions in other scripts.** The bridge scans for `@CC` and the
  Cyrillic `@СС`. If your user name for Claude Code is different
  (editable in the DPC UI under Firewall → Agent Permissions → CC
  Display Name; persists to `[agent_chat] cc_display_name` in
  `~/.dpc/config.ini`), add the actual `@<name>` variant to the scan
  instructions.
- **Quiet mode.** The `If no actionable mentions, do nothing and
  don't report` clause is important — without it you get a noise
  stream of "no mentions found" every minute.

## Related

- [`cc_agent_bridge.py`](./cc_agent_bridge.py) — the bridge script
  invoked by the prompt
- [`../../docs/agent/CC_INTEGRATION_GUIDE.md`](../../docs/agent/CC_INTEGRATION_GUIDE.md) —
  full integration guide (setup, authentication, troubleshooting)
- [`cc_cron_prompt.md`](./cc_cron_prompt.md) — internal variant with
  Protocol 13 references, for reference

## Version notes

This template tracks the bridge CLI as of v4.1 of the internal prompt:
`--conversation-id` is required when more than one agent exists, and
name resolution maps display names to folder ids. If the bridge CLI
changes incompatibly, update this template alongside the internal one.
