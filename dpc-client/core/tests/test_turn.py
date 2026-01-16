#!/usr/bin/env python3
"""
Quick test to verify TURN server connectivity and ICE candidate gathering.
Run this to diagnose WebRTC NAT traversal issues.
"""

import asyncio
from pathlib import Path
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer

# Import settings to get TURN credentials
import sys
sys.path.insert(0, str(Path(__file__).parent))
from dpc_client_core.settings import Settings

async def test_turn_connectivity():
    """Test TURN server connectivity by gathering ICE candidates."""

    print("Testing TURN/STUN server connectivity...")
    print("=" * 60)

    # Load TURN credentials from Settings (same as webrtc_peer.py)
    dpc_home = Path.home() / ".dpc"
    settings = Settings(dpc_home)
    turn_username = settings.get_turn_username()
    turn_credential = settings.get_turn_credential()

    # Track which servers we're testing
    turn_servers_to_test = []

    # Same configuration as webrtc_peer.py
    ice_servers = [
        # STUN servers (for discovering public IP)
        RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
        RTCIceServer(urls=["stun:global.stun.twilio.com:3478"]),
    ]

    # Add TURN servers if credentials are configured
    if turn_username and turn_credential:
        print(f"\n[OK] TURN credentials found: {turn_username[:8]}...")

        # Metered.ca TURN servers
        turn_servers_to_test.extend([
            ("Metered.ca STUN", "stun:stun.relay.metered.ca:80"),
            ("Metered.ca TURN (UDP 80)", "turn:global.relay.metered.ca:80"),
            ("Metered.ca TURN (TCP 80)", "turn:global.relay.metered.ca:80?transport=tcp"),
            ("Metered.ca TURN (UDP 443)", "turn:global.relay.metered.ca:443"),
            ("Metered.ca TURN (TLS 443)", "turns:global.relay.metered.ca:443?transport=tcp"),
        ])

        ice_servers.append(
            RTCIceServer(
                urls=[
                    "stun:stun.relay.metered.ca:80",
                    "turn:global.relay.metered.ca:80",
                    "turn:global.relay.metered.ca:80?transport=tcp",
                    "turn:global.relay.metered.ca:443",
                    "turns:global.relay.metered.ca:443?transport=tcp",
                ],
                username=turn_username,
                credential=turn_credential
            )
        )
    else:
        print("\n[!] Warning: No TURN credentials configured!")
        print("  Set environment variables:")
        print("    DPC_TURN_USERNAME=your_username")
        print("    DPC_TURN_CREDENTIAL=your_password")
        print("  Or add to ~/.dpc/config.ini:")
        print("    [turn]")
        print("    username = your_username")
        print("    credential = your_password")
        print()

        # Fallback to free servers
        turn_servers_to_test.extend([
            ("OpenRelay (UDP 80)", "turn:openrelay.metered.ca:80"),
            ("OpenRelay (UDP 443)", "turn:openrelay.metered.ca:443"),
            ("OpenRelay (TCP 443)", "turn:openrelay.metered.ca:443?transport=tcp"),
        ])

        ice_servers.append(
            RTCIceServer(
                urls=[
                    "turn:openrelay.metered.ca:80",
                    "turn:openrelay.metered.ca:443",
                    "turn:openrelay.metered.ca:443?transport=tcp"
                ],
                username="openrelayproject",
                credential="openrelayproject"
            )
        )

    print("\nTURN servers to test:")
    for name, url in turn_servers_to_test:
        print(f"  • {name}: {url}")
    print()

    configuration = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(configuration=configuration)

    gathering_complete = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def on_gathering_state():
        state = pc.iceGatheringState
        print(f"\nICE gathering state: {state}")
        if state == "complete":
            gathering_complete.set()

    # Create a data channel to trigger ICE gathering
    pc.createDataChannel("test")

    # Create an offer to start ICE gathering
    print("\nCreating offer to trigger ICE gathering...")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    # Wait for ICE gathering to complete (max 15 seconds)
    try:
        await asyncio.wait_for(gathering_complete.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        print("\n[!] Warning: ICE gathering timeout after 15s")

    # Parse ICE candidates from SDP (aiortc embeds them in SDP, not as events)
    print("\nParsing ICE candidates from SDP...")
    sdp = pc.localDescription.sdp
    candidates_gathered = {
        "host": 0,
        "srflx": 0,  # Server reflexive (STUN)
        "relay": 0,  # TURN relay
    }

    relay_candidates_by_server = {}  # Track which TURN server provided relay candidates

    for line in sdp.split('\n'):
        if line.startswith('a=candidate:'):
            if "typ host" in line:
                candidates_gathered["host"] += 1
                print(f"[+] Host candidate (local network)")
            elif "typ srflx" in line:
                candidates_gathered["srflx"] += 1
                # Extract the IP to show which STUN server was used
                parts = line.split()
                ip = parts[4] if len(parts) > 4 else "unknown"
                print(f"[+] SRFLX candidate (STUN/public IP): {ip}")
            elif "typ relay" in line:
                candidates_gathered["relay"] += 1
                # Extract relay candidate details
                # Format: a=candidate:foundation component protocol priority ip port typ relay raddr X rport Y
                parts = line.split()
                relay_ip = parts[4] if len(parts) > 4 else "unknown"
                relay_port = parts[5] if len(parts) > 5 else "unknown"
                protocol = parts[2].upper() if len(parts) > 2 else "unknown"

                raddr_idx = line.find("raddr")
                related_addr = "unknown"
                if raddr_idx != -1:
                    raddr_parts = line[raddr_idx:].split()
                    if len(raddr_parts) > 1:
                        related_addr = raddr_parts[1]

                print(f"[+] RELAY candidate (TURN): {relay_ip}:{relay_port} {protocol} (related: {related_addr})")

                # Match RELAY to TURN server
                # Note: relay_port is the allocated ephemeral port, not the TURN server's listening port
                # TURN servers listen on 80/443 but allocate different ports for relay traffic
                matched = False

                if turn_username and turn_credential:
                    # Using Metered.ca credentials - relay must be from Metered.ca
                    # Match by protocol to identify which server type
                    if protocol == "UDP":
                        # Could be from UDP 80 or UDP 443 listener
                        # We can't distinguish without more info, so just mark as "Metered.ca TURN (UDP)"
                        server_name = "Metered.ca TURN (UDP)"
                        relay_candidates_by_server[server_name] = f"{relay_ip}:{relay_port}"
                        matched = True
                    elif protocol == "TCP":
                        # Could be from TCP 80, TLS 443, or TCP 443
                        # Mark as generic TCP relay
                        server_name = "Metered.ca TURN (TCP)"
                        relay_candidates_by_server[server_name] = f"{relay_ip}:{relay_port}"
                        matched = True
                else:
                    # Using OpenRelay - mark generically
                    if protocol == "UDP":
                        server_name = "OpenRelay TURN (UDP)"
                    else:
                        server_name = "OpenRelay TURN (TCP)"
                    relay_candidates_by_server[server_name] = f"{relay_ip}:{relay_port}"
                    matched = True
            else:
                print(f"? Unknown candidate type: {line[:80]}...")

    # Results
    print("\n" + "=" * 60)
    print("ICE Candidate Gathering Results:")
    print("=" * 60)
    print(f"  Host candidates (local):     {candidates_gathered['host']}")
    print(f"  SRFLX candidates (STUN):     {candidates_gathered['srflx']}")
    print(f"  RELAY candidates (TURN):     {candidates_gathered['relay']}")
    print()

    # Diagnosis
    if candidates_gathered["host"] == 0:
        print("[X] ERROR: No host candidates - network interface issue")
    else:
        print(f"[OK] Host candidates OK ({candidates_gathered['host']} found)")

    if candidates_gathered["srflx"] == 0:
        print("[!] WARNING: No SRFLX candidates - STUN servers may be unreachable")
        print("  This means your public IP cannot be discovered")
    else:
        print(f"[OK] STUN OK ({candidates_gathered['srflx']} public IP candidates)")

    # Detailed TURN server status
    print()
    print("TURN Server Test Results:")
    print("-" * 60)

    if candidates_gathered["relay"] == 0:
        print("[X] ALL TURN servers FAILED - No RELAY candidates!")
        print()
        print("Tested servers:")
        for name, url in turn_servers_to_test:
            print(f"  [X] {name}: {url}")
        print()
        print("Possible causes:")
        print("  1. Firewall blocking UDP/TCP to TURN servers")
        print("  2. TURN server credentials invalid")
        print("  3. All tested TURN servers are down")
        print("  4. Corporate network blocking TURN ports")
        print()
        print("[!] WebRTC connections will FAIL unless both peers are on same network")
        print()
        print("Solutions:")
        print("  - For local testing: Use Direct TLS (dpc://IP:8888/node-id)")
        print("  - For production: Set up your own TURN server (coturn)")
        print("  - Get free credentials: https://numb.viagenie.ca/")
    else:
        print(f"[OK] SUCCESS - {candidates_gathered['relay']} RELAY candidate(s) obtained!")
        print()
        if relay_candidates_by_server:
            print("Working TURN servers:")
            for name, ip in relay_candidates_by_server.items():
                print(f"  [+] {name}: {ip}")
        print()
        # Don't show individual servers as "failed" since we group by protocol
        # The important thing is whether we got UDP/TCP relays, not which specific port

    print("\n" + "=" * 60)

    # Clean up
    await pc.close()

if __name__ == "__main__":
    asyncio.run(test_turn_connectivity())
