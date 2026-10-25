"""Who may send this node a file, and what happens to a sender nobody named.

Board entry FILE-TRANSFER-DENIED-AFTER-THE-FIREWALL-CHECK-WAS-REVIVED; Mike's
call, 2026-10-25. Two people who connected could not exchange a file, a pasted
screenshot or a voice message until each added the other's node id by hand.
The receiver now decides in this order, the first that names the sender wins:

1. its node entry (allow or deny, with its limits);
2. its first group that has an entry (allow or deny);
3. `file_transfer.proved_connection` when the sender is on a proved connection
   (`p2p_manager.PROVED_CONNECTION_TYPES`, direct TLS). Its default, which also
   stands in when an older rules file lacks the key, is "ask" (Mike's call,
   2026-10-25: a proved key is not an acquaintance); "allow" is the owner's
   opt-in, with its limits;
4. otherwise — and when that entry says ask — the offer, of any kind, waits for
   the user's "allow files from X?"; "always allow" writes a node entry, a
   decline tells the sender why.

The firewall and the transfer manager are real; only the wire and the UI socket
are collected.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from dpc_client_core.firewall import (
    FILE_TRANSFER_PROVED_CONNECTION_DEFAULT,
    FILE_TRANSFER_PROVED_CONNECTION_KEY,
    ContextFirewall,
)
from dpc_client_core.managers.file_transfer_manager import (
    ASKED_OFFER_DECLINED_TEXT,
    FILE_TRANSFER_ALLOW,
    FILE_TRANSFER_ASK,
    FILE_TRANSFER_DENY,
    FileTransferManager,
)
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService

PEER = "dpc-node-" + "b" * 32
SELF = "dpc-node-" + "a" * 32
MB = 1024 * 1024

DIRECT = "direct_tls"
WEBRTC = "webrtc"

NO_ENTRIES = {"file_transfer": {"nodes": {}, "groups": {}}}  # an older file: no proved_connection key
# The owner's opt-in: every directly connected peer may send within these limits
ALLOW_DIRECT = {"file_transfer": {"nodes": {}, "groups": {}, FILE_TRANSFER_PROVED_CONNECTION_KEY: {
    "file_transfer.allow": "allow",
    "file_transfer.max_size_mb": 1000,
    "file_transfer.allowed_mime_types": ["*"],
}}}


_NODES = []


@pytest_asyncio.fixture(autouse=True)
async def _close_every_node():
    """An offer left waiting owns an answer clock; stop it before the loop goes."""
    yield
    while _NODES:
        await _NODES.pop().close()


class _Node:
    """One receiving node: real firewall, real manager, real handler and coordinator."""

    def __init__(self, tmp_path: Path, rules: dict, connection_type=None):
        _NODES.append(self)
        self.rules_path = tmp_path / "privacy_rules.json"
        self.rules_path.write_text(json.dumps(rules), encoding="utf-8")
        self.firewall = ContextFirewall(self.rules_path)
        self.sent = []
        self.events = []

        peers = {}
        if connection_type is not None:
            peers[PEER] = SimpleNamespace(node_id=PEER, connection_type=connection_type)

        async def _send(node_id, message):
            self.sent.append(message)

        async def _broadcast(name, payload):
            self.events.append((name, payload))

        self.p2p = SimpleNamespace(node_id=SELF, peers=peers, send_message_to_peer=_send)
        self.manager = FileTransferManager(
            p2p_manager=self.p2p,
            firewall=self.firewall,
            settings=SimpleNamespace(get=lambda section, key, default=None: default),
            storage_base_path=tmp_path,
        )
        api = SimpleNamespace(broadcast_event=_broadcast)
        self.manager.local_api = api

        async def _notify_peers():
            return None

        service = SimpleNamespace(
            firewall=self.firewall,
            file_transfer_manager=self.manager,
            p2p_manager=self.p2p,
            hub_client=None,
            local_api=api,
            llm_manager=SimpleNamespace(providers={}),
            peer_metadata={PEER: {"name": "Stranger"}},
        )
        # The editor's own save path, bound to this stand-in: a copy would
        # answer for a save nobody ships.
        service._merge_unknown_agent_profile_keys = (
            lambda r: CoreService._merge_unknown_agent_profile_keys(service, r)
        )
        service._notify_peers_of_provider_changes = _notify_peers
        service.save_firewall_rules = lambda r: CoreService.save_firewall_rules(service, r)
        self.service = service
        self.handler = FileOfferHandler(service)
        self.coordinator = P2PCoordinator(service)

    async def close(self):
        """Drop what is left, which stops the stall watch an accept started."""
        self.manager.active_transfers.clear()
        await asyncio.sleep(0)

    def commands(self):
        return [m["command"] for m in self.sent]

    def offered(self):
        return [p for name, p in self.events if name == "file_transfer_offered"]


def _offer(transfer_id="t1", kind="file", size_bytes=1024, group_id=None, filename=None):
    payload = {
        "transfer_id": transfer_id,
        "filename": filename or f"{transfer_id}.bin",
        "size_bytes": size_bytes,
        "hash": "none",
        "mime_type": "application/octet-stream",
        "chunk_size": 65536,
    }
    if kind == "image":
        payload["mime_type"] = "image/png"
        payload["filename"] = filename or f"{transfer_id}.png"
        payload["image_metadata"] = {"dimensions": {"width": 1, "height": 1}, "thumbnail_base64": ""}
    elif kind == "voice":
        payload["mime_type"] = "audio/wav"
        payload["filename"] = filename or f"{transfer_id}.wav"
        payload["voice_metadata"] = {"duration_seconds": 1.0}
    if group_id:
        payload["group_id"] = group_id
    return payload


# --- explicit entries win over a direct connection ---------------------------


@pytest.mark.asyncio
async def test_a_node_deny_beats_a_direct_connection(tmp_path):
    rules = {"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "deny"}}, "groups": {}}}
    node = _Node(tmp_path, rules, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer(kind="image"))

    assert node.commands() == ["FILE_CANCEL"]
    assert node.sent[0]["payload"]["reason"] == "firewall_denied"


@pytest.mark.asyncio
async def test_a_group_deny_beats_a_direct_connection(tmp_path):
    rules = {
        "node_groups": {"strangers": [PEER]},
        "file_transfer": {"nodes": {}, "groups": {"strangers": {"file_transfer.allow": "deny"}}},
    }
    node = _Node(tmp_path, rules, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer(kind="image"))

    assert node.commands() == ["FILE_CANCEL"]
    assert node.sent[0]["payload"]["reason"] == "firewall_denied"


@pytest.mark.asyncio
async def test_the_first_group_with_an_entry_decides_even_when_it_denies(tmp_path):
    rules = {
        "node_groups": {"blocked": [PEER], "friends": [PEER]},
        "file_transfer": {"nodes": {}, "groups": {
            "blocked": {"file_transfer.allow": "deny"},
            "friends": {"file_transfer.allow": "allow"},
        }},
    }
    node = _Node(tmp_path, rules)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")

    assert (decision.outcome, decision.basis) == (FILE_TRANSFER_DENY, "group")


# --- a direct connection allows, within its limits ---------------------------


@pytest.mark.asyncio
async def test_a_direct_peer_is_allowed_within_the_limits_of_an_explicit_allow_entry(tmp_path):
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 999 * MB, "application/zip")

    assert (decision.outcome, decision.basis) == (FILE_TRANSFER_ALLOW, FILE_TRANSFER_PROVED_CONNECTION_KEY)


@pytest.mark.asyncio
async def test_a_direct_peer_over_the_limit_of_an_allow_entry_is_refused_naming_it(tmp_path):
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)
    limit = ALLOW_DIRECT["file_transfer"][FILE_TRANSFER_PROVED_CONNECTION_KEY]["file_transfer.max_size_mb"]

    await node.handler.handle(PEER, _offer(size_bytes=(limit + 1) * MB))

    assert node.commands() == ["FILE_CANCEL"]
    assert f"exceeds {limit} MB limit" in node.sent[0]["payload"]["denial_reason"]


@pytest.mark.asyncio
async def test_an_explicit_allow_entry_lets_a_direct_peer_paste_an_image(tmp_path):
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer(kind="image"))

    assert node.commands() == ["FILE_ACCEPT"]
    assert node.offered() == []
    await node.close()


@pytest.mark.asyncio
async def test_an_allowed_direct_peers_file_still_shows_the_ordinary_dialog_without_the_question(tmp_path):
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer())

    assert node.commands() == []
    [event] = node.offered()
    assert "awaiting_permission" not in event


@pytest.mark.asyncio
async def test_the_owner_can_switch_direct_peers_to_deny(tmp_path):
    rules = {"file_transfer": {"nodes": {}, "groups": {},
                               FILE_TRANSFER_PROVED_CONNECTION_KEY: {"file_transfer.allow": "deny"}}}
    node = _Node(tmp_path, rules, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer(kind="voice"))

    assert node.commands() == ["FILE_CANCEL"]


@pytest.mark.asyncio
async def test_a_connection_that_proves_no_key_is_not_a_direct_one(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES, connection_type=WEBRTC)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")

    assert decision.outcome == FILE_TRANSFER_ASK


# --- a proved key is not an acquaintance: a direct peer is asked by default ---


@pytest.mark.asyncio
async def test_the_default_sends_a_direct_stranger_to_the_question(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES, connection_type=DIRECT)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")

    assert decision.outcome == FILE_TRANSFER_ASK
    assert decision.basis == FILE_TRANSFER_PROVED_CONNECTION_KEY


@pytest.mark.parametrize("kind,group_id", [
    ("file", None), ("image", None), ("voice", None), ("file", "group-x"),
])
@pytest.mark.asyncio
async def test_an_old_rules_file_without_the_key_asks_a_direct_peer_of_any_kind(tmp_path, kind, group_id):
    node = _Node(tmp_path, NO_ENTRIES, connection_type=DIRECT)

    await node.handler.handle(PEER, _offer(kind=kind, group_id=group_id))

    assert node.commands() == []  # neither refused nor auto-accepted
    [event] = node.offered()
    assert event["awaiting_permission"] is True
    assert event["kind"] == kind
    assert node.manager.active_transfers["t1"].awaiting_permission is True
    await node.close()


@pytest.mark.asyncio
async def test_a_node_entry_still_beats_the_ask_of_the_direct_connection_rule(tmp_path):
    rules = {"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "allow"}}, "groups": {}}}
    node = _Node(tmp_path, rules, connection_type=DIRECT)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")

    assert (decision.outcome, decision.basis) == (FILE_TRANSFER_ALLOW, "node")


@pytest.mark.asyncio
async def test_ask_is_a_value_of_the_direct_connection_entry_only(tmp_path):
    """On a node entry anything but "allow" still reads as a refusal."""
    rules = {"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "ask"}}, "groups": {}}}
    node = _Node(tmp_path, rules, connection_type=DIRECT)

    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")

    assert (decision.outcome, decision.basis) == (FILE_TRANSFER_DENY, "node")


@pytest.mark.asyncio
async def test_an_always_allow_on_a_direct_peers_question_writes_its_node_entry(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES, connection_type=DIRECT)
    await node.handler.handle(PEER, _offer("t1"))

    await node.coordinator.accept_file_transfer("t1", remember=True)

    on_disk = json.loads(node.rules_path.read_text(encoding="utf-8"))
    assert on_disk["file_transfer"]["nodes"][PEER]["file_transfer.allow"] == "allow"
    decision = await node.manager._file_transfer_decision(PEER, "x.bin", 10, "text/plain")
    assert (decision.outcome, decision.basis) == (FILE_TRANSFER_ALLOW, "node")
    await node.close()


# --- a stranger is asked, not refused ----------------------------------------


@pytest.mark.parametrize("kind,group_id", [
    ("file", None), ("image", None), ("voice", None), ("file", "group-x"),
])
@pytest.mark.asyncio
async def test_a_strangers_offer_of_any_kind_is_put_to_the_user(tmp_path, kind, group_id):
    node = _Node(tmp_path, NO_ENTRIES, connection_type=None)

    await node.handler.handle(PEER, _offer(kind=kind, group_id=group_id))

    assert node.commands() == []  # neither refused nor auto-accepted
    [event] = node.offered()
    assert event["awaiting_permission"] is True
    assert event["kind"] == kind
    assert event["sender_name"] == "Stranger"
    assert event.get("group_id") == group_id
    assert node.manager.active_transfers["t1"].awaiting_permission is True


@pytest.mark.asyncio
async def test_a_caller_that_cannot_ask_reads_the_question_as_no(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)

    allowed, reason = await node.manager._check_file_transfer_permission(PEER, "x.bin", 10, "text/plain")

    assert allowed is False
    assert reason


@pytest.mark.asyncio
async def test_always_allow_writes_the_node_entry_and_the_next_offer_is_not_asked(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))

    result = await node.coordinator.accept_file_transfer("t1", remember=True)

    assert result["remembered"] is True
    assert node.commands() == ["FILE_ACCEPT"]
    on_disk = json.loads(node.rules_path.read_text(encoding="utf-8"))
    entry = on_disk["file_transfer"]["nodes"][PEER]
    assert entry["file_transfer.allow"] == "allow"
    assert entry["file_transfer.max_size_mb"] == FILE_TRANSFER_PROVED_CONNECTION_DEFAULT["file_transfer.max_size_mb"]
    assert ("firewall_rules_updated" in [name for name, _ in node.events])

    node.events.clear()
    await node.handler.handle(PEER, _offer("t2", kind="image"))

    assert node.commands() == ["FILE_ACCEPT", "FILE_ACCEPT"]
    assert node.offered() == []
    await node.close()


@pytest.mark.asyncio
async def test_always_allow_also_accepts_the_senders_other_waiting_offers(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1", kind="image"))
    await node.handler.handle(PEER, _offer("t2", kind="image"))

    result = await node.coordinator.accept_file_transfer("t2", remember=True)

    accepted = [m["payload"]["transfer_id"] for m in node.sent if m["command"] == "FILE_ACCEPT"]
    assert sorted(accepted) == ["t1", "t2"]
    assert result["also_accepted"] == ["t1"]
    await node.close()


@pytest.mark.asyncio
async def test_accepting_once_writes_nothing(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))

    await node.coordinator.accept_file_transfer("t1")

    on_disk = json.loads(node.rules_path.read_text(encoding="utf-8"))
    assert PEER not in on_disk["file_transfer"]["nodes"]
    await node.close()


@pytest.mark.asyncio
async def test_a_decline_tells_the_sender_why_in_words(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1", kind="voice"))

    await node.coordinator.cancel_file_transfer("t1", "user_rejected")

    assert node.commands() == ["FILE_CANCEL"]
    payload = node.sent[0]["payload"]
    assert payload["reason"] == "user_rejected"
    assert payload["denial_reason"] == ASKED_OFFER_DECLINED_TEXT
    assert "declined" in payload["denial_reason"]


@pytest.mark.asyncio
async def test_rejecting_an_ordinary_offer_adds_no_sentence(tmp_path):
    """The regression half: only an answer to the question carries one."""
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)
    await node.handler.handle(PEER, _offer("t1"))

    await node.coordinator.cancel_file_transfer("t1", "user_rejected")

    assert node.commands() == ["FILE_CANCEL"]
    assert "denial_reason" not in node.sent[0]["payload"]


@pytest.mark.asyncio
async def test_cancelling_after_accepting_is_not_a_decline(tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))
    await node.coordinator.accept_file_transfer("t1")

    await node.coordinator.cancel_file_transfer("t1", "user_cancelled")

    assert node.commands() == ["FILE_ACCEPT", "FILE_CANCEL"]
    assert "denial_reason" not in node.sent[1]["payload"]


# --- the rules file ----------------------------------------------------------


def test_a_new_rules_file_carries_the_direct_connection_entry(tmp_path):
    firewall = ContextFirewall(tmp_path / "privacy_rules.json")

    written = json.loads((tmp_path / "privacy_rules.json").read_text(encoding="utf-8"))

    assert written["file_transfer"][FILE_TRANSFER_PROVED_CONNECTION_KEY] == FILE_TRANSFER_PROVED_CONNECTION_DEFAULT
    assert firewall.rules["file_transfer"][FILE_TRANSFER_PROVED_CONNECTION_KEY]["file_transfer.allow"] == "ask"


@pytest.mark.parametrize("entry", [
    "allow",
    {"file_transfer.allow": "maybe"},
    {"file_transfer.allow": "allow", "file_transfer.max_size_mb": -1},
    {"file_transfer.allow": "allow", "file_transfer.max_size_mb": "big"},
    {"file_transfer.allow": "allow", "file_transfer.allowed_mime_types": "*"},
])
def test_the_validator_refuses_a_malformed_direct_connection_entry(entry):
    ok, errors = ContextFirewall.validate_config({"file_transfer": {FILE_TRANSFER_PROVED_CONNECTION_KEY: entry}})

    assert not ok
    assert any(FILE_TRANSFER_PROVED_CONNECTION_KEY in e for e in errors)


@pytest.mark.parametrize("entry", [
    dict(FILE_TRANSFER_PROVED_CONNECTION_DEFAULT),
    {"file_transfer.allow": "deny"},
    {"file_transfer.allow": "ask"},
    {"file_transfer.allow": "allow"},
    {"file_transfer.allow": "allow", "file_transfer.max_size_mb": None},  # the editor's empty field
])
def test_the_validator_accepts_a_direct_connection_entry(entry):
    ok, errors = ContextFirewall.validate_config({"file_transfer": {FILE_TRANSFER_PROVED_CONNECTION_KEY: entry}})

    assert ok, errors
