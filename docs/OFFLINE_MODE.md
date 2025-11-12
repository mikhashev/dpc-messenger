# Offline Mode Implementation Guide

**Status:** ✅ Completed
**Version:** 0.6.0
**Last Updated:** 2025-11-12

## Overview

DPC-Client now supports offline mode, allowing the application to function gracefully when the Hub is unavailable. This guide explains the offline mode features and how to use them.

---

## Architecture

### Components

1. **TokenCache** - Securely stores authentication tokens
2. **PeerCache** - Stores known peer information
3. **ConnectionStatus** - Tracks connectivity state
4. **Graceful Degradation** - Adapts functionality based on availability

---

## Token Caching

### Features

- **Secure Storage:** Tokens encrypted using node private key
- **Automatic Loading:** Tokens loaded on startup
- **Expiration Handling:** Expired tokens automatically cleared
- **Persistence:** Survives app restarts

### Implementation

**File:** `dpc_client_core/token_cache.py`

```python
from dpc_client_core.token_cache import TokenCache

# Initialize (automatically done in CoreService)
token_cache = TokenCache(
    cache_dir=Path.home() / ".dpc",
    node_key_path=Path.home() / ".dpc" / "node.key"
)

# Tokens are automatically saved after OAuth login
# Tokens are automatically loaded on next startup
```

###Storage Location

- **File:** `~/.dpc/auth_cache.enc`
- **Format:** Encrypted binary (Fernet encryption)
- **Contents:** JWT token, refresh token, expiration time

---

## Peer Caching

### Features

- **Known Peers:** Stores metadata about previously connected peers
- **Direct Connection Info:** Saves last known IP for Direct TLS fallback
- **Recent Peers:** Query peers seen within specified timeframe
- **Cleanup:** Automatically remove old peer entries

### Implementation

**File:** `dpc_client_core/peer_cache.py`

```python
from dpc_client_core.peer_cache import PeerCache

# Initialize
peer_cache = PeerCache(Path.home() / ".dpc" / "known_peers.json")

# Add/update peer
peer_cache.add_or_update_peer(
    node_id="dpc-node-alice-123",
    display_name="Alice",
    direct_ip="192.168.1.100",
    supports_direct=True
)

# Get peer
peer = peer_cache.get_peer("dpc-node-alice-123")

# Get recent peers
recent = peer_cache.get_recent_peers(hours=24)

# Get peers with Direct TLS info
direct_peers = peer_cache.get_peers_with_direct_connection()
```

### Storage Location

- **File:** `~/.dpc/known_peers.json`
- **Format:** Plain JSON
- **Contents:** Peer metadata, connection info, last seen timestamps

---

## Connection Status

### Operation Modes

1. **Fully Online** - All features available
   - Hub connected ✅
   - WebRTC available ✅
   - Direct TLS available ✅

2. **Hub Offline** - Limited features
   - Hub connected ❌
   - WebRTC available ❌
   - Direct TLS available ✅

3. **Fully Offline** - Local only
   - Hub connected ❌
   - WebRTC available ❌
   - Direct TLS available ❌

### Implementation

**File:** `dpc_client_core/connection_status.py`

```python
from dpc_client_core.connection_status import ConnectionStatus

# Initialize
status = ConnectionStatus()

# Update status
status.update_hub_status(connected=True)
status.update_webrtc_status(available=True)
status.update_direct_tls_status(available=True)

# Get operation mode
mode = status.get_operation_mode()  # OperationMode enum

# Get status message
message = status.get_status_message()  # Human-readable

# Check connection capability
can_connect, method = status.can_connect_to_peer(
    peer_supports_webrtc=True,
    peer_on_lan=False
)
```

---

## Graceful Degradation

### Hub Unavailable Behavior

**Before (No Offline Mode):**
```
❌ App fails to start if Hub unreachable
❌ OAuth required every restart
❌ No peer information retained
```

**After (With Offline Mode):**
```
✅ App starts with cached credentials
✅ Silent re-authentication if tokens valid
✅ Direct TLS connections still work
⚠️  WebRTC unavailable (requires Hub)
✅ Cached peers accessible
```

### Automatic Fallback

The client automatically falls back through connection methods:

1. **Try WebRTC** (if Hub available and peer supports it)
2. **Try Direct TLS to cached IP** (if peer in cache)
3. **Try Direct TLS discovery** (if on same network)
4. **Fail gracefully** with clear error message

---

## Hub Reconnection

### Automatic Reconnection

When the Hub connection is lost, the client automatically attempts to reconnect with exponential backoff:

**Reconnection Strategy:**
- **Max Attempts:** 5 (configurable)
- **Backoff Delays:** 2s, 4s, 8s, 16s, 32s (exponential)
- **Auto-stop:** After max attempts, stays in offline mode
- **Manual Retry:** "Login to Hub" button resets counter

**Example Reconnection Flow:**
```
Hub connection lost, attempting to reconnect (attempt 1/5)...
   Waiting 2s before reconnection...
Hub reconnection failed: Server rejected WebSocket connection: HTTP 400

Hub connection lost, attempting to reconnect (attempt 2/5)...
   Waiting 4s before reconnection...
Hub reconnection failed: Server rejected WebSocket connection: HTTP 400

... (continues up to attempt 5) ...

[Hub Offline] Max reconnection attempts (5) reached
   Staying in offline mode. Use 'Login to Hub' button to reconnect manually.
```

**Implementation:**
```python
# In CoreService
self._hub_reconnect_attempts = 0
self._max_hub_reconnect_attempts = 5  # Configurable

# Exponential backoff calculation
backoff_delay = min(2 ** self._hub_reconnect_attempts, 32)
```

### Manual Reconnection

Users can manually reconnect to the Hub:

1. Click "Login to Hub" button in UI
2. Reconnection counter resets to 0
3. Hub monitor restarts with fresh attempts
4. OAuth flow initiates if needed

---

## UI Status Display

### Connection Mode Badge

The UI displays the current operation mode with colored indicators:

- 🟢 **Online** (Green) - All features available
  - Hub connected ✅
  - WebRTC available ✅
  - Direct TLS available ✅

- 🟡 **Hub Offline** (Yellow) - Limited features
  - Hub connected ❌
  - WebRTC unavailable ❌
  - Direct TLS available ✅

- 🔴 **Offline** (Red) - Local only
  - All connections unavailable ❌

### Features Indicator

Expandable "Available Features" section shows:
- ✓ hub discovery (available)
- ✓ webrtc connections (available)
- ✓ direct tls connections (available)
- ✓ peer discovery (available)
- ✗ authentication (unavailable - shown struck-through)

### Cached Peers Count

Shows number of cached peers for offline Direct TLS fallback:
```
💾 3 cached peer(s)
```

### Real-time Updates

Status updates automatically when:
- Hub connection changes
- Operation mode changes
- WebRTC availability changes
- Peer cache updates

**WebSocket Event:**
```typescript
{
  "event": "connection_status_changed",
  "payload": {
    "status": {
      "operation_mode": "hub_offline",
      "connection_status": "Hub Offline - Direct TLS connections only",
      "available_features": { ... },
      "cached_peers_count": 3
    }
  }
}
```

---

## User Experience

### First Run

```
1. User launches app
2. No cached tokens found
3. OAuth flow initiated
4. Tokens saved to cache
5. Peer connections established
6. Peer info saved to cache
```

### Subsequent Runs (Hub Online)

```
1. User launches app
2. Cached tokens loaded
3. Tokens validated with Hub
4. Silent re-authentication
5. WebRTC + Direct TLS available
```

### Subsequent Runs (Hub Offline)

```
1. User launches app
2. Cached tokens loaded
3. Hub connection attempt fails
4. App continues with Direct TLS only
5. Automatic reconnection attempts (max 5 with backoff)
6. After max attempts, stays in offline mode
7. Cached peers displayed
8. Direct connections to cached peers work
9. UI shows "🟡 Hub Offline" status with available features
10. User can manually retry via "Login to Hub" button
```

---

## Configuration

### Enable/Disable Features

**In `~/.dpc/config.ini`:**

```ini
[hub]
url = https://hub.example.com
auto_connect = true

[offline_mode]
enable_token_cache = true
enable_peer_cache = true
cache_expiry_days = 30
```

**Via Environment Variables:**

```bash
export DPC_OFFLINE_MODE_ENABLE_TOKEN_CACHE=true
export DPC_OFFLINE_MODE_ENABLE_PEER_CACHE=true
```

---

## Security

### Token Cache Security

- **Encryption:** Tokens encrypted with Fernet (symmetric encryption)
- **Key Derivation:** Encryption key derived from node private key
- **Access Control:** Cache file readable only by owner (chmod 600)
- **Expiration:** Tokens cleared when expired

### Peer Cache Security

- **Plain Storage:** Peer metadata stored in plain JSON
- **No Secrets:** Does not store sensitive information
- **Optional:** Can be disabled if privacy is concern

---

## API Reference

### Token Cache

```python
class TokenCache:
    def __init__(cache_dir: Path, node_key_path: Path)
    def save_tokens(jwt_token, refresh_token, expires_in)
    def load_tokens() -> Optional[Dict]
    def clear() -> bool
    def is_valid() -> bool
    def get_jwt_token() -> Optional[str]
    def get_refresh_token() -> Optional[str]
```

### Peer Cache

```python
class PeerCache:
    def __init__(cache_file: Path)
    def add_or_update_peer(node_id, display_name, ...)
    def get_peer(node_id) -> Optional[CachedPeer]
    def get_all_peers() -> List[CachedPeer]
    def get_recent_peers(hours) -> List[CachedPeer]
    def get_peers_with_direct_connection() -> List[CachedPeer]
    def remove_peer(node_id) -> bool
    def clear()
    def cleanup_old_peers(days)
```

### Connection Status

```python
class ConnectionStatus:
    hub_connected: bool
    webrtc_available: bool
    direct_tls_available: bool
    authenticated: bool

    def get_operation_mode() -> OperationMode
    def update_hub_status(connected, error)
    def update_webrtc_status(available)
    def update_direct_tls_status(available, port)
    def get_status_message() -> str
    def can_connect_to_peer(...) -> (bool, str)
```

---

## Testing

### Test Token Cache

```bash
cd dpc-client/core
poetry run python -m dpc_client_core.token_cache
```

**Expected Output:**
```
[PASS] Save tokens
[PASS] Load tokens
[PASS] Helper methods
[PASS] Clear cache
[PASS] Expired token handling
[PASS] All TokenCache tests passed!
```

### Test Peer Cache

```bash
poetry run python -m dpc_client_core.peer_cache
```

**Expected Output:**
```
[PASS] Add peer
[PASS] Get peer
[PASS] Update peer
[PASS] Get all peers
[PASS] Get recent peers
[PASS] Get peers with direct connection
[PASS] Remove peer
[PASS] Persistence
[PASS] Clear cache
[PASS] All PeerCache tests passed!
```

### Test Connection Status

```bash
poetry run python -m dpc_client_core.connection_status
```

**Expected Output:**
```
[PASS] Initial state
[PASS] Direct TLS mode
[PASS] Fully online mode
[PASS] Hub offline mode
[PASS] Status message
[PASS] Available features
[PASS] Connection capability
[PASS] Status change callback
[PASS] All ConnectionStatus tests passed!
```

---

## Troubleshooting

### Token Cache Not Working

**Issue:** Tokens not cached after login

**Solutions:**
1. Check that `~/.dpc/node.key` exists
2. Check file permissions on `~/.dpc/`
3. Look for encryption errors in logs

### Peer Cache Not Persisting

**Issue:** Peers disappear after restart

**Solutions:**
1. Check that `~/.dpc/known_peers.json` is writable
2. Look for JSON serialization errors in logs
3. Verify peer metadata is valid

### App Not Using Cached Tokens

**Issue:** OAuth required every restart

**Solutions:**
1. Check that TokenCache is initialized in CoreService
2. Verify tokens haven't expired
3. Check token cache file exists: `ls ~/.dpc/auth_cache.enc`

---

## Recently Implemented (v0.6.0)

- ✅ **Token Caching:** Secure encrypted storage with PBKDF2HMAC
- ✅ **Peer Caching:** Known peers with Direct TLS fallback info
- ✅ **Connection Status Tracking:** Three operation modes
- ✅ **Graceful Degradation:** Continues when Hub fails
- ✅ **Hub Reconnection:** Automatic retry with exponential backoff (max 5 attempts)
- ✅ **UI Status Display:** Real-time connection mode indicators
- ✅ **Manual Reconnection:** "Login to Hub" button resets retry counter

---

## Future Enhancements

### Planned for v0.7.0

- **Hub Discovery:** DNS-based hub discovery
- **Multi-Hub Support:** Connect to multiple hubs
- **Peer Reputation:** Track peer reliability
- **Connection History:** Track connection success/failure patterns

### Planned for v0.8.0

- **DHT Integration:** Fully decentralized peer discovery
- **Offline Messages:** Queue messages for delivery when peer comes online
- **Sync Protocol:** Synchronize cached data across devices
- **Smart Peer Selection:** Choose best connection method based on history

---

## See Also

- [Configuration Guide](./CONFIGURATION.md)
- [Quick Start](./QUICK_START.md)
- [WebRTC Setup](./WEBRTC_SETUP_GUIDE.md)

---

**Questions?** [Open an issue](https://github.com/mikhashev/dpc-messenger/issues)
