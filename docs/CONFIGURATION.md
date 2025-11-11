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

## See Also

- [Quick Start Guide](./QUICK_START.md)
- [WebRTC Setup Guide](./WEBRTC_SETUP_GUIDE.md)
- [Firewall Configuration](../dpc-client/.dpc_access.example)
- [Fixes Summary](../FIXES_SUMMARY.md)

---

**Questions or issues?** [Open an issue](https://github.com/mikhashev/dpc-messenger/issues)
