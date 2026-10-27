"""`list_cached_peers` answers "which" where `get_status` only says "how many".

The sidebar's "N cached peers" line opens a list the owner can copy names and
node ids from. The cache behind it also holds the address each peer was last
reached on; the list must carry names and ids and never that address, so the
rows are checked by their exact keys and the addresses are searched for in the
whole answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.peer_cache import PeerCache
from dpc_client_core.service import CoreService


def _service_over(cache: PeerCache) -> SimpleNamespace:
    return SimpleNamespace(p2p_manager=SimpleNamespace(peer_cache=cache))


def _cache(tmp_path) -> PeerCache:
    """A cache in a temp dir; nothing here may touch ~/.dpc."""
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer(
        "dpc-node-aaaa", display_name="Alice", direct_ip="203.0.113.7", direct_port=9001
    )
    cache.add_or_update_peer(
        "dpc-node-bbbb", display_name="Bob", direct_ip="198.51.100.4", direct_port=8888
    )
    cache.add_or_update_peer("dpc-node-cccc", direct_ip="2001:db8::5")
    # Fix the order the test relies on instead of racing the wall clock.
    cache.get_peer("dpc-node-aaaa").last_seen = "2026-10-20T10:00:00+00:00"
    cache.get_peer("dpc-node-bbbb").last_seen = "2026-10-26T10:00:00+00:00"
    cache.get_peer("dpc-node-cccc").last_seen = None
    return cache


@pytest.mark.asyncio
async def test_the_answer_holds_names_and_ids_and_no_address(tmp_path):
    answer = await CoreService.list_cached_peers(_service_over(_cache(tmp_path)))

    assert answer["status"] == "success"
    assert all(set(row) == {"node_id", "display_name", "last_seen"} for row in answer["peers"])
    wire = json.dumps(answer)
    for address in ("203.0.113.7", "198.51.100.4", "2001:db8::5", "9001"):
        assert address not in wire, f"{address} left the node in a list meant for copying"


@pytest.mark.asyncio
async def test_the_newest_peer_comes_first_and_one_never_seen_comes_last(tmp_path):
    answer = await CoreService.list_cached_peers(_service_over(_cache(tmp_path)))

    assert [row["node_id"] for row in answer["peers"]] == [
        "dpc-node-bbbb",
        "dpc-node-aaaa",
        "dpc-node-cccc",
    ]
    assert [row["display_name"] for row in answer["peers"]] == ["Bob", "Alice", None]


@pytest.mark.asyncio
async def test_an_empty_cache_is_an_empty_list_not_an_error(tmp_path):
    answer = await CoreService.list_cached_peers(
        _service_over(PeerCache(tmp_path / "peer_cache.json"))
    )

    assert answer == {"status": "success", "peers": []}


@pytest.mark.asyncio
async def test_a_cache_that_cannot_be_read_answers_with_an_error_and_no_rows():
    broken = SimpleNamespace(
        p2p_manager=SimpleNamespace(
            peer_cache=SimpleNamespace(get_all_peers=lambda: 1 / 0)
        )
    )

    answer = await CoreService.list_cached_peers(broken)

    assert answer["status"] == "error"
    assert answer["peers"] == []
