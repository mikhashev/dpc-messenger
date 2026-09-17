"""A peer that reaches us and that we cannot reach is not worth redialling.

Every disconnect from a roster member scheduled five attempts. In a hub-and-
spoke topology a dial from the hub to a spoke cannot succeed — the spoke is
behind a firewall, or the address held for it is not its listener — so those
five were five guaranteed misses, repeated on every drop.

Who placed the last connection that worked answers it, and answers it again
when the topology changes: the alternative, a flag for "an outbound dial once
succeeded", never clears.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.service import CoreService

PEER = "dpc-node-" + "b" * 32


def _service(direction, cached=True):
    peer = SimpleNamespace(last_connection_direction=direction) if cached else None
    return SimpleNamespace(
        p2p_manager=SimpleNamespace(
            peer_cache=SimpleNamespace(get_peer=lambda nid: peer),
        ),
    )


def test_a_peer_that_only_ever_dialled_us_is_left_alone():
    assert CoreService._worth_dialling(_service("in"), PEER) is False


def test_a_peer_we_reached_last_is_dialled_again():
    assert CoreService._worth_dialling(_service("out"), PEER) is True


def test_an_unknown_peer_is_tried_rather_than_written_off():
    """Otherwise the rule would only ever learn from failures."""
    assert CoreService._worth_dialling(_service(None), PEER) is True


def test_a_peer_absent_from_the_cache_is_tried():
    assert CoreService._worth_dialling(_service(None, cached=False), PEER) is True


def test_it_survives_a_service_without_a_cache():
    bare = SimpleNamespace(p2p_manager=SimpleNamespace())
    assert CoreService._worth_dialling(bare, PEER) is True


def test_the_decision_reverses_when_we_reach_the_peer_once():
    """Self-correcting: the same peer, after one successful outbound dial."""
    service = _service("in")
    assert CoreService._worth_dialling(service, PEER) is False

    service.p2p_manager.peer_cache.get_peer = (
        lambda nid: SimpleNamespace(last_connection_direction="out")
    )
    assert CoreService._worth_dialling(service, PEER) is True


# --------------------------------------------------------------------------
# The two halves the helper alone does not cover: writing it, and using it
# --------------------------------------------------------------------------

def test_the_cache_records_which_way_the_connection_went(tmp_path):
    from dpc_client_core.peer_cache import PeerCache

    cache = PeerCache(cache_file=tmp_path / "peers.json")

    cache.add_or_update_peer(node_id=PEER, direct_ip="10.0.0.1", direction="in")
    assert cache.get_peer(PEER).last_connection_direction == "in"

    cache.add_or_update_peer(node_id=PEER, direct_ip="10.0.0.1", direction="out")
    assert cache.get_peer(PEER).last_connection_direction == "out"


def test_a_caller_that_does_not_know_the_direction_does_not_erase_it(tmp_path):
    from dpc_client_core.peer_cache import PeerCache

    cache = PeerCache(cache_file=tmp_path / "peers.json")
    cache.add_or_update_peer(node_id=PEER, direct_ip="10.0.0.1", direction="out")

    cache.add_or_update_peer(node_id=PEER, display_name="renamed")

    assert cache.get_peer(PEER).last_connection_direction == "out"


def test_the_disconnect_handler_actually_asks():
    """The helper is worth nothing if the path that schedules the redial
    never consults it."""
    import inspect

    source = inspect.getsource(CoreService._handle_peer_disconnected)

    assert "_worth_dialling" in source
    schedule = source.index("_auto_reconnect_peer(peer_id)")
    assert "_worth_dialling" in source[:schedule]
