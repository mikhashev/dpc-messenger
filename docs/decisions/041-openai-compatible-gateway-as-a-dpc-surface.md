---
adr: 041
title: "Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by identity rather than by an API key — and give every consumer of a model one usage row"
status: proposed
date: 2026-08-31
deciders: [Mike]
consulted: [Ark, Johnny, CC]
informed: []
depends_on: [ADR-040]
related: [ADR-002, ADR-026, ADR-038]
supersedes: []
session: "DPC Project #82–#96, 2026-08-31 — Mike's colleague wants Qwen3.8-27B from Continue in VSCode; Ark proposed the gateway (#85), Johnny objected to one clause of it (#95), Mike asked for the ADR (#96)"
---

# ADR-041: Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by identity rather than by an API key — and give every consumer of a model one usage row

> **Status: proposed.** This is a recommendation standing on measurements, in the
> form ADR-040 used, not a statement that we are building it. Every claim below is
> marked `Observed` (read in this tree today, location given), `Inferred`, or
> `Not verified`. The three measurements Ark asked for before drafting are all
> closed — **and two of them came back differently from what the thread assumed**,
> which is the reason they were run. Two further measurements nobody asked for
> change the cost of the first decision and the shape of the last one.
>
> What Mike has to decide is in **Open Questions**. Nothing here is started.

## Context and Problem Statement

A colleague of Mike's wants to use our production model, `qwen3.8 27b Mythos`,
from the Continue plugin in VSCode (#84). Continue speaks the OpenAI HTTP API.
Two paths were put on the table:

1. **Expose a llama-server to him** (Ark, #83) — a second child with
   `--host 0.0.0.0`, a fixed port, `--api-key`, reached over Tailscale. Our
   supervisor hardcodes `--host 127.0.0.1` on a dynamic port, so this is a
   process outside DPC, configured by hand.
2. **Serve the OpenAI API from inside DPC** (Ark, #85; Mike asked for it, #84) —
   the colleague runs a DPC node, an OpenAI-compatible surface listens on *his*
   loopback, and the request travels our own P2P path to whichever node serves
   the alias.

Mike then added the requirement that decides the shape of the third decision
below: **usage has to be countable afterwards** — «сколько usage и т.д. для
аналитики», «если в дальнейшем кто то захочет монетизировать такой шаринг»
(#87–#89).

The problem this ADR settles is not «can we reach the model». It is **what the
outward-facing surface of this product is**, and whether a thing that consumes
one of our models leaves a record that anyone can read later.

## Measurements taken before the decision

### M1 — There is no streaming over the peer path. `Observed`

`p2p_coordinator.request_inference_from_peer` mints one `request_id`, awaits one
`asyncio.Future` through a single `asyncio.wait_for`, and returns `result`
whole. There is no SSE, no chunk callback, no partial delivery. Johnny found
this (#95); it is repeated here because it was read again rather than taken.

**Consequence, and it belongs in the decision rather than in a later surprise:**
the gateway can stream only what it serves locally. A colleague on his own node
reaches our model through the peer path, so **he gets no streaming** until that
path learns to stream. Continue works without it; it is less pleasant, and it is
a property of our transport, not a defect of the gateway.

*A second thing observed in the same function, which closes an unrelated open
question from the same day:* the peer wait **does** carry a timeout that reaches
the await — `asyncio.wait_for(response_future, timeout=timeout)`, defaulting to
1200 s in the coordinator and passed as 180 s by `llm_adapter`. The peer path is
not among the places that can hang unbounded.

### M2 — Usage crosses the wire already; nothing on the serving side records it. `Observed`

Both reviewers had this as unverified, and it is half better and half worse than
they assumed.

`create_remote_inference_response` (`dpc-protocol/protocol.py:58`) carries
`tokens_used`, `prompt_tokens`, `response_tokens`, `model_max_tokens`,
`thinking_tokens`, `model` and `provider`. `handle_inference_request` fills every
one of them from `llm_manager.query(..., return_metadata=True)`. So the requester
already learns what his call cost in tokens.

Two things qualify that, and both matter to anyone who would bill on it:

- **The counts are ours, not the engine's.** They come from our own
  `count_tokens` over the prompt and the answer. The log line names them
  `prompt_tokens_est` / `response_tokens_est` for exactly this reason, and the
  comment beside it says the daemon's own figures for the same call sit on the
  neighbouring line and are «close and different».
- **The host who spends the card writes no row.** The comment at
  `p2p_coordinator.py:220` states it outright: *«A peer's request belongs to no
  agent, so it writes no row in any events.jsonl and appears in no cost series.
  This line is the record.»* The record of a stranger's use of our GPU is one
  INFO line in a rotating log.

### M3 — A request_id exists end to end and reaches nothing. `Observed`

`request_inference_from_peer` mints `str(uuid.uuid4())`, the response echoes it,
and `RemoteInferenceResponseHandler` uses it to resolve the future. It then
disappears: it is **not** in the `Peer inference served` line, and there is no
usage row anywhere for it to key. So the identifier Ark asked for already
exists; what is missing is any destination for it.

### M4 — The core runs no HTTP server, and this is the price of D1. `Observed`

`dpc-client/core/pyproject.toml` names `websockets>=11.0` and no `fastapi`,
`uvicorn`, `aiohttp`, `starlette` or `hypercorn`. `local_api.py` is a WebSocket
server on 9999. An OpenAI-compatible surface is HTTP with `text/event-stream`
for streaming, so D1 costs either a new server dependency in the client or a
hand-rolled HTTP/SSE server. Nobody in the thread priced this, and the answer is
«more than ~200–400 lines of translation».

### M5 — A locally served model has no dollar cost by construction. `Observed`

`dpc_agent/pricing.py:compute_cost_usd` returns `0.0` for anything that is not
pay-per-token; local aliases are not. So a shared local model produces
`cost_usd = 0` in every row that exists today. **Monetising shared local compute
therefore cannot read `cost_usd`** — the quantities that exist are tokens and
occupancy of the card. This does not block the ledger; it decides which column
the ledger has to carry, and it is why the ledger and the price must be
separated (D3).

## Decision

### D1 — The OpenAI surface is a component of the DPC client, on loopback

The gateway lives inside the client, listens on `127.0.0.1`, and hands requests
to the provider layer, which already routes locally or to a peer. It is not a
second llama-server with a port open to a network.

The gain is not convenience: it is that **no port is exposed anywhere**. The
colleague's tool talks to his own machine; the inference travels our
authenticated, encrypted P2P channel. Everything the adapter already does —
alias resolution, reasoning effort, usage capture, local/peer routing — is
inherited rather than reimplemented.

The cost is M4: a server the client does not currently have.

### D2 — Access is DPC identity, gated by the firewall we already run

Authorisation is membership plus `privacy_rules.json`, not a shared secret on an
open socket. The mechanism exists and is the right shape already:
`firewall.can_request_inference(peer_id, model, provider)` decides, and
`compute.serving_alias` decides what this node will run — with the alias the
caller names treated as **evidence for the gate, never as an instruction to the
router** (ADR-040 D4-0). A caller who names an alias we do not serve is refused
by name, which is the behaviour observed in production on 2026-08-31.

The gateway's own local key is a convenience for the client on that machine, not
the boundary. The boundary is the roster.

### D3 — One usage row per model call, whoever the caller is — attribution only, not price

Every consumption of a model writes one row in one schema: **who** (node id and,
where it exists, agent id), **what** (alias, model), **how much** (prompt,
completion, thinking tokens; and whether the counts are ours or the engine's),
**when** (UTC), **by which route** (local / peer), keyed by the `request_id`
that M3 shows already exists.

Today the agent path writes `task_complete` rows in a per-agent
`logs/events.jsonl` with `cost_usd` and a token block; the peer path writes
nothing (M2). Unifying them is the whole of this decision.

**Johnny's objection is adopted, and it is a correction to Ark's third point,
not a footnote.** Attribution and monetisation are different levels: the ledger
records that a thing was consumed, a policy decides what that fact is worth. M5
is the evidence that they cannot be one field — a locally served model has no
price today, and a ledger that stores «cost» would store zero for exactly the
traffic this ADR is about. **This ADR decides the ledger. It does not decide the
price.**

### D4 — First version is narrow, and its limits are stated now rather than found later

In: `/v1/chat/completions` and `/v1/models`, local routing, streaming **on the
local path only** (M1).

Deferred, explicitly: peer routing for external clients; `/v1/embeddings`, which
Continue wants for indexing and which our bge-m3 could serve; streaming over
P2P.

## Consequences

- **The product grows an outward-facing surface.** Everything that has been
  true only of our own UI — the firewall's coverage, the shape of refusals, what
  a log line reveals — now faces a tool we do not control.
- **A new dependency or a hand-written HTTP/SSE server** (M4).
- **The colleague needs a place in the roster** to be authorised at all; «просто
  знакомый» is not an identity our gate can read.
- **The card is unchanged.** ONE-CARD-ONE-OWNER still holds: the gateway adds no
  VRAM, and a second consumer competes for the same slots as the agents. That is
  a policy question, deliberately not decided here.
- **The ledger becomes the thing that must not be broken**, because analytics and
  any later billing both read it. Writing it after the fact would produce the
  second schema Ark warned about.
- **`_est` token counts become a published number.** As soon as anyone reads the
  ledger to settle anything, «close and different» from the engine's own count
  stops being an internal curiosity.

## Open questions — Mike's, not the code's

1. **Does the colleague get a temporary bridge now?** Mike said «ща никуда ниче
   не выдаем, коллеге не горит» (#96), so the assumption here is **no**, and Ark's
   path 1 stays unbuilt. If that changes, it is a separate decision with its own
   risk, and it should not be justified by this ADR.
2. **Whose loopback?** The design above puts the gateway on the *consumer's*
   node, which is what makes «no open port» true. A gateway on the *serving*
   node reached over a tailnet is simpler and gives streaming immediately — and
   gives up the property that motivates D1. Not decided.
3. **Does the ledger live per agent or per node?** Today it is per agent, which
   is precisely why a peer's call has nowhere to go. A node-level ledger changes
   where every existing reader looks.
4. **What is counted for a local model** — tokens, seconds of card occupancy, or
   both? M5 says «dollars» is not available, and the answer decides the schema
   before anything is written.

## Falsifiers

One per claim that would change the decision if it failed.

- **M1:** grep the peer path for a chunk callback or an `AsyncIterator`. If one
  exists, D4's «local path only» is wrong and the first version is bigger.
- **M2:** run one peer inference and read the requester's result dict. It must
  carry non-null `prompt_tokens` and `response_tokens`. Then grep every
  `events.jsonl` under `~/.dpc/agents/` for that call — there must be no row.
  If a row exists, D3 is already half done.
- **M3:** grep the log for the `request_id` of that same call. It must appear
  only in the debug line of the requester, nowhere on the serving side.
- **M4, run 2026-08-31, and the obvious command is a trap.** Ask the client's
  own environment, not the shell's: `uv run --directory dpc-client/core python -c
  "import fastapi"` must fail, or equivalently
  `ls dpc-client/core/.venv/Lib/site-packages | grep -Ei "^(fastapi|uvicorn|starlette)"`
  must be empty — it is. `uv pip list | grep fastapi` **does** hit, and that hit
  is the Hub's virtualenv: `VIRTUAL_ENV` commonly points at `dpc-hub/.venv` in a
  shell here, `uv` warns that it will ignore it and `uv pip list` reads it
  anyway. fastapi 0.141.1, starlette 1.4.1 and uvicorn 0.52.1 live there,
  because the Hub is a FastAPI service; the client venv holds none of the three.
- **M5, run 2026-08-31.** `compute_cost_usd("qwen3.8 27b Mythos", 1000, 1000)` →
  `0.0`, `get_billing_model` → `subscription`; the same call on `deepseek_flash`
  → `0.00088`, `pay_per_use`. Local compute is priced at zero by construction,
  which is D3's whole argument. If this ever returns non-zero for a local alias,
  D3's reasoning needs re-reading.
- **D2, the one worth running before any code:** ask a peer for an alias this
  node does not serve, and confirm the refusal names the alias. Observed once
  already in production, on the Ubuntu node, on 2026-08-31 — as the symptom of a
  different defect.
