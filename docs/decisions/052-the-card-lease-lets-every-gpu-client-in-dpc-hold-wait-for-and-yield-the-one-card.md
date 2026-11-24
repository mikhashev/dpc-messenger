---
adr: 052
title: "The card lease lets every GPU client in DPC hold, wait for and yield the one card"
status: "implemented in part: S1 to S4 built, S5 to S8 and S10 pending, operation not yet observed; the ADR as a whole has no recorded acceptance by Mike"
date: 2026-11-21
axis: network
deciders: [Mike]
consulted: [CC, Ark, Zcode, Johnny, Forge]
informed: []
depends_on: [ADR-044, ADR-051]
related: [ADR-046]
supersedes: []
session: "AI Studio group chat (group-60aff6110f02), 2026-11-21 — Whisper plan step 2; Mike approved at #20 that llama-server and Strata wait for Whisper or music to free the card, and that the embedder and other GPU-capable parts join later"
---

# ADR-052 — The card lease

> **Proposed 2026-11-21. S1 to S4 are built since (2026-11-22 to 2026-11-24); S5 to S8 and S10 are
> not.** Built means: S1 `53a2eaf5`, S2 `1e57932f`, S3a to S3e (`a5b52f79` through `ac9307d5`, with
> `5984876e`), S4 `b67b3cc0`, then follow-up 8 (`bf11758b`), 9a (`4ad35c86`) and 9b (`05bcce1d`); Zcode
> accepted S3e and S4 in review (#87, #88 of 2026-11-24). It has not run long enough to say how the
> figures behave in operation, and the record holds no acceptance of the ADR as a whole by Mike, only
> the decisions listed under **Decided by Mike**; so the front matter says what is built and does not
> say `accepted`. The sections below were written as a design and are corrected where the build or a
> later decision changed them; **Implementation Status** and **Review notes** say where. The design
> was produced read-only by an
> agent for CC and checked against `dev` HEAD `b9725990`. **Revised the same day** to answer
> two independent reviews in the AI Studio group chat: Zcode (#41) and Ark (#44, #45), and
> then their second reviews of decision 6 (Zcode #52, Ark #53). Every reviewer claim was
> checked against the code at `dev` HEAD `5099b326` (docs-only commits since, up to
> `db1b2022`) before it was written in; the claims that did not hold are listed under
> **Review notes**, with the evidence. Line numbers drift.
>
> **Decided by Mike so far, and only this:** at #20 he approved that llama-server and Strata
> wait for Whisper or music to free the card, and that the embedder and other GPU-capable
> parts join later. At #62 he chose a **smart GPU/CPU choice for dictation and
> transcription** (rule L12). At #68, #70 and #71 (2026-11-21) he decided: Whisper never
> loads beside llama-server or Strata (#68); a peer may wait when the line is short and is
> refused when it is long (#70); music unloads an idle server when nothing waits, amending
> SPEC decision 32 (#70); the CPU may take files up to 5 minutes even while Strata is busy
> (#71); a model stays in memory only while something waits for it (#71); peers do not use
> the owner's CPU by default, with an optional setting (#71). Section **Decided by Mike**
> lists them; **Open for Mike** keeps what is still open. Anything marked **[unverified]**
> was not confirmed. **Since then (2026-11-22 and 2026-11-23):** item 7 (Whisper's idle
> unload, 300 s), and, at #15 of 2026-11-23, items 1a, 2, 6A and 9 at their proposed defaults,
> item 7's carrier, and one VRAM reserve per engine; at #17, the L13 rule for a peer's long audio
> (Decided by Mike).
>
> **Second revision** (same day): answers Zcode #59, Ark #60 and #61, and Mike #62, checked
> against `dev` HEAD `d80d76f7`. **Third revision:** answers Ark #64 on the GPU/CPU choice
> (L12 compares times, counts the CPU's cold start and contention, and the CPU route is a
> separate provider object), same HEAD. **Fourth revision:** records Mike's decisions
> #68–#71 with Ark #67 and Zcode #69, checked against `dev` HEAD `0b017ebd`.
>
> **Fifth revision:** settles the four points that had to be fixed before S1 code (Johnny
> #83, Ark #82, Mike #85; CC #84, #87, #89), checked against `dev` HEAD `84425302`: a peer's
> wait builds on ADR-044's amendment of 2026-11-18 (rule L13); the VRAM quantities L11 reads
> are named, with units, platforms and the PDH bias (L11, "The quantities"); what swapping
> Strata out drops and who is told (section 2.4); and what an agent's own turn does when it
> needs a transcription (section 3.5). No decision of Mike's changes. The four points raised
> four questions that only he can settle; they are Open for Mike, items 10–13.
>
> **Sixth revision:** records Mike's answers to items 10–13 and the four reviews of
> `c3852d75`, checked against the same HEAD. The group's message count restarted after #89;
> numbers from here are the new count, all on 2026-11-21: #10 CC (17:01), #11 Mike (17:13),
> #12 Zcode (17:22), #13 Ark (17:25), #14 Johnny (17:53), #15 Forge (18:01). Mike's governing
> line at #11: "don't overcomplicate for now; we'll learn the timings and the queue in
> operation; the music player needs transcription too". So this revision adds no mechanism:
> it fixes contradictions and wrong facts, records the answers, and says what is left to tune
> from operation.
>
> **Seventh revision:** the four reviews of `ffd06369` (new count #18–#22) and Mike's answers of
> 2026-11-22 (item 7, N11). Text only, no new mechanism (Mike #11): one scale and one view for
> `need`, Whisper's measured overhead, a carrier for the real-time ratio, the two doors of
> "unknown", and the accepted consequences in one sentence each.
>
> **Ninth revision:** the last review before S1 code (new count #30–#33). Text only: Whisper's
> estimate divides by the ratio, a peer has one clock, the flat peak is a named choice.
>
> **Tenth revision, 2026-11-23 (text only):** records Mike's answers of 2026-11-23 (AI Studio
> group, #7–#9 and #15; CC's proposal #14, confirmation #16) and the reviews #10 Ark, #11 Johnny
> and #12 Forge, folded into the S3 plan. Items 1a, 2, 6A and 9 move from Open for Mike to
> Decided by Mike; the design text is not rewritten, only the places that said "proposed" or
> "open" for those items. One reserve per engine replaces the single 2048 constant (L11, "One
> reserve"). The numbers #7–#16 are the group's count of 2026-11-23.
>
> **ADR-051 N11** (how an external GPU job joins the card queue): Mike decided on 2026-11-22
> that it is in scope and that CC writes S10 (Decided by Mike). This ADR answers it as
> "(b)+(c)".

## Context and Problem Statement

DPC has one GPU card (an RTX PRO 4500, 32 GB on the owner's machine) and, since ADR-044,
one registry of who holds it: `_CARD_CLAIMS` in `managers/llama_server_supervisor.py`,
read by `card_holders()` and by the card queue. That registry knows only the supervised
llama.cpp children (llama-server and Strata). Every other GPU client in the backend is
invisible to it and to each other: Whisper loads its model on demand, the music engines
(YuE2, ACE-Step) are supervised children that never appear in `card_holders()`, the
embedder and GLiNER pick `cuda` when it is there, and any process DPC did not start
(ComfyUI, an external music app) is outside the picture altogether. So three things go
wrong:

- A transcription during a running YuE2 job loads Whisper beside it and nobody waits or
  names a holder (the Done-when of the Whisper card, backlog
  `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…`). On the owner's 32 GB card both fit, so it
  works today; on a smaller card it is an out-of-memory failure of one of the two, and
  nothing in DPC decides which.
- Mike's ask at #20, that llama-server and Strata wait for Whisper or music, has nothing
  to act on, because those clients hold nothing the queue can see.
- A GPU job DPC did not start can neither ask an idle holder to give the card back nor be
  waited for (backlog
  `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED`,
  filed 2026-11-06; ADR-051 N11).

### 0. What the code does today

Findings of the design, which correct or sharpen the earlier survey, plus the corrections
the reviews brought (marked with the review number).

**The card registry** (`managers/llama_server_supervisor.py`)

- `_CARD_CLAIMS` (657) is a `Dict[LlamaServerSupervisor, str]`. Values are written only by
  `_claim_card` (2486) and `stop()` (5425–5440). **Entries are removed in three places**
  (Zcode #41, count corrected by Zcode #59): `stop()` (5440), `_live_claims()` (667, a
  child that exited on its own) and `_release_card_unless_live()` (3180, a start that
  failed before leaving a live child).
- `_live_claims()` (660) reads it, and reads each supervisor's `_proc`, `props` and
  `_draining` to name the state (`stopping`, `draining`, `running`, `starting`). Its callers
  are `card_holders()` (681), `_blocking_holders()` (2500), and
  `strata_supervisor._pin_launcher_running` (~2829).
- `_excludes(other)` (2479) is `other.ENGINE != self.ENGINE or either.EXCLUSIVE_ON_THE_CARD`.
  llama-server sets `EXCLUSIVE_ON_THE_CARD = False` (2301), so two llama-server children
  may hold the card together today; Strata sets `EXCLUSIVE_ON_THE_CARD = True` and
  `DROPS_PARKED_ON_STOP = True` (`strata_supervisor.py:1038-1047`). Several holders at once
  is therefore already normal, decided by **engine identity** (Ark #53).
- Tests poke the dict directly: `conftest.py:86`,
  `test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card.py:554`,
  `test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced.py:360,1498` and
  8 more. So `_CARD_CLAIMS` must keep its exact shape.

**The queue**

- `_CARD_QUEUE` / `_CardPlace` / `_QueuedRequest` are at 1025–1180 and `card_queue()` at
  1040. `_CardPlace.arrival` (1033) is drawn from the one clock `_CARD_ARRIVAL` (698), the
  same counter `_CardHold.arrival` uses.
- The queue only exists while `swap_when_idle` is on. `_wait_in_the_card_queue` (2787)
  returns None when the switch is off (line 2805, **before** its background-call check at
  2809), and `_claim_card` then refuses at once with `CardHeldError`.
- **How a holder is found** (Zcode #41): `_blocking_holders()` walks `_live_claims()` and
  reads `other.alias` and `_excludes(other)` (so `ENGINE` and `EXCLUSIVE_ON_THE_CARD`);
  `_blocking_holder()` asks `_this_request_holds(other.alias)`; `_card_held_error` reads
  `other.alias`, `other.ENGINE`, `DROPS_PARKED_ON_STOP` (through
  `_description_may_not_stop`) and `holder._draining` (through `_why_no_card_borrow`).
  `_proc` and `props` are read only inside `_live_claims()`, so a lease must reach these
  readers through `_live_holders()` and never through `_live_claims()`.
- **What `_free_the_card` (3009) touches on a holder**, grepped at `5099b326` (every
  `holder.<private>` read in `managers/` and `providers/`): `alias`, `ENGINE`, the state
  word from the holder tuple, `_draining`, `_swap_for`, `_busy_for_the_card(state,
  but_for)`, `_begin_swap_drain(new)`, `_end_swap_drain()`, `DROPS_PARKED_ON_STOP`,
  `_parked_conversations()` (awaited unconditionally by `_stop_for_the_swap`, 3123), and
  `stop_by_hand(holder, why="swap")` (1615) via `_stop_for_the_swap` (3117).
  `_will_not_reopen()`, `_check_a_wait_can_end()`, `_start_lock`, `_successor` and
  `_in_flight` are read only on the **waiting** supervisor (`self`), never on a holder.
- The docstring of `_free_the_card` (3016–3021) and the comment at 3072 ("no await since
  the last check above") keep one invariant: the last holder check, the busy test and the
  drain mark are one synchronous stretch, and so are the last check and the return before
  `_claim_card`. The stop itself is awaited between rounds (`_wait_for_the_stop`, 3091),
  and each round re-checks the holder.
- That small surface is what makes a lease holder possible without rewriting the queue.

**Request holds**

- The `_CARD_HOLD` contextvar (754) follows the agent turn into tool threads:
  `dpc_agent/loop.py:1656` uses `carrying_card_hold` (883).
- So a lease can tell when "this request already stands on X". The existing pattern for
  that check is `_this_request_holds` (893).

**Music**

- `music/runner.py:656-683` `_wait_for_card` polls `card_holders()` (through
  `default_card_holders`, runner.py:156) every 3 s and stops nothing (SPEC decision 32). It
  returns False as soon as the job is no longer `queued` (661–662).
- It also treats a failed read of the holder list as a free card (`runner.py:665-667`,
  "treating the card as free"). That breaks the Ruach rule that a failed read is never
  "all free".
- `music/jobs.py:607-632`: `set_waiting_for_card` refuses with `illegal_transition` unless
  the job is `queued` (621), and `clear_waiting_for_card` goes through it (634). The runner
  catches that `MusicJobError` and **re-raises it unless the job was cancelled**
  (`runner.py:671-676`).
- `MusicEngines.exclusive` (`music/engines.py:95-129`) stops an idle other music engine
  with no switch.
- `SupervisedEngine.start()` and `stop()` both run under `async with self._the_lock()`
  (`music/engine_base.py:444-448`, `591-595`). `in_use()` (658) counts `_busy`; `busy`
  (359) reads it; `_idle_loop` (667) stops an idle engine. Music never appears in
  `card_holders()`.
- The runner's worker is one task created at `MusicJobRunner.start()`
  (`runner.py:455`), so a job never inherits the `_CARD_HOLD` of the turn that proposed it.
  There is no agent tool in core that proposes a music job today (grep for
  `music_propose`, `propose_job` under `dpc_agent/tools`: none).

**Whisper** (`providers/whisper_provider.py`; read, not edited, since another agent is
editing it)

- `transcribe(audio_path, release_if_loaded_here=False, language=None, task=None)` (872)
  takes `_load_lock` and loads under it.
- `unload_if_idle(reason)` (516) returns **False both when the model is not loaded and
  when a transcription is running** (Ark #45). `_unload_locked` (446) returns True when
  nothing was loaded.
- The rest of the surface: `_active_transcriptions`, `unload_model_async(reason)`,
  `schedule_idle_unload`, `hand_over_to(new)` / `_current_owner()`, `retire()`,
  `shutdown_unload()`, `_finish_thread` (472), and `ensure_model_loaded()` (340, the
  preload that `voice_service.py:228` calls).
- **What `_finish_thread` can end in** (read at `d80d76f7`; it matters for when a lease may
  be released, section 3.1):
  1. the thread ran `fn` → `(result, error, cancelled)`; a cancel of the caller is held
     until the thread ends and re-raised by the caller after its bookkeeping;
  2. the worker task was cancelled **before its thread started** → `(None,
     RuntimeError("the worker was cancelled before its thread started"), False)` at once,
     or `CancelledError` when the caller was cancelled too (`d80d76f7`); `fn` never ran;
  3. the worker future ended with an exception without running `run` (an executor that
     refuses new work at shutdown) → the exception **escapes** `_finish_thread`, because
     the shield loop catches only `CancelledError` (Ark #60 §2; the docstring names three
     return fields, not this fourth outcome); `fn` never ran;
  4. a thread that never ends holds `_load_lock` until the shutdown bound; nothing bounds
     it otherwise (stated in `d80d76f7`).
  In `_unload_locked` (446), outcomes 2 and 3 raise before any log or announcement, and
  `model_loaded` stays True with the pipeline intact. `_unload_model` (358) sets
  `model_loaded = False` both on success and when the release itself raised (it returns
  False then).
- Device is chosen inside `_load_model` (`_detect_device` imports torch).
- Callers of `transcribe`: `service.py:4797` (owner's voice); `p2p_coordinator.py:1398`
  (peer; **not** wrapped in `serving_a_peer`); `dpc_agent/tools/transcribe.py:172` (agent
  tool, runs inside a turn that holds `accepted_request`; tool timeout 1800 s at 293);
  `music/commands.py:324` (music Transcribe, `release_if_loaded_here=True`). That is **four
  doors** (Forge #20-7): Telegram has no call of its own and reaches the provider through
  `service.transcribe_audio`; the fifth hit, `whisper_provider.py:886`, is the provider's own
  forward to its successor.
- `service.transcribe_audio` (4615–4657, the local provider call at 4797) serves the
  owner's dictation command, received voice messages, and both Telegram paths
  (`coordinators/telegram_coordinator.py:509`, `managers/agent_telegram_bridge.py:1674`).
- **Deadlines on the way to a local transcription** (corrected by Ark #53):
  - `service.transcribe_audio` itself has **no deadline**; it awaits the provider unbounded.
    Neither Telegram call site bounds it either.
  - `[voice_transcription] timeout_seconds` = 240 (`settings.py:358`) is **not** a limit on
    local transcription. Its one consumer, `service.py:5102`, is a receiver's passive wait
    for the *sender's* transcript of a voice message, after which the receiver transcribes
    itself.
  - The one real caller limit on the owner's dictation is the UI's `transcribe_audio`
    command timeout, 240 s hard-coded at `ui/src/lib/coreService.ts:1492`.
  - The agent tool's timeout is 1800 s (`dpc_agent/tools/transcribe.py:293`).
- **The received-voice path cascades over providers** (Ark #53): `service.py:5152-5160`
  loops over `provider_priority` (default `whisper-large-v3-turbo, whisper-medium,
  whisper-small, openai`, `settings.py:361`) with `except Exception: continue`. Before it,
  step 8 (`service.py:5141-5145`) drops the local attempt if the peer's transcript has
  arrived; step 9a re-checks after transcribing and prefers the peer's text. Receivers wait
  `recipient_delay_seconds` = 3 (`settings.py:357`) before they try.

**The embedder** (`dpc_agent/memory.py`)

- `get_embedding_provider` (407) is a per-process singleton under a `threading.Lock`.
- `EmbeddingProvider.device` defaults to "auto" (cuda, then mps, then cpu), and there is a
  class-level `_gpu_semaphore = threading.Semaphore(1)`.
- It is synchronous and runs in worker threads (the heavy caller is
  `agent_manager._sync_index` ~368/592 `embed_batch`).
- **`unload()` (798) only sets `_model = None`.** No move to CPU, no `gc`, no
  `empty_cache`, so it does not actually free VRAM.
- It is "forced onto the CPU" only by configuration: `[memory] embedding_device` /
  agent-profile `embedding_device: cpu`, documented in the backlog cards at 14490 and
  11770.
- Callers (orbit plus grep): `agent.py:233`, `context.py:213`, `retrieval/factory.py:145`,
  `tools/core.py:1113`, `agent_manager.py:368`.

**GLiNER** (`dpc_agent/knowledge_graph.py:130-165`)

- `_get_gliner_model(device)` is a process-wide singleton.
- Auto means `"cuda" if torch.cuda.is_available()`, and it has **no unload path at all**.
- This machine's `~/.dpc/config.ini` has `gliner_device = cpu` and
  `[local_engines] swap_when_idle = true, swap_wait_s = 1800`.
- It runs in a worker thread from `sleep_pipeline.py:~1510`.

No other GPU users exist in `dpc_client_core` (grepped for torch, cuda, onnxruntime,
ctranslate2 and mlx).

**VRAM readers**

- Per-process, in `process_registry.py`: `read_vram_windows()` (2028) uses the PDH
  counter `\GPU Process Memory(*)\Dedicated Usage`, a WDDM counter, so vendor-neutral on
  Windows; it is a commitment, not a physical figure. `read_vram_nvml()` (2112) works on
  Linux, NVIDIA only. On macOS, `_default_vram_reader` returns `None`.
- `nvidia-smi --query-compute-apps` reads `[N/A]` per process under WDDM
  (`eval/gaia/campaign.py:129`), so it is useless for per-process figures on Windows.
- Device-level, **in `llama_server_supervisor.py`, not `process_registry.py`** (Zcode
  #41): `_free_vram_mib()` (1793) runs `nvidia-smi --query-gpu=memory.free` as a
  registered device probe with a 10 s timeout. It returns None both when there is no
  nvidia-smi and when the read fails, and cannot tell the two apart. `_total_vram_mib()`
  (1814) is its twin for physical memory.
- `_DESKTOP_RESERVE_MIB = 2048` (1779) is the desktop's share of the card in the admission
  arithmetic: `strata_provider.py:451` admits a peer start only when `free >= total -
  _DESKTOP_RESERVE_MIB`. **Since S3e** the gate reads the alias's own reserve instead (`vram_reserve()`,
  `strata_provider.py:457-465`): `free >= total - reserve`, with 2048 only as the last fallback (L11,
  "One reserve per engine"); `_DESKTOP_RESERVE_MIB` is now at `llama_server_supervisor.py:1989`. It is not a
  CUDA-context figure (Ark #44): Whisper's own overhead
  needs its own constant (L11, `_WHISPER_OVERHEAD_MIB`).

**The frontend already reads the card keys** (Zcode #41; this closes Q4). Twelve files under
`dpc-client/ui/src`:

- holder and queue rows: `lib/components/ProvidersEditor.svelte` (557, 575; the
  `cardStatus()` snippet at 2375, rendered once at 2538 and only when a `strata` or
  `llamacpp_server` provider is configured), `lib/utils/cardQueue.ts` and
  `cardQueue.test.ts`;
- the music job's wait: `lib/utils/musicCreate.ts` (44, 144–146: `cardHolders()` returns
  holders **only while the job is `queued`**), `musicCreate.test.ts`,
  `lib/components/music/MusicCreate.svelte` (401–414, the "Stop X" buttons);
- the turn's wait reason `card_queue`: `lib/utils/turnCommandWait.ts` (12),
  `turnCommandWait.test.ts`, `lib/utils/agentMap.ts` (353), `agentMap.test.ts`,
  `lib/utils/agentTurnNotice.ts` (11, 158), `agentTurnNotice.test.ts`.

What those files assume (section 4 says what each lease row must do about it):

- `card_holder` in `local_engine_status` is `card_holders()[0]`
  (`llamacpp_server_provider.py:1991-1997`); the panel prints it as "Card holder: X" with a
  **Stop engine** button that calls `stop_local_engine(X)`.
- `otherCardHolderLines` (`cardQueue.ts:37-39`) prints the rest as "Also on the card:
  alias (engine, state)".
- `cardQueueLines` (`cardQueue.ts:42-51`) prints a row whose `give_up_at` is `null` as
  "coming back from a description".
- `cardQueue.test.ts:90-105` pin the source strings
  `otherCardHolderLines(engineStatus?.card_holders)` and `card_holders: r.card_holders ??
  []`.

## Decision Drivers

- **One card, many clients.** Whisper, two music engines, the embedder, GLiNER, llama-server
  and Strata all want the same 32 GB, and only two of them are visible to the queue.
- **Mike's #20 ask.** llama-server and Strata must wait for Whisper or music, and later
  parts must be able to join without a redesign.
- **Mike's #49–#50 question.** Dictation that meets a busy card should queue, "the same
  mechanism as the model queue", rather than be refused.
- **Mike's #68–#71 decisions.** No Whisper beside a local LLM; peers wait on the owner's
  terms, as for inference; music takes the card from an idle server when nothing waits; the
  CPU takes files up to 5 minutes; nothing stays in memory without a queue for it.
- **The queue already works.** `_free_the_card` reaches a holder through a small
  duck-typed surface. A new kind of holder that speaks that surface costs a few edits, not
  a rewrite of a 5,555-line file other agents are editing.
- **Cross-platform.** Windows, Linux and macOS are mandatory. The arbitration among DPC's
  own clients must not depend on any vendor tool being present.
- **A failed read is never "free".** The Ruach rule. Today music breaks it
  (`runner.py:665-667`).
- **Reload cost differs by client.** Unloading Whisper is cheap. Stopping an idle music
  engine costs an engine boot and a 5–9 GB model reload for the next job. Stopping Strata
  drops parked conversations and costs a long reload. The rules should follow that cost.
- **Existing tests must see identical behaviour** when no lease is registered.

## Prior art outside our engine

Required by the template (Mike's call, 2026-11-17).

- **(a) The project's own reading.** Searched 2026-11-21 with `rg --no-ignore -i -l -w
  "lease|gpu scheduling|time-slic\w*|gpu sharing|MPS"` and `rg --no-ignore -i -n "gpu
  (sharing|scheduling|arbitrat)|model (swap|unload)\w*|keep_alive"` over
  `dpc-messenger/ideas/dpc-research/articles` (47 files). **Nothing found**: no hit for
  either pattern. The articles do not cover GPU arbitration.
- **(b) The topic, not tied to our engine.** Not re-fetched for this ADR; each line is
  from general knowledge and is **[unverified]** here until someone reads the source:
  - Ollama keeps models resident for a `keep_alive` time and unloads idle models to make
    room for a new one: the same "idle holders yield, busy ones are waited for" shape.
  - llama-swap (a proxy that swaps llama.cpp servers per request) is the same idea for
    inference servers only.
  - Kubernetes device plugins and Slurm GRES hand out a GPU to one job at a time, and
    Kubernetes `Lease` objects use a TTL with renewal for a client that dies: the model for
    S10's 90 s TTL and heartbeat.
  - NVIDIA MPS and MIG partition a card between processes. Both are NVIDIA-only, and MIG
    only on supported datacentre parts, so neither can be the cross-platform base.
- **(c) What we take, or why not.** We take the idle-yields / busy-waits shape and the
  TTL-with-heartbeat for external clients. We do not take hardware partitioning
  (vendor-bound) or a proxy that owns the engines (DPC's engines are separate supervised
  children and in-process models).

## Decision

**Add one in-process lease registry, `managers/card_lease.py`, that makes every GPU
client in DPC a visible holder of the card. Lease holders and supervised llama.cpp holders
are read together through a single union, `_live_holders()`, and waiters of both kinds
are ordered by the one clock `_CARD_ARRIVAL`. Foreign GPU use is detected by a
platform-aware probe (S8), and an HTTP lease for external processes follows later
(S10).** This is option (b), in-process, plus option (c) from the card (see Considered
Options); the supervised-worker option (a) is not taken.

`_CARD_CLAIMS` is **unified with, not replaced by** the lease registry (section 2.3).

### Rationale

- The lease is pure Python and in-process on all three operating systems. It serialises
  DPC's own clients whether or not any GPU tool exists. A lease never asks nvidia-smi
  whether it may be granted; it asks the registry. (The one measured exception, rule L11,
  lets Whisper share with a busy music engine only when declared sizes and a VRAM reading
  both prove the room; when anything is unknown it queues.)
- A lease with `EXCLUSIVE_ON_THE_CARD = True` speaks the surface `_free_the_card` already
  uses (section 2.2.1 lists it member by member), so llama-server and Strata honour it with
  a handful of small edits.
- Music, Whisper, the embedder and GLiNER already differ in what yielding costs. The lease
  carries `yield_cost` and `on_yield` per client, so each unloads in its own way.
- It does the one thing option (a) cannot: it works for in-process models (Whisper, the
  embedder, GLiNER), which have no child process to supervise.

### 1. API

New module: `dpc-client/core/dpc_client_core/managers/card_lease.py`. The supervisor
module does not import it at load time (avoids an import cycle). Instead the lease module
installs hooks into it, the same pattern as `_still_configured` at 508:

```python
# llama_server_supervisor.py
_LEASE_HOLDERS: Callable[[], List[Tuple[Any, str]]] = lambda: []        # set by card_lease on import
_LEASE_WAITERS: Callable[[], List[Tuple[int, Any, bool]]] = lambda: []   # (arrival, lease, for_a_peer)

def _live_holders():                     # NEW; _live_claims stays supervisor-only
    return _live_claims() + list(_LEASE_HOLDERS())
```

The lease module reads `_CARD_QUEUE` and `_CARD_ARRIVAL` from the supervisor module lazily
(inside functions), so neither imports the other at load time.

#### 1.1 Objects

```python
YieldOutcome = Literal["let_go", "nothing_to_give", "refused", "could_not_let_go"]

class CardLease:
    """One client's standing on the card: registered once; granted, used, released many times."""
    IS_CARD_LEASE = True
    EXCLUSIVE_ON_THE_CARD = True        # read by LlamaServerSupervisor._excludes unchanged
    DROPS_PARKED_ON_STOP = False
    def __init__(self, holder: str, *, kind: str, engine: str, display: str,
                 vram_mib: Optional[int],            # None = unknown, never 0
                 yield_cost: Literal["cheap", "engine"],
                 on_yield: Callable[[str], Awaitable[YieldOutcome]],
                 waits_for_engines: bool = False,    # see the matrix in 2.1
                 shares_with: Callable[["CardLease"], Awaitable[Optional[str]]] = None,  # rule L11
                 eta_s: Callable[[], Optional[float]] = lambda: None,
                 stop_hint: str = ""): ...
    alias -> str                 # == holder; the key in card_holders/card_queue/stop_local_engine
    state -> "free" | "waiting" | "starting" | "running" | "draining"
    async def acquire(self, what: str, *, wait_s: Optional[float], background: bool = False,
                      check: Optional[Callable[[], Awaitable[None]]] = None,
                      on_wait: Optional[Callable[[dict], None]] = None) -> None
    @asynccontextmanager
    async def use(self, what: str, **acquire_kw)     # acquire if not granted, then one open use for the body
    def release(self, why: str) -> None              # sync; routed via _now_on_the_owner_loop
    def acquire_blocking(self, what, **kw) -> None   # for thread clients; RuntimeError on the owner loop
    def rebind(self, on_yield) -> None               # Whisper's providers-reload hand-over
    async def let_go_by_hand(self, why: str, bound: float) -> bool   # the "Stop X" button (2.2 item 8)
    who_uses() -> str
    # the holder surface the card queue reads: section 2.2.1
```

Holder names: `whisper:<alias>`, `music:yue2`, `music:acestep`, `embedder:BAAI/bge-m3`,
`ner:gliner`. Kinds: `transcription`, `music`, `embedding`, `ner`, and later `external`.

The lease's `state` uses the supervisor vocabulary of `_live_claims()` on purpose, so every
reader that switches on the state word keeps working: `starting` = granted while the client
loads (a Whisper load, a music engine boot), `running` = granted and loaded, `draining` =
marked for a contender while `give_up_for` runs. `stopping` is not used by a lease.

#### 1.2 Rules

**L1. Grant.**

- `acquire` runs on the owner loop: it is wrapped `on_the_owner_loop` at line 319, so
  callers on a foreign loop work.
- The "nothing excludes me and nobody earlier waits" check (L5a) and the grant happen with
  **no await between them**, the `_claim_card` discipline. Two concurrent acquires cannot
  both pass. A check that has to await a reading (L11) checks again after the reading,
  in the stretch of the grant, as `strata_provider.py:457` does.
- A lease that is already granted is re-entrant: `use()` opens one more use.
- **Busy is one source, the lease's own:** an open use, or `state == "starting"`, or
  `state == "draining"`. Each client opens its uses only through `lease.use()`; for music,
  `SupervisedEngine.in_use()` enters `card_lease.use()` and counts `_busy` in the same
  place, so `engine.busy` (read by `_idle_loop` and `MusicEngines.exclusive`) and the
  lease's busy cannot disagree (Ark #45, finding 3).

**L2. Waiting holds nothing.**

- A waiting lease is in `_LEASE_LINE`, a FIFO in the lease module. It holds no grant.
- A granted lease never waits for the card again while it is granted. (A music engine
  whose child exited releases its grant, rule L8, and its next start acquires again.)
- So the wait-for graph is a forest and has no cycles (see Risks).

**L3. Refusals.** All subclass the existing `CardQueueRefusal(holder_alias,
holder_engine)`, so the gateway, the peer door and `card_queue_refusal_in` already read
them as "never retried".

- `LeaseRefusal`: the base; it carries a `.wait` estimate.
- `LeaseWaitedTooLong`: the deadline passed. It names the holder, why it is busy
  (`who_uses()`, or `_who_stands_on` for a supervisor) and the bound.
- `LeaseWouldWaitOnItself`: the request in `_CARD_HOLD` stands on the very holder it would
  wait for, a lease or a supervisor. Refused at once, as rule 9 does.
- `LeaseSkipped`: `background=True` and the card is not free right now. The client falls
  back to CPU.
- `LeaseRefusedForPeer`: worded for a peer, and says **"right now"** so it does not read as
  "never" (Ark #45, D3): "this node's GPU is busy with another job right now, for about N
  min" (the estimate when known). It names no alias, agent or chat (rule 7). A peer's
  transcription is refused when its whole trip cannot be shown to fit in 120 s (L13).
- `LeaseBusy`: `let_go_by_hand` on a busy lease. It names `who_uses()`.
- `LeaseClosed`: the backend is stopping.

**L4. Deadlines.** One deadline per acquire, fixed at acceptance (rule 5). A person's
doors queue the way model starts do (decision 6, Mike #49–#50), with a bound of their own:
the **dictation bound**, a new `[local_transcription] queue_wait_s`, 600 s (Zcode
#52; the alternative is to inherit `swap_wait_s`, 1800 s on this machine). The shape is
decided (Mike #62, #70, #71); the bound, 600 s, was decided on 2026-11-23 (item 6A, Mike #15).

**The queue's deadline is never longer than its caller's** (Ark #53): `wait_s = min(caller's
remaining time − margin, bound)`, where the margin is the transcription's own expected time,
`max(10 s, estimate)`, the estimate of L10 (audio duration divided by the real-time ratio).
A **peer** has one formula, not this one: L13, point 2. A caller that has no
deadline today gets one: `service.transcribe_audio` takes an explicit bound (the dictation
bound plus the margin), so the local path and both Telegram paths are no longer unbounded.

| Door | Bound | Notes |
|---|---|---|
| owner's dictation (UI `transcribe_audio` → `service.transcribe_audio`) | L12 first (GPU now, CPU, or the line); in the line: dictation bound, capped by the UI command timeout | the UI's 240 s (`coreService.ts:1492`) is raised to the bound plus the margin in S3, or it caps the queue at ~3 min; cancellable |
| received voice messages (`service.py:5152`) | L12 first; in the line: dictation bound | the cascade stops on a card refusal and the peer check runs again after the wait (section 3.1) |
| Telegram voice (`telegram_coordinator.py:509`, `agent_telegram_bridge.py:1674`) | L12 first; in the line: dictation bound | today unbounded; the explicit bound above applies |
| agent tool `transcribe_audio_file` | L12 first; in the line: the turn hold's deadline, never more than `queue_wait_s` (600 s; Mike 2026-11-23), and the tool timeout less the margin (`waiting_for_the_card_at_most`) | the tool timeout is 1800 s (`tools/transcribe.py:293`); without a turn hold the cap is `queue_wait_s`, so the wait is 600 s, not ~1790 s |
| music Transcribe (the music player's lyrics chain, a person's click) | L12 first; in the line: `wait_s=None`, until `check()` raises (cancel) | a track's vocals are usually minutes long, so L12 mostly sends it to the line |
| music jobs | `wait_s=None`: until `check()` raises (cancel) | |
| peer (`p2p_coordinator.py:1398`) | **Mike #70, #11:** granted at once → served; otherwise it waits in `_LEASE_LINE`, behind every owner's waiter, only when the estimated whole trip fits the guest's 120 s, else refused at once with the estimate (L13, point 2); no CPU route unless the owner's optional setting allows it (Mike #71) | the peer bound `_a_peers_slot_wait_s()` (`llama_server_supervisor.py:1505`: the smaller of `swap_wait_s`, 300 s by default, and `remote_inference_timeout`, 1200 s; 1200 s on this machine) is always over 120 s, so it plays no part for a transcription; ADR-044's amendment of 2026-11-18, which L13 extends |
| background transcription | L12: on the CPU twin when its estimate passes, otherwise skipped (`LeaseSkipped`) | rule 10 |
| background, other (Whisper preload, embedder steady state) | never waits: `LeaseSkipped` | rule 10 |

**The ETA routes and informs; it never refuses on its own** (Ark #53, #60, #61). It feeds
L12's choice between the CPU and the line, where an unknown ETA counts as long; in the line
a person's door shows "≈N min" when the holder's ETA is known and "time unknown" when it is
not, and waits up to its deadline either way, cancellable. Reason: `estimate_yue2_seconds`
(`music/job_schemas.py:271-279`) counts sung lines only, returns the intro alone for an
instrumental, and says of itself "never a refusal"; an instrumental generation that runs for
minutes reads as seconds. A low ETA can therefore send a short file to the line instead of
the CPU; the cost is a wait bounded by the deadline, not a refusal. (Mike's #62 choice
replaces the former sub-choice B, wait or refuse on an unknown ETA.)

The first draft's `[local_transcription] card_wait_s` = 120 is dropped; `queue_wait_s`
replaces it with a different default and meaning (the bound of a wait in line, not of a
refusal).

**L5. No new use jumps a waiter (rule 4 analog).**

- While a contender waits for a granted lease (a lease in `_LEASE_LINE`, or a supervisor's
  `_CARD_QUEUE` place whose blocking holder is this lease), **new `use()` calls are not
  admitted**. They wait in `_LEASE_LINE` behind the contender, with their own arrival.
- From `_begin_swap_drain` on (L6), no new use is admitted at all until the yield has
  ended.
- So a dictation stream cannot starve a music job or a llama-server start.

**L5a. One order across the two lines** (Zcode #41, gap 2; mandatory with decision 6,
Zcode #52). There are two lines, `_LEASE_LINE` and `_CARD_QUEUE`; they share one clock,
and **no waiter overtakes another in either direction**: a queued dictation is not starved
by a series of server starts, and a server start is not starved by a stream of dictations.
Two named exceptions: a peer never goes ahead of an owner's waiter (rule 7), and a call the
CPU twin takes is in no line at all.

1. **One clock.** Every lease waiter draws `arrival = next(_CARD_ARRIVAL)` when it enters
   `_LEASE_LINE`, and every lease use draws one when it opens. A supervisor place has
   `_CardPlace.arrival` (1033) from the same counter.
2. **Order on release.** When a holder lets go, the waiter with the **lowest arrival**
   among those it was blocking goes first, whichever line it is in. A lease waiter is
   granted inside `release()`, in the same synchronous stretch; a supervisor place finds
   `found is None` on its next `_free_the_card` poll and claims. A later waiter, of either
   line, that excludes an earlier one does not take the card while the earlier one waits:
   - `acquire` asks `_CARD_QUEUE` (through the lazy read above) for a place with a lower
     arrival that this lease excludes, and waits behind it;
   - `_wait_to_be_first` and `_free_the_card` ask `_LEASE_WAITERS()` for a lease waiter
     with a lower arrival that excludes the place, and wait behind it as behind a place
     (bounded by the request's deadline; the refusal names it, as
     `_behind_a_place_refusal` does).
3. **Who acts.** Only the earliest waiter that a holder blocks may mark it (ask it to let
   go, or swap it). The decision-32 exception of earlier revisions is **gone** (Mike #70):
   a music lease now acts on an idle supervisor like an owner's start (section 3.2), so it
   no longer has to stand aside for a later server start.
4. **The bound.** A waiter waits only for holders granted before it arrived and for
   waiters with a lower arrival; both sets are closed at its arrival (L5 for leases, rule 4
   `_stands_before_the_line` for supervisor holds). So a series of server starts that
   arrive after a waiting music lease cannot take the card before it: when the busy holder
   lets go, the music lease is granted first, and the starts then wait for the busy music
   job (Mike's #20 ask) up to their own deadlines.
5. **In the first slice now (S2).** Leases wait for supervisors (Whisper since Mike #68,
   music since #70), so `_stands_before_the_line` (1202) compares a hold with the lowest
   arrival across both lines, not only `_CARD_QUEUE[0].arrival`; otherwise a stream of
   agent turns accepted after a waiting dictation would keep Strata busy for it.

This also closes the window Ark #44 named: between a lease's `give_up_for` and the
supervisor's next round, another lease cannot be granted unless it arrived earlier, and if
it did, the next round finds it as a new holder (up to `_MAX_SWAP_ROUNDS`, 509), never a
refusal from `_claim_card`.

**L6. Yield ("please yield" to idle holders, wait for busy ones).**

- Only the earliest waiter acts (L5a point 3).
- In one synchronous stretch it checks `not holder._busy_for_the_card(...)`, then marks
  `holder._begin_swap_drain(contender)`, which sets `_draining`, `_swap_for` and `state =
  "draining"` and closes the lease to new uses.
- Then it runs `give_up_for()` as its own task, held in a module set like `_SWAP_STOPS`
  (638) so a contender's deadline or cancel never cuts a yield in half. That task calls
  `on_yield(reason)`, which returns one of four outcomes (Ark #45, finding 2; Ark #60 §3
  and #61 §2a for the fourth):

  | Outcome | Meaning | The lease then |
  |---|---|---|
  | `let_go` | the client freed its memory | `release("yield for <contender>")` |
  | `nothing_to_give` | granted but nothing loaded (a warm grant whose model is gone, an engine whose child already exited) | `release(...)` at once |
  | `refused` | a use slipped in through a path the lease does not see (for example a hand-over carrying running transcriptions) | `_end_swap_drain()`: back to `running`, the contender keeps waiting until its deadline |
  | `could_not_let_go` | the memory is still held and the unload never ran (Whisper: the unload's worker was cancelled before its thread started, or the executor refused it; the model is loaded and owned) | **no release**; `_end_swap_drain()`, one WARNING naming the reason; the contender keeps waiting, and its next round may ask again (bounded by `_MAX_SWAP_ROUNDS` and its deadline) |

  | Holder | `on_yield` does |
  |---|---|
  | Whisper | `yield_card(why)` (new, section 3.1): under `_load_lock`; not loaded → `nothing_to_give`; a transcription running → `refused`; else unload → `let_go` if `model_loaded` is now False, `could_not_let_go` if it is still True |
  | Music engine | `_yield_card(why)` (section 3.2): child not alive → `nothing_to_give`; `_the_lock` held or busy → `refused`; else `stop("swap")` → `let_go` |
  | Embedder / GLiNER | set a flag the thread reads between batches; it moves to CPU or drops the model, then `release()` → `let_go` |
  | Llama-server / Strata (supervisor holders) | unchanged: `stop_by_hand(holder, why="swap")` |

- `give_up_for` never raises for `refused` or `could_not_let_go`; it ends the mark and
  returns its outcome. A supervisor contender then finds the holder busy or still there on
  its next round and waits. **"Nothing to give" means no model; "could not let go" means a
  model that is there and was not unloaded** (Ark #61 §2a): only the first releases.
- **Busy holders are never marked and never asked.** They are waited for and named.

**L7. Visibility.**

- `card_holders()` adds lease rows **after** the supervisor rows. Each keeps the existing
  keys and adds new ones: `{"alias": holder, "engine": engine, "state":
  "starting"|"running"|"draining", "port": None, "kind", "display", "busy": bool, "uses":
  [{"what", "since"}], "vram_mib": int|None, "since", "yield_cost", "eta_s": float|None,
  "beside": alias|None}`. `beside` names the holder it shares the card with under L11.
- Supervisor rows get `"kind": "inference"`.
- `card_holder()` (`llamacpp_server_provider.py:1991`) keeps returning the first
  **supervisor** row until S2 item 8 makes **Stop engine** work for a lease alias; until
  then the panel's Stop button can never be pointed at a lease.
- `card_queue()` appends lease waiters: `{"alias": holder, "kind", "what", "queued_at",
  "give_up_at"|None, "until_cancelled": bool, "wait": {...}}`. `until_cancelled` says what
  a `null` `give_up_at` means for a lease, because `cardQueueLines` reads a `null` as "coming
  back from a description" (section 4).
- The music `set_waiting_for_card` keeps only `alias` and `engine` (`jobs.py:607`), so it
  still works.

**L8. Release.**

- `release(why)` is synchronous and safe to call on any path: unload, stop, child exit,
  shutdown, failed load, a cancelled task.
- Releasing a lease that is not granted does nothing.
- It grants the next lease waiter by L5a point 2, in the same stretch. It logs one INFO
  line: `card: <display> let go (<why>), held <s> s`.

**L9. Thread clients.**

- `acquire_blocking` uses `asyncio.run_coroutine_threadsafe(..., _OWNER_LOOP)`.
- Called on the owner loop's own thread, it raises at once instead of deadlocking.

**L10. Wait estimate.** Shape `{"wait_s": float|None, "known": bool, "behind": [display
strings], "where": "busy_holder"|"yielding"|"lease_line"|"card_queue"|"foreign"}`.

- If any part of the estimate is unknown, `wait_s` is None and `known` is False. It is
  never 0.
- **An engine's yield has a bound, not a figure (701cc077).** A music engine's stop has no measured
  time; its lease declares `yield_bound_s` = 2 x `STOP_GRACE_S` (30 s: the signal wait and the wait
  after the kill, `_stop_proc`). The estimate counts that bound for the holder and adds
  `"upper_bound": True` to its shape when any part of `wait_s` is such a bound; the real wait is at
  most that. Every line that shows the estimate then says "at most about N s": a transcription's
  (`card_wait_words`) and a start by hand's (`card_wait_words_for_a_start`). **The start by hand's wait
  row:** `start_music_engine` asks with `yields_idle=True` and an `on_wait`, so the engine's status
  carries `card_wait {text, behind, wait_s, known, upper_bound}` while it waits ("yue2.cpp is waiting
  for the card: Whisper (turbo) is letting go, at most about 30 s"), cleared at the grant or the
  refusal; the Create panel's engine line shows it. A supervisor that is going counts its own
  `_expected_stop_s()` and is not a bound.
- `eta_s` comes from the holder:
  - **Orientations, stated once.** The Whisper **ratio** is audio seconds per wall second
    (`_realtime_ratio`, `whisper_provider.py:816-822`, `duration / elapsed`; the speech table
    prints 15.9–43×), so `eta = duration / ratio` (ratio 20×, 30 s of audio: 1.5 s; a product
    would say 600 s). The YuE2 **factor** is **audio seconds per wall second**, as the trial card's "about 3.5x real
    time" reads (faster than real time). Mike flipped the orientation on 2026-11-24 (group chat), built in
    follow-up 9b; the record had first written it as wall seconds per audio second, which made a 240 s song
    read about 12 minutes where the trial measured about 80 s. `estimate_yue2_seconds` returns **audio**
    seconds: the form compares it with the duration cap (`shortCapWarning` in
    `ui/src/lib/utils/musicCreate.ts`), so wall time = estimate ÷ factor (`job_schemas.yue2_wall_s`,
    `job_schemas.py:303`). The job's own timeout, `max(DEFAULT_ENGINE_JOB_TIMEOUT_S, duration_s * 6 + 300)` with the default at 1800 s (`runner.py:992`, `:147`; measured 2026-11-24 at 670141bb: the 1800 s floor rules every cap below 250 s), is a
    deliberately generous safety net and does not read the factor. S4 records the factor by song length: `trial.md` (`ai-studio/experiments/music-player/engine/yue2-cpp/trial.md:40-41`) saw about 3.5× for a 30 s song and about 3× for 4-minute ones, and its 6-line text came back as 71–144 s songs, so the 10 s per line constant understates a song's length (a concern for the UI's warning, not for the lease). **Built in S4** as `job_schemas.YUE2_FACTOR_BY_LENGTH` ((30 s, 3.5), (240 s, 3.0), straight between, the end value beyond), the table unchanged by the flip; **the orientation was open for Mike and is decided** (2026-11-24, group chat: yes): the trial table reads this way (a 240 s song in 80.5 s of wall time, a 30 s one in 8.9 s, the model's load included), so the factor is audio seconds per wall second.
  - Whisper: the load when the model is not loaded (`load_first_s` before this model has loaded on
    that device in this process, `load_reload_s` after; S3 follow-up 4, Forge #42-4; per device
    since S3 follow-up 6, Forge #48-1, Ark #49-1; per device and model since S3 follow-up 7, Ark
    #61-1), plus the audio duration
    divided by the last measured warm ratio of its length band, plus that band's `penalty_s`, the
    seconds the first call after a load costs on top (added, never a ratio: S3 follow-up 2, Johnny
    #24-1/2; by band since S3 follow-up 4, Johnny #41-2), else None.
    **The ratio had no carrier before S3** (Johnny B, Forge #21-5, Zcode #22-3): `_realtime_ratio()`
    (`whisper_provider.py:817-822`) only formats a log string (`:986`, `:1060`). S3 records
    (the provider side; S1 carries no ratio) the ratio the provider already computes there, **per device and length band** (the batch
    descent's bands, up to 60, 120, 180 s and over 180 s), with `penalty_s` by the same bands and the two load figures per device; the estimate uses the device
    the route goes to (Forge #20-2), and with no figure for it the estimate is unknown (a peer
    is then refused, L13). L12's table is the CPU's; the GPU's follow it. **The duration** is
    ffprobe's (`service.py:515-523`), else the recorder's declared one (`:533-537`; for Whisper's
    estimate `card_ask`'s `declared_s`: the UI's `duration_seconds` at the dictation door, the
    message's `voice_metadata` in the received-voice cascade; S3 follow-up 2, Johnny #24-3);
    the old `_get_audio_duration` returned **0.0** on failure, not None, and 0.0 counts as
    unknown (Johnny E, Forge #21-6); it had no caller and was removed in S3 follow-up 4 (Ark
    #40-3), and `audio_seconds`, the one probe, reads 0 as unknown.
  - Music, **a job that has not started:** the text estimate (`estimate_yue2_seconds`
    per variant, `music/job_schemas.py:271-279`, audio seconds) divided by the YuE2 factor for that
    length (audio seconds per wall second, `YUE2_FACTOR_BY_LENGTH`: 3.5 at 30 s to 3.0 at 240 s) and
    times the variants, marked as an estimate; it may route (L12), never refuse.
  - Music, **a running job** (Ark #64 §5): the remaining time must come from the job's own
    progress, not from the text estimate, which counts sung lines only and gives an
    instrumental its intro alone. **Today there is no usable progress signal:** the YuE2
    runner sets progress to 0.05, 0.1, and then one step per finished variant
    (`runner.py:846-900`), nothing inside a variant, which is where the minutes go; the
    job records no start time (`created_at`, `updated_at` only, `jobs.py:420-425`); and the
    "about 3.5x real time" figure is a comment from the trial card (`runner.py:94, 856`),
    not a measurement this machine keeps. So in v1 **a running music job's ETA is unknown,
    and L12 treats it as long.** S4 may add the signal: the start time of each variant and
    a per-machine YuE2 factor recorded from finished variants (the take's audio length
    over wall time); then `eta = variants left × duration_s ÷ factor − elapsed in the current
    variant`, floored at 0 and marked as an estimate. Acestep's progress is as coarse
    (`runner.py:734-784`).
  - Supervisors: None: a supervisor has no ETA. **One that is going** (stopping, draining, or an
    idle one the waiter itself swaps out) counts its stop, `_expected_stop_s()`: the last stop it
    measured, else its grace default (S3a, Ark #10-4). A busy one, a starting one and an idle one
    nobody stops for this waiter stay unknown.

**L11. One shared cell: Whisper beside a busy music engine when it provably fits**
(decision 6, option (d); accepted with conditions by Zcode #52 and Ark #53; **decided by Mike
2026-11-24 (Q6), built: S3e, live since S4**).

- **The primary axis stays engine identity.** Who excludes whom is a property of the
  holder, as today: `_excludes` and `EXCLUSIVE_ON_THE_CARD` (llama-server `False` at 2301,
  Strata and every lease `True`). Measured headroom is a **second, narrower axis** that can
  only turn "excludes" into "shares", never the reverse, and only in the one cell listed
  here. It is evaluated by the lease module at grant time and recorded on the grant
  (`beside`); it does not change `_excludes` and does not make exclusion a property of the
  pair (Ark #53).
- **One cell, not a rule** (Zcode #52): contender = a Whisper lease, holder = a **busy**
  music lease. No other pair shares by measurement in v1; each further shared cell needs
  its own arithmetic and tests. Whisper never shares with llama-server or Strata (Mike
  #68). **That is an exclusion checked before any arithmetic** (Forge #33): with a local
  engine resident, `_excludes` puts Whisper outside the cell and the arithmetic is not
  consulted; L12 decides. The only cell where arithmetic decides is Whisper beside music
  with no engine resident (test 28); its supervisor terms below guard an engine that
  appears between the reading and the grant, and are not the production path.
- **When.** Additionally, no earlier waiter in either line is blocked by that music lease
  (a co-grant must not add to an earlier waiter's wait).
- **The arithmetic: declared peaks against total, a live reading may only veto** (Ark's
  and Zcode's versions reconciled). Free memory at load time alone is not enough, because
  a music job grows from stage to stage (Ark #53); declared sizes alone are blind to
  anything the registry does not know (Zcode #52). So:
  0. **Two different sums** (Ark #60 §5, #61 §2b). *Who is in the way* decides who is
     asked to yield: `_blocking_holders()` (2500), filtered by `_excludes`. *Who is
     resident* decides the fit: **every** holder in `_live_holders()`, unfiltered,
     supervisor children and a co-living Whisper alias included. Building the fit from the
     "in the way" list would leave out exactly the residents that do not exclude the
     contender, and report a fit where the card would run out.
  1. `need = whisper_peak + Σ peak of every resident + _DESKTOP_RESERVE_MIB`, **every term in
     MiB** (since S3e the reserve is the resident engine's own, "One reserve per engine" below, and
     2048 with no engine on the card; `whisper_peak` is the call's length band's, next bullet but two), where `whisper_peak = model_size_bytes(model, revision)[0] // 2**20` plus a new
     `_WHISPER_OVERHEAD_MIB`, proposed **2900**, and:
     - **One scale, one view** (Ark #18-3, Forge #21-3, Zcode #22-1). `model_size_bytes`
       (`providers/model_sizes.py:74-79`) returns **bytes** (turbo 1 622 443 339 B, row at
       `:48-49`, so 1547 MiB); `_total_vram_mib()` and `_DESKTOP_RESERVE_MIB` are MiB. MiB is
       the one scale: bytes are converted once, where `whisper_peak` is built, and the logged
       `need` is that figure. A peak is a **torch-view** figure plus the context, not the
       driver's `nvidia-smi used` (one load: 1826 MiB torch peak, +752…+967 MiB driver view,
       Forge #21); the torch view is the conservative one, and the declared check uses it.
     - **The overhead, measured and where** (first Forge #21, 2026-11-21, then CC on speech,
       2026-11-22; a fresh process in the backend's venv, **not inside the backend**): CUDA
       context ≈477–485 MiB (280 on an empty card); turbo fp16 weights 1544 MiB allocated.
       Forge's synthetic tone gave a 1826 MiB peak (chunk 30, batch 16), activations ≈282 MiB,
       and so a first 770; **real speech shows the peak depends on the provider's adaptive
       batch**: at batch 8 (clips of 60–120 s) activations reach ≈2.1 GiB (3650 MiB allocated,
       3948 reserved; see "GPU figures"). The declared overhead is the worst case, torch
       reserved peak 3948 + context 485 ≈ 4433, less the 1547 of weights, rounded up:
       **2900**. `whisper_peak` ≈ 1547 + 2900 = 4447 MiB (the driver view peaked at 4240), against 3362 with the former 1740. (4447 is the worst band's figure: since S3e it applies to 61 to 180 s and the
       other bands are 3147, next bullet.) That
       ~1.5–1.7 GiB was the **backend's own residency** (`bge-m3`, GLiNER; `040-…md:78`), not
       a Whisper context, and on top of the weights it counted the model twice (Ark #18-4,
       Johnny A, Forge #20-6, Zcode #22-2). A start value, tuned from operation.
     - **The flat figure was a conscious choice, replaced by the band in S3e** (Johnny #32-2, Forge
       #33, Ark #30; then Johnny #30-2, Forge #31-6, #42-6, #60): `whisper_peak` was the worst case of
       the provider's adaptive batch (batch 8 at 61–120 s) and overstated a 10 s dictation about 2.2×
       (1984 reserved against 4447); its context is 485 MiB, the larger figure (Strata resident; 280 on
       an empty card). **Since S3e** the overhead follows the call's length band, on the batch
       descent's thresholds (`_WHISPER_OVERHEAD_BY_BAND_MIB`): up to 60 s 1600 (ru_60 at batch 16, 2618
       reserved, the band's largest), up to 120 s 2900 (en_120 at batch 8, 3948), up to 180 s 2900
       **estimated** (batch 4 was never measured; in `gpu_run1.log` the peak follows the chunks decoded
       at once, 1 chunk ≈1984–2088, 2 chunks 2618 and 2660, 4 chunks 3840–3948, and batch 4 decodes
       four), over 180 s 1600 (en_300 at batch 2, 2660). For turbo that is 3147 / 4447 / 4447 / 3147
       MiB, **derived, not measured beside strata.exe** (Forge's live run measures each band). An
       unknown length takes the worst band, as does the lease's own `vram_mib` (its `card_holders`
       row); test 28 pins 6495 (the 61 to 120 s band's 4447, at a 90 s length, plus the 2048 reserve; a fixed length, not the
       unknown-length case).
     - **S3 calls `model_size_bytes` at a new call site, without a revision**: the provider calls
       it only for an uncached model (`whisper_provider.py:264-266`), and the row fallback works
       only for `revision is None` (`model_sizes.py:86-91`). Test 28's fake row is revision-less.
     - **The source is logged** (Ark #18-8): `measured` or `hf_api`; `hf_api` overstates a model
       that ships more than one format (`model_sizes.py:78-80`). `torch.compile` fails on torch
       2.11 (guard at `whisper_provider.py:312-320`), so the figures carry no compile cost.
     - a music holder's peak is its declared `vram_mib` (YuE2 Q8 5.2–5.6 GB is already a
       peak; the larger figure is used);
     - another loaded Whisper model's peak is computed the same way as `whisper_peak`, **once
       per loaded model, not per lease or use** (Johnny C, Forge #20-4): two uses on one alias
       are one model object (`_current_owner`, `whisper_provider.py:673-697`);
     - **a llama-server child's peak is the figure its own start already admitted it
       with**: `_predicted_total_mib(admission, cache_type)` (3554; weights + KV for its
       `n_ctx` and KV type + overhead, from `_admission`, 3539), recorded on the
       supervisor at launch by S3 as `predicted_vram_mib` (**built in S3e**: set when the child
       answers `/props`, from the `_admission()` its start computed and the KV type it was given;
       None before a start and for a start that could not size the model; **a Strata child declares its
       own peak instead, since c5c1272c**, see the next bullet). Chosen over a live measurement
       (per-process figures read `[N/A]` under WDDM through nvidia-smi and need S8's PDH
       reader) and over "always unknown" (which would send every dictation beside a loaded
       LLM to L12 on the owner's 32 GB card for no reason): it is the arithmetic DPC
       already trusts to admit that very child, and it includes the measured overhead
       (`_DEFAULT_OVERHEAD_MIB`, 4608);
     - **a Strata child declares its peak as the card's total less its own `--vram-reserve-mib`**
       (`strata_supervisor.declared_peak_of`, c5c1272c): the engine takes up to that and leaves the
       reserve untouched (1152 on the live alias), so the figure is the engine's own bar, not a
       measurement. It stays None, and L11 answers `unknown`, when the total could not be read or the
       start config carries no reserve. Strata never calls `_admission` (it reads `gguf_path`, Strata's
       model keys are `native_gguf`, `ple_gguf`, `pack_dir`, `mtp_dir`), which is why it needed a
       figure of its own; the earlier **[unverified]** question is closed by reading
       `declared_peak_of`. **Beside a resident Strata, L11 answers `does_not_fit` at the declared check**
       (a Whisper peak of 3147 or 4447 against the 32 GB card, less what Strata declares, does not fit).
       **F2 (Forge's review, confirmed by agent D of follow-up 11):** `_shared_holders` returns `[]` when
       any blocker is an engine, so `_fit_now` is never reached while an engine holds the card; a `fits`
       is reachable only with no engine on the card. A llama-server child whose start could not size its
       model (`_admission` None) still has no figure: `unknown`. A later refactor may give supervisors
       a common `vram_mib`;
     - the desktop is counted **once, as `_DESKTOP_RESERVE_MIB`**. A measured `dwm.exe`
       figure is never added: `dwm` is not a resident (it is on S8's ignore list), so the
       declared check cannot pay for the desktop twice (Ark #60 §5.2).
  2. **Declared check:** `need <= _total_vram_mib()` (supervisor 1814). A `need` above a known total is `does_not_fit`
     whether or not `free` could be read (Zcode #88-6, follow-up 9a); `unknown` is for a missing size or a missing total.
  3. **Live veto:** `_free_vram_mib()` (1793) `>= whisper_peak + growth_left`, where
     `growth_left` is the music peak less what the music child is measured to hold now
     (per-process reader, S8), or the whole peak while it cannot be measured. **In v1 it is
     always the whole peak**: the registry keeps no "holds now" figure (see "The quantities"
     below). A reading can only turn "fits" into "does not fit", never the reverse.
  4. Both readings run in a thread (`asyncio.to_thread`: each is a subprocess with a 10 s
     timeout); the L1 check runs again in the stretch of the grant.
- **The quantities, named** (Johnny #83: "name which quantity L11 takes from the agent map's
  source"; Mike #70: the source is the agent map's). L11 uses three kinds of number and no
  other. Points 2 and 3 above read the first two **through `read_vram_mib()`, not by
  calling the two helpers**, so a dictation does not spawn nvidia-smi.
  - **`total`**: the physical VRAM of the first NVIDIA GPU in **MiB**, from `nvidia-smi
    --query-gpu=memory.total --format=csv,noheader,nounits`, first output line only
    (`_total_vram_mib`, `llama_server_supervisor.py:1812-1827`). The agent map's
    `vram_total_mib` is the same call (`service.py:12132-12144`, through
    `commit_memory.read_vram_mib`, 135-156). Read once and kept for the backend's life.
    Used by the declared check (point 2).
  - **`free`**: `memory.free` of the same GPU in **MiB**, same command shape and first line
    (`_free_vram_mib`, 1793-1809), through the same wrapper, so at most
    `_VRAM_READING_TTL_S` = 30 s old (`commit_memory.py:130`). It is the **device-level**
    figure the driver reports: all processes' use together, the desktop's and a foreign
    process's included, so it needs no desktop term and no per-process sum. Used only by
    the live veto (point 3), which can only narrow.
  - **Declared peaks** (`vram_mib` of a lease, `model_size_bytes` converted to MiB plus the
    overhead constant, `_predicted_total_mib` of a llama-server child): MiB, **not a
    reading**.
  - **Not used: the per-process figures** (`peak_vram_mib`, PDH on Windows, NVML on Linux).
    Why, from the code: the registry keeps one running **peak** per engine entry, summed
    over the process tree, sampled every `SAMPLE_INTERVAL_S` = 2 s
    (`process_registry.py:268, 2349-2359, 2425-2426`); there is no "holds now" figure to
    subtract, only engines have one (`ENGINES`, 281, so an in-process Whisper has none),
    and the PDH counter is "a commitment, which can exceed what the card holds ... never
    summed into a card total" (`process_registry.py:36-38, 2015-2017`). So in v1
    `growth_left` is the music peak **whole** (point 3's fallback becomes the rule): a
    PDH over-reading would shrink `growth_left` and soften the veto, and a spike between
    two samples would do the same, so neither is allowed in. When someone measures PDH
    against `total − free` on this machine, the rule can change; until then it is the
    conservative direction.
  - **Where PDH is still read**: S8's foreign-process test, per pid, against its 2048 MiB
    threshold, never summed into a total. There its over-reading makes a foreign process
    look larger, so the card closes earlier for DPC's own start: the safe direction, and
    named here as accepted.
  - **The cache, exactly** (`commit_memory.py:126-156`; Johnny #14, Ark #13, Forge #15). A
    helper that **returns** None (no nvidia-smi, or `OSError`/`ValueError`/`SubprocessError`,
    a 10 s timeout among them, `llama_server_supervisor.py:1793-1810`) is stored with `at`
    like a reading (155), so it is served for 30 s; a helper that **raises** (the registry
    closed during the backend's stop) returns `(None, None)` and is not stored (152-154, as
    the docstring says at 139-140). `total` is read once and kept for the backend's life
    (151). No function clears the dict. **Decided (Mike #11, item 13):** the cache stays,
    and S3 adds a function that clears `at` and `free` (never `total`, or every clear costs
    a second nvidia-smi) on every grant and release and after a None reading, so a failure
    is not served for 30 s. The 30 s pin
    (`test_the_agent_map_reads_the_cards_vram_at_most_every_30_seconds.py`) stays green.
    **Accepted** (Ark #18-5): the reader is shared, so the agent map's 5 s poll re-probes after
    each lease event too; Forge measured one probe at about 30 ms. The reset never passes a
    future `now` (`commit_memory.py:147, 155`: an `at` in the future switches the cache off for
    the rest of the process). **Built in S3e** as `commit_memory.forget_vram_reading()` (clears
    `at`, `wall` and `free`, never `total`), called by the lease on every grant, on `mark_running`
    (a load that finished under a grant changes the card after the grant) and on every release, and
    by L11 after a reading that came back None. Supervisor starts and stops do not call it (the
    record names the lease only); the agent map's 30 s pin stays green.
  - **One reserve per engine, from the engine's own config** (Forge #15 §4; amended by Mike's
    decision of 2026-11-23, #7 and #15: "one source of truth, the one that is set"). L11's
    declared check is the engine's admission arithmetic, and only its live veto reads `free`,
    which already holds the desktop (section 5). S2 adds no second reserve. What the reserve
    is, per engine:
    - **Strata:** the single source is the alias's `vram_reserve_mib` (live value 1152 in
      `providers.json`), the value the engine itself honours as `--vram-reserve-mib`. The peer
      start gate (`strata_provider._peer_start_refusal`, before S3e `total -
      _base._DESKTOP_RESERVE_MIB`) and the L11 fit read it instead of the 2048 constant. When an
      alias has no `vram_reserve_mib`, 2048 is the fallback, and the refusal line names which
      number it used. **As built (S3e, `strata_supervisor.vram_reserve_of`,
      `StrataSupervisor.vram_reserve`)**, the layers are the ones the start config applies, in
      its order: the alias's typed `vram_reserve_mib` (0 is a value, Strata's "leave nothing"),
      then the alias's older `model_params.vram_reserve_mib`, then the **base config's**
      `--vram-reserve-mib` (the UI shows it as the base value, 1536 in the test install), and
      only then 2048. Read as: "the value that is configured" is what the engine will actually
      be started with, and the base config is configured too; skipping it would bar a start at
      total − 2048 while the engine leaves 1536. A `model_params` value of None drops the base's
      flag (the engine's own default applies, which DPC does not know), so it reads as the
      fallback. `STRATA_DEFAULTS["vram_reserve_mib"]` is None ("keeps the base config's value"),
      so it adds no layer of its own.
    - **llama-server:** it has no engine reserve flag, so `_DESKTOP_RESERVE_MIB = 2048`
      (`llama_server_supervisor.py:1943`, used by `_admission`) stays the only number;
      `LlamaServerSupervisor.vram_reserve()` answers it, so L11 reads every engine's reserve
      through one method.
    - **Why this was needed (written before S3e; the state it describes is gone).** The two numbers did
      not add up anywhere in code: 2048 was a pre-start check only, and 1152 is what the engine leaves
      untouched. The gate therefore assumed Strata takes up to total − 2048 = 30575 MiB (derived, not measured; to be re-measured on v0.2.20 before it becomes a test constant (Forge #12, Zcode #22-5)), while the engine takes up to
      total − 1152 = 31471 MiB, 896 MiB more. Since S3e the gate reads the alias's own reserve, so for an alias that
      leaves 1152 the bar is total − 1152 = 31471 MiB.
    - **The Strata peak 30575** in L11's arithmetic and test 28 was derived from the gate line
      (total − 2048), not measured. It is to be re-measured on Strata v0.2.20 before it becomes
      a test constant (Forge #12, 2026-11-23; Zcode #22-5). Read it as the figure of the 2048 fallback only: with an
      alias's own reserve the gate's bar is total less that reserve (31471 MiB with 1152).
  - **By platform**:

    | Platform | `total` / `free` | Result in L11 |
    |---|---|---|
    | Windows, NVIDIA | read at device level by nvidia-smi; only the per-process query is `[N/A]` under WDDM (`eval/gaia/campaign.py:128-130`). Two pairs, both real. A local engine resident: `free` ≈1100 MiB (Forge #15, #33: 1100 and 1108 with Strata; readings, about what the engine's own reserve of 1152 leaves free, not a constant the code uses) → the live veto refuses (1100 is below every band's peak, 3147 or 4447), as Mike #68 requires. No engine: `free` ≈30 000 MiB (`dpc-client.log`, 2026-11-18 15:06:42: "32298 MiB of VRAM free"; Strata's own demand there was `total` 32623 less the 2048 fallback reserve = 30575, as the gate computed it before S3e; it now subtracts the alias's reserve, `strata_provider.py:457-465`) → the veto passes | fit arithmetic runs |
    | Windows, AMD or Intel | `shutil.which("nvidia-smi")` is None → `(None, None)` | `unknown` → L12 |
    | Linux, NVIDIA | the same two queries | fit arithmetic runs |
    | Linux, AMD or Intel | no nvidia-smi → `(None, None)` | `unknown` → L12 |
    | macOS | no nvidia-smi → `(None, None)`; unified memory, no device figure | `unknown` → L12: dictation queues or takes the CPU twin behind music |

    Only the **first** GPU line is read (`splitlines()[0]`), so a second card is invisible
    (Open Questions, Q12). `unknown` is never "fits".

- **Three outcomes, logged apart** (Ark #53: "unknown" is its own outcome):
  - `fits` → granted **beside** at once, no wait; both rows in `card_holders()`, the
    Whisper row with `beside: "music:yue2"`; one INFO line `card: <display> loaded beside
    <music display> (fits: <need> of <total> MiB declared, <free> MiB free)`.
  - `does_not_fit` (a known size or reading says no) → L12 chooses between the CPU and
    the line (`_LEASE_LINE`, L4).
  - `unknown` (any size unknown: `model_size_bytes` source None, a resident with no peak
    such as a llama-server child whose start could not size its model, or a Strata child whose total or reserve could not be read; or a reading unavailable or failed: no nvidia-smi, macOS, AMD,
    an error) → never "loads beside anyway" and never "refused" for a person's door
    (Zcode #52: fail-safe); like `does_not_fit`, it goes to L12's choice between the CPU
    and the line.
  - **A peer** in either non-fit outcome takes no CPU route (Mike #71) and has one test,
    L13's: it waits in the line only when its estimated whole trip fits in 120 s, and is
    refused at once otherwise (Johnny #14 obj. 1). Background transcriptions follow L12 (L4).
  - **One Whisper peak per loaded model**, whatever the number of open uses: the provider runs
    parallel calls on one model (`_active_transcriptions`; inference outside `_load_lock`,
    `whisper_provider.py:891-915`) and L1 makes a granted lease re-entrant, so the lease
    stays multi-use (Ark #13 new #2). The extra activation memory of parallel decodes is not
    counted; learned in operation.
  - **Which door "unknown" belongs to** (Zcode #22): waiting in the lease line is a
    **person's** request meeting an unreadable card or an unsized resident; a **peer's** trip
    estimate with any unknown part is refused at once (L13, test 37).
- **The music lease is untouched by a co-holder:** its busy test, its yield and its stop
  read only its own uses and child.
- **A third contender while two hold** (Zcode #52, Ark #53). A llama-server start or a
  second music job meets two holders. `_free_the_card` (and the lease line) ask to yield
  **only holders that are in the contender's way**: `contender._excludes(holder)` and not
  covered by a measured share for this contender. Among those, **idle cheap holders first**
  (`yield_cost="cheap"`: Whisper), then busy ones are waited for, each in `_CARD_ARRIVAL`
  order of the waiters. In v1 a co-living Whisper is in the way of every contender except
  the shared cell itself, so the "not in the way" clause guards any later share; it is
  pinned by a test now.
- **What it means for "one owner"** (Zcode #52): the invariant of one owner per model
  object (one Whisper object owns the pipeline through a reload) is untouched. What gives
  is the card's exclusivity, in one cell, with proof of headroom, visible as two holder
  rows. An idle music holder is still yielded as the matrix says; whether a fit should
  also spare an idle engine its stop is left open.
- This pulls a fit check into S3 for the Whisper+music cell only. There is no general fit
  check for Whisper beside llama-server: Mike #68 ruled that sharing out.

**L12. The GPU/CPU choice for a transcription** (Mike #62 and #71, decided; restated after
Ark #64 and #67 and Zcode #69 to compare times and to put the Strata check inside the
choice). Before a transcription takes a place in the line, in this order:

1. **The card is free, or L11 says `fits`** → the GPU, at once. (L11's half since S3e:
   `route_view` answers `fit` when everything in the way is a busy holder the lease shares with; no
   production holder is one before S4 gives music its lease; since S4 the music engines' leases are.)
2. **The audio is longer than `[local_transcription] cpu_max_audio_s` = 300 s** (Mike #71:
   "up to 5 minutes, easy") → the line; the CPU twin is not considered. **0 turns the CPU route
   off** (Mike 2026-11-24, Q3; Forge #60), as `idle_unload_s` 0 means never and `queue_wait_s` 0
   means do not wait: with `cpu_max_audio_s` or `cpu_max_s` at 0 every call L12 is asked about goes
   to the line (a free card or L11's `fits` still gives the GPU at once, point 1), the INFO line
   says "CPU route off: cpu_max_s is 0", a peer the owner lets onto the twin does not reach it, and
   an agent's transcription inside its own turn on a local alias (3.5) is refused at once ("…and the
   CPU route is off ([local_transcription] cpu_max_s is 0), so nothing was queued."). The settings
   reader and the provider's own defaults path both read 0 as 0; a value below 0, unreadable or
   not finite still reads as the default. `cpu_contention_factor` is a multiplier, not a switch: 0
   there reads as the default. The seeded `config.ini` line of each cap says "0 = never use the CPU".
3. **Is the CPU computing something else now?** The signal is explicit (Zcode #69), each
   engine's own test, not a guess from timing. **Mike 2026-11-24 (Q2; Ark #58-3, #61-2), narrowed the
   same day (#84 of the new count; built in follow-up 9b):** only small CPU work counts, "whisper, embedder,
   gliner"; a heavy engine kept off the card is not counted, nobody would run it there. What counts, each read
   as "computes something now":
   - a resident Strata **or llama-server** child (a claim on the card) busy by its own
     `_busy_for_the_card`: a live `accepted_request` hold in `_CARD_HOLDS` (the registry the
     `_CARD_HOLD` contextvar's holds are entered into, 693–701), a call in flight
     (`_in_flight > 0`), a caller in its window, or a child starting. An idle resident one is
     not contention (llama-server serves on the GPU, but a busy one takes CPU threads for its own
     work; an idle one computes nothing);
   - **not** a llama-server kept off the card (`--device none`): it holds no claim and computes on the CPU,
     but it is heavy and nobody would run a transcription beside it (follow-up 8 counted it while a call was
     in flight; follow-up 9b removed it);
   - the embedding model and GLiNER while they compute on the CPU: each wraps its compute call in
     `cpu_work.computing_on_the_cpu` (`dpc_client_core/cpu_work.py`; the embedder only when its device is
     `cpu`, GLiNER only when `_GLINER_DEVICE` is `cpu`, a call on a GPU counts nothing), and
     `_cpu_contention` reads `cpu_work.computing_now()`. They have no busy signal of their own, and the
     two are not card leases until S6 and S7;
   - a local Whisper transcription already running on the CPU: a CPU twin's, or any local Whisper
     whose model runs on the CPU (an alias set to `device: cpu`, a GPU load that fell back), one per
     call (`whisper_provider._CPU_RUNS`, `cpu_transcriptions_running()`), counted from before the model's
     load when the load is known to run on the CPU (the twin, an alias set to `device: cpu`, or a route's
     `cpu` device: `_loads_on_the_cpu`, `whisper_provider.py:819`), else once the model is up on the CPU (an
     `auto` alias, a GPU load that fell back); a call that waits behind another's load on `_load_lock` is not
     counted. A loaded twin that is idle is not.
   `card_lease.route_view` answers the engines and `cpu_work`'s calls (`cpu_contention`, `_cpu_contention`,
   `card_lease.py:1206`), the provider
   adds its CPU transcriptions, and the INFO line names each one it counted ("x2 for a busy
   llama-server 'qwen27' and a CPU transcription on 'turbo@cpu'"; at a factor of 1, "…,
   cpu_contention_factor 1, no penalty"); `last_route["contention"]` keeps the list. When any is
   counted, the CPU twin **is still allowed** for files within the 300 s cap (Mike #71, replacing
   the hard stop of the third revision), and its estimate is multiplied by
   `[local_transcription] cpu_contention_factor`, once, whatever the number of contenders.
4. **Compare times:**
   - **`gpu_wait`:** the busy holders' remaining time plus the waiters ahead in the line
     (L10); any unknown part makes it **long**. A running music job and a supervisor in a
     turn are both unknown in v1, so both are long.
   - **`cpu_estimate`:** the CPU twin's time for this file, cold (the twin is not loaded:
     its load and first call added) or warm, interpolated from the measured lengths below,
     times the contention factor from point 3 when it applies.
   - **The CPU twin** when `cpu_estimate < gpu_wait` **and** `cpu_estimate <= cpu_max_s`.
   - **Behind a local engine that stays on the card** (item 1a; S3 follow-up 7, Ark #58-2,
     Johnny #59-1): `gpu_wait` is not estimated (`route_view`'s `engine_stays`), so point 4
     compares nothing. A file within the 300 s cap goes to the twin when `cpu_estimate <=
     cpu_max_s` and a cold twin passes the RAM check; on this machine, where Strata is resident
     most of the time, that is every file up to 300 s, and only a longer one reaches the line and
     the swap. The INFO line says "not compared" for it. **The choice is a snapshot** (Ark #61-4):
     `route_view` reads the card once, at the seam. A call sent to the GPU on a free card that
     meets a holder when it asks waits in the line, and a call sent to the line stays there
     however the card changes (no switch in mid-flight).
5. **Otherwise the line** for the GPU, bounded by the door's deadline (L4; `queue_wait_s`
   for a person's doors), with the status line and a cancel.

Around the rule:

- **Background transcriptions:** the same test; failing it, skipped (`LeaseSkipped`).
- **Peers:** not on the CPU twin unless the owner turns on the optional setting (Mike #71,
  6C, decided; S5 puts it next to the device selector). Otherwise L13's 120 s rule.
- **Starting figures** (Mike #11, "learn the timings in operation"): the table below ships
  as the twin's figures, and every twin run replaces them with this machine's. The fifth
  revision's "nothing measured → no CPU route" is gone: a twin that never ran recorded
  nothing, so the route could never open. The rows over 10 s are warm figures; a cold twin
  adds ≈9 s (Johnny #14). **In code** (S3 follow-up 2, Johnny #24-2) the twin's warm figure is
  kept by the batch descent's length bands, the slowest row of each: up to 60 s 0.91× (10 s in
  11 s), up to 120 s 1.15× (120 s in 104 s), over 180 s 0.98× (300 s in 306 s); up to 180 s has
  no row and takes the over-180 s figure. **The carrier** (Johnny #53-4, S3 follow-up 6): these
  rows are this table's, numbers reported in a chat reply, and they are slower than the repo's own
  `cpu_run.log` (RU 30 s in 20.31 s, 1.5×; RU 118 s in 107.99 s, 1.1×; see "GPU figures on speech")
  and than Forge's runs (2.36× / 1.53× / 1.42× at 30 / 118 / 300 s). They are kept as conservative
  seeds: L12 picks the twin less often than it could, until the twin measures its own. **The bands
  are not monotonic** (Forge #48-2): up to 120 s 1.15× against up to 180 s 0.98×, so the twin's
  estimate jumps at 120/121 s by about 19 s (104 s against 123 s) because of the table, not of
  data; L12's verdict near 120 s inherits that step until the twin measures its own figures. The ≈9 s of load and first call is all in `load_first_s`
  (9 s, the twin's first load in the process); a reload in a running backend is
  `load_reload_s` 1.0 s, the backend's own log line below ("Whisper model loaded in 1.0 seconds
  (device=cpu)"), by the same logic as the GPU's (S3 follow-up 4). The 9 s is a cold process's:
  Forge measured the twin's load at 1.09 s with torch already imported (#60), so in a backend where
  the GPU loaded first the twin's first load errs long by about 8 s (kept: the safe side); and the first call's
  `penalty_s` is 0 in every band until the twin measures one.
- **Enough RAM:** a cold CPU route is taken only when available host RAM covers the twin
  plus a margin (one constant, `twin_ram_mib` + `ram_margin_mib`, values unverified; test 40
  fakes it), read as `psutil.virtual_memory().available`, the one instrument for the
  gate, the log and the UI; otherwise the line (proposal). `commit_memory.read_free_commit()`
  also exists and reads commit too; it is not used here (Forge #20-3, who read 7.1 GB
  available against the twin's ≈4 GB, up to ≈10 GB). **What the two constants stand for**
  (Forge's measurement, 2026-11-23, this machine, CUDA hidden, 4 runs; Forge #54): the twin loads
  fp32 weights, 3086 MiB, not the 1543 MiB of fp16. From an empty process, `import torch` adds
  656 MiB, the weights 3101, the pipeline 19, and the transcription 914–1355 by batch; total RSS
  +4527 MiB (30 s, batch 16), +5125 (118 s, batch 4), +4688 (300 s, batch 2). So
  `_TWIN_RAM_MIB` 4096 is about the import plus the weights, and `_RAM_MARGIN_MIB` 1024 is about
  the transcription's activations: the "margin" is the second half of the load, not headroom. The
  values are kept; their 5120 MiB covers each measured run but the 118 s one at batch 4 (by 5 MiB,
  arithmetic on Forge's figures). **On this machine** (Forge #60, Strata resident with its 22 GiB
  cache): 9308 MiB available before the twin loaded, 6063 after, so the gate passes today. **Two
  checks** (S3 follow-up 7, Ark #61-3): at the seam for a cold twin (the snapshot L12 decides on)
  and in the twin's own load, under its load lock, which catches a twin unloaded by its idle timer
  after the seam; a failed load is the call's failure, or section 3.5's refusal. A warm twin holds
  about 4.5–5 GiB of host RAM until its idle unload (60 s, `TWIN_IDLE_UNLOAD_S`, or the GPU
  alias's `cpu_idle_unload_s`); no card line counts it, and the next cold check reads it in
  `available`. **The gate is not model-aware** (Ark #61-3): 5120 MiB for any model; sizing it from
  the model's measured size is S3e's, beside `vram_mib` by band.
- **No switch in mid-flight** (Zcode #69): the choice is final for that call. A dictation
  that started on the CPU twin stays there even if the card frees a second later; only new
  calls take the GPU. A call sent to the line does not later jump to the CPU. **One exception
  before the flight** (Johnny #63-5, S3e): a twin that fails to **load** (its own RAM check, a load
  error; not a model that is not cached, which the GPU would meet too) has processed no audio, so
  `transcribe_with_route` sends the call to the card's line with its own arguments and L12 is not
  asked again (`cpu_route=False`); inside an agent's own turn (3.5) it is refused at once, as
  before. A twin that fails while transcribing has started: that failure is the call's.
- **`cpu_max_s`, 600 s (start value, decided 2026-11-23, item 9):** neither route makes a person wait longer than the
  dictation bound. 0 turns the CPU route off (point 2, Mike 2026-11-24). With the measured factor of about 1.0–1.1× real time, the 300 s audio
  cap binds first when nothing contends.
- **`cpu_contention_factor`, 1.0 (start value, decided 2026-11-23, item 9; tuned from operation)**, defined as the CPU ratio with an
  engine computing divided by the CPU ratio alone; the base CPU speed on speech (1.1–1.5×) is
  the twin's own figure, not this factor (Ark #30). Mike's #71 and the logs
  (`~/.dpc/logs/dpc-client.log*`: the Whisper alias ran with `device=cpu` on 2026-11-14 to
  11-20, e.g. "Whisper model loaded in 1.0 seconds (device=cpu)" on 2026-11-20) say a 5-minute
  file on the CPU beside Strata works; a factor of 2.0 would push a 300 s file (≈306 s) past
  `cpu_max_s` and contradict him. The measurement Ark #67 and Zcode #69 ask for (a Strata
  turn's tokens per second and length with and without the twin running) sizes it. Both
  numbers ship as start values and are tuned from operation (Mike #11); the in-turn path of
  3.5 does not depend on them. **What it reads** (S3 follow-up 7, Ark #58-3 and #61-2, Johnny
  #59-2; widened by Mike 2026-11-24, Q2, S3/S4 follow-up 8): point 3's signal, each contender named:
  a busy resident Strata or llama-server, the embedding model or GLiNER computing on the CPU, a
  local Whisper transcription already running on the CPU (a llama-server off the card was counted by follow-up 8
  and is not since 9b, Mike's narrowing of 2026-11-24). Before follow-up 8 only a busy Strata
  raised it, and two CPU routes at once were each estimated as if alone. At 1.0 it applies no
  penalty, and the INFO line says "cpu_contention_factor 1, no penalty", not "x1". The
  measurement names its threads: `torch.get_num_threads()` was 16 on this machine (Forge #60).

**Residency** (Mike #71: "keep a model in memory only if there is a queue for it"). The CPU
twin has **its own `idle_unload_s`**, the mechanism already built and pinned in step 1
(Ark #67, Zcode #69), with a **short default, proposed 60 s**; it is never loaded
speculatively, and its life is not tied to "the card is held" (which would add a second
unload trigger and a mid-dictation question). The first call after an idle period pays the
cold start (≈9 s, below). Mike's sentence also covers the GPU Whisper alias, whose idle
unload shipped in step 1 at 300 s; Mike confirmed that default on 2026-11-22 (Decided by
Mike). Nothing in code changes here.

**The measurement** (CC's subagent, reported at #66: Whisper turbo on the CPU of this
machine, a Ryzen 9950X, Mike's own Telegram voice message; not re-measured here):

| Audio | CPU time | Notes |
|---|---|---|
| 10 s | 11 s warm, ≈20 s cold | so load + first call ≈ 9 s |
| 30 s | 22 s | |
| 60 s | 53 s | |
| 120 s | 104 s | |
| 300 s | 306 s | ≈1.0–1.1× real time (Ark #67) |
| host RAM | ≈4 GB, up to ≈10 GB on long recordings | as #66 reported it; not measured beside a resident Strata **[unverified]**; the one RAM figure this record uses |
| CPU load of a running YuE2 job | [pending] | decides whether music counts as CPU-contended |
| a Strata turn with the twin running | [pending] | sizes `cpu_contention_factor` |

The CPU route holds **no card lease** and touches no VRAM.

**GPU figures, the first** (Forge #21, 2026-11-21; outside the backend, a synthetic tone, not
speech; **speech figures follow below**): cold load of turbo fp16 ≈1–2.4 s (`from_pretrained` 0.1–0.2 s, `.to(cuda)` 0.8–2.2 s),
reload after an unload 0.6–0.9 s (a direct `from_pretrained` and `.to(cuda)` outside the backend,
not the provider's `pipeline(...)` path; Forge #48, S3 follow-up 6); ratio 79× (30 s, `generate`) to 154× (120 s, pipeline), an
**upper bound** because a tone compresses to a few tokens. `torch.compile` fails on torch 2.11, so the
"4.5× speedup" comment at `whisper_provider.py:313` does not apply and these figures are not
comparable with the CPU table. The provider docstring's "RTX 3060 ~10-13×" (`:63-66`) is not
our figure. A CPU number is never used for a GPU route.

**GPU figures on speech** (CC's measurement, 2026-11-22; Mike asked for it). The real
`LocalWhisperProvider` (`_load_model`/`_unload_model`), alias `whisper-large-v3-turbo`, cuda,
fp16, compile tried and failed (uncompiled, sdpa), chunk 30, the provider's adaptive batch
(>60 s: 8, >120 s: 4, >180 s: 2, else 16), `return_timestamps=True`, a fresh process in the
backend's venv, **the card empty** (0 MiB used, Strata not resident). Russian voice message and
English podcast, language auto-detected, transcripts correct. Median of 3:

| Clip | GPU time | Ratio | torch peak alloc / reserved, MiB | driver used after, MiB |
|---|---|---|---|---|
| RU 10 s | 0.23 s | 43× | 1766 / 1984 | 2274 |
| RU 30 s | 1.18 s | 25.5× | 1830 / 2052 | 2342 |
| RU 60 s | 1.65 s | 36× | 2385 / 2618 | 2910 |
| RU 118 s (batch 8) | 4.31 s | 27× | 3499 / 3840 | 4132 |
| EN 30 s | 1.42 s | 21× | 1852 / 2088 | 2380 |
| EN 120 s (batch 8) | 4.08 s | 29× | 3650 / 3948 | 4240 |
| EN 300 s (batch 2) | 18.89 s | 15.9× | 2355 / 2660 | 2952 |

- **The table is two recordings sliced to seven lengths, not seven samples** (Johnny #56, small 3): the RU voice message cut to its first 10, 30, 60 and 118 s, the EN podcast to its first 30, 120 and 300 s, so the clips of one language are prefixes of each other. The RU 10 s median of 43× is the median of three; its **first** run was 8.8×, the warm-up the next bullet counts. The range for any plan is 16–43× with that caveat.
- **The ratio on speech is ≈16–43× on the GPU** (the tone's 79–154× was an upper bound). The
  first call after a load costs ≈0.9 s more on a 10 s clip and ≈0.7 s on a 60 s one; at 30 s and at
  118 s and longer it is within run-to-run spread (`gpu_run1.log`, "first" against the median).
  **Read with the log** (S3 follow-up 2): only RU 10 s's first run is the first call after a
  load; the other clips' first runs came on the already-loaded model, so the 60 s figure is a
  first run of a new clip, not a first call after a load. These seed the GPU route's whole-trip
  estimate, as the CPU table seeds the CPU route, as `load_s + audio / warm[band] + penalty_s`
  (Johnny #24-1/2): the warm ratio by the batch descent's length bands, the slowest clip's
  median in each (up to 60 s 21.1×, EN 30 s; up to 120 s 27.4×, RU 118 s; up to 180 s 15.9×,
  estimated from the batch-2 figure, since batch 4 was never measured (Johnny #41-1: this note
  called it "the slowest figure", which is true of the number and not why it was taken); over
  180 s 15.9×, EN 300 s), `penalty_s` 1.0 s in every band (RU 10 s's +0.91 s rounded up, the one
  first call after a load in the log; by band since S3 follow-up 4; **only the up-to-60 s band is
  measured**: the bands over 60 s carry RU 10 s's figure, while the log shows a first run at 118 s
  and longer within run-to-run spread, ≈0, so those bands err long by about a second until a first
  call there measures its own; Forge #48-3, S3 follow-up 6), and a load of 12.8 s
  before any load has run in the process (`load_first_s`) and 0.8 s after one has
  (`load_reload_s`, the reload under Load, 0.76–0.83 s; S3 follow-up 4, Forge #42-4). The
  recorded figures take over per device and band after each run: a later call moves its own
  band's warm ratio, the first call after a load moves its own band's `penalty_s` (what it took
  beyond `audio / warm`, at least 0), a load moves the load figure of its kind. On a warm GPU a 300 s voice note (≈19 s) fits a peer's 120 s budget; so does a
  cold first load after backend start (≈13 s more, measured as a fresh interpreter; see Load).
- **Memory:** the context is 280 MiB on an empty card (Forge saw 477–485 with Strata
  resident); after load 1543 allocated, 1674 reserved. The peak is **batch-dependent**: it is
  highest at batch 8 (60–120 s clips), lower at 2 (300 s) and at 16 (short clips). That is
  what sets `_WHISPER_OVERHEAD_MIB` in L11.
- **Load:** cold load in a fresh process 12.8 s (a fresh interpreter, so it includes the first
  library init; inside a running backend torch is already imported and Forge measured the load
  step alone at 1–2.4 s); unload 0.35–0.61 s, VRAM back to the context only; reload
  0.76–0.83 s through the backend's own `pipeline(...)` path (Forge's 0.6–0.9 s is a direct
  `from_pretrained` outside the backend, S3 follow-up 6). That reload is the cost the 300 s idle unload rests on.
- **The CPU on the same speech** (float32, same transcripts): 30 s in 20.3 s (1.5×), 118 s in
  108 s (1.1×), in line with the step-5 table above (1.0–1.1×). The GPU is ≈17–25× faster.
- Logs: `ideas/measurements/whisper-gpu-bench-2026-11-22/` (`bench.py`, `gpu_run1.log`, `cpu_run.log`; gitignored, local to this machine), copied from CC's session scratchpad on 2026-11-22. SHA-256: `bench.py` `627050fc18bddd92d12024e13029aa21ac606e27ee8ee29028f0a75491bc5449`; `gpu_run1.log` `95d13a76b021994e49f8ea9e119fd5578b323461e1a90777f5969b4675467092`; `cpu_run.log` `ccb3f1500b5dc8662520ff5e841f5e9e87d48bb3846befa2acb211ac55d33292`. The seven stimulus WAVs (`ru_10`, `ru_30`, `ru_60`, `ru_118`, `en_30`, `en_120`, `en_300`) are kept in the same folder, local and gitignored; one is a private voice message and is never committed.

**L13. A peer's wait builds on ADR-044's amendment of 2026-11-18, "a peer waits for the card
behind every owner request"** (Mike #85: "why new? there are tasks in the backlog and the ADR
describes it"; CC #87 agreed and corrected #84).

The amendment (`044-…md:1608`) is Mike's call of 2026-11-09 (#262), repeated 2026-11-17: option
1 of the card `A-PEER-REFUSED-FOR-A-BUSY-CARD-IS-NOT-QUEUED-AND-THE-GUEST-DOES-NOT-ASK-AGAIN`
(gitignored `backlog.md`, 1318). It is still a **parked draft, not built** (ADR-044, the
amendment below it, line 1871), and the code agrees: `_why_a_peer_may_not_swap` still refuses a
busy holder at once and "claims and awaits nothing" (`llama_server_supervisor.py:2632-2651`).
So Mike's #70 decision does not add a branch to DPC's peer handling; it extends the one he
already decided, from the card queue to the lease line.

**Reused, by reference and not restated:**

- **The order.** A peer's place stands behind every owner request (044 point 3). L5a's "a peer
  never goes ahead of an owner's waiter" is that rule in the second line.
- **The bound** (044 point 6, `_a_peers_slot_wait_s()`, `llama_server_supervisor.py:1505-1512`)
  is reused for inference peers only; a transcription's limit is the guest's 120 s (point 2).
- **Best effort.** Under continuous owner load a peer may never get the card (044, "A peer is
  best effort").
- **A peer's place blocks no owner work** and makes no holder busy (044 point 3). A peer in
  `_LEASE_LINE` makes no lease busy and does not stop L5 from admitting an owner's use.
- **The words.** A refusal to a peer names no agent, chat or caller (044 point 7) and says
  "right now" (L3).
- **The owner's terms.** A peer makes an idle holder yield only when the owner's own conditions
  hold: swap on, nothing queued, nothing busy, and the holder idle for 60 s since the owner's last request
  (`PEER_SWAP_IDLE_S`, Mike 2026-11-24) (ADR-044's amendment of 2026-11-06, the module predicate
  `why_a_peer_may_not_swap`, `llama_server_supervisor.py:727`). With `swap_when_idle` off, the peer keeps today's
  refusal (044 point 9 and Known limits 1 of the 2026-11-18 amendment).
- **Inference peers meeting a lease.** S2 item 1 makes `_blocking_holders()` return leases, so
  a peer's inference place, once the amendment is built, waits behind a busy lease the way it
  waits behind a busy supervisor. **Order of work:** the amendment's code lands before or with
  S2. If it does not, S2 leaves an inference peer's refusal as it is today (refused at once
  while a lease is busy) and only transcription peers wait.

**New here (what the amendment does not say, and S3 must build):**

1. **A second kind of peer request, with no `provider_loading`.** The amendment is written for
   inference: a refusal code, `retry_after_sec`, a guest that asks again every 10 s (044 points
   4 and 5). A peer's transcription is a different exchange, `REMOTE_TRANSCRIPTION_REQUEST` and
   `_RESPONSE`. The response carries `text` with `status` "success" (and `language`,
   `duration_seconds`, `provider`) or `error` with `status` "error", and no code or
   `retry_after_sec` (`dpc-protocol/dpc_protocol/protocol.py:392-415`); the host's handler
   (`p2p_coordinator.py:1342-1420`) runs the provider directly and answers every failure as
   `error=str(e)`; the guest sends the audio once, in the request (`audio_base64`), and has no
   re-ask loop. So the amendment's freshness rule (a place marks nothing before a second ask)
   and its `provider_loading` do not exist for transcription. **The baseline today** (Ark #13
   new #1, #2): the handler has no engine gate (`_local_engine_refusal` is called only on the
   inference path, `p2p_coordinator.py:1135`), so a peer can load GPU Whisper beside a live
   Strata; and peers transcribe in parallel (`_peer_inference_lock`, 110, is taken only for
   inference, 1119). The lease adds the gate; parallel uses stay (L11, one Whisper peak). **Accepted consequence** (Ark #18-1, Mike
   #11): every refusal text reaches the foreign node verbatim, as `error=str(e)`
   (`p2p_coordinator.py:1412`; the access-denied reply is a bare string too, `:1354-1357`),
   and the response has no `code` or `retry_after_sec` (`protocol.py:392-399`; the inference
   response has both). S1 writes its refusal texts for a foreign node (no alias, agent or
   chat), and a guest cannot tell a card refusal from a broken provider.
2. **The guest gives up at 120 s, for the whole trip.** Both call sites hard-code it
   (`service.py:4680`, `:10168`; defaults `service.py:10093`, `p2p_coordinator.py:1664`), from
   sending the audio to the answer, so the wait, a Whisper load and the transcription all count.
   **Decided (Mike #11, item 10: "ok 120"), in the reviewers' framing (Ark #13, Forge #15):** a
   peer's transcription that is granted at once is served as today; otherwise it is taken into
   `_LEASE_LINE` only when the estimated whole trip (the wait, the load when Whisper is cold,
   and the audio duration divided by the measured real-time ratio) fits in 120 s, and is refused at once,
   "right now", when it does not or when any part is unknown (L10). No protocol change. The
   fifth revision's `min(peer bound, 120 s)` was always 120 s (Johnny #14 obj. 2), and waiting
   an unknown estimate out to the cap would answer a guest that had left. **One clock** (Ark #30, Zcode #31, Johnny #32): a
   peer's clock runs from arrival to the answer, so `wait_s = 120 − (load estimate +
   transcription estimate)`, with no floor (L4's margin is for a person's door); the line is
   entered only when the holder's own ETA fits in that `wait_s`. In v1 a running
   music job and a busy supervisor have unknown ETAs, and the backend itself has never loaded
   Whisper on the GPU, so the per-device recorded ratio is empty (the seed figures of "GPU
   figures on speech" stand in, Q7), so a peer is in practice served when the card is free or
   Whisper is warm and refused at once otherwise; since Mike's decision of 2026-11-24 it may have an idle Strata or llama-server swapped out on the owner's terms (which include 60 s idle since the owner's last request) when that stop plus its own work fits ("As built" below). The line opens to
   peers as figures accrue. **Known limit (Ark #13):** the guest tries its candidates in turn
   (`service.py:10157-10172`, `continue` after a failure), so a person on the guest's side may
   wait candidates × 120 s. Not changed here. **Decided in S3c, from the code** (read on
   `e8b5ac1a`; Review notes, "S3c build"): the guest walks its peers, one voice alias per peer
   (`_peer_transcription_candidates`, `service.py:10148-10173`, `break` after a peer's first voice
   alias; `_transcribe_with_peer`, `:10207-10222`, `continue` after a failed peer). A peer's card
   refusal reaches the guest as text only (`error=str(e)`, `p2p_coordinator.py:1437`, turned into
   `RuntimeError(error)` by `transcription_handler.py:79`), and the next peer is another node's card.
   So the walk over peers is not a walk over this node's GPU aliases (section 3.1, condition 2), and
   the card-refusal break does not apply to it; `test_a_peer_that_fails_is_followed_by_the_next_one`
   keeps pinning it.
   **A rule, decided by Mike #17, 2026-11-23:** a peer's transcription whose whole trip does not
   fit the guest's 120 s (`PEER_TRIP_S`) is refused at once, long audio that would go to the CPU
   twin included: at the CPU ratio of 1.1–1.5×, 300 s of audio takes about 200–270 s, so it is
   refused. Before #17 the refusal followed from the arithmetic; now it is stated.
   **As built in the lease line (S2 follow-up; Mike's option (a), by consensus, review #75–#91).**
   A supervisor's claim: a peer gets the card only when no claim stands on it, or when the owner's terms below
   let it have an idle one stopped (2026-11-24); the card queue and the eviction of a supervisor are otherwise
   the owner's mechanism, and a peer reaches the eviction only on those terms. In the code: a peer's lease request is
   granted at once when nothing is in its way, and is refused at once whenever a supervisor holds
   the card and is not going (starting or busy, swap on or off: a supervisor has no ETA; idle, unless
   the owner's terms below let the peer have it stopped) or any owner's place stands in the card
   queue (`_estimate` reads both as unknown). **S3a:** a supervisor that is already going (stopping or draining) counts its stop,
   `_expected_stop_s()`, so a peer behind it may wait when that stop plus its own `expected_s` fits.
   **A peer may stop an idle engine (Mike, 2026-11-24; supersedes "a peer stops none" of option
   (a), and the S4 report's "peers never force an idle engine"; S3/S4 follow-up 8).** ADR-044's
   amendment of 2026-11-06 already let an inference peer swap out an idle supervisor on the owner's
   terms; the lease line now reads the same rule for a peer's transcription, one predicate for both
   lines: `llama_server_supervisor.why_a_peer_may_not_swap(holders)` (the supervisor's
   `_why_a_peer_may_not_swap` calls it with its `_blocking_holders()`, `card_lease._swaps_engines`
   with the holders on the card): the swap switch on, nothing in the card queue and no owner's
   request waiting in the lease line, no holder busy by rule 1 or already stopping, **and no holder that served the owner's own request within the
   last 60 s** (`PEER_SWAP_IDLE_S`, `llama_server_supervisor.py:858`; follow-up 9b, Mike's call of 2026-11-24 on
   Zcode #85-1): `_owner_request_ended_at[alias]` is stamped when a card hold leaves its alias
   (`_off_the_alias`: a chat turn, an agent turn or a gateway call, success or failure; a peer's call holds no card
   hold), and an alias that never served the owner counts as idle. `caller_idle_s` is another rule: it covers a
   gateway client and a peer, not the owner. When it holds,
   for a lease that `waits_for_engines` (the real Whisper lease, since S3b), the idle resident
   Strata or llama-server counts its own stop figure, `_expected_stop_s()`, in the peer's estimate,
   and the whole trip (that stop, plus the peer's `expected_s`, which holds Whisper's load when cold
   and its work) must fit `PEER_TRIP_S` = 120 s; then the peer takes a place, and the line stops the
   supervisor by the owner-start mechanism (`swap_out_for_a_lease`: the owner's live conversation
   saved first, Strata's parked count read and said, the owner's swap note) and grants the peer when
   the claim is gone. A stop figure that cannot be read leaves the trip unknown: refused at once
   (L10). A stop plus work over 120 s: refused at once, nothing stopped (the check comes before any
   place or mark). A busy supervisor still refuses the peer at once, as do the swap off, an owner's
   waiter in either line, and a lease that waits for no engine. The refusal words to a peer are the
   generic `LeaseRefusedForPeer` ones and name no alias, engine, agent, chat or holder (L3, 044 point
   7; the leak Zcode #22 (iv) fixed in S3e stays fixed). L12's view for a peer is unchanged
   (`route_view`'s `engine_stays`, item 1a): with `let_peers_use_cpu_twin` on, a short file still
   takes the twin first. Tests: `test_a_peer_may_have_an_idle_engine_swapped_out_on_the_owners_terms.py` and, through both lines with
   an injected clock, `test_a_peer_may_swap_an_engine_only_a_minute_after_the_owners_last_request.py`;
   `test_a_peer_lease_waits_only_when_its_estimate_fits_its_deadline.py` re-pinned (the busy half
   stays, the idle half is refused only when the owner's terms or a figure are missing). Behind lease
   holders and lease waiters only, it may wait: each part of the estimate is known (a busy lease's
   `eta_s`, an idle cheap lease's 1 s unload when `_may_ask` allows a peer to ask, each waiter
   ahead's `expected_s`; an idle engine-cost lease has no figure) and that wait plus the peer's
   own `expected_s` fits in `PEER_TRIP_S` = 120 s. The line's bound for a peer is
   `min(door's bound, 120 − expected_s)`, and 120 − `expected_s` when the door gave none
   (`_a_peers_slot_wait_s()` is no longer read for a lease). A peer whose own `expected_s` is unknown
   is refused at once, **also when the card is free** (L10: an unknown part leaves the trip unknown; Mike #17: a trip that cannot be shown to fit is refused; Johnny #30-4, S3 follow-up 3), in the same `LeaseRefusedForPeer` words as behind a blocked card; before follow-up 3 a free card served it with no ceiling. **The 120 s rule also at the grant (Ark #20-1):** a peer whose own `expected_s` is known and over `PEER_TRIP_S` is refused at once even when the card is free, in the generic peer words and naming no holder; before this a free card granted any length (a CPU twin at about 0.98x on 300 s, or the GPU's 15.9x on 1800 s, both over 120 s). The provider
   produces `expected_s` since S3a (`LocalWhisperProvider.expected_seconds`, `card_ask`); since S3b the
   Whisper lease takes it, and the peer's door re-checks the guest in the wait and at the grant (point 3).
3. **Where the wait sits.** In `_LEASE_LINE`, before `_load_lock`, behind every owner waiter
   (L5a), the handler wrapped in `serving_a_peer` (S3). A frame held open has no "second ask":
   the place ends at a grant, at its deadline (`wait_s` above), or when the guest's connection ends.
   **[unverified]** whether the handler task is cancelled when the peer disconnects mid-wait;
   if it is not, the waiter lives to its cap (Open Questions, Q11).
   **Accepted consequence** (Ark #18-2): transcription has no cancel frame. A guest that gives
   up drops only its own future (`p2p_coordinator.py:1690-1695`) and an answer to a request no
   longer pending is dropped (`transcription_handler.py:65-68`), so the host keeps working for a
   guest that left. One cheap guard: at the grant S3 re-checks `peer_id in
   self.p2p_manager.peers` (pattern at `:1672`), and counts "transcription answer dropped,
   request gone". A cancelled waiter leaves in the existing queue's form, `except
   BaseException: request.leaves()` (`llama_server_supervisor.py:3221-3223`; Johnny F).

### 2. Integration

#### 2.1 Who excludes whom (v1)

The guiding rule: a lease is visible to everyone, and who waits or yields for whom follows
Mike's words (#20, #68, #70) and the cost of a reload.

| contender \ holder | llama-server | strata | music lease | whisper / embed / ner lease |
|---|---|---|---|---|
| llama-server start | arithmetic, as today | excludes | **excludes: waits while busy, yields while idle** | **excludes: waits while busy, yields while idle** |
| strata start | excludes | excludes | **same** | **same** |
| music start | **excludes: waits while busy; idle and nothing waiting → swaps it out like an owner's start, with swap on** (Mike #70, SPEC decision 32 amended) | same | music engines: `exclusive()` as today; their leases agree | **excludes: waits while busy, yields while idle** |
| whisper load | **excludes (Mike #68): busy → L12 (CPU twin or the line); idle → L12 first, then a swap with swap on (item 1a, decided 2026-11-23)** | **same** | **busy: the one shared cell, beside if it provably fits (L11), else L12; idle: yields** | same holder: re-entrant; another whisper alias: excludes |
| embedder / ner (background) | granted only if the card is free, otherwise CPU | CPU | CPU | CPU |

**Whisper waits for supervisors** (Mike #68: "no, it must not load beside").

- The whisper × supervisor exclusion is symmetric in the first slice. The v1 asymmetry of
  earlier revisions, its "loaded beside" log line and its `beside` row are gone; `beside`
  stays only for L11's one shared cell.
- A busy supervisor (a turn in progress) is waited for through L12: a file up to 300 s
  goes to the CPU twin (Mike #71), a longer one takes the line.
- **The consequence Zcode #69 names:** a dictation longer than the CPU allowance (300 s
  audio) during a long agent turn takes the line, and if the turn holds the card past the
  dictation bound (600 s) the dictation is refused.
- **An idle supervisor (item 1a, decided 2026-11-23, Mike #15):** L12 first (a file up to 300 s
  goes to the CPU twin, so a dictation never costs the LLM its reload or Strata its parked
  conversations); a longer file, with the swap on and nothing waiting, swaps the idle
  supervisor out exactly as an owner's start or a music job does (section 3.2). With the
  swap off it waits, like an owner's start today.
- The self-wait case is now in the first slice: an agent turn on Strata whose tool
  transcribes stands on Strata, so its lease would wait for its own turn. L12 sends a file
  up to 300 s to the CPU twin; a longer one is refused at once (`LeaseWouldWaitOnItself`).

**The swap switch.**

- `swap_when_idle` keeps governing only the stopping of llama-server and Strata.
- Yielding a cheap in-process lease (unloading Whisper) or an idle music engine is not
  gated by the switch. That matches the music idle stop and `MusicEngines.exclusive`
  today, and nothing is lost but a reload.
- The two costs are not equal (Ark #45, D2): unloading Whisper costs a reload of about
  1.5 GB of weights, measured at 0.6–0.9 s outside the backend (Q7); stopping an idle music
  engine costs the next job an engine boot and a
  5–9 GB model reload. Whisper's reload is measured (0.76–0.83 s, a fresh process, not inside
  the backend); **[unverified: the music engine's boot and reload time]**. Neither
  loses anyone's work.
- **Item 2, decided 2026-11-23 (Mike #15).** It interacts with Mike #70: music swapping an idle
  supervisor **is** gated by `swap_when_idle` (owner-start rule), while a music engine or
  Whisper yielding to another lease is not.

#### 2.2 How llama-server and Strata start honouring leases

All in `llama_server_supervisor.py` unless noted. Small edits.

1. `_live_holders()` as above.
   - `card_holders()` and `_blocking_holders()` read `_live_holders()`.
     `_why_a_peer_may_not_swap` gets leases through `_blocking_holders()`.
   - `_live_claims()` and every `_CARD_CLAIMS` access stay supervisor-only, including
     `_this_request_stands_elsewhere_on_the_card`, `_this_request_is_not_new`,
     `_card_held_error`'s `self not in _CARD_CLAIMS` and Strata's `_pin_launcher_running`.
2. `_excludes(other)` needs no change: a lease has `EXCLUSIVE_ON_THE_CARD = True`.
3. `_wait_in_the_card_queue` (2805): `if not swap_policy()[0] or not self.on_the_card():
   return None` becomes:
   - return None if not on the card;
   - with swap off, return None unless the start is blocked and **every** blocking holder
     is a lease. With nothing blocking, the swap-off early return stays as today.
   - In other words, with swap off a start may still take a place when only leases stand in
     its way.
   - The request carries `for_leases=True`, so `_wait_to_be_first` (2873) and
     `_free_the_card` (3029) skip their "the switch was thrown" early return for it.
   - **This makes `_CARD_QUEUE` non-empty with swap off**, so the background-call check
     at 2809 (`_CARD_QUEUE or not self._child_is_up()`) starts skipping background calls
     in a configuration where it never did (Ark #44). See Negatives; it has its own test.
4. `_free_the_card`, before the busy test: if the request's `_CARD_HOLD` stands on the
   blocking holder, refuse at once. For a lease, "stands on" means one of its open uses
   carries `request.hold`; for a supervisor it is today's `_this_request_holds`. Raise
   `LeaseWouldWaitOnItself` for a lease holder; keep rule 9's path for a supervisor.
   - The busy test is `holder._busy_for_the_card(state, but_for=request.hold)`; section
     2.2.1 defines it for a lease.
5. `_stop_for_the_swap` (3117), per holder kind: `if getattr(holder, "IS_CARD_LEASE",
   False): parked = 0; outcome = await holder.give_up_for(self.alias, request.stop_bound);
   live_saved = None` else today's `_parked_conversations()` and `stop_by_hand(...)`.
   - `_note_swap` runs only when the outcome is `let_go`. Its words already fit a lease:
     with `DROPS_PARKED_ON_STOP = False` no parked text is printed ("swapped
     'whisper:local' out for 'qwen'"). The toast title "Local engine swapped" is chosen per
     kind: "Card handed over" for a lease. `_LAST_SWAP` works unchanged.
6. `_who_stands_on(alias)` (986): when `alias` is a lease, return `f" In use by
   {lease.who_uses()}."` Labels: "dictation", "music Transcribe of track T", "agent 'X'
   tool transcribe_audio_file", "music job J", "a peer's transcription".
   - This is owner-only; the peer path never reaches it.
7. `_card_held_error`: use `holder.stop_hint` when it exists ("it lets go when idle; Stop X
   unloads it now") instead of "Stop it from the providers settings".
8. `stop_local_engine` (`providers/llamacpp_server_provider.py:2001`) and
   `service.stop_local_engine` (12177): if the alias is a lease holder, call
   `lease.let_go_by_hand("Stop by hand", bound)`: idle → `give_up_for`; busy →
   `LeaseBusy` "busy with <who_uses>". This keeps the music card's "Stop X" button and the
   panel's **Stop engine** working for lease holders (Ark #45).
9. Background calls (rule 10) need no change: with a lease in the way, `_child_is_up()` is
   false and the call is skipped as today.
10. `_wait_to_be_first` and `_free_the_card` consult `_LEASE_WAITERS()` for an earlier
    excluding lease waiter (L5a point 2).
11. `_blocking_holder()` (2507), which picks the holder a round acts on, orders several
    holders (L11, a third contender): the request's own stand first (rule 9, as today),
    then idle cheap leases, then the rest. Only holders in the contender's way are in the
    list at all (`_blocking_holders` already filters by `_excludes`; a measured share for
    this contender removes its co-holder too).
12. **A lease contender swaps an idle supervisor** (Mike #70 for music; item 1a for
    Whisper). A module function, `swap_out_for_a_lease(holder, lease, stop_bound)`, runs
    the owner-start path for a contender that is not a supervisor: the same conditions as
    `_free_the_card` (swap on, the contender first by L5a, `holder._busy_for_the_card(state)`
    False, no description-borrow refusal), then `holder._begin_swap_drain(lease)` in the
    stretch of the busy test, `stop_by_hand(holder, why="swap")` under `_SAVE_BY` so the
    owner's live conversation is saved before the stop (as
    `test_a_swap_saves_the_owners_live_conversation_before_the_stop.py` pins), and the
    `_note_swap` words and toast, with Strata's parked conversations said dropped (the count
    is read before the stop, as `_stop_for_the_swap` does; section 2.4). The lease waits for
    the stop as `_wait_for_the_stop` does.
13. `_stands_before_the_line` compares with the lowest arrival across both lines (L5a
    point 5).

##### 2.2.1 The lease as a holder: its full duck surface

What every reader in `llama_server_supervisor.py` touches on a holder, and what a lease
answers. Nothing here is read through `_live_claims()`.

| Member | Read by | Lease semantics |
|---|---|---|
| `alias` | `_blocking_holders`, `_this_request_holds`, every refusal, `_note_swap` | the holder name, `whisper:<alias>` etc. |
| `ENGINE` | `_excludes`, refusals, `_LAST_SWAP` | `"whisper"`, `"yue2.cpp"`, `"acestep"`, `"embedder"`, `"gliner"` |
| `EXCLUSIVE_ON_THE_CARD` | `_excludes` | `True` |
| `DROPS_PARKED_ON_STOP` | `_description_may_not_stop`, `_note_swap` | `False` |
| state word (second item of the `_LEASE_HOLDERS()` tuple) | `_free_the_card` (`state == "stopping"`), `_why_a_peer_may_not_swap`, `_busy_for_the_card(state)`, the UI | `starting` / `running` / `draining`, as in section 1.1 |
| `_draining` (plain attribute) | `_free_the_card` (3038/3068: a holder stopping is waited for like a busy one), `_why_a_peer_may_not_swap`, `_why_no_card_borrow` | True from `_begin_swap_drain` until `give_up_for` has ended, whatever its outcome; False otherwise |
| `_swap_for` (plain attribute) | `_draining_error` wording, `_stop_for_the_swap` (sets it to None after the stop) | the contender's alias during a mark, else None |
| `_busy_for_the_card(state, but_for=None)` | `_free_the_card` (3057–3058), `_why_a_peer_may_not_swap` (2647) | True iff `state in ("starting", "draining")` or an open use `u` exists with `u.hold is not but_for` and `u.arrival` lower than the acting waiter's arrival. Each open use carries `what`, `since`, `arrival` (from `_CARD_ARRIVAL` at open) and `hold` (the `_CARD_HOLD` value at open, or None). There is no `process` field and none is read |
| `_begin_swap_drain(new)` | `_free_the_card` (3072), in the stretch of the busy test | sync: `_swap_for = new.alias`, `_draining = True`, `state = "draining"`; from here no new use is admitted (they wait in `_LEASE_LINE`), and `acquire_blocking` waits likewise |
| `_end_swap_drain()` | `_stop_for_the_swap` on an exception; the lease itself on `refused` | sync: `_swap_for = None`, `_draining = False`, back to `running`; waiting uses are admitted only by L5a (the contender is still earlier) |
| `async _parked_conversations()` | today `_stop_for_the_swap` (3123); after S2 item 5 not called for a lease | returns 0 |
| `async give_up_for(contender, bound)` | `_stop_for_the_swap` for a lease (S2 item 5), `let_go_by_hand` | runs `on_yield`; returns its outcome; releases on `let_go` and `nothing_to_give`; ends the drain on `refused` and `could_not_let_go` without releasing; never raises for either |
| `stop_hint` | `_card_held_error` (S2 item 7) | the client's own words |
| `who_uses()` | `_who_stands_on` (S2 item 6) | the open uses' labels |
| `_proc`, `props`, `port`, `_start_lock`, `_successor`, `_in_flight`, `_will_not_reopen()`, `_check_a_wait_can_end()` | supervisors only (inside `_live_claims()` or on the waiting `self`) | not defined on a lease, and must never be reached for one |

#### 2.3 Unify or replace `_CARD_CLAIMS`?

**Unify, not replace.**

- `_CARD_CLAIMS` stays exactly as it is: the registry of supervised llama.cpp children,
  which ~11 tests and `conftest.py` poke directly, written by `_claim_card` and `stop()`
  and pruned by `stop()`, `_live_claims()` and `_release_card_unless_live()`.
- `_CARD_LEASES` (in `card_lease.py`) is a second registry. `_live_holders()` is the one
  union every "who holds the card" reader uses.
- With no lease registered, `_live_holders() == _live_claims()`, so every existing card
  test sees identical behaviour. Pin that with a test.
- A later, separate refactor could make supervisors themselves lease clients (`CardLease`
  per alias). Not needed now. That refactor would touch the 5,555-line file, which other
  agents are editing.

`conftest.py:_no_card_claims_cross_tests` (77–92) also clears `card_lease._CARD_LEASES`,
`_LEASE_LINE` and the yield-task set.

**Test files at risk.** Behaviour should not change, but these import the internals
touched in 2.2:

- **Card machinery:**
  - `test_one_local_engine_holds_the_card.py`
  - `test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced.py`
  - `test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card.py`
  - `test_a_peer_may_swap_an_idle_card_when_the_owner_allows_it.py`
  - `test_the_card_queue_is_reported_and_a_background_call_neither_swaps_nor_queues.py`
  - `test_a_request_holds_the_card_from_its_first_round_to_its_last.py`
  - `test_a_turns_description_may_borrow_the_card_and_the_turn_gets_its_alias_back.py`
  - `test_a_turns_pre_turn_check_and_its_loop_are_one_request_on_the_card.py`
  - `test_a_persons_group_image_waits_for_the_card_briefly.py`
  - `test_strata_is_offered_to_peers_on_the_owners_terms.py`
  - `test_a_conversation_keeps_the_slot_until_its_turn_ends.py`
- **Start and pin paths:**
  - `test_a_cancelled_start_leaves_no_child_behind.py`
  - `test_a_cancelled_start_on_a_real_port_leaves_one_child.py`
  - `test_a_client_that_leaves_a_start_does_not_take_it_from_the_next.py`
  - `test_a_missing_engine_pin_is_offered_for_download_and_never_fetched_inside_a_start.py`
  - `test_a_missing_strata_pin_is_offered_for_download_with_its_progress.py`
  - `test_a_new_pin_that_fails_its_first_start_falls_back_once_to_the_retained_pin.py`
  - `test_a_strata_alias_can_choose_its_engine_backend.py`
  - `test_a_strata_alias_without_an_install_dir_runs_dpcs_pinned_install.py`
- **Save and stop:**
  `test_a_save_that_fails_or_times_out_never_stops_the_stop.py`,
  `test_a_swap_saves_the_owners_live_conversation_before_the_stop.py`,
  `test_a_drain_saves_nothing_while_a_call_is_in_flight_or_the_backend_stops.py`
- **Readers of `card_holders` (keys added, none removed):**
  `test_the_eval_runs_a_strata_alias_and_names_the_engine_that_holds_the_card.py`,
  `test_the_campaign_gate_says_what_holds_the_card.py`,
  `test_the_gaia_report_keeps_the_span_it_graded_and_its_own_trace.py`
- **Music (change on purpose in S4):**
  - `test_a_generate_from_text_job_runs_end_to_end_on_the_supervised_engine.py`: lines 152
    and 180 inject `holders=` through `_acestep_helpers.Rig(card_holders=...)`
  - `_yue2_helpers.py`,
    `test_a_yue2_job_runs_one_request_per_variant_and_is_the_default_engine.py`
  - `test_only_one_music_engine_is_on_the_card_at_a_time.py`
  - `test_the_acestep_server_is_started_watched_and_stopped_by_dpc.py`,
    `test_the_yue2_server_is_started_watched_and_stopped_by_dpc.py`
  - `test_the_music_engine_commands_report_start_and_stop_the_acestep_server.py`
  - `test_a_music_job_never_hangs_in_queued_and_its_files_are_named_for_the_style.py`,
    plus the provenance and loudness tests that use the Rig
- **Whisper:**
  - `test_the_whisper_model_unloads_safely_and_when_idle.py`
  - `test_a_providers_reload_keeps_whispers_running_transcriptions_and_unloads.py`
  - `test_a_transcribe_run_unloads_the_whisper_model_it_loaded.py`
  - `test_a_transcription_takes_its_language_and_task_per_call.py`
  - `test_transcribe_tool.py`, `test_characterization_voice.py`
  - `test_a_peer_is_the_last_step_of_the_transcription_chain.py`
  - `test_transcribe_turns_a_tracks_vocals_into_lyrics_only_when_asked.py`
  - **Q2, answered** (Zcode #41, re-checked): no test resolves a real CUDA device. The
    first four build providers with `"device": "cpu"`; `test_transcribe_tool.py`,
    `test_characterization_voice.py` and the peer test use fakes; the `"cuda"` in the
    lyrics test is a schema string, not a device. **But** two files build
    `"device": "mlx"` providers (`test_a_transcribe_run_unloads_the_whisper_model_it_loaded.py:294,305`,
    and the language test) and set `model_loaded = True` by hand. Under section 3.1 an
    explicit `mlx` device builds a lease, so those tests get a lease that is loaded but
    never granted. That must stay harmless: `use()` grants on an empty card, and
    `release()` of a lease that is not granted does nothing. Conftest clears the registry.

#### 2.4 What swapping Strata out for a lease drops, and who is told

This is the cost of 2.2 item 12: a lease contender swaps an idle supervisor out (music, Mike
#70; a long dictation, item 1a, decided 2026-11-23). It does not apply to a lease that yields
(`DROPS_PARKED_ON_STOP` is False for a lease), nor to a supervisor start that waits for a
lease, nor with `swap_when_idle` off, when nothing is stopped.

- **What is lost.** Strata keeps the conversations of agents that are not live in its own RAM
  conversation cache (`conversation_cache` of the launcher's `/metrics`: `slots`, `parked`,
  `bytes`; two slots on Mike's aliases, ADR-044 line 1873), and a stop drops every parked entry
  (`DROPS_PARKED_ON_STOP = True`, "A stop drops every conversation the launcher parked in host
  RAM", `strata_supervisor.py:1045-1047`). Nothing else holds them: a KV snapshot save writes
  only the live slot, never a parked conversation (ADR-044, amendment of 2026-11-18, Q2, line
  1877). What is lost is **the engine's cache of those conversations, not the conversations**:
  their messages are DPC's own history and go to the engine again with the next call (a
  parked conversation "turns a cold prefill of a long agent conversation into a short
  restore", ADR-044 line 258, so what a park saves is the prefill). So the loss is time. The measured cost is a cold read of the prompt, 8.4 s at 30K tokens and 23.2 s
  at 90K, against 0.05–0.14 s for a park's return (ADR-044 line 1878). The live conversation is
  saved by the stop (`drain` → `_save_live_conversation`, `llama_server_supervisor.py:5358-5400`,
  4379) when its folder is on and `_why_not_this_record` (4353) lets it; ADR-044 measured such a
  stop save on Strata (line 1883: 183,128 tokens, 28,625 ms). Only an **idle** Strata is swapped
  (`_busy_for_the_card`, 2668-2679: no hold, no call in flight, no caller inside its window), so
  no turn is cut; the agents hit are those between turns.
- **What the agent pays on its next turn.** The engine must start again (a Strata load is
  bounded by `start_timeout_s`, 600 s by default, ADR-044 line 1815), and while the music job
  or the dictation holds the card, the start waits behind the busy lease for `swap_wait_s`
  (300 s by default, `settings.py:147`; 1800 s in this machine's `config.ini`) and is refused
  at its end naming the holder (L3). Then the prompt is read cold, as above. This is the
  "agent's next turn pays the reload" of the Consequences. **The accepted cost is part
  measured, part bounded** (Johnny #14 obj. 4): the cold read (8.4–23.2 s) is measured; the
  Strata restart is not, only bounded by `start_timeout_s`, 600 s. Both are learned in
  operation (Mike #11).
- **With Strata on the card, nothing else fits** (Forge #15 §5): nvidia-smi read 1100 MiB free
  of 32623 with Strata resident (2026-11-21 17:53), so Whisper (≈4.4 GB, its measured worst-case peak) and ComfyUI (≈25 GB)
  run only after a swap. That is Mike #68 working as decided.
- **On Windows, `free` is not a wall** (Forge #21 §1; a property of the machine, no redesign,
  Zcode #22). With Strata resident `nvidia-smi` read 879–1100 MiB free while
  `torch.cuda.mem_get_info()` read about 30 GB, and Forge allocated 10 GiB at "67 MiB free"
  with no failure or slowdown (WDDM). So L11's live veto on `free` refuses whenever an engine is
  resident, which agrees with Mike #68; whether to keep it on WDDM is learned in operation.
- **Who is told.**
  - **The owner:** by `_note_swap` (3141-3168): one INFO line, "swapped 'X' out for 'Y'" with
    "; its parked conversations are dropped" whenever the engine's count was not zero, and a
    count that could not be read within 3 s (`strata_supervisor.py:1049-1058`) is said as if
    some went (`dropped = DROPS_PARKED_ON_STOP and parked != 0`, 3151); one `notice_toast`
    "Local engine swapped", shown for 10 s (3165, `_toast` 440-450); `parked_dropped` in
    `last_swap`. No screen reads `last_swap` (Backlog cards, "Changes"), so the toast is the
    only word on screen, and it gives no count and names no agent.
  - **The agent: not told.** Nothing carries `_LAST_SWAP` or the dropped count into an agent's
    context (grep of `dpc_agent/` for `last_swap`, `_LAST_SWAP`, `parked_dropped` and "parked
    conversations": no hit, CC 2026-11-21). Its next turn is slow and says nothing about why.
- **What the lease does about it.**
  1. **It is accepted**, as Mike decided at #70 ("if the queue is empty and the server just
     idles, unload it and take the card; the principle is simple"). This record adds no new
     protection. It differs on purpose from the one guard that does protect parks:
     `_description_may_not_stop` (2554-2558, Mike 2026-11-04), which stops a *description*
     from stopping an engine that drops what it parked. That guard is about an auxiliary call
     inside an owner's turn, stays as it is, and is not extended to music or dictation.
  2. **It reads the count first.** `swap_out_for_a_lease` awaits `holder._parked_conversations()`
     before `stop_by_hand`, as `_stop_for_the_swap` does for a supervisor contender (3123).
     S2 item 5 skips that call only when the *holder* is a lease.
  3. **It says engine, holder and contender.** The line and toast name both aliases, as
     `_note_swap` already does; the contender's `alias` and `ENGINE` (written to `last_swap`
     as `to` and `to_engine`) are what 2.2.1 gives a lease. Proposed, no decision needed: the
     INFO line adds "N parked" when the count is known, because the toast's "some" hides the
     difference between one conversation and five.
  4. **The gates are the whole protection**: the swap on, the holder idle by rule 1, the
     contender first by L5a, nothing waiting behind it. The CPU twin avoids the loss for
     files up to 5 minutes (L12), which is why item 1a puts L12 first.
  5. **The agent is not told.** Decided, Mike #11, item 11: "why would it need to know?"

### 3. Clients, in order

#### 3.1 Whisper (in-process; holder and contender)

- **Building the lease.** Build `self._card_lease` only when the resolved device is not
  CPU.
  - Explicit `device` in `cuda`/`mps`/`mlx`: use it as is, no torch import.
  - `auto`: resolve once in a thread with the existing `_detect_device`.
  - CPU or no accelerator (CI): no lease, today's path.
  - Holder `whisper:<alias>`, engine `"whisper"`, kind `transcription`,
    `yield_cost="cheap"`.
  - `vram_mib` comes from `model_sizes.model_size_bytes(model, revision)`
    (`providers/model_sizes.py:74`), converted to MiB, plus a **new** overhead constant
    (`_WHISPER_OVERHEAD_MIB`, L11; `_DESKTOP_RESERVE_MIB` means something else), else None.
- **`transcribe(..., purpose: Optional[str] = None)`.** `purpose` is per call, the same
  precedent as language and task in b9725990.
  - Wrap the **whole** existing body in `async with self._card_lease.use(purpose or
    "transcription", wait_s=..., on_wait=...)`.
  - The lease use is taken **before** `_load_lock`. The lock order is: lease, then
    `_load_lock`, never the reverse. `yield_card` takes `_load_lock`.
  - Doors pass `purpose` and their bound (L4): "dictation" (`service.py:4797`), "music
    Transcribe" (`music/commands.py:324`), the agent tool (`tools/transcribe.py:172`, with
    `on_wait` calling `ctx.emit_progress("waiting for the card: <holder> holds it…")`),
    "peer <node>" (`p2p_coordinator.py:1398`, which **must now wrap the call in
    `serving_a_peer(peer_id)`**).
- **The yield, `yield_card(why) -> YieldOutcome`** (new; replaces the first draft's
  `unload_if_idle` as `on_yield`, whose `False` means two different things). On the current
  owner, under `_load_lock`: `not model_loaded` → `nothing_to_give`;
  `_active_transcriptions > 0` → `refused`; else `_unload_locked(f"card_yield: {why}")`,
  then `let_go` when `model_loaded` is now False, and `could_not_let_go` when it is still
  True (the unload's thread never ran: `_finish_thread` outcome 2 or 3 in section 0, which
  `_unload_locked` raises; `yield_card` catches it, logs a WARNING and keeps the model and
  the grant). `unload_if_idle` keeps its contract for its existing callers. `on_yield =
  lambda why: self._current_owner().yield_card(why)`.
- **Warm means granted with 0 uses.** A warm grant whose model is gone (an idle unload
  that raced the grant, a load that failed) answers `nothing_to_give` and is released at
  once (Ark #45, finding 2), so a waiting music job is never kept behind it.
- **Release on every unload that ran, and only then** (Ark #60 §3, #61 §2a). Releasing the
  lease and unloading the model are not the same thing. `_unload_locked` releases in a
  `finally` around `_finish_thread`, whichever trigger fired (idle timer, deferred,
  `release_if_loaded_here`, switch_provider, auto_transcribe_disabled, shutdown, a yield),
  **on one condition: `not self.model_loaded`** when it gets there. Case by case, from the
  outcomes in section 0:
  - the thread ran and freed the model → `model_loaded` False → release;
  - the thread ran and the release itself raised (`_unload_model` returns False, drops the
    reference and sets `model_loaded = False`) → release, with the existing ERROR that
    VRAM may not be free;
  - the caller was cancelled while the thread ran → `_finish_thread` waits for the thread
    to end; `model_loaded` False → release, then the cancel is re-raised;
  - the worker was cancelled before its thread started (`d80d76f7`), or the executor
    refused it (the escaping exception) → `fn` never ran, `model_loaded` stays True and
    the pipeline is intact → **no release**: the model is still in VRAM and owned, and a
    release would let the next holder load over it;
  - a thread that never ends → `_finish_thread` never returns, so no release either; the
    lock and the grant stay until the shutdown bound.
  The condition reads state, not the path taken, so a fifth outcome added to
  `_finish_thread` later cannot release a loaded model by accident.
- **A load that fell back to the CPU releases the lease** (Ark #13 new #3). When
  `model.to(device)` fails with a CUDA or NVIDIA error, `_load_model` sets `device = "cpu"`
  and goes on (`whisper_provider.py:282-291`); the model then lives in host RAM, so the
  lease is released as the load returns, and the call runs without a card hold. The same
  branch also switches the dtype to float32 (`:281-291`) and so is a **CPU route that L12
  never approved**: it must pass L12's RAM check (the twin's, with its own margin) or the call
  is refused with the reason (Ark #30).
- **Preload.** `ensure_model_loaded()` calls `lease.acquire("preload", background=True)`.
  On `LeaseSkipped` it logs "Whisper preload skipped: the card is held by X" and stays
  lazy. A preload never swaps anything out.
- **Hand-over.** `hand_over_to(new)` sets `new._card_lease = self._card_lease;
  lease.rebind(new.yield_card)` and never releases. `retire()` with no successor relies on
  the unload path's release.
- **Shutdown.** `shutdown_unload` releases in its finally block.
- **Sharing with busy music (L11, decision 6).** `shares_with` runs L11's arithmetic and
  returns `fits` with the music holder's alias, `does_not_fit` or `unknown`. **As built (S3e):**
  `shares_with` is a predicate on the holder (Whisper's: a lease of kind `music`), and the
  arithmetic is the lease module's own (`card_lease.l11_arithmetic`, run by `_try_beside` and
  `route_view`), since it reads every resident and the card, which no client sees; see Review
  notes, "S3e build".
- **Queueing for a person** (decision 6; conditions from Ark #53 and Zcode #52). When the
  outcome is not `fits`, the owner's dictation, received voice messages, Telegram voice and
  the agent tool wait in `_LEASE_LINE`; the wait shows as "transcription queued behind
  yue2.cpp (song, ≈6 min | time unknown)" from L10 and is cancellable. Four conditions:
  1. **The line sits before `_load_lock`.** The lease use is taken before `_load_lock` (the
     lock order above), so a waiting dictation holds no lock: a peer's request is refused
     at once instead of after waiting for the lock, and the player's Transcribe and the
     agent tool take their own places in the line instead of queueing on a lock.
  2. **A card refusal ends the cascade over GPU aliases, and one call keeps one place**
     (reconciled with the CPU twin, Ark #64 §4). In the received-voice loop
     (`service.py:5152-5160`), a `CardQueueRefusal` (any `Lease*` refusal) is caught before
     the generic `except Exception: continue` and stops the walk over **further GPU
     aliases** (any alias whose provider holds a card lease). Otherwise each next GPU alias
     would queue again on the same busy card (up to N × the bound; four names by default)
     and load another model. The walk **may move on to a CPU alias**: the CPU twin, when
     L12 routed there (`RoutedToCpu`, below), or a configured `device: cpu` alias, when L12
     passes for it; and a cloud alias later in the list (`openai`) does not touch the card
     and may still be tried (proposal). The wait happens at most once, inside the first GPU
     alias's `transcribe`.
  3. **The peer check runs again after the wait.** Step 8 (`service.py:5141-5145`) checks
     "has the peer already sent the transcript?" before the call, and the wait now happens
     inside it. The received-voice path passes a `check` to the lease that raises when
     `transfer_id in self._voice_transcriptions`, so the local attempt is dropped the moment
     the peer's text arrives, before any load. Step 9a stays as the last guard.
  4. **Every caller has a deadline, and the queue's is shorter** (L4): `wait_s = min(caller's
     remaining − margin, dictation bound)`. S3 integration work:
     - `service.transcribe_audio` gets an explicit bound (today none), which also bounds
       both Telegram call sites (`telegram_coordinator.py:509`,
       `agent_telegram_bridge.py:1674`, today unbounded);
     - the UI's `transcribe_audio` command timeout, 240 s hard-coded at
       `ui/src/lib/coreService.ts:1492`, is raised to the bound plus the margin, or replaced
       by a progress event that keeps the command open;
     - the agent tool passes its own remaining time (the 1800 s tool timeout,
       `tools/transcribe.py:293`, and the turn hold's deadline);
     - `[voice_transcription] timeout_seconds` (240) is **not** one of these: it only decides
       when a receiver stops waiting for a sender's transcript and transcribes itself
       (`service.py:5102`). A sender queued behind music for longer than that means both
       sides transcribe, each on its own node; condition 3 and step 9a keep the text single.
- **The CPU route (L12, Mike #62), in S3: a separate provider object, the CPU twin** (Ark
  #64 §4). Not a second pipeline inside the same `LocalWhisperProvider`: there,
  `model_loaded` means "the pipeline exists", and the hand-over machinery
  (`_copy_state_to`, 685; `_settle_finished_load`, 692; `hand_over_to`, 700) moves **one**
  model state, the one-owner invariant closed from `b796af4a` to `d80d76f7`. A second
  pipeline in the same object would make `model_loaded` ambiguous and break it.
  - **What it is:** an ordinary `local_whisper` provider (`llm_manager.py:62`) with
    `device: "cpu"`, the same model and revision as its GPU alias, created by the providers
    manager beside each GPU Whisper alias as `<alias>@cpu` (not written to
    `providers.json`), or an alias the owner configures with `device: cpu`. It has its own
    `model_loaded`, `_load_lock`, idle timer, hand-over and per-call `language`/`task`
    (the precedent of `b9725990`); its result says `device: "cpu"`.
  - **No card lease:** a CPU device builds none (section 3.1, first bullet), so L11, the
    lines and "who holds the card" do not apply to it, and its load and unload never touch
    the lease. Its RAM is guarded by L12's RAM check, with the figure in L12's table
    (unverified). On
    macOS, where the GPU route is MLX or MPS, the twin is the transformers CPU pipeline
    **[unverified on macOS]**.
  - **Selection:** the GPU alias's `transcribe` runs L12 at entry. When L12 says CPU it
    raises `RoutedToCpu(twin_alias)` (not a refusal; nothing waited), and the caller's
    selector calls the twin with the same arguments. One helper,
    `transcribe_with_route(provider, ...)`, is the selector for every door:
    `service.transcribe_audio` (the owner's dictation, voice messages, both Telegram
    paths), the received-voice cascade, the agent tool and music Transcribe. A configured
    `device: cpu` alias later in `provider_priority` is reached the same way.
  - **Residency:** its own `idle_unload_s`, short (proposed 60 s), never loaded
    speculatively (L12, "Residency"; Mike #71).
  - **Peers:** the twin serves a peer only when the owner turns on the optional setting
    beside the device selector (Mike #71; S5).
  - **The measured figures** (cold, warm by length) start from L12's table and are replaced
    per machine after every twin run (L12, "Starting figures").
- **What was S9 is in the first slice** (Mike #68): Whisper waits for supervisors, L12
  applies against them, the self-wait case goes to the CPU twin or is refused, and
  `_stands_before_the_line` uses one order (S2). No second shared cell is planned: Mike
  ruled out Whisper beside a local LLM.

#### 3.2 Music engines (supervised children; holder and contender)

- **`SupervisedEngine.__init__`:** `self.card_lease = CardLease(f"music:{self.KEY}",
  kind="music", engine=self.ENGINE, display=self.DISPLAY,
  vram_mib=self.VRAM_ESTIMATE_MIB, yield_cost="engine", on_yield=self._yield_card,
  waits_for_engines=True)`.
  - Estimates: YuE2 Q8 peaks 5.2–5.6 GB, acestep ~9.3 GB (`engines.py:5-6`).
- **Lock order: the lease first, then `_the_lock`** (Ark #44, finding 1). The first draft
  granted the lease inside `_the_lock` (`engine_base.py:444-448`), which broke this ADR's
  own rule that no lease is acquired under a client lock, and made `stop()` (591–595, also
  under `_the_lock`) unreachable for a yield while a start held the lock. Now:
  - the runner acquires through `use()` before it calls `start()`, so `start()` finds the
    lease already granted and never waits under `_the_lock`;
  - `start_music_engine` by hand asks the card **before** `start()` and before any lock, through
    `_card_for_a_start("start by hand", yields_idle=True)` (`acquire(..., wait_s=0, yields_idle=True)`,
    `music/engine_base.py:484`). It is refused at once, naming the holder (`card_busy`), when a holder is busy
    or has a queue behind it. When every holder in the way is idle with no queue, and the lease may ask or
    swap it (an idle Whisper model, the other music engine, a local LLM engine with `swap_when_idle` on), the
    request waits in the lease line for those holders only and takes the same ask and swap path a queued
    job takes, bounded by the owner's `swap_wait_s` and at least `BY_HAND_YIELD_S` (30 s). Mike, 2026-11-24
    (group chat): anything idle with no queue may be unloaded for a start by hand. The other music engine is
    checked first (`MusicEngines.refuse_while_switching`: a busy one refuses with `engine_busy`) but is stopped
    only after the card is granted, so a refused start stops nothing;
  - `start()` itself **acquires, and never waits**: when the engine holds no grant it asks with `wait_s=0`
    (`_card_for_a_start()`, `engine_base.py:473-482`), before `_the_lock`, so a direct start (the engine
    tests, a caller that forgot) is refused at once naming the holder (`card_busy`) instead of loading beside
    it; with a grant already held (a job's, from `in_use`) it asks nothing. `_start_child()`, under the lock,
    acquires nothing. Only the start by hand passes `yields_idle`: `start()` alone never asks a holder to let go.
- **`_yield_card(why) -> YieldOutcome`:** child not alive → `nothing_to_give`; `_the_lock`
  held (a start or a stop is under way) or `self.busy` → `refused`, without calling
  `stop()`; else `await self.stop("swap")` → `let_go`. A start in progress is also busy for
  the card (`state == "starting"`, L1), so a contender never marks an engine that is
  booting.
- **Released on every end of the child:** in `stop()` (`ui_stop`, `idle_unload`, `swap`,
  `failed_start`), in `_watch` (exited on its own), and in `shutdown()`. A child that exits
  under an open use (a job) releases the grant too, because its memory is gone; the use
  stays open, and the job's next engine start acquires again, with a fresh arrival, while
  the job is `running`.
- **Busy: one source** (Ark #45, finding 3). `in_use()` enters `card_lease.use(what)` and
  counts `_busy` in the same place; the lease's busy is the authority for the card.
- **Runner.** `MusicJobRunner.run_job` replaces `_wait_for_card(job_id)` with `async with
  engine.card_lease.use(f"music job {job_id}", wait_s=None, check=lambda:
  self._check(job_id), on_wait=self._show_card_wait(job_id))` around `start_job`, `in_use`
  and the production steps.
  - `on_wait(estimate)` calls `set_waiting_for_card(job_id, holders)` with the blocking
    holders, so the existing "waiting for the card: X holds it" text and the Stop X button
    stay.
  - **Two gates accept `running`, not one** (Ark #44 finding 4; Ark #60 §4):
    - `jobs.py`: `set_waiting_for_card` and `clear_waiting_for_card` accept a job in
      `running` as well as `queued` (today 621 refuses anything but `queued`). Without it, a
      wait after `start_job` (the re-acquire above) raises `illegal_transition`, and
      `runner.py:671-676` re-raises it, failing the job.
    - `runner.py`: the wait loop's own gate. `_wait_for_card` (656) returns False as soon
      as `job["state"] != js.QUEUED` (661–662), and False means "the job was cancelled"
      (its docstring; `run_job` returns at 591 without starting anything). A mid-job wait
      through that gate would treat a running job as cancelled and drop it, leaving it
      `running` with no worker. The wait that replaces it (`on_wait` inside
      `card_lease.use`) ends only on `self._check(job_id)`, which raises for a job that is
      really cancelled (`_cancelled`, 531), and never on `state != queued`.
    - The frontend follows: `musicCreate.ts:145` shows holders for a `running` job with a
      `card_wait` too.
  - A busy lease holder (Whisper) is waited for. An idle one is yielded (unloaded).
  - A busy supervisor holder is waited for. **An idle one, with nothing waiting, is swapped
    out** (Mike #70, amending SPEC decision 32: "if the queue is empty and the server just
    idles, unload it and take the card; the principle is simple"). It is the owner-start
    rule, `swap_out_for_a_lease` (2.2 item 12): **gated by `swap_when_idle`**, the same as
    an owner's start today; the owner's live conversation is saved before the stop; a
    stopped Strata drops its parked conversations and the swap note says so. With the swap
    off, the job waits for a person's Stop X, as before. The SPEC file
    (`ai-studio/experiments/music-player/SPEC.md`) needs the decision-32 amendment; this
    record does not edit it.
  - A failed read of the holders is no longer "free": the lease registry is in-process and
    has no read that can fail.
- `MusicEngines.exclusive` stays. Its stops of the other idle music engine release that
  engine's lease, so the two agree. Folding it into the lease is a later cleanup.

#### 3.3 Minimal first slice for the Done-when

"A transcription during a running YuE2 job waits and names the holder" needs S1 (lease
core) + S3 (Whisper) + S4 (music holds leases). S2 (supervisors) is not required for it.
**S3 and S4 land together** (one branch, separate commits): `default_card_holders`
(`runner.py:156`) reads `card_holders()`, so with S3 alone the old `_wait_for_card` would see
a warm Whisper as a holder and keep a music job waiting behind it, with a "Stop X" button
that `stop_local_engine` cannot honour before S2 item 8.

Flow on a card where it does not fit (or where L11's answer is `unknown`):

1. YuE2 job runs; `music:yue2` is granted and busy.
2. `transcribe` calls `use()`.
3. L11: `does_not_fit` or `unknown`. L12: the running job's ETA is unknown, so the GPU
   wait is long; a 20 s voice message whose CPU estimate (cold or warm, as measured) is
   under `cpu_max_s` goes to the CPU twin and is transcribed there (no lease, no wait; the
   flow ends here). A 40-minute recording goes on:
   the Whisper lease is excluded and enters `_LEASE_LINE`, before `_load_lock`.
4. Log line: `card: whisper[whisper-large-v3-turbo] waits for yue2.cpp (busy: music job
   01J…); wait about 6 min (estimate), up to 600 s`.
5. `card_queue()` shows the Whisper row with `wait.behind == ["yue2.cpp (music job …)"]`.
6. The door's `on_wait` names the holder to the person: "transcription queued behind
   yue2.cpp (song, ≈6 min)", with a cancel.
7. When the job ends, the engine is idle.
8. The Whisper waiter yields it (`stop("swap")`), then is granted, loads and transcribes.
9. On timeout, `LeaseWaitedTooLong` names "yue2.cpp, busy with music job …".

On the owner's 32 GB card with no local LLM resident, L11 should find the fit at step 3 (declared: `whisper_peak` 4447 MiB, derived, not measured; to be re-measured on v0.2.20 before it becomes a test constant (Forge #12, Zcode #22-5); the 1547 of weights from `model_size_bytes` plus
2900, + 5600 MiB YuE2 + 2048 reserve = 12 095 MiB against `total` 32 623; live: far more free
than Whisper's peak plus the music growth left) and Whisper loads beside at once, as today,
now named in `card_holders()` and the log **[unverified: the YuE2 figure, 5.6 GB read as 5600
MiB, is a comment in `music/engines.py:5-6`; no `vram_mib` is declared in code yet]**. Since S4 it is
declared: `Yue2Server.VRAM_ESTIMATE_MIB` = 5600, the trial table's whole-card peaks being 5222–5570 MiB
(already MiB, `nvidia-smi` polled every 200 ms on an empty card); acestep.cpp's 9300.

**The first slice is now S1+S2+S3+S4** (Mike #68). Whisper must not load beside a local
LLM, so supervisors must honour leases (S2) and Whisper must wait for supervisors in the
same slice; S2 cannot trail S3. What that adds to the first slice:

- S2 in full (2.2 items 1–13), in the 5,555-line supervisor file other agents edit;
- Whisper excluded by and waiting for supervisors, with L12 and the CPU twin as the escape;
- the self-wait case of an agent's own transcription tool;
- music swapping an idle supervisor (Mike #70).

The earlier open point "Whisper no longer loads beside a model: refuse / wait / load
anyway" is answered: it waits, or the CPU twin takes a file up to 5 minutes.

#### 3.4 Embedder, then GLiNER (S6, S7)

- **Device mode.** New value `lease` for `[memory] embedding_device` and
  `[knowledge_graph] gliner_device`. Proposal: `auto` comes to mean `lease` on CUDA. This
  is a further decision for Mike, listed under Open for Mike as not numbered (no default
  is proposed beyond the one stated).
- **Embedder.**
  - `EmbeddingProvider` keeps a CPU model, plus a GPU model loaded only inside
    `lease.use_blocking("index rebuild"|"memory search", background=...)`.
  - Bulk rebuilds (`agent_manager._sync_index` when `rebuild_decision` says a full rebuild
    is pending) take a foreground lease and wait. Steady-state queries take
    `background=True` and run on CPU when skipped. That is exactly the auto rule in the
    backlog card at 7507: GPU+fp16 for a rebuild, CPU+bf16 for the steady state.
  - `on_yield` sets a flag that `_encode_within_memory` checks between batches. The thread
    moves to CPU, frees the GPU copy (`del`, `gc.collect()`, `torch.cuda.empty_cache()`;
    today's `unload()` does none of that), then calls `release()`.
- **GLiNER** first needs an unload. It has none today (`knowledge_graph.py:123-165`). The
  sleep pass takes `background=False` with a short `wait_s`; when skipped it runs on CPU
  (measured cost per sleep: 10.9 s CPU against 2.7 s CUDA).
- **Risk.** fp16-GPU and bf16-CPU vectors differ slightly (cosine 0.998–1.000 measured).
  The cache key has no device in it. That is acceptable per the measurement, and it is
  stated here on purpose; a card to put the device in the key is filed (new card (f)).

#### 3.5 An agent's own turn that needs a transcription (Ark #82 §2, CC #84; Mike #11)

Two rules of Mike's meet here: Whisper never loads beside llama-server or Strata (#68), and
the CPU twin may take a file up to 5 minutes even while Strata is busy (#71). Mike's answer to
item 12 was "look at our flow". The flow, read on `dev` `84425302`:

| Path | Caller | Inside a turn on a local alias? | Typical audio |
|---|---|---|---|
| Dictation (`ChatPanel.svelte:1245-1263` → `transcribe_audio`; a recording in a 1:1 AI chat goes the same way, 1221) | the person; the words go into the draft | no, before any turn | up to 300 s (the recorder's cap, `[voice_messages] max_duration_seconds`, `settings.py:345`) |
| A received P2P or group voice message (`_maybe_transcribe_voice_message`, `service.py:5025`; the cascade at 5152) | the service, when the transfer completes | no | voice notes, up to 300 s |
| Telegram voice, to a chat or to an agent (`telegram_coordinator.py:509`; `agent_telegram_bridge.py:1627`, transcribed before the burst runs the turn) | the service | no, the turn starts with the text in | voice notes |
| A peer's request (`p2p_coordinator.py:1342`) | another node's voice fallback or `remote:` alias | no | the guest's voice notes; it waits 120 s |
| The music player's lyrics Transcribe (`music/commands.py:283-330`, built) | the person's click | no | a track, usually minutes |
| The agent tool `transcribe_audio_file` (`tools/transcribe.py:121-215`): a recording or file sent into an agent chat reaches the agent as a marker (`context.py:980`, `service.py:545`) and the agent may transcribe it | the agent, in its turn | **yes, when the agent runs on a local alias** | a recording up to 300 s; a file (mp3, podcast, song) of any length (the tool's doc speaks of 30 minutes) |
| The music player, planned: "Transcribe (agent)" and the agent's "ears" (SPEC flows 14 and 24, [P]) | the agent tool above | yes, the same path | a hum, a song or a clip of one |

So the in-turn path is real and narrow: one tool, off by default (`transcribe.py:294`), turned
on for seven agents in this machine's firewall, its use not countable from the logs (it logs
nothing on success). Every other path runs outside any turn and takes L12 as written. The
music player is a caller twice: by a person's click (L4's music Transcribe door) and, once
SPEC flows 14 and 24 are built, through the agent tool.

**Facts from the code.** A turn stands on its local alias from the pre-turn check to the
loop's last round, tool rounds included: one `accepted_request` hold
(`dpc_agent/agent.py:728-735`, `card_held_for`, `llama_server_supervisor.py:844-867`; busy by
`_busy_for_the_card`, 2668-2679); a cloud alias stands on nothing (`_CardHold.alias` None,
701-703). The tool runs inside the turn with the hold carried into its thread
(`dpc_agent/loop.py:1644-1656`), calls `provider.transcribe` directly
(`tools/transcribe.py:172`, 1800 s timeout at 293), and an exception comes back to the model
as text, "Transcription failed: <message>" (174-175), so a refusal's words are what the model
reads. The turn keeps the card through its tools (L6; ADR-044 line 1604, Mike's call).

**The rule** (the simplest that holds #68, #71 and #11 together):

| The turn runs on | The file | What the tool does |
|---|---|---|
| a cloud alias | any | Nothing stands on the card, so there is no self-wait: L12 as for any caller (GPU if free or `fits`; the CPU twin; else the line, bounded by the tool timeout less the margin, with `emit_progress` naming the holder). |
| a local alias | up to 300 s | The CPU twin at once. L12's time comparison is skipped: the only GPU route is behind this very turn, so there is nothing to compare, and the 300 s cap is the test (Mike #71). Only L12's RAM check stays. No lease, no wait. |
| a local alias | over 300 s, or the twin cannot run (too little RAM, its load failed) | **Refused at once**, `LeaseWouldWaitOnItself`, naming the alias and the reason. Never queued: the line's holder would be this turn. |

**Why there is no deadlock, and who yields.**

1. The only cycle is a request that waits for a holder that waits for it. It is cut at the
   door: `acquire` checks `_CARD_HOLD` before it takes a place (`_this_request_holds`, 2.2 item
   4; L3), so the refusal comes in under half a second, not at a deadline.
2. **The transcription yields, never the turn.** The turn is busy until it ends and the tool is
   inside it; no round of the card asks a busy holder to give way (L6).
3. **The turn's next round never waits for its own transcription.** GPU Whisper can be granted
   only under a cloud-alias turn, and its use closes when the tool returns. If that turn then
   moves to a local alias between rounds (`card_hold_follows`, 869-880), the idle warm lease is
   asked to yield like any other (L6, cheap). A Whisper use still open then, in a tool that
   calls the LLM inside itself (Q1, unverified), would be `LeaseWouldWaitOnItself` because the
   use carries the hold.
4. **Another agent's turn** that needs the card while this turn's Whisper use is open waits
   behind the busy lease for its own `swap_wait_s`, and the turn's second transcription use
   queues behind that waiting turn (L5: no new use jumps a waiter).
5. Parallel tool calls in one turn share the one hold (Risks): two transcriptions both go to
   the twin or are both refused.
6. A person's Stop on the turn cancels the tool, and a waiter leaves `_LEASE_LINE`.

**Words** (Mike's rule, 2026-11-18, ADR-044 line 1598: true and clear). They promise no route
that an unmeasured figure could close (Ark #13 #5, Johnny #14 obj. 5, Forge #15 §6–7): "Not
transcribed: this turn runs on the local model '<alias>', which holds the card. A file over
300 s needs the card, so nothing was queued." For a twin that cannot run: "Not transcribed:
the CPU could not take it (<reason>), and this turn holds the card, so nothing was queued."

**The consequence, plainly.** On a local alias an agent can transcribe files up to 5 minutes
and no longer files, until the turn ends: the price of #68 and #71 together, as Zcode #69's
for a dictation, except that an agent cannot wait out its own turn. A hum or a voice note
fits; a whole song or podcast is refused. **Learned in operation (Mike #11):** with Strata
resident, Forge read 8.3 GB of host RAM available at 86 % load (#15, 17:53), so the RAM check
may close the twin in exactly this case; and how much a twin run slows the Strata turn is
unmeasured (L12's pending rows).

**The self-wait rule for a tool that queues a card job (2026-11-24; Zcode #51 correction 1,
ADR-051 D51 and D42; decided, not built).** The music tools (`music_wait`, `music_check`,
`music_propose_job`) queue a card job, a music engine run, from inside a turn. A tool in a
turn that holds a live **card hold** on a **local alias** must not wait on a card job that is
queued behind that hold: the job's holder would be this very turn, the same cycle as the
transcription above. The tool:

- **answers at once** with the job's real state, never waiting out the turn's own hold;
- says **"queued, starts after this turn"** only when both are true: the turn holds a live
  card hold on a local alias, **and** the job is `queued`. A job in `review`, `failed` or
  `cancelled` is reported as that, not as queued;
- reads the hold as a **local alias**, not as `current_card_hold()` being non-empty: that
  returns a hold also in a cloud-alias turn (`alias=None`, nothing stands on the card) and in a
  tool thread that outlived its turn (`llama_server_supervisor.py`), and `card_wait` is still
  empty right after `propose` (the runner writes it), so the state comes from the job.

The precedent is the glossary's **card hold** row: a request that would wait on a holder that
is itself "is refused at once instead of waiting on itself". On a cloud alias the job starts
inside the turn and unloads a resident Strata, so there is no self-wait and the rule does not
apply.

What the rule leaves for the owner to know (Zcode #51, not new facts of this ADR):

- **A wake during the job queues on the card.** A `check_back` or a message that wakes the
  agent while the music job runs takes its place in the card queue and waits up to
  `swap_wait_s` (1800 s from acceptance on this machine), then `CardQueueRefusal`; the owner
  sees `wait_reason = card_queue`.
- **After the job the turn pays Strata's cold start**, about 23 s (23.5 s from spawn to smoke
  passed, 24.6 s to the first answer, measured 2026-11-24 on backend `670141bb`, in
  `ai-studio` `knowledge/adr052-gpu-card-lease.md`). The delay of a `check_back` is the job's
  ETA plus this start (ADR-051 D49).
- **Cancelling a running job does not stop the engine.** It stays on the card until its idle
  stop, `yue2_idle_stop_s` (300 s). The takes already made stay in the library, and Keep and
  Discard are refused for a `cancelled` job. Existing behaviour that agents will make common;
  Stop on the turn cancels only the jobs still `queued` (ADR-051 D48).

### 4. What the UI and logs show

**Logs, one INFO line per transition, no repeats while polling:**

- `card: <display> granted (<what>)`
- `card: <display> loaded beside <music display> (fits: N MiB)`, only for L11's one shared
  cell
- `card: decided without knowledge of the card (<reason>): <decision>` (section 5)
- `card: <display> waits for <holder display> (<busy: who_uses | idle, asking it to let go
  | yielding>); wait <N s | about N min (estimate) | unknown>, up to <bound | until
  cancelled>`
- `card: asked <holder> to let go for <contender>`
- `card: <display> let go (<why>), held <s> s`
- `card: <display> refused: <reason>`
- A swap of a supervisor out for a lease reuses `_note_swap` with its existing words; the
  toast title is per kind (2.2 item 5).

**`local_engine_status`** (`providers/strata_provider.py:577`, keys at 613–617):
`card_holders` and `card_queue` carry the lease rows from L7. Lease-related keys added:
`kind`, `display`, `busy`, `uses`, `vram_mib` (None = "unknown size"), `eta_s` (None =
"unknown", shown as "—", never "0 s"), `beside`, `until_cancelled`, `wait`.

**The frontend already reads these keys, in 12 files** (section 0). So the stale card
1925 is not the UI's starting point, S5 is smaller than first written, and each lease row
must be checked against what those files assume:

- **Card holder and Stop engine** (`ProvidersEditor.svelte`, `cardStatus()` 2375):
  `card_holder` stays the first supervisor row until S2 item 8 (L7), so the Stop engine
  button never points at a lease before `stop_local_engine` can honour one.
- **"Also on the card"** (`otherCardHolderLines`, `cardQueue.ts:37-39`, used at
  `ProvidersEditor.svelte:2423`): lease rows come after supervisor rows, so they print as
  "Also on the card: whisper:local (whisper, running)". The snippet renders **once**, in the
  Local engines block, and only when a `strata` or `llamacpp_server` provider is configured
  (2538). A node with Whisper and music but no local LLM provider sees no holder list
  there; S5 decides whether that block shows for any lease holder.
- **The queue lines** (`cardQueueLines`, `cardQueue.ts:42-51`): a lease waiter has no
  `agent_id` or `conversation_id` (rendered as nothing, fine), and a `null` `give_up_at`
  would print "coming back from a description". S5 reads `until_cancelled` and prints
  "until cancelled" instead; S1 sets it.
- **The music job's wait** (`musicCreate.ts:144-146`, `MusicCreate.svelte:401-414`):
  holders show only for a `queued` job today; S4 widens it to `running` with a
  `card_wait` (section 3.2). The "Stop X" button calls `stop_local_engine`, so a lease
  holder needs S2 item 8, or the button is hidden for lease rows until then.
- **Turn wait reasons** (`turnCommandWait.ts`, `agentMap.ts`, `agentTurnNotice.ts`):
  unchanged; a transcription in a turn waits inside a tool, and the tool's
  `emit_progress` says so.
- **Pinned sources:** `cardQueue.test.ts:90-105` pin
  `otherCardHolderLines(engineStatus?.card_holders)`,
  `cardQueueLines(engineStatus?.card_queue)` and `card_holders: r.card_holders ?? []`. S5
  keeps those lines or updates the pins in the same commit.

**Holder names in human words:**

- "Whisper (whisper-large-v3-turbo) — dictation"
- "yue2.cpp — music job 01J…"
- "Embedding model (bge-m3) — index rebuild"

**Waiting reasons are visible:**

- music job card: "waiting for the card: Whisper holds it"
- dictation or voice message: "transcription queued behind yue2.cpp (song, ≈6 min | time
  unknown)", with a cancel
- agent tool: `emit_progress`

**Peers** see only "this node's GPU is busy with another job right now".

### 5. Cross-platform behaviour

- **The lease is pure Python, in-process, on all three OSes.** It serialises DPC's own
  clients whether or not any GPU tool exists. A lease never asks nvidia-smi whether it may
  be granted; it asks the registry. L11 is the one place a reading can *widen* a grant
  (Whisper beside busy music), and without a reading it does not.
  - macOS: Whisper on MLX or MPS uses unified memory. The lease still serialises music,
    Whisper and the embedder, which matters even more there. L11 has no reading on macOS,
    so dictation queues behind music there.
  - AMD and Intel: the same, through the registry; L11 has no reading (nvidia-smi only).
- **Foreign-process detection (S8)** is a new `managers/card_probe.py` built on the
  existing `process_registry._default_vram_reader(sys.platform)`:

  | Platform | Source | Coverage |
  |---|---|---|
  | Windows | PDH `\GPU Process Memory(*)\Dedicated Usage` | Vendor-neutral, because it is a WDDM counter. **[unverified on AMD/Intel]**. It is a commitment and can exceed physical memory. |
  | Linux | NVML | NVIDIA only. AMD/ROCm reports "not measured: no NVML". |
  | macOS | none | Reports "foreign use not measured on this platform". |

  - The probe result states which case applies, in `card_holders` and in the log.
  - **DPC's own pids are not foreign:** `os.getpid()` (in-process leases) and every registry
    entry and its tree (supervised children and music engines).
  - **The desktop: which rule decides what** (Ark #45). Two different questions, each
    answered once:
    - *Is this process a holder?* The **ignore list** decides: `dwm.exe`, `csrss.exe` are
      never foreign holders. The backlog (7366) measured dwm at **2,709 MiB**, so the
      Ruach "a foreign process over 2 GiB closes the card" rule would trip permanently on
      Windows without it. The ignore list subtracts nothing from any arithmetic.
    - *Does a load fit?* A reserve decides, and only it, and only in arithmetic against **total**
      memory: since S3e the engine's own (`vram_reserve()`; `_DESKTOP_RESERVE_MIB` = 2048 is
      llama-server's and the last fallback), in the Strata peer gate (`strata_provider.py:465`) and
      in L11's declared check. L11's live veto compares **measured free** memory, which already excludes the
      desktop's current use, with Whisper's peak and the music growth left, and adds no
      reserve. Nobody subtracts both the desktop's measured use and the reserve.
  - **A foreign process over 2048 MiB** becomes a synthetic holder: `engine "foreign"`,
    `alias "pid 23200 python.exe (ComfyUI?)"`, busy, never yieldable. Contenders wait,
    bounded, and are refused naming it.
  - **Where the readings come from** (Mike #70: "how can that fail? the agent map already
    collects RAM and VRAM"). The agent map reads the same helpers, and the probe reuses
    them instead of spawning anything new:
    - the card's free and total VRAM in the agent map (`vram_free_mib`,
      `vram_total_mib`) come from `commit_memory.read_vram_mib()` (135), which calls
      `_free_vram_mib` / `_total_vram_mib`, i.e. nvidia-smi, at most once per
      `_VRAM_READING_TTL_S` = 30 s (130). L11 reads through it; the lease module
      invalidates that cache on every grant and release, so a reading never predates the
      last change of holder (Mike #11, item 13; S3 adds the clearing function, L11 "The
      cache, exactly");
    - per-process VRAM (`peak_vram_mib`) comes from the process registry's sampler, every
      `SAMPLE_INTERVAL_S` = 2 s (268), through `_default_vram_reader` (PDH on Windows, NVML
      on Linux), which never spawns nvidia-smi (`process_registry.py:36-40`); S8 reads the
      sampler's latest full reading instead of calling the reader again. The sampler keeps
      only a running peak per engine (`process_registry.py:2425-2426`), so **L11 never reads
      it** (L11, "The quantities"); only S8's foreign-process test does.
  - **What can still fail, honestly:** no NVIDIA card or no nvidia-smi (AMD, Intel,
    macOS); nvidia-smi timing out (10 s) during a driver hang or reset; output it cannot
    parse (a driver/NVML mismatch); a probe refused during the backend's stop
    (`ProcessRegistryClosed`, unless `while_stopping`); PDH or NVML unavailable for the
    per-process figures. Then the reading is "unreadable", never "free": background leases
    are refused (CPU), and **every decision taken without a reading logs that it was taken
    without knowledge of the card**, with the reason (Ark #67, Zcode #69): "card: decided
    without knowledge of the card (<reason>): <decision>". Foreground leases go ahead with
    that line at WARNING.
  - **The 90 s claim, in one sentence** (Zcode #41): a lease that is granted counts its
    `vram_mib` as in use, in the fit arithmetic and in the probe's "is it ours" test, from
    its grant until 90 s after its load has finished, because a fresh load's memory is not
    yet visible to the readers; 90 s is also the TTL of the future HTTP lease (S10).
  - Separately, `_free_vram_mib` (supervisor 1793) should return a distinct error for a
    failed read versus no nvidia-smi (new card (b) below). L11 treats both as "no reading".

## Considered Options

The three options come from the backlog card
`EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED`.
Unify-versus-replace of `_CARD_CLAIMS` is a separate sub-choice (section 2.3).

- **(a) Supervised external worker** (the ADR-046 path). DPC starts the worker as a
  supervised process or local engine, so it gets a claim, a place and a recorded end like
  Strata.
- **(b) Claim/lease API.** A registry that a client calls to take a place in the card
  queue and to release it, with a lease timeout for a client that dies. Chosen, in-process
  first, with an HTTP surface in `local_api.py` as S10.
- **(c) Foreign-usage detection** through per-process VRAM reads (the card says
  `nvidia-smi --query-compute-apps`; the design uses the readers that already exist in
  `process_registry.py`, see section 0). A DPC start that finds a process it did not start
  on the card waits or refuses, naming it. Chosen as S8, beside (b).

### Pros and Cons of the Options

#### (a) Supervised external worker

- Good: the strongest guarantees; a claim, a place and a recorded end for free.
- Bad: costs one supervisor per worker kind.
- Bad: fails for a job started from a browser or a DAW, and for YuE2-Studio unless DPC
  becomes the one that starts it headless.
- Bad: cannot cover in-process models (Whisper, the embedder, GLiNER) at all, because
  there is no child process to supervise. The music engines already took this route
  (6b34b44c), and the lease is how they become visible holders.

#### (b) Lease API

- Good: works for anything that can make a call: in-process through Python, external
  through HTTP.
- Good: reuses the queue and the small holder surface `_free_the_card` already speaks.
- Bad: advisory for anything that does not call it. Hence (c) beside it.

#### (c) Foreign-usage detection

- Good: needs no cooperation from the foreign process, and is the cheapest.
- Bad: it only protects DPC from the job; it never frees the card for the job (CC's note
  2026-11-19 in the card).
- Bad: per-process reads differ by platform (section 5), and the Windows counter needs a
  desktop ignore list.
- Neutral: compatible with either (a) or (b), as the card itself says.

#### Supervisors become lease clients (replace `_CARD_CLAIMS`)

- Rejected for now: would touch the 5,555-line `llama_server_supervisor.py` that other
  agents are editing and ~11 tests that poke the dict. Left as a possible later refactor.

## Consequences

- **Positive:** the Whisper card's Done-when ("a transcription during a running YuE2 job
  waits and names the holder") is met by S1 + S3 + S4. Mike's #20 ask (llama-server and
  Strata wait) is met by S2. The embedder and GLiNER can join without a redesign.
- **Positive:** dictation during music keeps working where it fits (L11), now visible; where
  it does not, a short file goes to the CPU when that is quicker and a long one queues
  (L12, Mike #62), instead of an unchecked out-of-memory failure on a small card.
- **Negative:** the CPU twin keeps a second copy of the model in host RAM while it is
  loaded (L12's RAM figure, unverified) until its own short idle timer frees it.
- **Negative: CPU contention** (Ark #64 §3). Strata keeps its experts in host RAM (the
  resident budget, `docs/GLOSSARY.md`; the help text at `ProvidersEditor.svelte:2366`) and,
  by Ark's reading of `strata_details.md` (not in this repo), computes missed experts on
  the CPU in parallel with the GPU. A CPU transcription during a Strata turn would slow
  the holder of the card and add the twin's RAM beside the expert cache. Mike #71 kept the
  route open then for files up to 5 minutes; L12 multiplies the estimate by
  `cpu_contention_factor`, tuned in operation. Music engines are assumed GPU-bound until
  measured.
- **Positive:** music no longer reads a failed holder list as a free card: the lease
  registry is in-process and has no read that can fail. Unreadable probe results are never
  "free".
- **Positive:** `_CARD_CLAIMS` keeps its shape, so existing tests see identical behaviour
  (`_live_holders() == _live_claims()` with no lease registered, pinned by a test).
- **Negative:** where L11 answers `does_not_fit` or `unknown` (a smaller card, macOS, AMD,
  a size it cannot read), the owner's dictation during a music job **waits** in the line,
  up to the dictation bound (600 s), instead of today's load beside. The person
  sees why and can cancel; a music job longer than the bound still ends in a refusal
  (decision 6).
- **Negative:** `service.transcribe_audio` and the Telegram paths gain a deadline they do
  not have today; a transcription that used to wait for a slow provider indefinitely now
  ends at the bound.
- **Negative:** a peer's transcription that cannot be granted at once waits only when its
  whole trip fits the guest's 120 s, behind the owner's waiters, and is otherwise refused at
  once (L13; Mike #70, #11); in v1 that means refused during a music job or a busy turn.
  Today a peer would load Whisper beside the music engine, or beside Strata.
- **Negative (Ark #13):** while the backend stops, `read_vram_mib` gives `(None, None)` (the
  probe is refused without `while_stopping`; not cached, `commit_memory.py:152-154`), so L11
  says `unknown` and nothing loads beside music. Intended, not a defect.
- **Negative (Mike #68, Zcode #69):** Whisper no longer loads beside a local LLM. A
  dictation over 5 minutes of audio during a long agent turn waits in the line and is
  refused if the turn outlasts the dictation bound. Shorter files go to the CPU twin.
- **Negative:** the first slice grows to S1+S2+S3+S4, and S2 changes the 5,555-line
  supervisor file other agents edit.
- **Negative:** with swap off, supervisors now take queue places when only leases block
  them. That path existed only with swap on until now.
- **Negative (Ark #44):** with swap off, **background calls start being skipped** while a
  lease-only place waits, because `_CARD_QUEUE` is then non-empty and the background check
  at `llama_server_supervisor.py:2809` skips on a non-empty queue. Today, with swap off, a
  background call never meets that check. Mike would see it as background work in agent
  chats being skipped with the reason "a request is waiting in the queue for the card".
  Own test (Confirmation, 16).
- **Negative (Mike #70):** with the swap on, a music job swaps out an idle llama-server or
  Strata when nothing waits; a stopped Strata drops its parked conversations (the live one
  is saved first), and the agent's next turn pays the reload. With the swap off, music
  still waits for a person's Stop X. What is lost is the engine's cache, not the
  conversations, the owner is told by a toast and the agent is not, and the length of the
  agent's wait is written down in section 2.4.
- **Negative (Mike #68 and #71, Ark #82):** an agent on a local alias can transcribe files up
  to 5 minutes (CPU twin, when host RAM allows) and nothing longer while its turn runs; a
  longer one is refused at once (section 3.5).
- **Negative:** fp16-GPU and bf16-CPU embedding vectors differ slightly (cosine
  0.998–1.000 measured) and the cache key has no device in it.
- **Neutral:** S1 to S4 add a new module and touch the supervisor in a few places, but not
  the dict. S2 touches the large supervisor file and should land after the Whisper
  editing settles.

### Risks

- **Locks inside a client (deadlock).** A lease is never acquired while holding a client
  lock: Whisper `_load_lock`, the music engine `_the_lock` or `MusicEngines._lock`, the
  embedder `_load_lock` or `_gpu_semaphore`, GLiNER `_GLINER_LOAD_LOCK`. Yield callbacks
  do take those locks. The first draft broke this for music (grant inside `_the_lock`);
  section 3.2 now acquires before `_the_lock`, and `_yield_card` answers `refused` without
  touching `stop()` while `_the_lock` is held. Tests: a yield during a waiting acquire must
  finish (Confirmation, 14), and the music lock-order falsifier (15).
- **A cycle between a supervisor and a lease.**
  - Invariant L2: a waiter holds nothing, and a granted holder never waits for the card.
    So "llama-server waits for Whisper while Whisper waits for llama-server" cannot
    happen.
  - Whisper and music now wait for supervisors (Mike #68, #70), but as waiters they hold
    nothing (L2).
  - The one way to violate L2 is a request that stands on X (`_CARD_HOLD`) and waits for
    something that waits for X. That is caught at once by `LeaseWouldWaitOnItself` /
    `_this_request_holds`, for a lease holder and a supervisor holder alike (2.2 item 4).
    Examples: an agent turn on Strata whose tool transcribes, so would wait for Strata (in
    the first slice now; L12 sends a file up to 300 s to the CPU twin, a longer one is
    refused at once); a tool that holds a Whisper use and then calls the LLM inside the
    same tool.
  - **The supervisor variant through music** (Zcode #41): an agent on Strata that proposes
    a music job and waits for it. The job's lease waits for Strata (decision 32), and the
    turn holds Strata. The contextvar does **not** reach the job, because the runner's
    worker task was created at `MusicJobRunner.start()` (`runner.py:455`). No such agent
    tool exists in core today. When one is added, it must not await the job while its turn
    stands on a supervisor that the job's lease would wait for; it is refused at once
    instead, naming the holder (test 9, supervisor variant).
  - Parallel tool calls in one turn share the same hold, so they are treated as "itself".
    **[unverified]** whether any tool calls the LLM while a transcription use is open.
- **Thread clients.** `acquire_blocking` on the owner loop thread raises. It does not
  block.
- **Yield tasks.** They are kept in a module set and bounded by the contender's
  `stop_bound`. Tests clear them in conftest. The open card
  THE-CARD-QUEUE-TESTS-LEAVE-PENDING-TASKS-THAT-CAN-FAIL-A-LATER-TEST (1550) already shows
  how leaked tasks fail later tests, so assert no pending tasks.
- **Interpreter and worker task cancellation** (Ark #45 finding 1; Ark #60 §2–3, #61
  §2a). A loop teardown or a backend stop can cancel the task that runs an unload or a
  yield, or the worker task under it, and an executor that is shutting down can refuse the
  work. The lease must be released exactly once, never while the client's thread still
  holds the memory, and **never when the unload did not run**: Whisper releases in a
  `finally` around `_finish_thread` only when `model_loaded` is False (section 3.1), which
  covers all four of `_finish_thread`'s outcomes, including the exception that escapes it
  when the executor refuses (not in its docstring, Ark #60 §2) and the worker cancelled
  before its thread started (`d80d76f7`). A yield task cancelled mid-way ends its mark
  (`_end_swap_drain`) in a `finally`; `release()` is idempotent. Tests 26 and 27.
- **A thread that never ends** holds `_load_lock` and the grant until the shutdown bound;
  `_finish_thread` cannot bound it (stated in `d80d76f7`). The lease does not make this
  worse, and does not fix it.
- **The CPU twin under memory pressure.** The twin's model loaded while Strata keeps its
  experts in host RAM could push the machine into paging; L12 checks available RAM before a
  cold route, and the twin has its own idle timer.
- **A wrong CPU estimate.** Figures measured on an idle machine under-read a busy one, a
  single-length figure misjudges both ends, and a cold start ignored makes short files look
  cheap (Ark #64 §1–2). L12 uses cold and warm figures at several lengths, the contention
  factor, `cpu_max_s` and the `cpu_max_audio_s` guard, and refreshes the figures after
  every twin run; a wrong choice costs at most `cpu_max_s`.
- **A wrong music ETA.** A running job's remaining time is unknown in v1 (L10) and counts
  as long, so L12 leans to the CPU twin during music; the text estimate is never used for a
  running job (Ark #64 §5).
- **Starvation.**
  - Both lines are FIFO on one clock (L5a). L5 stops new uses of a granted lease while
    someone waits for it.
  - Music behind a **supervisor**: a busy one is waited for; an idle one is swapped out
    with the swap on (Mike #70). With the swap off the job waits for a person's Stop X.
  - A dictation behind a busy supervisor waits, or goes to the CPU twin (Mike #68, #71).
  - A series of server starts cannot overtake a music lease that waits for a lease holder
    (L5a point 4; Confirmation, 3b).
- **Ping-pong.** Dictation, then llama-server, then dictation: each swap costs an unload or
  reload of Whisper, which is cheap. A llama-server is stopped for Whisper only for a file
  over 5 minutes with the swap on and nothing waiting (item 1a), and for music under the
  same conditions. A safety net like `_MAX_SWAP_ROUNDS` applies to lease contenders too.
- **Background calls.** `background=True` never queues, waits or yields (rule 10 analog).
  The embedder's steady state and the Whisper preload are background. LLM background calls
  keep rule 10 unchanged, except for the swap-off case in Negatives.
- **L11 misjudges the room.** A free-VRAM read taken while YuE2 is in a lighter stage
  sees more room than the stage peak leaves (Ark #53). L11 therefore decides on declared
  peaks against total memory, lets the live reading only veto, subtracts the music growth
  left (the whole peak until S8 can measure the child), and treats any unknown as
  `unknown`, which queues. Remaining risks: a declared peak that is wrong (the YuE2 figure
  is a measured range, the larger end is used), and a desktop that grows past the
  reserve; the second is the risk the admission arithmetic carries today.
- **The provider cascade multiplies a wait** (Ark #53). Without condition 2 of section
  3.1, a card refusal falls into `except Exception: continue` (`service.py:5152-5160`) and
  the next local alias queues again and loads another model. Test 22.
- **P2P peers using our Whisper.**
  - `p2p_coordinator.handle_transcription_request` must wrap the call in `serving_a_peer`
    (it is not wrapped today, line 1398).
  - A peer's transcription is granted at once if the card is free, if only idle cheap
    leases stand in its way, or if L11 finds a fit beside busy music; otherwise it waits,
    behind every owner's waiter, only when its whole trip fits in 120 s, and an over or
    unknown estimate is refused at once (L13, Mike #11).
  - Yielding an idle music engine for a peer happens only on the owner's terms (swap on,
    nothing queued, nothing busy, as in `_why_a_peer_may_not_swap`).
  - The refusal is worded for a peer, says "right now" and gives the estimate when known.
- **Hand-over during a reload.** One lease object follows the model, never two. Test that
  `hand_over_to` keeps a single registry entry.
- **`on_yield` that lies.** A client that returns `let_go` without freeing memory is caught
  later only by the probe (S8). Whisper's `_unload_model` returning False is logged.
- **Caller deadlines and the queue.** A caller that gives up before the lease line does
  (the UI's 240 s command timeout) leaves a transcription that runs for nobody; a caller
  with no deadline at all (`service.transcribe_audio`, the Telegram paths) waits as long as
  the queue lets it. S3 gives every caller a deadline and takes the queue's from it (`min(
  remaining − margin, bound)`, L4); until then decision 6's queue is not shipped.
- **Duplicate work on a voice message.** A receiver whose local attempt waited in the line
  would transcribe even though the peer's transcript arrived meanwhile; condition 3 of
  section 3.1 drops it at the end of the wait (test 23).

## Confirmation

No GPU is needed and every wait is bounded. General approach:

- Fake lease clients carry an `asyncio.Event`-gated `on_yield`.
- Fake supervisors use the existing monkeypatched `_launch`/`_spawn` patterns from the card
  tests.
- Fake settings go through `bind_local_engine_settings`. `_SWAP_POLL_S` is monkeypatched to
  0.01.
- Every `await` is under `asyncio.wait_for(..., 5)`.
- Whisper uses `device: "cuda"` in the config, with `_load_model`, `_unload_model` and
  `_transcribe_loaded` monkeypatched, as
  `test_the_whisper_model_unloads_safely_and_when_idle.py:35-37` does. The lease path must
  not import torch when the device is explicit.
- Music uses the `_yue2_helpers` Rig with its fake engine.
- The free-VRAM reading for L11 is injected (a fake `_free_vram_mib`), so the fit tests run
  on Windows and Linux CI.

New test files:

1. `test_a_lease_is_granted_once_and_two_acquires_cannot_both_pass.py`: an atomic grant,
   re-entrant `use()`, release on every path, `acquire_blocking` from `to_thread` works,
   from the owner loop it raises.
2. `test_a_busy_lease_is_waited_for_and_an_idle_one_is_asked_to_let_go.py`: a busy holder
   is never asked; an idle one is marked `draining` before `on_yield`; `refused` goes back
   to `running` and the contender keeps waiting; **`nothing_to_give` releases at once**
   (a granted Whisper lease with no model loaded never keeps a contender waiting); a
   deadline refusal names the holder and `who_uses`; `wait.wait_s is None` when the ETA is
   unknown and never 0.
3. `test_no_new_use_jumps_a_waiter.py` (L5 and FIFO): (a) a dictation stream cannot starve
   a waiting music lease; (b) **a series of server starts does not starve a waiting music
   lease** (Zcode #41): music waits for a busy Whisper, three llama-server/Strata starts
   arrive after it, Whisper lets go, music is granted first, the starts then wait for the
   busy music job and are refused naming it at their deadlines; (c) a waiting music lease
   that is first in line swaps an idle supervisor itself (Mike #70) and a later supervisor
   start does not overtake it.
4. `test_a_transcription_during_a_running_yue2_job_waits_and_names_the_holder.py`: **the
   Done-when**, with the reading faked to "does not fit"; plus the reverse (a music job
   yields an idle warm Whisper and waits for a busy one, `card_wait.holders` naming
   Whisper).
5. `test_llama_server_and_strata_wait_for_a_busy_lease_and_unload_an_idle_one.py`: with
   swap on **and** swap off; `config.get("gguf_path")` read on a live Strata supervisor; the swap note and `_LAST_SWAP` (no "parked" words, the
   lease's toast title); a lease that does not let go within `stop_bound` gives a refusal
   that names it.
6. `test_with_no_lease_registered_the_card_behaves_as_before.py`: `_live_holders() ==
   _live_claims()` and `card_holders()` rows equal the old rows plus `kind`.
7. `test_a_background_lease_never_queues_and_never_yields.py`: the Whisper preload and the
   embedder fake give `LeaseSkipped` while anything holds the card or someone waits.
8. `test_a_peer_lease_waits_only_when_its_estimate_fits_its_deadline.py` (Mike #70, #11;
   fake clients): the deadline comes from the peer's door; an estimate that fits waits
   behind every owner's waiter and is granted; an estimate over it, or unknown, is refused
   at once; an owner's request arriving later goes ahead of the waiting peer; the refusal
   contains no alias, agent or conversation ids, says "right now" and gives the estimate.
9. `test_a_lease_that_would_wait_on_its_own_request_is_refused_at_once.py`: inside
   `card_held_for(alias, "accepted_request")` and the use-carries-hold case; **and the
   supervisor-holder variant** (Zcode #41): a turn on Strata whose code waits for a lease
   that waits for Strata is refused at once. Elapsed < 0.5 s in each case.
10. `test_whisper_holds_one_lease_across_a_reload_and_lets_go_on_every_unload.py`: idle
    timer, deferred, `release_if_loaded_here`, switch, shutdown, a yield; the CPU device
    makes no lease; an `mlx` provider loaded by hand and never granted releases nothing; a
    `cuda` load whose `model.to` fails over to the CPU releases the lease (Ark #13).
11. `test_a_music_engine_lets_go_of_the_card_however_it_stops.py`: idle stop, ui_stop,
    swap, exited on its own (fake `proc.wait` returns), failed start; a child that exits
    under a job's open use releases, and the job's next start acquires again while
    `running`.
12. S8 `test_foreign_card_use_is_named_and_an_unreadable_card_is_never_free.py`:
    - a fake reader returns `{own: 1500, dwm: 2709, foreign: 9000}`: only the foreign pid
      is named;
    - the reader raises: "unreadable", background refused, and the refusal's reason logged;
    - the reader returns None: "not measured on this platform".
    - Runs on Windows and Linux CI because the reader is injected.
13. Rewrite
    `test_a_generate_from_text_job_runs_end_to_end_on_the_supervised_engine.py:152,180`.
    Register a fake supervisor claim (`_CARD_CLAIMS[FakeSup] = "claimed"`) instead of
    `Rig(holders=...)`. Keep the assertion "nothing is stopped" and the exact
    `state_reason`.
14. `test_a_yield_during_a_waiting_acquire_completes.py` (kept from the Risks, Zcode #41):
    a contender waits, the holder's `on_yield` takes the client lock, the yield finishes
    and the contender is granted.
15. `test_a_music_engine_start_never_waits_for_the_card_under_its_own_lock.py` (Ark #44's
    falsifier): a fake supervisor holder, `engine.start()` by hand and a contender that asks
    the music lease to let go; the yield completes (or answers `refused` while a start holds
    `_the_lock`) and never sits on `_the_lock` until `stop_bound`.
16. `test_with_swap_off_a_lease_only_place_skips_background_calls_and_says_why.py` (Ark
    #44): swap off, a llama-server start blocked only by a lease takes a place; a
    background call is skipped with the queue reason; with no place waiting it passes as
    today.
17. `test_a_music_job_that_waits_while_running_shows_its_holder.py` (Ark #44):
    `set_waiting_for_card` on a `running` job records `card_wait`; the runner does not fail
    the job; `clear_waiting_for_card` clears it.
18. `test_whisper_shares_with_busy_music_only_when_it_provably_fits.py` (decision 6,
    L11; Zcode #52, Ark #53): `fits` → granted beside at once, **both rows in
    `card_holders()`**, `beside` set, the "loaded beside" line; `does_not_fit` (declared
    sum over total, or the live reading vetoes) → waits in the lease line; `unknown`
    (`model_size_bytes` source None, a holder with no declared peak, the reading None or
    raising) → **waits in the lease line, neither beside nor refused** (a person's door; a
    peer is test 37's), logged as unknown;
    a live reading never turns a declared "no" into "yes"; an earlier waiter blocked by the
    music lease → no co-grant; the music lease's busy, yield and stop are unchanged by the
    co-holder; a peer or background call that does not fit is refused at once; no other
    pair (Whisper × llama-server, music × Whisper) is ever co-granted by measurement.
19. `test_whisper_never_loads_beside_a_local_llm_engine.py` (Mike #68): with llama-server
    or Strata on the card, a Whisper load never shares it; busy → L12 (a 60 s file to the
    CPU twin, a 400 s file to the line); the self-wait of an agent's own tool → CPU twin
    for 60 s, refused at once for 400 s; no "loaded beside" line is ever written for a
    supervisor.
20. `test_a_queued_transcription_outlives_no_caller.py` (S3 integration): the queue's
    deadline is `min(caller's remaining − margin, bound)` for the owner's door, the agent
    tool (1800 s tool timeout) and a Telegram call; `service.transcribe_audio` ends at its
    explicit bound; an unknown ETA waits with "time unknown" and is not refused before the
    deadline; a person's cancel takes the waiter out of `_LEASE_LINE`.
21. `test_a_third_contender_asks_only_holders_in_its_way_cheap_idle_first.py` (Zcode #52,
    Ark #53): Whisper beside busy YuE2, then a llama-server start: the idle Whisper is
    asked to yield first, the busy music job is waited for, in `_CARD_ARRIVAL` order; a
    holder not in the contender's way (a measured share for that contender, faked) is
    never asked.
22. `test_a_card_refusal_ends_the_provider_cascade_and_one_call_keeps_one_place.py` (Ark
    #53's falsifier): two local Whisper aliases in `provider_priority` and a busy card;
    one received voice message puts exactly one waiter in `_LEASE_LINE`, no second model
    load is attempted, and a cloud alias later in the list is still tried.
23. `test_a_voice_message_queued_behind_music_is_dropped_when_the_peer_transcript_arrives.py`
    (Ark #53): the peer's transcript arrives during the wait; the lease `check` ends the
    wait, nothing loads, and no second transcript is stored.
24. `test_a_waiting_dictation_holds_no_whisper_lock.py` (Ark #53): while a dictation waits
    in the line, a peer request is refused at once (elapsed < 0.5 s) and never waits on
    `_load_lock`.
25. UI (S5, vitest): a lease row in "Also on the card"; a lease waiter with
    `until_cancelled` prints "until cancelled", not "coming back from a description";
    `cardHolders()` shows holders for a `running` job with a `card_wait`.
26. `test_an_unload_that_never_ran_keeps_the_lease.py` (Ark #60 §3's falsifier): during a
    yield, the unload's worker is cancelled before its thread starts; `model_loaded is
    True`, the lease is still granted, the outcome is `could_not_let_go`, no
    `whisper_model_unloaded` is announced, and the contender keeps waiting. The same with
    the executor refusing the work (the exception that escapes `_finish_thread`). A third
    case: the thread runs and `_unload_model` returns False → released.
27. `test_nothing_to_give_and_could_not_let_go_are_told_apart.py` (Ark #61 §2a): a granted
    lease with no model → `nothing_to_give` and released at once; a granted lease with a
    model whose unload did not run → `could_not_let_go` and kept.
28. `test_the_fit_counts_every_resident_not_only_those_in_the_way.py` (the **worst-case**
    `need`, flat by choice, L11; Ark #60 §5, #61 §2b; the arithmetic of a cell the runtime does not
    produce before S3: until then two leases are served one after the other, `CardLease._excludes`): three residents (a llama-server child with `predicted_vram_mib`, a busy YuE2, a
    co-living second Whisper alias) and a Whisper contender: the fit sums all three; the
    yield list holds only those that exclude the contender; a sum built from the yield list
    would say "fits" and the test proves L11 says `does_not_fit`. With a Strata child as a
    resident the outcome is `unknown`. `dwm` in a fake per-process reading changes nothing.
    The test also **pins the value of `need` in MiB**, not only its outcome (Ark #18-3): Whisper
    alone, no resident, from a fake revision-less turbo row of 1 622 443 339 B → `whisper_peak` 1547 + 2900 =
    4447 and `need` = 4447 + 2048 = 6495 MiB (derived, not measured; to be re-measured on v0.2.20 before it becomes a test constant (Forge #12, Zcode #22-5)), logged with its `model_size_bytes` source; a
    `need` left in bytes (≈1.6e9) fails it.
29. `test_a_running_job_waiting_for_the_card_does_not_start_its_engine.py` (Ark #60 §4):
    a job in `running`, its engine's child gone, the card held by a busy holder: the engine
    does not start, the job shows its `card_wait`, it is neither failed nor dropped as
    cancelled, and it starts when the holder lets go; a real cancel during that wait ends
    it.
30. `test_the_gpu_cpu_choice_compares_times_not_audio_length.py` (L12, Mike #62, Ark #64):
    card free → GPU; L11 `fits` → GPU beside; Ark's two examples: a 130 s file, CPU 260 s,
    GPU wait 600 s → CPU twin; a 90 s file with the card freeing in 10 s → GPU; unknown GPU
    wait → CPU only when `cpu_estimate <= cpu_max_s`; a file over `cpu_max_audio_s` → the
    line whatever the estimate; a twin that never ran → chosen by the shipped starting
    figures, which its first run replaces; too little RAM for a cold
    twin → the line; background passing → CPU, failing → `LeaseSkipped`; a peer → refused
    at once, never CPU.
31. `test_a_cold_cpu_twin_counts_its_load_and_a_warm_one_does_not.py` (Ark #64 §2, Mike
    #71): with a long load and a 20 s file, a cold twin loses to a short GPU wait and a
    warm twin wins; the twin unloads after its own `idle_unload_s` whatever holds the
    card; it is never loaded because the card became busy.
32. `test_a_busy_strata_lets_short_files_on_the_cpu_with_the_contention_factor.py` (Mike
    #71, Zcode #69): "busy" is read from a live `accepted_request` hold in `_CARD_HOLDS`
    or `_in_flight`; a busy Strata and a 120 s file → the CPU twin, estimate × factor; a
    busy Strata and a 400 s file → the line; the order of the checks is the one in L12; a
    call started on the CPU stays there when the card frees mid-run.
33. `test_the_cpu_twin_is_its_own_provider.py` (Ark #64 §4): the twin is a separate
    `local_whisper` object with `device: "cpu"`, no lease, its own `model_loaded`, lock and
    idle timer; a GPU alias's hand-over moves only the GPU model state; `RoutedToCpu` sends
    the call to the twin with the same `language` and `task`; in the received-voice cascade
    a card refusal skips further GPU aliases and still reaches the twin or a cloud alias.
34. `test_a_running_music_jobs_eta_is_unknown_until_it_has_progress.py` (Ark #64 §5): a
    running YuE2 job with only per-variant progress → ETA unknown (long); a queued job →
    the text estimate, marked as an estimate; an instrumental never reads as "almost done"; the factor is audio seconds per wall second (follow-up 9b), so 80 audio seconds at factor 3.5 read about 22.9 s, and the trial card's 240 s and 30 s songs read their measured 80.5 s and 8.9 s within 5 %.
35. `test_music_swaps_out_an_idle_supervisor_when_nothing_waits.py` (Mike #70): swap on,
    an idle Strata, empty lines → the music lease marks it, the live conversation is saved
    before the stop, the swap note says the parked conversations are dropped, the job
    runs; a busy Strata → waited for; another waiter ahead → no swap; swap off → the job
    waits and nothing is stopped.
36. `test_the_card_probe_reuses_the_agent_maps_readings.py` (Mike #70): L11 and S8 read
    `read_vram_mib` and the sampler, and spawn no nvidia-smi of their own; the cache is
    invalidated on a grant; a reading that fails writes "decided without knowledge of the
    card" with its reason (Ark #67, Zcode #69).
37. `test_a_peers_transcription_waits_no_longer_than_its_guest_does.py` (L13, Mike #70, #85,
    #11): with the card free a peer's transcription is served at once; behind a busy Whisper
    use whose whole-trip estimate fits in 120 s it waits in `_LEASE_LINE`, behind an owner's
    waiter, and is answered within 120 s of arrival (the formula of L13, point 2) whatever `swap_wait_s`
    and `remote_inference_timeout` say; with a recorded ratio of 20× and 30 s of audio the
    estimate is 1.5 s, which a multiplication would turn into a refusal; a trip over 120 s, or an unknown one (a running music
    job, a busy Strata), is refused at once (< 0.5 s; Johnny #14 obj. 1), in words with
    "right now" and no alias, agent or chat; an owner's use arriving later goes ahead of the
    waiting peer; the handler runs under `serving_a_peer`; with `swap_when_idle` off an idle
    holder is not asked to yield for the peer.
38. `test_l11_reads_free_and_total_through_the_agent_maps_reader_and_never_per_process.py`
    (Johnny #83, #14, Ark #13, Mike #70, #11): a helper returning None (no nvidia-smi,
    `sys.platform` faked to darwin and linux, a timeout) and a helper raising (registry
    closed) both give `unknown`, never `fits`; the None is not served again (the cache is
    cleared after it) and the exception was never stored; a clear on grant or release makes
    the next decision read `free` again and keeps `total`; a fake per-process reader with a
    huge PDH figure changes nothing; `growth_left` is the whole music peak; the 30 s pin of
    `test_the_agent_map_reads_the_cards_vram_at_most_every_30_seconds.py` still holds.
39. `test_a_lease_that_swaps_strata_out_reads_the_parked_count_and_says_so.py` (section 2.4):
    a Strata holder with 2 parked conversations → the count is read before the stop, the INFO
    line and toast say the parked conversations are dropped, `last_swap.parked_dropped` is
    true; a count of 0 → not said; a count that cannot be read in 3 s → said as if some went;
    a lease yielding (Whisper, music) → no parked words; with the swap off nothing is stopped.
40. `test_an_agents_transcription_inside_its_own_turn.py` (section 3.5): a turn on a local
    alias with a 300 s file → the CPU twin, no lease, no wait, whatever
    `cpu_contention_factor` and `cpu_max_s` say; a 400 s file →
    `LeaseWouldWaitOnItself` in under 0.5 s, naming the alias, no place in either line; a
    120 s file with too little RAM for the twin (`twin_ram_mib` + margin) → the same refusal
    with the reason, and the
    words offer no other route; a turn on a cloud
    alias → the line, with `emit_progress`, no refusal; a turn that moves to a local alias
    between rounds → the idle warm lease yields and nothing is refused; a person's Stop takes
    the waiter out of `_LEASE_LINE`.

Compliance checks beyond tests:

- [ ] With no lease registered, every existing card test passes unchanged (full core suite
      before any "green" claim).
- [ ] No lease is acquired while a client lock is held.
- [ ] No wait estimate ever reads 0 when unknown; no refusal text for a peer names an
      alias, agent or chat.
- [ ] S3's Done-when includes one live run with `transcribe_audio_file` enabled (the in-turn
      path, 3.5) and one with the local engine stopped (L11's arithmetic beside music), because
      tests alone exercise neither (Ark #18-6, #18-7); the second logs `need`, `total`, `free`
      and the real `nvidia-smi used` (Johnny #32-9).

## Scope

- new `dpc-client/core/dpc_client_core/managers/card_lease.py` (lease, line, refusals,
  wait estimate, L5a, L11's one shared cell) and, in S8, `managers/card_probe.py`
- `managers/llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_LEASE_WAITERS`,
  `_live_holders`, `card_holders`, `card_queue`; and, in S2, section 2.2 items 3–10)
- `providers/whisper_provider.py` (designed against its interface; coordinate with the
  agent editing it), `music/commands.py` (`_music_transcribe_with_whisper`),
  `tools/transcribe.py` (`transcribe_audio_file`), `p2p_coordinator.py`
  (`handle_transcription_request`), `service.py` (`transcribe_audio`'s bound, the provider
  cascade in `_maybe_transcribe_voice_message`, the peer check; Telegram,
  `telegram_coordinator._transcribe_voice_and_notify` and `agent_telegram_bridge._transcribe_voice`,
  is covered through `transcribe_audio`), `settings.py` (`[local_transcription] queue_wait_s`),
  `ui/src/lib/coreService.ts` (the `transcribe_audio` command's timeout)
- `music/engine_base.py`, `music/runner.py`, `music/commands.py`, `music/jobs.py`
  (`running` in `set_waiting_for_card`), `music/acestep.py`/`yue2.py` (VRAM estimates),
  test helpers; `ui/src/lib/utils/musicCreate.ts`
- `providers/llamacpp_server_provider.py` (`stop_local_engine`, `card_holder`),
  `service.stop_local_engine`
- `dpc_agent/memory.py`, `managers/agent_manager.py`, `settings.py` (S6);
  `dpc_agent/knowledge_graph.py`, `sleep_pipeline.py` (S7)
- `process_registry.py` (reuse the readers, S8); `local_api.py` (S10)
- `dpc-client/ui` (`ProvidersEditor.svelte`, `cardQueue.ts` and their tests) and the status
  payload (S5)
- `dpc-client/core/tests/conftest.py` (`_no_card_claims_cross_tests` also clears
  `card_lease._CARD_LEASES`, `_LEASE_LINE` and the yield-task set)

## Implementation Status

S1 to S4 are built (S3 and S4 meet the Whisper card's Done-when, S2 keeps Whisper off a local LLM's
card), with follow-ups 1 to 9 on `dev`; S5 to S8 and S10 are pending and S9 is folded. The order that
was recommended and followed (Mike #68 made S2 part of the first slice): S0, then
S1, then S2, S3 and S4 together (they meet the Whisper card's Done-when and keep Whisper
off a local LLM's card), then S5–S8 and S10. S2 changes the 5,555-line file, so it is
coordinated with the agents editing it rather than postponed.

| # | Commit | Touches | Proves | Status |
|---|---|---|---|---|
| S0 | docs: this ADR (the design proposed an ADR-044 amendment; this record carries the matrix, L1–L12 and the decisions instead), revised after reviews #41–#89, the new count's #10–#15, and Mike #68–#71 and #11 | `docs/decisions/052-…md` | — | This commit |
| S1 | feat(card-lease): lease registry, lease line, one order across both lines (L5a), refusals, wait estimate; `card_holders`/`card_queue` include lease rows through the hook (supervisor rows first, `until_cancelled`); `card_holder()` stays supervisor-only | new `managers/card_lease.py`; `llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_LEASE_WAITERS`, `_live_holders`, `card_holders`, `card_queue`); `tests/conftest.py` | tests 1–3, 6–8, 14 with fake clients only | Built at 53a2eaf5 (2026-11-22); **S1 follow-up (review #54–#57)** in the commit after it (`for_a_peer` in the waiter hook, lease rows in the UI readers, `seconds_from_ratio`, `let_go_by_hand` answers False at its bound, pins; see Review notes, "S1 follow-up"), reviewed and accepted as the base for S2; S1-level parts of tests 3 (b) and 8 only, the rest of 3 (b) and 3 (c) are S2, the 120 s door of test 8 is S3; a third hook `_LEASE_QUEUE_ROWS` added (one lease, several waiters) |
| S3 | feat(whisper): the local model holds a card lease, yields by unloading when idle (`yield_card`, releasing only when the unload ran), loads beside busy music only in the one shared cell when it provably fits (L11), otherwise chooses the CPU twin when its estimate beats the GPU wait (L12, Mike #62, Ark #64) or queues before `_load_lock`; the CPU twin is a separate `local_whisper` provider with `device: cpu` and no lease, selected by `transcribe_with_route`; doors pass `purpose` and their bounds; every caller gets a deadline and the queue's is shorter; a card refusal ends the provider cascade; the peer check runs again after a wait; a peer's transcription waits only when its whole trip fits the guest's 120 s, else is refused at once (L13); a load that fell back to the CPU releases the lease; an agent's transcription inside its own turn on a local alias goes to the twin up to 300 s or is refused at once (3.5); the provider records the real-time ratio per device, the carrier L10's Whisper estimate needs (S3, not S1); the twin starts from L12's table; llama-server children record `predicted_vram_mib`; L11 reads `free` and `total` through `read_vram_mib` and never per-process figures | `providers/whisper_provider.py`; `service.py` (`transcribe_audio`'s bound, the received-voice cascade in `_maybe_transcribe_voice_message`, the peer check), `music/commands.py` (`_music_transcribe_with_whisper`), `tools/transcribe.py` (`transcribe_audio_file`: `purpose`, `on_wait`); `p2p_coordinator.py` (`handle_transcription_request`, `serving_a_peer`); Telegram needs no edit of its own, it reaches the provider through `service.transcribe_audio` (four doors, Forge #20-7); `settings.py` (`queue_wait_s`, `cpu_max_s`, `cpu_max_audio_s`, `cpu_contention_factor`); `ui/src/lib/coreService.ts` (the `transcribe_audio` command's timeout); `llama_server_supervisor.py` (`predicted_vram_mib`, one attribute set at launch); `managers/commit_memory.py` (a function that clears `at` and `free` of the 30 s VRAM reading, never `total`) | 9, 10, 18–24, 26–28, 30–34, 36–38, 40 | **S3a built** (2026-11-23, branch `feat/card-lease-s3-s4`): the producer of `expected_s` (per device: warm ratio by length band, first-call `penalty_s` in seconds and load time since follow-up 2; start figures from L12 and "GPU figures on speech"), the four doors' ask (`purpose`, bound, `expected_s`) carried to `transcribe`, a going supervisor's stop figure in `_estimate`, `queue_wait_s` and item 7's carrier; see Review notes, "S3a build". **S3b built** (2026-11-23, same branch): the local Whisper model holds a card lease on a GPU device (taken before `_load_lock`, the door's ask consumed, `waits_for_engines` by item 1a), yields by unloading, lets go on every unload that ran and on a CPU fallback, the preload as a background call, each door's `on_wait`, the peer's re-check at the grant, the UI's `transcribe_audio` timeout 840 s; until S4 the music runner's old card wait asks an idle Whisper to let go; tests 10, 19 (part), 20 (part), 24, 26, 27, 37 (part); see Review notes, "S3b build". **S3c built** (2026-11-23, same branch): a card refusal (`CardQueueRefusal`, which every `Lease*` refusal is) ends the received-voice walk over GPU aliases, told from an ordinary failure by the field `card_refusal` of `transcribe_audio`'s error answer; a cloud, `remote:` or `device: cpu` alias later in the list is still tried; at the dictation door a refusal takes the owner's OpenAI-then-peer chain as a failure does; the received-voice `check` (queueing condition 3) ends a wait when the sender's transcript arrives; the guest's walk over peers is unchanged (each peer is another card); tests 22, 23; see Review notes, "S3c build". **S3d built** (2026-11-23, same branch): L12 at the seam before the lease is asked (item 1a: L12 first), the CPU twin `<alias>@cpu` (a `local_whisper` object on `device: cpu`, no lease, its own 60 s idle unload, built by its GPU alias and carried by the providers reload, never in the providers list), `RoutedToCpu` and `transcribe_with_route` at the four doors, the RAM check (also for a GPU load that fell back to the CPU), `cpu_max_s`, `cpu_max_audio_s` and `cpu_contention_factor` seeded into `config.ini`, an agent's transcription inside its own turn on a local alias to the twin up to `cpu_max_audio_s` or refused at once (3.5), peers on the twin only with the owner's `let_peers_use_cpu_twin` and within 120 s, the cascade question that writes nothing (Ark #44-2); tests 30, 31, 32, 33, 40 (each in part where noted); see Review notes, "S3d build". **S3e built** (2026-11-24, branch `dev`): one reserve per engine (`5984876e`: Strata's peer gate reads the alias's `vram_reserve_mib`, then its base config, 2048 the fallback; llama-server keeps 2048), L11's one shared cell and live veto at L12's point 1 and at the grant, `vram_mib` by length band (L11 its consumer), `predicted_vram_mib` on llama-server children, the VRAM cache clear, a CPU twin that does not load sends the call to the line (Johnny #63-5), Zcode (iii) and (iv), the UI timeout's carrier pinned; tests 18, 28 and five more; see Review notes, "S3e build". The cell was dormant until S4 and is live since: S4 gave it its production pair, 5600 MiB for YuE2 and 9300 for ACE-Step. First slice |
| S4 | feat(music): music engines hold the card lease (acquired before `_the_lock`); the runner waits through it and names lease holders; a `running` job may wait for the card (both gates: `jobs.py` and the runner's wait loop) | `music/engine_base.py`, `music/runner.py`, `music/commands.py` (start by hand), `music/jobs.py`, `music/acestep.py`/`yue2.py`, test helpers, `ui/src/lib/utils/musicCreate.ts` | **4 (Done-when)**, 11, 13, 15, 17, 29 | **S4 built** (2026-11-24, branch `dev`): each music engine holds `music:<key>` (kind `music`, declared peak 5600 / 9300 MiB, `engine` yield, `waits_for_engines`), the lease first and `_the_lock` second (a start asks with `wait_s=0` before the lock, `card_busy` naming the holder), let go on every end of the child, kept across a yue2.cpp restart for boot options; the runner's job holds it through `in_use` (one use, from before `start_job`), waits queued with `card_wait` naming the holders, until cancelled; a running job whose child ended asks again and waits `running` (both gates); the S3b interim card wait is gone; L11's shared cell has its production pair; the queued job's expected time with YuE2's factor by length (audio seconds per wall second, so wall time is the estimate divided by it, since follow-up 9b); the start by hand (refused by any other holder in S4; since follow-up 9b it may unload an idle holder that has no queue, and the other music engine is stopped only after the card is granted); the UI shows a running job's wait. Tests 4, 11, 13, 15, 17, 21, 29, 34; see Review notes, "S4 build". First slice |
| S2 | feat(card): llama-server and Strata wait for a busy lease and unload an idle one, with the swap on or off; a lease contender swaps an idle supervisor on the owner-start path, reading the parked count first (2.4); one order across both lines; an inference peer waits behind a lease only once ADR-044's amendment of 2026-11-18 is built (L13) | `llama_server_supervisor.py` (2.2 items 3–13), `providers/llamacpp_server_provider.py` (`stop_local_engine`, then `card_holder` may return lease rows) | 5, 16, 35, 39, and the full list of existing card tests green | Built at 1e57932f (2026-11-22), in review; also the supervisor side of tests 3 (b) and 3 (c), test 6 extended to every new S2 path with no lease registered, and `strata_provider.py`'s peer refusal worded without a lease's name. ADR-044's amendment of 2026-11-18 is still not built, so an inference peer keeps today's refusal while a lease is busy (L13). `card_holder()` stays supervisor-only (the Stop engine button is unchanged; S5). Readings of this record taken in the build: see Review notes, "S2 build". **S2 follow-up (review #75–#91)** in the commit after it: see Review notes, "S2 follow-up" |
| S5 | feat(ui): lease rows and lease waiters in the existing holder and queue lines, "until cancelled", unknown ETA as "—"; whether the block shows without a local LLM provider; the optional "let peers use the CPU twin" setting beside the device selector of a Whisper provider (Mike #71) | `ProvidersEditor.svelte`, `cardQueue.ts`, `cardQueue.test.ts`, `MusicCreate.svelte`; the status payload | 25 and manual | Pending |
| S6 | feat(memory): the embedder joins the lease (GPU for rebuilds, CPU when skipped), with a real unload | `dpc_agent/memory.py`, `managers/agent_manager.py`, `settings.py` | fake-thread client tests | Pending |
| S7 | feat(graph-ner): GLiNER gains an unload and joins the lease | `dpc_agent/knowledge_graph.py`, `sleep_pipeline.py` | same | Pending |
| S8 | feat(card-probe): foreign GPU use from the process registry sampler's PDH/NVML readings (no new nvidia-smi), own pids and desktop excluded, unreadable is never free and says it was decided without knowledge of the card | new `managers/card_probe.py`, `process_registry.py`, `managers/commit_memory.py` | 12, 36 | Pending |
| S9 | — folded into S1–S4 (Mike #68): Whisper waits for supervisors in the first slice; no shared cell with a local LLM | — | — | Folded |
| S10 | feat(local-api): external lease with a 90 s TTL and heartbeat | `local_api.py` | HTTP tests | Pending |

**S10, ComfyUI notes** (Forge #33, `C:/ComfyUI-030` v0.37.0; CC decides in S10). `POST /free`
works on an empty queue but needs a JSON body (`server.py:1196`) and answers 200 before the card
is free (worker thread, gc interval 10 s, `main.py:384-404`): the lease confirms release by
`nvidia-smi`, never `/system_stats`. A global `/interrupt` can hit the next job: use `prompt_id`
or `/api/jobs/{id}/cancel`, and queue delete or clear. Under DynamicVRAM (default on NVIDIA) it
has no stable peak, only a `--vram-headroom` promise (ours is 0): its L11 term differs in kind.

### Backlog cards

**Closes or advances**

- `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…` (161): part (1), the queue, closes with S1+S3+S4
  (+S2). Part (2) (GPU choice and CPU fallback) is now mostly S3 (L12, the CPU route); its
  remainder against supervisors joined the first slice (Mike #68). Part (3) (per-use
  models) stays open. Step 5 of the Whisper plan gains the CPU measurement.
- `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-…` (1524): this ADR is its option (b)
  in-process plus (c) as S8. The HTTP surface in `local_api.py` (S10: a lease with a 90 s
  TTL and heartbeat, `kind="external"`) closes it. That card's Done is not met until S10.
  Record that this answers ADR-051 **N11** as "(b)+(c)"; Mike decided on 2026-11-22 that it
  is in scope (Decided by Mike).
- `THE-WHISPER-ALIAS-IS-THE-BACKENDS-LAST-STANDING-CUDA-RESIDENT` (7366): can close after
  S3, together with b796af4a.
- `THE-EMBEDDING-DEVICE-IS-A-STANDING-CHOICE-…` (7507) and
  `THE-EMBEDDING-MODEL-HOLDS-GPU-MEMORY-…` (14490): S6 is their mechanism.
- `A-NODE-HAS-NO-ONE-SWITCH-…` (11770): gains the `lease`/`auto` value.
- `ONE-CARD-ONE-OWNER-…` (7335): partly (S8, S10). L11 is the first measured exception to
  one owner, and the card should record it.

**Changes**

- `THE-CARD-QUEUE-AND-THE-LAST-SWAP-ARE-NOT-SHOWN-ANYWHERE-IN-THE-UI` (now at backlog line
  1967; the first draft said 1925) is **stale in its title** (Zcode #41): the holder and the
  queue have been shown in the Local engines block since `01932d09`, and 12 frontend files
  read the card keys. What it still says truly is the last swap: no screen reads
  `last_swap` (grep of `dpc-client/ui/src`: none). Re-word it to the last swap, and point
  the lease rows at S5.
- `THE-CARD-HOLDER-AND-ITS-STOP-BUTTON-SIT-INSIDE-EACH-STRATA-ALIAS-…` (2078): the holder
  list is now multi-kind.
- `THE-ADMISSION-ARITHMETIC-SIZES-ONE-CHILD-AND-CANNOT-SEE-THE-OTHERS` (7119): L11 uses
  the lease estimates.
- `THE-CARD-QUEUE-TESTS-LEAVE-PENDING-TASKS-…` (1550): the new tests must assert no pending
  tasks.

**New cards to file**

- (a) `music/runner.py:665-667` treats an unreadable holder list as a free card.
- (b) `_free_vram_mib` (`llama_server_supervisor.py:1793`) cannot tell no nvidia-smi from a
  failed read.
- (c) `EmbeddingProvider.unload` frees no device memory.
- (d) GLiNER has no unload, so it cannot yield.
- (e) A PDH-based foreign threshold needs a desktop ignore list (dwm at 2.7 GiB).
- (f) The embedding cache key carries no device, so fp16-GPU and bf16-CPU vectors share it
  (Zcode #41).

**Documents**

- **A SPEC decision 32 amendment is needed** (Mike #70): music swaps out an idle server
  when nothing waits, with the swap on. File: `ai-studio/experiments/music-player/SPEC.md`.
  This record does not edit it.
- ADR-044 and ADR-051 are not edited by this record. If accepted, ADR-044 may get a short
  pointer to it.

## Decided by Mike

Recorded as decided; each line names the message.

- **#20 (2026-11-21):** llama-server and Strata wait for Whisper or music to free the card;
  the embedder and other GPU-capable parts join later.
- **#62 (2026-11-21):** a smart GPU/CPU choice for dictation and transcription (L12).
- **#68 (2026-11-21), decision 1: no asymmetry.** Whisper does not load beside llama-server
  or Strata; the exclusion is symmetric in the first slice, which therefore includes S2
  (section 3.3). The "loaded beside" line for supervisors goes with the exception. A
  dictation during an agent turn goes to the CPU twin when the file is up to 5 minutes
  (#71); a longer one waits and may be refused at the dictation bound (Zcode #69).
- **#70 (2026-11-21), decision 3 (peers): not refused outright.** A peer waits when the
  line is short and is refused when it is long, by the mechanism inference sharing already
  has: the peer bound `_a_peers_slot_wait_s()` (L4). Background calls stay as decided. For a
  transcription the limit became the guest's 120 s at #11.
- **#70 (2026-11-21), decision 4: SPEC decision 32 amended.** With nothing waiting and the
  server idle, music unloads the server and takes the card, by the owner-start rule and
  gated like it by `swap_when_idle` (section 3.2). The SPEC file needs the amendment
  (Documents).
- **#70 (2026-11-21), decision 5 (unreadable probe): answered by reuse.** Mike asked how
  the read can fail when the agent map already collects VRAM. It reads the same helpers;
  the probe now reuses them (section 5), and the residual failures (no NVIDIA, a driver
  hang or reset, a timeout, unparseable output, the backend stopping) keep the default:
  background refused, foreground on with "decided without knowledge of the card". Mike
  did not object to that default; his explicit OK was not given.
- **#71 (2026-11-21), decision 6: the CPU takes files up to 5 minutes**, even while Strata
  is busy: `cpu_max_audio_s` = 300 s, the contention factor applies to the estimate, the
  Strata check is a condition of the choice (L12 point 3), and a call that started on the
  CPU stays there (Zcode #69).
- **#71 (2026-11-21), decision 6G: residency.** "Keep a model in memory only if there is a
  queue for it." The CPU twin gets its own short `idle_unload_s` (proposed 60 s).
- **#71 (2026-11-21), decision 6C: peers on the CPU twin, no by default**, with an optional
  setting in the provider UI beside the CPU/GPU device selector (S5).
- **#85 (2026-11-21): a peer's wait is not a new branch.** CC #84 had called it one. Mike:
  there are cards in the backlog and the ADR describes it. It is ADR-044's amendment of
  2026-11-18, which he decided on 2026-11-09 and 2026-11-17; CC #87 agreed. This record builds
  on it (L13).
- **#11 (2026-11-21 17:13, new count), his answers to items 10–13:**
  - **Item 10: "ok 120".** A peer's transcription is served at once when it can be, and
    otherwise waits only when its whole trip fits the guest's 120 s; else it is refused at
    once (L13, point 2, in Ark #13's and Forge #15's framing). Item 8 closes with it: an
    unknown estimate cannot be shown to fit, and waiting it out would answer a guest that
    had left. CC's reading of the answer; Mike may overrule it.
  - **Item 11: not told** ("why would it need to know?"). Section 2.4.
  - **Item 12: "look at our flow".** Not a decision: CC read the flow (section 3.5). Only the
    agent tool transcribes inside a turn; on a local alias it takes the CPU twin up to 300 s
    and is refused at once beyond, with no estimate and no seed, from #68, #71 and the line
    below.
  - **Item 13: "ok".** The VRAM reading through the 30 s cache, cleared on every grant and
    release (L11, "The cache, exactly").
  - **The governing line:** "don't overcomplicate for now; we'll learn the timings and the
    queue in operation; the music player needs transcription too." Read as: start values
    ship as proposed and are tuned from operation; the music player is a transcription caller
    (section 3.5, L4).
- **2026-11-22, item 7: the GPU Whisper alias's idle unload stays at 300 s** ("ok 300s"),
  because a reload costs 0.6–0.9 s (Forge #21; CC on speech, 2026-11-22: 0.76–0.83 s) and a
  cold load 1–2.4 s (12.8 s in a fresh process with library init), so holding the model costs less than a second when it is wanted again.
  **The carrier (Mike #9 and #15, 2026-11-23):** the default is in code (`settings.py`, the
  default block, `'idle_unload_s': '300'`) **and** a key `[local_transcription] idle_unload_s`
  is written in `config.ini`; the key in the file wins over the code default, the pattern used
  in many places already. S3 builds the writing of the key.
- **2026-11-23, items 1a, 2, 6A and 9: accepted at their proposed defaults** (Mike #15, "по
  дефолтам"; CC's proposal #14, confirmation #16).
  - **Item 1a, an idle supervisor in front of a dictation:** L12 first (a file up to 5
    minutes goes to the CPU twin); a longer file, with the swap on and nothing waiting, swaps
    the idle supervisor out by the owner-start rule, like music. With the swap off it waits.
  - **Item 2:** lease yields (unloading Whisper, stopping an idle music engine for another
    lease) are not gated by `swap_when_idle`. Music swapping an idle supervisor still is.
  - **Item 6A:** `[local_transcription] queue_wait_s` = 600 s.
  - **Item 9:** `cpu_max_s` = 600 s and `cpu_contention_factor` = 1.0, as start values, tuned
    from operation.
- **Mike 2026-11-23 (AI Studio group): the agent tool's wait without a turn hold is capped at `queue_wait_s` (600).** With a turn hold the smaller of the two wins. Replaces the open question (Ark #4: 600 or ~1790 s). Built in `transcribe.py` `_card_wait_bound_s`, the door's `bound_s`; `card_ask` is unchanged.
- **2026-11-23, one VRAM reserve per engine, from the engine's own config** (Mike #7: "one
  source of truth, the one that is set"; #15, "do as you proposed"). Strata reads the alias's
  `vram_reserve_mib` (live 1152), with 2048 as the fallback; llama-server keeps
  `_DESKTOP_RESERVE_MIB` = 2048. Detail in L11, "One reserve per engine".
- **#17 (2026-11-23), a peer's long audio:** L13 states it as a rule: a peer's transcription whose
  whole trip does not fit the guest's 120 s (`PEER_TRIP_S`) is refused at once, including long
  audio that would go to the CPU twin (1.1–1.5×: 300 s of audio takes ≈200–270 s). L13, point 2.
- **2026-11-24, Mike's answers Q1–Q6 and the peer's swap** (S3/S4 follow-up 8):
  - **Q1, no:** item 1a stays: behind an idle Strata a long file's wait reads as long; no
    swap-cost pricing.
  - **Q2, yes:** a CPU twin already running a transcription, or a running llama-server, counts as
    CPU contention (L12 point 3, read as "computes something now"); **narrowed later that day, see the
    entry below.**
  - **Q3, yes:** `cpu_max_s` or `cpu_max_audio_s` at 0 turns the CPU route off (L12 point 2).
  - **Q4 and Q5, yes, already built:** the three L12 keys are seeded into an existing
    `config.ini`; the base config's `--vram-reserve-mib` is a reserve layer.
  - **Q6:** L11 decided; built in S3e, live since S4.
  - **A peer may stop an idle engine:** on ADR-044's owner terms of 2026-11-06, one predicate for
    both lines, the stop figure counted in the peer's 120 s (L13, "As built").
- **2026-11-24, later in the same chat (#84 of the new count; built in follow-up 9b):**
  - **YuE2's factor is flipped:** audio seconds per wall second, wall time = the song's length divided by
    it (L10); the job's timeout is left as it is.
  - **A start by hand may unload anything idle that has no queue:** an idle Whisper and, with the swap on,
    an idle Strata or llama-server (3.2).
  - **A peer may stop an idle engine only after 60 s of idleness since the owner's last request on it**
    (L13, "As built").
  - **Only small CPU work is contention:** Whisper on the CPU, the embedder and GLiNER; a llama-server kept
    off the card is not counted (L12 point 3).
- **2026-11-22, N11: in scope, and CC writes S10, the ComfyUI client half included.** The yield
  verb for ComfyUI is its own `POST /free {"unload_models": true, "free_memory": true}` plus
  `POST /interrupt` (Forge verified it live, 24.9 → 1.8 GB). Order: after S1.
- **2026-11-22: CC measures the GPU ratio** Forge asked about (#21), on real speech.

## Open for Mike

Still open after #68–#71, #11 and the decisions of 2026-11-22 and 2026-11-23. Each has a
proposed default.

- Items 1a, 2, 6A and 9 were decided on 2026-11-23 at their proposed defaults (Mike #15; all in
  Decided by Mike).
- **L13, a peer's long audio on the CPU twin** (Forge #12, Ark #10): **decided** by Mike #17,
  2026-11-23: refused at once as a rule (Decided by Mike; L13, point 2).
- Items 10, 11 and 13 were answered at #11; item 12 by reading the flow, as he asked; item 8
  closes with item 10; item 7 was answered on 2026-11-22, its carrier on 2026-11-23 (all in Decided by Mike).
- **The reference for the GPU real-time ratio** (Forge #21, §5): **closed.** Mike's answer,
  2026-11-22: CC measures it on real speech; done, ≈16–43× (L12, "GPU figures on speech").

Also open, and not numbered: whether `auto` means `lease` on CUDA for the embedder and
GLiNER (section 3.4); whether L11 should also spare an *idle* music engine its stop when
Whisper fits beside it; and, outside this ADR, whether Ark writes the track into his knowledge base (CC
#75 D).

## Open Questions

- **Q1:** Whether any agent tool calls the LLM while a transcription use is open (a
  `LeaseWouldWaitOnItself` risk). **[unverified]** Live again in the first slice, since
  Whisper waits for supervisors (Mike #68). — CC
- **Q2:** Answered (section 2.3): no test resolves a real CUDA device; two build `mlx`
  providers, which get a lease that must stay harmless.
- **Q3:** Whether the PDH counter is vendor-neutral on AMD and Intel. **[unverified]** — CC
  Since the fifth revision L11 does not read PDH at all, so this no longer decides a fit; it
  still decides what S8 can name on Windows.
- **Q4:** Answered (section 0): the frontend reads the card keys in 12 files.
- **Q5:** The prior-art lines in (b) above are from general knowledge and unread here. —
  CC
- **Q6:** Whether `nvidia-smi --query-gpu=memory.free` reads correctly under WDDM while a
  music child runs (L11's declared check reads `memory.total` and its live veto
  `memory.free`; only the per-process query is known to read `[N/A]`).
  **Partly answered (fifth revision):** the pair reads at device level on this Windows machine
  with the card idle ("32298 MiB of VRAM free", `total` 32623 MiB, `dpc-client.log`
  2026-11-18 15:06:42). Whether `free` falls by the child's real use while a music child runs
  is still **[unverified]**. — CC
- **Q7:** The reload times behind decision 2's cost difference (Whisper vs an idle music
  engine). **Whisper's side answered (Forge #21, 2026-11-21; CC on speech, 2026-11-22):** GPU
  reload 0.6–0.9 s, cold load 1–2.4 s as the load step and 12.8 s in a fresh process, measured
  outside the backend (L12, "GPU figures", "GPU figures on speech"); an idle music
  engine's reload is still **[unverified]**. The backend itself has not loaded Whisper on the
  GPU: the alias was
  configured `device: cpu` until 2026-11-20 and `cuda` since 2026-11-21 21:41 (the init line
  prints the configured device, `whisper_provider.py:76, 121`), and no load is logged
  since. The core venv's torch is CUDA-capable: `2.11.0+cu128 12.8 True` (`torch.__version__`,
  `torch.version.cuda`, `torch.cuda.is_available()`, read 2026-11-22 with `uv run
  --no-sync`). So the GPU route waits for a first run, not for a torch build. — CC
- **Q8:** The CPU twin's figures. Speed: L12's table ships as the start (#66, warm rows, one
  cold row). Unmeasured: host RAM beside a resident Strata (Forge #15 read 8.3 GB available
  at 86 % load with Strata on) and the contention figures. Learned in operation.
  **[unverified]** — CC
- **Q9:** Whether the KV type a llama-server child was started with is readable on the
  supervisor at launch, so `predicted_vram_mib` can be recorded without re-running the
  ladder (the ladder writes it to the fit memo, 3514–3639). **[unverified]** — CC
- **Q10:** Whether Strata computes missed experts on the CPU in parallel with the GPU.
  Host-RAM residency is documented here (the resident budget); the CPU computation is
  Ark's reading of `strata_details.md`, which is not in the dpc-messenger repo.
  **[unverified]** — CC
- **Q11:** Whether the host's transcription handler is cancelled when the guest disconnects
  while its request waits in `_LEASE_LINE` (L13, point 3). Not read: `handle_transcription_request`
  has no disconnect check, and the transport's behaviour was not traced. **[unverified]**
  The form to copy for the waiter is the existing queue's `except BaseException:
  request.leaves()` (L13, point 3). — CC
- **Q12:** Multi-GPU machines. `_free_vram_mib` and `_total_vram_mib` read the first line of
  `nvidia-smi --query-gpu` (`llama_server_supervisor.py:1807, 1825`), so a second card is
  invisible and L11 may judge the wrong one. Nothing in DPC chooses a GPU index that this
  record found. **[unverified]** — CC

## Review notes

Reviewer claims that did not hold when checked against the code at `5099b326`, and what
was done instead. The underlying concern is kept wherever it stands on its own.

- **Ark #44, finding 2: "`_begin_swap_drain` (2583–2611) cancels `_start_task` and
  releases `_start_lock`."** Not so. `_begin_swap_drain` is at 2681–2686 and only sets
  `_swap_for` and `_draining`; there is no `_start_task` in `llama_server_supervisor.py`.
  The concern stands anyway: after the mark a lease admits no new use (section 2.2.1).
- **Ark #44, finding 2: "`_begin_swap_drain` and `_parked_conversations` are not in the
  ADR."** Both were in the first draft's surface list (section 0 and 1.1). What was missing
  was their semantics for a lease; section 2.2.1 now gives them.
- **Ark #44, finding 2: "`_will_not_reopen` (via `_check_a_wait_can_end`) must be defined
  for a lease, or a waiting start meets `EngineDrainingError`."** Not so. Both are called
  only on the waiting supervisor itself (`self._check_a_wait_can_end` at 2877 inside
  `_wait_to_be_first`; `self._will_not_reopen()` at 2784 and 2833), never on a holder; a
  grep of every `holder.<private>` read finds `_begin_swap_drain`, `_busy_for_the_card`,
  `_draining`, `_end_swap_drain`, `_parked_conversations` and `_swap_for`, nothing else. A
  lease waits in `_LEASE_LINE`, not in `_CARD_QUEUE`, so it is never `self` there. What a
  waiting start does read on a yielding lease is `holder._draining`, which section 2.2.1
  defines as True for the whole yield, so the start waits instead of failing.
- **Ark #44, finding 2: the "single synchronous stretch" breaks with `await
  give_up_for`.** Partly. The stop of a supervisor holder is already awaited between the
  mark and the next round (`_wait_for_the_stop`, 3091), and each round re-checks the
  holder, so the invariant covers check-busy-mark and last-check-claim, not the stop. The
  window Ark names is real for a lease (another lease could be granted in it); L5a closes
  it: only an earlier arrival can be granted there, and the next round finds it as a new
  holder rather than a `_claim_card` refusal.
- **Ark #44, finding 3: "`_busy_for_the_card` (2505–2517) iterates
  `holder._uses_on_the_card()` and skips `claim.process is None`, reading
  `claim.hold.arrival` and `claim.begins_a_request`."** Not so. Lines 2500–2522 are
  `_blocking_holders`, `_blocking_holder` and `_card_held_error`. `_busy_for_the_card` is at
  2668 and reads `state == "starting"`, `self._in_flight`, `_CARD_HOLDS[alias]` with
  `_stands_before_the_line` and `_waits_for_the_slot_of`, and
  `_callers_in_their_window`; `_uses_on_the_card`, `begins_a_request` and `claim.process`
  do not exist in core. Because it is a method called on the holder, a lease defines its
  own; the real point (a busy lease must never read as idle, and uses must be ordered by
  arrival) is kept in section 2.2.1.
- **Ark #44, finding 4: "the runner silently swallows the `MusicJobError`."** The other
  way round: `runner.py:671-676` re-raises it unless the job was cancelled, so a mid-job
  wait would **fail the job**, not hide the wait. The fix Ark asks for (allow `running`)
  is adopted, and the frontend's `queued`-only `cardHolders()` is widened with it.
- **Ark #44, finding 5 and 6 and 7 cite text the first draft does not contain** ("S11 …
  the job's state stays running", a matrix row at ":400" about an agent turn on
  llama-server, "§6", `excludable=True`, `LocalEngines.svelte`, "the same keys `uses` and
  `eta`"). None of these strings is in `f481bf3a`. The points behind them were checked on
  their own and adopted where they hold (sections 3.3, 2.2 item 3, 4).
- **Ark #44, finding 7: "holder rows go to two places; `otherCardHolderLines` runs on
  per-engine status, so a lease row would show under one engine only."** Not so. The
  `cardStatus()` snippet (`ProvidersEditor.svelte:2375`) renders once, at 2538, from the one
  `engineStatus`; there is no per-engine copy and no `LocalEngines.svelte`. What is true:
  that one block renders only when a `strata` or `llamacpp_server` provider is configured,
  and `card_holder` (`card_holders()[0]`) feeds the Stop engine button; section 4 handles
  both. The pinned source strings in `cardQueue.test.ts:90/104` are confirmed.
- **Ark #45, finding 4: "`_note_swap(..., parked=...)` prints the count, so a lease would
  read 'parked 0'."** Not so. `_note_swap` (3141) uses `parked` only to compute `dropped =
  holder.DROPS_PARKED_ON_STOP and parked != 0`; with `DROPS_PARKED_ON_STOP = False` it
  prints no parked words. The call to `_parked_conversations()` is unconditional (3123),
  as Ark says, and S2 item 5 now skips it for a lease; the toast title is set per kind.
- **Ark #45, finding 1** concerns commit `c8782ec4`, not this ADR. The code no longer calls
  `worker.exception()`; `5099b326` ("a cancelled worker task cannot free the model under its
  still-running thread") addresses it. The ADR keeps the lesson as a Risk.
- **Zcode #41: "card 1925 is stale."** Half. The holder and queue parts are shown since
  `01932d09`; the last swap is still on the wire and read by no screen. The card is
  re-worded, not closed (Backlog cards).
- **Zcode #41: "S3 threatens no test."** Almost. No test resolves a real CUDA device, but
  two files build `mlx` providers, which get a lease under section 3.1 (section 2.3).
- **CC's chat claim at #51 that `[voice_transcription] timeout_seconds` = 240 limits the
  owner's dictation** was wrong (Ark #53, confirmed): its one consumer, `service.py:5102`,
  is a receiver's passive wait for a sender's transcript. The local path
  (`service.transcribe_audio`, 4615–4657) and both Telegram call sites have no deadline at
  all; L4 and section 3.1 now give them one. The first revision of this record repeated the
  claim in section 3.1; it is corrected there.
- **Ark #53, minor:** the line numbers given for `_excludes` and `_blocking_holders` were a
  few lines off at `5099b326`; the content is as Ark says. Ark's figure of 1800 s for `swap_wait_s` is this machine's `config.ini`
  value (section 0), not a default.
- **Zcode #52 vs Ark #53 on how to compute the fit** (actual use plus peaks vs declared
  sizes): not an error on either side; reconciled in L11 as declared peaks against total
  with a live reading that may only veto, and any unknown queues.
- **Mike #70 on the probe ("how can nvidia-smi fail, the agent map collects VRAM"):**
  checked. The agent map's free/total VRAM is `commit_memory.read_vram_mib()` (135), a 30 s
  cache over the same `_free_vram_mib`/`_total_vram_mib` nvidia-smi helpers; its
  per-process VRAM is the registry sampler (2 s, PDH/NVML, no nvidia-smi). So the agent map
  can fail the same ways; the probe reuses it rather than adding calls (section 5).
- **Mike #71 ("we already did it, see the logs"):** checked. `~/.dpc/logs/dpc-client.log*`
  show the Whisper alias initialised and loaded with `device=cpu` from 2026-11-14 to
  2026-11-20 (e.g. "Whisper model loaded in 1.0 seconds (device=cpu)", 2026-11-20 08:42).
  Whether those runs overlapped a busy Strata turn was not checked.
- **Ark #64, checked.** §1 (length vs time) and §2 (cold start) are design points, adopted
  in L12. §3: Strata's experts live in host RAM (confirmed: the resident budget in
  `docs/GLOSSARY.md`, `ProvidersEditor.svelte:2366`); that it computes missed experts on
  the CPU is from `strata_details.md`, which this repo does not contain (Q10), so the
  contention rule rests on Ark's reading until measured. §4: confirmed, `hand_over_to`
  (700), `_copy_state_to` (685) and `_settle_finished_load` (692) move one model state;
  the second-pipeline design of the previous revision is removed. §5: confirmed, and
  sharper than stated: the running job has no finer progress than per finished variant
  and no start time, so its ETA is unknown in v1, not merely mis-estimated.
- **Ark #60 §4: "with a mid-job wait in state `running`, `_wait_for_card` would report the
  card free and start the engine over a holder."** The gate is real (`runner.py:661-662`,
  `state != QUEUED` → `return False`), but False means "the job was cancelled" (its
  docstring; `run_job` returns at 591 without starting anything). The failure would be a
  running job dropped as if cancelled, not an engine started over a holder. The fix Ark
  asks for (both gates accept `running`, and a test that a running job behind a busy card
  does not start its engine) is adopted in section 3.2 and test 29, with the job also
  pinned as not dropped.
- **Ark #60 §5.2 (desktop counted twice):** not the case in the revision Ark reviewed
  (section 5 already said the ignore list subtracts nothing), but L11 did not say it
  itself. It now names the reserve as the one desktop figure.
- **Ark #60 §2 (fourth outcome of `_finish_thread`):** confirmed by reading `d80d76f7`:
  the shield loop catches only `CancelledError`, so an exception from a worker future whose
  `run` never executed escapes. Ark measured it with a probe; this record did not run it.
- **Zcode #59:** `_CARD_CLAIMS` loses entries in three places (667, 3180, 5440), not four;
  section 0 corrected.
- **Johnny #83 §2: "everything in the agent map is a sum over processes; nvidia-smi lives only
  in `device_context_collector.py`".** Not for the card figures. `list_processes` returns
  `vram_free_mib` and `vram_total_mib` from `read_vram_mib()`, which calls `_free_vram_mib` and
  `_total_vram_mib`, that is nvidia-smi (`service.py:12132-12144`, `commit_memory.py:135-156`,
  `llama_server_supervisor.py:1793-1827`). PDH is the per-process figure only.
- **Johnny #83 §2, the two biases of PDH.** "Can exceed what the card holds" is true
  (`process_registry.py:36-38`, `2015-2017`). The "lower bound ... at most one interval" he
  quotes (`:32-34`) belongs to the CPU and IO paragraph, not to VRAM; the VRAM figure's own
  limits are a running peak, sampled every 2 s, for engines only (`2349-2359`, `2425-2426`).
  The concern behind it, name the quantity and say how bias is handled, is adopted: L11, "The
  quantities".
- **Johnny #83 §3: "llama-server saves the live conversation before a stop, Strata does not".**
  Not as written. `drain` saves through `_save_live_conversation` for any supervisor
  (`llama_server_supervisor.py:5390`), and ADR-044 measured a stop save on Strata (line 1883).
  What no save writes is Strata's **parked** conversations (ADR-044 line 1877). The point that
  the swap line must name the engine and the cost is adopted (section 2.4).
- **Johnny #83 §4 and CC #84: "a peer waiting is a new branch".** It is ADR-044's amendment of
  2026-11-18 (Mike #85, CC #87), unbuilt. What is new is only the transcription exchange and
  the guest's 120 s (L13).
- **Johnny #83 §6, `cpu_contention_factor` = 1.0 is a stand-in:** already so in L12 ("proposed
  1.0 until measured"); the log evidence shows the CPU route worked, not that it cost the
  turn nothing. Unchanged.
- **Ark #82 §2, an agent's transcription inside its own turn:** adopted as section 3.5. His
  "L3 means refusal, not waiting" is right for a file over 300 s and for a twin that cannot
  run; for a file up to 300 s the answer is the CPU twin (Mike #71).
- **Ark #82 §1, Whisper's reload time:** unchanged here. His log lines show `device=cpu` on
  this machine, so the idle-unload default (item 7) waited for a GPU figure; Forge measured
  it at #21 and Mike decided 300 s on 2026-11-22.
- **Review of `c3852d75` (new count #12–#15), what held and what changed.** Zcode #12: ready
  for S1, four nits, all fixed (the response's success fields, `swap_wait_s` 300 by default
  with 1800 from this machine's `config.ini`, the history range, the order of Q9). Johnny #14
  obj. 1, L11 and L13 contradicting each other on peers: right; one rule now, in L13, with
  L11 pointing to it and test 37 pinning the refusal. Johnny obj. 2, Ark #13, Forge #15 on item
  10: right, `min()` was always 120 s and 120 s is the whole trip (L13). Johnny obj. 3, Ark #6–#8,
  Forge §1 on the cache: right; the fifth revision's "one failure cached too" held only for a
  helper that returns None (L11, "The cache, exactly"). Johnny obj. 4: adopted (2.4). Johnny
  obj. 5, Ark #5, Forge §6–7: adopted in shape; the in-turn rule no longer rests on the factor,
  the words promise nothing, one RAM figure is kept (3.5, L12). Ark new #1–#3: confirmed
  (`p2p_coordinator.py:110, 1119, 1135`; `whisper_provider.py:282-291`) and written in (L13,
  3.1, test 10).
- **Ark #13 new #2, "S1 must choose single-use or multi-use":** multi-use, not the simpler
  single use, because the code says so: the provider runs parallel calls on one model and L1's
  re-entrant `use()` relies on it; a single-use Whisper lease would also make a turn's second
  parallel transcription look like a wait on itself. L11 counts one peak; the rest is learned.
- **Ark #13 new #4, "`device=cuda` never appeared because the venv's torch is CPU-only":** not
  so. The core venv's torch reads `2.11.0+cu128 12.8 True` (2026-11-22, `uv run --no-sync`).
  The `device=cpu` lines were the configured device, printed at init from the config
  (`whisper_provider.py:76, 121`); `providers.json` says `cuda` since 2026-11-21 21:41 (Q7).
  Whether the torch build was the same on 2026-11-20 was not checked.
- **Forge #15 §2–3, "`read_vram_mib` answers bytes and falls back to `device_spec["vram_mb"]`
  or `DPC_DEVICE_VRAM_MB`":** not so. It answers MiB (`nounits`, `int(...)`,
  `llama_server_supervisor.py:1793-1827`) and has no fallback (`commit_memory.py:135-156`); no
  function in `dpc-messenger` or `ai-studio` carries either name (grep, 2026-11-22). The point
  kept: L11 never takes VRAM from a function that answers bytes or a configured figure.
- **Forge #15 §4, "L11 measures `free − reserve`, Strata `total − reserve`":** half. L11's
  declared check is `total − reserve`, as Strata's; only its live veto reads `free`, with no
  reserve (L11, "One reserve").
- **Review of `ffd06369` (new count #18–#22).** All four call it a base for S1 after text fixes
  (Zcode #22). Held, and written in without new mechanism (Mike #11): `need` mixed bytes with MiB
  (it ran toward "does not fit always", so a test awaiting only a refusal passes on the bug; test
  28 pins the value); the 1740 was the backend's `bge-m3` + GLiNER residency, not Whisper's; the
  ratio had no carrier; `free` is not a wall under WDDM; the estimate must come from the routed
  device; "unknown" has two doors; verbatim refusals and the missing cancel frame are accepted;
  0.0 duration, one RAM instrument, the cache reset's scope, two live runs, the logged size
  source, multi-use counted once per model, Strata's missing figure marked unverified, four doors
  not five. **Did not hold as written:** Zcode's `error=str(e)` in `p2p_coordinator.py` (the line
  cited is the access-denied string; `str(e)` is a later branch of the same handler); Forge's
  `_realtime_ratio` call (it is called from the two paths of `_transcribe_loaded`, not where
  cited); several line numbers had drifted by one to four (in `transcription_handler.py`, at
  `_get_audio_duration` (removed in S3 follow-up 4), at `_current_owner`, in
  `llama_server_supervisor.py`); the names hold, the numbers did not (S3 follow-up 6, Ark #49-5,
  keeps the names only). **Withdrawn:** Forge's #15 §2–3 (bytes and a
  `vram_mb` fallback in `read_vram_mib`; the division he remembered is `whisper_provider.py:954`,
  a log line) and Ark's #13 torch claim; Forge's item-7 vote moved from 60 s to 300 s after he
  measured the reload.
- **Review of `ffc81e00` (new count #30–#33).** Held: Whisper's `eta_s` was written as a
  product while the ratio is audio seconds per wall second (Ark #30-1, Zcode; the CPU table's
  ≈1.0–1.1× hid it), now `duration / ratio` with every orientation stated (L10); two deadline
  formulas for a peer became one (Ark withdrew his own 60 s floor); the v1 reason cited a closed
  Q7; §3.3 still carried the cancelled 1740; the flat `whisper_peak` is a named choice; Mike
  #68 is an exclusion before arithmetic (Forge's Strata peak for `does_not_fit` is not needed);
  the production pair is two pairs. Recorded: the silent CPU fallback, the contention factor,
  the twin RAM constant, the revision-less call site, the `gguf_path` check, the live run's log
  fields, the split `[unverified]`, the 43× row, Forge's ComfyUI facts. **Did not hold:**
  Johnny #32-3, that `× factor` counts YuE2 twice: `estimate_yue2_seconds` is **audio**
  seconds (the form compares it with the duration cap, `shortCapWarning` in `musicCreate.ts`; the test pins
  80 for six lines), so the product stood **[superseded 2026-11-24: the factor is audio seconds per wall second
  and wall time is the estimate divided by it (L10); that the estimate is audio seconds stands]**; kept from Johnny: no code records the factor yet
  (3.5 is a start value; S4 records it).
- **The first draft's own errors** found on the way: `_CARD_CLAIMS` is pruned in four
  places, not two; `_free_vram_mib` is in the supervisor; `p2p_coordinator.py:1397` is now
  1398; card 1925 is now at line 1967.
- **S1 follow-up, reviews #54 Zcode, #55 Ark, #56 Johnny, #57 Forge on `53a2eaf5`**, all "S1 is the base for S2". Fixed in the follow-up commit: `_LEASE_WAITERS()` returns `(arrival, lease, for_a_peer)` (Forge 2.4; L5a's owners-before-peers needs it, `lease.alias` names the holder, the supervisor still does not read it); `cardQueue.ts` reads lease rows (`kind`, `what`, `until_cancelled` = "until cancelled", a lease listed as "(lease, <state>)" and never dropped by the holder line; Johnny obj. 3, Forge 2.5); `seconds_from_ratio(audio_s, audio_s_per_wall_s)` = division, unknown for `None`/≤0/NaN, test 30 s at 20× = 1.5 s (Johnny obj. 1, Ark's falsifier of test 37), and `eta_s` is named "seconds of work left (wall clock)"; `let_go_by_hand` answers `False` when its `bound` runs out, as documented, the yield going on (Zcode debt 2); a deadline refusal with `wait_s` None no longer raises (Forge 4); pins for `but_for`, the 1.0 s cheap estimate, `mark_starting`/`mark_running`, and the order against card-queue places. Pinned, not changed (Johnny small 2): `_blockers` compares no aliases; every lease excludes every supervisor, so an earlier place blocks an ungranted lease whatever its alias, and in S1 a place does not hold up a use of a granted lease (S2 adds a place whose blocking holder is that lease). **Recorded for the slices that close them:**
  - **S3, Johnny obj. 2 (warm/cold).** The cheap yield estimate (`_CHEAP_YIELD_S` = 1.0 s) is the holder's **unload** time, and a **lower bound** (Ark #40-2, S3 follow-up 4): the 0.35–0.61 s it rests on is the internal `_unload_model` alone, not the path a yield takes (`yield_card` → `_unload_locked`, under `_load_lock`, with the thread hand-off and the lease's release); Forge's live run measures that path. An idle Whisper unloaded by its idle timer has already released its lease and is not a holder. The warm/cold **load** cost (reload 0.76–0.83 s against 12.8 s in a fresh process) belongs to the requester's own whole-trip estimate, not to the holder's yield.
  - **S3 acceptance, Forge 2.1.** In S1 a lease is never granted while a supervisor child is resident (a supervisor has no ETA and is not asked), consistent with Mike #68. The engine-stopped live run is the only way to see `fits` (L11, test 28's arithmetic) on the owner's card.
  - **S2 acceptance, Forge 2.2.** S1's exclusion is one-sided: the supervisor does not see leases. Two checks: a supervisor start asks an idle lease to let go; and the residents on the card are counted after an engine start across a lease (never two).
  - **S3, Forge 2.3.** `vram_mib` is consumed by L11's `need`; a test that a lease row's `vram_mib` enters `need`.
  - **S2 item 8, Forge 2.5.** `stop_local_engine` on a lease alias today answers `no local engine child for alias '<alias>'` (an error, nothing stopped). The panel's Stop engine button cannot reach it (`card_holder()` skips leases). The music job's "Stop X" can, once a lease holds the card: its `card_wait.holders` carries alias and engine only (`job_schemas.py:485`), so `musicJobs.ts` cannot tell a lease; S4 owns that record, S2 item 8 makes the stop work.
  - **Known debts (Zcode #54).** (1) A double bare `acquire()` on one lease and the cancel of the first: `_leaves` gives the grant back from under the second granted waiter; every ordinary client goes through `use()`: closed in **S4**. (3) `LeaseRefusedForPeer.wait` carries display names: **S3 must not serialize `.wait`** to a peer, only `str(e)`. (4) `acquire_blocking` calls `.result()` with no timeout: an owner loop that dies at shutdown leaves the thread waiting: closed in **S3/S4** with the first thread client (Whisper, the embedder).
  - **`_MAX_SWAP_ROUNDS`** (4) is one constant shared with the supervisor's swap rounds, and the budget is **per (waiter, holder) pair**: a stream of short requests gives each new waiter its own four asks, with no aggregate ceiling. In S2 the lease's asks and the supervisor's rounds count separately, so one holder may be asked up to 4 times by each; if a ping-pong appears, this is where it is cut (Johnny small 1, Forge 4).
- **S2 build, `1e57932f` (CC, 2026-11-22): where this record was read one way, and why.** For review.
  - **`waits_for_engines`.** S1 stored it as "whether a waiting lease swaps an idle supervisor out"; section 3.2 builds music engines with `True`, and music is the client that swaps (Mike #70). Read as: `True` = waits for a busy supervisor and swaps an idle one out by the owner-start rule (swap on, first in both lines); `False` (the default; Whisper while item 1a was open, decided 2026-11-23) = waits for a supervisor until it goes by itself. No production client sets it yet.
  - **Item 5's bound.** `_stop_for_the_swap` calls `give_up_for(self.alias)` with no bound, not with `request.stop_bound`: a `TimeoutError` out of `give_up_for` would reach the stop task's exception path, which ends the mark while the yield still runs. `_wait_for_the_stop` bounds the request's wait instead, and its refusal names the lease ("is already letting go … The yield goes on").
  - **A lease that refuses or could not let go** is asked again after `_LEASE_ASK_AGAIN_S` (1 s), at most `_MAX_SWAP_ROUNDS` times per start; such an ask is not one of the start's rounds (nothing was swapped out), and after the last ask the lease is waited for like a busy holder until the deadline, not refused early by the out-of-rounds path (L6: "the contender keeps waiting until its deadline").
  - **Item 3.** `for_leases` is set on the request; with the swap off, a place taken for leases alone leaves the line (today's refusal at the claim) if a supervisor comes into its way. A request accepted with the swap on whose switch is thrown off mid-wait keeps today's behaviour (leaves, refused at the claim), even when only a lease is in its way. A start with the swap off and nothing in its way takes no place, as written; if a lease is granted in the awaits between that check and the claim, the claim refuses naming the lease (a race the record accepts by "with nothing blocking, the swap-off early return stays as today").
  - **Peers.** Beyond items 3–13, and for L5a's "a peer never goes ahead of an owner's waiter" and L13: `_why_a_peer_may_not_swap`, a peer's call at the card-queue door and the rule 7 check of a peer's start with no place also refuse while an owner's lease waiter waits; a peer's refusal by a lease names no holder (`_a_peer_cannot_swap_refusal`, `_card_held_error`, `strata_provider._peer_start_refusal`).
  - **Not built here, as written:** a call on an engine already up is not held back by a waiting lease (only a place stops new calls, as before), so a lease that does not swap waits for the engine to go by itself; the "N parked" addition to the INFO line (2.4, item 3, "proposed") is not made, because the existing swap words are pinned exactly by tests; `card_holder()` keeps returning supervisor rows only ("may return lease rows" read as optional: the Stop engine button stays the supervisor's, and a lease is stopped through `stop_local_engine` by its holder name, which the music card's Stop X uses). `service.stop_local_engine` is unchanged: a lease that is busy or did not let go raises from the provider, and the command answers `ERROR` with that text. Test 5's "`config.get("gguf_path")` read on a live Strata supervisor" is read as: the Strata start across a lease needs no `gguf_path` (asserted `None`).
- **S2 follow-up, review #75–#91 on `1e57932f` (Ark, Zcode, Johnny, Forge; Mike: "делай по консенсусу").** Fixed in the commit after `373d7b7e`:
  - **A person's Stop reaches the lease line.** `leave_card_queue` → `_chat_leaves_the_line` now also calls the hook `_LEASE_CHAT_LEAVES` (`card_lease._chat_leaves_the_lease_line`): each lease waiter asked under a `_CARD_HOLD` of that chat leaves the line in the Stop's stretch and meets `LeaseTakenOut`, a `TakenOutOfTheCardQueue`; a waiter with `wait_s=None` no longer stays until cancelled. **Read as:** a mark the waiter made is not cleared by the Stop. A holder it asked keeps `draining` until its yield ends, and `_run_yield` ends the mark whatever the outcome (Johnny #89-4's phantom mark does not arise: no path leaves a mark after the yield); clearing it mid-yield would admit a new use onto a model being unloaded (L5, L6), and the yield runs unbounded by item 5's reading above. What the Stop ends is the asking: nothing asks again for a waiter that left. A supervisor it swapped out stops to its end, as every swap stop does.
  - **A peer's whole trip is 120 s** (L13, point 2, "As built"): `PEER_TRIP_S`; the peer's own `expected_s` counts; its unknown `expected_s` is refused. The reviewers' example (19 s of own work behind a 100 s estimate) fits, 119 s ≤ 120 s, and waits; the test of a refusal uses 110 s + 19 s.
  - **One interval for asking a lease again:** the lease line reads the supervisors' `_LEASE_ASK_AGAIN_S` where used; its own `_YIELD_RETRY_S` is gone, and one patch in the tests moves both.
  - **Pinned:** a start's two budgets (at most `_MAX_SWAP_ROUNDS` asks to one lease, a `refused` costing no round; at most `_MAX_SWAP_ROUNDS` holders let go or stopped); a peer refused at once by a running supervisor, idle or busy, swap on or off. `test_the_card_queue_rows_keep_their_keys` freezes both clocks in both modules (it compared two computations of `give_up_at` ms apart: red on the last two CI runs).
  - **Runs.** 13801 passed / 0 failed: CC's worktree on `1e57932f`. 13836 / 0: Zcode's full run on `373d7b7e`. The follow-up's own full run: see the commit message.
  **Recorded for the slices that close them:**
  - **`swap_when_idle` defaults to `false`** (`settings.py`, Mike's call 2026-11-02). With it off no supervisor is evicted for a lease (`swap_out_for_a_lease` and `_swaps_engines` read it) and a peer asks no holder to let go. **Required state for the S3 and L11 live acceptance:** `[local_engines] swap_when_idle = true` (this machine has `true`, `swap_wait_s = 1800`, read by Zcode).
  - **S3: a card refusal ends the provider cascade.** Not yet: the received-voice cascade (`service.py`, `for provider_alias in provider_priority:` with `except Exception: … continue`) tries the next provider after a card refusal, and so does the guest's peer-candidate loop (`_request_transcription_from_peer`, the same `continue`). **Corrected in S3c** (see "S3c build"): neither `continue` saw a card refusal, since `transcribe_audio` answers every local error as a dict; and the guest's loop walks other nodes' cards, so it stays as it is (L13 point 2, "Known limit").
  - **S3: the producer of `expected_s`.** `seconds_from_ratio` is called only in `card_lease` and one test; no provider passes `expected_s`, so until S3 a peer's estimate behind an engine is unknown and it is refused. **Built in S3a** (see "S3a build" below).
  - **S3: `vram_mib` on lease rows** has no role yet; S3 gives it one in L11's fit or removes it.
  - **S3: L11's live veto** (`_free_vram_mib() >= whisper_peak + growth_left`, point 3) is not built; when it is, no unit test reaches a real card, and only the live run checks it.
  - **Open, no slice yet: Stop by hand on a lease alias** (`stop_local_engine` → `_let_a_lease_go_by_hand` → `let_go_by_hand`) waits up to `LEASE_STOP_BY_HAND_S` = 120 s, since `service.stop_local_engine` passes no `timeout`; the UI's command timeout for `stop_local_engine` is 180 s (`coreService.ts`), so the call is held, not cut. **Pinned in S3e** (Zcode (iii)): the UI's timeout outlasts the backend's bound. The `CardHeldError` naming the holder reaches only the owner's UI; a peer's start that meets a lease gets `CardQueueRefusal(…, "", "")` (`_a_peer_cannot_swap_refusal`).
- **S3a build (CC, 2026-11-23, branch `feat/card-lease-s3-s4`): where this record was read one way, and why.** For review.
  - **The producer of `expected_s`** (L10, L13 point 2; Johnny obj. 2), **as amended by S3 follow-up 2** (Johnny #24-1/2). `LocalWhisperProvider` keeps, per device it really ran on (a load that fell back to the CPU counts as `cpu`), the last measured **warm** ratio of each length band (the batch descent's: up to 60, 120, 180 s and over 180 s, one constant `_BATCH_DESCENT` for both), the **first call's penalty** `penalty_s` (seconds the first call after a load takes beyond `audio / warm` of its band, at least 0) and the **load** time, in memory, carried across a providers reload band by band and never persisted. Start figures (`_START_FIGURES`): see L12, "GPU figures on speech" and "Starting figures". `expected_seconds(audio_s)` = `load_s` (when not loaded) + `seconds_from_ratio(audio_s, warm[band])` + `penalty_s` (the first call after a load). S3a's first form divided the whole audio by a "cold ratio" (8.8×, the 10 s clip's first run) and kept one warm ratio per device, the last measured: a cold 300 s file came to 12.8 + 300 / 8.8 = 46.9 s against ≈31.7 s measured, and after a 10 s clip at 43× a 300 s file looked like 7 s against 18.9 s, which a peer's 120 s check rested on. Both are gone; a 10 s measurement now moves only the up-to-60 s band. Interpolation inside a band is not done (the band is the batch size, which sets the speed). **Amended by S3 follow-up 4** (Johnny #41-2, Forge #42-3/4): `penalty_s` is kept by length band as `warm` is, and the load is two figures, `load_first_s` until a load has run in this process and `load_reload_s` after; see there.
  - **The doors** carry `purpose`, `wait_s` and `expected_s` to `transcribe` (keyword arguments, as 3.1 writes them), computed by `card_ask` with L4's `min(caller's remaining − margin, bound)`: `service.transcribe_audio` "dictation" with `queue_wait_s` (600; both Telegram paths and the received-voice cascade come through it); music Transcribe "music Transcribe", no bound; the agent tool "agent tool", 1800 s less the margin, capped by the turn hold's `accepted_at + wait_s`; the peer door "peer <node>", no bound (the lease line gives 120 − `expected_s`), wrapped in `serving_a_peer`. The length is ffprobe's, else soundfile's, else (since follow-up 2) the recorder's declared one, else unknown. **The dictation bound of 600 s is the time waiting in the line** (Ark #20-5); it is not a ceiling on waiting plus transcription. **`_estimate` adds the holders' stop to the work of the waiters ahead, and that sum is the line's own model, not a margin** (Johnny #30-1, correcting this note's reading of Ark #20-9, "the two run in parallel"; verified in S3 follow-up 3). `_blockers` lists every holder on the card as a blocker of every ungranted waiter, the waiter ahead included, and `_settle` grants a waiter only when nothing blocks it, earliest first; so the waiter ahead starts when the holder's stop ends, and the next one when the waiter ahead's work ends. Only the holders go together (each lets go by itself; the longest counts). Pinned by `test_a_going_holders_stop_and_the_waiter_ahead_add_up` (a stop of 20 s, an owner's waiter ahead with 60 s of work, then a peer: 80 s, not max(20, 60) = 60 s). **Not built here (S3b):** the lease that consumes them, `on_wait`, the UI's 240 s at `coreService.ts:1492`, and the re-check of `peer_id in peers` at the grant.
  - **A going supervisor's stop figure** (Ark #10-4). The review note said "so a peer is not refused behind a resident engine merely for lack of a figure". Read with L13's "As built" (Mike's option (a)): only a supervisor that is already going (stopping, draining, or an idle one this very waiter swaps out, which a peer never is) counts `_expected_stop_s()`. A running supervisor that nobody stops for the waiter, idle or busy, stays unknown, so a peer is still refused at once behind it, as pinned by `test_a_running_supervisor_idle_or_busy_refuses_a_peer_at_once`. Giving an idle resident a figure for a peer would make the peer wait for a stop nobody starts; letting a peer stop a supervisor is a change of option (a), for Mike. No `[unverified]` mark stood on the stop figure, so none was removed. **Superseded by Mike, 2026-11-24** (S3/S4 follow-up 8): a peer may have an idle resident supervisor stopped on the owner's terms (`why_a_peer_may_not_swap`, one predicate with ADR-044's inference peer), and then that idle supervisor counts `_expected_stop_s()` for the peer, so the peer waits for a stop the line itself starts; without those terms, or with no figure, the peer is still refused at once. The test is re-pinned (L13, "As built").
  - **Item 7's carrier.** No code wrote a missing key into an existing `config.ini` before; the precedent followed is the firewall's merge-on-startup for `privacy_rules.json` (`_seed_missing_tools_into_rules`, b1c6df06): only a missing key is written, an owner's value never, and a file that has it is not touched. The key goes in as one line under its section header (the section appended when absent) so the owner's other lines and comments stay; `Settings.set` rewrites the whole file through configparser, which drops comments, so it was not used.
- **S3b build (CC, 2026-11-23, branch `feat/card-lease-s3-s4`): where this record was read one way, and why.** For review.
  - **Where the lease lives.** `LocalWhisperProvider` builds `whisper:<alias>` on the first call that routes to a GPU device (`_route_device`: an explicit device as is, `auto` detected once in a thread), not in `__init__`: a providers reload builds the new object before `hand_over_to`, and a lease built there would register a second standing for the same holder. `hand_over_to` moves the lease and rebinds `on_yield`; a lease a retired object left standing (no hand-over) is adopted, one per alias. Display "Whisper (<model>)"; `vram_mib` from the measured size table only plus `_WHISPER_OVERHEAD_MIB` = 2900 (`model_sizes.measured_size_bytes`, so the request path never waits on the HfApi fallback's 20 s), else None; L11 gives it its role in S3e. `eta_s` is the longest remaining `expected_s` of the calls holding it, unknown on an overrun or an unknown part.
  - **Acquire.** `transcribe` takes `lease.use(purpose, wait_s, expected_s, on_wait, check)` before `_load_lock`; a load runs with the lease `starting`. The "L12 first" step of item 1a is a marked seam in `transcribe`, before the lease is asked (S3d). **Read as:** until S3d every GPU call takes the line, a dictation under 300 s included, so with the swap on and nothing waiting a dictation swaps an idle Strata out (the lease `waits_for_engines`, as music's), and with the swap off it waits to its bound (600 s) and is refused naming the engine. An agent's tool inside its own turn on a local alias is refused at once (`LeaseWouldWaitOnItself`) for every length until the twin exists; before S3b it loaded beside the engine.
  - **Release, a second condition** (3.1 "on one condition: `not self.model_loaded`"). Read as: the card is let go when nothing is on it **and no use is open**. With the condition alone, an idle unload that ran while another call was granted and still waiting for `_load_lock` would release, and that call would then load with no grant. So `_unload_locked` calls `release_when_empty` (new on `CardLease`: decided on the owner loop, after the uses that closed before it; kept while a use is open, `starting` or `draining`), and `transcribe` calls it again as its use closes. A yield marks the lease `draining`, so its release is `_run_yield`'s on `let_go`. A failed load, a check that ended the call, `release_if_loaded_here` and shutdown all end in the same call. A load that fell back to the CPU (`_loaded_device == "cpu"`, S3a's `_note_load`) releases as the load returns, and later calls route to the CPU with no lease; L12's RAM check for that route is S3d's.
  - **The preload** asks with `use("preload", wait_s=0, background=True)`, not a bare `acquire`: a grant with no use is idle (L6) and could be asked to let go between the grant and the load. It raises `LeaseSkipped` when the card is not free; `voice_service` logs it at INFO and still sends `whisper_model_loading_failed` with the reason.
  - **The doors' `on_wait`** (section 4), worded by `card_wait_words` ("transcription queued behind <holder>, about N s | about N min | time unknown"): dictation and both Telegram paths, the event `transcription_waiting_for_card` (`purpose`, `text`, `wait`), which no screen reads yet (S5); the agent tool, `emit_progress("Waiting for the card: …")`; the player's Transcribe, the run record's `card_wait` (`transcribe_run`), through a context variable `music.lyrics.show_card_wait` so the transcriber's signature stays. Nothing signals the grant: a run's `card_wait` stays until the run ends, and the event has no "granted" twin; S5 decides.
  - **The peer** (L13 point 3): the handler passes a `check` that raises when `peer_id` is gone from `p2p_manager.peers`; it runs on every round of the wait and once at the grant, before anything loads. A guest that left is dropped with one INFO line, "Transcription answer dropped, request gone", counted in `P2PCoordinator.transcription_answers_dropped`, and no answer is sent. The received-voice re-check (`transfer_id in _voice_transcriptions`, queueing condition 3) sits in the cascade and is left with the cascade break to S3c.
  - **One audio length per call** (Ark #20-7b). The door's `card_ask` measures it once (`audio_seconds`: ffprobe, else soundfile) for `expected_s`, and `card_ask(audio_s=...)` takes a length the door already has without probing. The call itself measures nothing again: its running length is the decoded samples' (`len(audio_array) / sample_rate`, which the batch descent and the recorded ratio read), and `_get_audio_duration` had no caller at all (removed in S3 follow-up 4, Ark #40-3). So there is one probe, not two; `test_a_transcription_probes_the_audio_length_once` pins it. The decoded length is left as the call's own, since it costs no probe and is the one the ratio is recorded against. **Amended by S3 follow-up 3** (Johnny #30-6): one length by value, not only by number of probes; the length the estimate read now travels with the ask and the call runs and records in its band (see there).
  - **The UI timeout** for `transcribe_audio` is `TRANSCRIBE_QUEUE_WAIT_MS` (600 000, the dictation bound's default) plus `TRANSCRIBE_WORK_MARGIN_MS` (240 000, the former timeout, now the transcription's own allowance): 840 s. The margin covers L4's `max(10 s, estimate)` for the recorder's 300 s cap cold on the GPU at the start figures (≈47 s by S3a's cold ratio, 12.8 + 300 / 8.8; ≈33 s by follow-up 2's `12.8 + 300 / 15.9 + 1.0`); a 300 s file on a CPU-only machine (≈306 s) already outran the former 240 s and still can. A `queue_wait_s` raised above 600 in `config.ini` is not seen by the UI.
  - **Music until S4** (3.3, "S3 and S4 land together"). `default_card_holders` now lists a warm Whisper. The old `_wait_for_card` asks a holder row that names an idle lease with a cheap yield to let go (`give_up_for`, bounded 30 s) and goes on; a busy one is waited for and named in `card_wait`, its Stop X honoured by S2 item 8. A supervisor row is untouched, as before. The reverse (Whisper waiting for a running job) needs the music lease: S4. **Removed in S4** (`_wait_for_card`, `_ask_idle_leases_to_let_go`, `default_card_holders` and the runner's `card_holders` reader, with `test_until_s4_a_music_job_asks_an_idle_whisper_to_let_go.py`, whose two cases test 4 now holds through the lease).
  - **Not built here:** L12 and the CPU twin (S3d), L11's shared cell and live veto, `predicted_vram_mib`, the VRAM cache clear (S3e), the cascade break (S3c). **[unverified]** an agent tool's transcription runs on the tool's per-call loop (`on_the_owner_loop`'s note), so the idle timer it arms dies with that loop: the warm grant then stays until a contender asks it to let go, which works (the yield runs on the owner loop); not measured live.
- **S3c build (CC, 2026-11-23, branch `feat/card-lease-s3-s4`): where this record was read one way, and why.** For review. Line numbers are `e8b5ac1a`'s.
  - **Where a refusal really went** (Zcode #22, Ark #20, checked). `transcribe_audio` (`service.py:4634`) catches every local error (`:4826`) and, after the OpenAI fallback and the peer, ends in `except Exception` (`:4921`) answering `{"status": "error", ...}` (`:4939`). So the received-voice loop (`:5181-5188`) got a dict, took it as a result (`break`, then "Transcription returned empty text"), and its `except Exception: continue` was reached only by what raises before that `try` (a `remote:` alias, unreadable or oversized audio). Before S3c a card refusal therefore did **not** queue a second GPU alias, as the Risks entry feared; it ended the cascade, and a cloud alias later in the list was **not** tried either (test 22's first variant failed on that alone).
  - **Telling a refusal from a failure.** By type, never by text: an exception that is a `CardQueueRefusal` (every `Lease*` refusal of S1: `LeaseWaitedTooLong`, `LeaseWouldWaitOnItself`, `LeaseSkipped`, `LeaseTakenOut`, `LeaseRefusedForPeer`, `LeaseBusy`, `LeaseClosed`; and the card queue's own) sets the field `card_refusal` (the class name) on `transcribe_audio`'s error answer, as `model_not_cached` is set (finding D). Read as section 3.1 condition 2 writes it: "a `CardQueueRefusal` (any `Lease*` refusal)"; any other exception is an ordinary failure, and its answer carries no such field.
  - **The received-voice walk** (condition 2). After an answer with `card_refusal`, the loop skips every later alias that would ask this node's card (`_voice_alias_asks_for_the_card`: a configured local Whisper whose route is not the CPU, `whisper_provider.asks_for_the_card`; and a name that is not configured, since `transcribe_audio` turns it into the first local Whisper) and still tries a cloud alias, a `remote:` alias and a `device: cpu` alias. An ordinary failure keeps today's course (the dict ends the loop): making every failure walk on would load the next GPU model after any error, a change S3c was not asked for. **Verified and fixed in S3 follow-up 3** (Zcode #29-3, Ark F4; this note said "[unverified, accepted]"): with the OpenAI fallback on, a refused alias's own chain inside `transcribe_audio` asks the first OpenAI-type provider and then the peers; when both give nothing the answer carries `card_refusal` and the walk went on, so a later cloud alias naming that provider called it a second time, and a later `remote:` alias asked a peer the chain had already asked (`test_openai_once_and_the_peer_once`: two calls each before the fix). The refusal's answer now carries `already_asked` (the provider's alias, the peers' node ids), and the walk skips an alias naming one of them; another cloud alias is still tried.
  - **The dictation door** (`transcribe_audio` alone: dictation, both Telegram paths, music Transcribe through a non-Whisper alias). It walks no GPU aliases: one local provider, then the OpenAI fallback, then a peer (Mike's call, 2026-09-07). **Read as:** a refusal takes that chain as a failure does, because section 3.1 condition 2 says "a cloud alias later in the list (`openai`) does not touch the card and may still be tried", `CardQueueRefusal`'s own rule is "never tried again by another path, **which would only stand in the same line**", and a peer stands in its own node's line (L13). With the fallback off the refusal reaches the person in the error text, as before, now with `card_refusal` beside it.
  - **Condition 3, the received-voice re-check** (left to S3c by "S3b build"). The cascade passes the lease a `check` that raises when `transfer_id in _voice_transcriptions`; it reaches `transcribe_audio`'s `card_ask` through the context variable `_VOICE_MESSAGE_CARD_CHECK`, the precedent of `music.lyrics.show_card_wait`, so the command's signature stays. The exception it raises passes through `transcribe_audio` without the fallback (the transcript is in: nothing is left to do), and the cascade returns. The same test runs once before each alias of the walk, so a refusal that waited out the bound does not send the message on to the cloud after the sender's text came.
  - **The guest's walk over peers** (the ADR's two readings, decided). L13 point 2's "Not changed here" holds; the Review notes' "so does the guest's peer-candidate loop" and the review history's "tries one peer, and there is no second `continue` loop" were both inexact: the guest tries one voice alias **per peer** and walks the peers in turn (L13, "Known limit", where the evidence is). A peer's card refusal is a bare text on the guest, and the next peer is another card, so nothing there changed; `test_a_peers_card_refusal_is_followed_by_the_next_peer` pins it beside the older test.
  - **Confirmed, left as built:** the peer door's re-check at the grant (S3b: `p2p_coordinator.handle_transcription_request`'s `check`, L13 point 3).
  - **Not built here:** L12 and the CPU twin (S3d; the seam in `whisper_provider.transcribe` stays), L11's fit and live veto (S3e), S4.
- **S3 follow-up 2 (CC, 2026-11-23, branch `feat/card-lease-s3-s4`, on `07015765`): Johnny #24, Zcode #22-2 to #22-5.** For review.
  - **The speed model** (Johnny #24-1/2): see "S3a build", as amended, and L10, L12. Zcode #22-2: `_copy_state_to` did not carry the measured figures, so a load that finished on the old object after a providers reload lost its `load_s`; it now carries them, the fresh load time winning over the successor's. Zcode #22-3: the tests read the start figures from `_START_FIGURES`.
  - **The recorder's length as the fallback** (Johnny #24-3). soundfile does not read WebM, the recorder's default, so without ffprobe a dictation had no length and no estimate. `card_ask` takes `declared_s`, used only when the probe reads none. Read as: the UI's `duration_seconds` the review pointed to (`coreService.ts`, `send_voice_message` and `send_group_voice_message`) never reached `transcribe_audio`; it reaches the received-voice cascade in the message's `voice_metadata`, which now passes it, and the dictation door (`ChatPanel.svelte`'s `transcribe_audio` command) now sends `duration_seconds` from the recorder too, into `transcribe_audio`'s new `duration_seconds` argument. Telegram (Ogg, which soundfile reads) and the peer door (no protocol change, L13) pass none. In `card_lease`, a peer with an unknown `expected_s` gets the whole 120 s as its bound by an explicit branch, not `expected_s or 0`; `_enter` refuses it at once on a blocked card and serves it only on a free one, as L13 point 2 then said, pinned with no door bound too (S3 follow-up 3: refused on a free card too).
  - **The peer's probe under the peer mark** (Zcode #22-4): the peer door builds its ask inside `serving_a_peer`; the guest re-check stays on the ask, at the grant.
  - **`config.ini`** (Johnny #24, minor): the seed keeps the file's line endings (read with `newline=''`, CRLF when the file has any) and reads the file back for the values in memory. Both opens keep the locale's default encoding, as `Settings` reads the file with `self._config.read(self.config_file)` and no encoding; an explicit UTF-8 in the seed alone would read and write differently from every other access.
  - **The idle-unload source** (Johnny #24, question): wired on every production path. `run_service.py` builds `CoreService`, whose `__init__` registers `settings.get_local_transcription_idle_unload_s` before the providers are injected; it is a module-level getter read on every schedule, so a provider built by a providers reload and a `hand_over_to` successor read it as well, and nothing in the package clears it. With no source (a provider built outside the service) the timer is off by design. Pinned, not changed.
  - **L11's derived figures** (Zcode #22-5): 30575, `whisper_peak` 4447 and test 28's 6495 are marked in place as derived, not measured.

- **S3 follow-up 3 (CC, 2026-11-23, branch `feat/card-lease-s3-s4`, on `00fa2b16`): reviews #29 Zcode, #30 Johnny, #31 Forge, #32 Ark.** For review.
  - **A test wrote the real `config.ini`** (item 1). `~/.dpc/config.ini` got `[local_transcription] idle_unload_s = 300  # Unload the model ...` at 15:59:38 +0700, between `c3b97a76` (15:32, the seed with `idle_unload_s` only) and `e8b5ac1a` (16:22, which seeds `queue_wait_s` too, never written there since). Found: no test in the S3 lists builds a `Settings` on the real home (`fu2` list run with a recording guard: none), but four other files do, by code paths that pick the home themselves: test_conversation_monitoring's fixture builds `CoreService()` (`Settings(DPC_HOME_DIR)`, `service.py:814`), and the `kg` fixtures of test_knowledge_graph, test_graph_snapshot and test_legacy_forms build a `KnowledgeGraph` on a tmp root, whose `_dpc_home_for` falls back to `DPC_HOME` or the real home (`knowledge_graph.py:1214`, then `Settings(...)` at `:1235` and `:1246`). Any of them run under the branch's code seeds the real file. **[inferred]** which run wrote it at 15:59:38: no tool call of the S3 sessions' transcripts falls on that second (the nearest pytest run ended at 15:59:23), so it was a run outside them, or one of those files. Fixed in `conftest.py` (`_settings_never_open_the_real_home`): every `Settings` asked for the real home is built on the test's `tmp_path`, so a test sees what a CI runner sees and the real file is never opened; pinned by `test_a_test_run_never_writes_the_real_config_ini.py`. Not covered: a child process a test starts (it does not load `conftest.py`). The real file was not edited.
  - **A peer's unknown length on a free card** (Johnny #30-4, Mike #17): refused at the grant too, in the blocked card's class and words ("this node's GPU is busy with another job right now, and how long is not known"), for a grant and for a new use of a warm one; L13 point 2 now says so. **Read as:** the identical wording is what the item asked for, and it tells the guest nothing about the card's state; on a free card "busy" is not literally true. **Consequence, for Mike:** the peer door passes no declared length (no protocol change, follow-up 2), so on a host without ffprobe a guest's WebM recording (soundfile does not read WebM) has no length and is now always refused, where a free card served it before.
  - **An `auto` alias after a refusal** (Johnny #30-7). `asks_for_the_card` detects an `auto` device not detected yet the way the call itself routes it (`_route_device`: torch's CUDA check, no model load; kept on the provider) instead of counting it as the card's; only a detection that raises errs on the card side. The cascade calls it in a thread (`asyncio.to_thread`), since it may import torch.
  - **The fallback asked twice**: see "S3c build", the note that said "[unverified, accepted]".
  - **A refusal's own words in the log** (Zcode #29-4, Ark (4)): "Local transcription was refused the card (<class>): ..." instead of "Local transcription failed", and at the end of the chain a WARNING "Audio transcription ended on a card refusal" with no traceback instead of the ERROR "Audio transcription failed"; at the end of the walk "No transcription for transfer ...: the card refused it (<class>) and no alias that does not use the card gave one" instead of "All transcription providers failed". Only when `card_refusal` / `refused_by_the_card` is set; an ordinary failure keeps its words.
  - **The check goes only with a card ask** (Ark (6)). The context variable stays set for the walk; `transcribe_audio` hands it to the provider only when `asks_for_the_card` says the call asks the card. **Read with the code:** before this a CPU alias got the check in `transcribe`'s arguments but never ran it (no lease, `_transcribe_holding` takes none), so no call was killed in fact; now it is not handed at all, pinned by the hand-off.
  - **An LLM turn behind a warm idle Whisper** (Forge #31-8): pinned green as built, no change. A llama-server start with a warm idle Whisper (`idle_unload_s` 300) holding the lease asks it to let go and has the card in under `2 × _CHEAP_YIELD_S`, swap on and off.
  - **One audio length by value** (Johnny #30-6; **amended by S3 follow-up 6**, Ark #49-3: the batch keeps the estimate's length, the recorded run takes the decoded one, see there). The estimate read the probe's length (ffprobe, else soundfile, else the recorder's), the batch descent and the recorded run the decoded samples' (`len(audio_array) / sample_rate`); a file probed at 60.4 s and decoded at 59.6 s was estimated in the up-to-120 s band and run (batch 16) and recorded in the up-to-60 s one. Of the two ways, the length now travels with the ask: `card_ask` adds `audio_s` when its estimate was made from a known length, `transcribe` hands it to `_transcribe_loaded`, and the descent and `_record_run` read it; with none, the decoded length as before. The other way (estimate from the decoded length) needs the audio decoded before the lease is asked, a full decode on the door's side for every call. The returned `duration` and the segments keep the decoded length.
  - **`_estimate`'s sum** (Johnny #30-1): see "S3a build", the note on `_estimate`, corrected.
  - **An ordinary failure still ends the walk** (Ark #24-2, read with the code): S3c changed only the card path. With `fallback_to_openai` on, an ordinary local failure takes the OpenAI-then-peer chain inside `transcribe_audio`; when that gives nothing the answer is an error dict without `card_refusal`, and the walk ends on it (`break`, then "Transcription returned empty text"), as with the fallback off. **Two keys of one name** (Johnny): `fallback_to_openai` is written in `[voice_transcription]` and in `[local_transcription]`; only the local one decides (`get_local_transcription_fallback_to_openai`, read in `transcribe_audio`, which the cascade calls); the voice one is only reported (`voice_service.py:493`).
  - **The guest's 120 s** (Ark (3)): `timeout=120.0` in `_transcribe_with_peer` and in `transcribe_audio`'s `remote:` branch is the transport timeout, `request_transcription_from_peer`'s `asyncio.wait_for` on the answer (`p2p_coordinator.py:1715`); it equals `PEER_TRIP_S` by coincidence, and neither reads the other.
  - **Where `card_refusal` can come from** (Zcode #29-1): only the local Whisper branch of `transcribe_audio` sets it. The `remote:` branch raises `ValueError`, the cloud branch's errors are ordinary, and a peer's card refusal reaches the guest as error text (`RuntimeError(error)`), so none of them can carry a `CardQueueRefusal` today.
  - **Not done here (S3e):** `vram_mib` per length, the reserve sources, `_supervisor_stop_estimate` remaining against full, a new measure of `_CHEAP_YIELD_S`, `TRANSCRIBE_QUEUE_WAIT_MS`. The agent tool's wait without a turn hold was open here (600 or 1790 s); Mike decided it on 2026-11-23: capped at `queue_wait_s` (Decided by Mike).
- **S3 follow-up 4 (CC, 2026-11-23, branch `feat/card-lease-s3-s4`, on `43503073`): group-chat reviews #40 Ark, #41 Johnny, #42 Forge.** For review.
  - **The first call's penalty by length band** (Johnny #41-2, Forge #42-3). `_note_run` kept `penalty_s` per device, so the first call after a load set it for every length: a 300 s first call (within run-to-run spread in `gpu_run1.log`, so ≈0) took away a 10 s clip's +0.9 s. `penalty_s` is now `{band: s}` like `warm`: the start figure 1.0 s in every band on cuda and 0 on the cpu, a first call moves its own band only, `speed_figures` merges measured over start band by band, and a providers reload (`_carry_figures_to`) carries it band by band, the new object keeping a band it measured itself. Johnny's falsifier (a 300 s first call, then a 10 s clip's estimate keeps its penalty) failed before the change and passes after.
  - **A holder that is loading has an ETA** (Ark #40-1, Johnny #41-5). **Read with the code:** a transcription registers its run (`_card_runs`) right after its use is granted and before `_load_lock` and the load, so a call whose door passed `expected_s` already had an ETA while it loaded (the door's estimate counted the load when the model was not loaded at the ask). Two cases had none or a wrong one: the preload (a use with no run: `eta_s` None, so an owner saw "time unknown" and a peer was refused) and a call whose estimate was made on a loaded model that went before its grant (no load in it: the ETA ran over into unknown). `eta_s` is now, while a load runs under the lease, the load's remaining time (from its start and the current load figure of its device, `load_first_s` or `load_reload_s`) plus the longest first-call work (`audio / warm[band] + penalty_s[band]`, from the length the call carries since follow-up 3) of the calls waiting for it; a call with no length counts its own `expected_s − elapsed`, one with neither leaves the ETA unknown, and a load past its figure is unknown (L10, as an overrun was). The load is recorded in `_card_load`, shared with a successor like `_card_runs`. Pinned: a peer behind a loading preload with a known length waits (estimate ≈12.8 s, within its 120 s) and is granted after the load; a call of unknown length during a load leaves the peer refused at once.
  - **Two start load figures** (Forge #42-4; **amended by S3 follow-up 6**, Forge #48-1, Ark #49-1: "a load has run" is per device, and a reload past its figure falls back to the first-load figure; see there). The 12.8 s is a fresh process's cold load (the imports and the first library init); inside a running process a reload is 0.76–0.83 s (Forge 0.6–0.9 s). `load_s` is now `load_first_s` (cuda 12.8 s, cpu 9.0 s) until a load has run in this process and `load_reload_s` (cuda 0.8 s, cpu 1.0 s from the backend's log line in L12) after. "A load has run" is one module flag, set by `_note_load` on any provider object and any device: a providers reload builds new objects, and torch and transformers are imported once per process. **Read as:** a GPU alias's first load after the CPU twin loaded reads 0.8 s where it would cost about the load step, 1–2.4 s (Forge #21), so it errs short by about a second; a flag per device would err long by about 11 s. A measured load is recorded in its own kind (the first of the process or a reload) and wins over that kind's start figure only, so the cold load measured at backend start does not stand for the next reload; this changes what five existing tests of the speed model read after a load (they now read the kind, or the reload once a load has run). Tests reset the flag per test (`conftest.py`), as a fresh backend starts.
  - **The dead length probe** (Ark #40-3). `_get_audio_duration` had no caller in the package (grep); it is removed, with its fake in `test_a_transcription_probes_the_audio_length_once` and the references above (L10, "One audio length per call", the drifted line list).
  - **Lines only.** The up-to-180 s figure is "estimated from the batch-2 figure; batch 4 was never measured" (Johnny #41-1), in the code's comment and under "GPU figures on speech". `_CHEAP_YIELD_S` = 1.0 s is a lower bound (Ark #40-2; see "S3, Johnny obj. 2"). **One length by value** (Johnny #30-6) was resolved in `43503073` by carrying the estimate's length into the call (`card_ask`'s `audio_s`, read by the batch descent and `_record_run`). Forge #42-5 preferred the decoded samples' length; it is not taken because the samples exist only after a full decode (`librosa.load`), which runs inside the call after the card is granted, while the estimate is needed before the card is asked (the bound, a peer's 120 s, the wait estimate of later waiters); deciding by samples would mean a full decode on the door's side of every call, before the lease. The samples' length is still the returned `duration` and the segments' end.
  - **Not done here:** `vram_mib` per band (S3e), the reserves, protocol changes. (The agent tool's cap, 600 or 1790 s: decided by Mike on 2026-11-23, `queue_wait_s` without a turn hold; built in `674b1971`.)

- **S3 follow-up 5 (CC, 2026-11-23, branch `feat/card-lease-s3-s4`, on `3f10138c`): group-chat review #44 Ark.** For review.
  - **A peer refused for an unknown length is told so** (Ark #44-1). `_enter` refused a peer whose `expected_s` is None with "this node's GPU is busy with another job right now, and how long is not known", also on a free card, where the card is not busy. It now says "this node cannot tell how long this audio is, so it cannot promise to finish it within the time the request is given" (same class, `LeaseRefusedForPeer`). **Blocked card with an unknown length uses the new text too:** the length is what is unknown, and it is what makes the refusal certain (a known length could have waited its turn); the unknown-length check runs before the blockers are read, so the blocked-card branch is only reached with a known length and keeps its own words ("busy ... for about N min", or "and how long is not known" when the wait, not the length, is unknown).
  - **`ask["audio_s"]` follows the length, not the estimator** (Ark #44-2). `card_ask` carries `audio_s` whenever the length is known (probe or declared), also for a provider without `expected_seconds`. Consumers read it: `transcribe(audio_s=...)` (the only `transcribe` in the package is the Whisper provider's; every door passes `**ask` to it), `_card_runs`, the batch descent and `_record_run`; all already treat it as optional and positive, so a provider with no figures only gains the one length.
  - **Module-scoped fixtures and the Settings guard** (Ark #44-3). Read: every `scope="module"`/`"session"` fixture in `tests/` (17 files). None builds `Settings`, `CoreService()`, `KnowledgeGraph`, nor reaches `dpc_agent/utils.py:644` or `webrtc_peer.py:48`: they are a signing key, local HTTP servers, Chromium contexts (`AuthBrowser(agent_id=...)` does not read Settings), `git archive` snapshots into `tmp_path_factory`, a gaia module load and ids. The function-scoped guard `_settings_never_open_the_real_home` is enough today; a new module fixture that builds one would escape it, and that is the thing to grep for.
  - **Notes.** (a) The batch descent (and later `vram_mib` by band) uses the probe or declared length. `declared_s` precision is not checked by a decode; Forge measured probe against samples at about 0.1% on synthetic opus. (b) On a node without ffprobe the peer path for WebM is fully dead: soundfile does not read WebM, the length is unknown and the peer is refused even on a free card. The fix is a length field in the peer transcription protocol (`handle_transcription_request` has none); the guest sends only base64 audio, so the host-side voice metadata (`declared_s`) is not available at the peer door. (c) Item 7's acceptance is a live check of `~/.dpc/config.ini` in Forge's run: the guard hides the real file from the suite, so no test can see it.
  - **Not done here:** `_route_device` caching from the predicate (S3d), `vram_mib` (S3e).
- **S3d build (CC, 2026-11-23, branch `feat/card-lease-s3-s4`, on `70fc74e0`): L12 and the CPU twin; where this record was read one way, and why.** For review.
  - **Where the twin lives** (3.1, "The CPU route"). The record says the providers manager creates `<alias>@cpu` beside each GPU Whisper alias, not written to `providers.json`. **Read as:** beside its GPU alias and carried by the providers manager's reload, but not entered in `llm_manager.providers`: about a hundred places enumerate that dict (the UI's provider list through `GET_PROVIDERS`, whose save writes `providers.json` back; the firewall's compute rows; the voice and model-download lookups), and each would see an alias nobody configured. So the GPU alias builds its twin on the first call L12 considers for it (`_the_cpu_twin`: the GPU alias's config on `device: cpu`, `compile_model` off, `_cpu_twin_of` naming the GPU alias, `idle_unload_s` = `TWIN_IDLE_UNLOAD_S` 60 s, or the GPU alias's optional `cpu_idle_unload_s`). A providers reload hands it to the new GPU object's twin in `hand_over_to` (`_hand_the_twin_to`) by the steps the manager takes for a GPU alias: the load lock shared, a loaded model carried unless its unload runs, then the twin's own `hand_over_to`, which carries its measured figures; a new alias on `device: cpu` or another model retires the old twin and unloads it, and so does `retire()` with no successor. The twin is its own object (Ark #64 §4): its own `model_loaded`, load lock, idle timer and speed figures; a CPU device builds no lease. `RoutedToCpu` carries the twin object itself, since nothing could look it up by name.
  - **The choice** (L12, in `LocalWhisperProvider._choose_route`, after `_lease_for_the_card` built the lease and before `use()`; what it reads comes from `card_lease.route_view`, on the owner loop, which takes no place, asks no holder and logs nothing). Point 1: no blocker → the GPU (L11's `fits` is S3e's). Point 2: a length over `cpu_max_audio_s` → the line; **an unknown length → the line** (the CPU's time cannot be shown). Point 3: a resident Strata busy by its own `_busy_for_the_card` multiplies the twin's estimate by the factor. Point 4, **read as the GPU's finish, not its wait alone**: `gpu = wait + the GPU's own expected time` (the door's `expected_s`, else `expected_seconds`), against `cpu = twin.expected_seconds(length) × factor`, cold or warm by the twin's own state. Comparing a wait against a CPU time that includes the work would favour the CPU by the GPU's work; Ark's two examples come out the same either way. Then `cpu <= cpu_max_s`, and a cold twin passes the RAM check. Otherwise the line, where the lease applies as before (bound, `on_wait`, item 1a's swap). **No switch in mid-flight** (Zcode #69): the choice is made once; a call sent to the line stays there, and a twin's failure is the call's failure.
  - **Item 1a** (an idle supervisor in front of a dictation). `_estimate` gives a supervisor this waiter would swap out its stop figure (S3a), so read alone the GPU would win every short file and swap Strata out. **Read as:** for L12 a supervisor in the way that is not already going (not stopping or draining) makes the GPU's wait long (`route_view`'s `engine_stays`): a busy one has no ETA (L10), and an idle one's swap costs what no figure holds (the LLM's reload, Strata's parked conversations), which is why item 1a puts L12 first. So a file up to 300 s takes the twin when its estimate is within `cpu_max_s` and the RAM allows; a longer one takes the line and, with the swap on and nothing waiting, swaps the idle supervisor out by the owner-start rule, as built in S3b. A supervisor already going keeps its stop figure.
  - **The RAM check** ("Enough RAM"). `psutil.virtual_memory().available` against `_TWIN_RAM_MIB` 4096 + `_RAM_MARGIN_MIB` 1024 (both start values, the twin's from L12's table, unverified). Only a cold twin is checked (a warm one is already in RAM). **RAM that cannot be read does not cover it** (no cold route). A GPU load that falls back to the CPU (3.1, Ark #30) runs the same check before it goes on, or raises with the reason ("the load on cuda failed and the CPU cannot take the model instead (N MiB of host RAM available, 5120 MiB needed)"), an ordinary failure. The test suite starts every test with RAM unreadable (`conftest.py`, `_the_hosts_ram_is_unknown_unless_a_test_says`), so a runner's real RAM decides no route, as a GPU decides none; a test of the CPU route says how much there is. **[unverified]** a twin found warm at the check and unloaded by its idle timer before the call loads cold without a check; not guarded. **Guarded since S3 follow-up 7** (Ark #61-3): the twin's own load runs the check; see there.
  - **Section 3.5.** "Inside its own turn on a local alias" is read as the request's card hold standing on a holder in its way (the test `_refusal_for_a_self_wait` already used, now `_holder_it_stands_on`); a local alias whose engine is not on the card leaves the card free and takes the GPU at once. The 300 s cap is `cpu_max_audio_s` (the same setting, Mike #71). Within it and with the RAM check passed: the twin, no comparison, no lease, no wait, whatever `cpu_max_s` and the factor say. Over it, **of unknown length**, too little RAM, or a twin whose load or run fails: `LeaseWouldWaitOnItself` at once, holder the supervisor's alias, in the record's words ("Not transcribed: this turn runs on the local model 'flash', which holds the card. A file over 300 s needs the card, so nothing was queued."; for an unknown length "The length of this file cannot be read, and a file over 300 s needs the card"; for the twin "Not transcribed: the CPU could not take it (<reason>), and this turn holds the card (the local model 'flash'), so nothing was queued."). The twin's failure is turned into the refusal by `transcribe_with_route` (`RoutedToCpu.if_it_fails`). Before S3d every length was refused.
  - **The selector** (`transcribe_with_route`, at every door: `service.transcribe_audio` with both Telegram paths and the received-voice cascade, the agent tool, music Transcribe, the peer door). It passes the call as asked; on `RoutedToCpu` it runs the door's `check` once (as a grant would) and calls the twin with the same `language`, `task`, `audio_s` and `release_if_loaded_here`; the card's own arguments are dropped. The answer gains `"device": "cpu"`. `release_if_loaded_here` is passed only when set, as each door passed it.
  - **Peers** (Mike #71, 6C; L13 point 2, Mike #17). By default a peer takes no CPU route and no twin is built for it. The optional setting is read as a key on the GPU alias, `let_peers_use_cpu_twin` (false unless set; S5 puts it beside the device selector): with it, L12 runs for the peer and the twin is taken only when its estimate also fits `PEER_TRIP_S`; otherwise the line, where the lease's 120 s rule refuses or admits the peer as before.
  - **The settings** (item 9; item 7's carrier). `[local_transcription] cpu_max_s` 600, `cpu_max_audio_s` 300 and `cpu_contention_factor` 1.0: a code default in the seed table and a getter that reads the file first (`Settings._local_transcription_figure`: a value missing, unreadable or not above 0 reads as the default). **Seeded into an existing `config.ini`** like `idle_unload_s` and `queue_wait_s`: item 7's rule (the default in code and the key in the file, the file winning) is the pattern `queue_wait_s` followed for Mike's item 6A value, and these are Mike's decided start values "tuned from operation", which the file is where the owner tunes. The service registers `Settings.get_local_transcription_cpu_route` as the provider's source (`set_cpu_route_source`), read on every choice; with none, the code defaults.
  - **Observable.** One INFO line per choice when the card is not free, "Whisper '<alias>': L12 sends <purpose> to the CPU twin | the line for the card: <why>", the why naming the CPU and GPU estimates, cold or warm, the factor and a local engine in the way; DEBUG on a free card. The last choice is kept as `LocalWhisperProvider.last_route`.
  - **The cascade question writes nothing** (Ark #44-2). `_route_device(remember=False)` from `asks_for_the_card` detects an `auto` device without keeping it; the call's own paths (`_lease_for_the_card`, and `expected_seconds` from the door's `card_ask`) keep a detection that returned, and the question reads it once kept. A detection that raises is never kept. `test_the_detection_is_kept_for_the_call` (S3 follow-up 3) pinned the opposite and is now `test_the_question_keeps_no_detection`.
  - **After a card refusal** (S3c; section 3.1 condition 2). The twin is reached only through L12 inside the first GPU alias's call. A refusal means that call went to the line, so the walk does not try its twin (no switch in mid-flight) and skips later GPU aliases with theirs; a configured `device: cpu` alias is still tried, once. Pinned in test 33.
  - **Never the default** (Zcode). The twin is not in the providers list, so `sorted(candidates)[0]` in the agent tool and the preload's first local Whisper cannot meet it; both also skip any provider marked `is_cpu_twin`, pinned with a twin whose name sorts first.
  - **Tests.** 30 `test_the_gpu_cpu_choice_compares_times_not_audio_length.py` (all but the background part: no background transcription door exists); 31 `test_a_cold_cpu_twin_counts_its_load_and_a_warm_one_does_not.py`; 32 `test_a_busy_strata_lets_short_files_on_the_cpu_with_the_contention_factor.py`, with item 1a's two cases; 33 `test_the_cpu_twin_is_its_own_provider.py`; 40 `test_an_agents_transcription_inside_its_own_turn.py` (the cloud-alias row without its `emit_progress`; the turn that moves between rounds and the person's Stop are the lease's, unchanged); and `test_l12_settings_peers_and_a_twin_that_is_never_the_default.py` (the settings, the peer setting, the twin never the default, the question that writes nothing, the CPU fallback's RAM check).
  - **Not built here:** a background transcription's L12 (no such door; `LeaseSkipped` for one stays unbuilt); L11's `fits` (S3e); L12's RAM check for an alias the owner configured with `device: cpu` (it runs as before); the UI for `let_peers_use_cpu_twin` and any sign in the UI that a call went to the CPU (S5); the pending measurements that size the factor and the twin's RAM figure; the twin on macOS **[unverified]**. A configured `device: cpu` alias of the same model and the twin are two CPU copies when both are loaded; not guarded.
- **S3 follow-up 6 (CC, 2026-11-23, branch `dev`, on `9ae34f92`): group-chat reviews #47 Johnny, #48 Forge, #49 Ark of follow-ups 3–5; Forge #54 and Johnny #53-4 on two comments.** For review.
  - **A reload figure only for a device that has loaded** (Forge #48-1, Ark #49-1). Follow-up 4's one module flag, set by any load on any device, picked `load_reload_s` for every device. S3d made a CPU twin's load common, so the GPU's first load after it read 0.8 s instead of 12.8 s, and `_card_eta` returned None once a load passed its figure: after 0.8 s an owner saw "time unknown" and a peer was refused for the rest of a ~12 s load. It is now `_LOADED_DEVICES`, the set of devices a load has run on in this process (still per process: a providers reload builds new objects; **amended by S3 follow-up 7**, Ark #61-1: per device and model, `_LOADED_WEIGHTS`). **Read as:** the GPU's first load after the twin now reads 12.8 s where the imports are already paid (Forge #21: the load step alone is 1–2.4 s), so it errs long by about 11 s where follow-up 4 erred short; long is the safe side here, since it tells a peer a longer wait (a cold 30 s file is still ≈15 s, inside 120 s) where short turned the ETA into "unknown" and refused it. **The overrun, chosen and why:** a reload past its figure falls back to the device's first-load figure (`_load_left`), and only past that is the ETA unknown. A reload over 0.8 s is most likely reading the weights from disk again (the OS cache evicted them), which the first load did too, imports aside, so the first-load figure is the slower load this device is known for, not a guess; a first load past its own figure has nothing above it and stays unknown (L10). Keeping None for a reload would have refused peers for most of such a load. Tests build a CPU and a GPU provider in one test, since the conftest clears the set per test.
  - **The ETA during a load reads the device L12 routed on** (Ark #49-2). `_lease_for_the_card` is now `_route_for_the_card`, which returns the route's device with the lease; the device is carried through `_preload` and `_transcribe_holding` into `_load_under_the_lease`, which records it in `_card_load` and never reads the device again. A model that loaded on another device is logged at WARNING ("the load was estimated on cuda and ran on mps; the ETA during the load read cuda's figures"); a CUDA load that fell back to the CPU logs this beside its own line.
  - **The detection off the loop** (Forge #48-4). **Read with the code:** on `9ae34f92` the preload's first detection already ran in a thread: `_lease_for_the_card` moved to a thread when the device was `auto` and undetected, and kept what it found (`_route_device(remember=True)`), so `_route_device()` in `_load_under_the_lease` read the kept value; the new test passes on `9ae34f92` as well. Made structural: `_load_under_the_lease` takes the route's device and detects nothing, and `_would_detect` decides the thread hop, which also covers a loaded model whose device is not known. Pinned for the preload and a transcription with a detection that records its thread.
  - **The warm figure learns from the decoded length** (Ark #49-3). Amends follow-up 3's "One audio length by value": the batch descent keeps the estimate's length (behaviour: the call runs the batch its estimate assumed), and `_record_run` takes the decoded length (the measurement: the elapsed time is the decoded audio's), on both the PyTorch and the MLX path. A WARNING names both when they differ by more than 1 s (`_LENGTH_MISMATCH_S`). `test_the_band_of_the_estimate_is_the_band_of_the_execution` now records in the decoded band; a 300 s estimate decoded at 270 s records 270 / elapsed in the over-180 s band.
  - **The double-counted load** (Forge #48-5). **Read with the code: not reproduced.** In `_card_eta`'s loading branch a call of unknown length adds `expected_s − elapsed` to a list whose maximum is taken beside `load_left`; the two are compared, not added. When `expected_s` holds the load (the door's estimate on an unloaded model) the maximum is ≈ `expected_s − elapsed`, the right figure. What is left is the other side: an `expected_s` made on a loaded model that went before the grant counts the load only as `max(load_left, work − elapsed)`, short by up to the load; the call carries nothing that tells the two apart, and since follow-up 5 no door gives `expected_s` without `audio_s` (a known length is always carried, and the known-length branch adds the load to the first call's work). Both branches pinned; the docstring says max, not sum.
  - **Timing tests** (Ark #49-4). Both files exist on origin/dev `dc495597` (added in `43be0684` and `77dcd2e7`, unchanged by S3). `test_a_holder_still_starting_is_busy_and_is_not_marked` gated its holder on a 0.1 s health delay and asserted after fixed 20 and 30 ms pauses, so a runner slower than 0.1 s saw the holder start and be marked; its `/health` now waits for an event the test sets after the asserts, and the asserts wait for the state ("starting", the start in the line). Changed the same way, where the assertion needs something to have happened: `test_a_hold_taken_while_a_start_waits_does_not_keep_it_waiting` (two holds), `test_a_stop_by_hand_landing_during_the_wait_gives_the_waiting_start_the_card` (draining), `test_a_hold_entered_on_another_loop_is_written_on_the_owner_loop` (the owner loop's write) and `test_three_starts_race_and_are_served_in_the_order_they_came` (all three in the line before the card frees). Left: pauses followed only by a negative assertion (`not start.done()`, nothing marked) once the positive state was awaited, which a slow runner cannot fail, and pauses that only yield or stand for a slow step.
  - **The whole home in tests** (Johnny #47-2). The conftest guard now also points every module-level copy of the home at the test's stand-in (`_HOME_COPIES`: `service.DPC_HOME_DIR`, the copies in `p2p_manager`, `device_context_collector`, `certificate_handler`, `discord_coordinator`, `local_api`, `telegram_service`, `voice_service`, `single_instance`, and `dpc_protocol`'s `crypto` (with `KEY_FILE`, `CERT_FILE`, `NODE_ID_FILE`), `commit_integrity`, `markdown_manager`, `pcm_core`), and the default arguments that hold a home path (`_HOME_DEFAULTS`: `LLMManager.__init__`'s `providers.json`, `single_instance.acquire` and `release`). A test that sets its own home patches after the autouse fixture and wins. One line of production code moves with it: `CoreService` makes its home with `mkdir(parents=True, exist_ok=True)`, the same on a real machine (the user's home exists) and needed for the stand-in, whose parent does not; making the stand-in for every test instead put a folder into every `tmp_path` and broke two process-registry tests that list theirs. `test_a_test_run_never_writes_the_real_home.py` hashes the real `config.ini`, `privacy_rules.json` and `providers.json` (read only) before and after a full `CoreService()` and finds them unchanged and the two writers' files on the stand-in; another test parses the package and `dpc_protocol` for module-level home copies and fails on one the guard does not list (it found three a grep had missed: `telegram_service`, `voice_service`, `single_instance`). **Not covered:** `Path.home() / ".dpc"` built at run time inside functions; a probe with an audit hook saw `CoreService()` under the guard still read the real `conversations/*/metadata.json`, `peer_cache.json`, the groups registries and `voice_transcription_settings.json`, and make `agents/default` and `conversations` (both already there, `exist_ok`), writing no file; a child process a test starts. **[inferred]** what `CoreService()` wrote before the guard: from the code (`ContextFirewall` creates and prunes `privacy_rules.json`, `LLMManager` writes a default `providers.json`); a before-run on the real home was not made, since it would have written there.
  - **Text** (Forge #48-2, #48-3, #48): L12's CPU bands are not monotonic and the step at 120/121 s is the table's ("Starting figures"); `penalty_s` over 60 s is carried from RU 10 s, not measured ("GPU figures on speech"); Forge's 0.6–0.9 s reload is a direct `from_pretrained` outside the backend ("GPU figures, the first", "Load"). In the code's comment beside `_START_FIGURES` too.
  - **Two comments** (Forge #54, Johnny #53-4; text only, values kept): what `_TWIN_RAM_MIB` and `_RAM_MARGIN_MIB` stand for, from Forge's RSS measurement ("Enough RAM"); the carrier of the CPU start figures, a chat-reported table slower than `cpu_run.log` and Forge's runs, kept as conservative seeds ("Starting figures").
  - **Addresses** (Ark #49-5). Lists of line numbers are replaced by the names of the functions they pointed at: the S3 row's files, Scope's list, "Ark #53, minor", the "Did not hold as written" list and the S3c places under Review history. Single inline citations elsewhere are kept.
  - **The name.** The agent tool's cap (Mike 2026-11-23) was built in `674b1971`, which this record called "the S3 follow-up 6 commit"; it is named by its hash now, and "S3 follow-up 6" is this note.
  - **Not done here:** `vram_mib` per band, the reserves, L11's fit (S3e), the music lease (S4).
- **S3 follow-up 7 (CC, 2026-11-23, branch `dev`, on `86b158bb`): group-chat reviews #58 and #61 Ark, #59 Johnny, #60 Forge of S3d (`c4590d19`) and the commits after it, up to `9ae34f92`.** For review. Follow-up 6 had already closed what the four repeat about it: the per-device reload set (Ark #58-1, #61-1, Johnny #59-3), the loading ETA on the route's device (Ark #61, #49-2), the carriers of the twin's 60 s and of `let_peers_use_cpu_twin` (Forge #60).
  - **A load figure per device and model** (Ark #61-1). Follow-up 6's set was per device, so a second model on a device that had loaded another read the reload figure (0.8 s on cuda) for a load that reads weights no load had read: short, the side that admits a peer too easily. The set is now `_LOADED_WEIGHTS`, of (device, model) pairs; the twin and a configured `device: cpu` alias of one model share a pair. Ark's other proposal, a separate figure for "torch imported, these weights not read yet" (Forge: the twin 1.09 s, the GPU's load step 1–2.4 s), is a new start figure and waits for Forge's staged run; until then such a load reads `load_first_s` and errs long.
  - **The twin checks the RAM in its own load** (Ark #61-3; the S3d note's unguarded case). `_load_model` of a twin runs `cpu_ram_check` before anything is imported, under the twin's load lock, and raises "the CPU twin '<alias>' does not load: <N MiB of host RAM available, 5120 MiB needed>". Outside section 3.5 it is the call's failure (no switch in mid-flight); inside it, `transcribe_with_route` turns it into the refusal. The seam's check stays: it is what L12 decides on.
  - **The INFO line says what L12 compared** (Ark #58-2, #58-3; Johnny #59-1, #59-2). Behind a local engine that stays on the card the line read "against the GPU long (unknown), behind a local engine on the card", which reads as a measurement; it now reads "against the GPU not compared: its wait is not estimated behind a local engine on the card (item 1a)", and `last_route` carries `gpu_compared`. A busy Strata at factor 1 read "x1 for a busy Strata", a penalty the arithmetic did not apply; it now reads "a busy Strata, cpu_contention_factor 1, no penalty". L12's text says both (point 4, "`cpu_contention_factor`").
  - **`let_peers_use_cpu_twin: 1`** (Ark #61-6). A JSON 1 turned it off while the string "1" turned it on; 1 (an int, not 1.0, not 2) now turns it on.
  - **One `queue_wait_s` default** (Johnny #59). The agent tool's `_DEFAULT_QUEUE_WAIT_S` (600, for a context with no Settings) is now pinned to the seed table's value; the UI's `TRANSCRIBE_QUEUE_WAIT_MS` was already pinned to Settings (`test_the_ui_waits_for_the_dictation_bound_plus_the_margin`). Ark #61 asked to name the chain: both the dictation door's bound and the agent tool's cap read `queue_wait_s` from `config.ini`; the UI's 840 s is a constant and does not follow a value raised above 600 ("The UI timeout", S3b), so a raised value makes the UI give up before the backend at the dictation door. The agent tool is not under the UI's timeout.
  - **Text** (L12): the snapshot at the seam and "not compared" behind an engine that stays (Ark #61-4, #58-2, Johnny #59-1); what the factor reads and the threads to name (Ark #58-3, #61-2, Forge #60); the cpu `load_first_s` 9.0 as a cold process's against 1.09 s measured (Ark #58-1, Forge #60); RAM on this machine, the two checks, a warm twin's host RAM, the gate not model-aware ("Enough RAM"; Forge #60, Johnny #59-4, Ark #61-3).
  - **Read and written down, no change.** (a) **Section 3.5 caps the length, not the time** (Ark #58-6): within `cpu_max_audio_s` and the RAM check the twin takes the file whatever `cpu_max_s` and the factor say; the S3d note says so ("Section 3.5"), so with a measured factor above 1 a twin inside its own turn can run longer than `cpu_max_s`. (b) **0 does not turn the CPU route off** (Forge #60): `cpu_max_s`, `cpu_max_audio_s` and `cpu_contention_factor` read a value not above 0 as the default (S3d, "The settings"), unlike `idle_unload_s` (0 = never) and `queue_wait_s` (0 = do not wait); there is no off switch, and a small positive `cpu_max_audio_s` is the nearest. Whether 0 should mean off is for Mike. (c) **`let_peers_use_cpu_twin` is an alias key in `providers.json`**, false unless set, not a `config.ini` key, so it is not in `_SEEDED_KEYS` (Ark #58-5, Forge #60); S5 puts it beside the device selector. (d) **Forge's swap check needs a file over 300 s** (Johnny #59-1): with Strata resident a file up to `cpu_max_audio_s` takes the twin, so "a dictation with a resident Strata swaps it out" is only reachable with a longer file, one of unknown length, or a cold twin the RAM gate refuses. (e) **The full suite of `0d5d8284`'s body** (Johnny #59): "14292 passed" and one failure, the 1790 s pin, was a run of `pytest tests` with no `-k` (nothing deselected) at `674b1971` **[inferred]** from the failure it names; the log was not kept and the skip count is not known. This follow-up's full run is reported with its HEAD.
  - **Not reproduced or not a defect.** (a) **A RAM gate that closes the CPU path "with no reason in the log"** (Ark #58-4): the INFO line names it ("..., but the twin is cold and N MiB of host RAM available, 5120 MiB needed", pinned in test 30), and Forge #60 measured the gate passing on this machine. (b) **`route_view`'s probe draws an arrival** (Johnny #59): the probe has to stand as a request arriving now for `_blockers` to see the places ahead of it, and a number drawn and dropped changes no order between real waiters, which compare arrivals; the probe is never reused. (c) **Ark #61-5** reads the "unknown is long" branch as item 1a working as decided; Johnny #59-1 reads the same branch as a cost nobody priced (a swap he estimates at 4–35 s for a 60 s file against 66 s on the CPU). Both describe the code correctly; which is wanted is item 1a's, Mike's.
  - **For S3e/S4** (listed, not built): the RAM gate from the model's measured size, with a coefficient declared a hypothesis until measured (Ark #61-3); `vram_mib` by band, the reserves, L11's `fits` (S3e); the music lease (S4); the staged measurements in Forge's run (the factor beside a busy Strata, `_CHEAP_YIELD_S` against a real `yield_card`, the load figures with torch imported, `available` before and after each step).
  - **For Mike** (each changes a decision or adds a user-facing behaviour): whether item 1a should price a swap behind an idle engine instead of reading its wait as long (Johnny #59-1 for, Ark #61-5 reads the current rule as intended); whether a running twin, or llama-server, should count as CPU contention for point 3, which today names a busy Strata only (Ark #61-2, #58-3); whether 0 should turn the CPU route off (Forge #60); whether the three L12 numbers should be seeded into an existing `config.ini` as `idle_unload_s` and `queue_wait_s` are (Johnny #59: item 7's rule was decided for those two).
  - **Decided by Mike, 2026-11-24** (the four questions above, Q1–Q4 in Mike's answers; Q5 and Q6 from the S3e notes below): **Q1, no:** item 1a stays; behind an idle Strata a long file's wait reads as long, no swap-cost pricing (Ark #61-5's reading). **Q2, yes:** a CPU twin already running a transcription, or a running llama-server, counts as CPU contention; read as "computes something now" (a busy resident engine by its own test, a llama-server off the card with a call in flight, any local Whisper transcription running on the CPU), built in S3/S4 follow-up 8 (L12 point 3). **Q3, yes:** 0 in `cpu_max_s` or `cpu_max_audio_s` turns the CPU route off, built in follow-up 8 (L12 point 2). **Q4, yes, already built:** the three L12 keys are seeded into an existing `config.ini` (S3d). **Q5, yes, already built:** the base config's `--vram-reserve-mib` is a reserve layer (S3e, `5984876e`). **Q6:** L11's heading reads decided and built.
  - **Not done here:** `vram_mib` per band, the reserves, L11's fit (S3e), the music lease (S4).
- **S3e build (CC, 2026-11-24, branch `dev`, on `90f877a6`): the reserves, L11's shared cell and live veto, `vram_mib` by band, `predicted_vram_mib`, the cache clear, a twin that does not load, Zcode (iii) and (iv).** For review. Two commits: `5984876e` (the reserves, its own test) and the one after it.
  - **One reserve per engine** (`5984876e`; Mike #7, #15). See L11, "One reserve per engine", as built. **Read as:** "the value that is configured" includes the base config's `--vram-reserve-mib`, because that is what the engine is started with when the alias sets none; the layers are the start config's own order (alias typed field, alias `model_params`, base config, then 2048). The peer gate's refusal and its INFO line name the number and its source. `test_strata_is_offered_to_peers_on_the_owners_terms` pinned the old bar (total − 2048); it now bars at total − 1536, the test base config's flag: Mike's decision, not a new behaviour. **Decided by Mike, 2026-11-24 (Q5):** the base config's flag is a reserve layer, as built.
  - **`vram_mib` by length band** (Johnny #30-2, Forge #31-6, #42-6, #60). See L11 point 1, "The flat figure was a conscious choice, replaced by the band in S3e". **Read as:** the brief's "batch 16: 1984–2088" left out ru_60 (60.0 s is the up-to-60 s band, batch 16, two chunks in one batch, 2618 reserved), the band's largest, so the band takes 2618. The call's figure is `whisper_vram_mib(model, audio_s)` and travels with the request (`CardLease.use(..., vram_mib=)`, `_LeaseWaiter.vram_mib`), since a lease outlives its calls; the lease's own `vram_mib` stays the worst band. L11 is its one consumer. The CPU twin holds no lease and has no figure.
  - **L11's one shared cell** (`card_lease.l11_arithmetic`, `_shared_holders`, `_try_beside`, `route_view`'s `fit`). Readings chosen where the record is silent or reads two ways:
    - **`shares_with` is a predicate on the holder**, not an awaitable that runs the arithmetic (3.1's text): the arithmetic needs every resident and the card, which only the lease module sees, so it is written once there, and a client says only whom it may share with (Whisper: a lease of kind `music`). A lease with no `shares_with` never shares, so music × Whisper and every other pair never co-grant (test 18).
    - **The cell opens only when every blocker is a busy lease holder the contender shares with.** A supervisor shuts it before any arithmetic (Mike #68, Forge #33); so does an idle or draining holder (asked to let go as before) and any earlier waiter in either line, the stricter reading of "no earlier waiter blocked by that music lease": every earlier waiter is a blocker already, and overtaking one by a co-grant is what L5a forbids.
    - **Residents are every holder on the card but the contender** (`_live_holders()`, point 0); a lease's peak is its `vram_mib`, a supervisor child's its `predicted_vram_mib`; any None is `unknown`.
    - **`growth_left` is the whole declared peak of every lease resident**; a supervisor child allocates at its start (llama-server its KV, Strata its cache to what is free), so what it holds is in `free` already and it grows no more. In the one cell the only resident is the busy music lease, so this is L11 point 3's "the music peak whole".
    - **The reserve is the resident engine's own** (`vram_reserve()`, the largest when more than one, never a sum), and 2048 with no engine on the card. Since an engine shuts the cell, L11 counts 2048 in every reachable case today; the alias's `vram_reserve_mib` enters a fit only where an engine can be a resident of one (the arithmetic and test 28).
    - **WDDM `free`**: kept as L11 and 2.4 write it (the veto refuses whenever an engine is resident; whether to keep it on WDDM is learned in operation); no special case.
    - **Where it is wired.** L12 point 1 at the seam: `route_view` answers `fit`; `fits` sends the call to the GPU at once and the L12 line says "L11 fits it beside …"; `does_not_fit` and `unknown` are appended to L12's own why, and L12 goes on as before. The grant: `_enter` tries the cell before the line (so a peer or a background call that fits is granted beside, and one that does not meets the rules after it as before) and in each round of the line once per change of the shared holders (one reading per change, not one per 5 ms poll). After the awaited reading the blockers are read again and the arithmetic runs on that stretch's residents (L1; point 4).
    - **A larger band on a grant made beside** (not in the record): the grant records the peak it was fitted with; a new use that declares more (a 90 s dictation on a grant fitted for a 10 s one) is fitted again while the shared holder is busy, and waits for it when it does not fit. An idle shared holder does not hold it back.
    - **The lines:** `card: <display> loaded beside <music display> (fits: <need> of <total> MiB declared, <free> MiB free; reserve <R> MiB, <source>)` and `card: L11 says does_not_fit|unknown for <display> beside <music display> (<why>)`.
    - **Dormant until S4:** no production lease has kind `music` before S4, so no production call reaches the cell; tests 18 and 28 use fake music leases. (Live since S4: test 4 grants a real Whisper beside a real yue2.cpp job.) `conftest.py` starts every test with no reading (`_the_cards_vram_is_unknown_unless_a_test_says`), as a CI runner has none.
  - **`predicted_vram_mib`** (L11, "a llama-server child's peak"): a class attribute of `LlamaServerSupervisor`, cleared at each `_launch`, set when the child answers `/props` from the `_admission()` its start computed (kept as `_last_admission`, so no second nvidia-smi) and the KV type it was given (`cache_type`, else `cache_type_k`). None for a start that could not size the model and for Strata. **S2's question answered from the code:** Strata never calls `_admission` (its `ensure_running` has its own launch closure), so it has no figure by construction, not a `KeyError`.
  - **The cache clear** (L11, "The cache, exactly"): `commit_memory.forget_vram_reading()`. **Read as:** the record's "every grant and release" and the brief's "after a load or unload changes the card" both: the grant, `mark_running` (the load under the grant changes the card after the grant was made) and the release; and L11 after a None reading.
  - **A twin that does not load** (Johnny #63-5): `TwinDidNotLoad`, raised by the twin's `_transcribe_holding` when its load fails, its own RAM check included; never for `ModelNotCachedError` (the GPU alias shares the model, so the line would wait only to fail the same way). See L12, "No switch in mid-flight", amended.
  - **Zcode (iii)** (`_stop_local_engine`'s timeout, group #22): no code change; nobody passes a `timeout`, so the bound is `LEASE_STOP_BY_HAND_S` 120 s inside the call, and the UI's 180 s outlasts it. Pinned (`test_s3e_pins_the_stop_by_hand_bound_and_a_peers_strata_refusal_by_a_lease.py`).
  - **Zcode (iv)** (`_card_held_error`, `_why_a_peer_may_not_swap`), read with grep: the first is called by `_claim_card` and `strata_provider._peer_start_refusal`; the second by `_a_peers_request_that_may_swap`, `_free_the_card` and the same Strata gate. **Found and fixed:** the P2P door asks the Strata gate before it marks the call (`p2p_coordinator._local_engine_refusal` runs outside `serving_a_peer`), so with a card lease holding the card `_card_held_error` answered in the owner's words and the foreign node read "the card is held by whisper alias 'whisper:turbo'" (red on `5984876e`, verified). The gate now asks both under the peer mark: "this node's GPU is busy with another job right now". The S2 note's "a peer's refusal by a lease names no holder (… `strata_provider._peer_start_refusal`)" held only for a gate asked under the mark.
  - **The UI timeout** (`coreService.ts`, item 6): the record says nothing more for S3e than "Not done here (S3e): … `TRANSCRIBE_QUEUE_WAIT_MS`" (follow-up 3). Done: a comment naming the carrier (the seed table's `queue_wait_s`) and a vitest pin of 600 000 + 240 000 (`transcribeTimeout.test.ts`). **A known third carrier of 600**: the seed table, the agent tool's `_DEFAULT_QUEUE_WAIT_S` and the UI's `TRANSCRIBE_QUEUE_WAIT_MS`, each pinned to the seed; a value raised in `config.ini` still does not reach the UI.
  - **After S3d the VRAM path of L11 is rare** (Forge #64, finding 2, written down as asked): with Strata resident most of the time, a file up to `cpu_max_audio_s` takes the twin and a longer one swaps the idle engine out first, so the arithmetic a dictation meets is mostly the twin's host-RAM gate; L11's VRAM arithmetic is reached only beside a busy music job with no engine on the card, which S4 makes possible.
  - **For Forge's live run** (nothing here can be measured): the Strata peak on v0.2.20 (the engine holds about 650 MiB more: "need 1802 MiB: reserve 1152 + 650 post-cache", Zcode #22), so the derived 30575 and 31471 stay derived; Whisper's peak beside strata.exe at batch 16, 4 and 2 (the up-to-180 s band's 2900 is estimated, and the other bands come from an empty card); a live `fits` needs S4's music lease; the live veto on WDDM. Upstream Strata `00810bdd9` ("free VRAM" guards, 15 commits past our pin) is noted for L11: when the pin moves, what the engine leaves free, and so what `free` reads beside it, may change **[unverified: the commit was not read]**.
  - **Not built here:** the RAM gate sized from the model's measured size (Ark #61-3; its coefficient would be a hypothesis until measured); test 21's third contender (it needs S4's music lease); the `model_size_bytes` source in the fit's line (since S3b the figure comes from the measured table only, so its source is always "measured", else the peak is None and the outcome `unknown`); the music lease (S4).
  - **Tests.** New: `test_one_reserve_per_engine_strata_reads_the_alias_vram_reserve_mib.py` (first commit), 18 `test_whisper_shares_with_busy_music_only_when_it_provably_fits.py`, 28 `test_the_fit_counts_every_resident_not_only_those_in_the_way.py`, `test_whisper_declares_its_vram_by_length_band.py`, `test_the_cards_vram_reading_is_dropped_on_every_change_of_holder.py`, `test_a_llama_server_child_records_the_vram_its_start_predicted.py`, `test_a_cpu_twin_that_does_not_load_sends_the_call_to_the_card_line.py`, `test_s3e_pins_the_stop_by_hand_bound_and_a_peers_strata_refusal_by_a_lease.py`; UI `transcribeTimeout.test.ts`.
  - **For Mike:** L11's heading still reads "proposed, pending Mike's OK"; the S3 row asked for it and it is built, dormant until S4. **Decided by Mike, 2026-11-24 (Q6):** the heading reads decided, built in S3e and live since S4.
- **S4 build (CC, 2026-11-24, branch `dev`, on `ac9307d5`): music holds the card lease; where this record was read one way, and why.** For review.
  - **One use per job: `in_use` is the runner's use** (3.2: "`in_use()` enters `card_lease.use(what)` and counts `_busy` in the same place" and "the runner … `card_lease.use(…)` around `start_job`, `in_use` and the production steps"). **Read as one use, not two nested ones:** a second use opened by `in_use` after the runner's grant would, by L5, wait behind a contender that arrived in between and waits for the first use, so the job would wait on itself until cancelled. So `SupervisedEngine.in_use(what, **acquire_kw)` is `card_lease.use(...)` with `_busy` counted inside it, and the runner calls it with `wait_s=None`, its `check`, its `on_wait` and its `expected_s`, from before `start_job` to the end of the job.
  - **A start never waits for the card, and asks before the lock** (3.2 as first written: "`start()` itself asserts the lease is granted and never acquires"; 3.2 now states what is
    built, follow-up 9c). **Read as:** `start()` = `_card_for_a_start()` (a grant with `wait_s=0` when the engine holds none, before `_the_lock`; refused at once, `MusicEngineError("card_busy")`, naming the holder) then `_start_child()` (under the lock, which acquires nothing and refuses with `card_busy` if the grant was let go in between). The lock order Ark #44 asked for holds, no path can load without a grant, and a direct start (the engine tests, a caller that forgot) gets the by-hand behaviour instead of an assertion. The start by hand asks the same way, labelled "start by hand"; as built in S4 it came after `MusicEngines.exclusive(wait=False)` had stopped an idle other music engine, which follow-up 9b reversed (the card first, the other engine stopped only once the card is granted); the lease is never asked under the switch lock either. A by-hand start that brought no child up keeps no grant (`release_when_empty`).
  - **A yue2.cpp restart for boot options keeps the card** (not in the record). `Yue2Server.start` stops a child that runs another backbone or lacks an adapter split (`stop("swap")`) and starts a new one inside one call. L8 releases on every stop, and the start would then fail its own grant check, or hand the card to a waiter for the seconds between the two children. **Read as:** one start, two children: `stop(keep_the_card=True)`, the lease marked `starting` first (busy: never asked to let go), let go only if the new child does not come up.
  - **Released on every end of the child** (3.2, L8): `stop()` in all its words, `_watch` ("exited on its own"), a start that ends with no child alive ("failed_start", also a refusal before the spawn), `shutdown()`. **The stop path** (`engine_base._stop_proc`, 9a): the child is signalled (SIGTERM; CTRL_BREAK on Windows), given `STOP_GRACE_S` = 15 s to exit, killed, and waited for another `STOP_GRACE_S`; a child still there after that is logged and no longer counted as DPC's, so the card is let go and is not held for ever for a process that will not die. `starting` from just before the spawn, `running` once `/props` answers. `_yield_card`: not alive → `nothing_to_give`; `_the_lock` held or busy → `refused`; else `stop("swap")` → `let_go` (`could_not_let_go` if the child is somehow still alive). After a job's use closes, a grant with no engine running goes back (`release_when_empty`, `holds=engine.alive`), so a job cancelled before its engine came up leaves no warm grant with nothing on the card.
  - **The other music engine.** Every lease excludes every other, so a yue2.cpp job's use waits for `music:acestep` (an idle one is asked to let go, which stops it; a busy one is waited for). The job therefore waits **queued**, its `card_wait` naming `music:acestep`, where before S4 it waited **running** inside `MusicEngines.exclusive`. `exclusive` stays and agrees (3.2). `test_only_one_music_engine_is_on_the_card_at_a_time.py` follows (listed in 2.3 as changed on purpose).
  - **Two gates accept `running`** (3.2, tests 17, 29). `jobs.set_waiting_for_card` takes `queued` and `running` (every other state still raises `illegal_transition`), and so does the schema's `card_wait` check (`job_schemas.validate_job`); every move out of `queued`/`running` clears the wait, and a running job that waited is settled at a restart as before. The runner's second gate is the re-acquire in `_start_engine`: when the job's engine has no grant (its child ended under the job's use), `card_lease.acquire(…, wait_s=None, check=…)` before any lock, with a fresh arrival; it ends on the grant or on a real cancel (`_Cancelled` from `check`), never on the job's state. `test_a_music_job_moves_only_along_the_spec_state_machine.py` pinned the old refusal for a running job; it now pins the running wait and the refusal after `failed`.
  - **What `card_wait` names.** `card_lease.holders_in_the_way(lease)`: `{alias, engine}` of the holders in the way of the lease's earliest waiter, else of the waiters and card-queue places ahead of it; read in the lease's `on_wait` (one INFO line and one call per change of who it waits for), written by one task at a time so the last change wins, cleared at the grant. The record keeps `alias` and `engine` only (S2 item 8 note: "S4 owns that record"): the Stop X button reaches a lease by its holder name since S2 item 8, so it needs no more.
  - **The job's cancel ends its wait.** `check` reads the job every `card_poll_s` (3 s) and at once after every `wake()` (a cancel wakes the runner), not on each 0.1 s round of the line; `_Cancelled` takes the waiter out (`_leaves`). Zcode #54's known debt (1) is closed for music: every music wait goes through `use()`; the two bare `acquire` calls are a start's `wait_s=0` (no line, so no cancel can race a grant) and the running re-acquire under the job's open use (`_leaves` keeps a grant while a use is open).
  - **The declared peaks** (L11: "a music holder's peak is its declared `vram_mib`"): yue2.cpp 5600 MiB (trial table: 5222–5570 MiB whole-card peaks, already MiB; "the larger figure"), acestep.cpp 9300 (its trial's "about 9.3 GB", read as MiB, the larger reading). Neither grows past its peak in v1 (`growth_left` is the whole peak, L11 point 3).
  - **Expected time and ETA** (L10). A job that has not started carries `expected_s = min(estimate_yue2_seconds(lyrics), duration_s) × yue2_factor(that) × variants` (`job_schemas.music_expected_s`; **as S4 built it, the product; flipped in follow-up 9b to a division by the factor, audio seconds per wall second, L10**): capped by the duration because a YuE2 song ends by itself and never runs past its cap (record silent). acestep.cpp: unknown (no measured factor). A running job: unknown, as v1 decides; the progress signal L10 says S4 "may" add (variant start times, a recorded per-machine factor) is not built.
  - **The start by hand is refused by any other holder** (**superseded in part by follow-up 9b:** an idle holder with no queue is let go of for it; a busy one, or one with a queue, still refuses) (3.2: `acquire("start by hand", wait_s=0)`, "refused naming the holder"). Before S4 a start by hand looked at no holder but the other music engine, so it loaded beside a local LLM or a warm Whisper; now it is refused (`card_busy`) with the holder's name, and the person stops that holder first. Built as written; it changes what the Start button does with Strata resident.
  - **Events and UI.** No new event: the job's wait goes out as `music_job_updated` with `card_wait`, as before. `musicCreate.ts` shows the holders and "Waiting for the GPU: X holds it" for a `running` job with a `card_wait` too (3.2, section 4), and `stopCardHolder` asks such a job again after a Stop; `musicCreate.test.ts` pins it. The rest of the UI is S5's.
  - **Not built here:** a running job's ETA from progress (L10 "may"); S5's holder and queue lines; the SPEC's decision-32 amendment (the SPEC file is outside this repository, 3.2); Q7's music engine boot and reload time (unmeasured).
  - **For Forge's live run** (music; nothing here can be measured): a live L11 `fits` with the local engine stopped and a YuE2 job running (log `need`, `total`, `free` and the real `nvidia-smi used`; Q6: whether `free` falls by the child's real use under WDDM); the YuE2 peak beside Whisper against the declared 5600; the wall time of one YuE2 variant against its received length (the factor's orientation is decided, 2026-11-24; this checks its figures); an idle engine's stop-and-reload cost when a dictation asks it to let go (Q7); the `card_busy` words of a start by hand with Strata resident; a job waiting queued behind a busy dictation, then running.
  - **For Mike (yes/no):** (1) YuE2's factor: flip L10's orientation to audio seconds per wall second (a 240 s song took 80.5 s and a 30 s one 8.9 s in the trial table, load included), so a queued job's estimate is about a tenth of what it reads now? Built as the record oriented it then (test 34 pinned "80 audio seconds at factor 3.5 read 280 s"; since follow-up 9b it pins about 22.9 s); the estimate only routes and informs, and a long one routes a dictation to the CPU twin and refuses a peer, the cautious side. (2) The start by hand refused by a warm idle Whisper (`wait_s=0` asks no idle holder to let go): keep, or let it ask an idle cheap lease (one unload, about a second) before refusing? **Answered by Mike, 2026-11-24 (group chat, #84 of the new count): (1) yes, flip it; (2) yes, let it ask an idle Whisper and, with the swap on, an idle Strata or llama-server (CC's options a and b: "anything idle with no queue may be unloaded"). Built in follow-up 9b.**
  - **Tests.** New: 4 `test_a_transcription_during_a_running_yue2_job_waits_and_names_the_holder.py` (the Done-when, its deadline refusal, L11 `fits` live, and the reverse: an idle warm Whisper let go, a busy one waited for queued and named), 11 `test_a_music_engine_lets_go_of_the_card_however_it_stops.py`, 15 `test_a_music_engine_start_never_waits_for_the_card_under_its_own_lock.py`, 17 `test_a_music_job_that_waits_while_running_shows_its_holder.py`, 21 `test_a_third_contender_asks_only_holders_in_its_way_cheap_idle_first.py`, 29 `test_a_running_job_waiting_for_the_card_does_not_start_its_engine.py`, 34 `test_a_running_music_jobs_eta_is_unknown_until_it_has_progress.py`. Changed: 13 in `test_a_generate_from_text_job_runs_end_to_end_on_the_supervised_engine.py` (a fake supervisor claim in `_CARD_CLAIMS`, "nothing is stopped" and the exact `state_reason` kept), `test_only_one_music_engine_is_on_the_card_at_a_time.py`, `test_a_music_job_moves_only_along_the_spec_state_machine.py`, the rigs (`_acestep_helpers.Rig`, `_yue2_helpers.DualRig`: no `holders`) and `test_the_music_engine_commands_report_start_and_stop_the_acestep_server.py` (no `card_holders` reader); removed `test_until_s4_a_music_job_asks_an_idle_whisper_to_let_go.py`; UI `musicCreate.test.ts`.

- **S3/S4 follow-up 8 (CC, 2026-11-24, branch `dev`, on `b67b3cc0`): Mike's answers of 2026-11-24 built.** For review.
  - **CPU contention (Q2), read as "computes something now"** (as built in follow-up 8; **narrowed in follow-up 9b**: a llama-server kept off the card no longer counts, and the embedder and GLiNER do, L12 point 3). Mike: "a running twin, or llama-server, should count as CPU contention". **Read as:** a llama-server serves on the GPU but takes CPU threads for its own work while it computes, so a resident one counts when it is busy by the same test as Strata (`_busy_for_the_card`: a call in flight, a turn standing on it, a caller in its window, a child starting), not merely because it is resident; an idle resident one is no contention. A llama-server off the card (`--device none`) has no claim and no card state, so it counts only while a call is in flight on it (`_in_flight > 0`). "A running twin" is read as any local Whisper transcription in flight on the CPU, a twin's or an alias's whose model runs on the CPU (one CPU, one load), counted per call around `_transcribe_loaded` (`_CPU_RUNS`), not a loaded twin. The factor is applied once, however many contend; the INFO line and `last_route["contention"]` name each. `card_lease._a_cpu_engine_is_busy` became `_cpu_contention` (a list), and `route_view`'s `cpu_busy` became `cpu_contention`. Tests: `test_a_running_cpu_transcription_or_a_busy_llama_server_is_cpu_contention.py`.
  - **0 turns the CPU route off (Q3).** `settings._local_transcription_figure(..., zero_is_off=True)` for the two caps and `whisper_provider.cpu_route_settings` (`_CPU_ROUTE_SWITCHES`, `_a_zero`) read 0 as 0; below 0, unreadable or not finite still reads as the default (as before); the factor keeps its reading. In `_choose_route` the switch is read right after point 1, so a free card and L11's `fits` still give the GPU; inside an agent's own turn (3.5) it is refused at once before the cap or the twin is read (the twin is not built); a peer allowed onto the twin goes to the line. The seeded lines say "0 = never use the CPU"; an existing `config.ini` keeps its old comment (the seed writes a key only when it is missing). Tests: `test_zero_turns_the_cpu_route_off.py`; `test_l12_settings_peers_and_a_twin_that_is_never_the_default.py::test_the_provider_reads_the_registered_source_and_falls_back` re-pinned (its `cpu_max_audio_s=0` read as the default; now `-1` does, and 0 is off).
  - **A peer may stop an idle engine (Mike, 2026-11-24).** L13 "As built". The predicate moved out of the supervisor method, unchanged, to `why_a_peer_may_not_swap(holders)`; the method and `card_lease._swaps_engines` both call it. **Read as:** "the same owner terms" includes "no holder already stopping": a peer behind a supervisor that is already going keeps S3a's reading (its stop counted, no new mark), and one behind a going supervisor and an idle one together is refused, as the inference peer is. Tests: `test_a_peer_may_have_an_idle_engine_swapped_out_on_the_owners_terms.py` (swap off, an owner's waiter, an owner's card-queue place, a busy engine, no figure, stop plus work over 120 s: refused at once and nothing stopped; idle, swap on, fits: stopped and served; one predicate for both lines); `test_a_peer_lease_waits_only_when_its_estimate_fits_its_deadline.py` re-pinned (busy half kept, idle half split by cause).
  - **Q1 (no), Q4, Q5, Q6:** text only, see "Decided by Mike" and the notes above.
  - **For Forge's live run:** a busy llama-server or a running twin raising the factor in the INFO line on this machine; a peer's transcription behind an idle Strata on a second node (the stop, the swap note, the answer within 120 s); `cpu_max_s = 0` in `config.ini` sending a dictation behind a busy Strata to the line.

- **S3/S4 follow-up 9 (CC, 2026-11-24, branch `dev`, on `bf11758b`): Mike's answers built, and the findings of Zcode's three reviews (#85, #87, #88).** For review. 9a is `4ad35c86`, 9b `05bcce1d`, 9c this text.
  - **9a, code from the reviews.** `commit_memory.forget_vram_reading()` never waits for an `nvidia-smi` probe: the lock covers the cached figures only, a separate probe lock keeps one probe at a time, and a reading begun before a forget answers its own caller and is not cached (`total` is kept either way); the owner loop calls it on every grant, release and `mark_running` (Zcode #88-1). L11 with a known total, an unread `free` and `need > total` is `does_not_fit`, not `unknown` (#88-6). A music engine lets the card go however a stop is cut short: `_stop_proc` kills the child and drops it on a second cancel or a failed wait, `_kill` logs an `OSError` other than `ProcessLookupError`, and `shutdown()` kills what its stop did not end and releases in a `finally` (#87-4). `transcribe_with_route` treats the door's `wait_s` as a deadline from the call's start: a call whose CPU twin did not load goes to the line with what is left of it, 0 at the least, `None` staying `None` (#88-4). Test 28 pins that the fit reads through `read_vram_mib` (#88-2).
  - **9b, Mike's answers.** The YuE2 factor flipped (L10, test 34 now pins numbers worked out from `trial.md`); a start by hand unloads what is idle and has no queue, through `acquire(yields_idle=True)` and the same ask and swap path, and stops the other music engine only after the card is granted (3.2); a refusal at once no longer says an idle holder was asked to let go; CPU contention narrowed (L12 point 3); a peer waits 60 s after the owner's last request on an engine (L13), pinned through both lines by `test_a_peer_may_swap_an_engine_only_a_minute_after_the_owners_last_request.py`, and the factor applied once for two contenders. Three ADR-044 sentences that still said "a peer never swaps" are marked replaced.
  - **9c, text only.** The status and banner of this ADR say what is built; the Whisper peak by band, the reserve in the Strata gate and the 30575 figure carry their S3e notes; 3.2 states what `start()` does (it acquires with `wait_s=0` and never waits; Zcode #87-2); the stale sentences of ADR-044, the glossary and three test docstrings follow the code.
  - **Tests of 9a and 9b.** New: `test_a_music_engine_lets_go_of_the_card_when_its_stop_is_cut_short.py`, `test_a_peer_may_swap_an_engine_only_a_minute_after_the_owners_last_request.py`, `test_a_start_by_hand_unloads_what_is_idle_and_has_no_queue.py`, `test_the_embedder_and_gliner_count_as_cpu_contention_while_they_compute.py`. Re-pinned: test 34 (literal numbers from `trial.md`), `test_a_running_cpu_transcription_or_a_busy_llama_server_is_cpu_contention.py` (a llama-server off the card no longer counts; the factor once for two contenders), the L11 tests of 28 and 18 (the VRAM peaks 5600 and 9300 imported from the engines, not repeated), `test_a_cpu_twin_that_does_not_load_sends_the_call_to_the_card_line.py` (the retry gets what is left of the door's bound).
  - **Not verified in code, left as written:** the Strata peak 30575 and 31471 stay derived; the band of 121 to 180 s keeps its estimated 2900; the live behaviour of any of this in operation.
- **S3/S4 follow-up 10b (CC, 2026-11-24, branch `dev`, on `ea3ef006`): Zcode's review (#14 of the group chat) of 9a, 9b and 9c.** For review. Code is `b78a7cc1`, this text the commit after it.
  - **A shutdown cut short no longer releases the card under a start** (`engine_base.shutdown`): when the stop was cancelled while a start owns `_the_lock` and has no child yet, the grant is the start's and it lets go on its own failure path; a child that exists is still killed and the card let go. **Read as:** Zcode's candidate was "skip kill and release while the lock is held"; that would also have left a live child running after a cancelled shutdown, and the 9a test where the lock is held by a fake owner with a child, so the skip is only for the lock held with `_proc` None. A start that then succeeds keeps its child with the lease; a failed one releases once.
  - **A doubly cancelled start ends in `error`**, not `starting` (the state is set in a `finally` around the stop).
  - **The certificate wait's cleanup is not decided by the clock** (`file_transfer_manager.wait_for_certificate`): found as a real defect, not test timing: on Windows a timer can fire one 15.6 ms tick early and the entry stayed behind (7 of 1000 waits of 10 ms). It deletes the entry it stamped unless a later ask or wait stored a new one.
  - **The embedder on `device: auto` already counted the device it resolved to** (the provider's `device` property resolves cuda, then mps, then cpu, and the count reads it); no code change, a test pins auto to cpu counted, auto to cuda and to mps not.
  - **Wording:** `start_music_engine` and `refuse_while_switching` say what holds (a refusal before the grant stops nothing; one after it can follow an idle holder the lease already let go); the glossary and `LeaseSkipped` say the CPU twin is built and the door that sends a background transcription into L12 is not; the stop path above names `STOP_GRACE_S`.

## Review history

- **#41, Zcode** (architecture accepted; 11 of 14 load-bearing claims true, 3 corrected).
  Taken: the cross-line arbitration rule and its starvation test (L5a; test 3b); dictation
  during music as decision 6 with ETA-early-refuse as an option; `_CARD_CLAIMS` pruning
  sites; the holder-discovery path in the surface list; `_free_vram_mib`'s location; the
  frontend already reading card keys (12 files), card 1925 stale, S5 smaller; test 9's
  supervisor variant; the kept "yield during a waiting acquire" test (14); the "loaded
  beside" log line; the 90 s claim in one sentence; card (f) for a device in the embedder
  cache key. Votes on decisions 1–5 recorded.
- **#44, Ark** (design accepted). Taken: the music lock order (lease before `_the_lock`,
  test 15); the lease's duck surface with semantics (2.2.1), including no new use after the
  mark and `_draining` for the whole yield; one source of busy; `jobs.py` accepting
  `running` (test 17); the plain statement of what S1+S3+S4 do not change; `excludable=True`
  and the CPU fallback in one slice (S9); the swap-off background skip as its own Negative
  (test 16); the UI's `card_holder`/Stop button and pinned sources. Corrected: see Review
  notes.
- **#45, Ark** (c8782ec4 review plus ADR findings). Taken: "refused" vs "nothing to give"
  (L6, `yield_card`); one source of the music lease's busy; the swap text per holder kind
  (with the "parked 0" premise corrected); which desktop rule decides (section 5);
  `let_go_by_hand` for the Stop X button; interpreter and worker task cancellation in Risks;
  votes on decisions 1–5, the cost difference in decision 2, "right now" for peers, and the
  logged reason for an unreadable probe.
- **#49–#50, Mike** (question, not a decision): "can't dictation just be queued?" Led to
  decision 6's revised recommendation and the dropped `card_wait_s`.
- **#52, Zcode** (decision 6: (d) + queue accepted with conditions). Taken: one shared
  cell only (L11); the "one owner" invariant named as the model object's, untouched, and
  the card's exclusivity as what gives; fit including actual use and the holder's peak
  (reconciled with Ark's declared sizes); unknown → queue, not beside, not refused; a third
  contender asks cheap idle holders first, then waits for busy ones in arrival order
  (2.2 item 11, test 21); the tests for both rows, the yield order and unknown → queue; a
  dictation bound of its own (~10 min, sub-choice A); no overtaking across lines in either
  direction (L5a). Presented to Mike rather than adopted: an unknown ETA refused at once
  (sub-choice B).
- **#53, Ark** (decision 6: (d) + queue accepted with conditions). Taken: the primary
  sharing axis named (engine identity; measured headroom a narrower second axis, L11);
  yield asked only of holders in the contender's way; fit from declared sizes
  (`model_size_bytes`) plus a context figure, with "unknown" as its own outcome; a card
  refusal ends the provider cascade and a call keeps one place (test 22); the queue's
  deadline under the caller's (`min(remaining − margin, bound)`) and an explicit deadline
  for the local and Telegram paths; the queue before `_load_lock` (test 24); the peer
  re-check after the wait (test 23); the ETA never the sole basis of a refusal (proposed
  default of sub-choice B). Corrected CC's 240 s claim (Review notes).
- **#59, Zcode** (no blockers on `186f01df` or `d80d76f7`). Taken: three deletion points
  from `_CARD_CLAIMS`, not four. Confirmed independently: card 1925 half stale, the `mlx`
  test files.
- **#60, Ark** (second look at `186f01df` and `d80d76f7`; withdrew three of his #44 claims
  after grepping). Taken: §2 the fourth `_finish_thread` outcome (section 0, Risks); §3
  release conditioned on the unload having run, with the falsifier (section 3.1, L6's
  `could_not_let_go`, test 26); §4 the runner's second gate in S4 (section 3.2, test 29;
  consequence corrected in Review notes); §5 the two sums, a supervisor child's peak, one
  desktop figure, three residents (L11, test 28); §7 votes: 6A 600 s, 6B wait.
- **#61, Ark** (same review, condensed). Taken: §2a "no model" vs "model there, unload did
  not run" (test 27); §2b the two sums (test 28); votes as in #60.
- **#62, Mike** (product decision): a smart GPU/CPU choice for dictation and
  transcription. Led to L12, the CPU route in S3, S9 reduced to `excludable=True` against
  supervisors, decision 6 rewritten, sub-choice B replaced, sub-choices C (peers on CPU)
  and D (the short threshold) added.
- **#67, Ark** (before Mike's answers). Taken: the measurement at #66 confirms the cold
  start (≈9 s) and a CPU at about 1.0–1.1× real time, so the Strata check belongs inside
  the choice (L12 point 3); the Strata-turn measurement is required before rollout (the
  pending row of L12's table); the CPU twin gets its own `idle_unload_s` instead of a
  card-tied life; peers not on the CPU; the "decided without knowledge of the card" log
  line (section 5). Confirmed `_predicted_total_mib` exists (3554, called at 3566, 3610).
- **#68, Mike:** decision 1, no asymmetry (Decided by Mike).
- **#69, Zcode** (before and after #68). Taken: the symmetric exclusion and its
  consequence for a long dictation during a long turn; the order of conditions with the
  Strata check inside; no switch in mid-flight; "Strata busy" named as the live request
  hold; the twin's own `idle_unload_s`; the "without knowledge" line; withdrew his 6B
  "refuse at once" in favour of Ark's wait.
- **#70, Mike:** decisions 3 (peers wait on the inference-sharing terms), 4 (SPEC
  decision 32 amended) and the question behind 5 (Decided by Mike).
- **#71, Mike:** decisions 6 (CPU up to 5 minutes, also beside a busy Strata), 6G
  (residency only with a queue) and 6C (peers not on the CPU, optional setting).
- **#64, Ark** (the GPU/CPU choice: direction right, three wrong answers in it). Taken: §1
  compare times, not audio length; 120 s threshold dropped, a 1200 s long-audio guard and
  `cpu_max_s` in its place (L12, test 30); §2 cold and warm CPU figures at several lengths,
  the load counted when the twin is cold, the twin kept while the same holder keeps the
  card (test 31); §3 contention with a CPU-computing resident such as Strata, named in
  Consequences (test 32); §4 the CPU route as a separate provider object, the CPU twin, and
  the cascade reconciled (test 33); §5 a running music job's ETA from progress, unknown in
  v1 (L10, test 34); §6 nothing measured → the line, with the measurement table left for
  the numbers; §7 peers not on the CPU.

- **#82, Ark** (Whisper's load times from the logs; the agent that transcribes inside its own
  turn; three tests). Taken: §2 as section 3.5; the three tests are already tests 26, 28
  and 29.
- **#83, Johnny** (the PDH biases, Strata's price, the peer mechanism, CI). Taken: the quantity
  named with units and platforms (L11); the engine named in the swap words and the cost written
  down (section 2.4); the peer wait reduced to what is new (L13). Corrected: see Review notes.
- **#84, #87, #89, CC** (replies). #87 agreed with Mike's #85; #89 listed the four points of
  this revision.
- **#85, Mike** (question, not a decision): "why new? there are cards and the ADR describes
  it." Led to L13 and the Decided by Mike line. #86 and #88 are not listed: the `c3852d75`
  commit message's "#82–#89" was wider than this history (Zcode #12).
- **New count, 2026-11-21.** **#10, CC:** the CI and `c3852d75` posted for review. **#11,
  Mike:** answers to items 10–13 and the governing line (Decided by Mike). **#12, Zcode**
  (ready for S1; four nits), **#13, Ark** (the whole-trip framing; the cache's two failures;
  the peer path's missing gate and parallel calls; the CPU fallback; the torch question),
  **#14, Johnny** (L11 vs L13; `min()` always 120 s; the cache wording; the cost bounded, not
  measured; the factor as a decision), **#15, Forge** (live readings at 17:53: 1100 MiB free
  with Strata, 8.3 GB host RAM available; the reserve; the N11 offer of ComfyUI's
  `POST /free`, which waits on Mike). Taken and corrected: Review notes.
- **Seventh revision, 2026-11-21/22.** **#18 Ark, #19 Johnny, #20 and #21 Forge, #22 Zcode** on
  `ffd06369` (Review notes), and Mike's answers of 2026-11-22 (item 7 at 300 s; N11 in scope, S10
  by CC; CC measures the GPU ratio on speech).
- **Eighth revision, 2026-11-22 (text only).** CC's GPU measurement on speech folded in: the
  Whisper peak is batch-dependent, so `_WHISPER_OVERHEAD_MIB` goes 770 → 2900, `whisper_peak`
  2317 → 4447 and test 28's `need` 4365 → 6495 MiB; the GPU ratio is measured (≈16–36×); Q7
  and the GPU-ratio open item are closed; the CPU same-speech figures sit beside step 5.
- **Ninth revision, 2026-11-22 (text only).** **#30 Ark, #31 Zcode, #32 Johnny, #33 Forge** on
  `ffc81e00`, all "go for S1" after the fixes (Review notes).

- **New count, 2026-11-23 (#7–#16), text only.** **#7–#9, #15 Mike:** one source of truth for
  the reserve (#7), the carrier of item 7's 300 s (#9), and "по дефолтам" for items 1a, 2, 6A
  and 9 with "do as you proposed" for the reserve (#15) (Decided by Mike). **#14 CC:** the
  proposal; **#16 CC:** the confirmation. Reviews folded into the S3 plan:
  - **#10 Ark, #11 Johnny, #12 Forge.** **S3a:** the supervisor branch of `_estimate`
    (`card_lease.py:803-808`) takes its stop estimate from `_expected_stop_s()`
    (`llama_server_supervisor.py:4510`, overridden at `strata_supervisor.py:2816`) instead of
    `None`, so a peer is not refused behind a resident engine merely for lack of a figure;
    `expected_s` carries a cold and a warm ratio, since the first call after an unload is 3–5×
    slower (Johnny's bench). **S3c:** the places are `service.transcribe_audio`'s local branch
    and the received-voice cascade (`_maybe_transcribe_voice_message`); the guest peer-candidate path tries one peer, and there is no second `continue`
    loop (Ark correcting his own point of 2026-11-22). **S3e:** Whisper's `need` follows the
    same batch descent by audio length as the call (`whisper_provider.py:1020-1030`) rather
    than the constant 4447; the Whisper peak is re-measured beside `strata.exe` and at batch
    16. Strata v0.2.20's `--conversation-spill-persist` (opt-in, off here) means that "a stop
    loses the parking" is a property of the config, not of the engine; S3e decides whether
    the cold-read price is paid.
  - **Corrected:** the Strata peak 30575 was derived, not measured (L11, "One reserve per
    engine").
  - **Open, not decided:** the L13 rule for a peer's long audio on the CPU twin (Open for
    Mike). **Decided at #17, Mike:** refused at once as a rule when the whole trip does not fit
    the guest's 120 s, CPU twin included (Decided by Mike; L13, point 2).
- **#22 Zcode, #24 Johnny (reviews of the S3 branch), S3 follow-up 2.** Johnny #24-1: the first
  call after a load costs seconds, not a ratio (`penalty_s`). Johnny #24-2: the warm figure is
  kept by length band on the batch descent; the "keyed by device only until S3d" caveat of
  `e8b5ac1a` is removed, since it is fixed. Johnny #24-3: the recorder's length is the fallback
  when the probe reads none. Johnny #24, minor: the `config.ini` seed keeps line endings and
  reads back; the idle-unload source is wired on every production path. Zcode #22-2: figures
  carried by `_copy_state_to`; #22-3: tests import the start figures; #22-4: the peer's probe
  under the peer mark; #22-5: L11's derived figures marked. See Review notes, "S3 follow-up 2".
- **#29 Zcode, #30 Johnny, #31 Forge, #32 Ark (reviews of the S3 branch), S3 follow-up 3.** Johnny
  #30-4: a peer's unknown length is refused on a free card too (L13 point 2). #30-7: an `auto`
  alias is read by its detection. #30-6: one audio length by value. #30-1: `_estimate`'s sum is
  the line's sequential model, the S3a note corrected. Zcode #29-3 / Ark F4: the fallback is not
  asked twice. Zcode #29-4 / Ark (4): a refusal's own log words. Ark (6): the check only with a
  card ask. Forge #31-8: pinned green. Ark #24-2, Ark (3), Zcode #29-1: written down. A test that
  wrote the real `config.ini`: found and guarded. See Review notes, "S3 follow-up 3".
- **#40 Ark, #41 Johnny, #42 Forge (group-chat reviews of the S3 branch), S3 follow-up 4.** Johnny
  #41-2 / Forge #42-3: `penalty_s` by length band. Ark #40-1 / Johnny #41-5: a loading holder's
  ETA is the load's remaining time plus the work waiting for it. Forge #42-4: two start load
  figures, the fresh process's and the reload. Ark #40-3: the dead `_get_audio_duration` removed.
  Johnny #41-1, Ark #40-2: lines corrected. Forge #42-5: one length by value stays the
  estimate's, with the reason. See Review notes, "S3 follow-up 4".
- **#44 Ark (group-chat review of the S3 branch), S3 follow-up 5.** Ark #44-1: a peer refused for an
  unknown length is told the length is unknown, free card or blocked. #44-2: `audio_s` in the ask
  follows the length, not the estimator. #44-3: module-scoped fixtures checked against the Settings
  guard, none builds one. Written down: declared length precision, the WebM peer path without
  ffprobe and the protocol field it needs, item 7's live acceptance. See Review notes, "S3 follow-up 5".
- **Mike 2026-11-23, the agent tool's cap (built in `674b1971`).** The agent tool's wait for the card without a turn hold is capped at `queue_wait_s` (600 s), the smaller of it and the turn hold's time left. The open Ark #4 question is closed. The door computes the bound (`_card_wait_bound_s`); `card_ask` is unchanged.
- **#47 Johnny, #48 Forge, #49 Ark (group-chat reviews of follow-ups 3–5), S3 follow-up 6.** Forge
  #48-1 and Ark #49-1: the reload figure only for a device that has loaded, and a reload past its
  figure falls back to the first load's. Ark #49-2: the loading ETA reads the route's device, with a
  WARNING when the model loaded elsewhere. Ark #49-3: the warm figure learns from the decoded
  length, the batch keeps the estimate's. Ark #49-4: timing tests wait for state. Johnny #47-2:
  tests never touch the real home, with a sentinel. Forge #48-4: already off the loop on
  `9ae34f92`, made structural. Forge #48-5: not reproduced (max, not sum), pinned. Text: Forge
  #48-2, #48-3, #48, Ark #49-5; Forge #54 and Johnny #53-4 on two comments. See Review notes,
  "S3 follow-up 6".
- **#58 and #61 Ark, #59 Johnny, #60 Forge (group-chat reviews of S3d and the commits after it), S3
  follow-up 7.** Ark #61-1: the load figure per device and model. Ark #61-3: the twin checks the RAM
  in its own load. Ark #58-2, #58-3, Johnny #59-1, #59-2: the INFO line says "not compared" behind
  an engine that stays and "no penalty" at factor 1. Ark #61-6: a JSON 1 turns
  `let_peers_use_cpu_twin` on. Johnny #59: one `queue_wait_s` default, pinned. Text: Ark #58-6,
  #61-4, Forge #60, Johnny #59-4. Not reproduced: Ark #58-4, Johnny #59 (the probe's arrival). To
  S3e: a model-aware RAM gate. To Mike: item 1a's price of a swap, what counts as CPU contention, 0
  as off, the seeding of L12's numbers. See Review notes, "S3 follow-up 7".
- **S3e (CC, 2026-11-24).** Mike #7, #15: one reserve per engine, Strata's from its alias, then its
  base config (`5984876e`). Johnny #30-2, Forge #31-6, #42-6, #60: `vram_mib` by length band, read by
  L11. L11's one shared cell and live veto, at L12's point 1 and at the grant, dormant until S4.
  `predicted_vram_mib`; the VRAM cache clear (Mike #11, item 13). Johnny #63-5: a twin that does not
  load sends the call to the line. Zcode #22 (iii) pinned, (iv) a peer's Strata refusal named a lease
  holder, fixed. Forge #64 finding 2 written down. See Review notes, "S3e build".
- **S4 (CC, 2026-11-24).** Music holds the card lease: `music:yue2` and `music:acestep`, the lease
  before `_the_lock`, let go on every end of the child, the runner's job waiting through `in_use`
  (queued, named, until cancelled) and again `running` when its child ended (both gates); the S3b
  interim card wait removed; L11's cell live; a queued job's expected time with YuE2's factor by
  length; the start by hand refused by any holder. Tests 4, 11, 13, 15, 17, 21, 29, 34. Two questions
  for Mike (the factor's orientation, a by-hand start beside an idle Whisper), both answered on 2026-11-24 and built in follow-up 9. See Review notes, "S4
  build" and "S3/S4 follow-up 9".
- **S3/S4 follow-up 8 (CC, 2026-11-24).** Mike 2026-11-24: Q2 (a running twin or llama-server is CPU
  contention), Q3 (0 turns the CPU route off), a peer may stop an idle engine on the owner's terms
  (supersedes option (a)'s "a peer stops none"); Q1 (no swap pricing behind an idle engine), Q4, Q5
  and Q6 recorded as decided. See Review notes, "S3/S4 follow-up 8".
- **S3/S4 follow-up 9 (CC, 2026-11-24).** Zcode #85, #87, #88 and Mike's answers of #84: the YuE2 factor
  flipped, a start by hand unloads what is idle, a peer waits 60 s after the owner's last request, CPU
  contention narrowed to small work, the VRAM forget off the probe's lock, grants released when a stop is cut
  short, the twin-failed retry bounded by the door's deadline; this record's status, 3.2 and stale sentences
  corrected. See Review notes, "S3/S4 follow-up 9".

## Authors

- **Mike** — Decisions (#20, #62, #68, #70, #71, 2026-11-21; #11 of the new count);
  questions at #49–#50 and #85
- **CC** — Request, review, this record
- **Ark, Zcode** — Consulted; reviews #41, #44, #45, #52, #53, #59, #60, #61, #64, #67, #69, #82;
  new count #12, #13, #18, #22
- **Johnny** — Consulted; reviews #83, new count #14, #19
- **Forge** — Consulted; review and live readings, new count #15, #20, #21

## References

- [ADR-044](044-strata-as-a-second-supervised-local-engine.md) — the card queue, the swap
  and `_CARD_CLAIMS`
- [ADR-051](051-the-music-player-is-a-dpc-window-over-one-core-music-service.md) — the
  music player; open question N11
- [ADR-044, amendment of 2026-11-18](044-strata-as-a-second-supervised-local-engine.md) — "a
  peer waits for the card behind every owner request" (parked draft; L13 builds on it)
- ADR-046 — supervised processes (option (a))
- Backlog card
  `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED`
- Backlog card `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…` (the Whisper plan)
- `ai-studio/experiments/music-player/SPEC.md` — decision 32
- Design source: the card-lease design, `dev` HEAD `b9725990`; revision checked at
  `5099b326`
- Reviews: AI Studio group chat, messages #41, #52 and #59 (Zcode), #44, #45, #53, #60,
  #61, #64, #67 and #82 (Ark), #69 (Zcode), #83 (Johnny), #49–#50, #62, #68, #70, #71 and #85
  (Mike); new count #11 (Mike), #12 (Zcode), #13 (Ark), #14 (Johnny), #15 (Forge), #18 (Ark),
  #19 (Johnny), #20 and #21 (Forge), #22 (Zcode)
