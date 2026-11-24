"""A node off a group's roster can neither post into it nor have its post stored.

Card A-NODE-REMOVED-FROM-A-GROUP-CAN-STILL-POST-INTO-IT-AND-THE-OWNER-STORES-THE-POST
(2026-11-23): the Linux node, removed from "AI Studio" at roster v14 (members =
the Windows creator only), posted an agent message through its bridge about four
hours after it had applied v14 and been told `not_a_member`; the creator stored
it in its current session. Three doors were open: the receiver checked the
signature and that the group is held, never the author against `members`; the
sender never checked that its own node is on `members`; and `agents[node]`,
which a removal keeps on purpose, still let the bridge pick a name to post as.

The falsifier is the incident replayed at the creator with a real signature:
refused with the existing `not_a_member` word, nothing stored, shown or relayed.
"""
import datetime
import logging
import runpy
import sys
import types
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
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.message_handlers.group_handler import (
    GroupAccessDeniedHandler,
    GroupTextHandler,
)
from dpc_client_core.service import CoreService
from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    BRIDGE, GROUP as BRIDGE_GROUP, bridge,
)

CREATOR = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
MEMBER = "dpc-node-" + "b" * 32
TIMESTAMP = "2026-11-23T07:39:36+00:00"


def _node_key():
    """A real key and certificate: the author is read from the signature."""
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
    return node_id, key, cert


@pytest.fixture
def signed(tmp_path, monkeypatch):
    """Two signing nodes with cached certificates: `removed` and `member`."""
    peers = tmp_path / "peers"
    peers.mkdir()
    nodes = {}
    for role in ("removed", "member"):
        node_id, key, cert = _node_key()
        (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        nodes[role] = (node_id, CommitSigner(node_id, key))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    return nodes


def _agent_post(group_id, author_node_id, signer, message_id="m-1", text="status from Linux"):
    """The incident's shape: an agent message from a bridge, signed by its node."""
    payload = {
        "group_id": group_id,
        "text": text,
        "sender_name": "Zcode_linux",
        "sender_type": "agent",
        "sender_node_id": author_node_id,
        "agent_owner": author_node_id,
        "message_id": message_id,
        "timestamp": TIMESTAMP,
        "mentions": [],
        "is_agent": True,
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=group_id, message_id=message_id,
        sender_node_id=author_node_id, sender_name=payload["sender_name"],
        sender_type="agent", agent_owner=author_node_id,
        timestamp=TIMESTAMP, content=text, tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = author_node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    return payload


class _CreatorService:
    """The creator's node: a real GroupManager, everything else recorded."""

    def __init__(self, dpc_home):
        self.group_manager = GroupManager(dpc_home, CREATOR)
        self.p2p_manager = SimpleNamespace(node_id=CREATOR, peers={}, send_message_to_peer=AsyncMock())
        self.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        self.conversation_monitors = {}
        self._processed_message_ids = set()
        self.stored = []

    def _get_or_create_conversation_monitor(self, conversation_id):
        service = self

        class _Monitor:
            message_ids = ()

            async def on_message(self, message):
                service.stored.append((message.message_id, message.sender_node_id))

            def save_history(self):
                pass

            def get_message_history(self):
                return [{"id": m} for m, _ in service.stored]

        return _Monitor()


@pytest.fixture
def creator(tmp_path, monkeypatch, signed):
    """The creator at a v14-like roster: members = itself and one member; the
    removed node's agents entry kept, as `remove_member` keeps it."""
    service = _CreatorService(tmp_path / "creator")
    removed_id = signed["removed"][0]
    member_id = signed["member"][0]
    group = service.group_manager.create_group("AI Studio", "", [member_id, removed_id])
    service.group_manager.set_node_agents(group.group_id, removed_id, ["ext:Zcode_linux"],
                                          {"ext:Zcode_linux": "Zcode_linux"})
    assert service.group_manager.remove_member(group.group_id, removed_id, CREATOR)
    group = service.group_manager.get_group(group.group_id)
    assert removed_id not in group.members and removed_id in group.agents
    for peer in (removed_id, member_id):
        service.p2p_manager.peers[peer] = object()
    handler = GroupTextHandler(service)
    woken = []

    async def _spy(*args, **kwargs):
        woken.append(args)

    monkeypatch.setattr(handler, "_handle_agent_mentions", _spy)
    return SimpleNamespace(service=service, handler=handler, group=group, woken=woken,
                           removed=signed["removed"], member=signed["member"])


def _frames(service):
    return [(c.args[0], c.args[1]["command"], c.args[1]["payload"])
            for c in service.p2p_manager.send_message_to_peer.await_args_list]


# --- receive: the creator refuses an author off the roster --------------------------


@pytest.mark.asyncio
async def test_the_removed_nodes_signed_post_is_refused_at_the_creator_and_not_stored(creator):
    """The falsifier: the 07:39:36Z post replayed against the creator at v14."""
    removed_id, signer = creator.removed
    gid = creator.group.group_id

    await creator.handler.handle(removed_id, _agent_post(gid, removed_id, signer))

    assert creator.service.stored == [], "the removed node's post was stored"
    creator.service.local_api.broadcast_event.assert_not_awaited()
    assert creator.woken == []
    assert _frames(creator.service) == [
        (removed_id, "GROUP_ACCESS_DENIED", {"group_id": gid, "reason": "not_a_member"})
    ], "refused with the existing word, to the author only, and relayed to nobody"
    assert f"{gid}:m-1" not in creator.service._processed_message_ids


@pytest.mark.asyncio
async def test_a_removed_nodes_post_relayed_by_a_member_is_dropped_and_the_member_is_not_told(creator, caplog):
    """A member whose roster is stale stored and relayed it; the creator drops it
    and says nothing to the member, which is not the one refused."""
    removed_id, signer = creator.removed
    member_id = creator.member[0]
    gid = creator.group.group_id

    with caplog.at_level(logging.WARNING):
        await creator.handler.handle(member_id, _agent_post(gid, removed_id, signer))

    assert creator.service.stored == []
    creator.service.local_api.broadcast_event.assert_not_awaited()
    assert _frames(creator.service) == []
    assert any("relayed by" in r.getMessage() and removed_id[:20] in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_an_unsigned_post_from_a_node_off_the_roster_is_refused(creator):
    """A node too old to sign is its own socket; off the roster it is refused too."""
    removed_id, signer = creator.removed
    gid = creator.group.group_id
    payload = _agent_post(gid, removed_id, signer)
    for field in ("content_hash", "signature", "signer_node_id", "preimage_version"):
        payload.pop(field)

    await creator.handler.handle(removed_id, payload)

    assert creator.service.stored == []
    assert [f[1] for f in _frames(creator.service)] == ["GROUP_ACCESS_DENIED"]


@pytest.mark.asyncio
async def test_a_current_members_signed_post_is_still_stored_and_shown(creator):
    member_id, signer = creator.member
    gid = creator.group.group_id

    await creator.handler.handle(member_id, _agent_post(gid, member_id, signer))

    assert creator.service.stored == [("m-1", member_id)]
    shown = [c.args[0] for c in creator.service.local_api.broadcast_event.await_args_list]
    assert shown == ["group_text_received"]
    assert all(f[1] != "GROUP_ACCESS_DENIED" for f in _frames(creator.service))


# --- receive: a group file whose author is off the roster ---------------------------


def _file_offer(group_id, author, described_author=None):
    offer = {"transfer_id": "t-1", "filename": "notes.txt", "size_bytes": 100,
             "hash": "0" * 64, "mime_type": "text/plain", "chunk_size": 64 * 1024,
             "group_id": group_id, "message_id": "f-1", "sender_node_id": author}
    if described_author:
        offer["file_description"] = {"sender_node_id": described_author}
    return offer


def _offer_service(creator):
    """The file door up to the firewall: a decision that denies stops it there,
    so a FILE_CANCEL `firewall_denied` means the member gate let the offer by."""
    creator.service.file_transfer_manager = SimpleNamespace(
        file_requests=None,
        _file_transfer_decision=AsyncMock(
            return_value=SimpleNamespace(outcome="deny", reason="stopped by the test")),
        active_transfers={},
    )
    return creator.service


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed, described", [("removed", None), ("member", "removed")])
async def test_a_group_file_relayed_by_a_member_is_refused_when_its_author_is_off_the_roster(
        creator, claimed, described):
    """The hop is a member, so the old door let it by; the author is not."""
    service = _offer_service(creator)
    ids = {"removed": creator.removed[0], "member": creator.member[0]}
    member_id = ids["member"]
    offer = _file_offer(creator.group.group_id, ids[claimed],
                        ids[described] if described else None)

    await FileOfferHandler(service).handle(member_id, offer)

    assert _frames(service) == [
        (member_id, "FILE_CANCEL", {"transfer_id": "t-1", "reason": "not_a_member"})]
    service.file_transfer_manager._file_transfer_decision.assert_not_awaited()
    assert service.file_transfer_manager.active_transfers == {}


@pytest.mark.asyncio
async def test_a_members_group_file_passes_the_member_gate(creator):
    service = _offer_service(creator)
    member_id = creator.member[0]

    await FileOfferHandler(service).handle(
        member_id, _file_offer(creator.group.group_id, member_id, member_id))

    service.file_transfer_manager._file_transfer_decision.assert_awaited_once()
    assert [f[2]["reason"] for f in _frames(service)] == ["firewall_denied"]


# --- send: the removed node refuses its own post -------------------------------------


REMOVED = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"
GROUP = "group-60aff6110f02"


class _RemovedNodeService:
    """The removed node: its copy of the group, its kept `agents` entry, and
    the real send paths and refusal bookkeeping bound to it."""

    def __init__(self, members, created_by=CREATOR):
        self.group = SimpleNamespace(
            group_id=GROUP, name="AI Studio", created_by=created_by, members=list(members),
            agents={REMOVED: ["ext:Zcode_linux"]},
            agent_names={REMOVED: {"ext:Zcode_linux": "Zcode_linux"}},
        )
        self.group_manager = SimpleNamespace(
            get_group=lambda gid: self.group if gid == GROUP else None)
        self.p2p_manager = SimpleNamespace(node_id=REMOVED, get_display_name=lambda: "Mike (linux)")
        self.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        self._group_access_denied = set()
        self.written = []
        self.broadcast = []

    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as
    note_group_access_denied = CoreService.note_group_access_denied
    clear_group_access_denied = CoreService.clear_group_access_denied

    def get_cc_display_name(self):
        return "CC"

    def _get_agent_display_name(self, agent_id):
        return agent_id

    def parse_mentions(self, text, members):
        return []

    def _get_or_create_conversation_monitor(self, _):
        raise AssertionError("a refused post must not reach the history")

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)


@pytest.mark.asyncio
async def test_a_node_the_roster_dropped_refuses_its_own_agent_post(caplog):
    """The bridge's door, `send_group_agent_message`, with the tag the removal kept."""
    service = _RemovedNodeService(members=[CREATOR])

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        result = await CoreService.send_group_agent_message(
            service, GROUP, "Zcode_linux", "status from Linux")

    assert isinstance(result, dict) and result["status"] == "error"
    assert "not on the roster" in result["message"] and GROUP in result["message"]
    assert service.broadcast == [] and service.local_api.broadcast_event.await_count == 0
    assert any(GROUP in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_node_the_roster_dropped_refuses_its_own_human_post():
    service = _RemovedNodeService(members=[CREATOR])

    result = await CoreService.send_group_message(service, GROUP, "hello")

    assert result["status"] == "error" and "not on the roster" in result["message"]
    assert service.broadcast == []


def test_a_node_the_roster_dropped_has_no_name_to_post_as():
    """`agents[node]` outlives the removal on purpose; it grants nothing."""
    removed = _RemovedNodeService(members=[CREATOR])
    member = _RemovedNodeService(members=[CREATOR, REMOVED])

    assert CoreService._names_this_node_may_post_as(removed, removed.group) == set()
    assert CoreService._names_this_node_may_post_as(member, member.group) == {"zcode_linux"}


@pytest.mark.asyncio
async def test_the_creators_not_a_member_marks_the_group_removed_and_the_send_is_refused():
    """The node's own copy still lists it (it was away for the removal); the
    creator's refusal is what it learns from, and it stops posting on it."""
    service = _RemovedNodeService(members=[CREATOR, REMOVED])
    await GroupAccessDeniedHandler(service).handle(
        CREATOR, {"group_id": GROUP, "reason": "not_a_member"})

    result = await CoreService.send_group_agent_message(
        service, GROUP, "Zcode_linux", "status from Linux")

    assert result["status"] == "error" and "creator" in result["message"]
    assert service.broadcast == []
    assert CoreService._names_this_node_may_post_as(service, service.group) == set()


@pytest.mark.asyncio
async def test_another_members_not_a_member_decides_nothing():
    """A member may answer from a stale roster of its own; only the creator's counts."""
    service = _RemovedNodeService(members=[CREATOR, REMOVED, MEMBER])
    await GroupAccessDeniedHandler(service).handle(
        MEMBER, {"group_id": GROUP, "reason": "not_a_member"})

    assert CoreService._names_this_node_may_post_as(service, service.group) == {"zcode_linux"}


# --- the bridge: says so before it connects, and marks such groups in --list ----------

OFF_THE_ROSTER = "dpc-node-" + "d" * 32


def _run_cli(bridge, monkeypatch, capsys, *args):
    """The CLI by file path, with a `websockets` that fails the test if it is dialled."""
    fake = types.ModuleType("websockets")

    def _no_dial(url):
        raise AssertionError("a refused send must not connect")

    fake.connect = _no_dial
    monkeypatch.setitem(sys.modules, "websockets", fake)
    (bridge.DPC_HOME / ".ws_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(bridge.DPC_HOME))
    monkeypatch.setattr(sys, "argv", ["cc_group_chat_bridge.py", *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(BRIDGE), run_name="__main__")
    return exc.value.code, capsys.readouterr().out


def test_the_bridge_refuses_a_send_into_a_group_this_node_is_not_on(bridge, monkeypatch, capsys):
    (bridge.DPC_HOME / "node.id").write_text(OFF_THE_ROSTER, encoding="utf-8")

    code, out = _run_cli(bridge, monkeypatch, capsys,
                         "--group", BRIDGE_GROUP, "--as", "CC_mike", "--send", "hi")

    assert code == 1
    assert f"[ERROR] refused: this node is not on the roster of group {BRIDGE_GROUP}" in out
    assert "[SENT]" not in out


def test_the_bridge_list_marks_a_group_this_node_is_not_on(bridge, monkeypatch, capsys):
    assert bridge.list_groups()[0]["on_roster"] is True
    (bridge.DPC_HOME / "node.id").write_text(OFF_THE_ROSTER, encoding="utf-8")
    assert bridge.list_groups()[0]["on_roster"] is False

    code, out = _run_cli(bridge, monkeypatch, capsys, "--list")

    assert code == 0
    assert "[REMOVED: this node is not on its roster, cannot post]" in out
