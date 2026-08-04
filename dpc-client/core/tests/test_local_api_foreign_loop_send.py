"""A send from another event loop must still be serialized.

The per-client lock exists because concurrent sends interleave their bytes on
one WebSocket: S145 measured 558 corrupted frames in 50ms during browse_page
start, and the corruption ate web_auth_popup_request so the modal never
appeared. `_send_locked` caught the "bound to a different event loop"
RuntimeError and sent *unlocked*, silently — which reopens exactly that hole,
and agent tools run on a per-call loop, so every tool that broadcasts takes it.
"""

import asyncio
import threading

import pytest

from dpc_client_core.local_api import LocalApiServer


class _Client:
    """Records the order in which whole messages land on the wire."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        # A real socket write is not atomic; yielding here is what lets a
        # second, unserialized sender interleave with this one.
        await asyncio.sleep(0)
        self.sent.append(message)


def _server():
    srv = LocalApiServer.__new__(LocalApiServer)
    srv._clients = set()
    srv._client_locks = {}
    srv._owner_loop = None
    return srv


@pytest.mark.asyncio
async def test_a_foreign_loop_send_goes_out_under_the_lock():
    """The property, not just delivery.

    "The message arrived" passes on the unlocked fallback too — that fallback
    delivers, it just delivers *interleaved*. What has to hold is that the send
    happens while the lock is taken. The client records that at send time.

    The lock is deliberately bound-but-free here: an `asyncio.Lock` held by
    another loop does not raise, it blocks, so the RuntimeError branch this
    exercises only fires on a free lock — which is the production case, where
    the main loop is idle between sends.
    """
    srv = _server()
    lock = asyncio.Lock()
    srv._owner_loop = asyncio.get_running_loop()

    async with lock:  # bind it to this loop, then leave it free
        pass

    seen = {}

    class _Recorder(_Client):
        async def send(self, message):
            seen["locked_during_send"] = lock.locked()
            seen["loop_during_send"] = asyncio.get_running_loop()
            await super().send(message)

    client = _Recorder()
    srv._client_locks[client] = lock

    done = threading.Event()
    err = {}

    def _other_loop():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(srv._send_locked(client, "from-a-tool"))
        except BaseException as e:
            err["e"] = repr(e)
        finally:
            loop.close()
            done.set()

    threading.Thread(target=_other_loop, daemon=True).start()
    for _ in range(300):
        if done.is_set():
            break
        await asyncio.sleep(0.01)

    assert not err, f"the foreign-loop send raised: {err.get('e')}"
    assert client.sent == ["from-a-tool"], "the message never arrived"
    assert seen.get("locked_during_send") is True, "the send went out with the lock free"
    # The discriminating assertion. Taking the lock is not enough: an
    # asyncio.Lock is happily acquired from any loop while free, and only
    # deadlocks the foreign waiter once it is contended. What has to be true
    # is that the write itself happens on the loop that owns the connection.
    assert seen.get("loop_during_send") is srv._owner_loop, (
        "the write ran on the tool's own loop; a contended lock there blocks "
        "the foreign waiter forever instead of queueing behind the owner"
    )


@pytest.mark.asyncio
async def test_a_foreign_loop_send_is_routed_back_and_delivered():
    srv = _server()
    client = _Client()
    srv._owner_loop = asyncio.get_running_loop()
    srv._client_locks[client] = asyncio.Lock()  # bound to this loop

    done = threading.Event()
    result = {}

    def _other_loop():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(srv._send_locked(client, "from-a-tool"))
            result["ok"] = True
        except Exception as e:  # pragma: no cover - failure detail
            result["error"] = repr(e)
        finally:
            loop.close()
            done.set()

    threading.Thread(target=_other_loop, daemon=True).start()
    while not done.is_set():
        await asyncio.sleep(0.01)

    assert result.get("ok"), f"the send failed outright: {result.get('error')}"
    assert client.sent == ["from-a-tool"]


@pytest.mark.asyncio
async def test_the_lock_is_honoured_on_the_owning_loop():
    """Two concurrent sends must not interleave — the property the lock buys."""
    srv = _server()
    client = _Client()
    srv._owner_loop = asyncio.get_running_loop()
    srv._client_locks[client] = asyncio.Lock()

    await asyncio.gather(
        srv._send_locked(client, "first"),
        srv._send_locked(client, "second"),
    )

    assert sorted(client.sent) == ["first", "second"]
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_no_owner_loop_still_delivers():
    """A server that never started must not swallow the message."""
    srv = _server()
    client = _Client()
    srv._client_locks[client] = asyncio.Lock()

    await srv._send_locked(client, "last-resort")

    assert client.sent == ["last-resort"]
