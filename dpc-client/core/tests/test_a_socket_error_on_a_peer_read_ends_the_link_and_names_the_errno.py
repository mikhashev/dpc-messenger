"""A socket error on a peer read ends the link and names the errno.

`_listen_to_peer` caught only IncompleteReadError, ConnectionResetError and
TimeoutError. A read that failed with any other OSError - live on the Windows
node as WinError 121, "The semaphore timeout period has expired" - still ran
the cleanup in `finally`, but then escaped the task and surfaced as "Task
exception was never retrieved", with the errno nowhere beside the peer. The
read now ends the link through the same cleanup and says so once, at WARNING.

Cross-platform: no socket at all - the reader raises the OSError the Windows
log recorded; `winerror` exists only on Windows, so the test reads only the
errno and the peer from the line. Nothing touches ~/.dpc.
"""

import asyncio
import logging

from dpc_client_core.p2p_manager import P2PManager, PeerConnection

PEER = "dpc-node-" + "ab" * 16


class _DeadRouteReader:
    """A reader whose first read fails as the Windows node's did."""

    async def readexactly(self, n):
        raise OSError(22, "The semaphore timeout period has expired", None, 121)


class _Writer:
    def __init__(self):
        self.closed = False

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _manager() -> P2PManager:
    """Only the state the listener and its cleanup touch — `__init__` reads ~/.dpc."""
    manager = P2PManager.__new__(P2PManager)
    manager.peers = {}
    manager._peer_listener_tasks = {}
    manager._intentional_disconnects = set()
    manager._pending_webrtc = {}
    manager._ice_candidates_buffer = {}
    manager._hub_client_refs = {}
    manager.on_message_received = None
    manager.on_peer_list_change = None
    manager.on_peer_disconnected = None
    return manager


def test_a_read_that_raises_oserror_ends_the_link_once_with_a_warning(caplog):
    async def scenario():
        manager = _manager()
        disconnected: list = []
        list_changes: list = []

        async def on_disconnected(peer_id):
            disconnected.append(peer_id)

        async def on_list_change():
            list_changes.append(True)

        manager.on_peer_disconnected = on_disconnected
        manager.on_peer_list_change = on_list_change

        writer = _Writer()
        peer = PeerConnection(PEER, _DeadRouteReader(), writer)
        manager.peers[PEER] = peer
        task = asyncio.create_task(manager._listen_to_peer(peer))
        manager._peer_listener_tasks[PEER] = task
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        return manager, task, writer, disconnected, list_changes

    with caplog.at_level(logging.INFO, logger="dpc_client_core.p2p_manager"):
        manager, task, writer, disconnected, list_changes = asyncio.run(scenario())

    # The task ended on its own, not on an exception nobody retrieves.
    assert task.exception() is None

    # The same cleanup as any other read error.
    assert PEER not in manager.peers
    assert PEER not in manager._peer_listener_tasks
    assert writer.closed
    assert disconnected == [PEER]
    assert list_changes == [True]
    messages = [r.getMessage() for r in caplog.records]
    assert sum(f"Connection with peer {PEER} was lost" in m for m in messages) == 1

    # One WARNING naming the peer and the errno.
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "dpc_client_core.p2p_manager"
    ]
    assert len(warnings) == 1, warnings
    assert PEER in warnings[0]
    assert "errno 22" in warnings[0]
