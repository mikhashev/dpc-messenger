# D-PC Messenger v0.9.0 Release Notes

Released: December 2, 2025

## Overview

Version 0.9.0 focuses on **connection reliability and diagnostics**, ensuring D-PC works seamlessly even when starting before network connectivity is established. This release also completes the logging migration to Python's standard library for better debugging and troubleshooting.

## Key Features

### 1. Network-Resilient STUN Discovery
- **Problem:** Service failed to discover external IP when started before WiFi connected
- **Solution:** Internet connectivity checks, exponential backoff retry, periodic re-discovery
- **Impact:** WebRTC connections now work reliably regardless of startup timing
- **Details:**
  - Checks connectivity before STUN attempts (TCP test to 8.8.8.8, 1.1.1.1, OpenDNS)
  - Retry logic with exponential backoff (immediate, +5s, +15s)
  - Periodic re-discovery every 5 minutes to detect network changes
  - Filters out unusable IPv4 link-local addresses (169.254.0.0/16)

### 2. Enhanced Connection Diagnostics
- **Actionable error messages** for WebRTC failures (STUN failed, TURN not configured, etc.)
- **Pre-flight port checks** for Direct TLS connections (detects blocked ports early)
- **ICE candidate analysis** (host/srflx/relay counts logged for troubleshooting)
- **Examples:**
  - "STUN failed - No server reflexive candidates found. Check internet connectivity."
  - "TURN not configured - Required for symmetric NAT traversal"
  - "Port 8888 not accessible - Check firewall and port forwarding"

### 3. Windows Dual-Stack IPv6 Support
- **Fixed:** Windows only listened on IPv6, rejected IPv4 connections
- **Now:** Separate IPv4 and IPv6 listeners on Windows, dual-stack on Linux/macOS
- **Impact:** Cross-platform Direct TLS connections work without configuration

### 4. Complete Logging Migration
- **All print statements** converted to Python standard library logging
- **Version information** added to logs ("D-PC Messenger v0.9.0 initializing...")
- **Centralized version management** via VERSION file (single source of truth)
- **Benefits:**
  - Better debugging (know which version produced logs)
  - Consistent logging across all components
  - Easy version updates (edit VERSION file only)

### 5. Documentation & Specifications
- **DPTP v1 Specification** (`specs/dptp_v1.md`) - Formal protocol documentation
- **Protocol Library Documentation** (`dpc-protocol/README.md`) - Complete API reference

## Upgrade Instructions

**No breaking changes** - v0.9.0 is fully backward compatible.

### Optional: Configure STUN/TURN Servers

Add to `~/.dpc/config.ini`:
```ini
[webrtc]
stun_servers = stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302

[turn]
servers = turn:your-turn-server.com:3478?transport=udp
turn_username = your_username
turn_password = your_password
```

### Optional: IPv6 Configuration

Default is dual-stack. To customize:
```ini
[p2p]
listen_host = dual      # Default: both IPv4 and IPv6
# listen_host = 0.0.0.0  # IPv4 only
# listen_host = ::        # IPv6 only
```

### Optional: Enable Debug Logging

To troubleshoot connection issues:
```ini
[logging]
level = DEBUG
module_levels = dpc_client_core.stun_discovery:DEBUG,dpc_client_core.webrtc_peer:DEBUG
```

Check logs:
```bash
tail -f ~/.dpc/logs/dpc-client.log
```

## Known Issues

- **External IP P2P connections** marked as experimental (tested locally, not across internet)
  - Recommendation: Use WebRTC for NAT traversal scenarios
  - Direct TLS over external IP requires port forwarding (default: 8888)

## Testing Checklist

- [x] Backend tests: 58 passed, 2 skipped
- [x] Frontend type check: 0 errors
- [x] Logging migration: 100% complete
- [ ] Manual: STUN discovery with delayed network connectivity
- [ ] Manual: Windows dual-stack connections
- [ ] Manual: IPv6 connections with bracket notation (`dpc://[::1]:8888`)
- [ ] Manual: External IP display in UI

## Changelog Highlights

For complete details, see [CHANGELOG.md](../CHANGELOG.md#090---2025-12-02)

**Added:**
- Network-resilient STUN discovery
- Enhanced connection diagnostics
- Windows dual-stack binding fix
- DPTP v1 protocol specification
- Protocol library documentation

**Changed:**
- Logging migration complete (all print statements → logger)
- Centralized version management (VERSION file)

**Fixed:**
- STUN DNS failures on startup (service starting before network ready)
- Windows rejecting IPv4 connections (dual-stack binding)
- Cryptic connection errors (added pre-flight diagnostics)
- Knowledge extraction bugs (hardcoded conversation_id, user identity)

## Contributors

- Core development: Mike (legoogmiha@gmail.com)
- Testing: Community contributors

## Next Steps: Phase 2 (Q1 2026)

v0.9.0 completes Phase 1 (Federated MVP). Next up:

**Pre-Phase 2 Refactoring (estimated 7-10 days):**
- Message handler registry (pluggable command handlers)
- Service coordinator extraction (split 3,600-line monolith)
- Enhanced test coverage (30% → 60%+)

**Phase 2.1 - Team Collaboration & Decentralized Infrastructure:**
- Group chats and team management
- DHT-based peer discovery (eliminates Hub dependency)
- Pluggable transport framework
- Compute marketplace features

See [ROADMAP.md](../ROADMAP.md) for details.

## Support

- **Documentation:** [docs/](../docs/)
- **Troubleshooting:** [docs/CONNECTION_TROUBLESHOOTING.md](../docs/CONNECTION_TROUBLESHOOTING.md) (coming soon)
- **Configuration:** [docs/CONFIGURATION.md](../docs/CONFIGURATION.md)
- **Quick Start:** [docs/QUICK_START.md](../docs/QUICK_START.md)
- **Issues:** [GitHub Issues](https://github.com/anthropics/claude-code/issues)

## Version Information

**Client:** D-PC Messenger v0.9.0
**Hub:** D-PC Hub v0.9.0
**Protocol:** DPTP v1.0

To verify your version:
```bash
# Client logs
tail -f ~/.dpc/logs/dpc-client.log | grep "initializing"

# Expected output:
# D-PC Messenger v0.9.0 initializing...
```
