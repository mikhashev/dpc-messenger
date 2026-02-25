# D-PC Messenger v0.10.1 Release Notes

**Released:** December 9, 2025

---

## Overview

Version 0.10.1 is a **security and reliability update** that adds DTLS encryption to UDP Hole Punching, improves connection reliability for high-latency networks (mobile, CGNAT, satellite), and enhances UI visibility for connection strategies. This release makes the Priority 4 connection strategy (UDP Hole Punching) production-ready with end-to-end encryption.

**Key Achievement:** All 6 connection strategies now support end-to-end encryption, completing the secure fallback hierarchy introduced in v0.10.0.

---

## What's New

### 1. DTLS Encryption for UDP Hole Punching

**Status Change:** UDP Hole Punching (Priority 4) is now **production-ready** with DTLS 1.2 encryption.

**Security Improvements:**
- **DTLS 1.2 Handshake:** End-to-end encrypted UDP transport
- **Certificate-Based Authentication:** Uses existing node certificates (same as TLS)
- **Perfect Forward Secrecy:** Each session uses ephemeral keys
- **No Cleartext:** All UDP hole punch traffic is encrypted

**Implementation:**
- File: [connection_strategies/udp_hole_punch.py](../dpc-client/core/dpc_client_core/connection_strategies/udp_hole_punch.py)
- File: [managers/hole_punch_manager.py](../dpc-client/core/dpc_client_core/managers/hole_punch_manager.py)
- Comprehensive unit test suite: [tests/test_dtls.py](../dpc-client/core/tests/test_dtls.py)

**Configuration:**
```ini
[hole_punch]
enabled = true  # Now enabled by default (was disabled in v0.10.0)
timeout = 15    # Timeout in seconds
```

**Testing:**
- See [MANUAL_TESTING_GUIDE.md](MANUAL_TESTING_GUIDE.md) for DTLS verification with Wireshark

---

### 2. Connection Timeout Increases for High-Latency Networks

**Problem:** Default timeouts were too aggressive for mobile networks, CGNAT, and satellite connections.

**Solution:** Increased timeouts for improved reliability on slow/high-latency networks.

**Timeout Changes:**

| Timeout Type | v0.10.0 | v0.10.1 | Use Case |
|--------------|---------|---------|----------|
| **Pre-flight** | 5s | **30s** | Initial DHT/Hub queries |
| **IPv4/IPv6 Direct** | 10s | **60s** | Direct TLS connections |
| **WebRTC** | 30s | 30s | (unchanged) |
| **UDP Hole Punch** | 15s | 15s | (unchanged) |
| **Relay** | 20s | 20s | (unchanged) |
| **Gossip** | 5s | 5s | (unchanged) |

**Benefits:**
- **Mobile Networks:** More time for cellular NAT traversal
- **CGNAT:** Tolerates carrier-grade NAT complexities
- **Satellite:** Accommodates high-latency satellite connections
- **Rural Networks:** Better reliability on slow DSL/rural broadband

**Configuration:**
```ini
[connection]
ipv4_direct_timeout = 60  # Was 10 in v0.10.0
ipv6_direct_timeout = 60  # Was 10 in v0.10.0
preflight_timeout = 30    # Was 5 in v0.10.0
```

---

### 3. UI Connection Strategy Visibility

**New Feature:** Available Features menu now shows **peer counts per connection strategy**.

**UI Display:**
```
Available Features:
├── ipv4_direct (2 peers)
├── ipv6_direct (0 peers)
├── webrtc (2 peers)
├── udp_hole_punch (1 peer)
├── relay (2 peers)
└── gossip (3 peers)
```

**How It Works:**
- Each P2P connection now sets `strategy_used` metadata
- UI queries backend for peer counts per strategy
- Backend fix ensures all connection types set this field (commit 5a91719)

**Benefits:**
- **Network Debugging:** See which strategies are actually working
- **Performance Tuning:** Identify which peers use which strategies
- **User Awareness:** Understand connection diversity in your network

**Files Modified:**
- [Available Features UI](../dpc-client/ui/src/routes/+page.svelte) - UI display
- Backend coordinators - strategy_used metadata

---

### 4. Comprehensive Manual Testing Guide

**New Documentation:** [MANUAL_TESTING_GUIDE.md](MANUAL_TESTING_GUIDE.md)

**Contents:**
- **All 6 Connection Strategies:** Step-by-step testing procedures
- **Wireshark Verification:** Packet capture verification for each strategy
- **NAT Simulation:** How to test hole punching with simulated NAT
- **Relay Testing:** Volunteer relay mode testing
- **Gossip Testing:** Store-and-forward testing
- **DTLS Verification:** Verify encryption with Wireshark filters

**Use Cases:**
- Manual QA testing before releases
- User troubleshooting
- Contributor testing
- Network debugging

---

## Commits in v0.10.1

| Commit | Type | Description |
|--------|------|-------------|
| 5a91719 | fix(ui) | Set strategy_used for all P2P connection types |
| 4a19dc4 | feat(ui) | Show peer counts per connection strategy (v0.10.1) |
| 6ea4198 | fix(connection) | Increase timeouts for high-latency networks |
| 590ef65 | docs | Add comprehensive manual testing guide |
| 884adb1 | test | Add comprehensive unit tests for DTLS |
| 4a58c9b | feat(core) | Implement DTLS encryption for UDP hole punching |

---

## Upgrade Notes

### From v0.10.0 to v0.10.1

**Breaking Changes:**
- **None** - v0.10.1 is fully backward compatible

**Configuration Changes:**
- UDP Hole Punching now **enabled by default** (was disabled in v0.10.0)
- New default timeouts (automatic, no config changes required)
- Existing config files work without modification

**Automatic Upgrades:**
- DTLS automatically enabled for UDP Hole Punching
- Timeout increases apply automatically
- No user action required

**Optional Configuration:**
```ini
# If you want to revert to v0.10.0 timeouts (not recommended):
[connection]
ipv4_direct_timeout = 10
ipv6_direct_timeout = 10
preflight_timeout = 5

# If you want to disable UDP Hole Punching (not recommended):
[hole_punch]
enabled = false
```

---

## Testing v0.10.1

### Automated Testing

**Backend Tests:**
```bash
cd dpc-client/core
poetry run pytest tests/test_dtls.py  # DTLS unit tests
poetry run pytest -v                   # Full test suite
```

**Frontend Tests:**
```bash
cd dpc-client/ui
npm run check  # TypeScript checks
npm run build  # Build verification
```

### Manual Testing

**1. DTLS Encryption Verification:**
```bash
# Start Wireshark capture on UDP port 8888
# Connect to peer via UDP Hole Punching
# Verify encrypted DTLS handshake (not cleartext UDP)
```

**2. High-Latency Network Testing:**
```bash
# Simulate high latency with tc (Linux):
sudo tc qdisc add dev eth0 root netem delay 500ms
# Attempt connections - should succeed with new timeouts
```

**3. Connection Strategy Visibility:**
- Open Available Features menu
- Verify peer counts show per strategy
- Connect via different strategies and verify counts update

**Full Testing Guide:** See [MANUAL_TESTING_GUIDE.md](MANUAL_TESTING_GUIDE.md)

---

## Security Notes

### DTLS Implementation

**Encryption Details:**
- **Protocol:** DTLS 1.2 (UDP-based TLS)
- **Cipher Suites:** TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384 (preferred)
- **Certificate Validation:** Node certificates validated same as TLS
- **Perfect Forward Secrecy:** Yes (ECDHE key exchange)

**Security Posture:**
- **v0.10.0:** UDP Hole Punching was cleartext (disabled by default)
- **v0.10.1:** UDP Hole Punching now encrypted (enabled by default)

**Impact:**
- All 6 connection strategies now have end-to-end encryption
- No cleartext communication in any fallback scenario
- Zero-knowledge relays (Priority 5) remain zero-knowledge

---

## Configuration Reference

### New/Changed Defaults in v0.10.1

```ini
[connection]
# Increased timeouts for high-latency networks
ipv4_direct_timeout = 60  # Was 10 in v0.10.0
ipv6_direct_timeout = 60  # Was 10 in v0.10.0
preflight_timeout = 30    # Was 5 in v0.10.0

[hole_punch]
# UDP Hole Punching now enabled by default
enabled = true            # Was false in v0.10.0
timeout = 15              # (unchanged)
```

### No Changes Required

All other configuration sections unchanged from v0.10.0:
- `[dht]` - DHT configuration
- `[relay]` - Relay configuration
- `[gossip]` - Gossip configuration
- `[hub]` - Hub configuration
- `[p2p]` - P2P configuration

---

## Known Issues

**None reported** as of December 9, 2025.

If you encounter issues:
1. Check [MANUAL_TESTING_GUIDE.md](MANUAL_TESTING_GUIDE.md) for troubleshooting
2. Verify firewall allows UDP port 8888 (for hole punching)
3. Report issues at https://github.com/anthropics/dpc-messenger/issues

---

## What's Next

**v0.11.0 Roadmap:**
- Phase 7: Conversation history and context inclusion improvements
- Knowledge commit integrity verification
- Performance optimizations for large DHT networks
- Enhanced relay quality scoring

**Stay tuned!**

---

## Credits

**Contributors:**
- DTLS implementation and testing
- Timeout optimization for mobile networks
- UI improvements for connection visibility
- Comprehensive testing documentation

**Special Thanks:**
- Community testers for CGNAT/mobile network feedback
- Security reviewers for DTLS audit

---

## Resources

**Documentation:**
- [MANUAL_TESTING_GUIDE.md](MANUAL_TESTING_GUIDE.md) - Comprehensive testing guide
- [CHANGELOG.md](../CHANGELOG.md) - Full version history
- [CLAUDE.md](../CLAUDE.md) - Project architecture
- [CONFIGURATION.md](CONFIGURATION.md) - Complete configuration reference

**Previous Releases:**
- [v0.10.0 Release Notes](RELEASE_NOTES_V0_10_0.md) - Phase 6 fallback hierarchy
- [v0.9.5 Release Notes](RELEASE_NOTES_V0_9_5.md) - DHT peer discovery
- [v0.9.0 Release Notes](RELEASE_NOTES_V0_9_0.md) - Knowledge architecture

---

**Thank you for using D-PC Messenger!**
