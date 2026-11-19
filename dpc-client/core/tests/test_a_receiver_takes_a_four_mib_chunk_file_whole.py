"""A receiver takes a file sent in 4 MiB chunks whole, on the receive path it already had.

ADR-047 phase 0 changes only the sender: the chunk size travels in the offer
and the receiver writes at index * the offer's chunk_size (DPTP §3.11), so an
older receiver gets the larger size with no change of its own. Zcode's point 4
(2026-11-19) asked for that to be shown, not assumed: this sends a file of
three 4 MiB-scale chunks over a real loopback socket, with PeerConnection's
framing on both ends (each chunk is one ~5.6 MB frame, under MAX_FRAME_BYTES),
into the receive path this change leaves untouched (FileOfferHandler,
FileTransferManager.handle_file_chunk and _finalize_download), and checks the
bytes on disk against the sender's SHA-256, and the CRC list against the size.

Loopback TCP through asyncio on both sides; no TLS (the framing is the same),
no socket options beyond what PeerConnection sets; every file in tmp_path.
"""

import asyncio
import hashlib
import math
import socket

import pytest

from dpc_client_core.p2p_manager import PeerConnection
from tests.perf import file_transfer_throughput as bench

MiB = 1024 * 1024


async def _loopback_pair():
    accepted = asyncio.get_running_loop().create_future()

    async def on_connect(reader, writer):
        accepted.set_result((reader, writer))

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c_reader, c_writer = await asyncio.open_connection("127.0.0.1", port)
    s_reader, s_writer = await accepted
    return server, (c_reader, c_writer), (s_reader, s_writer)


@pytest.mark.asyncio
async def test_a_file_in_four_mib_chunks_lands_whole_and_its_hash_matches(tmp_path):
    server, (c_reader, c_writer), (s_reader, s_writer) = await _loopback_pair()
    sender = bench.Sender(tmp_path, {"chunk_size": "4MiB"})
    receiver = bench.Receiver(tmp_path, {})
    to_receiver = PeerConnection(bench.RECEIVER, c_reader, c_writer, direction="out")
    to_sender = PeerConnection(bench.SENDER, s_reader, s_writer, direction="in")
    sender.peers[bench.RECEIVER] = to_receiver
    receiver.peers[bench.SENDER] = to_sender
    readers = [asyncio.create_task(bench._read_into(to_receiver, sender.dispatch)),
               asyncio.create_task(bench._read_into(to_sender, receiver.dispatch))]
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    size = 2 * 4 * MiB + 1234567
    path = bench.make_file(outbox, size, "model.gguf")
    try:
        row = await sender.send(path, 60, receiver.chat_waits, lambda: receiver.offers)
    finally:
        for task in readers:
            task.cancel()
        c_writer.close()
        s_writer.close()
        server.close()

    assert not row["timed_out"] and not row["errors"] and not receiver.errors, (row, receiver.errors)
    (offer,) = receiver.offers
    assert offer["chunk_size"] == 4 * MiB
    assert len(offer["chunk_hashes"]) == math.ceil(size / (4 * MiB)) == 3
    landed = tmp_path / "b" / "conversations" / bench.SENDER / "files" / "model.gguf"
    data = path.read_bytes()
    assert landed.read_bytes() == data
    assert offer["hash"] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_both_ends_of_a_direct_link_send_small_frames_without_nagle():
    """asyncio skips TCP_NODELAY on an accepted socket whose proto reads 0 (Windows);
    PeerConnection sets it on both ends."""
    server, (c_reader, c_writer), (s_reader, s_writer) = await _loopback_pair()
    try:
        for writer, direction in ((c_writer, "out"), (s_writer, "in")):
            PeerConnection("dpc-node-" + "c" * 32, None, writer, direction=direction)
            sock = writer.get_extra_info("socket")
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0, direction
    finally:
        c_writer.close()
        s_writer.close()
        server.close()
