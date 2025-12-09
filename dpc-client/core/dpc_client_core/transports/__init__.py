"""
Transport Layer Abstractions

This package provides transport layer implementations for D-PC Messenger connections.

Available transports:
- DTLSPeerConnection: DTLS-encrypted UDP connection (for hole-punched sockets)
- UDPPeerConnection: PeerConnection-compatible wrapper for DTLS over UDP
- (Future: RelayedConnection, MeshConnection, etc.)
"""

from .dtls_connection import (
    DTLSPeerConnection,
    DTLSHandshakeError,
    DTLSCertificateError,
)
from .udp_peer_connection import UDPPeerConnection

__all__ = [
    "DTLSPeerConnection",
    "DTLSHandshakeError",
    "DTLSCertificateError",
    "UDPPeerConnection",
]
