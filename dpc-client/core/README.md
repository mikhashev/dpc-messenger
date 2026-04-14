# dpc-client/core

Python backend service for the D-PC Messenger desktop client — runs on
the user's machine, manages P2P connections, AI providers, the local
WebSocket API for the UI, and the embedded autonomous agent.

**License:** GPL v3 — see [`../../LICENSE.md`](../../LICENSE.md)

---

## Install

```bash
poetry install

# Optional: GPU-accelerated Whisper transcription (Apple Silicon only)
poetry install -E mlx
```

Python 3.12+.

## Run

```bash
poetry run python run_service.py
```

Listens on `127.0.0.1:9999` (WebSocket API for the UI) and, by default,
`0.0.0.0:8888` (direct P2P TLS).

## Test

```bash
poetry run pytest
poetry run pytest --cov=dpc_client_core
```

---

## What's in it

### Entry point + orchestration

| File | Purpose |
|------|---------|
| [`run_service.py`](run_service.py) | Process entrypoint |
| [`dpc_client_core/service.py`](dpc_client_core/service.py) | `CoreService` — lifecycle, component wiring |
| [`dpc_client_core/local_api.py`](dpc_client_core/local_api.py) | WebSocket API for the UI |
| [`dpc_client_core/message_router.py`](dpc_client_core/message_router.py) | Dispatcher for incoming P2P messages |
| [`dpc_client_core/message_handlers/`](dpc_client_core/message_handlers/) | One handler per DPTP command (text, files, voice, knowledge commits, gossip, relay, session, etc.) |

### Connections (6-tier fallback)

| File | Purpose |
|------|---------|
| [`dpc_client_core/coordinators/connection_orchestrator.py`](dpc_client_core/coordinators/connection_orchestrator.py) | Tries IPv6 → IPv4 → Hub WebRTC → UDP hole punch → volunteer relay → gossip |
| [`dpc_client_core/p2p_manager.py`](dpc_client_core/p2p_manager.py) | Direct TLS transport |
| [`dpc_client_core/webrtc_peer.py`](dpc_client_core/webrtc_peer.py) | WebRTC peer (aiortc) |
| [`dpc_client_core/dht/`](dpc_client_core/dht/) | Kademlia DHT for serverless peer discovery |
| [`dpc_client_core/managers/hole_punch_manager.py`](dpc_client_core/managers/hole_punch_manager.py) | UDP hole punching (DTLS) |
| [`dpc_client_core/managers/relay_manager.py`](dpc_client_core/managers/relay_manager.py) | Volunteer relay discovery + routing |
| [`dpc_client_core/managers/gossip_manager.py`](dpc_client_core/managers/gossip_manager.py) | Epidemic store-and-forward |

### AI, context, firewall

| File | Purpose |
|------|---------|
| [`dpc_client_core/llm_manager.py`](dpc_client_core/llm_manager.py) | Provider registry (Ollama, OpenAI, Anthropic, Z.AI, Gemini, GigaChat, GitHub Models, local Whisper) |
| [`dpc_client_core/providers/`](dpc_client_core/providers/) | Per-provider implementations |
| [`dpc_client_core/firewall.py`](dpc_client_core/firewall.py) | Context access control (the single gate between agent/peer requests and personal data) |
| [`dpc_client_core/conversation_monitor.py`](dpc_client_core/conversation_monitor.py) | Knowledge-commit proposal pipeline |
| [`dpc_client_core/consensus_manager.py`](dpc_client_core/consensus_manager.py) | Multi-party voting with devil's advocate |

### Embedded agent (`dpc_client_core/dpc_agent/`)

Autonomous agent with tool calling, per-agent memory, scheduled tasks,
background consciousness, and an evolution sandbox. Tools are
registered in `dpc_agent/tools/` (core, browser, git, review, skills,
archive, messaging) and the runtime list is available inside the agent
via `list_my_tools`.

For the full agent guide (identity files, skills, scheduling,
evolution, Telegram linking): [`../../docs/DPC_AGENT_GUIDE.md`](../../docs/DPC_AGENT_GUIDE.md).

### Federation Hub client (optional)

| File | Purpose |
|------|---------|
| [`dpc_client_core/hub_client.py`](dpc_client_core/hub_client.py) | OAuth + WebRTC signaling against `dpc-hub` |

### Caches

`context_cache.py`, `peer_cache.py`, `token_cache.py` — in-memory +
on-disk caches for personal context, known peers, and OAuth tokens.

---

## Configuration

All client configuration lives in `~/.dpc/`:

- `config.ini` — service settings
- `providers.json` — AI provider credentials
- `privacy_rules.json` — context firewall rules (editable in the UI)
- `personal.json` — personal context
- `device_context.json` — auto-collected hardware/software info
- `node.key` / `node.crt` / `node.id` — identity (from `dpc-protocol`)
- `agents/{agent_id}/` — per-agent state (memory, skills, scheduled tasks)

Full reference: [`../../docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md).

---

## Related

- [`../../README.md`](../../README.md) — project overview
- [`../../docs/DPC_AGENT_GUIDE.md`](../../docs/DPC_AGENT_GUIDE.md) — agent usage and tools
- [`../../docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) — configuration reference
- [`../../specs/dptp_v1.md`](../../specs/dptp_v1.md) — wire protocol spec
