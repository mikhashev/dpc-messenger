# dpc-client/core/dpc_client_core/connection_status.py

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable
from datetime import datetime


class OperationMode(Enum):
    """Operating modes based on connectivity."""
    FULLY_ONLINE = "fully_online"  # Hub + WebRTC + Direct TLS
    HUB_OFFLINE = "hub_offline"     # Direct TLS only
    FULLY_OFFLINE = "fully_offline"  # Local operation only


@dataclass
class ConnectionStatus:
    """
    Tracks the connectivity status of all DPC components.

    This allows the application to adapt its behavior based on
    what connections are available.
    """

    # Hub connectivity
    hub_connected: bool = False
    hub_last_connected: Optional[str] = None  # ISO timestamp
    hub_connection_error: Optional[str] = None

    # WebRTC availability (requires Hub)
    webrtc_available: bool = False
    webrtc_enabled: bool = True  # User preference

    # Direct TLS availability (always available if P2P server running)
    direct_tls_available: bool = False
    direct_tls_port: int = 8888

    # Authentication status
    authenticated: bool = False
    auth_method: Optional[str] = None  # "oauth", "cached"
    node_id: Optional[str] = None

    # Callbacks for status changes
    _on_status_change: Optional[Callable] = None

    def get_operation_mode(self) -> OperationMode:
        """
        Determine current operation mode based on connectivity.

        Returns:
            Current OperationMode
        """
        if self.hub_connected and self.webrtc_available and self.direct_tls_available:
            return OperationMode.FULLY_ONLINE

        if self.direct_tls_available:
            return OperationMode.HUB_OFFLINE

        return OperationMode.FULLY_OFFLINE

    def update_hub_status(self, connected: bool, error: Optional[str] = None):
        """Update Hub connection status."""
        old_mode = self.get_operation_mode()

        self.hub_connected = connected
        if connected:
            self.hub_last_connected = datetime.utcnow().isoformat()
            self.hub_connection_error = None
        else:
            self.hub_connection_error = error
            self.webrtc_available = False  # WebRTC requires Hub

        self._notify_if_changed(old_mode)

    def update_webrtc_status(self, available: bool):
        """Update WebRTC availability status."""
        old_mode = self.get_operation_mode()

        self.webrtc_available = available and self.hub_connected

        self._notify_if_changed(old_mode)

    def update_direct_tls_status(self, available: bool, port: int = 8888):
        """Update Direct TLS availability status."""
        old_mode = self.get_operation_mode()

        self.direct_tls_available = available
        self.direct_tls_port = port

        self._notify_if_changed(old_mode)

    def update_auth_status(
        self,
        authenticated: bool,
        method: Optional[str] = None,
        node_id: Optional[str] = None
    ):
        """Update authentication status."""
        self.authenticated = authenticated
        self.auth_method = method
        self.node_id = node_id

    def set_on_status_change(self, callback: Callable):
        """Set callback for status changes."""
        self._on_status_change = callback

    def _notify_if_changed(self, old_mode: OperationMode):
        """Notify if operation mode changed."""
        new_mode = self.get_operation_mode()
        if old_mode != new_mode and self._on_status_change:
            self._on_status_change(old_mode, new_mode)

    def get_available_features(self) -> dict:
        """Get dictionary of available features."""
        return {
            "hub_discovery": self.hub_connected,
            "webrtc_connections": self.webrtc_available,
            "direct_tls_connections": self.direct_tls_available,
            "peer_discovery": self.hub_connected,
            "authentication": self.authenticated
        }

    def get_status_message(self) -> str:
        """Get human-readable status message."""
        mode = self.get_operation_mode()

        if mode == OperationMode.FULLY_ONLINE:
            return "Online - All features available"
        elif mode == OperationMode.HUB_OFFLINE:
            return "Hub Offline - Direct TLS connections only"
        else:
            return "Offline - Local operation only"

    def get_detailed_status(self) -> dict:
        """Get detailed status information for debugging."""
        return {
            "mode": self.get_operation_mode().value,
            "hub_connected": self.hub_connected,
            "hub_last_connected": self.hub_last_connected,
            "hub_error": self.hub_connection_error,
            "webrtc_available": self.webrtc_available,
            "direct_tls_available": self.direct_tls_available,
            "direct_tls_port": self.direct_tls_port,
            "authenticated": self.authenticated,
            "auth_method": self.auth_method,
            "node_id": self.node_id,
            "features": self.get_available_features()
        }

    def can_connect_to_peer(self, peer_supports_webrtc: bool, peer_on_lan: bool) -> tuple[bool, str]:
        """
        Check if we can connect to a peer.

        Args:
            peer_supports_webrtc: Whether peer supports WebRTC
            peer_on_lan: Whether peer is on local network

        Returns:
            Tuple of (can_connect, method)
        """
        # Try Direct TLS first if on LAN
        if peer_on_lan and self.direct_tls_available:
            return (True, "direct_tls")

        # Try WebRTC if available
        if peer_supports_webrtc and self.webrtc_available:
            return (True, "webrtc")

        # Try Direct TLS as fallback
        if self.direct_tls_available:
            return (True, "direct_tls")

        return (False, "none")


# Self-test
if __name__ == "__main__":
    print("Testing ConnectionStatus...")

    status = ConnectionStatus()

    # Test initial state
    assert status.get_operation_mode() == OperationMode.FULLY_OFFLINE
    print("[PASS] Initial state")

    # Test Direct TLS only
    status.update_direct_tls_status(True)
    assert status.get_operation_mode() == OperationMode.HUB_OFFLINE
    print("[PASS] Direct TLS mode")

    # Test fully online
    status.update_hub_status(True)
    status.update_webrtc_status(True)
    assert status.get_operation_mode() == OperationMode.FULLY_ONLINE
    print("[PASS] Fully online mode")

    # Test Hub offline
    status.update_hub_status(False, error="Connection timeout")
    assert status.get_operation_mode() == OperationMode.HUB_OFFLINE
    assert status.webrtc_available == False  # WebRTC requires Hub
    print("[PASS] Hub offline mode")

    # Test status message
    msg = status.get_status_message()
    assert "Hub Offline" in msg
    print("[PASS] Status message")

    # Test available features
    features = status.get_available_features()
    assert features["direct_tls_connections"] == True
    assert features["webrtc_connections"] == False
    print("[PASS] Available features")

    # Test connection capability
    can_connect, method = status.can_connect_to_peer(
        peer_supports_webrtc=True,
        peer_on_lan=True
    )
    assert can_connect == True
    assert method == "direct_tls"
    print("[PASS] Connection capability")

    # Test callback
    # Use a mutable container to avoid global/nonlocal issues
    callback_state = {"called": False, "old_mode": None, "new_mode": None}

    def test_callback(old, new):
        callback_state["called"] = True
        callback_state["old_mode"] = old
        callback_state["new_mode"] = new

    status.set_on_status_change(test_callback)
    status.update_hub_status(True)  # Reconnect Hub
    status.update_webrtc_status(True)  # Enable WebRTC (should trigger callback)
    assert callback_state["called"] == True
    assert callback_state["old_mode"] == OperationMode.HUB_OFFLINE
    assert callback_state["new_mode"] == OperationMode.FULLY_ONLINE
    print("[PASS] Status change callback")

    print("\n[PASS] All ConnectionStatus tests passed!")
