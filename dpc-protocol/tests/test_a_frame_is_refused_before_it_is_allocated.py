"""A frame is refused at the length it declares, before a byte of it is read.

`read_message` took the ten-digit header at its word: `int(header)` and then
`readexactly(payload_length)`, which buffers whatever a stranger declared — up
to 9 999 999 999 bytes — because asyncio's 64 KiB StreamReader limit guards
`readline()` and `readuntil()` and not `readexactly()`. The port this reads
from is the one ADR-041 D2 puts on the open internet (M7, D8).

The refusal is asserted on the reader, not on the return value: a reader asked
for ten billion bytes also returns None once the peer hangs up, and that is
the defect. And the cap has to clear what legitimately crosses in one frame —
a whole conversation history in a CHAT_HISTORY_RESPONSE, 3 885 616 bytes at
the largest measured — so a large round trip is asserted beside the refusal.

Cross-platform: loopback TCP through asyncio.start_server, no socket options,
no signals; the same on Windows, Linux and macOS.
"""

import asyncio
import json

import pytest

from dpc_protocol.protocol import MAX_FRAME_BYTES, read_message, write_message


class _Reader:
    """A StreamReader stand-in that remembers every length it was asked for."""

    def __init__(self, data: bytes):
        self._data = data
        self.asked = []

    async def readexactly(self, n: int) -> bytes:
        self.asked.append(n)
        chunk, self._data = self._data[:n], self._data[n:]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk


class _Writer:
    """A StreamWriter stand-in that keeps what was written."""

    def __init__(self):
        self.written = []

    def write(self, data: bytes):
        self.written.append(data)

    async def drain(self):
        pass


def _frame(payload: bytes) -> bytes:
    return f"{len(payload):010d}".encode() + payload


def _payload_of(size: int) -> bytes:
    """A JSON object whose encoding is exactly `size` bytes."""
    padding = size - len(json.dumps({"pad": ""}).encode())
    payload = json.dumps({"pad": "x" * padding}).encode()
    assert len(payload) == size
    return payload


async def _cross(raw: bytes, **read_kwargs):
    """Push raw bytes through loopback TCP and read them back as one message."""
    received = asyncio.get_running_loop().create_future()

    async def _handle(reader, writer):
        try:
            received.set_result(await read_message(reader, **read_kwargs))
        except Exception as e:  # pragma: no cover - surfaces as the test's failure
            received.set_exception(e)
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(raw)
            # A refusing server hangs up with the payload unread; the reset
            # that can follow is the refusal seen from this side, not an error.
            try:
                await writer.drain()
            except ConnectionError:
                pass
            return await asyncio.wait_for(received, timeout=30)
        finally:
            writer.close()
    finally:
        server.close()
        await server.wait_closed()


# --- the refusal -------------------------------------------------------------


def test_a_header_declaring_ten_billion_bytes_never_reaches_the_reader():
    """The worst case of the header format, refused before the allocation.

    `asked` is the assertion. Without the cap the reader is asked for the
    declared length, and only the peer hanging up turns that into None.
    """
    reader = _Reader(b"9999999999" + b"{}")

    assert asyncio.run(read_message(reader)) is None
    assert reader.asked == [10], "the reader was asked for the declared length"


def test_a_frame_exactly_at_the_cap_is_read_and_one_byte_over_is_refused():
    """The boundary, on the real StreamReader over loopback."""
    cap = 4096

    async def run():
        at = await _cross(_frame(_payload_of(cap)), max_frame_bytes=cap)
        over = await _cross(_frame(_payload_of(cap + 1)), max_frame_bytes=cap)
        return at, over

    at, over = asyncio.run(run())

    assert at == json.loads(_payload_of(cap))
    assert over is None


def test_write_message_refuses_an_oversize_payload_before_writing_a_byte():
    """Loud at the origin: the far end would only have closed the connection."""
    writer = _Writer()
    message = {"pad": "x" * 100}
    size = len(json.dumps(message).encode())

    with pytest.raises(ValueError) as err:
        asyncio.run(write_message(writer, message, max_frame_bytes=64))

    assert str(size) in str(err.value) and "64" in str(err.value)
    assert writer.written == []


def test_a_negative_or_non_numeric_header_still_closes_the_connection():
    """The path that existed before the cap keeps its job."""

    async def run():
        return (
            await _cross(b"-000000001"),
            await _cross(b"GET / HTTP/1.1\r\n"),
        )

    assert asyncio.run(run()) == (None, None)


# --- what the cap must clear ---------------------------------------------------


def test_a_four_megabyte_history_still_crosses():
    """CHAT_HISTORY_RESPONSE carries a whole history in one frame.

    The largest on the reference box was 3 885 616 bytes when the cap was
    chosen; this one is bigger, through the real writer and the real reader.
    """
    history = {"messages": [{"role": "user", "content": "x" * 4000}] * 1000}
    assert len(json.dumps(history).encode()) > 3_885_616

    async def run():
        received = asyncio.get_running_loop().create_future()

        async def _handle(reader, writer):
            received.set_result(await read_message(reader))
            writer.close()

        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await write_message(writer, history)
                return await asyncio.wait_for(received, timeout=60)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) == history


def test_the_cap_clears_the_ceilings_it_was_measured_against():
    """The numbers in the comment above MAX_FRAME_BYTES, as assertions.

    Whoever moves the number must clear these; histories grow, so clearing
    them by a hair is not clearing them.
    """
    largest_history_seen = 3_885_616
    largest_image_framed = 5 * 1024 * 1024 * 4 // 3  # vision.max_image_size_mb, base64
    file_chunk_framed = 64 * 1024 * 4 // 3 + 10

    assert MAX_FRAME_BYTES > 4 * largest_history_seen
    assert MAX_FRAME_BYTES > 4 * largest_image_framed
    assert MAX_FRAME_BYTES > file_chunk_framed
