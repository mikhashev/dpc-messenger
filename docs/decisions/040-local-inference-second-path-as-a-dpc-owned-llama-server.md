---
adr: 040
title: "Add the second local inference path as a DPC-owned llama-server child, not an in-process binding — and fix residency before adding an engine"
status: accepted
date: 2026-08-18
deciders: [Mike]
consulted: [Ark, Johnny, Warren, CC, Fable 5, GLM 5.3]
informed: []
depends_on: [ADR-002]
related: [ADR-012, ADR-018, ADR-021, ADR-022]
supersedes: []
session: "two review rounds 2026-08-17/18 — DPC Project #42–#54 and the round-2 re-review; independent reviews by Fable 5 and GLM 5.3"
---

# ADR-040: Add the second local inference path as a DPC-owned llama-server child, not an in-process binding — and fix residency before adding an engine

> **Status note (accepted, 2026-08-18).** Drafted from two independent reviews
> (`ideas/dpc-research/llamacpp-local-provider-review-fable-5.md`, `…-review-glm5.3.md`), the round-1
> prompt and the thread #42–#54; both reviews were read in full and were written without reading each
> other. Every figure is marked `Observed` (measured on this box, location given), `Inferred`, or
> `Not verified`, and the decisions left to Mike are in **Open Questions**, not buried in the text.
>
> **A second review round followed the draft** (`…-round2-fable-5.md`, `…-round2-glm5.3.md`, prompt
> `…-server-provider-prompt-round2.md`), asked to break the decision rather than the premise. The
> engine choice survived; **the order did not**, and three figures in the draft were superseded. Those
> corrections are marked **`Round 2, 2026-08-18`** where they land. The two premises that fell were the
> ones nobody had measured: that `keep_alive` pins a model, and that "sharing is implemented and
> working" meant the shared path was fit for the role the frame gives it.

## Context and Problem Statement

DPC Messenger runs local inference through Ollama only. Backlog entry `LLAMACPP-LOCAL-PROVIDER`
(HIGH, 2026-07-06) proposes a second local path — a provider type using **in-process
`llama-cpp-python`** — on the argument that Ollama does not expose two levers: micro-scaling 4-bit
weights (NVFP4/MXFP4) and KV-cache quantisation, and that brainbake's in-process backend (its
inference-backend ADR, `C:\Users\mikha\Documents\brainbake\docs\decisions\014-inference-backend.md`)
"solved KV cache, weights and load/unload" and can be ported. Mike's framing (2026-08-17): the main
production model is **Qwen3.8-27B, multimodal**, rotated from a list of **NVFP4** checkpoints; agents
need **262 144 tokens of context as a floor**.

Two independent adversarial reviews (Fable 5, GLM 5.3) and a five-voice thread measured the premises on
this machine (RTX PRO 4500 Blackwell, 32 623 MiB; Ollama 0.32.14; one process, nine agents) and found:

1. **The main model has no sliding-window layers** — `n_swa = 0` in the loader log and no
   `sliding_window` key in the GGUF (both reviews, two instruments, `Observed`). Brainbake's ~9× KV
   lever (`swa_full=false` on Gemma-4) is **zero** for qwen3.8, and `llama-server` already defaults
   `swa_full=false`; the 9× corrected a C-API default that only in-process bindings inherit
   (`common/common.h:562` vs `llama-context.cpp:3546`, `Observed`).
2. **The KV cache accepts nine types** (`f32 f16 bf16 q8_0 q4_0 q4_1 iq4_nl q5_0 q5_1`, `--help`,
   `Observed`); **MXFP4/NVFP4 are weight types**, absent from `ggml-cuda/cpy.cu` and `fattn.cu`. The
   "28 `KvCacheType` variants including MXFP4" premise that conditionally retired
   `OLLAMA-CUSTOM-BUILD-NVFP4` is false as a KV lever.
3. **NVFP4 artefacts are larger than the Q4_K_M we run**, not smaller: the vault's
   `nvidia--Qwen3.6-27B-NVFP4` is 20.42 GiB `MIXED_PRECISION`, `unsloth/Qwen3.8-27B-NVFP4` 21.81 GiB
   (vLLM-only), against the Ollama blob's 15.65 GiB (`Observed`). Community NVFP4-GGUF re-quantisations
   save 0.2–2.1 GiB depending on tier — a saving `IQ4_XS` (14.63 GiB) also delivers with no new engine.
   Metal has no NVFP4 kernels at llama.cpp `4df29be` (`Observed`), so NVFP4 cannot be a fleet format.
4. **In-process `llama-cpp-python` has no CUDA wheel** as of 0.3.35 (released 2026-08-17): sdist plus
   CPU-only wheels; the CUDA index stops at 0.3.4/cu124 (`Observed`). Route (a) is a source build into
   the live core venv — the exact class of operation that caused the four-day
   `ACTIVE-RECALL-TORCH-METADATA-EMPTY` outage — and it loses `draft-mtp` and crash isolation.
5. **What agents actually wait on is re-prefill after cache loss, not VRAM.** One day of the Ollama
   log: 233 requests, 3 235 s of prompt-eval, of which 45 prefills >10K tokens took 2 734 s (84 %);
   23 of those 45 were the first three requests after a `llama-server` relaunch (26 launches/day —
   5-minute keep-alive expiry, plus `qwen3-vl:8b` loads for image pre-analysis via
   `vision_provider=ollama_vision`); a single 164 494-token prefill took 293 s (`Observed`,
   Fable 5 §1.5). The 27B itself loads in 4.0–4.8 s.
6. **The daemon's residency arithmetic is 10 GiB short.** `ollama ps` reports 18 GB for a load whose
   own buffers sum to 27.1 GiB; on that estimate the scheduler co-schedules `qwen3-vl` "alongside" a
   card with ~0.4 GiB free (`Observed`, Fable 5 §1.4). Per-process WDDM counters partition the card:
   `llama-server` 28.1–28.7 GiB, the DPC backend (`bge-m3` FP16 + CUDA context) **1.58–1.70 GiB**
   (two independent measurements), desktop ≈2 GiB (`Observed`, both reviews). Mike's "3–4 GB" for
   `bge-m3` is retired.
   **`Round 2, 2026-08-18`: the backend figure is not stable and is not one tenant.** Re-measured at
   **2 788 MiB** — the process holds `bge-m3`, a GLiNER singleton pinned to cuda
   (`knowledge_graph.py:148`) and the sleep pass. So 0d banks what `bge-m3` itself costs, not the whole
   1.6–2.8 GiB, and the spread between the two measurements is a second tenant appearing, not noise.
   Whoever reads the 0d falsifier must expect a drop smaller than the process total.
7. **The shipped `llama-server.exe` (Ollama's build) already does what the provider needs**, exercised
   standalone, CPU-only, on the exact production blob + projector: OpenAI `/v1/chat/completions` with
   native `tool_calls`, `modalities.vision = true`, `chat_template_caps.supports_reasoning_effort`,
   per-request `enable_thinking`, `--spec-type draft-mtp` accepted, `usage` + `timings`, `/slots`,
   `/metrics`, `/props` (`Observed`, both reviews). Its router mode is compiled out
   (`subprocess is not enabled on this build`).
8. **This box is one of three nodes** (`ROADMAP.md:82`: Windows, Linux, macOS); the Linux node has no
   GPU (`backlog.md`, `AN-AGENT-CANNOT-BE-GIVEN-ITS-OWN-EYES`), macOS has no CUDA. A local provider is a
   three-OS product feature, and "the binary is already on disk" is true only where Ollama installed
   it.

The question this ADR answers: **what is the second local inference path — an in-process binding, our
own llama-server, vLLM, or ExLlama — in what order relative to the levers that need no engine, and who
owns residency on one card shared by nine agents and remote peers.**

## Decision Drivers

- **262 144-token context is a floor** for agents (Mike, 2026-08-17); no lever may shrink it.
- **Rivalrous compute under transparent, opt-in governance** is a stated value (`VISION.md` C8, C10,
  "Compute Commons"): allocation must be governable by DPC, visible, and never silent — today a
  peer's `describe_image` can page the resident 27B out without anyone seeing it.
- **One process, many agents**: the DPC backend already holds torch-CUDA (cu128), Whisper and
  `bge-m3`; a crash in an in-process engine takes every agent, every P2P connection and the API server
  with it.
- **Parity the agent loop needs**: native tool calling (`generate_with_tools`), vision (`mtmd`),
  streaming, thinking on/off + the accepted effort ordinal, MTP/speculative parity with today's
  Ollama launch (`draft-mtp`, mean accepted length 2.73, `Observed`), and per-call usage.
- **The live venv is a hard constraint**: no native CUDA build is installed into
  `dpc-client/core/.venv` (backlog `ACTIVE-RECALL-TORCH-METADATA-EMPTY`; ADR-012's pinned-index
  approach is the durable answer if a wheel ever has to be owned).
- **Three OSes, heterogeneous hardware**: distribution and install cost per OS is a selection
  criterion, not a caveat.
- **Resilience**: DeepSeek is the only paid provider and its runway is measured in days
  (Warren, estimate, re-measure pending); a second *working* local path with tools has value only if it
  carries tools.

## Decision

**Do not build the in-process `llama-cpp-python` provider. Add the second local path as a
DPC-owned `llama-server` child process behind a new provider type, and only after four
configuration levers that remove today's measured wall without any engine.**

In order:

### D1 — Stage 0: one atomic configuration bundle (no engine, ~30 lines)

Applied as **one commit** and read from the log afterwards (Johnny #45: applied singly, the first
lever's falsifier gives a false negative):

- **0a** — `keep_alive` per Ollama alias, sent on the plain and tools paths as it already is on the
  vision path (`ollama_provider.py:489`); the 27B alias set to `-1`.
  **`Round 2, 2026-08-18`: `-1` is not a pin, and the word "pinned" is withdrawn.** Ollama v0.32.14
  `server/sched.go`: `findRunnerToUnload` sorts runners by `uint64(sessionDuration)` — `-1` merely sorts
  last — and then returns the first idle runner; the pending path sets `sessionDuration = 0` and expires
  it (`Observed`, Fable 5 read the sources). So `keep_alive` protects against the **TTL** and against
  nothing else: under memory pressure the "pinned" 27B is still evicted. That answers Q3, and it demotes
  0a: it stops the five-minute unload, and what protects residency is removing the second tenant (0b) —
  not the flag.
- **0b** — `vision_provider` on this box → `qwen3.8:latest` (already vision-capable and resident);
  and the durable form, because `_ensure_config_exists` (`:171`) seeds `ollama_vision` into every fresh
  `providers.json` and a small separate vision model remains the right default for a node without a 27B.
  **`Round 2 answers, 2026-08-18`: the durable form belongs to the caller, not to `query`.** The draft
  put it in `llm_manager.query` auto-selection (`:591-593`), and two facts refuse that: `query(prompt,
  provider_alias=None, return_metadata, images, **kwargs)` carries **no caller identity** — every call
  site passes `None` (`llm_adapter.py:846`, sleep pipeline, tools) — and **resident-detection does not
  exist** anywhere in core (`/api/ps` is called nowhere) (`Observed`, Johnny round 2, re-read by CC).
  Making `query` prefer "the requesting agent's own provider when resident" therefore means adding an
  identity parameter to a contract every caller uses, plus a residency probe that has no
  implementation — not the ~30 lines the draft assumed. **The vision pre-analysis call site passes the
  requesting agent's own `provider_alias` instead of `None`;** `query` and its other callers are
  untouched. The config change above is what works on this box today; the durable form is for a node
  that has no 27B.
- **0d** — `bge-m3` off the GPU: an `embedding_device` field on `MemoryConfig`
  (`memory_config.py:14-30`, none today) passed to `get_embedding_provider(device=…)` at its call
  sites. **Errata 2026-08-18: six, not five.** The count in the draft came from an earlier reading; a
  repo-wide enumeration while implementing it found `managers/agent_manager.py:364` as well — the eager
  index pass, the heaviest of the six. And the field could not live only there: the agent builds the
  per-process singleton in its own constructor (`agent.py:164`), which runs before the manager reads
  `MemoryConfig` at all, so the device travels on `AgentConfig` too or it applies to nothing on the live
  path. Shipped in `bbdcf877`. Banks ~1.6–1.7 GiB; CPU cost measured at 55 ms per sentence, 537 ms per ~800-token chunk
  (`Observed`, Fable 5 §1.10). Also expected to give Step-3 the headroom a second slot needs.
- **0e** — the host prompt cache above its 8 192 MiB default, via `LLAMA_ARG_CACHE_RAM` in the Ollama
  service environment. **`Round 2, 2026-08-18`: no longer a hypothesis, and it is the lever of the
  bundle.** `llm/llama_server.go:446` sets `cmd.Env = os.Environ()` and the launch line carries no
  `--cache-ram`, so the variable reaches the child; the binary's `--help` names it (`Observed`).
  Measured today: cache 8 192 MiB, entries 1.8–7.2 GiB (median 5.2), **36 evictions in 26 h**. With 0a
  demoted, this is what decides whether the card can carry the fleet on a given day — zero code, one
  environment variable, and it is Mike's action on the box.
  **Size: start at 24 GiB, not 40.** System RAM is 61.7 GiB with ~51 free (`Observed`, round 1); 40 GiB
  is 65 % of the machine and leaves ~11 GiB for the OS, the browser and `bge-m3` once 0d moves it to
  CPU. Fable's own round-1 figure — «≥ 24 GiB holds five agents' entries» — is the right first step
  (Johnny, round 2); 40 GiB belongs to the day the fleet actually migrates.
  **Value format (`Round 2 answers, 2026-08-18`), read from the shipped binary's own `--help` rather
  than assumed:** `-cram, --cache-ram N — set the maximum cache size in MiB (default: 8192, -1 - no
  limit, 0 - disable) (env: LLAMA_ARG_CACHE_RAM)`. The value is a **plain integer in MiB**, so 24 GiB
  is `LLAMA_ARG_CACHE_RAM=24576` — not `24GiB`, not `24G` (`Observed`). This mattered because a wrong
  format shows the default 8 GiB in the "prompt cache … size limit" line and would be misread as "the
  variable never reached the child", which Q7 has already answered. One neighbouring flag is worth
  recording with it: `--cache-idle-slots` is **on by default and requires `cache-ram`**, so raising the
  ceiling also gives idle-slot saving somewhere to work for the first time.

Falsifier for the bundle: if request-#1-to-#3-after-launch prefills persist at the same rate after
the bundle, the residual cost is cross-agent cache eviction and the cache size (0e, or per-alias
slots under D3) is the lever — not the TTL.

**What Stage 0 does not fix, said here so the bundle is not read as the cure.** The measured root of
the residency wall is the daemon's own accounting: Ollama budgets a model's footprint about **10 GiB
below the truth** and co-schedules a second model on that estimate with ~0.4 GiB actually free
(Fable 5 §1.4, `Observed`). The bundle removes the *source of pressure* — 0b takes the second model
out of the picture on this box — and leaves the estimate exactly as wrong as it was. Any future load
that Ollama decides to co-schedule meets the same arithmetic. The permanent fix is **D4** (a
single-resident governor in DPC that owns what is loaded), not Stage 0.

> **`Round 2, 2026-08-18` — the order changed, and this is the round's main finding.** Stage 0 no
> longer goes first. **D4-0 and the telemetry line go before it**, for two reasons the draft could not
> see: the shared path routes a peer onto the paid provider (D4-0 below), and the bundle's falsifier as
> written is a hand-run `awk` over `server.log` — a falsifier nobody will run twice is not a falsifier.
> The honest size of the first package is therefore **D4-0 + telemetry + the bundle ≈ 200–300 lines
> plus a restart**, not "~30 lines"; it is still cheap and still right, but it is a small project rather
> than a one-liner, and it should be chosen as one.

### D2 — Stage 1: NVFP4 is measured through Ollama itself, not built for (0 code)

Ollama's shipped `ggml-base.dll` registers the `nvfp4` type name (`Observed`), so `ollama create` from
a community NVFP4-GGUF (`esatapedico/…-MEDIUM` 15.25 GiB or `williamliao/…-Quality-v2` 14.96 GiB, plus
the projector) is a one-Modelfile test. Compared at 262 144 context against Q4_K_M **and IQ4_XS**
(14.63 GiB): loader model-buffer MiB, `predicted_per_second` at ≥60K depth, and a quality probe. This
closes `OLLAMA-CUSTOM-BUILD-NVFP4` either way and settles the FP4 premise on the production model
before any provider code exists. **NVFP4 is not the driver of this task**; the target artefact is
Qwen3.8-27B as a GGUF chosen per node, not a format.

### D3 — Stage 2: route (b) — a DPC-owned `llama-server` behind `llamacpp_server`

- **Engine binary, two forms with different roles.** *(b1)* Ollama's private `llama-server.exe` is the
  measurement rig on this box (zero download, the exact production blob and flags) — and if the
  DeepSeek runway makes Stage 2 a race, the first working provider may run on it as a bridge.
  *(b2)* the **shipped mechanism** is a **pinned mainline llama.cpp release per OS**, fetched on
  first use by tag + sha256 into `~/.dpc/bin/llama.cpp/<tag>/<os-arch>/` the way Whisper models are
  fetched today (`voice_service.download_whisper_model`, `ModelDownloadPanel.svelte`): release
  `b10472` (2026-08-17) carries `win-cuda-13.3-x64` (139.9 MiB + cudart 372.9 MiB), `macos-arm64`
  (Metal, 10.6 MiB), `ubuntu-x64` CPU (15.9 MiB) and Vulkan; no Linux-CUDA asset (`Observed`). We own
  three *downloads* of upstream builds, not three builds; a build only if a needed flag or patch is
  missing upstream (none identified). Never hard-code Ollama's install layout: `binary_path` in
  configuration with auto-discovery as a fallback.
- **Model source for (b2): HF GGUFs** (`unsloth/…`, `ggml-org/…` with `mmproj` and, on mainline, a
  separate `mtp-*.gguf`); whether the mainline loader takes Ollama's blob (in-blob MTP, 866 tensors) is
  `Not verified` and is not to be counted on. On this box, (b1) uses the blob directly.
- **Provider** `providers/llamacpp_server_provider.py`, a 13th `PROVIDER_MAP` type, **derived from
  `deepseek_provider.py`** (the OpenAI-compatible sibling that already has `generate_with_tools`,
  `generate_response_stream`, usage, retry, effort mapping) — not from the bare `openai_compatible`
  type, which requires an API key, has a hard-coded vision list and neither tools nor streaming.
  Capabilities come from `/props` (`modalities`, `chat_template_caps`), not a name list. Thinking:
  per-request `chat_template_kwargs.enable_thinking` and `reasoning_effort` mapped from the accepted
  ordinal `off/low/medium/high/max`, with the room-may-spend-less-never-more precedence Ollama already
  enforces; `--reasoning-budget N` per alias as the instrument for the empty-answer record (flag
  accepted `Observed`, effect `Not verified`). Precondition: the usage contract moves onto
  `providers/base.py` — a convergence of three existing shapes rather than a lift from one.
  DeepSeek keeps a stateful accessor (`get_last_usage`, `deepseek_provider.py:116`); Ollama and
  `zai_coding` each build a `usage` dict, return it inline from the tools path and log it, and
  expose no accessor at all. The single reader, `llm_adapter.py:283` inside `DpcLlmAdapter.chat`,
  reaches for it through `hasattr`, so today only DeepSeek is priced by what it reported and every
  other provider is priced by an estimate the loop computes for itself — filed as
  `THE-COST-A-PROVIDER-REPORTS-IS-READ-FROM-ONE-PROVIDER-AND-GUESSED-FOR-THE-REST`. A fourth
  private copy is therefore not the risk; a second unread one is.
- **Supervisor** `managers/llama_server_supervisor.py`: spawn with per-alias flags (`-c`, `-ctk/-ctv`,
  `--mmproj`, `--spec-type draft-mtp`, `-np`/`--kv-unified`, `--reasoning-*`, `--cache-ram`,
  `-fit off`), `/health` wait, drain, stop; `/slots` and `/metrics` read through to the UI.
- **UI:** `ProviderType` union + add-form defaults + render branches in `ProvidersEditor.svelte`
  (4–6 places; the union is already one type behind the backend — `remote_peer`).
- **Volume:** provider ~350–450 lines, supervisor ~200–300, binary fetcher ~120–180, base.py contract
  ~30, UI ~40, tests ~400 → **~1 300–1 500 lines across ~10 files**, no native build, no venv change.

### D4 — Residency and arbitration live in DPC, not in the engine

**`Round 2, 2026-08-18`: D4 splits, and its first half moves to the front of the whole plan.**

#### D4-0 — before Stage 0. No engine, ~60–100 lines

The shared path is not a future concern; it is wired, gated, and mis-routed today. Measured on this box:
it has carried **two** requests ever (`dpc-client.log.4` 2026-08-07 02:18, `.3` 2026-08-10 12:05, same
peer), and **both were relayed to `deepseek_flash`** — the paid provider (`Observed`, both reviewers,
confirmed independently by Ark and by CC on the logs). Four defects, each closed by a few lines:

- **The gate and the router speak different vocabularies.** `handle_inference_request` calls
  `firewall.can_request_inference(peer_id, model)` (`p2p_coordinator.py:172`) — the firewall is handed
  `model` and **never `provider`** — and four lines later routes on `provider_alias_to_use = provider`
  (`:187`, `:196`), the alias the *peer* named. A peer that sends `provider` and omits `model` passes a
  check that examined nothing. Filling `compute.allowed_models` does not close it: with `model=None`
  that branch never runs. (An empty `allowed_models` meaning *all models* is intended and the UI says
  so — that is not the defect.) With both fields omitted the alias falls to `default_provider`, which
  on this box is paid.
  **`Round 2 answers, 2026-08-18`: the draft's fix — "the shared alias is never `default_provider`" —
  is withdrawn; it is a fix that looks like a fix.** It closes only the both-fields-empty fallback.
  `providers.json` on this box holds 16 aliases, **two** of them DeepSeek, and `default_provider` is
  `deepseek_flash` (`Observed`); a peer naming `deepseek_pro` is not the default and passes (Johnny,
  round 2). **Fix as decided (Mike, 2026-08-18): the peer does not name the alias at all.** The host
  designates what it serves — a single `compute.serving_alias` in `privacy_rules.json`; the request's
  `provider` field is ignored, and when `serving_alias` is unset the request is refused with a stated
  reason rather than served by whatever the router would have picked. `model` still travels and the
  existing `allowed_models` gate is unchanged. A list (`compute.allowed_providers`) was considered and
  rejected: `allowed_models` already means *all* when empty and the UI declares it, so a second list
  with the opposite empty-semantics is a trap that re-opens this same hole the first time someone
  leaves it blank. One named alias has one reading. This is also what D4 means by governable — the
  host allocates, not the caller.
- **A peer's request is billed to nobody.** It belongs to no agent, so it writes no row in
  `events.jsonl` and appears in no cost series. One log line in `handle_inference_request` closes the
  record.
- **A late response is dropped in silence.** `inference_handler.py:73-93` — `if request_id in
  _pending_inference_requests:` with **no `else`**. The requester has already given up; the host has
  already paid.
- **The three remote ceilings are all below the host's own budget.** 240 s (UI door, not reachable from
  configuration), 60 s (`remote_peer`), 180 s (`dpc_agent`) against `DEFAULT_TIMEOUT_SECONDS = 900.0`
  (`ollama_provider.py:69`). Anything that runs longer than the requester's ceiling and shorter than the
  host's is abandoned mid-flight while the host keeps generating for nobody — the wire edition of
  `A-TIMED-OUT-VISION-CALL-KEEPS-GENERATING-AND-THE-NEXT-ONE-PAYS-FOR-IT`. **Fix: 1200 s on all three
  doors** — the host's budget plus overhead (Mike, 2026-08-18: «900 +300… с учётом того как долго может
  работать vision»), and the UI door gains a configurable value at the same time.

A semaphore on the shared alias belongs here too. Everything else — the full queue with priorities and
a remote-share cap — is **D4-β** and stays in Stage 2.

Two more facts about the shared path, recorded because they bound what D4-0 can promise. The
`remote_peer` provider type is **broken as a type**: it reports `supports_vision() → True`
(`remote_peer_provider.py:110-112`) with no `generate_with_vision`, and returns a dict from a method
annotated `-> str`; there are no aliases of it and no tests. And the sharing path has **no streaming**
(`REMOTE_INFERENCE.md`), so a remote consumer waits on a spinner where a local caller on route (b)
would stream. Today the path therefore works for the UI compute-host door and not for an agent.

#### D4-β — Stage 2, with the engine

- **One resident large model at a time** on this class of card, declared per alias
  (`resident: pinned | on-demand`, a co-residency budget); a request for a second ≥15 GiB model queues
  behind a policy decision and never silently pages the first out.
- **The eviction unit for the agents' model is the cache entry, not the model.** `load → infer →
  unload` per turn is refused for the agent-loop model: weights reload in 4–5 s but a cold cache costs
  60–100 s at 60K tokens and ~5 min at 160K (`Observed` prefill rates). Rotation of a *cold* model into
  a slot is legitimate only for batch jobs (sleep synthesis) whose cache is not needed again.
- **The queue is DPC's**, keyed by identity (local agent id / peer node id) with priorities
  (human-facing turn > agent tool loop > sleep > remote peer), a remote-share cap, and the rule that a
  remote request never triggers a model swap. `dpc_agent/budget.py`'s `ProviderLimits`
  (`max_concurrent`, per-minute/day) is the existing primitive, applied to local aliases for the first
  time; the engine's slots (`-np`, `--kv-unified`) are the enforcement mechanism, `/slots` and
  `/metrics` the visibility `VISION.md` C10 asks for. This is what makes allocation governable rather
  than accidental (§Q0 of both reviews) — and it applies whichever engine serves the request.

### D4-T — Telemetry ships with the first package, not after it (`Round 2, 2026-08-18`)

Every lever in this document is justified by a number, and after the bundle lands nothing records
tokens/s at depth, queue wait, swap counts or VRAM headroom per alias. The bundle's falsifier as drafted
is a hand-run `awk` over `server.log`, and a falsifier that needs a person will not run twice. A
collector of ~100 lines on the existing `_log_usage` line closes it; the Ollama SDK's `ChatResponse`
already carries `load_duration`, `prompt_eval_duration` and `eval_duration` and DPC reads none of them
(`Observed`), so the cheapest first series costs one line. A per-agent cost series already exists
(`~/.dpc/agents/<id>/logs/events.jsonl`, tariffed by `dpc_agent/pricing.py`) — the gap is the four
series above, and the peer requests that belong to no agent (D4-0). Filed as
`THE-LEVERS-WE-ARE-ABOUT-TO-PULL-CANNOT-BE-TOLD-APART-IN-PRODUCTION`.

**`Round 2 answers, 2026-08-18`: what D4-T actually ships, because "~100 lines" bought one row of four.**
Of the four series named above, exactly one is reachable from what DPC already receives: `load_duration`,
`prompt_eval_duration` and `eval_duration` on `ChatResponse` → **tokens/s at depth**, one line added to
`_log_usage` (`ollama_provider.py:364-383`). The other three have no source in the process: **queue wait**
does not exist until there is a queue (D4-β); **swap counts and evictions** live in Ollama's `server.log`,
which DPC does not read anywhere — 0 matches for `server.log` across core (`Observed`, Johnny round 2);
**VRAM headroom** lives in the WDDM per-process counters, outside the SDK entirely. So **D4-T = the
tokens/s row plus the peer-request accounting line from D4-0**, and nothing else is promised here. A
`server.log` reader (who reads, what it greps, where it writes — AP-11) is its own entry with its own
size; without it the bundle's falsifier stays a hand-run `awk`, which is exactly the deficiency named
above and is hereby carried forward rather than quietly declared closed.

### D5 — Fleet: the provider is a per-node capability, not a fleet uniform

On a node whose GPU cannot hold an alias's model, the alias reports itself unavailable (and the UI
refuses it as an agent's main model) rather than falling to CPU — a CPU 27B prefills at ~40 tok/s
(`Observed`), i.e. 25 minutes for a 60K agent context. The GPU-less Linux node is a **consumer** of
the compute commons through the existing sharing path (`p2p_coordinator.py:166-196`, text and
images), which is exactly what that path is for; macOS runs the Metal build with models its unified
memory holds (`Not verified` for the 27B); the format common to all three nodes is Q4_K_M/IQ4_XS-class
GGUF, so configuration names a **GGUF path per node**.

### D6 — Refused

- **In-process `llama-cpp-python`** as the provider: no CUDA wheel (build into the venv, or we own a
  wheel), no MTP, no crash isolation, a second CUDA runtime beside torch's cu128 DLLs, a process-wide
  lock around a non-thread-safe object called from a `ThreadPoolExecutor` — and nothing on the lever
  list that the server does not already expose. Reopened only by a maintained sm_120 wheel *and* a
  feature that needs per-token Python control.
- **vLLM/SGLang**: Linux-only (WSL2 on Windows), pre-reserves the card, owns residency on its own
  terms, would carry the *larger* NVFP4 artefacts; usable on 0 of 3 nodes.
- **ExLlamaV3 as a provider**: a matching wheel exists for this venv (`cu128 · torch2.11 · cp312 ·
  win_amd64`) and it supports the Qwen3.5 architecture with vision, but it is EXL3 (a third artefact),
  in-process torch (route (a)'s safety gate), CUDA-only (1 of 3 OSes). Kept as a quality-per-GiB
  *experiment* in a throw-away venv (turboderp 3.5–4.5 bpw vs Q4_K_M/IQ4_XS/NVFP4-GGUF at 262K), not a
  route.
- **An NVFP4 conversion pipeline** (no lossless import path exists: `convert_hf_to_gguf.py` has no
  NVFP4/ModelOpt support and `llama-quantize` has no NVFP4 target, `Observed`), **per-turn
  load/unload for the agent model**, **more than one large engine child at a time**, and **any native
  build into the live core venv**.

### Rationale

- **The premises were inherited, not measured, and three of four fell on this box** (Context 1–4);
  the fourth (FP4 memory) narrowed to a rounding error a standard quant also delivers. A provider
  justified by those premises would have been built for levers it cannot pull.
- **The wall agents feel is time, and its cause is residency policy** (Context 5–6): a 5-minute TTL
  and a per-process prompt cache on a model whose *cache* is worth minutes and whose *weights* reload
  in seconds, plus a daemon that co-schedules a second model on a wrong estimate. Both are configuration
  and policy, not engine capability — which is why Stage 0 precedes everything and is atomic.
- **Route (b) is the only one of four that satisfies the governance clause and the parity list at
  once**: DPC owns the child (policy point = blast-radius boundary), the engine exposes per-alias
  flags, `/slots`/`/metrics` make allocation visible, and tools/vision/MTP/usage are `Observed` on the
  exact production blob. Brainbake's requirement — orchestrator-owned residency — is adopted whole;
  its *means* (in-process FFI) was the answer for a Rust core whose alternative was an opaque daemon.
  DPC's alternative is a controlled child.
- **(b2) over (b1) as the shipped mechanism** because Ollama's private binary is an internal of an
  auto-updating installer whose layout on Linux/macOS is unverified and whose router mode is compiled
  out; upstream publishes the three OS builds we need, so ownership costs three downloads, not the
  six-condition Windows/CUDA build recipe brainbake accepted.
- **Derive from DeepSeek's provider, not `openai_compatible`**: the sibling with tools, streaming,
  usage and effort already exists next door; the bare type would reproduce the "control that renders
  and does nothing" defect this project spent a month on.

## Considered Options

- **Option 0 — status quo (Ollama only, env-level levers).** Zero work; residency accidental,
  per-alias engine flags impossible, second-tenant paging invisible.
- **Option A — in-process `llama-cpp-python`** (the backlog entry as filed).
- **Option B1 — spawn Ollama's private `llama-server`** behind a provider.
- **Option B2 — DPC-owned `llama-server` from pinned mainline releases per OS** behind a provider
  (chosen; B1 as rig/bridge).
- **Option C — vLLM / SGLang.**
- **Option D — ExLlamaV3.**

### Pros and Cons of the Options

#### Option 0 — status quo
- Good: nothing to build; capability discovery via `/api/show` just rebuilt.
- Bad: residency owned by a daemon whose footprint estimate is 10 GiB short (`Observed`); TTL unload
  costs ~26 min/day of re-prefill (`Observed`); no per-alias KV/slots/budget; no streaming on the
  local path; governance only at the firewall boolean.

#### Option A — in-process binding
- Good: full Python-level control; the vendored llama.cpp (0.3.35 → `4df29be`) knows `qwen35` and has
  an `MTMDChatHandler` (`Observed`).
- Bad: no CUDA wheel (`Observed`) → source build into the venv or an owned wheel; no MTP/draft
  (`LlamaPromptLookupDecoding` only, `Observed`); crash kills the whole backend; two CUDA runtimes and
  two allocators in one process; GIL/thread-safety around a shared object; worst exactly on the
  Windows node.

#### Option B1 — Ollama's private binary
- Good: on disk, proven on the production blob with every flag (`Observed`); zero download.
- Bad: couples DPC to an unversioned, auto-updated private layout on three OSes (`Not verified`
  beyond Windows); router mode compiled out; Ollama chooses per model between its Go engine and
  llama-server.

#### Option B2 — pinned mainline release per OS (chosen)
- Good: native on all three OSes from upstream builds (`Observed` b10472 assets); flags and version
  under DPC's control; same provider code as B1; MTP via separate `mtp-*.gguf` on ggml-org's repo.
- Neutral: HF GGUF download per GPU node (~16 GiB + 0.87 GiB projector); a fetcher (~150 lines).
- Bad: `sm_120` in the win-cuda-13.3 artefact `Not verified` (Ollama's cuda_v13 DLL proves the
  toolchain emits `sm_120a` PTX, `Observed`); no Linux-CUDA asset if a Linux GPU node ever appears.

#### Option C — vLLM / SGLang
- Good: the only native reader of NVFP4 safetensors; throughput serving.
- Bad: Linux-only (WSL2 on Windows, `Observed` docs); own scheduler = foreign arbitration; reserves
  the card; the NVFP4 artefacts it would carry are 4.8 GiB *larger* than Q4_K_M; 0 of 3 nodes.

#### Option D — ExLlamaV3
- Good: prebuilt wheel exactly matching the venv; Qwen3.5 arch with vision; EXL3 quality per bpw.
- Bad: EXL3 only; in-process torch (route A's safety gate); CUDA-only → Windows-only in this fleet;
  `sm_120` kernels `Not verified`.

## Consequences

- **Positive:** the measured wall (re-prefill after cache loss; second-tenant paging) is removed by
  Stage 0 in any world; NVFP4 is settled by one Modelfile; the second local path, when built, carries
  tools, vision, streaming, usage, per-alias KV/slots and a reasoning budget; residency becomes a
  DPC policy with identity and visibility; the provider works on three OSes from upstream binaries.
- **Negative / accepted:** DPC learns an install layout for the first time (its own, under
  `~/.dpc/bin`); a GGUF download per GPU node; a supervisor to maintain; Stage 2's two measurement
  phases are **production windows** on this box (the five qwen3.8 agents have no model while the card
  is used), merged into one window: (27B in) Step-3 → KV cell; (27B out) NVFP4 cell; return.
- **Neutral:** Ollama stays for the long tail of small aliases; `LLAMACPP-LOCAL-PROVIDER` is
  re-titled to *server*, `OLLAMA-CUSTOM-BUILD-NVFP4` closes on Stage 1's result, and the two measured
  production defects (TTL re-prefill; the daemon's footprint estimate) get entries of their own.

## Confirmation

Compliance, not progress — each item is a measurement with a stated failing result:

- [ ] **D4-0 (`Round 2 answers, 2026-08-18`; the draft had no cell for it, Johnny round 2)** — three
      measurements, each with its failing result. **(1)** A peer request naming a provider that is not
      `compute.serving_alias` is **refused — including when it carries no `model`**, which is Johnny's
      exact case (`provider: "deepseek_pro"`, no model, today passes a check that examined nothing); and
      when `serving_alias` is unset, every peer request is refused rather than served by the router's
      pick. A test for this does not exist today (`test_p2p_coordinator.py` covers only deny/allow by
      model) and it must be red before the change (fail: it passes on the unchanged code → the test is
      asserting the old behaviour, not the new rule). **(2)** An allowed peer
      request writes one accounting line naming the peer, the serving alias and the token counts (fail:
      the shared path is still billed to nobody, which is the defect this item exists for). **(3)** A
      response arriving after its `request_id` has been discarded produces a log line rather than
      silence (fail: `inference_handler.py` still returns on the `if` with no `else`). The prod signal
      is deliberately *not* the falsifier: the shared path carried two requests in eleven days, so
      waiting on production here would be waiting on nothing.
- [ ] **D4-T** — after the first agent turn, one line carries tokens/s at depth derived from
      `load_duration`/`prompt_eval_duration`/`eval_duration` (fail: the fields are absent from the row →
      the SDK's durations are not being read, and every later comparison of the levers is unmeasurable).
      The other three series named in D4-T are explicitly **not** part of this cell.
- [ ] **Stage 0 bundle** — after one day: request-#1-to-#3-after-launch prefills >10K tokens are gone
      from the Ollama log (fail: same rate → cross-agent eviction, cache size is the lever);
      `qwen3-vl` launches are zero on this box; `python.exe` dedicated usage by the WDDM counter drops
      by ≈1.6 GiB (fail: attribution wrong); the "prompt cache … size limit" line reflects the
      configured size (fail: env not passed → per-alias cache belongs to Stage 2).
- [ ] **Stage 1 NVFP4 cell** — an NVFP4-GGUF loads through Ollama at 262K (fail: `GGML_ASSERT` on the
      type → the custom-build entry stays open); model buffer within 1 GiB of Q4_K_M and
      `predicted_per_second` within noise at ≥60K depth **kill** the FP4 memory and speed stories.
- [ ] **Step-3 (route b on the card, one production window, after 0d)** — the shipped binary serves
      the 27B at `-c 262144 -np 2 --kv-unified` within the free VRAM (`common_memory_breakdown_print`
      line recorded; **read the second load, not the first** — the cuda_v13 build is PTX-only and JITs
      on first load), tools parsed on the real template, MTP acceptance ≥ Ollama's 0.69/pos, and a
      20-token turn overlapping a 60K prefill returns without waiting for it (fail on any → `-np 1`;
      fail on all → mainline build; fail on both → route (a) is back).
- [ ] **KV cell at 262K** — KV **total ≈ 4 608 MiB** at q4_0 (per side 2 304; a saving of 4 096 MiB
      against the logged 8 704 MiB at q8_0, whose own per-side figure is 4 352); paired
      runs on five real agent transcripts (47–76K tokens) at q8_0 vs q4_0: next-action agreement
      ≥ 90 % and a 20-question recall probe within 10 points (fail → q4_0 dead for the main model).
- [ ] **Provider parity** — `generate_with_tools`, vision via `image_url`, `generate_response_stream`,
      the effort ordinal with the "less, never more" precedence, `get_last_usage` via the base
      contract; a silently ignored `reasoning_effort` fails this item.
- [ ] **Governance** — a remote peer's request is queued behind local human-facing turns and never
      causes a model swap (test: peer request while a local turn holds the slot); allocation state is
      readable from `/slots` in the UI.
- [ ] **Fleet** — on a node without a usable GPU the alias reports unavailable rather than falling to
      CPU; on macOS the Metal binary serves a GGUF the node's memory holds.
- [ ] **Venv discipline** — `uv sync --dry-run` shows no change to `dpc-client/core/.venv` after
      Stage 2 lands.

## Scope

- `dpc-client/core/dpc_client_core/providers/ollama_provider.py` — `keep_alive` on plain and tools
  paths (0a).
- `dpc-client/core/dpc_client_core/llm_manager.py` — auto-selection prefers the agent's own
  vision-capable resident provider (0b); `PROVIDER_MAP` entry (Stage 2).
- `dpc-client/core/dpc_client_core/dpc_agent/memory_config.py`, `dpc_agent/memory.py`, `dpc_agent/agent.py`
  and the six `get_embedding_provider` call sites — `embedding_device` (0d, shipped `bbdcf877`).
- `~/.dpc/providers.json` (this box) — `vision_provider`, `keep_alive` on the 27B alias (0a/0b).
- `dpc-client/core/dpc_client_core/providers/base.py` — usage contract (Stage 2 precondition).
- `dpc-client/core/dpc_client_core/providers/llamacpp_server_provider.py` — new (Stage 2).
- `dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py` — new: spawn/health/drain,
  residency policy, binary fetcher (Stage 2).
- `dpc-client/core/dpc_client_core/p2p_coordinator.py` — the peer's `provider` field is ignored and the
  host's `compute.serving_alias` is used instead, refusing with a stated reason when it is unset;
  provider alias into the firewall call, semaphore, peer-request log line, ceiling 240 → 1200 (D4-0).
- `dpc-client/core/dpc_client_core/firewall.py` — `can_request_inference` takes the provider, and
  `compute.serving_alias` joins the compute block (unset = share nothing, never "share anything") (D4-0).
- `dpc-client/core/dpc_client_core/message_handlers/inference_handler.py` — the missing `else`: a late
  response is logged rather than dropped in silence (D4-0).
- `dpc-client/core/dpc_client_core/providers/remote_peer_provider.py`,
  `providers/dpc_agent_provider.py` — ceilings 60 and 180 → 1200 (D4-0).
- `docs/REMOTE_INFERENCE.md` — the timeout it documents in four places is one of three and none of them
  is 60 any more; the doc is corrected in the same change (D4-0).
- the telemetry line on `_log_usage` — the SDK's three durations, tokens/s at depth (D4-T; the other
  three series need a `server.log` reader, filed separately).
- `dpc-client/core/dpc_client_core/dpc_agent/budget.py` + `llm_manager.query` — `ProviderLimits`
  applied to local aliases (Stage 2 / D4-β).
- `dpc-client/ui/src/lib/components/ProvidersEditor.svelte` — provider type (Stage 2).
- `backlog.md` — done 2026-08-18 (CC, board owner): `LLAMACPP-LOCAL-PROVIDER` re-titled
  `LLAMACPP-SERVER-PROVIDER` and rewritten against these measurements;
  `OLLAMA-CUSTOM-BUILD-NVFP4` amended to close on Stage 1's result either way; four new entries —
  the TTL re-prefill cost, the daemon's footprint estimate, the telemetry harness of Q8, and the
  cost door read through `hasattr`.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| D4-0: provider in the gate, host-designated `compute.serving_alias` (the peer's `provider` ignored), peer-request log line, orphan-drop log, three ceilings → 1200, semaphore | Pending — first, before the bundle | — |
| D4-T: the tokens/s row on `_log_usage` (one line; the other three series are not in scope) | Pending — with D4-0, not after | — |
| Stage 0 bundle 0a+0b+0d, one commit; **0e** is Mike's environment variable on the box (24 GiB) | Pending — after D4-0 + D4-T | — |
| Stage 1 NVFP4-GGUF via `ollama create`, measured vs Q4_K_M/IQ4_XS at 262K | Pending — production window (27B out) | — |
| Step-3: route (b) on the card at 262K, 2 unified slots (after 0d) | Pending — production window (27B in), read second load | — |
| KV cell q8_0 vs q4_0 at 262K with quality probe | Pending — same window as Step-3 | — |
| `base.py` usage contract | Pending | — |
| `llamacpp_server` provider + supervisor + fetcher + UI | Pending — timing depends on Open Question 1 | — |
| `ProviderLimits` on local aliases; remote-share rule | Pending | — |
| Backlog: re-title, two amendments, four new entries | Done 2026-08-18 — CC (the board is gitignored) | — |

## Open Questions

- **Q1 — DeepSeek top-up: yes / no / how much. `Round 2, 2026-08-18`: the threshold has fired and the
  question is no longer hypothetical.** Balance **$25.64** (Mike, live), against $36.68 on 08-15 —
  a balance-derived burn of ~$5.3/day, above the $2.7–4.5 projection this ADR was drafted under.
  Events-attributed burn over the same window is ~$3.45/day, and the ~$1.9/day gap between the two is
  itself a finding, not an explanation (peer requests and the plain path belong to no agent, so they
  reach no series). Runway to the $12 floor: **2.6–4.0 days**. And the fleet leans on DeepSeek harder
  than the draft assumed: **7 of 9 agents run `deepseek_flash` as their main provider** (Ark, Forge,
  Johnny, Muse, Pulse, Scout, Warren; Iris and Kotler on `qwen3.8:latest`) — measured
  **2026-08-18 by CC**, and the figure is dated because the field moves: the same three agents read
  `qwen3.8:latest` the evening before and their configs were rewritten at 03:01. So a top-up is not a
  bridge over Stage 2; it is a running cost until those seven migrate, which Stages 0–2 do not by
  themselves accomplish. Stages 0–1 still do not wait on this. — Mike
- **Q2 — The production window** for Step-3 + KV cell (27B in) and the NVFP4 cell (27B out): when
  may the five qwen3.8 agents be without a model, and for how long. — Mike
- **Q3 — ~~Does `keep_alive=-1` protect a model from Ollama's own eviction under memory pressure~~ —
  CLOSED `Round 2, 2026-08-18`: only from the TTL.** Read in Ollama v0.32.14 `server/sched.go`:
  `findRunnerToUnload` sorts by `uint64(sessionDuration)` and returns the first idle runner; the pending
  path sets `sessionDuration = 0` and expires it. `-1` sorts last and nothing more. 0a alone would never
  have been enough; see D1/0a and 0e.
- **Q4 — Does the mainline `win-cuda-13.3` artefact carry `sm_120`?** `cuobjdump --list-ptx` on its
  `ggml-cuda.dll` (two minutes). If not, the cuda-12.4 asset or an owned build. — CC
- **Q5 — Does the mainline loader take the HF Qwen3.8 GGUF with the separate `mtp-*.gguf` at 262K,
  keeping MTP parity on (b2)?** If not, measure decode without MTP before accepting (b2) for this
  model. — CC
- **Q6 — macOS node memory. Reframed by Mike 2026-08-18, and it is no longer an input to Q1.**
  The original wording — below ~30 GB the provider is "a one-node feature" — measured the wrong
  thing. The purpose of this box is to host models *and to share inference with the other nodes*,
  and that sharing is implemented and running (`p2p_coordinator.py:166-196`, D5). So the three-OS
  criterion reads on **consumers** of inference, not on every node hosting a 27B, and a macOS node
  that cannot hold the model is the case D5 already covers rather than a reduction in scope. The
  unified-memory figure is still worth having — it closes the `Not verified` cell in D5 and answers
  whether macOS would ever *also* host — but it gates nothing, and Q1 is decided on runway against
  the cost of a DeepSeek outage alone. — Mike
- **Q7 — ~~Ollama environment pass-through~~ — CLOSED `Round 2, 2026-08-18`: it passes.**
  `llm/llama_server.go:446` sets `cmd.Env = os.Environ()` and the launch line carries no `--cache-ram`,
  so `LLAMA_ARG_CACHE_RAM` reaches the child; the binary's `--help` names it. The remaining check is the
  "prompt cache … size limit" line after the restart, which now reads as confirmation rather than as
  the question. **The value format was still unrecorded and is now measured** (`Round 2 answers`,
  Johnny's point 5): an integer in MiB, `24576` for 24 GiB — see 0e.
- **Q8 — ~~Telemetry harness: own entry?~~ — DECIDED `Round 2, 2026-08-18`: yes, and it ships with the
  first package rather than after it.** See D4-T; filed as
  `THE-LEVERS-WE-ARE-ABOUT-TO-PULL-CANNOT-BE-TOLD-APART-IN-PRODUCTION`.
- **Q9 — What the vision move costs when the target model thinks (`Round 2`, new).** 0b survived its
  first measurement — at n=2 pages `qwen3.8:latest` beat `qwen3-vl:8b` on both (0 errors vs 2 on a
  scan; 0.8 % vs 1.3 % CER born-digital; 4–11 s vs 24–140 s, the 8b returning empty at 139.6 s on
  `done_reason=length`). But the `qwen3.8` alias carries `think: True` and `read_document` passes only
  `temperature`, so every page moved onto the 27B may carry a thinking tax in time and GPU. The full
  R4 run is a **confirmation item, not a precondition**, and it must record the think state and its
  cost — otherwise a "2× slower" result fires for the wrong reason. — CC

## Authors

- **Mike** — Decision (top-up, production window, verbs on Stages 0–2 pending).
- **Fable 5** — independent review (`…-review-fable-5.md`, §§1–7), this ADR draft.
- **GLM 5.3** — independent review (`…-review-glm5.3.md`, M1–M4, Q0–Q8, per-OS section).
- **CC** — review prompt, prompt amendments (fleet, per-OS criterion, measured `bge-m3`), code checks
  (`keep_alive` paths, seed at `:171`, `memory_config` fields), board ownership.
- **Ark** — synthesis (#42, #54), the top-up framing, the production-window correction.
- **Johnny** — differential-test framing, atomic-bundle rule, 0d-before-Step-3, the two-phase window,
  the cross-platform question.
- **Warren** — ROI gate, runway arithmetic, the bge-m3 correction across instruments.

## References

- `ideas/dpc-research/llamacpp-local-provider-prompt.md` — the review prompt (amended 2026-08-18).
- `ideas/dpc-research/llamacpp-local-provider-review-fable-5.md` — Fable 5 review (§1 measurements,
  §6 per-OS addendum, §7 thread responses).
- `ideas/dpc-research/llamacpp-local-provider-review-glm5.3.md` — GLM 5.3 review (M1 GGUF metadata
  parse, M2 server probe, M3 partition, M4 supply chain, per-OS section).
- `ideas/dpc-research/llamacpp-server-provider-prompt-round2.md` — the round-2 prompt, and
  `…-round2-fable-5.md` / `…-round2-glm5.3.md` — the two round-2 reviews that produced the corrections
  marked `Round 2` above; `…-round2-findings-2026-08-18.md` — the findings that outlive them.
- `C:\Users\mikha\Documents\brainbake\docs\decisions\014-inference-backend.md` — brainbake's
  inference-backend decision (in-process `llama-cpp-2`, `swa_full`, 128K gate, FP4 measurement).
- `backlog.md` — `LLAMACPP-LOCAL-PROVIDER`, `OLLAMA-CUSTOM-BUILD-NVFP4`,
  `AN-AGENT-CANNOT-BE-GIVEN-ITS-OWN-EYES`, `ACTIVE-RECALL-TORCH-METADATA-EMPTY`,
  `A-MODEL-CAN-SPEND-ITS-WHOLE-BUDGET-THINKING-AND-RETURN-AN-EMPTY-ANSWER`.
- `VISION.md` — C8 "Compute respects agency", C10 "Shared infrastructure is regulated commons",
  "Compute Commons"; `ROADMAP.md:82, :211`.
- `%LOCALAPPDATA%\Ollama\server.log` (2026-08-17) — launch lines, loader buffers, prefill/decode
  timings, `common_memory_breakdown_print`, `common_params_fit_impl`.
- llama.cpp release `b10472` (2026-08-17) asset list; llama-cpp-python 0.3.35 (PyPI, GitHub release
  assets, wheel index); Hugging Face repos `unsloth/Qwen3.8-27B-{NVFP4,GGUF}`,
  `williamliao/Qwen3.8-27B-NVFP4-GGUF`, `esatapedico/Qwen3.8-27B-NVFP4-MTP-GGUF`,
  `ggml-org/Qwen3.8-27B-GGUF`, `turboderp/Qwen3.8-27B-exl3`; ExLlamaV3 v1.4.2 release; vLLM GPU
  installation docs.
