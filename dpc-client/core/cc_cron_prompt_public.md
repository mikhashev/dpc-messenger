# CC Agent Chat Monitor — Public Cron Prompt Template

A generic Claude Code cron prompt for monitoring a DPC agent chat and
responding to `@CC` mentions via the bridge. Use this as a starting
point when setting up Claude Code integration in your own project.

The internal variant the project maintainers run lives at the repo root
as `cc_cron_prompt.md` (gitignored — contains team-specific references:
Protocol 13, an agent named `Ark`, absolute user paths). This public
template drops those so you can adopt the pattern cleanly in any context.

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
Check DPC agent chat. Run: cd <path-to-dpc-client-core> && python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions are found, read context and respond via the bridge. For plain text without backticks use: python cc_agent_bridge.py --send "response text" --conversation-id <agent>. For markdown responses with backticks, code blocks, or any shell-special characters, write the response to <path-to-temp-file> and send it via: python cc_agent_bridge.py --send-file <path-to-temp-file> --conversation-id <agent>. Keep responses in markdown formatting. Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). If no actionable mentions, do nothing and don't report.
```

`<path-to-dpc-client-core>` is the absolute path to the
`dpc-client/core` directory inside your clone of dpc-messenger (or
wherever you have `cc_agent_bridge.py` available).

`<path-to-temp-file>` is any writable absolute path outside your git
tree (so the file isn't committed). Examples: `~/.dpc/cc-out.md`
on Linux/macOS, `C:\Users\<you>\.dpc\cc-out.md` on Windows. The bridge
reads this file directly (no shell interpretation), so backticks and
code blocks pass through intact.

## Eliminating per-send permission prompts

Claude Code prompts for approval before running each unfamiliar Bash
command. With no allowlist, **every cron fire and every bridge send
triggers a new prompt** — because the command string varies by agent
name, message content, or file path. That kills the whole point of a
cron-driven monitor.

Add a permission pattern to `.claude/settings.local.json` at the repo
root (project-specific, gitignored) — or `~/.claude/settings.local.json`
for user-wide coverage:

```json
{
  "permissions": {
    "allow": [
      "Bash(python cc_agent_bridge.py*)",
      "Bash(cd <path-to-dpc-client-core> && python cc_agent_bridge.py*)"
    ]
  }
}
```

The trailing `*` wildcard in each entry covers every bridge subcommand
(`--once`, `--send`, `--send-file`, `--mentions`, `--status`, etc.) and
every argument value. Add the entry once; no more prompts for the
lifetime of the project.

**Common pitfall — do NOT reach for `python -c` as a workaround.** When
`--send "text"` eats your message (bash treats backticks or `$(...)` in
the quoted string as command substitution), it's tempting to write a
`python -c "import sys; sys.path.insert(...); from cc_agent_bridge import send_response_sync; ..."` wrapper that reads the message from
a file. That wrapper command string does NOT match the allowlist
pattern above — you'll get a fresh prompt every time. Use `--send-file
<path>` instead: it matches `Bash(python cc_agent_bridge.py*)` and the
bridge reads the file directly (no shell interpretation, so backticks
and code blocks survive intact).

**Pre-check also via the bridge.** The cron prompt already uses
`python cc_agent_bridge.py --once --last 10 --full ...` for its
polling. If you add a pre-send check in your workflow (to catch
messages that landed during compose), run it through the same CLI —
`python cc_agent_bridge.py --once --last 5 --full --conversation-id <agent>` —
not through a different tool. One allowlist entry covers both.

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
- `cc_cron_prompt.md` (at repo root, gitignored) — internal variant
  with Protocol 13 references and team-specific hard-coded paths

## Version notes

This template tracks the bridge CLI as of v4.2 of the internal prompt.
Changes from v4.1:

- Added `--send-file <path>` instruction for markdown responses that
  contain backticks or code blocks (rule-of-thumb: when in doubt, use
  the file path — it sidesteps shell command substitution). The
  existing `--send "text"` is still valid for plain-text replies.

Earlier baseline (v4.1):

- `--conversation-id` is required when more than one agent exists;
  name resolution maps display names to folder ids.

If the bridge CLI changes incompatibly, update this template alongside
the internal one.
