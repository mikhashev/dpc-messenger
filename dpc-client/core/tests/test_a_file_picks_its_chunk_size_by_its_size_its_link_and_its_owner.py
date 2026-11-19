"""A file's chunk size is picked by its size, its link and its owner, once, before it is hashed.

Every file used to go in 64 KB chunks, whatever its size: an 8.3 GB file was
127 099 FILE_CHUNK messages and moved at about 0.75 MB/s on the LAN (card
THE-SENDER-USES-ONE-64-KB-CHUNK-FOR-EVERY-FILE-SO-A-LARGE-FILE-COSTS-A-MESSAGE-PER-64-KB).
ADR-047 phase 0, accepted 2026-11-19 with Q2 decided by Mike as "scaled by file
size and by the link's measured speed": the sender picks the size per file,
the receiver already writes at index * the offer's chunk_size. The CRC list,
the offer and the send loop must all use the one size, so it is decided
before the fingerprint (managers/chunk_window.py).

Pure functions and the real FileTransferManager with a collecting wire;
nothing touches ~/.dpc; the same on every OS.
"""

import math
import zlib
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import chunk_window as cw
from dpc_client_core.managers.chunk_window import (
    KiB, MiB, GiB, PeerLinkRates, choose_chunk_size, parse_chunk_size_setting,
)
from dpc_client_core.managers.file_transfer_manager import FileTransferManager

US = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
SLOW = "dpc-node-" + "c" * 32


# --- by file size: ADR-047's table ------------------------------------------------


@pytest.mark.parametrize("size, chunk", [
    (0, 64 * KiB),
    (1, 64 * KiB),
    (100 * MiB, 64 * KiB),          # the boundary is inclusive
    (100 * MiB + 1, 1 * MiB),
    (1 * GiB, 1 * MiB),
    (1 * GiB + 1, 4 * MiB),
    (47_039_860_096, 4 * MiB),      # the shard of 2026-10-20
])
def test_on_a_measured_fast_direct_link_the_size_follows_the_table(size, chunk):
    fast = 100e6  # a LAN rate: the rate cap is above every table entry
    assert choose_chunk_size(size, "direct_tls", fast) == chunk


def test_with_no_rate_measured_a_large_file_waits_at_one_mib():
    """The first large file to a peer: 1 MiB, the ADR's size, until a rate is known."""
    assert choose_chunk_size(8_329_503_582, "direct_tls", None) == 1 * MiB
    assert choose_chunk_size(500 * MiB, "direct_tls", None) == 1 * MiB
    assert choose_chunk_size(4 * MiB, "direct_tls", None) == 64 * KiB


# --- by tier ------------------------------------------------------------------------


@pytest.mark.parametrize("tier, chunk", [
    ("direct_tls", 4 * MiB),
    ("webrtc", 1 * MiB),
    ("udp_dtls", 64 * KiB),
    ("unknown", 64 * KiB),
    (None, 64 * KiB),
])
def test_the_connection_tier_caps_the_size(tier, chunk):
    assert choose_chunk_size(8 * GiB, tier, 100e6) == chunk


# --- by speed -----------------------------------------------------------------------


@pytest.mark.parametrize("rate, chunk", [
    (100e6, 4 * MiB),      # LAN
    (1.1e6, 128 * KiB),    # the VPN of 2026-10-28: 0.2 s holds 220 KB
    (0.75e6, 128 * KiB),   # the LAN figure of the same day
    (300e3, 32 * KiB),
    (50e3, 16 * KiB),      # tens of KB/s: never below the floor
    (5e3, 16 * KiB),
])
def test_a_slow_link_gets_a_smaller_chunk_so_one_frame_holds_it_about_the_slo(rate, chunk):
    got = choose_chunk_size(8 * GiB // 64, "direct_tls", rate)  # 128 MiB: the table says 1 MiB
    assert got == min(chunk, 1 * MiB)
    assert choose_chunk_size(4 * MiB, "direct_tls", rate) == min(chunk, 64 * KiB)
    if got > cw.MIN_CHUNK_BYTES:
        assert got / rate <= cw.CHAT_WAIT_SLO_S


def test_the_rate_cap_never_lengthens_the_crc_list_past_its_bound():
    size = 47_039_860_096
    chunk = choose_chunk_size(size, "direct_tls", 50e3)  # a crawl
    assert math.ceil(size / chunk) <= cw.MAX_CHUNKS_PER_OFFER


# --- the owner's setting ------------------------------------------------------------


@pytest.mark.parametrize("raw, pinned", [
    ("auto", None), ("AUTO", None), ("", None), (None, None),
    ("65536", None),                 # the old default in every config.ini: no choice
    ("64KiB", 64 * KiB), ("1MiB", 1 * MiB), ("4 mib", 4 * MiB), ("2m", 2 * MiB),
    ("131072", 128 * KiB), ("65537", 65537),
    ("100", 1 * KiB),                # clamped up
    ("1024MiB", cw.MAX_CHUNK_BYTES),  # clamped down, a third of MAX_FRAME_BYTES once framed
    ("lots", None),
])
def test_the_setting_is_adaptive_unless_the_owner_wrote_a_size(raw, pinned):
    assert parse_chunk_size_setting(raw) == pinned


def test_a_pinned_size_wins_over_size_tier_and_speed():
    assert choose_chunk_size(8 * GiB, "udp_dtls", 5e3, pinned=2 * MiB) == 2 * MiB


def _manager(tmp_path, settings=None, peers=None):
    sent = []

    class _Wire:
        node_id = US

        async def send_message_to_peer(self, node_id, message):
            sent.append((node_id, message))

    wire = _Wire()
    wire.peers = peers or {}
    values = dict(settings or {})
    manager = FileTransferManager(
        p2p_manager=wire, firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda s, k, d=None: values.get(k, d)),
        local_api=None, storage_base_path=tmp_path,
    )
    return manager, sent


def test_the_manager_reads_the_old_default_as_adaptive_and_a_size_as_pinned(tmp_path):
    adaptive, _ = _manager(tmp_path, {"chunk_size": "65536"})
    assert adaptive.chunk_size_pinned is None and adaptive.chunk_size == 64 * KiB
    pinned, _ = _manager(tmp_path, {"chunk_size": "1MiB"})
    assert pinned.chunk_size_pinned == 1 * MiB
    assert pinned.chunk_size_for(10, [PEER]) == 1 * MiB


def test_chunk_delay_is_zero_by_default(tmp_path):
    manager, _ = _manager(tmp_path)
    assert manager.chunk_delay == 0


def test_a_fan_out_takes_the_smallest_size_any_member_would_get(tmp_path):
    peers = {PEER: SimpleNamespace(connection_type="direct_tls"),
             SLOW: SimpleNamespace(connection_type="direct_tls")}
    manager, _ = _manager(tmp_path, peers=peers)
    manager.link_rates.remember(PEER, "direct_tls", 100e6)
    manager.link_rates.remember(SLOW, "direct_tls", 300e3)
    assert manager.chunk_size_for(2 * GiB, [PEER]) == 4 * MiB
    assert manager.chunk_size_for(2 * GiB, [PEER, SLOW]) == 32 * KiB


def test_a_remembered_rate_is_forgotten_with_time_or_a_tier_change():
    now = [0.0]
    rates = PeerLinkRates(clock=lambda: now[0])
    rates.remember(PEER, "direct_tls", 1e6)
    assert rates.get(PEER, "direct_tls") == 1e6
    assert rates.get(PEER, "webrtc") is None
    now[0] = cw.LINK_RATE_MEMORY_S + 1
    assert rates.get(PEER, "direct_tls") is None


# --- one size for the CRCs, the offer and the loop ----------------------------------


@pytest.mark.asyncio
async def test_the_offer_carries_the_chosen_size_and_a_crc_list_cut_by_it(tmp_path):
    manager, sent = _manager(tmp_path, peers={PEER: SimpleNamespace(connection_type="direct_tls")})
    manager.link_rates.remember(PEER, "direct_tls", 100e6)
    path = tmp_path / "big.bin"
    size = 100 * MiB + 12345  # just over the first boundary: 1 MiB chunks
    with open(path, "wb") as f:
        f.truncate(size)

    await manager.send_file(PEER, path)

    (offer,) = [m["payload"] for _, m in sent if m["command"] == "FILE_OFFER"]
    assert offer["chunk_size"] == 1 * MiB
    assert len(offer["chunk_hashes"]) == math.ceil(size / (1 * MiB)) == 101
    transfer = manager.active_transfers[offer["transfer_id"]]
    assert transfer.chunk_size == 1 * MiB and transfer.total_chunks == 101
    zero_mib = f"{zlib.crc32(bytes(1 * MiB)) & 0xffffffff:08x}"
    assert offer["chunk_hashes"][0] == zero_mib
    await manager.cancel_transfer(offer["transfer_id"])


@pytest.mark.asyncio
async def test_a_fan_outs_precomputed_pair_is_used_with_its_own_size(tmp_path):
    manager, sent = _manager(tmp_path, peers={PEER: SimpleNamespace(connection_type="direct_tls")})
    path = tmp_path / "f.bin"
    path.write_bytes(b"z" * (300 * KiB + 7))
    size = 128 * KiB
    file_hash, crcs = await manager.compute_file_fingerprint(path, chunk_size=size)
    assert len(crcs) == 3

    await manager.send_file(PEER, path, file_hash=file_hash, chunk_hashes=crcs, chunk_size=size)

    (offer,) = [m["payload"] for _, m in sent if m["command"] == "FILE_OFFER"]
    assert offer["chunk_size"] == size and offer["chunk_hashes"] == crcs
    await manager.cancel_transfer(offer["transfer_id"])


@pytest.mark.asyncio
async def test_a_pair_hashed_at_another_size_is_hashed_again_not_offered_wrong(tmp_path):
    manager, sent = _manager(tmp_path, peers={PEER: SimpleNamespace(connection_type="direct_tls")})
    path = tmp_path / "f.bin"
    path.write_bytes(b"z" * (300 * KiB + 7))
    file_hash, crcs = await manager.compute_file_fingerprint(path, chunk_size=128 * KiB)

    await manager.send_file(PEER, path, file_hash=file_hash, chunk_hashes=crcs, chunk_size=64 * KiB)

    (offer,) = [m["payload"] for _, m in sent if m["command"] == "FILE_OFFER"]
    assert offer["chunk_size"] == 64 * KiB and len(offer["chunk_hashes"]) == 5
    await manager.cancel_transfer(offer["transfer_id"])
