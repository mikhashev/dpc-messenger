# dpc-client/core/dpc_client_core/stun_discovery.py
"""
Network-resilient STUN discovery for detecting external IP address.
Includes connectivity checks, retry logic, and periodic re-discovery.
"""

import asyncio
import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)


async def _check_internet_connectivity() -> bool:
    """
    Check if internet is reachable before attempting STUN.

    Tests connectivity to well-known DNS servers via TCP port 53.
    This is more reliable than DNS lookups on Windows during network transitions.

    Returns:
        True if internet is reachable, False otherwise
    """
    test_hosts = [
        ("8.8.8.8", 53),         # Google DNS
        ("1.1.1.1", 53),         # Cloudflare DNS
        ("208.67.222.222", 53)   # OpenDNS
    ]

    for host, port in test_hosts:
        try:
            # Quick TCP connection test (timeout 2s)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)

            # Use async connect
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.sock_connect(sock, (host, port)),
                timeout=2.0
            )
            sock.close()

            logger.info("Internet connectivity confirmed via %s", host)
            return True

        except Exception as e:
            logger.debug("Connectivity check failed for %s:%d: %s", host, port, e)
            continue

    logger.warning("No internet connectivity detected (tried %s)",
                  ", ".join(f"{h}:{p}" for h, p in test_hosts))
    return False


async def _resolve_stun_address(stun_url: str) -> tuple[str, int]:
    """
    Parse and resolve STUN server URL to (IP, port).

    Args:
        stun_url: STUN URL like 'stun:stun.l.google.com:19302'

    Returns:
        Tuple of (resolved_ip, port)

    Raises:
        ValueError: If URL format is invalid
        OSError: If DNS resolution fails
    """
    # Parse STUN URL
    if stun_url.startswith('stun:'):
        stun_url = stun_url[5:]  # Remove 'stun:' prefix

    if ':' in stun_url:
        hostname, port_str = stun_url.rsplit(':', 1)
        port = int(port_str)
    else:
        hostname = stun_url
        port = 3478  # Default STUN port

    # Async DNS resolution with timeout (Windows-safe)
    loop = asyncio.get_event_loop()
    try:
        addr_info = await asyncio.wait_for(
            loop.getaddrinfo(hostname, port, family=socket.AF_INET,
                           type=socket.SOCK_DGRAM),
            timeout=5.0
        )

        if not addr_info:
            raise OSError(f"DNS resolution returned no results for {hostname}")

        resolved_ip = addr_info[0][4][0]  # Extract IP from ((family, type, proto, canonname, (ip, port)))
        logger.debug("Resolved %s → %s", hostname, resolved_ip)
        return (resolved_ip, port)

    except asyncio.TimeoutError:
        logger.warning("DNS timeout for %s", hostname)
        raise OSError(f"DNS timeout for {hostname}")
    except Exception as e:
        logger.error("DNS failed for %s: %s", hostname, e)
        raise


async def _stun_binding_request(host: str, port: int, timeout: float, transaction_id: bytes) -> Optional[str]:
    """
    Perform a STUN binding request to discover external IP (fully async).

    Args:
        host: STUN server IP address (already resolved)
        port: STUN server port
        timeout: Request timeout in seconds
        transaction_id: 12-byte random transaction ID

    Returns:
        External IP address or None
    """
    sock = None
    try:
        # Create UDP socket (non-blocking)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        # STUN binding request message
        # Format: [Message Type (2 bytes)] [Message Length (2 bytes)] [Magic Cookie (4 bytes)] [Transaction ID (12 bytes)]
        message_type = b'\x00\x01'  # Binding Request
        message_length = b'\x00\x00'  # No attributes
        magic_cookie = b'\x21\x12\xa4\x42'  # RFC 5389 magic cookie

        stun_request = message_type + message_length + magic_cookie + transaction_id

        # Send request (non-blocking)
        loop = asyncio.get_event_loop()
        await loop.sock_sendto(sock, stun_request, (host, port))

        # Receive response with timeout (non-blocking)
        data = await asyncio.wait_for(
            loop.sock_recv(sock, 1024),
            timeout=timeout
        )

        # Parse STUN response
        if len(data) < 20:
            logger.debug("STUN response too short from %s:%d", host, port)
            return None

        # Check if it's a binding response (0x0101)
        if data[0:2] != b'\x01\x01':
            logger.debug("Invalid STUN response type from %s:%d", host, port)
            return None

        # Verify transaction ID matches
        response_transaction_id = data[8:20]
        if response_transaction_id != transaction_id:
            logger.warning("Transaction ID mismatch from %s:%d", host, port)
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

                        # XOR each byte
                        ip_xor = bytes(ip_bytes[i] ^ magic_cookie[i] for i in range(4))
                        external_ip = '.'.join(str(b) for b in ip_xor)
                    else:  # MAPPED-ADDRESS
                        ip_bytes = attr_data[4:8]
                        external_ip = '.'.join(str(b) for b in ip_bytes)

                    return external_ip

            # Move to next attribute (attribute length + padding to 4-byte boundary)
            attr_length_padded = (attr_length + 3) & ~3
            offset += 4 + attr_length_padded

        logger.debug("No IP address attribute found in STUN response from %s:%d", host, port)
        return None

    except asyncio.TimeoutError:
        logger.debug("STUN request timeout for %s:%d", host, port)
        return None
    except Exception as e:
        logger.debug("STUN error for %s:%d: %s", host, port, e)
        return None
    finally:
        if sock:
            sock.close()


async def discover_external_ip(stun_servers: list[str], timeout: float = 3.0, retry_count: int = 3) -> Optional[str]:
    """
    Discover external IP address using STUN servers with network resilience.

    Features:
    - Network connectivity check before attempting STUN
    - Retry logic with exponential backoff (0s, 5s, 15s)
    - Fully async operations (non-blocking sockets)
    - Random transaction IDs for security
    - Detailed error logging

    Args:
        stun_servers: List of STUN server URLs (e.g., ['stun:stun.l.google.com:19302'])
        timeout: Timeout in seconds for each STUN query (default: 3.0)
        retry_count: Number of retry attempts (default: 3)

    Returns:
        External IP address as string, or None if discovery failed
    """
    for attempt in range(retry_count):
        # Check internet connectivity before each attempt
        if not await _check_internet_connectivity():
            if attempt < retry_count - 1:
                delay = 5 * (2 ** attempt)  # Exponential backoff: 5s, 15s
                logger.info("Skipping STUN discovery - no internet connection")
                logger.info("Will retry STUN discovery in %d seconds (attempt %d/%d)",
                          delay, attempt + 2, retry_count)
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning("Skipping STUN discovery - no internet connection (final attempt)")
                return None

        # Try all STUN servers
        for stun_url in stun_servers:
            try:
                # Resolve hostname to IP (async DNS)
                try:
                    resolved_ip, resolved_port = await _resolve_stun_address(stun_url)
                except OSError as e:
                    logger.debug("Failed to resolve %s: %s", stun_url, e)
                    continue

                # Generate random transaction ID for security (12 bytes)
                transaction_id = os.urandom(12)

                # Perform STUN binding request (fully async)
                external_ip = await _stun_binding_request(
                    resolved_ip, resolved_port, timeout, transaction_id
                )

                if external_ip:
                    logger.info("Discovered external IP %s via %s (attempt %d/%d)",
                              external_ip, stun_url, attempt + 1, retry_count)
                    return external_ip

            except Exception as e:
                logger.debug("STUN failed for %s: %s", stun_url, e)
                continue

        # If we get here, all servers failed
        if attempt < retry_count - 1:
            delay = 5 * (2 ** attempt)  # Exponential backoff: 5s, 15s
            logger.warning("STUN discovery failed on attempt %d/%d, retrying in %d seconds",
                         attempt + 1, retry_count, delay)
            await asyncio.sleep(delay)

    logger.warning("Could not discover external IP after %d attempts", retry_count)
    return None


async def start_periodic_stun_discovery(service, interval: int = 300):
    """
    Background task: Periodically re-discover external IP to detect network changes.

    This task runs indefinitely and will:
    - Re-discover external IP every `interval` seconds (default: 5 minutes)
    - Detect IP address changes (e.g., WiFi reconnect, VPN changes)
    - Log IP changes for visibility
    - Update service._last_external_ip tracking field

    Args:
        service: CoreService instance (needs settings and _last_external_ip field)
        interval: Re-discovery interval in seconds (default: 300 = 5 minutes)
    """
    logger.info("Started periodic STUN re-discovery (every %d seconds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)

            logger.debug("Running periodic STUN re-discovery")
            stun_servers = service.settings.get_stun_servers() if service.settings else []

            if not stun_servers:
                logger.warning("No STUN servers configured, skipping periodic discovery")
                continue

            # Perform discovery with single retry (faster for periodic checks)
            external_ip = await discover_external_ip(stun_servers, timeout=3.0, retry_count=1)

            if external_ip:
                # Check if IP changed
                if hasattr(service, '_last_external_ip'):
                    if external_ip != service._last_external_ip:
                        logger.info("External IP changed: %s → %s",
                                  service._last_external_ip or "(none)", external_ip)
                        service._last_external_ip = external_ip
                        # TODO: Trigger WebRTC ICE restart if needed
                else:
                    # First discovery
                    service._last_external_ip = external_ip
                    logger.debug("Periodic discovery: External IP is %s", external_ip)
            else:
                logger.debug("Periodic STUN re-discovery failed (network may be down)")

        except Exception as e:
            logger.error("Error in periodic STUN discovery: %s", e, exc_info=True)


# Self-test (example servers - configure real servers in ~/.dpc/config.ini)
if __name__ == "__main__":
    async def test():
        # Example STUN servers for testing only
        # In production, servers are loaded from ~/.dpc/config.ini [webrtc] stun_servers
        stun_servers = [
            'stun:stun.l.google.com:19302',
            'stun:stun1.l.google.com:19302',
        ]

        print("Testing network-resilient STUN discovery...")
        print("=" * 60)

        # Test connectivity check
        print("\n1. Testing internet connectivity...")
        has_internet = await _check_internet_connectivity()
        print(f"   Result: {'[OK] Connected' if has_internet else '[FAIL] No connection'}")

        # Test STUN discovery
        print("\n2. Testing STUN discovery...")
        external_ip = await discover_external_ip(stun_servers, retry_count=2)

        if external_ip:
            print(f"   [OK] External IP: {external_ip}")
        else:
            print("   [FAIL] Failed to discover external IP")

        print("\n" + "=" * 60)

    # Configure logging for test
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    asyncio.run(test())
