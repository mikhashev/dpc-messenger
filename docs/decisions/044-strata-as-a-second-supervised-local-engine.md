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

Mike's call, team chat 2026-11-02, after review by Ark, Johnny and Zcode. **Decided, partly
built.** In the code: rule 1, with `accepted_request` as the only kind of hold, at the two doors
it names and on a gateway call; rule 2, no mark while busy; the queue and its cancel (rules 3 and
12); the order (rule 4); the single deadline (rule 5); rule 6, the `caller_window`, as built
there; the gateway and peer doors (rules 7, 8 and 15); rule 9, the from-inside refusal; rule 10,
as built below; rule 13's switch and safety net; `wait_reason: card_queue`; and the status key
`card_queue`. Not in the code: rule 9's second sentence. `swap_when_idle` stays off by default until
all of it is live-checked. The work is the epic
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
   agent) queues as a normal request. **Decided, not built** (Mike's call, variant B, 2026-11-02):
   a request's auxiliary call may give up its own alias, through the queue, within its one
   deadline, on a stop that loses no parked conversations (`DROPS_PARKED_ON_STOP` false on the
   stopped engine; that direction confirmed as the default, Mike's call, 2026-11-03), with the
   description cached once per turn; background calls never. Until that is built the refusal above
   stands.
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
stand is the owner's question.

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
