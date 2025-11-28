# Server Configuration Guide

## Overview

All STUN and TURN server addresses are configured in `~/.dpc/config.ini`. This allows users to customize servers based on regional availability and avoid blocked servers in different countries.

## Configuration Location

**File:** `~/.dpc/config.ini`

## Default Configuration

When you first run DPC Messenger, a default `config.ini` is created with these settings:

```ini
[webrtc]
# STUN servers for NAT traversal (discovering public IP)
stun_servers = stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302,stun:global.stun.twilio.com:3478,stun:stun.rtc.yandex.net:3478

[turn]
# TURN credentials (required for WebRTC through restrictive NATs/firewalls)
username =
credential =

# TURN server URLs (used only when username/credential are set)
servers = stun:stun.relay.metered.ca:80,turn:global.relay.metered.ca:80,turn:global.relay.metered.ca:80?transport=tcp,turn:global.relay.metered.ca:443,turns:global.relay.metered.ca:443?transport=tcp

# Fallback TURN servers (public, may be unreliable)
fallback_servers = turn:openrelay.metered.ca:80,turn:openrelay.metered.ca:443,turn:openrelay.metered.ca:443?transport=tcp
fallback_username = openrelayproject
fallback_credential = openrelayproject
```

## Customizing Servers

### Adding/Removing STUN Servers

Edit the `stun_servers` line to add or remove servers (comma-separated):

```ini
[webrtc]
stun_servers = stun:your-server.com:3478,stun:another-server.com:19302
```

**Popular STUN Servers:**
- `stun:stun.l.google.com:19302` (Google)
- `stun:stun1.l.google.com:19302` (Google backup)
- `stun:global.stun.twilio.com:3478` (Twilio)
- `stun:stun.rtc.yandex.net:3478` (Yandex)
- `stun:stun.stunprotocol.org:3478` (Open source)

### Configuring TURN Servers

**Option 1: Use Your Own TURN Server**

1. Sign up for a TURN service (e.g., [Metered.ca](https://metered.ca), [Twilio](https://www.twilio.com))
2. Add credentials to `config.ini`:

```ini
[turn]
username = your-username-here
credential = your-password-here
servers = turn:your-turn-server.com:3478,turns:your-turn-server.com:5349
```

**Option 2: Use Environment Variables**

```bash
export DPC_TURN_USERNAME="your-username"
export DPC_TURN_CREDENTIAL="your-password"
```

**Option 3: Use Fallback Public Servers**

Leave `username` and `credential` empty. The client will automatically use the configured fallback servers (less reliable).

### Regional Considerations

**If servers are blocked in your country:**

1. Find alternative STUN/TURN servers available in your region
2. Update `config.ini` with accessible servers
3. Remove blocked servers from the list

**Example for China:**
```ini
[webrtc]
stun_servers = stun:stun.qq.com:3478,stun:stun.miwifi.com:3478

[turn]
# Use Chinese TURN providers if available
servers = turn:your-chinese-turn-provider.com:3478
```

**Example for Russia:**
```ini
[webrtc]
stun_servers = stun:stun.rtc.yandex.net:3478,stun:stun.l.google.com:19302
```

## How It Works

1. **STUN Discovery**: On startup, DPC queries STUN servers to discover your external IP address
2. **WebRTC Connections**: When establishing WebRTC peer connections, STUN/TURN servers facilitate NAT traversal
3. **Fallback Logic**:
   - If TURN credentials provided → use configured TURN servers
   - If no credentials → use fallback public TURN servers (if configured)
   - Always use configured STUN servers

## Troubleshooting

**"No STUN servers configured" Warning:**
- Check that `[webrtc]` section exists in `~/.dpc/config.ini`
- Verify `stun_servers` line is not empty

**WebRTC Connections Fail:**
- Ensure at least one STUN server is accessible
- Configure TURN credentials for restrictive NAT/firewall environments
- Test server connectivity using `turn_connectivity_check.py`

**Slow External IP Discovery:**
- STUN servers may be slow or blocked
- Try alternative STUN servers
- Check firewall allows UDP on port 3478

## Testing Server Connectivity

Run the diagnostic tool:

```bash
cd dpc-client/core
poetry run python turn_connectivity_check.py
```

This will test connectivity to all configured servers.

## Environment Variable Overrides

You can override any setting with environment variables:

```bash
# Override STUN servers
export DPC_WEBRTC_STUN_SERVERS="stun:server1.com:3478,stun:server2.com:19302"

# Override TURN credentials
export DPC_TURN_USERNAME="myusername"
export DPC_TURN_CREDENTIAL="mypassword"

# Override TURN servers
export DPC_TURN_SERVERS="turn:myturn.com:3478"
```

Priority: **Environment Variables** > **config.ini** > **Built-in defaults (only when creating config.ini)**

## Security Notes

- TURN credentials are stored in plain text in `config.ini` (file permissions: user-only)
- Consider using environment variables for credentials instead
- Never commit `config.ini` with real credentials to version control
- Use secure TURN servers (TLS/DTLS) when transmitting sensitive data

## No Hardcoded Servers in Production Code

All server addresses are loaded from `config.ini` or environment variables. There are **no hardcoded servers** in the production codebase, ensuring maximum flexibility for international users.
