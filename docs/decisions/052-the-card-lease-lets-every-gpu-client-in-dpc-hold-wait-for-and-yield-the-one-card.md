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
> agent for CC and checked against `dev` HEAD `b9725990` (the orbit index was at
> `39d79624`, so every line number below was confirmed by reading the files). **Decided by
> Mike so far, and only this:** at #20 in the AI Studio group chat he approved that
> llama-server and Strata wait for Whisper or music to free the card, and that the
> embedder and other GPU-capable parts join later. Everything else, including the five
> items under **Open for Mike**, is a proposal. Anything marked **[unverified]** was not
> confirmed. Line numbers drift.
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
  `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…`).
- Mike's ask at #20, that llama-server and Strata wait for Whisper or music, has nothing
  to act on, because those clients hold nothing the queue can see.
- A GPU job DPC did not start can neither ask an idle holder to give the card back nor be
  waited for (backlog
  `EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED`,
  filed 2026-11-06; ADR-051 N11).

### 0. What the code does today

Findings of the design, which correct or sharpen the earlier survey.

**The card registry** (`managers/llama_server_supervisor.py`)

- `_CARD_CLAIMS` (657) is a `Dict[LlamaServerSupervisor, str]`. Only `_claim_card` (2486)
  and `stop()` (5421–5440) write to it.
- `_live_claims()` (660) reads it. Its callers are `card_holders()` (681),
  `_blocking_holders()` (2500), and `strata_supervisor._pin_launcher_running` (~2829).
- `_excludes(other)` (2479) is `other.ENGINE != self.ENGINE or either.EXCLUSIVE_ON_THE_CARD`.
  Strata sets `EXCLUSIVE_ON_THE_CARD = True` and `DROPS_PARKED_ON_STOP = True`
  (`strata_supervisor.py:1038-1047`).
- Tests poke the dict directly: `conftest.py:86`,
  `test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card.py:554`,
  `test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced.py:360,1498` and
  8 more. So `_CARD_CLAIMS` must keep its exact shape.

**The queue**

- `_CARD_QUEUE` / `_CardPlace` / `_QueuedRequest` are at 1025–1180 and `card_queue()` at
  1040.
- The queue only exists while `swap_when_idle` is on. `_wait_in_the_card_queue` (2787)
  returns None when the switch is off, and `_claim_card` then refuses at once with
  `CardHeldError`.
- `_free_the_card` (3009) touches a holder only through a small duck-typed surface:
  `alias`, `ENGINE`, `_draining`, `_swap_for`; `_busy_for_the_card(state, but_for)`,
  `_begin_swap_drain(new)`, `_end_swap_drain()`; `DROPS_PARKED_ON_STOP`,
  `_parked_conversations()`; and `stop_by_hand(holder, why="swap")` (1615) via
  `_stop_for_the_swap` (3117).
- That small surface is what makes a lease holder possible without rewriting the queue.

**Request holds**

- The `_CARD_HOLD` contextvar (754) follows the agent turn into tool threads:
  `dpc_agent/loop.py:1656` uses `carrying_card_hold`.
- So a lease can tell when "this request already stands on X". The existing pattern for
  that check is `_this_request_holds` (893).

**Music**

- `music/runner.py:656-683` `_wait_for_card` polls `card_holders()` every 3 s and stops
  nothing (SPEC decision 32).
- It also treats a failed read of the holder list as a free card (`runner.py:665-667`,
  "treating the card as free"). That breaks the Ruach rule that a failed read is never
  "all free".
- `MusicEngines.exclusive` (`music/engines.py:95-129`) stops an idle other music engine
  with no switch.
- `SupervisedEngine.in_use` / `_idle_loop` are at `music/engine_base.py:657-680`. Music
  never appears in `card_holders()`.

**Whisper** (`providers/whisper_provider.py`; interface only, since another agent is
editing it)

- `transcribe(audio_path, release_if_loaded_here=False, language=None, task=None)` takes
  `_load_lock` and loads under it.
- The rest of the surface: `_active_transcriptions`, `unload_if_idle(reason)`,
  `unload_model_async(reason)`, `_unload_locked`, `schedule_idle_unload`,
  `hand_over_to(new)` / `_current_owner()`, `retire()`, `shutdown_unload()`, and
  `ensure_model_loaded()` (the preload that `voice_service.py:228` calls).
- Device is chosen inside `_load_model` (`_detect_device` imports torch).
- Callers of `transcribe`: `service.py:4797` (owner's voice); `p2p_coordinator.py:1397`
  (peer; **not** wrapped in `serving_a_peer`); `dpc_agent/tools/transcribe.py:172` (agent
  tool, runs inside a turn that holds `accepted_request`); `music/commands.py:324` (music
  Transcribe, `release_if_loaded_here=True`).

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

**Per-process VRAM readers already exist** in `process_registry.py`:

- `read_vram_windows()` (2028) uses the PDH counter
  `\GPU Process Memory(*)\Dedicated Usage`. That is a WDDM counter, so it is
  vendor-neutral on Windows. It is a commitment, not a physical figure.
- `read_vram_nvml()` (2112) works on Linux, NVIDIA only.
- On macOS, `_default_vram_reader` returns `None`.
- `nvidia-smi --query-compute-apps` reads `[N/A]` per process under WDDM
  (`eval/gaia/campaign.py:129`), so it is useless for per-process figures on Windows.
- `_free_vram_mib()` (1793) returns None both when there is no nvidia-smi and when the
  read fails. It cannot tell the two apart.

## Decision Drivers

- **One card, many clients.** Whisper, two music engines, the embedder, GLiNER, llama-server
  and Strata all want the same 32 GB, and only two of them are visible to the queue.
- **Mike's #20 ask.** llama-server and Strata must wait for Whisper or music, and later
  parts must be able to join without a redesign.
- **The queue already works.** `_free_the_card` reaches a holder through a small
  duck-typed surface. A new kind of holder that speaks that surface costs a few edits, not
  a rewrite of a 5,555-line file other agents are editing.
- **Cross-platform.** Windows, Linux and macOS are mandatory. The arbitration among DPC's
  own clients must not depend on any vendor tool being present.
- **A failed read is never "free".** The Ruach rule. Today music breaks it
  (`runner.py:665-667`).
- **Reload cost differs by client.** Unloading Whisper is cheap. Stopping Strata drops
  parked conversations and costs a long reload. The rules should follow that cost.
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
are read together through a single union, `_live_holders()`. Foreign GPU use is detected
by a platform-aware probe (S8), and an HTTP lease for external processes follows later
(S10).** This is option (b), in-process, plus option (c) from the card (see Considered
Options); the supervised-worker option (a) is not taken.

`_CARD_CLAIMS` is **unified with, not replaced by** the lease registry (section 2.3).

### Rationale

- The lease is pure Python and in-process on all three operating systems. It serialises
  DPC's own clients whether or not any GPU tool exists. A lease never asks nvidia-smi
  whether it may be granted; it asks the registry.
- A lease with `EXCLUSIVE_ON_THE_CARD = True` speaks the surface `_free_the_card` already
  uses, so llama-server and Strata honour it with a handful of small edits.
- Music, Whisper, the embedder and GLiNER already differ in what yielding costs. The lease
  carries `yield_cost` and `on_yield` per client, so each unloads in its own way.
- It does the one thing option (a) cannot: it works for in-process models (Whisper, the
  embedder, GLiNER), which have no child process to supervise.

### 1. API

New module: `dpc-client/core/dpc_client_core/managers/card_lease.py`. The supervisor
module does not import it at load time (avoids an import cycle). Instead the lease module
installs a hook into it, the same pattern as `_still_configured` at 508:

```python
# llama_server_supervisor.py
_LEASE_HOLDERS: Callable[[], List[Tuple[Any, str]]] = lambda: []   # set by card_lease on import

def _live_holders():                     # NEW; _live_claims stays supervisor-only
    return _live_claims() + list(_LEASE_HOLDERS())
```

#### 1.1 Objects

```python
class CardLease:
    """One client's standing on the card: registered once; granted, used, released many times."""
    IS_CARD_LEASE = True
    EXCLUSIVE_ON_THE_CARD = True        # read by LlamaServerSupervisor._excludes unchanged
    DROPS_PARKED_ON_STOP = False
    def __init__(self, holder: str, *, kind: str, engine: str, display: str,
                 vram_mib: Optional[int],            # None = unknown, never 0
                 yield_cost: Literal["cheap", "engine"],
                 on_yield: Callable[[str], Awaitable[bool]],   # client unloads; True = it let go
                 waits_for_engines: bool = False,    # see the matrix in 2.1
                 eta_s: Callable[[], Optional[float]] = lambda: None): ...
    alias -> str                 # == holder; the key in card_holders/card_queue/stop_local_engine
    state -> "free" | "waiting" | "granted" | "yielding"
    async def acquire(self, what: str, *, wait_s: Optional[float], background: bool = False,
                      check: Optional[Callable[[], Awaitable[None]]] = None,
                      on_wait: Optional[Callable[[dict], None]] = None) -> None
    @asynccontextmanager
    async def use(self, what: str, **acquire_kw)     # acquire if not granted, then uses += 1 for the body
    def release(self, why: str) -> None              # sync; routed via _now_on_the_owner_loop
    def acquire_blocking(self, what, **kw) -> None   # for thread clients; RuntimeError on the owner loop
    def rebind(self, on_yield) -> None               # Whisper's providers-reload hand-over
    # the holder surface _free_the_card uses:
    _draining, _swap_for, _busy_for_the_card(state, but_for=None), _begin_swap_drain(new),
    _end_swap_drain(), async _parked_conversations() -> 0, who_uses() -> str,
    async give_up_for(contender_alias: str, bound: float) -> None
```

Holder names: `whisper:<alias>`, `music:yue2`, `music:acestep`, `embedder:BAAI/bge-m3`,
`ner:gliner`. Kinds: `transcription`, `music`, `embedding`, `ner`, and later `external`.

#### 1.2 Rules

**L1. Grant.**

- `acquire` runs on the owner loop: it is wrapped `on_the_owner_loop` at line 319, so
  callers on a foreign loop work.
- The "nothing excludes me" check and the grant happen with **no await between them**, the
  `_claim_card` discipline. Two concurrent acquires cannot both pass.
- A lease that is already granted is re-entrant: `use()` only increments its use count.
- Busy means `uses > 0`, or a load in progress, or `state == "yielding"`.

**L2. Waiting holds nothing.**

- A waiting lease is in `_LEASE_LINE`, a FIFO in the lease module. It holds no grant.
- A granted lease never waits for the card again.
- So the wait-for graph is a forest and has no cycles (see Risks).

**L3. Refusals.** All subclass the existing `CardQueueRefusal(holder_alias,
holder_engine)`, so the gateway, the peer door and `card_queue_refusal_in` already read
them as "never retried".

- `LeaseRefusal`: the base; it carries a `.wait` estimate.
- `LeaseWaitedTooLong`: the deadline passed. It names the holder, why it is busy
  (`who_uses()`, or `_who_stands_on` for a supervisor) and the bound.
- `LeaseWouldWaitOnItself`: the request in `_CARD_HOLD` stands on the very holder it would
  wait for. Refused at once, as rule 9 does.
- `LeaseSkipped`: `background=True` and the card is not free right now. The client falls
  back to CPU.
- `LeaseRefusedForPeer`: worded for a peer. It names no alias, agent or chat (rule 7).
- `LeaseClosed`: the backend is stopping.

**L4. Deadlines.**

- `wait_s=None` means wait until `check()` raises (music jobs, music Transcribe).
- Otherwise there is one deadline, fixed at acceptance (rule 5).
- Default per door (**Open for Mike, item 3**):
  - owner's dictation: `swap_wait_s`, capped by a new `[local_transcription]
    card_wait_s`, proposed default 120
  - agent tool: the turn hold's own deadline, via `waiting_for_the_card_at_most`
  - peer: 0 (no wait)
  - background: never waits

**L5. Order (rule 4 analog).**

- One clock: the existing `_CARD_ARRIVAL` (698).
- While a contender (a lease in `_LEASE_LINE`, or a supervisor's first `_CARD_QUEUE` place
  whose blocking holder is this lease) waits for a granted lease, **new `use()` calls are
  not admitted**. They queue behind the contender.
- So a dictation stream cannot starve a music job or a llama-server start.

**L6. Yield ("please yield" to idle holders, wait for busy ones).**

- Only the first waiter acts, as in `_free_the_card`.
- In one synchronous stretch it checks `not holder._busy_for_the_card(...)`, then marks
  `holder._begin_swap_drain(contender)`, which sets `state = "yielding"`.
- Then it runs `give_up_for()` as its own task, held in a module set like `_SWAP_STOPS` so
  a contender's deadline or cancel never cuts a yield in half. That task calls
  `on_yield(reason)`:

  | Holder | `on_yield` does |
  |---|---|
  | Whisper | `unload_if_idle("card_yield")` (it takes `_load_lock` and refuses if a transcription slipped in) |
  | Music engine | `stop("swap")` if `not busy` |
  | Embedder / GLiNER | set a flag the thread reads between batches; it moves to CPU or drops the model, then `release()` |
  | Llama-server / Strata (supervisor holders) | unchanged: `stop_by_hand(holder, why="swap")` |

- If `on_yield` returns False, `_end_swap_drain()` puts the holder back to granted and the
  contender keeps waiting until its deadline.
- **Busy holders are never marked and never asked.** They are waited for and named.

**L7. Visibility.**

- `card_holders()` adds lease rows that keep the existing keys and add new ones:
  `{"alias": holder, "engine": engine, "state": "running"|"yielding"|"loading", "port":
  None, "kind", "display", "busy": bool, "uses": [{"what", "since"}], "vram_mib":
  int|None, "since", "yield_cost", "eta_s": float|None}`
- Supervisor rows get `"kind": "inference"`.
- `card_queue()` appends lease waiters:
  `{"alias": holder, "kind", "what", "queued_at", "give_up_at"|None, "wait": {...}}`
- The music `set_waiting_for_card` keeps only `alias` and `engine` (`jobs.py:607`), so it
  still works.

**L8. Release.**

- `release(why)` is synchronous and safe to call on any path: unload, stop, child exit,
  shutdown, failed load.
- Releasing a lease that is not granted does nothing.
- It wakes the first waiter. It logs one INFO line: `card: <display> let go (<why>), held
  <s> s`.

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
    time per `runner.py:93`), marked as an estimate, else None.
  - Supervisors: None.

### 2. Integration

#### 2.1 Who excludes whom (v1)

The guiding rule: a lease is visible to everyone, but who waits or yields for whom follows
Mike's words and the cost of a reload.

| contender \ holder | llama-server | strata | music lease | whisper / embed / ner lease |
|---|---|---|---|---|
| llama-server start | arithmetic, as today | excludes | **excludes: waits while busy, yields while idle** | **excludes: waits while busy, yields while idle** |
| strata start | excludes | excludes | **same** | **same** |
| music start | excludes: waits, **never stops it** (decision 32 kept) | same | music engines: `exclusive()` as today; their leases agree | **excludes: waits while busy, yields while idle** |
| whisper load | **not excluded in v1** (loads beside, as today) | **not excluded in v1** | **excludes: waits while busy, yields while idle** | same holder: re-entrant; another whisper alias: excludes |
| embedder / ner (background) | granted only if the card is free, otherwise CPU | CPU | CPU | CPU |

**Why Whisper does not wait for supervisors in v1.**

- Making it wait would turn dictation during an agent turn into a wait of up to
  `swap_wait_s` (1800 s on this machine).
- Swapping an idle Strata out for a 3 GB transcription drops parked conversations and
  costs a long reload.
- Mike's decision covers the other direction: supervisors wait for Whisper.
- The later fit check (slice S9) replaces this exception: Whisper shares with llama-server
  only when the measured free VRAM covers `vram_mib` plus `_DESKTOP_RESERVE_MIB`, and
  otherwise falls back to CPU.
- **Open for Mike, item 1:** confirm this asymmetry for v1, or make it strict.

**The swap switch.**

- `swap_when_idle` keeps governing only the stopping of llama-server and Strata.
- Yielding a cheap in-process lease (unloading Whisper) or an idle music engine is not
  gated by the switch. That matches the music idle stop and `MusicEngines.exclusive`
  today, and nothing is lost but a reload.
- **Open for Mike, item 2.**

#### 2.2 How llama-server and Strata start honouring leases

All in `llama_server_supervisor.py` unless noted. Small edits.

1. `_live_holders()` as above.
   - `card_holders()` and `_blocking_holders()` read `_live_holders()`.
     `_why_a_peer_may_not_swap` gets leases through `_blocking_holders()`.
   - `_live_claims()` and every `_CARD_CLAIMS` access stay supervisor-only, including
     `_this_request_stands_elsewhere_on_the_card` and Strata's `_pin_launcher_running`.
2. `_excludes(other)` needs no change: a lease has `EXCLUSIVE_ON_THE_CARD = True`.
3. `_wait_in_the_card_queue` (2803): `if not swap_policy()[0] or not self.on_the_card():
   return None` becomes:
   - return None if not on the card;
   - return None if swap is off **and** some blocking holder is not a lease.
   - In other words, with swap off a start may still take a place when only leases stand in
     its way.
   - The request carries `for_leases=True`, so `_wait_to_be_first` (2878) and
     `_free_the_card` (3029) skip their "the switch was thrown" early return for it.
4. `_free_the_card`, before the busy test: if the holder is a lease and its busy uses carry
   `request.hold` (the same `_CardHold`), raise `LeaseWouldWaitOnItself` at once.
   - The busy test is `holder._busy_for_the_card(state, but_for=request.hold)`. For a
     lease it counts only uses with an arrival earlier than the acting place (rule 4).
5. `_stop_for_the_swap` (3117): `if getattr(holder, "IS_CARD_LEASE", False): await
   holder.give_up_for(self.alias, request.stop_bound); live_saved = None` else
   `stop_by_hand(...)` as today. `_note_swap` words and `_LAST_SWAP` work unchanged
   ("swapped 'whisper:…' out for 'qwen'").
6. `_who_stands_on(alias)` (986): when `alias` is a lease, return `f" In use by
   {lease.who_uses()}."` Labels: "dictation", "music Transcribe of track T", "agent 'X'
   tool transcribe_audio_file", "music job J", "a peer's transcription".
   - This is owner-only; the peer path never reaches it.
7. `_card_held_error`: use `holder.stop_hint` when it exists ("it lets go when idle; Stop X
   unloads it now") instead of "Stop it from the providers settings".
8. `stop_local_engine` (`providers/llamacpp_server_provider.py:2001`) and
   `service.stop_local_engine` (12177): if the alias is a lease holder, call
   `give_up_for("Stop by hand", bound)` when idle, or refuse "busy with <who_uses>". This
   keeps the music card's "Stop X" button working for lease holders.
9. Background calls (rule 10) need no change: with a lease in the way, `_child_is_up()` is
   false and the call is skipped as today.

#### 2.3 Unify or replace `_CARD_CLAIMS`?

**Unify, not replace.**

- `_CARD_CLAIMS` stays exactly as it is: the registry of supervised llama.cpp children,
  which ~11 tests and `conftest.py` poke directly.
- `_CARD_LEASES` (in `card_lease.py`) is a second registry. `_live_holders()` is the one
  union every "who holds the card" reader uses.
- With no lease registered, `_live_holders() == _live_claims()`, so every existing card
  test sees identical behaviour. Pin that with a test.
- A later, separate refactor could make supervisors themselves lease clients (`CardLease`
  per alias). Not needed now. That refactor would touch the 5,555-line file, which other
  agents are editing.

`conftest.py:_no_card_claims_cross_tests` also clears `card_lease._CARD_LEASES`,
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
  - The first one builds providers with `"device": "cpu"` (verified at line 18), and the
    lease is skipped for CPU, so it is unaffected. **[unverified]** that the others also
    use cpu.

### 3. Clients, in order

#### 3.1 Whisper (in-process; holder and contender)

- **Building the lease.** Build `self._card_lease` only when the resolved device is not
  CPU.
  - Explicit `device` in `cuda`/`mps`/`mlx`: use it as is, no torch import.
  - `auto`: resolve once in a thread with the existing `_detect_device`.
  - CPU or no accelerator (CI): no lease, today's path.
  - Holder `whisper:<alias>`, engine `"whisper"`, kind `transcription`,
    `yield_cost="cheap"`.
  - `vram_mib` comes from `model_sizes.model_size_bytes(model, revision)` plus a measured
    CUDA-context figure (the backlog measured ~1.5–1.7 GiB process residency), else None.
- **`transcribe(..., purpose: Optional[str] = None)`.** `purpose` is per call, the same
  precedent as language and task in b9725990.
  - Wrap the **whole** existing body in `async with self._card_lease.use(purpose or
    "transcription", wait_s=..., on_wait=...)`.
  - The lease use is taken **before** `_load_lock`. The lock order is: lease, then
    `_load_lock`, never the reverse. `on_yield` takes `_load_lock`.
  - Doors pass `purpose`: "dictation" (`service.py:4797`), "music Transcribe"
    (`music/commands.py:324`), the agent tool (`tools/transcribe.py:172`, with `on_wait`
    calling `ctx.emit_progress("waiting for the card: <holder> holds it…")`), "peer
    <node>" (`p2p_coordinator.py:1397`, which **must now wrap the call in
    `serving_a_peer(peer_id)`**).
- **Warm means granted with 0 uses.** `on_yield = lambda why:
  self._current_owner().unload_if_idle(f"card_yield: {why}")`.
- **Release on every unload.** `_unload_locked` calls `lease.release(reason)` after its
  worker returns, whichever trigger fired (idle timer, deferred, `release_if_loaded_here`,
  switch_provider, auto_transcribe_disabled, shutdown).
  - If `released is False` (the unload failed), still release, but log that VRAM may not be
    free. The reference is dropped either way.
- **Preload.** `ensure_model_loaded()` calls `lease.acquire("preload", background=True)`.
  On `LeaseSkipped` it logs "Whisper preload skipped: the card is held by X" and stays
  lazy. A preload never swaps anything out.
- **Hand-over.** `hand_over_to(new)` sets `new._card_lease = self._card_lease;
  lease.rebind(new._yield_card)` and never releases. `retire()` with no successor relies on
  the unload path's release.
- **Shutdown.** `shutdown_unload` releases in its finally block.
- **Later (S9, not in the first slice):** CPU fallback on
  `LeaseSkipped`/`LeaseWouldWaitOnItself` when the device is `auto`. It needs a second, CPU
  pipeline, which is part 2 of the Whisper card.

#### 3.2 Music engines (supervised children; holder and contender)

- **`SupervisedEngine.__init__`:** `self.card_lease = CardLease(f"music:{self.KEY}",
  kind="music", engine=self.ENGINE, display=self.DISPLAY,
  vram_mib=self.VRAM_ESTIMATE_MIB, yield_cost="engine", on_yield=self._yield_card,
  waits_for_engines=True)`.
  - Estimates: YuE2 Q8 peaks 5.2–5.6 GB, acestep ~9.3 GB (`engines.py:5-6`).
- **Granted at `start()`**, before the spawn, inside `_the_lock`.
  - The runner path is already granted through `use()`.
  - `start_music_engine` by hand calls `acquire("start by hand", wait_s=0)`, so it is
    refused naming the holder, as `exclusive(wait=False)` refuses today.
- **Released on every end of the child:** in `stop()` (`ui_stop`, `idle_unload`, `swap`,
  `failed_start`), in `_watch` (exited on its own), and in `shutdown()`.
- **Busy:** `in_use()` also enters `card_lease.use(what)`.
- **`_yield_card(why)`:** `if self.busy: return False; return await self.stop("swap")`.
- **Runner.** `MusicJobRunner.run_job` replaces `_wait_for_card(job_id)` with `async with
  engine.card_lease.use(f"music job {job_id}", wait_s=None, check=lambda:
  self._check(job_id), on_wait=self._show_card_wait(job_id))` around `start_job`, `in_use`
  and the production steps.
  - `on_wait(estimate)` calls `set_waiting_for_card(job_id, holders)` with the blocking
    holders, so the existing "waiting for the card: X holds it" text and the Stop X button
    stay.
  - A busy lease holder (Whisper) is waited for. An idle one is yielded (unloaded).
  - A supervisor holder is waited for and never stopped (decision 32).
  - A failed read of the holders is no longer "free": the lease registry is in-process and
    has no read that can fail.
- `MusicEngines.exclusive` stays. Its stops of the other idle music engine release that
  engine's lease, so the two agree. Folding it into the lease is a later cleanup.

#### 3.3 Minimal first slice for the Done-when

"A transcription during a running YuE2 job waits and names the holder" needs S1 (lease
core) + S3 (Whisper) + S4 (music holds leases). S2 (supervisors) is not required for it.

Flow:

1. YuE2 job runs; `music:yue2` is granted and busy.
2. `transcribe` calls `use()`.
3. The Whisper lease is excluded and enters `_LEASE_LINE`.
4. Log line: `card: whisper[whisper-large-v3-turbo] waits for yue2.cpp (busy: music job
   01J…); wait unknown, up to 120 s`.
5. `card_queue()` shows the Whisper row with `wait.behind == ["yue2.cpp (music job …)"]`.
6. The door's `on_wait` names the holder to the person.
7. When the job ends, the engine is idle.
8. The Whisper waiter yields it (`stop("swap")`), then is granted, loads and transcribes.
9. On timeout, `LeaseWaitedTooLong` names "yue2.cpp, busy with music job …".

#### 3.4 Embedder, then GLiNER (S6, S7)

- **Device mode.** New value `lease` for `[memory] embedding_device` and
  `[knowledge_graph] gliner_device`. Proposal: `auto` comes to mean `lease` on CUDA. This
  is a further decision for Mike, outside the five below (no default is proposed beyond
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
  stated here on purpose.

### 4. What the UI and logs show

**Logs, one INFO line per transition, no repeats while polling:**

- `card: <display> granted (<what>)`
- `card: <display> waits for <holder display> (<busy: who_uses | idle, asking it to let go
  | yielding>); wait <N s | unknown>, up to <bound | until cancelled>`
- `card: asked <holder> to let go for <contender>`
- `card: <display> let go (<why>), held <s> s`
- `card: <display> refused: <reason>`
- A swap of a supervisor out for a lease reuses `_note_swap` with its existing words and
  toast.

**`local_engine_status`** (`providers/strata_provider.py:615`): `card_holders` and
`card_queue` carry the lease rows from L7. Lease-related keys added: `kind`, `display`,
`busy`, `uses`, `vram_mib` (None = "unknown size"), `eta_s` (None = "unknown", shown as
"—", never "0 s"), `wait`.

**Holder names in human words:**

- "Whisper (whisper-large-v3-turbo) — dictation"
- "yue2.cpp — music job 01J…"
- "Embedding model (bge-m3) — index rebuild"

**Waiting reasons are visible:**

- music job card: "waiting for the card: Whisper holds it"
- dictation toast or progress: "waiting for the card: yue2.cpp is generating a song (about
  4 min | time unknown)"
- agent tool: `emit_progress`

**Peers** see only "this node's GPU is busy with another job".

The card-queue UI itself is still the open card
THE-CARD-QUEUE-AND-THE-LAST-SWAP-ARE-NOT-SHOWN-ANYWHERE-IN-THE-UI (backlog 1925). S5 adds
a small holder and queue list. **[unverified]** whether the frontend already reads
`card_queue` keys anywhere; the backlog says no.

### 5. Cross-platform behaviour

- **The lease is pure Python, in-process, on all three OSes.** It serialises DPC's own
  clients whether or not any GPU tool exists. A lease never asks nvidia-smi whether it may
  be granted; it asks the registry.
  - macOS: Whisper on MLX or MPS uses unified memory. The lease still serialises music,
    Whisper and the embedder, which matters even more there.
  - AMD and Intel: the same, through the registry.
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
  - **An ignore list is needed:** `dwm.exe`, `csrss.exe`. The backlog (7366) measured dwm
    at **2,709 MiB**, so the Ruach "a foreign process over 2 GiB closes the card" rule
    would trip permanently on Windows without it. The desktop's share is what
    `_DESKTOP_RESERVE_MIB` already covers.
  - **A foreign process over 2048 MiB** becomes a synthetic holder: `engine "foreign"`,
    `alias "pid 23200 python.exe (ComfyUI?)"`, busy, never yieldable. Contenders wait,
    bounded, and are refused naming it.
  - **A failed read is "unreadable", never "free".** Background leases are refused (CPU).
    Foreground leases go ahead with a WARNING naming the failure. **Open for Mike, item
    5:** fail closed for foreground too?
  - **The 90 s claim.** A lease granted but not yet loaded counts its `vram_mib` toward the
    fit arithmetic and the probe's "is it ours" test until 90 s after its load finishes.
    This covers the window in which its VRAM is not yet visible. It is the same number as
    the TTL of the future HTTP lease (S10).
  - Separately, `_free_vram_mib` should return a distinct error for a failed read versus
    no nvidia-smi (new card (b) below).

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
- **Positive:** music no longer reads a failed holder list as a free card: the lease
  registry is in-process and has no read that can fail. Unreadable probe results are never
  "free".
- **Positive:** `_CARD_CLAIMS` keeps its shape, so existing tests see identical behaviour
  (`_live_holders() == _live_claims()` with no lease registered, pinned by a test).
- **Negative:** a **new refusal for peers** while music runs. Today a peer would load
  Whisper beside the music engine.
- **Negative:** with swap off, supervisors now take queue places when only leases block
  them. That path existed only with swap on until now.
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
  do take those locks. Test it: a yield during a waiting acquire must finish.
- **A cycle between a supervisor and a lease.**
  - Invariant L2: a waiter holds nothing, and a granted holder never waits for the card.
    So "llama-server waits for Whisper while Whisper waits for llama-server" cannot
    happen.
  - In v1, Whisper does not wait for supervisors at all. A waiting music lease holds
    nothing.
  - The one way to violate L2 is a request that stands on X (`_CARD_HOLD`) and waits for
    something that waits for X. That is caught at once by `LeaseWouldWaitOnItself` /
    `_this_request_holds`. Examples: an agent turn on Strata whose tool would wait for
    Strata (only once S9 makes Whisper excludable); a tool that holds a Whisper use and
    then calls the LLM inside the same tool.
  - Parallel tool calls in one turn share the same hold, so they are treated as "itself".
    **[unverified]** whether any tool calls the LLM while a transcription use is open.
- **Thread clients.** `acquire_blocking` on the owner loop thread raises. It does not
  block.
- **Yield tasks.** They are kept in a module set and bounded by the contender's
  `stop_bound`. Tests clear them in conftest. The open card
  THE-CARD-QUEUE-TESTS-LEAVE-PENDING-TASKS-THAT-CAN-FAIL-A-LATER-TEST (1550) already shows
  how leaked tasks fail later tests, so assert no pending tasks.
- **Starvation.**
  - The lease line is FIFO. L5 stops new uses of a granted lease while someone waits for
    it.
  - Music behind a **supervisor**: the music job waits for an idle llama-server until a
    person presses Stop X, because supervisors never idle-stop. That is today's behaviour
    under decision 32. Amending decision 32 (swap on: music yields an idle supervisor) is
    **Open for Mike, item 4**.
  - A llama-server start can overtake a waiting music lease, since the queues are separate
    in v1. This is documented; unifying the two lines comes with that amendment.
- **Ping-pong.** Dictation, then llama-server, then dictation: each swap costs an unload or
  reload of Whisper, which is cheap. Llama-server is never stopped for Whisper in v1. A
  safety net like `_MAX_SWAP_ROUNDS` applies to lease contenders too.
- **Background calls.** `background=True` never queues, waits or yields (rule 10 analog).
  The embedder's steady state and the Whisper preload are background. LLM background calls
  keep rule 10 unchanged.
- **P2P peers using our Whisper.**
  - `p2p_coordinator.handle_transcription_request` must wrap the call in `serving_a_peer`
    (it is not wrapped today, line 1397).
  - A peer lease takes no place and does not wait (rule 7). It is granted if the card is
    free or only idle cheap leases stand in its way.
  - Yielding an idle music engine for a peer happens only on the owner's terms (swap on,
    nothing queued, nothing busy, as in `_why_a_peer_may_not_swap`).
  - The refusal is worded for a peer. This is a **new refusal for peers** while music runs;
    today they would load beside it.
- **Hand-over during a reload.** One lease object follows the model, never two. Test that
  `hand_over_to` keeps a single registry entry.
- **`on_yield` that lies.** A client that returns True without freeing memory is caught
  later only by the probe (S8). Whisper's `_unload_model` returning False is logged.
- **Swap off and the queue.** Swap-off supervisors now take places when only leases block
  them. That code path existed only with swap on until now, so it needs its own tests for
  background-call skips (`_CARD_QUEUE` non-empty skips them) and for peer refusals while a
  lease-only place waits.

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

New test files:

1. `test_a_lease_is_granted_once_and_two_acquires_cannot_both_pass.py`: an atomic grant,
   re-entrant `use()`, release on every path, `acquire_blocking` from `to_thread` works,
   from the owner loop it raises.
2. `test_a_busy_lease_is_waited_for_and_an_idle_one_is_asked_to_let_go.py`: a busy holder
   is never asked; an idle one is marked yielding before `on_yield`; `on_yield` False goes
   back to granted and the contender keeps waiting; a deadline refusal names the holder and
   `who_uses`; `wait.wait_s is None` when the ETA is unknown and never 0.
3. `test_no_new_use_jumps_a_waiter.py` (L5 and FIFO): a dictation stream cannot starve a
   waiting music lease.
4. `test_a_transcription_during_a_running_yue2_job_waits_and_names_the_holder.py`: **the
   Done-when**, plus the reverse (a music job yields an idle warm Whisper and waits for a
   busy one, `card_wait.holders` naming Whisper).
5. `test_llama_server_and_strata_wait_for_a_busy_lease_and_unload_an_idle_one.py`: with
   swap on **and** swap off; the swap note and `_LAST_SWAP`; a lease that does not let go
   within `stop_bound` gives a refusal that names it.
6. `test_with_no_lease_registered_the_card_behaves_as_before.py`: `_live_holders() ==
   _live_claims()` and `card_holders()` rows equal the old rows plus `kind`.
7. `test_a_background_lease_never_queues_and_never_yields.py`: the Whisper preload and the
   embedder fake give `LeaseSkipped` while anything holds the card or someone waits.
8. `test_a_peer_lease_takes_no_place_and_is_refused_in_a_peers_words.py`: assert the text
   contains no alias, agent or conversation ids.
9. `test_a_lease_that_would_wait_on_its_own_request_is_refused_at_once.py`: inside
   `card_held_for(alias, "accepted_request")` and the use-carries-hold case. Elapsed < 0.5
   s.
10. `test_whisper_holds_one_lease_across_a_reload_and_lets_go_on_every_unload.py`: idle
    timer, deferred, `release_if_loaded_here`, switch, shutdown; the CPU device makes no
    lease.
11. `test_a_music_engine_lets_go_of_the_card_however_it_stops.py`: idle stop, ui_stop,
    swap, exited on its own (fake `proc.wait` returns), failed start.
12. S8 `test_foreign_card_use_is_named_and_an_unreadable_card_is_never_free.py`:
    - a fake reader returns `{own: 1500, dwm: 2709, foreign: 9000}`: only the foreign pid
      is named;
    - the reader raises: "unreadable" and background refused;
    - the reader returns None: "not measured on this platform".
    - Runs on Windows and Linux CI because the reader is injected.
13. Rewrite
    `test_a_generate_from_text_job_runs_end_to_end_on_the_supervised_engine.py:152,180`.
    Register a fake supervisor claim (`_CARD_CLAIMS[FakeSup] = "claimed"`) instead of
    `Rig(holders=...)`. Keep the assertion "nothing is stopped" and the exact
    `state_reason`.

Compliance checks beyond tests:

- [ ] With no lease registered, every existing card test passes unchanged (full core suite
      before any "green" claim).
- [ ] No lease is acquired while a client lock is held.
- [ ] No wait estimate ever reads 0 when unknown; no refusal text for a peer names an
      alias, agent or chat.

## Scope

- new `dpc-client/core/dpc_client_core/managers/card_lease.py` (lease, line, refusals,
  wait estimate) and, in S8, `managers/card_probe.py`
- `managers/llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_live_holders`,
  `card_holders`, `card_queue`; and, in S2, section 2.2 items 3–8)
- `providers/whisper_provider.py` (designed against its interface; coordinate with the
  agent editing it), `service.py:4797`, `music/commands.py:324`, `tools/transcribe.py:172`,
  `p2p_coordinator.py:1397`
- `music/engine_base.py`, `music/runner.py`, `music/commands.py`,
  `music/acestep.py`/`yue2.py` (VRAM estimates), test helpers
- `providers/llamacpp_server_provider.py` (`stop_local_engine` for leases),
  `service.stop_local_engine`
- `dpc_agent/memory.py`, `managers/agent_manager.py`, `settings.py` (S6);
  `dpc_agent/knowledge_graph.py`, `sleep_pipeline.py` (S7)
- `process_registry.py` (reuse the readers, S8); `local_api.py` (S10)
- `dpc-client/ui` (providers panel) and the status payload (S5)
- `dpc-client/core/tests/conftest.py` (`_no_card_claims_cross_tests` also clears
  `card_lease._CARD_LEASES`, `_LEASE_LINE` and the yield-task set)

## Implementation Status

Nothing is built. Recommended order: S0, then S1, S3, S4 (together these meet the Whisper
card's Done-when), then S2 (Mike's "llama-server and Strata wait"), then S5–S10. S2 could
be pulled ahead of S3 if Mike wants supervisors honouring leases first; it changes the
5,555-line file, so land it after the Whisper editing settles.

| # | Commit | Touches | Proves | Status |
|---|---|---|---|---|
| S0 | docs: this ADR (the design proposed an ADR-044 amendment; this record carries the matrix, L1–L10 and the five decisions instead) | `docs/decisions/052-…md` | — | This commit |
| S1 | feat(card-lease): lease registry, lease line, refusals, wait estimate; `card_holders`/`card_queue` include lease rows through the hook | new `managers/card_lease.py`; `llama_server_supervisor.py` (`_LEASE_HOLDERS`, `_live_holders`, `card_holders`, `card_queue`); `tests/conftest.py` | tests 1–3, 6–8 with fake clients only | Pending |
| S3 | feat(whisper): the local model holds a card lease, yields by unloading when idle, waits for a busy holder | `providers/whisper_provider.py`; `service.py:4797`, `music/commands.py:324`, `tools/transcribe.py:172` (`purpose`, `on_wait`); `p2p_coordinator.py:1397` (`serving_a_peer`) | 9, 10 (lease against lease) | Pending |
| S4 | feat(music): music engines hold the card lease; the runner waits through it and names lease holders | `music/engine_base.py`, `music/runner.py`, `music/commands.py` (start by hand), `music/acestep.py`/`yue2.py`, test helpers | **4 (Done-when)**, 11, 13 | Pending |
| S2 | feat(card): llama-server and Strata wait for a busy lease and unload an idle one, with the swap on or off | `llama_server_supervisor.py` (2.2 items 3–8), `providers/llamacpp_server_provider.py` | 5, and the full list of existing card tests green | Pending |
| S5 | feat(ui): holder and queue list with kinds, waiting reasons, unknown ETA as "—" | `dpc-client/ui` (providers panel), the status payload | manual and UI tests | Pending |
| S6 | feat(memory): the embedder joins the lease (GPU for rebuilds, CPU when skipped), with a real unload | `dpc_agent/memory.py`, `managers/agent_manager.py`, `settings.py` | fake-thread client tests | Pending |
| S7 | feat(graph-ner): GLiNER gains an unload and joins the lease | `dpc_agent/knowledge_graph.py`, `sleep_pipeline.py` | same | Pending |
| S8 | feat(card-probe): foreign GPU use from PDH/NVML, own pids and desktop excluded, unreadable is never free | new `managers/card_probe.py`, `process_registry.py` | 12 | Pending |
| S9 | feat(whisper): share with llama-server when the measured fit allows, else CPU fallback | `card_lease.py`, `whisper_provider.py` | fit and fallback tests | Pending |
| S10 | feat(local-api): external lease with a 90 s TTL and heartbeat | `local_api.py` | HTTP tests | Pending |

### Backlog cards

**Closes or advances**

- `WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-…` (161): part (1), the queue, closes with S1+S3+S4
  (+S2). Parts (2) (GPU choice and CPU fallback, S9) and (3) (per-use models) stay open.
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
- `ONE-CARD-ONE-OWNER-…` (7335): partly (S8, S10).

**Changes**

- `THE-CARD-QUEUE-AND-THE-LAST-SWAP-ARE-NOT-SHOWN-ANYWHERE-IN-THE-UI` (1925): S5.
- `THE-CARD-HOLDER-AND-ITS-STOP-BUTTON-SIT-INSIDE-EACH-STRATA-ALIAS-…` (2078): the holder
  list is now multi-kind.
- `THE-ADMISSION-ARITHMETIC-SIZES-ONE-CHILD-AND-CANNOT-SEE-THE-OTHERS` (7119): S9 uses the
  lease estimates.
- `THE-CARD-QUEUE-TESTS-LEAVE-PENDING-TASKS-…` (1550): the new tests must assert no pending
  tasks.

**New cards to file**

- (a) `music/runner.py:665-667` treats an unreadable holder list as a free card.
- (b) `_free_vram_mib` cannot tell no nvidia-smi from a failed read.
- (c) `EmbeddingProvider.unload` frees no device memory.
- (d) GLiNER has no unload, so it cannot yield.
- (e) A PDH-based foreign threshold needs a desktop ignore list (dwm at 2.7 GiB).

**Documents**

- A SPEC decision 32 amendment, if Mike lets music yield idle supervisors (item 4).
- ADR-044 and ADR-051 are not edited by this record. If accepted, ADR-044 may get a short
  pointer to it.

## Open for Mike

Five decisions, each with the design's proposed default. They must be answered before S2.

1. **The Whisper/supervisor asymmetry in v1.** Supervisors wait for Whisper, but Whisper
   does not wait for supervisors (it loads beside llama-server and Strata, as today).
   *Proposed default:* keep the asymmetry for v1. S9's measured-fit check replaces it later.
2. **Lease yields not gated by `swap_when_idle`.** Unloading Whisper or stopping an idle
   music engine for a contender happens even with the switch off; the switch keeps
   governing only llama-server and Strata. *Proposed default:* not gated.
3. **Per-door default waits (L4).** Owner's dictation: `swap_wait_s` capped by the new
   `[local_transcription] card_wait_s`; agent tool: the turn hold's deadline; peer: 0;
   background: never waits. *Proposed default:* `card_wait_s` = 120, as listed.
4. **SPEC decision 32.** Music never stops an idle supervisor (it waits until a person
   presses Stop X). *Proposed default:* kept as is in v1. The alternative, with swap on,
   lets music yield an idle supervisor, and comes with unifying the two lines.
5. **An unreadable probe (S8).** *Proposed default:* fail closed for background leases
   only (they fall back to CPU); foreground leases go ahead with a WARNING naming the
   failure. The alternative fails closed for foreground leases too.

Also open, and not among the five: whether `auto` means `lease` on CUDA for the embedder
and GLiNER (section 3.4), and ADR-051 N11, which this ADR answers as "(b)+(c)" only once
Mike confirms.

## Open Questions

- **Q1:** Whether any agent tool calls the LLM while a transcription use is open (a
  `LeaseWouldWaitOnItself` risk). **[unverified]** — CC
- **Q2:** Whether the Whisper tests other than the first use the cpu device.
  **[unverified]** — CC
- **Q3:** Whether the PDH counter is vendor-neutral on AMD and Intel. **[unverified]** — CC
- **Q4:** Whether the frontend already reads `card_queue` keys anywhere. **[unverified]**
  — CC
- **Q5:** The prior-art lines in (b) above are from general knowledge and unread here. —
  CC

## Authors

- **Mike** — Decision (#20, 2026-11-21)
- **CC** — Request, review, this record
- **Ark, Zcode** — Consulted

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
- Design source: the card-lease design, `dev` HEAD `b9725990`
