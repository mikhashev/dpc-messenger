---
adr: 054
title: "A peer runs music jobs on another node's engines through a remote music provider"
status: proposed
date: 2026-11-27
axis: network, collective
deciders: [owner]
consulted: [CC]
informed: []
depends_on: [ADR-051, ADR-052, ADR-053]
related: [ADR-031, ADR-040, ADR-041, ADR-047, ADR-048]
supersedes: []
session: "AI Studio group chat, 2026-11-27, #72 — backlog card A-PEER-NODES-MUSIC-PLAYER-CANNOT-USE-THE-MUSIC-ENGINES-SET-UP-ON-ANOTHER-NODE-BECAUSE-MUSIC-HAS-NO-REMOTE-PROVIDER; the owner: write the ADR if it is needed"
---

# ADR-054 — A peer runs music jobs on another node's engines

> **Proposed, 2026-11-27. Draft by CC; nothing here is decided.** Every claim about
> existing behaviour carries a `file:line` read at `dev` HEAD `80df5c55` on 2026-11-27
> (paths under `dpc-client/core/dpc_client_core/` unless stated). Anything not read in the
> code is marked **Assumption**. Every new name (commands, settings, fields) is a proposal.
> Items the owner must settle are marked **Open (owner decides)** and gathered in
> [Open Questions](#open-questions).

## Context and Problem Statement

The music engines are local by construction. acestep.cpp and yue2.cpp are started and
watched by the core and bound to loopback only: "The host is always 127.0.0.1"
(`music/engine_base.py:40`, `LOOPBACK` at `:80`; `music/acestep.py:30`; ADR-051 D30,
`051:466-471`). A job reaches them only through the core's job store and runner, one job
at a time, oldest first (`music/runner.py:1-12`), holding the card lease while it runs
(`music/runner.py:1022-1027`, holder `music job <id>`).

Two other compute kinds already cross nodes, and music has neither:

- **Text models:** a guest configures a `remote_peer` provider naming the host's
  `peer_id` and alias (`providers/remote_peer_provider.py:145-180`); the host's door
  checks the peer's proved key first (`p2p_coordinator.py:855-875`, ADR-041 D2), then the
  firewall's `compute` allow-lists (`firewall.py:2246-2306`), class, ceiling and quota, and
  writes one ledger row per served call (`node_ledger.py:1-60`, `served_rows` `:880-890`).
  Answers stream back as `REMOTE_INFERENCE_CHUNK` frames (`dpc_protocol/protocol.py:215-227`).
- **Voice:** a `remote:<node_id>:<alias>` voice provider sends the audio to a peer's
  Whisper (`service.py:4910-4947`); the host checks `can_request_transcription`
  (`firewall.py:2308-2340`) and runs the job on its card as served to that peer
  (`p2p_coordinator.py:1416-1419`: `serving_a_peer`, holder `peer <id>`). The music
  player's lyrics chain already uses it: a `remote:` voice choice is passed through
  (`music/commands.py:36`, `:515-516`).

So a node without engines (for example a Linux node) cannot have its **own player** run
a job on engines that another node has already set up. The only path today is the agent:
in a group, the peer asks the owner's agent; `music_propose_job`
(`dpc_agent/tools/music.py:697-888`) raises the owner's approval card (ADR-053,
`dpc_agent/owner_approval.py:351-362`), the job runs on the owner's card, and the take is
posted to the group as a plain audio attachment whose music block never crosses
(ADR-051, `051:938-943`). That is *asking the owner*, not *the peer's player computing on
the owner's node*: the peer gets no job card, no progress, no provenance record, and no
library entry of its own.

## Decision Drivers

- **One card, owner first.** The owner's GPU is one card shared through the lease
  (ADR-052). A peer's job must queue behind the owner's waiters, as a peer's
  transcription does (`052:529`), and never preempt a running owner job.
- **Consent is the owner's, per peer.** Every existing remote door is off by default with
  per-node and per-group allow-lists (`firewall.py:1762-1791`).
- **Nothing private leaves either node.** Rule #19 (`dpc_agent/tools/music.py:197-203`,
  `dpc_agent/owner_approval.py:133-181`): in a multi-node group nothing from 1:1 chats
  leaks. Provenance has one redaction function for anything leaving a node
  (`music/provenance.py:456-462`) and a privacy check (`:265-282`).
- **Licences travel, they do not block.** The owner, relayed in the brief for this ADR
  (2026-11-27): "nothing blocks the sale, it is a warning to the user, like a kitchen
  knife". The licence block (`music/provenance.py:170-192`) must reach the peer intact.
- **Reuse what is built.** The remote_peer split (guest-side provider, host-side door),
  the music capability report (`music/capabilities.py:1-47`), the job store, the file
  transfer.
- **Cross-platform.** The peer may be Linux or macOS with no engine; the owner any OS.

## Prior art outside our engine

- **(a) The project's own reading.** `rg --no-ignore -l -i
  "music|petals|swarm|offload|remote gpu|job queue|consent" ideas/dpc-research/articles`
  (2026-11-27): hits concern MoE expert offload (`2608.16157-freetoken-edge-native-moe-serving.md:121`)
  and agent papers; nothing on running a job on another node's engine. **Nothing applicable
  found.** Ruach Studio notes (ai-studio `experiments/music-player/docs/RUACH-BORROW.md`,
  grep `remote|peer|network|queue`): one hit, line 59, `waitguard.py` — an ETA that is
  `None` when unknown, never 0. **Take:** a remote job's ETA is unknown until measured.
- **(b) The topic elsewhere.** From CC's knowledge, **not fetched this session**:
  - AI Horde (https://aihorde.net): async submit, poll status, fetch result; workers
    choose which models they serve; a credit ("kudos") orders the queue. **Take:** the
    async submit/status/fetch shape and a ledger hook for credit; **not taken:** a public
    pool of strangers.
  - ComfyUI's HTTP API (https://github.com/comfyanonymous/ComfyUI): `/prompt` returns an
    id, progress arrives as events, outputs are fetched from history. **Take:** job id,
    pushed progress, result fetched separately from the request.
  - Blender Flamenco (https://flamenco.blender.org): a manager owns the queue, workers
    only run. **Take:** the owner's job store is the only queue; the peer holds a proxy.
  - BOINC (https://boinc.berkeley.edu): redundant computation and result validation
    against untrusted hosts. **Not taken:** peers here are allow-listed and proved.
  - Petals (https://github.com/bigscience-workshop/petals): one model split across nodes.
    **Not taken:** a song job fits one card; splitting is not the problem.

## Decision

**A peer reaches another node's music engines through a remote music provider on its own
side and a music door on the owner's side, over DPTP, in the shape of `remote_peer`: the
peer's music service treats `remote:<node_id>` as one more engine choice; the owner's door
admits, queues, runs and returns the job through its own job store, card lease and
provenance; the take lands in the peer's library as the peer's own track.**

### D1. Shape: guest-side provider plus host-side door (the remote_peer split)

- **Peer side.** The music service gains a remote engine source: for each allowed peer,
  a capability report fetched over DPTP and merged into the local report under
  `remote:<node_id>`, so the Create form renders it as it renders `yue2` and `acestep`
  today (`music/capabilities.py:200`, `report` `:578-667`). A job on it is a **proxy job**
  in the peer's own job store (same `mp.job/1` record, engine word `remote:<node_id>`),
  so job cards, cancel and review work unchanged. The runner hands a proxy job to the
  remote client instead of an engine.
- **Owner side.** A music door in `p2p_coordinator.py`, beside `handle_inference_request`
  and `handle_transcription_request`: it calls `propose_music_job` (`music/commands.py:1353`)
  with a peer requester, so the job goes through the same checks, store, runner and lease
  as a person's job.
- **Wire (proposed names, a DPTP minor bump as v1.7 and v1.14 were):**
  `MUSIC_CAPABILITIES_REQUEST/RESPONSE`, `MUSIC_JOB_REQUEST` (type, params, `request_id`
  minted by the peer), `MUSIC_JOB_STATUS` (owner → peer, pushed on each state change and
  answered on request), `MUSIC_JOB_CANCEL`, `MUSIC_JOB_RESULT` (the withheld provenance
  record and the file reference); the take itself as a file (D3).
- **Never tunnel the engine port.** The peer never talks HTTP to the owner's engine.

### D2. Consent and limits on the owner's side

- **A new `music` section in `privacy_rules.json`, off by default**, mirroring `compute`
  and `transcription` (`firewall.py:1762-1791`): `enabled: false`, `allow_nodes`,
  `allow_groups`, `engines` (empty = none), `adapters` (D4), `approval`, `quota`. A
  `can_request_music(peer, engine)` check in the shape of `can_request_transcription`
  (`firewall.py:2308-2340`).
- **Identity before everything** (ADR-041 D2): the door runs `peer_proof` first, as the
  inference door does (`p2p_coordinator.py:855-875`). The transcription door has no such
  check today (`p2p_coordinator.py:1350-1360` goes straight to the firewall); the music
  door follows inference, not transcription.
- **Approval.** Two modes, set per allow entry: `per_job` (the owner sees a card naming
  the peer, engine, type and length; the job is queued only on yes) or `standing` (queued
  at once). **Recommended default: `per_job`**, and `standing` for nodes in the owner's
  own node group (ADR-053 2.2 counts those nodes as the owner, `053:121-131`).
  **Open (owner decides).** The ADR-053 card is raised from an agent turn
  (`owner_approval.py:351-362` takes a `ToolContext`); a peer request has none.
  **Assumption:** the card's UI can be reused with a new non-agent trigger.
- **Quota**, counted from ledger rows (D6) with `NodeLedger.count_since`
  (`node_ledger.py:807-832`): `jobs_per_day`, `gpu_seconds_per_day`, `concurrent`
  (proposed default 1 running or queued per peer, as `[music] agent_queue_max` limits an
  agent, `music/commands.py:96-97`, `:433`). A refused job is not a row, as on the
  inference door. Numbers: **Open (owner decides).**
- **Queue priority.** The runner today is strictly oldest first (`music/runner.py:1-2`,
  `:10`). Proposed: the runner takes the oldest **owner** job (person or agent) before
  any queued peer job; a running peer job is never preempted.
- **The card lease.** The job keeps the runner's holder name (`music job <id>`,
  `music/runner.py:1025`) and runs inside `serving_a_peer(peer_id)`
  (`managers/llama_server_supervisor.py:232-238`), so the lease knows it is a guest's.
  It waits in the lease line behind every owner waiter, as ADR-052 gives a peer
  (`052:529`); a refusal shown to the peer is `LeaseRefusedForPeer`, which names no alias,
  agent or chat (`managers/card_lease.py:148-150`, `052:503-504`). Music jobs wait with no
  deadline (`052:528`); a peer job gets one: `[music] peer_wait_s` (proposed), plus the
  cheap guard the transcription door uses (`p2p_coordinator.py:1413`: the peer is still
  connected) at the wait and at the grant. A granted peer job yields only by the owner's
  hand (the existing Cancel and Stop buttons), never automatically.
- **Not shared onward** (ADR-041 D7): the door serves only local engines; a node never
  re-serves a `remote:` engine it reaches itself.

### D3. What travels each way

- **In:** `type`, `params`, `request_id`, optionally `request.title`. Checked by the same
  code as a local job: `check_params` and `unsupported_reason`
  (`music/capabilities.py:532-553`, `:467-476`), the job schemas. **No `track_id`, no
  library reference**: the door accepts only types that need no source audio on the
  owner (generate from text or from lyrics) until a later slice carries source audio in.
- **Out, the take:** the FLAC as a file over the existing transfer
  (`managers/file_transfer_manager.py:1001` `send_file`; ADR-047's pull when built,
  `047:404-413`), never inline. The voice precedent inlines base64 in one frame
  (`dpc_protocol/protocol.py:358-390`), which caps at 64 MiB (`:662`) — right for a voice
  note, wrong for a song. **Assumption:** a 4-minute stereo FLAC is roughly 25-50 MB,
  under the sender's 1000 MB default (`service.py:508`).
- **Out, the record:** the take's provenance passed through `withhold_for_export`
  (`music/provenance.py:456-462`: `node_id`, `requested_by.conversation_id/turn_id/node_id`,
  `request.message_id`, `request.text` become `withheld_on_export`, `:407`), then
  `check_privacy` on the serialized record (`:265-282`: no paths, home, user name, IP,
  token) before it is sent. A record that fails is not sent and the job fails with the
  kind named, never the match.
- **What must stay:** the `licence` block exactly as `licence_record` wrote it
  (`music/provenance.py:170-192`, docs `:54-61`), so the peer's library can tell
  non-commercial takes apart. It is shown as a warning, never as a block. ADR-051 D17 still
  reads "They block anything published or sold" (`051:314-316`); the export code has no
  such block (grep of `non_commercial` in `music/export.py`: none). Aligning D17's text with
  the owner's 2026-11-27 statement is outside this ADR.
- **What the peer sees of the owner:** the capability report filtered to the allowed
  engines and adapters, by name and status only; never the owner's library, other jobs,
  queue contents or who holds the card. Default: **no library access**, and nothing in
  this door can read one.

### D4. Which engines and adapters

- The peer may pick any engine the owner lists in `music.engines` and the capability
  report marks available.
- **Voice adapters: not offered by default.** They are trained on real readers' voices
  without stated consent and are marked "for local listening only"
  (`music/yue2.py:67-69`, `ADAPTER_NOTE` `:147-148`). Purpose adapters (the instrumental
  one, chosen by the job's kind) may be offered. `music.adapters` lists the names the
  owner opens. **Open (owner decides).**

### D5. Where the take lives

- **On the peer:** its own library entry, through the one entry point
  (`music/library.py:1441-1499` `register_generated`, keyed by the proxy job's id), with
  the received record as its provenance plus a peer-side field
  `computed_on: {kind: "remote_node", node_id}`. `_withhold_for_export`
  (`music/provenance.py:433-454`) must learn that field so the peer's own export withholds
  it too.
- **On the owner:** the take is not registered in the owner's library. Its files and job
  record are kept only until the peer confirms the file, or until `[music]
  peer_result_hold_h` (proposed 24 h) passes, then removed. The ledger row (counts only, no
  content) stays. Whether the owner may keep a copy: **Open (owner decides)**, default no.

### D6. Ledger

One row per served job on the owner (`caller_kind: "peer"`, `route: "local"`, alias such
as `music:yue2`, tokens null, `duration_s` the job's card time) and one consumed row on
the peer (`route: "peer"`, `served_by`), the pair the ledger already defines
(`node_ledger.py:880-902`). **Assumption:** the row vocabulary (enforced, test
`test_the_vocabulary_is_enforced_at_the_row`) needs a value for a non-token job. The
tariff is per 1M tokens (`firewall.py` `serving_tariff`, `node_ledger.py:41-52`), so a
music price (per job or per GPU second) is a new column group. **Open (owner decides).**

### D7. Failure and cancel

| Event | Owner side | Peer side |
|---|---|---|
| Engine not installed / not built | capability report says so (`not_installed`, `not_built`, `music/capabilities.py:14-19`); propose refused `unsupported` | the form never offers it |
| Card busy | job `queued`, waiting in the lease line | status "waiting for the owner's GPU", no holder named, ETA `None` until known |
| Peer offline while queued or waiting | the guard cancels it (no card time spent) | proxy job marked failed `peer_gone` on reconnect |
| Peer offline while running | job finishes; result held for `peer_result_hold_h` | on reconnect asks `MUSIC_JOB_STATUS`, then fetches |
| Owner cancels | the person's Cancel takes any job (`music/commands.py:1474-1478`) | `cancelled_by_host` |
| Peer cancels | `MUSIC_JOB_CANCEL` → `cancel_job` with the peer requester, own jobs only (as an agent's own-only cancel, `music/jobs.py:763-795`) | proxy job `cancelled` |
| Owner restarts | the store resumes queued jobs (`music/runner.py:10-12`); a peer job resumes only if its peer is connected | status re-sent on reconnect |
| Request id reused | refused, per peer, as on the inference door (`p2p_coordinator.py:119-121`) | — |

### D8. Privacy and the agent path

- The door reads no conversation, no memory layer and no library, so rule #19 has nothing
  to leak through it. A peer job's `requested_by` is a new kind `peer` with `node_id`
  only (`REQUESTER_KINDS` is person and agent today, `music/job_schemas.py:233`, schema
  `:65-71`); it carries no `conversation_id` or `turn_id`.
- **The agent path stays.** It serves a different act: a group member asks the owner's
  agent in chat, the owner approves, the take is posted to the room (ADR-053; ADR-051
  `051:752-760`, `:938-943`). The new door is player to player, 1:1, with no agent and no
  chat. Neither replaces the other; their queue limits are counted separately.

### D9. Cross-platform

- **Peer:** needs no engine, binary or GPU; the remote source is pure DPTP and file
  transfer, so it runs on Windows 10-11, Linux and macOS alike. No path crosses the wire.
- **Owner:** any OS whose capability report shows an available engine. yue2.cpp has a
  Windows build recipe only today (`music/yue2.py:150`), so a Linux or macOS owner will
  usually report it `not_installed`; the probe, not the OS, decides. On macOS the lease
  has no VRAM reading (`052:943`); it does not change the queue rule.
- **Tests:** local runs are Windows, CI is ubuntu; every slice's tests use a fake peer
  and a fake engine and must pass on both. Each slice names its live two-node check
  (owner on Windows with engines, peer on Linux without).

### Rationale

The remote_peer split already answers the hard parts for text: identity, consent, the
host deciding what runs, a ledger row on the node that spent the card. Music needs the
same parts plus a long-running job, a large result and a provenance record. Putting the
door at the **job** level, not the engine level, keeps the owner's store, runner, lease
and provenance as the single path every take goes through (ADR-051's "one core music
service"), so a peer's job is limited, queued, recorded and redacted by code that already
exists. Putting a proxy job in the **peer's** store keeps its player unchanged.

## Considered Options

- **A. Tunnel the engine HTTP port to the peer.** The peer's runner drives the owner's
  engine directly.
- **B. Owner exposes a music service over DPTP; the peer gets a separate "remote music"
  UI.**
- **C. Guest-side remote provider plus host-side job door (recommended).** B's door
  with A's place in the peer's player.
- **D. Keep the agent path only.**

### Pros and Cons of the Options

#### Option A
- Good: smallest wire; the peer's runner already knows the engine API.
- Bad: bypasses the owner's job store, lease, quotas and provenance; breaks loopback-only
  (`music/engine_base.py:40`); no consent point; one engine call is not one job.

#### Option B
- Good: all control on the owner.
- Bad: a second UI for the same act; the peer's library and job cards would not know the
  take.

#### Option C
- Good: owner keeps control (B); peer's player unchanged (A); same pattern as text.
- Neutral: a proxy job is new state on the peer that must be reconciled after
  disconnects.
- Bad: the largest build of the four.

#### Option D
- Good: nothing to build; already approved per job.
- Bad: the card's problem stays: no job card, progress, record or library entry on the
  peer, and the owner has to be at the keyboard for every job.

## Consequences

- **Positive:** a node without engines makes music on its own player; the owner's card
  is used by guests under the same rules as text and voice; every take keeps its licence
  and lineage wherever it is computed.
- **Negative:** a new DPTP command family and a privacy-rules section to maintain; peer
  jobs lengthen the owner's queue; the transcription door's missing proof check
  (`p2p_coordinator.py:1350-1360`) becomes more visible beside a music door that has one
  (a card for it is suggested, not filed by this ADR).
- **Neutral:** the agent path is unchanged.

## Confirmation

- [ ] With `music.enabled` false, every music command from a peer is refused before the
      firewall answers about engines, and no job, file or ledger row exists.
- [ ] A peer over an unproved tier is refused before any engine is named (ADR-041 D2).
- [ ] The owner's queued job runs before an older queued peer job; a running peer job is
      never preempted.
- [ ] The record the peer receives has `node_id`, `requested_by.node_id`, `request.text`
      withheld and passes `check_privacy`; its `licence` equals the owner's record's.
- [ ] No voice adapter is offered or accepted unless the owner listed it.
- [ ] The owner's library gains no entry from a peer job; its files are gone after the
      hold.
- [ ] A peer that disconnects while waiting spends no card time.

## Scope

- `dpc-protocol/dpc_protocol/protocol.py` — the music command builders; DPTP doc bump.
- `firewall.py` — the `music` section and `can_request_music`.
- `p2p_coordinator.py` — the music door (proof, firewall, quota, propose, status, cancel,
  result).
- `music/commands.py`, `music/jobs.py`, `music/job_schemas.py` — requester kind `peer`,
  owner-first pick, peer cancel, result hold and cleanup.
- `music/runner.py` — owner-first order; proxy jobs handed to the remote client.
- `music/capabilities.py` — filtered report for a peer; merged `remote:` engines.
- `music/provenance.py` — `computed_on`, and its withholding.
- `node_ledger.py` — a row for a non-token job.
- `ui/src/lib/components/music/` — the remote engine in the form; the owner's approval
  card and settings.

## Implementation plan (slices)

| Slice | What | Tests | Live two-node check |
|---|---|---|---|
| S1 | Read-only capability probe: `music` rules section, proof + firewall, `MUSIC_CAPABILITIES_*`, filtered report, peer merges it under `remote:<node>` and the form shows it (disabled Create) | refused when off / unproved / not allowed; report carries no path, no library, no voice adapter by default; merge on Linux and Windows | the peer sees the owner's engines and their status |
| S2 | One job end to end: request, approval card or standing, owner job with requester `peer`, owner-first pick, FLAC by file transfer, withheld record, `register_generated` on the peer, ledger pair | privacy check on the sent record, licence intact, idempotent `request_id`, owner library untouched, quota of 1 concurrent | one short instrumental generated on the owner's card plays in the peer's library with its licence shown |
| S3 | Progress and cancel: `MUSIC_JOB_STATUS` pushes, peer and owner cancel, peer-gone guard, result hold and fetch after reconnect, restart resume | each row of the D7 table with a fake peer and a fake engine | peer cancels while waiting; peer drops while running and fetches after reconnect |
| S4 | Quotas and UI: jobs/day, GPU seconds/day from the ledger, owner settings page, peer job marked in the owner's queue | counts from ledger rows; refusal not recorded as a row | owner sets a quota of 1/day and the second job is refused with the reason |
| later | Source-audio types (cover, edit) with audio carried in; ADR-047 pull for the take | — | — |

Each slice runs the full core suite before it is called green.

## Open Questions

All **Open (owner decides)**:

- **Q1. Quota numbers:** jobs per day, GPU seconds per day, concurrent jobs per peer
  (proposed 1), `peer_wait_s`, `peer_result_hold_h` (proposed 24 h).
- **Q2. Approval default:** per job for every peer, or standing for the owner's own node
  group (proposed) and per job for others.
- **Q3. Copies:** may the owner keep or audition a peer's take (proposed: no copy beyond
  the hold)?
- **Q4. Economy:** charge or credit peer music jobs; if so, per job or per GPU second, and
  which ledger columns (the token tariff does not fit).
- **Q5. Adapters:** may a peer use the owner's adapters? Proposed: voice adapters never by
  default; purpose adapters (instrumental) yes when the engine is allowed.
- **Q6. ADR-051 D17 wording:** amend "They block anything published or sold" to the
  warning reading of 2026-11-27, in ADR-051 itself.

## Authors

- **The owner** — Decision (pending)
- **CC** — Analysis, Draft

## References

- [ADR-051](051-the-music-player-is-a-dpc-window-over-one-core-music-service.md) — the
  music service, D17 licence, D30 loopback, D43 agent turns
- [ADR-052](052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md)
  — holder names `052:458-459`, peer refusal `:503-504`, waits `:528-529`
- [ADR-053](053-who-asked-decides-what-an-agent-may-do.md) — who asked, owner approval card
- [ADR-041](041-openai-compatible-gateway-as-a-dpc-surface.md) — D2 proved key (`041:279`),
  D3 ledger (`:317`), D5 quota (`:1159`), D7 not shared onward (`:1608`)
- [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md) — D4-0: the
  alias a peer names is evidence, not an instruction
- [ADR-031](031-participant-roles-llm-payload.md) — per-reader roles
- [ADR-047](047-multi-source-file-transfer-and-peer-file-search.md) — large-file transfer
- [ADR-048](048-memory-layer-audience.md) — the room decides what may be said
- `backlog.md:72` — the card this ADR answers
