"""Two bridges that tag each other stop after one crossing.

The cross-node bound (specs/dptp_v1.md §4.3, Mike's call 2026-10-20, "A,
depth 1") counted only for embedded agents: the `cc_group_mention` event
carried no depth and the bridge posted through `send_group_agent_message`
without one, so a bridge on each of two nodes reset the count to 0 at every
crossing and could answer each other forever (Ark's review, 2026-10-20).

Here node A's bridge posts `@CC_b`, node B wakes its bridge, the bridge
answers with the depth the event gave it, and node A wakes nobody. Both nodes
run the real `send_group_agent_message` with a real key and the real
`GroupTextHandler`; the bridge is the real script, loaded by path; between
them the payload is handed over as the local API hands it, by keyword.
"""

import datetime
import importlib.util
from pathlib import Path
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
from dpc_protocol.message_signing import PREIMAGE_VERSION
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService


GROUP = "group-5a1e"
BRIDGE = Path(__file__).resolve().parents[1] / "cc_group_chat_bridge.py"


@pytest.fixture
def bridge():
    spec = importlib.util.spec_from_file_location("cc_group_chat_bridge_two_nodes", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """Two real keys, both certificates cached where verification looks."""
    peers = tmp_path / "peers"
    peers.mkdir()
    out = []
    for _ in range(2):
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
        (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        out.append((node_id, CommitSigner(node_id, key)))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return out


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Node:
    """One node: a bridge registered as `tag`, the real send and mention code."""

    def __init__(self, key, tag, others):
        node_id, signer = key
        self.tag = tag
        self.p2p_manager = SimpleNamespace(node_id=node_id, peers={})
        self.group_manager = self
        self.local_api = _Api()
        self.broadcast = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self._group_agent_locks = {}
        self.llm_manager = SimpleNamespace(get_context_window=lambda _: 128000,
                                           get_active_model_name=lambda: "m")
        self.agents = {node_id: [f"ext:{tag}"], **{o: [f"ext:{t}"] for o, t in others.items()}}
        monitor = ConversationMonitor(
            conversation_id=GROUP,
            participants=[{"node_id": node_id, "name": "self", "context": "local"}],
            llm_manager=None,
        )
        monitor._signer = signer
        self.conversation_monitors = {GROUP: monitor}

    def get_group(self, group_id):
        return SimpleNamespace(group_id=GROUP, members=list(self.agents), agents=self.agents,
                               agent_names={})

    def _get_or_create_conversation_monitor(self, _):
        return self.conversation_monitors[GROUP]

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []

    def _get_default_agent_id(self):
        return "agent_001"

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"

    _signature_fields_for = staticmethod(CoreService._signature_fields_for)
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as
    _handle_group_agent_mentions = CoreService._handle_group_agent_mentions
    send_group_agent_message = CoreService.send_group_agent_message

    async def local_api_command(self, command):
        """What the local API does with a whitelisted command: payload as keywords."""
        assert command["command"] == "send_group_agent_message"
        return await getattr(self, command["command"])(**command["payload"])

    def mentions_for_my_bridge(self, bridge):
        return [p for p in (bridge._mention_for_me({"event": n, "payload": p}, GROUP, [self.tag.lower()])
                            for n, p in self.local_api.events) if p is not None]


def _handler(node):
    handler = GroupTextHandler(service=node)
    handler._invoke_agent = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_two_bridges_that_tag_each_other_stop_after_one_crossing(keys, bridge):
    a = _Node(keys[0], "CC_a", {keys[1][0]: "CC_b"})
    b = _Node(keys[1], "CC_b", {keys[0][0]: "CC_a"})
    handler_a, handler_b = _handler(a), _handler(b)

    # Node A's bridge opens, asked by a human: no mention behind it.
    await a.local_api_command(bridge._build_send_command(GROUP, "CC_a", "@CC_b can you look?"))
    opening = a.broadcast[-1]["payload"]
    assert opening["preimage_version"] == PREIMAGE_VERSION and "mention_depth" not in opening

    await handler_b.handle(a.p2p_manager.node_id, opening)
    woke_b = b.mentions_for_my_bridge(bridge)
    assert len(woke_b) == 1, "node B did not wake its bridge on a depth-0 agent post"
    assert woke_b[0]["mention_depth"] == 1, "the event did not hand the bridge its reply's depth"

    # Node B's bridge answers, and tags back.
    command = bridge._build_send_command(GROUP, "CC_b", "@CC_a done, over to you",
                                         woke_b[0]["mention_depth"])
    await b.local_api_command(command)
    reply = b.broadcast[-1]["payload"]
    assert reply["mention_depth"] == 1, "the bridge's reply reset the count"
    _, verdict, _ = handler_a._authenticate_author(a.p2p_manager.node_id, reply)
    assert verdict == "verified", verdict

    await handler_a.handle(b.p2p_manager.node_id, reply)
    assert a.mentions_for_my_bridge(bridge) == [], "the reply woke node A's bridge: the chain goes on"
    assert handler_a._invoke_agent.await_count == 0


# --- the event carries the depth on both paths ---------------------------------

@pytest.mark.asyncio
async def test_a_peer_humans_mention_hands_the_bridge_depth_zero(keys, bridge):
    a = _Node(keys[0], "CC_a", {keys[1][0]: "CC_b"})
    b = _Node(keys[1], "CC_b", {keys[0][0]: "CC_a"})
    await _handler(b)._handle_agent_mentions(
        GROUP, {"message_id": "m1", "sender_type": "human"}, "@CC_b hi", "Mike",
        a.p2p_manager.node_id, verification="verified")
    assert [p["mention_depth"] for p in b.mentions_for_my_bridge(bridge)] == [0]


@pytest.mark.asyncio
async def test_a_post_on_the_bridges_own_node_hands_it_the_depth_it_carries(keys, bridge):
    a = _Node(keys[0], "CC_a", {})
    a.agents[a.p2p_manager.node_id].append("ext:Zcode")
    await CoreService._handle_group_agent_mentions(
        a, GROUP, "@Zcode your turn", "CC_a", is_agent_sender=True, mention_depth=1)
    events = [p for n, p in a.local_api.events if n == "cc_group_mention"]
    assert [(p["agent_tag"], p["mention_depth"]) for p in events] == [("zcode", 1)], (
        "a hop inside one node must hand the count on unchanged")


# --- the bridge's send command and its CLI -------------------------------------

def test_the_send_command_carries_the_depth_only_above_zero(bridge):
    assert "mention_depth" not in bridge._build_send_command(GROUP, "CC_a", "x")["payload"]
    assert "mention_depth" not in bridge._build_send_command(GROUP, "CC_a", "x", 0)["payload"]
    assert bridge._build_send_command(GROUP, "CC_a", "x", 2)["payload"]["mention_depth"] == 2


def test_the_cli_takes_mention_depth_and_refuses_a_negative_one(bridge):
    parser = bridge._build_parser()
    # Absent is None, not 0: the send then carries the depth the bridge remembers.
    assert parser.parse_args(["--group", GROUP, "--send", "x"]).mention_depth is None
    assert parser.parse_args(["--group", GROUP, "--send", "x", "--mention-depth", "0"]).mention_depth == 0
    assert parser.parse_args(["--group", GROUP, "--send", "x", "--mention-depth", "1"]).mention_depth == 1
    for bad in ("-1", "one", "1.5", "1_0"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--group", GROUP, "--send", "x", "--mention-depth", bad])
    assert "--mention-depth" in parser.format_help()


def test_the_mention_line_shows_the_depth_to_answer_with(bridge):
    payload = {"group_id": GROUP, "text": "@CC_b look", "sender_name": "CC_a",
               "agent_tag": "cc_b", "mention_depth": 1}
    assert bridge._format_mention(payload, ts="12:00:00") == (
        "[MENTION] 12:00:00 CC_a (@cc_b) [--mention-depth 1] in group-5a1e: @CC_b look")
    assert "--mention-depth" not in bridge._format_mention(dict(payload, mention_depth=0), ts="t")


def test_a_history_line_shows_the_depth_a_reply_to_it_carries(bridge):
    me, peer = "dpc-node-" + "a" * 32, "dpc-node-" + "b" * 32
    line = lambda **m: bridge.format_message(0, {"sender_name": "X", "content": "@CC_a hi",
                                                 "timestamp": "2026-10-20T10:00:00", **m}, me)
    assert line(sender_type="human", sender_node_id=peer) == "  [1] 10:00:00 X: @CC_a hi"
    assert line(sender_type="agent", sender_node_id=peer) == \
        "  [1] 10:00:00 X [--mention-depth 1]: @CC_a hi", "a peer agent's post counts one more"
    assert line(sender_type="agent", sender_node_id=peer, mention_depth=1) == \
        "  [1] 10:00:00 X [--mention-depth 2]: @CC_a hi"
    assert line(sender_type="agent", sender_node_id=me, mention_depth=1) == \
        "  [1] 10:00:00 X [--mention-depth 1]: @CC_a hi", "a post from this node is inherited"
    assert line(sender_type="agent", sender_node_id=me) == "  [1] 10:00:00 X: @CC_a hi"


# --- the local API's door ------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-1, "1", 1.5, True])
async def test_send_group_agent_message_refuses_a_depth_that_is_not_a_whole_number(keys, bad):
    a = _Node(keys[0], "CC_a", {})
    result = await a.local_api_command({"command": "send_group_agent_message", "payload": {
        "group_id": GROUP, "agent_name": "CC_a", "text": "x", "mention_depth": bad}})
    assert isinstance(result, dict) and result["status"] == "error", result
    assert a.broadcast == [], "a refused depth still reached the wire"


# --- the receiving side reads only a whole number ------------------------------

@pytest.mark.parametrize("raw,expected", [(0, 0), (1, 1), (0.5, None), (1.5, None),
                                          (True, None), ("0", None), (-1, None)])
def test_a_signed_depth_that_is_not_a_whole_number_wakes_nobody(raw, expected):
    """int() read a signed 0.5 as 0, and 0 wakes an agent here."""
    from dpc_client_core.message_handlers.group_handler import signed_mention_depth
    payload = {"preimage_version": PREIMAGE_VERSION, "sender_type": "agent", "mention_depth": raw}
    assert signed_mention_depth(payload, "verified") == (True, expected)
