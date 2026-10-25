"""A group send tells its sender which connected members were not offered the file, and why.

`send_file` refuses a new upload when `max_concurrent_transfers` (3) uploads are
open. The three group fan-outs caught that per member, logged a warning and still
answered `{"status": "success", "transfer_ids": [...]}`: in a group with more than
three connected members everyone past the third got nothing and the sender was told
it worked (Mike's call, 2026-10-25: show the sender a correct notice, nothing more).

The result now carries `not_offered` (one row per connected member that got no
offer: `node_id`, `name`, `reason`, and `detail` for anything but the cap) and,
when the cap was hit, `upload_limit` (`limit`, `open_uploads`, `sentence`). The cap
is told from other errors by its type, `MaxConcurrentTransfersExceeded`, a
`RuntimeError`. A member that is not connected is not a failure: in a star it gets
the file by relay.

Real FileTransferManager and CoreService group send paths; receivers never answer,
so every upload stays open and holds its slot.
"""

import asyncio

import pytest

from dpc_client_core.managers.file_transfer_manager import (
    NOT_OFFERED_SEND_ERROR,
    NOT_OFFERED_UPLOAD_LIMIT,
    MaxConcurrentTransfersExceeded,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import (  # noqa: F401 - root is a fixture
    _Network,
    root,
    send,
    star,
)

KINDS = ["file", "image", "voice"]


class _SilentNetwork(_Network):
    """Records what is sent and answers nothing: an offer nobody has looked at."""

    async def _deliver(self, to, frm, message):
        try:
            self.frames.append((to, frm, message["command"], message["payload"]))
        finally:
            self.in_flight -= 1


def _hub_and_leaves(root, leaves):
    net = _SilentNetwork()
    hub, *others = star(net, root, leaves=leaves)
    return net, hub, others


async def _offered_to(net):
    await asyncio.sleep(0.05)
    return [f[0] for f in net.frames if f[2] == "FILE_OFFER"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_the_members_past_the_cap_are_named_with_the_reason(root, kind):
    net, hub, leaves = _hub_and_leaves(root, 5)
    hub.service.peer_metadata = {leaves[3].me: {"name": "Dora"}}

    result = await send(hub, kind, root)

    offered = await _offered_to(net)
    assert offered == [leaf.me for leaf in leaves[:3]]
    assert len(result["transfer_ids"]) == 3
    assert [r["node_id"] for r in result["not_offered"]] == [leaves[3].me, leaves[4].me]
    assert {r["reason"] for r in result["not_offered"]} == {NOT_OFFERED_UPLOAD_LIMIT}
    assert [r["name"] for r in result["not_offered"]] == ["Dora", None]
    assert all("detail" not in r for r in result["not_offered"]), "the cap's reason is its sentence below"
    limit = result["upload_limit"]
    assert limit["limit"] == 3
    assert [(u["node_id"], u["status"]) for u in limit["open_uploads"]] == \
        [(leaf.me, "pending") for leaf in leaves[:3]]
    assert "3" in limit["sentence"]
    assert "[file_transfer] max_concurrent_transfers" in limit["sentence"]
    assert "config.ini" in limit["sentence"]
    assert "waiting" in limit["sentence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_send_under_the_cap_reports_nothing(root, kind):
    net, hub, leaves = _hub_and_leaves(root, 3)

    result = await send(hub, kind, root)

    assert len(result["transfer_ids"]) == 3
    assert "not_offered" not in result
    assert "upload_limit" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_member_that_is_not_connected_is_not_reported(root, kind):
    net, hub, leaves = _hub_and_leaves(root, 5)
    # b is in the group but not connected to the sender: four are connected, so the
    # cap leaves out one, e, and b is not counted among the failures
    away = leaves[1].me
    hub.service.p2p_coordinator.get_connected_peers = \
        lambda: [leaf.me for leaf in leaves if leaf.me != away]

    result = await send(hub, kind, root)

    assert away not in await _offered_to(net)
    assert [r["node_id"] for r in result["not_offered"]] == [leaves[4].me]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_an_offline_member_alone_does_not_make_a_notice(root, kind):
    net, hub, leaves = _hub_and_leaves(root, 3)
    hub.service.p2p_coordinator.get_connected_peers = lambda: [leaves[0].me, leaves[1].me]

    result = await send(hub, kind, root)

    assert len(result["transfer_ids"]) == 2
    assert "not_offered" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_another_error_is_reported_under_its_own_reason(root, kind):
    net, hub, leaves = _hub_and_leaves(root, 3)
    failing = leaves[1].me
    real_send = hub.service.p2p_manager.send_message_to_peer

    async def send_message_to_peer(to, message):
        if to == failing and message["command"] == "FILE_OFFER":
            raise ConnectionError("Peer closed the connection")
        return await real_send(to, message)

    hub.service.p2p_manager.send_message_to_peer = send_message_to_peer

    result = await send(hub, kind, root)

    assert [(r["node_id"], r["reason"], r["detail"]) for r in result["not_offered"]] == \
        [(failing, NOT_OFFERED_SEND_ERROR, "Peer closed the connection")]
    assert "upload_limit" not in result, "the cap was not what stopped this one"
    assert len(result["transfer_ids"]) == 2


def test_the_cap_is_a_runtime_error_of_its_own_type_so_old_callers_keep_working():
    assert issubclass(MaxConcurrentTransfersExceeded, RuntimeError)
    error = MaxConcurrentTransfersExceeded(
        3, [{"node_id": "dpc-node-x", "name": None, "filename": "a.txt", "status": "transferring"}])
    assert error.limit == 3 and error.open_uploads[0]["filename"] == "a.txt"
    assert "a.txt to dpc-node-x (sending)" in str(error)


@pytest.mark.asyncio
async def test_a_one_to_one_send_at_the_cap_raises_the_same_sentence(root):
    net, hub, leaves = _hub_and_leaves(root, 4)
    path = root / "outbox" / "notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    for leaf in leaves[:3]:
        await hub.manager.send_file(leaf.me, path)

    with pytest.raises(RuntimeError) as raised:
        await hub.manager.send_file(leaves[3].me, path)

    assert isinstance(raised.value, MaxConcurrentTransfersExceeded)
    assert str(raised.value).startswith("Upload limit reached: 3 uploads are open and the limit is 3")
    assert str(raised.value) == await _group_cap_sentence(root), "one sentence for both screens"


async def _group_cap_sentence(root):
    net, hub, leaves = _hub_and_leaves(root / "g", 4)
    result = await send(hub, "file", root / "g")
    return result["upload_limit"]["sentence"]


@pytest.mark.asyncio
async def test_a_relaying_member_past_the_cap_shows_its_user_nothing(root):
    from tests.test_a_group_file_crosses_a_full_mesh_once import _Network as RealNetwork

    net = RealNetwork()
    hub, author, other_leaf = star(net, root, leaves=2)
    hub.manager.max_concurrent_transfers = 0
    unretrieved = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unretrieved.append(ctx))

    result = await send(author, "file", root)
    await net.settle()
    await asyncio.sleep(0.05)

    assert unretrieved == []
    assert "not_offered" not in result, "the author offered the hub, and the hub took it"
    shown = [(name, payload) for name, payload in hub.api.events
             if "not_offered" in str(payload) or "upload_limit" in str(payload)
             or "Upload limit" in str(payload)]
    assert shown == [], "the hub's user sent nothing and is told nothing"
    assert net.offers_to(other_leaf.me) == [], "the relay was refused, as before"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["screenshot", "voice"])
async def test_a_one_to_one_screenshot_or_voice_at_the_cap_is_not_wrapped_in_another_sentence(root, kind):
    """`send_p2p_image` and `send_voice_message` turned every error into
    "Failed to start ...: <error>"; the cap's own sentence is what the user reads."""
    import base64

    from dpc_client_core.service import CoreService
    from tests.test_a_group_file_crosses_a_full_mesh_once import _png_data_url

    net, hub, leaves = _hub_and_leaves(root, 1)
    hub.manager.max_concurrent_transfers = 0
    if kind == "screenshot":
        call = CoreService.send_p2p_image(hub.service, leaves[0].me, _png_data_url(), "shot.png", "")
    else:
        audio = base64.b64encode(b"\x1a\x45\xdf\xa3" + b"v" * 2000).decode()
        call = CoreService.send_voice_message(hub.service, leaves[0].me, audio, 1.5)

    with pytest.raises(MaxConcurrentTransfersExceeded) as raised:
        await call

    assert str(raised.value).startswith("Upload limit reached: 0 uploads are open and the limit is 0")
