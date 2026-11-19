"""A file and the words written with it are one message, on the sender and on every peer.

Until 2026-11-19 the composer sent a picked file through its own confirm box,
with no place for words, so a file and a sentence about it were two messages.
Now the composer holds the file as it holds a pasted screenshot and Send sends
one message: the file alone, or the file with the text (Mike's call,
2026-11-19). `send_group_file` and the one-to-one `send_file` take `text`; it is
the sender's record text, it rides FILE_OFFER `text` (every relay copies it),
and a peer's record of the file carries it instead of "Received file: ...".
Without text nothing changes.

Real FileTransferManager, FileOfferHandler and FileCompleteHandler over the
in-process network of test_a_group_file_crosses_a_full_mesh_once.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP, _ids, _make_node, _Network, full_mesh, received_files, records, star,
)

SONG = b"ID3" + b"\x00" * 4000
WORDS = "@Ark listen to this melody"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _picked(root, name="song.mp3"):
    f = root / "outbox" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(SONG)
    return f


def _woken_agents(node):
    """Record what the send hands to the group's agents."""
    calls = []

    async def routed(group_id, text, sender_name, **kw):
        calls.append((group_id, text))

    node.service._handle_group_agent_mentions = routed
    return calls


# --- a group ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_group_of_one_node_records_the_file_and_its_words_as_one_message(root):
    net = _Network()
    me = _ids(1)[0]
    node = _make_node(net, root, me, [], [me])
    woken = _woken_agents(node)

    result = await CoreService.send_group_file(node.service, GROUP, str(_picked(root)), text=f"  {WORDS}\n")

    assert result["status"] == "success", result
    (rec,) = records(node)
    assert rec["content"] == WORDS
    assert rec["id"] == result["message_id"]
    (att,) = rec["attachments"]
    assert att["type"] == "file" and att["mime_type"] == "audio/mpeg"
    (shown,) = node.api.named("group_file_received")
    assert shown["text"] == WORDS
    assert shown["attachments"] == [att]
    assert woken == [(GROUP, WORDS)], "the words wake the group's agents, as a caption does"


@pytest.mark.asyncio
async def test_a_connected_peer_gets_one_record_with_the_same_words(root):
    net = _Network()
    author, peer = full_mesh(net, root, 2)

    result = await CoreService.send_group_file(author.service, GROUP, str(_picked(root)), text=WORDS)
    await net.settle()

    assert not net.errors, net.errors
    (offer,) = net.offers_to(peer.me)
    assert offer[3]["text"] == WORDS
    # The sender: one record, the words as its text, no second from the echo.
    (author_rec,) = records(author)
    assert author_rec["content"] == WORDS
    assert len(author.api.named("group_file_received")) == 1
    # The peer: one copy, one record, the same words, under the author's id.
    assert received_files(peer) == ["song.mp3"]
    (peer_rec,) = records(peer)
    assert peer_rec["content"] == WORDS
    assert peer_rec["id"] == result["message_id"]
    (peer_shown,) = peer.api.named("group_file_received")
    assert peer_shown["text"] == WORDS


@pytest.mark.asyncio
async def test_a_relay_passes_the_words_on_with_the_file(root):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)

    await CoreService.send_group_file(author.service, GROUP, str(_picked(root)), text=WORDS)
    await net.settle()

    assert not net.errors, net.errors
    (relayed,) = net.offers_to(other_leaf.me)
    assert relayed[3]["text"] == WORDS
    for node in (hub, other_leaf):
        (rec,) = records(node)
        assert rec["content"] == WORDS, node.me


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   \n"])
async def test_without_words_the_file_is_recorded_as_before(root, text):
    net = _Network()
    author, peer = full_mesh(net, root, 2)
    woken = _woken_agents(author)

    await CoreService.send_group_file(author.service, GROUP, str(_picked(root)), text=text)
    await net.settle()

    assert not net.errors, net.errors
    (offer,) = net.offers_to(peer.me)
    assert "text" not in offer[3]
    (author_rec,) = records(author)
    assert author_rec["content"].startswith("Sent file: song.mp3 (")
    assert author.api.named("group_file_received")[0]["text"] == ""
    (peer_rec,) = records(peer)
    assert peer_rec["content"].startswith("Received file: song.mp3 (")
    assert woken == []


@pytest.mark.asyncio
async def test_an_offer_whose_text_is_not_a_string_records_the_file_alone(root):
    net = _Network()
    author, peer = full_mesh(net, root, 2)
    real_send = author.service.p2p_manager.send_message_to_peer

    async def tampered(to, message):
        if message["command"] == "FILE_OFFER":
            message = dict(message, payload=dict(message["payload"], text=["not", "words"]))
        await real_send(to, message)

    author.service.p2p_manager.send_message_to_peer = tampered  # the manager's wire too

    await CoreService.send_group_file(author.service, GROUP, str(_picked(root)), text=WORDS)
    await net.settle()

    assert not net.errors, net.errors
    (peer_rec,) = records(peer)
    assert peer_rec["content"].startswith("Received file: song.mp3 (")


# --- one to one ---------------------------------------------------------------

def _p2p_records(node, other):
    monitor = node.monitors.get(other.me)
    return monitor.message_history if monitor else []


async def _accept(peer, author):
    """The person on the peer presses Accept (P2PCoordinator.accept_file_transfer)."""
    (offer,) = peer.api.named("file_transfer_offered")
    coordinator = SimpleNamespace(service=peer.service, p2p_manager=peer.service.p2p_manager)
    await P2PCoordinator.accept_file_transfer(coordinator, offer["transfer_id"])
    return offer


@pytest.mark.asyncio
async def test_a_one_to_one_file_and_its_words_are_one_message_on_both_sides(root):
    net = _Network()
    author, peer = full_mesh(net, root, 2)
    coordinator = SimpleNamespace(service=author.service)

    sent = await P2PCoordinator.send_file(coordinator, peer.me, str(_picked(root)), text=f"{WORDS} ")
    await net.settle()
    offer = await _accept(peer, author)
    await net.settle()

    assert not net.errors, net.errors
    assert offer["transfer_id"] == sent["transfer_id"]
    assert offer["text"] == WORDS, "the accept dialog can show the words"
    # The sender's record is written when the peer has the file, with the words.
    (author_rec,) = _p2p_records(author, peer)
    assert author_rec["content"] == WORDS
    assert author_rec["attachments"][0]["type"] == "file"
    (author_shown,) = author.api.named("new_p2p_message")
    assert author_shown["text"] == WORDS
    # The peer: one record and one bubble, the same words.
    (peer_rec,) = _p2p_records(peer, author)
    assert peer_rec["content"] == WORDS
    assert peer_rec["attachments"][0]["filename"] == "song.mp3"
    (peer_shown,) = peer.api.named("new_p2p_message")
    assert peer_shown["text"] == WORDS


@pytest.mark.asyncio
async def test_a_one_to_one_file_without_words_is_recorded_as_before(root):
    net = _Network()
    author, peer = full_mesh(net, root, 2)
    coordinator = SimpleNamespace(service=author.service)

    await P2PCoordinator.send_file(coordinator, peer.me, str(_picked(root)))
    await net.settle()
    offer = await _accept(peer, author)
    await net.settle()

    assert not net.errors, net.errors
    assert "text" not in offer
    assert "text" not in net.offers_to(peer.me)[0][3]
    (author_rec,) = _p2p_records(author, peer)
    assert author_rec["content"].startswith("Sent file: song.mp3 (")
    (peer_rec,) = _p2p_records(peer, author)
    assert peer_rec["content"].startswith("Received file: song.mp3 (")
    assert peer.api.named("new_p2p_message")[0]["text"] == ""
