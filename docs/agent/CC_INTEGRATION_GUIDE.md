# External Agent Integration Guide

> The file name (`CC_INTEGRATION_GUIDE.md`) and the `cc_` prefixes on the bridge
> scripts and the config key are historical — they date from when Claude Code was
> the only harness wired in. The name an external agent answers to is set by you.

This guide explains how to connect an external agent — any harness that can run
a shell command, read a file and call a local WebSocket — as a third participant
in a DPC agent chat, alongside you and your embedded DPC agent.
[Claude Code](https://claude.com/claude-code) is the worked example throughout,
and the identity in the examples is the tag the maintainers registered for it
(`CC_mike`); substitute your harness and your tag.

> **Status:** this is the same integration the project maintainers use
> day-to-day (see `protocol-13-public.md` for how the three-way
> collaboration is structured). The bridges are local helpers — nothing
> in DPC requires an external agent. If you want an agent chat without one,
> skip this file.

---

## What to read first

The rest of this file is wiring. It gets an agent talking to the chat and tells
it nothing about the project it has joined — which for a long time meant the
maintainers pasted the same handful of paths by hand at the start of every
session. Read these at session start, before the first task, in this order:

1. [`protocol-13-public.md`](../../protocol-13-public.md) — the working contract
   between a human and the agents around them: who decides, who executes, who
   reviews, and the rule that an agent acts on an explicit verb and not on
   "sounds good". Short, and the piece most worth taking.
2. [`VISION.md`](../../VISION.md) — the direction, and the list of things this
   deliberately is not.
3. [`docs/BACKLOG_FORMAT.md`](../BACKLOG_FORMAT.md) — how work is recorded here.
   An external agent writes into the same board the internal ones read, so the
   entry shape, the status vocabulary and the rule for closing an entry are the
   same for everyone. Check your work with
   `uv run python tools/backlog/build.py --check`, which reports and never
   rewrites.
4. [`docs/decisions/`](../decisions/) — the architecture decisions, each with the
   alternatives that were rejected and why. Read the ones your task touches
   rather than all of them.
5. [`QUICK_START.md`](../../QUICK_START.md) and [`README.md`](../../README.md) —
   how to get the thing running, and what it is.

**What is not in the repository, and where it lives instead.** A project's
board, its research notes and its session logs are the content; this repository
only carries their shape. In this one `backlog.md`, `ideas/`, `audit/` and
`sprint-logs/` are all gitignored, so a directory-wide search does not see them
— reach them by explicit path, or with `rg --no-ignore`. The agent's own state
— memory, knowledge, conversation history — is under `~/.dpc/`, not in any
clone. Adapt this list to your project rather than copying it: the point is that
one exists and is written down, not that it names these five files.

---

## What the external agent sees, what it does

The external agent runs outside DPC — Claude Code in your VSCode (or terminal),
or any other harness — as a separate LLM session. Two small Python helpers in
this repo, the **agent-chat bridge** (`cc_agent_bridge.py`, for 1:1 chats) and
the **group bridge** (`cc_group_chat_bridge.py`, for group chats), let it:

1. **Read** the DPC chat by loading `history.json` from disk.
2. **Send** messages back over the local WebSocket API (the same one
   the Tauri UI uses).

The external agent is not magically embedded. It is a second LLM session
authorized to read/write the same conversation file your DPC agent uses. A
one-minute cron tick inside the harness (Claude Code has one built in) tells it
to check the chat, respond to mentions of its own tag, and stay quiet otherwise;
harnesses without a cron use the group bridge's `--listen` instead (below).

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
│ external     │◀──── bridge script  ─────▶│  ~/.dpc/     │
│ agent        │  (agent-chat / group)     │  .ws_token   │
└──────────────┘                           └──────────────┘
```

Port `9999` is the default for the local API server; override it via
`[api] port` in `~/.dpc/config.ini` if you need to move it. The bridges
read the same config and follow.

### Which name it answers to

- **In a group:** the tag you registered for this node in **Group Settings →
  External agents** (stored in the group's `metadata.json` under `agents` /
  `agent_names`, keyed by `~/.dpc/node.id`). This is the identity — see
  "One name, several machines" below for why registration matters.
- **Fallback, and the only identity in a 1:1 chat:** the configured display
  name, `[agent_chat] cc_display_name` in `~/.dpc/config.ini` (default `CC`;
  editable in the DPC UI under Firewall → Agent Permissions → CC Display Name).

Every user-visible line the bridges print — the `--mentions` banner, the
`[INFO] posting as <tag>` line, the `--listen` status — names the resolved tag.

---

## Prerequisites

- DPC client installed and running ([QUICK_START.md](../../QUICK_START.md)).
- At least one agent linked to your DPC instance (an `agent_*` folder
  under `~/.dpc/agents/`). With exactly one agent, the agent-chat bridge uses it
  by default. With more than one, you pick the target via
  `--conversation-id` (see below).
- Your harness set up — for the worked example,
  [Claude Code](https://claude.com/claude-code) in VSCode (or the terminal).
- Python 3.12+ with `websockets` installed in the same virtualenv that
  runs the DPC backend (it is already a dependency of `dpc-client/core`).

---

## How the bridge authenticates

The backend writes a 256-bit random token to `~/.dpc/.ws_token` at
startup. Anything that can read that file can talk to the local API.
The bridge reads it and presents it as its first WebSocket message,
same as the Tauri frontend.

Implication: treat `~/.dpc/` as sensitive. File permissions on the
directory are your trust boundary.

---

## The bridge scripts

Two bridge scripts cover 1:1 agent chats and group chats:

### The agent-chat bridge (1:1)

[`cc_agent_bridge.py`](../../dpc-client/core/cc_agent_bridge.py) — for
1:1 conversations with a single agent. Useful flags:

| Command | What it does |
|---------|--------------|
| `uv run python cc_agent_bridge.py --once --last 10 --full` | Dump the last 10 messages, full content. This is what the cron runs. |
| `uv run python cc_agent_bridge.py --send "text"` | Post a response to the current agent conversation. |
| `uv run python cc_agent_bridge.py --status` | Check whether the backend is up and when `history.json` last changed. |
| `uv run python cc_agent_bridge.py --mentions` | Show only messages that `@` mention the configured display name. |
| `uv run python cc_agent_bridge.py` | Poll mode — watch for new messages in a terminal (5-second interval). |

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

### The group bridge

[`cc_group_chat_bridge.py`](../../dpc-client/core/cc_group_chat_bridge.py) —
for group conversations with multiple agents. Same architecture as the
agent-chat bridge but reads from group history files and sends via
`send_group_agent_message` WebSocket command.

| Command | What it does |
|---------|--------------|
| `uv run python cc_group_chat_bridge.py --list` | List available group chats. |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --last 10` | Dump the last 10 messages from a group. |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --send "text"` | Post a response to the group under this bridge's tag. |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --send-file path` | Send from file (backtick-safe). |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --mentions` | Show only mentions of this bridge's tag, matched as a whole word (`@CC_mike`, not `@CC` or `@CC_mike2`). |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --as TAG --send "text"` | Post (or, with `--mentions`, scan) as one specific registered tag. |
| `uv run python cc_group_chat_bridge.py --group GROUP_ID --listen` | Block and print one `[MENTION]` line per `cc_group_mention` event for this bridge's tag as the backend emits it; reconnects with backoff when the backend drops. `--once` exits 0 after the first match, `--json` prints the payload as one JSON object per line, `--all-tags` takes every tag in the group. |

The `--group` argument accepts either the canonical group ID
(`group-abc123`) or the slugged directory name (`group-abc123-my-project`).
The bridge resolves the canonical ID from `metadata.json` automatically.

The name the bridge posts and listens under is resolved in this order:
`--as` → the tag this node registered in the group (from `metadata.json`
`agents` / `agent_names`, keyed by `~/.dpc/node.id`) → `[agent_chat]
cc_display_name`. When the name differs from `cc_display_name` the send
prints `[INFO] posting as <tag>` before `[SENT]`. With several tags
registered and no `--as`, `--send` refuses with exit code 2 and lists them;
`--mentions` scans for all of them and its banner names every tag scanned
(`No mentions of @CC_mike found.` / `=== 2 mention(s) of @CC_mike ===`).

**Harnesses without a cron: `--listen`.** Instead of polling `history.json`
every minute, `--listen` authenticates on the local WebSocket (the same
`.ws_token` handshake as `--send`) and blocks, printing one line per
`cc_group_mention` event whose `group_id` is this group and whose `agent_tag`
is one of the names resolved above (`--as` applies). The backend emits the
event on both the local send path and the peer path with
`{group_id, text, sender_name, sender_node_id, agent_tag}`. `--once` returns 0
after the first match, so `--listen --once --json` is "block until mentioned"
for a script; without `--once` it runs until Ctrl-C (exit 0, `[LISTEN] stopped`
on stderr) and reconnects with 1s→30s backoff when the backend restarts. Status
lines go to stderr, events to stdout.

### One name, several machines — read this before the second bridge

Register the bridge in **Group Settings → External agents**, by the tag you want
to be addressed as. That registration is what makes `@tag` reach *this* machine
and not every machine running a bridge under the same name.

Why it needs saying: a display name is a routing key here. The embedded-agent
path has always checked whether the agent is registered to *this* node; the
external path did not, so a name match alone raised the event and every node
carrying that name answered. That is not a cosmetic collision — the two have
different working trees and different memory, so a reply from the machine that
*cannot* do the work looks exactly like a reply from the one that can. It cost
this project a round before the field existed.

**Until you register something, the old behaviour stands.** Nothing was
registrable before, so gating on registration from the first run would have left
a mention of the configured name waking nobody, everywhere, with no error to
explain it. Instead the gate arrives per group, on the day somebody fills the
field:

| this node's external agents in that group | who answers a mention of the configured name (`@CC` by default) |
|---|---|
| none registered | this node, by its configured display name — and so does every other node with that name. A warning naming the group goes to `dpc-client.log`. |
| one or more registered | only the nodes whose registered tag was actually mentioned |

So the transition costs nothing and nothing breaks; what changes is that the
collision stops as soon as the people in the group each register their own tag.

**Choose a tag that survives mention routing.** The field refuses anything
outside `\w` and says why, because the failure is invisible otherwise: routing
parses `@(\w+)\b` and stops at the first non-word character, so `@CC-lnx`
reaches `CC`, and `@Fifth Agent` reaches `Fifth`.

| tag | reaches |
|---|---|
| `CC-lnx` | every `CC` in the group — **refused by the field** |
| `CC_lnx` | `CC_lnx` only |
| `CC2` | `CC2` only |

What makes that worth refusing rather than documenting is that the message still
*reads* as you typed it. The chat shows `@CC-lnx`, because the text is displayed
verbatim; only the routing truncated it. Nothing on screen says the mention went
somewhere else, so the mistake looks like it worked.

You can register several tags on one node; the group mention event names which
one was matched. The group bridge reads the same registration: it posts under
the registered tag (so the message is attributed `CC_mike`, not `CC`) and its
`--mentions` scan matches that tag as a whole word — `@CC_mike` and `@cc_mike,`
wake it, `@CC` and `@CC_mike2` do not. `--as TAG` overrides the choice; with
several tags and no `--as`, sending refuses and the scan matches all of them.
The node's own `[agent_chat] cc_display_name` in `~/.dpc/config.ini` still
decides what an unregistered node answers to.

**In a one-to-one chat you cannot separate them at all, and registration does not
help.** That send path carries no sender field, so both bridges arrive under the
node's single `cc_display_name`, and a de-duplication check drops the second
one's identical reply with nothing but a log line. Group settings register a tag
for a *group*; a 1:1 conversation has no such list. Until that changes: one
external agent per 1:1 chat.

### Sending markdown

For responses containing backticks or code blocks, use `--send-file` to avoid
bash command-substitution issues — write the response to a temp file first,
then send it.

---

## The cron loop (harness side)

The worked example runs a cron inside Claude Code. The exact prompt text lives in
[`cc_cron_prompt_public.md`](../../dpc-client/core/cc_cron_prompt_public.md)
and is versioned there. The internal version (`cc_cron_prompt.md` at
project root, gitignored) may have additional project-specific prompts
for group chats.

**Schedule:** every minute while the Claude Code session is open. Cron
jobs are in-session only and disappear when Claude Code closes, so you
need to recreate the cron after reopening the IDE. A harness with a
persistent scheduler, or one that can wait on a subprocess with
`--listen`, does not have this step.

**Behavior each fire (1:1 agent chat):**

1. Run `uv run python cc_agent_bridge.py --once --last 10 --full --conversation-id <agent>`.
2. Scan the output for mentions of your tag (`@<tag>`, plus `@СС` in
   Cyrillic when the tag is `CC`) from anyone who isn't you.
3. If there is an unanswered direct question, respond via
   `uv run python cc_agent_bridge.py --send "..." --conversation-id <agent>`.
   Keep responses in markdown.
4. If nothing actionable, do nothing.

**Behavior each fire (group chat):**

1. Run `uv run python cc_group_chat_bridge.py --group <group_id> --last 10`.
2. Same scan and response logic, against the tag you registered.
3. Respond via `--send "..."` or `--send-file path` for markdown content.

You can run both crons in parallel — one for 1:1, one for group chat.

The cron prompt does the filtering; the external agent just executes what
the cron says. Substitute the agent name (or folder id) for `<agent>` and
your tag for `<tag>` when you create the cron — see the canonical prompt in
[`cc_cron_prompt_public.md`](../../dpc-client/core/cc_cron_prompt_public.md), which
ships with `Ark` as the default and notes how to swap it.

---

## Where the external agent fits in Protocol 13

Protocol 13 is the project's three-agent collaboration contract
(see [`../../protocol-13-public.md`](../../protocol-13-public.md)). In short:

- **Mike** (human) — decides, approves actions.
- **Ark** (embedded DPC agent) — reviews, flags risks, writes
  rationale.
- **The external agent** (Claude Code, registered as `CC_mike` in the
  worked example) — executes code changes, runs tests, commits.

This is a working pattern, not a requirement of the software. Your
own setup can use the external agent differently (or not use one at all).

---

## Setup steps

1. Start the DPC backend and leave it running:

   ```bash
   cd dpc-client/core
   uv sync
   uv run python run_service.py
   ```

2. Verify the bridge can reach the backend:

   ```bash
   cd dpc-client/core
   uv run python cc_agent_bridge.py --status
   ```

   You should see `Backend: UP` and a fresh `history.json`
   update time.

3. For a group chat, open the group's **Group Settings → External agents**
   in the DPC UI and register the tag this machine should answer to. For a
   1:1 chat there is nothing to register; the bridge answers to
   `[agent_chat] cc_display_name` (default `CC`).

4. In your harness, create the recurring check using the exact prompt from
   [`cc_cron_prompt_public.md`](../../dpc-client/core/cc_cron_prompt_public.md).
   In Claude Code that is a cron with schedule `every 1 minute`. The shipped
   prompt targets the agent named `Ark`; if your agent uses a different
   display name, replace `Ark` with that name (or with the folder id) in both
   the `--once` and `--send` invocations, and replace `<tag>` with your tag.

5. Open the chat in the DPC UI and mention `@<tag>` in a message.
   Within ~60 seconds the external agent should respond.

6. If the harness restarts (IDE reload, window closed), recreate the
   cron — in Claude Code it does not persist.

---

## Troubleshooting

**The external agent does not respond.** Check
`uv run python cc_agent_bridge.py --status --conversation-id <agent>`. If the
backend is down, start it. If you see
`[ERROR] Multiple agents found, specify --conversation-id...`, the
cron prompt is missing the flag — recreate the cron with the current
[`cc_cron_prompt_public.md`](../../dpc-client/core/cc_cron_prompt_public.md). If the
warning is `--conversation-id=... did not match any known agent`, you
have a typo (or the agent was deleted) — the bridge prints the list of
known agents alongside the warning. In a group, check that the tag you
mention is the one this node registered: `--mentions` prints the names it
scanned for in its banner.

**`websockets not installed`.** You are running the bridge in a
different virtualenv than the one with `dpc-client/core` deps. Use
`uv run python cc_agent_bridge.py ...` from `dpc-client/core/`.

**Auth rejected.** The token in `~/.dpc/.ws_token` is regenerated on
every backend start. If the bridge was last run against a previous
backend process, re-run it — it reads the file fresh each time.

**It responds when it shouldn't (or vice versa).** The cron prompt
defines the filter. Tune it in `cc_cron_prompt_public.md` and recreate the
cron — the prompt version you create the cron with is what runs.

---

## Related

- [`../../dpc-client/core/cc_agent_bridge.py`](../../dpc-client/core/cc_agent_bridge.py) — the agent-chat bridge
- [`../../dpc-client/core/cc_group_chat_bridge.py`](../../dpc-client/core/cc_group_chat_bridge.py) — the group bridge
- [`../../dpc-client/core/cc_cron_prompt_public.md`](../../dpc-client/core/cc_cron_prompt_public.md) — canonical cron prompt
- [`../../protocol-13-public.md`](../../protocol-13-public.md) — three-agent collaboration contract
- [`./DPC_AGENT_GUIDE.md`](./DPC_AGENT_GUIDE.md) — embedded DPC agent (the one the external agent talks *with*, not the one it *is*)
- [`./DPC_AGENT_TELEGRAM.md`](./DPC_AGENT_TELEGRAM.md) — Telegram integration (parallel concept, different channel)
