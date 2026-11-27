"""
NetworkService — the network, hub and gateway keys of config.ini.

Follows the TelegramDiscordService pattern (docs/decisions/001-service-split.md):
the readers are owned by P2PManager, the connection-strategy managers, the
relay/gossip/hole-punch/file-transfer managers, HubClient, GatewayServer and
webrtc_peer.py, and the config-parity audit
(audit/config_ui_parity_2026-11-27.md) found no UI writer for any of these keys
(card THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI).

Responsibilities:
- get/save of the live keys (p2p.connection_timeout, the per-use [connection]
  timeouts, hole_punch.dtls_handshake_timeout, p2p.auto_connect_node_groups,
  the six [turn] keys, webrtc.stun_servers)
- get/save of the start-only keys ([dht], [relay], [gossip], the boot-time
  [connection] keys, [hole_punch] udp_punch_port, the remaining [p2p] keys,
  [hub], [oauth], [gateway] enabled/port, [file_transfer])

NOT in scope (stays where it is):
- the readers themselves (p2p_manager.py, service.py, the managers, hub_client.py,
  gateway.py, webrtc_peer.py) and get_status / get_gateway_state
- p2p.listen_port: shown read-only (it is bound when the backend starts and is
  set in config.ini before that — the owner's decision), with no writer here
- gateway.host: the gateway refuses any value but 127.0.0.1 (ADR-041 D1), so it
  is not a setting a person can meaningfully change
- [api] host/port (the API-port card owns them)
- the dead keys of these sections (the dead-key card owns them)

A stored TURN credential is never sent back as text: the getter answers
`credential_set` / `fallback_credential_set` only, the save reply says only that
it saved, and the broadcast carries the stored shape, never a value.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# What each section of the form may write. p2p.listen_port is knowingly absent
# (read-only display, no writer); gateway.host is knowingly absent (ADR-041 D1);
# the dead keys of these sections (removed from the template by 1cc35220) are
# knowingly absent; [api] belongs to its own card. The two [file_transfer]
# thresholds are knowingly absent too: FileTransferManager reads them into
# attributes nothing then reads, so a control for them would turn nothing.
_P2P_KEYS = frozenset({
    "listen_host", "connection_timeout", "auto_connect_node_groups",
    "auto_connect_delay",
})
_DHT_KEYS = frozenset({
    "enabled", "port", "k", "alpha", "bootstrap_timeout", "lookup_timeout",
    "bucket_refresh_interval", "announce_interval", "seed_nodes",
})
_RELAY_KEYS = frozenset({"volunteer", "max_peers", "bandwidth_limit_mbps"})
_GOSSIP_KEYS = frozenset({"max_hops", "fanout", "ttl_seconds", "sync_interval"})
_CONNECTION_KEYS = frozenset({
    "remote_inference_timeout", "reconnect_slow_interval_max_seconds",
    "hole_punch_timeout", "hello_timeout", "link_silence_timeout",
    "max_pending_hellos_per_ip", "max_proved_connections_per_ip",
    "enable_hole_punching",
})
_HOLE_PUNCH_KEYS = frozenset({"udp_punch_port", "dtls_handshake_timeout"})
_TURN_KEYS = frozenset({
    "username", "credential", "clear_credential",
    "servers", "fallback_servers", "fallback_username",
    "fallback_credential", "clear_fallback_credential",
})
_WEBRTC_KEYS = frozenset({"stun_servers"})
_HUB_KEYS = frozenset({"url", "auto_connect"})
_OAUTH_KEYS = frozenset({"callback_host", "callback_port", "default_provider"})
_GATEWAY_KEYS = frozenset({"enabled", "port"})
_FILE_TRANSFER_KEYS = frozenset({
    "chunk_size", "chunk_delay", "max_concurrent_transfers", "verify_hash",
})

_SECTION_KEYS = {
    "p2p": _P2P_KEYS,
    "dht": _DHT_KEYS,
    "relay": _RELAY_KEYS,
    "gossip": _GOSSIP_KEYS,
    "connection": _CONNECTION_KEYS,
    "hole_punch": _HOLE_PUNCH_KEYS,
    "turn": _TURN_KEYS,
    "webrtc": _WEBRTC_KEYS,
    "hub": _HUB_KEYS,
    "oauth": _OAUTH_KEYS,
    "gateway": _GATEWAY_KEYS,
    "file_transfer": _FILE_TRANSFER_KEYS,
}


class NetworkService:
    """
    The network, hub and gateway keys of config.ini the form covers.

    Dependencies injected at construction time (no EventBus):
    - settings: Settings — config read/write
    - local_api: LocalApiServer — UI event broadcasting
    """

    def __init__(self, settings: Any, local_api: Any):
        self.settings = settings
        self.local_api = local_api

    async def get_network_config(self) -> Dict[str, Any]:
        """
        Get the network form's keys.

        UI Integration: Called when user opens the Network and Gateway form.
        A stored TURN credential is answered as `credential_set` /
        `fallback_credential_set` and nothing else — never its value, prefix
        or length; see the settings docstring for when a written value is in
        force (per key, and said so in the form).

        Returns:
            Dict with the network form settings
        """
        try:
            config = self.settings.get_network_settings()
            return {"status": "success", **config}
        except Exception as e:
            logger.error("Error getting network config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_network_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save the network form's keys.

        UI Integration: Called when user saves any section of the Network and
        Gateway form. The config dict holds one object per config.ini section
        this form covers; a key left out keeps its stored value, a TURN
        credential left out or empty keeps the stored one (only
        `clear_credential` / `clear_fallback_credential` removes it), and an
        invalid value is refused with its reason and nothing is written. The
        reply and the broadcast never carry a credential value, only the
        `credential_set` booleans of the stored shape — a credential travels
        exactly once, in the save command itself.

        Args:
            config: Network configuration dict

        Returns:
            Dict with status and message
        """
        if not isinstance(config, dict):
            return {
                "status": "error",
                "message": "Network config must be an object of its config.ini sections."
            }
        try:
            unknown = set(config) - set(_SECTION_KEYS) - {"status"}
            if unknown:
                return {
                    "status": "error",
                    "message": f"Unknown network section(s): {', '.join(sorted(unknown))}"
                }
            for name, allowed in _SECTION_KEYS.items():
                section = config.get(name)
                if section is None:
                    continue
                if not isinstance(section, dict):
                    return {
                        "status": "error",
                        "message": f"{name} must be an object of its config.ini keys."
                    }
                unknown_keys = set(section) - allowed
                if unknown_keys:
                    return {
                        "status": "error",
                        "message": f"Unknown {name} key(s): {', '.join(sorted(unknown_keys))}"
                    }
            self.settings.set_network_settings(**{
                name: config[name] for name in _SECTION_KEYS if name in config})
            stored = self.settings.get_network_settings()
            await self.local_api.broadcast_event("network_config_updated", stored)
            return {
                "status": "success",
                "message": "Network settings saved successfully"
            }
        except ValueError as e:
            return {
                "status": "error",
                "message": str(e)
            }
        except Exception as e:
            logger.error("Error saving network config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }
