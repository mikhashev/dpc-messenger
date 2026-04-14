# CC Agent Chat Monitor — Cron Prompt v4.1

This prompt is used with Claude Code's CronCreate to monitor DPC agent chat.
Recreate after every VSCode restart (cron jobs are session-only).

## Cron Command

```
CronCreate: every 1 minute
```

## Prompt v4.1

```
Check DPC agent chat. Run: cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python cc_agent_bridge.py --once --last 10 --full --conversation-id Ark. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions found, read context and respond via: python cc_agent_bridge.py --send "response text" --conversation-id Ark. Keep responses in markdown formatting. Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). Follow Protocol 13. If no actionable mentions, do nothing and don't report.
```

For another agent, swap `Ark` for that agent's display name (case-sensitive,
quote it if it contains spaces, e.g. `--conversation-id "Fifth Agent"`).
Folder ids like `agent_001` also work.

## Version History

- **v4.1** — Add `--conversation-id Ark` to both bridge calls (S37, commit `1d8a87b`). Bridge no longer silently defaults to the first agent alphabetically; `--conversation-id` is now required when more than one agent exists. Reason: name hardcode (`return "agent_001"`) removed from bridge per Mike's feedback in chat [157].
- **v4.0** — Add markdown rule (S27, Mike [16]): responses must be  markdown formatting. Reason: Mike easy to understand.
- **v3.1** — Add `--full` flag to prevent truncated message reading (S11 feedback)
- **v3.0** — Replace `--check` with `--once --last 10` (fixes race condition with history.json writes)
- **v2.1** — Use `--check N` flag (single auto-approved command) instead of inline Python
- **v2.0** — Use `find_mentions()` to scan full content (fixed truncation bug)
- **v1.0** — Read `--last 5` truncated output, visually scan for @CC
