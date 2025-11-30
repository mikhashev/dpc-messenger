# DPC-Client Configuration Guide

> **Version:** 0.5.0+
> **Last Updated:** 2025-11-11

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

**Migration:** The client automatically migrates old filenames to new ones on startup. Both formats work for backwards compatibility, but documentation uses current standard names.

**Example Migration:**
```bash
# Old (pre-v0.6) - still works
~/.dpc/.dpc_access.json  → migrated to → ~/.dpc/privacy_rules.json
~/.dpc/providers.toml    → migrated to → ~/.dpc/providers.json

# New (v0.6+) - recommended
~/.dpc/privacy_rules.json  # Use this
~/.dpc/providers.json      # Use this
```

---

## Default Configuration

On first run, DPC-Client creates a default configuration file:

```ini
# D-PC Client Configuration
# You can override these settings with environment variables:
# DPC_HUB_URL, DPC_OAUTH_CALLBACK_PORT, etc.

[hub]
url = http://localhost:8000
auto_connect = true

[oauth]
callback_port = 8080
callback_host = 127.0.0.1

[p2p]
listen_port = 8888
listen_host = 0.0.0.0

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
- **Default:** `true`
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
- **Default:** `0.0.0.0` (all interfaces)
- **Environment Variable:** `DPC_P2P_LISTEN_HOST`
- **Common Values:**
  - `0.0.0.0` - Listen on all interfaces (recommended)
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

### Migration Process

1. **Backup:** Old config saved to `config.ini.bak`
2. **Extraction:** Hub URL extracted from old format
3. **Recreation:** New config created with proper sections
4. **Preservation:** Original Hub URL preserved

---

## Troubleshooting

### Issue: "File contains no section headers"

**Cause:** Old configuration format
**Solution:** Automatic migration on next run. Manual fix:

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

## Reference: All Environment Variables

| Variable | Section | Key | Default | Description |
|----------|---------|-----|---------|-------------|
| `DPC_HUB_URL` | hub | url | `http://localhost:8000` | Federation Hub URL |
| `DPC_HUB_AUTO_CONNECT` | hub | auto_connect | `true` | Auto-connect to Hub |
| `DPC_OAUTH_CALLBACK_PORT` | oauth | callback_port | `8080` | OAuth callback port |
| `DPC_OAUTH_CALLBACK_HOST` | oauth | callback_host | `127.0.0.1` | OAuth callback host |
| `DPC_P2P_LISTEN_PORT` | p2p | listen_port | `8888` | P2P server port |
| `DPC_P2P_LISTEN_HOST` | p2p | listen_host | `0.0.0.0` | P2P server host |
| `DPC_API_PORT` | api | port | `9999` | Local API port |
| `DPC_API_HOST` | api | host | `127.0.0.1` | Local API host |

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

## See Also

- [Quick Start Guide](./QUICK_START.md)
- [WebRTC Setup Guide](./WEBRTC_SETUP_GUIDE.md)
- [GitHub OAuth Setup](./GITHUB_AUTH_SETUP.md)
- [Firewall Configuration](../dpc-client/privacy_rules.example.json)
- [Fixes Summary](../FIXES_SUMMARY.md)

---

**Questions or issues?** [Open an issue](https://github.com/mikhashev/dpc-messenger/issues)
