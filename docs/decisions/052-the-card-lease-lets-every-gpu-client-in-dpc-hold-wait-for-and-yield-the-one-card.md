---
adr: 052
title: "The card lease lets every GPU client in DPC hold, wait for and yield the one card"
status: proposed
date: 2026-11-21
axis: network
deciders: [Mike]
consulted: [CC, Ark, Zcode]
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
> transcription**: a short file goes to the CPU when the card is busy and the line is long;
> a long file, or a free card, uses the GPU (rule L12). The thresholds, the peer rule and
> the unmeasured-CPU rule under L12 are CC's proposals. Everything else, including the six
> items under **Open for Mike**, is a proposal. Decision 6 (dictation during a music job)
> is **proposed, pending Mike's OK**, with sub-choices left to him. Anything marked
> **[unverified]** was not confirmed.
>
> **Second revision** (same day): answers Zcode #59, Ark #60 and #61, and Mike #62, checked
> against `dev` HEAD `d80d76f7`.
>
> **ADR-051 N11** (how an external GPU job joins the card queue) is answered here as
> "(b)+(c)" **only once Mike confirms**. He has not chosen N11. Until he does, this ADR
> does not close it.

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
  `music/commands.py:324` (music Transcribe, `release_if_loaded_here=True`).
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
  _DESKTOP_RESERVE_MIB`. It is not a CUDA-context figure (Ark #44): a Whisper process's
  ~1.5–1.7 GiB of residency needs its own constant.

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
  "never" (Ark #45, D3): "this node's GPU is busy with another job right now". It names no
  alias, agent or chat (rule 7).
- `LeaseBusy`: `let_go_by_hand` on a busy lease. It names `who_uses()`.
- `LeaseClosed`: the backend is stopping.

**L4. Deadlines.** One deadline per acquire, fixed at acceptance (rule 5). A person's
doors queue the way model starts do (decision 6, Mike #49–#50), with a bound of their own:
the **dictation bound**, a new `[local_transcription] queue_wait_s`, proposed 600 s (Zcode
#52; the alternative is to inherit `swap_wait_s`, 1800 s on this machine: decision 6,
sub-choice A). **Open for Mike, items 3 and 6.**

**The queue's deadline is never longer than its caller's** (Ark #53): `wait_s = min(caller's
remaining time − margin, bound)`, where the margin is the transcription's own expected time
(audio duration × the last measured real-time ratio, floor 60 s). A caller that has no
deadline today gets one: `service.transcribe_audio` takes an explicit bound (the dictation
bound plus the margin), so the local path and both Telegram paths are no longer unbounded.

| Door | Bound | Notes |
|---|---|---|
| owner's dictation (UI `transcribe_audio` → `service.transcribe_audio`) | L12 first (GPU now, CPU, or the line); in the line: dictation bound, capped by the UI command timeout | the UI's 240 s (`coreService.ts:1492`) is raised to the bound plus the margin in S3, or it caps the queue at ~3 min; cancellable |
| received voice messages (`service.py:5152`) | L12 first; in the line: dictation bound | the cascade stops on a card refusal and the peer check runs again after the wait (section 3.1) |
| Telegram voice (`telegram_coordinator.py:509`, `agent_telegram_bridge.py:1674`) | L12 first; in the line: dictation bound | today unbounded; the explicit bound above applies |
| agent tool `transcribe_audio_file` | L12 first; in the line: the turn hold's deadline and the tool timeout less the margin (`waiting_for_the_card_at_most`) | the tool timeout is 1800 s (`tools/transcribe.py:293`) |
| music Transcribe | L12 first; in the line: `wait_s=None`, until `check()` raises (cancel) | a track's vocals are usually minutes long, so L12 mostly sends it to the line |
| music jobs | `wait_s=None`: until `check()` raises (cancel) | |
| peer (`p2p_coordinator.py:1398`) | 0: refused at once ("right now"), with the holder's ETA when known; no CPU route unless Mike says so (decision 6, sub-choice C) | rule 7, no place |
| background transcription | L12: short audio on the CPU, long audio skipped (`LeaseSkipped`) | rule 10 |
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
Zcode #52). There are two lines in v1, `_LEASE_LINE` and `_CARD_QUEUE`; they share one
clock, and **no waiter overtakes another in either direction**: a queued dictation is not
starved by a series of server starts, and a server start is not starved by a stream of
dictations. The one named exception (point 3) is a waiter that by decision 32 will never
act, which would otherwise hold the line forever.

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
   go, or swap it). **One exception, decision 32:** a music lease whose only blockers are
   supervisors does not act on them, and it does not hold up a later supervisor place
   blocked by the same supervisor, which swaps as it does today. That is the one way a
   server start overtakes a waiting music lease in v1, and it changes nothing for the music
   job, which was waiting for a person's "Stop X" either way. Amending decision 32
   (item 4) removes the exception.
4. **The bound.** A waiter waits only for holders granted before it arrived and for
   waiters with a lower arrival; both sets are closed at its arrival (L5 for leases, rule 4
   `_stands_before_the_line` for supervisor holds). So a series of server starts that
   arrive after a waiting music lease cannot take the card before it: when the busy holder
   lets go, the music lease is granted first, and the starts then wait for the busy music
   job (Mike's #20 ask) up to their own deadlines.
5. **Later (S9).** Once Whisper waits for supervisors, `_stands_before_the_line` (1202)
   compares a hold with the lowest arrival across both lines, not only
   `_CARD_QUEUE[0].arrival`. In v1 no lease that acts waits for a supervisor (point 3), so
   that change is not needed before S9.

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
  - Whisper: audio duration times the last measured real-time ratio, else None.
  - Music: a rough figure from the YuE2 duration cap and variants left (about 3.5x real
    time per `runner.py:93`), marked as an estimate, else None. The engines have
    `estimate_ms` today, not an ETA.
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
  its own arithmetic and tests. Whisper beside llama-server stays the v1 asymmetry (2.1),
  not a measured share, until S9.
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
  1. `need = whisper_peak + Σ peak of every resident + _DESKTOP_RESERVE_MIB`, where
     `whisper_peak = model_size_bytes(model, revision)` (`providers/model_sizes.py:74`,
     which returns `(size, "measured"|"hf_api"|None)`) plus a new
     `_WHISPER_CUDA_CONTEXT_MIB` (the backlog measured ~1.5–1.7 GiB; proposed 1740), and:
     - a music holder's peak is its declared `vram_mib` (YuE2 Q8 5.2–5.6 GB is already a
       peak; the larger figure is used);
     - another Whisper lease's peak is computed the same way as `whisper_peak`;
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
       `unknown`. S2 or a later refactor may give supervisors a `vram_mib`;
     - the desktop is counted **once, as `_DESKTOP_RESERVE_MIB`**. A measured `dwm.exe`
       figure is never added: `dwm` is not a resident (it is on S8's ignore list), so the
       declared check cannot pay for the desktop twice (Ark #60 §5.2).
  2. **Declared check:** `need <= _total_vram_mib()` (supervisor 1814).
  3. **Live veto:** `_free_vram_mib()` (1793) `>= whisper_peak + growth_left`, where
     `growth_left` is the music peak less what the music child is measured to hold now
     (per-process reader, S8), or the whole peak while it cannot be measured. A reading
     can only turn "fits" into "does not fit", never the reverse.
  4. Both readings run in a thread (`asyncio.to_thread`: each is a subprocess with a 10 s
     timeout); the L1 check runs again in the stretch of the grant.
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
  - Peers are refused at once in both non-fit outcomes; background transcriptions follow
    L12 (L4).
- **The music lease is untouched by a co-holder:** its busy test, its yield and its stop
  read only its own uses and child.
- **A third contender while two hold** (Zcode #52, Ark #53). A llama-server start or a
  second music job meets two holders. `_free_the_card` (and the lease line) ask to yield
  **only holders that are in the contender's way**: `contender._excludes(holder)` and not
  covered by a measured share for this contender. Among those, **idle cheap holders first**
  (`yield_cost="cheap"`: Whisper), then busy ones are waited for, each in `_CARD_ARRIVAL`
  order of the waiters. In v1 a co-living Whisper is in the way of every contender except
  the shared cell itself, so the "not in the way" clause guards S9's later shares; it is
  pinned by a test now.
- **What it means for "one owner"** (Zcode #52): the invariant of one owner per model
  object (one Whisper object owns the pipeline through a reload) is untouched. What gives
  is the card's exclusivity, in one cell, with proof of headroom, visible as two holder
  rows. An idle music holder is still yielded as the matrix says; whether a fit should
  also spare an idle engine its stop is left open.
- This pulls a fit check into S3 for the Whisper+music cell only. The general fit check
  (Whisper beside llama-server) stays in S9; the CPU route moved to S3 with L12.

**L12. The GPU/CPU choice for a transcription** (Mike #62, a product decision; the numbers
and points 4–6 are proposals). Before a transcription takes a place in the line:

1. **The card is free, or L11 says `fits`** → the GPU, at once.
2. **The card is busy and L11 says `does_not_fit` or `unknown`** → compare two figures:
   - **the GPU wait:** the busy holders' ETA plus the ETAs of the waiters ahead in the line
     (L10); any unknown part makes it **long**;
   - **the CPU time:** audio duration × the measured CPU real-time factor of this machine,
     plus the CPU model's load time when it is not loaded.

   **The CPU** when the audio is short (`[local_transcription] cpu_max_audio_s`, proposed
   120) **and** the CPU time is below the GPU wait. **Otherwise the line** for the GPU,
   bounded by the door's deadline (L4; the dictation bound `queue_wait_s` = 600 s for a
   person's doors), with the status line and a cancel.
3. **Background transcriptions:** short audio goes to the CPU, long audio is skipped
   (`LeaseSkipped`), as rule 10 skips today.
4. **Peers:** unchanged, refused at once with "busy right now". Spending the owner's CPU
   on a peer's file needs Mike's word (decision 6, sub-choice C; default no).
5. **No measured CPU factor yet** → no CPU route; the transcription takes the line (CC's
   proposal, pending review). The factor is recorded from every CPU transcription (the
   provider already computes a real-time ratio for its log, `_realtime_ratio`, 816) and
   from the measurement in step 5 of the Whisper plan.
6. **Enough RAM:** the CPU route is taken only when available host RAM covers the CPU
   model plus a margin (`psutil`), because Strata keeps experts in host RAM; otherwise the
   line (proposal).

The CPU route holds **no card lease** and touches no VRAM. It uses a second, CPU pipeline
(section 3.1). The choice in point 2 is final for that call: a call sent to the line does
not later jump to the CPU, and a CPU call takes no place.

### 2. Integration

#### 2.1 Who excludes whom (v1)

The guiding rule: a lease is visible to everyone, but who waits or yields for whom follows
Mike's words and the cost of a reload.

| contender \ holder | llama-server | strata | music lease | whisper / embed / ner lease |
|---|---|---|---|---|
| llama-server start | arithmetic, as today | excludes | **excludes: waits while busy, yields while idle** | **excludes: waits while busy, yields while idle** |
| strata start | excludes | excludes | **same** | **same** |
| music start | excludes: waits, **never stops it** (decision 32 kept; L5a point 3) | same | music engines: `exclusive()` as today; their leases agree | **excludes: waits while busy, yields while idle** |
| whisper load | **not excluded in v1** (loads beside, as today, and now says so in the log) | **not excluded in v1** | **busy: the one shared cell, beside if it provably fits (L11), else L12: short audio on the CPU when that is quicker, otherwise the lease line; idle: yields** | same holder: re-entrant; another whisper alias: excludes |
| embedder / ner (background) | granted only if the card is free, otherwise CPU | CPU | CPU | CPU |

**Why Whisper does not wait for supervisors in v1.**

- Making it wait would turn dictation during an agent turn into a wait of up to
  `swap_wait_s` (1800 s on this machine).
- Swapping an idle Strata out for a 3 GB transcription drops parked conversations and
  costs a long reload.
- Mike's decision covers the other direction: supervisors wait for Whisper.
- When Whisper loads beside a supervisor it logs one INFO line (Zcode #41): `card: <display>
  loaded beside <engine> '<alias>' (v1: Whisper does not wait for local LLM engines)`, and
  its holder row carries `beside`. Today that exception is invisible.
- The later fit check (slice S9) replaces this exception: Whisper shares with llama-server
  only when the measured free VRAM allows (L11's arithmetic), and otherwise falls back to
  CPU.
- **Open for Mike, item 1:** confirm this asymmetry for v1, or make it strict.

**The swap switch.**

- `swap_when_idle` keeps governing only the stopping of llama-server and Strata.
- Yielding a cheap in-process lease (unloading Whisper) or an idle music engine is not
  gated by the switch. That matches the music idle stop and `MusicEngines.exclusive`
  today, and nothing is lost but a reload.
- The two costs are not equal (Ark #45, D2): unloading Whisper costs a reload of about
  3 GB in seconds; stopping an idle music engine costs the next job an engine boot and a
  5–9 GB model reload **[unverified: neither reload time is measured here]**. Neither
  loses anyone's work.
- **Open for Mike, item 2.**

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
    (`providers/model_sizes.py:74`) plus a **new** CUDA-context constant (the backlog
    measured ~1.5–1.7 GiB process residency; `_DESKTOP_RESERVE_MIB` means something else),
    else None.
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
  2. **A card refusal ends the provider cascade, and one call keeps one place.** In the
     received-voice loop (`service.py:5152-5160`), a `CardQueueRefusal` (any `Lease*`
     refusal) is caught before the generic `except Exception: continue` and stops the walk
     over local aliases. Otherwise each next local alias would queue again on the same busy
     card (up to N × the bound; four names by default) and load another model. A cloud
     alias later in the list (`openai`) does not touch the card and may still be tried
     (proposal). The wait happens once, inside the first local alias's `transcribe`.
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
- **The CPU route (L12, Mike #62), in S3.** A second pipeline on the CPU, beside the GPU
  one:
  - **What it is:** the same model and revision as the alias, loaded by transformers on the
    CPU in fp32, about 3 GB of host RAM for turbo **[unverified: from the parameter count,
    ~0.8 B × 4 bytes; S3 measures it]**. On macOS, where the GPU route is MLX or MPS, the
    CPU route is the transformers CPU pipeline **[unverified on macOS]**.
  - **Loaded on demand**, on the first call L12 sends to the CPU, under its own lock
    (`_cpu_load_lock`), never under `_load_lock`, so a CPU load never blocks the GPU route
    or a yield; with **its own idle unload** timer (the same setting as the GPU idle
    unload, proposal) and its own `shutdown_unload` step.
  - **No card lease and no release:** it holds no lease, appears in no holder row, and its
    load and unload never touch the lease. Its RAM is guarded by L12 point 6.
  - **Per call, like the GPU route:** `language` and `task` are per-call arguments (the
    precedent of `b9725990`), so a CPU call never mutates the shared provider; `purpose`
    and the door's `on_wait` behave the same, and its result says `device: "cpu"` so the
    caller and the log can tell.
  - **The CPU real-time factor** is recorded per machine after every CPU transcription and
    read by L12; until there is one, L12 does not route to the CPU (point 5). The
    measurement of turbo on this CPU joins step 5 of the Whisper plan (beside turbo
    against large-v3).
  - Hand-over across a providers reload moves the CPU pipeline with the GPU state, the same
    way `hand_over_to` moves the GPU one.
- **Later (S9, not in the first slice):** `excludable=True`, Whisper waiting for
  supervisors. With the CPU route already in S3, S9 still needs three things: L12 applied
  against supervisor holders (a short file beside a busy local LLM goes to the CPU instead
  of loading beside); `_stands_before_the_line` across both lines (L5a point 5); and the
  self-wait case, where a turn on a local engine whose tool would wait for that engine
  goes to the CPU instead of `LeaseWouldWaitOnItself` when L12 allows it. Whether Whisper
  beside llama-server becomes a second measured shared cell is a separate choice with its
  own arithmetic and tests (Zcode #52: one cell per decision).

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
  - A supervisor holder is waited for and never stopped (decision 32).
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
3. L11: `does_not_fit` or `unknown`. L12: a 20 s voice message with a measured CPU factor
   goes to the CPU route and is transcribed there at once (no lease, no wait; the flow
   ends here). A 40-minute recording, or any file while no CPU factor is measured, goes
   on: the Whisper lease is excluded and enters `_LEASE_LINE`, before `_load_lock`.
4. Log line: `card: whisper[whisper-large-v3-turbo] waits for yue2.cpp (busy: music job
   01J…); wait about 6 min (estimate), up to 600 s`.
5. `card_queue()` shows the Whisper row with `wait.behind == ["yue2.cpp (music job …)"]`.
6. The door's `on_wait` names the holder to the person: "transcription queued behind
   yue2.cpp (song, ≈6 min)", with a cancel.
7. When the job ends, the engine is idle.
8. The Whisper waiter yields it (`stop("swap")`), then is granted, loads and transcribes.
9. On timeout, `LeaseWaitedTooLong` names "yue2.cpp, busy with music job …".

On the owner's 32 GB card, L11 should find the fit at step 3 (declared: turbo's weights,
about 1.6 GB, + 1.7 GB context + 5.6 GB YuE2 peak + 2 GB reserve, well under 32 GB; live:
far more free than Whisper's peak plus the music growth left) and Whisper loads beside at
once, as today, now named in `card_holders()` and the log **[unverified: the weights figure
is from general knowledge; S3 reads it from `model_size_bytes`]**.

**What S1+S3+S4 do not change** (Ark #44, finding 5), said plainly for Mike:

- Whisper **still loads beside llama-server and Strata**. It waits only for music and for
  other Whisper aliases.
- So open point 1's "Whisper no longer loads beside a model: refuse / wait / load anyway"
  is **not** answered by S3. "Refuse" or "wait" need the lease to wait for supervisors,
  which is S9 (`excludable=True`). The CPU route that keeps agents on a local engine from
  meeting `transcribe_audio_file` refusals in between is already in S3 (L12). S2 makes the
  other direction work (supervisors wait for Whisper).
- L12's CPU route in S3 applies against a busy **music** holder only; against a busy local
  LLM, Whisper still loads beside it until S9.

#### 3.4 Embedder, then GLiNER (S6, S7)

- **Device mode.** New value `lease` for `[memory] embedding_device` and
  `[knowledge_graph] gliner_device`. Proposal: `auto` comes to mean `lease` on CUDA. This
  is a further decision for Mike, outside the six below (no default is proposed beyond
  the one stated).
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

### 4. What the UI and logs show

**Logs, one INFO line per transition, no repeats while polling:**

- `card: <display> granted (<what>)`
- `card: <display> loaded beside <holder display> (<why: fits N MiB | v1: Whisper does not
  wait for local LLM engines>)`
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
  - **A failed read is "unreadable", never "free".** Background leases are refused (CPU),
    **and the refusal logs its reason** ("background lease refused: the card probe could not
    be read: <error>"), so "CPU instead of GPU" never looks like chance (Ark #45, D5).
    Foreground leases go ahead with a WARNING naming the failure. **Open for Mike, item
    5:** fail closed for foreground too?
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
- **Negative:** the CPU route keeps a second copy of the model in host RAM while it is
  loaded (about 3 GB for turbo, unverified), and a CPU transcription competes for CPU time
  with whatever else runs, Strata's expert offload included.
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
- **Negative:** a **new refusal for peers** while music runs and Whisper does not fit.
  Today a peer would load Whisper beside the music engine.
- **Negative:** S1+S3+S4 still let Whisper load beside llama-server and Strata (section
  3.3); only S9 changes that.
- **Negative:** with swap off, supervisors now take queue places when only leases block
  them. That path existed only with swap on until now.
- **Negative (Ark #44):** with swap off, **background calls start being skipped** while a
  lease-only place waits, because `_CARD_QUEUE` is then non-empty and the background check
  at `llama_server_supervisor.py:2809` skips on a non-empty queue. Today, with swap off, a
  background call never meets that check. Mike would see it as background work in agent
  chats being skipped with the reason "a request is waiting in the queue for the card".
  Own test (Confirmation, 16).
- **Negative:** a music job behind an idle **supervisor** still waits until a person
  presses Stop X, because supervisors never idle-stop (SPEC decision 32, kept in v1).
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
  - In v1, Whisper does not wait for supervisors at all. A waiting music lease holds
    nothing.
  - The one way to violate L2 is a request that stands on X (`_CARD_HOLD`) and waits for
    something that waits for X. That is caught at once by `LeaseWouldWaitOnItself` /
    `_this_request_holds`, for a lease holder and a supervisor holder alike (2.2 item 4).
    Examples: an agent turn on Strata whose tool would wait for Strata (only once S9 makes
    Whisper excludable; Zcode notes this makes Q1 unreachable through Whisper in v1); a
    tool that holds a Whisper use and then calls the LLM inside the same tool.
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
- **The CPU route under memory pressure.** A 3 GB CPU model loaded while Strata keeps its
  experts in host RAM could push the machine into paging; L12 point 6 checks available
  RAM first, and the CPU pipeline has its own idle unload.
- **A wrong CPU factor.** A factor measured on an idle machine under-reads a busy one, and
  a short file could then take longer on the CPU than the line would have. The file is
  short by definition (`cpu_max_audio_s`), so the loss is bounded; the factor is updated
  after every CPU run.
- **Starvation.**
  - Both lines are FIFO on one clock (L5a). L5 stops new uses of a granted lease while
    someone waits for it.
  - Music behind a **supervisor**: the music job waits for an idle llama-server until a
    person presses Stop X, because supervisors never idle-stop. That is today's behaviour
    under decision 32. Amending decision 32 (swap on: music yields an idle supervisor) is
    **Open for Mike, item 4**.
  - A later supervisor start may swap a supervisor that a music lease is waiting for (L5a
    point 3, the decision 32 exception). The music job loses nothing by it: it waits for a
    person's Stop X in both cases.
  - A series of server starts cannot overtake a music lease that waits for a lease holder
    (L5a point 4; Confirmation, 3b).
- **Ping-pong.** Dictation, then llama-server, then dictation: each swap costs an unload or
  reload of Whisper, which is cheap. Llama-server is never stopped for Whisper in v1. A
  safety net like `_MAX_SWAP_ROUNDS` applies to lease contenders too.
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
  - A peer lease takes no place and does not wait (rule 7). It is granted if the card is
    free, if only idle cheap leases stand in its way, or if L11 finds a fit beside busy
    music.
  - Yielding an idle music engine for a peer happens only on the owner's terms (swap on,
    nothing queued, nothing busy, as in `_why_a_peer_may_not_swap`).
  - The refusal is worded for a peer and says "right now".
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
   busy music job and are refused naming it at their deadlines; (c) the decision 32
   exception: a music lease waiting only for an idle supervisor does not hold up a later
   supervisor start that swaps that supervisor.
4. `test_a_transcription_during_a_running_yue2_job_waits_and_names_the_holder.py`: **the
   Done-when**, with the reading faked to "does not fit"; plus the reverse (a music job
   yields an idle warm Whisper and waits for a busy one, `card_wait.holders` naming
   Whisper).
5. `test_llama_server_and_strata_wait_for_a_busy_lease_and_unload_an_idle_one.py`: with
   swap on **and** swap off; the swap note and `_LAST_SWAP` (no "parked" words, the
   lease's toast title); a lease that does not let go within `stop_bound` gives a refusal
   that names it.
6. `test_with_no_lease_registered_the_card_behaves_as_before.py`: `_live_holders() ==
   _live_claims()` and `card_holders()` rows equal the old rows plus `kind`.
7. `test_a_background_lease_never_queues_and_never_yields.py`: the Whisper preload and the
   embedder fake give `LeaseSkipped` while anything holds the card or someone waits.
8. `test_a_peer_lease_takes_no_place_and_is_refused_in_a_peers_words.py`: assert the text
   contains no alias, agent or conversation ids, and says "right now".
9. `test_a_lease_that_would_wait_on_its_own_request_is_refused_at_once.py`: inside
   `card_held_for(alias, "accepted_request")` and the use-carries-hold case; **and the
   supervisor-holder variant** (Zcode #41): a turn on Strata whose code waits for a lease
   that waits for Strata is refused at once. Elapsed < 0.5 s in each case.
10. `test_whisper_holds_one_lease_across_a_reload_and_lets_go_on_every_unload.py`: idle
    timer, deferred, `release_if_loaded_here`, switch, shutdown, a yield; the CPU device
    makes no lease; an `mlx` provider loaded by hand and never granted releases nothing.
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
    raising) → **waits in the lease line, neither beside nor refused**, logged as unknown;
    a live reading never turns a declared "no" into "yes"; an earlier waiter blocked by the
    music lease → no co-grant; the music lease's busy, yield and stop are unchanged by the
    co-holder; a peer or background call that does not fit is refused at once; no other
    pair (Whisper × llama-server, music × Whisper) is ever co-granted by measurement.
19. `test_whisper_beside_a_local_llm_engine_says_so.py` (Zcode #41): v1 load beside
    llama-server logs the "loaded beside" line once and sets `beside`.
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
28. `test_the_fit_counts_every_resident_not_only_those_in_the_way.py` (Ark #60 §5, #61
    §2b): three residents (a llama-server child with `predicted_vram_mib`, a busy YuE2, a
    co-living second Whisper alias) and a Whisper contender: the fit sums all three; the
    yield list holds only those that exclude the contender; a sum built from the yield list
    would say "fits" and the test proves L11 says `does_not_fit`. With a Strata child as a
    resident the outcome is `unknown`. `dwm` in a fake per-process reading changes nothing.
29. `test_a_running_job_waiting_for_the_card_does_not_start_its_engine.py` (Ark #60 §4):
    a job in `running`, its engine's child gone, the card held by a busy holder: the engine
    does not start, the job shows its `card_wait`, it is neither failed nor dropped as
    cancelled, and it starts when the holder lets go; a real cancel during that wait ends
    it.
30. `test_a_short_transcription_goes_to_the_cpu_only_when_that_is_quicker.py` (L12, Mike
    #62): card free → GPU; L11 `fits` → GPU beside; busy and short with a measured factor
    and a long ETA → CPU, no lease taken, no holder row; busy and short with an unknown
    ETA → CPU (unknown counts as long); busy and longer than `cpu_max_audio_s` → the line;
    CPU slower than the known GPU wait → the line; no measured factor → the line; too
    little RAM → the line; background short → CPU, background long → `LeaseSkipped`; a
    peer → refused at once, never CPU.
31. `test_the_cpu_pipeline_holds_no_lease_and_unloads_on_its_own.py`: the CPU pipeline
    loads on demand under its own lock while a GPU yield holds `_load_lock`; it never
    touches the lease; its idle timer unloads it; `language` and `task` are per call; the
    result carries `device: "cpu"`; the CPU factor is recorded after the run.

Compliance checks beyond tests:

- [ ] With no lease registered, every existing card test passes unchanged (full core suite
      before any "green" claim).
- [ ] No lease is acquired while a client lock is held.
- [ ] No wait estimate ever reads 0 when unknown; no refusal text for a peer names an
      alias, agent or chat.

## Scope

- new `dpc-client/core/dpc_client_core/managers/card_lease.py` (lease, line, refusals,
  wait estimate, L5a, L11's one shared cell) and, in S8, `managers/card_probe.py`
- `managers/llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_LEASE_WAITERS`,
  `_live_holders`, `card_holders`, `card_queue`; and, in S2, section 2.2 items 3–10)
- `providers/whisper_provider.py` (designed against its interface; coordinate with the
  agent editing it), `service.py:4797`, `music/commands.py:324`, `tools/transcribe.py:172`,
  `p2p_coordinator.py:1398`, `service.py` (`transcribe_audio`'s bound, the provider cascade
  at 5152–5160, the peer check), `coordinators/telegram_coordinator.py:509`,
  `managers/agent_telegram_bridge.py:1674`, `settings.py` (`[local_transcription]
  queue_wait_s`), `ui/src/lib/coreService.ts:1492`
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

Nothing is built. Recommended order: S0, then S1, then S3 and S4 together (they meet the
Whisper card's Done-when), then S2 (Mike's "llama-server and Strata wait"), then S5–S10.
S2 could be pulled ahead of S3 if Mike wants supervisors honouring leases first; it changes
the 5,555-line file, so land it after the Whisper editing settles.

| # | Commit | Touches | Proves | Status |
|---|---|---|---|---|
| S0 | docs: this ADR (the design proposed an ADR-044 amendment; this record carries the matrix, L1–L11 and the six decisions instead), revised after reviews #41, #44, #45 | `docs/decisions/052-…md` | — | This commit |
| S1 | feat(card-lease): lease registry, lease line, one order across both lines (L5a), refusals, wait estimate; `card_holders`/`card_queue` include lease rows through the hook (supervisor rows first, `until_cancelled`); `card_holder()` stays supervisor-only | new `managers/card_lease.py`; `llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_LEASE_WAITERS`, `_live_holders`, `card_holders`, `card_queue`); `tests/conftest.py` | tests 1–3, 6–8, 14 with fake clients only | Pending |
| S3 | feat(whisper): the local model holds a card lease, yields by unloading when idle (`yield_card`, releasing only when the unload ran), loads beside busy music only in the one shared cell when it provably fits (L11), otherwise chooses the CPU route for a short file when that is quicker (L12, Mike #62) or queues before `_load_lock`; a second, CPU pipeline with no lease; doors pass `purpose` and their bounds; every caller gets a deadline and the queue's is shorter; a card refusal ends the provider cascade; the peer check runs again after a wait; llama-server children record `predicted_vram_mib` | `providers/whisper_provider.py`; `service.py` (`transcribe_audio` bound, the cascade at 5152, the peer check), `music/commands.py:324`, `tools/transcribe.py:172` (`purpose`, `on_wait`); `p2p_coordinator.py:1398` (`serving_a_peer`); `coordinators/telegram_coordinator.py:509`, `managers/agent_telegram_bridge.py:1674`; `settings.py` (`queue_wait_s`, `cpu_max_audio_s`); `ui/src/lib/coreService.ts:1492`; `llama_server_supervisor.py` (`predicted_vram_mib`, one attribute set at launch) | 9, 10, 18–24, 26–28, 30, 31 | Pending; lands with S4 |
| S4 | feat(music): music engines hold the card lease (acquired before `_the_lock`); the runner waits through it and names lease holders; a `running` job may wait for the card (both gates: `jobs.py` and the runner's wait loop) | `music/engine_base.py`, `music/runner.py`, `music/commands.py` (start by hand), `music/jobs.py`, `music/acestep.py`/`yue2.py`, test helpers, `ui/src/lib/utils/musicCreate.ts` | **4 (Done-when)**, 11, 13, 15, 17, 29 | Pending; lands with S3 |
| S2 | feat(card): llama-server and Strata wait for a busy lease and unload an idle one, with the swap on or off | `llama_server_supervisor.py` (2.2 items 3–10), `providers/llamacpp_server_provider.py` (`stop_local_engine`, then `card_holder` may return lease rows) | 5, 16, and the full list of existing card tests green | Pending |
| S5 | feat(ui): lease rows and lease waiters in the existing holder and queue lines, "until cancelled", unknown ETA as "—"; whether the block shows without a local LLM provider | `ProvidersEditor.svelte`, `cardQueue.ts`, `cardQueue.test.ts`, `MusicCreate.svelte`; the status payload | 25 and manual | Pending |
| S6 | feat(memory): the embedder joins the lease (GPU for rebuilds, CPU when skipped), with a real unload | `dpc_agent/memory.py`, `managers/agent_manager.py`, `settings.py` | fake-thread client tests | Pending |
| S7 | feat(graph-ner): GLiNER gains an unload and joins the lease | `dpc_agent/knowledge_graph.py`, `sleep_pipeline.py` | same | Pending |
| S8 | feat(card-probe): foreign GPU use from PDH/NVML, own pids and desktop excluded, unreadable is never free and says why | new `managers/card_probe.py`, `process_registry.py` | 12 | Pending |
| S9 | feat(whisper): Whisper waits for supervisors (`excludable=True`): L12 against a busy local LLM, the self-wait case on the CPU route, one order across both lines in `_stands_before_the_line`; a second shared cell (Whisper beside llama-server) only if Mike and the reviewers take it | `card_lease.py`, `whisper_provider.py`, `llama_server_supervisor.py` (`_stands_before_the_line`) | supervisor-holder and self-wait tests | Pending |
| S10 | feat(local-api): external lease with a 90 s TTL and heartbeat | `local_api.py` | HTTP tests | Pending |

### Backlog cards

**Closes or advances**

- `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…` (161): part (1), the queue, closes with S1+S3+S4
  (+S2). Part (2) (GPU choice and CPU fallback) is now mostly S3 (L12, the CPU route); its
  remainder against supervisors is S9. Part (3) (per-use models) stays open. Step 5 of the
  Whisper plan gains the CPU real-time factor measurement.
- `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-…` (1524): this ADR is its option (b)
  in-process plus (c) as S8. The HTTP surface in `local_api.py` (S10: a lease with a 90 s
  TTL and heartbeat, `kind="external"`) closes it. That card's Done is not met until S10.
  Record that this answers ADR-051 **N11** as "(b)+(c)", but only after Mike confirms. He
  has not chosen N11.
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
- `THE-ADMISSION-ARITHMETIC-SIZES-ONE-CHILD-AND-CANNOT-SEE-THE-OTHERS` (7119): S9 and L11
  use the lease estimates.
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

- A SPEC decision 32 amendment, if Mike lets music yield idle supervisors (item 4).
- ADR-044 and ADR-051 are not edited by this record. If accepted, ADR-044 may get a short
  pointer to it.

## Open for Mike

Six decisions, each with the proposed default and the reviewers' votes, plus the
sub-choices under decision 6. They must be answered before S2; decisions 3 and 6 also
before S3.

1. **The Whisper/supervisor asymmetry in v1.** Supervisors wait for Whisper, but Whisper
   does not wait for supervisors (it loads beside llama-server and Strata, as today, and
   now logs that it did). *Proposed default:* keep the asymmetry for v1; S9 replaces it
   (`excludable=True`, with L12's CPU route, which S3 already brings).
   *Votes:* Zcode — the default. Ark — the default (otherwise dictation meets
   `swap_wait_s` = 1800 s).
   **Said plainly:** S1+S3+S4 do not stop Whisper loading beside llama-server or Strata.
   The earlier open point "Whisper no longer loads beside a model: refuse / wait / load
   anyway" is answered only by S9 (and S2 for the other direction), not by S3.
2. **Lease yields not gated by `swap_when_idle`.** Unloading Whisper or stopping an idle
   music engine for a contender happens even with the switch off; the switch keeps
   governing only llama-server and Strata. *Proposed default:* not gated. The costs differ:
   unloading Whisper is a reload of about 3 GB in seconds; stopping an idle music engine
   costs the next job an engine boot and a 5–9 GB model reload; neither loses anyone's
   work.
   *Votes:* Zcode — the default. Ark — the default, with the cost difference named (done
   above).
3. **Per-door waits (L4).** *Proposed default (revised after Mike's #49–#50 and #62, and
   the reviews #52, #53):* a transcription first goes through L12 (GPU now, CPU, or the
   line); in the line, the owner's dictation, received voice messages, Telegram voice and
   the agent tool wait up to the **dictation bound** (decision 6, sub-choice A: 600 s), and
   never longer than their caller's own remaining time less a margin; every caller gets a
   deadline (the local path and Telegram have none today); music jobs and music Transcribe
   wait until cancelled; peers 0 (refused at once, "right now"); background transcriptions
   short on the CPU, long skipped; other background calls never wait. The first draft's
   `[local_transcription] card_wait_s` = 120 is dropped.
   *Votes (both given on the 120 s version, before the revision):* Zcode — ETA-early-refuse
   first, then 120 s makes sense. Ark — 120 s, peer 0 s with "right now" in the refusal
   (adopted). On the revised shape: Ark #53 — the queue's deadline must be
   `min(caller's remaining, bound)` (adopted); Zcode #52 — a dictation bound of its own,
   about 10 min (proposed as the default of sub-choice A).
4. **SPEC decision 32.** Music never stops an idle supervisor (it waits until a person
   presses Stop X). *Proposed default:* kept as is in v1, with the one-order exception of
   L5a point 3. The alternative, with swap on, lets music yield an idle supervisor and
   removes that exception.
   *Votes:* Zcode — the default. Ark — the default (otherwise music would drop Strata's
   parked conversations for a generation).
5. **An unreadable probe (S8).** *Proposed default:* fail closed for background leases
   only (they fall back to CPU, and the refusal logs its reason); foreground leases go
   ahead with a WARNING naming the failure. The alternative fails closed for foreground
   leases too.
   *Votes:* Zcode — the default. Ark — the default, plus the logged reason (adopted).
6. **Dictation and transcription while the card is busy, in the first slice.** S1+S3+S4
   make Whisper and music exclusive, so without more, dictation during a YuE2 job (minutes
   long) would wait and then be refused where today it loads beside and works: a
   regression (Zcode #41). Options considered: (a) ETA-early-refuse; (b) a CPU fallback;
   (c) accept the regression; (d) load beside when it fits (L11).
   **Mike's decision at #62** settles the shape: a smart GPU/CPU choice. *Proposed rule,
   **pending Mike's OK of the details**:*
   1. The card is free, or Whisper provably fits beside a busy music engine (L11, the one
      shared cell) → the GPU at once, visible as two holder rows when shared.
   2. The card is busy and it does not fit (or the fit is unknown) → compare the GPU wait
      (the holders' ETA plus the line ahead; unknown counts as long) with the CPU time
      (audio duration × the measured CPU real-time factor). **The CPU** when the audio is
      short (`[local_transcription] cpu_max_audio_s`, proposed 120 s) and the CPU is
      quicker; **otherwise the line** for the GPU: before `_load_lock`, one place per call
      (a card refusal ends the provider cascade), the peer check again after the wait,
      deadline `min(caller's remaining − margin, dictation bound)`, visible
      ("transcription queued behind yue2.cpp (song, ≈6 min | time unknown)") and
      cancellable.
   3. Background transcriptions: short on the CPU, long skipped.
   4. Peers: refused at once, "busy right now" (sub-choice C).
   5. No measured CPU factor yet → no CPU route; the line (CC's proposal, pending review).
   6. Not enough free RAM for the CPU model → the line (proposal).
   This moves (b), the CPU route, from S9 into S3 (section 3.1). (a) survives only as the
   peer's immediate refusal; the ETA never refuses on its own.
   **Sub-choices for Mike:**
   - **A. The dictation bound:** `[local_transcription] queue_wait_s` = 600 s. *Votes:*
     Zcode #52 and Ark #60/#61 both for 600 s; CC leans the same way. The alternative,
     inheriting `swap_wait_s` (1800 s here), would leave the agent tool (1800 s timeout)
     no time to transcribe after the wait (Ark #60 §7).
   - **B. An unknown ETA:** **replaced by Mike's #62.** An unknown ETA counts as a long
     GPU wait: a short file goes to the CPU, a long one waits with "time unknown" up to
     the bound. Neither reviewer's former option (refuse at once, Zcode #52; wait, Ark
     #53/#60/#61) is needed any more.
   - **C. Peers on the owner's CPU.** *Proposed default:* no; a peer is refused at once
     while the card is busy, as today. Spending the owner's CPU on a peer's file needs
     Mike's word (#62/#63).
   - **D. The short-file threshold:** `cpu_max_audio_s` = 120 s proposed (Mike's "1–2
     minutes" at #62); to be checked against the CPU factor measured in step 5 of the
     Whisper plan.
   *Votes:* on (d) + queue, Zcode #52 and Ark #53 accept with conditions, all of them in
   this record (L4, L5a, L11, section 3.1, tests 18–24). On Mike's #62 GPU/CPU choice, the
   short threshold and the unmeasured-CPU rule, neither reviewer has voted yet (CC asked
   at #63).

Also open, and not among the six: whether `auto` means `lease` on CUDA for the embedder
and GLiNER (section 3.4), whether L11 should also spare an *idle* music engine its stop
when Whisper fits beside it, and ADR-051 N11, which this ADR answers as "(b)+(c)" only once
Mike confirms.

## Open Questions

- **Q1:** Whether any agent tool calls the LLM while a transcription use is open (a
  `LeaseWouldWaitOnItself` risk). **[unverified]** In v1 the self-wait is unreachable
  through Whisper, because Whisper does not wait for supervisors (Zcode #41); the question
  moves to S9's risks. — CC
- **Q2:** Answered (section 2.3): no test resolves a real CUDA device; two build `mlx`
  providers, which get a lease that must stay harmless.
- **Q3:** Whether the PDH counter is vendor-neutral on AMD and Intel. **[unverified]** — CC
- **Q4:** Answered (section 0): the frontend reads the card keys in 12 files.
- **Q5:** The prior-art lines in (b) above are from general knowledge and unread here. —
  CC
- **Q6:** Whether `nvidia-smi --query-gpu=memory.free` reads correctly under WDDM while a
  music child runs (L11's declared check reads `memory.total` and its live veto
  `memory.free`; only the per-process query is known to read `[N/A]`).
  **[unverified]** — CC
- **Q7:** The reload times behind decision 2's cost difference (Whisper vs an idle music
  engine). **[unverified]** — CC
- **Q8:** The CPU route's figures: host RAM of turbo in fp32 (~3 GB from the parameter
  count) and its real-time factor on this CPU (step 5 of the Whisper plan). Until
  measured, L12 does not route to the CPU. **[unverified]** — CC
- **Q9:** Whether the KV type a llama-server child was started with is readable on the
  supervisor at launch, so `predicted_vram_mib` can be recorded without re-running the
  ladder (the ladder writes it to the fit memo, 3514–3639). **[unverified]** — CC

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

## Authors

- **Mike** — Decisions (#20 and #62, 2026-11-21); question at #49–#50
- **CC** — Request, review, this record
- **Ark, Zcode** — Consulted; reviews #41, #44, #45, #52, #53, #59, #60, #61

## References

- [ADR-044](044-strata-as-a-second-supervised-local-engine.md) — the card queue, the swap
  and `_CARD_CLAIMS`
- [ADR-051](051-the-music-player-is-a-dpc-window-over-one-core-music-service.md) — the
  music player; open question N11
- ADR-046 — supervised processes (option (a))
- Backlog card
  `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED`
- Backlog card `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…` (the Whisper plan)
- `ai-studio/experiments/music-player/SPEC.md` — decision 32
- Design source: the card-lease design, `dev` HEAD `b9725990`; revision checked at
  `5099b326`
- Reviews: AI Studio group chat, messages #41, #52 and #59 (Zcode), #44, #45, #53, #60
  and #61 (Ark), #49–#50 and #62 (Mike)
