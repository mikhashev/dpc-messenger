# Hub + TURN Server Integration Guide

## Overview

This document explains how to integrate a TURN server with your D-PC Hub to enable reliable WebRTC connections across the internet without depending on third-party TURN services.

---

## Architecture

### Current Setup (Hub Only)
```
Client A (MacOS)                                    Client B (Windows)
     |                                                      |
     |---- WebSocket Signaling (SDP exchange) ------------>|
     |<------------------- Via Hub -------------------------|
     |                                                      |
     |<----------- Direct P2P (WebRTC) fails ------------->|
                (No TURN relay available)
```

### New Setup (Hub + TURN)
```
Client A (MacOS)                HUB SERVER                Client B (Windows)
     |                              |                            |
     |--- WebSocket Signaling ----->|<--- WebSocket Signaling ---|
     |                              |                            |
     |                        TURN Server                        |
     |                       (coturn:3478)                       |
     |                              |                            |
     |---- Allocate TURN relay ---->|                            |
     |                              |<---- Allocate TURN relay ---|
     |                              |                            |
     |<=== Encrypted P2P data via TURN relay (UDP/TCP) ========>|
```

**Key Benefits:**
- Single server for signaling AND relay
- No dependency on unreliable free TURN services
- You control credentials and availability
- Works with ngrok setup (see below)

---

## Implementation Plan

### Phase 1: Install coturn on Hub Server

#### Step 1.1: Install coturn Package

**On Windows (your current setup):**
```powershell
# Install via Chocolatey (if you have it)
choco install coturn

# Or download pre-built binary from:
# https://github.com/coturn/coturn/releases
```

**On Linux (if you move Hub to Linux later):**
```bash
sudo apt-get update
sudo apt-get install coturn

# Enable coturn service
sudo systemctl enable coturn
```

#### Step 1.2: Configure coturn

**Create/edit coturn config file:**

**Windows:** `C:\ProgramData\coturn\turnserver.conf`
**Linux:** `/etc/turnserver.conf`

```ini
# === Basic Configuration ===
# TURN server listening ports
listening-port=3478
tls-listening-port=5349

# Your ngrok domain (or public IP if not using ngrok)
realm=unfascinated-semifiguratively-velda.ngrok-free.dev
server-name=unfascinated-semifiguratively-velda.ngrok-free.dev

# === Authentication ===
# Static username/password (simple for MVP)
user=dpc-turn-user:your_secure_password_here

# Or use long-term credentials (recommended for production)
lt-cred-mech

# === Network Configuration ===
# Your server's public IP (ngrok will handle this)
# Leave commented if using ngrok - coturn will auto-detect
# external-ip=YOUR_PUBLIC_IP

# Allow relay traffic
relay-ip=127.0.0.1

# Relay address range (allow all private networks)
min-port=49152
max-port=65535

# === Security ===
# Disable by default, enable what you need
no-cli
no-loopback-peers
no-multicast-peers

# Fingerprint required (WebRTC standard)
fingerprint

# === Logging ===
verbose
log-file=C:\ProgramData\coturn\turnserver.log  # Windows
# log-file=/var/log/turnserver.log  # Linux

# === Performance ===
# Recommended settings for small deployment
user-quota=0
total-quota=0
```

#### Step 1.3: Start coturn

**Windows:**
```powershell
# Start coturn service (if installed as service)
net start coturn

# Or run manually for testing
coturn -c C:\ProgramData\coturn\turnserver.conf
```

**Linux:**
```bash
sudo systemctl start coturn
sudo systemctl status coturn

# View logs
sudo tail -f /var/log/turnserver.log
```

---

### Phase 2: Configure ngrok to Expose TURN Ports

#### Step 2.1: Update ngrok Configuration

**Current ngrok config (HTTP only):**
```yaml
# ngrok.yml
version: "2"
authtoken: YOUR_NGROK_TOKEN
tunnels:
  dpc-hub:
    proto: http
    addr: 8000
    domain: unfascinated-semifiguratively-velda.ngrok-free.dev
```

**New ngrok config (HTTP + TURN):**
```yaml
# ngrok.yml
version: "2"
authtoken: YOUR_NGROK_TOKEN
tunnels:
  dpc-hub:
    proto: http
    addr: 8000
    domain: unfascinated-semifiguratively-velda.ngrok-free.dev

  turn-udp:
    proto: udp
    addr: 3478
    # Note: UDP tunnels may require ngrok paid plan

  turn-tcp:
    proto: tcp
    addr: 3478
    # TCP fallback for restrictive firewalls
```

**Important Notes:**
- **ngrok Free Plan:** Only supports HTTP/HTTPS tunnels
- **ngrok Paid Plan ($8/month):** Supports UDP/TCP for TURN
- **Alternative:** Use a VPS with public IP instead of ngrok (DigitalOcean, AWS, etc.)

#### Step 2.2: Start ngrok with Multiple Tunnels

```powershell
# Start all tunnels
ngrok start --all --config ngrok.yml
```

---

### Phase 3: Update dpc-client to Use Hub's TURN

#### Step 3.1: Update webrtc_peer.py

**File:** `dpc-client/core/dpc_client_core/webrtc_peer.py`

**Change from:**
```python
ice_servers = [
    # STUN servers
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),

    # Free TURN servers (unreliable)
    RTCIceServer(
        urls=["turn:openrelay.metered.ca:80"],
        username="openrelayproject",
        credential="openrelayproject"
    ),
]
```

**Change to:**
```python
ice_servers = [
    # STUN servers (public IP discovery)
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),

    # Hub's TURN server (reliable, self-hosted)
    RTCIceServer(
        urls=[
            "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478",
            "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478?transport=tcp"
        ],
        username="dpc-turn-user",
        credential="your_secure_password_here"
    ),

    # Fallback: Keep one free TURN server for redundancy
    RTCIceServer(
        urls=["turn:openrelay.metered.ca:80"],
        username="openrelayproject",
        credential="openrelayproject"
    ),
]
```

#### Step 3.2: Update test_turn.py

**File:** `dpc-client/core/test_turn.py`

Add Hub TURN to the test list:
```python
turn_servers_to_test = [
    ("Hub TURN (UDP)", "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478"),
    ("Hub TURN (TCP)", "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478?transport=tcp"),
    ("OpenRelay (port 80)", "turn:openrelay.metered.ca:80"),
    # ... other servers
]

ice_servers = [
    # ... existing STUN servers ...

    # Hub's TURN server
    RTCIceServer(
        urls=[
            "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478",
            "turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478?transport=tcp"
        ],
        username="dpc-turn-user",
        credential="your_secure_password_here"
    ),
    # ... other servers ...
]
```

---

### Phase 4: Testing

#### Step 4.1: Test coturn Connectivity

**From Windows client:**
```bash
cd dpc-client/core
poetry run python test_turn.py
```

**Expected output:**
```
TURN Server Test Results:
------------------------------------------------------------
✓ SUCCESS - 2 RELAY candidate(s) obtained!

Working TURN servers:
  ✓ Hub TURN (UDP): 165.227.xxx.xxx
  ✓ Hub TURN (TCP): 165.227.xxx.xxx
```

#### Step 4.2: Test WebRTC Connection

**Start both clients and connect via Hub:**

**Windows logs should show:**
```
[dpc-node-xxx] ICE gathering complete - host:5 srflx:1 relay:2
[dpc-node-xxx] ICE connection state: completed
✅ WebRTC connection established with dpc-node-yyy
✅ Data channel opened with dpc-node-yyy
  - Sent name to dpc-node-yyy
  - Requested AI providers from dpc-node-yyy
CoreService received message: PROVIDERS_RESPONSE
  - Received X providers from dpc-node-yyy
```

**MacOS logs should show:**
```
[dpc-node-yyy] ICE gathering complete - host:3 srflx:1 relay:2
[dpc-node-yyy] ICE connection state: completed
✅ WebRTC connection established with dpc-node-xxx
✅ Data channel opened with dpc-node-xxx
CoreService received message: GET_PROVIDERS
  - Sending X providers to dpc-node-xxx
```

---

## Your ngrok Question: Can You Still Use ngrok on Windows?

### Answer: YES, but with Limitations

**What Works:**
- ✅ HTTP/WebSocket traffic (Hub signaling) - **ngrok Free Plan**
- ✅ TURN over TCP (port 3478) - **Requires ngrok Paid Plan ($8/mo)**
- ❌ TURN over UDP (port 3478) - **Requires ngrok Paid Plan**

### Option 1: ngrok Paid Plan (Recommended for Testing)

**Cost:** $8/month
**Pros:**
- Keep your Windows PC setup
- No need to manage a VPS
- UDP + TCP support for TURN

**Setup:**
1. Upgrade ngrok: https://ngrok.com/pricing
2. Update ngrok.yml with UDP/TCP tunnels (see Phase 2.1)
3. Run: `ngrok start --all`

### Option 2: ngrok Free + TCP-Only TURN (Limited)

**Cost:** Free
**Pros:**
- No monthly cost
- Might work for some clients

**Cons:**
- UDP TURN not available (less efficient)
- Some WebRTC clients prefer UDP

**Setup:**
```yaml
# ngrok.yml - TCP only
tunnels:
  dpc-hub:
    proto: http
    addr: 8000
    domain: unfascinated-semifiguratively-velda.ngrok-free.dev

  turn-tcp:
    proto: tcp
    addr: 3478
```

Then update dpc-client to use TCP only:
```python
RTCIceServer(
    urls=["turn:unfascinated-semifiguratively-velda.ngrok-free.dev:3478?transport=tcp"],
    username="dpc-turn-user",
    credential="your_secure_password_here"
),
```

### Option 3: Move to VPS with Public IP (Best for Production)

**Cost:** $5-10/month (DigitalOcean, AWS, Hetzner)
**Pros:**
- Full UDP + TCP support
- Static public IP
- Better performance than ngrok tunnels
- No ngrok relay overhead

**Setup:**
1. Rent Ubuntu VPS (DigitalOcean Droplet, AWS EC2, etc.)
2. Install Hub + coturn on VPS
3. Point domain directly to VPS IP (no ngrok needed)

---

## Summary

**Goal:** Use Hub server as TURN relay for reliable WebRTC connections

**Implementation:**
1. Install coturn alongside Hub (Windows or Linux)
2. Configure coturn with credentials
3. Expose TURN ports via ngrok (paid plan) or use VPS
4. Update dpc-client to use Hub's TURN server
5. Test with `test_turn.py` to verify RELAY candidates

**ngrok Compatibility:**
- ✅ **Works** with ngrok paid plan ($8/mo) - UDP + TCP
- ⚠️ **Limited** with ngrok free plan - TCP only
- ✅ **Best** with VPS + public IP - no ngrok needed

**Next Steps:**
1. Choose your deployment option (ngrok paid, ngrok free + TCP, or VPS)
2. Install coturn on your Windows Hub server
3. Configure coturn and test connectivity
4. Update client code to use Hub TURN
5. Verify WebRTC connections work across internet

---

## Additional Resources

- **coturn Documentation:** https://github.com/coturn/coturn/wiki
- **ngrok UDP/TCP Tunnels:** https://ngrok.com/docs/tcp-udp-tls/
- **WebRTC TURN Server Guide:** https://www.html5rocks.com/en/tutorials/webrtc/infrastructure/
- **VPS Providers:**
  - DigitalOcean: https://www.digitalocean.com/pricing/droplets
  - Hetzner: https://www.hetzner.com/cloud (cheapest)
  - AWS Lightsail: https://aws.amazon.com/lightsail/pricing/
