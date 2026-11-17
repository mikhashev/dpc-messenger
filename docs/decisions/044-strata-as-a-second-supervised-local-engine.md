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
   set to the conversation size parking was measured at, not to `--max-context`; left
   unset, the engine answers (amendment of 2026-11-03). Vision
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

1. **The setting is off: today's refusal.** `[local_engines] swap_when_idle` off gives
   `CardHeldError`, unchanged, and the refusal names the switch. The default was first on (the
   alternative is the failed first message) and is **off since the same day's live check**:
   a holder idle between two calls of an agent's loop was swapped out mid-task, and the two
   aliases traded the card. It goes back on once the card queue (busy for a whole loop, FIFO)
   is live-checked — Mike's call and the reviewer's, team chat 2026-11-02. With no settings bound (tests, a script that runs no service) there
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
   launcher parked in host RAM; `llama-server` parks nothing), one `notice_toast` (a non-blocking
   toast; the rollback notice stays an `error_toast`) carrying the
   same sentence, and `last_swap` {from, to, from_engine,
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

**A busy holder, since the card queue's rules 1 and 2 were built** (Mike's call, 2026-11-02;
the rules above stand as first written). Busy is now a child starting, a call in flight or a
request standing on the alias, so a claim whose child is still starting no longer counts as
idle. A busy holder is not marked and keeps serving, new requests included; the start waits, at
most `swap_wait_s`, and the mark and the stop come only once the holder is no longer busy. On
the timeout the holder was never touched, and the refusal also names who stands on the alias. A
start already waiting when a stop for another reason lands goes on waiting for it. The refusal
"'<holder>' is stopping to free the card for '<new>'" now comes only after a mark, which falls
only on a holder that is not busy. A start that first meets a holder stopping for another
reason still gets today's refusal.

## Amendment, 2026-11-02 — the card queue

Mike's call, team chat 2026-11-02, after review by Ark, Johnny and Zcode. **Decided and built.** In
the code: rule 1, with `accepted_request` as the only kind of hold, at the two doors it names and
on a gateway call; rule 2, no mark while busy; the queue and its cancel (rules 3 and 12); the order
(rule 4); the single deadline (rule 5); rule 6, the `caller_window`, as built there; the gateway and
peer doors (rules 7, 8 and 15); rule 9, the from-inside refusal and the card-borrow, as built there;
rule 10, as built below; rule 13's switch and safety net; `wait_reason: card_queue`; and the status
key `card_queue`. `swap_when_idle` stays off by default until all of it is live-checked. The work is
the epic
`THE-CARD-HAS-NO-QUEUE-SO-A-SWAP-CAN-CUT-AN-AGENT-LOOP-AND-TWO-ALIASES-PING-PONG` (gitignored
`backlog.md`). This note replaces the parts of the idle swap amendment it names below and
rewrites nothing above. **Nothing is ever forced.**

The case, from `dpc-client.log` and the node ledger, local time, with the swap switched on: at
16:23:06 `qwen3.8-flash-next-iq3_s Strata` was swapped out for `strata flash-next`; Johnny's
agent made calls on `strata flash-next` from 16:24:04 to 16:24:29; at 16:24:34, between two
calls of that loop, `strata flash-next` was swapped out for the other alias; the loop's next
call began at 16:24:51; at 16:25:06 an owner's request on the other alias was refused with
"is stopping to free the card for 'strata flash-next'"; at 16:25:10 the card was swapped back.
One call in flight is too short a measure of busy, and two aliases trade the card.

1. **Busy.** A holder is busy while a child is starting or a call is in flight, or while an
   `accepted_request` or a `caller_window` stands on its alias. An `accepted_request` runs
   from acceptance to its answer, its wait in the queue included. **Acceptance is per door**
   (Mike's call, variant A, 2026-11-02). An agent turn is accepted on entry into
   `DpcAgent.process`, and the hold is taken around `run_llm_loop`; waiting at the run gate
   stays a **waiting** item and holds nothing. An owner's chat is accepted in
   `InferenceOrchestrator._execute_local_inference`. The cost: a holder freed while a request
   waits at the gate can lose the card before that request starts; the request then queues, it
   does not fail. This is inferred from the code, not observed. A **turn** stays the glossary's span (run gate held to released),
   so an agent's accepted request lies inside its turn; they are not two kinds of busy. A turn
   waiting for a person's approval counts as busy: that is the honest cost of "never forced". A
   contender waits to its deadline and is refused with the holder named and who stands on its
   alias; its `wait_reason` joins them with the queue.
2. **No mark while busy.** A busy holder is not marked draining and not stopped; it serves what
   it has. The mark and the stop come only once it is no longer busy, with no await between the
   last check and the claim (the discipline `_free_the_card` already states).
3. **The queue.** One `_CARD_QUEUE`, at the level of `ensure_running` (the base supervisor's and
   the Strata override's), entered before `_start_lock`, oldest first. A request whose alias
   already has a place joins it. A place belongs to the alias's oldest live request; when that
   request leaves (deadline, cancel) the place stays with the next oldest live one of that
   alias, and is gone only when empty. Only the first place acts.
4. **Order.** A request accepted after a place was taken waits behind it, also on the holder's
   own alias. Later rounds of a running request and calls of an open window are not new and are
   served. A waiting start waits for what stood there when it queued, no more. The first code
   commit is therefore a registry of accepted requests, because `_draining` is a plain flag and
   cannot express this.
5. **One deadline per request.** `swap_wait_s` (the default stays 300) is fixed at acceptance
   and read once, counted from acceptance, and covers the wait and the stop phase;
   `stop_by_hand` gets the remainder. This replaces two things: `_free_the_card` starts its
   clock again on each round, so one start can wait `_MAX_SWAP_ROUNDS` times `swap_wait_s`, and
   it calls `stop_by_hand` with no timeout. On expiry the request leaves the queue with a
   refusal naming the holder, why it is busy and the setting; the holder is untouched. The
   refusal does not promise that waiting was right; it promises not to hold a person for 20
   minutes.
6. **`caller_window`.** One setting, `[local_engines] caller_idle_s` (default 120, 0 = no
   window), covers a peer's and a gateway client's run of calls, both invisible to DPC. A
   caller counts as busy for that many seconds after its last call, and calls inside the window
   renew it. A gateway call is an `accepted_request`; the window only extends busy between
   calls. The trade-off is written down: a gap longer than the window still cuts that caller's
   loop. `[connection] remote_inference_timeout` (default 1200 s) is a door limit and not this
   window; the two numbers answer different questions.
   **As built**, where the rule left a choice to the code, and open to its review. A window
   is a test of `_swap_clock` against `_last_caller_call_at` at the busy test, not a hold in
   `_CARD_HOLDS`: a hold is entered and left around a body, a window has no body to leave, and it
   needs no timer. `card_held_for` therefore refuses the word. Each note and each look drop every
   ended window, so the record holds one entry per caller with a call inside its window. What
   opens or renews a window is a served call on a local alias, the one that leaves a usage row; a
   refused or failed call does not, and a vendor's has no card. A busy refusal names a caller in
   its window as "caller '<id>'"; a gateway call in progress is named by nobody, because a hold
   names an agent and a conversation and it has neither. The gateway's hold stands from acceptance
   since rule 8's order landed; it stood inside `_peer_inference_lock` before that, because the
   start happened in there and a hold outside would have had the start waiting for a client
   waiting for the same semaphore. A call of a client's open window is not new (rule 4): the hold
   says so at acceptance, by the caller's name, and a peer's is asked by name at `ensure_running`.
7. **Peers and the gateway.** A peer never swaps and never takes a place. A new peer while a
   place waits is refused `provider_unavailable`, with no `retry_after_sec`; the **refusal
   code** row's "retryable later but not by a short wait" stays true, and so does DPTP §3.4. A
   gateway client is the owner's own user and queues like any owner start.
8. **The semaphore is not the queue.** `P2PCoordinator._peer_inference_lock`, shared by the peer
   door and the gateway, is bypassed by owner chat, agent turns, sleep and background calls. It
   stays the line of guest calls on an already loaded alias, with the door limit. The gateway's
   order for a local alias becomes card queue, then semaphore, then the call (today the
   semaphore comes first and the start happens inside it), so the gateway no longer waits for
   the card while holding it and a live peer does not lose its window. Peers do not queue, so
   there is no cycle.
   **As built.** `Gateway.complete` takes its hold at acceptance; with the swap on it asks
   `ensure_running` before the semaphore, which is the place in line and, for a child that is down,
   the start; then it takes the semaphore with its door limit, then `_call`, whose own
   `ensure_running` passes on the admitted hold. With the swap off there is no first step and a
   start happens inside `_call` as before. A start that fails in the first step answers what it
   answers inside `_call`, 502. Johnny's objection, that two gateway starts and a peer's would
   converge in one wait loop on the semaphore, is answered by the queue: gateway starts wait in line
   holding no semaphore and only the first place acts, and a peer takes no place and is refused,
   so whoever sits on the semaphore waits for nothing behind it. A client's total wait is the two
   added, `swap_wait_s` in the line and `[connection] remote_inference_timeout` on the semaphore,
   with no limit over both: that sum is what this order costs, and it is taken on purpose.
9. **From inside.** A request holding an excluding alias that asks for another is refused at
   once, in the `_card_held_error` form; otherwise it would wait on itself. On this node the
   real route is `describe_image` to `vision_provider`. A request holding no alias (a cloud
   agent) queues as a normal request. **The card-borrow** (Mike's call, variant B, 2026-11-02):
   a request's auxiliary call may give up its own alias, through the queue, within its one
   deadline, on a stop that loses no parked conversations (`DROPS_PARKED_ON_STOP` false on the
   stopped engine; that direction confirmed as the default, Mike's call, 2026-11-03), with the
   description cached once per turn; background calls never. Wherever these conditions fail, the
   refusal above stands.
   **As built**, where the rule left a choice to the code, and open to its review. The marked calls
   are the two that describe: `_pre_analyze_image_for_agent` and the `describe_image` tool, each
   inside `a_card_borrow()`. Nothing else borrows: `read_document`'s page look and a compaction on
   another alias meet the refusal. A llama-server child is the only holder that can be given up
   and, since two of them do not exclude each other, a Strata vision alias the only thing it is
   given up for. So the first swap stops a child that parks nothing, and the way back stops Strata.
   - **A description never stops a Strata holder** (Mike's call, team chat, 2026-11-04, agreed by the
     team as a decision until the session-files measurement; the one place to change it is
     `_description_may_not_stop`). This holds also where the request does not stand on it: a cloud
     agent, or a turn whose alias has no child now. Such a description is not a borrow, and it does
     queue as a normal request as the sentence before says, except that a holder with
     `DROPS_PARKED_ON_STOP` true is refused, at once if it is on the card when the description asks and
     at its turn in line if it took the card meanwhile, in the words of the Strata-holder refusal. The
     turn goes on with a failed description and what is parked stays. An idle llama-server child is
     swapped out for it as before. This covers only the calls marked as a description
     (`_pre_analyze_image_for_agent`, `describe_image`). Not changed: an ordinary request's swap, so the
     model call of a turn on a childless alias still swaps out an idle Strata holder, and what it
     parked goes, right after its description was refused to protect it; and a person's group image
     (`_describe_image_for_agents`), unmarked, which is a request of its own and swaps out an idle
     Strata holder within its 30 s, `parked_dropped` as the engine's count says; and `read_document`'s
     page look (`dpc_agent/tools/document.py`), unmarked as well, which from a request not standing on
     the card, such as a cloud agent's, swaps out an idle Strata holder the same way.
   - **The first swap.** The request takes a place for the vision alias, on the deadline of its
     hold, and acts as any first place does; its own hold does not make its alias busy
     (`_busy_for_the_card`'s `but_for`), and every other stand does: a second request on the alias,
     a call in flight, a caller in its window. Those are waited for within the deadline and never
     forced, and on its expiry the holder is untouched. The request keeps standing on its alias
     through that wait, so nothing can take the alias from under it for nothing. Refused at once,
     with nobody stopped: a Strata holder, another child on the card beside the request's alias, any
     place already in line (it may be waiting for this very hold, so behind it the description would
     wait on itself, and ahead of it two swaps would be spent on someone else's wait; whether an
     older request's description should go ahead of a younger place is open), a request past its
     deadline (so `swap_wait_s` 0 never borrows, and neither does a long turn's late
     `describe_image`). `_blocking_holder` asks about the request's own stand first, so no other
     holder is stopped for a request that is then refused.
   - **The way back** (`_CardBorrow`). In the stretch of the mark the request starts to stand on the
     vision alias too, and takes a place of its own for its own alias right behind the acting place,
     so that what came later stands behind it. Nobody joins that place, and a request that joined the
     description's place while it waited goes behind it when the description leaves. Its next round's
     `ensure_running` takes that place, which reads as a row of `card_queue` with `give_up_at` null
     from the mark until then. **It gets a fresh bound, and the refusal at its end is the one way a
     borrow can cost a turn** (Mike's call, DPC Project chat, 2026-11-04): `swap_wait_s` as it stands
     when the next round asks, counted from then and not from acceptance, so rule 9's "within its one
     deadline" covers the first swap and this bound the way back. What it can wait for is the
     description's own call still in flight after a tool timeout, an older request on the vision alias
     with no child when the borrow began (served under rule 4), and a caller in its window. At the end
     of the bound the refusal names the holder and who stands on it, not the request itself, and says
     the request gave up its alias for a description; the turn ends `failed`, in those words. With
     `swap_wait_s` 0 no borrow begins; one set to 0 during a borrow makes the way back a request with no
     time left, which a busy child refuses at once and an idle one does not. Once it has marked the
     vision alias, its own stop is bounded by `_STOP_WITH_NO_WAIT_LEFT_S` (60 s from the mark, whatever
     is left of the bound), because a launcher can take longer to go than a short `swap_wait_s`; a stop
     slower than that refuses it too, naming that limit, and the stop goes on. `wait_reason` reads
     `card_queue`. A person's Stop ends the wait at once, and Stop engine on the alias ends it with its
     own refusal. The switch does not: a borrow begun with the swap on finishes with it off, its row
     included. Stopping the vision alias is an ordinary mark on an alias not busy but for this request.
     The first swap does wait for a busy alias within the deadline, as above (Mike's call, 2026-11-04).
   - **A failure leaves the turn working.** A description refused, or failed on the vision alias
     after the stop, goes into the turn as a failed analysis as the refusal did; the next round's
     way back then finds the card empty and starts its alias again. The way back's refusal above is
     not a description failing, and it is the exception. A person's Stop that takes the
     request out of the line, a description's wait or a way back, is no failure and is not kept: the
     turn ends `stopped` with "Stopped by user" and no further model call. On the pre-analyser's route
     `TakenOutOfTheCardQueue` reaches the loop as the round's own. A tool cannot raise it there, since
     `ToolRegistry.execute` turns any exception into the tool's failure text, so the hold says so
     (`taken_out`) and `_finalize_after_user_stop` asks for no closing call; a Stop with nothing of
     this request in line keeps that call, which is the loop's rule for any Stop between rounds.
   - **A borrow cut short leaves the vision alias's child up** (a turn that ended before its way back:
     a Stop, a Kill, or a last round that needed none). Nothing stops it and nothing starts the
     request's alias again: the card stays with it until a person's Stop engine or the next local
     start. That start reads the card as with any idle holder: with the swap on it takes its place
     and swaps the child out, and with the swap off it meets the card-held refusal naming the vision
     alias and the switch. A peer and a background call read it as they would any holder. This costs
     a Strata child idle on the card, against a stop nobody asked for; it is written down here as
     decided by the code, and open to review.
   - **What outlives its request.** `describe_image` waits for the card at most the tool's own
     timeout from the request's acceptance (`waiting_for_the_card_at_most`), so no thread is left in
     line after the turn has gone on: such a thread's stand would not count, and it would stop the
     alias under the turn. A thread that outlives its request (a Kill) is refused with no stop and
     no load, whether it stands in line by then or only asks. Past the first swap a thread that
     outlives the tool's timeout finishes on the vision alias, and the way back waits for it within
     its bound. On the
     tool route the wait shows no `wait_reason`, because a pool thread does not carry the turn's mark;
     a cloud agent's tool has the same gap.
   - **Once per turn** (`description_key`, `turn_description`, `keep_turn_description`). Kept on the
     hold, so the end of the request drops it; by the images' bytes and the asker: the pre-analyser
     by its alias, `describe_image` by alias, path and prompt, so another question is another look.
     An outcome is kept, and so is a failure that a retry would only repeat or pay two swaps for: a
     local engine's refusal, or anything met while a borrow stands, a vendor's error included, since
     the code does not tell them apart by then. A vendor's passing error with no borrow standing is
     asked again next round, as before. With the swap off nothing is kept and every round asks, as
     before.
   - **What is said.** Two swaps, each said once with its toast and `last_swap`. Whether "its parked
     conversations are dropped" is said, here and on any swap, comes from the engine's own count just
     before the stop (`_parked_conversations`, the launcher's `/metrics`): zero is not said and is
     `parked_dropped` false, and a count that cannot be read within 3 s is said as if some went. So
     a child that served one description should say nothing on the way back, nor at the next start's
     swap after a turn that ended during a borrow; that a real launcher reports zero there is
     inferred, not measured. This narrows point 5 of the idle swap amendment, which said it of every
     Strata stop.
   - **What it costs elsewhere.** While the vision alias is up a compaction on an alias that normally
     sits beside the request's own is refused at once; `apply_compaction` absorbs that as any
     summariser failure (it keeps more verbatim and tells the user) and the next compaction goes
     ahead. Only the tool route meets this: the pre-analyser's way back is taken in the same round.
10. **Background calls** (the self-reflection confirmation, skill reflection and the like) never
    swap and never queue. They run only if their alias is on the card; otherwise a named skip goes
    into a record with a reader, on the model of `last_swap`, never silently. Nothing tries a skip
    again; what follows is each call's own, as built below. The reason is
    `DROPS_PARKED_ON_STOP = True` on Strata.
    **Amended in place, 2026-11-03.** The list named knowledge extraction and the rule ended "and
    they retry next cycle". Extraction is out by the owner's call, 2026-11-03: it starts at a
    person's word (End Session, `/extract_knowledge`, Extract Knowledge) and is a request that
    queues, not a background call. The retry is out after the review by Ark, Zcode and Johnny,
    2026-11-03: no such cycle exists for either call that remains. "Check" became "confirmation" in
    the same review (Ark): the call confirms a conversation's type and reflects on nothing.
11. **No `sleep` kind and no `preemptible` class.** Sleep is a request like others; on this node
    it runs on the agent's own alias or a cloud one. `preemptible` is parked until a stop stops
    dropping parked conversations.
12. **Death and Stop engine.** A holder's death and Stop engine free the card and advance the
    queue; it never waits for the dead. Queue cancel is its own command beside `_CARD_QUEUE`, not
    an extension of `stop_waiting`, which only takes a chat out of the agent's run-gate line.
13. **The switch and the safety net.** `_MAX_SWAP_ROUNDS` stays as a safety net, with a WARNING
    it does not print today; FIFO is what closes the ping-pong. The queue lives only when
    `swap_when_idle` is true; when false, today's immediate refusal.
14. **Storage.** Accepted requests and the queue are keyed by alias, so they survive a settings
    save replacing the supervisor. `_CARD_CLAIMS` stays keyed by supervisor object, because it
    records which child is physically on the card, and a retired predecessor keeps its claim
    until its `stop()`. These are two facts, not two keys for one. This is inferred from the
    code, not observed.
15. **The gateway's refusal.** `card_busy` stays the semaphore's word. A queue refusal is
    `provider_unavailable`, not today's 502 `upstream_error`. `_draining_error` must reach
    `_engine_unavailable_cause`, so that a guest stops reading it as `peer_refused`. The refusal
    count stays the same: no twelfth code.
16. **Evidence and test.** The log sequence above is the evidence. The replay test targets HEAD
    with `swap_when_idle=True` forced, not the default-off state; it asserts that the "is
    stopping" refusal is replaced by queueing, and it asserts the request's total time.

**Names.** The kind of hold is `accepted_request`, registered through
`card_held_for(alias, kind, *, agent_id=None, conversation_id=None)` in `_CARD_HOLDS`; `caller_window`,
decided as a second kind, is a test of the clock as built (rule 6); `caller_idle_s` is read by
`get_caller_idle_s()`; `note_caller_call` and `_last_caller_call_at` take the place of the
draft's peer-named pair. The status key is `card_queue` (`queued_at`, `give_up_at`, ordered by
position with no position field, as on **waiting**), and the holder is named by the existing
`card_holders()`. `held_by` is not reused. `wait_reason` gains `card_queue` in
`TURN_WAIT_REASONS` (ADR-046 point 8). The bare word `lease` is not used.

**What this does to the idle swap amendment above.**

- **Replaced:** rule 3's idle test (`_in_flight == 0`), by rule 1 here; rule 3's "refused at once
  and does not queue behind the stop", by rule 4 here; rule 4's mark on a busy holder, its timeout
  that clears the mark and its clock, by rules 2 and 5 here; in the paragraph on two owner starts,
  "a claim whose child is still starting counts as idle" and the wait for a holder another start
  is swapping out, by rules 1 and 3 here.
- **Narrowed:** the settings paragraph's "read at the moment of each start" no longer covers
  `swap_wait_s`, read once at acceptance; "at most four holders" stays only as rule 13's safety
  net; in the last paragraph, a later round of a running request is served instead of meeting the
  refusal at the slot, and a stopped alias's next owner call queues instead of swapping.
- **Stands:** rule 1 (the setting off, and the default until the live check); rule 2 (a peer never
  swaps), which rule 7 here extends; rule 5 (a swap is said once, `last_swap`); the stop by
  `stop_by_hand`; and what a swap does not do.

**What the rules left open, decided** (Mike's call, team chat 2026-11-02).

1. **A holder stopping for another reason.** A request arriving while a holder is draining or
   stopping for a reason of its own (a settings save retiring it, a Stop) takes a place and waits.
   There is no refusal. This replaces the idle swap amendment's rule 4, last sentence, and the
   paragraph after it, while the queue is on; the refusal that names the state stays with the swap
   off and for a peer.
2. **A deadline that passes after the mark.** The stop is not cancelled and runs to its end. The
   request leaves with a refusal saying the holder is already stopping, and the next in the queue
   takes the card. Rule 5's "the holder is untouched" applies to a deadline met before the mark.
3. **The remainder is not `drain`'s timeout.** `drain(timeout)` reads a timeout as how long to wait
   before it stops the child anyway, so the remainder is not passed into `stop_by_hand`. The stop
   runs as its own task and the request waits for it on its own side, bounded by the remainder,
   so that neither the deadline nor a cancel reaches the stop; by rule 2 nothing is in flight by
   then. A refusal leaves no half-stopped mark behind on any path: the stop clears the mark when it
   ends, a stop that fails clears it and the holder serves again, and a holder a settings save
   retired stays closed with no mark naming anybody.

**As built, where the rules left a choice to the code, and open to its review.**

- **Who takes a place.** Any start that needs a claim, on an empty line too, so that two starts
  racing need no second path. A child already up with an empty line takes none. Places stand in the
  order they were taken, and a place's requests in the order they came, so "oldest" is the oldest
  in the place: a later round joining its own alias's place stands behind the requests already
  there. `_CardPlace` is a place, `_QueuedRequest` one request in it.
- **What is not new (rule 4).** A hold on this alias that stands before the line
  (`_stands_before_the_line`: older than the first place, or `admitted`, which a request becomes by
  passing through the line or at acceptance as a call of its caller's open window), with this
  alias's child on the card, a child still starting included. A not-new request whose child is gone
  needs the card, and takes a place. A request accepted after the first place was taken is not
  counted by the holder's busy test (`_busy_for_the_card`) or named by the refusal, so it cannot
  extend the wait it stands behind; a call already in flight always counts.
- **A wait on itself that is not rule 9's.** A request whose hold is on another alias with a child
  on the card, asking for an alias that does not exclude that one, does not queue while a place
  waits, because that place may be waiting for its hold; it meets the refusal of `_card_held_error`
  at once.
- **`_card_held_error` and strangers (rule 7).** While a place waits it refuses a supervisor holding
  no claim whose alias is not the first place's, with `CardQueueRefusal` and text that names nobody.
  The swap off skips this check. `_launch`'s second claim is skipped by the claim it holds.
- **The deadline's edges (rule 5).** A request with no hold, and a cloud agent's call on a local
  alias, are accepted at the place; a hold on the alias keeps its acceptance. A request with time
  left is refused at its deadline rather than marking a holder that just became idle. A request
  that came with no time left, which is one of `swap_wait_s` 0 or an old request that lost its
  alias between two rounds, is refused at once by a busy holder, and a holder that is not busy
  asks it for no wait: it swaps, bounded by `_STOP_WITH_NO_WAIT_LEFT_S` (60 s, a constant, since
  `_stop_child` asks a child for 20 s and then kills it). This keeps `swap_wait_s` 0 meaning "do not
  wait for a busy holder". The cost is on purpose: an agent turn older than `swap_wait_s` whose
  alias was stopped under it, and which finds the card busy, ends with the refusal at once; finding
  it idle, it stops the holder, and a Strata holder's parked conversations go with it. Both this and
  the constant are Mike's call, 2026-11-03.
- **What the 60 s limits.** `_STOP_WITH_NO_WAIT_LEFT_S` reaches the code only as `stop_bound`, which
  is the deadline for a request with time left and arrival plus 60 s for one that came with none.
  `stop_bound` applies in three places: `_within`, around `_clear_unfinished_start` and
  `_await_predecessor`; `_start_lock_for`, only while the alias is draining; and
  `_wait_for_the_stop`, for the stop this same request marked. So it is what lets go of whatever
  stands between a first place and its claim, not a wait for somebody else's stop: a request with
  no time left does not wait for a holder someone else is already stopping, and `_free_the_card`
  refuses it at once, as it refuses a busy holder and as `_wait_to_be_first` refuses it behind a
  place. In those refusals the setting is the bound and is what they name. A refusal at `stop_bound`
  in this phase names the 60 s and says it is the limit on waiting for a stop, not `swap_wait_s`.
- **One deadline on waiting, not on work (rule 5).** The deadline bounds what a request waits for
  and no more. `_start_lock_for`, when the lock is held and the alias is not draining, waits with no
  bound, apart from the in-line check and a supervisor that will not reopen. `resolve_binary` and
  `_plan_pin_start`, in threads under that lock, do not check it, and neither does anything after
  the claim. A request that takes longer than `swap_wait_s` from acceptance to its answer is
  therefore not a defect of this rule.
- **The waits between a first place and its claim.** The superseded child's drain and an unfinished
  start's stop are bounded by the same deadline, as is a stop of the alias's own child behind its
  `_start_lock`. A start of the same alias holding that lock is what the request wants and is
  bounded by `start_timeout_s`. A supervisor retired while its request waits raises the draining
  refusal and starts no child. A request on a supervisor that will not reopen (`_will_not_reopen`:
  draining and retired by a settings save, or draining with its provider closed) meets that refusal
  at once, on arrival and in any of these waits, instead of waiting out its deadline. One that a
  `stop_by_hand` or a swap's mark is on its way to reopening is waited for, by decision 1.
- **Stop (rule 12).** `leave_card_queue(conversation_id)` is called from
  `DpcAgentManager._interrupt_loop`, the seam the UI's Stop and Telegram's `/stop` share, and raises
  `TakenOutOfTheCardQueue` in the waiter; the agent loop ends the turn as `stopped`, with no closing
  model call. That seam is synchronous, and Telegram's handler runs on the loop its bridge was
  started on, so the line is changed through `_now_on_the_owner_loop`, as a hold is; a call that may
  be handed over answers nothing, so it returns nothing. A Kill cancels the wait through
  `_chat_unless_killed`. `take_card_queue_place(alias)`
  is called from `stop_local_engine` and not from `stop_by_hand`, so a swap stopping a holder leaves
  a place that alias has further back. `DpcLlmAdapter._chat` does not fall back to its text path on a
  queue refusal, which would stand in the same line again past a Stop or a deadline.
- **A refusal in an agent turn: `failed` is the accepted word.** Any other `CardQueueRefusal`, the
  `CardHeldError` of the swap off and `EngineDrainingError` (`engine_refusal_in`, read through the
  provider's `RuntimeError(...) from e`) end the turn with `turn_end` `failed`, so there is no new
  wire value and the UI mirrors stay as they are. No model failed, so the chat text is the refusal
  itself, without the "LLM error" label, without the wrap's "llamacpp_server streaming provider ...
  failed: CardHeldError:" and with no traceback from the loop, and `DpcLlmAdapter._chat` takes none of
  them to its text path. `refused` is not used: it is the host's refusal of a peer call, and a group
  post keeps a `refused` turn's text, where a `failed` turn posts `FAILED_TURN_NOTICE` and sends its
  text to the owner as a toast; this text names the owner's agents and chats. Any other
  `LlamaServerError`, a start that failed above all, is a failure and reads "LLM error".
- **Said once, whoever is left (rule 5 of the idle swap).** The swap is said by the stop's own task.
  A request that left during the stop gets "stopped '<holder>' to free the card for '<new>', which
  stopped waiting" in the same three places, because a stopped Strata's parked conversations are gone
  all the same.
- **Background calls (rule 10).** A call is one by its name, closed in `_BACKGROUND_CALLS`, each
  with what follows its skip. `self_reflection_confirmation`
  (`ConversationMonitor._is_self_reflection_conversation`, on the default alias with its 10 s
  `wait_for`) is not a background job of its own: it is a step inside a person's extraction, deciding
  `conversation_type`. It keeps the mark because it has no hold and its alias may not be the one on
  the card: unmarked it could take a place and stop a holder, dropping parked conversations, for a
  10 s classification. The mark says it neither swaps nor queues. A skip classifies the conversation
  as general, and that is a result, not a gap: the extraction goes on with the general prompt, and
  nothing recomputes the type in that pass. `skill_reflection` (`SkillReflector._do_reflect`) is
  started inside the turn by `DpcAgent.process`, once the request's hold has ended, so it has no
  hold; a skip ends that reflection, and the next long task with a used skill
  (`REFLECTION_ROUNDS_THRESHOLD`) reflects. `a_background_call(what)` marks a call as `serving_a_peer`
  marks a peer's, and `_wait_in_the_card_queue`, the queue's door, asks: a child already up with
  nobody in line lets it run, and anything else raises `BackgroundCallSkipped`, into
  `last_background_skip` {name: {alias, reason, what_follows, at}} in `local_engine_status`, in
  memory, and an INFO line. That class is a `CardQueueRefusal` for its "never retried"; the call never
  stood in the queue, so it is not to be read as a refusal of the line. The reason names the first
  place's alias when one waits, which outranks a child that is down or stopping.
  A place waiting skips it on its own alias as well, for two reasons: it would go ahead of one who
  waits (rule 4), and its `_in_flight` would make the holder busy, so that the line could not stop
  the very holder it waits for and that place's deadline would burn on a call nobody waits for. The
  door checks once, so a call past it may run after a place was taken: that it neither swaps nor
  queues holds always, and that the queue is empty at work is best effort. Past the door, `_claim_card`
  refuses the mark for a child that went, and `_draining_error` turns into the skip for a supervisor
  that began draining, at `call_slot` and behind `_start_lock` alike. One row per name, not one for
  all as `last_swap` is, so that one that skips often does not hide another. It asks nothing of a
  vendor's alias, a peer route or a model kept off the card (`on_the_card`), which have no card to be
  on, and nothing with the swap off, where the call goes on as it did. A request with a hold is not
  one, so rule 9 keeps the auxiliary calls inside a turn: `describe_image`,
  `_pre_analyze_image_for_agent`, a PDF page's vision pass, the snapshot summary and compaction.
  Holding an excluding alias such a call waits 0 s, by the refusal of rule 9; a cloud agent's, holding
  none, queues within `swap_wait_s`, inside the 300 s `describe_image` has as a tool.
  **Knowledge extraction is a request** (the owner's call, 2026-11-03): it starts at a person's word,
  End Session, `/extract_knowledge` or Extract Knowledge, all through `end_conversation_session`, and
  its main call, run locally, takes an `accepted_request` at `_execute_local_inference`. The automatic
  branch in `on_message` is dead: `auto_detect` is always false since CLEAN-4. **The vote and the group
  image are requests too.** `_ai_agent_vote_on_proposal` waits within `swap_wait_s`, and a refusal is one
  WARNING naming it and the alias, with no vote cast: it is not an error, and not "could not evaluate".
  `_describe_image_for_agents` carries a refusal to `vl_error` and `warnings` in its own words, without
  the provider's "vision failed". **A person's group image waits for the card about 30 s** (Mike's call,
  2026-11-03, on Johnny's objection), because the description goes into the message and the message
  waits for it: `GROUP_IMAGE_CARD_WAIT_S`, a constant, passed by `waiting_for_the_card_at_most` to
  the deadline `_QueuedRequest` sets; a `swap_wait_s` that is less still wins, and a refusal names
  whichever decided (`wait_limit`). The message then goes with its warning and no description, and a
  stop that request began runs on (decision 2). Other requests keep `swap_wait_s`, and the swap off
  changes nothing. The limit (`_CARD_WAIT_LIMIT`) is for a request with no hold, the only kind made
  inside it today: `send_group_image` is a command of the local API, so its 30 s count from the moment
  it joins the line. A request with a hold keeps its clock from acceptance, and a limit set over one
  would shorten its wait from that acceptance and never start a new 30 s. By rule 5 this bounds waiting
  and no work: a description running on the card is not cut, nor is a start past the claim, nor a start
  of the same alias it waits behind. So the 30 s do not bound `start_timeout_s`, by default 300 s on
  `llama-server` and 600 s on Strata, which then runs its smoke, and a message can still hang through
  that load and through the description. One load can be followed by another: `llama-server` steps down
  a KV rung after an out-of-memory start, and a new pin's failed first start rolls back to the retained
  pin, each under a `start_timeout_s` of its own. The owner's choice, 2026-11-03: variant A, as built, so
  the message waits for the work; a later "send now and attach the description" is left to a card. Sleep
  is rule 11's.
- **The status.** `local_engine_status` reports `card_queue` from `card_queue()`: one row per request
  in position order, with no position field, as **waiting** does, each `alias`, `agent_id`,
  `conversation_id` (null where the hold has none), `queued_at` and `give_up_at`, both wall-clock
  seconds as `last_swap`'s `at` is. `queued_at` is the time the request took its place, which is not
  its acceptance; `give_up_at` counts from the acceptance. `card_holders` beside it is every holder,
  which `card_holder` is not when `llama-server` aliases share. The Providers editor's Local engines
  block shows them under the card holder, read when it opens and on Refresh. It is a command of the
  local API, on 127.0.0.1, and on no peer's path: nothing else reads it, so these ids of the owner's
  agents and chats stay with the owner. Who stands on the holder's alias stays in the busy refusal.
- **The doors' words (rule 15).** A queue refusal is `CardQueueRefusal`, a `CardHeldError`, so the
  peer door already read it as `provider_unavailable`; the gateway answers 503 `provider_unavailable`
  with no `Retry-After`, from the first step and from inside `_call`. `_draining_error` raises
  `EngineDrainingError`, which `_engine_unavailable_cause` reads. At the gateway a draining refusal
  stays 502, which rule 15 does not name. `_who_stands_on` is only called for a request in the queue
  and a peer never is one, so agent and conversation ids do not reach a peer.

## Amendment, 2026-11-03 — where an alias's window comes from

Mike's order, 2026-11-02: when an agent's model is switched, everything must work. The case:
an agent switched to a `strata` alias Add Provider had made from the model files folder
answered its first message with a full context, 33,533 of 4,096 tokens, and called no model.
The alias had no `context_window`, its model name was in no table, and the engine it would
start held 131072. Zcode's review then found the refusal held only for local aliases, so the
guess was removed everywhere. This note replaces what point 7 says of an unset field and
rewrites nothing above.

1. **One reader per alias.** `LLMManager.alias_context_window(alias)` answers, in this order:
   the alias's own `context_window`; the engine behind it (`server_window`), which is the
   running child's `n_ctx` from `/props`, else the start the alias would make (`--max-context`
   for `strata`, `n_ctx` over the slots unless the pool is unified for `llamacpp_server`);
   and, for an alias with no engine of its own, `MODEL_CONTEXT_WINDOWS`. A caller that holds
   an alias asks here, because two aliases may serve one model name. A local alias never
   reads the table: what its child was started with is known to no list of model names.
2. **Point 7 stands for a value that is set.** The owner still sets `context_window` to the
   conversation size parking was measured at, it wins over the engine, and nothing writes it.
   Left unset, the engine answers, and `strataWindowHint` says so under the field.
3. **A window nobody knows has no number.** The table's `default` row and
   `get_context_window`, which answered 4096 for a model it did not know, are gone. An agent
   turn is refused by a sentence naming the alias and the way out (`window_unknown`). A monitor
   holds 0, the limit nothing measures against, so an AI chat goes to the model and the engine
   decides. A display says "window not known" and shows no percentage. A `PROVIDERS_RESPONSE`
   row carries `context_window: null`, as DPTP requires, a `/v1/models` row leaves the key out,
   and `model_max_tokens` stays off the wire.
4. **A number on disk is not a window.** `history.json`'s `token_limit` is what was true for
   the alias and the code that wrote it. The header of an agent chat, an AI chat read back from
   disk and a group read back from disk all read the alias; a chat its record sends to a peer
   keeps the last reply's number until the next reply.

Two named defaults stay and are not a window of this kind: the agent loop's 204800 for a run
with no session behind it, and sleep's 128000 for the synthesis budget. Whether they should
stand is the owner's question. (On a local engine sleep no longer reaches that default: see
the amendment of 2026-11-09 below.)

## Amendment, 2026-11-09 — sleep synthesis reads the engine's window

Mike's call, 2026-11-09. The 2026-11-03 amendment's point 1 puts the alias's own
`context_window` first. For the sleep synthesis on a local engine (`llamacpp_server`,
`strata`) the window is the engine's reported `n_ctx` (`/props`) instead, and every count
against it is the engine's own (`/tokenize`, or Strata's `/v1/messages/count_tokens`).
Synthesis is not a parked conversation: the alias's value is the size parking was measured
at, and a prompt sized by it reached a 261,120 window as 262,410 tokens. One gate decides:
the window less the answer reserve, with no margin factor; when the engine cannot be asked,
or one finding does not fit, the sleep stops and says so. The alias's `context_window` still
governs parking and live turns, and a provider with no engine keeps the estimate and its
margin.

## Amendment, 2026-11-03 — a size short of RAM

The owner's case, 2026-11-02: a new alias on IQ3_S, made by Add Provider from the run config
Strata's setup wrote, died on its first start, and the form had suggested a cache budget and
nothing else. Decided by Mike with Zcode and Ark; Johnny argued against the cap in point 2.
Nothing below has been run live: acceptance is a live start with no budget, short of RAM.

1. **One field is prefilled, `resident_budget_gib`.** It is host RAM, which is what the form's
   reading measures, and the engine's own warning names it. `--vram-reserve-mib` and
   `--kv-resident` are about the card, and a reading of free host memory says nothing of them. The
   engine's words on the reserve also pull both ways in one file: a smaller one at the 13:37 start
   below (line 36) and a larger one, 964 and then 1536, at the 13:59 start (lines 108-109). The form
   names both and derives no value. The owner's own `strata-iq3_s-lowmem.json` carries all three
   (24, 1536, 32768); the 13:59 start ran with the budget and a reserve of 700.
2. **The rule.** When the size's `ram_needed_gb`, compared as GiB as Strata holds its `ram_gb`
   against total RAM, is more than free memory now: the lower of free physical and free commit,
   physical alone where commit is not measured, less 9 GiB, rounded down to a whole GiB, held to the
   arena, and no value when under 8 GiB is left after the 9. It is a hint at overflow and not at
   every start: a size that fits free memory gets none. It is marked not measured, and never replaces
   a typed value or a budget the base config names. With 33.10 GiB free on IQ3_S it gives 24, and with
   60 GiB free 51 held to 46.
   - **The cap** is `experts_gb`, Strata's `arena_gb` as `list_strata_models` passes it
     (`tools/strata_mcp.py`), decimal GB: divided by 1.073741824 and floored as `setup.py:3353` does,
     50.3 is 46, beside the engine's "46.84 GiB". With no figure there is no cap. Add Provider writes
     the alias past setup, so setup's own cap does not reach it, and past the arena the form's "the
     rest" would not exist. Johnny's objection: `BUDGET_FLAGS` sets no upper bound because Strata
     clamps the budget itself, so such a number is not refused.
   - **The floor** is setup's `max(8, ...)`: a budget it would never write is not suggested.
   - **The 9 GiB** come from one worked example and from no rule. A budget of 24 started at 30.4,
     31.5 and 34.1 GiB available on the smaller IQ3_XXS (`strata-strata_flash-next-engine.log`,
     2026-10-25); on IQ3_S the lowest seen is 36.76 GiB, at the load of the 17:46 start on 2026-11-03
     (its engine log, line 389; Johnny's correction). At those three figures the hint
     gives 21, 22 and 25, and a budget of 31 started there too, at 30.8 to 33.7 GiB where it gives 21
     to 24. So it mostly comes out below what ran, slower and not proved safer, and it is not to change
     without a live measurement (Zcode).
3. **Counted from what is free now, as a stated choice.** Upstream counts from total RAM:
   `resident_budget_gib` in `setup.py:3348-3353`, for Unsloth's files,
   `max(8, min(round(ram) - 24 - ceil(kv_ram), arena_gib))`, which on this machine's 61.7 GiB is 38
   before a KV cache in RAM, where this hint gives 24 at 33.10 GiB free. The base differs on purpose:
   Add Provider knows what the other programs hold at that moment and upstream's setup does not ask.
   So this is not the engine's formula with another number, and its numbers will not match setup's.
4. **The engine's arena warning is quoted and not called the cause.** It is a warning, and the load
   goes on. `_arena_short_note` reads the engine log of this launch, one 8 MiB chunk of it, and a
   failed start's text carries the line and names the field, saying these are the engine's numbers.
   In `~/.dpc/logs/strata-qwen3.8-flash-next-iq3_s_Strata-engine.log`, read 2026-11-03:
   - The 13:37 start (line 7) had no budget. Memory guard: 34.3 GiB available (line 8). "the expert
     arena needs 46.84 GiB of RAM but 33.10 GiB is available (13.74 GiB short, after waiting)" (line
     27); it loaded them all (line 29); "expert cache auto: 0.00 GiB free" and "no VRAM is left for the
     expert cache" (lines 35-36); "session is up" and "the embedding buffer failed" (lines 39-40), and
     the launcher saw exit code 1.
   - The 13:59 start (line 47) had `--resident-budget-gib 24`. Memory guard: 46.9 GiB available (line
     48), and 45.44 GiB at line 79; "expert cache auto: 23.54 GiB free" (line 67); it came up.

   The two differ in the budget, in 12.6 GiB of free RAM and in 23.54 GiB of free VRAM, and the last
   words of the one that died are about VRAM. So this log cannot separate the budget's effect from the
   freed memory, and the card may have mended a symptom. Only the live start says.
5. **What the budget costs, in the engine's words.** At 24 the same file says "1413 of 1413 lendable
   slots (100%) will read their experts from the pack during a long prompt", "Raise
   --resident-budget-gib" (line 92), and "prompts can read ~3x slower than with --prefill auto" (line
   93). So the hint is the slower way to run, and its text says so: "reads any others from the model
   files, which can be slower".
6. **`--mmap-experts` is one observation.** The warning advises it together with the budget. The 13:59
   command carried no `--mmap-experts` and the engine logged "experts via mmap (--mmap-experts; the
   GGUF shards in place, no experts.bin)" (line 60). DPC sets no such flag. The live start has to look
   at this too.
7. **The words when nothing is suggested.** "Close other programs" is said only where that can help.
   Where the size needs more than this PC's total RAM, or Strata's `on_this_pc` opens with "does not
   fit", the text says closing programs will not make it fit and asks for a typed budget.
8. **Off Windows.** `local_engine_status` answers `free_commit` with `read_free_commit_or_physical`,
   as `list_processes` does, because this hint needs physical memory there; the two commit fields stay
   null. A null is then a reading that failed, on every OS. The Strata supervisor keeps
   `read_free_commit`, which is None off Windows.

## Amendment, 2026-11-06 — the pin moves to Strata v0.2.11 (engine 0.1.57)

*(Mike's order to update the engines together. Read in the release's own Control packages of v0.2.9 and v0.2.11; the start smoke is recorded on the point-5 card.)*

1. **One engine per install is now DPC's rule alone.** The amendment of 2026-11-01 gives Strata's updater as a reason: it kept only the new and the previous engine. In v0.2.11 it removes none (`strata_control/updater.py:1217`). A start still picks within a family by newest, never by build (`engine_component.py:1145-1152`), so one folder per family stays.
2. **The launcher takes Strata's shared activity lease** (`serve/server.py` `main`) in the per-user runtime folder. DPC's updater still does not see a launcher of ours, because it runs with a throwaway home; Strata's own updater, run with the real home, now does.
3. **The launcher decides more at a start, from a start config with no key for it**: the drafter (`engine_args`, Auto: DFlash2 when its pack is in the model data folder, else the MTP, with one line) and the picture reader (`apply_vision_defaults`: on one NVIDIA card the reader starts with the first picture, and a reserve of 700 or none becomes 2048). DPC's start config writes no `drafting` key and no `elastic` key.
4. **The build commit may be spelled in full.** The Linux sycl engine's `build_id` carries all 40 digits; the identity check takes the commit as the row has it or in full, and a test runs every engine row of the feed through it.

## Amendment, 2026-11-06 — the drafter and the picture reader get alias fields

*(Mike's call, 2026-11-06; the names went to the team room. This replaces the last clause of point 3 of the amendment above: DPC's start config now writes these two keys when the alias chooses, and still writes neither when it does not.)*

1. **`drafting`: `mtp` or `dflash2`.** Strata's own run-config key (`serve/server.py` `engine_args`, `strata_control/drafting.py`), written at the root of the start config. It is not a `model_params` key (Strata's page does not edit it, so `model_param_error` still refuses it there) and not an engine flag. `off` is not offered: it is the same as `model_params.mtp = false`, and one thing gets one key.
2. **`vision_elastic`: `true` or `false`.** Written as `elastic` inside the start config's `vision` block, never at the root. `true` starts the picture reader with the first picture, `false` with the engine. A set value on an alias whose start has no `vision` block (the base config has none, or `vision` is off) is refused on save and at the start. It frees VRAM only on an engine that has `--vram-elastic`; Strata starts the reader anyway where the engine has not, so DPC writes no `--vram-elastic`.
3. **No empty choice.** Each dropdown lists `auto` first and shows it by default. `auto` is the key's absence: nothing is written, Strata decides, and a base config's own `drafting` or `elastic` stays. Right now that is the picture reader on demand on one NVIDIA card, and the drafter DFlash2 when its pack is in the data folder, else the MTP.
4. **What the engine chose is shown.** Strata puts `drafting` (`mtp` / `dflash2` / `off`) and `drafting_note` (plain words, or empty) under `engine` in its `/metrics` once the engine is ready (`strata_control/drafting.py` `engine_info`, merged at `serve/server.py` READY). `local_engine_status` carries them in a running alias's `metrics` as `drafter` and `drafting_note`, and the providers panel prints them.

## Amendment, 2026-11-06 — a peer may swap an idle card when the owner allows swaps

*(Mike's call, 2026-11-06. The case: a guest on another node asked for a stopped Strata alias while an idle `llama-server` alias held the card, `swap_when_idle` was on and nothing was queued, and the peer was refused with the blanket "a peer's request does not start it now". This replaces rule 2 of the idle-swap amendment of 2026-11-02 ("A peer never swaps") and the first half of the first sentence of point 7 of the card-queue amendment ("A peer never swaps and never takes a place"). Everything else in both stands, "never takes a place" included.)*

1. **The rule.** If there is no queue, a peer may swap the same way as the node's owner, if the owner has that option on. With `swap_when_idle` on, a peer's request for a local alias that is not on the card (a call served to a peer, or the start `start_for_peer` begins for one) stops the holder through the owner's path (`_free_the_card`, `stop_by_hand`, registry `stop_kind` `swap`, one notice with the parked-conversations words) when, and only when, the owner's own conditions hold: `_CARD_QUEUE` is empty, and no holder is busy by rule 1 of the card queue (a child starting, a call in flight, an `accepted_request`, a caller in its `caller_window`) or already stopping. `_why_a_peer_may_not_swap` is that test.
2. **Same stretch.** The test is asked again inside `_free_the_card`, in the synchronous stretch of the drain mark, with no `await` between the last look and the mark. The peer policy's look beforehand (`StrataProvider._peer_start_refusal`) only decides whether to begin the start. No new lock.
3. **No wait, no place.** The swap's request (`_a_peers_request`) has no time to wait, so a busy holder is refused at once, and it is in no line: its place is never put in `_CARD_QUEUE`. An owner request that arrives during the peer's stop takes the first place of an empty line and the card; the peer's claim then meets the place that waits and its start is refused. The peer is never ahead of an owner request, and an owner request is never made to wait behind a peer's place.
4. **What the peer is answered.** For a Strata alias nothing changes on the wire: the policy begins the background start and answers `provider_loading` ("loading", DPTP §3.4) while the swap and the start run, one start at a time, and a guest agent waits on that code. The free-VRAM bar of point 9 is not read for a start that swaps, because the holder is what takes the VRAM; the owner's own swap reads none either. A call served to a peer on a `llama-server` alias starts in-line as it always did, and its swap is part of that wait.
5. **Where it still refuses, and what it says.** The switch off: today's refusal, "this node's owner has not turned on swapping local models for an idle holder". Anything queued: "a request on this node is waiting for the card, and a peer's request does not go ahead of it". A busy holder: "it is busy, and a peer's request does not wait for a busy holder". A holder already stopping: "it is already stopping". All are `provider_unavailable`, never retried, and name no agent, conversation or caller.
6. **Not changed.** A peer-marked call still never starts a Strata launcher itself (point 9); a peer still never asks for or fetches a missing pin; a launcher stopped after an engine death stays the owner's to reset.

## Amendment, 2026-11-11 — a providers save does not refuse a call that has not started

*(Mike's call, 2026-11-11. The case, 2026-11-10 23:34-23:37: an agent's call waited in the card queue for a Strata alias with no child yet, another alias holding the card; a providers save replaced that alias's supervisor, `_retire` drained the old one, and the waiting call met `EngineDrainingError` ("is draining; new calls refused") and its turn ended on round 1. This replaces the sentence of the card queue's "as built" notes that a supervisor retired while its request waits raises the draining refusal and starts no child, for a supervisor with a successor of the same engine; and it adds the case that the provider's adoption did not name.)*

1. **No child, same flags: adopted.** A reload leaving the flag signature equal adopts the registered supervisor of the same class whether or not a child is up. A supervisor with no child has nothing to drain, and the calls waiting on it stay where they are. Non-flag keys are merged as for a running child. The exception is a Strata launcher with a latched `failure` (point 3): it is not adopted, a save builds a fresh supervisor for it as before, and nothing can be waiting on it because a latched failure is refused at once.
2. **Flags changed, same engine: the waiting call goes to the successor.** The provider tells the old supervisor which supervisor replaced it (`superseded_by`, beside `supersedes`). A request that has not started on the old one is not refused: while it waits for its place the retired supervisor's refusal is suppressed, and when `_start_if_needed` meets the drain the request goes on under the successor (the last of a chain of saves) with the place it holds in `_CARD_QUEUE`, so nothing behind it goes ahead and nothing it had ahead of it is lost; its deadline is the hold's, unchanged (rule 5). A request that had no place (the swap off, or a child already up) or had already passed the line asks the successor afresh. The provider the call came through points at the successor from then on (`where_calls_go`), so the port, the slot and the props are the successor's. The successor still waits for the old child's drain before it starts (`_await_predecessor`): the card is never held twice.
3. **A call in flight keeps the drain.** A call that holds a slot on the old child finishes there; new work on the old supervisor is refused; the child stops after the last slot. Nothing about this changes, and its provider stays on the old supervisor.
4. **A changed engine still refuses.** An alias whose type changed (`llamacpp_server` to `strata` or back) keeps the old refusal for what waits on the old supervisor: the waiting call belongs to a provider that speaks the old engine's dialect and cannot be pointed at the other engine's launcher. The save logs a WARNING naming the count.
5. **What the log says.** One INFO line at the save, naming the alias, whether a child of it was up and how many requests were waiting for the card, in place of the nothing a save with no child used to leave; one INFO line for each request passed on.
6. **Not covered.** A call whose child was mid-launch when a flag-changing save arrived is cancelled by the drain's stop and is not passed on. A deleted alias's supervisor with no child is dropped from the registry without a drain (`retire_absent`), and what waits on it is not looked at here.

Names kept from the code: `supersedes`, predecessor and successor are the supervisor's own words; no new term entered the vocabulary beyond the three methods `superseded_by`, `where_calls_go` and `waiting_in_the_card_queue`.

## Amendment, 2026-11-13 — a peer's call parks when the cache has room

*(Voted 2026-11-12/13 by CC, Zcode and Johnny as variant C2 of `ideas/peer-cache-variant-c-2026-11-12.md`; Ark's vote is pending. This replaces the first sentence of point 9 ("Every call served to a peer is sent with checkpointing off") and the part of its known cost about parking. Everything else in point 9 stands. The owner keeps two things: the alias's `conversation_cache_slots`, and whether and when the request below goes to Strata's maintainer.)*

1. **The rule.** A call served to a peer goes out with checkpointing on when, read right before the call, the conversation cache has room for it. Otherwise it goes out with checkpointing off (`strata_checkpoint: false`, the engine's `ckpt=0`) as before.
   - Room means four things together, read from the launcher's `/metrics` `conversation_cache` and the host's free physical RAM (`ullAvailPhys` on Windows, `MemAvailable` elsewhere), both fresh at that moment:
     - the parked count, less this peer's own entry, plus two (our live conversation and the peer's snapshot), plus one slot kept free for our next new conversation, is within `slots`;
     - `bytes` plus the peer's estimated snapshot is within `budget_mib`;
     - free physical RAM less that estimate stays at or above the engine's `conversation_cache_min_free_mib`;
     - no other peer holds the one guest entry.
   - The estimate is a fit of the engine's own parked sizes on this alias (a fixed part plus a part per token; the fit, its source and its date are `GUEST_SNAPSHOT_*` in `strata_supervisor.py`, since this record carries no figure that changes), applied to the peer's next call: its last call's prompt and reply plus the growth between its last two calls, or the last times 1.15 when only one is known. A peer with no earlier call has no estimate, and its call is off.
   - A `/metrics` that does not answer within 3 s, or answers without `conversation_cache`, means off.
   - The decision is taken in the provider's last await before the wire body is built (`StrataProvider._ensure`, `StrataSupervisor.guest_checkpoint`), so all four request paths carry it.
2. **The switch.** The `strata` alias field `guest_conversation_cache`, true or false, on when absent. Off sends every call served to a peer with checkpointing off and reads no `/metrics` for it. It changes nothing the launcher is started with, so a save does not restart it.
3. **One guest entry.** One peer at a time may hold an entry. It is reserved when the decision for a call is on (`guest_checkpoint`), not when the call finishes, because a call cancelled before its last usage chunk records nothing and the engine may hold the peer's session all the same; a finished call with checkpointing on renews it and adds the call's size to the peer's estimate. It is dropped when that peer's next call goes out off (that call takes the entry back if it matches and parks nothing after it), when the engine's eviction counter moves (the entry may be the one evicted), when its holder has made no call of its own for `GUEST_ENTRY_IDLE_S` (15 minutes; the engine has no time limit on a parked conversation, so without one a silent peer would keep every other peer off until an eviction, and the feature exists to drive evictions to zero), and at a new launch. The clock is the monotonic time stored with the entry, set at the reservation and refreshed by each of the holder's calls with checkpointing on; the check is in `_guest_entry_holder`, and a drop for idleness is one INFO line naming the peer and the seconds. A reservation for a call that was decided on and never sent (or never finished) is held by the same rules: it keeps other peers off at most until the idle limit, an eviction or a new launch. The snapshot of a peer dropped for idleness may still sit in the engine's cache; the release frees the right to park, not the slot, so the engine's own eviction still reclaims it.
4. **What the engine does with it.** With checkpointing on, the engine keeps the peer's session after its turn and parks it at the next switch, as it does ours. The peer's next call restores it (a restore of a few hundred milliseconds in place of a cold read of its whole prompt) and takes it out of the cache. A call sent with checkpointing off still takes back an entry it matches, and parks nothing after it, so a peer's entry lasts at most until that peer's next call.
5. **What it does not promise.** Strata evicts the conversation parked longest ago (`evict_oldest`, by slots, by budget and by the RAM floor), and has no per-request way to say "evict this one first".
   - A peer's snapshot is usually the newest entry, so if we start more new conversations after it than the slot kept free, the next eviction takes one of ours, not the peer's.
   - The rule keeps the peer out of room we are using when its call goes out. It does not reserve the room afterwards.
   - The full rule needs an engine key for an evict-first entry (`park=spare` in the ideas file, the mirror image of `pin`), and per-entry sizes in `/metrics`. Until a Strata release has them, this paragraph is the known cost.
6. **The gateway is not a peer.** ADR-041's gateway calls are this node's own and keep checkpointing on.
7. **What is said.** Two INFO lines, both naming no agent or conversation:
   - one per call served to a peer: on or off, how long the `/metrics` read took in milliseconds (that await sits before every peer call on an engine that serves one request at a time; on a timeout or an error the elapsed time is printed too), the parked count against `slots` with the peer's own entry and the slot kept free, MiB held and the estimate against the budget, free physical RAM and the estimate against the floor, the estimate with what it was made from, and the reasons when off; it names no node but the peer;
   - one whenever the cache's `parked`, `slots`, `bytes` or `budget_mib` change, from the engine watcher's poll (`_note_cache_view`), so a day's occupancy can be read from the log before the rule is tuned (Johnny's condition: record before deciding).
8. **Known cost.**
   - A peer's entry takes one slot and one snapshot of host RAM between its calls, for at most `GUEST_ENTRY_IDLE_S` after its last call (the engine keeps the snapshot until it evicts it).
   - `/metrics` `conversation_cache` reflects the engine's last cache event, so the view read just before a call may not include a park or restore still in flight; per Zcode's review this is mostly conservative (unverified here).
   - With `slots = 2` the rule never applies: our live conversation, the peer's snapshot and the slot kept free already need three. It is meant for a `conversation_cache_slots` of at least 3, which is the owner's setting (point 7), not a default. Until then the INFO lines say why it is off.
9. **Reading the first day.**
   - The acceptance criterion for the first day is read against `budget_mib` (the MiB budget), not against physical RAM: on this node the RAM floor (`conversation_cache_min_free_mib`) was 1000 MiB with about 9 GiB free, so the floor never binds and cannot explain a "no fire".
   - The engine has a third mechanism besides park and evict, a partial borrow from a parked entry that "stays parked" (engine log lines "borrowed ... which stays parked"); it is known and not used by this rule.
   - Johnny's note on counting (accepted, 2026-11-13): count decisions per engine start, and leave out the first decision after a launch, which is off by construction (no earlier call of the peer to estimate from, and the entry is empty). "New evictions" are read from the INFO line `engine eviction counter +N since the last call` (or the same words ending a `conversation-cache loss(es)` line), which `log_restore_refusals` prints after a call from `_new_cache_evictions`; the counter it reads is advanced by both the engine watcher's poll and the decision call (`guest_checkpoint` runs `_note_cache_evictions` on the `/metrics` it has just read), so an eviction can be counted by either and is printed once. The occupancy line also carries `evictions=` as a running total. Read that line, not the decision line, for evictions; read the decision line for on or off.

Names: `guest_conversation_cache` is new (GLOSSARY). Its "guest" is the peer a call is served to, not the guest ceiling of ADR-041 D5 (`vendor_request_quotas`, `guest_vendor_quota.py`).

## Amendment, 2026-11-13 — KV snapshots: the live conversation is saved to disk at a stop and restored into an empty engine

*(Accepted 2026-11-13 (Mike, DPC Project chat), after review round 1 by Ark, Zcode and Johnny; from `ideas/parked-conversations-to-disk-plan-2026-11-13.md`. Ark's round 2 came after Mike's call and is folded where it says "(Ark, round 2)": it changes mechanism text, not a decision. Mike's calls, DPC Project chat, 2026-11-13: the noun (point 1); the folder is chosen by the user in the alias's engine settings, on any disk; a reset or a voted new session deletes the conversation's snapshots (point 7); peers get a design, not a "never", with a TTL of their own (point 11); the peer must be told (point 11); the llama slot-wipe is part of this work (point 9); and, later the same day, the names, defaults, peer TTL, renaming of the shipped "session files" text, missing-disk behaviour and the place of slice 2 as listed under "Decided" at the end. The card is `A-SWAP-LOSES-THE-PARKED-CONVERSATIONS-AND-NOTHING-SAVES-THEM-TO-DISK-WITH-A-TTL` (gitignored `backlog.md`). This replaces, in this record: the last sentence of point 7, "DPC does not call save or restore itself yet (Mike's call, 2026-10-23: first a measurement)"; in point 5 of the amendment of 2026-11-02 on the idle swap, the parenthesis "`llama-server` parks nothing" and the `last_swap` schema {from, to, from_engine, to_engine, at, parked_dropped}, which point 10 extends (the comment above `_LAST_SWAP` in `llama_server_supervisor.py` changes with it). The card queue amendment's "a child that parks nothing" stays: it speaks of parked entries, which do not change. In point 7, in the amendment of 2026-10-25 and in ADR-046 point 7.3, the word "session files" reads "KV snapshots" from here on; those texts are not rewritten (the shipped UI, example and refusal texts are renamed, see "Decided"). Built: nothing yet.)*

**The case.** Every stop drops what the engine holds. Strata says so (`DROPS_PARKED_ON_STOP = True`); `llama-server` says it parks nothing (`DROPS_PARKED_ON_STOP = False`, the idle-swap amendment's "`llama-server` parks nothing"), yet its live slot and its `--cache-ram` host cache die with the process too, and neither is counted. The swap notice undercounts on both engines: `_note_swap` counts only parked entries, so a stop with nothing parked says nothing although the live conversation went. CC counted 34 swaps between 2026-11-02 and 11-12 in the six `dpc-client.log` files (24 Strata to Strata, 10 Strata to `llama-server`); in 28 the swapped-out alias came back by a later swap, median 5.5 h later, 23 within 24 h, 11 within 1 h. Whether anything was live at each swap the log line does not say.

**What was measured,** 2026-11-13, per the card's entries of that day (raw data in the CC session scratchpad, `slot-save-2026-11-13`, `drafter-and-llama-save-2026-11-13`, `precode-checks-2026-11-13` and `window-256k-2026-11-13`; CC spot-checked the rows the plan quotes). Strata v0.2.11 / engine 0.1.57 with `drafting: mtp`: a 250K conversation is a 4.05 GB file, saved in 2.1 s and restored in 3.2 s from SSD, 37.8 s and 43.3 s from HDD, against a 67 s cold read; a file saved by one launcher restores in a fresh one and the next request reuses the whole prompt. `llama-server` b12110, alias `qwen3.8 27b` with `--mmproj`: 6.10 GB, saved in 5.5 s and restored in 4.5 s from SSD, against a 430.7 s cold read; HDD not measured. A file has a fixed floor as well as a part per token (Strata about 236 MB plus ~15.3 MB per 1K tokens; `llama-server` about 0.47 GB plus ~22.6 KB per token, a two-point fit). The engine refuses a file whose model or config fingerprint differs, the drafter included. The 27B's cold prefill falls with depth (1,840 tok/s at 32K, 942 at 128K, 579 at 249K); Strata's barely moves (4,010 to 3,697).

1. **The word.** The file is a **KV snapshot**, in full a **conversation KV snapshot** (Mike's word, 2026-11-13). What it keeps is the conversation's engine state, not the conversation: the history lives in `history.json`, so "saved conversation" names the wrong thing (his ground, same day).
   - The file holds the KV cache, the context checkpoints, the draft state and the token identity (Strata's `DETAILS.md`: "engine state and token identity, not a chat export"; `llama-server`'s log lines `save_slot_ch` and `save_slot_dr`). Ark and Johnny noted that it is mostly checkpoints and draft state rather than attention KV; the help text says so, the word stands.
   - Strata's own docs say "session file" for this disk file and "snapshot" for a conversation the cache parks in RAM; neither is our word for it. The GLOSSARY row names both, and the session archive.
   - The field stays **`slot_save_path`**, the engines' own key (a top-level key of Strata's start config, `--slot-save-path` on `llama-server`). Its label on both engines becomes "KV snapshots folder (slot_save_path)", with a Browse button on both, and its help text: "A snapshot of the conversation's engine state (KV cache, checkpoints, draft state) is kept here when the engine stops, and brought back when that conversation returns. Empty: off." Strata's label was "Session files folder (slot_save_path)", `llama-server`'s "Slot save path"; the test that pins the old label (`strataSessionFilesField.test.ts`) changes with it. The other texts that say "session files" for this folder are renamed in the same commit (see "Decided").

2. **What is saved: one live conversation per engine, the owner's.**
   - **Strata** saves slot 0, the only slot its session API saves: the live conversation. Parked entries are not saved: DPC has no identity for an entry and the API has no way to save one. Asking Strata's maintainer for that (upstream #5500 is the closest, an open PR) stays Mike's, as the amendment of 2026-11-13 on peers keeps it.
   - **`llama-server`** reduces to the same shape (pre-code checks 2 and 3). No response and no `/slots` field names the slot or the conversation, and with serial requests the slot that did not serve last is empty (`n_prompt_tokens 0`); its conversation sits in the `--cache-ram` host cache, which the slot API cannot save. So DPC reads `/slots` and saves the one slot with `n_prompt_tokens` above 0. None: nothing is saved. More than one (two calls ran side by side): nothing is saved and the skip is logged, because DPC cannot tell which slot is whose.
   - **Whose it is** is read from one record per alias of the last call that reached the engine, with its conversation key, its **caller kind** (`agent`, `owner` for the owner's own chat, `peer` or `gateway`; Zcode, round 1; the four words are the record's own, see "Named in the group naming check"), the hash of the request's message prefix (point 5) and its place in the order the calls reached the engine (a sequence number, not the wall clock: Windows ticks in 15.6 ms and NTP can step the clock back; independent review, 2026-11-13). The kind cannot be read in `drain`: `peer_being_served()` there sees the context of whoever started the stop (the swap's stop task inherits the waiting request's context, `_wait_for_the_stop`), and `None` on Stop engine, a retire or a rebuild; and `_CardHold` keeps no caller (the gateway's `caller` is used once, in `_hold_enters`, and dropped).
   - **One hook, in the provider's `_ensure`** (Ark, round 2), for every call and on both engines: it is the one place every caller passes (the base `_ensure` is awaited by all five `llamacpp_server` call paths, and `StrataProvider._ensure` already reads `peer_being_served()` there for `guest_checkpoint`). The owner's key (agent and conversation id) comes from the hold context (`current_card_hold()`), a peer's from the peer-call context (`serving_a_peer`), a gateway client's from its caller, which `_CardHold` does not carry today, so the code commit gives that kind a carrier. **The call that reached the engine last wins**: owner, peer and gateway calls share one line (the gateway is handed the peer inference lock, `inference_lock` in `gateway.py`), and one hook at a call's end beside another at its start would make "last" mean two things. Two steps make "last" mean what the slot holds: `_ensure` notes the call as pending (`note_engine_call`), and the call becomes the alias's record only when it is sent, inside `call_slot` with no await before the request goes out (`mark_sent`). A call held up by an await after `_ensure` (a peer's call waiting on `guest_checkpoint`) can reach the engine after a later-noted owner call; ordered by noting, the peer's prompt would be saved under the owner's key. A call cancelled or refused before it is sent never becomes the record (independent review of the wiring, 2026-11-13). Nothing is written in `_hold_moves_to`, `_hold_enters`, the borrow steps or at a hold's end, where a card-borrow would write a record for an alias the request only stood on and a peer's call, which has no hold, would write none (Ark, rounds 1 and 2; point 12 keeps the borrow unchanged).
   - **A peer's call holds no card.** `p2p_coordinator` serves it inside `serving_a_peer` and calls `llm_manager.query` or `query_messages` directly: `card_held_for` is in `gateway.py`, `inference_orchestrator.py` and `dpc_agent/agent.py` only, and not in `p2p_coordinator.py` (Ark, round 2). The gateway's comment "Peers never wait in that queue" is about the order of a gateway call and is not the evidence. On a `llama-server` alias a peer's call has no hold, no queue and no policy at all: `_local_engine_refusal` returns None for any alias that is not Strata. That is where slice 1 reads `/slots`, finds slot 0 occupied and would see a peer's prompt with no marker on it. A hold's end alone would never name a peer, and slice 1 would write the peer's prompt to disk under the owner's key; the record written in `_ensure` prevents that, also when the peer's call ended more than `caller_idle_s` before a stop.
   - The prefix hash of point 5 needs the request's messages, which a hold never sees; `_ensure` takes it for the same call and writes it in the same record.
   - **Slice 1 saves owner conversations only:** an agent's turn (`agent_id` and `conversation_id`) or an owner's chat with a `conversation_id`. The save is skipped, and logged, when the record's caller kind is peer or gateway, when a caller still has a call inside its `caller_window` (`_callers_in_their_window`), or when the record names no conversation. On Strata such a skip also means the owner conversation that was live before the peer's call is not saved; the log line says so.

3. **When.**

   | stop | saved | where |
   |---|---|---|
   | swap | yes, slice 1 | in `drain`, after `_wait_idle` and before `stop()` (`_stop_for_the_swap` → `stop_by_hand(why="swap")` → `drain`) |
   | Stop engine | yes, slice 1 | the same `drain`, through `stop_local_engine` → `stop_by_hand` |
   | a settings save that retires a child (`_retire`) | yes, slice 1 | the same `drain`; the successor's pre-launch sweep (point 6) deletes a file the changed alias cannot read |
   | provider rebuild (`close()`, `drain(timeout=120.0)`) | slice 1b | the same `drain`; but `close()`'s only production caller is `LLMManager.shutdown`, after `begin_shutdown`, so slice 1's "skip while stopping" makes this row the backend stop below (corrected 2026-11-13, Johnny) |
   | backend stop | slice 1b | normally the same `drain`: `LLMManager.shutdown` closes each provider first and calls `stop_all_supervisors` after; only a retiring alias and a child no provider holds reach that function's direct `stop()` |
   | Strata idle unload | yes, slice 3 | DPC's own idle timer, below |
   | engine death | never | nothing is left to save |

   - A save runs only when `_in_flight` is 0 at that moment. The ground is Mike's rule of 2026-08-22, quoted in `drain`'s docstring: a settings save "must never cut a generation that is already running". `drain` already refuses new work (`_draining`), so nothing enters during the save; a `drain` that timed out with calls still in flight saves nothing. The gate covers a peer's call too: it goes through the same providers and the same `call_slot` (Ark, round 2).
   - `stop_by_hand` drains inside `async with supervisor._start_lock`, so a save also delays a start of the same supervisor queued behind the stop, and pending starts wait on retiring children. The bound of point 4 caps that.
   - **Backend stop, slice 1b.** Two routes. The normal one is `close()` → `drain`, with the process registry already stopping (`begin_shutdown` runs before the providers close): slice 1's save in `drain` is skipped while `get_registry().stopping` is true, so the exit is not spent on a write before 1b is ready. The other is `stop_all_supervisors`: for a retiring alias it cancels the drain task and calls `stop()` directly, and for an active alias it takes the supervisor out of the registry before `await supervisor.stop()`, inside a `try` that logs and moves on. The save there gets a `try` of its own: in the existing one a save error would skip the stop and leave a child nothing can reach (Ark, round 1). Skipped while `_in_flight > 0`, logged: a generation cut at exit would fail the prefix check anyway. It runs from the stopping thread and ends before the backend's job can close, as the amendment of 2026-10-25 and ADR-046 point 7.3 already reserve. It waits for a reading of how long the UI and the OS give the backend at exit.
   - **Strata idle unload, slice 3.** Today the launcher unloads by its own timer and DPC learns of it on its next `/metrics` poll, with no hook before the unload. In slice 3 DPC owns the timer: it stops writing `idle_unload_s` into the start config, and after the same seconds its supervisor saves and then sends `POST /unload`. `_unload_expected` changes with it: it returns true today for `idle_unload_s > 0` or `lazy_load` true, and with DPC's timer and `lazy_load` false neither holds, so the function, not only the timer, must count DPC's own unload or it reads as a death. **In slice 1, two clocks act on one engine** while `idle_unload_s` is still written into the start config (Ark, round 2, read in the Strata repository copy, not the pin): a save moves the launcher's own idle clock (`last_request_at`, "the idle unload counts this as activity"), and an idle unload that lands between the idle check and the save leaves nothing loaded to save, so that save fails and is logged as not saved.
   - **Each path names its stop.** Only the swap passes a `why` today (`DEFAULT_STOP` is `ui_stop`; `_retire` and `close()` pass none). The save outcome is recorded by the save step itself, not read off the registry's stop mark.

4. **Bound and failure. A save never blocks a stop and never fails one.**
   - **The bound** is the smaller of the per-file write ceiling (the constant `KV_SNAPSHOT_WRITE_CEILING_S`, 60 s) and what is left of the waiting request's stop bound **when the save starts**, less the stop still to come. The waiting request watches the whole `stop_by_hand`, save included (`_wait_for_the_stop` refuses once `_swap_clock() >= request.stop_bound`), so a share fixed at the mark would let save plus stop overrun it (Ark, round 1). The stop still to come is the alias's last measured stop time, or before one is measured the stop's own bounds: 20 s for the process (`_stop_child`) and on Strata up to `vram_release_timeout_s` (30 s) of the VRAM release check. Those are not a ceiling: past the 20 s, `_stop_child` kills the child and awaits `proc.wait()` with no timeout, and a child that cannot be stopped keeps the card (`stop`). The save does not lengthen that path and cannot count it.
   - With no waiting request (Stop engine, a retire, a rebuild) the ceiling alone applies. A request's stop bound is its own `give_up_at`; 60 s (`_STOP_WITH_NO_WAIT_LEFT_S`) replaces it for a request that came late and for a peer's swap, so on Strata a peer's swap leaves the save about 10 s at best, often nothing, and the save is then skipped. Before a future peer refusal is blamed on the save, today's log is read for the refusal `_wait_for_the_stop` raises ("is already stopping to free the card for it, and … ran out") on peer swaps caused by the stop time alone.
   - **Skip on estimate.** The estimate is the live conversation's tokens (the engine's own count for the slot, `n_prompt_tokens` from `/slots`; on Strata after an unload, which empties that count, the record's last prompt and reply; corrected 2026-11-13, Johnny, Zcode) times this alias's last bytes per token plus the floor, at this folder's last measured write rate. An estimate over the bound is skipped without trying. A folder with no measured rate yet is tried once within the bound, and that write sets its rate.
   - **The size threshold.** A conversation is saved only when its estimated save plus restore is shorter than its estimated cold read: this folder's write and read rates, a file of floor plus the part per token, and the alias's own cold read **at that length** from the usage ledger's cold rows. Per alias changes the result, not only the form: the ledger's cold medians are 3,755 tok/s on the Strata alias and 1,805 on the 27B (`cached_tokens` 0, prompt ≥ 5K, `counts_source` engine; n = 130 and 22; Johnny's reading, reproduced by CC on 2026-11-13). n = 22 is small: the 27B's p10 of 1,303 alone moves its threshold by a third. At that length, because the 27B's rate falls with depth (above). By the 2026-11-13 figures (CC's arithmetic, not a measurement): Strata from SSD breaks even at about 1.2K tokens; Strata from HDD never at a stop (save plus restore ~0.31 s per 1K tokens against ~0.27 s of cold read, floor apart); `llama-server` from SSD at about 1.5K (1.1K at the p10); `llama-server` from HDD is not measured, and at Strata's HDD rates it costs about 9 s plus 0.45 s per 1K tokens, which pays from roughly 50K tokens up, where the 27B's cold read slows. Both sides count at a stop, because the save is paid by the request waiting for the card. The idle save of slice 3 costs nobody a wait, so there only the restore counts (on Strata HDD, break-even about 24K tokens).
   - **Free space.** Before writing, the disk must hold the estimate plus a constant floor; short, and the save is skipped with a log line. The floor is 4,096 MiB, taken from Strata's own default reserve for a session file (`session_min_free_mib`, read in the Strata repository copy; DPC does not pass it), below which Strata answers 507 even when DPC's check passed. One floor serves both engines: `llama-server` has no such reserve, and the shared value is declared rather than split (Johnny, Zcode).
   - **Thin evidence.** The cold rate at depth can rest on one or two ledger rows (none at 256K on 2026-11-13); with none, the threshold is unknown and the save is tried. Each decision line names the tokens, the cold rate and the number of ledger rows it rests on, so a log shows how thin the figure was (Johnny, Zcode).
   - **Not saved in slice 1:** an owner chat whose prefix is one message. `InferenceOrchestrator` sends the whole rebuilt prompt as one message, so a one-message prefix can never match the next turn; every such file would be written at a stop and deleted at the next lookup. Agents' turns, which are many messages, are unaffected. A later card if the owner's chat on a local engine matters (P4's finding, 2026-11-13).
   - **Staging name.** Both session APIs take the file name in the request body (`{"filename": …}`, a plain name inside the folder; the 2026-11-13 measurements saved under names of their own), so DPC names the file before the call: a staging name, renamed to its final name after a 200 (`os.replace`, same folder). A launcher stopped in the middle of a write truncates only the staging file, and the sweep removes staging leftovers.
   - **On Strata a failed save can end the engine, not only answer 500** (Ark, round 2, from `Engine.session_file` in the Strata repository copy at v0.2.9, not the pinned v0.2.11; to be re-read on the pin before code): a line out of step makes the launcher end the engine (`EngineDied`), and silence longer than `engine_silence_s` (300 s by default) raises `EngineSilent`; a step that blocks may be allowed up to `SESSION_WAIT_MAX_S` (3,600 s). That outcome is logged as its own, apart from "refused (500)" and from "not saved": the stop still goes on, the next launch is a new launch, and a death is not read as "saving suspended for this launch". A save also takes the launcher's FIFO lock, the lock inference takes, so it blocks any request inside the engine, not only the ones DPC refuses; with `_in_flight` at 0 that is safe.
   - **Every failure is caught inside the save step** (a timeout, an HTTP error, a disk error), logged, and the stop goes on. The reason is in the swap: `_stop_for_the_swap` wraps the stop in `except BaseException`, and `_end_swap_drain` puts the holder back in service, so a save error escaping `drain` would cancel the swap and refuse the request it was for.

5. **Restore: only into an empty engine.**
   - **The gate (pre-code check 1).** On Strata a restore beside a live conversation drops it: with A live at 30K and B restored, the cache stayed at `parked 0, parks 0`, B continued with `cached_tokens 29906`, and A's next turn re-read all 30,044 tokens (7.45 s); an ordinary switch parks A as expected. So a restore runs only on the first request after a start or an unload, and only when that request is the saved conversation's own. Otherwise the file waits (until its TTL) and the request runs as today. `llama-server` follows the same rule.
   - **Not while draining** (Ark, round 2). `_ensure` runs before `call_slot` (in all five call paths), so a new call can pass `_ensure` during a save and restore a snapshot into an engine that `call_slot` then refuses with `_draining_error`: a restored conversation nobody is served by, and a refused call that paid for the restore. The restore hook checks that the supervisor is not draining, besides the engine being empty, and checks it as late as it can, right before the restore call; a drain that begins after the check is logged by the refusal.
   - **Where.** In `_ensure`, the one pre-request point all five `llamacpp_server` call paths go through and which `StrataProvider` overrides, beside the await where `guest_checkpoint` sits; the speed clock starts after `_ensure`, so a restore does not skew the tok/s ledger. After an unload the model is loaded first (`POST /load`), because a restore needs it (503 otherwise).
   - **Who.** Only the owner's own call: a call served to a peer (`peer_being_served()`, which in `_ensure` is the call's own context) or a gateway client never restores, or an owner's snapshot would be loaded while the engine serves someone else. The lookup is by the same conversation key only (agent and conversation id, or the owner chat's conversation id, plus the alias); any other request matches no entry and so can never delete one.
   - **Checks before the call.** The lookup sweeps first (point 6), so an expired entry is never restored. Then the prefix: the request's first k messages must hash to the saved value with the saved length. A compaction or a cut since the save makes the file useless: it is deleted and the request runs cold, instead of paying a restore for nothing.
   - **The answer is the HTTP status of the restore call,** on both engines. The `llama-server` log scan for refused restores (`new_restore_refusals`) reads a different text and is not extended. A file the engine refuses (Strata's 400 "saved with another model", `llama-server`'s session-version refusal of ADR-040's (ii)) is deleted at once. `llama-server`'s 400 "No available space in KV cache" refuses the room, not the file: in an empty engine it should not occur, and if it does the file is kept and the request runs cold. A 415, 403 or 501 is DPC's or the configuration's fault, as at a save (point 8): the file is kept. No other slot is ever erased to make room.
   - **After a restore the file is kept** until a newer save of the same key replaces it, or the TTL, the cap, a mismatch or a cleared history removes it; after an engine death, which saves nothing, it still brings back most of the prefix.

6. **The index, the TTL, the cap and the sweep.**
   - **One index file in the folder, `dpc-kv-snapshots.json`,** beside the files, written whole each time (temporary file, then `os.replace`). File names are opaque and random, so a listing of the folder names no agent and no conversation. An entry carries: the file, alias, engine, engine version (Strata's engine version; the `llama.cpp` tag), model identity (`gguf_path`, or the Strata model and the drafter), the conversation key and the caller kind, `n_saved`, bytes, the save's milliseconds, when it was saved, its expiry, and the hash of the request's message prefix with its length.
   - **TTL:** the alias field `kv_snapshot_ttl_s`, in seconds (every timing field of the alias block is in seconds), default **24 h** (86,400 s). **Cap:** the alias field `kv_snapshot_max_gib`, in GiB, default **32 GiB**, plus a constant floor of free space on that disk. Neither is in the restart key sets (`_FLAG_KEYS` in `llamacpp_server_provider.py`, `_STRATA_FLAG_KEYS` in `strata_provider.py`), so editing them restarts nothing. Over the cap the oldest go first by save time, never the file just written; a file that alone exceeds the cap is not saved. The cap counts files, floors included (32 GiB holds about 66 small `llama-server` files).
   - **The index is the only record of what is DPC's** (Ark, round 2). The cap counts indexed files only; a file in the folder that the index does not name is neither counted nor deleted. Strata's image job saves a session file before it unloads the engine and restores it after the next load (`images.py`, `restore_session`, in the Strata repository copy), in the image job's own output directory, which is the slot folder only if the owner points both there; Ark read it as the slot folder, which the copy CC read does not show (not confirmed). Either way the index does not assume every file in the folder is DPC's.
   - **The sweep** runs at supervisor start before the launch, after each save, and at each lookup. It deletes only files the index names and DPC's staging names, never anything else in a folder the user chose. Strata never deletes these files itself.
   - **The pre-launch sweep compares expected values,** because `StrataSupervisor.engine_version` is filled only after `/health`: `_expected_engine_version()` (`strata_fetcher.STRATA_ENGINE_VERSION`) or the pin's tag, the model, and the alias's configured `drafting`. A drafter change is an alias edit, not an engine update, and it makes every file of that alias unreadable. An alias with `drafting` at `auto` (the key absent) records the drafter the engine reported (`drafter` in `local_engine_status`) and cannot be compared before the launch; the engine's fingerprint refusal at the restore deletes such a file.
   - **One folder per alias.** Validation refuses a folder another alias already uses: one index, one writer.
   - **Validation on both engines** (`llama-server` has none today): absolute, no control characters, not a filesystem root or a system folder (the `model_files_dir` rule), writable (a probe file), not inside the Strata install, not another alias's folder.
   - **Default: empty, which is off** (Mike's call, 2026-11-13). `slot_save_path_error` already reads `None` and `""` as off, and `llama-server`'s default is `None`. Mike's two Strata aliases already set a folder (per CC's reading of `providers.json`; no reviewer could open it), so the feature turns on for them when it ships. A change of folder restarts the engine, which drops what is parked; the help text says so.
   - **A missing disk and a folder that cannot be made are two cases** (Johnny, round 1). Today both go through the one `OSError` branch of `StrataSupervisor.write_start_config` (`os.makedirs`, then `raise`), and both refuse the start. Decided: a folder whose parent does not exist (an unplugged USB drive, an unmounted Linux mount point, where `makedirs` would create the folder on the system disk) turns the feature off for that launch, never the engine: the folder is not created, the engine starts with saving off, a WARNING and "off this launch: folder missing" in the Local engines block. The engine launcher creates the folder itself too (`slot_save_dir` does `os.makedirs(path, mode=0o700, exist_ok=True)`, parents included, in the Strata repository copy), so for a missing parent "saving off" also means the key is left out of the start config for that launch, or the launcher makes the folder on the system disk anyway (Ark, round 2; the second half is CC's inference). A parent that exists but in which the folder cannot be made (a read-only volume, no rights, a file in the way) is a configuration error and reads as one, with the OS error in it, not as "off". The second case keeps today's refusal, and the test that pins it (`test_a_folder_that_cannot_be_made_is_refused_in_words`, which puts a file in the way) stands; the first is a change of that behaviour (Mike's call, 2026-11-13).
   - **The hint,** dated 2026-11-13: "SSD: a 250K conversation saves in ~2 s (Strata) / ~6 s (llama-server) and returns in ~3 s / ~5 s. HDD: on Strata a save takes ~38 s, which the agent waiting for the card waits through, and a restore ~43 s, slower in total than reading it again (~67 s), so Strata does not save to an HDD at a stop. On llama-server a 250K cold read is ~7 min, so a deep conversation pays on either disk. An encrypted volume is your option." On Windows a network share draws a warning.

7. **A cleared history deletes its snapshots** (Mike's call, 2026-11-13). Four paths clear a conversation: `reset_conversation`; `clear_history` after a ratified new session; turning `persist_history` off (`set_conversation_persist_history`, which saves the setting off and then calls `clear_history`); and `clear_before`, which applies a new-session marker that arrived from another node (`group_handler`, `monitor.clear_before(marker)`) and keeps the messages after the marker live (Ark, round 1). All four reach `_advance_live_history_boundary` (`clear_before` twice, with and without messages to drop; `reset_conversation`; `clear_history`, also on the persist-off path), and the deletion hangs on that call, whatever it returns. Not on the boundary moving: the method returns without moving when `persist_history` is off, which the third path has already set. A `clear_before` whose archive fails returns before the call and deletes nothing, as it drops no history. The deletion removes every entry of that conversation key on every alias, and marks the conversation so the **next** `drain` does not save the same slot again (the engine does not know the conversation ended); the mark lasts until that conversation's next served call. The prefix check would refuse such a file anyway; it must not wait for the TTL.

8. **The drafter.** With `drafting: dflash2`, which both of Mike's Strata aliases run, every save answers 500 "invalid K/V extent" (upstream Niko1221/Strata #5153, open, not fixed in 0.2.12 or 0.2.13). `mtp` saves, and so does dflash2 with `--draft-fallback mtp`, but only through the launcher's `split_engine_note` path, which is not a supported switch.
   - **The code does not depend on the drafter.** The first 500 from a save suspends saving for that launch, with one WARNING and "off this launch: the engine refuses saves" in the Local engines block. Only a 500 does. A 415 (no `application/json`), a 403 (a foreign `Origin`), a 501 (no folder, or parallel requests) or a 400 (a slot other than 0) is DPC's or the configuration's fault: each is logged under its own reason, WARNING once per launch, and never shown as the engine refusing saves, or a DPC defect would be filed as #5153 (Johnny, round 1, from `serve/server.py` `slot_action` and `_own_page` in a Strata repository copy at tag v0.2.9, `dpc-research\Strata`, not the pinned release; to be re-read on the pin before code). DPC writes no `parallel` key today; set by hand, it turns every save into a 501.
   - **The day #5153 is fixed,** dflash2 aliases start saving with no change in DPC's code, but only once the Strata pin (v0.2.11, engine 0.1.57, `strata_fetcher`) moves to the fixed release; that pin move is where the pre-launch sweep of point 6 removes the older engine's files (Johnny, round 1). Strata's own `--session-save-reclaim`, a retry of a save inside the launcher, is not passed by DPC (no hit in `dpc_client_core`), and nothing here depends on it.
   - **CC recommends `mtp` on both Strata aliases** (decode medians overlap, dflash2/mtp, 3 interleaved rounds: short English 157/143, code 182/178, Russian 105/106, 64K English 140/138 tok/s). The choice is Mike's, and it is a precondition of the first live check on Strata; the first live check can run on `llama-server`, where saving works today.

9. **Known risk: the llama slot-wipe.** On the `llamacpp_server` alias (b12110, `-np 2 --kv-unified`, `-c 262144`) a short request in one slot threw away the 249,823-token conversation cached in the other, although the shared pool had room; the next turn re-read 249,850 tokens in 433 s. A restored snapshot can be lost the same way, and DPC does not pin requests to a slot. The card is `A-REQUEST-IN-ONE-LLAMA-SLOT-WIPES-THE-OTHER-SLOTS-CACHED-PREFIX-THOUGH-THE-POOL-HAS-ROOM` (Mike, 2026-11-13: part of this work). Its first step decides whether the 27B alias runs one slot; until then a restore there carries this risk, and the restore line with the next call's `cached_tokens` shows it.

10. **What is said.**
    - **A result channel from `drain`.** `drain` returns the save outcome (it returns `None` today); `_stop_for_the_swap` hands it to `_note_swap`, and `stop_local_engine`'s command result carries it.
    - **`last_swap` gains one field:** {`from`, `to`, `from_engine`, `to_engine`, `at`, `parked_dropped`, `live_saved`}. `parked_dropped` keeps its meaning. `live_saved` is null when the engine held no live conversation, and otherwise the outcome: saved, with bytes and milliseconds, or not saved, with the reason in the words of the log line. In memory until the backend restarts, as before.
    - **The notice counts the live conversation:** "swapped 'A' out for 'B'; <agent>'s live conversation saved (4.0 GB, 2.1 s); 1 parked conversation dropped", or "… not saved: <reason>". A live conversation that was not saved is said to be lost, on both engines, which ends the undercount. The test that pins a bare `llama-server` notice ("llama-server parks nothing, so no second half", `test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card.py`) changes with it.
    - **The Local engines block, per alias:** the files, GB held against the cap, the oldest expiry, the last save and restore outcomes, and "off this launch: <reason>" (folder missing, or the engine refuses saves).
    - **Log, INFO:** each save (alias, slot, conversation key, tokens, bytes, ms, outcome), its line reading "saved N bytes in M ms" so a live check can see it; each skipped save with its reason (a peer or gateway call last, a caller in its window, a call in flight, more than one slot occupied, the backend stopping, estimate over the bound, below the size threshold, disk short, suspended for this launch); each restore and each skipped restore (expired, prefix mismatch, no room, refused); each sweep deletion with its reason, "refused and deleted" never in the words of "not saved". WARNING once per launch for a repeated engine refusal, and once per launch for each transport or configuration refusal (point 8). No prompt text, ever; a peer appears only as its node id.

11. **Peers: slice 2, designed now, shipped after the DPTP card and one check.** Saving a peer's call writes another node's prompt to this disk, where today it lives only in engine RAM (the C2 guest entry, and slot 0 after a peer's call).
    - **Opt-in per alias, off by default,** the alias field `guest_kv_snapshot` (after `guest_conversation_cache`), disclosed in the Inference Sharing settings text ("a peer's conversation may be kept on this disk for up to N minutes").
    - **Key:** the remote node id for a P2P peer, and the alias alone for the gateway, plus the hash of the request's message prefix; restore matches by that prefix and never across peers. A peer's request carries no DPC conversation id, and a gateway call on loopback is recorded with this node's own id: none of the 353 peer rows or 797 gateway rows in the November ledger has a `conversation_id` (Johnny's reading, checked by CC on 2026-11-13); two harnesses behind one key share one snapshot by the prefix check.
    - **TTL:** short, tens of minutes, not 24 h (a median peer prompt of 86,580 tokens is ~1.55 GB on disk); it is an explicit per-alias field, `guest_kv_snapshot_ttl_s` in seconds, with the host's two existing idle clocks for a peer (`GUEST_ENTRY_IDLE_S`, 900 s; `caller_idle_s`, default 120 s) shown beside it in the settings, and one rule: a peer's TTL is shorter than the owner's `kv_snapshot_ttl_s`. It is not derived from either clock, because an edit of the busy window (`caller_idle_s`, a node setting) would silently change how long another node's prompt stays on disk. The name `guest_kv_snapshot_ttl_s` is Ark's recommendation (round 2), after `guest_conversation_cache`; Mike's call named the form, so the identifier is confirmed in the group naming check. `_busy_for_the_card` keeps an alias busy for a caller's window (`caller_idle_s`, default 120 s; `swap_wait_s`, default 300 s), so a swap can come minutes after a peer's last call and that peer's prompt would be saved looking fresh: one more reason against 24 h for peers, besides the disk (Ark, round 2). Peers get their own share of the cap, so a busy peer cannot push the owner's snapshots out.
    - **What exists today** (Ark, round 2): `guest_conversation_cache` is true when absent, so a peer's conversation is already parked in host RAM; the disk is the second step of that policy, not a new one.
    - **Cleanup:** the restore runs only for the same peer's call; a peer's new session cannot be seen from here, so the prefix check and the TTL are the only cleanup. Peer windows count as in slice 1.
    - **The check first.** By Johnny's split of the November ledger's cold turns (cached 0, prompt ≥ 5K; his script, not re-run by CC), 17 of the 24 classifiable cold peer turns are a prompt the harness changed, which stays cold, and 6 a lost engine state; for our agents it is 127 lost states against 10 changed prompts. So slice 1 is backed by its numbers, and the 66.5 % that the peer re-reads is mostly rewritten prefixes, which a snapshot cannot help. His grouping rests on `(caller, conversation_id)`, which peer rows lack, so a peer running several conversations side by side would turn the 17 into lost states. The falsifier is cheap: the same peer, two different prompts, and the next turn's `cached_tokens`. Slice 2 waits on it as well as on the DPTP card.
    - **The DPTP card.** Slice 2 does not ship before `A-PEER-IS-NOT-TOLD-WHETHER-THE-HOST-KEEPS-ITS-CONVERSATION-STATE-OR-FOR-HOW-LONG` is reviewed (Mike, 2026-11-13: needed, not optional; harnesses such as Claude Code, Zcode and dsh show a prompt cache and its TTL): a response field naming whether the host keeps a snapshot and until when, and a request flag "do not keep" that the host honours by skipping the park and the save. The flag outranks `guest_conversation_cache: true` for that call, so it amends the peer amendment of 2026-11-13 (its point 2, the switch) as well as this one (Ark, round 1).
    - **Security.** The host can read a kept snapshot: it holds token identity, and token ids detokenize back to the prompt (on `llama-server` the slot file carries the slot's tokens, per llama.cpp's sequence-state format, not opened here); even without them a prompt's KV encodes the prompt, it does not protect it. The disk adds persistence, not visibility: the host already sees every peer prompt in plain text while serving it. What a kept snapshot adds is that the prompt survives a power-off, sits in backups and leaves with the disk, until the TTL. Against that: opt-in per alias, a short TTL, the sweep, the owner's choice of folder (an encrypted volume is his option), the peer told through DPTP and able to ask "do not keep", no prompt text in any log line. **DPC does not encrypt the files:** the engine writes and reads them itself, so DPC would have to decrypt to a plain temporary file before every restore; the plaintext would touch the disk anyway, and the restore would pay a second full read. The owner's own snapshots add nothing new either: `history.json` keeps the same conversation in plain text.

12. **Not changed.**
    - `DROPS_PARKED_ON_STOP` stays true on Strata (parked entries still drop) and false on `llama-server`. The false no longer claims more than it holds: it does not count the live slot or the `--cache-ram` host cache, which a stop also loses. Flipping it is a separate question, because it changes the **card-borrow**.
    - The card-borrow and `_description_may_not_stop` are unchanged: their reason, parked entries lost at a stop, still holds. Revisited when Strata saves cache entries.
    - The C2 guest entry is a parked entry and is never saved; a peer in slot 0 is skipped (point 2).
    - Gateway callers have no conversation identity and are not saved in slice 1. A "clear KV snapshots" button and saving parked entries (upstream) are later.

13. **Falsifiers for slice 1.**
    - **The positive control, in one run:** the line "saved N bytes in M ms" for the conversation, then its restore line, then the next call's `cached_tokens` close to its prompt. `cached_tokens` alone fails the same way for three causes: no save, a save lost to another slot (point 9), a prefix that moved (Johnny, round 1).
    - A file refused and deleted is logged apart from a save not made.
    - An expired, version-changed or (configured) drafter-changed file is removed by the pre-launch sweep; a file of a `drafting: auto` alias that the engine refuses is removed by that refusal at the restore; a prefix-mismatched file is removed at the lookup (Ark, round 1).
    - On Strata, a save that ends the engine (`EngineDied`, `EngineSilent`) is logged as that outcome, apart from a 500 and from "not saved"; the stop still completes.
    - A save answered 415, 403, 501 or 400 logs its own reason and leaves saving on; only a 500 suspends it.
    - The folder stays under its cap. A save that times out or fails still stops the engine, and a swap still serves its request within the request's stop bound.
    - An unplugged folder disk still starts the engine; a folder that cannot be made on an existing parent shows the OS error.
    - A slot whose last call was a peer's writes nothing, also when that call ended more than `caller_idle_s` before the stop; the skip is logged.
    - A reset, a ratified new session, turning `persist_history` off and another node's new-session marker each leave no file of that conversation.
    - A call that arrives during a save and passes `_ensure` restores nothing: the restore hook sees the draining supervisor (Ark, round 2).
    - A restore does not run the machine out of commit: a 4 GB restore beside the children of both engines, read against the free commit (Ark, round 1; today's `-ub` measurement moved free commit by −4.5 GiB at 2048 and −13 GiB at 4096, against 1024).

**Decided** (Mike, DPC Project chat, 2026-11-13: every recommendation of the review draft taken as written).
- **Names:** `kv_snapshot_ttl_s` and `kv_snapshot_max_gib` (alias fields, DPC's own noun because the engine never sees them; the folder keeps the engines' `slot_save_path`), `dpc-kv-snapshots.json` (the index file), `KV_SNAPSHOT_WRITE_CEILING_S` (the per-file write ceiling, 60 s), `guest_kv_snapshot` (slice 2's peer opt-in field), `live_saved` (the new field of `last_swap`), and the label "KV snapshots folder (slot_save_path)" with the help text of point 1.
- **Defaults:** TTL 24 h, cap 32 GiB per alias, an empty folder is off.
- **The peer TTL:** an explicit per-alias field, with both existing idle clocks shown beside it, and one rule: a peer's TTL is shorter than the owner's (point 11). The field is `guest_kv_snapshot_ttl_s` (Ark's recommendation, round 2, after `guest_conversation_cache`); Mike's call named the form, not the identifier, so the name is still to confirm in the group naming check.
- **Renaming:** the texts that say "session files" in the engine's sense are renamed in the commit that changes the label: `strata_supervisor.py` (two comments, and the refusal "cannot create the session files folder", with `tests/test_a_strata_alias_can_name_a_session_files_folder.py`, which matches it as a string and carries it in its file name), `providers.example.json` (the `slot_save_path` help, whose "About 18 KiB per token" is roughly 15 % off the 2026-11-13 fit and lacks the ~236 MB floor per file, which is what is missing), `ProvidersEditor.svelte` (type comment, Browse title, label, button title) and `strataSessionFilesField.test.ts`. The session archive's "session file" (`dpc_agent/tools/archive.py`, `conversation_monitor.py` `_archive_messages`, `tests/perf`) is another meaning and stays, and so does the engine's own start banner ("session files are kept until deleted"), which is not ours to rename: the rename covers DPC's texts only (Ark, round 2).
- **A missing disk:** a folder whose parent does not exist turns saving off for that launch and the engine starts; a parent that exists but refuses the folder keeps today's refusal (point 6).
- **Slice 2 (peers):** the design stays in this record (point 11); the build is off the plan until the peer falsifier answers and the DPTP card `A-PEER-IS-NOT-TOLD-WHETHER-THE-HOST-KEEPS-ITS-CONVERSATION-STATE-OR-FOR-HOW-LONG` lands.
- **Not decided here:** the Strata drafter (`mtp`, point 8) and the 27B's `-ub` stay Mike's lines in `providers.json`, as before.

**Named in the group naming check** (DPC Project chat, 2026-11-13; Mike: "agree among yourselves, look at the glossary"):
- the record keeps the ledger's word `caller_kind` with a fourth value, `owner`, for the owner's own chat, which has no ledger row; `node_ledger.CALLER_KINDS` keeps its three words, and the GLOSSARY row "caller_kind" says the fourth lives only in the KV snapshot record (CC, Zcode; Ark's `call_kind` alternative and Johnny's "vocabulary, not name" objection are answered by that row). Not `OWNER_TURN` (`dpc_agent/agent.py`), a turn class for memory layers. Functions `note_engine_call`, `mark_sent`, `engine_call_of`, `forget_conversation`;
- index entry keys beyond the engine's `n_saved`: `save_ms`, `saved_at`, `expires_at`, `prefix_sha256`, `prefix_messages`;
- `live_saved` = {`outcome`, `bytes`, `ms`, `reason`}, `outcome` one of the module's outcome words, the same dict in the log, `last_swap` and the status (Ark);
- the per-alias status key `kv_snapshots` in `local_engine_status`, with `off_reason`, `last_save`, `last_restore`.
- **Residuals, stated** (Ark, Zcode): the last-call record and the cleared mark live in memory until the backend restarts; after a restart nothing is saved until a call has been sent. A file whose delete the OS refused stays in the index and every sweep retries it. A conversation is saved only when a history-clearing path can reach it, that is, when a conversation monitor exists for its id (`note_clearable` at the monitor's creation): an agent's task turn keyed by the task's id (a scheduled run with no reply conversation) has no monitor, so it is not saved in slice 1, and a later slice can map task to chat through `_reply_conversation_id` (Ark; Zcode for the strict form, 2026-11-13). A peer's record carries no conversation and, on Strata, no prefix: slice 2 starts from that absence, not from a record to extend (Johnny). After a restore on a `llama-server` with `-np 2`, a request in the other slot can make two slots occupied and the next save is skipped, the slot-wipe card's risk again (point 9). The "skip while stopping" of the backend stop is a property of the moment (the registry is stopping), not of `close()`.
- **Known limits of slice 1, stated** (team review, 2026-11-13):
  - **A context cut between the save and the return deletes the snapshot.** The prefix check hashes the call's whole message list (the turn base since the amendment of 2026-11-16, below); the agent loop's cut of old tool results (`last_context_cut`) rewrites early messages, so the next call's prefix no longer matches and the file is deleted ("prefix moved"). The engine would not reuse past that point either (`cache_reuse` off), so nothing is lost against today, but long sessions, where a snapshot matters most, are where cuts happen. A longest-matching-prefix restore is a later idea, not slice 1. The live acceptance runs one turn with a cut and one without (Johnny, Zcode).
  - **Three 507 answers in a row on one alias** turn saving off for that launch, as an engine refusal does; any other outcome resets the count (Ark).
  - **The first call into an empty engine spends the restore**: a peer's or gateway client's call that arrives first fills the engine, and the owner's snapshot waits until the engine is empty again (a new launch, or on Strata an expected unload). The log line says which call filled it (Johnny).
  - **`note_clearable` only grows** for the backend's life (conversation ids, small); after a restart nothing is saved until a call has been sent and its size is known (Ark).
  - **The unmounted-disk check is a heuristic**: an empty folder under `/mnt`, `/media`, `/run/media` or `/Volumes`. A disk mounted elsewhere whose parent folder exists is not told apart from a folder that is simply missing, and `mkdir` would create it on the system disk (Ark). The log line names the heuristic.
- **The rename also covers** ADR-046's other "Strata session files" mentions (not only point 7.3); like 7.3 they read "KV snapshots" from here on and are not rewritten (Johnny).

## Amendment, 2026-11-16 — the AI's vote is gone

The "vote" in the sentence "The vote and the group image are requests too" no longer exists:
`_ai_agent_vote_on_proposal` and its evaluation prompt were removed, because only people vote, and only
on Extract Knowledge (Mike's call, 2026-11-16; [ADR-009](009-knowledge-flows-redesign.md) already said the
AI provider in a Local AI chat is not a voter). The group image stays a request.


## Amendment, 2026-11-16 — a KV snapshot's prefix is the turn base

*(Mike's call, 2026-11-16; reviewed by Zcode and Johnny. This replaces "the hash of the request's message prefix" of point 6 and of the known limit on context cuts, read as the whole message list of the last call. The restore check of point 5 is unchanged: the request's first k messages hash to the saved value.)*

1. **The change.** A noted call hashes its turn base: its messages up to and including the last `user` message, the turn's trigger (`kv_snapshots.base_end`, the same index the `KV snapshot prefix at save:` line prints as `base_end`). `prefix_messages` is that length. A call with no `user` message keeps the whole list.
2. **Why.** An agent's turn sends the system message, the history, one `user` trigger and then its tool rounds; the next turn replays the history but never those tool rounds, so a whole-list hash matched only the same turn continuing. Measured 2026-11-16 at be24af30: Johnny's snapshot held 53 messages with `base_end=47`; after a backend restart his next turn sent 49, messages 0 to 46 byte for byte the same, system message included, and the first difference at 47. The whole-list check deleted that file; the turn base matches it.
3. **What it relies on.** The history is append-only and replayed byte for byte, the old trigger included (ADR-040 F2).
4. **Where it breaks.** A group-history merge that reorders and re-chains entries (`conversation_monitor.py`, `restore_chronological_order` after a merge); `clear_before`; a context cut that rewrites early messages (the known limit above); a system message that changes between turns, seen at the start of about a third of turns in the logs of 2026-11-12..16, cause not established. Each fails the check, and the file is deleted as before.
5. **The open risk.** The restored slot holds the old turn's tool rounds, so it is longer than the new prompt and the engine must reuse only the common beginning. `llama-server` does that by its prompt-cache prefix match; on Strata it is unverified. The acceptance is the `reuse=` figure of the `llamacpp usage` line on the first call after a restore.
6. **Not changed.** The owner's chat sends one message, so its turn base is 1 and it is still refused as a single-prompt call; an agent's turn whose base is one message is still saved.


## Amendment, 2026-11-16 — the size threshold counts the restore only

*(Mike's call, 2026-11-16. This replaces, in point 4's size threshold, "save plus restore" and "Both sides count at a stop, because the save is paid by the request waiting for the card".)*

A conversation is saved at a swap or a stop when its estimated restore is shorter than its estimated cold read; the save is no longer added. The save runs while the GPU idles, the cold read holds it. The save still has to fit its bound (`save_bound`, the 60 s ceiling), checked as before. Measured 2026-11-16 on the Strata folder on an HDD (write 102 MB/s, read 90 MB/s, 15.3 KB per token, cold prefill 3,700-3,800 tok/s): restore 0.17 ms per token against 0.27 ms cold, save plus restore 0.32 ms, so the old rule never saved Strata on an HDD.

With the restore alone deciding, an inflated read rate would call every restore free. The read rate the threshold uses, and the one a restore records, is therefore at most twice the folder's write rate: a restore of a file written minutes earlier on 2026-11-16 read from the Windows file cache and recorded 2.3 GB/s for a disk that does about 90-130 MB/s.


## Amendment, 2026-11-17 — a KV snapshot at an agent switch (llama-server, slice 1)

*(Mike's call, 2026-11-17, on the card `A-CONVERSATION-AN-AGENT-SWITCH-PUSHES-OUT-OF-THE-SLOT-IS-NOT-SAVED-TO-THE-KV-SNAPSHOTS-FOLDER` (gitignored `backlog.md`), a child of the card of the KV snapshot amendment of 2026-11-13. Designed in `ideas/kv-snapshots-at-an-agent-switch-plan-2026-11-17.md` (gitignored) after review round 1 by Johnny, Ark and Zcode and engine checks over raw HTTP; built in e7201c0d. It extends the 2026-11-13 KV snapshot amendment, whose points are cited bare below: point 3 ("When") gains a switch beside the stops, and point 5 ("Restore: only into an empty engine") gains a second case on `llama-server`. Strata is unchanged: a later slice.)*

**The case.** Agents run one after another, so most switches happen with no stop. A switch is the word of point 5 ("an ordinary switch parks A"): the incoming call's conversation key differs from the key of the alias's last sent record. On `llama-server` the conversation a switch pushed out of the slot went only to the `--cache-ram` host cache, which no slot API can save, and was lost at the next stop or when the host cache let it go. Mike's call, 2026-11-17: the KV snapshots folder the user chose is used at a switch too, an HDD included, and the incoming agent's wait for the save is accepted; a save is decided by the restore-only threshold, as at a stop (amendment of 2026-11-16, "the size threshold counts the restore only").

**What was measured,** 2026-11-17, `llama-server` b12110 with the 27B at `--ctx-checkpoints 16`, raw HTTP with DPC's child stopped, a folder on the HDD (the plan's §12-13; raw data in CC's probe folder `switch-prechecks-2026-11-17`, not in the repository; the figures below re-read from it by CC):
- **A restore into an occupied slot works.** With `-np 1 --cache-ram 0`, A's file restored into slot 0 while it held B answered 200 with `n_read` equal to the file's size, and A's next request reused 32,040 of 32,074 tokens.
- **A failed restore empties its target slot, and only it.** A truncated, a random, a zeroed-header and a missing file all got the same 400 "No available space in KV cache or invalid slot save file"; with `-np 1` the slot then read 0 tokens and the next request ran cold; with `-np 2 --kv-unified` the other slot and the host cache stayed. The status cannot tell a bad file from no room or no file.
- **`n_saved` is the slot's count.** `/slots` `n_prompt_tokens` equalled the save's `n_saved` in all 11 pairs read right before a save (P1 2, P3 1, P5 8).
- **The host cache empties the idle slot.** With `-np 2 --cache-ram 8192` (the 27B's lines today) the idle slot is emptied into the host cache as soon as the other slot starts, and a save after that answers 200 with `n_saved` 0. With `-np 2 --cache-ram 0`, B took slot 1 and A stayed in slot 0: two occupied slots.
- **A short read is a damaged file.** A file cut inside its checkpoint and draft appendix restores with 200 and the full `n_restored`, but `n_read` 716,934,272 of 891,718,378 bytes, and the next request recomputes everything; every good restore read the whole file.
- **Timings.** A save keeps every checkpoint the slot holds (14-16 here, 149.646 MiB each at any depth). 94K-token files of 4.48-4.64 GB saved in 42-46 s; 128K files of 5.26-5.58 GB in 38-43 s (probably OS write-back). A restore of a just-written file took 1.9-2.5 s, from the OS file cache; a restore from a cold HDD was not measured.

**What is built** (e7201c0d; line numbers at that commit, tests in `test_a_kv_snapshot_is_saved_and_restored_at_an_agent_switch.py`).
1. **Where the switch is seen.** The provider's `_note_engine_call`, in `_ensure` and so on all five call paths, reads the outgoing record (`engine_call_of`) before it notes the incoming call. When the engine is not fresh it hands over to `_kv_at_a_switch` (`llamacpp_server_provider.py` 548), which calls the supervisor's `kv_at_a_switch` (`llama_server_supervisor.py` 4338) on `llama-server` with a folder set, and only when the call saves, restores, or must wait for a switch in progress. The empty-engine restore of point 5 stays beside it.
2. **Who waits.** A peer's or gateway client's call never waits: one INFO line, "KV snapshot save not waited for", and it goes on (point 11 keeps peers for slice 2). Any other incoming key, an owner's chat or a background call included, triggers the save of an owner record that names a conversation, and the save runs before the incoming call is sent: once that call is sent, the outgoing conversation is in the host cache or gone (P5).
3. **One switch at a time per alias.** `kv_at_a_switch` runs on the owner loop (`@on_the_owner_loop`; agent tools run on per-call loops) under a per-alias `asyncio.Lock`, `_kv_switch_lock`. After the lock it reads the record again, refuses while draining, and takes a call slot, so a drain begun later waits for it.
4. **The save** is `_save_live_conversation(save_by, own_slots=1, after_s=_KV_SWITCH_DEADLINE_MARGIN_S)`, with the stop save's gates (the backend stopping, one occupied slot in `_kv_slot_to_save`, `_why_not_this_record`, callers in their window, the cleared mark, `off_reason`, the threshold, disk short, the estimate over the bound) and the hook's own slot not counted as in flight. Its bound is the incoming request's own deadline (`_kv_switch_save_by`: the card hold's `accepted_at + wait_s`, else `swap_wait_s` from now, as `_QueuedRequest` reads it) less a 2 s margin, never above the 60 s write ceiling; the restore that follows is bounded by what is left of the same deadline (Johnny's review of the slice, 2026-11-17: without it the incoming call could wait 60 s of save plus 120 s of restore). A request already past its deadline gets the late request's 60 s, as at a stop. A slot the index already holds (its `n_saved` equals the slot's tokens and its `prefix_sha256` the record's) is skipped as "already saved". The save is its own task under `asyncio.shield`: a cancelled caller leaves it writing, and the task keeps the call slot and the lock to its end, so no drain stops the engine in the middle of the write.
5. **Checked again around the request.** In `_kv_save_within` (4029), with no await before the request: no other call in flight and the record's `sent` unchanged. After the 200, in `_kv_write`: a call sent meanwhile discards the file, and on `llama-server` an `n_saved` other than the measured tokens discards it.
6. **The incoming conversation is restored** (`_kv_restore_at_a_switch`, 4525) for an owner call with a conversation, after the save ended and only when it landed or was skipped (a save tried and failed restores nothing and keeps the file), and only when: the snapshot passes the prefix check on the turn base (amendment of 2026-11-16; a mismatch deletes it, as before); its restore at the capped read rate is shorter than a cold read of its `n_saved` (`_kv_restore_estimate`); with a host cache on (`cache_ram_mib` not 0), it was saved from that key's last sent call, compared as (`sent`, `sent_mono`) through `kv_snapshots.last_sent_of`, or this launch never served the key, because a restored slot that is a whole prefix of the request is never compared with a longer host-cache copy (llama.cpp #28276, per Zcode's review, not re-read here); and nothing unsaved sits in a slot a failed restore would wipe (`_kv_why_the_slot_is_held`: tokens no call of this launch names, a peer's or gateway client's call, or an owner call no snapshot holds; a background call's prompt or a slot the index holds may go). Two occupied slots restore nothing. Right before the request: not draining, no call in flight but the hook's, the record unchanged.
7. **The restore's answer.** At a switch the 400 never deletes the file: it cannot be told from no room, and the slot is wiped either way. (The empty-engine path reads that 400 as a bad file and deletes it: see the amendment at the end, 2026-11-17.) A refused ("saved with another model") or missing file is deleted as before. A 200 whose `n_read` is short of the file's size deletes the file as damaged (`_kv_read_short`, 4301; `llama-server`, on both restore paths).
8. **The marker.** `_kv_restored_after_sent` records that a switch restore was tried after the record's call (restored, wiped by a failed restore, or unknown after a cancel), so neither the next switch nor a stop saves slot 0 under that record (`_kv_restore_since`). A restore of the record's own conversation saved from its last sent call clears it, and so does a new launch.
9. **A record sent to an earlier launch is never saved,** at a stop as at a switch: its `sent_mono` precedes this launch's `/props` (`_kv_launched_mono`; `_kv_sent_before_this_launch`, 4450), and a new child's slot holds no call of the old one. Strata reads its own reload (8bd23a4c).
10. **One decision line per switch,** "KV snapshot decision at a switch from <outgoing key> to <incoming call>": the tokens, the estimate, the cold rate and the ledger rows it rests on, the read rate and whether the cap cut it, and the incoming restore against its cold read, so neither wait is silent. With no save, the restore side writes the same line with the reason.

**The size model under both thresholds** (f2dcaecf, 4226c0ab). Point 4's "a file of floor plus the part per token" now adds the alias's last measured checkpoint bytes, which `llama-server` names in its save line ("appended N context checkpoint(s)"), and floor and slope are fitted together by least squares over the last `SIZE_SAMPLES_PER_ALIAS` saves once two span `SIZE_FIT_MIN_SPREAD_TOKENS`, never mixing files with and without those bytes; otherwise the one-file rule stands. The reason: after `ctx_checkpoints` went from 2 to 16 the old model estimated 2.0 GB for a 4.04 GB file, and the seed floor with only the slope refitted put a 200K-token file about 9 % low, close enough to the 60 s ceiling for a save to start and be cut. The read rate stays capped at twice the write rate (8cbb66fb, amendment of 2026-11-16).

**Not decided, Mike's.**
- **`cache_ram` 0 and `-np 1` work as a pair.** With `cache_ram 0` and `-np 2` both slots stay occupied, `_kv_slot_to_save` refuses and the save is skipped; with today's `cache_ram 8192` the save runs and a restore is skipped whenever the host cache may hold a newer copy. CC, Johnny, Ark and Zcode recommend `cache_ram 0` for the 27B, and the slot-wipe card's first step decides `-np 1` (point 9); both are lines in his `providers.json`.
- **A minimum number of ledger rows** for the cold rate: none in code; the decision line prints the rows it rests on.
- **What a switch save costs others.** A background call inside an agent's turn now waits for that agent's save, and an owner's chat call pays the outgoing agent's save although the owner's chat itself is never saved (the plan's review kept both; the line says it). Whether the 60 s ceiling should be shorter where an agent waits is the plan's Q3.
- **The word.** "Switch" in this sense has a GLOSSARY row (Mike's naming, 2026-11-17).
- **Peers** stay slice 2 (point 11); **Strata** at a switch is a later slice.

**Live acceptance pending.** A → B → A on the 27B through DPC: at B's first call the decision line and "saved N bytes in M ms"; at A's return its restore line and the `reuse=` figure on the first call (amendment of 2026-11-16, point 5), the restore's milliseconds telling an OS-cache read from a cold-disk one. And a run with a context cut, as the known limit of 2026-11-13 "The live acceptance runs one turn with a cut and one without" requires.

## Amendment, 2026-11-17 — an ambiguous 400 into an empty engine deletes the file

*(Mike's call, 2026-11-17. It replaces the sentence in point 5 of the 2026-11-13 amendment that says `llama-server`'s 400 "No available space in KV cache" refuses the room, not the file, and that the file is kept and the request runs cold.)*

In an empty engine that 400 deletes the snapshot file, as the code does (`llamacpp_server_provider.py`). Measured on 2026-11-17: a truncated, a random, a zeroed-header and a missing file all got the same 400, so the status cannot tell a bad file from no room, and an empty engine has room, so it is the file. Keeping a bad file would fail every start; the cost of deleting a good one is a cold first call. At a switch the 400 still never deletes the file (point 7 of the 2026-11-17 switch amendment).

## Amendment, 2026-11-17 — a save no longer waits on the restore-vs-cold-read comparison

*(Mike's call, 2026-11-17, DPC Project chat. For saves it supersedes the restore-only threshold of the amendment of 2026-11-16 ("the size threshold counts the restore only") and, in the switch amendment above, "a save is decided by the restore-only threshold" and the threshold among the save's gates.)*

1. **The change.** A save at a swap, a stop or a switch is no longer skipped because its estimated restore is slower than its estimated cold read (`kv_snapshots.worth_saving`). The decision line still prints "restore ~X s against a cold read ~Y s", so the comparison stays visible; it no longer decides.
2. **Why.** A snapshot exists so a conversation survives until it returns, on the disk the host chose, with the host's TTL and cap; it is not there to win a comparison at save time, and at save time nobody knows whether the file will still be in the OS file cache at the return. In `dpc-client.log` of 2026-11-17, 12:06-12:49, 5 of the 6 restores on the 27B read 1.4-3.2 GB in 0.7-1.8 s, a rate only the OS file cache gives on that disk, where the four estimates logged for them said 10.0-14.8 s; the sixth, after a swap, took 17.3 s.
3. **Unchanged.** Every other skip: size unknown, the bound and the incoming request's deadline ("estimate over the bound", "no time left"), disk short, the folder off, the backend stopping, calls in flight, peers and gateway clients, single-prompt calls. At the return the restore side still chooses restore or cold read (`_kv_restore_estimate`).
4. **The estimate** now only decides whether a save fits its bound, so it adds the conversation's own context checkpoints: those its index entry recorded at its previous save (`checkpoint_bytes`, absent from older entries), else the median over the alias's size samples, else the alias's last value; a whole-file fit still adds none. The same log shows why: with the previous save's checkpoints, another conversation's, a 26,225-token save (4 checkpoints) was estimated at 2.25 times its file and a 54,210-token one (11 checkpoints) a fifth under.

## Amendment, 2026-11-17 — a snapshot whose turn base differs is restored when its common messages pay

*(Mike's call, 2026-11-17, DPC Project chat: decide by time, as the restore at a return already decides. On `llama-server` it narrows the restore check of point 5 — kept unchanged by the turn-base amendment of 2026-11-16 — and "a mismatch deletes it, as before" in point 6 of the switch amendment above.)*

1. **The change.** When the request's turn base no longer hashes to the saved one, the snapshot is not deleted at once. The request's leading whole messages are compared with the saved turn base's, one short hash each, and the common ones decide: the reusable tokens are estimated as `n_saved` times the share of the saved call's characters those messages hold (every message the call sent, the tool rounds after the base included), and the snapshot is restored when the estimated cold read of that common part (tokens at the alias's cold rate for that length, `_kv_cold_rate`) takes longer than the estimated restore of the file (its bytes at the folder's capped read rate, as `_kv_restore_estimate` reads it); otherwise it is deleted as before. No message in common restores nothing. Unknown rates try the restore, as at a return. Both restore paths: into an empty engine (`llamacpp_server_provider.py`, `_restore_into_an_empty_engine`) and at a switch (`_kv_restore_at_a_switch`); the rule is `_kv_common_part` in `llama_server_supervisor.py` over `kv_snapshots.common_part`.
2. **The index.** A new entry carries its turn base's per-message short hashes (`prefix_hashes`, the 8 hex characters `message_fingerprints` logs) and running canonical character counts (`prefix_chars`), and the canonical length of every message of the call (`call_chars`), copied by `new_entry` from the call's fingerprint. An entry with the first two and without `call_chars` estimates against the base's characters. Hashes and counts only, never message text. An entry written before the fields keeps today's rule: an exact match or a deletion.
3. **One decision line**, on both paths: "KV snapshot of … partly matches: K of N messages common (~T of S tokens, estimate; the engine rounds down to a checkpoint, up to ~8192 tokens lower), restore ~X s … against a cold read of the common part ~Y s: restoring", or "…: deleted, the call runs cold". The `reuse=` figure of the usage line on the call that follows is the acceptance, as in point 5 of the turn-base amendment.
4. **What was measured,** P6, 2026-11-17, raw HTTP on `llama-server` b12110 with the 27B (`-np 1 --kv-unified --cache-ram 0 --ctx-checkpoints 16`, DPC's child stopped; raw data in CC's probe folder `switch-prechecks-2026-11-17/p6`, not in the repository; the figures re-read from its summary rows). A conversation grown over three calls to a 49,373-token prompt was saved (49,388 tokens, 2,841,245,324 bytes, 10 checkpoints), restored into a fresh engine, and a request B sharing only a prefix sent; each B also ran cold on a fresh engine. The engine resumed at the largest checkpoint at or below the common prefix in every variant:

   | B shares | common tokens | reused | B's prompt after the restore | cold |
   |---|---|---|---|---|
   | every message of the saved call (control) | 49,372 | 49,369 (99.9 %) | 0.4 s | 30.1 s |
   | up to a late message | 44,453 | 44,450 (89.7 %) | 4.6 s | 30.3 s |
   | up to a middle message | 36,042 | 36,039 (72.8 %) | 10.7 s | 30.4 s |
   | the system message only | 26,835 | 26,832 (53.8 %) | 17.0 s | 30.9 s |
   | half the system message | 13,542 | 0, a forced full re-read | 30.6 s | 30.4 s |

   Each restore took 1.11-1.24 s, a rate only the OS file cache gives on that disk. A divergence inside the first message wastes the restore, which is why no message in common restores nothing.
5. **Unchanged.** The exact match restores as before, and into an empty engine it still has no time rule; at a switch the restore of the whole `n_saved` against its cold read still comes first, and a snapshot below it is kept. Every other guard stays: draining, calls in flight, the host-cache gate (`last_sent_of`), the slot-held checks, the restore marker `_kv_restored_after_sent`, the deadline bounds, a short `n_read`. After a partial restore the call proceeds and the file's fate is as before: the next save of the key replaces it.
6. **Strata keeps the exact match.** Its reuse of a restored slot that only partly matches the request is unmeasured (point 5 of the turn-base amendment already calls even the exact case unverified there), so `_kv_common_part` answers nothing on Strata and the provider asks it only on `llama-server`.
7. **Known limits.** A save keeps at most `--ctx-checkpoints` (16 on the 27B) and the engine drops the oldest first, so in conversations of 100K tokens and more an early divergence may fall back further than the common part — unmeasured. The engine checkpoints a user message's start only when it is the last user message or more than `checkpoint_min_step` (default 8192) past the previous checkpoint, so the reuse rounds down to the nearest checkpoint, up to about 8K tokens below the common part (llama.cpp b12110, `tools/server/server-context.cpp` 4502-4507, default in `common/common.h` 643; not modelled in the estimate). `n_saved` also holds the reply the saved call generated, which no message carries, so the estimate runs a little high by that reply.

## Amendment, 2026-11-17 — the slot queue: a conversation keeps the slot until its turn ends

*(Mike's calls, DPC Project chat, 2026-11-17: the wait in front of the slot is named **slot queue**, and a conversation keeps the slot until its turn ends while the next one waits, his choice for now, with its cost accepted: a second agent may wait 30 minutes and more, to be revisited with logs (06:15-06:16 UTC); variant A for a peer (08:10 UTC, point 8); the frame of 04:15 UTC: KV snapshots are for scale, an HDD on purpose, many local models across agents and peers, long autonomous runs, with the TTL and the cap the host's per alias (`kv_snapshot_ttl_s`, `kv_snapshot_max_gib`); and, later the same day in the same chat, his answers to the three questions of the review draft, each taking CC's pick: the peer's deadline and what happens at it (point 8), calls with no hold (point 2), the owner's chat (point 6), and the names (at the end). CC's four proposals, put to him in the same message as standing unless he objected, stand: he raised no objection (the slot place before the peer door's semaphore, point 8; the slot-held guard skipped on a one-slot alias with no host cache, point 7; no limit on an owner's wait, point 9; Strata joining with its switch card, point 10). The card is `A-SWITCH-WHILE-ANOTHER-CALL-IS-IN-FLIGHT-SAVES-NOTHING-SO-AGENTS-SHARING-ONE-MODEL-LOSE-THEIR-KV-CACHE` (gitignored `backlog.md`), a child of the card of the KV snapshot amendment of 2026-11-13. It extends the switch amendment of 2026-11-17 above and replaces its point 2 ("Who waits"); it narrows one test of the card queue for the card-borrow (point 5) and changes no other card-queue rule. Drafted from the code at f4edd043 (line numbers at that commit) and revised after one context-free code review. Built: nothing yet.)*

**The case.** At a switch on `llama-server` the outgoing conversation is saved only when no other call is in flight: `_save_live_conversation` skips with "N call(s) still in flight" (`llama_server_supervisor.py` 4025-4026), `_kv_save_within` asks again before the request (4110), and the switch restore is refused the same way (4691). DPC has no wait inside one model: calls to the alias on the card go to the engine at once, and with one slot `llama-server` orders overlapping calls itself. So when agent B's call arrives while agent A's call is still generating, B's switch saves nothing, B waits inside the engine behind A and then takes slot 0 over A's conversation, which is lost (with `cache_ram_mib` 0 its next turn re-reads it cold). A peer's call does the same on purpose today (point 2 of the switch amendment: it never waits and never saves). Per the card's entry of 2026-11-17 (CC's ledger mining, 2026-11-09..17, not re-run here): on the 27B 1 switch in 108 calls, none overlapping; on the Strata alias 227 switches in 6,464 calls, of which at most 56 overlapped by wall-clock intervals, 47 of them with a peer. So today the overlap is mostly a peer's; the slot queue is for the scale the frame above names, where several agents share one model.

1. **The word and what it is not.** The **slot queue** is DPC's wait in front of one local alias's slot: who sends calls into the engine next. It is not the **card queue**, which decides which alias holds the card; not `llama-server`'s own ordering of requests, which DPC cannot see; and not the **run gate**, which keeps one run per agent and one agent per room. A call first waits for its model to be on the card, then for the slot (point 5). It covers `llama-server` aliases only until Strata joins (point 10): the hook asks the engine itself, because `StrataProvider._ensure` runs the base `_ensure` (`strata_provider.py` 222).

2. **Who holds the slot, and for how long.** The slot is held by an agent's turn or by one call.
   - **An agent's turn**: its `accepted_request` hold, taken in `DpcAgent.process` around the pre-turn measurement and `run_llm_loop` (`card_held_for`, `dpc_agent/agent.py` 734). It lies inside the glossary's **turn** (run gate held to released), as the card queue's rule 1 says. "Until its turn ends" means until the turn ends **or leaves this alias**: a turn whose next round runs on another alias moves its hold (`card_hold_follows`, `dpc_agent/loop.py` 1947 and 2773) and gives this slot back.
   - **The owner's chat**: the hold in `InferenceOrchestrator._execute_local_inference` (`inference_orchestrator.py` 111) stands around one `query`, so it holds the slot for one call.
   - **A gateway call**: the hold in `Gateway.complete` (`gateway.py` 1112), one call. DPC cannot see a gateway client's own turn; its `caller_window` keeps the card busy between calls (card queue rule 6) and does not hold the slot.
   - **A peer's call**: one call, marked by `serving_a_peer` (`p2p_coordinator.py` 1147), with no hold (KV amendment of 2026-11-13, point 2). Its `caller_window` does not hold the slot either.
   - **A call with no live hold on this alias**: `card_held_for` has three callers (the three above), so every other model call on a local alias arrives without a hold: sleep, a person's Extract Knowledge, a marked background call (`a_background_call`; skill reflection is started fire-and-forget after the turn's hold, `dpc_agent/agent.py` 874), and a tool thread that outlived its turn (below). One call each.
   - **Calls with no hold: a person's or a named background call** (Mike's call, 2026-11-17). The code tells them apart by one mark only, the `_BACKGROUND_CALL` context variable that `a_background_call(what)` sets (`llama_server_supervisor.py` 257), whose names are closed in `_BACKGROUND_CALLS` (249): `self_reflection_confirmation` (`conversation_monitor.py` 1084) and `skill_reflection` (`dpc_agent/skill_reflection.py` 200). **A named background call** is a call made with that mark set and no live hold of its own on the alias. It follows the card queue's rule 10 for the slot: it runs when the slot is free (nobody holds it and nobody waits) and otherwise is skipped by name, never waiting, through the existing skip (`_background_call_skipped`, 2239: the log line, `last_background_skip` and `BackgroundCallSkipped`, with the reason "the slot of '<alias>' is held by <who>" and the name's own `what_follows`). Unlike rule 10 it does not depend on `swap_when_idle`. **A call a person started** is, in code, every other call with no live hold: nothing marks a person's start, so the slot queue treats an unmarked call as one. It waits in the slot queue in its arrival order (point 6). By this rule the knowledge extraction's own calls (`ConversationMonitor._calculate_knowledge_score`, `conversation_monitor.py` 1505; `_generate_commit_proposal`, 1863), the AI-assisted instruction editor (`service.py` 3837) and the image description for agents (`service.py` 8227) wait, and so do sleep's calls (`dpc_agent/sleep_pipeline.py` 509, 1315, 1698), which no person starts: sleep is "a request like others" by the card queue's rule 11, and a new background call that must not wait gets a name in `_BACKGROUND_CALLS`. The list is a grep of `llm_manager.query` callers, not exhaustive.
   - **Not in the slot queue at all:** the tokenizer and window reads (`_engine_post`, `engine_context_window`, `llamacpp_server_provider.py` 1216 and 1249), which call `_ensure_running` only, touch no slot and take no call slot.

3. **Whose call is it: the turn's own passes.** Whether a call belongs to the turn holding the slot is read from the hold, not from the KV record's caller kind: a call passes when `current_card_hold()` is live and stands on this alias, its own or the one it borrowed (`_this_request_holds`, `llama_server_supervisor.py` 866), and that hold is the slot's. The KV reader `_caller_of_this_call` (`llamacpp_server_provider.py` 430) is the wrong test here: under the turn's own live hold it answers "no conversation" for a card-borrow (`_CARD_BORROW`) and for a marked background call (449-456), and a turn's own `describe_image` on its own alias (`dpc_agent/tools/vision.py` 180, `_resolve_vision_alias` returns the agent's alias when its model can see) or its image pre-analysis (`dpc_agent/llm_adapter.py` 1735) would wait behind its own turn. So the turn's own calls pass: later rounds, a compaction, a description, a tool's model call on a thread that carries the hold (`carrying_card_hold`, 856; the tool registry runs an async handler on a loop of its own, `dpc_agent/tools/registry.py` 726). The KV record keeps its own reader, unchanged.
   - **A tool thread that outlived its turn** carries a hold that is no longer live: `_execute_with_timeout` stops waiting at the tool's timeout and the thread runs on (`dpc_agent/loop.py` 1719-1730). Such a call does not wait behind another conversation's turn: when the slot is held by someone else it is refused in the words a description already gets for an ended request (`_ended_request_refusal`, 2223: "the request this description was for has ended"); when the slot is free it passes as a call with no hold.

4. **Giving the slot back, with no hold left behind.**
   - An agent's turn, the owner's chat and a gateway call give the slot back when their hold leaves the alias: at `_hold_ends`, which `card_held_for`'s `finally` reaches on every end (an answer, an exception, a guard's refusal, a Stop, a Kill; 836-839), and at `_hold_moves_to` (800). Both reach the owner loop through `_now_on_the_owner_loop` (747-757), where the slot queue lives.
   - **A one-call grant has an identity.** A call with no hold, a peer's and a gateway call's grant is a token minted before the call's first await (in the provider entry point, or at the door for a peer and a gateway, point 8) and carried in a context variable. Every attempt of the call re-runs `_ensure_for` (`llamacpp_server_provider.py` 1313, 1385, 1540, 1605, 1761) under `_retry_with_backoff` (`providers/base.py` 894, at most `max_retry_seconds`, default 600 s); an attempt that finds its own token passes and never queues behind its own grant. The token is given back in a `finally` around the entry point (`generate_response` 1305, `generate_response_stream` 1365, `generate_with_tools` 1503 with its stream path at 1604, `generate_with_vision` 1731), on an answer, an error, a cancellation or the retries spent, and that release too goes through `_now_on_the_owner_loop`, since the call may run on a tool's own loop. **The slot stays held through the backoff sleeps:** the call is not over, and letting another conversation in between two attempts would turn the retry into a switch. The bound is `max_retry_seconds`.
   - **A grant never lands on a dead waiter.** A release reaches the owner loop by `call_soon_threadsafe`, and a waiter's cancellation reaches it on its own path, so the grant checks that the waiter is still waiting and its hold, if any, still live, and otherwise passes to the next.
   - **Stop reaches a waiting turn.** A person's Stop takes a waiting turn out of the slot queue as well as out of the card queue: `_chat_leaves_the_line` (1181) walks only `_CARD_QUEUE`, so the slot queue gets its own path beside it.
   - A turn that makes no call for a while keeps the slot: a long tool, or a tool waiting for a person's approval (the shell's approval lasts 60 s, `APPROVAL_TTL_SECONDS`, `dpc_agent/tools/shell.py` 2294). That is the cost of a whole turn, as the card queue's rule 1 states it for the card.
   - **The engine.** A child's death or a Stop engine ends no turn. After its grant a call reads the child again and, when it went, asks `ensure_running` again, keeping its grant, so nobody passes it. A drain (a swap, a retire, Stop engine) refuses the peers waiting on that alias with the draining refusal (`_draining_error`, `provider_unavailable` by card queue rule 15). The queue is keyed by alias, so a settings save that replaces the supervisor keeps it, as rule 14 keeps the card queue's; it lives in memory and ends with the backend. When the owner loop is closed or not running, `_foreign_owner_loop` returns None and the owner-loop helpers run inline on the caller's loop (inferred from `_foreign_owner_loop`, not tested); the slot queue then lets calls pass rather than wait on a loop that is gone.

5. **Where it sits, and the card queue.** For the owner's own calls, in the base `_ensure` (`llamacpp_server_provider.py` 406): after `_ensure_running` (415), which is the card queue, and before `_note_engine_call` (425), which writes the record and runs the switch. For a peer and a gateway call, at their door (point 8). The card first, the slot second. The wait runs on the owner loop (`@on_the_owner_loop`, as `kv_at_a_switch` does, 4353).
   - **A waiting turn counts for the card, with one exception.** A turn waiting in the slot queue stands on its alias, so it keeps the holder busy (rule 1) and no swap stops the model under it. The exception is the card-borrow of the turn holding the slot: a description that gives up the alias (rule 9) asks whether the holder is busy with every stand counted but the borrowing request's own (`_busy_for_the_card`'s `but_for`, 2304; `_why_no_card_borrow`, 2196), and a waiting turn that arrived before the borrow's place counts (`_stands_before_the_line`, 1175). The holder's turn would wait for the waiter to leave, and the waiter for the holder's turn to end: a deadlock broken only by the description's bound (300 s for `describe_image`, `waiting_for_the_card_at_most`; the hold's card deadline for the pre-analysis). So a stand that is waiting in the slot queue does not count against a card-borrow by the turn that holds that slot. The waiter then gets the slot after the turn ends and, the alias having been stopped and started meanwhile, asks `ensure_running` again (point 4).
   - **Cost:** a swap for another alias waits behind the whole slot line, because every waiting turn keeps the holder busy; with whole turns that wait can be many turns long, bounded by each request's own card deadline (rule 5).

6. **Order.** The owner's own work (agents' turns, gateway calls and the calls a person started, point 2) waits first in, first out, by the arrival of its first call at the slot queue. Peers wait behind every waiting owner request, first in, first out among themselves: a peer is never ahead of an owner request, the rule the amendment of 2026-11-06 (point 3) keeps for the card. A call that finds the slot free and nobody waiting passes at once.
   - **The owner's chat goes in after the current call** (Mike's call, 2026-11-17), because a person waits for it. When a turn holds the slot, the owner's chat waits only for the call that turn has in the engine; then, before the turn's next call is sent, the turn's conversation is saved (the owner's bound of point 7), the chat's one call is sent, and the turn's next call restores its snapshot, as at any switch back. The turn keeps the slot throughout: the chat holds it for its one call and gives it back to the turn, not to the next in line, and the turns waiting behind stay where they were. Two chats go in one after the other, in arrival order. The chat itself is never saved (a single-prompt call, KV amendment of 2026-11-13, point 4).

7. **The hand-over.** When the slot passes from A to B, B's first call runs the existing switch (`_note_engine_call` → `_kv_at_a_switch`, 548 → `kv_at_a_switch`, 4354): A's slot is saved, B's snapshot restored, and only then is B's call sent inside its call slot.
   - **Calls already in the engine.** A's turn has ended and its own calls have returned, but a call may still be in the engine that no turn holds: a tool thread that outlived its turn (point 3) or a call with no hold that passed while the slot was free. So the hand-over first waits for the alias's calls in flight to end (`_in_flight` at 0, read on the owner loop); a call that arrives meanwhile queues. The in-flight guards (4025-4026, 4110, 4691) stay; when one fires, the line says which call was in the engine, at INFO, as a known case and not a defect.
   - **The wait does not eat the save.** The save bound of an owner's incoming call is today its card deadline less a margin (`_kv_switch_save_by`, 4468), so a turn that waited 29 minutes of a 30-minute `swap_wait_s` would leave its predecessor seconds and the save would be skipped. From the grant, an owner's call gets at least the limit a late request gets (`_STOP_WITH_NO_WAIT_LEFT_S`, 60 s), as `_kv_switch_save_by` already gives a request with nothing left; the 60 s write ceiling stays the cap, and the restore keeps its bound of the switch amendment, counted from the grant.
   - **The slot-held guard** (CC's proposal, accepted without objection). `_kv_why_the_slot_is_held` (4574) refuses a restore while slot 0 holds a peer's or a gateway client's call, or an owner call no snapshot holds (4586-4595), whatever `cache_ram_mib` is (it is asked at 4675-4679, outside the `cache_ram_mib` branch). Behind the slot queue that call's turn has ended, and on an alias with one slot and no host cache (`n_parallel` 1, `cache_ram_mib` 0, the 27B's lines in `providers.json` on 2026-11-17) the incoming call overwrites slot 0 whether the restore lands or fails, so the guard protects nothing there and would stop every return after a peer, a gateway call or the owner's chat from restoring, variant A's saved conversation included. At a hand-over on such an alias the guard is skipped; on any other alias it stands. By code reading, the owner's-chat half of this already costs the return of an agent after the owner's chat today; not tested.

8. **A peer, variant A, and the peer door's semaphore** (Mike's call, 08:10 UTC).
   - **Before the semaphore** (CC's proposal, accepted without objection). `_peer_inference_lock` is one `Semaphore(1)` across every local alias (`p2p_coordinator.py` 110, taken at 1119), and the gateway shares it (`service.py` 1612). The gateway's order rests on "nothing on the semaphore waits for anything behind it" (`gateway.py` 1107-1110; card queue rule 8). A peer waiting in the slot queue inside the semaphore would break that across aliases: a gateway call on another alias would wait out a peer that waits for an owner's turn. So a peer takes its place in the slot queue at the peer door, before the semaphore, and a gateway call takes its place after its card wait and before the semaphore (`gateway.py` 1112-1125). Whoever sits on the semaphore then holds its slot and waits for nothing behind it. Each carries its grant to `_ensure` by the token of point 4.
   - **A peer's call joins the slot queue** (point 6). At its hand-over the outgoing owner conversation is saved before the peer's call is sent, within the peer's own deadline.
   - **The deadline** (Mike's call, 2026-11-17): the smaller of `swap_wait_s` and `[connection] remote_inference_timeout`, counted on `_swap_clock` from the peer's arrival at the peer door, before the semaphore (`p2p_coordinator.py` 1119). The host had none for a peer: the door waited on its semaphore with no limit, DPTP carries no deadline, and the guest gives up at its own `remote_inference_timeout` (default 1200 s, `settings.py` 225), which the host cannot read; the smaller of the two never outlives a guest on default settings and is never longer than the owner's own wait. It is passed to the switch explicitly: `_kv_switch_save_by` gives a call with no hold `swap_wait_s` from the moment of the switch (4478), which says nothing about the peer.
   - **When the deadline comes while an owner's turn holds the slot** (Mike's call, 2026-11-17), the peer goes in between two calls of that turn, saving first, as the owner's chat does (point 6): it waits for the call the turn has in the engine, the turn's conversation is saved within what is left of the peer's deadline, the peer's call is sent, and the turn's next call restores its snapshot; the turn keeps the slot. So that "saving first" has room, the peer stops waiting for the turn's end at its deadline less the save's allowance, the write ceiling plus the switch margin (`KV_SNAPSHOT_WRITE_CEILING_S` plus `_KV_SWITCH_DEADLINE_MARGIN_S`, 62 s). A call in the engine that runs past that allowance is what makes the save not fit, below. A peer is never refused for a held slot; it is refused only by a drain (point 4).
   - **"The save does not fit"** is decided by the save's existing checks, with the bound set to what is left of the peer's deadline less the switch margin (`_KV_SWITCH_DEADLINE_MARGIN_S`, 2 s), never above the 60 s ceiling: "estimate over the bound", "no time left" (`_save_live_conversation` 4032-4034, `_kv_save_within` 4093-4096), or a save cut at its bound (4039-4041). Any of the three: the peer's call goes out, the owner's conversation is evicted unsaved, and its next turn re-reads it. Other skips (already saved, a single-prompt call, a cleared conversation) are not evictions and keep their own lines.
   - **The eviction line**, INFO: "KV snapshot of <agent and conversation> evicted unsaved by peer <node id>: the save (~X s estimated, or cut at Y s) does not fit the peer's deadline (Z s left); re-reading it costs N tokens (~C s cold at R tok/s on K ledger rows)". N is the slot's own count (`n_prompt_tokens` read by `_kv_slot_to_save`), C and R from `_kv_cold_rate`, as the switch decision line already prints them. No prompt text; the peer only as its node id (point 10 of the KV amendment).
   - **The peer no longer passes unseen.** `kv_at_a_switch`'s early return for a peer's or gateway client's call ("KV snapshot save not waited for", 4365-4377) goes: such a hand-over runs the save as an owner's does. The gateway is the owner's own user (card queue rule 7), so its call's save gets the owner's bound of point 7, not the peer's.
   - **Unchanged for peers:** a peer's own conversation is not saved (slice 2 of the KV amendment, point 11, which now waits on the peer's consent as well: Mike's call, 2026-11-17, a peer's conversation reaches this disk only with the peer's consent); the C2 guest entry on Strata is a parked entry and is untouched.

9. **Bounds, and what a waiting call sees.**
   - **What bounds who holds the slot.** An agent's turn: `max_rounds` (`[dpc_agent] max_rounds`, default 200, `settings.py` 397; `RoundLimitGuard`, `dpc_agent/loop.py` 2364), its tools' own timeouts, and a person's Stop or Kill. No wall-clock limit on a turn was found in `loop.py` (a grep for a turn timeout, not exhaustive). Every other holder is one call, with its retries (point 4).
   - **An owner's wait** (CC's proposal, accepted without objection): no deadline in the slot queue; the wait ends with the turns ahead, a person's Stop, or the backend's stop. That is the "30 minutes and more" Mike accepted; the card queue's one deadline (`swap_wait_s` from acceptance, 1800 s on Mike's node) would refuse a turn that waited exactly that long. The cost: a holder that hangs is bounded only by its tools' timeouts, `max_rounds` and a Stop. A gateway call keeps its card deadline, because a client on the other end of a socket times out on its own.
   - **An agent's turn waiting** reads a new `wait_reason`, `slot_queue` (beside `card_queue` in `TURN_WAIT_REASONS`, `dpc_agent/run_gate.py` 59), from its first real wait, as `card_queue` does.
   - **The owner's chat** gets nothing new on screen in this slice; the log says it waited.
   - **A peer** is told nothing while it waits: nothing changes on the wire, and its guest waits as for any slow call. A drain's refusal is `provider_unavailable` in the form of point 5 of the amendment of 2026-11-06, naming no agent, conversation or caller.
   - **Status**: `local_engine_status` gains a per-alias key `slot_queue`: who holds the slot (kind and since) and who waits, in order, with kind and since, positioned with no position field as `card_queue` is.

10. **Strata** (CC's proposal, accepted without objection). The slot queue's order matters on Strata too, but its value there is the save at the hand-over, which Strata does not have: `kv_at_a_switch` and the provider's switch return for any engine but `llama-server` (4363, `llamacpp_server_provider.py` 556), and Strata parks the outgoing conversation in its own RAM cache (`conversation_cache_slots`, 2 on both of Mike's aliases) at a few hundred milliseconds a restore. Ordering alone would make Strata agents wait whole turns for fewer parks and nothing on disk. Strata joins the slot queue in the commit that gives it the save and the restore at a switch, under its own card (`A-STRATA-DOES-NOT-SAVE-THE-OUTGOING-CONVERSATION-AT-AN-AGENT-SWITCH`, whose first step is a raw-HTTP probe); until then its calls pass as today, by the engine guard of point 1. Strata's save and restore at a switch are that card's, not this amendment's.

11. **What is said.** INFO lines, each naming the alias; who is "agent <id> conversation <id>", "the owner's chat <conversation id>", "gateway client <name>", "peer <node id>" or "a call with no hold":
    - joining: "slot queue[<alias>]: <who> waits, <n> ahead; the slot is held by <who> for <s> s";
    - the hand-over: "slot queue[<alias>]: the slot passes from <A> (held <s> s, <k> calls) to <B> after a wait of <w> s", with the calls in flight it waited for, if any;
    - going in between two calls: "slot queue[<alias>]: <the owner's chat | peer <node id>, its deadline less the save's allowance reached> goes in between two calls of <A> after a wait of <w> s";
    - a named background call skipped: the existing skip line of rule 10 with the slot's reason (point 2);
    - giving back with nobody waiting: "... given back by <A> (held <s> s, <k> calls): <the turn ended | failed | was stopped | moved to <alias> | the call ended>";
    - leaving without the slot: "... <who> leaves the slot queue after <w> s: <Stop | the engine is draining | its turn has ended>";
    - the peer eviction line of point 8; the switch decision line and the save and restore lines of the switch amendment, unchanged.
    The revisit Mike asked for reads these: waits and holds per turn, hand-overs per hour, and how often a peer evicted.

12. **Tests the code commit starts with, red first.**
    - B's first call arrives while A's call is in flight under A's hold: B waits; A's later round is sent before B's first call; when A's hold ends A's conversation reaches the KV snapshots folder (an index entry and its file), then B's call is sent; no "still in flight" line is logged. (The card's own test.)
    - A's own `describe_image` on A's alias and A's pre-analysis pass while B waits; a tool thread carrying A's live hold passes; a tool thread whose turn has ended is refused while B holds the slot.
    - A's card-borrow for another alias goes ahead while B waits on A's alias, and B gets the slot after A's turn ends (no deadlock).
    - A's turn ends by an exception, by a cancellation (Stop, Kill) and by a move to another alias: each gives the slot to B. A call with no hold keeps its grant across a retry and gives it back when its entry point raises, retries spent. A grant whose waiter was cancelled passes to the next.
    - A person's Stop takes a turn out of the slot queue.
    - A call still in the engine at a hand-over is waited for before the save.
    - Order: an owner's turn that arrives after a waiting peer goes first; two owners' turns go in arrival order.
    - A peer waiting in the slot queue does not hold the peer door's semaphore: a gateway call on another alias passes it.
    - A peer with time: the owner's conversation is in the folder before the peer's call is sent. A peer without time: its call is sent, nothing is written, and the eviction line names the peer's node id and the tokens.
    - A drain while a peer waits refuses it with the draining refusal; a child that died while an owner's turn waited is asked for again and the turn keeps its place.
    - A return after a peer restores its snapshot on a one-slot, no-host-cache alias; a Strata call passes the hook as today (pinned so that joining is a deliberate change).
    - The peer's deadline: with `swap_wait_s` 1800 and `remote_inference_timeout` 1200, a peer that arrives while A's turn holds the slot stops waiting 1,138 s after its arrival at the door; the time spent on the semaphore is inside it.
    - A peer at its deadline less the save's allowance while A's turn holds the slot: it goes in after A's call in the engine ends; A's conversation is in the folder before the peer's call is sent; A's next call restores A's snapshot; A keeps the slot and B, waiting behind A, still waits.
    - The same with A's call in the engine running past the allowance: the peer's call is sent, nothing is written, and the line "evicted unsaved by peer <node id>" names the tokens.
    - A call marked `a_background_call("skill_reflection")` while A's turn holds the slot is skipped by name (`last_background_skip` names the slot) without waiting; the same call with the slot free runs. An unmarked call with no hold (a knowledge extraction's) waits in the slot queue behind A's turn.
    - The owner's chat while A's turn holds the slot: it is sent after A's call in the engine ends; A's conversation is saved first; A's next call restores; a second chat follows the first; turns waiting behind A stay behind A.

13. **Not changed.** The card queue's rules but the card-borrow's busy test of point 5; the peer door's refusals for the card (the card `A-PEER-REFUSED-FOR-A-BUSY-CARD-IS-NOT-QUEUED-AND-THE-GUEST-DOES-NOT-ASK-AGAIN`, decided 2026-11-09, stays its own work); every gate of KV slice 1 other than point 2 of the switch amendment; peers' snapshots (slice 2); `n_parallel` and `cache_ram_mib` (Mike's lines in `providers.json`); and the C2 guest entry.

**Names.** **slot queue** is Mike's (2026-11-17); its GLOSSARY row goes in the code commit. Confirmed by Mike the same day (DPC Project chat), after the card queue's own names: `_SLOT_QUEUE` (the module's queue per alias, as `_CARD_QUEUE`), `SlotQueueRefusal` (as `CardQueueRefusal`), the `wait_reason` value `slot_queue`, the status key `slot_queue`, and the log words "evicted unsaved" for a peer's push of an owner's conversation out of the slot, which is DPC's act and not the engine's eviction that Strata counts (`evict_oldest`, the engine eviction counter). Confirmed by Mike on 2026-11-18 (same chat), from the code: `in_the_slot_queue` (a peer's or gateway call's place, taken at its door), `slot_queue()` (the status, beside `card_queue()`), `TakenOutOfTheSlotQueue` (a Stop out of the slot queue; a subclass of `TakenOutOfTheCardQueue`, so every branch that handles a Stop out of a queue still applies), its chat line "Stopped by user in round N while waiting for the slot of '<alias>': another conversation was using the model, so no model call was made." (his rule: true and clear to the person and to the agent who reads it), and the UI phrase "waiting for the slot".

**Prior art outside our engine** (written after the amendment, under the template rule of the same day, Mike's call, 2026-11-17; the survey with every source is the gitignored `ideas/slot-queue-prior-art-2026-11-17.md`, searched 2026-11-17 over `ideas/dpc-research/articles` and the web).
- *vLLM* (V1 scheduler; PagedAttention, arXiv 2309.06180): preempts by FCFS or priority and resumes by recompute; prefix blocks evicted LRU. Not taken: GPU-to-CPU regime with many slots; on an HDD with one slot the save-or-cold decision above stays.
- *SGLang* (RadixAttention, arXiv 2312.07104; MambaRadixCache): longest-prefix scheduling, LRU leaf eviction, a hybrid model's checkpoint at the reusable prefix frontier. Taken as confirmation: the turn base of the KV amendment is the same idea; the longest-prefix order is not taken (its own paper warns of starvation).
- *LMCache* (arXiv 2510.09665; clone at 97e71cbb): prefix-chained 256-token chunk hashes, prefix-only match, LRU/ARC tiers; CacheBlend (arXiv 2405.16444) for non-prefix reuse. Not usable here (Linux, CUDA, vLLM/SGLang only); chunk hashing and CacheBlend noted in `ideas/adopt-from-lmcache-2026-11-17.md`.
- *InferCept* (arXiv 2402.01869), *Continuum* (arXiv 2511.02230): at a tool call choose preserve, discard or swap by cost, or keep the KV for a TTL then evict. Not taken in this amendment: a turn keeps the slot through its tools (Mike's call), so on one slot the card idles while a tool runs; handing the slot over during a long tool when someone waits is left to a child card.
- *CachedAttention* (arXiv 2403.19708): queue-aware eviction beat LRU in its setup. Not taken yet: protecting waiting turns' snapshots from the TTL and the cap is a child card.
- *llama.cpp upstream*: PR #29738 (session-keyed automatic slot save/restore, open, conflicting, no maintainer review, 2026-11-09) saves a stamped slot when it is reused, restores the whole file by a client session id, caps by count (128), and defers a request whose slot is busy instead of evicting it. It would replace at most the slot-file transport, not this policy (key and turn base, TTL and byte cap, peers, reset, the save-or-cold decision, the order of this queue). PR #32303 ("implement disk cache", open since 2026-11-15, after #28092 closed unmerged) is the content-hash design the maintainers chose; it is the one to watch. #26004 (checkpoints in the slot file) is in the b12110 pin; #28194 (no reuse after a hybrid restore) is still open upstream. The live 27B restores here reused 91-96 % where the request diverged after the first message; where it diverges inside the first message the reuse is 0 here too (P6 `sys_half`: no checkpoint below the divergence, a full re-read no faster than cold), so the difference from #28194 is the depth of the divergence, not the absence of the defect.
