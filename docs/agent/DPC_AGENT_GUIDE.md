# DPC Embedded Agent - Testing and Usage Guide

This guide explains how to configure, test, and use the embedded autonomous AI agent in DPC Messenger.

## Overview

The embedded agent is a self-modifying AI agent adapted from the [Ouroboros project](https://github.com/razzant/ouroboros), integrated directly into DPC Messenger's codebase. It provides:

- **Tools**: File operations, web search, memory management, git, task scheduling, evolution control, skill execution
- **Background Consciousness**: Proactive thinking between tasks (optional)
- **Persistent Memory**: Scratchpad, identity, and knowledge base
- **Self-Modification**: Can modify files within its sandbox (`~/.dpc/agent/`)
- **DPC Integration**: Uses DPC's LLM providers and personal/device context

## Architecture

```
DPC Messenger
    └── DpcAgentProvider (AIProvider)
            └── DpcAgentManager
                    ├── AgentRegistry (manages multiple agents)
                    └── DpcAgent (per-instance)
                            ├── ToolRegistry (sandboxed to ~/.dpc/agents/{agent_id}/)
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

### 4. Managing Multiple Agents

The DPC Agent system supports multiple isolated agents, each with its own configuration, storage, and permission profile.

#### Creating Agents

**Via UI:**


1. Click **"🛡️ Firewall Rules"** in sidebar


2. Go to **Agent Profiles** tab


3. Click **"Add Agent"** button


4. Fill in:
   - **Name**: Human-readable name (e.g., "Research Assistant")
   - **Provider**: LLM provider from `providers.json`
   - **Profile**: Permission profile (default/researcher/coder)
   - **Instruction Set**: Task specialization (optional)


5. Click **Create**

**Via Backend API:**
```python
result = await service.create_agent(
    name="Research Assistant",
    provider_alias="ollama_llama3",
    profile_name="researcher",
    instruction_set_name="general",
    budget_usd=5

0.0,
    max_rounds=200
)
```

#### Listing Agents

**Via UI:** Agents appear in sidebar under "Agents" section

**Via Backend API:**
```python
result = await service.list_agents()
# Returns: {"status": "success", "agents": [...]}
```

#### Updating Agent Config

**Via UI:**


1. Click agent in sidebar


2. Click **Edit** in Agent Permissions panel


3. Modify tools, evolution settings, context access


4. Click **Save** (creates custom profile for agent)

**Via Backend API:**
```python
result = await service.update_agent_config(
    agent_id="agent_abc123researcher",
    updates={"budget_usd": 10

0.0, "profile_name": "coder"}
)
```

#### Deleting Agents

**Via UI:**


1. Right-click agent in sidebar


2. Click **Delete Agent**


3. Confirm deletion

**Via Backend API:**
```python
result = await service.delete_agent(agent_id="agent_abc123researcher")
# Deletes agent registry entry + storage directory
```

#### Profile Inheritance

Agents can inherit settings from the global `dpc_agent` configuration:

- **Inheriting**: Agent uses global `dpc_agent` settings
- **Custom**: Agent has its own profile (overrides global)
- **UI indicator**: Banner shows "Using inherited settings" or "Custom profile"

Reset to inherited:
```python
result = await service.reset_agent_to_global(agent_id="agent_abc123")
```

## Configuration Reference

### Provider Configuration (`providers.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `alias` | string | required | Provider alias name |
| `type` | string | required | Must be `"dpc_agent"` |
| `tools` | string[] | `[]` | Tool whitelist (empty = all core tools) |
| `background_consciousness` | bool | `false` | Enable proactive thinking |
| `budget_usd` | float | `5

0.0` | Maximum budget per task |
| `max_rounds` | int | `200` | Maximum LLM rounds |
| `context_window` | int | `200000` | Agent context window |

### Firewall Rules Configuration (`privacy_rules.json`)

Agent tool permissions and sandbox paths are configured in
`~/.dpc/privacy_rules.json` (editable via the Firewall Rules UI):

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
| `budget_usd` | float | `5

0.0` | Maximum budget per task |
| `max_rounds` | int | `200` | Maximum LLM rounds |
| `evolution_enabled` | bool | `false` | Enable self-modification |
| `evolution_interval_minutes` | int | `60` | Evolution cycle interval |
| `evolution_auto_apply` | bool | `false` | Auto-apply evolution changes |
| `extended_sandbox_paths` | object | `{}` | Paths outside sandbox (read_only, read_write) |
| `skills.self_modify` | bool | `true` | Allow agent to append improvements to skill files |
| `skills.create_new` | bool | `true` | Allow agent to create new skill files |
| `skills.rewrite_existing` | bool | `false` | Allow full skill rewrites (not just appends) |
| `skills.accept_peer_skills` | bool | `false` | Accept skills received from peers (Phase 5) |

### Agent Profiles Configuration

Agent profiles define reusable permission templates in `~/.dpc/privacy_rules.json`:

```json
{
  "agent_profiles": {
    "default": {
      "enabled": true,
      "personal_context_access": true,
      "device_context_access": true,
      "knowledge_access": "read_only",
      "tools": {
        "repo_read": true,
        "repo_list": true,
        "update_scratchpad": true,
        "browse_page": true,
        "search_web": true
      },
      "evolution": {
        "enabled": false,
        "interval_minutes": 60,
        "auto_apply": false
      }
    },
    "researcher": {
      "enabled": true,
      "tools": {
        "browse_page": true,
        "search_web": true,
        "fetch_json": true,
        "knowledge_read": true,
        "knowledge_write": true
      }
    },
    "coder": {
      "enabled": true,
      "tools": {
        "repo_read": true,
        "repo_list": true,
        "git_status": true,
        "git_diff": true,
        "search_files": true,
        "search_in_file": true
      }
    }
  }
}
```

**Profile Inheritance:**
- Agents inherit from `dpc_agent` (global defaults) if no custom profile is specified
- Custom profiles override specific settings (tools, evolution, context access)
- `inherit_from` field allows chaining profiles (planned feature)

### Agent Registry

The agent registry tracks all created agents in `~/.dpc/agents/_registry.json`:

```json
{
  "version": 1,
  "agents": {
    "agent_abc123researcher": {
      "agent_id": "agent_abc123researcher",
      "name": "Research Assistant",
      "provider_alias": "ollama_llama3",
      "profile_name": "researcher",
      "created_at": "2026-03-06T10:00:00Z",
      "instruction_set_name": "general"
    },
    "agent_def456coder": {
      "agent_id": "agent_def456coder",
      "name": "Code Reviewer",
      "provider_alias": "openai_gpt4",
      "profile_name": "coder",
      "created_at": "2026-03-06T11:00:00Z",
      "instruction_set_name": "code_review"
    }
  }
}
```

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
| `execute_skill` | Load a skill strategy by name (Memento-Skills router) | ✅ |
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

### Extended Sandbox Tools

These tools access paths outside `~/.dpc/agent/` via firewall-controlled permissions:

| Tool | Description | Risk |
|------|-------------|------|
| `extended_path_read` | Read from firewall-allowed paths | Medium |
| `extended_path_list` | List firewall-allowed directories | Low |
| `extended_path_write` | Write to firewall-allowed paths | Medium |
| `list_extended_sandbox_paths` | List configured extended paths | Low |

### Search Tools

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

## Storage Structure

All agent data is stored in `~/.dpc/agents/{agent_id}/`:

```
~/.dpc/agents/                    # Base directory for all agents
├── _registry.json               # Agent registry (metadata for all agents)
├── agent_abc123researcher/      # Per-agent storage (isolated)
│   ├── memory/
│   │   ├── scratchpad.md         # Working memory
│   │   ├── identity.md           # Self-understanding
│   │   └── dialogue_summary.md   # Conversation summaries
│   ├── knowledge/                 # Knowledge base
│   │   ├── _index.md             # Topic index
│   │   └── [topic].md            # Topic files
│   ├── skills/                    # Memento-Skills
│   │   ├── _stats.json           # Per-skill performance tracking
│   │   ├── skill-creator/SKILL.md
│   │   ├── code-analysis/SKILL.md
│   │   ├── knowledge-extraction/SKILL.md
│   │   ├── p2p-research/SKILL.md
│   │   ├── web-research/SKILL.md
│   │   └── pending_improvements.jsonl  # Shadow-mode queue
│   ├── logs/
│   │   ├── events.jsonl          # Event log
│   │   ├── tools.jsonl           # Tool execution log
│   │   ├── progress.jsonl        # Progress messages
│   │   └── consciousness.jsonl   # Background thoughts
│   ├── state/
│   │   └── state.json            # Budget, status
│   └── task_results/             # Subtask results
└── agent_def456coder/           # Another agent's storage
    ├── memory/
    └── ...

~/.dpc/agent/                    # Legacy location (auto-migrated)
```

### Migration from Legacy Setup

The system automatically migrates the legacy single-agent setup to the new multi-agent system:

- Legacy storage (`~/.dpc/agent/`) → Migrated to `~/.dpc/agents/default/`
- Legacy global config → Imported as `default` profile
- Migration happens on first startup after upgrade
- Original files are preserved (copied, not moved)

## Tool Access Control

Tool access is controlled via the DPC firewall system (`~/.dpc/privacy_rules.json`). This provides fine-grained control over which tools the agent can use.

### Per-Agent Tool Control

Tools can be enabled/disabled per agent via profiles:

```json
{
  "agent_profiles": {
    "safe_agent": {
      "tools": {
        "repo_read": true,
        "browse_page": true,
        "search_web": true,
        "run_shell": false,
        "git_commit": false
      }
    },
    "advanced_agent": {
      "tools": {
        "run_shell": true,
        "git_commit": true,
        "repo_commit_push": true
      }
    }
  }
}
```

**Inheritance priority:**


1. Agent-specific profile (highest priority)


2. Global `dpc_agent` settings (fallback)


3. Default tool allowlist (lowest priority)

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

By default, each agent is restricted to its own sandbox at `~/.dpc/agents/{agent_id}/`. Extended sandbox paths allow controlled access to additional directories:

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

## Remote Peer Inference

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
uv run python -c "
from dpc_client_core.dpc_agent import DpcAgent, AgentConfig
from dpc_client_core.llm_manager import LLMManager

# Create LLM manager
llm = LLMManager()

# Create agent
config = AgentConfig(budget_usd=

5.0, max_rounds=10)
agent = DpcAgent(llm_manager=llm, config=config)

print('Agent initialized:', agent.get_status())
"
```

### 2. Tool Registry Test

Verify tools are loaded:

```bash
uv run python -c "
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
uv run python -c "
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
uv run python -c "
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
uv run python -c "
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
User: Search for the latest Python 

3.12 features

Agent: I'll search for Python 

3.12 features.
[Uses search_web tool]

Here are the latest Python 

3.12 features:


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

### Example 5: Multi-Agent Workflow

```
User: I have three agents:
- Research Assistant (uses Ollama llama3, web browsing)
- Code Reviewer (uses GPT-4, git tools)
- Notification Bot (sends Telegram updates)

Research Agent: I'll search for the latest papers on LLMs.
[Uses search_web, browse_page tools]

Code Reviewer: Let me check your recent commits.
[Uses git_status, git_diff tools]

Notification Bot: Sending update to Telegram...
[Uses send_user_message tool]
```

### Example 6: Per-Agent Provider Selection

```json
{
  "providers": [
    {"alias": "ollama_llama3", "type": "ollama", "model": "llama3"},
    {"alias": "openai_gpt4", "type": "openai", "model": "gpt-4"}
  ],
  "agent_profiles": {
    "fast_agent": {
      "provider_alias": "ollama_llama3",
      "budget_usd": 1

0.0
    },
    "smart_agent": {
      "provider_alias": "openai_gpt4",
      "budget_usd": 10

0.0
    }
  }
}
```

## Troubleshooting

### Agent Not Initializing



1. Check LLMManager has a default provider configured


2. Verify `~/.dpc/agents/{agent_id}/` directory exists and is writable


3. Check logs: `~/.dpc/logs/dpc-client.log`

### Tools Not Working



1. Check tool is in the whitelist (config `tools` field)


2. Verify sandbox permissions (`~/.dpc/agents/{agent_id}/` directory)


3. Check tool logs: `~/.dpc/agents/{agent_id}/logs/tools.jsonl`

### Budget Exceeded



1. Check current spend: `~/.dpc/agent/state/state.json`


2. Increase `budget_usd` in configuration



### Agent Not Listed in Sidebar

1. Check agent registry: `cat ~/.dpc/agents/_registry.json`

2. Verify agent was created successfully in logs

3. Check that agent storage exists: `ls ~/.dpc/agents/`

### Agent Not Using Custom Profile

1. Check if agent has custom profile in registry

2. Verify profile exists in `privacy_rules.json`

3. Look for "Using inherited settings" banner in UI

4. Check logs for profile loading errors

### Migration Issues

1. Legacy files not migrated? Check logs: `tail -f ~/.dpc/logs/dpc-client.log`

2. Manual migration: `cp -r ~/.dpc/agent ~/.dpc/agents/default`

3. Verify registry created: `cat ~/.dpc/agents/_registry.json`



1. Verify `background_consciousness = true` in config


2. Check consciousness is enabled in AgentConfig


3. View consciousness logs: `~/.dpc/agent/logs/consciousness.jsonl`

## Security Considerations

### Sandbox Boundaries

Each agent is restricted to its own sandbox at `~/.dpc/agents/{agent_id}/`:

- ✅ **CAN** read/write files in its own sandbox
- ✅ **CAN** modify its own memory and identity
- ✅ **CAN** access web resources
- ❌ **CANNOT** access other agents' storage
- ❌ **CANNOT** access DPC codebase
- ❌ **CANNOT** access `~/.dpc/personal.json`
- ❌ **CANNOT** access `~/.dpc/config.ini`

**Multi-Agent Isolation:**
- Each agent has isolated storage directory
- Agents cannot access other agents' files
- Agent registry is managed by DPC (not agent-writable)

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
    budget_usd=5

0.0,
    max_rounds=200,
    background_consciousness=False,
)
agent = DpcAgent(llm_manager=llm, config=config)
response = await agent.process("Hello", "conv-123")
```

### Agent Management API

```python
# Create agent
await service.create_agent(
    name="My Agent",
    provider_alias="dpc_agent",
    profile_name="default",
    instruction_set_name="general"
)

# List agents
await service.list_agents()

# Get agent config
await service.get_agent_config(agent_id="agent_abc123")

# Update agent config
await service.update_agent_config(
    agent_id="agent_abc123",
    updates={"budget_usd": 10

0.0}
)

# Delete agent
await service.delete_agent(agent_id="agent_abc123")

# Reset agent to inherited settings
await service.reset_agent_to_global(agent_id="agent_abc123")
```

## Next Steps



1. **Add Custom Tools**: Create new tool modules in `dpc_agent/tools/`


2. **Write Custom Skills**: Add `~/.dpc/agents/{id}/skills/{name}/SKILL.md` — see [DPC Agent Skills Guide](DPC_AGENT_SKILLS.md)


3. **Extend Memory**: Add new memory types or knowledge structures


4. **Custom Consciousness**: Modify thought types in `consciousness.py`


5. **UI Integration**: Add agent status/controls to DPC UI

## Memento-Skills System

The agent implements a Memento-Skills style Read-Write Reflective Learning loop:

- **Skills** are markdown strategy files that teach the agent *how to combine tools* for a class of tasks
- **Read phase**: before each task, the agent sees all skill descriptions and calls `execute_skill()` to load the relevant strategy
- **Write phase**: after tasks with ≥5 LLM rounds, the agent reflects on whether the skill had gaps and appends improvements
- **Evolution integration**: `evolution.py` reads skill performance stats (`_stats.json`) and targets underperforming skills

See **[DPC Agent Skills Guide](DPC_AGENT_SKILLS.md)** for full documentation including skill format, reflection loop, firewall permissions, and how to write custom skills.

---

## Related

- **[DPC_AGENT_SKILLS.md](./DPC_AGENT_SKILLS.md)** — Memento-Skills system: teach the agent multi-step strategies, skill reflection loop, custom skills
- **[DPC_AGENT_TELEGRAM.md](./DPC_AGENT_TELEGRAM.md)** — link an agent to a Telegram chat (per-agent linking or legacy `[dpc_agent_telegram]`)
- **[CC_INTEGRATION_GUIDE.md](./CC_INTEGRATION_GUIDE.md)** — connect Claude Code as a second participant in the same agent chat
- **[../CONFIGURATION.md](../CONFIGURATION.md)** — full client configuration reference
- **[../../QUICK_START.md](../../QUICK_START.md)** — user-facing quick start (install + first agent creation via UI)
