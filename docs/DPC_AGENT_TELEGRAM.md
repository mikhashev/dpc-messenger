# DPC Agent Telegram Integration

## ⚠️ DEPRECATION NOTICE

**The `[dpc_agent_telegram]` system is DEPRECATED as of v0.15.0 and will be REMOVED in v0.20.0.**

### What's Deprecated?

The **old system** using `[dpc_agent_telegram]` config section in `~/.dpc/config.ini`:
- ❌ Separate bot just for agent events
- ❌ One bridge for ALL agents
- ❌ Requires managing two Telegram bots
- ❌ Global configuration for all agents

### What's New?

The **new system** using **per-agent Telegram linking**:
- ✅ Uses main `[telegram]` bot (same bot for P2P + agents)
- ✅ Per-agent configuration in `agent's config.json`
- ✅ Each agent can have separate Telegram chat
- ✅ Simpler: one bot instead of two
- ✅ Granular control: enable/disable per agent

### Migration Timeline

- **v0.15.0** (current): Deprecation warning added
- **v0.20.0** (future): Old system will be **REMOVED**

---

## Quick Migration Guide

### Before (Old System - DEPRECATED)

```ini
# ~/.dpc/config.ini
[dpc_agent_telegram]
enabled = true
bot_token = 8237861519:AAFDhSOaZrxxRnCTweaoav__QrMYNZOKhO0
allowed_chat_ids = ["429727247"]
event_filter = task_completed,task_failed,evolution_cycle_completed,code_modified,agent_message
```

### After (New System - RECOMMENDED)

**Step 1:** Ensure main Telegram bot is configured in `~/.dpc/config.ini`:
```ini
[telegram]
enabled = true
bot_token = 8307009971:AAGUisLxzksqw_CGW9vrg_kslUkrf2zWruo
allowed_chat_ids = ["429727247", "41783586", "475097760", "454942631"]
```

**Step 2:** Link each agent individually in its `config.json`:
```json
// ~/.dpc/agents/default/config.json
{
  "agent_id": "default",
  "name": "Default Agent",
  "telegram_enabled": true,
  "telegram_chat_id": "429727247",
  "telegram_linked_at": "2026-03-06T18:30:00.000000+00:00"
}
```

**Step 3:** Remove old `[dpc_agent_telegram]` section from `~/.dpc/config.ini`

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
poetry run python run_service.py
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

### Comparison: Old vs New System

| Feature | Old System | New System |
|---------|-----------|-----------|
| **Config location** | `config.ini` (global) | `agents/{id}/config.json` (per-agent) |
| **Bot** | Separate bot | Main bot (shared with P2P) |
| **Scope** | All agents | Per-agent |
| **Granularity** | Global on/off | Per-agent on/off |
| **Two-way messaging** | ✅ Yes | ✅ Yes |
| **Voice messages** | ✅ Yes | ✅ Yes |
| **Agent-initiated messages** | ✅ Yes | ✅ Yes |
| **Multiple chats** | ❌ One for all agents | ✅ Each agent can have own chat |
| **Status** | ⚠️ Deprecated | ✅ Recommended |

## Related Documentation

- [DPC Agent Guide](DPC_AGENT_GUIDE.md) - General agent usage
- [Telegram Setup Guide](TELEGRAM_SETUP.md) - Main Telegram bot configuration
- [Configuration](CONFIGURATION.md) - Full configuration reference
- [CLAUDE.md](../CLAUDE.md) - Project development guide
