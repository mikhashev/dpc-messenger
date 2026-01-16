# Telegram Bot Integration - Setup Guide

DPC Messenger v0.14.0+ includes a Telegram bot integration that enables:
- **Voice message transcription** using your local Whisper model
- **Two-way messaging bridge** between Telegram and DPC P2P chats
- **Private/whitelist-only access** control
- **Integrated UI** - Telegram messages appear within existing DPC conversations

---

## Features

### Voice Message Transcription
- Send voice messages from Telegram
- Automatic transcription using your local Whisper model
- Transcription sent back to Telegram chat
- Voice messages appear in DPC UI with transcriptions

### Two-Way Messaging
- Send messages from Telegram → appear in DPC UI
- Send messages from DPC → appear in Telegram
- Optional forwarding to P2P peers

### Privacy & Security
- **Whitelist mode**: Only authorized chat_ids can interact
- Voice files stored locally (same as P2P voice messages)
- Transcriptions follow same privacy model as DPC messages

---

## Setup Steps

### Step 1: Create Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send the `/newbot` command
3. Follow the prompts to name your bot (e.g., "My DPC Bot")
4. Copy the **bot token** (format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Step 2: Get Your Chat ID

1. Search for **@userinfobot** in Telegram
2. Send any message (e.g., `/start`)
3. Bot will reply with your **Chat ID** (e.g., `123456789`)

> **Note:** Your Chat ID is a numeric identifier. Keep it private - it's your access key.

### Step 3: Configure DPC Messenger

Edit `~/.dpc/config.ini`:

```ini
[telegram]
enabled = true
bot_token = 123456789:ABCdefGHIjklMNOpqrsTUVwxyz
allowed_chat_ids = ["123456789"]
transcription_enabled = true
bridge_to_p2p = false
```

#### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `enabled` | Master toggle for Telegram integration | `false` |
| `bot_token` | Bot token from @BotFather | (empty) |
| `allowed_chat_ids` | JSON array of whitelisted chat IDs | `[]` |
| `transcription_enabled` | Auto-transcribe voice messages | `true` |
| `bridge_to_p2p` | Forward messages to P2P peers | `false` |
| `use_webhook` | Use webhook mode (production) | `false` |
| `webhook_url` | Public URL for webhooks | (empty) |
| `webhook_port` | Local port for webhook server | `8443` |

### Step 4: Install Python Dependency

```bash
cd dpc-client/core
poetry install
```

This installs `python-telegram-bot` library (>=21.0,<22.0).

### Step 5: Restart DPC Messenger

```bash
# Stop existing instance (if running)
# Start new instance
cd dpc-client/core
poetry run python run_service.py
```

You should see logs indicating successful Telegram bot initialization:
```
[INFO] Telegram bot integration initialized (whitelist: 1 chat_ids)
[INFO] Starting Telegram bot integration...
[INFO] Telegram bot started (polling mode)
```

### Step 6: Link Conversation in UI

1. Open DPC Messenger UI
2. Select or create a conversation
3. Click **"Link Chat"** in the Telegram status panel
4. Enter your Chat ID (from Step 2)
5. Click **"Link Chat"**

---

## Usage

### Sending Messages from Telegram

1. Open your bot in Telegram
2. Send a text message
3. Message appears in DPC UI with **📱 Telegram** badge

### Sending Voice Messages

1. Record a voice message in Telegram
2. Send to your bot
3. Automatic transcription using local Whisper
4. Transcription sent back to Telegram
5. Voice appears in DPC UI with transcription

### Sending Messages from DPC

1. Select a Telegram-linked conversation
2. Type message and send
3. Message appears in Telegram with format:
   ```
   👤 Your Name (DPC):
   Your message here
   ```

---

## Troubleshooting

### Bot Not Responding

**Symptom:** Bot doesn't respond to messages in Telegram

**Solutions:**
1. Verify bot token is valid:
   ```bash
   curl https://api.telegram.org/bot<TOKEN>/getMe
   ```
2. Check `config.ini` has `enabled = true`
3. Check service logs for errors:
   ```bash
   tail -f ~/.dpc/logs/dpc-client.log
   ```

### "Chat not in whitelist" Error

**Symptom:** Error: "Chat ID 123456789 not in whitelist"

**Solution:**
1. Get your correct Chat ID from @userinfobot
2. Add to `allowed_chat_ids` in `config.ini`:
   ```ini
   allowed_chat_ids = ["123456789"]
   ```
3. Restart DPC Messenger

### Voice Transcription Fails

**Symptom:** Voice message received but no transcription

**Solutions:**
1. Check Whisper provider is configured in `~/.dpc/providers.json`
2. Verify `transcription_enabled = true` in `config.ini`
3. Check logs for Whisper errors
4. Ensure enough VRAM/RAM for Whisper model

### ImportError: telegram

**Symptom:** `Failed to import telegram library`

**Solution:**
```bash
cd dpc-client/core
poetry install
```

### Permission Denied (Webhook Mode)

**Symptom:** Webhook server fails to bind to port 8443

**Solutions:**
1. Use polling mode (default for development):
   ```ini
   use_webhook = false
   ```
2. Or run with sudo/admin privileges (not recommended)
3. Or use a different port:
   ```ini
   webhook_port = 8443
   ```

---

## Advanced Configuration

### Webhook Mode (Production)

For production deployment with public URL:

```ini
[telegram]
enabled = true
use_webhook = true
webhook_url = https://your-domain.com/telegram/webhook
webhook_port = 8443
```

**Requirements:**
- Public HTTPS URL with valid SSL certificate
- Open port 8443 in firewall
- Configure reverse proxy (nginx/Apache) to forward to localhost:8443

**Nginx Example:**
```nginx
location /telegram/webhook {
    proxy_pass http://localhost:8443;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Multiple Chat IDs

To allow multiple users:

```ini
allowed_chat_ids = ["123456789", "987654321", "555555555"]
```

### P2P Bridging

To forward Telegram messages to P2P peers:

```ini
bridge_to_p2p = true
```

**Warning:** This will share your Telegram messages with connected DPC peers.

### Disable Transcription

To disable automatic transcription:

```ini
transcription_enabled = false
```

---

## Security Considerations

### Whitelist Enforcement

- **Only whitelisted chat_ids** can interact with the bot
- Unauthorized chat attempts are logged and rejected
- Always verify Chat ID before adding to whitelist

### Bot Token Security

- **Never share your bot token** publicly
- Bot token grants full control over your bot
- Rotate token if compromised via @BotFather

### Webhook Security

If using webhook mode:
- Use HTTPS with valid SSL certificate
- Set webhook URL with random path
- Consider setting a secret token

### Privacy

- Voice files stored in `~/.dpc/conversations/` (same as P2P)
- Transcriptions follow same privacy model as DPC messages
- No data sent to Telegram servers beyond user messages

---

## API Reference

### WebSocket Commands

#### `send_to_telegram`

Send message from DPC to Telegram.

```typescript
import { sendToTelegram } from '$lib/coreService';

await sendToTelegram(conversationId, "Hello from DPC!");
```

#### `link_telegram_chat`

Link a DPC conversation to a Telegram chat.

```typescript
import { linkTelegramChat } from '$lib/coreService';

await linkTelegramChat(conversationId, "123456789");
```

#### `get_telegram_status`

Get Telegram bot status.

```typescript
import { getTelegramStatus } from '$lib/coreService';

const status = await getTelegramStatus();
// Returns: { enabled, connected, webhook_mode, whitelist_count, ... }
```

---

## File Locations

| File | Location |
|------|----------|
| Config | `~/.dpc/config.ini` |
| Logs | `~/.dpc/logs/dpc-client.log` |
| Voice files | `~/.dpc/conversations/telegram-{chat_id}/files/` |
| Manager | `dpc-client/core/dpc_client_core/managers/telegram_manager.py` |
| Bridge | `dpc-client/core/dpc_client_core/coordinators/telegram_coordinator.py` |
| UI Component | `dpc-client/ui/src/lib/components/TelegramStatus.svelte` |

---

## Future Enhancements

Potential features for future versions:

1. **Multi-chat support** - Link multiple Telegram chats to different DPC conversations
2. **Group chat bridging** - Forward Telegram group messages to DPC group chats
3. **File transfer** - Support documents, photos, videos (not just voice)
4. **Inline commands** - `/status`, `/transcribe`, `/link` from Telegram
5. **End-to-end encryption** - Encrypt messages before sending to Telegram

---

## Support

For issues or questions:
1. Check logs: `~/.dpc/logs/dpc-client.log`
2. Verify configuration in `~/.dpc/config.ini`
3. Check [GitHub Issues](https://github.com/your-repo/issues)
4. Review [CLAUDE.md](../CLAUDE.md) for architecture details
