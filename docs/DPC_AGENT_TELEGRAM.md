# DPC Agent Telegram Integration

Two-way communication between the DPC Agent and users via Telegram.

## Overview

The Agent Telegram integration enables:

1. **Event Notifications** - Receive real-time notifications about agent activity
2. **Two-way Messaging** - Send messages to the agent and receive responses
3. **Agent-initiated Communication** - Agent can proactively send messages to users

## Architecture

```
┌─────────────────┐      Events       ┌───────────────────┐      API       ┌──────────┐
│   DPC Agent     │ ─────────────────>│ AgentTelegramBridge│ ─────────────>│ Telegram │
│                 │                    │                   │               │   API    │
│  (tools, tasks) │ <───────────────── │                   │ <─────────────│          │
└─────────────────┘     Messages       └───────────────────┘    Messages   └──────────┘
        │                                      ▲
        │                                      │
        ▼                                      │
┌─────────────────┐                            │
│ AgentEventEmitter│ ──────────────────────────┘
│   (singleton)   │        Event subscription
└─────────────────┘
```

### Components

- **`AgentTelegramBridge`** - Manages Telegram bot connection and message routing
- **`AgentEventEmitter`** - Global event emitter for agent lifecycle events
- **`EventType.AGENT_MESSAGE`** - Event type for agent-initiated messages
- **`send_user_message` tool** - Agent tool for sending Telegram messages

## Configuration

### Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Save the **bot token** (looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Step 2: Get Your Chat ID

1. Open Telegram and search for **@userinfobot**
2. Send any message
3. Save your **chat ID** (a number like `123456789`)

### Step 3: Configure DPC

Edit `~/.dpc/config.ini`:

```ini
[dpc_agent_telegram]
enabled = true
bot_token = 123456789:ABCdefGHIjklMNOpqrsTUVwxyz
allowed_chat_ids = ["123456789"]
event_filter = task_completed,task_failed,evolution_cycle_completed,code_modified,agent_message
transcription_enabled = true
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `false` | Enable/disable Telegram integration |
| `bot_token` | string | "" | Telegram bot token from @BotFather |
| `allowed_chat_ids` | JSON array | `[]` | List of chat IDs to receive notifications |
| `event_filter` | comma-separated | (see below) | Event types to forward |
| `transcription_enabled` | bool | `true` | Enable voice message transcription |

### Default Event Filter

```
task_completed,task_failed,evolution_cycle_completed,code_modified,agent_message
```

## Using the Telegram Bot

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize the bot and show welcome message |
| `/help` | Show available commands and usage tips |
| `/status` | Check agent status (running, queue, etc.) |

### Sending Messages to Agent

Simply send any text message to the bot. The agent will process it and respond.

**Examples:**
- "What's the weather forecast?"
- "Check my recent git commits"
- "What files are in the memory directory?"
- "Schedule a task to review code in 5 minutes"

### Voice Messages

The bot supports voice messages with automatic transcription:

1. Record a voice message in Telegram
2. Send it to the bot
3. The bot transcribes it using Whisper
4. The transcribed text is processed by the agent
5. You receive both the transcription and the agent's response

**Requirements:**
- `transcription_enabled = true` in config (enabled by default)
- Whisper provider configured in DPC (same as voice message transcription for P2P)

**Example flow:**
```
User: [Voice message: "Check the status of my git repository"]
Bot: 📝 Transcription: Check the status of my git repository
Bot: I'll check your git repository status...
      [Agent response with git status information]
```

### Agent-Initiated Messages

The agent can proactively send messages using the `send_user_message` tool. Messages include a priority level with visual indicators:

| Priority | Emoji | Use Case |
|----------|-------|----------|
| `urgent` | 🔴 | Critical notifications |
| `high` | 🟠 | Important updates |
| `normal` | 🟡 | Standard messages |
| `low` | 🟢 | Informational messages |

## Tool Reference

### `send_user_message`

The agent uses this tool to send Telegram messages to users.

**Parameters:**
- `message` (string, required) - The message to send (Markdown supported)
- `priority` (string, optional) - One of: `urgent`, `high`, `normal`, `low`

**Example Agent Usage:**
```python
# Agent can use this tool to send messages
await send_user_message(
    message="I found an interesting pattern in the codebase...",
    priority="normal"
)
```

## Event Types

The bridge forwards these event types by default:

| Event | Description |
|-------|-------------|
| `task_completed` | A task finished successfully |
| `task_failed` | A task failed with an error |
| `evolution_cycle_completed` | Agent self-improvement cycle finished |
| `code_modified` | Agent modified code in its sandbox |
| `agent_message` | Agent-initiated message to user |

Additional events available for filtering:
- `agent_started`, `agent_stopped`
- `task_scheduled`, `task_started`
- `thought_started`, `thought_completed`
- `tool_executed`
- `budget_warning`, `rate_limit_hit`

## Rate Limiting

The bridge includes built-in rate limiting to prevent spam:

- **Max events per minute**: 20 (configurable)
- **Cooldown between same-type events**: 3 seconds (configurable)

## Troubleshooting

### Bot Not Responding

1. Check that `enabled = true` in config
2. Verify `bot_token` is correct
3. Ensure your `chat_id` is in `allowed_chat_ids`
4. Check the service logs for errors

### Messages Not Received

1. Start a conversation with the bot first (send `/start`)
2. Check that the event type is in `event_filter`
3. Verify rate limiting isn't blocking messages

### Agent Messages Not Sending

1. Ensure `agent_message` is in `event_filter`
2. Verify the agent has access to the `send_user_message` tool
3. Check firewall rules in `~/.dpc/privacy_rules.json`

### Voice Transcription Not Working

1. Check that `transcription_enabled = true` in config
2. Ensure a Whisper provider is configured in `~/.dpc/providers.json`
3. Verify the voice provider alias is set in providers config
4. Check service logs for transcription errors
5. Ensure the voice file was downloaded (check `~/.dpc/agent/voice/`)

## Security Notes

- The Telegram bot is **separate** from the main DPC Telegram integration
- Only chat IDs in `allowed_chat_ids` can interact with the bot
- Messages are sent over Telegram's encrypted connection
- The bot token should be kept secret

## Related Documentation

- [DPC Agent Guide](DPC_AGENT_GUIDE.md) - General agent usage
- [Configuration](CONFIGURATION.md) - Full configuration reference
- [CLAUDE.md](../CLAUDE.md) - Project development guide
