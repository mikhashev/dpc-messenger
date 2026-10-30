"""A mention that arrives by history sync wakes the mentioned agent once.

Live 2026-10-30: the link to the Linux node was down when CC_linux posted
'@Ark ...' in a group; on reconnect GROUP_HISTORY_RESPONSE merged exactly that
record and nobody routed it, so Ark never woke. The rule (Mike's call,
2026-10-30): a record this merge added, not older than an hour by its own
timestamp, passing what a live GROUP_TEXT would pass, wakes once.

Real `ConversationMonitor` and real `GroupHistoryResponseHandler` on both
sides of the merge; records are signed with a real key whose certificate is
cached where verification looks. Only the agent run itself is a double.
"""

import datetime
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
from dpc_client_core.message_handlers.group_handler import (
    GroupHistoryResponseHandler,
    GroupTextHandler,
)

GROUP = "group-5a1e0c0de001"
ME = "dpc-node-" + "a" * 32
HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupHistoryResponseHandler"


def _key(peers_dir, cache=True):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    if cache:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        (peers_dir / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return node_id, CommitSigner(node_id, key)


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A peer whose certificate is cached, and one whose is not."""
    peers = tmp_path / "peers"
    peers.mkdir()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return SimpleNamespace(peer=_key(peers), stranger=_key(peers, cache=False))


def _record(key, text, *, age=datetime.timedelta(minutes=4), sender_type="agent",
            depth=None, version=PREIMAGE_VERSION, signed=True, name="CC_linux"):
    """A record as a peer's export_history ships it."""
    node_id, signer = key
    record = {
        "id": uuid.uuid4().hex[:16],
        "role": "user",
        "content": text,
        "timestamp": (datetime.datetime.now(datetime.timezone.utc) - age).isoformat(),
        "sender_node_id": node_id,
        "sender_name": name,
    }
    if sender_type:
        record["sender_type"] = sender_type
    if depth is not None:
        record["mention_depth"] = depth
    if signed:
        record["content_hash"] = message_content_hash(
            conversation_id=GROUP, message_id=record["id"], sender_node_id=node_id,
            sender_name=name, sender_type=sender_type, agent_owner=None,
            timestamp=record["timestamp"], content=text, tool_calls=None,
            tool_calls_digest=None, mention_depth=depth, version=version,
        )
        record["signature"] = signer.sign_commit(record["content_hash"])
        record["signer_node_id"] = node_id
        record["preimage_version"] = version
    return record


class _Service:
    """The receiving node: Ark registered here, and an external agent `CC`."""

    def __init__(self, members, agents=("agent_001", "ext:CC")):
        self.p2p_manager = SimpleNamespace(node_id=ME, peers={},
                                           send_message_to_peer=AsyncMock())
        self.group = SimpleNamespace(group_id=GROUP, members=[ME, *members],
                                     agents={ME: list(agents)}, agent_names={})
        self.group_manager = SimpleNamespace(get_group=lambda gid: self.group)
        self.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        self.history_requests = HistoryRequestRegistry()
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self.run = AsyncMock(return_value=None)
        manager = SimpleNamespace(process_message=self.run)
        self.llm_manager = SimpleNamespace(
            providers={"dpc_agent": SimpleNamespace(get_manager=lambda aid: manager)})
        self.monitor = ConversationMonitor(
            conversation_id=GROUP,
            participants=[{"node_id": ME, "name": "self", "context": "local"}],
            llm_manager=None,
        )
        self.conversation_monitors = {GROUP: self.monitor}

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    def _get_default_agent_id(self):
        return "agent_001"

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"

    def _group_chat_context(self, group_id):
        return None

    def cc_wakes(self):
        return [c.args[1]["agent_tag"] for c in self.local_api.broadcast_event.await_args_list
                if c.args[0] == "cc_group_mention"]


async def _sync(service, peer, records):
    """One answered request: the peer's GROUP_HISTORY_RESPONSE with these records."""
    request_id = uuid.uuid4().hex[:8]
    service.history_requests.note(peer, GROUP, request_id)
    await GroupHistoryResponseHandler(service).handle(
        peer, {"group_id": GROUP, "history": records, "request_id": request_id})


@pytest.mark.asyncio
async def test_one_synced_mention_starts_exactly_one_run(keys):
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look at the merge")

    await _sync(service, peer, [record])

    assert service.run.await_count == 1
    assert service.run.await_args.kwargs["trigger_message_id"] == record["id"]


@pytest.mark.asyncio
async def test_a_record_already_in_the_history_wakes_nobody(keys):
    """Routed live or by an earlier merge — after a restart too, nothing stored."""
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look")
    service.monitor.merge_history([dict(record)])

    await _sync(service, peer, [record])

    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_a_second_identical_response_does_not_wake_again(keys):
    """Two answers for one group arrived 26 ms apart on 2026-10-30."""
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look")

    await _sync(service, peer, [record])
    await _sync(service, peer, [dict(record)])

    assert service.run.await_count == 1


@pytest.mark.asyncio
async def test_a_mention_older_than_an_hour_wakes_nobody_and_says_so(keys, caplog):
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look", age=datetime.timedelta(minutes=61))

    await _sync(service, peer, [record])

    assert service.run.await_count == 0
    lines = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and "wakes nobody" in r.getMessage()]
    assert len(lines) == 1, lines
    assert record["id"][:8] in lines[0] and "s old" in lines[0]


@pytest.mark.asyncio
async def test_a_timestamp_ahead_of_our_clock_counts_as_age_zero(keys):
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look", age=-datetime.timedelta(hours=3))

    await _sync(service, peer, [record])

    assert service.run.await_count == 1


@pytest.mark.asyncio
async def test_an_unverified_agent_record_wakes_nobody(keys):
    """Signed, but this node holds no certificate for the signer: as live."""
    stranger = keys.stranger[0]
    service = _Service([stranger])
    record = _record(keys.stranger, "@Ark have a look")

    await _sync(service, stranger, [record])

    assert service.monitor.message_history[-1]["verification"] == "unverified"
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_an_agent_record_over_a_legacy_preimage_wakes_nobody(keys):
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark have a look", version="dptp-msg-v2")

    await _sync(service, peer, [record])

    assert service.monitor.message_history[-1]["verification"] == "verified"
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_an_unsigned_agent_record_wakes_nobody(keys):
    peer = keys.peer[0]
    service = _Service([peer])

    await _sync(service, peer, [_record(keys.peer, "@Ark have a look", signed=False)])

    assert service.monitor.message_history[-1]["verification"] == "legacy"
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_an_agent_record_at_the_depth_limit_wakes_nobody(keys):
    peer = keys.peer[0]
    service = _Service([peer])

    await _sync(service, peer, [_record(keys.peer, "@Ark have a look", depth=1)])

    assert service.run.await_count == 0
    assert service.cc_wakes() == []


@pytest.mark.asyncio
async def test_a_synced_cc_mention_reaches_the_bridge_as_live(keys):
    """The external-agent wake is the live path's own: tag and reply depth."""
    peer = keys.peer[0]
    service = _Service([peer])

    await _sync(service, peer, [_record(keys.peer, "@CC pull and rerun")])

    assert service.cc_wakes() == ["cc"]
    event = next(c.args[1] for c in service.local_api.broadcast_event.await_args_list
                 if c.args[0] == "cc_group_mention")
    assert event["mention_depth"] == 1
    assert event["sender_node_id"] == peer
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_records_are_woken_in_timestamp_order(keys):
    peer = keys.peer[0]
    service = _Service([peer])
    later = _record(keys.peer, "@Ark second", age=datetime.timedelta(minutes=2))
    earlier = _record(keys.peer, "@Ark first", age=datetime.timedelta(minutes=9))

    await _sync(service, peer, [later, earlier])

    assert [c.kwargs["trigger_message_id"] for c in service.run.await_args_list] == [
        earlier["id"], later["id"]]


@pytest.mark.asyncio
async def test_the_live_copy_and_the_synced_copy_wake_once_between_them(keys):
    """Whichever of the two routes stores the record first, one run."""
    peer = keys.peer[0]
    for live_first in (True, False):
        service = _Service([peer])
        record = _record(keys.peer, "@Ark have a look", sender_type="human", name="Mike")
        live = dict(record, message_id=record["id"], text=record["content"], group_id=GROUP)

        if live_first:
            await GroupTextHandler(service).handle(peer, live)
            await _sync(service, peer, [record])
        else:
            await _sync(service, peer, [record])
            await GroupTextHandler(service).handle(peer, live)

        assert service.run.await_count == 1, f"live first: {live_first}"


@pytest.mark.asyncio
async def test_a_known_mention_beside_a_new_record_wakes_nobody(keys):
    """Only what this merge added is read, not everything the answer carried."""
    peer = keys.peer[0]
    service = _Service([peer])
    known = _record(keys.peer, "@Ark have a look")
    service.monitor.merge_history([dict(known)])

    await _sync(service, peer, [known, _record(keys.peer, "no mention here")])

    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_a_sync_landing_while_the_live_copy_is_relayed_wakes_once(keys):
    """The live handler awaits its relay between the dedup check and the store."""
    peer, third = keys.peer[0], "dpc-node-" + "c" * 32
    service = _Service([peer, third])
    service.p2p_manager.peers = {third: object()}
    record = _record(keys.peer, "@Ark have a look", sender_type="human", name="Mike")
    live = dict(record, message_id=record["id"], text=record["content"], group_id=GROUP)

    async def relay_then_sync(*_):
        service.p2p_manager.send_message_to_peer.side_effect = None
        await _sync(service, peer, [dict(record)])
    service.p2p_manager.send_message_to_peer.side_effect = relay_then_sync

    await GroupTextHandler(service).handle(peer, live)

    assert service.run.await_count == 1
