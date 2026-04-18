# DPC Agent Telegram Integration

Two Telegram integration systems coexist in the DPC client. Both are
functional. The **current system** (per-agent linking) is recommended for
new agents; the **legacy system** (`[dpc_agent_telegram]` INI section) is
still supported and used by existing deployments.

---

## Two Systems — Overview

### Current System — Per-Agent Linking

Each agent links itself to a Telegram chat through fields in its own
`config.json`. Uses the main `[telegram]` bot (same bot as P2P messaging),
so one bot token covers everything.

Configured in `~/.dpc/agents/{agent_id}/config.json`:

- `telegram_enabled` (bool)
- `telegram_chat_id` (string)
- `telegram_linked_at` (ISO timestamp)

Full setup: see [Per-Agent Telegram Linking](#per-agent-telegram-linking-current-system) below.

### Legacy System — Global `[dpc_agent_telegram]` INI Section

One dedicated bot shared across all agents, configured globally in
`~/.dpc/config.ini` under `[dpc_agent_telegram]`. Pre-dates per-agent
linking.

Still functional — the client reads the INI section if present, and an
agent with no per-agent Telegram fields falls back to the global config
with a deprecation warning in the log.

**Code touchpoints** (for reviewers working in this area):

| File | Role |
|------|------|
| `dpc-client/core/dpc_client_core/settings.py` | `get_dpc_agent_telegram_*` accessors read the INI section |
| `dpc-client/core/dpc_client_core/telegram_service.py` | Entrypoint for legacy-to-per-agent migration |
| `dpc-client/core/dpc_client_core/dpc_agent/utils.py` | `migrate_global_dpc_agent_telegram_config()` — copies INI values into the default agent's `config.json` |
| `dpc-client/core/dpc_client_core/managers/agent_manager.py` | Fallback: agents without per-agent fields read the global config and emit a deprecation warning |
| `dpc-client/core/dpc_client_core/managers/agent_telegram_bridge.py` | Bridge that forwards agent events to Telegram |

There is no planned removal date. To move an existing deployment off the
legacy system: migrate the INI config into an agent's `config.json`
(manually, or via the helper in `dpc_agent/utils.py`), then delete the
`[dpc_agent_telegram]` section.

---

## Migrating From the Legacy System

If you have an existing `[dpc_agent_telegram]` block, this is the
mechanical translation.

### Before (legacy)

```ini
# ~/.dpc/config.ini
[dpc_agent_telegram]
enabled = true
bot_token = YOUR_BOT_TOKEN_HERE
allowed_chat_ids = ["123456789"]
event_filter = task_completed,task_failed,evolution_cycle_completed,code_modified,agent_message
```

### After (current)

**Step 1** — ensure the main Telegram bot is configured in `~/.dpc/config.ini`:

```ini
[telegram]
enabled = true
bot_token = YOUR_BOT_TOKEN_HERE
allowed_chat_ids = ["123456789", "987654321"]
```

**Step 2** — link each agent individually in its `config.json`:

```json
// ~/.dpc/agents/default/config.json
{
  "agent_id": "default",
  "name": "Default Agent",
  "telegram_enabled": true,
  "telegram_chat_id": "123456789",
  "telegram_linked_at": "2026-03-06T18:30:00.000000+00:00"
}
```

**Step 3** — remove the old `[dpc_agent_telegram]` section from `~/.dpc/config.ini`.

---

## Per-Agent Telegram Linking (Current System)

### Overview

Each agent can be individually linked to Telegram for **two-way messaging**:

### Features

1. **Two-Way Messaging** - Send messages to agent and receive responses
2. **Voice Messages** - Send voice messages, agent transcribes and responds
3. **Agent-Initiated Messages** - Agent can proactively send you messages
4. **Per-Agent Control** - Each agent has independent Telegram configuration

### Architecture

```
┌─────────────┐                 ┌──────────────┐              ┌──────────┐
│  Agent A    │ ←Telegram Chat→ │              │  Main Bot   │          │
│  (default)  │     ID: 123     │   Telegram   │ ←←←←←←←←←→ │ Telegram │
└─────────────┘                 │   Manager    │              │   API    │
                                │              │              └──────────┘
┌─────────────┐                 │              |
│  Agent B    │ ←Telegram Chat→ │              |
│  (agent_X)  │     ID: 456     │              |
└─────────────┘                 └──────────────┘
```

### Configuration

#### Step 1: Configure Main Telegram Bot

Edit `~/.dpc/config.ini`:

```ini
[telegram]
enabled = true
bot_token = YOUR_BOT_TOKEN_HERE
allowed_chat_ids = ["123456789", "987654321"]
transcription_enabled = true
```

**This is the SAME bot used for:**
- P2P messaging with contacts
- Agent chat linking
- Voice message transcription

#### Step 2: Get Your Chat ID

1. Open Telegram and search for **@userinfobot**
2. Send any message
3. Save your **chat ID** (a number like `123456789`)

#### Step 3: Link Agent to Telegram

**Option A: Via UI (Recommended)**

1. Open DPC Messenger UI
2. Click on the agent in the sidebar
3. Click "Link Telegram" button
4. Select your chat ID from the list
5. Confirm linking

**Option B: Manually Edit Config**

Edit the agent's `config.json`:

```json
{
  "agent_id": "default",
  "name": "Default Agent",
  "provider_alias": "dpc_agent",
  "profile_name": "default",
  "instruction_set_name": "general",
  "telegram_enabled": true,
  "telegram_chat_id": "123456789",
  "telegram_linked_at": "2026-03-06T18:30:00.000000+00:00"
}
```

#### Step 4: Restart Service

```bash
cd dpc-client/core
uv run python run_service.py
```

### Configuration Options

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `telegram_enabled` | bool | Yes | Enable Telegram linking for this agent |
| `telegram_chat_id` | string | Yes | Your Telegram chat ID (number) |
| `telegram_linked_at` | string | Yes | ISO timestamp of when linking was created |

### Using the Telegram Bot

#### Sending Messages to Agent

Simply send any text message to the bot. The agent will process it and respond.

**Examples:**
- "What's the weather forecast?"
- "Check my recent git commits"
- "What files are in the memory directory?"
- "Schedule a task to review code in 5 minutes"

#### Voice Messages

The bot supports voice messages with automatic transcription:

1. Record a voice message in Telegram
2. Send it to the bot
3. The bot transcribes it using Whisper
4. The transcribed text is processed by the agent
5. You receive both the transcription and the agent's response

**Example flow:**
```
User: [Voice message: "Check the status of my git repository"]
Bot: 📝 Transcription: Check the status of my git repository
Bot: I'll check your git repository status...
     [Agent response with git status information]
```

### Agent-Initiated Messages

The agent can proactively send messages using the `send_user_message` tool.

**Example:**
```python
# Agent can use this tool to send messages
await send_user_message(
    message="I found an interesting pattern in the codebase...",
    priority="normal"
)
```

**Priority levels:**
| Priority | Emoji | Use Case |
|----------|-------|----------|
| `urgent` | 🔴 | Critical notifications |
| `high` | 🟠 | Important updates |
| `normal` | 🟡 | Standard messages |
| `low` | 🟢 | Informational messages |

### Troubleshooting

#### Bot Not Responding

1. Check that main `[telegram]` section has `enabled = true`
2. Verify `bot_token` is correct
3. Ensure your `chat_id` is in `allowed_chat_ids`
4. Check agent's config has `telegram_enabled = true`
5. Check service logs for errors

#### Agent Not Responding to Messages

1. Verify agent is running and not at budget limit
2. Check agent's `telegram_chat_id` matches your chat ID
3. Ensure agent has access to necessary tools
4. Check firewall rules in `~/.dpc/privacy_rules.json`

#### Voice Transcription Not Working

1. Check that `transcription_enabled = true` in `[telegram]` section
2. Ensure a Whisper provider is configured in `~/.dpc/providers.json`
3. Verify the voice provider alias is set in providers config
4. Check service logs for transcription errors

### Security Notes

- Uses main DPC Telegram bot (same bot for P2P + agents)
- Only chat IDs in `allowed_chat_ids` can interact with agents
- Each agent must have explicit `telegram_enabled = true`
- Agent can only send to its linked `telegram_chat_id`
- Messages are sent over Telegram's encrypted connection

### Advanced: Multiple Agents

You can link multiple agents to different Telegram chats:

**Agent 1 (Default Agent):**
```json
// ~/.dpc/agents/default/config.json
{
  "telegram_enabled": true,
  "telegram_chat_id": "123456789",
  "telegram_linked_at": "2026-03-06T18:30:00.000000+00:00"
}
```

**Agent 2 (Code Review Agent):**
```json
// ~/.dpc/agents/agent_code_review/config.json
{
  "telegram_enabled": true,
  "telegram_chat_id": "987654321",
  "telegram_linked_at": "2026-03-06T18:35:00.000000+00:00"
}
```

**Main Telegram Config:**
```ini
[telegram]
allowed_chat_ids = ["123456789", "987654321"]
```

Now each agent responds to messages from its linked Telegram chat!

### Comparison: Legacy vs Current System

| Feature | Legacy System | Current System |
|---------|---------------|----------------|
| **Config location** | `config.ini` (global) | `agents/{id}/config.json` (per-agent) |
| **Bot** | Separate bot | Main bot (shared with P2P) |
| **Scope** | All agents | Per-agent |
| **Granularity** | Global on/off | Per-agent on/off |
| **Two-way messaging** | ✅ Yes | ✅ Yes |
| **Voice messages** | ✅ Yes | ✅ Yes |
| **Agent-initiated messages** | ✅ Yes | ✅ Yes |
| **Multiple chats** | ❌ One for all agents | ✅ Each agent can have own chat |
| **Status** | Functional, no planned removal | Recommended for new agents |

## Related Documentation

- [DPC Agent Guide](DPC_AGENT_GUIDE.md) - General agent usage
- [Telegram Setup Guide](../TELEGRAM_SETUP.md) - Main Telegram bot configuration
- [Configuration](../CONFIGURATION.md) - Full configuration reference
- [CLAUDE.md](../../CLAUDE.md) - Project development guide
