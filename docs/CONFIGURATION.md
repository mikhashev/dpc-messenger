# DPC-Client Configuration Guide

> **Version:** 0.29.0
> **Last Updated:** 2026-08-10 — reconciled against `settings.py`; the key
> reference below that date line is generated, not hand-maintained

## Overview

DPC-Client now supports flexible configuration via both configuration files and environment variables. This guide explains all available configuration options.

---

## Configuration Hierarchy

Configuration values are resolved in this order (highest priority first):

1. **Environment Variables** (e.g., `DPC_HUB_URL`)
2. **Config File** (`~/.dpc/config.ini`)
3. **Built-in Defaults**

---

## Configuration File Location

The main configuration file is located at:

```
~/.dpc/config.ini
```

**On Windows:** `C:\Users\<username>\.dpc\config.ini`
**On Linux/Mac:** `/home/<username>/.dpc/config.ini`

---

## Configuration File Naming (Updated v0.6+)

**Current filenames:**
- `privacy_rules.json` - Firewall access control (previously `.dpc_access.json`)
- `providers.json` - AI provider settings (previously `providers.toml`)

**There is no automatic migration, and the old names no longer work.** The filenames
are hardcoded (`service.py`: `PRIVACY_RULES = "privacy_rules.json"`,
`PROVIDERS_CONFIG = "providers.json"`) with no fallback, and nothing in the client
reads `.dpc_access.json` or `providers.toml` — there is no TOML parser in the
dependency tree at all.

**What happens if you still have the old files:** the client does not see them. Missing
`privacy_rules.json` is not an error — the firewall writes a fresh default file instead,
so a pre-v0.6 `.dpc_access.json` full of access rules becomes **silently inert** and
your firewall runs wide-open defaults without saying so.

**Rename them yourself before first run:**
```bash
mv ~/.dpc/.dpc_access.json ~/.dpc/privacy_rules.json
# providers.toml has no automatic equivalent: re-enter the providers in the UI,
# or hand-write ~/.dpc/providers.json (see providers.example.json in the repo).
```

---

## Default Configuration

On first run the client writes **24 sections and 147 keys** into `~/.dpc/config.ini`.
The four below are the ones most people touch; the rest are in
[the complete reference](#complete-reference-every-key-the-code-writes), which is
generated from the code rather than maintained by hand.

```ini
[hub]
url = http://localhost:8000
auto_connect = false

[oauth]
callback_port = 8080
callback_host = 127.0.0.1

[p2p]
listen_port = 8888
listen_host = dual

[api]
port = 9999
host = 127.0.0.1
```

---

## Configuration Options

### Hub Settings (`[hub]`)

#### `url`
- **Description:** The URL of your DPC Federation Hub
- **Default:** `http://localhost:8000`
- **Environment Variable:** `DPC_HUB_URL`
- **Examples:**
  ```bash
  # Local development
  url = http://localhost:8000

  # Production hub
  url = https://hub.example.com

  # Staging environment
  url = https://staging-hub.example.com
  ```

#### `auto_connect`
- **Description:** Automatically connect to Hub on startup
- **Default:** `false` — the client starts offline and you click a login button
- **Environment Variable:** `DPC_HUB_AUTO_CONNECT`
- **Valid Values:** `true`, `false`, `yes`, `no`, `1`, `0`

---

### OAuth Settings (`[oauth]`)

#### `callback_port`
- **Description:** Port for OAuth callback server
- **Default:** `8080`
- **Environment Variable:** `DPC_OAUTH_CALLBACK_PORT`
- **Note:** Must be available on your system

#### `callback_host`
- **Description:** Host address for OAuth callback server
- **Default:** `127.0.0.1`
- **Environment Variable:** `DPC_OAUTH_CALLBACK_HOST`
- **Common Values:** `127.0.0.1`, `localhost`, `0.0.0.0`

---

### P2P Settings (`[p2p]`)

#### `listen_port`
- **Description:** Port for direct TLS P2P connections
- **Default:** `8888`
- **Environment Variable:** `DPC_P2P_LISTEN_PORT`
- **Note:** Must be open in your firewall for incoming connections

#### `listen_host`
- **Description:** Host address to bind P2P server
- **Default:** `dual` — dual-stack, binds both IPv4 and IPv6
- **Environment Variable:** `DPC_P2P_LISTEN_HOST`
- **Common Values:**
  - `dual` - both stacks (the default; IPv6 direct is connection Priority 1)
  - `0.0.0.0` - IPv4 only. Setting this **disables IPv6 direct connections**
  - `127.0.0.1` - Local connections only
  - Specific IP - Bind to specific interface

---

### API Settings (`[api]`)

#### `port`
- **Description:** Port for local WebSocket API (UI ↔ Core)
- **Default:** `9999`
- **Environment Variable:** `DPC_API_PORT`
- **Note:** Used by Tauri UI to communicate with Core Service

#### `host`
- **Description:** Host address for local API server
- **Default:** `127.0.0.1`
- **Environment Variable:** `DPC_API_HOST`

---

### Gateway Settings (`[gateway]`)

The gateway (ADR-041 D1): a second loopback listener that serves `GET /v1/models`,
`POST /v1/chat/completions` (the OpenAI form) and `POST /v1/messages` (the Anthropic
Messages form) to tools on this machine — an IDE plugin such as Continue, Claude Code,
a CLI, a script — from the aliases this node names in `privacy_rules.json`. Off by
default: a new open port is opt-in.

#### `enabled`
- **Description:** Start the gateway with the client
- **Default:** `false`
- **Environment Variable:** `DPC_GATEWAY_ENABLED`

#### `port`
- **Description:** Port the gateway listens on. 9998 is the file server and 9999 the
  local API, so the three loopback listeners are neighbours
- **Default:** `9997`
- **Environment Variable:** `DPC_GATEWAY_PORT`

#### `host`
- **Description:** Read, not chosen: the gateway listens on `127.0.0.1` only, and any
  other value is refused at start with a message naming it (ADR-041 D1)
- **Default:** `127.0.0.1`

**The key.** The first start writes `~/.dpc/.gateway_key` (`secrets.token_urlsafe(32)`)
and never rewrites it: a tool keeps the key in its own config, so a key that changed on
every restart would break it on every restart. Every request carries it as
`Authorization: Bearer <key>`; a request without it, or with a different one, is
answered `401`. **Rotation is the `rotate_gateway_key` command** (the Rotate button on
the Inference Sharing tab, or the local API directly): a new key is written over the
file and swapped into the running listener, so the old key is answered `401` from the
next request on with no restart, and the new one is returned once in clear to paste into
the tool's config. With the listener off the file is still rewritten. The file is
written to a temporary name in the same directory and moved into place, so a client
reading it mid-rotation gets one key or the other and never an empty string. On
Linux/macOS the mode is `0600`; on Windows the mode bits are advisory and the file
inherits the ACL of your home directory, as `.ws_token` does.
The Anthropic form's clients send the same key as `x-api-key: <key>` instead; both
header forms open every route.

**What crosses, and what is refused by name.** Besides the conversation and `tools`, the
gateway carries two more things the peer wire under it already had (ADR-041 D4, amendment
2026-09-14):

- **Reasoning effort.** OpenAI form: `reasoning_effort`. Messages form:
  `output_config.effort`, and `thinking: {"type": "disabled"}`, which is `off`; `enabled`
  and `adaptive` name no depth and ask for the alias's own default, and `budget_tokens` is
  not read. The words are `off, low, medium, high, max` (`xhigh` is read as `high`), and
  where a model's own template named its rungs those are the words that alias knows — a
  word reaching none of them is `400` listing them. The usage row's `served_effort` names
  the rung the call ran on.
- **Images.** An OpenAI `image_url` part carrying a `data:<mime>;base64,<payload>` URL, or
  an Anthropic `image` block with a `{"type": "base64", "media_type", "data"}` source.
  They travel beside the prompt, so their position among the turns is not kept. Refused by
  name: an `http(s)` URL or a `url` source (the gateway fetches nothing from the web); an
  image past `[vision] max_image_size_mb`, answered `413`; tools sent beside an image; an
  alias or a peer that says it has no vision path.

Sampling — `max_tokens`, `temperature`, `top_p`, `stop_sequences` — stays the alias
owner's configuration on this node and is not read from the request.

**Two switches, one door.** `[gateway] enabled` above is not the only one: `compute.enabled`
in `privacy_rules.json` governs **both** of this node's doors — the peer door it has always
governed, and this loopback gateway (Mike's call, 2026-09-13). The table between them is AND.
`compute.enabled` is about what this node **gives**, never about what it may **ask**: a
`remote:<peer>:<alias>` row is the peer's door, guarded by the peer's own flag, so a node that
shares nothing still reaches its peers through its own gateway (Mike's call, 2026-09-14):

| `compute.enabled` | `[gateway] enabled` | This node's own aliases | `remote:<peer>:<alias>` rows |
|---|---|---|---|
| `true` | `true` | **Open.** `/v1/models` lists the two serving lists; completions are served | **Open.** Listed and called, one row per proved, connected peer |
| `true` | `false` | **Shut.** No listener at all; the peer door stays open | **Shut.** No listener to ask through |
| `false` | `true` | **Shut.** The listener runs, `/v1/models` lists none of them, and a local or vendor completion is `404` naming `compute.enabled`; the peer door is shut too | **Open.** Listed and called as above — what a peer serves is the peer's to refuse |
| `false` | `false` | **Shut.** Neither door serves anything | **Shut.** No listener to ask through |

The flag is read from the live firewall on every request, so turning sharing off in the UI
(or editing `privacy_rules.json` and reloading) closes the gateway on the next request
without restarting the client — and turning it back on reopens it the same way.

**What it serves.** Only the aliases in the two serving lists of `privacy_rules.json`;
an alias outside them is `404`, and the gateway never falls back to `default_provider`:

```json
"compute": {
  "serving_local": ["ollama_local"],
  "serving_vendor": ["ds_flash"],
  "vendor_quotas": {"ds_flash": 2.0}
}
```

- `serving_local` — aliases whose provider runs on this machine (`ollama`,
  `llamacpp_server`, `local_whisper`). The card is the scarce resource: a request queues
  behind peer inference on the same lock, and one that would wait longer than
  `[connection] remote_inference_timeout` is answered `503` (the card is busy). The
  first entry is also what the P2P door serves peers from; the older `serving_alias`
  key is still read and folded into this list with a warning, and a file carrying both
  keys with different values is refused at load. A `local_whisper` alias belongs in this
  list — that is how the P2P door offers transcription to a peer who holds the
  permission — but it transcribes and does not chat, so the gateway leaves it off
  `/v1/models` and answers a completion addressed to it `404` saying so. The same
  applies to a peer's `local_whisper` row: it is not listed as `remote:<peer>:<alias>`.
- `serving_vendor` — aliases whose provider is a paid API (`anthropic`, `deepseek`,
  `zai`, `openai_compatible`, `gemini`, `github_models`, `gigachat`). Money is the
  scarce resource, so **every entry needs a ceiling in `vendor_quotas`** — USD per UTC
  calendar day, per caller; a vendor alias without one is a configuration error refused
  at load with a message naming it (ADR-041 D5). The day's spend is read from the node
  ledger (`~/.dpc/ledger/`), so it survives a restart; at or over the ceiling the
  gateway answers `429` naming the alias, the ceiling and the spend.
- A `remote_peer` or `dpc_agent` alias may stand in neither list: what is shared is not
  shared onward (ADR-041 D7).

**What it costs a peer.** The owner sets a price for what this node serves; the same block
holds it (ADR-041 D3, amendment). The peer door applies it, the loopback gateway does not
charge its own machine:

```json
"compute": {
  "currency": "RUB",
  "serving_tariff": {"ollama_local": [{"from": "2026-09-01", "in": 20, "out": 60}]},
  "free_nodes": ["dpc-node-alice-123"],
  "free_groups": ["friends"]
}
```

- `currency` — the ISO 4217 code the rates below are in, checked against the standard's
  list. Unset means no tariff is declared whatever `serving_tariff` says, and every served
  call is a gift.
- `serving_tariff` — per alias, dated entries `{from, in, out}` in that currency per 1M
  prompt and per 1M output tokens. The newest entry whose `from` is on or before the call's
  UTC day applies; an alias with no entry is a gift. Reasoning is billable output at `out`.
  A rate that is negative, non-finite (`NaN` and `Infinity` are JSON literals) or malformed
  is refused at load and at save, naming the alias and the field.
- `free_nodes` / `free_groups` — which of the peers already allowed are served at zero.
  Every entry must also be in `allow_nodes` / `allow_groups`: the allow lists are the door,
  and a free list only distinguishes among those already through it. Inside a declared
  tariff a free peer gets a rate of `0` — a price; with nothing declared it gets the same
  gift as everyone else.
- Each served call leaves the applied rates, their currency, the dated entry and the
  amount on the host's usage row (`tariff_in`, `tariff_out`, `tariff_currency`,
  `tariff_at`, `tariff_amount`) and sends the same group to the guest, whose row copies it
  and keeps `cost_usd` null. `cost_usd` is only ever what a call cost the node that ran it.
  Resetting the rules to defaults rewrites the block and drops the tariff with it.

**Reading the door from the UI.** Four commands on the local API answer for the gateway
itself. `get_gateway_state` says whether it is enabled in `config.ini` and whether a
listener is actually holding the port — the two differ whenever the door refused to open
— with the port, the bind, the key masked (`sk-…abcd`), the key file, the two serving
lists, the refusal that stopped them being classified where there is one, and
`compute.enabled`. `rotate_gateway_key` is the rotation described above.
`get_gateway_client_lines` returns the paste-ready configuration for Continue, Cursor,
Claude Code and curl with the key in clear — the two examples on this page are that
command's own output, compared by a test so the page and the button cannot drift.
`get_peer_provider_menu(peer_id)` returns the rows a named peer would be sent in
`PROVIDERS_RESPONSE`, from the same function that sends them, with `known`, `connected`,
`allowed` and, where the list is empty, the reason in words. Beside them,
`validate_firewall_rules(rules)` checks a rules object and names what is wrong without
saving anything.

**Where they appear.** The Inference Sharing tab of the firewall dialog opens with one
sentence from `get_gateway_state` and `compute.enabled` together — the AND table above,
said in words — and carries the door as block «5. IDE door» (address, configured,
listening, masked key, serving lists, the Rotate button behind a confirm that names the
`401`, and the client blocks collapsed behind the masked key) and the preview as block
«6. What a peer sees», a picker over the peers the application already has a name for,
connected first. Its Validate button sends `validate_firewall_rules` the exact object Save
would write — the whole draft the dialog holds, an edit on any other tab included — not a
narrower copy built from the file on disk.

**Reading the rows back.** Two commands on the local API read the node ledger.
`get_usage_summary` is the owner's burn — every row this node ran itself, folded by
caller, alias and month. `get_inference_usage` reads the same rows by role and answers
with three series: `served`, what this node ran for peers, by the peer that asked and by
the alias that answered, carrying its own `cost_usd` and what it is owed per currency;
`consumed`, what peers ran for it, keyed `remote:<host node id>:<alias>`, where `cost_usd`
is null by construction and the money is `tariff_amount`, what this node owes; and `own`,
its own calls on its own key, neither side of a sharing. Both take optional `since` /
`until` ISO datetime bounds, and `get_inference_usage` a `month` of `YYYY-MM` to read one
partition. A tariff that applied over counts nobody could price (`tariff_unpriceable`) and
a call with no tariff declared at all (`untariffed`) are counted apart from the money and
never added into it as a zero. The Inference Sharing tab of the firewall dialog reads
`get_inference_usage` for the current month and shows the three series as three lists.

**Shape and limits.** `model` in a request is the alias; `/v1/models` lists the aliases
with `owned_by` `local` or `vendor`. `stream: true` yields the text as it is made,
followed by `data: [DONE]`; where the answer arrives whole — an image on either route, a
provider or a peer host with no streaming path — it is one chunk, as this door wrote for
everything before 2026-09-14. Every
completion leaves one usage row with `caller_kind = gateway` in the node ledger. A
request whose `Host` header is neither `127.0.0.1:<port>` nor `localhost:<port>` is
answered `400`.

**Example** (Continue, `config.json`):
```json
{
  "models": [{
    "title": "DPC ollama_local",
    "provider": "openai",
    "apiBase": "http://127.0.0.1:9997/v1",
    "apiKey": "<contents of ~/.dpc/.gateway_key>",
    "model": "ollama_local"
  }]
}
```

**The Anthropic Messages form.** `POST /v1/messages` takes the request as the Anthropic
SDKs and Claude Code send it — `model` is the alias, `system` a string or text blocks,
`messages` the user/assistant turns — and answers with one `message` object holding one
text block; an error is the Anthropic envelope (`not_found_error` for an alias outside
the lists, `rate_limit_error` for a spent vendor ceiling, `api_error` for a provider
failure). **Tools cross the door** (since 2026-09-14, on a local alias): `tools` in
either form reach the model, a call comes back as a `tool_use` block with
`stop_reason: "tool_use"` (OpenAI form: `tool_calls` with `finish_reason: "tool_calls"`),
and the next request carrying `tool_result` (OpenAI: `role: "tool"`) completes the round
trip. Forcing is not available — `tool_choice` `any` / `tool` / `required` and
`parallel_tool_calls: false` are refused with 400, because every provider here runs
`auto`. A peer alias (`remote:<node>:<alias>`) carries tools too since 2026-09-14
(DPTP v1.7), and is refused with 400 only when the peer's own menu row does not say
`supports_tools` — its host refuses the same request on the wire, so the refusal here
merely saves the round trip. **The stream is real** on either route:
`stream: true` yields text deltas as they are produced, with the cumulative
usage in `message_delta` (OpenAI: the usage-only chunk under `stream_options.include_usage`)
equal to the usage row of the same request; a tool call arrives as one block at the end,
and a provider or a host that hands the answer back whole still yields one delta. `max_tokens`, `temperature`, `thinking` and the rest are
accepted and ignored: sampling is the alias's own configuration on this node.

**Example** (Claude Code, environment):
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9997
export ANTHROPIC_API_KEY=<contents of ~/.dpc/.gateway_key>
export ANTHROPIC_MODEL=ollama_local        # the alias name, as in /v1/models
```
`ANTHROPIC_AUTH_TOKEN=<key>` (sent as `Authorization: Bearer`) works in place of
`ANTHROPIC_API_KEY`. Not verified against a live Claude Code run at the time of writing;
the shape is verified by the test suite.

**A peer's model, through your own gateway.** After the two local lists, `/v1/models`
shows one row per alias each connected peer serves to this node, named
`remote:<node_id>:<alias>` and owned by that node — the same form a peer's provider has
everywhere else on this node. A completion on such a name travels the P2P path to that
peer (`REMOTE_INFERENCE_REQUEST`) and is served only while the peer is connected over
direct TLS, where its key has been proved (ADR-041 D2); a peer reached over WebRTC, a
relay or gossip is answered `503` naming the rule, never served from a local alias
instead. The peer's own firewall decides what it serves you (`404` when the alias is
not on its menu, `502` carrying the peer's refusal), the peer's card and quota bound
the call — this node's card lock and `vendor_quotas` are not consulted — and a peer
that does not answer within `[connection] remote_inference_timeout` is `504`. The row
this node writes says `route = peer` under the request id both nodes share, with the
peer's token counts and its price copied when it sent them and `cost_usd` left null
when it did not: this node did not run the call and does not price it. **The
conversation, its tools and the stream all cross** since 2026-09-14 (DPTP v1.7): the
turns travel un-flattened, `tools` reach the host's model, and `stream: true` brings
REMOTE_INFERENCE_CHUNK frames back as the host makes the answer. The counts and the
usage row are still built from the response that ends the stream — nothing is counted
off a delta — so a stream cut before that response leaves this node no row at all while
the host keeps its own. A host too old to know these fields ignores them and answers
whole, and your editor then shows the whole reply as one chunk the moment it lands,
which is what every peer-routed answer did before that date. A request too large for one
64 MiB DPTP frame — tools plus a long conversation can reach it — is `413` before a byte
leaves this node.

**What a host sees, what a guest gets.** Sharing compute is trusting the host as a
person, not only as a machine (Mike's call, 2026-09-14; ADR-041 D7, amendment
2026-09-14).

- The host's machine receives the prompt in plaintext by construction — decryption
  happens *at* the host, whichever encrypted path (direct TLS, WebRTC/DTLS, or the
  relay/gossip hybrid scheme) carried it — and the host can read it if they choose.
  This application shows, stores and logs none of it: `handle_inference_request`
  logs only the peer id, the request id and `images: yes/no`, and the usage row above
  carries counts, duration, served effort, tariff and proof — never prompt or answer
  text. Checked on the engine side too (Zcode, 2026-09-14, one machine, default
  verbosity): `llama-server` and Ollama write counters and timings, not content,
  unless a host turns on `-v` or the engine's own request-body logging, neither of
  which DPC turns on by default — and this application cannot prove to a guest that a
  host has not. Two-sided: the node whose model you call sees your prompt in full;
  serving peers means their prompts arrive on your machine and you could read them.
- A host's own model settings — weights, quantization, template, context, sampling,
  output ceiling — are what a served call runs at; there is no per-guest override and
  no `max_tokens` on the wire. The one exception is reasoning effort, the caller's
  preference under the host's own cap (above). A guest's protection against an
  unbounded reply is the declared tariff and the host's ceiling, not a number it
  sends.
- A guest should see the host's full effective settings, even the ones it cannot
  change, before choosing to route a request there — a future menu card's job; this
  paragraph states only the principle.

---

### System Settings (`[system]`)

#### `auto_collect_device_info`
- **Description:** Automatically collect device context on startup
- **Default:** `true`
- **Environment Variable:** `DPC_AUTO_COLLECT_DEVICE_INFO`
- **Valid Values:** `true`, `false`, `yes`, `no`, `1`, `0`
- **Note:** Generates `~/.dpc/device_context.json` with hardware/software specifications

#### `collect_hardware_specs`
- **Description:** Include hardware details (CPU, RAM, GPU, storage) in device context
- **Default:** `true`
- **Environment Variable:** `DPC_COLLECT_HARDWARE_SPECS`
- **Valid Values:** `true`, `false`
- **Requires:** `auto_collect_device_info = true`
- **Privacy:** Hardware specs use privacy-rounded tiers (e.g., "32GB" instead of "31.8GB")

#### `collect_dev_tools`
- **Description:** Include development tools and package managers in device context
- **Default:** `true`
- **Environment Variable:** `DPC_COLLECT_DEV_TOOLS`
- **Valid Values:** `true`, `false`
- **Collects:** Git, Docker, Node, npm, Python, Rust, package managers (pip, poetry, npm, etc.)

#### `collect_ai_models`
- **Description:** Include installed AI models (e.g., Ollama models) in device context
- **Default:** `false` (opt-in for privacy)
- **Environment Variable:** `DPC_COLLECT_AI_MODELS`
- **Valid Values:** `true`, `false`
- **Privacy Note:** Disabled by default. Enable only if you want to share compute resources with peers.

**Example Configuration:**
```ini
[system]
auto_collect_device_info = true
collect_hardware_specs = true
collect_dev_tools = true
collect_ai_models = false  # Opt-in only
```

**Device Context Schema:**

As of schema version **1.1**, device context includes a `special_instructions` block that provides AI systems with interpretation guidelines, privacy rules, and usage scenarios. See [DEVICE_CONTEXT_SPEC.md](DEVICE_CONTEXT_SPEC.md) for complete specification.

**Special Instructions Block:**
- **Interpretation rules:** How to map GPU specs to model capabilities, CUDA version compatibility
- **Privacy rules:** Which fields to filter (executable paths), what to share by default
- **Update protocol:** Auto-refresh behavior, staleness detection (>7 days old)
- **Usage scenarios:** Local vs remote inference, dev environment detection, cross-platform commands

**Example Use Cases:**
- AI can recommend "Your RTX 3060 (12GB) can run llama3:13b" without asking about hardware
- Platform-specific commands: "Windows detected → use winget" vs "Linux → use apt"
- Privacy-safe sharing: Only OS version and dev tools shared by default, hardware requires firewall rules
- Staleness detection: If context is >7 days old, AI suggests restarting client to refresh

---

## Complete Reference: every key the code writes

<!-- BEGIN GENERATED CONFIG REFERENCE -->

Every section and key `_create_default_config` writes into a fresh `~/.dpc/config.ini`: **26 sections, 155 keys**. Generated from `settings.py` by `tools/config_reference.py` — edit the code, then re-run it; do not hand-edit between the markers.

An empty default means the key is written blank and the feature stays off until you fill it in. Every key also accepts an environment variable named `DPC_<SECTION>_<KEY>` in upper case.

#### `[agent_telegram]`

| Key | Default | Notes |
|---|---|---|
| `auto_link_on_create` | `false` | Auto-link Telegram chat when creating agents |
| `require_confirmation` | `true` | Require user confirmation before linking |
| `default_enabled` | `false` | Default telegram_enabled state for new agents |

#### `[api]`

| Key | Default | Notes |
|---|---|---|
| `port` | `9999` |  |
| `host` | `127.0.0.1` |  |

#### `[connection]`

| Key | Default | Notes |
|---|---|---|
| `enable_ipv6` | `true` | Try IPv6 direct connections (Priority 1) |
| `enable_ipv4` | `true` | Try IPv4 direct connections (Priority 2) |
| `enable_hub_webrtc` | `false` | Try Hub WebRTC with STUN/TURN (Priority 3) - requires Hub connection |
| `enable_hole_punching` | `false` | Try DHT-coordinated UDP hole punching (Priority 4) - DISABLED: lacks DTLS encryption (v0.10.0) |
| `enable_relays` | `true` | Try volunteer relay nodes (Priority 5) |
| `enable_gossip` | `true` | Use gossip store-and-forward fallback (Priority 6) |
| `ipv6_timeout` | `60` | Includes 30s pre-flight check + 30s SSL handshake |
| `ipv4_timeout` | `60` | Includes 30s pre-flight check + 30s SSL handshake |
| `webrtc_timeout` | `30` |  |
| `hole_punch_timeout` | `15` |  |
| `relay_timeout` | `20` |  |
| `gossip_timeout` | `5` | How long to wait before falling back to gossip |
| `remote_inference_timeout` | `1200` |  |
| `hello_timeout` | `10` | Seconds the listener waits for HELLO after its challenge |
| `max_pending_hellos_per_ip` | `8` | Inbound connections one address may hold before HELLO_ACK |

#### `[conversations]`

| Key | Default | Notes |
|---|---|---|
| `default_persist_p2p_history` | `false` | Persist P2P chat history by default (false = ephemeral, synced from peer) |
| `default_persist_telegram_history` | `true` | Persist Telegram chat history by default |
| `storage_version` | `2` | Storage schema version (1 = legacy groups/, 2 = unified conversations/) |

#### `[dht]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable DHT peer discovery |
| `port` | `8889` | UDP port for DHT RPCs (TLS port + 1) |
| `k` | `20` | Kademlia k parameter (nodes per bucket) |
| `alpha` | `3` | Parallelism factor for iterative lookups |
| `bootstrap_timeout` | `30` | Bootstrap timeout in seconds |
| `lookup_timeout` | `10` | Lookup timeout in seconds |
| `bucket_refresh_interval` | `3600` | Bucket refresh interval (1 hour) |
| `announce_interval` | `3600` | Re-announce interval (1 hour) |
| `seed_nodes` | *(empty)* | Comma-separated list of seed nodes (ip:port) |

#### `[dpc_agent]`

| Key | Default | Notes |
|---|---|---|
| `budget_usd` | `50` | Maximum budget per task in USD |
| `max_rounds` | `200` | Maximum LLM rounds before stopping |
| `context_window` | `200000` | Agent context window size (tokens) |
| `enable_task_queue` | `true` | Enable background task scheduling |
| `billing_model` | `subscription` | 'subscription' or 'pay_per_use' |

#### `[file_transfer]`

| Key | Default | Notes |
|---|---|---|
| `chunk_size` | `65536` | Chunk size in bytes (64KB) |
| `background_threshold_mb` | `50` | Background transfer threshold in MB |
| `direct_tls_only_threshold_mb` | `100` | Direct TLS preference threshold in MB |
| `max_concurrent_transfers` | `3` | Max concurrent file transfers |
| `verify_hash` | `true` | Verify file hash after transfer (SHA256) |
| `preparation_timeout_base` | `60` | Base timeout in seconds (for small files) |
| `preparation_timeout_per_gb` | `40` | Additional timeout per GB (40s/GB) |
| `preparation_progress_interval_mb` | `100` | Emit progress every N MB during SHA256 |
| `preparation_progress_interval_chunks` | `10000` | Emit progress every N chunks during CRC32 |

#### `[gateway]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Serve /v1/models and /v1/chat/completions to local tools (ADR-041). This node's own aliases need `compute.enabled` in `privacy_rules.json` too — open only when both are true — while a peer's `remote:<peer>:<alias>` needs this switch alone |
| `port` | `9997` | 9998 is the file server, 9999 the local API |
| `host` | `127.0.0.1` | Not configurable: any other value is refused at start (ADR-041 D1) |

The gateway has no image cap of its own: an image at either door is bounded by
`[vision] max_image_size_mb`, the same setting the P2P door enforces.

#### `[gossip]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable gossip protocol |
| `max_hops` | `5` | Maximum hops for message forwarding |
| `fanout` | `3` | Number of random peers to forward to |
| `ttl_seconds` | `86400` | Message TTL (24 hours) |
| `sync_interval` | `300` | Anti-entropy sync interval (5 minutes) |
| `cleanup_interval` | `600` | Expired message cleanup interval (10 minutes) |
| `priority` | `normal` | Default message priority: low, normal, high |

#### `[history]`

| Key | Default | Notes |
|---|---|---|
| `reject_unsigned` | `false` | Refuse a synced record whose signature this node cannot check (stored labelled `verification: legacy` either way) |

#### `[hole_punch]`

| Key | Default | Notes |
|---|---|---|
| `udp_punch_port` | `8890` | UDP port for hole punching |
| `nat_detection_enabled` | `true` | Detect NAT type (cone vs symmetric) |
| `stun_timeout` | `5` | Endpoint discovery timeout (seconds) |
| `punch_attempts` | `3` | Number of punch attempts before giving up |
| `enable_dtls` | `true` | Enable DTLS encryption for hole-punched connections |
| `dtls_handshake_timeout` | `3` | DTLS handshake timeout (seconds) |
| `dtls_version` | `1.2` | DTLS protocol version (1.2 or 1.3) |

#### `[hub]`

| Key | Default | Notes |
|---|---|---|
| `url` | `http://localhost:8000` |  |
| `auto_connect` | `false` |  |

#### `[knowledge]`

| Key | Default | Notes |
|---|---|---|
| `token_warning_threshold` | `0.8` | Warn when context window reaches 80% |
| `auto_extraction_enabled` | `true` | Automatically suggest knowledge extraction |
| `cultural_perspectives_enabled` | `false` | Include cultural perspective analysis in knowledge extraction |
| `cold_fallback_provider` | *(empty)* |  |

#### `[local_transcription]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable local Whisper transcription (v0.13.1+) |
| `model` | `openai/whisper-large-v3-turbo` | Model name (HuggingFace) |
| `device` | `auto` | Device: 'cuda', 'cpu', or 'auto' (auto-detects CUDA) |
| `compile_model` | `true` | Use torch.compile for 4.5x speedup (PyTorch 2.4+) |
| `use_flash_attention` | `false` | Use Flash Attention 2 (requires flash-attn package) |
| `chunk_length_s` | `30` | Chunk length for long-form transcription (speed vs accuracy) |
| `batch_size` | `16` | Batch size for chunked transcription (higher = faster, more VRAM) |
| `language` | `auto` | Language: 'auto' (detect) or ISO 639-1 code (e.g., 'en', 'es') |
| `task` | `transcribe` | Task: 'transcribe' or 'translate' (to English) |
| `fallback_to_openai` | `true` | Fallback to OpenAI API if local fails |
| `max_file_size_mb` | `25` | Max audio file size for local transcription (VRAM limit) |
| `lazy_loading` | `true` | Load model on first use (faster startup) |

#### `[logging]`

| Key | Default | Notes |
|---|---|---|
| `level` | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `console` | `true` | Enable console output |
| `console_level` | `INFO` | Console log level (can differ from file) |
| `file` | `~/.dpc/logs/dpc-client.log` | Log file path |
| `max_bytes` | `10485760` | Max bytes per log file before rotation (10MB) |
| `backup_count` | `5` | Number of backup log files to keep |

#### `[logging.modules]`

| Key | Default | Notes |
|---|---|---|
| `httpcore` | `WARNING` |  |
| `httpx` | `WARNING` |  |
| `anthropic` | `WARNING` |  |
| `telegram.ext.Application` | `INFO` |  |
| `openai` | `WARNING` |  |

#### `[oauth]`

| Key | Default | Notes |
|---|---|---|
| `callback_port` | `8080` |  |
| `callback_host` | `127.0.0.1` |  |
| `default_provider` | `google` |  |

#### `[p2p]`

| Key | Default | Notes |
|---|---|---|
| `listen_port` | `8888` |  |
| `listen_host` | `dual` | dual-stack (IPv4 + IPv6), can be "0.0.0.0" (IPv4 only) or "::" (IPv6 only) |
| `connection_timeout` | `30` | Connection establishment timeout in seconds |
| `auto_connect_node_groups` | `true` | Auto-connect to firewall node group members on startup |
| `auto_connect_delay` | `5` | Seconds to wait before attempting (let DHT bootstrap) |

#### `[relay]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable relay client mode |
| `prefer_region` | `global` | Preferred region: us-west, eu-central, global, etc. |
| `cache_timeout` | `300` | Relay discovery cache timeout (5 minutes) |
| `volunteer` | `false` | Volunteer this node as relay (opt-in) |
| `max_peers` | `10` | Max concurrent relay sessions (server mode) |
| `bandwidth_limit_mbps` | `10.0` | Bandwidth limit for relaying |
| `region` | `global` | Geographic region for relay announcements |

#### `[system]`

| Key | Default | Notes |
|---|---|---|
| `auto_collect_device_info` | `true` | Automatically collect device/system info for AI context |
| `collect_hardware_specs` | `true` | Collect hardware tiers (RAM, CPU, disk, GPU) |
| `collect_dev_tools` | `true` | Collect installed dev tools and versions |
| `collect_ai_models` | `false` | Collect locally available AI models (opt-in for inference-sharing) |

#### `[telegram]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Enable Telegram bot integration (v0.14.0+) |
| `bot_token` | *(empty)* | Bot token from @BotFather |
| `allowed_chat_ids` | `[]` | JSON array of whitelisted chat IDs (private access) |
| `use_webhook` | `false` | Use webhook mode (true) or polling mode (false) |
| `webhook_url` | *(empty)* | Public URL for webhook (production) |
| `webhook_port` | `8443` | Local port for webhook server |
| `owner_contact` | *(empty)* | Bot owner contact info (shown to unauthorized users) |
| `access_denied_message` | *(empty)* | Custom access denied message (optional) |
| `transcription_enabled` | `true` | Auto-transcribe Telegram voice messages (uses default voice provider) |
| `bridge_to_p2p` | `false` | Forward Telegram messages to P2P peers (see NOTE below) |
| `conversation_links` | `{}` | JSON map of telegram_chat_id -> conversation_id |
| `fetch_history_on_startup` | `true` | Fetch historical messages on bot startup |
| `history_fetch_limit` | `100` | Max messages to fetch per chat (Telegram limit: 100) |
| `history_max_age_hours` | `24` | Maximum age of messages to fetch (Telegram limit: 24 hours) |
| `history_message_types` | `text,voice,photo,document,video` | Comma-separated message types |
| `drop_pending_updates` | `false` | Drop pending updates on startup |
| `last_update_id` | `{}` | Track last processed update_id per chat (JSON object) |

#### `[turn]`

| Key | Default | Notes |
|---|---|---|
| `username` | *(empty)* | Leave empty or set via environment variable DPC_TURN_USERNAME |
| `credential` | *(empty)* | Leave empty or set via environment variable DPC_TURN_CREDENTIAL |
| `servers` | *(empty)* | Your provider's TURN/STUN URLs; used only when username/credential are set |
| `fallback_servers` | *(empty)* | A public relay, if you accept that your traffic passes through it |
| `fallback_username` | *(empty)* |  |
| `fallback_credential` | *(empty)* |  |

#### `[vision]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable vision API features (screenshot paste, image analysis) |
| `default_provider` | `openai` | Default AI provider for vision: 'openai' or 'anthropic' |
| `max_image_size_mb` | `5` | Maximum image size in MB (clipboard paste and uploads) |
| `thumbnail_quality` | `85` | Thumbnail JPEG quality (0-100) |

#### `[voice_messages]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable voice message recording and playback (v0.13.0+) |
| `max_duration_seconds` | `300` | Maximum recording duration in seconds (5 minutes) |
| `max_size_mb` | `10` | Maximum voice message file size in MB |
| `mime_types` | `audio/webm,audio/opus,audio/ogg,audio/mp4,audio/mpeg,audio/wav` | Supported audio formats (includes WAV for Tauri/Rust backend) |
| `default_sample_rate` | `48000` | Default sample rate in Hz (48kHz for quality) |
| `default_channels` | `1` | Default audio channels (1 = mono, 2 = stereo) |
| `default_codec` | `opus` | Default audio codec (opus for web compatibility) |

#### `[voice_transcription]`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Enable auto-transcription of received voice messages (v0.13.2+) |
| `sender_transcribes` | `false` | Should sender transcribe their own voice messages |
| `recipient_delay_seconds` | `3` | Wait N seconds before recipients attempt transcription (coordination) |
| `timeout_seconds` | `240` | Max wait time for peer's transcription before trying locally (increased to 240s for cold model loads that take 180+s) |
| `provider_priority` | `whisper-large-v3-turbo,whisper-medium,whisper-small,openai` | Comma-separated provider priority (aliases from providers.json) |
| `show_transcriber_name` | `false` | Show who transcribed the message in UI |
| `cache_transcriptions` | `true` | Cache transcriptions in memory |
| `fallback_to_openai` | `true` | Fallback to OpenAI API if local Whisper unavailable |

#### `[webrtc]`

| Key | Default | Notes |
|---|---|---|
| `stun_servers` | `stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302,stun:global.stun.twilio.com:3478,stun:stun.rtc.yandex.net:3478,stun:74.125.250.129:19302,stun:74.125.250.127:19302` |  |

<!-- END GENERATED CONFIG REFERENCE -->

## Using Environment Variables

Environment variables override config file settings and are useful for:
- Docker deployments
- CI/CD pipelines
- Testing different configurations
- Keeping sensitive settings out of version control

### Naming Convention

Environment variables follow this pattern:
```
DPC_<SECTION>_<KEY>
```

All uppercase, sections and keys separated by underscores.

### Examples

**Linux/Mac:**
```bash
export DPC_HUB_URL=https://hub.example.com
export DPC_OAUTH_CALLBACK_PORT=8080
export DPC_P2P_LISTEN_PORT=8888
```

**Windows (PowerShell):**
```powershell
$env:DPC_HUB_URL="https://hub.example.com"
$env:DPC_OAUTH_CALLBACK_PORT="8080"
$env:DPC_P2P_LISTEN_PORT="8888"
```

**Windows (CMD):**
```cmd
set DPC_HUB_URL=https://hub.example.com
set DPC_OAUTH_CALLBACK_PORT=8080
set DPC_P2P_LISTEN_PORT=8888
```

**Docker:**
```yaml
services:
  dpc-client:
    image: dpc-client:latest
    environment:
      - DPC_HUB_URL=https://hub.example.com
      - DPC_OAUTH_CALLBACK_PORT=8080
```

---

## Common Configuration Scenarios

### Scenario 1: Development (Default)

**Use case:** Testing on localhost with local Hub

```ini
[hub]
url = http://localhost:8000
auto_connect = true
```

No additional configuration needed.

---

### Scenario 2: Production Deployment

**Use case:** Connect to production Hub with custom settings

**Option A: Config File**
```ini
[hub]
url = https://hub.production.com
auto_connect = true

[p2p]
listen_port = 9000
listen_host = 0.0.0.0
```

**Option B: Environment Variables**
```bash
export DPC_HUB_URL=https://hub.production.com
export DPC_P2P_LISTEN_PORT=9000
```

---

### Scenario 3: Behind Corporate Firewall

**Use case:** Restricted network, custom ports

```ini
[hub]
url = https://internal-hub.corp.com:8443
auto_connect = true

[oauth]
callback_port = 9080

[p2p]
listen_port = 9888
```

---

### Scenario 4: Multi-Instance Testing

**Use case:** Run multiple clients on same machine

**Client 1:**
```bash
export DPC_OAUTH_CALLBACK_PORT=8080
export DPC_P2P_LISTEN_PORT=8888
export DPC_API_PORT=9999
```

**Client 2:**
```bash
export DPC_OAUTH_CALLBACK_PORT=8081
export DPC_P2P_LISTEN_PORT=8889
export DPC_API_PORT=9998
```

---

### Scenario 5: Docker Deployment

**docker-compose.yml:**
```yaml
version: '3.8'

services:
  dpc-client:
    build: ./dpc-client
    environment:
      - DPC_HUB_URL=${HUB_URL:-http://localhost:8000}
      - DPC_P2P_LISTEN_PORT=8888
      - DPC_API_PORT=9999
    ports:
      - "8888:8888"
      - "9999:9999"
    volumes:
      - dpc-data:/root/.dpc

volumes:
  dpc-data:
```

**.env file:**
```bash
HUB_URL=https://hub.production.com
```

---

## Configuration Migration

### Automatic Migration

DPC-Client automatically migrates old configuration formats:

**Old format (invalid):**
```
url = https://hub.example.com
```

**New format (migrated automatically):**
```ini
[hub]
url = https://hub.example.com
auto_connect = true
...
```

### What actually happens

1. **Backup:** the old file is copied to `config.ini.bak`
2. **Recreation:** a fresh config is written from the built-in defaults

That is the whole of it (`settings.py`, `_recreate_config_with_backup` →
`_create_default_config`). **Nothing is carried over.** In particular your Hub URL is
*not* extracted and *not* preserved — the new file gets `url = http://localhost:8000`
like any fresh install.

**Copy your Hub URL out of `config.ini.bak` and put it back by hand** after the client
recreates the file. Same for anything else you had customised: the backup is the only
copy.

---

## Troubleshooting

### Issue: "File contains no section headers"

**Cause:** old configuration format (no `[section]` headers)
**Solution:** the client backs the file up to `config.ini.bak` and writes a fresh
default — it does not migrate your values. Recover them from the backup, or fix the
file by hand first:

```ini
# Add [hub] section header
[hub]
url = your-hub-url
```

### Issue: Port already in use

**Error:** `Address already in use`
**Solution:** Change port in config or via environment variable

```bash
export DPC_OAUTH_CALLBACK_PORT=8081
export DPC_P2P_LISTEN_PORT=8889
export DPC_API_PORT=9998
```

### Issue: Can't connect to Hub

**Symptoms:** Connection timeout, refused
**Solutions:**
1. Check Hub URL is correct
2. Verify Hub is running: `curl https://your-hub-url/health`
3. Check firewall allows outbound HTTPS
4. Verify DNS resolution

### Issue: Config not taking effect

**Solution:** Check precedence - environment variables override config file

```bash
# Check what's actually being used
echo $DPC_HUB_URL

# Unset if needed
unset DPC_HUB_URL
```

---

## Advanced Topics

### Programmatic Configuration

For advanced use cases, you can access settings in code:

```python
from pathlib import Path
from dpc_client_core.settings import Settings

# Load settings
settings = Settings(Path.home() / ".dpc")

# Get values
hub_url = settings.get_hub_url()
oauth_port = settings.get_oauth_callback_port()

# Set values (writes to config file)
settings.set('hub', 'url', 'https://new-hub.com')
settings.reload()
```

### Custom Settings

While not officially supported, you can add custom sections:

```ini
[custom]
my_setting = my_value
```

Access via:
```python
value = settings.get('custom', 'my_setting')
```

---

## Security Considerations

### Sensitive Data

- **DO NOT** commit `config.ini` with production credentials to version control
- Use environment variables for sensitive settings
- Consider using secret management tools (Vault, AWS Secrets Manager)

### Recommended `.gitignore` Entry

```gitignore
# DPC Client configuration
.dpc/config.ini
.dpc/config.ini.bak
.dpc/node.key
.dpc/*.json
```

### Permissions

Ensure config file has appropriate permissions:

```bash
# Linux/Mac
chmod 600 ~/.dpc/config.ini

# Only owner can read/write
```

---

## Reference: Environment Variables

**Every key in every section has one.** The name is built mechanically —
`DPC_<SECTION>_<KEY>`, upper case — so all 147 keys in the reference above can be set
from the environment without appearing in any list. That includes the secrets this page
tells you to keep out of version control: `DPC_TELEGRAM_BOT_TOKEN`, `DPC_TURN_USERNAME`,
`DPC_TURN_CREDENTIAL`.

An environment variable wins over the config file (see the hierarchy at the top).

The eight most commonly used:

| Variable | Section | Key | Default |
|----------|---------|-----|---------|
| `DPC_HUB_URL` | hub | url | `http://localhost:8000` |
| `DPC_HUB_AUTO_CONNECT` | hub | auto_connect | `false` |
| `DPC_OAUTH_CALLBACK_PORT` | oauth | callback_port | `8080` |
| `DPC_OAUTH_CALLBACK_HOST` | oauth | callback_host | `127.0.0.1` |
| `DPC_P2P_LISTEN_PORT` | p2p | listen_port | `8888` |
| `DPC_P2P_LISTEN_HOST` | p2p | listen_host | `dual` |
| `DPC_API_PORT` | api | port | `9999` |
| `DPC_API_HOST` | api | host | `127.0.0.1` |

---

## The Ollama daemon's own environment (not DPC's, and DPC cannot set it)

If you run local models through Ollama, three settings that live **outside DPC entirely**
change how long your agents wait. DPC does not write them, does not read them, and cannot
tell you they are missing — they belong to the Ollama service, and every user sets them by
hand, once, per machine. They are listed here because the numbers below were measured on a
working install and are the difference between a four-second turn and a four-minute one.

| Variable | Suggested value | What it does |
|----------|-----------------|--------------|
| `LLAMA_ARG_CACHE_RAM` | see the table below | Prompt-cache ceiling **in MiB** (`-1` unlimited, `0` disabled). Default is `8192`. |
| `OLLAMA_FLASH_ATTENTION` | `1` | Flash attention; required before the KV cache can be quantised. |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | KV cache type. Halves KV memory against the `f16` default. |

**Why `LLAMA_ARG_CACHE_RAM` matters most.** `llama-server` keeps the KV state of past
conversations so a request whose prefix it has already seen skips the prefill. One agent's
entry measures 1.8–7.2 GiB (median 5.2), so the 8 GiB default holds about **one and a
half** of them: on a machine running several agents the cache evicts constantly — 36
evictions in 26 hours on the reference box — and each eviction costs that agent a full
re-prefill on its next turn: 60–100 s at 60K tokens, 293 s measured at 164K. The cost is
**system RAM, not VRAM**, and it is a ceiling rather than a reservation — memory is used
only as entries accumulate.

### How large to make it

The value is not "as much as possible". Everything else on the machine has to fit
alongside it: the OS, a browser or IDE, DPC itself with its agents, `bge-m3` when memory
indexing runs, and Whisper when a voice message arrives. Whisper is the one that catches
people out — it appears only while transcribing, which is exactly when the cache is
already full.

| Installed RAM | `LLAMA_ARG_CACHE_RAM` | Roughly holds |
|---------------|----------------------|---------------|
| 8 GB          | `0` (disable)        | nothing — the cache would push the machine into swap |
| 16 GB         | `4096`               | less than one long conversation |
| 24 GB         | `6144`               | about one |
| 32 GB         | `10240`              | two |
| 48 GB         | `16384`              | three |
| 64 GB         | `24576`              | four to five (the reference box) |
| 96 GB or more | `32768`              | six or more |

The table is a quarter of installed RAM at the small end and rises to about a third on
large machines, so a small node gets the *smaller* share — the opposite of what a flat
percentage or a fixed reserve produces. If the machine starts swapping after a heavy
session, halve the value; **slow is cheaper than swapping**, and `0` is a legitimate
setting on a machine that cannot spare the memory.

Two caveats worth reading before quoting the "holds N conversations" column:

- **It depends on the KV cache type.** Entry sizes above were measured with
  `OLLAMA_KV_CACHE_TYPE=q8_0`. A smaller KV type halves the entries, so the same ceiling
  holds twice as many; a larger one does the reverse.
- **Nothing measures this for you.** DPC's device context records how much RAM is
  *installed* (`hardware.memory.ram_gb`) and not how much is free — the `free_gb` in that
  file is disk. The variable is also read only when Ollama starts, so there is no runtime
  adjustment to be had: pick a value for the machine, set it once, and watch for swapping.

**Setting it (Windows).** Ollama passes its own environment to the `llama-server` child, so
a user-scope variable is enough — but the tray app inherits the environment it was
*started* with, so a new terminal will not do: quit Ollama from the tray and start it
again.

```powershell
[Environment]::SetEnvironmentVariable('LLAMA_ARG_CACHE_RAM','24576','User')
# then: Ollama tray icon → Quit → launch Ollama again
```

On Linux/macOS put it where the service reads its environment
(`systemctl edit ollama.service` → `Environment="LLAMA_ARG_CACHE_RAM=24576"`, or
`launchctl setenv`), then restart the service.

**Checking that it took.** In the Ollama server log
(`%LOCALAPPDATA%\Ollama\server.log` on Windows, `journalctl -u ollama` on Linux):

```
srv    load_model: prompt cache is enabled, size limit: 24576 MiB
```

If it still says `8192`, the variable did not reach the child — the value format is not the
suspect, it is a plain integer. The second signal, over the following day, is the count of
`making room for prompt cache entry, removing oldest entry` lines, which should fall.

> These are stopgaps for the Ollama path. When DPC runs its own inference server
> (ADR-040 Stage 2) it passes the equivalent flags itself and this section stops being
> the user's problem.

---

## Device Identity and Multi-Device Considerations

### Device-Specific Identity

Each DPC Client device generates a **unique cryptographic identity** on first initialization:

**Identity Files (stored in `~/.dpc/`):**
- `node.key` - RSA private key (2048-bit, unique per device)
- `node.crt` - Self-signed X.509 certificate
- `node.id` - Node identifier (e.g., `dpc-node-8b066c7f3d7eb627`)

**How Node ID is Generated:**
```
1. Generate RSA key pair (2048-bit)
2. Hash public key with SHA256
3. Node ID = "dpc-node-" + first 16 hex characters of hash
```

**Key Characteristics:**
- Each device has a unique node_id derived from its RSA public key
- Private keys never leave the device (security by design)
- Node identities cannot be shared between devices

---

### Single Device Per User (Current Limitation)

**Important:** The current Hub implementation supports **one device per user account**.

When you authenticate with the Hub via OAuth (Google or GitHub), the Hub associates your email address with your device's `node_id`. If you log in from a different device with the same email, the Hub will update the `node_id` to the new device, effectively "orphaning" the previous device.

**Example Scenario:**

```
Device 1 (Laptop):
1. Generate node_id: dpc-node-aaaa1111
2. Login with user@example.com
3. Hub database: {email: "user@example.com", node_id: "dpc-node-aaaa1111"}
4. ✅ Device 1 is registered and connected

Device 2 (Desktop):
1. Generate node_id: dpc-node-bbbb2222 (different device = different keys)
2. Login with user@example.com (same email!)
3. Hub database: {email: "user@example.com", node_id: "dpc-node-bbbb2222"}
4. ✅ Device 2 is now registered
5. ❌ Device 1 is now "orphaned" (node_id no longer matches Hub records)
```

**What This Means:**
- You can only actively use **one device** per email account
- Logging in from a second device will disconnect the first device from Hub services
- Direct P2P connections (TLS) still work between devices (no Hub needed)
- WebRTC connections require Hub signaling, so only the most recently logged-in device can use WebRTC

**Workaround (Development/Testing):**
- Use different email addresses for different devices
- Or use multi-instance testing with different OAuth credentials (see Scenario 4)

**Future Enhancement:**
Multi-device support would require Hub database schema changes to support one-to-many relationships between users and devices. This is not currently implemented.

---

### OAuth Provider Choice

The `default_provider` configuration (Google vs GitHub) is about **which OAuth account you authenticate with**, not about using multiple devices.

**Configuration:**
```ini
[oauth]
default_provider = github  # or 'google'
```

**What This Controls:**
- Which OAuth provider to use for authentication (Google or GitHub)
- Which email address gets associated with your device's node_id
- You can switch between providers, and the Hub will update the `provider` field

**What This Does NOT Control:**
- Multi-device support (not available in current version)
- Device selection (each device always uses its own `node_id`)

**Example:**
```
Device 1 with node_id "dpc-node-aaaa1111":
- Login with Google → Hub: {email: "user@gmail.com", node_id: "dpc-node-aaaa1111", provider: "google"}
- Later, login with GitHub → Hub: {email: "user@github.com", node_id: "dpc-node-aaaa1111", provider: "github"}

Same device, different OAuth accounts = different Hub user profiles
```

---

## Phase 6: Connection Strategy Configuration (v0.10.0+)

Phase 6 introduces a 6-tier connection fallback hierarchy for near-universal P2P connectivity. This section documents the new configuration options for connection strategies, UDP hole punching, volunteer relays, and gossip protocols.

### Connection Strategy Settings (`[connection]`)

Configure which connection strategies are enabled and their timeouts.

#### `enable_ipv6`
- **Description:** Try IPv6 direct connections (Priority 1)
- **Default:** `true`
- **Values:** `true`, `false`
- **Example:**
  ```ini
  enable_ipv6 = true
  ```

#### `enable_ipv4`
- **Description:** Try IPv4 direct connections (Priority 2)
- **Default:** `true`
- **Values:** `true`, `false`

#### `enable_hub_webrtc`
- **Description:** Try Hub WebRTC with STUN/TURN (Priority 3)
- **Default:** `false` — shipped off since 2026-06-01
- **Values:** `true`, `false`
- **Note:** Requires a Hub connection. Turning it on also means WebRTC will look for a
  TURN relay, and `[turn]` is empty by default — supply your own credentials rather
  than relying on a public relay

#### `enable_hole_punching`
- **Description:** Try DHT-coordinated UDP hole punching (Priority 4)
- **Default:** `false` — experimental, opt-in
- **Values:** `true`, `false`
- **Success Rate:** 60-70% for cone NAT (fails gracefully for symmetric NAT)
- **Note:** discovery runs over the DHT, and `[dht] seed_nodes` is empty by default, so
  turning this on alone is not enough — give the DHT something to bootstrap from first

#### `enable_relays`
- **Description:** Try volunteer relay nodes (Priority 5)
- **Default:** `true`
- **Values:** `true`, `false`
- **Note:** 100% NAT coverage, Hub-independent

#### `enable_gossip`
- **Description:** Use gossip store-and-forward fallback (Priority 6)
- **Default:** `true`
- **Values:** `true`, `false`
- **Note:** Eventual delivery, not real-time

#### Strategy Timeouts

Per-strategy timeout configuration in seconds:

```ini
[connection]
ipv6_timeout = 60          # IPv6 direct: 30s pre-flight + 30s TLS handshake
ipv4_timeout = 60          # IPv4 direct: 30s pre-flight + 30s TLS handshake
webrtc_timeout = 30        # Hub WebRTC timeout
hole_punch_timeout = 15    # UDP hole punching timeout
relay_timeout = 20         # Volunteer relay timeout
gossip_timeout = 5         # Gossip fallback timeout
```

#### `hello_timeout`
- **Description:** Seconds the listener waits for `HELLO` after issuing its
  `HELLO_CHALLENGE`. A peer that completes TLS and then says nothing is hung up on
  when this expires, and the expiry counts as a failed HELLO for the per-address
  rate limiter (10 failures in 300 s close that address silently)
- **Default:** `10`
- **Note:** ADR-041 D8. Raise it only for peers on very high-latency links; the
  dial side budgets `ipv4_timeout` / `ipv6_timeout` for the whole exchange, so
  this has no reason to be longer than those

#### `max_pending_hellos_per_ip`
- **Description:** Inbound connections one address may hold between accept and
  `HELLO_ACK`. The next one is closed silently, as a rate-limited address is, and
  is not counted as a failed HELLO
- **Default:** `8`
- **Note:** ADR-041 D8. A NAT puts many honest peers behind one address, which is
  why this counts connections *still waiting for HELLO* rather than connections:
  an honest peer holds a slot for one round trip and releases it

**Example Configuration** — this is what a fresh install writes, so copying it changes
nothing. Two strategies ship off; turn them on deliberately, not by pasting a block.
```ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = false     # off by default
enable_hole_punching = false  # off by default, experimental
enable_relays = true
enable_gossip = true

ipv6_timeout = 60
webrtc_timeout = 30
relay_timeout = 20
```

**Disable Specific Strategies:**
```ini
[connection]
# Work only with direct connections and WebRTC (disable Hub-independent fallbacks)
enable_hole_punching = false
enable_relays = false
enable_gossip = false
```

---

### UDP Hole Punching Settings (`[hole_punch]`)

Configure DHT-coordinated UDP hole punching (Priority 4 strategy).

#### `udp_punch_port`
- **Description:** UDP port for hole punching
- **Default:** `8890`
- **Environment Variable:** `DPC_HOLE_PUNCH_UDP_PUNCH_PORT`
- **Example:**
  ```ini
  udp_punch_port = 8890
  ```

#### `nat_detection_enabled`
- **Description:** Detect NAT type (cone vs symmetric)
- **Default:** `true`
- **Values:** `true`, `false`
- **Note:** Automatic NAT type detection helps optimize connection strategy

#### `stun_timeout`
- **Description:** Endpoint discovery timeout in seconds
- **Default:** `5`
- **Note:** Timeout for querying DHT peers for reflexive address

#### `punch_attempts`
- **Description:** Number of punch attempts before giving up
- **Default:** `3`
- **Note:** Higher values increase success rate but add latency

**Example Configuration:**
```ini
[hole_punch]
udp_punch_port = 8890
nat_detection_enabled = true
stun_timeout = 5
punch_attempts = 3
```

#### DTLS encryption

Hole-punched UDP connections are encrypted with DTLS. This shipped in v0.10.1; an
earlier version of this page still warned that the transport was unencrypted and told
you to switch the strategy off for that reason — that reason is gone.

```ini
[hole_punch]
enable_dtls = true            # default
dtls_handshake_timeout = 3    # seconds
dtls_version = 1.2            # 1.2 or 1.3
```

Hole punching itself is still **off by default** (`[connection] enable_hole_punching`),
but because it is experimental and DHT-dependent, not because it is in the clear.

---

### Volunteer Relay Settings (`[relay]`)

Configure volunteer relay functionality (Priority 5 strategy). Relays provide 100% NAT coverage by forwarding encrypted messages between peers.

#### Client Mode Settings

#### `enabled`
- **Description:** Enable relay client mode (use relays for outbound connections)
- **Default:** `true`
- **Values:** `true`, `false`

#### `prefer_region`
- **Description:** Preferred geographic region for relay selection
- **Default:** `global`
- **Values:** `us-west`, `eu-central`, `ap-southeast`, `global`, etc.
- **Note:** Regional relays reduce latency

#### `cache_timeout`
- **Description:** Relay discovery cache timeout in seconds
- **Default:** `300` (5 minutes)
- **Note:** Caching reduces DHT queries

#### Server Mode Settings (Volunteering)

#### `volunteer`
- **Description:** Volunteer this node as relay for others (opt-in)
- **Default:** `false`
- **Values:** `true`, `false`
- **Important:** Requires stable internet connection and firewall configuration

#### `max_peers`
- **Description:** Maximum concurrent relay sessions (server mode)
- **Default:** `10`
- **Note:** Higher values allow helping more peers but consume more bandwidth

#### `bandwidth_limit_mbps`
- **Description:** Bandwidth limit for relaying in Mbps
- **Default:** `10.0`
- **Note:** Prevents relay abuse

#### `region`
- **Description:** Geographic region for relay announcements
- **Default:** `global`
- **Values:** `us-west`, `eu-central`, `ap-southeast`, `global`, etc.

**Example Configuration (Client Mode):**
```ini
[relay]
enabled = true
prefer_region = us-west      # Prefer US West relays
cache_timeout = 300          # 5-minute cache
volunteer = false            # Don't volunteer as relay
```

**Example Configuration (Server Mode - Volunteering):**
```ini
[relay]
enabled = true
volunteer = true             # Volunteer as relay
max_peers = 20               # Support up to 20 concurrent sessions
bandwidth_limit_mbps = 50.0  # 50 Mbps limit
region = eu-central          # Announce in EU Central region
```

**Privacy Note:** Relays forward encrypted payloads only. They cannot decrypt message content but can see:
- Peer node IDs
- Message sizes
- Message timing

---

### Gossip Protocol Settings (`[gossip]`)

Configure epidemic gossip store-and-forward protocol (Priority 6 strategy). Gossip provides eventual message delivery in disaster scenarios.

#### `enabled`
- **Description:** Enable gossip protocol
- **Default:** `true`
- **Values:** `true`, `false`

#### `max_hops`
- **Description:** Maximum hops for message forwarding
- **Default:** `5`
- **Range:** 1-10
- **Note:** Higher values increase reach but add latency

#### `fanout`
- **Description:** Number of random peers to forward to
- **Default:** `3`
- **Range:** 1-10
- **Note:** Higher values increase reliability but add bandwidth

#### `ttl_seconds`
- **Description:** Message TTL (time-to-live) in seconds
- **Default:** `86400` (24 hours)
- **Note:** Messages expire after TTL to prevent indefinite forwarding

#### `sync_interval`
- **Description:** Anti-entropy sync interval in seconds
- **Default:** `300` (5 minutes)
- **Note:** Periodic reconciliation using vector clocks

#### `cleanup_interval`
- **Description:** Expired message cleanup interval in seconds
- **Default:** `600` (10 minutes)
- **Note:** Remove expired messages from storage

#### `priority`
- **Description:** Default gossip message priority
- **Default:** `normal`
- **Values:** `low`, `normal`, `high`

**Example Configuration:**
```ini
[gossip]
enabled = true
max_hops = 5                 # Max 5 hops
fanout = 3                   # Forward to 3 random peers
ttl_seconds = 86400          # 24-hour TTL
sync_interval = 300          # Sync every 5 minutes
cleanup_interval = 600       # Cleanup every 10 minutes
priority = normal
```

**High-Reliability Configuration:**
```ini
[gossip]
enabled = true
max_hops = 7                 # Increase reach
fanout = 5                   # Increase redundancy
ttl_seconds = 172800         # 48-hour TTL
sync_interval = 180          # Sync every 3 minutes
```

**Low-Bandwidth Configuration:**
```ini
[gossip]
enabled = true
max_hops = 3                 # Reduce hops
fanout = 2                   # Reduce redundancy
ttl_seconds = 43200          # 12-hour TTL
sync_interval = 600          # Sync every 10 minutes
```

---

## See Also

- [Quick Start Guide](../QUICK_START.md) — at the repository root, not in `docs/`
- [WebRTC Setup Guide](./WEBRTC_SETUP_GUIDE.md)
- [GitHub OAuth Setup](./GITHUB_AUTH_SETUP.md)
- [Firewall Configuration](../dpc-client/privacy_rules.example.json)

There is no changelog: the project keeps its history in git and in the backlog instead.

---

**Questions or issues?** [Open an issue](https://github.com/mikhashev/dpc-messenger/issues)
