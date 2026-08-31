---
adr: 041
title: "Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by identity on the one transport that proves it — and move the usage ledger from the agent to the node"
status: proposed
date: 2026-08-31
deciders: [Mike]
consulted: [Ark, Johnny, CC]
informed: []
depends_on: [ADR-040]
related: [ADR-002, ADR-026, ADR-038]
supersedes: []
session: "DPC Project #82–#110, 2026-08-31 — Mike's colleague wants Qwen3.8-27B from Continue in VSCode; Ark proposed the gateway (#85), Johnny objected to one clause (#95), Mike settled four questions and added a fifth (#101–#102), then ruled the Hub out of the trust path (#110); two review rounds by Ark and Johnny"
---

# ADR-041: Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by identity on the one transport that proves it — and move the usage ledger from the agent to the node

> **Status: proposed.** A recommendation standing on measurements, in the form
> ADR-040 used. Every claim is marked `Observed` (read in this tree today,
> location given), `Inferred`, or `Not verified`. The three measurements Ark
> asked for before drafting are closed and **two came back against the thread's
> assumption**; three more were needed and one of them changes the price of D1.
>
> Nothing here is started. Of the four questions the first draft left open, all
> four are now decided; **D6** carries a recommendation and waits on Mike.

## Context and Problem Statement

A colleague of Mike's wants our production model, `qwen3.8 27b Mythos`, from the
Continue plugin in VSCode (#84). Continue speaks the OpenAI HTTP API. Two paths
were proposed: expose a second `llama-server` to a network (Ark, #83), or serve
the OpenAI API from inside DPC and let the request travel our own P2P path
(Ark, #85, at Mike's asking).

Mike then added the requirement that shapes the third decision: usage must be
countable afterwards — «сколько usage и т.д. для аналитики», «если в дальнейшем
кто то захочет монетизировать такой шаринг» (#87–#89) — and, later, that
API-backed models should be shareable the same way (#102).

The question this ADR settles is not «can we reach the model». It is **what the
outward-facing surface of this product is**, and whether a thing that consumes
one of our models leaves a record anyone can read afterwards.

## Measurements taken before the decision

### M1 — There is no streaming over the peer path. `Observed`

`p2p_coordinator.request_inference_from_peer` mints one `request_id`, awaits one
`asyncio.Future` through a single `asyncio.wait_for`, and returns `result`
whole. No SSE, no chunk callback, no partial delivery. Found by Johnny (#95),
re-read here rather than taken.

**Consequence, stated in the decision rather than discovered later:** a consumer
on his own node reaches our model through the peer path, so **he gets no
streaming** until that path learns to stream. Continue works without it. This is
a property of our transport, not a defect of the gateway.

*Also observed in the same function, closing an unrelated question from the same
day:* the peer wait carries a timeout that reaches the await —
`asyncio.wait_for(response_future, timeout=timeout)`, 1200 s by default in the
coordinator and 180 s as `llm_adapter` passes it. The peer path is not among the
places that can hang unbounded.

### M2 — Usage crosses the wire already; nothing on the serving side records it. `Observed`

Both reviewers had this as unverified, and it is half better and half worse than
assumed.

`create_remote_inference_response` (`dpc-protocol/protocol.py:58`) carries
`tokens_used`, `prompt_tokens`, `response_tokens`, `model_max_tokens`,
`thinking_tokens`, `model` and `provider`, and `handle_inference_request` fills
every one from `llm_manager.query(..., return_metadata=True)`. The requester
already learns what his call cost in tokens.

Two qualifications, both load-bearing for anyone who would bill on it:

- **The counts are ours, not the engine's** — our `count_tokens` over the prompt
  and the answer. The log line names them `prompt_tokens_est` /
  `response_tokens_est` for that reason, and the comment beside it says the
  daemon's own figures for the same call are «close and different».
- **The host who spends the card writes no row.** `p2p_coordinator.py:220` says
  it outright: *«A peer's request belongs to no agent, so it writes no row in any
  events.jsonl and appears in no cost series. This line is the record.»* The
  record of a stranger's use of our GPU is one INFO line in a rotating log.

### M3 — A request_id exists end to end and reaches nothing. `Observed`

`request_inference_from_peer` mints `str(uuid.uuid4())`, the response echoes it,
`RemoteInferenceResponseHandler` uses it to resolve the future — and it then
disappears. It is not in the `Peer inference served` line, and there is no usage
row for it to key. The identifier already exists; what is missing is a
destination.

### M4 — The core runs no HTTP server, and this is the price of D1. `Observed`

`dpc-client/core/pyproject.toml` names `websockets>=11.0` and no `fastapi`,
`uvicorn`, `aiohttp`, `starlette` or `hypercorn`; `local_api.py` is a WebSocket
server on 9999. An OpenAI-compatible surface is HTTP with `text/event-stream`,
so D1 costs either a new server dependency in the client or a hand-written
HTTP/SSE server. Nobody priced this in the thread, and it is a decision to take
here rather than at code review — see **D6**.

### M5 — A locally served model has no dollar cost by construction. `Observed`

`compute_cost_usd('qwen3.8 27b Mythos', 1000, 1000)` → `0.0`, billing
`subscription`; the same call on `deepseek_flash` → `0.00088`, `pay_per_use`.
Monetising shared *local* compute cannot read `cost_usd`; the quantities that
exist are tokens and occupancy of the card. This does not block the ledger — it
decides which columns it must carry, and it is why the ledger and the price are
separate decisions.

### M6 — Every path is encrypted; only the direct path proves who is on the other end. `Observed`

Added because D2's whole claim is «identity is the boundary», and that claim is
exactly as strong as the weakest path the gateway can route over.

| path | encryption | authentication of the peer's identity |
|---|---|---|
| Direct TLS (IPv4/IPv6) | TLS to the peer's self-signed certificate | **three checks, and they are stricter than a CA chain.** `_verify_hello_identity`: cert CN equals the claimed node_id; `SHA256(public key)[:32]` equals the claimed node_id; and an RSA-PSS signature over a fresh 32-byte nonce from `HELLO_CHALLENGE`. The identity is self-certifying, so a forged certificate fails check 2 and a stolen public certificate fails check 3 |
| WebRTC via the Hub | DTLS (aiortc, SCTP over DTLS) | **none of its own.** The challenge runs in `_handle_direct_connection` only; `hello_handler.py` says so in its own docstring — «Mainly for WebRTC connections that don't have initial handshake». The binding between the DTLS fingerprint and the node identity is whatever the Hub signalled |
| Volunteer relay | end-to-end AES-256-GCM + RSA-OAEP **above** the transport, keyed from the peer's certificate | **recipient only.** Only the holder of the matching private key can decrypt, and the GCM tag proves the ciphertext is untampered — but nothing signs the sender. `grep sign\|signature\|verify` over `transports/relayed_connection.py` and `managers/gossip_manager.py` returns nothing |
| Gossip | the same hybrid scheme | the same — recipient and integrity, not sender |
| Hub | signalling only; message content never passes through it | — |

`verify_mode = ssl.CERT_NONE` on the direct path is not a weakening: it is
required because there is no shared CA, and the manual binding that replaces it
is stricter than a chain — the identity is the hash of the key.

**The conclusion that matters to D2 is sharper than «one gap».** The firewall
gates compute on `can_request_inference(peer_id, …)`, and `peer_id` is
`sender_node_id`, which the transport supplies. Only the direct TLS path
establishes that value cryptographically. On WebRTC it is what the Hub routed;
on relay and gossip it is a field in an envelope. **The one path that proves who
is asking is the one this ADR should require for gateway traffic** — which is
D2.

*Found while reading this and reported separately, not part of this ADR:* the
relay path as written cannot deliver at all. `relay_message_handler.py:102`
refuses when `payload["from"] != sender_node_id`, and on the **destination**
node those two are the originator and the relay respectively — the check fires
before the client-mode branch at `:123` is reached. There are no tests for
`RELAY_MESSAGE`. Read from the code, not run.

## Decision

### D1 — The OpenAI surface is a component of the DPC client, on the consumer's loopback

The gateway lives inside the client, listens on `127.0.0.1` **of the machine
that is consuming**, and hands requests to the provider layer, which already
routes locally or to a peer. It is not a second `llama-server` with a port open
to a network.

Mike settled the «whose loopback» question (#101): the consumer's. The reason is
not convenience — it is that **no port is exposed anywhere**, and that the
consumer gets a DPC node rather than a URL. A colleague who installs the client
has an identity, can be given rules, and can in turn share his own compute. The
gateway is not «access to a model»; it is the product, with the model as one of
its services.

The cost is M4.

### D2 — Access is DPC identity, gated by the firewall we already run

Authorisation is roster membership plus `privacy_rules.json`, not a shared
secret on an open socket. The mechanism exists and is already the right shape:
`firewall.can_request_inference(peer_id, model, provider)` decides, and
`compute.serving_alias` decides what this node will run — with the alias the
caller names treated as **evidence for the gate, never as an instruction to the
router** (ADR-040 D4-0). A caller naming an alias we do not serve is refused by
name; that behaviour was observed in production on 2026-08-31.

The gateway's local key is a convenience for the tool on that machine, not the
boundary. The boundary is the roster.

**And the boundary is only as strong as the transport that carried the caller's
name (M6), so this decision names the transport.** Gateway traffic is authorised
on the direct TLS path, where `sender_node_id` is proved by the HELLO challenge
against a self-certifying identity. It is **not** authorised on the WebRTC path,
whose binding is the Hub's word.

That is a decision rather than a measurement, and Mike took it (#110): «Хаб
планировали выпиливать, так что пофиг на него». The written form of the same
direction is ROADMAP §211 — «Hub becomes optional bootstrap, not architecture
center». *No board entry planning the Hub's removal was found; the plan is
Mike's word plus that roadmap line.* Either way the consequence is the same:
building an outward-facing authorisation boundary on Hub-mediated identity would
tie the product's new surface to the component it is moving away from.

**What this costs:** a consumer behind a NAT that only WebRTC could traverse
cannot use the gateway until either the WebRTC path gains its own challenge or a
lower tier serves him. Relay and gossip do not currently substitute — per M6 they
prove the recipient, not the sender, which is the half D2 needs.

### D3 — The usage ledger moves from the agent to the node, and carries who called

One row per model call, in one schema, on the node that owns the resource:

```
request_id, caller, caller_kind (agent|peer|gateway), alias, model,
route (local|peer), prompt/completion/thinking tokens, counts_source (ours|engine),
started_at, duration_s, card_seconds, billing (subscription|pay_per_use), cost_usd
```

**Why the node and not the agent.** The card is on the node. The DeepSeek key is
on the node. All three kinds of call — an agent of ours, a peer, a future
gateway client — consume the same node's resource. Today the ledger is per agent
(`agent_root/logs/events.jsonl`), and that is precisely why a peer's call has
nowhere to go (M2): it belongs to no agent, so it is written nowhere. The
traffic this ADR exists to account for is the traffic the current design cannot
see.

What it looks like:

| today | with a node ledger |
|---|---|
| our agent, local model → row in that agent's `events.jsonl` | row, `caller=<agent>`, `route=local` |
| a peer asks us for Mythos → **nothing** but one INFO line | row, `caller=<their node>`, `route=local` (we ran it) |
| a gateway client through his own node → **nothing** | row, `caller=<his node>`, `caller_kind=gateway` |

Then «how much did the colleague use» and «how much did that agent use» are two
filters on one table rather than a reconciliation of two schemas.

**Price of the change:** the per-agent files become a projection
(`WHERE caller=<agent>`), and every existing reader — the UI, the agent's own
history — has to be moved onto it. That is a migration, and it is the reason
this is a decision rather than an addition. Keeping per-agent files and writing
a second ledger only for strangers is cheaper and produces exactly the two
schemas this decision exists to avoid.

**When a paid provider is what is shared, the money is counted — and it has to
be counted at the moment of the call.** Mike, #111: «если таким образом или иным
шарится доступ к платному провайдеру то надо считать и деньги». That is not
merely the `cost_usd` column being present; it is a constraint on *when* it is
filled, and the reason is in `pricing.py`.

A DeepSeek call's price is not a function of its tokens. `rates_at` reads the
UTC hour against `PEAK_WINDOWS_UTC` (01:00–04:00 and 06:00–10:00,
`PEAK_MULTIPLIER = 2.0`), suspends those windows on a *Beijing* weekend — the
vendor's calendar, which spans three UTC days — and only from
`WEEKEND_OFF_PEAK_FROM = 2026-08-22 16:00 UTC`; and `_peak_applies` limits the
whole rule to DeepSeek, because Z.AI shares the tables and not the clock. The
table itself has already changed twice this month.

So a row that stores only tokens **cannot** be priced later: recomputing it needs
the exact instant, that day's rate table, and that day's version of these rules.
`cost_usd` and `billing` are therefore written by the node that made the call, at
the time it made it, and are never re-derived. `Observed`: the machinery exists
and is correct — `compute_cost_usd(..., at=...)` takes the moment for exactly
this reason; only the row is missing.

*A design question this raises and does not answer:* the response to the
requester carries tokens but **no cost field** (`create_remote_inference_response`
has none). So today the host can know what a shared call cost and the guest
cannot. Whether the guest should be told is a decision for whoever takes up
pricing; the ledger works either way.

**Beyond that it records attribution, not price.** Johnny's objection (#95) is adopted
as a correction rather than a footnote: the ledger records that a thing was
consumed; a policy decides what that fact is worth. M5 is the evidence that they
cannot be one field — a local model is priced at zero, which is exactly the
traffic in question. Mike's «всё что можно и пригодится для аналитики» (#101) is
satisfied by the columns above; **this ADR does not decide the price.**

### D4 — First version is narrow, and its limits are stated now

In: `/v1/chat/completions` and `/v1/models`, local routing, streaming **on the
local path only** (M1).

Deferred, explicitly: peer routing for external clients; `/v1/embeddings`, which
Continue wants for indexing and which our bge-m3 could serve; streaming over
P2P.

### D5 — API-backed models are shareable too, and that is the case the ledger must not exclude

Mike, #102. Sharing a vendor-backed alias is not the same act as sharing a local
one: **the node holding the key pays real money.** It therefore needs three
things this design must leave room for, and two of them do not exist today:

1. **Two lists, not one longer list** — Ark's correction (#108), adopted. The
   single `compute.serving_alias` becomes an allow-list *per class*: local
   aliases, whose scarce resource is the card and whose refusal is «the card is
   busy»; and vendor aliases, whose scarce resource is money and whose refusal is
   «the budget is spent». One list would put two different refusal policies
   behind one setting, and the owner would not be able to say «share the GPU
   freely, the API never» — which is the setting most people would want first.
   `Observed`: the firewall serves exactly one alias today, and
   `p2p_coordinator` refuses out loud when it is unset.
2. **Per-caller quotas or limits** — `Observed absent on this path, and present
   three times elsewhere.` `firewall.py` and `p2p_coordinator.py` contain no
   quota, rate limit, budget or throttle: `can_request_inference` answers yes or
   no and nothing counts how often. Meanwhile `dpc_agent/budget.py` enforces
   `requests_per_minute` / `requests_per_day` per agent and provider,
   `coordinators/discord_coordinator.py` enforces per-user and global windows for
   an outside surface, and `managers/relay_manager.py` rate-limits a relayed peer
   at 100 messages per second. The shape is written three times in this
   repository and wired to neither the peer nor the gateway — which makes item 2
   a wiring job rather than a design one.
3. **A ledger row carrying `cost_usd`**, which for a pay-per-use alias is
   already computable (M5: `0.00088` for a 1000/1000 DeepSeek call). The column
   exists; the row to put it in does not.

A node-level ledger with `caller` and `cost_usd` admits this case. A per-agent
ledger excludes it, because a stranger's paid call belongs to no agent. That is
the second, independent reason for D3.

Shipping order is unchanged: local first, API-backed sharing second — but the
schema is decided now so the second step is not a rewrite.

## Consequences

- **The product grows an outward-facing surface.** Everything that has been true
  only of our own UI — the firewall's coverage, the shape of refusals, what a log
  line reveals — now faces a tool we do not control.
- **A new dependency or a hand-written HTTP/SSE server** (M4).
- **A migration of every usage reader** onto the node ledger (D3).
- **The colleague needs a place in the roster**; «просто знакомый» is not an
  identity our gate can read.
- **The card is unchanged.** ONE-CARD-ONE-OWNER still holds: the gateway adds no
  VRAM and a second consumer competes with the agents for the same slots. That
  is a policy question, deliberately not decided here.
- **`_est` token counts become a published number.** The moment anyone reads the
  ledger to settle anything, «close and different» from the engine's own count
  stops being an internal curiosity — hence `counts_source` in the schema.
- **The WebRTC path's identity gap (M6) becomes load-bearing.** Today it decides
  who may read a chat; under D2 it would also decide who may spend the card.

## D6 — The HTTP server: a decision to take here, with both cases put

Ark asked (#108) that this stop being an open question and be argued in the ADR,
and he is right: it is architecture, not code review. Both cases, honestly:

**Hand-written asyncio HTTP + SSE.** No new dependency in a client that must run
on a user's machine without the Hub; complete control of the error surface; and
the repository already contains one hand-written asyncio server — `local_api.py`
on 9999 — so this is a second instance of an existing pattern rather than a first.

**`fastapi` + `uvicorn`.** The Hub already runs them, so the idiom is not foreign
to the team; and the parts that are cheap to write badly are exactly the parts
this buys — chunked transfer, client-disconnect mid-stream, and the HTTP status
vocabulary a tool like Continue expects (400/401/403/429/503).

**Johnny's correction to the size estimate is adopted** (#109): `local_api.py` is
a *WebSocket* server, not an HTTP one, so «second instance of the pattern»
understates the work. With SSE, disconnect handling and status codes, his figure
is 500–600 lines with tests rather than 300–400.

**Recommendation: hand-written**, on the strength of the dependency argument — a
client that must start on a machine with no server stack has a different budget
from a service that is one. But the deciding vote is Mike's, and if the answer is
FastAPI the ADR is unchanged apart from this section.

## Open Questions

None outstanding. The four of the first draft were settled in #101–#102 and
#110 and are now inside D1, D2, D3 and D5; D6 carries a recommendation awaiting
Mike's word.

## Falsifiers

One per claim that would change the decision if it failed.

- **M1:** grep the peer path for a chunk callback or an `AsyncIterator`. One
  exists ⇒ D4's «local path only» is wrong and the first version is bigger.
- **M2:** run one peer inference and read the requester's result dict — it must
  carry non-null `prompt_tokens` and `response_tokens`. Then grep every
  `events.jsonl` under `~/.dpc/agents/` for that call: there must be no row. A
  row ⇒ D3 is already half done.
- **M3:** grep the log for that call's `request_id`. It must appear only in the
  requester's debug line, nowhere on the serving side.
- **M4, run 2026-08-31, and the obvious command is a trap.** Ask the client's own
  environment: `uv run --directory dpc-client/core python -c "import fastapi"`
  must fail, or `ls dpc-client/core/.venv/Lib/site-packages | grep -Ei
  "^(fastapi|uvicorn|starlette)"` must be empty — it is. `uv pip list | grep
  fastapi` **does** hit, and that hit is the Hub's virtualenv: `VIRTUAL_ENV`
  commonly points at `dpc-hub/.venv` in a shell here, `uv` warns that it will
  ignore it and `uv pip list` reads it anyway. fastapi 0.141.1, starlette 1.4.1
  and uvicorn 0.52.1 live there because the Hub is a FastAPI service; the client
  venv holds none of the three.
- **M5, run 2026-08-31.** `compute_cost_usd("qwen3.8 27b Mythos", 1000, 1000)` →
  `0.0` / `subscription`; `deepseek_flash` → `0.00088` / `pay_per_use`. Non-zero
  for a local alias ⇒ D3's reasoning needs re-reading.
- **M6:** connect two nodes directly and corrupt one byte of the certificate CN
  in transit — the connection must be refused naming the check that failed. Then
  connect the same two over WebRTC and confirm that **no** `HELLO_CHALLENGE` is
  exchanged. If one is, the table's second row is wrong and the gap does not
  exist.
- **D2, worth running before any code:** ask a peer for an alias that node does
  not serve; the refusal must name the alias. Observed once already in
  production, on the Ubuntu node, 2026-08-31 — as the symptom of a different
  defect.
- **D5, run 2026-08-31.** `grep -niE "quota|rate_limit|max_requests|budget|throttl"`
  over `firewall.py` and `p2p_coordinator.py` returns nothing; the same pattern
  across the client finds `dpc_agent/budget.py` (per agent and provider) and
  `coordinators/discord_coordinator.py` (per user and global). Item 2 of D5 is
  therefore a wiring job. A hit on the compute path ⇒ it is already paid for.
