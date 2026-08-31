---
adr: 041
title: "Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by a proved peer key rather than by an API key — and give every model call one usage row on the node that ran it"
status: accepted
date: 2026-08-31
deciders: [Mike]
consulted: [Ark, Johnny, Warren, CC, Fable 5, GLM 5.3]
informed: []
depends_on: [ADR-040]
related: [ADR-002, ADR-026, ADR-036, ADR-038]
supersedes: []
session: "DPC Project #82–#136, 2026-08-31 — Mike's colleague wants Qwen3.8-27B from Continue in VSCode; three internal review rounds (Ark, Johnny, Warren) and two independent adversarial reviews (Fable 5, GLM 5.3) against the prompt at ideas/dpc-research/adr-041-adversarial-review-prompt-2026-08-31.md"
---

# ADR-041: Serve outside tools an OpenAI-compatible surface from inside the DPC client, gated by a proved peer key rather than by an API key — and give every model call one usage row on the node that ran it

> **Status: accepted, 2026-08-31** — Mike, after three internal review rounds and
> two independent adversarial reviews: «ADR принимаю». The work is filed as the
> epic `AN-OPENAI-COMPATIBLE-SURFACE-IS-THE-PRODUCTS-FIRST-OUTWARD-FACING-DOOR`
> with six children in the shipping order of D4, and the two production defects
> the reviews turned up are filed separately because they do not wait on this.
>
> A recommendation standing on measurements, in the form ADR-040 used. Every claim is marked `Observed` (read in this tree, location
> given), `Inferred`, or `Not verified`.
>
> **Two outside reviews broke four of this document's supports and none of its
> decisions** (`ideas/dpc-research/adr-041-review-fable-5.md`,
> `…-glm5.3.md`, written independently). Where they overturned something the
> text below says so and names the finding; where a figure of ours was an
> estimate it has been replaced by a count. **M4 was false and D6 rested on
> it**, so D6 is re-decided here with the option that was missing. The one
> question that had been left for Mike was D6's vote; it was taken with the
> reviews, for `aiohttp.web`.

## Context and Problem Statement

A colleague of Mike's wants our production model, `qwen3.8 27b Mythos`, from the
Continue plugin in VSCode (#84). Continue speaks the OpenAI HTTP API. Two paths
were proposed: expose a second `llama-server` to a network (Ark, #83), or serve
the OpenAI API from inside DPC and let the request travel our own P2P path
(Ark, #85, at Mike's asking).

Mike then added the requirement that shapes the ledger: usage must be countable
afterwards — «сколько usage и т.д. для аналитики», «если в дальнейшем кто то
захочет монетизировать такой шаринг» (#87–#89); that API-backed models should
be shareable the same way (#102); that money must be counted when a paid
provider is shared (#111); and that **what is shared may not be shared onward**
(#121).

The question this ADR settles is not «can we reach the model». It is **what the
outward-facing surface of this product is**, and whether a thing that consumes
one of our models leaves a record anyone can read afterwards.

## Measurements

### M1 — There is no streaming over the peer path. `Observed`

`p2p_coordinator.request_inference_from_peer` mints one `request_id`, awaits one
`asyncio.Future` through a single `asyncio.wait_for`, and returns `result`
whole. No SSE, no chunk callback, no partial delivery. Found by Johnny (#95),
confirmed by both outside reviewers.

**And streaming is per provider type, not «local», which is narrower than D4's
first wording** (Fable). `Observed`: `LLMManager.query` (`llm_manager.py:585`)
has no streaming form at all; streaming exists as `generate_response_stream` on
`llamacpp_server_provider` and `dpc_agent_provider`, reached through `hasattr`
in `llm_adapter.py`. Ollama has none. So a streaming gateway must either bypass
`LLMManager.query` — and with it the `return_metadata` path the `_est` counts
come from — or `LLMManager` gains a streaming entry point.

**A consequence nobody priced** (GLM): when peer routing arrives, the gateway's
SSE is a *re-chunked buffer* — the consumer's editor renders token by token
what arrived seconds earlier all at once. That is a support ticket forever, and
it belongs in the shipping order rather than in a footnote.

*Also observed:* the peer wait carries a timeout that reaches the await —
`asyncio.wait_for(response_future, timeout=timeout)`, 1200 s by default,
180 s as `llm_adapter` passes it. The peer path is not among the places that
can hang unbounded.

### M2 — Usage crosses the wire; the serving node records it in one place, and that place is wrong. `Observed`

`create_remote_inference_response` (`dpc-protocol/protocol.py:58`) carries
`tokens_used`, `prompt_tokens`, `response_tokens`, `model_max_tokens`,
`thinking_tokens`, `model` and `provider`, and `handle_inference_request` fills
every one from `llm_manager.query(..., return_metadata=True)`. The requester
learns what his call cost in tokens.

- **The counts are ours, not the engine's** — `llm_manager.query` counts them
  centrally for every provider alike (`llm_manager.py:694–698`, GLM checked this
  for an M6-shaped error and found none). The log line names them
  `prompt_tokens_est` / `response_tokens_est` for that reason.
- **The comment this ADR quoted three times is wrong for a paid alias** (Fable
  F5, verified here). `p2p_coordinator.py:221` says a peer's request «appears in
  no cost series». But `deepseek_provider._log_usage` describes its own output
  as *«the one line the burn history is made of»* (docstring, `:187`, naming
  5 406 lines). A peer's call on a DeepSeek serving alias **writes that line** —
  with `conv=-` and no peer field. So the money is already counted, in the
  owner's own burn series, indistinguishable from the owner's spend. **That is
  worse than not counting it**, and D3 must say which series is the record and
  which is derived.

### M3 — A request_id exists end to end and reaches nothing. `Observed`

Minted as `str(uuid.uuid4())`, echoed in the response, used to resolve the
future — and absent from the `Peer inference served` line and from every row.
The identifier exists; what is missing is a destination.

### M4 — **Overturned.** The client already ships an HTTP server, and already runs one. `Observed`

The first writing of this measurement said the core runs no HTTP server, on the
strength of `pyproject.toml` naming only `websockets`. Both outside reviewers
overturned it independently, from two different directions, and both are right —
verified here:

1. **`aiohttp` 3.14.1 is in the client's own venv**, and it arrives through
   `discord.py>=2.7.0`, which is in the **base** `dependencies` list
   (`pyproject.toml:38`) rather than an extra. So **every client install ships a
   full HTTP/1.1 server** — `aiohttp.web`, `StreamResponse`, chunked transfer,
   mid-stream disconnect detection. `uv run python -c "import aiohttp.web"`
   succeeds in the client interpreter.
2. **The core already runs an HTTP server** (GLM). `file_server.py` is a stdlib
   `SimpleHTTPRequestHandler` on a `TCPServer`, constructed at `service.py:188`
   on `127.0.0.1:9998`. It is GET-only and cannot carry `text/event-stream`, so
   it does not solve D6 — but the sentence «the core runs no HTTP server» is
   simply false.
3. `fastapi` is already in the client's **dev** extra (`pyproject.toml:52`), so
   «the idiom is foreign to this client» was true of the runtime and false of
   the repository.

**How the error was made, recorded because the class matters more than the
fact.** This ADR's own falsifier warns that `uv pip list` reads the Hub's
virtualenv because `VIRTUAL_ENV` points there — and then the measurement
verified the **declared** dependencies instead of the **resolved environment**.
GLM's words: the falsifier caught the trap in one direction and walked into it
in the other. The rule this yields: **check what is installed, not what is
declared.**

### M5 — A locally served model has no dollar cost by construction. `Observed`

`compute_cost_usd('qwen3.8 27b Mythos', 1000, 1000)` → `0.0`, billing
`subscription`; `deepseek_flash` → `0.00088`, `pay_per_use`. Reproduced
independently by GLM in the client's environment. Monetising shared *local*
compute cannot read `cost_usd`.

### M6 — Every path is encrypted; only an inbound direct connection proves who is on the other end. `Observed`

| path | encryption | authentication of the peer's identity |
|---|---|---|
| Direct TLS, **inbound** (a peer dials us) | TLS to the peer's self-signed certificate | **three checks, stricter than a CA chain.** `_verify_hello_identity`: cert CN equals the claimed node_id; `SHA256(public key)[:32]` equals it; an RSA-PSS signature over a fresh 32-byte nonce from `HELLO_CHALLENGE`. The identity is self-certifying, so a forged certificate fails check 2 and a stolen public certificate fails check 3 |
| Direct TLS, **outbound** (we dial a peer) | the same TLS | **two string comparisons and no proof.** `_validate_peer_certificate` (`p2p_manager.py:923`) reads the certificate's CN, compares it to the node_id from the URI, and stops; `HELLO_ACK` is then checked against a `node_id` the peer merely states. Nothing asks the far end to prove it holds a key |
| Volunteer relay | end-to-end AES-256-GCM + RSA-OAEP **above** the transport | **recipient and integrity only.** Nothing signs the sender: `grep sign\|signature\|verify` over `transports/relayed_connection.py` and `managers/gossip_manager.py` returns nothing. The certificate *fetch* is sound — `gossip_manager.py:449` derives the node id from the fetched certificate before trusting a `cert:<node_id>` record, so a DHT poisoner cannot substitute a key (Fable F10) |
| Gossip | the same hybrid scheme | the same |
| Hub | signalling only; content never passes through it | — |

**A correction to this table's own correction.** An earlier revision said an
active middle's certificate «is then persisted as the peer's». **It is not**
(GLM): `_persist_peer_certificate` derives the node id from the public key and
refuses — *«Refusing to cache certificate for %s: its public key hashes to %s»*
(`p2p_manager.py:1111`). The store is not poisoned. What is true, and is Fable's
finding: **that function's return value is ignored at the call site**
(`p2p_manager.py:745`), so a mismatch is a log line and the connection
continues. The impersonation on the outbound leg is real; the escalation the
sentence implied is not. Three errors of one shape in this table's short life —
true of the handshake, written as true of the store; true inbound, written of
the path — which is why D2 below is phrased as a property to prove rather than a
path to name.

**The relay tier cannot deliver at all** (found here, confirmed by reading the
whole path by both reviewers). `relay_manager.py:657` forwards with
`"from": from_peer` unchanged; on the destination the message arrives over the
*relay's* connection, so `relay_message_handler.py:102` refuses when
`payload["from"] != sender_node_id` — before the client-mode branch at `:123`
that would have dispatched it correctly. No test names `RELAY_MESSAGE`. The
check conflates «who is on the socket» with «who authored the payload», which is
the distinction ADR-036 exists to make. Consequence for this ADR: **the only
working NAT traversal today is WebRTC**, whose identity is the Hub's word, so
the direct-only rule excludes more of the world than the table suggests.

### M7 — The listener that D2 puts on the internet is unguarded. `Observed`, consequence `Inferred`

Fable's F3, and neither the author nor three internal reviewers saw it.

- `read_message` (`dpc-protocol/protocol.py:391`): `payload_length =
  int(header.decode())` then `await reader.readexactly(payload_length)` — **no
  upper bound**. Ten ASCII digits allow 9 999 999 999 bytes, and `readexactly`
  accumulates them; `asyncio.start_server`'s 64 KiB limit applies to
  `readline`/`readuntil`, not to `readexactly`.
- The server context requires **no client certificate**, so any TCP client that
  completes TLS reaches `read_message` — and that read has no `wait_for`.
- `_is_rate_limited` counts **failed** HELLOs per IP. A client that never
  finishes one never fails one.

`Inferred`, not run: an unauthenticated stranger can hold connections open
indefinitely and make the process allocate up to the length it declares.

### M8 — Peer inference is serialised, and the admitted population is a list, not the roster. `Observed`

Two corrections to figures this ADR and its reviewers put out:

- `p2p_coordinator.py:33` `self._peer_inference_lock = asyncio.Semaphore(1)`,
  held around the query at `:215`. Peer inference on a serving node is
  **serialised**. «No quota» is right about counting and wrong about throttling.
- `can_request_inference` admits `compute.allow_nodes` and
  `compute.allow_groups` — **explicit lists the node owner maintains**, not «any
  roster member», which is what this ADR and Warren both wrote. Warren's own
  correction (#134) puts it best: bounded by a list you have to remember to
  update is bounded by discipline.
- With the serialisation, Fable re-priced the exposure: not $0.88 per thousand
  calls but **$80–170/day** from one grantee at 128 k prompts. `Inferred` — he
  assumed a latency and says so.

## Decision

### D1 — The OpenAI surface is a component of the DPC client, on the consumer's loopback

The gateway lives inside the client, listens on `127.0.0.1` **of the consuming
machine**, and hands requests to the provider layer. Mike settled «whose
loopback» (#101): the consumer's, because the consumer then gets a DPC node
rather than a URL — an identity, a place in a roster, and the ability to share
in turn.

**The reason first given for it was wrong and is replaced.** D1 said «no port is
exposed anywhere». D2 requires the direct path, and direct means the serving
node's 8888 must be reachable from the consumer's network — for a colleague off
the LAN, the public internet (Fable F3). What D1 actually buys is that **the
exposed port is gated by a self-certifying identity instead of a bearer
secret**. That is a real and better property; it is not «no port».

**The null option is rejected on its merits, not on timing.** The first writing
removed it because Mike said the colleague is not in a hurry — a reason to
defer, not to prefer the larger design; both outside reviewers said so. The
honest rejection is that **an API key is a bearer credential**: inherently
re-shareable, unattributed, unrecorded. It therefore fails two of Mike's own
stated requirements — usage countable for analytics (#87–#89) and no onward
sharing (#121) — and no amount of Tailscale fixes that.

**And it remains the right immediate step, which is a different decision**
(Warren #134). Thirty minutes of configuration serves one colleague today; the
gateway is the platform. This ADR decides the platform. Whether to run the
config-level bridge in the meantime is Mike's and is not decided here — he has
said the colleague is not waiting (#96).

**The serving-side gateway is a bet declined, and should be labelled one.** A
gateway component on the *serving* node, loopback-bound and reached over a
tailnet, opens no port either, streams immediately (M1 does not apply), needs no
WebRTC challenge and no consumer-side install. What it does not give is the
consumer's identity, roster place and transitive-compute future. Choosing
against it is a product bet, not a derivation.

### D2 — Authorised on a connection whose peer key has been **proved**

Replaces the earlier phrasing «authorised on the direct TLS path». Two reasons,
both from outside:

- **«Gateway traffic» is not a class the wire can see** (GLM). The serving node
  receives `REMOTE_INFERENCE_REQUEST` over whatever connected the two nodes; a
  request born in Continue and one born in an agent are the same command with
  the same fields, and `handle_inference_request` gets `peer_id` and a payload.
  A rule about «gateway traffic» authorises nothing implementable.
- **«Direct TLS» is not the property being relied on** (Fable). The property is
  that `sender_node_id` was proved, and M6 shows that holds inbound and not
  outbound.

**The rule.** Peer inference — all of it, not a gateway subset — is served only
over a connection on which the peer's key has been proved:

- **inbound** direct TLS proves it today (nonce signature and key hash);
- **outbound** direct TLS proves it as soon as `connect_directly` derives the
  node id from the presented certificate's public key and refuses on mismatch.
  TLS already requires the far end to hold the private key of the certificate it
  presents, so a key-hash check turns possession into identity. **The check
  already exists and its result is discarded**: making `_persist_peer_certificate`'s
  return at `p2p_manager.py:745` a refusal is the whole fix;
- **WebRTC, relay and gossip prove no sender** and are excluded.

This is a **stronger and narrower decision than the one it replaces**: it
restricts all peer compute, not gateway traffic, and it changes today's
behaviour for any peer reached over WebRTC. That is deliberate. Mike removed the
Hub from the trust path (#110) — «Хаб планировали выпиливать, так что пофиг на
него» — and ROADMAP §211 says the same in writing («Hub becomes optional
bootstrap, not architecture center»); *no board entry planning the Hub's removal
was found.*

**The outbound key check is a precondition of this ADR and a defect in
production today.** It is filed separately, at Ark's and Johnny's insistence,
because it should not wait on this decision.

### D3 — One usage row per model call, on the node that ran it, carrying who called

```
request_id, caller, caller_kind (agent|peer|gateway), alias, model,
route (local|peer), prompt/completion/thinking tokens, counts_source (ours|engine),
started_at, duration_s, billing (subscription|pay_per_use), cost_usd
```

`card_seconds` was in the first draft and is deliberately absent: nothing
measures it, and under `kv_unified` with four slots «seconds of card» is not
defined for a single call — two calls overlapping on two slots each occupied the
card for their whole duration and together used it once. `duration_s` is what is
measurable.

**Why the node.** The card is on the node; the vendor key is on the node; an
agent's call, a peer's call and a gateway client's call consume the same node's
resource.

**This is a new ledger, not a migration — the first writing had it backwards.**
Both reviewers, verified here:

- `events.jsonl` is a **task-lifecycle stream**. Usage appears only as a field
  block inside `task_complete`, aggregated over a task's rounds. **There is no
  per-call usage row anywhere today**, so nothing that reads that file reads
  what D3 stores, and **no reader has to change**. `events.jsonl` keeps its
  lifecycle rows and its readers.
- The earlier count of «five read sites» included **two writers**:
  `dpc_agent/events.py:203` and `:255` are both `append_jsonl`. The true reader
  count is **three** — `agent_service.py:459`, `context.py:291` via
  `Memory.read_jsonl_tail`, `tools/core.py:875` — plus the wrapper chain
  `get_agent_tasks` → local API → `AgentTaskBoard.svelte:196`, plus four test
  files. GLM searched the whole tree for a further hidden reader and found none.
- The work the first writing did not count is the other side: **eight sites
  where per-call usage is born**, all in `dpc_agent/llm_adapter.py`
  (`compute_cost_usd` at `:315, :343, :464, :551, :556, :768, :779, :789`), plus
  the peer path at `p2p_coordinator.py:216`, plus the gateway.
- **One consistency rule the design must state:** the sum of a task's per-call
  rows equals the `task_complete.cost_usd` the burn series already carries.
- **And one series must be named the record.** M2 shows a paid peer call already
  lands in the `DeepSeek usage:` line the burn history is built from,
  unattributed. The ledger either replaces that series or is written from the
  same call site; two sources of one number is what this decision exists to
  prevent.

**Money is counted at the moment of the call, and this is a constraint on
*when*, not only on which column.** Mike, #111. A DeepSeek price is not a
function of its tokens: `rates_at` reads the UTC hour against
`PEAK_WINDOWS_UTC` (01:00–04:00, 06:00–10:00, `PEAK_MULTIPLIER = 2.0`), suspends
those windows on a *Beijing* weekend — a calendar spanning three UTC days — and
only from `WEEKEND_OFF_PEAK_FROM = 2026-08-22 16:00 UTC`; `_peak_applies` limits
the rule to DeepSeek, because Z.AI shares the tables and not the clock. The
table has changed twice this month. `pricing.py:177` states the invariant in its
own words: *«a call is priced once, when it is made, and no later repair
reprices the line already written»*. So `cost_usd` and `billing` are written by
the node that made the call, at the time it made it, and are never re-derived.
`compute_cost_usd(..., at=…)` already takes the moment; only the row is missing.

**The guest's copy of the number belongs in v1 of the wire format** (Warren
#120): adding it later is a wire-format change, the one kind of addition old
clients cannot read. The response gains an optional cost field now; whether it
is populated stays policy.

**Attribution, not price.** Johnny's objection (#95), adopted as a correction:
the ledger records that a call happened, whose it was, and what it cost *the
host*. What it is worth to the guest is a policy above it, and M5 is why the two
cannot be one field.

### D4 — Shipping order, and the colleague is served at step 4

Replaces the earlier «narrow first version», which GLM showed contradicted
itself: D1 puts the gateway on the *consumer's* node, D4 deferred peer routing,
and the consumer has no local Mythos — so v1 as written **could not serve the
request that caused this ADR to exist**.

1. **The node ledger** (D3). Additive: usage rows are new, lifecycle rows stay,
   no reader changes. It comes first because D5's quota needs a persistent
   source and because a gateway shipped before it leaves exactly the nothing a
   peer's call leaves today.
2. **The listener hardening** (D8). It precedes any decision that puts 8888 in
   front of a network we do not own.
3. **The gateway serving local aliases**, plus the quota wiring and D5's two
   lists. Streaming here is per provider type (M1), not «local».
4. **Peer-routed gateway — the colleague is served here.** Non-streaming (M1),
   over a proved connection only (D2), attribution keyed to the proved sender
   (D7). The re-chunked-SSE caveat (M1) is stated to the user, not discovered by
   him.
5. **API-backed sharing** (D5), which does not start before its quota exists.

Deferred beyond this list, explicitly: `/v1/embeddings`, which Continue wants
for indexing; streaming over P2P; `/v1/completions` for tab autocomplete
(`Not verified` against Continue's documentation — both reviewers flagged it
from memory).

### D5 — API-backed models are shareable, and the quota is a financial control

Sharing a vendor-backed alias is a different act: **the node holding the key
pays real money.**

1. **Two lists, not one longer list** (Ark #108). Local aliases, whose scarce
   resource is the card and whose refusal is «the card is busy»; vendor aliases,
   whose scarce resource is money and whose refusal is «the budget is spent».
   One list would put two refusal policies behind one setting, and the owner
   could not say «share the GPU freely, the API never».
2. **A per-caller quota, and it is a control rather than wiring** (Warren #134,
   adopted). `firewall.py` and `p2p_coordinator.py` contain no quota, rate limit
   or budget; the shape exists three times elsewhere (`dpc_agent/budget.py` per
   agent and provider, `discord_coordinator.py` per user and globally,
   `relay_manager.py` at 100 msg/s) and is wired to neither the peer nor the
   gateway. **«Does not ship before the quota» is a sentence, and a sentence is
   not a control.** The enforceable form, mirroring the unset-alias refusal
   already at `p2p_coordinator.py:196`: **a vendor-class alias in the serving
   list with no configured quota is a configuration error, refused at load with
   a named reason.**
3. **The quota's durability is a decision, and it is «persistent».** All three
   existing shapes hold their counters in memory and reset on restart. A ceiling
   that resets every boot is abuse damping, not a financial control — so the
   quota reads the node ledger, which makes **D3 a precondition of D5**, not a
   sibling.
4. `cost_usd` for a pay-per-use alias is already computable (M5). The column
   exists; the row does not.

*Corroboration this ADR did not use* (GLM): the open cheque has already
happened — `backlog.md` records that the shared path carried two requests ever
and both were relayed to the paid `deepseek_flash`, which is the measured origin
of ADR-040 D4-0. `Observed` as a board entry; the incident is the board's claim.

### D6 — `aiohttp.web`, declared explicitly

**Re-decided.** The first writing offered two options — hand-written asyncio
HTTP+SSE against `fastapi`+`uvicorn` — and recommended hand-written on the
argument that a client must start on a machine with no server stack. M4 shows
the dichotomy was false: **the machine already has one.**

| option | new wheels | upfront | maintained by |
|---|---|---|---|
| **`aiohttp.web`** | **none** — 3.14.1 ships with every install via `discord.py` | a declaration line in `pyproject.toml` | upstream |
| hand-written HTTP+SSE | none | 500–600 lines with tests (Johnny's estimate, #109) | us, forever |
| `fastapi`+`uvicorn` | three runtime wheels, ASGI lifecycle, pydantic coupling | small | upstream |

Both outside reviewers rank them **`aiohttp.web` > hand-written > fastapi**, and
Warren's TCO reading (#134) agrees. The decisive argument is not the upfront
cost: it is that a hand-written HTTP listener means owning HTTP's long tail for
the life of the product — `Expect: 100-continue` (which curl emits, and people
test with curl), keep-alive semantics, chunked request bodies, partial writes,
malformed requests, request smuggling — each of which is an upstream fix under a
library and a support ticket under ours. M7 is what that looks like on a
protocol we did write ourselves.

**Declared, not inherited.** Relying on a transitive presence is fragile: if
`discord.py` ever leaves, the gateway leaves with it. One line in the base
dependencies.

**Decided** with the reviews rather than against them, and voted by Mike on
2026-08-31 — «A (aiohttp)»: `aiohttp.web`, with `aiohttp` declared in the base
dependencies so the gateway does not hang on `discord.py` staying.

### D7 — What is shared may not be shared onward

Mike, #121. Sharing is a permission between two nodes and does not travel.

**The prior record** is a research finding, not a task:
`ideas/cc-mike-research/2026-04-15/named-unsolved-problems.md`, Finding 16,
«Consent propagation on re-sharing» — *«Firewall rules are pairwise… at mesh,
re-sharing is the dominant mode. Without propagation constraints, "shared with
150" really means "shared with the 150's 150s" = transitively public.»* Named in
April, never filed.

**The compute version is unprevented today.** `remote_peer` is a provider type
taking a `peer_id` and a remote alias; `compute.serving_alias` is validated for
*existence* only and nothing looks at its **type**. So B can point a
`remote_peer` alias at A, designate it as what B serves, and C reaches A through
B with A's ledger recording `caller = B`.

**Part 1 — refuse to serve from an alias that is itself remote.** A
`serving_alias` whose provider type is `remote_peer` or `dpc_agent` is a
configuration error, refused at load with a named reason. Cheap, complete
against accident, testable. **And the same type rule must extend to gateway
routes** (GLM): part 1 guards the P2P door, and the day peer routing lifts,
B's loopback gateway → B's `remote_peer` alias → A re-shares through the HTTP
door while part 1 watches the other one. A second reason for part 1 (Fable):
two nodes each serving from a `remote_peer` alias pointed at the other recurse
under their own `Semaphore(1)` until the 1200 s timeout — a distributed
deadlock.

**Part 2 — a request may declare whom it acts for, and the declaration may only
refuse, never subsidise.** A node declaring a third party is refused unless that
party is itself allowed. **Attribution and quota always bind to the *proved
sender*.** GLM found the hole the naive reading opens: if «allowed» meant the
declared party's allowance is spent and the row attributed to him, a modified
client would declare a large-quota member and spend the victim's quota under the
victim's name — both instruments defeated by the honesty mechanism itself.

**The impossibility claim, in its strongest form.** Both reviewers were asked to
break it and both failed, and both independently sharpened it. On a connection
where the sender's identity is self-certifying and possession-proved, whatever B
sends, B sends under B's key. Therefore **with attribution and quota keyed to
the proved sender, re-sharing reduces to B donating his own quota and wearing
his own attribution** — it cannot increase A's exposure beyond what A granted B.
That is a design invariant, not a hope, and it rests on exactly two things this
ADR controls: D2's proof requirement actually enforced, and part 2's
sender-keyed rule above. The escapes that exist are ours to close: unproved
transports, declaration-keyed instruments, automatic roster admission.

Attestation was considered and rejected: it would raise «modified client» to
«modified client plus defeated attestation», which is DRM this product has no
business paying for, and it fails on commodity hardware anyway. Watermarking
detects re-sharing after the fact and prevents nothing.

**What must not be claimed:** that re-sharing is prevented. Part 1 prevents it in
our client; against a modified peer the instruments are attribution and quotas,
and they are sufficient only in the sense above.

### D8 — Harden the DPTP listener before putting it in front of a network we do not own

New, from M7, and it precedes D1 in the shipping order rather than following it.

- A maximum frame size in `read_message`, refused rather than allocated.
- A `wait_for` around the HELLO read.
- Pre-HELLO connections counted per IP, not only failed HELLOs.

Each is small and testable, and each guards the one port this design puts on the
open internet. The gateway's own loopback listener is the small end of the
exposure; 8888 is the large one.

## Consequences

- **The WebRTC challenge precedes wide deployment.** If peer inference is served
  only over a proved connection, a consumer behind a NAT that only WebRTC could
  traverse has no service at all — and M6 shows the relay tier cannot deliver
  today, so WebRTC is the only traversal there is. For one colleague on a
  reachable network this is no obstacle; for anything wider it is the blocking
  item.
- **D2 changes existing behaviour**, not only future behaviour: peers reached
  over WebRTC stop being served compute.
- **A declared dependency** on `aiohttp` (D6) — no new wheel, but a new promise.
- **A new ledger and one reconciliation rule** (D3); no reader migration.
- **The scheduling externality nobody had priced** (GLM). Peer inference is
  serialised under one lock held across the whole call (M8), the caller's
  default patience is 1200 s, and the owner's agents compete for the same four
  slots with no priority. The 3 a.m. event is not «the server crashed»; it is
  «someone else's editor hung for twenty minutes holding the card». ADR-040's
  ONE-CARD-ONE-OWNER covers VRAM and is silent on scheduling; scheduling is what
  pages you.
- **Operational responsibility has an owner or it has none** (Warren #134). This
  ADR names the requirement and not the person: whoever owns the serving node
  carries it, and at n=1 the service level is «serialised behind the owner's
  agents, no alerting, best effort». Written down so that it is a statement
  rather than an omission.
- **The consumer is not charged in v1, and that is a decision** (Warren #134,
  GLM). Free is the default that ships, and a free default becomes a precedent;
  the ledger is built so a price can be introduced without a schema change. At
  more than one consumer the quota becomes the only scarcity instrument and the
  owner's card-time is the subsidy.
- **A standing compatibility obligation** to tools we do not control, against an
  upstream API that moves.
- **`_est` token counts become a published number.** Hence `counts_source`.

## Open Questions

None. D6 was the last, and the two outside reviews decided it: `aiohttp.web`,
declared in the base dependencies.

## Falsifiers

- **M1:** grep the peer path for a chunk callback or an `AsyncIterator`. One
  exists ⇒ D4 step 4 is bigger than written.
- **M2:** run one peer inference on a DeepSeek serving alias, then grep the log
  for `DeepSeek usage:` — the line must be there with `conv=-`. Absent ⇒ F5 is
  wrong and the code comment stands.
- **M3:** grep the log for that call's `request_id` — it must appear only in the
  requester's debug line.
- **M4, run 2026-08-31, and the obvious command is a trap in both directions.**
  `uv run --directory dpc-client/core python -c "import aiohttp.web"` must
  succeed — it does, 3.14.1 — and `uv pip list` will separately mislead you with
  the Hub's virtualenv, because `VIRTUAL_ENV` points there. Ask the client's own
  interpreter, and ask it about the *installed* set.
- **M5, run 2026-08-31.** `0.0` / `subscription` for the local alias,
  `0.00088` / `pay_per_use` for `deepseek_flash`.
- **M6:** corrupt one byte of the CN in transit on an inbound connection — it
  must be refused naming the check. Then confirm no `HELLO_CHALLENGE` is
  exchanged over WebRTC. And confirm `_persist_peer_certificate` refuses a
  mismatched key while the connection survives — that pair is the defect.
- **M7:** send a 10-digit length header to 8888 from a non-DPC client and watch
  the process's memory. Not run.
- **M8:** two peers request inference simultaneously; the second must wait for
  the first. Not run.
- **D2:** ask a peer for an alias the node does not serve — the refusal must name
  the alias (observed in production 2026-08-31, as the symptom of another
  defect). Then, after the fix: dial a node presenting a certificate whose key
  does not hash to the expected id — the connection must be refused, not logged.
- **D5:** put a vendor-class alias in the serving list with no quota configured;
  the service must refuse at load.
- **D7, the enforceable half:** point `compute.serving_alias` at a `remote_peer`
  alias and start the service — it must refuse at load, naming the type. Today
  it starts.
- **D7, the unenforceable half:** there is no check distinguishing an honest
  peer from one proxying for a third party, and there cannot be. The falsifier
  for «we prevent re-sharing» is that it must never be written.
- **D8:** each of the three guards has its own red-before-green test; the frame
  cap is the one that must refuse rather than truncate.
