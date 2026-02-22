# DPC Client Core

Python backend service for the D-PC Messenger desktop client.

## Overview

This package provides the core backend functionality for D-PC Messenger, including:

- **P2P Connection Management** - 6-tier connection fallback hierarchy (IPv6, IPv4, WebRTC, UDP hole punching, volunteer relays, gossip)
- **DHT Peer Discovery** - Kademlia-based distributed hash table for serverless peer discovery
- **Federation Hub Client** - OAuth authentication and WebRTC signaling (optional)
- **LLM Integration** - Multi-provider AI support (Ollama, OpenAI, Anthropic, Z.AI)
- **Context Firewall** - Privacy-first access control for personal context sharing
- **WebSocket API** - Local API server for frontend communication (port 9999)
- **Device Context Collection** - Automatic hardware/software detection
- **DPC Agent** - Embedded autonomous AI agent with tools, memory, and evolution capabilities

## Installation

```bash
poetry install

# Optional: GPU-accelerated Whisper transcription (Apple Silicon only)
poetry install -E mlx
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

### Core Service & Routing
- [service.py](dpc_client_core/service.py) - CoreService orchestrator
- [message_router.py](dpc_client_core/message_router.py) - P2P command dispatcher
- [message_handlers/](dpc_client_core/message_handlers/) - Command-specific handlers (12+ handlers)

### Connection Management
- [connection_orchestrator.py](dpc_client_core/coordinators/connection_orchestrator.py) - 6-tier fallback coordinator (v0.10.0+)
- [p2p_manager.py](dpc_client_core/p2p_manager.py) - Low-level P2P connections (TLS + WebRTC)
- [webrtc_peer.py](dpc_client_core/webrtc_peer.py) - WebRTC peer wrapper
- [connection_status.py](dpc_client_core/connection_status.py) - Connection state tracking

### Decentralized Infrastructure
- [dht/manager.py](dpc_client_core/dht/manager.py) - DHT peer discovery (v0.10.0+)
- [hole_punch_manager.py](dpc_client_core/managers/hole_punch_manager.py) - UDP hole punching (v0.10.0+)
- [relay_manager.py](dpc_client_core/managers/relay_manager.py) - Volunteer relay management (v0.10.0+)
- [gossip_manager.py](dpc_client_core/managers/gossip_manager.py) - Gossip store-and-forward (v0.10.0+)

### AI & Context
- [llm_manager.py](dpc_client_core/llm_manager.py) - AI provider integration
- [consensus_manager.py](dpc_client_core/consensus_manager.py) - Knowledge voting with devil's advocate
- [conversation_monitor.py](dpc_client_core/conversation_monitor.py) - Background knowledge extraction
- [firewall.py](dpc_client_core/firewall.py) - Context access control

### DPC Agent (v0.15.0+)

Embedded autonomous AI agent with tool calling, memory, and self-modification capabilities.

**Core Agent Package** (`dpc_client_core/dpc_agent/`):
- [agent.py](dpc_client_core/dpc_agent/agent.py) - Main DpcAgent class with tool execution loop
- [loop.py](dpc_client_core/dpc_agent/loop.py) - Agent execution loop with multi-round tool calls
- [memory.py](dpc_client_core/dpc_agent/memory.py) - Memory system (identity.md, scratchpad.md, knowledge/)
- [context.py](dpc_client_core/dpc_agent/context.py) - Context assembly for LLM prompts
- [llm_adapter.py](dpc_client_core/dpc_agent/llm_adapter.py) - LLM provider integration

**Advanced Features**:
- [consciousness.py](dpc_client_core/dpc_agent/consciousness.py) - Background self-reflection mode
- [evolution.py](dpc_client_core/dpc_agent/evolution.py) - Self-modification within sandbox
- [task_queue.py](dpc_client_core/dpc_agent/task_queue.py) - Background task scheduling
- [budget.py](dpc_client_core/dpc_agent/budget.py) - Subscription-aware rate limiting
- [events.py](dpc_client_core/dpc_agent/events.py) - Event emission for notifications

**Tools** (`dpc_client_core/dpc_agent/tools/`):
- [core.py](dpc_client_core/dpc_agent/tools/core.py) - 20+ core tools (repo_read, repo_write_commit, browse_page, etc.)
- [browser.py](dpc_client_core/dpc_agent/tools/browser.py) - Web browsing and search tools
- [git.py](dpc_client_core/dpc_agent/tools/git.py) - Git operations (repo_commit, repo_status, etc.)
- [review.py](dpc_client_core/dpc_agent/tools/review.py) - Multi-model self-review tools
- [registry.py](dpc_client_core/dpc_agent/tools/registry.py) - Tool registration and discovery

**Agent Managers** (`dpc_client_core/managers/`):
- [agent_manager.py](dpc_client_core/managers/agent_manager.py) - DpcAgentManager for CoreService integration
- [agent_telegram_bridge.py](dpc_client_core/managers/agent_telegram_bridge.py) - Two-way Telegram communication

### Caching & Offline
- [context_cache.py](dpc_client_core/context_cache.py) - In-memory context cache
- [peer_cache.py](dpc_client_core/peer_cache.py) - Known peers cache
- [token_cache.py](dpc_client_core/token_cache.py) - OAuth token cache

### Federation (Optional)
- [hub_client.py](dpc_client_core/hub_client.py) - Federation Hub communication
- [local_api.py](dpc_client_core/local_api.py) - WebSocket API for UI

## DPC Agent Features

The embedded agent provides autonomous AI capabilities:

### Tool System (37+ tools)
- **File Operations**: `repo_read`, `repo_list`, `repo_write_commit`, `drive_read`, `drive_list`
- **Memory**: `update_scratchpad`, `update_identity`, `knowledge_read`, `knowledge_list`, `knowledge_write`
- **Web**: `browse_page`, `search_web`, `fetch_url`
- **Git**: `repo_status`, `repo_diff`, `repo_commit`, `repo_log`
- **Self-Review**: `self_review`, `compare_approaches`, `get_second_opinion`
- **Communication**: `chat_history`, `send_notification`
- **Evolution**: `schedule_task`, `get_task_status`, `pause_evolution`, `resume_evolution`

### Event System
Real-time event emission for notifications:
- `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`
- `EVOLUTION_CYCLE_COMPLETED`, `CODE_MODIFIED`
- `BUDGET_WARNING`, `RATE_LIMIT_HIT`

### Telegram Integration
- Two-way communication via `agent_telegram_bridge.py`
- Commands: `/start`, `/help`, `/status`
- Event notifications with configurable filters
- Rate limiting (20 events/min, 3s cooldown)

### Evolution Mode
Self-modification within sandbox boundaries:
- **CAN modify**: `~/.dpc/agent/` (memory, tools, config)
- **CANNOT modify**: DPC Messenger codebase, `personal.json`, `config.ini`
- Auto-apply or manual approval modes
- Configurable evolution interval (default: 60 minutes)

### Configuration (`~/.dpc/config.ini`)
```ini
[dpc_agent]
enabled = true
background_consciousness = true
budget_usd = 50
max_rounds = 200
context_window = 128000
enable_task_queue = true
evolution_enabled = true
evolution_interval_minutes = 60
evolution_auto_apply = false
billing_model = subscription

[dpc_agent_telegram]
enabled = true
bot_token = YOUR_BOT_TOKEN
allowed_chat_ids = ["123456789"]
event_filter = task_started,task_completed,task_failed,evolution_cycle_completed,code_modified
```

See [../../docs/DPC_AGENT_GUIDE.md](../../docs/DPC_AGENT_GUIDE.md) for complete usage guide.

## License

GPL v3 - See [../../LICENSE.md](../../LICENSE.md)
