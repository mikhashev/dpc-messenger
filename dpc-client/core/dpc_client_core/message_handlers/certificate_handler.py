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

import time
from pathlib import Path
from typing import Any, Callable, Dict, Hashable, Iterator, Optional

from dpc_protocol.paths import dpc_home, resolve_module_home

from . import MessageHandler

_INITIAL_DPC_HOME = dpc_home()
DPC_HOME_DIR = _INITIAL_DPC_HOME

# How long a CERT_REQUEST is waited for before the same node may be asked about
# again: the answer wait of a requested file (file_request.REQUEST_ANSWER_TIMEOUT_S),
# which waits for the same certificate on the file path.
CERT_REQUEST_TIMEOUT_S = 10.0
# How many unanswered asks are remembered at once; a signer id is a peer's to
# choose, so made-up ones must not grow the record without end.
PENDING_CERTIFICATE_REQUESTS_MAX = 1024


class PendingCertificateRequests:
    """The certificates asked for and not yet received, each for a bounded time.

    `CoreService.pending_certificate_requests`, read by the message path
    (`MessageHandler._ask_for_certificate`) as a set: `in`, `add`, `discard`.
    It was a plain set, emptied only by an arriving certificate, so a node
    whose certificate one neighbour withheld was never asked about again,
    and every later message from it stayed `unverified` for good (found in
    review by a fresh subagent, 2026-11-20). An ask now expires after
    `timeout_s`, and the next message that needs the certificate asks again.

    The message path keys an ask by `(signer, neighbour asked)`, so one
    neighbour's withheld answer does not keep another's message from asking
    (found in the second review by a fresh subagent, 2026-11-20). A signer id
    alone reads as "any ask about it pending", and `discard(signer)` drops
    every ask about it: the certificate is here, whoever sent it.
    """

    def __init__(self, timeout_s: float = CERT_REQUEST_TIMEOUT_S,
                 max_entries: int = PENDING_CERTIFICATE_REQUESTS_MAX,
                 clock: Callable[[], float] = time.monotonic):
        self.timeout_s = timeout_s
        self.max_entries = max_entries
        self._clock = clock
        self._asked: Dict[Hashable, float] = {}

    def _expire(self) -> None:
        now = self._clock()
        for node_id in [n for n, when in self._asked.items() if now - when >= self.timeout_s]:
            del self._asked[node_id]

    @staticmethod
    def _signer(key: object) -> object:
        return key[0] if isinstance(key, tuple) and key else key

    def add(self, key: Hashable) -> None:
        self._expire()
        self._asked.pop(key, None)
        self._asked[key] = self._clock()
        while len(self._asked) > self.max_entries:
            del self._asked[next(iter(self._asked))]  # the oldest ask

    def discard(self, key: Hashable) -> None:
        """Forget `key`; a bare signer id forgets every ask about that signer."""
        self._asked.pop(key, None)
        if not isinstance(key, tuple):
            for asked in [k for k in self._asked if self._signer(k) == key]:
                del self._asked[asked]

    def __contains__(self, key: object) -> bool:
        self._expire()
        if key in self._asked:
            return True
        return not isinstance(key, tuple) and any(self._signer(k) == key for k in self._asked)

    def __iter__(self) -> Iterator[Hashable]:
        self._expire()
        return iter(list(self._asked))

    def __len__(self) -> int:
        self._expire()
        return len(self._asked)


def _read_local_certificate(node_id: str, own_node_id: str) -> Optional[str]:
    """Our own certificate, or a peer's if we happen to hold it.

    `node_id` is the asker's to choose and names a file, so anything but a
    node id as `generate_node_id` writes it (`../node`, a path, an upper-case
    copy) is answered as not held, never read.
    """
    from dpc_protocol.crypto import is_valid_node_id
    if not isinstance(node_id, str) or not is_valid_node_id(node_id):
        return None
    home = resolve_module_home(DPC_HOME_DIR, _INITIAL_DPC_HOME)
    if node_id == own_node_id:
        own = home / "node.crt"
        if own.exists():
            return own.read_text(encoding="utf-8")
        return None
    cached = home / "peers" / f"{node_id}.crt"
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
        # A requested file held until its author's description can be checked
        # (ADR-047 phase 1, managers/file_request.py) is accepted now.
        arrived = getattr(getattr(self.service, "file_transfer_manager", None),
                          "certificate_arrived", None)
        if callable(arrived):
            arrived(node_id)

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
