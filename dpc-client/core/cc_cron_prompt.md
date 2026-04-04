# CC Agent Chat Monitor — Cron Prompt v2.1

This prompt is used with Claude Code's CronCreate to monitor DPC agent chat.
Recreate after every VSCode restart (cron jobs are session-only).

## Cron Command

```
CronCreate: every 1 minute
```

## Prompt v2.1

```
Check DPC agent chat. First run: cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python cc_agent_bridge.py --status. If backend is DOWN, report "DPC backend down, skipping" and do nothing else. If backend is UP, run: python cc_agent_bridge.py --check 0. This scans FULL message content for @CC mentions (not truncated). Compare TOTAL to previous check. If MENTION lines appear from non-CC senders with unanswered questions, read full context and respond via: python cc_agent_bridge.py --send "response text". Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). Follow Protocol 13. If no actionable mentions, do nothing and don't report.
```

## Version History

- **v2.1** — Use `--check N` flag (single auto-approved command) instead of inline Python
- **v2.0** — Use `find_mentions()` to scan full content (fixed truncation bug)
- **v1.0** — Read `--last 5` truncated output, visually scan for @CC
