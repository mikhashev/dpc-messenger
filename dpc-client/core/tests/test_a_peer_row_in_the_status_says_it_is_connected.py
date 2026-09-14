"""Every `peer_info` row in `get_status` is a live connection, and says so.

`peer_info` is built from `p2p_manager.peers` — the connections that exist —
so a row there is a connected peer by construction. The rows carried
`node_id`, `name` and `strategy_used` and nothing about the connection, while
the Inference Sharing tab reads `is_connected` off them
(`inferenceSharing.ts`, `peersFrom`): the badge was false for every caller,
always. The field is now on the row, and this test is what keeps it there.

`display_name` is deliberately *not* added: the same reader takes
`peer.display_name || peer.name`, and `name` is already carried, so a second
spelling of one value would be two places to keep true.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.service import CoreService


def _status_stub(peers: dict, names: dict | None = None) -> SimpleNamespace:
    """The narrowest object `get_status` can run against."""
    return SimpleNamespace(
        hub_client=SimpleNamespace(websocket=None),
        p2p_manager=SimpleNamespace(
            peers=peers,
            node_id="dpc-node-0000000000000000",
            get_external_ips=lambda: [],
            peer_cache=SimpleNamespace(get_all_peers=lambda: []),
        ),
        peer_metadata=names or {},
        llm_manager=SimpleNamespace(get_active_model_name=lambda: "m"),
        _get_local_ips=lambda: [],
        settings=SimpleNamespace(get_p2p_listen_port=lambda: 8888),
        _external_ip=None,
        _is_global_ipv6=lambda ip: False,
        connection_status=SimpleNamespace(
            get_operation_mode=lambda: SimpleNamespace(value="online"),
            get_status_message=lambda: "ok",
            get_available_features=lambda: [],
        ),
        connection_orchestrator=None,
    )


@pytest.mark.asyncio
async def test_a_peer_row_carries_the_connection_it_was_built_from():
    stub = _status_stub(
        {"dpc-node-alice": SimpleNamespace(strategy_used="ipv4_direct")},
        {"dpc-node-alice": {"name": "Alice"}},
    )

    status = await CoreService.get_status(stub)

    (row,) = status["peer_info"]
    assert row["node_id"] == "dpc-node-alice"
    assert row["name"] == "Alice"
    assert row["is_connected"] is True, (
        "the row exists because the connection does; a reader must not have to "
        "infer that from the list it arrived in"
    )


@pytest.mark.asyncio
async def test_no_peers_means_no_rows_rather_than_a_row_saying_false():
    """The absent-peer baseline: the field is never a *dis*connected claim."""
    status = await CoreService.get_status(_status_stub({}))

    assert status["peer_info"] == []
    assert status["p2p_peers"] == []
