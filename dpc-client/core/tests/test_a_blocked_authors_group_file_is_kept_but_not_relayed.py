"""A group file, image or voice message by a blocked author is kept but not relayed onward.

The relay of a group file decides by its author (the offer's
`sender_node_id`, carried unchanged by every relay), as the GROUP_TEXT relay
does. On a node that blocks the author the file is received and shown as
today — the UI folds it — and offered to no other member. With no block the
same star delivers it to the leaf the author cannot reach.

The full-mesh harness of test_a_group_file_crosses_a_full_mesh_once: real
FileTransferManager, offer, complete and cancel handlers and the CoreService
group send paths over an in-process network, every file in tmp_path.
"""

import logging

import pytest

from tests.test_a_group_file_crosses_a_full_mesh_once import (
    _Network,
    received_files,
    root,  # noqa: F401 - the fixture, by name
    send,
    star,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_the_hub_keeps_a_blocked_authors_file_and_offers_it_to_nobody(root, kind, caplog):
    caplog.set_level(logging.INFO, logger="dpc_client_core.managers.file_transfer_manager")
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)
    hub.service.firewall.is_blocked = lambda node_id: node_id == author.me

    await send(author, kind, root)
    await net.settle()

    assert not net.errors, net.errors
    assert len(received_files(hub)) == 1, "kept"
    assert len(hub.api.named("group_file_received")) == 1, "shown, for the UI to fold"
    assert net.offers_to(other_leaf.me) == []
    assert received_files(other_leaf) == []
    assert any("by blocked node" in r.getMessage() and author.me in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_with_another_node_blocked_the_hub_still_relays(root, kind):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)
    hub.service.firewall.is_blocked = lambda node_id: node_id == "dpc-node-" + "z" * 32

    await send(author, kind, root)
    await net.settle()

    assert not net.errors, net.errors
    assert len(net.offers_to(other_leaf.me)) == 1
    assert len(received_files(other_leaf)) == 1
