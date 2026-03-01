# DPC Embedded Agent - Testing and Usage Guide

This guide explains how to configure, test, and use the embedded autonomous AI agent in DPC Messenger.

## Overview

The embedded agent is a self-modifying AI agent adapted from the [Ouroboros project](https://github.com/razzant/ouroboros), integrated directly into DPC Messenger's codebase. It provides:

- **45+ Tools**: File operations, web search, memory management, git, task scheduling, evolution control
- **Background Consciousness**: Proactive thinking between tasks (optional)
- **Persistent Memory**: Scratchpad, identity, and knowledge base
- **Self-Modification**: Can modify files within its sandbox (`~/.dpc/agent/`)
- **DPC Integration**: Uses DPC's LLM providers and personal/device context

## Architecture

```
DPC Messenger
    └── DpcAgentProvider (AIProvider)
            └── DpcAgentManager
                    └── DpcAgent
                            ├── ToolRegistry (sandboxed to ~/.dpc/agent/)
                            ├── DpcLlmAdapter → DPC's LLMManager
                            ├── Memory (identity, scratchpad, knowledge)
                            └── BackgroundConsciousness (proactive thinking)
```

## Quick Start

### 1. Enable the Agent

Add a provider configuration to `~/.dpc/providers.json`:

```json
{
  "alias": "dpc_agent",
  "type": "dpc_agent",
  "tools": ["repo_read", "repo_list", "update_scratchpad", "browse_page", "search_web"],
  "background_consciousness": false,
  "budget_usd": 50,
  "max_rounds": 200,
  "context_window": 200000
}
```

### 2. Configure Agent Settings

Agent settings are configured in `~/.dpc/privacy_rules.json` under the `dpc_agent` section:

```json
{
  "dpc_agent": {
    "enabled": true,
    "background_consciousness": false,
    "tools": {
      "repo_read": true,
      "repo_list": true,
      "update_scratchpad": true,
      "browse_page": true,
      "search_web": true
    },
    "budget_usd": 50,
    "max_rounds": 200,
    "evolution_enabled": false,
    "extended_sandbox_paths": {
      "read_only": [],
      "read_write": []
    }
  }
}
```

> **Note:** You can also use the Firewall Editor UI (🛡️ Firewall Rules button in sidebar) to configure agent settings visually.

### 3. Use as AI Provider

In the DPC UI, select "dpc_agent" as your AI provider, or set it as default:

```json
{
  "default_provider": "dpc_agent",
  "providers": [...]
}
```

## Configuration Reference

### Provider Configuration (`providers.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `alias` | string | required | Provider alias name |
| `type` | string | required | Must be `"dpc_agent"` |
| `tools` | string[] | `[]` | Tool whitelist (empty = all core tools) |
| `background_consciousness` | bool | `false` | Enable proactive thinking |
| `budget_usd` | float | `50.0` | Maximum budget per task |
| `max_rounds` | int | `200` | Maximum LLM rounds |
| `context_window` | int | `200000` | Agent context window |

### Settings Configuration (`privacy_rules.json`)

Agent settings are configured in `~/.dpc/privacy_rules.json`:

```json
{
  "dpc_agent": {
    "enabled": true,
    "background_consciousness": false,
    "tools": {
      "repo_read": true,
      "repo_list": true,
      "update_scratchpad": true,
      "browse_page": true,
      "search_web": true
    },
    "budget_usd": 50,
    "max_rounds": 200,
    "evolution_enabled": false,
    "evolution_interval_minutes": 60,
    "evolution_auto_apply": false,
    "extended_sandbox_paths": {
      "read_only": ["~/Documents/projects"],
      "read_write": ["~/Documents/agent-workspace"]
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Master toggle for agent |
| `background_consciousness` | bool | `false` | Enable proactive thinking |
| `tools` | object | `{}` | Tool enable/disable map (empty = all core tools) |
| `budget_usd` | float | `50.0` | Maximum budget per task |
| `max_rounds` | int | `200` | Maximum LLM rounds |
| `evolution_enabled` | bool | `false` | Enable self-modification |
| `evolution_interval_minutes` | int | `60` | Evolution cycle interval |
| `evolution_auto_apply` | bool | `false` | Auto-apply evolution changes |
| `extended_sandbox_paths` | object | `{}` | Paths outside sandbox (read_only, read_write) |

## Available Tools

### Core Tools (Always Available)

| Tool | Description | Safe |
|------|-------------|------|
| `repo_read` | Read files in sandbox | ✅ |
| `repo_list` | List files in sandbox | ✅ |
| `update_scratchpad` | Update working memory | ✅ |
| `update_identity` | Update self-understanding | ✅ |
| `browse_page` | Fetch and parse web pages | ✅ |
| `search_web` | DuckDuckGo search | ✅ |
| `self_review` | Content quality analysis | ✅ |
| `request_critique` | Devil's advocate analysis | ✅ |

### Browser Tools

| Tool | Description |
|------|-------------|
| `browse_page` | Fetch and parse web page content |
| `fetch_json` | Fetch JSON from API endpoints |
| `extract_links` | Extract links from web pages |
| `check_url` | Check URL accessibility |
| `search_web` | Search via DuckDuckGo (no API key) |

### Review Tools

| Tool | Description |
|------|-------------|
| `self_review` | Self-review content for quality |
| `request_critique` | Critical devil's advocate analysis |
| `compare_approaches` | Compare multiple approaches |
| `quality_checklist` | Generate quality checklist |
| `consensus_check` | Check for consensus among responses |

### Memory & Knowledge Tools

| Tool | Description | Safe |
|------|-------------|------|
| `update_scratchpad` | Update working memory | ✅ |
| `update_identity` | Update self-understanding | ✅ |
| `deduplicate_identity` | Remove duplicate sections from identity | ✅ |
| `knowledge_read` | Read knowledge base files | ✅ |
| `knowledge_write` | Write to knowledge base | ✅ |
| `knowledge_list` | List knowledge topics | ✅ |
| `extract_knowledge` | Extract knowledge from conversations | ✅ |
| `get_dpc_context` | Get DPC personal/device context | ✅ |

### Task Scheduling Tools

| Tool | Description | Safe |
|------|-------------|------|
| `schedule_task` | Schedule a background task | ✅ |
| `get_task_status` | Check task execution status | ✅ |
| `register_task_type` | Register custom task type | ✅ |
| `list_task_types` | List available task types | ✅ |
| `unregister_task_type` | Remove custom task type | ✅ |

### Evolution Control Tools

| Tool | Description | Safe |
|------|-------------|------|
| `pause_evolution` | Pause self-modification | ✅ |
| `resume_evolution` | Resume self-modification | ✅ |
| `get_evolution_stats` | View evolution statistics | ✅ |
| `approve_evolution_change` | Approve pending change | ✅ |
| `reject_evolution_change` | Reject pending change | ✅ |

### Extended Sandbox Tools (v0.16.0+)

These tools access paths outside `~/.dpc/agent/` via firewall-controlled permissions:

| Tool | Description | Risk |
|------|-------------|------|
| `extended_path_read` | Read from firewall-allowed paths | Medium |
| `extended_path_list` | List firewall-allowed directories | Low |
| `extended_path_write` | Write to firewall-allowed paths | Medium |
| `list_extended_sandbox_paths` | List configured extended paths | Low |

### Search Tools (v0.16.0+)

| Tool | Description | Safe |
|------|-------------|------|
| `search_files` | Search for files by name pattern | ✅ |
| `search_in_file` | Search content within files | ✅ |

### Messaging Tools

| Tool | Description | Safe |
|------|-------------|------|
| `send_user_message` | Send message to user via DPC | ✅ |

### Git Tools (Restricted)

| Tool | Description | Risk |
|------|-------------|------|
| `git_status` | Check git status | Low |
| `git_diff` | View changes | Low |
| `git_log` | View history | Low |
| `git_branch` | List branches | Low |
| `git_add` | Stage files | Medium |
| `git_commit` | Create commit | Medium |
| `git_init` | Initialize repo | Medium |

### Restricted Tools (Require Firewall Enablement)

These tools are restricted by default and must be explicitly enabled in the firewall:

| Tool | Description | Risk Level | Status |
|------|-------------|------------|--------|
| `git_add` | Stage git files | Medium | ✅ Implemented |
| `git_commit` | Create git commits | Medium | ✅ Implemented |
| `git_init` | Initialize git repo | Medium | ✅ Implemented |

### Planned Tools (Not Yet Implemented)

These tools are planned for future releases. See [implementation plan](../../ideas/implement-missing-agent-tools.md) for details:

| Tool | Description | Risk Level |
|------|-------------|------------|
| `run_shell` | Execute shell commands (whitelist-controlled) | ⚠️ High |
| `repo_commit_push` | Push to remote git repositories | Medium |
| `claude_code_edit` | Structured code editing operations | Medium |

## Storage Structure

All agent data is stored in `~/.dpc/agent/`:

```
~/.dpc/agent/
├── memory/
│   ├── scratchpad.md         # Working memory
│   ├── identity.md           # Self-understanding
│   └── dialogue_summary.md   # Conversation summaries
├── knowledge/                 # Knowledge base
│   ├── _index.md             # Topic index
│   └── [topic].md            # Topic files
├── logs/
│   ├── events.jsonl          # Event log
│   ├── tools.jsonl           # Tool execution log
│   ├── progress.jsonl        # Progress messages
│   └── consciousness.jsonl   # Background thoughts
├── state/
│   └── state.json            # Budget, status
└── task_results/             # Subtask results
```

## Tool Access Control

Tool access is controlled via the DPC firewall system (`~/.dpc/privacy_rules.json`). This provides fine-grained control over which tools the agent can use.

### Basic Tool Control

Enable or disable individual tools in the `dpc_agent.tools` object:

```json
{
  "dpc_agent": {
    "enabled": true,
    "tools": {
      "repo_read": true,
      "repo_list": true,
      "run_shell": false,
      "repo_commit_push": false
    }
  }
}
```

### Extended Sandbox Paths

By default, the agent is restricted to `~/.dpc/agent/`. Extended sandbox paths allow controlled access to additional directories:

```json
{
  "dpc_agent": {
    "extended_sandbox_paths": {
      "read_only": ["~/Documents/projects", "~/Downloads"],
      "read_write": ["~/Documents/agent-workspace"]
    }
  }
}
```

- **read_only**: Agent can read but not modify files in these paths
- **read_write**: Agent can read and write files in these paths

### Shell Command Whitelist (Planned)

When `run_shell` is implemented, allowed commands will be controlled via:

```json
{
  "dpc_agent": {
    "shell_allowed_commands": [
      "echo", "cat", "ls", "pwd", "whoami", "date",
      "grep", "find", "head", "tail", "wc"
    ]
  }
}
```

### Git Configuration (Planned)

When `repo_commit_push` is implemented, git access will be controlled via:

```json
{
  "dpc_agent": {
    "git": {
      "enabled": true,
      "user_name": "DPC Agent",
      "user_email": "agent@example.com",
      "auth_method": "ssh",
      "ssh_key_path": ".ssh/id_ed25519",
      "allowed_remotes": {
        "origin": "git@github.com:user/repo.git"
      },
      "allowed_branches": ["main", "develop"]
    }
  }
}
```

### UI Configuration

Use the **Firewall Editor** UI (click 🛡️ Firewall Rules in sidebar) to configure agent settings visually. The Agent tab provides:

- Tool enable/disable toggles
- Extended sandbox path configuration
- Evolution settings
- Budget controls

## Background Consciousness

When enabled, the agent thinks proactively between tasks:

### Thought Types

1. **Identity Reflection** (20%): Think about who it is becoming
2. **Action Review** (25%): Analyze recent tool calls
3. **Improvement Planning** (20%): Consider how to improve
4. **Memory Consolidation** (20%): Summarize and organize
5. **Curiosity Exploration** (15%): Learn something new

### Configuration

```ini
[dpc_agent]
background_consciousness = true
```

### Monitoring

View consciousness logs:
```bash
tail -f ~/.dpc/agent/logs/consciousness.jsonl
```

## Remote Peer Inference (v0.18.1+)

The agent can route LLM requests to remote peers for distributed compute. This allows using more powerful models running on a peer's machine.

### Configuration

Add peer routing settings to your provider configuration in `~/.dpc/providers.json`:

```json
{
  "alias": "dpc_agent_remote",
  "type": "dpc_agent",
  "peer_id": "dpc-node-alice-123",
  "remote_model": "llama3:70b",
  "remote_provider": "ollama",
  "timeout": 300
}
```

| Field | Description |
|-------|-------------|
| `peer_id` | Target peer's node ID |
| `remote_model` | Model to use on remote peer |
| `remote_provider` | Provider type on remote (ollama, openai, etc.) |
| `timeout` | Request timeout in seconds (default: 300, max: 600) |

### Requirements

- Remote peer must have compute sharing enabled in their firewall
- Remote peer must have the requested model available
- P2P connection must be established with the remote peer

## Testing

### 1. Basic Smoke Test

Start the DPC backend and verify agent initialization:

```bash
cd dpc-client/core
poetry run python -c "
from dpc_client_core.dpc_agent import DpcAgent, AgentConfig
from dpc_client_core.llm_manager import LLMManager

# Create LLM manager
llm = LLMManager()

# Create agent
config = AgentConfig(budget_usd=5.0, max_rounds=10)
agent = DpcAgent(llm_manager=llm, config=config)

print('Agent initialized:', agent.get_status())
"
```

### 2. Tool Registry Test

Verify tools are loaded:

```bash
poetry run python -c "
from dpc_client_core.dpc_agent.tools import ToolRegistry

registry = ToolRegistry()
tools = registry.available_tools()
print(f'Loaded {len(tools)} tools:')
for t in sorted(tools):
    print(f'  - {t}')
"
```

### 3. Memory Test

Test memory operations:

```bash
poetry run python -c "
from dpc_client_core.dpc_agent import Memory

memory = Memory()
memory.ensure_files()

print('Scratchpad:', memory.scratchpad_path())
print('Identity:', memory.identity_path())

scratch = memory.load_scratchpad()
print(f'Scratchpad content ({len(scratch)} chars)')
"
```

### 4. Consciousness Test

Test background consciousness:

```bash
poetry run python -c "
import asyncio
from dpc_client_core.dpc_agent import DpcAgent, AgentConfig, BackgroundConsciousness
from dpc_client_core.llm_manager import LLMManager

async def test():
    llm = LLMManager()
    config = AgentConfig(background_consciousness=True)
    agent = DpcAgent(llm_manager=llm, config=config)

    # Start consciousness
    agent.start_consciousness()
    print('Consciousness running:', agent.is_consciousness_running())

    # Let it think for a moment
    await asyncio.sleep(2)

    # Stop
    agent.stop_consciousness()
    print('Consciousness stopped')

asyncio.run(test())
"
```

### 5. Provider Integration Test

Test via DpcAgentProvider:

```bash
poetry run python -c "
import asyncio
from dpc_client_core.llm_manager import LLMManager

async def test():
    llm = LLMManager()

    # Check if dpc_agent provider is configured
    if 'dpc_agent' in llm.providers:
        provider = llm.providers['dpc_agent']
        print(f'Provider type: {type(provider).__name__}')
        print(f'Supports vision: {provider.supports_vision()}')
        print(f'Supports thinking: {provider.supports_thinking()}')
    else:
        print('dpc_agent provider not configured')

asyncio.run(test())
"
```

## Usage Examples

### Example 1: Simple Query

```
User: What files are in my agent directory?

Agent: I'll check the files in your agent sandbox.
[Uses repo_list tool]

Your agent directory contains:
- memory/
  - scratchpad.md
  - identity.md
- logs/
  - events.jsonl
- state/
  - state.json
```

### Example 2: Web Research

```
User: Search for the latest Python 3.12 features

Agent: I'll search for Python 3.12 features.
[Uses search_web tool]

Here are the latest Python 3.12 features:
1. Improved error messages
2. Per-Interpreter GIL
3. Improved typing features
...
```

### Example 3: Memory Update

```
User: Remember that I prefer functional programming

Agent: I'll update my scratchpad with this preference.
[Uses update_scratchpad tool]

Done. I've noted your preference for functional programming
in my working memory.
```

### Example 4: Self-Review

```
User: Review this code for me: [code snippet]

Agent: Let me perform a quality review.
[Uses self_review tool]

**Strengths:**
- Clear variable naming
- Good error handling

**Issues:**
- Missing type hints
- No docstrings

**Suggestions:**
- Add type annotations
- Include docstrings for functions

**Rating: 7/10** - Good foundation, minor improvements needed
```

## Troubleshooting

### Agent Not Initializing

1. Check LLMManager has a default provider configured
2. Verify `~/.dpc/agent/` directory exists and is writable
3. Check logs: `~/.dpc/logs/dpc-client.log`

### Tools Not Working

1. Check tool is in the whitelist (config `tools` field)
2. Verify sandbox permissions (`~/.dpc/agent/` directory)
3. Check tool logs: `~/.dpc/agent/logs/tools.jsonl`

### Budget Exceeded

1. Check current spend: `~/.dpc/agent/state/state.json`
2. Increase `budget_usd` in configuration
3. Reset budget by editing state file (not recommended)

### Consciousness Not Running

1. Verify `background_consciousness = true` in config
2. Check consciousness is enabled in AgentConfig
3. View consciousness logs: `~/.dpc/agent/logs/consciousness.jsonl`

## Security Considerations

### Sandbox Boundaries

The agent is restricted to `~/.dpc/agent/`:

- ✅ **CAN** read/write files in sandbox
- ✅ **CAN** modify its own memory and identity
- ✅ **CAN** access web resources
- ❌ **CANNOT** access DPC codebase
- ❌ **CANNOT** access `~/.dpc/personal.json`
- ❌ **CANNOT** access `~/.dpc/config.ini`

### Tool Whitelisting

Tool access is controlled via the firewall (`~/.dpc/privacy_rules.json`). See [Tool Access Control](#tool-access-control) for details.

Quick example - restrict to safe tools only:

```json
{
  "dpc_agent": {
    "tools": {
      "repo_read": true,
      "repo_list": true,
      "browse_page": true,
      "search_web": true,
      "run_shell": false,
      "repo_commit_push": false
    }
  }
}
```

### Budget Control

- Per-task budget limit
- Hard stop at 50% of remaining budget
- Background consciousness capped at 10% of total

## API Reference

### DpcAgentManager

```python
from dpc_client_core.managers.agent_manager import DpcAgentManager

manager = DpcAgentManager(service, config)
await manager.start()
response = await manager.process_message("Hello", "conv-123")
await manager.stop()
```

### DpcAgentProvider

```python
from dpc_client_core.llm_manager import LLMManager

llm = LLMManager()
provider = llm.providers.get("dpc_agent")
response = await provider.generate_response("Hello")
```

### Direct Agent Usage

```python
from dpc_client_core.dpc_agent import DpcAgent, AgentConfig

config = AgentConfig(
    budget_usd=50.0,
    max_rounds=200,
    background_consciousness=False,
)
agent = DpcAgent(llm_manager=llm, config=config)
response = await agent.process("Hello", "conv-123")
```

## Next Steps

1. **Add Custom Tools**: Create new tool modules in `dpc_agent/tools/`
2. **Extend Memory**: Add new memory types or knowledge structures
3. **Custom Consciousness**: Modify thought types in `consciousness.py`
4. **UI Integration**: Add agent status/controls to DPC UI
