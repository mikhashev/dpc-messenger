# Connection Fallback Logic - 6-Tier Hierarchy

> **Version:** 0.10.0+
> **Last Updated:** 2025-12-07

## Overview

D-PC Messenger implements an intelligent 6-tier connection fallback hierarchy for near-universal P2P connectivity. When you attempt to connect to a peer, the **ConnectionOrchestrator** automatically tries multiple connection strategies in priority order until one succeeds.

**Key Innovation:** The Hub is now **completely optional**. When the Hub is unavailable (offline mode, censorship, disaster scenarios), the system falls back to fully decentralized alternatives.

---

## The 6-Tier Hierarchy

```
Priority 1: IPv6 Direct Connection (no NAT)
         ↓ (fails)
Priority 2: IPv4 Direct Connection (local network / port forward)
         ↓ (fails)
Priority 3: Hub WebRTC (STUN/TURN via Hub)
         ↓ (fails or Hub offline)
Priority 4: UDP Hole Punching (DHT-coordinated, 60-70% NAT)
         ↓ (fails - symmetric NAT)
Priority 5: Volunteer Relay (100% NAT coverage)
         ↓ (fails - no relays available)
Priority 6: Gossip Store-and-Forward (disaster fallback)
```

**Design Philosophy:**
- **Best first:** Try lowest-latency, highest-performance options first
- **Hub-optional:** Priorities 4-6 work without Hub infrastructure
- **Universal coverage:** 6 layers ensure connectivity in almost any network condition
- **Privacy-preserving:** End-to-end encryption maintained at all levels

---

## Priority 1: IPv6 Direct Connection

**Goal:** Future-proof, NAT-free connectivity

### How It Works

1. Client queries DHT for peer's IPv6 address
2. Verifies peer has global IPv6 address (2000::/3 range)
3. Establishes direct TLS connection over IPv6
4. No NAT traversal needed (IPv6 has no NAT)

### When It Works

- Both peers have global IPv6 addresses
- ISPs support IPv6 (40%+ networks worldwide as of 2024)
- No firewall blocks IPv6 connections

### Advantages

- **Lowest latency** - Direct peer-to-peer, no intermediaries
- **Best performance** - No NAT overhead
- **Future-proof** - IPv6 is the future of the internet
- **No infrastructure** - Works offline, Hub-independent

### Configuration

```ini
[connection]
enable_ipv6 = true
ipv6_timeout = 10
```

### Timeout

10 seconds

### Success Rate

- 40%+ for networks with IPv6 support
- 0% for IPv4-only networks (falls back to Priority 2)

---

## Priority 2: IPv4 Direct Connection

**Goal:** Direct connectivity on local networks and port-forwarded setups

### How It Works

1. Client queries DHT for peer's IPv4 address (local and external)
2. Attempts direct TLS connection to external IPv4 address
3. Works if peer has:
   - Public IPv4 address (no NAT), OR
   - Port forwarding configured (NAT with manual port mapping)

### When It Works

- Local network (both peers on same LAN)
- Peer has public IPv4 address (VPS, static IP)
- Peer configured port forwarding (router maps external port to internal port)

### Advantages

- **Low latency** - Direct connection
- **Reliable** - No complex NAT traversal
- **Hub-independent** - Works offline

### Configuration

```ini
[connection]
enable_ipv4 = true
ipv4_timeout = 10

[p2p]
listen_port = 8888
listen_host = dual  # or 0.0.0.0 for IPv4 only
```

### Timeout

10 seconds

### Success Rate

- ~100% for local networks
- ~5-10% for internet-wide (public IPs or port forwarding)
- 0% for typical NAT setups (falls back to Priority 3)

---

## Priority 3: Hub WebRTC (STUN/TURN)

**Goal:** Leverage existing Hub infrastructure for NAT traversal

### How It Works

1. Check if Hub is connected
2. Send WebRTC signaling via Hub (offer/answer/ICE candidates)
3. Use STUN servers to discover external IP:port
4. Attempt direct connection via STUN
5. If STUN fails (symmetric NAT), fall back to TURN relay

### When It Works

- Hub is online and connected
- At least one peer behind cone NAT (STUN works)
- TURN relay available (for symmetric NAT fallback)

### Advantages

- **Existing infrastructure** - Uses battle-tested WebRTC stack (aiortc)
- **High success rate** - STUN (~80%) + TURN (~20%) = ~100% when Hub available
- **Well-understood** - WebRTC is mature technology

### Disadvantages

- **Hub dependency** - Requires Hub for signaling
- **Infrastructure cost** - TURN relays are expensive to run
- **Privacy concerns** - TURN relays see traffic metadata

### Configuration

```ini
[connection]
enable_hub_webrtc = true
webrtc_timeout = 30

[webrtc]
stun_servers = stun:stun.l.google.com:19302,...

[turn]
# Optional: Configure your own TURN servers
username = your_turn_username
credential = your_turn_password
servers = turn:your-turn-server.com:3478
```

### Timeout

30 seconds (includes ICE gathering and connectivity checks)

### Success Rate

- ~100% when Hub is available (STUN + TURN fallback)
- 0% when Hub is offline (falls back to Priority 4)

---

## Priority 4: UDP Hole Punching (DHT-Coordinated)

**Goal:** Hub-independent NAT traversal using DHT coordination

### How It Works

**Step 1: Endpoint Discovery (STUN-like, but via DHT peers)**
1. Client sends UDP packets to 3 random DHT peers
2. DHT peers respond with client's reflexive IP:port (source address they see)
3. Client learns its external endpoint without STUN servers

**Step 2: NAT Type Detection**
1. Send UDP from same port to 2 different DHT peers
2. Compare reflexive ports:
   - **Same port** = Cone NAT (hole punching will work ✅)
   - **Different ports** = Symmetric NAT (hole punching will fail ❌)

**Step 3: Coordinated Simultaneous Send (Birthday Paradox)**
1. Both peers announce endpoints in DHT
2. Peers coordinate sync time (5 seconds from now)
3. Both peers send UDP packets at exact same timestamp
4. NAT creates bidirectional mapping (birthday paradox)
5. Connection established!

### When It Works

- **Cone NAT** (60-70% of NATs) - Same external port for all destinations
- DHT has active peers to query for reflexive address
- Both peers can synchronize timing via DHT

### When It Fails

- **Symmetric NAT** (30-40% of NATs) - Different external port per destination
- Firewall blocks UDP entirely
- No DHT peers available for endpoint discovery

### Advantages

- **Hub-independent** - Works when Hub is offline
- **No infrastructure cost** - Uses existing DHT network
- **60-70% success rate** - Covers most NAT types
- **Privacy** - No third-party relay sees traffic

### Disadvantages

- **Fails for symmetric NAT** - Falls back to Priority 5
- **Timing-sensitive** - Requires clock synchronization
- **UDP only** - Needs DTLS upgrade for encryption (TODO)

### ⚠️ Security Warning: DTLS Encryption (v0.10.0)

**Current Status:** UDP hole punching establishes **unencrypted** UDP connections.

**DTLS (Datagram Transport Layer Security)** upgrade is deferred to **v0.11.0+**. Until then:

**Recommendation:**
```ini
[connection]
enable_hole_punching = false  # Disable unencrypted UDP (recommended for v0.10.0)
```

**Why is it disabled by default?**
- **Privacy violation:** UDP connections lack encryption layer
- **Better alternatives exist:**
  - Priority 3 (Hub WebRTC) - Has built-in DTLS encryption
  - Priority 5 (Volunteer Relays) - Uses TLS encryption
- **Implementation complexity:** DTLS handshake adds 2-3 seconds to connection time

**When will DTLS be implemented?**
- **v0.11.0 (Future):** Full DTLS upgrade
- **Flow:**
  1. Perform UDP hole punch (as before)
  2. Exchange certificates over punched UDP hole
  3. Perform DTLS handshake (similar to TLS)
  4. Upgrade socket to DTLS wrapper
  5. All subsequent messages encrypted via DTLS

**Current Workarounds:**
1. **Disable hole punching** (recommended) - Use Priority 3 or 5 instead
2. **Use only for testing** - Don't send sensitive data
3. **Local network only** - Use UDP hole punching only on trusted networks

### Configuration

```ini
[connection]
enable_hole_punching = true
hole_punch_timeout = 15

[hole_punch]
udp_punch_port = 8890
nat_detection_enabled = true
stun_timeout = 5
punch_attempts = 3
```

### Timeout

15 seconds

### Success Rate

- 60-70% for cone NAT
- 0% for symmetric NAT (falls back to Priority 5)

---

## Priority 5: Volunteer Relay Nodes

**Goal:** 100% NAT coverage using volunteer community relays

### How It Works

**Client Mode (Using Relays):**

1. Query DHT for available relay nodes (`relay:*` keys)
2. Score relays by quality:
   - **Uptime** (50% weight)
   - **Available capacity** (30% weight) - 1.0 - (current_peers / max_peers)
   - **Latency** (20% weight) - Normalized 0-500ms range
3. Select highest-scored relay (optionally filter by region)
4. Connect to relay via TLS (Priority 1 or 2)
5. Send `RELAY_REGISTER` request with target peer ID
6. Relay creates session when both peers register
7. Relay forwards `RELAY_MESSAGE` packets between peers

**Server Mode (Volunteering as Relay):**

1. Announce relay availability in DHT:
   ```json
   {
     "node_id": "dpc-node-relay-abc123",
     "ip": "203.0.113.50",
     "port": 8888,
     "available": true,
     "max_peers": 10,
     "current_peers": 3,
     "region": "us-west",
     "uptime": 0.98,
     "latency_ms": 50.0,
     "bandwidth_mbps": 50.0
   }
   ```
2. Handle `RELAY_REGISTER` requests from clients
3. Create relay session when both peers register
4. Forward `RELAY_MESSAGE` packets (encrypted payloads)
5. Apply rate limiting (100 messages/second per peer)
6. Track bandwidth usage, enforce limits

### When It Works

- **Always** - Works for all NAT types (cone, symmetric, CGNAT, restrictive firewalls)
- Requires at least one volunteer relay in DHT
- Peers can connect to relay (via Priority 1 or 2)

### Privacy Guarantees

**Relay sees (unavoidable):**
- Peer node IDs (who is talking to whom)
- Message sizes (payload size in bytes)
- Message timing (when messages are sent)

**Relay does NOT see (end-to-end encryption maintained):**
- Message content (encrypted payload)
- Conversation context
- Personal data

### Advantages

- **100% NAT coverage** - Works for symmetric NAT, CGNAT, everything
- **Hub-independent** - No central infrastructure required
- **Privacy-preserving** - End-to-end encryption maintained
- **Zero infrastructure cost** - Community-powered (volunteer relays)

### Disadvantages

- **Higher latency** - Extra hop through relay
- **Requires volunteers** - Needs community participation
- **Metadata visibility** - Relay sees peer IDs, sizes, timing
- **Dependency on relays** - If no relays available, falls back to Priority 6

### Configuration

**Client Mode:**
```ini
[connection]
enable_relays = true
relay_timeout = 20

[relay]
enabled = true
prefer_region = us-west
cache_timeout = 300
volunteer = false
```

**Server Mode (Volunteering):**
```ini
[relay]
volunteer = true           # Opt-in to volunteer as relay
max_peers = 20             # Support up to 20 sessions
bandwidth_limit_mbps = 50.0
region = us-west
```

### Timeout

20 seconds

### Success Rate

- ~100% when relays are available in DHT
- 0% when no relays available (falls back to Priority 6)

---

## Priority 6: Gossip Store-and-Forward

**Goal:** Disaster-resilient messaging via multi-hop routing

### How It Works

**Epidemic Spreading Algorithm:**

1. Sender creates `GOSSIP_MESSAGE` with:
   - `id`: Unique message ID
   - `source`: Sender node ID
   - `destination`: Recipient node ID
   - `payload`: Encrypted message
   - `hops`: Current hop count (starts at 0)
   - `max_hops`: Maximum allowed hops (default 5)
   - `already_forwarded`: List of nodes that have forwarded this message
   - `vector_clock`: Lamport timestamps for causality tracking
   - `ttl`: Time-to-live (default 24 hours)

2. Sender forwards to N=3 random connected peers (fanout)

3. Each intermediate node:
   - **Is destination?** → Deliver to user, stop forwarding
   - **Already seen?** → Ignore (deduplication via message ID)
   - **TTL expired?** → Drop message
   - **Max hops reached?** → Drop message
   - **Otherwise:** Forward to N=3 random peers (exclude `already_forwarded`)

4. Message propagates through network like epidemic

**Anti-Entropy Sync:**

Every 5 minutes:
1. Pick random connected peer
2. Exchange vector clocks
3. Compare clocks to detect missing messages
4. Request missing messages from peer
5. Send messages peer is missing

**Vector Clocks:**

Track causality:
```python
{
  "dpc-node-alice": 10,  # Alice has seen 10 events
  "dpc-node-bob": 5,     # Bob has seen 5 events
  "dpc-node-charlie": 8  # Charlie has seen 8 events
}
```

Detect relationships:
- `happens_before(A, B)` - A caused B
- `concurrent_with(A, B)` - A and B are independent
- `equals(A, B)` - Same state

### When It Works

- **Always** (eventual delivery guarantee)
- No real-time requirement
- At least one multi-hop path exists between sender and receiver
- Network is connected (not completely partitioned)

### Use Cases

- **Offline messaging** - Send message while peer is offline, delivered when they come online
- **Disaster scenarios** - Infrastructure failures, natural disasters, internet outages
- **Censorship resistance** - Multi-hop routing bypasses censorship
- **Knowledge sync** - Eventual consistency for knowledge commits

### Advantages

- **Eventual delivery guarantee** - Messages eventually reach destination
- **Disaster resilient** - Works when all infrastructure fails
- **Multi-hop routing** - Messages can traverse network indirectly
- **No infrastructure** - Fully peer-to-peer

### Disadvantages

- **Not real-time** - High latency (seconds to minutes)
- **High bandwidth** - Epidemic spreading creates redundancy
- **No delivery confirmation** - Best-effort only
- **Message expiration** - TTL limits (24 hours default)

### Configuration

```ini
[connection]
enable_gossip = true
gossip_timeout = 5  # How long to wait before falling back to gossip

[gossip]
enabled = true
max_hops = 5                # Maximum hops for forwarding
fanout = 3                  # Forward to 3 random peers
ttl_seconds = 86400         # 24-hour TTL
sync_interval = 300         # Anti-entropy sync every 5 minutes
cleanup_interval = 600      # Cleanup expired messages every 10 minutes
priority = normal
```

### Timeout

5 seconds (how long to wait for other strategies before accepting gossip as fallback)

### Success Rate

- ~100% eventual delivery (given enough time and network connectivity)
- Not suitable for real-time communication

---

## Decision Flow

Here's how the ConnectionOrchestrator makes decisions:

```
User connects to peer "dpc-node-bob"
        ↓
Query DHT for peer endpoints
        ↓
    ┌───────────────────────────────────────┐
    │   Try Priority 1: IPv6 Direct         │
    │   - Has IPv6? → Try connection        │
    │   - Timeout: 10s                      │
    └────────────┬──────────────────────────┘
                 ↓ (fails)
    ┌───────────────────────────────────────┐
    │   Try Priority 2: IPv4 Direct         │
    │   - Has external IPv4? → Try          │
    │   - Timeout: 10s                      │
    └────────────┬──────────────────────────┘
                 ↓ (fails)
    ┌───────────────────────────────────────┐
    │   Try Priority 3: Hub WebRTC          │
    │   - Hub connected? → WebRTC signaling │
    │   - STUN → direct connection          │
    │   - Fallback to TURN relay            │
    │   - Timeout: 30s                      │
    └────────────┬──────────────────────────┘
                 ↓ (fails or Hub offline)
    ┌───────────────────────────────────────┐
    │   Try Priority 4: UDP Hole Punch      │
    │   - Discover endpoint via DHT         │
    │   - Detect NAT type                   │
    │   - Coordinated simultaneous send     │
    │   - Timeout: 15s                      │
    └────────────┬──────────────────────────┘
                 ↓ (fails - symmetric NAT)
    ┌───────────────────────────────────────┐
    │   Try Priority 5: Volunteer Relay     │
    │   - Query DHT for relays              │
    │   - Score by quality                  │
    │   - Connect via relay                 │
    │   - Timeout: 20s                      │
    └────────────┬──────────────────────────┘
                 ↓ (fails - no relays)
    ┌───────────────────────────────────────┐
    │   Try Priority 6: Gossip              │
    │   - Send gossip message               │
    │   - Epidemic spreading (fanout=3)     │
    │   - Eventual delivery                 │
    │   - Timeout: 5s (to accept gossip)    │
    └────────────┬──────────────────────────┘
                 ↓ (success)
        Connected to peer!
```

**Total timeout:** Up to 90 seconds (10+10+30+15+20+5) for all strategies

**Early success:** Connection established as soon as any strategy succeeds (e.g., IPv6 succeeds in 10s, skip remaining strategies)

---

## Hybrid Mode: With vs Without Hub

**With Hub Available:**
```
IPv6 Direct (10s)
    ↓
IPv4 Direct (10s)
    ↓
Hub WebRTC (30s) ← Existing STUN/TURN infrastructure
    ↓
Gossip (5s)
```
- Priorities 4-5 skipped (WebRTC already covers NAT traversal)
- Total: 55s

**Without Hub (Offline/Censored):**
```
IPv6 Direct (10s)
    ↓
IPv4 Direct (10s)
    ↓
UDP Hole Punch (15s) ← Hub-independent
    ↓
Volunteer Relay (20s) ← Hub-independent
    ↓
Gossip (5s)
```
- Priority 3 skipped (Hub offline)
- Total: 60s

**Key Insight:** Both paths provide 100% eventual connectivity. Hub provides faster/easier path, but system works without it.

---

## Success Rate Analysis

Based on network composition:

| Scenario | Success Strategy | Success Rate |
|----------|-----------------|--------------|
| Both IPv6 | Priority 1 (IPv6 Direct) | ~100% |
| Local network | Priority 2 (IPv4 Direct) | ~100% |
| Hub + Cone NAT | Priority 3 (Hub WebRTC STUN) | ~100% |
| Hub + Symmetric NAT | Priority 3 (Hub WebRTC TURN) | ~100% |
| No Hub + Cone NAT | Priority 4 (UDP Hole Punch) | 60-70% |
| No Hub + Symmetric NAT | Priority 5 (Volunteer Relay) | ~100% (if relays available) |
| Disaster scenario | Priority 6 (Gossip) | ~100% (eventual) |

**Overall success rate:** ~100% (given enough time for gossip)

**Real-time success rate:** 95%+ (assuming some relay volunteers)

---

## Configuration Examples

### Scenario 1: Maximum Performance (Local Network)

```ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = false  # Not needed on local network
enable_hole_punching = false
enable_relays = false
enable_gossip = false
```

### Scenario 2: Internet-Wide, Hub Available

```ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = true   # Use Hub for NAT traversal
enable_hole_punching = false
enable_relays = false
enable_gossip = true       # Fallback only
```

### Scenario 3: Offline Mode / Hub-Independent

```ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = false  # Hub offline
enable_hole_punching = true  # DHT-based NAT traversal
enable_relays = true         # Community relays
enable_gossip = true         # Disaster fallback
```

### Scenario 4: Disaster Resilience (All Strategies)

```ini
[connection]
enable_ipv6 = true
enable_ipv4 = true
enable_hub_webrtc = true
enable_hole_punching = true
enable_relays = true
enable_gossip = true

# Increase timeouts for challenging conditions
ipv6_timeout = 20
ipv4_timeout = 20
webrtc_timeout = 60
hole_punch_timeout = 30
relay_timeout = 40
gossip_timeout = 10
```

---

## Monitoring and Debugging

### Check Connection Statistics

```python
# Get orchestrator stats (TODO: Expose via WebSocket API)
{
  "total_attempts": 100,
  "successful_connections": 95,
  "failed_connections": 5,
  "strategy_usage": {
    "ipv6_direct": 40,      # 40% IPv6
    "ipv4_direct": 10,      # 10% local network
    "hub_webrtc": 30,       # 30% WebRTC
    "udp_hole_punch": 10,   # 10% hole punch
    "volunteer_relay": 5,   # 5% relay
    "gossip": 0             # 0% gossip (disaster only)
  }
}
```

### Common Issues

**Issue:** All strategies fail
- **Cause:** Peer not announced in DHT
- **Solution:** Peer must run `dht_manager.announce_full()` on startup

**Issue:** IPv6 always fails
- **Cause:** No global IPv6 address
- **Solution:** Check `ip -6 addr` (Linux) or `ipconfig` (Windows) for 2000::/3 addresses

**Issue:** Hole punching always fails
- **Cause:** Symmetric NAT or UDP blocked
- **Solution:** Verify NAT type with `hole_punch_manager._detect_nat_type()`

**Issue:** No relays available
- **Cause:** No volunteers in DHT
- **Solution:** Set `relay.volunteer = true` to volunteer, or wait for community growth

---

## See Also

- [Configuration Guide](./CONFIGURATION.md) - All config options
- [DHT Architecture](../CHANGELOG.md#phase-21-dht-based-peer-discovery) - DHT implementation details
- [ROADMAP](../ROADMAP.md) - Phase 6 completion status
