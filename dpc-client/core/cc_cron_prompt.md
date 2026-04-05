# CC Agent Chat Monitor — Cron Prompt v3.0

This prompt is used with Claude Code's CronCreate to monitor DPC agent chat.
Recreate after every VSCode restart (cron jobs are session-only).

## Cron Command

```
CronCreate: every 1 minute
```

## Prompt v3.0

```
Check DPC agent chat. Run: cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python cc_agent_bridge.py --once --last 10. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions found, read context and respond via: python cc_agent_bridge.py --send "response text". Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). Follow Protocol 13. If no actionable mentions, do nothing and don't report.
```

## Version History

- **v3.0** — Replace `--check` with `--once --last 10` (fixes race condition with history.json writes)
- **v2.1** — Use `--check N` flag (single auto-approved command) instead of inline Python
- **v2.0** — Use `find_mentions()` to scan full content (fixed truncation bug)
- **v1.0** — Read `--last 5` truncated output, visually scan for @CC
