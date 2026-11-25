---
adr: 053
title: "Who asked decides what an agent may do; the room still decides what it may say"
status: accepted
date: 2026-11-25
axis: network, collective
deciders: [Mike]
consulted: [CC, Zcode]
informed: []
depends_on: [ADR-048]
related: [ADR-023, ADR-026, ADR-030, ADR-038, ADR-051, ADR-052]
supersedes: []
session: "AI Studio group chat (group-60aff6110f02), 2026-11-25 — Mike's decisions at #105, #108, #123, #124, #126, #128, #135 and #138; Zcode's review of commits 702b0510..4d045edf at #125 and its wake count at #130"
---

# ADR-053 — Who asked decides what an agent may do

> **Accepted 2026-11-25 (Mike, AI Studio group chat #105, #108, #123, #124, #126, #128,
> #135, #138, #148). Built on `dev` 2026-11-25 (steps 1-9; see
> [As built](#as-built-2026-11-25)), not observed live.**
> The section [Decision](#decision) states what he decided. The section
> [Details CC derived from the code](#details-cc-derived-from-the-code) holds points that
> are CC's reading of the code and are not Mike's words; they are open to review. Every
> code reference was checked against `dev` HEAD `4d045edf` on 2026-11-25. Line numbers
> drift; the names do not.

## Context and Problem Statement

ADR-048 decides what an agent may **say** in a group. It classes each turn by the
members of the room (`turn_class`, `dpc_agent/agent.py:93-114`) and closes the memory
layers a class may not see. The class comes from the members, not from who wrote the
mention, because the answer goes to all of them (ADR-048, Decision item 3). That stays
right for memory and privacy.

It is the wrong key for what an agent may **do**. Four facts, all read in the code:

1. **Nothing reads who asked.** The peer path passes the sender's node id all the way into
   `DpcAgent.process` (`group_handler.py:600-602`, `661-683`; `agent_manager.py:1173-1174`,
   `1338`). Nothing uses it. The code comment says so: "Nothing reads it yet; it is here
   for the group policy that is still undecided" (`agent.py:581-583`). The local path does
   not even pass it (`service.py:7937-7940`). `ToolContext` has no field for it
   (`tools/registry.py:279-345`).
2. **The class refuses; it never asks.** `owner_only` tools (ComfyUI x7, `music_*` x9,
   `chat_post_file`) are not offered on an `other_node` or `stranger` turn and are refused
   in `ToolRegistry.execute` (`registry.py:766-772`). The owner is not asked. Mike himself,
   typing in a group that has a friend's node in it, gets the same refusal as the friend
   would, and cannot approve his own request.
3. **Approvals look at the command, not at the asker.** `run_shell` Tier 1
   (`tools/shell.py:2390`), `check_back` (`tools/core.py:2079-2085`) and `music_propose_job`
   (`tools/music.py:466-469`) each ask the owner, whoever wrote the mention, and the card
   does not say who it was. Everything that is not a Tier 1 command runs with no question:
   Tier 0 `git push`, `copy`, `del` and redirects included (`shell.py:171` makes only
   `git push --force` Tier 1). Every other `schedule_task` type is never asked
   (`core.py:2016`).
4. **A foreign asker is treated as the owner once past the door.** The door checks are
   routing only: the agent must be listed for this node in the group, `@all` is a human's
   word, an agent post wakes an agent only below the mention-depth limit
   (`group_handler.py:555-571`, `service.py:270`), and a blocked node wakes nobody. After
   them a foreign human or a foreign agent gets the same agent, the same tool profile and
   the same provider as the owner. The board has carried this open since 2026-09-25 as
   `A-PEER-TRIGGERED-RUN-SPENDS-THE-OWNERS-PROVIDER-AND-ASKS-THE-OWNERS-SCREEN`.

ADR-030 settled one half: "Tier 1 approval routes to the agent owner, not to the @mention
initiator" (`030-run-shell-safety-guardrails.md`, group chat approval routing). It left
open what the owner is told about the initiator, and whether an initiator who is not the
owner should change the question at all.

## Decision Drivers

- **The owner pays and answers for what his agent does.** A foreign person must not be
  able to spend the owner's card, files, shell or identity just by writing a mention.
- **A friend's mention is not a threat by default.** The agent should still answer; only a
  consequential action needs the owner's yes.
- **One rule in one place.** ADR-048 and ADR-051 both show that a rule written per tool is
  forgotten for the next tool.
- **Honest prompts.** An approval card that does not say who asked cannot be answered
  well (board: `THE-APPROVAL-DIALOG-IS-NOT-BOUND-TO-THE-CONVERSATION-THAT-CAUSED-IT`).

## Prior art outside our engine

Mike's call, 2026-11-17: this section is required.

- **(a) The project's own reading.** `rg --no-ignore -il "approval|human-in-the-loop"` over
  `ideas/dpc-research/articles` on 2026-11-25. Two articles bear on this decision.
  - `2608.27146-sara-tool-outputs-as-commands.md` (arXiv 2608.27146): separate "action
    induction" from "execution authorization", and never let history launder an action's
    origin into authority ("No-History-Promotion"). We take the split: the model may be
    induced by anyone, the runtime authorizes by who asked. The laundering warning is
    why [Q2](#open-questions) exists: an own agent relaying a foreign request.
  - `2608.27141-safety-does-not-compose-loopharness.md` (arXiv 2608.27141): a monitor that
    sees one turn cannot see an attack spread over several. Our approver is per call. We
    take one thing: the asker travels with a scheduled wake (Decision 9), so a request
    split across `check_back` hops keeps its origin. We do not take a cross-turn risk
    state; that is a separate card.
- **(b) The topic, not tied to our engine.**
  `ideas/agent-communication-comparison/02-external-systems.md` section 5.4 and
  `04-differences-and-what-to-take.md` b3 item 4 (CC, 2026-09-25): Claude Code's inbound
  control names three outcomes, Delivered, Held (set aside until the human approves) and
  Refused, and the receiving session's own permission prompts still apply. We take
  "Held until the owner approves" for a consequential call, and refuse after 60 s as the
  approvals do today. We do not hold the whole run: the friend's question still gets an
  answer. No web search was made for this ADR on 2026-11-25.
- **(c) Not taken.** Per-peer tool subsets keyed on message source, as Discord has
  (ADR-026, `agent.py:1090-1093`): rejected by Mike's list, which keeps one profile and
  asks per call instead.

## Decision

Two axes, kept apart.

**1. Memory and privacy follow the room. Unchanged, ADR-048.** The answer is seen by every
member of the group. In a mixed group the agent does not quote the owner's private layers
even when the owner is the one who asked. `turn_class` and `open_layers` stay as they are.

**2. Actions follow who asked.**

**2.1 What a node is.** A node is hardware plus its human plus its agents, and the external
agents and harnesses running on it (ADR-006: internal or external is a connection
property, not a participant type). The asker gets the class of its node.

**2.2 Who is the owner.** Owner means:

- this node's human: the UI, and the owner's Telegram (`ui`, `telegram_owner`);
- this node's agents, and the external agents and harnesses on this node
  (`external_agent`);
- the nodes in the agent's `own_node_group` (ADR-048 Decision item 2).

Own nodes come only from the firewall config, the top-level `node_groups`
(`firewall.py:317`). Being in an own group grants **no file rights** by itself: rights live
in the firewall `groups` section (`firewall.py:1991-2012`, `can_access`). A **UI field to
pick `own_node_group`** is added to the agent permissions panel. None exists today: a grep
of `dpc-client/ui/src` finds neither `own_node_group` nor `layer_audience`, so both are
hand edits. The owner picks it.

**2.3 Asked by the owner.** Tools run as the profile's switches say, as today.

**2.4 Asked by anyone else.** A foreign human and a foreign agent get the same treatment.
Every call of a tool marked "needs owner approval on a foreign request" goes to the owner
as an **approval card** that names:

- the asker: name, node id, and whether it is a human or an agent;
- the chat;
- the tool;
- what exactly: for `run_shell` and the file tools, the resolved paths.

If the owner does not answer in 60 s, the call is refused, as today
(`APPROVAL_TTL_SECONDS = 60`, `shell.py:2294`; `_SCHEDULE_APPROVAL_TTL_SECONDS = 60`,
`core.py:1623`). The result tells the agent that the owner is not in focus, and the agent
says so in its answer. Telegram delivery of group cards is a later decision and is not in
this ADR (see [Q1](#open-questions)).

**2.5 The tool flag.** The rule is a property of the tool in the registry, like
`owner_only` (`ToolEntry.owner_only`, `registry.py:516`). Proposed name:
`owner_approval_for_others` (see the [glossary](../GLOSSARY.md#owner_approval_for_others)).
It is checked in one place, `ToolRegistry.execute`, next to the `owner_only` refusal
(`registry.py:766-772`), so that a new tool cannot skip it. **The list**, one named list,
with the card on every call:

- Mike's list (#105-#128): `run_shell` at **every tier** (Tier 0 `git push`, `copy`, `del`
  and redirects included); `write_file`; every git write (`git_add`, `git_commit`,
  `git_init`, `git_checkout`, `git_merge`, `git_tag`, `git_reset`, `git_snapshot`,
  `git_push`, the callers of `_closed_git_write` in `tools/git.py`); `repo_delete`;
  `schedule_task`; `send_user_message`; the browser actions (`browser_navigate`,
  `browser_click`, `browser_fill`, `browser_select`, `browser_download`).
- Mike's answer to Q3 (AI Studio group #138, 2026-11-25) names seven tools: six with a
  card, `execute_skill`, `import_skill_from_agent`, `register_task_type`,
  `unregister_task_type`, and the two that spend the GPU card, `describe_image` and
  `transcribe_audio_file`; and `kill_my_process` without one. The browser tools that only
  read carry no card.
- Mike's call at AI Studio group #148 (2026-11-25): the four plain web fetchers,
  `browse_page` (with or without the owner's login), `fetch_json`, `check_url` and
  `search_web`, carry the card too: each sends a URL the model chose out of this node, a
  way out for whatever the turn has read (Zcode's review). One switch in `tools/browser.py`
  (`CARD_FOR_PLAIN_WEB_FETCH`) holds this.
- Zcode's review: `browse_page` with `use_auth` acts as the owner on that site, and
  `list_auth_domains` tells which sites those are; both carry the card.
- The tools that were `owner_only` (2.6): `music_*` x9, ComfyUI x7, `chat_post_file`.

Reads and search carry no card. `update_identity` and `update_scratchpad` are **not** on the
list: the L5 layer (ADR-048) already closes them on a turn that is not the owner's.

**The completeness table.** Every registered tool is in one of the two columns; the registry
test of step 2 (`tests/test_every_registered_tool_is_flagged_or_named_in_the_no_card_table.py`)
fails on a tool in neither.

| Card on a foreign request (`owner_approval_for_others`) | No card |
|---|---|
| `run_shell` (every tier), `write_file`, `repo_delete` | reads: `read_file`, `list_dir`, `list_extended_sandbox_paths`, `search_files`, `search_in_file`, `read_document`, `chat_history`, `knowledge_list`, `memory_search`, `get_dpc_context`, `read_session_archive`, `read_session_detail`, `search_session_archives` |
| git writes: `git_add`, `git_commit`, `git_init`, `git_checkout`, `git_merge`, `git_tag`, `git_reset`, `git_snapshot`, `git_push` | git reads: `git_status`, `git_diff`, `git_log`, `git_branch` |
| `schedule_task`, `send_user_message` | self-introspection: `list_my_tools`, `list_my_skills`, `list_agent_skills`, `list_local_agents`, `list_task_types`, `get_task_status`, `get_task_board`, `card_status`, `list_my_processes` |
| browser actions: `browser_navigate`, `browser_click`, `browser_fill`, `browser_select`, `browser_download`; the web fetchers (#148): `browse_page`, `fetch_json`, `check_url`, `search_web`; `list_auth_domains` | browser reads: `browser_snapshot`, `browser_screenshot`, `browser_extract`, `browser_wait_for`, `browser_scroll`, `browser_collect`, `browser_switch_tab`, `browser_close` |
| the former `owner_only` tools (2.6): `music_*` x9, ComfyUI x7, `chat_post_file` | the L5 layer closes them (ADR-048): `update_scratchpad`, `update_identity`, `deduplicate_identity` |
| Q3: `execute_skill`, `import_skill_from_agent`, `register_task_type`, `unregister_task_type`, `describe_image`, `transcribe_audio_file` | Q3: `kill_my_process` (it stops only this agent's own processes) |

**2.6 The tools that are `owner_only` today.** `music_*`, the seven ComfyUI tools and
`chat_post_file` change from "always refused" to "owner approval on a foreign request". The
card states the cost: a local engine leaves the card; it comes back in about 25 s (measured
2026-11-25 in the live log: Strata start 11:27:31, smoke passed 11:27:56) and the next turn
may run cold. A swap saves the live conversation (1.1 GB, about 10 s) and its KV snapshot;
the restore can fail on a prefix mismatch, and then the next turn runs cold (reviewer
Johnny's correction of the earlier wording "the conversation cache is dropped"; ADR-052
holds the lease model). The same cost line is on the cards of `describe_image`,
`transcribe_audio_file` and `comfyui_submit`, which spend the card too, and on the owner's
own music card. Two limits found in review (Zcode) and kept:

- `owner_only` was the ComfyUI tools' only guard: they take a path or a server URL as given.
  On a foreign request their paths now go through the file tools' gate
  (`tools.core._resolve_file_path`: the sandbox, the profile's grants, what is closed to the
  turn) and only the default local server is spoken to, both before any card. The owner's
  own requests are unchanged.
- `chat_post_file` refuses every group (`chat_files.turn_refusal`), so "with owner approval"
  reaches only the agent's own 1:1 chat. Posting into a group is ADR-051 D50 decision G,
  separate work, not built here.

**2.7 `run_shell` and closed paths.** A path that is closed for the turn is refused
**without a card** (ADR-048, direction of 2026-11-09). The check goes in the shell tool
handler (`shell._closed_shell_path`), like the git gate in `tools/git.py:169-234`, and in
`run_shell`'s `approval_check`, so it comes before the card. It does **not** go inside
`validate_extended_path`: `DpcAgent._extended_path_allowed` (`agent.py`) calls that function
with a context that has no message source, for Active Recall. `turn_audience` itself does not
gate such a bare context, but a default-deny check there (as `is_owner_turn` fails closed on a
context with no class) would refuse every extended path to recall.

**2.8 One pending card per chat.** A second flagged call in a chat that already has a card
up is refused at once, with a one-line reason. This answers the board card about
unanswered approvals starving the tool pool
(`FOUR-PENDING-APPROVALS-STARVE-EVERY-AGENTS-TOOLS`; the pool is four workers,
`loop.py:400`). It answers it only per chat: the pool is the process's, so a second bound,
at most two pending cards across the process, is in [D-i](#details-cc-derived-from-the-code).

**2.9 The asker survives a scheduled wake.** A `check_back` scheduled on a foreign request
wakes with that asker. Today `core.py:2099` stores only `_message_source`, and the wake
calls `process` with no `sender_node_id` (`agent.py:1735-1741`).

**3. Knowledge** (follow-ups to Zcode's review #125 of commits 702b0510..4d045edf):

- An `APPLY_KNOWLEDGE_COMMIT` for a proposal this node did not vote on is applied only
  after an owner card. Today it is applied after a roster check and a provenance check,
  and the node merely does not sign it (`knowledge_handler.py:338-405`, `judged_here`;
  `consensus_manager.py:665-670`).
- A 1:1 knowledge proposal is accepted only from own nodes. Today any peer that names this
  node as the chat opens a vote (`knowledge_service.py:399-400`,
  `_local_roster_for_peer_proposal`).

**4. Not adopted.** A per-hour limit on wakes by foreign mentions (Mike, #128). Merging
bursts of mentions is deferred. Zcode counted 224 group wakes from 15 to 25 November with
no bursts and no foreign wakes (#130). If bursts appear, four conditions apply (#130): the
merge window is shorter than Telegram's 20 s; agent posts are never merged; it applies only
on the built-in agent's paths; and a held mention can still be stopped by Stop.

**5. The ADR-049 gap.** ADR-048 and the glossary cite "ADR-049's store" for team knowledge,
and the board card `A-GROUP-HAS-NO-AUDIENCE-OF-ITS-OWN-SO-ITS-PRIVACY-FOLLOWS-ITS-ROSTER`
plans a per-group `audience` field Mike asked for on 2026-11-09. ADR-049 was **reserved and
never written**; the number 050 went to another record on 2026-11-11. This ADR does not
write it and does not invent it. The references in ADR-048 and the glossary are corrected
in the same commit to say so, and point at the open card (`backlog.md:1478`, a local file
that is not in the repository).

### Rationale

The room decides what the answer may contain because everyone in it reads the answer. The
asker decides what the agent may do because an action has one author, and that author is
the person the owner is answerable for. Asking per call, not per run, lets the agent
answer a friend's question and stops only the step that touches the owner's machine. A flag
on the tool, checked once in `execute`, is what ADR-051 D43 already proved for `owner_only`.

## Details CC derived from the code

Not Mike's words. These are what the code forces when the decision is built. Each is open
to review before step 1.

**D-a. Only a verified peer post counts as an own node.** The peer path takes the author
from the signature when it verifies. An unverifiable post is kept with the claimed author
(`group_handler.py:262-345`, `_authenticate_author`: `verified`, `unverified`, `legacy`,
`rejected`; a `legacy` post takes the transport's node, which in a star can be the relay).
Whether the author is a human or an agent is read from the signed `sender_type`
(`signed_mention_depth`, `group_handler.py:65-92`); for an unverified post it is not known.
Proposed: an asker whose post is not `verified` is foreign, kind "unknown", and the card
says "not verified". A claimed own-node id must never buy owner rights.
Added from Zcode's review and built: the wake line logs the verdict ("signature verified",
"unverified", "legacy"); `unverified` keeps the author the payload claims and `legacy` the
transport's node, and neither is ever an own node. The history sync is a second peer door
(`_wake_merged_mentions` hands the record's stored verdict to the same
`_handle_agent_mentions`), so it gets the same asker. An unverified post is of unknown kind
for routing too: it may name an agent, whose turn then has a foreign asker, but its `@all`
wakes nobody. A peer reached over WebRTC through the Hub has a node id the Hub asserted, not
one a key proved; only a verified signature makes it an own node.

**D-b. One card per call.** A `run_shell` Tier 1 command asked for by a foreign asker gets
the one card, with the Tier 1 reason on it, not two. The same for any tool that already
asks (`check_back`, `music_propose_job`): the asker fields are added to that card.

**D-c. The call's clock.** `get_timeout(name)` is the loop's whole budget for a call
(`loop.py:2607`). `run_shell` already includes the 60 s approval (`shell.py:2304-2306`,
`RUN_SHELL_TOOL_TIMEOUT_SECONDS` = 60 + 300 + 30 = 390 s), and so do `schedule_task`
(60 + 30 = 90 s) and `music_propose_job` (`PROPOSE_TOOL_TIMEOUT_S` = 100 s, the 60 s card and
its calls). The other music tools do not: `music_wait` has 915 s (`WAIT_MAX_S` 900 +
`WAIT_MARGIN_S` 15), and `music_capabilities`, `music_check`, `music_cancel_job`,
`music_list_takes`, `music_list_library`, `music_get_lyrics` and `music_playlist_add` have
30 s each (`ONE_CALL_TOOL_TIMEOUT_S`), which a 60 s card would cut. The budget for a flagged
call on a foreign turn becomes the tool's own plus the approval wait, except for the three
tools whose own budget already holds it. As built: `ToolEntry.budget_holds_approval` marks
those three, `ToolRegistry.approval_wait_for` gives the 60 s to every other flagged tool on a
foreign turn (most 2.5 tools have 30 s, `git_push` 60 s), and a registry-wide test pins it.
Zcode's review found the second half: the loop's `asyncio.wait_for` cancels the await, not
the worker thread, so a call the model was told had timed out kept waiting for its card and a
late "yes" would still run it. The loop now gives the worker the call's deadline
(`owner_approval.run_with_call_deadline`), and every approval wait (the gate's card, the
schedule card, the shell's Tier 1 card) voids an answer that comes after it.

**D-d. The gate needs a hook.** To refuse a closed path before the card (2.7), to exempt the
music return (D-e), and to word "what exactly", `ToolEntry` gets an optional
`approval_check(ctx, args)` that answers refuse (with text), exempt, or ask (with the
description). The flag alone cannot do that, because `execute` runs before the handler.

**D-e. The music return stays free.** A `check_back` that returns to the agent's own
unfinished music job asks nothing today (`core.py:2052-2078`, ADR-051 D49). The owner
already approved that job. It stays exempt from the card.

**D-f. Telegram and Discord askers are foreign.** A `telegram` turn that is not the owner's
chat, and any `discord` turn, are askers who are not the owner. Discord already gets a
read-only whitelist with none of the flagged tools (`agent.py:1090-1093`). A Telegram
stranger's card would be offered in that stranger's chat (`shell.py:2484-2492`), where only
the owner's press counts (`agent_telegram_bridge.py:1589-1603`); whether to show it there
at all is part of Q1.

**D-g. A scheduled row with no stored asker fails closed.** A task written before this
change has `_message_source` and no asker. Owner sources stay owner; a `group` source with
no stored asker is treated as foreign.

**D-h. A latent trap in the registry.** `ToolRegistry.override_handler`
(`registry.py:813-826`) rebuilds a `ToolEntry` field by field. A new flag that is not
copied there is silently dropped. Its one non-test caller is the eval harness
(`eval/_harness/answer_key_policy.py`, which wraps handlers), and it keeps working. The build uses
`dataclasses.replace` instead.

**D-i. The tool pool, across chats** (reviewer Johnny). "One card per chat" (2.8) does not
protect the pool: the four workers (`dpc_agent/loop.py`, `_TOOL_WORKERS = 4`) are one pool for
the whole process, and four chats with one card each still fill it. So at most **two** owner
cards raised for foreign askers are pending across the whole process; a third flagged call
is refused at once with "another approval is already waiting for the owner". Two leave two
workers for every other agent's tools.

**D-j. The room axis gaps the review found** (Zcode; ADR-048, closed as the room closes,
not with cards). `transcripts/` (where `transcribe_audio_file` writes) and `output/` are mixed
stores (`tools/core.py`, `_MIXED_DIRS`); `get_task_status` withholds a task's result and error,
`list_my_processes` its command lines, and `get_dpc_context("device")` the device context, on
a turn that is not the owner's.

**D-k. The knowledge revision turn.** `_agent_auto_revise_proposal` runs as `ui` and pastes
the voters' comments into the prompt. With a comment from a node outside every own node
group, that node is the turn's asker (foreign, kind unknown), so a flagged tool the revision
reaches for asks the owner first; a revision with only own comments stays the owner's.

**D-l. A Telegram card only where only the owner can press it.** Without
`telegram_owner_user_id` a press counts in any private chat with the presser
(`AgentTelegramBridge._is_owner_press`), so a Telegram stranger could approve his own request.
On a foreign turn the card is offered on Telegram only when that id is set; otherwise only on
the desktop, and with no desktop client it is refused at once.

**D-m. Own nodes for the node's knowledge.** `own_node_group` is per agent, knowledge is the
node's. The 1:1 rule of point 3 uses the union of every agent profile's `own_node_group`
(`KnowledgeService._own_nodes`); no node-level setting exists.

## Considered Options

- **Gate the whole run, "Held" (ideas 04 b3.4).** One gate before `process_message` for
  every foreign trigger. Rejected: it also holds a harmless question, and it asks the
  owner about runs that never touch a tool.
- **Class by asker for memory too.** Rejected: ADR-048's rationale holds. A mixed room
  reads the answer, so the owner asking does not make a private layer safe to quote.
- **Per-tool switches ("ask on foreign request" in each profile).** Rejected: a switch
  per tool per agent is what the board calls "held by flags and written nowhere"
  (`THE-AUTONOMY-RULE-IS-HELD-BY-FLAGS-AND-WRITTEN-NOWHERE`). The flag is on the tool.
- **Keep refusing for `owner_only` tools.** Rejected by Mike: the owner can then never
  allow a friend one song, and cannot allow himself in a mixed room.
- **A reserved `own_nodes` group name.** Rejected already in ADR-048; the validated key
  `own_node_group` stays.

## Consequences

- **A friend's mention can now cost the owner a card.** On a node with a busy group the
  owner sees cards he did not see before. The one-card-per-chat rule bounds it to one per
  chat at a time.
- **An absent owner means a refusal.** A card shows on the desktop only, because a group
  turn has no Telegram chat id (`shell.py:2422`, `service.py:6385`). With no UI client
  attached, the call is refused at once ("nobody could be asked"). This is today's
  behaviour for Tier 1, now extended to more tools. See Q1.
- **`owner_only` stops meaning "refused".** For the tools in 2.6 it becomes the new flag.
  ADR-051 D43 and the glossary row `owner_only` are amended when this is built. The second
  owner checks inside the music tools (`music.py`, `@_guarded`) and `chat_post_file`
  (`chat_files.py:100-122`) must accept an approved call, and must do it per call, not per
  turn.
- **ADR-048's `run_shell` hole narrows, and does not close.** The path check reads literal
  paths only (ADR-048, Consequences). A Tier 0 command that builds a path at run time is
  still caught by the card for a foreign asker, not by the path check.
- **The relay rule holds on this node only.** Q2's inheritance is carried on the local path
  (`send_group_agent_message` -> `_handle_group_agent_mentions` ->
  `_invoke_agent_in_group`). Across nodes the asker is not on the wire: carrying it would need
  a signed field and a protocol version step (`message_signing.py`, the preimage versions), so
  a verified own node's agent post counts as an own agent. External harnesses on this node
  (CC_windows, Zcode_windows) are the owner by Decision 2.2: a foreigner who addresses one can
  have it wake an own agent with no card. `cc_group_mention` now carries the asker for the
  bridge to read, but its reply post carries nothing back; inheritance there is by
  convention, not code. Recorded as residual risk.
- **A relayed foreign request keeps its asker.** An own agent woken by a foreign asker can
  post a mention that wakes another own agent. That agent inherits the original asker for
  the whole chain (Q2, decided by Mike, AI Studio group #135, 2026-11-25), so its flagged
  calls still raise a card naming the foreign asker "via" the relaying agent.
- **The default of a new tool is "no card".** The flag is checked in one place, but each
  tool still has to carry it. The completeness test in step 2 is what stops a new tool
  from being forgotten.
- **The knowledge door narrows.** A peer's retransmit of a commit this node never judged
  waits for the owner.
- **In a mixed group the agent cannot write its own notes, even when the owner asked.**
  `update_scratchpad` and `update_identity` sit on the room axis (L5, `l5_closed_refusal` in
  `tools/core.py`), not on the asker axis, so Mike's own request in a group with a friend's
  node is refused there. This is ADR-048 as decided, and Mike's #126 ("personal memory vs
  group knowledge separation is in the ADRs"); recorded as an accepted consequence (reviewer
  Johnny), with no change.

## Implementation plan

Order matters. Steps 1 and 2 change nothing a person can see. Do not push before the full
core suite has run (about 30 minutes; subset runs have missed our own breakage twice).
Cross-platform: the card text, path display and the closed-path check must work on
Windows, Linux and macOS.

1. **The asker, carried.** A small value type `Asker` (kind: human, agent or unknown;
   name; node id; `is_owner`) next to `turn_class` in `dpc_agent/agent.py`. Compute it
   where the mention is routed and hand it to `process`:
   - peer path: `message_handlers/group_handler.py` (`_handle_agent_mentions`,
     `_invoke_agent`), from the author node, the verification verdict (D-a) and the
     signed `sender_type`;
   - local path: `service.py` (`_handle_group_agent_mentions`, `_invoke_agent_in_group`,
     and its other callers at the file and image posts; a voice post routes no mention), where the asker is this node's
     human, this node's agent or an external agent on this node;
   - `managers/agent_manager.py` (`process_message`, `process`) carries it;
   - `dpc_agent/agent.py` `process` replaces the unused `sender_node_id` parameter use and
     sets `ToolContext.asker`; `dpc_agent/tools/registry.py` `ToolContext` gets the field.
   Own nodes are `{this node} | own_node_group`, read as `turn_audience` reads them
   (`agent.py:394-405`). Doors other than a group: `ui`, `telegram_owner` and
   `external_agent` are the owner; `telegram` and `discord` are not.
2. **The flag and the gate.** `ToolEntry.owner_approval_for_others` and the optional
   `approval_check` (D-d) in `dpc_agent/tools/registry.py`; the gate in
   `ToolRegistry.execute` after the `owner_only` refusal; `override_handler` fixed (D-h).
   A **completeness test**: every registered tool is either flagged or named in an explicit
   read-only table, so a new tool fails the suite until someone decides. Set the flag on the
   tools of 2.5 in `tools/shell.py`, `tools/core.py`, `tools/git.py`, `tools/messaging.py`
   and `tools/browser.py`; the exact browser tools are fixed in this step and listed in the
   test.
3. **The card.** One owner approval card, built on the schedule card's machinery
   (`tools/core.py` `_await_schedule_approval`, `service.py` `announce_*_approval_request`,
   `local_api.py:323-325`), with the asker, chat, tool and "what exactly" fields; the UI
   side in `dpc-client/ui/src/lib/services/shellApproval.ts`,
   `services/scheduleApproval.ts`, `lib/coreService.ts` and the card component. One pending
   card per chat (2.8). The Tier 1 shell card carries the asker too (D-b). The loop's
   budget for a flagged call (D-c) in `dpc_agent/loop.py`.
4. **The `owner_only` tools.** Move `music_*` (`tools/music.py`), ComfyUI x7
   (`tools/comfyui.py`) and `chat_post_file` (`tools/chat_files.py`) from `owner_only` to
   the new flag; stop withholding them in `DpcAgent._get_allowed_tools`
   (`agent.py:1116-1122`); make their second owner checks accept an approved call; add the
   cost lines of 2.6 to the card.
5. **`run_shell` closed paths.** In `tools/shell.py`, refuse a path closed to the turn
   before any card, using `closed_to_this_turn` (`tools/core.py:132-199`). Leave
   `validate_extended_path` and `_extended_path_allowed` alone (2.7). This also closes the
   board card `THE-SHELL-GATE-DOES-NOT-KNOW-WHICH-PATHS-ARE-CLOSED-TO-THIS-TURN`.
6. **The scheduled wake.** Store the asker in the task data beside `_message_source`
   (`tools/core.py:2099`), always from the context, never from the payload; pass it into
   `process` at the wake sites (`agent.py:1535`, `1711`, `1739`, `1811`); fail closed for
   old rows (D-g).
7. **The UI field.** An `own_node_group` picker in
   `dpc-client/ui/src/lib/components/AgentPermissionsPanel.svelte`, listing the names in
   `node_groups` and saving through `save_firewall_rules`; `firewall.validate_config`
   already refuses an unknown name (`firewall.py:2917-2927`).
8. **Knowledge.** The owner card before `_apply_commit` for an unjudged
   `APPLY_KNOWLEDGE_COMMIT` (`message_handlers/knowledge_handler.py`), and own-node-only for
   a 1:1 proposal (`knowledge_service.py` `_local_roster_for_peer_proposal`).
9. **Docs.** GLOSSARY rows (added now), ADR-051 D43 and the glossary `owner_only` row
   amended, `docs/agent/DPC_AGENT_GUIDE.md` Tool Access Control section, and
   `docs/CONFIGURATION.md` for the new picker.

## Acceptance checks

Each is something a person or a test can see. Check them on a build, not on the code.

1. **Completeness.** The registry test of step 2 fails when a tool is registered that is
   neither flagged nor on the read-only table.
2. **Foreign human, flagged tool.** A friend's node in a group with Ark writes
   "@Ark save this to notes.txt". The owner's desktop shows one card naming the friend's
   name, node id and "human", the chat, `write_file` and the resolved path. With no answer
   for 60 s the file does not exist, and Ark's answer says the owner is not in focus.
3. **Owner in the same group.** Mike writes the same mention. No card, the file is written.
   The agent still does not quote L5 (ADR-048 unchanged).
4. **Own node.** A node listed in the agent's `own_node_group` asks: no card.
5. **Foreign agent.** A peer's agent at mention depth 0 asks: the card says "agent".
6. **Unverified post.** A post that is not `verified` asks: the card says "not verified"
   and the asker is foreign even if the claimed node id is an own node.
7. **Shell, every tier.** A foreign asker's `git push` (Tier 0), `copy a b` and
   `echo x > y` each raise a card with the command and the resolved `cwd`. A `git push
   --force` raises one card, not two (D-b).
8. **Closed path, no card.** A foreign asker's `type memory\scratchpad.md` is refused
   with a one-line reason, and no card is raised.
9. **Recall regression.** With the build, Active Recall still returns hits from a granted
   extended path on an owner turn (`_extended_path_allowed` unchanged in behaviour).
10. **One card per chat.** While a card is up, a second flagged call in the same chat is
    refused at once; the screen shows one card.
10a. **Two cards per process (D-i).** While two foreign-request cards are pending in two
    chats, a flagged call in a third chat is refused at once with "another approval is
    already waiting for the owner"; the tool pool keeps two free workers.
11. **`owner_only` tools.** A foreign asker's `music_propose_job` raises a card that states
    the cost; "refuse" creates no job file, "approve" runs it once. The next call asks
    again.
12. **The wake keeps the asker.** A `check_back` scheduled on a friend's request wakes
    with that friend as the asker; a flagged call in the wake raises a card naming the
    friend, not "owner".
13. **Own music return.** A `check_back` to the agent's own running music job raises no
    card, whoever the asker was when it was proposed (D-e).
14. **No owner at the desktop.** With no UI client attached and no Telegram origin, a
    flagged call on a foreign request is refused at once with "nobody could be asked".
15. **The picker.** Choosing a group in the agent permissions panel writes
    `memory.own_node_group` and the next turn uses it; the log line (`agent.py`,
    `_log_own_nodes`, run when the agent is created) shows it on the next start;
    choosing none leaves the field absent; a name not in `node_groups` is refused on save.
16. **Knowledge.** A peer's `APPLY_KNOWLEDGE_COMMIT` for a proposal this node never voted
    on waits for a card and is not applied on refusal; a 1:1 proposal from a node outside
    `own_node_group` is refused.
17. **Full suite.** The full core suite is green on Windows, and CI is green on Linux.

## Open Questions

- **Q1. Telegram for group cards** — Mike. A group turn has no Telegram chat id, so its
  card is desktop-only today and an owner away from the desktop gets a refusal. Options:
  the owner's private bot chat gets the card; or leave it desktop-only. Not decided, not in
  this ADR (Mike's call: later).
- **Q2. An own agent relaying a foreign request — decided** (Mike, AI Studio group #135,
  2026-11-25: "a good scenario"). A foreign human asks Ark; Ark posts "@Johnny, please run
  X". Without a rule Johnny's asker would be Ark, an own agent, and the request would be
  laundered. Decided: an agent woken by an own agent's post inherits the asker of the turn
  that wrote the post, for the whole chain; the card reads "Miron via Ark". The asker
  travels with the post the way `mention_depth` does, and Decision 2.9 does the same for
  `check_back`. Implementation: step 1 (the local path carries the posting turn's asker
  into the woken agent's `process`).
- **Q3. Tools off Mike's list — decided** (Mike, AI Studio group #138, 2026-11-25). With a
  card on a foreign request: `execute_skill`, `import_skill_from_agent`, `register_task_type`,
  `unregister_task_type`, `describe_image` and `transcribe_audio_file` (the last two spend the
  GPU card). Without a card: `kill_my_process` and the browser tools that only read. Folded
  into Decision 2.5 and its completeness table.
- **Q4. The roster has no owner.** The own-node test and the class read the member list,
  which a peer can change (`GROUP-ROSTER-HAS-NO-OWNER`, ADR-038 partly built). This ADR
  inherits that risk from ADR-048 and does not remove it.

## As built (2026-11-25)

Built on `dev` by CC, not pushed, not observed live. Where the build follows the code rather
than the text above, it says so here.

- **Step 1.** `Asker` (kind, name, node id, `is_owner`, `verified`, `via`) and
  `asker_for_source`, `peer_post_asker` sit next to `turn_class` in `dpc_agent/agent.py`.
  The routing computes it (peer path `group_handler._handle_agent_mentions`, local path
  `service._handle_group_agent_mentions`), `agent_manager` carries it, and
  `DpcAgent.resolve_asker` decides `is_owner` for a verified node of the agent's
  `own_node_group`, because that group is per agent and only the agent reads it. The
  `sender_node_id` parameter stays as the fallback of an older caller (a foreign asker of
  unknown kind on that node).
- **Step 2.** `ToolEntry.owner_approval_for_others`, `approval_check`,
  `budget_holds_approval`; the flags are set per module with `registry.mark_owner_approval`.
  The gate (`dpc_agent/owner_approval.py`, `open_gate`) runs in `ToolRegistry.execute` after
  the `owner_only` refusal **and after the argument check**, so the card shows the arguments
  the handler will get. Every turn `DpcAgent.process` makes carries an asker. A context with
  no asker, no source and no class is **not** the owner's since follow-up 13 (below): only a
  context marked `ToolContext.internal_call` passes as an internal call.
- **Step 3.** The card is the schedule card's machinery, task type `owner_approval`, with
  `asker`, `tool`, `details` and `cost` fields; the schedule, music and shell cards carry
  `asker` (and the shell card `cwd`) on a foreign turn. The UI shows them
  (`ScheduleApprovalDialog` via `utils/scheduleApprovalText.ts`, `ShellApprovalDialog`, which
  also hides "Approve + Whitelist" on a foreign request).
- **Step 4.** No tool is `owner_only` now; the filter in `_get_allowed_tools` stays for a
  later one.
- **Steps 5-9** as written, with D-a, D-c, D-i to D-m above.

Pinned by tests (`dpc-client/core/tests/`): acceptance checks 1, 2-6, 7, 8, 9, 10, 10a, 11,
12, 13, 14, 15 (backend half; the picker in `ui/src/lib/utils/ownerApprovalCard.test.ts`) and
16. Check 17, the full suite on Windows and CI on Linux, is the parent's run.

### Follow-up 13 (Zcode's review #154 on this record, #143 on the task queue; 2026-11-25)

Built on `dev` by CC, not pushed, not observed live.

1. **A wake answers in the chat it was scheduled in** (P2). `schedule_task`
   (`dpc_agent/tools/core.py`) drops every key of the model's `task_data` that starts with
   `_` and says which in its answer; `_reply_conversation_id` and `_reply_telegram_chat_id`
   come from the context only, like `_message_source` and `_asker` already did, and
   `DpcAgent.schedule_task` still sets `_provider_alias` itself. Before, a foreign asker could
   name `_reply_conversation_id` of a chat made only of the owner's nodes; the wake ran with
   that room's class (all layers open) and posted there. No owner-asked schedule targets
   another chat today (nothing documents or uses it), so there is no owner exception: the
   owner who wants a wake elsewhere schedules it from that chat.
2. **The two most dangerous handlers check again** (P3). `run_shell` (`tools/shell.py`,
   `_not_the_owners_call`) and `comfyui_submit` (`tools/comfyui.py`) refuse a foreign asker
   unless the call carries the gate's per-call mark (`owner_approval.may_run_owner_tool`:
   approved by the owner, or on its way to the tool's own Tier 1 card), as music's
   `_not_owner` and `chat_post_file` do. A direct handler call that bypasses
   `ToolRegistry.execute` no longer skips the card. run_shell's Tier 2 refusal stays first: it
   is a fixed rule for everybody.
3. **No turn is not the owner** (P3). `owner_approval.asker_is_owner` no longer treats "no
   asker, no source, no class" as the owner's: such a context, and no context at all, is
   foreign. A builder that means an internal call sets `ToolContext.internal_call=True`; a
   real asker or source always wins over the marker. No live builder sets it: the three that
   exist are `DpcAgent.process` (every field set), `ToolRegistry.__init__`'s default context
   (replaced by `set_context` before any call; left unmarked, so a call that ever reached it
   fails closed) and `DpcAgent._extended_path_allowed` (Active Recall's probe; it calls only
   `validate_extended_path`, which reads no asker, and keeps working unmarked). The room axis
   (`tools.core.turn_audience`) keeps its own rule for a bare context; that rule gates memory
   layers, not tools, and was not part of the review.
4. **A stored asker's `is_owner` is recomputed** (P3). `DpcAgent.resolve_asker` never reads
   the given flag: at the owner's doors (`ui`, `telegram_owner`, `external_agent`, D-f) the
   door is the owner unless the asker names a node this agent knows is neither this node nor
   an own node; everywhere else only a verified node that is this node or in the agent's
   `own_node_group`, read now, is the owner. A scheduled task's stored `_asker` with
   `is_owner: true` from a node since taken out of the group, or written by hand, is foreign.
5. **The knowledge card shows what would be written** (P3). The owner card for a peer's
   unvoted commit (`message_handlers/knowledge_handler.py`) adds the first line of each of the
   first five entries (120 characters each, the rest counted) to `details`, within the card's
   600-character bound.
6. **No window under the owner's login outlives a foreign turn** (P3). At the end of a turn
   whose asker is not the owner, the loop (`loop._end_foreign_turn`, in `run_llm_loop`'s
   `finally`) closes the agent's interactive browser window on a thread of its own, as the
   Kill does (`browser.close_browser_sessions_of(..., interactive_only=True)`); the fetch
   browser carries no identity and is left. `browser_snapshot`, `browser_extract` and
   `browser_screenshot` refuse,
   on a foreign turn, a logged-in page (a scoped, not anonymous session) last opened on the
   owner's own turn (`browser._owner_page_refusal`; `browse_page(keep_open)` and
   `browser_navigate` record the turn, a session with no record counts as the owner's).
7. **Task queue** (#143). An overdue `reminder`'s skip line quotes the first line of its text,
   up to 160 characters (`DpcAgent._post_overdue_notice`); the comment and log line about a
   task caught in `running` at a restart now say it is retried unless already overdue, when
   it is skipped as overdue (`task_queue._load_queue`); `DpcAgent.schedule_task` raises when
   its queue has no running processor, so the Task Board path refuses too.
8. **A check_back card is withdrawn by Stop** (#92/#103): its approval passes `void_on_stop`,
   as the music job's and the gate's do; before, only a Kill withdrew it.
9. **The local-path wake line names the sender** (`service.local_wake_sender`): name, node,
   human or agent, "written on this node", and for a relayed request its asker with that
   asker's verdict, so wakes can be counted by human vs agent.

Residuals, stated. A window the owner opened and was still using is closed by the next
foreign turn of the same agent; reopening it is one call. `browser_scroll`, `collect`,
`wait_for` and `switch_tab` still return what they read of a logged-in page on a foreign turn
without that check (the review named snapshot and extract; the end-of-turn close bounds the
rest to the turn that found the window). A context built by future code without an asker now
gets a card or a refusal on every flagged tool; that is the intended direction and the
reason the marker exists. Pinned by
`test_a_wake_answers_in_the_chat_it_was_scheduled_in_and_the_model_names_no_route.py`,
`test_the_gate_fails_closed_without_a_turn_and_the_two_most_dangerous_handlers_check_again.py`
and `test_a_foreign_turn_leaves_no_owner_window_and_cards_and_lines_say_what_they_are_about.py`.

### Follow-up 15 (Zcode's check #184 on follow-up 14; 2026-11-26)

1. **Click blindness** (P3). The cards of `browser_click`, `browser_fill` and `browser_select`
   (`browser._browser_click_approval`, `_browser_fill_approval`, `_browser_select_approval`)
   name where the action leads, read from the open page without acting
   (`AuthBrowser.action_destination`): a link's resolved address, a submit control's or a
   field's form action and method, each marked as the page's origin or another one; when the
   page cannot tell, "destination unknown (script)". After each action that can move the page
   (click, fill, select, download, switch_tab) the per-navigation mark is re-read
   (`browser._reevaluates_mark`): the origin the owner approved is kept with the mark, and a
   foreign turn's action that leaves it, or leaves it unreadable, blocks snapshot, extract and
   screenshot on a foreign turn until `browser_navigate` asks the owner again; the owner's own
   action resets the mark as the owner's. A session that cannot carry the marks counts as the
   owner's page and never breaks a browse (the visible-window tests that failed on a bare
   object are green). Residual: `browser_download` keeps its raw-arguments card, and
   `browser_scroll`, `collect` and `wait_for` still read without the check.
2. **The per-turn byte cap counts the copy** (P4). In the 1:1 path of `chat_post_file`
   the copy is read whole after it is made, its size and type come from the same read as in
   the group path, and the turn's 200 MB cap is checked against that size, so a source that
   grew between the stat and the copy no longer gets past it.
3. **`chat_history` clamps `limit`** (P4). With `include_tool_calls` a `limit` above 200 is
   lowered to 200, the 2000-call budget holds for any limit, and the header and the tool
   description say so.

Pinned by `test_a_click_card_names_where_it_leads_and_a_foreign_action_that_leaves_the_approved_origin_needs_a_new_approval.py`,
`test_chat_post_file_posts_only_allowed_types_by_content_and_leaves_no_partial_copy_after_a_cancel.py`
and `test_an_agent_reads_back_its_own_past_tool_calls.py`.

## Authors

- **Mike** — Decision
- **Zcode** — Review (#125, #130, #143, #154)
- **CC** — Design, research, this record

## References

- [ADR-048](048-memory-layer-audience.md) — the room axis; turn classes; `own_node_group`.
- [ADR-030](030-run-shell-safety-guardrails.md) — the tiers and the approval card.
- [ADR-051](051-the-music-player-is-a-dpc-window-over-one-core-music-service.md) — D43
  `owner_only`, D47 and D49 (the music card and the return).
- [ADR-052](052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md)
  — the card lease behind the cost line of 2.6.
- [ADR-023](023-group-chat-participant-model.md), [ADR-038](038-group-roster-as-signed-state.md)
  — the agents map and the roster.
- Board (local `backlog.md`, not in the repository): A-PEER-TRIGGERED-RUN-SPENDS-THE-OWNERS-PROVIDER-AND-ASKS-THE-OWNERS-SCREEN,
  THE-AUTONOMY-RULE-IS-HELD-BY-FLAGS-AND-WRITTEN-NOWHERE,
  THE-APPROVAL-DIALOG-IS-NOT-BOUND-TO-THE-CONVERSATION-THAT-CAUSED-IT,
  THE-SHELL-GATE-DOES-NOT-KNOW-WHICH-PATHS-ARE-CLOSED-TO-THIS-TURN,
  FOUR-PENDING-APPROVALS-STARVE-EVERY-AGENTS-TOOLS,
  A-GROUP-HAS-NO-AUDIENCE-OF-ITS-OWN-SO-ITS-PRIVACY-FOLLOWS-ITS-ROSTER (`backlog.md:1478`).
- `ideas/agent-communication-comparison/04-differences-and-what-to-take.md` b3 item 4;
  `ideas/dpc-research/articles/2608.27146-sara-tool-outputs-as-commands.md`;
  `ideas/dpc-research/articles/2608.27141-safety-does-not-compose-loopharness.md`.
