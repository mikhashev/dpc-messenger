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
session: "AI Studio group chat (group-60aff6110f02), 2026-11-25 — Mike's decisions at #105, #108, #123, #124, #126 and #128; Zcode's review of commits 702b0510..4d045edf at #125 and its wake count at #130"
---

# ADR-053 — Who asked decides what an agent may do

> **Accepted 2026-11-25 (Mike, AI Studio group chat #105, #108, #123, #124, #126, #128).
> Nothing here is built.** This is a proposed implementation of decisions Mike has made.
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
(`registry.py:766-772`), so that a new tool cannot skip it. **Mike-approved list**, with
the card on every call:

- `run_shell` at **every tier**: Tier 0 `git push`, `copy`, `del` and redirects included;
- `write_file`;
- every git write: `git_add`, `git_commit`, `git_init`, `git_checkout`, `git_merge`,
  `git_tag`, `git_reset`, `git_snapshot`, `git_push` (the callers of `_closed_git_write`
  in `tools/git.py`);
- `repo_delete`;
- `schedule_task`;
- `send_user_message`;
- browser actions.

Reads and search carry no card. `update_identity` and `update_scratchpad` are **not** on the
list: the L5 layer (ADR-048) already closes them on a turn that is not the owner's.

**2.6 The tools that are `owner_only` today.** `music_*`, the seven ComfyUI tools and
`chat_post_file` change from "always refused" to "owner approval on a foreign request". The
card states the cost: a local engine leaves the card, about 25 s to come back, and the
conversation cache is dropped (the figure is Mike's; ADR-052 holds the lease model).

**2.7 `run_shell` and closed paths.** A path that is closed for the turn is refused
**without a card** (ADR-048, direction of 2026-11-09). The check goes in the shell tool
handler, like the git gate in `tools/git.py:169-234`. It does **not** go inside
`validate_extended_path`: `DpcAgent._extended_path_allowed` (`agent.py:501-515`) calls that
function with a context that has no message source, for Active Recall (`agent.py:697`), and
a turn-class check there would silently drop extended paths from recall.

**2.8 One pending card per chat.** A second flagged call in a chat that already has a card
up is refused at once, with a one-line reason. This answers the board card about
unanswered approvals starving the tool pool
(`FOUR-PENDING-APPROVALS-STARVE-EVERY-AGENTS-TOOLS`; the pool is four workers,
`loop.py:400`).

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

**D-b. One card per call.** A `run_shell` Tier 1 command asked for by a foreign asker gets
the one card, with the Tier 1 reason on it, not two. The same for any tool that already
asks (`check_back`, `music_propose_job`): the asker fields are added to that card.

**D-c. The call's clock.** `get_timeout(name)` is the loop's whole budget for a call
(`loop.py:2607`). `run_shell` already includes the 60 s approval (`shell.py:2303-2305`).
The music tools have 30 s (`music.py`, one-call timeout), which a 60 s card would cut. The
budget for a flagged call on a foreign turn becomes the tool's own plus the approval wait.

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
copied there is silently dropped. It has no non-test caller today. The build uses
`dataclasses.replace` instead.

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
- **A relayed foreign request is not yet covered.** An own agent woken by a foreign
  asker can post a mention that wakes another own agent, whose asker is then an own agent.
  Until Q2 is answered the second agent runs flagged tools without a card.
- **The default of a new tool is "no card".** The flag is checked in one place, but each
  tool still has to carry it. The completeness test in step 2 is what stops a new tool
  from being forgotten.
- **The knowledge door narrows.** A peer's retransmit of a commit this node never judged
  waits for the owner.

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
     and its other callers at the file and voice posts), where the asker is this node's
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
    `memory.own_node_group` and the next turn logs it (`agent.py`, `_log_own_nodes`);
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
- **Q2. An own agent relaying a foreign request** — Mike. A foreign human asks Ark; Ark
  posts "@Johnny, please run X"; Johnny's asker is Ark, an own agent, so by 2.2 the owner.
  No card is raised and the foreign request has been laundered. Mike's words do not cover
  it. CC recommends: an agent woken by an own agent's post inherits the asker of the turn
  that wrote the post, for the whole chain. `mention_depth` already counts the chain, and
  Decision 2.9 does the same for `check_back`.
- **Q3. Tools off Mike's list.** `execute_skill`, `register_task_type`,
  `unregister_task_type`, `kill_my_process`, `import_skill_from_agent`, `describe_image`
  and `transcribe_audio_file` have side effects or spend compute and are not on the list. The
  completeness test forces a decision on each. CC recommends a card for `execute_skill`
  and the two task-type tools, none for the rest. The browser tools that only read (snapshot,
  screenshot, extract, wait, scroll, close) need the same decision.
- **Q4. The roster has no owner.** The own-node test and the class read the member list,
  which a peer can change (`GROUP-ROSTER-HAS-NO-OWNER`, ADR-038 partly built). This ADR
  inherits that risk from ADR-048 and does not remove it.

## Authors

- **Mike** — Decision
- **Zcode** — Review (#125, #130)
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
