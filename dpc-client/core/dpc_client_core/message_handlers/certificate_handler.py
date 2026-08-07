"""Certificates travel to where the signatures are.

A signature is only worth what the receiver can check, and the certificate that
checks it was reaching nodes by one route: a direct TLS handshake. On the far
side of a star there is no handshake, so every message from the far node was
stored `unverified` — 55 of them on the Linux node on 2026-08-07, and there they
would have stayed. Everything signed in this round — votes, session markers —
would have been unverifiable in exactly the place the signing was introduced for.

Asking a neighbour for it is safe by construction and needs no trust in the
neighbour: `node_id` is the SHA256 of the public key (`crypto.generate_node_id`),
so a certificate whose key does not hash to the id being asked about is refused
by `_persist_peer_certificate`. A relay can withhold a certificate or send
rubbish; it cannot substitute one.

The pair is deliberately dumb — no negotiation, no push, no gossip of its own.
It answers a question the asker already knows how to check.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from . import MessageHandler

DPC_HOME_DIR = Path.home() / ".dpc"


def _read_local_certificate(node_id: str, own_node_id: str) -> Optional[str]:
    """Our own certificate, or a peer's if we happen to hold it."""
    if node_id == own_node_id:
        own = DPC_HOME_DIR / "node.crt"
        if own.exists():
            return own.read_text(encoding="utf-8")
        return None
    cached = DPC_HOME_DIR / "peers" / f"{node_id}.crt"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    return None


class CertificateRequestHandler(MessageHandler):
    """Answers CERT_REQUEST with a certificate, if this node holds one."""

    @property
    def command_name(self) -> str:
        return "CERT_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        node_id = payload.get("node_id")
        if not node_id:
            return None

        cert_pem = _read_local_certificate(node_id, self.service.p2p_manager.node_id)
        if not cert_pem:
            self.logger.debug(
                "No certificate for %s to give %s", str(node_id)[:20], sender_node_id[:20]
            )
            return None

        await self.service.p2p_manager.send_message_to_peer(
            sender_node_id,
            {"command": "CERT_RESPONSE", "payload": {"node_id": node_id, "certificate": cert_pem}},
        )
        self.logger.info(
            "Sent certificate for %s to %s", str(node_id)[:20], sender_node_id[:20]
        )
        return None


class CertificateResponseHandler(MessageHandler):
    """Stores a certificate and re-checks what was parked for want of it."""

    @property
    def command_name(self) -> str:
        return "CERT_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        node_id = payload.get("node_id")
        cert_pem = payload.get("certificate")
        if not (node_id and cert_pem):
            return None

        # Refuses anything whose key does not hash to node_id, which is why it
        # is safe to accept this from a relay we have no reason to trust.
        stored = self.service.p2p_manager._persist_peer_certificate(node_id, cert_pem)
        if not stored:
            self.logger.warning(
                "Refused certificate for %s offered by %s",
                str(node_id)[:20], sender_node_id[:20],
            )
            return None

        self.service.pending_certificate_requests.discard(node_id)
        self.logger.info(
            "Cached certificate for %s (via %s)", str(node_id)[:20], sender_node_id[:20]
        )

        # ADR-036 listed this and it stayed Pending: messages parked as
        # `unverified` were never looked at again, so a missing certificate
        # marked a history permanently rather than temporarily.
        rechecked = 0
        for monitor in list(self.service.conversation_monitors.values()):
            try:
                rechecked += monitor.reverify_author(node_id)
            except Exception as e:  # noqa: BLE001 — one bad monitor must not stop the rest
                self.logger.warning("Re-verification failed for a conversation: %s", e)

        if rechecked:
            self.logger.info(
                "Re-verified %d message(s) from %s now that its certificate is here",
                rechecked, str(node_id)[:20],
            )
            await self.service.local_api.broadcast_event(
                "messages_reverified", {"node_id": node_id, "count": rechecked}
            )
        return None
