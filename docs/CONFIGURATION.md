# DPC-Client Configuration Guide

> **Version:** 0.28.0
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

Every section and key `_create_default_config` writes into a fresh `~/.dpc/config.ini`: **24 sections, 147 keys**. Generated from `settings.py` by `tools/config_reference.py` — edit the code, then re-run it; do not hand-edit between the markers.

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
| `collect_ai_models` | `false` | Collect locally available AI models (opt-in for compute-sharing) |

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
