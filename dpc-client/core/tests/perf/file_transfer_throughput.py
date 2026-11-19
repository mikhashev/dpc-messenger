"""File transfer throughput and chat wait between two nodes on one machine.

ADR-047 phase 0 / 2 benchmark. Two real FileTransferManagers, the real
FILE_OFFER handler and a real P2PManager send path on each side, joined by
either

- a loopback TLS link, the receiver in a process of its own (its own event
  loop and CPU time, as on another machine; PeerConnection on both ends), or
- an in-process link that moves a fixed number of bytes a second in order
  (`ThrottledLink`), to see what a slow line does to chat.

While a file moves, the sender sends a chat frame every CHAT_PROBE_INTERVAL_S
on the priority lane (`send_message_to_peer`); the receiver notes how long it
took from the send call to its arrival. `time.perf_counter` is one clock for
every process of the machine on Windows, Linux and macOS.

`--load` gives the sender's event loop LOAD_TASKS other tasks that each hold
it LOAD_SLICE_S per turn, so every turn of the loop costs about
LOAD_TASKS x LOAD_SLICE_S = 0.1 s: a stand-in for a node busy with a model,
where each await of a sender costs a turn (Zcode measured LINK_PONG 1.6-3.3 s
under load against 0.03-0.09 s idle, and about 0.4 s per 64 KB chunk, 2026-11-19).

The module uses only what the code before 2026-11-19 had too (send_file,
handle_file_accept, handle_file_chunk, PeerConnection, P2PManager.__new__),
so the same file measures the old build: put that build's packages first on
PYTHONPATH and run this file by its path.

Run, from dpc-client/core:
    python tests/perf/file_transfer_throughput.py --sizes 4 100 300
    python tests/perf/file_transfer_throughput.py --sizes 4 20 --load
    python tests/perf/file_transfer_throughput.py --sizes 20 --link-rate 2

Nothing touches ~/.dpc; everything is in a temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import multiprocessing
import os
import ssl
import statistics
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_client_core.managers.file_transfer_manager import FileTransferManager
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.p2p_manager import P2PManager, PeerConnection

SENDER = "dpc-node-" + "a" * 32
RECEIVER = "dpc-node-" + "b" * 32
CHAT_PROBE_INTERVAL_S = 0.25
LOAD_TASKS = 20
LOAD_SLICE_S = 0.005
MB = 1_000_000


# --- the in-process link -----------------------------------------------------------


class ThrottledLink:
    """One direction of an in-process link that moves `rate` bytes a second, in order.

    It looks like PeerConnection to P2PManager (`send`, and the data lane's
    `write_nowait` and `drain`). A frame is delivered whole when its last byte
    has crossed, so a frame queued behind others waits for them, as on a TCP
    stream. Delivery times come from a virtual clock, so a coarse timer (about
    15.6 ms on Windows) delays a delivery but never lowers the rate. `drain`
    behaves like asyncio's: it waits only while more than HIGH_WATER bytes are
    queued, and then until at most LOW_WATER are.
    """

    HIGH_WATER = 64 * 1024
    LOW_WATER = 16 * 1024
    connection_type = "direct_tls"

    def __init__(self, node_id: str, rate_bps: float, deliver: Callable[[dict], Any]):
        self.node_id = node_id
        self.rate = float(rate_bps)
        self.deliver = deliver
        self.link_rtt_s = None
        self._queue: deque = deque()  # (message, nbytes, due)
        self._queued_bytes = 0
        self._last_due = 0.0
        self._low = asyncio.Event()
        self._low.set()
        self._pump = None

    def write_nowait(self, message: dict) -> int:
        nbytes = len(json.dumps(message)) + 10
        self._last_due = max(self._last_due, time.perf_counter()) + nbytes / self.rate
        self._queue.append((message, nbytes, self._last_due))
        self._queued_bytes += nbytes
        if self._queued_bytes > self.LOW_WATER:
            self._low.clear()
        if self._pump is None or self._pump.done():
            self._pump = asyncio.get_running_loop().create_task(self._run())
        return nbytes

    async def drain(self) -> None:
        if self._queued_bytes > self.HIGH_WATER:
            while self._queued_bytes > self.LOW_WATER:
                await self._low.wait()

    async def send(self, message: dict) -> None:
        self.write_nowait(message)
        await self.drain()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while self._queue:
            wait = self._queue[0][2] - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.perf_counter()
            while self._queue and self._queue[0][2] <= now:
                message, nbytes, _ = self._queue.popleft()
                self._queued_bytes -= nbytes
                loop.create_task(self.deliver(message))
            if self._queued_bytes <= self.LOW_WATER:
                self._low.set()

    def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()


# --- the nodes -----------------------------------------------------------------


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _p2p(node_id: str, peers: Dict[str, Any]) -> P2PManager:
    p2p = P2PManager.__new__(P2PManager)
    p2p.node_id = node_id
    p2p.peers = peers
    return p2p


def _settings(values: Dict[str, str]):
    return SimpleNamespace(get=lambda section, key, default=None: values.get(key, default))


class Receiver:
    """The receiving node: allows the sender's files and accepts each offer as a person would.

    A chat probe's wait is kept in `chat_waits`, or, with `report=True`, sent
    back to the sender as BENCH_CHAT_WAIT (the receiver in its own process).
    """

    def __init__(self, root: Path, settings: Dict[str, str], report: bool = False):
        self.peers: Dict[str, Any] = {}
        self.p2p = _p2p(RECEIVER, self.peers)
        self.manager = FileTransferManager(
            p2p_manager=self.p2p,
            firewall=SimpleNamespace(rules={
                "file_transfer": {"nodes": {SENDER: {"file_transfer.allow": "allow"}}}}),
            settings=_settings(settings), local_api=_Api(), storage_base_path=root / "b")
        self.service = SimpleNamespace(
            file_transfer_manager=self.manager, p2p_manager=self.p2p,
            peer_metadata={}, local_api=self.manager.local_api)
        self.manager.service = self.service
        self.report = report
        self.chat_waits: List[float] = []
        self.offers: List[dict] = []
        self.errors: List[str] = []

    async def dispatch(self, message: dict) -> None:
        command, payload = message.get("command"), message.get("payload") or {}
        try:
            if command == "FILE_OFFER":
                self.offers.append(payload)
                await FileOfferHandler(self.service).handle(SENDER, payload)
                await self._accept(payload["transfer_id"])
            elif command == "FILE_CHUNK":
                await self.manager.handle_file_chunk(SENDER, payload)
            elif command == "FILE_CANCEL":
                await self.manager.handle_file_cancel(SENDER, payload)
            elif command == "SEND_TEXT":
                wait = time.perf_counter() - payload["sent_at"]
                self.chat_waits.append(wait)
                if self.report:
                    await self.p2p.send_message_to_peer(SENDER, {
                        "command": "BENCH_CHAT_WAIT", "payload": {"wait": wait}})
        except Exception as e:  # noqa: BLE001 - reported by the caller
            self.errors.append(f"{command} at receiver: {type(e).__name__}: {e}")

    async def _accept(self, transfer_id: str) -> None:
        """What the person's Accept does (P2PCoordinator.accept_file_transfer)."""
        transfer = self.manager.active_transfers.get(transfer_id)
        if transfer is None or transfer.status.value != "pending":
            return  # refused, or accepted by the handler itself
        mark = getattr(self.manager, "mark_answered", None)
        if mark is not None:
            mark(transfer)
        transfer.awaiting_permission = False
        await self.p2p.send_message_to_peer(SENDER, {
            "command": "FILE_ACCEPT", "payload": {"transfer_id": transfer_id}})
        self.manager.begin_receiving(transfer_id)


class Sender:
    """The sending node, and the measurement: offer to FILE_COMPLETE, chat waits on the way."""

    def __init__(self, root: Path, settings: Dict[str, str]):
        self.peers: Dict[str, Any] = {}
        self.p2p = _p2p(SENDER, self.peers)
        self.manager = FileTransferManager(
            p2p_manager=self.p2p, firewall=SimpleNamespace(rules={}),
            settings=_settings(settings), local_api=_Api(), storage_base_path=root / "a")
        self.manager.service = SimpleNamespace(peer_metadata={})
        self.completed: Dict[str, float] = {}
        self.completion = asyncio.Event()
        self.chat_waits: List[float] = []
        self.errors: List[str] = []

    async def dispatch(self, message: dict) -> None:
        command, payload = message.get("command"), message.get("payload") or {}
        try:
            if command == "FILE_ACCEPT":
                await self.manager.handle_file_accept(RECEIVER, payload["transfer_id"])
            elif command == "FILE_COMPLETE":
                await self.manager.handle_file_complete(RECEIVER, payload)
                self.completed[payload["transfer_id"]] = time.perf_counter()
                self.completion.set()
            elif command == "FILE_CHUNK_RETRY":
                await self.manager.handle_file_chunk_retry(RECEIVER, payload)
            elif command == "FILE_CANCEL":
                await self.manager.handle_file_cancel(RECEIVER, payload)
                self.errors.append(f"cancelled by receiver: {payload.get('reason')}")
                self.completion.set()
            elif command == "BENCH_CHAT_WAIT":
                self.chat_waits.append(payload["wait"])
        except Exception as e:  # noqa: BLE001 - reported by the caller
            self.errors.append(f"{command} at sender: {type(e).__name__}: {e}")

    async def chat_probe(self, stop: asyncio.Event) -> None:
        """A chat frame on the priority lane every CHAT_PROBE_INTERVAL_S until `stop`."""
        n = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), CHAT_PROBE_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            n += 1
            await self.p2p.send_message_to_peer(RECEIVER, {
                "command": "SEND_TEXT", "payload": {"text": f"probe {n}", "sent_at": time.perf_counter()}})

    async def send(self, path: Path, timeout_s: float, chat_waits: List[float],
                   offers: Callable[[], List[dict]] = lambda: []) -> dict:
        """Send one file; the time from the offer to FILE_COMPLETE, and the chat waits."""
        self.completion.clear()
        chat_waits.clear()
        errors_before = len(self.errors)
        stop = asyncio.Event()
        prober = asyncio.get_running_loop().create_task(self.chat_probe(stop))
        started = time.perf_counter()
        transfer_id = await self.manager.send_file(RECEIVER, path)
        try:
            await asyncio.wait_for(self.completion.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass
        done = self.completed.get(transfer_id)
        stop.set()
        await prober
        await asyncio.sleep(0.3)  # the last probe's answer
        size = path.stat().st_size
        elapsed = (done - started) if done else None
        transfer_offer = next((o for o in offers() if o.get("transfer_id") == transfer_id), {})
        return {
            "size_bytes": size,
            "seconds": elapsed,
            "mb_per_s": size / elapsed / MB if elapsed else None,
            "chunk_size": transfer_offer.get("chunk_size"),
            "chunks": len(transfer_offer.get("chunk_hashes") or []),
            "chat_waits": list(chat_waits),
            "errors": self.errors[errors_before:],
            "timed_out": done is None,
        }


# --- the loopback TLS link, receiver in its own process ----------------------------


def _self_signed(directory: Path) -> tuple:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bench")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "bench.crt", directory / "bench.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cert_file, key_file


async def _read_into(peer: PeerConnection, dispatch) -> None:
    """Each frame as its own task, as P2PManager._listen_to_peer does."""
    loop = asyncio.get_running_loop()
    while True:
        message = await peer.read()
        if message is None:
            return
        loop.create_task(dispatch(message))


async def _serve(root: str, settings: Dict[str, str], ports) -> None:
    root_path = Path(root)
    cert_file, key_file = _self_signed(root_path)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    receiver = Receiver(root_path, settings, report=True)
    finished = asyncio.Event()

    async def on_connect(reader, writer):
        peer = PeerConnection(SENDER, reader, writer, direction="in")
        receiver.peers[SENDER] = peer
        await _read_into(peer, receiver.dispatch)
        finished.set()

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0, ssl=ctx)
    ports.put(server.sockets[0].getsockname()[1])
    await finished.wait()
    server.close()


def serve_receiver(root: str, settings: Dict[str, str], ports) -> None:
    """The child process: one receiver behind a TLS listener, until the sender hangs up."""
    asyncio.run(_serve(root, settings, ports))


async def loop_load(stop: asyncio.Event) -> None:
    """LOAD_TASKS tasks that each hold the loop LOAD_SLICE_S per turn, until `stop`."""
    async def busy():
        while not stop.is_set():
            time.sleep(LOAD_SLICE_S)
            await asyncio.sleep(0)
    await asyncio.gather(*(busy() for _ in range(LOAD_TASKS)))


def make_file(directory: Path, size: int, name: str) -> Path:
    path = directory / name
    block = os.urandom(1 << 20)
    with open(path, "wb") as f:
        left = size
        while left > 0:
            f.write(block[:min(left, len(block))])
            left -= len(block)
    return path


def frame_cost(chunk_sizes=(64 * 1024, 1024 * 1024, 4 * 1024 * 1024), repeat: int = 5) -> None:
    """What one FILE_CHUNK costs a receiver of any build, on its event loop.

    The reader parses the frame (bytes -> str -> json.loads) before the next
    frame, a chat line included, can be read; the chunk's task then decodes
    base64 and checks the CRC. Times are the best of `repeat`; memory is the
    peak traced while one frame is handled (the frame bytes count once).
    """
    import base64
    import tracemalloc
    import zlib
    from dpc_protocol.protocol import encode_message
    for size in chunk_sizes:
        data = os.urandom(size)
        frame = encode_message({"command": "FILE_CHUNK", "payload": {
            "transfer_id": "t", "chunk_index": 0, "total_chunks": 1,
            "data": base64.b64encode(data).decode()}})
        body = frame[10:]
        parse, decode = [], []
        for _ in range(repeat):
            t0 = time.perf_counter()
            message = json.loads(body.decode())
            t1 = time.perf_counter()
            chunk = base64.b64decode(message["payload"]["data"])
            zlib.crc32(chunk)
            t2 = time.perf_counter()
            parse.append(t1 - t0)
            decode.append(t2 - t1)
        tracemalloc.start()
        message = json.loads(body.decode())
        chunk = base64.b64decode(message["payload"]["data"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del message, chunk
        print(f"  chunk {size // 1024:>5} KiB: frame {len(frame) / MB:.2f} MB; parse {min(parse) * 1000:.1f} ms "
              f"(blocks the reader), decode+CRC {min(decode) * 1000:.1f} ms; peak {peak / MB:.1f} MB "
              f"beyond the frame's {len(body) / MB:.1f} MB", flush=True)


def _ms(values: List[float]) -> str:
    if not values:
        return "no probe"
    return (f"p50 {statistics.median(values) * 1000:.0f} ms, max {max(values) * 1000:.0f} ms "
            f"({len(values)} probes)")


async def run(sizes_mb: List[float], link_rate_mb: Optional[float], load: bool,
              timeout_s: float, settings: Dict[str, str]) -> List[dict]:
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outbox = root / "outbox"
        outbox.mkdir()
        sender = Sender(root, settings)
        child = None
        reader_task = None
        throttled = []
        if link_rate_mb:
            receiver = Receiver(root, settings)
            forward = ThrottledLink(RECEIVER, link_rate_mb * MB, receiver.dispatch)
            back = ThrottledLink(SENDER, link_rate_mb * MB, sender.dispatch)
            sender.peers[RECEIVER], receiver.peers[SENDER] = forward, back
            throttled = [forward, back]
            chat_waits, offers = receiver.chat_waits, (lambda: receiver.offers)
        else:
            ctx = multiprocessing.get_context("spawn")
            ports = ctx.Queue()
            (root / "b").mkdir()
            child = ctx.Process(target=serve_receiver, args=(str(root / "b"), settings, ports), daemon=True)
            child.start()
            port = await asyncio.get_running_loop().run_in_executor(None, ports.get, True, 60)
            client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx)
            peer = PeerConnection(RECEIVER, reader, writer, direction="out")
            sender.peers[RECEIVER] = peer
            reader_task = asyncio.get_running_loop().create_task(_read_into(peer, sender.dispatch))
            chat_waits, offers = sender.chat_waits, None
            seen_offers: List[dict] = []
            real_send = sender.p2p.send_message_to_peer

            async def noting_offers(node_id, message):  # the offer is the sender's own, noted here
                if message.get("command") == "FILE_OFFER":
                    seen_offers.append(message["payload"])
                return await real_send(node_id, message)
            sender.p2p.send_message_to_peer = noting_offers
            offers = lambda: seen_offers  # noqa: E731
        stop_load = asyncio.Event()
        loader = asyncio.get_running_loop().create_task(loop_load(stop_load)) if load else None
        try:
            for size_mb in sizes_mb:
                path = make_file(outbox, int(size_mb * MB), f"f{size_mb:g}MB.bin")
                row = await sender.send(path, timeout_s, chat_waits, offers)
                row["label"] = f"{size_mb:g} MB"
                rows.append(row)
                path.unlink()
                print(f"  {row['label']:>8}: "
                      + (f"{row['mb_per_s']:.2f} MB/s in {row['seconds']:.2f} s"
                         if row["seconds"] else f"did not finish in {timeout_s:.0f} s")
                      + f", chunk {row['chunk_size']}, {row['chunks']} chunks, chat wait {_ms(row['chat_waits'])}"
                      + (f", errors {row['errors']}" if row["errors"] else ""), flush=True)
        finally:
            stop_load.set()
            if loader is not None:
                await loader
            for link in throttled:
                link.close()
            if reader_task is not None:
                sender.peers[RECEIVER].writer.close()
                reader_task.cancel()
            if child is not None:
                child.join(10)
                if child.is_alive():
                    child.terminate()
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sizes", nargs="+", type=float, default=[4, 100, 300], help="file sizes in MB")
    parser.add_argument("--link-rate", type=float, default=None,
                        help="MB/s of an in-process throttled link instead of loopback TLS")
    parser.add_argument("--load", action="store_true", help="block the sender's event loop as a busy node would")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds per file before giving up")
    parser.add_argument("--chunk-size", default=None, help="[file_transfer] chunk_size for both nodes")
    parser.add_argument("--log", action="store_true", help="print the sender's upload summary lines")
    parser.add_argument("--frame-cost", action="store_true",
                        help="only measure what one FILE_CHUNK of 64 KiB, 1 MiB, 4 MiB costs a receiver")
    args = parser.parse_args(argv)
    if args.log:
        import logging
        logging.basicConfig(level=logging.WARNING, format="    log: %(message)s")
        logging.getLogger("dpc_client_core.managers.file_transfer_manager").setLevel(logging.INFO)
        logging.getLogger("dpc_client_core.managers.file_transfer_manager").addFilter(
            lambda r: r.getMessage().startswith("Upload "))
    if args.frame_cost:
        print(f"python {sys.version.split()[0]} on {sys.platform}: one FILE_CHUNK at the receiver")
        frame_cost()
        return 0
    settings = {}
    if args.chunk_size is not None:
        settings["chunk_size"] = args.chunk_size
    import dpc_client_core
    print(f"dpc_client_core from {Path(dpc_client_core.__file__).parent}")
    print(f"python {sys.version.split()[0]} on {sys.platform}; link: "
          + (f"in-process, throttled to {args.link_rate} MB/s" if args.link_rate
             else "loopback TLS, receiver in its own process")
          + ("; sender loop loaded" if args.load else "; loop idle"), flush=True)
    rows = asyncio.run(run(args.sizes, args.link_rate, args.load, args.timeout, settings))
    return 0 if all(not r["timed_out"] and not r["errors"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
