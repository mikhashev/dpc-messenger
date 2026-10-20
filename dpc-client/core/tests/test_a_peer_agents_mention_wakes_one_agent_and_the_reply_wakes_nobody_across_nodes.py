"""A peer agent's mention wakes one agent here, and the reply wakes nobody across nodes.

Until 2026-10-20 the peer path returned on `is_agent` before reading a single
mention, so an agent on another node could never reach an agent on this one —
live that day, CC_win10 on dpc-node-eddf0e58 wrote "@Ark" and Ark did not wake.
Mike's call the same day, "A, depth 1": follow one cross-node agent mention and
no more (specs/dptp_v1.md §4.3). The count rides as `mention_depth`, inside the
v3 preimage, and the guard reads the signed `sender_type` rather than the
unsigned `is_agent`, which a relay could flip (board:
THE-CROSS-NODE-ANTI-LOOP-GUARD-TRUSTS-AN-UNSIGNED-FLAG).

The sender is the real `send_group_agent_message` writing into a real
`ConversationMonitor` with a real key; the receiver is the real
`GroupTextHandler`. Collaborators are stubs, the code under test is not.
"""

import datetime
import logging
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
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService


GROUP = "group-1234"
RECEIVER = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupTextHandler"


@pytest.fixture
def author(tmp_path, monkeypatch):
    """A real key, a real certificate, cached where verification looks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
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
    peers = tmp_path / "peers"
    peers.mkdir()
    (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return node_id, CommitSigner(node_id, key)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _monitor(node_id, signer=None):
    monitor = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": node_id, "name": "self", "context": "local"}],
        llm_manager=None,
    )
    if signer is not None:
        monitor._signer = signer
    return monitor


class _Sender:
    """The agent's own node: the real send method, stubbed collaborators."""

    def __init__(self, node_id, signer):
        self.p2p_manager = SimpleNamespace(node_id=node_id, peers={})
        self.group_manager = self
        self.local_api = _Api()
        self.broadcast = []
        self.local_mentions = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self.monitor = _monitor(node_id, signer)
        self.llm_manager = SimpleNamespace(get_context_window=lambda _: 128000,
                                           get_active_model_name=lambda: "m")

    def get_group(self, group_id):
        return SimpleNamespace(group_id=GROUP, members=[self.p2p_manager.node_id],
                               agents={self.p2p_manager.node_id: ["agent_001"]},
                               agent_names={self.p2p_manager.node_id: {"agent_001": "Johnny"}})

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    async def _handle_group_agent_mentions(self, *args, **kwargs):
        self.local_mentions.append(kwargs)

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []

    _signature_fields_for = staticmethod(CoreService._signature_fields_for)
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Johnny"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"


class _Receiver:
    """The other node: Ark is its default agent and CC_mike its registered bridge."""

    def __init__(self, author_node_id):
        self.p2p_manager = SimpleNamespace(node_id=RECEIVER, peers={})
        self.group_manager = self
        self.local_api = _Api()
        self.conversation_monitors = {}
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self.members = [RECEIVER, author_node_id]

    def get_group(self, group_id):
        return SimpleNamespace(group_id=GROUP, members=self.members,
                               agents={RECEIVER: ["agent_001", "ext:CC_mike"]})

    def _get_or_create_conversation_monitor(self, conversation_id):
        if conversation_id not in self.conversation_monitors:
            self.conversation_monitors[conversation_id] = _monitor(RECEIVER)
        return self.conversation_monitors[conversation_id]

    def _get_default_agent_id(self):
        return "agent_001"

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"


async def _agent_post(author, text, mention_depth=0):
    node_id, signer = author
    sender = _Sender(node_id, signer)
    await CoreService.send_group_agent_message(sender, GROUP, "Johnny", text,
                                               mention_depth=mention_depth)
    record = sender.monitor.get_message_history()[-1]
    payload = sender.broadcast[-1]["payload"]
    return sender, record, payload


def _receiver(author):
    receiver = _Receiver(author[0])
    handler = GroupTextHandler(service=receiver)
    handler._invoke_agent = AsyncMock()
    return receiver, handler


def _bridge_wakes(receiver):
    return [p["agent_tag"] for name, p in receiver.local_api.events if name == "cc_group_mention"]


def _signed(author, text, sender_type="agent", mention_depth=None,
            version=PREIMAGE_VERSION, sender_name="Johnny"):
    """A post signed by hand, for the versions and fields the sender no longer writes."""
    node_id, signer = author
    payload = {
        "group_id": GROUP, "text": text, "sender_name": sender_name,
        "sender_type": sender_type, "sender_node_id": node_id,
        "agent_owner": node_id if sender_type == "agent" else None,
        "message_id": "m-%s-%s" % (version[-2:], mention_depth),
        "timestamp": "2026-10-20T10:00:00+00:00", "mentions": [],
    }
    if sender_type == "agent":
        payload["is_agent"] = True
    if mention_depth is not None:
        payload["mention_depth"] = mention_depth
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"],
        sender_node_id=node_id, sender_name=sender_name, sender_type=sender_type,
        agent_owner=payload["agent_owner"], timestamp=payload["timestamp"],
        content=text, tool_calls=None, mention_depth=mention_depth, version=version,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = node_id
    payload["preimage_version"] = version
    return payload


# --- the chain: one cross-node agent mention is followed, the next is not ---

@pytest.mark.asyncio
async def test_a_peer_agents_post_at_depth_zero_wakes_the_named_agent_one_level_deeper(author):
    _, _, payload = await _agent_post(author, "@Ark can you check the build?")
    assert payload["preimage_version"] == PREIMAGE_VERSION
    assert "mention_depth" not in payload, "depth 0 is carried as absence"
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 1, "the named agent was not woken"
    assert handler._invoke_agent.await_args.kwargs["mention_depth"] == 1, (
        "the woken agent would answer at the depth it was woken at")


@pytest.mark.asyncio
async def test_the_woken_agents_reply_carries_depth_one_signed_and_wakes_nobody_across_nodes(author):
    sender, record, payload = await _agent_post(author, "@Ark @CC_mike over to you", mention_depth=1)

    assert payload["mention_depth"] == 1 and record["mention_depth"] == 1
    assert sender.local_mentions[-1]["mention_depth"] == 1, (
        "a local agent woken by this post would reset the count")
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0, "the reply woke an agent across nodes"
    assert _bridge_wakes(receiver) == [], "the reply woke a bridge across nodes"
    # The receiver's copy keeps the signed depth and re-exports verifiably.
    monitor = receiver.conversation_monitors[GROUP]
    stored = monitor.get_message_history()[-1]
    assert stored["mention_depth"] == 1
    _, verdict = monitor._verify_incoming(monitor.export_history()[-1])
    assert verdict == "verified", verdict


@pytest.mark.asyncio
async def test_the_peer_path_hands_the_depth_to_the_reply_it_posts():
    manager = SimpleNamespace(process_message=AsyncMock(return_value="done"))
    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": SimpleNamespace(
            get_manager=lambda aid: manager)}),
        _get_default_agent_id=lambda: "agent_001",
        _get_agent_display_name=lambda aid: "Ark",
        send_group_agent_message=AsyncMock(),
    )

    await GroupTextHandler(service)._invoke_agent(GROUP, "@Ark hi", "Johnny", "m1", mention_depth=1)

    assert service.send_group_agent_message.await_args.kwargs.get("mention_depth") == 1


@pytest.mark.asyncio
async def test_the_send_path_hands_the_depth_to_a_local_agent_it_wakes():
    """A hop inside one node does not count, so the local agent inherits the depth."""
    invoked = []

    async def _invoke(*args, **kwargs):
        invoked.append(kwargs.get("mention_depth"))

    svc = SimpleNamespace(
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(agents={"node-me": ["agent_002"]})),
        p2p_manager=SimpleNamespace(node_id="node-me"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        _get_agent_display_name=lambda aid: {"agent_002": "Muse"}.get(aid, aid),
        get_cc_display_name=lambda: "CC",
        _invoke_agent_in_group_serialized=_invoke,
    )
    await CoreService._handle_group_agent_mentions(
        svc, GROUP, "@Muse take it", "Johnny", is_agent_sender=True, mention_depth=1)
    import asyncio
    await asyncio.sleep(0)

    assert invoked == [1]


# --- what a peer agent's post may not do -------------------------------------

@pytest.mark.asyncio
async def test_at_all_from_a_peer_agent_wakes_nobody(author):
    _, _, payload = await _agent_post(author, "@all status?")
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0
    assert _bridge_wakes(receiver) == []


@pytest.mark.asyncio
async def test_a_peer_agent_does_not_wake_the_agent_that_bears_its_own_name(author):
    payload = _signed(author, "@Ark noted", sender_name="Ark")
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0, "an agent was woken by its own name"


@pytest.mark.asyncio
async def test_a_mention_inside_code_from_a_peer_wakes_nobody(author):
    """The send path strips code before parsing; the peer path parsed raw text."""
    text = "the bridge answers to `@CC_mike`, and\n```\n@Ark run\n```"
    payload = _signed(author, text, sender_type="human", sender_name="Mike")
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0
    assert _bridge_wakes(receiver) == []


# --- the guard reads only what the author signed ------------------------------

@pytest.mark.asyncio
async def test_a_lowered_depth_or_a_flipped_sender_type_breaks_the_signature(author):
    _, _, payload = await _agent_post(author, "@Ark again", mention_depth=1)
    handler = GroupTextHandler(service=None)
    assert handler._authenticate_author(RECEIVER, payload)[1] == "verified"
    lowered = dict(payload, mention_depth=0)
    dropped = {k: v for k, v in payload.items() if k != "mention_depth"}
    as_human = dict(payload, sender_type="human")

    for forged in (lowered, dropped, as_human):
        _, verdict, _ = handler._authenticate_author(RECEIVER, forged)
        assert verdict == "rejected", verdict


@pytest.mark.asyncio
async def test_a_flipped_is_agent_on_a_signed_v3_post_changes_nothing(author):
    """Not in the preimage, so not read: the signed sender_type decides."""
    _, _, agent_post = await _agent_post(author, "@Ark again", mention_depth=1)
    receiver, handler = _receiver(author)
    await handler.handle(author[0], dict(agent_post, is_agent=False))
    assert handler._invoke_agent.await_count == 0, "is_agent=False lifted the bound"

    human_post = _signed(author, "@Ark please look", sender_type="human", sender_name="Mike")
    receiver, handler = _receiver(author)
    await handler.handle(author[0], dict(human_post, is_agent=True))
    assert handler._invoke_agent.await_count == 1, "is_agent=True silenced a human"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_agent", [True, False])
async def test_an_agent_post_from_an_older_peer_wakes_nobody(author, is_agent):
    """Signed over v2, nothing binds a depth, so the rule from before holds."""
    payload = _signed(author, "@Ark @CC_mike ping", version="dptp-msg-v2")
    payload["is_agent"] = is_agent
    assert GroupTextHandler(service=None)._authenticate_author(RECEIVER, payload)[1] == "verified"
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0
    assert _bridge_wakes(receiver) == []


@pytest.mark.asyncio
async def test_an_unverified_agent_post_wakes_nobody(author, monkeypatch, tmp_path):
    """No certificate yet: the depth is the claim of whoever handed it over."""
    _, _, payload = await _agent_post(author, "@Ark can you check?")
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path / "no-certificates")
    receiver, handler = _receiver(author)

    await handler.handle(author[0], payload)

    assert handler._invoke_agent.await_count == 0


# --- what the log says --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_cross_node_wake_is_one_info_line_and_a_stop_is_a_debug_line(author, caplog):
    caplog.set_level(logging.DEBUG, logger=HANDLER_LOGGER)
    _, _, woke = await _agent_post(author, "@Ark can you check?")
    _, _, stopped = await _agent_post(author, "@Ark back to you", mention_depth=1)
    receiver, handler = _receiver(author)

    await handler.handle(author[0], woke)
    await handler.handle(author[0], stopped)

    info = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "mention_depth" in r.getMessage()]
    assert len(info) == 1, info
    assert "@ark" in info[0] and "Johnny" in info[0] and "mention_depth 0" in info[0]
    debug = [r.getMessage() for r in caplog.records
             if r.levelno == logging.DEBUG and "wakes nobody" in r.getMessage()]
    assert len(debug) == 1, debug
    assert "mention_depth 1" in debug[0] and stopped["message_id"][:8] in debug[0]
