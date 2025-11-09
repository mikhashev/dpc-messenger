#!/usr/bin/env python3
"""
Test TURN Server Connectivity
Run this to verify OpenRelay TURN server is accessible
"""

import asyncio
import socket

async def test_turn_connectivity():
    """Test if OpenRelay TURN server is reachable."""
    
    print("Testing OpenRelay TURN Server Connectivity...")
    print("=" * 50)
    
    # Test 1: DNS Resolution
    print("\n1. Testing DNS resolution...")
    try:
        host = "openrelay.metered.ca"
        ip = socket.gethostbyname(host)
        print(f"   ✅ DNS OK: {host} → {ip}")
    except Exception as e:
        print(f"   ❌ DNS FAILED: {e}")
        return False
    
    # Test 2: TCP Connectivity on port 80
    print("\n2. Testing TCP connectivity on port 80...")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 80),
            timeout=5.0
        )
        writer.close()
        await writer.wait_closed()
        print(f"   ✅ TCP Port 80 OK")
    except asyncio.TimeoutError:
        print(f"   ❌ TCP Port 80 TIMEOUT")
        return False
    except Exception as e:
        print(f"   ❌ TCP Port 80 FAILED: {e}")
        return False
    
    # Test 3: Try STUN server as comparison
    print("\n3. Testing STUN server (Google)...")
    try:
        stun_host = "stun.l.google.com"
        stun_ip = socket.gethostbyname(stun_host)
        print(f"   ✅ STUN DNS OK: {stun_host} → {stun_ip}")
    except Exception as e:
        print(f"   ❌ STUN DNS FAILED: {e}")
    
    print("\n" + "=" * 50)
    print("TURN Server Connectivity: PASSED")
    print("\nNOTE: This only tests if the server is reachable.")
    print("Actual TURN relay functionality is tested during WebRTC connection.")
    print("\nIf WebRTC still fails with 'ICE connection state: failed',")
    print("possible causes:")
    print("  1. UDP traffic is blocked by firewall")
    print("  2. TURN server is overloaded (try alternative)")
    print("  3. Both peers need different NAT traversal method")
    
    return True

if __name__ == "__main__":
    try:
        result = asyncio.run(test_turn_connectivity())
        exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        exit(1)