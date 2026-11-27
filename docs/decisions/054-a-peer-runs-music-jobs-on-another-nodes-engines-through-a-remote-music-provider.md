---
adr: 054
title: "A peer runs music jobs on another node's engines through a remote music provider"
status: proposed
date: 2026-11-27
axis: network, collective
deciders: [owner]
consulted: [CC, Zcode, Anti]
informed: []
depends_on: [ADR-051, ADR-052, ADR-053]
related: [ADR-031, ADR-040, ADR-041, ADR-047, ADR-048]
supersedes: []
session: "AI Studio group chat, 2026-11-27, #72 (card A-PEER-NODES-MUSIC-PLAYER-CANNOT-USE-THE-MUSIC-ENGINES-SET-UP-ON-ANOTHER-NODE-BECAUSE-MUSIC-HAS-NO-REMOTE-PROVIDER), #86-#103"
---

# ADR-054 — A peer runs music jobs on another node's engines

> **Proposed, 2026-11-27. Draft by CC.** History: first draft `8166e03b` (a job-level door),
> revised `96dee7bb` to an engine-level door on the owner's word (#86, #92), rewritten after
> review (Zcode #96, Anti #103) with the owner's scope (#98, #99, #101, #102). Every claim
> about existing code carries a `file:line` read at `dev` HEAD `e184b49b` (paths under
> `dpc-client/core/dpc_client_core/` unless stated); anything not read is marked
> **Assumption**; every new name is a proposal. What the owner has decided is cited by
> message number; what is still his is marked **Open (owner decides)** in
> [Open Questions](#open-questions).

## Context and Problem Statement

The music models are local by construction. acestep.cpp and yue2.cpp are started and
watched by the core and bound to loopback (`music/engine_base.py:40`; `music/yue2.py:777`
passes `--host LOOPBACK`); a job reaches them only through the core's job store and runner,
which holds the engine's card lease for the job (`music/runner.py:851`, holder
`music job <id>`). The describer and the QA judge are audio llama-server aliases on the same
node (`music/commands.py:1147-1161`, `music/qc_commands.py:75-78`), and lyrics transcription
uses the voice provider (`music/commands.py:506-520`).

**The owner's goal, in his words (#98):** "there is our Linux node, it has the music player
but no models and no hardware; I want that node to do everything in its own music player
that I do on my node, by sharing my models and their add-ons." And (#86): "my node just
provides remote inference, as for text LLMs."

Two compute kinds already cross nodes; music has neither. **Text:** a guest's
`remote_peer` provider (`providers/remote_peer_provider.py:145`) calls a host whose door
checks the proved key first (`p2p_coordinator.py:883`, ADR-041 D2), then the firewall's
`compute` allow-lists (`p2p_coordinator.py:890`, section at `firewall.py:1762`), and writes
one ledger row per served call (`p2p_coordinator.py:795`). **Voice:** a
`remote:<node_id>:<alias>` provider sends audio to a peer's Whisper (`service.py:4916`); the
host checks proof (`p2p_coordinator.py:1374`, since `49c78e3d`), then
`can_request_transcription` (`:1379`), and runs inside `serving_a_peer` (`:1435`). Today a
node without models can only ask the owner's agent in a group (ADR-053), which gives it no
job card, progress, provenance record or library entry of its own.

## Decision Drivers

- **One card, owner first.** The owner's GPU is one card shared through the lease (ADR-052);
  a peer stands behind every owner waiter (`managers/card_lease.py:779-784`).
- **Consent is the owner's, per peer and per model.** Every remote door is off by default
  with node and group allow-lists (`firewall.py:1762-1793`).
- **Nothing private leaves either node; the owner keeps nothing** (#86).
- **Licences travel, they do not block** (ADR-051 D17 as amended in `e54c6b74`, #90).
- **Reuse:** the runner's engine seam (`submit`/`submit_form`/`job_status`/`fetch_result`/
  `cancel`, `music/engine_base.py:956-1056`), the voice `remote:` provider, provenance.
- **Cross-platform.** The peer may be Linux or macOS with no GPU; the owner any OS.

## Prior art outside our engine

- **(a) The project's own reading.** `rg --no-ignore -l -i "music|petals|swarm|offload|remote
  gpu|job queue|consent" ideas/dpc-research/articles` (2026-11-27): MoE offload and agent
  papers only; **nothing applicable found.** Ruach Studio notes (ai-studio
  `experiments/music-player/docs/RUACH-BORROW.md`, grep `remote|peer|network|queue`): line 59,
  an ETA that is `None` when unknown, never 0. **Take:** a remote call's ETA is `None` until
  measured.
- **(b) The topic elsewhere** (from CC's knowledge, **not fetched this session**):
  - AI Horde (https://aihorde.net): submit, poll, fetch; workers choose the models they
    serve; credit orders the queue. **Take:** submit/status/fetch and a ledger hook for
    credit. **Not taken:** a public pool of strangers.
  - ComfyUI HTTP API (https://github.com/comfyanonymous/ComfyUI): an id per prompt, pushed
    progress, outputs fetched separately. **Take:** result fetched apart from the request.
  - Blender Flamenco (https://flamenco.blender.org): a manager owns the queue, workers only
    run. **Take, inverted from the first draft:** the peer's job store is the manager; the
    owner's node is a worker that runs one typed step and forgets it.
  - BOINC (https://boinc.berkeley.edu): redundant computation against untrusted hosts.
    **Not taken:** hosts here are allow-listed and proved.

## Decision

**The peer runs every music job on its own node; each model-backed step of that job is one
call to the owner's node, which runs it on its own models and returns the output and its
identity, and keeps nothing.** Decided by the owner (#101: "YES" to this plan and its four
building blocks: a firewall `music` section, the owner's door, the card-queue change, and an
"engine on node X" choice in the peer's Create panel). The queue rule itself is **not**
decided (#102, Q7).

### D1. Shape: the peer is the job, the owner is a remote model

- **Peer side.** The peer's job store, runner, parameters, lyrics, seeds, post-processing
  (WAV to FLAC), QA stage 1, tags, provenance record, library entry and export are its own,
  unchanged. The Create panel offers "engine on node X"; the job keeps `params.engine` =
  `yue2`/`acestep` and gains a separate field `compute: {kind: "remote_node", node_id}`
  (proposed name). `remote:<node>` must never go into `params.engine`: `engine_of` maps any
  unknown word to acestep silently (`music/job_schemas.py:473-479`) while
  `normalize_params_from_text` refuses it (`:590-593`). Where the runner calls a local
  engine it calls a `RemoteMusicEngine` client offering the same seam.
- **Owner side.** A music door in `p2p_coordinator.py` beside the inference and
  transcription doors. It serves **typed calls**, never forwarded engine bodies (D3), runs
  them on its loopback engines or aliases, and returns the output as a file plus a passport.
  The engine HTTP port is never tunnelled.
- **Wire (proposed, a DPTP minor bump):** `MUSIC_CAPABILITIES_REQUEST/RESPONSE`,
  `MUSIC_CALL_REQUEST` (kind, typed params, seed, adapter names, input-media refs,
  `request_id` minted by the peer), `MUSIC_CALL_STATUS` (pushed and on request),
  `MUSIC_CALL_CANCEL`, `MUSIC_CALL_RESULT` (passport + output-media ref), `MUSIC_MEDIA_*`
  (the media channel, D3) and `MUSIC_CALL_ACK` (the peer has the output).

**Every model-backed step of the player, and how the peer reaches it** (owner #98, #101):

| Step | Owner-side model | Peer call site today | How the peer calls it | Returned |
|---|---|---|---|---|
| Generate on acestep.cpp (text, lyrics) | acestep.cpp `/lm` then `/synth` | `runner._produce` (`music/runner.py:1064`, `/lm` `:1074`, `/synth` `:1098`) | one music call `acestep.generate`: the owner runs `/lm` and `/synth` back to back, so it never takes an LM answer from the peer | wav24 per variant; LM answer (`audio_codes`, `lm_seed`) in the passport |
| Generate on yue2.cpp | yue2.cpp `/synth`, one per variant (batch 1, `music/yue2.py:51-53`, `:117`) | `runner._produce_yue2` (`:1193`, `/synth` `:1220`) | one call `yue2.synth` per variant | wav24 + replay (`semantic_tokens`, abc, style) |
| Cover on yue2.cpp | SheetSage2 = yue2.cpp `/transcribe`, then `/synth` | `similar._transcribe` (`music/similar.py:675`, `:688`), `_cover_on_yue2` (`:877`) | `yue2.transcribe` (source audio in), then `yue2.synth` | ABC score; then wav24 + replay |
| Cover on acestep.cpp | `/synth` with source audio | `similar._cover_on_acestep` (`:609`, `:623`) | `acestep.cover` (source audio in) | wav24 + LM answer |
| Same style | yue2.cpp `/tokenize`, the describer, `/synth` | `similar._tokenize` (`:1039`, `:1048`), `_same_style_on_yue2` (`:1112`) | `yue2.tokenize` (audio in), `describe`, `yue2.synth` | codes; description; wav24 |
| Region edit (regenerate, fix seam) | acestep.cpp repaint `/synth` with a control WAV | `runner._edit_on_the_card` (`:1494`, `:1508`) | `acestep.repaint` (control audio in) | **wav32** takes (`music/region_edit.py:103`) |
| Describe a source | audio llama-server alias | `commands._music_describer` (`music/commands.py:1147`), `describe.describe_audio` (`music/describe.py:623`) | music call `describe`; the alias is the owner's own setting, never named by the peer (ADR-040 D4-0) | description JSON |
| QA judge (stage 2), if the owner has one | audio llama-server alias, `[music] qc_judge_alias` (`music/capabilities.py:152`) | `qc.judge_take` (`music/qc.py:606`) | music call `judge` | judge answer |
| Lyrics transcribe | Whisper | `_music_pick_transcriber` passes a `remote:` voice choice through (`music/commands.py:515-516`) | **the existing voice `remote:<node>:<alias>` provider** (`service.py:4916`), not duplicated | text only today (`dpc_protocol/protocol.py:392-418`): see below |
| Lyrics align / auto-align | Whisper word times | local Whisper only (`music/commands.py:650-660`, `:701-703`) | same voice provider, once it returns segment times | segment timestamps |
| Add-ons (yue2 adapters) | adapter splits on the owner | named in job params | names in the typed call, checked against the owner's grant (D4) | listed in the passport |

Two gaps on the voice path are in scope: the transcription response carries text only
(`protocol.py:392-418`), and align refuses a remote provider by design
(`music/commands.py:650-660`). Proposed: the response gains optional segment times (a DPTP
minor field), and align accepts a `remote:` provider the person chose explicitly. The peer
sends a 16 kHz mono downmix (**Assumption**: Whisper resamples to 16 kHz itself, so nothing
is lost; 6 min is ~11.5 MB as 16-bit WAV, less as FLAC), which keeps it inside the inline
frame (`MAX_FRAME_BYTES` 64 MiB, `protocol.py:665`). The voice door's 120 s rule applies to
it unchanged (D2).

**Not model-backed, already local on any OS without a GPU:** the UI, library, playlists,
comments, metadata editing, export (`music/export.py`), peaks (`music/peaks.py`), QA stage 1
and loudness (ffmpeg, `music/qa.py`, `music/loudness.py`), the LRCLIB lookup, FLAC packing.

**The capability report the peer merges** shows, per step above, whether the owner shares
it: `shared`, `not_shared` or `unavailable`, with the engine status words of
`music/capabilities.py:14-19` and nothing else (D3 whitelist).

### D2. Consent, limits and the card queue

- **A `music` section in `privacy_rules.json`, off by default**, in the shape of `compute`
  and `transcription` (`firewall.py:1762-1793`): `enabled: false`, `allow_nodes`,
  `allow_groups`, `steps` (the D1 rows the owner shares; empty = none), `adapters` (D4). A
  `can_request_music(peer, step, adapters)` check in the shape of `can_request_transcription`
  (`firewall.py:2308`).
- **Identity first, on every music command** (capabilities, call, status, cancel, media,
  ack): `_refuse_unproved_peer` (`p2p_coordinator.py:798`) is the first line, the order the
  inference door (`:883` then `:890`) and, since `49c78e3d`, the transcription door (`:1374`
  then `:1379`) use. The capability report is not built on `handle_get_providers_request`
  (`p2p_coordinator.py:1470-1489`), which has no proof check today (`service.py:3662`
  `menu_for_peer` reads only the firewall).
- **Consent is the allow-list, as for text** (recommended, **Open, Q2**): enabled + node or
  group + shared steps and adapters IS the consent; no per-call approval card by default. If
  ever an option, only in the owner's own UI, never in a chat, and with its own mechanism:
  the ADR-053 card lives 60 s and allows 2 pending per process, shared with agent cards
  (`dpc_agent/owner_approval.py:44`, `:49`).
- **Limits** (recommended, **Open, Q1**): no daily quotas by default; one concurrent call
  per peer (this also makes an in-flight reservation trivial, D6); `[music] peer_wait_s` =
  1800 per call; result hold `[music] peer_result_hold_h` = 24.
- **Not shared onward** (ADR-041 D7, `041:1608`): the door serves only local models; a node
  never re-serves a `remote:` model it reaches itself.

**The card queue: the problem** (Zcode #96 P1, Anti #103 item 1; verified):

1. *The lease refuses a long guest outright.* `PEER_TRIP_S = 120.0`
   (`managers/card_lease.py:83`). A waiter marked `serving_a_peer` gets
   `wait_s = max(0, 120 - expected_s)` (`:749-760`) and is refused at once, free card or
   not, when `expected_s > 120` (`:1468-1474`) or unknown (`:1476-1486`). The 120 s trip
   is the owner's own rule, "a rule, not only arithmetic" (`card_lease.py:80-82`, Mike #17,
   2026-11-23), so only the owner can change it (Johnny #111). It bites twice: `expected_s`
   is **unknown** for every acestep.cpp call (`music/job_schemas.py:526-528`: "acestep.cpp
   has no measured factor: unknown") and for every edit (`music/runner.py` region path
   passes `expected_s=None`), so those are refused even on an idle card; and YuE2 at the
   form's defaults exceeds it (2 variants of 360 s give about 164 s by
   `YUE2_OVERHEAD_S`/`YUE2_AUDIO_S_PER_WALL_S`, `job_schemas.py:507-508`). A `/synth` takes
   minutes, so every music call is refused. Without the peer mark the call loses
   owner-first ordering (`_goes_before`, `:779-784`).
2. *The lease does not serialize two music users of one engine.* `CardLease.use` is
   re-entrant: a granted lease opens one more use (`managers/card_lease.py:606-609`), so a
   door call under `music:yue2` would be admitted beside the runner's job.
3. *A different backbone or adapter set restarts yue2.cpp.* `Yue2Server.start` stops a
   running server to swap (`music/yue2.py:891-920`, `stop("swap")` at `:920`) and leaves the
   busy check to its caller (docstring `:896-897`). A peer call wanting another adapter set
   could kill the owner's generation mid-way.
4. *A peer job is N sequential calls*; owner jobs slot in between; the engine's idle stop
   (300 s, `music/yue2.py:116`, `music/acestep.py:93`) may restart it between calls (correct:
   calls are stateless and seeded; slow).

So one **music-compute line with owner priority**, shared by `MusicJobRunner` and the door,
is required whatever the option; it orders calls and jobs and owns engine swaps. Options:

- **Q7-A. A long-guest class in the lease.** The door passes its own bound (`peer_wait_s`)
  instead of `PEER_TRIP_S` and keeps the peer mark, so `_goes_before` still puts owners first.
  Good: smallest change (ADR-052 amendment to L13). Bad: fixes only item 1; a granted call
  holds the card for minutes while an owner job waits; fairness and swaps need more.
- **Q7-B. A music scheduler above the lease** (recommended). The runner owns one queue of
  owner jobs and peer calls; peer calls are booked with an ETA; owner jobs go between calls,
  never preempting a running call; anti-starvation: a peer's next call keeps its place for
  15 min after its previous one ends. Swaps happen only between items. It takes the lease
  under Q7-A's long-guest class, so Whisper and LLM clients still see owner first. Good:
  one place for items 1-4, an honest ETA. Bad: the largest build; runner changes.
- **Q7-C. Idle-only guests.** Peer calls run only when no owner job is queued or running;
  a new owner job cancels the running call at once and the peer retries. Good: the owner
  never waits. Bad: lost GPU minutes; on a busy owner a peer may never finish; a cancelled
  `/synth` loses all its work.

**Open (owner decides), Q7.** The owner (#102): "we will need to think about the queue and
the scheduler". Before A/B/C there is a question only the owner can answer (Johnny #111):
**Q7.0 — what makes a long guest call acceptable**, given his 120 s rule and that acestep.cpp
and edits have no duration estimate at all. **Decided 2026-11-27 (the owner, #115):** music is
a **separate guest class** — the 120 s trip stays for transcription and text; a music call is
admitted by **the load of the owner's own queue** (how much owner work is queued or running
on the card), not by a fixed trip; and the owner gets **music-class quotas for peers in the
settings** (the Q1 limits become real settings, not hidden constants), kept **in the
firewall settings** (#117): the `music` section of `privacy_rules.json`, beside its
allow-lists, as `vendor_quotas` sit in `compute` today. A measured estimate
for acestep and edits (a fit like YuE2's) is still wanted, for an honest ETA, but it does not
gate admission. **Admission rule, decided (#118 proposed, #119 "yes, a logical rule"):**
(1) admit a music call at once when no owner job is queued and the running one (if any) ends
within N minutes (proposed N = 5, a setting); (2) otherwise the call waits in line behind
every owner job, no longer than the peer's wait limit from the firewall `music` section;
(3) if the line would hold it longer than that limit, refuse with the honest reason "the
owner's GPU is busy" (no alias, chat or holder named). The choice among Q7-A/B/C below
(the scheduler that enforces this): **the owner chose (#122-#124, 2026-11-27) that CardLease
itself is the scheduler, with no second queue above it** ("if I am generating something, it
must take a place in CardLease, the lease for all GPU clients"). So: (a) a **music guest
class** in CardLease, exempt from the 120 s trip and admitted by the load rule above; (b)
CardLease **serializes music users of one engine**: a second use under the same holder that
needs another backbone or adapter set waits until the first use ends, so an engine is never
swapped mid-generation (today `use()` re-enters under `music:yue2`); (c) the anti-starvation
budget and the ETA live in CardLease too, one place for every client. This is Q7-A carried
to Q7-B's rules; the owner asked Zcode, Ark and Johnny for comments (#124) before it is
final. S1 does not depend on Q7;
S2-S4 do (an amendment to ADR-052 L13 adding the music guest class).
- **The peer mark must cover the whole trip** (Johnny #111): the mark is a context variable
  set inside `with serving_a_peer(...)` (`managers/llama_server_supervisor.py:228-243`); the
  output transfer, the peer's ACK and the owner's delete happen after the engine call, and
  must run under the same mark so `queue_snapshot()` (`for_a_peer`) and the owner's UI show
  the guest on both halves. S1 test: read `for_a_peer` during the call and during the
  transfer.

### D3. What travels each way

- **In: typed calls only** (Anti #103 item 2). The peer never sends an `/lm` or `/synth`
  body: acestep bodies name `lm_model`, `synth_model`, `vae`, `inference_steps` and
  `synth_batch_size` (`music/runner.py:318-325`, `:370-379`), and yue-server loads every
  split in the adapter folder (`music/yue2.py:778-779`), voice adapters included, and is
  unsafe at batch > 1 on CUDA (`:51-53`). The call carries the job's typed params, checked by
  the owner with the same `normalize_params_*`, `check_params` and `unsupported_reason`
  (`music/capabilities.py:467`, `:532`), plus the seed and variant; the owner rebuilds the
  engine request with its own builders, fixing model files, steps, batch and output format.
- **Media in both directions: a dedicated non-chat channel** (Anti #103 item 3). The chat
  file transfer does not fit: it is firewall-gated with "ask" by default for a proved
  connection and per-entry size caps of 50-500 MB in the default rules (`firewall.py:166`,
  `:1875-1887`), allows 3 concurrent transfers (`managers/file_transfer_manager.py:754`),
  writes "Sent/Received file" lines into the 1:1 history (`:2587`,
  `message_handlers/file_complete_handler.py:169`, `:189`), has no resume (`:24`) and no
  delete after the receiver's ACK. The music channel: chunked, hash-checked, never in chat
  history, ACK-ed explicitly, used for input audio (cover/same-style source, edit control)
  and for outputs. Lossless compression in transit is allowed (FLAC; the peer checks samples
  against STREAMINFO MD5 and the passport's hash of the engine's original bytes).
- **Sizes, from the code's rates:** acestep.cpp works at 48 kHz stereo
  (`music/region_edit.py:94`): wav24 is 288 000 B/s, so 3 min ~52 MB, 4 min ~69 MB, 6 min
  ~104 MB per variant; edit wav32 is 384 000 B/s (6 min ~138 MB). yue2.cpp's output rate is
  not stated in code (**Assumption**: 44.1 or 48 kHz; the runner records it per take,
  `music/runner.py:1863`). The reviewer figure of 104-198 MB for 3-6 min matches one wav24
  variant only at 6 min, or two variants. Each output is held and can be fetched again
  (D5), which stands in for resume.
- **Out: the passport**, built from a whitelist, then `check_privacy`
  (`music/provenance.py:265`) on the serialized passport; one that fails is not sent and the
  call fails `privacy_check` naming the kind, never the match. Fields: engine id
  (`yue2.cpp`/`acestep.cpp`), build and binary identity, backbone choice, weights and
  adapters used with source and halves sha256 (from `identity()`, names and hashes, no paths:
  `music/yue2.py:945-949`, `music/engine_base.py:1057-1066`), device identity
  (`music/provenance.py:370`), output format, rate, channels, bits, duration, the hash of the
  engine's bytes (`latent_sha256`, as `music/runner.py:1864` computes it), and the replay
  material the record builders need: acestep `lm_request`, `lm_result` (`audio_codes`,
  `lm_seed`), `synth_request` as sent (`music/provenance.py:543-547`); yue2 `sent`, `replay`,
  `lm_seed`, `backbone` (`:604-606`); without them `genome_sha256` is `not_stored`
  (`:753-768`). **Never sent:** `form_info()` (it carries `adapters_dir` as a path and
  reasons like "<path> does not exist", `music/yue2.py:757`, `:762`, merged into each engine
  of the report at `music/capabilities.py:589`) or `engine_base.status()` (PID, port,
  `card_wait`, `music/engine_base.py:442-470`).
- **What the peer sees of the owner:** per step shared or not, engine status words, allowed
  adapter names and the voice-adapter note; never the owner's library, other jobs, queue
  contents or who holds the card.

### D4. Which adapters (decided, #89)

- **The owner grants each adapter** through a "Share to peers" option in the music player,
  per adapter, mirrored in `music.adapters`. Nothing is shared until ticked.
- **Voice adapters are unticked by default** and show their note, "for local listening
  only" (`music/yue2.py:147-148`).
- **Purpose adapters are a separate row.** An Instrumental job on YuE2 gets the
  instrumental adapter by rule, or moves to acestep.cpp when it is not offered
  (`music/capabilities.py:486-500`); "Share to peers" lists it as a purpose adapter, so an
  unshared one shows the peer why its instrumental went to acestep.cpp, never silently.

### D5. Nothing kept on the owner, and the sweep (decided in principle, #86)

- No job record, no take, no provenance, no library entry on the owner. The runner's
  `write_payload` (`music/jobs.py:940`), which keeps each engine body on a job, is not used.
- Input media and outputs live in one door-owned folder. An input is deleted when its call
  ends; an output when the peer ACKs it, or after `peer_result_hold_h` (24 h) if the peer is
  offline. **The door owns the sweep**: at startup and on a timer, it removes what is past
  its hold or belongs to no call.
- The door logs no lyrics, prompts or params, even at DEBUG. The engines' own logs on the
  owner's disk are at the trust level of text prompts served to a peer, which the core
  "shows, stores and logs none of" (`firewall.py:1763`, ADR-041 D7 amendment 2026-09-14);
  an engine log may still hold request text (**Assumption**, not read in engine code).
- What the door does keep, for idempotency (D7): `request_id`, peer id, call kind, state,
  output file name and hash, timestamps; no content; removed with the output.

### D6. Ledger: one row per call

One served row per **call** on the owner (`caller_kind` `peer`, `route` `local`, alias such
as `music:yue2.synth`, tokens null, `duration_s` the call's card time) and one consumed row
on the peer (`route` `peer`), from the existing vocabularies (`node_ledger.py:84-85`).
`COUNTS_SOURCES` (`:86`) and `BILLINGS` (`:124`) are strict and need a value for a non-token
GPU call. `count_since` counts rows only (`:807-832`), so GPU-seconds need a summing method.
Quotas read finished calls only (`p2p_coordinator.py:966-970`), so a quota, if ever set, needs
an in-flight reservation; one concurrent call per peer gives it for free.

### D7. Status vocabulary, failure, cancel and idempotency

Statuses the peer sees (proposed): `off` (music sharing disabled), `not_allowed` (the same
answer whether the model exists or not: no existence oracle), `busy` (with an ETA or `None`;
no alias, chat or holder named, as `LeaseRefusedForPeer`, `managers/card_lease.py:148-150`),
`waiting`, `running` (progress), `done` (output held until a time), `failed` with a class
(`not_shared`, `invalid_params`, `engine_failed`, `timed_out`, `lease_refused`,
`privacy_check`, `owner_restarted`, `cancelled_by_owner`), `cancelled`.

| Event | Owner side | Peer's job card |
|---|---|---|
| Step not shared / model missing | `not_allowed` | the form never offers it; "not shared by node X" |
| Card busy | call waits in the music line (Q7) | "waiting for node X's GPU", ETA or unknown |
| Peer gone while waiting | the guard drops the call, no card time (as `p2p_coordinator.py:1430-1439`) | job resumes the call on reconnect |
| Peer gone while running | call finishes; output held 24 h | asks status on reconnect, fetches |
| Transfer drops at 90 % | output still held | fetches again from the start |
| Owner cancels | Stop/Cancel in the owner's UI | `failed: cancelled_by_owner` |
| Peer cancels | `MUSIC_CALL_CANCEL`, own calls only; engine job cancelled | `cancelled` |
| Owner restarts mid-call | the call is `failed: owner_restarted`, never rerun; a `done` output survives and is served | retries as a new call, or fetches |
| Same `request_id` again | returns the existing call's status or output; never runs twice | — |

**Idempotency** (Zcode #96 P2): the inference door's guard is in-flight only
(`p2p_coordinator.py:1061-1087`, released at `:1305`). A music call takes minutes, so the
door keeps `request_id` -> call persistently for the call's life plus the hold (D5), so an
owner restart never spends the GPU twice.

### D8. Privacy, provenance on the peer, and the agent path

- The door reads no conversation, memory or library, so rule #19 has nothing to leak
  through it.
- **The peer's record**: built by the peer's own builders from the passport, with
  `computed_on: {kind: "remote_node", node_id}`; `_withhold_for_export`
  (`music/provenance.py:433`) learns that field. `licence_record` must receive the real
  engine id from the passport (`yue2.cpp`/`acestep.cpp`, `music/provenance.py:153`,
  `:170-191`), never `remote:<node>`: an unknown engine gives `non_commercial: null` instead
  of `true` for stock YuE2. A remote take carries the same licence warning as a local one.
- **The peer computes the licence from its own table, never from the owner's word**
  (Johnny #111): the passport carries names (engine, backbone, weights, adapters); the peer
  maps them through its own `licence_record` constants (`music/provenance.py:146-155`) and
  stores what the owner reported as `asserted_by: {node_id, reported_licences}` beside it.
  A name the peer does not know is `unknown` (so `non_commercial: null`, shown as "licence
  unknown"), never `mit`. Test: a passport naming a backbone the peer does not know gives
  `unknown`/`null`, not `false`.
- **The agent path stays** (ADR-053): a group member asks the owner's agent and the take is
  posted to the room. This door is player to player with no agent and no chat; their limits
  are counted separately.

### D9. Cross-platform

- **Peer:** no engine, binary or GPU. It needs ffmpeg, as today, for FLAC, loudness and QA
  stage 1 (`music/runner.py:517`, `music/qa.py`). Windows 10-11, Linux and macOS alike; no
  path crosses the wire.
- **Owner:** any OS whose report shows a model available; the probe, not the OS, decides.
  On macOS the lease has no VRAM reading (`052:943`); the queue rule is unchanged.
- **Tests:** local runs are Windows, CI is ubuntu; every slice uses a fake peer and a fake
  engine and passes on both; each slice names a live two-node check (owner on Windows with
  models, peer on Linux without).

### Rationale

The owner wants remote inference, as for text: the guest builds the request and keeps the
result, the host spends its card and forgets. At the engine level the peer's player stays the
one place a job lives, so its job card, provenance, library and export work unchanged, and the
owner's side is small: a door, a typed request builder, a media channel and a sweep. Typed
calls instead of bodies keep the owner's model files, adapters and batch rules the owner's.
The queue is the one part that cannot be lifted from text, because a call holds the card for
minutes; it is left to the owner (Q7).

## Considered Options

- **A. Tunnel the engine HTTP port to the peer.**
- **B. A job-level door** (the first draft, `8166e03b`): the owner runs the whole job
  through its own `propose_music_job`, the peer holds a proxy job, the owner builds a
  withheld provenance record and returns a FLAC.
- **C. An engine-level door with typed calls (chosen, #86, #92, #101).**
- **D. Keep the agent path only.**

### Pros and Cons of the Options

#### Option A
- Good: smallest wire; the peer's runner already speaks the engine API.
- Bad: breaks loopback-only (`music/engine_base.py:40`); no consent point; the peer could
  name any model file or adapter and any batch size (D3).

#### Option B
- Good: the owner's store, lease and provenance would cover a peer's job.
- Bad: rejected by the owner: "my node just provides remote inference, as for text LLMs"
  (#86); "if we provide the engine and the peer sets everything up on its own side, why a
  FLAC?" (#92). The owner would hold job records and takes it does not want, and the peer's
  library would hold a record built elsewhere.

#### Option C
- Good: the peer's player is unchanged; the owner keeps nothing; the same pattern as text.
- Bad: a new door, media channel and passport; a job becomes N calls, so the queue needs
  work (Q7).

#### Option D
- Good: nothing to build.
- Bad: no job card, progress, record or library entry on the peer; the owner answers every
  request at the keyboard.

## Consequences

- **Positive:** a node without models does everything in its own player on the owner's
  shared models (#98); every take keeps its licence and lineage wherever it is computed.
- **Negative:** a DPTP command family, a privacy section, a media channel and a sweep to
  maintain; peer calls lengthen the owner's queue; the lease gains a long-guest class
  (ADR-052 amendment).
- **Neutral:** the music door follows the proof-first order the inference door and, since
  `49c78e3d`, the transcription door use; `handle_get_providers_request` still has no proof
  check (`p2p_coordinator.py:1470`) and is not built on (a card for it is suggested, not
  filed by this ADR). The agent path is unchanged.

## Confirmation

- [ ] With `music.enabled` false, every music command from a peer is refused, and no call,
      file or ledger row exists.
- [ ] An unproved peer is refused before any step or model is named, on every music command.
- [ ] `not_allowed` is the same for a missing model and an unshared one.
- [ ] No engine body from the peer reaches an engine; a call naming a model file, batch size
      or unshared adapter is refused.
- [ ] A running owner generation is never stopped by a peer call (no swap while busy).
- [ ] An owner job queued after a peer call runs before that peer's next call; a peer job of
      N calls still finishes (anti-starvation, per Q7).
- [ ] The passport and the capability report carry no path (S1 test extended), no PID, port
      or holder, and pass `check_privacy`.
- [ ] The peer's record names `yue2.cpp`/`acestep.cpp` and its licence equals a local take's.
- [ ] After ACK, or the hold, the owner's door folder is empty; a restart sweeps leftovers.
- [ ] A repeated `request_id` never runs the model twice, across an owner restart.
- [ ] No chat history line is written for any music media transfer.

## Scope

- `dpc-protocol/dpc_protocol/protocol.py`: the music command builders; optional segment
  times on the transcription response; DPTP doc bump.
- `firewall.py`: the `music` section (`steps`, `adapters`) and `can_request_music`.
- `p2p_coordinator.py`: the music door (proof first, firewall, typed rebuild, status,
  cancel, idempotency store, media, ack) and its sweep (startup + timer).
- `managers/card_lease.py` and ADR-052: **amendment** for a long-guest class (Q7-A, also used
  by Q7-B).
- `music/runner.py`, `music/engine_base.py`: the music-compute line shared with the door
  (Q7); the `RemoteMusicEngine` client on the peer.
- `music/yue2.py`: no swap while busy, enforced inside the music line.
- `music/capabilities.py`: the per-step shared report for a peer (whitelist); the peer
  merges `remote_node` sources.
- `music/job_schemas.py`: the `compute` field; `params.engine` unchanged.
- `music/provenance.py`: `computed_on`, its withholding, passport-driven engine id and
  licence on the peer.
- `music/commands.py`: align accepts an explicitly chosen `remote:` voice provider.
- `node_ledger.py`: vocabulary values for a non-token GPU call; a GPU-seconds sum.
- `ui/src/lib/components/music/`: "engine on node X" in Create; "Share to peers" per step
  and adapter; status words on the peer's job card.

## Implementation plan (slices)

> **Q1, Q2 and Q7 are not decided by the owner; every number below (`peer_wait_s` 1800, the
> 24 h hold, the 15 min budget, one concurrent call) is a draft recommendation, not a
> decision** (Johnny #111).

| Slice | What | Tests | Live two-node check |
|---|---|---|---|
| S1 (before Q7) | Capability probe: `music` section, proof + firewall on `MUSIC_CAPABILITIES_*`, per-step shared report from a whitelist, passport whitelist builder, peer merges it and Create shows "engine on node X" (Create disabled) | refused when off / unproved / not allowed, same answer for missing and unshared; report and passport carry no path, PID, port or holder; voice adapters unticked; merge on Linux and Windows | the peer sees which steps node X shares |
| S2 (after Q7) | One call end to end on the chosen queue: `yue2.synth` typed call, owner rebuild, music line with owner priority, media channel out, passport, peer record with `computed_on` and passport licence, ledger pair | no body forwarded; no swap while busy; licence equals local; owner folder empty after ACK | one short instrumental made on node X plays in the peer's library with its licence shown |
| S3 | Status, cancel, idempotency, hold, re-fetch; every other D1 row (acestep generate, cover, same style, repaint wav32, describe, judge); media in | each D7 row with a fake peer and engine; "drop at 90 %, re-fetch"; restart mid-call never reruns | the peer drops while running and fetches after reconnect |
| S4 | "Share to peers" UI per step and adapter, anti-starvation budget, ledger per call with GPU-seconds, Whisper segment times and remote align | budget keeps a peer's place; rows per call; align on a remote provider gives synced lyrics | the peer runs a cover and aligns its lyrics, all on node X |

Each slice runs the full core suite before it is called green.

## Open Questions

- **Q1. Limits (quotas): partly decided (#115): they are settings the owner sets for the music
  class of peers.** Open: the defaults. Recommended defaults: no daily quotas by default; one
  concurrent call per peer; `peer_wait_s` 1800 per call; result hold 24 h; the
  anti-starvation budget (a peer's next call keeps its place for 15 min).
- **Q2. Consent (approval): Open (owner decides), recommended "as for text inference"** (CC #88,
  Zcode #96): the allow-list is the consent; no per-call approval card by default; if ever
  an option, only in the owner's UI with its own mechanism.
- ~~Q3. Copies~~ **Decided (#86):** no copy, no audition.
- **Q4. Economy: Open (owner decides), recommended: no tariff now.** Record each call as
  `unpriced`, the path the peer ledger already has (`p2p_coordinator.py` `_record_peer_call`,
  Johnny #111); decide a price (per GPU-second, `duration_s` is in the row vocabulary) when
  a second node actually serves music. No tariff is a gift, as for text.
- ~~Q5. Adapters~~ **Decided (#89):** "Share to peers" per adapter plus `music.adapters`;
  voice adapters unticked with their note.
- ~~Q6. ADR-051 D17~~ **Decided (#90, `e54c6b74`):** a licence is a warning, never a block.
- **Q7. Queue and scheduler: Open (owner decides)** (#102). **Q7.0 decided (#115):** music is
  a separate guest class, admitted by the load of the owner's queue, with music-class peer
  quotas in the settings; the 120 s trip stays for transcription and text. Still open: the
  scheduler shape: **the owner chose CardLease as the one scheduler (#124)**, comments asked
  from Zcode, Ark, Johnny before final (D2). The
  load rule is decided (#119): admit at once if no owner job is queued and the running one
  ends within N minutes; else wait behind owner jobs up to the peer's limit; else refuse
  "owner's GPU is busy". S2-S4 wait for the scheduler choice.

## Authors

- **The owner**: Decision (direction #86, #92, #101; Q3, Q5, Q6 decided; Q1, Q2, Q4, Q7 open)
- **CC**: Analysis, Draft
- **Zcode, Anti**: Review (#96, #103)

## References

- [ADR-051](051-the-music-player-is-a-dpc-window-over-one-core-music-service.md): the music
  service, D17 licence (amended `e54c6b74`)
- [ADR-052](052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md):
  peer refusal `052:501-504`, peer waits and L13 `:529`
- [ADR-053](053-who-asked-decides-what-an-agent-may-do.md): who asked; the owner approval card
- [ADR-041](041-openai-compatible-gateway-as-a-dpc-surface.md): D2 proved key, D7 not shared
  onward (`041:1608`)
- [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md): D4-0, the alias
  a peer names is evidence, not an instruction
- [ADR-031](031-participant-roles-llm-payload.md), [ADR-047](047-multi-source-file-transfer-and-peer-file-search.md),
  [ADR-048](048-memory-layer-audience.md)
- `49c78e3d`: proof before the firewall on the transcription door
- `backlog.md:115`: the card this ADR answers
