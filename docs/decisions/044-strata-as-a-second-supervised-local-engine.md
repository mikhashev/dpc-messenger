---
adr: 044
title: "Run Strata as a second local engine, supervised by DPC as a child beside the llama-server child"
status: implemented
date: 2026-10-18
axis: network
deciders: [Mike]
consulted: [Ark, Johnny, CC]
informed: []
depends_on: [ADR-040]
related: [ADR-041, ADR-002]
supersedes: []
session: "DPC Project group chat, 2026-10-17/18 — epic STRATA-AS-A-SECOND-LOCAL-PROVIDER-BESIDE-THE-LLAMA-SERVER-CHILD; Mike's call 2026-10-18"
---

# ADR-044 — Strata as a second supervised local engine

> **Accepted, 2026-10-18 (Mike's call).** Extends ADR-040: that record made local
> inference a DPC-owned `llama-server` child; this one adds a second engine under the same
> ownership model. Implemented as the epic
> `STRATA-AS-A-SECOND-LOCAL-PROVIDER-BESIDE-THE-LLAMA-SERVER-CHILD` (provider type `strata`,
> `strata_supervisor.py`, `strata_provider.py`); the board holds what is still unobserved.
>
> **Amended the same day after Ark's and Johnny's reviews.** The decisions stand; the
> mechanisms are corrected against Strata's source (the child is Strata's launcher, not
> its engine). Per Mike's call, 2026-10-18, this record carries no figure that changes:
> measured values live on the gate card
> `PARKING-AT-REAL-AGENT-SCALE-IS-UNMEASURED-AND-GATES-EVERY-OTHER-STRATA-CARD`
> (gitignored `backlog.md`) and, as dated measurements, in the provider config; versions
> live in the provider config's *tested version*.

## Context and Problem Statement

Strata (github.com/Niko1221/Strata) serves the Flash-Next mixture-of-experts model on
this machine's single card. Measured against our `llama-server` child it prefills long
prompts faster; the figures are on the gate card.

What makes it worth a provider type is **conversation parking**
(`--conversation-cache-mib`, `--conversation-cache-slots`): a conversation is snapshotted
to host RAM and restored instead of re-prefilled, the wall ADR-040 named. The gate card
measured it at real agent scale and found:

- a restored turn costs a small fraction of a cold prefill, with no evictions, while the
  parked conversations fit the budget;
- one snapshot of a long agent conversation is larger than the budget in today's on-disk
  Strata config, so a start with that file would park nothing;
- **the limit is the Windows commit limit (RAM plus page file), not the budget and not
  RAM.** Strata takes most of the commit limit right after load, warns at start when
  planned host allocations approach it, and can exit with an NTSTATUS crash code and no
  message. A run with one more conversation than fits ended in that region, and the logs
  do not show whether Strata exited before the harness watchdog did.

Three more facts bound the design.

- **The process tree.** Strata's HTTP server is a Python launcher,
  `serve/server.py --engine strata --config <json> --port <port>`. It spawns the engine
  `strata.exe --serve` over pipes (the engine has no port) and `strata-vision.exe` for
  images, and puts each in a kill-on-close job object (`serve/winjob.py`, `contain()`).
  `/props`, `/slots`, `/v1`, `/metrics`, `/config` and the conversation cache all live in
  the launcher. When the engine dies, the launcher restarts it itself on the next request
  (`restart(tries=...)`, `RESTART_RETRY_S` between tries) and answers 503 `starting`
  meanwhile. `/metrics` carries no restart counter and no exit code: a dead engine reads
  as `live.state` `unloaded`, the request that met the death gets a 503 naming the exit
  code, and the launcher's log prints it. No launcher flag turns the restart off.
- **The card.** Strata and the `llama-server` child cannot hold the card together
  (`STRATA-AND-THE-27B-CHILD-CANNOT-HOLD-THE-CARD-TOGETHER-AND-NOTHING-ARBITRATES-WHO-UNLOADS`).
  The launcher already has hooks for sharing: `--min-free-vram-mib` (load only when that
  much VRAM is free, else 503), `--before-load` (a command run before the model is loaded
  again, e.g. to unload another server's model) and `--idle-unload` (unload after that
  many idle seconds).
- **The install.** Strata is a git clone with a Python venv, a prebuilt engine and packs.
  `START-HERE.bat` runs `setup.py` (install, then start); `UPDATE.bat` pulls main and runs
  the same setup without starting. `setup.py --update` fetches the prebuilt engine of the
  checkout's own version, so checking out a tag and running `START-HERE.bat --update` pins
  it; the previous engine is kept for `setup.py --rollback-engine`. Strata's `/config` is
  written by its own web page into `strata-<model>.json` and applies on the next start.
  The engine version reads back through `/metrics` `engine.version` (and `/props`
  `build_info`); launch facts through `/metrics` `engine.*`.

## Decision

**Strata becomes provider type `strata`: its launcher is started, watched and stopped by
DPC as a child, beside the `llamacpp_server` child and never at the same time on one
card.** Every point is Mike's call, 2026-10-18.

1. **Supervised, not external.** A subclass of `LlamaServerSupervisor` and of
   `LlamaServerProvider`. Its `build_command` emits the Strata venv's python and
   `serve/server.py`, never `strata.exe`, and always passes `--engine strata` and
   `--port` explicitly — the launcher defaults to the mock engine and to a port of its own
   (`WHETHER-DPC-SUPERVISES-STRATA-OR-LEAVES-IT-EXTERNAL-IS-UNDECIDED` is answered;
   `THERE-IS-NO-STRATA-PROVIDER-TYPE-AND-LLAMASERVERPROVIDER-ALREADY-SPEAKS-ITS-DIALECT`
   keeps its subclass, now with a child).
2. **One fate for the tree.** DPC's one child is the launcher; the engine and
   `strata-vision.exe` die with it through the job object. All of it dies with DPC through
   `stop_all_supervisors`. The job object can fail to apply (`contain()` returns False
   harmlessly), and then the engine could outlive the launcher holding the card, so `stop`
   verifies the card's VRAM is released, not only that the launcher exited. The cost of a
   DPC restart — a cold prefill per conversation — is open, not accepted (see
   `--slot-save-path` under Open Items).
3. **An engine death is fail-closed.** It is defined on what DPC can observe, since the
   engine is not DPC's child and its exit is not a child exit: an engine death seen
   through the launcher (`/metrics` `live.state` `unloaded` while DPC configured no idle
   unload (since 2026-10-20 it does: `idle_unload_s` is 1800, Mike's call, 2026-10-20, and the supervisor reads a
   configured unload as a state rather than a death, `_unload_expected` in `strata_supervisor.py`),
   or a 503 naming the engine's exit code) makes the supervisor stop the launcher
   before it relaunches the engine, and show the reason — the exit code as the launcher
   reported it, with the commit-limit explanation — in the UI. The subclass overrides the
   inherited `ensure_running` so DPC itself never relaunches after such a stop; a person
   does.
4. **The owner assigns the card.** Each agent is set to Strata or to the `llama-server`
   child in settings; there is no automatic swap today (replaced for the owner's own starts
   by the idle swap, see the amendment of 2026-11-02 below; a peer's call still never
   swaps). The launcher's
   `--min-free-vram-mib`, `--before-load` and `--idle-unload` are the named way to relax
   this later. An agent whose engine cannot load because the other holds the card gets an
   error naming the other engine. The **card-holder check** (b97b2beb) does that: before a
   start, each supervisor looks for the other, and the second start raises
   `CardHeldError` naming the holder, never retried. It was needed because `_admission`
   budgets from physical VRAM minus the desktop reserve, and with an explicit KV cache
   type `_warn_if_explicit_type_exceeds_budget` only warns and loads, so the
   `llama-server` child would otherwise start while Strata holds the card and silently
   page through WDDM. The supervisor's existing stop is exposed for a manual switch.
5. ~~**Updates are mixed.**~~ Superseded 2026-10-31 (Mike's call): the tested version gives
   way to a pin in code, see the amendment of that date below. Strata installs and updates through its own scripts
   (`START-HERE.bat`, `UPDATE.bat`); DPC starts `serve/server.py` directly with its own
   `--config` file (point 6). DPC stores a *tested version* with its date in the provider
   config. At start the supervisor reads the real engine's version from `/metrics`
   `engine.version`, **warns** on a mismatch with the tested version (does not refuse) and
   runs a smoke — `/health`, `/props`, two tool-call requests — which counts only after the
   version check, because the mock engine would pass the rest. The smoke runs once per
   start and forces one trivial tool by name twice, both with `strata_checkpoint: false`
   so neither parks anything: first with thinking off, then with thinking on at the effort
   the provider sends on a tools call of the alias (`_build_extra_body`, not a second copy
   of it), because agents run with thinking on and the first call does not touch that path
   (Johnny's review). The second call has its own window — `max_tokens` 1024 with a
   `reasoning_budget_tokens` of 512, at which Strata closes the thinking and writes the
   call's opening itself — so a short xhigh thinking phase cannot spend the window and
   read as no call; an alias whose effort is off skips it. A start whose launcher names
   no version, or whose smoke gets `/health` other than 200, `/props` other than an
   object, an HTTP error, or no tool call from either call (one exception below), is failed closed like an
   engine death (point 3: stopped, not started again until a person resets it). **No tool
   call is fatal, not a degradation** (Zcode's review): every round of the agent loop
   sends tools (`dpc_agent/loop.py` passes its tool schemas on every call), so an engine
   that cannot return a call cannot serve one agent round; and Strata's `forced_call`
   (`serve/frontend.py`) writes the call's opening into the reply itself, so a live engine
   cannot answer a forced call with prose — when it does, the build is a mock or broken.
   Three outcomes only warn and serve: a tool call that comes back with other arguments;
   a call with **no answer within `smoke_timeout_s`** (120 s; Ark's and Zcode's review) —
   `/health` answers before the model is loaded, so the first call is the request that
   pulls the shards in, and a slow load is not a broken engine; and a **thinking-on** call
   with no tool call whose `finish_reason` is `length` (Ark's and Zcode's review,
   reconciled) — the thinking spent the window before the call's opening was written, a
   budget question rather than an engine that cannot call. No tool call with any other
   `finish_reason` (`stop` among them) fails closed, and the thinking-off call keeps its
   rule whatever the reason: 64 tokens with nothing to think. Every verdict on a missing
   call, warning or refusal, names the `finish_reason` and the completion tokens (and the
   reasoning tokens when the reply carries the split). Falsifier (Ark's review):
   if two Strata updates in a row break our launch, revisit — DPC pins a tag (checkout,
   then `START-HERE.bat --update`) and installs it as it does llama.cpp, or Strata goes
   back to user-started.
6. **One config owner.** DPC writes its own start config file and passes it as `--config`,
   not `strata-<model>.json`; Strata's page only reads. The UI shows which file the start
   used.
7. **Provider fields.** Model files with Browse — the first shard (`--native`);
   `--ple-gguf`, `--pack`, `--mtp`, `--expert-profile`, mmproj and control vector derived
   from that shard or the Strata data directory by default, each overridable. Install
   path — optional since B2 step 2: empty runs DPC's own install of the pin, set runs that
   checkout (amendment of 2026-11-01). The cache pair `--conversation-cache-mib` / `--conversation-cache-slots` as two
   typed fields, with a hint that the byte budget is bounded by the Windows commit limit
   (RAM plus page file); the working pair is a dated measurement stored with the config,
   **not a default**, and the first supervised start uses a measured budget, never the
   on-disk Strata value. `--max-context` labelled as the **server** window. The agent's
   window is `context_window`, a config field nothing derives: for a `strata` alias it is
   set to the conversation size parking was measured at, not to `--max-context`. Vision
   on/off; control vector on/off. A *model parameters* block of Strata's `/config` keys
   (sampling defaults, `reasoning_budget_tokens`, …) written into the start config and
   read back after start. Every other flag through `extra_args`. Read-only status from
   `/metrics`: engine version against tested version, effective context, conversation
   cache, images, free VRAM. The `--resident-budget-gib` / `--vram-reserve-mib` values in
   today's configs were set for an older card layout; they are re-derived at the first
   supervised start and recorded beside the card state they were derived on. Both are
   typed alias fields (`resident_budget_gib`, `vram_reserve_mib`) over the base config's
   values; the typed reserve wins over a `model_params.vram_reserve_mib`. The cache's
   admission rules are typed alias fields too, and unlike the pair above each is written
   into every start config, Strata's own default included, so the value the engine parks
   by is always ours and named (Mike's call, 2026-10-19):
   `conversation_cache_min_free_mib` (`--conversation-cache-min-free-mib`, default 2560 —
   a floor of **physical** RAM, `MemAvailable` / `ullAvailPhys`, not free commit),
   `conversation_cache_min_tokens` (`--conversation-cache-min-tokens`, default 0 = park
   every conversation) and `conversation_cache_park_gain` (`--conversation-cache-park-gain`,
   default 0 = park every turn). The start line prints all five cache flags; the status
   shows them as the start config wrote them and as the engine's `INFO` line reports them.
   The typed field `slot_save_path` (Strata's own key) names the folder of its `/slots/0`
   save and restore (session files); it is written as a top-level key of the start config,
   not an engine flag (`strata.exe` rejects `--slot-save-path`), replaces the base config's
   value when set, and DPC creates the folder. DPC does not call save or restore itself yet
   (Mike's call, 2026-10-23: first a measurement).
8. **Vision by the fact of the start.** The subclass **overrides** `supports_vision`: the
   base class answers yes whenever the alias names an mmproj, even with no live child.
   For Strata it answers from the live start — `/props` `modalities.vision` of the running
   launcher — and, while none runs, from the start config DPC would write (`--vision` and a
   `"vision"` block, `StrataSupervisor.start_reads_images`), so an image routed to a stopped
   alias starts it on demand instead of being refused before the start. The global default vision provider stays where it is: an agent on Strata
   already sends images to its own provider through `_resolve_vision_alias`, and a global
   default on Strata would break `llama-server` agents whenever Strata is off
   (`VISION-ROUTING-NAMES-A-27B-THAT-IS-DOWN-WHILE-STRATA-HOLDS-THE-CARD`). An image request
   to Strata was the first smoke of the implementation, and if it failed, vision was off;
   the smoke card is closed on the board (2026-10-20).
9. **Inference Sharing: Strata is offered to peers.** Every call served to a peer is
   sent with checkpointing off (`strata_checkpoint: false`, the engine's `ckpt=0`): it
   takes no checkpoint and parks no snapshot, so a peer is served while our own
   conversations stay parked and while an owner call runs, which it queues behind (Mike's
   call, 2026-10-20; it replaces the rule that answered a peer 503 while our conversations
   held the cache budget). Known cost: one engine, serving one request at a time, so a
   peer's request delays ours, and its arrival parks our live conversation as a switch
   between two of our agents does, which can evict our oldest parked one when the cache
   is full. 503 while Strata is loading. A peer's request may start a stopped launcher in the background,
   one start at a time, when the other local engine does not hold the card and the
   card's free VRAM is enough, and is answered 503 "loading" meanwhile; a launcher
   stopped after an engine death stays the owner's to reset (Mike's call, 2026-10-20).
   With two local engines, "the default local alias" is no longer
   unique: the upstream a peer is served from is set explicitly in Inference Sharing
   settings, and the ADR-041 gateway names its local upstream rather than inferring one
   (today `compute_serving_alias` takes the first `serving_local` entry).
10. **Local by purpose.** `strata` joins `LOCAL_PROVIDER_TYPES` (`firewall.py`, the
    deciding set that pricing imports) and each subscriber set whose purpose requires it,
    in the same commit as the type
    (`LOCAL-CLASSIFICATION-LISTS-DO-NOT-NAME-STRATA-SO-ITS-CALLS-ARE-PRICED-AS-A-VENDOR`).
11. **The control vector differs by engine.** Strata applies it in `project` mode (removes
    the direction); llama.cpp's `--control-vector-scaled` only adds. The same model gives
    different output on the two engines; this is recorded, not a defect.

### Rationale

The commit-limit death ends in an exit code and no message. Under supervision DPC holds
the launcher, which sees that code and reports it; an external server would leave DPC with
a closed port or a silently restarted engine and no reason. Owning the start also lets
DPC own the launch flags, which is what makes the budget, the server window and the cache
pair fields in the UI rather than lines in somebody else's JSON. The rest follows
ADR-040's stance: residency is decided by a person and stated, never guessed by a
scheduler.

## Considered Options

- **External server, started by the user** — rejected: DPC loses the launch flags and the
  reason for a death, and the provider can only report a closed port.
- **A `llamacpp_server` alias on Flash-Next** — rejected 2026-10-18: no conversation
  parking (that layer, like all of Strata's HTTP, belongs to its launcher), no `project`
  control vector. That llama.cpp would compute the experts on the CPU is read from its
  `--help`, not measured.
- **DPC installs and pins Strata as it does llama.cpp** — ~~not now: the mixed update of
  point 5 is cheaper while upstream moves fast; it is the fallback Ark's falsifier names.~~
  Taken 2026-10-31 (Mike's call): see the amendment of that date below.

## Consequences

- **Positive:** two local engines under one lifecycle; a parked conversation turns a cold
  prefill of a long agent conversation into a short restore; an engine death becomes a
  visible, attributed failure instead of a silent restart; calls ledger as local, not as
  a vendor.
- **Negative:** the card is exclusive and switching is manual; a DPC restart costs a cold
  prefill per conversation until `--slot-save-path` is tried; a peer can cost us a parked
  conversation and delays our calls queued behind its own (point 9); Strata updates can break the launch between releases (point 5 watches for
  it); the capacity is a property of one machine's page file.
- **Code the subclass cannot simply inherit** (read 2026-10-18,
  `llama_server_supervisor.py`): `ensure_running` relaunches whenever the child's
  `returncode` is set, so point 3 needs an override; the child's exit code is surfaced
  only before health (`_wait_healthy`) and at `stop`, and in any case the launcher's exit
  is not the engine's, so the engine-death watch of point 3 is new state; `_admission`
  sees physical VRAM, not who holds the card (point 4). One `_proc` per alias suffices,
  because the launcher owns the engine and the vision process.

## Open Items

- **Engine-death signal.** `/metrics` exposes no restart counter or exit code; the watch
  of point 3 rests on `live.state`, the 503 body and the launcher's log. Whether DPC can
  stop the launcher before a waiting request triggers its restart is unverified.
- **The exit-code chain is unmeasured** — what code a commit-limit death shows in the
  launcher's report has not been observed.
- **`--slot-save-path DIR`** enables `POST /slots/{id}?action=save|restore`: a parked
  conversation could be saved to disk and restored after a DPC restart instead of a cold
  prefill. Not measured; its compatibility with the conversation cache is not checked.
- **`contain()` on this machine** — whether the job object applies; `stop`'s VRAM check
  (point 2) is the guard either way.
- **Two commit metrics disagree — closed (Mike's call, 2026-10-19): `GlobalMemoryStatusEx`.**
  ~~`GetPerformanceInfo` and `GlobalMemoryStatusEx` differed in the gate runs and are not
  reconciled; the UI hint must name which one it reads.~~ The reference is free commit,
  `ullAvailPageFile` of `ullTotalPageFile`, the reading that predicted the gate run's
  death; `managers/commit_memory.py` reads it on Windows (nothing elsewhere), and the
  start log line, the engine-death reason and the cache hint print it by field name,
  with `ullAvailPhys` after it.
- **A conversation beyond what fits** is untested past the watchdog stop; the page-file
  size it would need is the owner's machine setting, not a DPC default.
- `THE-2026-10-17-STRATA-EVAL-ROWS-IN-THE-LEDGER-CARRY-NO-COST-AND-NO-STATEMENT-OF-THEIR-ORIGIN`,
  `THE-EVAL-PREFLIGHT-REFUSES-ANY-LOCAL-TYPE-BUT-LLAMACPP-SERVER` and
  `THE-PROVIDERS-EDITOR-HAS-NO-STRATA-TYPE` stay open under the epic.

## References

- Strata: `serve/server.py` (`main` argument list; the engine class's `restart`,
  `RESTART_RETRY_S`, `alive`; the server's `loaded`, `metrics`, `slot_action`),
  `serve/winjob.py` (`contain`), `setup.py` (`--update`, `--rollback-engine`),
  `START-HERE.bat`, `UPDATE.bat`
- `dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py` — module docstring
  (CTRL_BREAK_EVENT, never `terminate()`); `build_command`, `extra_args` appended last;
  `ensure_running`; `_admission`; `_warn_if_explicit_type_exceeds_budget`;
  `_wait_healthy`; `stop`
- `dpc-client/core/dpc_client_core/providers/llamacpp_server_provider.py` —
  `LlamaServerProvider`; `supports_vision`; `stop_all_supervisors`, called from
  `llm_manager.py` at shutdown
- `dpc-client/core/dpc_client_core/dpc_agent/tools/vision.py` — `_resolve_vision_alias`
- `dpc-client/core/dpc_client_core/firewall.py` — `LOCAL_PROVIDER_TYPES`;
  `compute_serving_alias`
- [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md),
  [ADR-041](041-openai-compatible-gateway-as-a-dpc-surface.md)

## Amendment, 2026-10-25 — the launcher joins the backend's registry and job (ADR-046)

*(Mike's call, 2026-10-25: the Strata launcher is in the registry and in the job.
[ADR-046](046-the-backend-owns-every-process-it-starts.md) is the record; this note is what
it changes here.)*

- **Ownership.** The launcher is registered when `StrataSupervisor._spawn` starts it
  (launcher `strata`, pid and start time, its log file's modification time as last output,
  peak RSS, peak commit and VRAM from the sampler) and the registry is told when it ends.
  Resources are reported over the tree, so the engine and `strata-vision.exe` are counted
  with the launcher that owns them. Commit is shown beside RSS because point 3's death is a
  commit death.
- **The supervisor's stop stays first,** as in ADR-040's amendment of the same day:
  CTRL_BREAK and the grace period, then the verify that the card's VRAM is released
  (point 2). At the backend's orderly stop it runs after the command children are killed
  and before the job closes.
- **Session files are saved before the job closes.** When DPC calls `/slots/0` save (it
  does not yet; point 7, Mike's call, 2026-10-23: first a measurement), the save runs from
  the stopping thread and finishes before the backend's job can close, because the job
  kills hard and a kill in the middle of a write leaves a truncated file. The job is the
  backstop; the save is part of the orderly stop.
- **The launcher's own job nests inside the backend's.** `serve/winjob.py` `contain()`
  puts the engine and the vision process in a kill-on-close job of the launcher's, and the
  launcher is itself a member of the backend's. Nested jobs work since Windows 8; nobody
  has run this pair here, and ADR-046's Confirmation lists the test (kill the backend with
  `TerminateProcess`, assert the launcher and its engine are gone).
- **What the backend's job changes in point 2 and in the "`contain()` on this machine"
  open item.** If `contain()` fails to apply, the engine could outlive the launcher; the
  backend's job now makes sure it cannot outlive the *backend*, since the engine is born a
  member. It does not make it die with the launcher while the backend runs, so `stop`'s
  check that the card is released stays.
- **Exit reasons.** A commit-limit death, or any NTSTATUS-class exit nobody sent, is
  `crashed`; the launcher's `0xC000013A` (STATUS_CONTROL_C_EXIT) after the supervisor's
  CTRL_BREAK is not, because the stop was sent. At the backend's stop it is
  `backend_stop`; a stop while the backend runs (idle unload, a stop from the UI, a switch
  of engines) has no word yet, ADR-046 Q8. Point 3's fail-closed rule is unchanged.

## Amendment, 2026-10-31 — the Strata pin lives in code (B2, step 1)

*(Mike's call, 2026-10-31: DPC owns Strata's install the way it owns llama.cpp's, in three
steps, one mechanism for both local engines. The reason given was one consistent mechanism
for two engines. This note records the decision and step 1; it rewrites nothing above.)*

- **What the decision supersedes.** Point 5's "Updates are mixed" and the Considered Options
  line "DPC installs and pins Strata as it does llama.cpp — not now". Both stay above as the
  record of 2026-10-18; the cheaper mixed update is no longer the plan.
- **The three steps.** (1) a pin in code replaces the hand-raised tested version, **built
  with this note**; (2) a DPC-owned install fetched by that pin, the fetch arriving in
  `managers/strata_fetcher.py` beside a module the two engines share (**built**, amendment
  of 2026-11-01); (3) a
  consent dialog and progress for both engines' downloads (**not built**).
- **Step 1.** `strata_fetcher.py` holds `STRATA_RELEASE_TAG` (`v0.2.9`, the tag of a Strata release on
  Strata's app line, 0.2.x) and `STRATA_ENGINE_VERSION` (`0.1.55`, the engine version inside
  that release, which the engine's `/metrics` `engine.version` reports; its **Control**
  package `strata-control-<ver>-<platform>.zip` is on the same 0.1.x line).
  A person reads both: "Strata v0.2.9 (engine 0.1.55)". The alias no longer carries
  `tested_version` or `tested_version_date`: an alias that still has them loads and the keys
  are ignored, and the providers editor's Save drops them. The status of an alias (`engine_status`,
  `local_engine_status`) and the eval's provenance carry `pin: {release_tag, engine_version}` in
  place of the two keys; the version the engine reports stays beside it.
- **What point 5 keeps.** The start compares the reported engine version with the pin. A
  mismatch **warns and starts**, because until step 2 the alias's install is the owner's own
  checkout; a missing version is still refused as the mock engine's mark; the smoke still
  counts only after the version check.
- **Moving the pin.** Update Strata on a node, run DPC's start smoke there live, record the
  result on the point-5 card, then change both constants in one reviewed commit.
- **Words.** **Control** is Strata's package, **launcher** the program DPC starts
  (`serve/server.py`), **engine** `strata.exe` with `strata-vision.exe`; **pin** has two
  senses, told apart in `docs/GLOSSARY.md`.
- **Seen on the pinned release.** The node was updated to Strata v0.2.9, engine 0.1.55, on
  2026-10-31 and DPC's start smoke passed there live (`dpc-client.log`, 20:22:43 and 20:50:38
  local time). v0.2.9 adds the
  `mtp` key to the run config's editable keys; DPC mirrors it as a model parameter.

## Amendment, 2026-11-01 — DPC's own Strata install (B2, step 2)

*(Step 2 of Mike's call of 2026-10-31, built in three commits on 2026-11-01; the team's
consensus of the same day settled the open questions of the plan. This note rewrites
nothing above.)*

- **The fetch.** `strata_fetcher.install_pin` downloads the platform's **Control** archive
  by its row in `PLATFORM_ASSETS` (name, sha256, size) from the pinned release's own folder,
  never `releases/latest`, into a staging folder beside `~/.dpc/bin/strata/<tag>/<platform>/`.
  The swap is the one llama.cpp now shares (`local_engine_pin_fetcher.install_with_staging_swap`):
  the old root is renamed aside, staging takes its place, the old one is removed, and a
  failed rename puts it back — no state passes through "no install".
- **The engine, by Strata's own updater.** Control's python runs `strata.py update
  --no-control --feed <release folder> --components-dir <staging>/components --output json`
  under `run_supervised` (launcher `strata`, a subsystem child, an hour's timeout).
  `--no-control` keeps the updater off the Control DPC just checked; the release folder
  makes Strata pick its own feed file there (linux-aarch64 has its own). The environment
  drops every `STRATA_*` of the parent (`STRATA_UPDATE_TRUSTED_KEYS` among them, so Control's
  own key list, pinned by the archive digest, is the only trust) and sets
  `STRATA_NO_HANDOVER`, so `strata.py` never re-runs the command under a newer Control
  staged in `components/control/` (`strata_cli/handover.py`). APPDATA,
  LOCALAPPDATA and the XDG homes point into a throwaway folder: there the updater's test
  start finds no model to load in full on the card, and the user's Strata settings,
  tuning marks and process records stay untouched.
- **DPC's own check.** The engine Strata installed is then checked by DPC
  (`check_engine_identity`): the manifest's Ed25519 signature by the key held in code
  (`STRATA_UPDATE_PUBLIC_KEY`, key id derived as Strata derives it), `id`, `version`,
  `platform`, the platform's build commit as a whole hex token in `build_id`, and every
  file's size and sha256. `.dpc-pin.json` marks the install with the tag, the platform, the
  Control digest and the engine's folder and identity; every pin-mode start re-checks the
  signature, the fields and the manifest's sha256 against the marker, and a mismatch is
  refused, never refetched silently.
- **The launcher from Control.** An alias with an empty `strata_install_dir` runs Control's
  python and `serve/server.py` from the Control root, with `STRATA_COMPONENTS_DIR` naming
  its `components/` folder; LOCALAPPDATA is not redirected for the launcher, so its
  leftover cleanup is shared with a checkout's. The base config is read as before and,
  before any typed field, `StrataSupervisor._base_config_for_the_pin` makes `exe`
  `component:strata-engine`, the vision exe the reader the base names (`strata-vision`,
  `.exe` on Windows, `-hip` on AMD) in the pinned engine folder, `cwd` the Control root, drops `lib_dirs`, and moves every absolute
  path inside the base's original `cwd` (the checkout: `args` tokens, the file part of
  `--control-vector-scaled`, `tokenizer`, the vision `mmproj` and `model`) to the same
  place under the Control root, refusing one that is not there. "Inside" is a path
  comparison, case-folded on Windows, never a string prefix; Strata-data and an alias's own
  paths are never moved. On the owner's base config this moves `--expert-profile` alone.
- **`strata_install_dir` is optional and wins.** Set, the owner's checkout runs exactly as
  before and nothing is fetched; empty, `strata_base_config` must be an absolute path.
- **The fetch comes before the card.** `ensure_running` fetches before it clears an
  unfinished start, waits on a predecessor's drain or claims the card, so a download of
  minutes leaves the card to the other engine; `_draining` is read again after it.
- **A peer does not fetch.** A peer's request meeting a pin that is not installed yet is
  refused (`_peer_start_refusal`) and begins no start: the download is about 700 MB.
- **Point 5 on the pin.** An engine of DPC's own install reporting another version is
  refused like a missing version (fail-closed, `failed_start`), the reason naming what
  was found — the version and the marker's build_id and variant — against the pin
  (Strata release, engine version, build commit). The owner's checkout keeps warn and start.
- **Measured on the owner's node, 2026-11-01**, through the supervisor's own code outside
  the backend, alias copies with the cache at 1024 MiB: a from-scratch install took 133 s
  (Control 43 s, Strata's updater and the identity check with every file hashed 90 s); the
  staging rename right after the updater ran executables from staging succeeded and left
  neither `.staging` nor `.old`; the launcher answered `/health` in 34.5 s, the version
  check named the pin, the start smoke passed in 2.2 s, a text and an image request
  answered, VRAM peaked at 31,465 MiB and the stop gave the card back. The shared-LOCALAPPDATA
  question could not be put: killing a checkout launcher ended its engine with it (the
  launcher holds `strata.exe` in a kill-on-close job object), so no orphan of one install
  was there for the other's start to find. The case stays open only for an engine the job
  object did not hold.
- **Deferred.** A consent dialog and progress for the download (step 3); retention of an
  older pin's folder and a way back to the previous pin; a base config for a user with no
  checkout and no Strata-data.

## Amendment, 2026-11-01 — consent, progress and the engine backend (B2, step 3)

Agreed in the DPC Project chat, 2026-11-01 (Mike, Zcode, Johnny, Ark). It narrows the
step-2 rule "the fetch comes before the card".

- **The fetch asks first.** A pin-mode start with no install, or with an installed engine
  that is not the one the alias chose, asks the owner once through the model download
  consent flow (name `strata`) and refuses with `PinNotInstalledError`; nothing downloads
  inside a start. A peer's call, or a start begun for a peer, neither asks nor fetches and
  meets `provider_unavailable`.
- **The fetch before the card holds for the consent path only.** The owner's download runs
  outside any start, so the card stays with whoever holds it. Where no consent service is
  bound (tests, scripts) the start installs inline, and since step 3 it does so *after*
  it has waited for the predecessor and claimed the card, `_draining` read again after it:
  an inline install may replace the engine, and the engine must not be replaced under a
  running launcher.
- **One engine per install, replaced only when no launcher of it runs.** The alias key
  `strata_engine_backend` names the word Strata's updater takes as `--backend`; the
  rebased start config carries its family as `backend`. Engines of several families could
  sit side by side in `components/engine/`, but Strata's updater keeps only the new engine
  and the previous one of the requested family after an install (`updater.py` 1023-1035,
  1069, 1129, 1136-1137), and a start picks within a family by its own order, never by the
  build (`engine_component.py` 985-996, 1007-1016). Strata's own refusal to update a
  serving engine does not see a launcher of ours, because the updater runs with a
  throwaway home and no `STRATA_*` variables, so the rule is DPC's: an install that would
  replace the engine is refused while a launcher of this install runs, and checked again
  just before the swap. Two Strata aliases of different families on one node therefore
  take turns, each start replacing the engine; separate component folders per family
  would end that and are not built.
- **One engine per family, too.** A chosen build (`hip-rdna3`) reaches only the install:
  the start passes the family alone, and Strata picks among that family's folders by its
  own order. So the install, still in staging, removes every other engine folder of the
  chosen family (renamed aside under a dot name, which Strata's scan skips, then deleted),
  and an install that still holds two of one family refuses its start with the reason
  instead of refetching. DPC's retained pin is the way back, not Strata's "previous";
  Strata's `.updates/state.json` is never written.
- **Sizes.** The consent shows the chosen package's size when one package matches
  (`size_source` `package`), the largest matching package otherwise (`package_max`), and
  for an empty choice the largest and smallest of the platform. DPC refuses a download only
  when even the smallest package cannot fit; Strata's own check, exact and before its
  download, reports an exact shortfall (exit 6).

## Amendment, 2026-11-01 — the retained pin and the rollback (B2, step 3)

Mike's answers to the step-3 questions, agreed in the DPC Project chat, 2026-11-01. The same
rules hold for llama-server (ADR-040, amendment of the same date); what is Strata's own is
named here.

- **The retained pin.** No second constant: the retained pin is the other installed pin
  (a folder whose `.dpc-pin.json` names its own tag and platform) whose marker carries the
  newest `start_passed_at`. That field is written, atomically and off the loop, at the first
  start of a pin that passed (here: the version check and the start smoke), once per pin
  folder per backend process. A retained Strata pin is run on its marker's word — the engine
  manifest's bytes as the marker recorded them, its id, version and platform, the alias's
  `strata_engine_backend`, one engine of its family — and is not re-signed, since the key may
  move with a bump. Its version check compares with its own marker's engine version.
- **A pin bumped but not downloaded yet.** The owner's start runs the retained pin and asks
  for the new one with `on_decline` `degrade`, never refusing; a start begun for a peer may run
  the retained pin too, and never asks or fetches. With no retained pin the step-3 consent
  refusal stands.
- **Rollback once.** A first start of the current pin (no `start_passed_at`) that fails for the
  build's fault — the launcher died or never answered /health or /props, the version check
  refused, the smoke failed — with a retained pin present, falls back once inside the same
  `ensure_running`: the refusal's latched `failure` is cleared, one ERROR line and one
  `error_toast` say so, and the retained pin is started. If it fails too, the start latches
  as before. Later starts go straight to the retained pin until the alias's Reset
  (`reset_failure`) or a backend restart; the record is in memory. Never to an older pin, and
  not for a card held by the other engine, an admission refusal before the spawn, a config
  error, a cancellation, a drain, or a pin that has ever passed. This is DPC's rule, not
  Strata's `--rollback-engine`, and Strata's `.updates/state.json` is still never written.
- **Retention.** After the current pin's pass, the other marker-carrying pins are removed in a
  thread, keeping the retained one, skipping a folder a live child runs from or a configured
  binary sits in, each removal logged at INFO with its size. Nothing is removed while no other
  pin has passed: the pins installed before `start_passed_at` existed stay until the next new
  pin passes, and a folder without a matching marker is never touched.
- **Status.** `local_engine_status` gains `pins`, per engine (`llama.cpp`, `strata`): `pin`,
  `installed_pins` (tag, platform, `start_passed_at`), `retained_pin`, `running_pin` and
  `rolled_back_to` {from, to, reason, at}; each Strata row gains `running_pin`. The block is
  per engine: `rolled_back_to` is the engine's latest rollback and `running_pin` one live
  child's folder, whichever alias. No size rides it: nothing reads one, and walking a
  Strata install costs about 50 ms a status. The Local
  engines block shows one line per engine and a warning line after a rollback.

## Amendment, 2026-11-02 — the idle swap

Mike's call, team chat 2026-11-02; refinements by the reviewer. It replaces point 4's "there is
no automatic swap" for the owner's own starts. The reason is the second local alias: with a
card holder that excludes it (any pair: Strata and Strata, Strata and `llama-server`,
`llama-server` and Strata), the alias's first message would fail with `CardHeldError` until
the owner found Stop engine, although nothing was using the holder.

A start of a local alias that an excluding card holder blocks (the check of point 4,
`_card_held_error`) goes by these rules:

1. **The setting is off: today's refusal.** `[local_engines] swap_when_idle` (default `true`)
   off gives `CardHeldError`, unchanged. The default is on because the alternative is the
   failed first message. With no settings bound (tests, a script that runs no service) there
   is no swap either.
2. **A peer never swaps.** A call served to a peer (`peer_being_served()`), and the start
   begun for a peer's request (`start_for_peer`, `_STARTED_FOR_PEER`), meet today's
   `CardHeldError` even when the holder is idle: an engine of the owner's is not stopped for
   someone else's request. The policy of point 9 still asks `_card_held_error` first, so a
   peer's request does not start a stopped launcher over a holder.
3. **The owner's start, the holder idle** (`_in_flight == 0`): the holder is stopped by the
   Stop engine command's own path (`stop_by_hand`: behind the holder's start lock, drain,
   stop, reopen), and then the new alias starts. The idle check and the decision to stop run
   in one synchronous stretch, with no `await` between them, and so does the claim of the
   card that follows the swap, on the same owner loop as the claim of point 4 (no new lock:
   the discipline is the claim's own). In that stretch the holder is marked
   draining-for-swap. A request that arrives after the check, a new call or a start of the
   holder, is refused at once with "'<holder>' is stopping to free the card for '<new>'"
   and does not queue behind the stop and does not run on a stopping engine.
4. **The owner's start, the holder busy:** the holder is marked draining-for-swap the same
   way (new requests refused at once with the same reason, never a silent queue), and the
   start waits until the holder's in-flight count reaches 0, at most `[local_engines]
   swap_wait_s` seconds (default 300, whole seconds, 0 or more; 0 means do not wait and
   refuse a busy holder). Then the holder is stopped and the new alias starts. On the timeout
   the mark is cleared, the holder serves again, and the start fails with a `CardHeldError`
   that names both and the seconds: "the card is held by <engine> alias '<holder>', busy for
   N s". A start cancelled while it waits clears the mark too. A holder that is already
   draining or stopping for another reason (a settings save retiring it, a Stop) is not
   swapped: today's refusal names its state.
5. **A swap is said once.** An INFO line "swapped '<holder>' out for '<new>'" with "; its
   parked conversations are dropped" when the holder was Strata (a stop drops what the
   launcher parked in host RAM; `llama-server` parks nothing), one `error_toast` carrying the
   same sentence (the rollback notice's mechanism), and `last_swap` {from, to, from_engine,
   to_engine, at, parked_dropped} in `local_engine_status`, in memory until the backend
   restarts.

Two owner starts racing for the card: one claims it; the other sees the claim as a holder and
follows the same rules against it (a claim whose child is still starting counts as idle, and
its stop waits for that start to finish). A holder another start is already swapping out is
waited for, within `swap_wait_s`, and the rules then apply against whoever holds the card
next. One start swaps out at most four holders before it takes the refusal, so a holder that
comes back each time is a refusal and not a loop.

The settings are read at the moment of each start (a module-level binding made at service
start, as the consent service is), so a change needs no restart. They are set from the
Providers editor's Local engines block, under "Model files folder", through the command
`set_local_engine_swap` (`enabled`, `wait_s`), and `local_engine_status` reports them as
`swap_when_idle` and `swap_wait_s`.

What a swap does not do: it does not park or hand over the stopped holder's conversations
(Strata's are dropped), and it does not stop a holder on the strength of a peer's request. A
call that had already got past the holder's start when the swap began runs to its end (the
busy case) or meets the refusal at the slot (the idle case); the stopped alias stays
configured and its next owner call starts it again, swapping the new one out if that is idle.
