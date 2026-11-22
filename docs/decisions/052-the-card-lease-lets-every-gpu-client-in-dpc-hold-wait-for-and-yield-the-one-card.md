---
adr: 052
title: "The card lease lets every GPU client in DPC hold, wait for and yield the one card"
status: proposed
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

> **Proposed 2026-11-21. Nothing here is built.** The design was produced read-only by an
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
> was not confirmed.
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
  _DESKTOP_RESERVE_MIB`. It is not a CUDA-context figure (Ark #44): Whisper's own overhead
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
_LEASE_WAITERS: Callable[[], List[Tuple[int, Any, str]]] = lambda: []   # (arrival, lease, alias wanted)

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
the **dictation bound**, a new `[local_transcription] queue_wait_s`, proposed 600 s (Zcode
#52; the alternative is to inherit `swap_wait_s`, 1800 s on this machine). The shape is
decided (Mike #62, #70, #71); the bound is **Open for Mike, item 6A**.

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
| agent tool `transcribe_audio_file` | L12 first; in the line: the turn hold's deadline and the tool timeout less the margin (`waiting_for_the_card_at_most`) | the tool timeout is 1800 s (`tools/transcribe.py:293`) |
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
- `eta_s` comes from the holder:
  - **Orientations, stated once.** The Whisper **ratio** is audio seconds per wall second
    (`_realtime_ratio`, `whisper_provider.py:816-822`, `duration / elapsed`; the speech table
    prints 15.9–43×), so `eta = duration / ratio` (ratio 20×, 30 s of audio: 1.5 s; a product
    would say 600 s). The YuE2 **factor** is wall seconds per audio second ("about 3.5x real
    time", `runner.py:94`, and the job limit `duration_s * 6 + 300`, `:856-857`), and
    `estimate_yue2_seconds` returns **audio** seconds: the form compares it with the duration
    cap (`ui/src/lib/utils/musicCreate.ts:353-358`), so wall time = estimate × factor. S4 records the factor by song length: `trial.md` (`ai-studio/experiments/music-player/engine/yue2-cpp/trial.md:40-41`) saw about 3.5× for a 30 s song and about 3× for 4-minute ones, and its 6-line text came back as 71–144 s songs, so the 10 s per line constant understates a song's length (a concern for the UI's warning, not for the lease).
  - Whisper: audio duration divided by the last measured ratio, else None. **The ratio
    has no carrier today** (Johnny B, Forge #21-5, Zcode #22-3): `_realtime_ratio()`
    (`whisper_provider.py:817-822`) only formats a log string (`:986`, `:1060`). S3 records
    (the provider side; S1 carries no ratio) the ratio the provider already computes there, **per device**; the estimate uses the device
    the route goes to (Forge #20-2), and with no figure for it the estimate is unknown (a peer
    is then refused, L13). L12's table is the CPU's; the GPU's follow it. **The duration** is
    ffprobe's (`service.py:515-523`), else the recorder's declared one (`:533-537`);
    `_get_audio_duration` returns **0.0** on failure, not None (`whisper_provider.py:809-815`),
    and 0.0 counts as unknown (Johnny E, Forge #21-6).
  - Music, **a job that has not started:** the text estimate (`estimate_yue2_seconds`
    per variant, `music/job_schemas.py:271-279`, audio seconds) times the YuE2 factor (3.5 as
    a start value until S4 records one) times the variants, marked as an estimate; it may route (L12), never refuse.
  - Music, **a running job** (Ark #64 §5): the remaining time must come from the job's own
    progress, not from the text estimate, which counts sung lines only and gives an
    instrumental its intro alone. **Today there is no usable progress signal:** the YuE2
    runner sets progress to 0.05, 0.1, and then one step per finished variant
    (`runner.py:846-900`), nothing inside a variant, which is where the minutes go; the
    job records no start time (`created_at`, `updated_at` only, `jobs.py:420-425`); and the
    "about 3.5x real time" figure is a comment from the trial card (`runner.py:94, 856`),
    not a measurement this machine keeps. So in v1 **a running music job's ETA is unknown,
    and L12 treats it as long.** S4 may add the signal: the start time of each variant and
    a per-machine YuE2 factor recorded from finished variants (wall time over the take's
    audio length); then `eta = variants left × duration_s × factor − elapsed in the current
    variant`, floored at 0 and marked as an estimate. Acestep's progress is as coarse
    (`runner.py:734-784`).
  - Supervisors: None.

**L11. One shared cell: Whisper beside a busy music engine when it provably fits**
(decision 6, option (d); accepted with conditions by Zcode #52 and Ark #53; **proposed,
pending Mike's OK**).

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
     MiB**, where `whisper_peak = model_size_bytes(model, revision)[0] // 2**20` plus a new
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
       **2900**. `whisper_peak` ≈ 1547 + 2900 = 4447 MiB (the driver view peaked at 4240), against 3362 with the former 1740. That
       ~1.5–1.7 GiB was the **backend's own residency** (`bge-m3`, GLiNER; `040-…md:78`), not
       a Whisper context, and on top of the weights it counted the model twice (Ark #18-4,
       Johnny A, Forge #20-6, Zcode #22-2). A start value, tuned from operation.
     - **The flat figure is a conscious choice** (Johnny #32-2, Forge #33, Ark #30): `whisper_peak`
       is the worst case of the provider's adaptive batch (batch 8 at 61–120 s) and overstates a
       10 s dictation about 2.2× (1984 reserved against 4447); its context is 485 MiB, the larger
       figure (Strata resident; 280 on an empty card). A per-batch overhead is a later
       refinement if the music cell is refused in practice; test 28 is named for the worst case.
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
       supervisor at launch by S3 as `predicted_vram_mib`. Chosen over a live measurement
       (per-process figures read `[N/A]` under WDDM through nvidia-smi and need S8's PDH
       reader) and over "always unknown" (which would send every dictation beside a loaded
       LLM to L12 on the owner's 32 GB card for no reason): it is the arithmetic DPC
       already trusts to admit that very child, and it includes the measured overhead
       (`_DEFAULT_OVERHEAD_MIB`, 4608);
     - **a Strata child, or a llama-server child whose start could not size its model**
       (`_admission` None), has no such figure: its peak is unknown, and the outcome is
       `unknown`. **[unverified]** why Strata has none: `_admission` reads
       `self.config["gguf_path"]` (`llama_server_supervisor.py:3548-3549`) while Strata's
       model keys are `native_gguf`, `ple_gguf`, `pack_dir`, `mtp_dir`
       (`strata_supervisor.py:291-295`); whether `_admission` returns None or is never called
       for it was not read (Johnny D). **S2's acceptance reads** `sup.config.get("gguf_path")`
       on a live Strata supervisor: it decides whether `_admission` is never called (no figure
       by construction) or would raise `KeyError`, which is not `unknown` (Johnny #32-5). S2 or
       a later refactor may give supervisors a `vram_mib`;
     - the desktop is counted **once, as `_DESKTOP_RESERVE_MIB`**. A measured `dwm.exe`
       figure is never added: `dwm` is not a resident (it is on S8's ignore list), so the
       declared check cannot pay for the desktop twice (Ark #60 §5.2).
  2. **Declared check:** `need <= _total_vram_mib()` (supervisor 1814).
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
    the rest of the process).
  - **One reserve** (Forge #15 §4): Strata's admission budgets `total − _DESKTOP_RESERVE_MIB`
    (`llama_server_supervisor.py:1815-1817`, `3552`); L11's declared check is that same
    arithmetic with the same constant, and only its live veto reads `free`, which already
    holds the desktop (section 5). S2 adds no second reserve.
  - **By platform**:

    | Platform | `total` / `free` | Result in L11 |
    |---|---|---|
    | Windows, NVIDIA | read at device level by nvidia-smi; only the per-process query is `[N/A]` under WDDM (`eval/gaia/campaign.py:128-130`). Two pairs, both real. A local engine resident: `free` ≈1100 MiB (Forge #15, #33: 1100 and 1108 with Strata) → the live veto refuses (1100 < 4447), as Mike #68 requires. No engine: `free` ≈30 000 MiB (`dpc-client.log`, 2026-11-18 15:06:42: "32298 MiB of VRAM free"; Strata's own demand there was `total` 32623 less `_DESKTOP_RESERVE_MIB` 2048 = 30575, `strata_provider.py:446-455`) → the veto passes | fit arithmetic runs |
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
    such as a Strata child; or a reading unavailable or failed: no nvidia-smi, macOS, AMD,
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

1. **The card is free, or L11 says `fits`** → the GPU, at once.
2. **The audio is longer than `[local_transcription] cpu_max_audio_s` = 300 s** (Mike #71:
   "up to 5 minutes, easy") → the line; the CPU twin is not considered.
3. **Is a CPU-computing engine busy?** The signal is explicit (Zcode #69): a resident
   Strata child is busy when its alias has a live `accepted_request` hold in `_CARD_HOLDS`
   (the registry the `_CARD_HOLD` contextvar's holds are entered into, 693–701), or a call
   in flight (`_in_flight > 0`), the same test as `_busy_for_the_card` (2668). Not a guess
   from timing. When it is busy, the CPU twin **is still allowed** for files within the
   300 s cap (Mike #71, replacing the hard stop of the third revision), and its estimate is
   multiplied by `[local_transcription] cpu_contention_factor`.
4. **Compare times:**
   - **`gpu_wait`:** the busy holders' remaining time plus the waiters ahead in the line
     (L10); any unknown part makes it **long**. A running music job and a supervisor in a
     turn are both unknown in v1, so both are long.
   - **`cpu_estimate`:** the CPU twin's time for this file, cold (the twin is not loaded:
     its load and first call added) or warm, interpolated from the measured lengths below,
     times the contention factor from point 3 when it applies.
   - **The CPU twin** when `cpu_estimate < gpu_wait` **and** `cpu_estimate <= cpu_max_s`.
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
  adds ≈9 s (Johnny #14).
- **Enough RAM:** a cold CPU route is taken only when available host RAM covers the twin
  plus a margin (one constant, `twin_ram_mib` + `ram_margin_mib`, values unverified; test 40
  fakes it), read as `psutil.virtual_memory().available`, the one instrument for the
  gate, the log and the UI; otherwise the line (proposal). `commit_memory.read_free_commit()`
  also exists and reads commit too; it is not used here (Forge #20-3, who read 7.1 GB
  available against the twin's ≈4 GB, up to ≈10 GB; the twin's RAM figure stays unverified).
- **No switch in mid-flight** (Zcode #69): the choice is final for that call. A dictation
  that started on the CPU twin stays there even if the card frees a second later; only new
  calls take the GPU. A call sent to the line does not later jump to the CPU.
- **`cpu_max_s`, proposed 600 s:** neither route makes a person wait longer than the
  dictation bound. With the measured factor of about 1.0–1.1× real time, the 300 s audio
  cap binds first when nothing contends.
- **`cpu_contention_factor`, proposed 1.0 until measured**, defined as the CPU ratio with an
  engine computing divided by the CPU ratio alone; the base CPU speed on speech (1.1–1.5×) is
  the twin's own figure, not this factor (Ark #30). Mike's #71 and the logs
  (`~/.dpc/logs/dpc-client.log*`: the Whisper alias ran with `device=cpu` on 2026-11-14 to
  11-20, e.g. "Whisper model loaded in 1.0 seconds (device=cpu)" on 2026-11-20) say a 5-minute
  file on the CPU beside Strata works; a factor of 2.0 would push a 300 s file (≈306 s) past
  `cpu_max_s` and contradict him. The measurement Ark #67 and Zcode #69 ask for (a Strata
  turn's tokens per second and length with and without the twin running) sizes it. Both
  numbers ship as start values and are tuned from operation (Mike #11); the in-turn path of
  3.5 does not depend on them.

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
reload after an unload 0.6–0.9 s; ratio 79× (30 s, `generate`) to 154× (120 s, pipeline), an
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

- **The ratio on speech is ≈16–43× on the GPU** (the tone's 79–154× was an upper bound). The
  first call after a load costs ≈0.9 s more on a 10 s clip and ≈0.7 s on a 60 s one; at 30 s and at
  118 s and longer it is within run-to-run spread (`gpu_run1.log`, "first" against the median). These seed the GPU route's whole-trip estimate,
  as the CPU table seeds the CPU route; the per-device recorded ratio takes over after the
  first run. On a warm GPU a 300 s voice note (≈19 s) fits a peer's 120 s budget; so does a
  cold first load after backend start (≈13 s more, measured as a fresh interpreter; see Load).
- **Memory:** the context is 280 MiB on an empty card (Forge saw 477–485 with Strata
  resident); after load 1543 allocated, 1674 reserved. The peak is **batch-dependent**: it is
  highest at batch 8 (60–120 s clips), lower at 2 (300 s) and at 16 (short clips). That is
  what sets `_WHISPER_OVERHEAD_MIB` in L11.
- **Load:** cold load in a fresh process 12.8 s (a fresh interpreter, so it includes the first
  library init; inside a running backend torch is already imported and Forge measured the load
  step alone at 1–2.4 s); unload 0.35–0.61 s, VRAM back to the context only; reload
  0.76–0.83 s (Forge: 0.6–0.9 s). That reload is the cost the 300 s idle unload rests on.
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
  hold: swap on, nothing queued, nothing busy (ADR-044's amendment of 2026-11-06,
  `_why_a_peer_may_not_swap`, 2632-2651). With `swap_when_idle` off, the peer keeps today's
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
   Whisper is warm and refused at once otherwise; it never swaps Strata out, whose stop has no figure. The line opens to
   peers as figures accrue. **Known limit (Ark #13):** the guest tries its candidates in turn
   (`service.py:10157-10172`, `continue` after a failure), so a person on the guest's side may
   wait candidates × 120 s. Not changed here.
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
| whisper load | **excludes (Mike #68): busy → L12 (CPU twin or the line); idle → L12 first, then a swap with swap on (item 1a, proposed)** | **same** | **busy: the one shared cell, beside if it provably fits (L11), else L12; idle: yields** | same holder: re-entrant; another whisper alias: excludes |
| embedder / ner (background) | granted only if the card is free, otherwise CPU | CPU | CPU | CPU |

**Whisper waits for supervisors** (Mike #68: "no, it must not load beside").

- The whisper × supervisor exclusion is symmetric in the first slice. The v1 asymmetry of
  earlier revisions, its "loaded beside" log line and its `beside` row are gone; `beside`
  stays only for L11's one shared cell.
- A busy supervisor (a turn in progress) is waited for through L12: a file up to 300 s
  goes to the CPU twin (Mike #71), a longer one takes the line.
- **The consequence Zcode #69 names:** a dictation longer than the CPU allowance (300 s
  audio) during a long agent turn takes the line, and if the turn holds the card past the
  dictation bound (600 s proposed) the dictation is refused.
- **An idle supervisor (item 1a, proposed, pending Mike):** L12 first (a file up to 300 s
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
- **Open for Mike, item 2.** It now interacts with Mike #70: music swapping an idle
  supervisor **is** gated by `swap_when_idle` (owner-start rule), while a music engine or
  Whisper yielding to another lease is not (if item 2 stays at its default).

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
#70; a long dictation, item 1a, still open). It does not apply to a lease that yields
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
  returns `fits` with the music holder's alias, `does_not_fit` or `unknown`.
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
  - `start_music_engine` by hand calls `acquire("start by hand", wait_s=0)` **before**
    `start()`, so it is refused naming the holder, as `exclusive(wait=False)` refuses today;
  - `start()` itself asserts the lease is granted and never acquires.
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

On the owner's 32 GB card with no local LLM resident, L11 should find the fit at step 3 (declared: `whisper_peak` 4447 MiB, the 1547 of weights from `model_size_bytes` plus
2900, + 5600 MiB YuE2 + 2048 reserve = 12 095 MiB against `total` 32 623; live: far more free
than Whisper's peak plus the music growth left) and Whisper loads beside at once, as today,
now named in `card_holders()` and the log **[unverified: the YuE2 figure, 5.6 GB read as 5600
MiB, is a comment in `music/engines.py:5-6`; no `vram_mib` is declared in code yet]**.

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
    - *Does a load fit?* `_DESKTOP_RESERVE_MIB` decides, and only it, and only in
      arithmetic against **total** memory: `strata_provider.py:451`, and L11's declared
      check. L11's live veto compares **measured free** memory, which already excludes the
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
  up to the dictation bound (600 s proposed), instead of today's load beside. The person
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
    `need`, flat by choice, L11; Ark #60 §5, #61 §2b): three residents (a llama-server child with `predicted_vram_mib`, a busy YuE2, a
    co-living second Whisper alias) and a Whisper contender: the fit sums all three; the
    yield list holds only those that exclude the contender; a sum built from the yield list
    would say "fits" and the test proves L11 says `does_not_fit`. With a Strata child as a
    resident the outcome is `unknown`. `dwm` in a fake per-process reading changes nothing.
    The test also **pins the value of `need` in MiB**, not only its outcome (Ark #18-3): Whisper
    alone, no resident, from a fake revision-less turbo row of 1 622 443 339 B → `whisper_peak` 1547 + 2900 =
    4447 and `need` = 4447 + 2048 = 6495 MiB, logged with its `model_size_bytes` source; a
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
    the text estimate, marked as an estimate; an instrumental never reads as "almost done"; 80 audio seconds at factor 3.5 read 280 s.
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
  agent editing it), `service.py:4797`, `music/commands.py:324`, `tools/transcribe.py:172`,
  `p2p_coordinator.py:1398`, `service.py` (`transcribe_audio`'s bound, the provider cascade
  at 5152–5160, the peer check; Telegram, `coordinators/telegram_coordinator.py:509` and
  `managers/agent_telegram_bridge.py:1674`, is covered through `transcribe_audio`),
  `settings.py` (`[local_transcription] queue_wait_s`), `ui/src/lib/coreService.ts:1492`
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

Nothing is built. Recommended order (Mike #68 made S2 part of the first slice): S0, then
S1, then S2, S3 and S4 together (they meet the Whisper card's Done-when and keep Whisper
off a local LLM's card), then S5–S8 and S10. S2 changes the 5,555-line file, so it is
coordinated with the agents editing it rather than postponed.

| # | Commit | Touches | Proves | Status |
|---|---|---|---|---|
| S0 | docs: this ADR (the design proposed an ADR-044 amendment; this record carries the matrix, L1–L12 and the decisions instead), revised after reviews #41–#89, the new count's #10–#15, and Mike #68–#71 and #11 | `docs/decisions/052-…md` | — | This commit |
| S1 | feat(card-lease): lease registry, lease line, one order across both lines (L5a), refusals, wait estimate; `card_holders`/`card_queue` include lease rows through the hook (supervisor rows first, `until_cancelled`); `card_holder()` stays supervisor-only | new `managers/card_lease.py`; `llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_LEASE_WAITERS`, `_live_holders`, `card_holders`, `card_queue`); `tests/conftest.py` | tests 1–3, 6–8, 14 with fake clients only | Built at 53a2eaf5 (2026-11-22), in review; S1-level parts of tests 3 (b) and 8 only, the rest of 3 (b) and 3 (c) are S2, the 120 s door of test 8 is S3; a third hook `_LEASE_QUEUE_ROWS` added (one lease, several waiters) |
| S3 | feat(whisper): the local model holds a card lease, yields by unloading when idle (`yield_card`, releasing only when the unload ran), loads beside busy music only in the one shared cell when it provably fits (L11), otherwise chooses the CPU twin when its estimate beats the GPU wait (L12, Mike #62, Ark #64) or queues before `_load_lock`; the CPU twin is a separate `local_whisper` provider with `device: cpu` and no lease, selected by `transcribe_with_route`; doors pass `purpose` and their bounds; every caller gets a deadline and the queue's is shorter; a card refusal ends the provider cascade; the peer check runs again after a wait; a peer's transcription waits only when its whole trip fits the guest's 120 s, else is refused at once (L13); a load that fell back to the CPU releases the lease; an agent's transcription inside its own turn on a local alias goes to the twin up to 300 s or is refused at once (3.5); the provider records the real-time ratio per device, the carrier L10's Whisper estimate needs (S3, not S1); the twin starts from L12's table; llama-server children record `predicted_vram_mib`; L11 reads `free` and `total` through `read_vram_mib` and never per-process figures | `providers/whisper_provider.py`; `service.py` (`transcribe_audio` bound, the cascade at 5152, the peer check), `music/commands.py:324`, `tools/transcribe.py:172` (`purpose`, `on_wait`); `p2p_coordinator.py:1398` (`serving_a_peer`); Telegram needs no edit of its own, it reaches the provider through `service.transcribe_audio` (four doors, Forge #20-7); `settings.py` (`queue_wait_s`, `cpu_max_s`, `cpu_max_audio_s`, `cpu_contention_factor`); `ui/src/lib/coreService.ts:1492`; `llama_server_supervisor.py` (`predicted_vram_mib`, one attribute set at launch); `managers/commit_memory.py` (a function that clears `at` and `free` of the 30 s VRAM reading, never `total`) | 9, 10, 18–24, 26–28, 30–34, 36–38, 40 | Pending; first slice |
| S4 | feat(music): music engines hold the card lease (acquired before `_the_lock`); the runner waits through it and names lease holders; a `running` job may wait for the card (both gates: `jobs.py` and the runner's wait loop) | `music/engine_base.py`, `music/runner.py`, `music/commands.py` (start by hand), `music/jobs.py`, `music/acestep.py`/`yue2.py`, test helpers, `ui/src/lib/utils/musicCreate.ts` | **4 (Done-when)**, 11, 13, 15, 17, 29 | Pending; first slice |
| S2 | feat(card): llama-server and Strata wait for a busy lease and unload an idle one, with the swap on or off; a lease contender swaps an idle supervisor on the owner-start path, reading the parked count first (2.4); one order across both lines; an inference peer waits behind a lease only once ADR-044's amendment of 2026-11-18 is built (L13) | `llama_server_supervisor.py` (2.2 items 3–13), `providers/llamacpp_server_provider.py` (`stop_local_engine`, then `card_holder` may return lease rows) | 5, 16, 35, 39, and the full list of existing card tests green | Pending; first slice |
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
- **2026-11-22, N11: in scope, and CC writes S10, the ComfyUI client half included.** The yield
  verb for ComfyUI is its own `POST /free {"unload_models": true, "free_memory": true}` plus
  `POST /interrupt` (Forge verified it live, 24.9 → 1.8 GB). Order: after S1.
- **2026-11-22: CC measures the GPU ratio** Forge asked about (#21), on real speech.

## Open for Mike

Still open after #68–#71 and #11. Each has a proposed default.

- **Item 2.** **Lease yields not gated by `swap_when_idle`.** Unloading Whisper or stopping an idle
  music engine for another lease happens even with the switch off. *Proposed default:* not
  gated (CC #66 question 4; Zcode and Ark for). It interacts with decision 4: music
  swapping an idle **supervisor** is gated by the switch (owner-start rule).
- **Item 6A.** **The dictation bound** `[local_transcription] queue_wait_s`: *proposed* 600 s (CC #66
  question 8; Zcode and Ark for). The alternative, `swap_wait_s` (1800 s here), would
  leave the agent tool no time to transcribe after a full wait (Ark #60 §7). Johnny and
  Forge abstain until it is measured.
- **Item 1a.** **An idle supervisor in front of a dictation** (follows from #68 and #70). *Proposed:*
  L12 first (a file up to 5 minutes goes to the CPU twin and costs the LLM nothing); a
  longer file, with the swap on and nothing waiting, swaps the idle supervisor out by the
  owner-start rule, like music. *Alternative:* a dictation never stops a supervisor and
  waits. Votes: "L12 first" (Ark, Johnny).
- **Item 9.** **`cpu_max_s`** (proposed 600 s) and **`cpu_contention_factor`** (proposed 1.0 until the
  Strata measurement). They ship as start values and are tuned from operation (Mike #11's
  line); the numbers themselves are not decided. The in-turn path (3.5) no longer depends
  on them. Start values accepted by Johnny, with the named RAM constant.
- Items 10, 11 and 13 were answered at #11; item 12 by reading the flow, as he asked; item 8
  closes with item 10; item 7 was answered on 2026-11-22 (all in Decided by Mike).
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
- **Ark #53, minor:** the line numbers given for `_excludes` (2480–2484) and
  `_blocking_holders` (2494–2503) are 2479–2484 and 2500–2505 at `5099b326`; the content is
  as Ark says. Ark's figure of 1800 s for `swap_wait_s` is this machine's `config.ini`
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
  not five. **Did not hold as written:** Zcode's `p2p_coordinator.py:1354` as `error=str(e)`
  (the access-denied string; `str(e)` is `:1412`); Forge's `_realtime_ratio` call at `:857`
  (the calls are `:986`, `:1060`); line numbers drifted by one to four (`:1672` not `:1673`,
  `transcription_handler.py:65-68`, `_get_audio_duration` at `:809`, `_current_owner` at `:673`,
  `llama_server_supervisor.py:3221-3223`). **Withdrawn:** Forge's #15 §2–3 (bytes and a
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
  seconds (the form compares it with the duration cap, `musicCreate.ts:353-358`; the test pins
  80 for six lines), so the product stands; kept from Johnny: no code records the factor yet
  (3.5 is a start value; S4 records it).
- **The first draft's own errors** found on the way: `_CARD_CLAIMS` is pruned in four
  places, not two; `_free_vram_mib` is in the supervisor; `p2p_coordinator.py:1397` is now
  1398; card 1925 is now at line 1967.

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
