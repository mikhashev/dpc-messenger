---
adr: 048
title: "Mark each memory layer private or shared, and let a turn see only the layers its audience may"
status: accepted
date: 2026-11-09
axis: collective, knowledge
deciders: [Mike]
consulted: [Zcode, Johnny, Ark, CC]
informed: []
depends_on: [ADR-010]
related: [ADR-031, ADR-041]
supersedes: []
session: "DPC Project group chat and session, 2026-11-07..08 — A-GROUP-TURN-CAN-SHOW-THE-OWNERS-PRIVATE-MEMORY-TO-OTHER-NODES"
---

# ADR-048 — Memory layer audience

> **Accepted, 2026-11-08 (Mike's call, after review by Zcode, Johnny and Ark).** Steps 2
> and 3 of the design are in the working tree as of 2026-11-09; step 1 (the turn's source
> and chat in history, 2aac408d) is its prerequisite.

## Context and Problem Statement

An agent's memory had no boundary between what is the owner's and what may be said in a
room with other people. Active Recall and `memory_search` were closed to strangers'
turns on 2026-11-07 (eafed550), but a group turn still counted as the owner's whatever
its members, and everything else stayed open on every turn:

- **The prompt.** Every turn got the Scratchpad, Identity, Morning Brief, Dialogue
  Summary and the knowledge index (up to 50,000 characters) in its system block, and
  replayed hint tails of earlier turns.
- **The file tools.** `read_file`, `search_files` and `list_dir` reach every file in the
  agent's directory and every granted extended path on any turn. Observed live on
  2026-11-08: a Telegram turn that was not the owner's (Warren, owner field empty) was
  refused by `memory_search` and then read `memory/scratchpad.md` through `search_files`
  and `read_file` without asking anybody.

Two holes, then: a group with another person's node is served as the owner, and a turn
refused memory by recall reads the same memory as files. Mike, 2026-11-07: in a group
chat with other nodes (a work chat, say) the agent must not tell the owner's personal
information or secrets without his permission.

## Decision

Each memory layer carries an audience mark, and each turn is classed by who it answers;
a turn sees only the layers its class may see, in every channel at once.

1. **The mark.** `agent_profiles.<id>.memory.layer_audience` in `privacy_rules.json`,
   `{"L5": "private", "L6": "shared", "EXT": "private"}` by default. An absent field or
   key keeps the default; nothing is migrated. Values are exactly `private` and `shared`.
   L5 covers the agent's `knowledge/` and `memory/` (scratchpad, identity, dialogue
   summary, morning brief, system prompt), so identity closes with L5.
   `firewall.validate_config` refuses another key or value.
2. **Own nodes (a2).** `agent_profiles.<id>.memory.own_node_group` names a group in the
   existing top-level `node_groups`. Absent means no own nodes besides this one. Save
   refuses a name that is not in `node_groups`. At startup each agent logs one INFO line
   naming the group it uses (or why none) and how every chat group resolves.
3. **Turn classes** (`agent.turn_class`, `agent.open_layers`, carried on the tool context
   beside `message_source` and recomputed at run time for scheduled tasks):
   - the owner's doors — `ui`, `telegram_owner`, `external_agent` — see every layer,
     whatever the marks;
   - a group whose members are all this node or nodes of the own group, and which is not
     a Discord bridge, counts as the owner; any other group — another member, a Discord
     bridge, a group not found, an empty roster — sees only the layers marked `shared`.
     The class comes from the members, not from who wrote the mention: the answer goes to
     all of them;
   - a stranger — `telegram`, `discord`, no source, anything unknown — sees no layer.
4. **Enforcement.**
   - Active Recall runs only when some layer is open and drops candidates of closed
     layers before decay and the cut; a row's layer is its key prefix (`knowledge/`,
     `L6/`, `EXT/`), so a graph hit labelled L7 counts as its document's layer, and
     anything else is closed.
   - `memory_search` refuses with no open layer and otherwise drops rows of closed layers.
   - With L5 closed the prompt carries no Scratchpad, Identity, Morning Brief, Dialogue
     Summary or knowledge index, and the brief is not marked consumed. With EXT closed the
     extended-access section names no path.
   - A replayed tail that names a closed layer in a recall block loses that block, and
     loses its extended path names when EXT is closed.
   - `get_dpc_context("personal")` refuses on any turn that is not the owner's, whatever
     `personal_context_access` says.
   - `update_scratchpad`, `update_identity`, `deduplicate_identity` and any write into
     `knowledge/` or `memory/` refuse in one line on a turn with L5 closed.
   - **File tools**, a check of their own in `tools/core.py` (not in
     `validate_extended_path`, which run_shell's path gate calls): a stranger opens
     nothing in the agent's directory but the current turn's tool results, no extended
     path and no shared knowledge; a group with another node opens no `memory/` or
     `knowledge/` (unless L5 is shared), never `state/` (but its own tool results),
     `logs/`, `task_results/`, `knowledge_graph*` or `config.json*`, extended paths only
     when EXT is shared and shared knowledge only when L6 is. Every other file such a turn
     opens in the agent's directory is logged at INFO, without content. The session
     archive tools open, for a stranger, only its own `telegram-<chat id>` conversation
     (the rest of their rule is under [Session archives](#session-archives)).
     The git tools follow the same gate: on a turn that is not the owner's no git write
     touches the agent directory's own repository, and a `repo_path` read or write obeys
     the extended-path rule above.
5. **Prompt cache.** No per-turn audience text goes into the system block beyond the
   closed sections falling out. Since step 1 a stranger's Telegram turn runs in its own
   conversation, so each conversation keeps one class and one system block.

### Rationale

One mark per layer is the smallest thing that separates what the agent wrote and what
the owner granted from what the owner committed for the node, and it is the shape Teams
can take over later (Mike, 2026-11-08: layer marks now, Teams can take the same mark).
Classing by members rather than by sender follows the audience of the answer. The file
gate stands apart from `validate_extended_path` because run_shell calls that one, and
run_shell is a separate story (Mike, 2026-11-08). Strangers lose the whole agent root
because a work folder can be as personal as memory (Ark, measured on Warren: finance,
orders); other-node groups lose the memory and the mixed stores now, and the logged opens
decide an allow-list after a week (Johnny).

## Session archives

Added 2026-11-09 (Mike's call in DPC Project, review by Johnny and Zcode;
ARCHIVE-GROUP-ACCESS). A **session archive** is one closed session of a conversation,
kept on this node beside its history.

1. **The writer records the group.** When a group conversation is reset or a new session
   is voted, the archive gets `group`: `group_id`, `members` and `version`, copied from
   the group's `metadata.json` at that moment. A 1:1 archive gets no such key; a group
   whose metadata cannot be read gets none and one log line.
2. **The read rule** of `read_session_archive`, `read_session_detail` and
   `search_session_archives`, by turn class:
   - a stranger reads only its own `telegram-<chat id>` conversation, as above;
   - the owner's turn reads the current conversation, and, with
     `agent_profiles.<id>.history.archive_include_groups` true (false by default), every
     group conversation whose `metadata.json` agents map lists this agent under this
     node. The agents map is the group's side of the permission and the flag the agent's;
     both must allow. A group with no `metadata.json`, an unreadable one or an empty
     agents map is not included;
   - a turn in a group with another node reads that group's archive only, and only the
     sessions whose recorded `members` include every current member. A session with no
     recorded members (every archive written before this change) opens on the owner's
     turns only; when the current members cannot be read, no session opens. The answer
     says in one line how many sessions it did not show and why, so the agent does not
     take them for absent.
3. **Results name their conversation.** Each listed session and each search match
   carries `conversation_id`, and `read_session_detail` takes `conversation_id` to open
   a file of another conversation the rule opens; filenames repeat across conversations,
   so a bare filename is looked up in the current conversation only. A conversation the
   rule does not open is refused in one sentence.

Consequence: until the Linux node is in `own_node_group`, DPC Project counts as a group
with another node, so its sessions archived before this change close on its group turns.
Archives written by a sync that drops records older than a reset boundary carry no
`group` either and close the same way.

## Considered Options

- **A mark per file.** Exact, but nobody curates 400 L6 files one by one, and L6 is what
  the owner already approved. Zcode: a Teams-era topic.
- **Teams first.** None of the nine Track 2 rows exists in code; the mark ships now and
  Teams takes it over.
- **A reserved `own_nodes` group name.** Rejected: a name in a free namespace that a
  typo silently turns into "no own nodes", and `node_groups` names are also disclosure
  rules for `personal.json` (Ark). The name lives in a validated key instead (a2).
- **A separate id list beside `blocked_nodes`.** Rejected: a second roster of the same
  nodes beside `node_groups`, which already decides compute and transcription serving
  (Johnny); the own-node list should be the team roster, not a second list (Zcode).
- **Values `personal` / `shared`.** Rejected for `private`: "personal" collides with
  `personal_context_access` and `personal.json` (Johnny, Ark).

## Consequences

- **An agent in a mixed group is a different agent.** With L5 private it has no
  scratchpad, brief or identity there, and cannot write them; its voice may change.
  Splitting a shareable persona out of identity is a separate card.
- **L6 holds commits born in 1:1 chats.** Marked shared by default, so a group with
  another node can be offered them; Ark counted 5 of 400 L6 file names that look
  personal. L6 is owner-curated; a per-file audience is the Teams-era answer.
- **The gate inherits the ownerless roster** (GROUP-ROSTER-HAS-NO-OWNER): a peer that
  edits a group's member list changes whether the group counts as the owner's.
- **run_shell is not covered.** A turn can still read memory through a command the owner
  approves.
- **CC quoting what it read** in a stranger's chat is a residual risk DPC cannot enforce.
- **The mixed stores stay closed to every non-owner group turn** even with L5 marked
  shared: they hold EXT and L6 text side by side and cannot be sorted by layer.
- **A stranger writes no file in the agent's directory** (the root is closed to it), and
  its replayed tails and system block differ from the owner's, at the cost of one cold
  prefill when a conversation's class first changes.

## Confirmation

- [ ] In group 1234 or Brofish (another node present) a question about an L5- or EXT-only
  fact gets no such hint and `memory_search` lists only `L6/` keys; `read_file
  memory/scratchpad.md` is refused.
- [ ] In a group of this node and the Linux node only, with `own_node_group` naming a
  group that lists the Linux node, L5 and EXT hints come back.
- [ ] A Telegram turn that is not the owner's repeats the 2026-11-08 Warren test:
  `read_file memory/scratchpad.md` and `search_files` refused; run_shell still asks.
- [ ] The startup line names each agent's own group and the groups resolved as own.

## Scope

- `dpc_agent/agent.py` — `turn_class`, `open_layers`, `layer_audience`, `turn_audience`,
  the startup line.
- `dpc_agent/memory_config.py`, `firewall.py` — the two keys and their validation.
- `dpc_agent/active_recall.py`, `dpc_agent/context.py` — recall filter, prompt sections,
  replayed tails.
- `dpc_agent/tools/core.py`, `tools/git.py`, `tools/archive.py`, `tools/registry.py` —
  the file gate, writes, `memory_search`, `get_dpc_context`.
- `conversation_monitor.py` (`group` in a group's archive), `tools/archive.py` and
  `firewall.py` (`history.archive_include_groups`) — session archives.

## Authors

- **Mike** — Decision, names
- **Zcode, Johnny, Ark** — Review
- **CC** — Design, implementation

## References

- Board: A-GROUP-TURN-CAN-SHOW-THE-OWNERS-PRIVATE-MEMORY-TO-OTHER-NODES,
  A-TELEGRAM-TURN-CANNOT-TELL-THE-OWNER-FROM-ANYONE-IN-AN-ALLOWED-CHAT, the epic
  ACTIVE-RECALL-CANNOT-YET-BE-TRUSTED-ON-EMBEDDING-IDENTITY-PERMISSION-LATENCY-OR-ITS-OWN-NUMBERS.
- `eafed550` — recall and memory_search refuse strangers; `2aac408d` — step 1.
- [ADR-010](010-agent-memory-architecture.md) — the layers.
