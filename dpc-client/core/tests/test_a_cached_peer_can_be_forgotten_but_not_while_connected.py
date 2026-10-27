"""The sidebar's cached-peers list can forget one peer or all of them.

`remove_cached_peer` and `clear_peer_cache` change the peer cache and nothing
else. A peer connected now is never removed: the connection writes it back into
the cache at once, so a removal would look like it did nothing. Every cache here
lives in `tmp_path`; nothing may touch ~/.dpc.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.peer_cache import PeerCache
from dpc_client_core.service import CoreService


class _Api:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def _service(tmp_path, connected=()) -> SimpleNamespace:
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer("dpc-node-aaaa", display_name="Alice", direct_ip="203.0.113.7")
    cache.add_or_update_peer("dpc-node-bbbb", display_name="Bob", direct_ip="198.51.100.4")
    cache.add_or_update_peer("dpc-node-cccc", display_name="Carol")
    service = SimpleNamespace(
        p2p_manager=SimpleNamespace(peer_cache=cache, peers={n: object() for n in connected}),
        local_api=_Api(),
    )

    async def get_status():
        return {"cached_peers_count": len(cache.get_all_peers())}

    service.get_status = get_status
    service._push_status_after_cache_change = (
        lambda: CoreService._push_status_after_cache_change(service)
    )
    return service


def _ids_on_disk(tmp_path) -> set[str]:
    data = json.loads((tmp_path / "peer_cache.json").read_text())
    return {p["node_id"] for p in data["peers"]}


@pytest.mark.asyncio
async def test_one_peer_is_removed_from_the_cache_and_from_the_file(tmp_path):
    service = _service(tmp_path)

    answer = await CoreService.remove_cached_peer(service, "dpc-node-bbbb")

    assert answer == {"status": "success", "removed": ["dpc-node-bbbb"]}
    assert service.p2p_manager.peer_cache.get_peer("dpc-node-bbbb") is None
    assert _ids_on_disk(tmp_path) == {"dpc-node-aaaa", "dpc-node-cccc"}


@pytest.mark.asyncio
async def test_a_removal_pushes_the_status_so_the_count_in_the_ui_follows(tmp_path):
    service = _service(tmp_path)

    await CoreService.remove_cached_peer(service, "dpc-node-bbbb")

    assert service.local_api.events == [("status_update", {"cached_peers_count": 2})]


@pytest.mark.asyncio
async def test_a_peer_connected_now_is_refused_and_stays_cached(tmp_path):
    service = _service(tmp_path, connected=["dpc-node-aaaa"])

    answer = await CoreService.remove_cached_peer(service, "dpc-node-aaaa")

    assert answer == {
        "status": "error",
        "message": "Alice is connected now; it would be cached again at once",
    }
    assert service.p2p_manager.peer_cache.get_peer("dpc-node-aaaa") is not None
    assert "dpc-node-aaaa" in _ids_on_disk(tmp_path)
    assert service.local_api.events == []


@pytest.mark.asyncio
async def test_a_peer_that_is_not_cached_is_an_error_not_a_success(tmp_path):
    service = _service(tmp_path)

    answer = await CoreService.remove_cached_peer(service, "dpc-node-zzzz")

    assert answer["status"] == "error"
    assert "dpc-node-zzzz" in answer["message"]
    assert service.local_api.events == []


@pytest.mark.asyncio
async def test_clearing_the_cache_keeps_the_connected_peers_and_persists(tmp_path):
    service = _service(tmp_path, connected=["dpc-node-bbbb"])

    answer = await CoreService.clear_peer_cache(service)

    assert answer["status"] == "success"
    assert sorted(answer["removed"]) == ["dpc-node-aaaa", "dpc-node-cccc"]
    assert answer["kept_connected"] == ["dpc-node-bbbb"]
    assert _ids_on_disk(tmp_path) == {"dpc-node-bbbb"}
    # A node that restarts reads the file, not the object in memory.
    assert [p.node_id for p in PeerCache(tmp_path / "peer_cache.json").get_all_peers()] == [
        "dpc-node-bbbb"
    ]
    assert service.local_api.events == [("status_update", {"cached_peers_count": 1})]


@pytest.mark.asyncio
async def test_clearing_with_nobody_connected_empties_the_cache(tmp_path):
    service = _service(tmp_path)

    answer = await CoreService.clear_peer_cache(service)

    assert sorted(answer["removed"]) == ["dpc-node-aaaa", "dpc-node-bbbb", "dpc-node-cccc"]
    assert answer["kept_connected"] == []
    assert _ids_on_disk(tmp_path) == set()


@pytest.mark.asyncio
async def test_clearing_an_empty_cache_pushes_nothing(tmp_path):
    service = _service(tmp_path)
    service.p2p_manager.peer_cache.clear()

    answer = await CoreService.clear_peer_cache(service)

    assert answer == {"status": "success", "removed": [], "kept_connected": []}
    assert service.local_api.events == []


@pytest.mark.asyncio
async def test_the_list_marks_which_peers_are_connected_now(tmp_path):
    service = _service(tmp_path, connected=["dpc-node-cccc"])

    answer = await CoreService.list_cached_peers(service)

    assert {row["node_id"]: row["is_connected"] for row in answer["peers"]} == {
        "dpc-node-aaaa": False,
        "dpc-node-bbbb": False,
        "dpc-node-cccc": True,
    }


def test_the_ui_may_call_both():
    # The local API rejects anything not on its allowlist; a method alone is not enough.
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    assert "remove_cached_peer" in ALLOWED_COMMANDS
    assert "clear_peer_cache" in ALLOWED_COMMANDS
