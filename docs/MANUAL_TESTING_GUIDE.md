# Manual Testing Guide - All 6 Connection Strategies

**Version:** v0.10.1
**Status:** DTLS Implementation Testing
**Purpose:** Comprehensive manual testing of all connection strategies with real network scenarios

---

## Overview

D-PC Messenger uses a 6-tier connection fallback hierarchy. Manual testing requires **real network scenarios** with peers on different networks to properly test NAT traversal, encryption, and fallback logic.

**The 6 Connection Strategies:**
1. **IPv6 Direct** (Priority 1) - Global IPv6, no NAT
2. **IPv4 Direct** (Priority 2) - Local network or port forwarding
3. **Hub WebRTC** (Priority 3) - STUN/TURN via Hub server
4. **UDP Hole Punch + DTLS** (Priority 4) - DHT-coordinated NAT traversal ← **NEW in v0.10.1**
5. **Volunteer Relay** (Priority 5) - Privacy-preserving relay nodes
6. **Gossip Store-and-Forward** (Priority 6) - Disaster fallback, multi-hop

---

## Prerequisites

### Required Equipment

**Minimum Setup (2 devices):**
- **Device A:** Desktop/laptop (your main dev machine)
- **Device B:** Laptop/phone/VPS (different network)

**Ideal Setup (3+ devices):**
- **Device A:** Desktop (behind home NAT)
- **Device B:** Laptop (coffee shop WiFi / mobile hotspot)
- **Device C:** VPS/Cloud server (public IP for relay testing)

**Optional:**
- **Wireshark** - For packet capture and encryption verification
- **tcpdump** - For lightweight packet analysis
- **VPN** - For simulating different network conditions

### Network Requirements

**For each strategy:**
| Strategy | Network Requirement | Device Count |
|----------|---------------------|--------------|
| IPv6 Direct | Both have global IPv6 | 2 |
| IPv4 Direct | Same local network OR port forwarding | 2 |
| Hub WebRTC | Internet access + Hub running | 2 |
| UDP Hole Punch | Different networks, Cone NAT | 2 |
| Volunteer Relay | One device volunteers as relay | 3 (2 clients + 1 relay) |
| Gossip | Completely offline/isolated | 3+ (multi-hop) |

---

## Setup Instructions

### 1. Build and Deploy

**On each device:**

```bash
# Clone repository
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger

# Checkout dev branch (with DTLS)
git checkout dev
git pull origin dev

# Install dependencies and build
cd dpc-client/core
poetry install
cd ../ui
npm install
npm run build

# Verify DTLS imports
cd ../core
poetry run python -c "from dpc_client_core.transports import DTLSPeerConnection; print('DTLS available')"
```

### 2. Configure Each Device

**Device A (Home network, behind NAT):**
```bash
# ~/.dpc/config.ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = true
enable_hole_punching = true   # DTLS enabled
enable_relays = true
enable_gossip = true

[hole_punch]
enable_dtls = true
dtls_handshake_timeout = 3
```

**Device B (Coffee shop WiFi / Mobile hotspot):**
```bash
# Same config as Device A
# Different network ensures hole punching/relay testing
```

**Device C (VPS - Public IP for relay):**
```bash
# ~/.dpc/config.ini
[relay]
volunteer = true   # Act as relay server
max_peers = 10
region = global
```

### 3. Start Services

**On each device:**

```bash
# Terminal 1: Backend
cd dpc-client/core
poetry run python run_service.py

# Terminal 2: Frontend (if testing UI)
cd dpc-client/ui
npm run tauri dev

# Check logs
tail -f ~/.dpc/logs/dpc-client.log
```

---

## Test Strategy 1: IPv6 Direct (Priority 1)

**Goal:** Test direct IPv6 connection without NAT

**Requirements:**
- Both devices have global IPv6 addresses
- No NAT66 (IPv6 NAT)

**Setup:**

1. **Verify IPv6 availability:**
   ```bash
   # Check if you have global IPv6
   ip -6 addr show scope global  # Linux
   ifconfig | grep inet6         # macOS
   ipconfig /all                 # Windows

   # Test IPv6 connectivity
   ping6 google.com
   ```

2. **Check DHT IPv6 announcement:**
   ```bash
   # Check logs for IPv6 announcement
   grep "IPv6" ~/.dpc/logs/dpc-client.log
   grep "dht.*announce" ~/.dpc/logs/dpc-client.log
   ```

**Test Steps:**

1. Start both clients
2. Device A connects to Device B
3. Check which strategy was used:
   ```bash
   # In UI: Look for connection method display
   # In logs:
   grep "connection.*successful" ~/.dpc/logs/dpc-client.log
   grep "IPv6DirectStrategy" ~/.dpc/logs/dpc-client.log
   ```

**Expected Result:**
- Connection established via IPv6 Direct
- Logs show: "Connected to [node_id] via ipv6_direct"
- UI shows: "Connected (IPv6 Direct)"

**Verification:**
```bash
# Capture packets (on Device A)
sudo tcpdump -i any -n 'ip6 and port 8888' -w ipv6_direct.pcap

# Check for TLS handshake in Wireshark
# Filter: ipv6.addr == [peer_ipv6] && tcp.port == 8888
# Should see: TLS handshake, encrypted application data
```

---

## Test Strategy 2: IPv4 Direct (Priority 2)

**Goal:** Test direct IPv4 TLS connection (local network or port forwarding)

**Requirements:**
- Same local network OR port forwarding configured
- No IPv6 (disable to test fallback)

**Setup:**

1. **Disable IPv6 to force IPv4:**
   ```ini
   # ~/.dpc/config.ini
   [connection]
   enable_ipv6 = false
   enable_ipv4 = true
   ```

2. **Option A - Local Network:**
   - Both devices on same WiFi/LAN
   - Can reach each other's local IPs

3. **Option B - Port Forwarding:**
   - Device A: Forward port 8888 on router to Device A's local IP
   - Device B: Connect to Device A's public IP

**Test Steps:**

1. Start both clients
2. Device B connects to Device A
3. Check logs:
   ```bash
   grep "IPv4DirectStrategy" ~/.dpc/logs/dpc-client.log
   ```

**Expected Result:**
- Connection via IPv4 Direct
- Logs show: "Connected to [node_id] via ipv4_direct"
- UI shows: "Connected (IPv4 Direct)"

**Verification:**
```bash
# Capture packets
sudo tcpdump -i any -n 'host [peer_ip] and port 8888' -w ipv4_direct.pcap

# Check for TLS handshake
# Filter: ip.addr == [peer_ip] && tcp.port == 8888
```

---

## Test Strategy 3: Hub WebRTC (Priority 3)

**Goal:** Test WebRTC with STUN/TURN via Hub server

**Requirements:**
- Hub server running and accessible
- Both devices connected to Hub
- Different networks (to test STUN/TURN)

**Setup:**

1. **Start Hub server (Device C or cloud):**
   ```bash
   cd dpc-hub
   docker-compose up -d  # PostgreSQL
   poetry run alembic upgrade head
   poetry run uvicorn dpc_hub.main:app --host 0.0.0.0 --port 8000
   ```

2. **Configure clients to use Hub:**
   ```ini
   # ~/.dpc/config.ini
   [hub]
   url = https://your-hub-url:8000
   auto_connect = true

   [oauth]
   default_provider = google  # or github
   ```

3. **Disable direct strategies to force WebRTC:**
   ```ini
   [connection]
   enable_ipv6 = false
   enable_ipv4 = false
   enable_hub_webrtc = true
   enable_hole_punching = false  # Test WebRTC first
   ```

**Test Steps:**

1. Start Hub server
2. Start Device A → logs in to Hub (OAuth)
3. Start Device B → logs in to Hub (OAuth)
4. Device A connects to Device B
5. Check logs:
   ```bash
   grep "HubWebRTCStrategy" ~/.dpc/logs/dpc-client.log
   grep "WebRTC.*connected" ~/.dpc/logs/dpc-client.log
   grep "ICE.*candidate" ~/.dpc/logs/dpc-client.log
   ```

**Expected Result:**
- Connection via Hub WebRTC
- Logs show: "Connected to [node_id] via hub_webrtc"
- Logs show: ICE candidates, STUN binding, DTLS handshake
- UI shows: "Connected (WebRTC)"

**Verification:**
```bash
# Capture UDP packets (WebRTC uses UDP)
sudo tcpdump -i any -n 'udp' -w hub_webrtc.pcap

# In Wireshark:
# Filter: stun || dtls
# Should see: STUN Binding Requests, DTLS handshake, SRTP packets
```

---

## Test Strategy 4: UDP Hole Punch + DTLS (Priority 4) ← NEW!

**Goal:** Test DHT-coordinated UDP hole punching with DTLS encryption

**Requirements:**
- **CRITICAL:** Both devices on **different networks** (different public IPs)
- Both devices behind **Cone NAT** (most consumer routers)
- DHT active with at least 3 peers for endpoint discovery

**Setup:**

1. **Network Configuration:**
   - Device A: Home network (behind Cone NAT)
   - Device B: Coffee shop WiFi / Mobile hotspot (behind Cone NAT)
   - **DO NOT USE:** Same local network (hole punching won't be tested)

2. **Enable hole punching:**
   ```ini
   # ~/.dpc/config.ini
   [connection]
   enable_ipv6 = false
   enable_ipv4 = false
   enable_hub_webrtc = false
   enable_hole_punching = true  # Force hole punching
   enable_relays = false

   [hole_punch]
   enable_dtls = true
   dtls_handshake_timeout = 3
   dtls_version = 1.2
   ```

3. **Verify NAT type:**
   ```bash
   # Check logs after startup
   grep "NAT type" ~/.dpc/logs/dpc-client.log
   # Should show: "NAT type: cone" (good for hole punching)
   # If shows: "NAT type: symmetric" → hole punching will fail, relay used
   ```

**Test Steps:**

1. Start Device A (wait for DHT to stabilize, ~30 seconds)
2. Start Device B (wait for DHT to stabilize, ~30 seconds)
3. Device A connects to Device B
4. Watch logs in real-time:
   ```bash
   tail -f ~/.dpc/logs/dpc-client.log | grep -E "hole.*punch|DTLS|UDP"
   ```

**Expected Log Sequence:**

```
[INFO] Attempting UDP hole punch to dpc-node-abc123...
[INFO] Local endpoint discovered: 203.0.113.50:8890 (NAT type: cone, confidence: 100%)
[INFO] Starting hole punch: local=203.0.113.50:8890, peer=198.51.100.75:8890
[INFO] UDP hole punch successful to dpc-node-abc123, upgrading to DTLS...
[INFO] DTLS handshake successful
[INFO] UDP hole punch + DTLS successful to dpc-node-abc123 (encrypted)
[INFO] Connected to dpc-node-abc123 via udp_hole_punch
```

**Expected Result:**
- Connection via UDP Hole Punch + DTLS
- Logs show: "UDP hole punch successful", "DTLS handshake successful"
- UI shows: "Connected (UDP Hole Punch - DTLS)"

**Verification - Packet Capture:**

```bash
# On Device A
sudo tcpdump -i any -n 'udp port 8890' -w hole_punch_dtls.pcap

# On Device B (simultaneously)
sudo tcpdump -i any -n 'udp port 8890' -w hole_punch_dtls_peer.pcap
```

**Wireshark Analysis:**

1. Open `hole_punch_dtls.pcap` in Wireshark
2. Filter: `udp.port == 8890`
3. **Look for:**
   - **Punch messages:** Initial UDP packets (small, unencrypted coordination)
   - **DTLS handshake:** After punch success
     - Client Hello (UDP)
     - Server Hello (UDP)
     - Certificate exchange
     - Handshake complete
   - **Encrypted data:** All subsequent packets encrypted (DTLS application data)

4. **Verify encryption:**
   - Right-click packet → "Protocol Preferences" → "DTLS"
   - Try to decode without keys → should fail (encrypted)
   - Check packet bytes → should be gibberish after handshake

**Troubleshooting:**

| Issue | Cause | Solution |
|-------|-------|----------|
| "Symmetric NAT detected" | ISP uses symmetric NAT | Use mobile hotspot (usually Cone NAT) |
| "Hole punch timeout" | Firewall blocks UDP | Check router firewall settings |
| "DTLS handshake failed" | Certificate mismatch | Verify node IDs match |
| Falls back to relay | Hole punch works but DTLS fails | Check DTLS logs for errors |

---

## Test Strategy 5: Volunteer Relay (Priority 5)

**Goal:** Test privacy-preserving relay nodes

**Requirements:**
- 3 devices: Device A (client), Device B (client), Device C (relay)
- Device C has public IP or accessible via port forwarding

**Setup:**

1. **Device C - Configure as relay:**
   ```ini
   # ~/.dpc/config.ini
   [relay]
   volunteer = true
   max_peers = 10
   region = global
   bandwidth_limit_mbps = 10
   ```

2. **Devices A & B - Disable other strategies:**
   ```ini
   [connection]
   enable_ipv6 = false
   enable_ipv4 = false
   enable_hub_webrtc = false
   enable_hole_punching = false
   enable_relays = true
   enable_gossip = false
   ```

**Test Steps:**

1. Start Device C (relay) - wait for DHT announcement
2. Start Device A
3. Start Device B
4. Device A connects to Device B
5. Check logs:
   ```bash
   # On Device C (relay)
   grep "relay.*register" ~/.dpc/logs/dpc-client.log
   grep "relay.*forward" ~/.dpc/logs/dpc-client.log

   # On Device A/B (clients)
   grep "VolunteerRelayStrategy" ~/.dpc/logs/dpc-client.log
   grep "relay.*via" ~/.dpc/logs/dpc-client.log
   ```

**Expected Result:**
- Device C logs: "Relay session established for [A] <-> [B]"
- Devices A/B logs: "Connected to [node_id] via volunteer_relay"
- UI shows: "Connected (Relay)"

**Privacy Verification:**
```bash
# On Device C (relay) - should NOT see message content
grep "SEND_TEXT" ~/.dpc/logs/dpc-client.log
# Should only see: "RELAY_MESSAGE" (encrypted payload)

# Verify end-to-end encryption maintained
sudo tcpdump -i any -n 'port 8888' -w relay_traffic.pcap
# Relay should see encrypted payloads only
```

---

## Test Strategy 6: Gossip Store-and-Forward (Priority 6)

**Goal:** Test multi-hop epidemic routing for disaster scenarios

**Requirements:**
- 3+ devices (more = better multi-hop testing)
- Simulated network isolation (disable all other strategies)

**Setup:**

1. **All devices - Gossip only:**
   ```ini
   [connection]
   enable_ipv6 = false
   enable_ipv4 = false
   enable_hub_webrtc = false
   enable_hole_punching = false
   enable_relays = false
   enable_gossip = true

   [gossip]
   enabled = true
   max_hops = 5
   fanout = 3
   ttl_seconds = 86400  # 24 hours
   sync_interval = 300  # 5 minutes
   ```

**Test Steps:**

1. Start all devices
2. Device A sends message to Device C (not directly connected)
3. Message routes: A → B → C (multi-hop)
4. Check logs:
   ```bash
   grep "gossip" ~/.dpc/logs/dpc-client.log
   grep "vector.*clock" ~/.dpc/logs/dpc-client.log
   grep "forward.*message" ~/.dpc/logs/dpc-client.log
   ```

**Expected Result:**
- Message delivered via gossip (eventual delivery)
- Logs show: "Forwarding gossip message", "Message hop count: 2"
- UI shows: "Connected (Gossip - Multi-hop)"

---

## End-to-End Test Sequence

**Goal:** Test ALL 6 strategies in priority order (automatic fallback)

**Setup:**

```ini
# ~/.dpc/config.ini (both devices)
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = true
enable_hole_punching = true
enable_relays = true
enable_gossip = true
```

**Test Scenarios:**

### Scenario 1: Happy Path (IPv6)
- **Setup:** Both have IPv6
- **Expected:** IPv6 Direct (Priority 1)
- **Fallback chain:** None needed

### Scenario 2: IPv4 Fallback
- **Setup:** Disable IPv6, same local network
- **Expected:** IPv4 Direct (Priority 2)
- **Fallback chain:** IPv6 (failed) → IPv4 (success)

### Scenario 3: WebRTC Fallback
- **Setup:** Different networks, Hub running, hole punching disabled
- **Expected:** Hub WebRTC (Priority 3)
- **Fallback chain:** IPv6 (failed) → IPv4 (failed) → WebRTC (success)

### Scenario 4: Hole Punch Fallback
- **Setup:** Different networks, Hub offline, Cone NAT
- **Expected:** UDP Hole Punch + DTLS (Priority 4)
- **Fallback chain:** IPv6 → IPv4 → WebRTC → Hole Punch (success)

### Scenario 5: Relay Fallback
- **Setup:** Different networks, Hub offline, Symmetric NAT
- **Expected:** Volunteer Relay (Priority 5)
- **Fallback chain:** IPv6 → IPv4 → WebRTC → Hole Punch → Relay (success)

### Scenario 6: Gossip Fallback (Disaster)
- **Setup:** All strategies disabled except gossip
- **Expected:** Gossip Store-and-Forward (Priority 6)
- **Fallback chain:** All strategies failed → Gossip (eventual delivery)

---

## Verification Checklist

### For EACH Strategy Test:

- [ ] Connection established successfully
- [ ] Correct strategy used (check logs)
- [ ] UI shows correct connection method
- [ ] Can send text messages (encrypted)
- [ ] Can send context (encrypted)
- [ ] Can perform remote inference (encrypted)
- [ ] Graceful disconnect works
- [ ] Reconnect works

### Encryption Verification (ALL Strategies):

- [ ] Wireshark shows encrypted packets (TLS/DTLS)
- [ ] No plaintext message content visible
- [ ] Certificate validation working (invalid cert rejected)
- [ ] Node ID matches certificate CN

### DTLS-Specific Checks (Strategy 4):

- [ ] UDP hole punch succeeds (check logs)
- [ ] DTLS handshake succeeds (check logs)
- [ ] Handshake completes within 3 seconds
- [ ] Wireshark shows DTLS protocol (not plain UDP)
- [ ] Falls back to relay on handshake failure

---

## Common Issues and Solutions

### Issue: "All strategies exhausted"
**Cause:** No connection strategy succeeded
**Solution:**
1. Check DHT has peers: `grep "DHT.*peers" ~/.dpc/logs/dpc-client.log`
2. Verify internet connectivity
3. Check firewall rules (allow UDP 8888, 8890)
4. Try enabling each strategy one-by-one

### Issue: "DTLS handshake timeout"
**Cause:** Firewall blocking UDP, or peer certificate issue
**Solution:**
1. Check UDP port 8890 is not blocked
2. Verify both peers have valid certificates
3. Check logs for certificate validation errors
4. Try increasing `dtls_handshake_timeout = 5`

### Issue: "Symmetric NAT detected"
**Cause:** ISP/router uses symmetric NAT (hole punching won't work)
**Solution:**
1. **Expected behavior** - system falls back to relay (Priority 5)
2. Verify relay fallback working: `grep "VolunteerRelayStrategy" ~/.dpc/logs/dpc-client.log`
3. For testing, use mobile hotspot (usually Cone NAT)

### Issue: "Connection uses wrong strategy"
**Cause:** Higher priority strategy succeeding when testing lower priority
**Solution:**
1. Disable higher priority strategies in config
2. Restart both clients
3. Check strategy order in logs

---

## Logging and Debugging

### Enable Debug Logging

```ini
# ~/.dpc/config.ini
[logging]
level = DEBUG
module_levels = dpc_client_core.connection_strategies:DEBUG,dpc_client_core.transports:DEBUG
```

### Useful Log Filters

```bash
# Show connection strategy attempts
grep "ConnectionOrchestrator" ~/.dpc/logs/dpc-client.log

# Show DTLS activity
grep -E "DTLS|dtls" ~/.dpc/logs/dpc-client.log

# Show hole punching
grep -E "hole.*punch|UDP.*punch" ~/.dpc/logs/dpc-client.log

# Show all connection attempts
grep -E "Attempting|successful|failed" ~/.dpc/logs/dpc-client.log

# Show encryption activity
grep -E "TLS|DTLS|encrypted" ~/.dpc/logs/dpc-client.log
```

---

## Success Criteria (v0.10.1 Release)

### Must Pass:

- [ ] All 6 strategies tested end-to-end
- [ ] IPv6/IPv4 Direct work on local network
- [ ] Hub WebRTC works across internet
- [ ] **UDP Hole Punch + DTLS works with Cone NAT**
- [ ] Relay fallback works with Symmetric NAT
- [ ] Gossip works in disaster scenario
- [ ] **All strategies use encryption (TLS/DTLS)**
- [ ] Fallback order is correct (1→2→3→4→5→6)
- [ ] Wireshark confirms encryption on all strategies
- [ ] No plaintext message content visible

### Performance:

- [ ] IPv6 Direct: < 1 second connection time
- [ ] IPv4 Direct: < 1 second connection time
- [ ] Hub WebRTC: < 10 seconds connection time
- [ ] UDP Hole Punch + DTLS: < 5 seconds (punch + handshake)
- [ ] Volunteer Relay: < 8 seconds connection time
- [ ] Gossip: Eventual delivery (not real-time)

---

## Reporting Results

After testing, create a summary report:

```markdown
## DTLS Manual Testing Results

**Date:** YYYY-MM-DD
**Tester:** Your Name
**Build:** dev branch, commit [hash]

### Test Environment:
- Device A: [OS, network type, NAT type]
- Device B: [OS, network type, NAT type]
- Device C: [OS, network type] (if used)

### Results:

| Strategy | Status | Notes |
|----------|--------|-------|
| IPv6 Direct | ✅ PASS | Connection time: 0.5s |
| IPv4 Direct | ✅ PASS | Connection time: 0.3s |
| Hub WebRTC | ✅ PASS | Connection time: 8.2s |
| UDP Hole Punch + DTLS | ✅ PASS | Punch: 2.1s, DTLS: 0.8s |
| Volunteer Relay | ✅ PASS | Connection time: 6.5s |
| Gossip | ✅ PASS | Delivered in 15s (multi-hop) |

### Encryption Verification:
- [✅] Wireshark confirms TLS/DTLS encryption
- [✅] No plaintext visible
- [✅] Certificate validation working

### Issues Found:
- None

### Screenshots:
- [Attach Wireshark captures]
- [Attach UI screenshots showing connection methods]
```

---

## Next Steps

After successful manual testing:

1. **Document any issues found** (create GitHub issues)
2. **Update this guide** with lessons learned
3. **Write automated integration tests** (based on manual test scenarios)
4. **Update user documentation** (CONFIGURATION.md, FALLBACK_LOGIC.md)
5. **Prepare for v0.10.1 release**

---

**Last Updated:** 2025-12-09
**Version:** v0.10.1 (DTLS implementation testing)
