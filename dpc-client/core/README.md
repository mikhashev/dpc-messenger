# DPC Client Core

Python backend service for the D-PC Messenger desktop client.

## Overview

This package provides the core backend functionality for D-PC Messenger, including:

- **P2P Connection Management** - 6-tier connection fallback hierarchy (IPv6, IPv4, WebRTC, UDP hole punching, volunteer relays, gossip)
- **DHT Peer Discovery** - Kademlia-based distributed hash table for serverless peer discovery
- **Federation Hub Client** - OAuth authentication and WebRTC signaling (optional)
- **LLM Integration** - Multi-provider AI support (Ollama, OpenAI, Anthropic)
- **Context Firewall** - Privacy-first access control for personal context sharing
- **WebSocket API** - Local API server for frontend communication (port 9999)
- **Device Context Collection** - Automatic hardware/software detection

## Installation

```bash
poetry install
```

## Usage

Run the backend service:

```bash
poetry run python run_service.py
```

## Testing

```bash
poetry run pytest
poetry run pytest --cov=dpc_client_core  # With coverage
```

## Configuration

Configuration files stored in `~/.dpc/`:
- `config.ini` - Client settings
- `providers.json` - AI provider credentials
- `privacy_rules.json` - Context firewall rules
- `personal.json` - Personal context data
- `device_context.json` - Auto-collected device information

See [../../docs/CONFIGURATION.md](../../docs/CONFIGURATION.md) for complete configuration reference.

## Architecture

Entry point: [run_service.py](run_service.py)

Key components:

**Core Service & Routing:**
- [service.py](dpc_client_core/service.py) - CoreService orchestrator
- [message_router.py](dpc_client_core/message_router.py) - P2P command dispatcher
- [message_handlers/](dpc_client_core/message_handlers/) - Command-specific handlers (6 handlers)

**Connection Management:**
- [connection_orchestrator.py](dpc_client_core/coordinators/connection_orchestrator.py) - 6-tier fallback coordinator (v0.10.0+)
- [p2p_manager.py](dpc_client_core/p2p_manager.py) - Low-level P2P connections (TLS + WebRTC)
- [webrtc_peer.py](dpc_client_core/webrtc_peer.py) - WebRTC peer wrapper
- [connection_status.py](dpc_client_core/connection_status.py) - Connection state tracking

**Decentralized Infrastructure:**
- [dht/manager.py](dpc_client_core/dht/manager.py) - DHT peer discovery (v0.10.0+)
- [hole_punch_manager.py](dpc_client_core/managers/hole_punch_manager.py) - UDP hole punching (v0.10.0+)
- [relay_manager.py](dpc_client_core/managers/relay_manager.py) - Volunteer relay management (v0.10.0+)
- [gossip_manager.py](dpc_client_core/managers/gossip_manager.py) - Gossip store-and-forward (v0.10.0+)

**AI & Context:**
- [llm_manager.py](dpc_client_core/llm_manager.py) - AI provider integration
- [consensus_manager.py](dpc_client_core/consensus_manager.py) - Knowledge voting with devil's advocate
- [conversation_monitor.py](dpc_client_core/conversation_monitor.py) - Background knowledge extraction
- [firewall.py](dpc_client_core/firewall.py) - Context access control

**Caching & Offline:**
- [context_cache.py](dpc_client_core/context_cache.py) - In-memory context cache
- [peer_cache.py](dpc_client_core/peer_cache.py) - Known peers cache
- [token_cache.py](dpc_client_core/token_cache.py) - OAuth token cache

**Federation (Optional):**
- [hub_client.py](dpc_client_core/hub_client.py) - Federation Hub communication
- [local_api.py](dpc_client_core/local_api.py) - WebSocket API for UI

## License

GPL v3 - See [../../LICENSE.md](../../LICENSE.md)
