# Claude Code Integration Guide

This guide explains how to connect [Claude Code](https://claude.com/claude-code)
as a third participant in a DPC agent chat — alongside you and your
embedded DPC agent. We refer to Claude Code as **CC** throughout.

> **Status:** this is the same integration the project maintainers use
> day-to-day (see `protocol-13-public.md` for how the three-way
> collaboration is structured). The bridge is a local helper — nothing
> in DPC requires Claude Code. If you want an agent chat without CC,
> skip this file.

---

## What CC sees, what CC does

CC runs in your VSCode (or terminal) as a separate Claude Code session.
A tiny Python helper in this repo — `cc_agent_bridge.py` — lets CC:

1. **Read** the DPC agent chat by loading `history.json` from disk.
2. **Send** messages back over the local WebSocket API (the same one
   the Tauri UI uses).

CC is not magically embedded. It is a second LLM session authorized to
read/write the same conversation file your DPC agent uses. A
one-minute cron tick inside Claude Code tells CC to check the chat,
respond to `@CC` mentions, and stay quiet otherwise.

### Architecture (one chat, two AI participants)

```
┌──────────────┐        WebSocket         ┌──────────────┐
│  Tauri UI    │◀────── 127.0.0.1:9999 ──▶│  DPC Core    │
│  (Svelte)    │                           │  Service     │
└──────────────┘                           │  + Agent     │
                                           └──────┬───────┘
                                                  │
                           history.json + WS API  │
                                                  ▼
┌──────────────┐        file + WS          ┌──────────────┐
│ Claude Code  │◀──── cc_agent_bridge ────▶│  ~/.dpc/     │
│  (VSCode)    │                           │  .ws_token   │
└──────────────┘                           └──────────────┘
```

Port `9999` is the default for the local API server; override it via
`[api] port` in `~/.dpc/config.ini` if you need to move it. The bridge
reads the same config and follows.

---

## Prerequisites

- DPC client installed and running ([QUICK_START.md](../../QUICK_START.md)).
- At least one agent linked to your DPC instance (an `agent_*` folder
  under `~/.dpc/agents/`). With exactly one agent, the bridge uses it
  by default. With more than one, you pick the target via
  `--conversation-id` (see below).
- [Claude Code](https://claude.com/claude-code) set up in VSCode (or
  the terminal).
- Python 3.12+ with `websockets` installed in the same virtualenv that
  runs the DPC backend (it is already a dependency of `dpc-client/core`).

---

## How CC authenticates

The backend writes a 256-bit random token to `~/.dpc/.ws_token` at
startup. Anything that can read that file can talk to the local API.
The bridge reads it and presents it as its first WebSocket message,
same as the Tauri frontend.

Implication: treat `~/.dpc/` as sensitive. File permissions on the
directory are your trust boundary.

---

## The bridge script

[`cc_agent_bridge.py`](../../dpc-client/core/cc_agent_bridge.py) is the
entire integration. Useful flags:

| Command | What it does |
|---------|--------------|
| `python cc_agent_bridge.py --once --last 10 --full` | Dump the last 10 messages, full content. This is what the cron runs. |
| `python cc_agent_bridge.py --send "text"` | Post a CC response to the current agent conversation. |
| `python cc_agent_bridge.py --status` | Check whether the backend is up and when `history.json` last changed. |
| `python cc_agent_bridge.py --mentions` | Show only messages that `@` mention CC. |
| `python cc_agent_bridge.py` | Poll mode — watch for new messages in a terminal (5-second interval). |

### Picking a conversation

Every command above accepts `--conversation-id NAME-OR-FOLDER`:

- The value can be the agent's display name (`Ark`, `"Fifth Agent"` —
  quote names with spaces) or the folder id (`agent_001`).
- Display names are read from `~/.dpc/agents/*/config.json` and
  resolved to folder ids automatically.
- If you omit the flag and have exactly one agent, the bridge uses it.
  With multiple agents the bridge errors out and lists them — pick one.
- Folder ids that don't match any agent (group chats, P2P peers) pass
  through; the bridge prints a warning to stderr but does not exit.

There is no state kept inside the bridge between invocations. Each
call re-reads `history.json` from scratch.

---

## The cron loop (Claude Code side)

CC runs the following cron inside Claude Code. The exact prompt text
lives in [`cc_cron_prompt.md`](../../dpc-client/core/cc_cron_prompt.md)
and is versioned there.

**Schedule:** every minute while the Claude Code session is open. Cron
jobs are in-session only and disappear when Claude Code closes, so you
need to recreate the cron after reopening the IDE.

**Behavior each fire:**

1. Run `python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>`.
2. Scan the output for `@CC` or `@СС` (Cyrillic) mentions from anyone
   who isn't CC.
3. If there is an unanswered direct question, respond via
   `python cc_agent_bridge.py --send "..." --conversation-id <agent>`.
   Keep responses in markdown.
4. If nothing actionable, do nothing.

The cron prompt does the filtering; CC just executes what the cron
says. Substitute the agent name (or folder id) for `<agent>` when you
create the cron — see the canonical prompt in
[`cc_cron_prompt.md`](../../dpc-client/core/cc_cron_prompt.md), which
ships with `Ark` as the default and notes how to swap it.

---

## Where CC fits in Protocol 13

Protocol 13 is the project's three-agent collaboration contract
(see [`../../protocol-13-public.md`](../../protocol-13-public.md)). In short:

- **Mike** (human) — decides, approves actions.
- **Ark** (embedded DPC agent) — reviews, flags risks, writes
  rationale.
- **CC** (Claude Code) — executes code changes, runs tests, commits.

This is a working pattern, not a requirement of the software. Your
own setup can use CC differently (or not use CC at all).

---

## Setup steps

1. Start the DPC backend and leave it running:

   ```bash
   cd dpc-client/core
   poetry install
   poetry run python run_service.py
   ```

2. Verify the bridge can reach the backend:

   ```bash
   cd dpc-client/core
   poetry run python cc_agent_bridge.py --status
   ```

   You should see `Backend: UP` and a fresh `history.json`
   update time.

3. In Claude Code, create a cron using the exact prompt from
   [`cc_cron_prompt.md`](../../dpc-client/core/cc_cron_prompt.md). The
   schedule is `every 1 minute`. The shipped prompt targets the agent
   named `Ark`; if your agent uses a different display name, replace
   `Ark` with that name (or with the folder id) in both the
   `--once` and `--send` invocations.

4. Open the agent chat in the DPC UI and mention `@CC` in a message.
   Within ~60 seconds Claude Code should respond.

5. If Claude Code restarts (IDE reload, window closed), recreate the
   cron — it does not persist.

---

## Troubleshooting

**CC does not respond.** Check
`python cc_agent_bridge.py --status --conversation-id <agent>`. If the
backend is down, start it. If you see
`[ERROR] Multiple agents found, specify --conversation-id...`, the
cron prompt is missing the flag — recreate the cron with the current
[`cc_cron_prompt.md`](../../dpc-client/core/cc_cron_prompt.md). If the
warning is `--conversation-id=... did not match any known agent`, you
have a typo (or the agent was deleted) — the bridge prints the list of
known agents alongside the warning.

**`websockets not installed`.** You are running the bridge in a
different virtualenv than the one with `dpc-client/core` deps. Use
`poetry run python cc_agent_bridge.py ...` from `dpc-client/core/`.

**Auth rejected.** The token in `~/.dpc/.ws_token` is regenerated on
every backend start. If the bridge was last run against a previous
backend process, re-run it — it reads the file fresh each time.

**CC responds when it shouldn't (or vice versa).** The cron prompt
defines the filter. Tune it in `cc_cron_prompt.md` and recreate the
cron — the prompt version you create the cron with is what runs.

---

## Related

- [`../../dpc-client/core/cc_agent_bridge.py`](../../dpc-client/core/cc_agent_bridge.py) — bridge source
- [`../../dpc-client/core/cc_cron_prompt.md`](../../dpc-client/core/cc_cron_prompt.md) — canonical cron prompt
- [`../../protocol-13-public.md`](../../protocol-13-public.md) — three-agent collaboration contract
- [`./DPC_AGENT_GUIDE.md`](./DPC_AGENT_GUIDE.md) — embedded DPC agent (the one CC talks *with*, not the one CC *is*)
- [`./DPC_AGENT_TELEGRAM.md`](./DPC_AGENT_TELEGRAM.md) — Telegram integration (parallel concept, different channel)
