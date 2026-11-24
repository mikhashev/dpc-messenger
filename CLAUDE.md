# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Kill-switch — five rules checked at the moment of action, not at reading time

They open the file on purpose: a rule at the end of 1400 lines is recalled while reading
and skipped while acting. One fires — stop, close the check, then continue.

1. **A conclusion about a chat, a log or a file was drawn from a slice** (`| tail`,
   `| head`, `| grep`, `--last` other than 10, `Read` at a tail offset on a persisted
   dump) → the conclusion is void; re-read the channel from the start. Need less output —
   narrow the selection inside the command, never cut the output.
2. **A number came from a colleague, an aggregator, or my own older note** → fetch the
   primary source (`git show`, a datasheet, the registry, a script), or mark it
   "per X, unverified".
3. **"Done / green / works / fixed"** → name what was actually run in this session and the
   state of the tree (`git status`). CI is `.github/workflows/tests.yml`; until a run
   exists for the ref in question, "green" means "locally, on my toolchain, on the layer
   I touched".
4. **"X is absent / does not exist / was never filed"** → enumerate, or say "not found in
   \<place\>". A directory-wide search does not see `.gitignore`d paths, and this repo
   keeps `backlog.md`, `ideas/`, `audit/` and `sprint-logs/` there — reach those with
   `rg --no-ignore` or an explicit file path.
5. **Work started without an explicit verb from the owner** ("agreed", "ok", a created
   artefact — none of these is a command) → stop and ask. A compound instruction is
   expanded into a queue and closed part by part.

The working tree is shared — a second developer and a second CC session write here. Never
`git add -A`; before editing `backlog.md` or another shared gitignored document, check its
mtime and do not assume a change there is mine. `backlog.md` is a dated synthesis, not the
state of the code: an entry tells you what to check, never that it is still true.

## Vocabulary — read the project's words before naming anything

The project already has a vocabulary; the failure is not looking for it. Before naming a
file, a function, a field, a backlog entry or a concept in a document:

1. **Read the words that exist**, in this order: [docs/GLOSSARY.md](docs/GLOSSARY.md) (one
   row per word, each pointing at the document that owns it); the document that owns the
   area (`docs/BACKLOG_FORMAT.md` for the board, `protocol-13-public.md` for roles,
   `docs/GROUP_CHAT.md` for groups, `specs/dptp_v1.md` for the wire); then the neighbours in
   code — the handler, provider or tool beside the one being written.
2. **Build the name from those words.** No field called `login` means no `find_by_login`.
3. **Grep before you coin**: `rg --no-ignore -c "<word>"` over the repository and the
   gitignored working layer. Zero hits means the word is invented; many hits means check
   what it already means — `node` is a network node here and an XML or DOM node elsewhere,
   `tier` is a connection tier (1–6) and a shell tier (0–2), `session` carries three
   numbering families.
4. **A word the vocabulary lacks is a question to the owner before the code**, not a note
   after it. The owner of this vocabulary is Mike.

Everything that lands in this repository is English; Russian is for the working chat and
the gitignored layer only (`docs/BACKLOG_FORMAT.md` §6; the commit hook refuses Cyrillic).

## Project Overview

**D-PC Messenger** is a privacy-first, peer-to-peer messaging platform for collaborative AI
through secure sharing of personal contexts: "transactional communication", end-to-end
encryption, no server-stored messages. A multi-package monorepo: Python backend services and
a Tauri + SvelteKit desktop frontend. Why: [VISION.md](VISION.md); what next: [ROADMAP.md](ROADMAP.md).

```
dpc-messenger/
├── dpc-protocol/         # Shared protocol library (LGPL v3)
├── dpc-client/
│   ├── core/             # Python backend service (GPL v3)
│   └── ui/               # Tauri + SvelteKit frontend (GPL v3)
├── dpc-hub/              # Federation Hub server (AGPL v3)
├── specs/                # Protocol specifications (CC0)
└── docs/                 # Documentation
```

Licences per component: [LICENSING.md](LICENSING.md) is authoritative; the root `LICENSE` is
the GPL v3 text GitHub shows.

## Architecture in one screen

The full inventory is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); these are the facts needed
on every task.

- **Backend** `dpc-client/core/dpc_client_core/`: `service.py` (CoreService) owns the
  lifecycle and wires coordinators (`inference_orchestrator.py`, `context_coordinator.py`,
  `p2p_coordinator.py`, `coordinators/`) to managers (`p2p_manager.py`, `managers/`).
  Incoming P2P commands go through `message_router.py` to `message_handlers/`. AI providers
  are in `providers/` behind `llm_manager.py`; the embedded agent is `dpc_agent/` (tools in
  `dpc_agent/tools/`); access control is `firewall.py`; settings `settings.py`. All I/O is
  asyncio.
- **UI** `dpc-client/ui`: Tauri 2 + SvelteKit 5 (runes). It talks to the backend over a
  WebSocket on localhost:9999 (`local_api.py` ↔ `src/lib/coreService.ts`); domain logic is in
  `src/lib/panels/` and `src/lib/services/`.
- **Hub** `dpc-hub`: optional FastAPI + PostgreSQL server for OAuth login and WebRTC
  signalling only — never message routing or storage. It holds one device per user account
  ([CONFIGURATION.md](docs/CONFIGURATION.md#device-identity-and-multi-device-considerations)).
- **Protocol** `dpc-protocol`: node identity and certificates, DPTP framing, the Personal
  Context Model, knowledge commits.
- **DPTP**: a 10-byte ASCII length header + JSON `{"command": "...", "payload": {...}}`;
  every command is in [specs/dptp_v1.md](specs/dptp_v1.md).
- **Connections**: six tiers tried in order — IPv6 direct, IPv4 direct TLS, Hub WebRTC, UDP
  hole punching (off by default), volunteer relay, gossip store-and-forward
  ([ARCHITECTURE.md](docs/ARCHITECTURE.md#connection-tiers-6-tier-fallback-hierarchy)).
- **Node id**: `dpc-node-` + the first 32 hex characters of the SHA256 of the RSA public key;
  identity files `~/.dpc/node.{key,crt,id}`.
- **Config** under `~/.dpc/` (`C:\Users\<user>\.dpc\` on Windows): `config.ini` (overridden by
  `DPC_<SECTION>_<KEY>` environment variables), `providers.json`, `privacy_rules.json`,
  `personal.json`, `device_context.json` (re-collected in the background on every start),
  received files in `conversations/{peer_id}/files/`, logs in `logs/dpc-client.log`.
- **Cross-platform**: code runs on Windows, Linux and macOS — `pathlib.Path` for paths; no
  signal handlers on Windows (KeyboardInterrupt); check a library on each platform before
  suggesting it ([ARCHITECTURE.md](docs/ARCHITECTURE.md#cross-platform-compatibility)).

## Common Commands

**Client backend:**
```bash
cd dpc-client/core
uv sync --all-extras              # install dependencies with every extra (see the rule below)
uv run python run_service.py      # backend service (ports 8888 P2P, 9999 UI WebSocket)
uv run pytest                     # tests; --cov=dpc_client_core for coverage
```

> ⚠️ **`uv sync` is declarative, not additive.** A bare `uv sync` uninstalls every optional
> extra installed earlier (`browser`, `graph-ner`, `graph-grafeo`, `pdf`, `voice-memory`,
> `mlx`), and `uv sync --extra X` keeps X and drops the rest. Re-sync with
> `uv sync --all-extras`; add one extra without touching the others with
> `uv sync --extra X --inexact`; check first with `uv sync --dry-run`, which prints what would
> be uninstalled. `--all-extras` is safe on every OS only because **each extra carries its own
> platform markers** — add one without them and the command breaks on an untested platform.
> Never reach for `uv sync --reinstall`: it drops every extra not named on the line. Extras and their history: [QUICK_START.md](QUICK_START.md#optional-features).

**Frontend:**
```bash
cd dpc-client/ui
npm install
npm run tauri dev                 # full app; `npm run dev` is the Vite server only (port 1420)
npm run check                     # TypeScript type checking
npm run build                     # frontend; `npm run tauri build` for the desktop app
```

**Hub** (optional; direct TLS works without it):
```bash
cd dpc-hub
docker-compose up -d && cp .env.example .env && uv sync
uv run alembic upgrade head       # migrations; new one: alembic revision --autogenerate -m "..."
uv run uvicorn dpc_hub.main:app --reload
uv run pytest -v --cov=dpc_hub    # also: black, flake8, mypy on dpc_hub/
```
Test every migration as upgrade head → downgrade -1 → upgrade head. OAuth credentials:
[dpc-hub/README.md](dpc-hub/README.md), [docs/GITHUB_AUTH_SETUP.md](docs/GITHUB_AUTH_SETUP.md);
the client starts offline by default (`[hub] auto_connect = false`).

**Protocol library:** `cd dpc-protocol && uv sync && uv run pytest`.

**Full stack locally:** Hub (optional), backend and `npm run tauri dev` in three terminals
([QUICK_START.md](QUICK_START.md)).

## Testing

```bash
cd dpc-client/core
uv run pytest -v
uv run pytest tests/test_local_api.py   # one file
```

Coverage HTML lands in `htmlcov/index.html`. If `uv run pytest` fails with `uv trampoline
failed to canonicalize script path`, the tests are fine: run `uv run python -m pytest` and see
[QUICK_START.md](QUICK_START.md#if-a-command-in-venv-stops-starting-windows) for the repair.

**A test that waits reads a frozen clock, or keeps at least 50 ms of slack.** `time.monotonic` steps in
15.6 ms ticks on Windows, so a test that compares two readings a few milliseconds apart, or waits
a few milliseconds for something to happen, can pass on Linux and fail on Windows. The twin-wait test
failed 7 runs in 1000 until it read a frozen clock (6c03484c, 02fa3f48). That is a property of the
platform, not a flake to chase: freeze the clock (`freeze_clocks` in `tests/_card_lease_helpers.py`
stops the card's clocks), or make the margin at least 50 ms.

## Commit message hook — one command per clone

```bash
git config core.hooksPath tools/git-hooks
```

The hook **refuses a commit message containing Cyrillic** and **warns when a teammate's name
stands beside a quotation**: a commit message is public and permanent, a chat line is
neither. Write the reason in English and attribute the decision — «Mike's call, 2026-08-28» —
rather than transcribing the sentence. Deliberate exception: `git commit --no-verify`. The
wrapper `tools/git-hooks/commit-msg` must keep `100755` in the index and LF endings, or git
skips it on Linux and macOS; the why is in the comments of `commit-msg` and
`commit_msg_check.py`, the tests in `tests/test_a_commit_message_carries_no_quoted_chat.py`.

## Security and privacy invariants

Each changes how code here is written; the detail is in the linked section.

- **Private keys never leave the device; the Hub never sees message contents or personal
  contexts.** TLS 1.2+ with certificate validation on every connection.
- **TURN is opt-in.** A fresh config ships `[turn]` empty, so no traffic reaches a third party
  unasked; fill it or set `DPC_TURN_*` ([docs/TURN_SETUP.md](docs/TURN_SETUP.md)).
- **`blocked_nodes` keeps a node out and wins over every allow**: no session, dial, Hub answer,
  relay, gossip or file; DHT packets that claim its id are dropped (a claim nothing proves
  until the RPCs are signed). Block writes it and deletes every rule naming the id; Unblock
  restores none of that. Mike's call, 2026-10-31; the DHT part, 2026-11-05
  ([CONFIGURATION.md](docs/CONFIGURATION.md#blocked_nodes-keeps-a-node-out-and-wins-over-every-allow)).
- **Who may send this node a file** (files, screenshots, voice): the first entry naming the
  sender decides — its `file_transfer.nodes` entry, else its first group with a
  `file_transfer.groups` entry, else `file_transfer.proved_connection` (direct TLS, default
  `ask`); anyone else is asked, and 120 s without an answer refuses with `no_answer`. Mike's
  call, 2026-10-25 ([CONFIGURATION.md](docs/CONFIGURATION.md#who-may-send-this-node-a-file)).
- **Transcription has two directions and two permissions**: `enabled`/`allow_*` govern serving
  peers our Whisper; `send_to_nodes`/`send_to_groups` (empty = never) govern our own audio
  leaving the machine on fallback; a peer picked by hand as `remote:<node>:<alias>` is the
  consent. Mike's call, 2026-09-07
  ([CONFIGURATION.md](docs/CONFIGURATION.md#transcription-has-two-directions-and-two-permissions)).
- **Adding a new agent tool — `default_enabled` is required (S148).** Pass
  `default_enabled=True|False` explicitly on its `ToolEntry`: `True` only for safe-by-default
  tools (read-only, sandbox-confined); anything destructive, network-egress, restricted or
  touching state outside the sandbox is `False`. Startup seeds missing tools into
  `privacy_rules.json` and never overwrites a user value
  ([CONFIGURATION.md](docs/CONFIGURATION.md#agent-tools-default_enabled-is-required-s148)).
- **Z.AI only through the prepaid platform API.** `ZaiProvider` raises on the GLM Coding Plan
  URLs (`api/anthropic`, `api/coding/paas/v4`, `api/v1`); reaching the plan from DPC got the
  account banned for a month ([CONFIGURATION.md](docs/CONFIGURATION.md#zai)).

## Where to find

| Need | Document |
|---|---|
| A word and the document that owns it | [docs/GLOSSARY.md](docs/GLOSSARY.md) |
| The board: `backlog.md` entries, fields, the check | [docs/BACKLOG_FORMAT.md](docs/BACKLOG_FORMAT.md) |
| Component inventory, connection tiers, prompt assembly, voice, tech stack | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Every `config.ini` key, env vars, `privacy_rules.json`, `providers.json` and AI providers, in-app editors, device identity | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Setup on Windows/macOS/Linux, extras, GPU, `.dpc` contents | [QUICK_START.md](QUICK_START.md) |
| Device context schema and `special_instructions` | [docs/DEVICE_CONTEXT_SPEC.md](docs/DEVICE_CONTEXT_SPEC.md) |
| Logging, per-module levels, start-up troubleshooting | [docs/LOGGING.md](docs/LOGGING.md) |
| Group chats, Block / Unblock | [docs/GROUP_CHAT.md](docs/GROUP_CHAT.md) |
| Inference served to and by peers | [docs/REMOTE_INFERENCE.md](docs/REMOTE_INFERENCE.md) |
| Knowledge commits and the knowledge graph | [docs/KNOWLEDGE_ARCHITECTURE.md](docs/KNOWLEDGE_ARCHITECTURE.md) |
| Embedded agent: tools, memory, retrieval flags; agent on Telegram | [docs/agent/DPC_AGENT_GUIDE.md](docs/agent/DPC_AGENT_GUIDE.md), [docs/agent/DPC_AGENT_TELEGRAM.md](docs/agent/DPC_AGENT_TELEGRAM.md) |
| Telegram bot bridge | [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) |
| Hub OAuth (Google, GitHub), Hub API | [dpc-hub/README.md](dpc-hub/README.md), [docs/GITHUB_AUTH_SETUP.md](docs/GITHUB_AUTH_SETUP.md), [specs/hub_api_v1.md](specs/hub_api_v1.md) |
| WebRTC deployment and TURN | [docs/WEBRTC_SETUP_GUIDE.md](docs/WEBRTC_SETUP_GUIDE.md), [docs/TURN_SETUP.md](docs/TURN_SETUP.md) |
| macOS build | [docs/MACOS_BUILD.md](docs/MACOS_BUILD.md) |
| The wire (DPTP) | [specs/dptp_v1.md](specs/dptp_v1.md) |
| Roles and participants | [protocol-13-public.md](protocol-13-public.md) |
| Decisions: one ADR per file, `NNN-<the decision>.md`, amendments at the end | [docs/decisions/](docs/decisions/) |
| Protocol library API | [dpc-protocol/README.md](dpc-protocol/README.md) |
| Vision, roadmap, licences | [VISION.md](VISION.md), [ROADMAP.md](ROADMAP.md), [LICENSING.md](LICENSING.md) |
