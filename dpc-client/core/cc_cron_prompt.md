# CC Agent Chat Monitor — Cron Prompt v2.0

This prompt is used with Claude Code's CronCreate to monitor DPC agent chat.
Recreate after every VSCode restart (cron jobs are session-only).

## Cron Command

```
CronCreate: every 1 minute
```

## Prompt v2.0

Key improvement over v1.0: Uses `find_mentions()` to scan FULL message content,
not truncated --last output. Prevents missing @CC tags at end of long messages.

```
Check DPC agent chat for new messages and @CC mentions.

Step 1: Run `cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python cc_agent_bridge.py --status`
- If backend is DOWN, report "DPC backend down, skipping" and stop.

Step 2: Run this Python snippet to get message count AND scan full content for @CC mentions:
```
cd c:/Users/mike/Documents/dpc-messenger/dpc-client/core && python -c "
import cc_agent_bridge as b
msgs = b.read_history()
count = len(msgs)
print(f'TOTAL: {count}')
# Scan ALL messages from last known count for @CC mentions in FULL content
mentions = b.find_mentions(msgs, since_index=max(0, count-10))
for i, m in mentions:
    s = m.get('sender_name','?')
    c = m.get('content','')
    idx = c.find('@CC')
    if idx < 0: idx = c.lower().find('@cc')
    ctx = c[max(0,idx-20):idx+100] if idx >= 0 else c[:100]
    print(f'MENTION [{i}] {s}: {ctx}')
if not mentions:
    print('NO_MENTIONS')
"
```

Step 3: Compare TOTAL to previous count (track across checks).
- If MENTION lines appear from non-CC senders with unanswered questions, read full context and respond via: `python cc_agent_bridge.py --send "response text"`
- Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed)
- Follow Protocol 13.

Step 4: If no new messages for 3+ consecutive checks, do nothing.

If no actionable mentions, do nothing and don't report.
```

## Changes from v1.0

- **v1.0**: Read `--last 5/20` truncated output, visually scan for @CC in 200-char snippets
- **v2.0**: Use `find_mentions()` Python function to scan FULL message content
  - No more missed @CC at end of long messages
  - Shows context around @CC tag (20 chars before, 100 after)
  - Filters out CC's own messages automatically
  - Since_index prevents re-scanning old messages
