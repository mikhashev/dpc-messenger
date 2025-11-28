# dpc-client/core/dpc_client_core/stun_discovery.py
"""
Standalone STUN discovery for detecting external IP address.
Does not require establishing a WebRTC connection.
"""

import asyncio
import socket
from typing import Optional


async def discover_external_ip(stun_servers: list[str], timeout: float = 5.0) -> Optional[str]:
    """
    Discover external IP address using STUN servers.

    Args:
        stun_servers: List of STUN server URLs (e.g., ['stun:stun.l.google.com:19302'])
        timeout: Timeout in seconds for STUN query

    Returns:
        External IP address as string, or None if discovery failed
    """
    for stun_url in stun_servers:
        try:
            # Parse STUN URL
            if stun_url.startswith('stun:'):
                stun_url = stun_url[5:]  # Remove 'stun:' prefix

            if ':' in stun_url:
                host, port_str = stun_url.rsplit(':', 1)
                port = int(port_str)
            else:
                host = stun_url
                port = 3478  # Default STUN port

            # Perform STUN binding request
            external_ip = await _stun_binding_request(host, port, timeout)

            if external_ip:
                print(f"[STUN Discovery] External IP discovered: {external_ip} (via {host}:{port})")
                return external_ip

        except Exception as e:
            print(f"[STUN Discovery] Failed to query {stun_url}: {e}")
            continue

    print("[STUN Discovery] Could not discover external IP from any STUN server")
    return None


async def _stun_binding_request(host: str, port: int, timeout: float) -> Optional[str]:
    """
    Perform a STUN binding request to discover external IP.

    Args:
        host: STUN server hostname
        port: STUN server port
        timeout: Request timeout in seconds

    Returns:
        External IP address or None
    """
    try:
        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)

        # STUN binding request message
        # Format: [Message Type (2 bytes)] [Message Length (2 bytes)] [Magic Cookie (4 bytes)] [Transaction ID (12 bytes)]
        message_type = b'\x00\x01'  # Binding Request
        message_length = b'\x00\x00'  # No attributes
        magic_cookie = b'\x21\x12\xa4\x42'  # RFC 5389 magic cookie
        transaction_id = b'\x00' * 12  # Simple transaction ID

        stun_request = message_type + message_length + magic_cookie + transaction_id

        # Send request
        sock.sendto(stun_request, (host, port))

        # Receive response
        data, addr = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, sock.recvfrom, 1024),
            timeout=timeout
        )

        sock.close()

        # Parse STUN response
        if len(data) < 20:
            return None

        # Check if it's a binding response (0x0101)
        if data[0:2] != b'\x01\x01':
            return None

        # Parse attributes
        # Skip header (20 bytes) and look for MAPPED-ADDRESS or XOR-MAPPED-ADDRESS
        offset = 20
        while offset < len(data):
            if offset + 4 > len(data):
                break

            attr_type = int.from_bytes(data[offset:offset+2], 'big')
            attr_length = int.from_bytes(data[offset+2:offset+4], 'big')

            if offset + 4 + attr_length > len(data):
                break

            # XOR-MAPPED-ADDRESS (0x0020) - preferred
            # MAPPED-ADDRESS (0x0001) - fallback
            if attr_type in (0x0020, 0x0001):
                attr_data = data[offset+4:offset+4+attr_length]

                if len(attr_data) >= 8:
                    # Skip reserved byte and family byte
                    # Port is at bytes 2-3, IP is at bytes 4-7
                    if attr_type == 0x0020:  # XOR-MAPPED-ADDRESS
                        # XOR the IP with magic cookie
                        ip_bytes = attr_data[4:8]
                        magic = magic_cookie

                        # XOR each byte
                        ip_xor = bytes(ip_bytes[i] ^ magic[i] for i in range(4))
                        external_ip = '.'.join(str(b) for b in ip_xor)
                    else:  # MAPPED-ADDRESS
                        ip_bytes = attr_data[4:8]
                        external_ip = '.'.join(str(b) for b in ip_bytes)

                    return external_ip

            # Move to next attribute (attribute length + padding to 4-byte boundary)
            attr_length_padded = (attr_length + 3) & ~3
            offset += 4 + attr_length_padded

        return None

    except asyncio.TimeoutError:
        return None
    except Exception as e:
        print(f"[STUN] Error during binding request to {host}:{port}: {e}")
        return None


# Self-test (example servers - configure real servers in ~/.dpc/config.ini)
if __name__ == "__main__":
    async def test():
        # Example STUN servers for testing only
        # In production, servers are loaded from ~/.dpc/config.ini [webrtc] stun_servers
        stun_servers = [
            'stun:stun.l.google.com:19302',
            'stun:stun1.l.google.com:19302',
        ]

        print("Testing STUN discovery...")
        external_ip = await discover_external_ip(stun_servers)

        if external_ip:
            print(f"✓ External IP: {external_ip}")
        else:
            print("✗ Failed to discover external IP")

    asyncio.run(test())
