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

> **Run the bridge with `uv run python`, not plain `python`.** The
> **send** path (`--send` / `--send-file`) imports `websockets` to reach
> the backend WebSocket, and that dependency lives in the project venv,
> not necessarily in your system Python — plain `python` fails with
> `[ERROR] websockets not installed.` Invoking the bridge as
> `uv run python …` from the `dpc-client/core` directory guarantees the
> venv (with `websockets`) is used for every call. Reads (`--last N`)
> use only the stdlib and work under plain `python` too, but standardize
> on `uv run python` so one command form (and one allowlist entry)
> covers both poll and send. Prereq: the backend service must be running
> (WebSocket port open) and `~/.dpc/.ws_token` must exist for sends to
> authenticate.

## Prompt template

```
Check DPC agent chat. Run: cd <path-to-dpc-client-core> && uv run python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions are found, read context and respond via the bridge. For plain text without backticks use: uv run python cc_agent_bridge.py --send "response text" --conversation-id <agent>. For markdown responses with backticks, code blocks, or any shell-special characters, write the response to <path-to-temp-file> and send it via: uv run python cc_agent_bridge.py --send-file <path-to-temp-file> --conversation-id <agent>. Keep responses in markdown formatting. Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). If no actionable mentions, do nothing and don't report.
```

`<path-to-dpc-client-core>` is the absolute path to the
`dpc-client/core` directory inside your clone of dpc-messenger (or
wherever you have `cc_agent_bridge.py` available).

`<path-to-temp-file>` is any writable absolute path outside your git
tree AND outside any directory the agent has under
`sandbox_extensions.indexed_paths` in `privacy_rules.json` — otherwise
the outbox file feeds back into the agent's Active Recall as if it were
knowledge. Recommended: `~/.dpc/.cc_outbox/cc-out-{target}.md` where
`{target}` is the conversation-id or group-id being addressed (e.g.
`cc-out-Ark.md`, `cc-out-group-b88b65076b85.md`). Per-target files
prevent cross-chat stale content leaks when CC participates in multiple
conversations simultaneously. The leading dot on `.cc_outbox/` and its
placement under `~/.dpc/` keep it out of any typical indexed-paths scan.
The bridge reads this file directly (no shell interpretation), so
backticks and code blocks pass through intact.

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
      "Bash(uv run python cc_agent_bridge.py*)",
      "Bash(cd <path-to-dpc-client-core> && uv run python cc_agent_bridge.py*)"
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
<path>` instead: it matches `Bash(uv run python cc_agent_bridge.py*)`
and the bridge reads the file directly (no shell interpretation, so
backticks and code blocks survive intact).

**Pre-check also via the bridge.** The cron prompt already uses
`uv run python cc_agent_bridge.py --once --last 10 --full ...` for its
polling. If you add a pre-send check in your workflow (to catch
messages that landed during compose), run it through the same CLI —
`uv run python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>` —
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
- [`../../docs/BACKLOG_FORMAT.md`](../../docs/BACKLOG_FORMAT.md) — **required reading before
  writing a backlog entry.** An external agent working a project through this bridge writes
  into the same backlog the internal agents read; the entry shape, the status vocabulary and
  the rule for closing an entry are the same for everyone. Check your work with
  `uv run python tools/backlog/build.py --check` — it reports and never rewrites.
- `cc_cron_prompt.md` (at repo root, gitignored) — internal variant
  with Protocol 13 references and team-specific hard-coded paths

## Group chat variant

For monitoring a group chat instead of an agent 1:1 chat, use
`cc_group_chat_bridge.py` with `--group <group-id>`:

```
Check DPC group chat. Run: cd <path-to-dpc-client-core> && uv run python cc_group_chat_bridge.py --group <group-id> --last 10. Scan output for @CC or @СС mentions from non-CC senders. If unanswered @CC mentions with direct questions are found, read context and respond via the bridge. For plain text without backticks use: uv run python cc_group_chat_bridge.py --group <group-id> --send "response text". For markdown responses with backticks, code blocks, or any shell-special characters, write the response to <path-to-temp-file> and send it via: uv run python cc_group_chat_bridge.py --group <group-id> --send-file <path-to-temp-file>. Keep responses in markdown formatting. Distinguish: @CC as direct question (needs response) vs @CC mentioned in passing (no response needed). If no actionable mentions, do nothing and don't report.
```

Find your `<group-id>` by running `python cc_group_chat_bridge.py --list`.

Add the matching permission pattern:
```json
"Bash(uv run python cc_group_chat_bridge.py*)"
```
