# CC Agent Chat Monitor — Cron Prompt

This prompt is used with Claude Code's CronCreate to monitor DPC agent chat.
Recreate after every VSCode restart (cron jobs are session-only).

## Cron Command

```
CronCreate: every 1 minute
```

## Prompt

```
Check DPC agent chat for new messages and @CC mentions.

Step 1: Run `cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python cc_agent_bridge.py --status`
- If backend is DOWN, report "DPC backend down, skipping" and stop.

Step 2: Run `python cc_agent_bridge.py --once --last 20`
- Note the total message count.
- Compare to previous count (track across checks).

Step 3: If new messages exist (count increased):
- Scan ALL new messages (from previous count to current) for @CC or @СС mentions.
- Check sender — skip mentions FROM CC itself.
- If unanswered @CC mention found, read context and respond via:
  `python cc_agent_bridge.py --send "response text"`
- Follow Protocol 13.

Step 4: If no new messages for 3+ consecutive checks:
- Force re-read with `--last 20` to catch any missed messages.

If no mentions to answer, do nothing and don't report.
```

## Key Differences from Original

- **Index-based tracking**: Scan ALL new messages, not just --last 5 window
- **--last 20**: Larger window to catch rapid message bursts
- **Guaranteed delivery**: Every @CC mention in every new message is checked
- **No gaps**: Even if 10+ messages arrive in 1 minute, all are scanned
