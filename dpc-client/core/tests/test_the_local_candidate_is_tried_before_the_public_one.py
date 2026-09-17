"""ICE orders host candidates above server-reflexive ones; we had it reversed.

A router without hairpin NAT drops a packet sent from inside to its own public
address, so trying the external candidate first fails outright there. Where it
does work it costs a round trip and routes two machines on one LAN through the
internet.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.connection_strategies.ipv4_direct import IPv4DirectStrategy

PEER = "dpc-node-" + "b" * 32
LOCAL = "10.8.1.1:8888"
EXTERNAL = "38.180.192.51:8888"


def _endpoints(local=LOCAL, external=EXTERNAL):
    return SimpleNamespace(ipv4=SimpleNamespace(local=local, external=external, nat_type="cone"))


def _orchestrator(succeed_on=None):
    """Records every address dialled; connects only to `succeed_on`."""
    dialled = []

    async def _connect(ip, port, node_id):
        dialled.append(f"{ip}:{port}")
        if succeed_on is None or f"{ip}:{port}" != succeed_on:
            raise ConnectionError(f"nothing at {ip}:{port}")
        return SimpleNamespace(node_id=node_id)

    orchestrator = SimpleNamespace(
        p2p_manager=SimpleNamespace(connect_directly=_connect),
    )
    orchestrator.dialled = dialled
    return orchestrator


@pytest.mark.asyncio
async def test_the_local_address_is_dialled_first():
    orchestrator = _orchestrator(succeed_on=LOCAL)

    await IPv4DirectStrategy().connect(PEER, _endpoints(), orchestrator)

    assert orchestrator.dialled == [LOCAL]


@pytest.mark.asyncio
async def test_the_public_address_is_the_fallback_not_the_first_choice():
    orchestrator = _orchestrator(succeed_on=EXTERNAL)

    await IPv4DirectStrategy().connect(PEER, _endpoints(), orchestrator)

    assert orchestrator.dialled == [LOCAL, EXTERNAL]


@pytest.mark.asyncio
async def test_a_peer_with_no_public_address_is_still_reachable():
    orchestrator = _orchestrator(succeed_on=LOCAL)

    await IPv4DirectStrategy().connect(PEER, _endpoints(external=None), orchestrator)

    assert orchestrator.dialled == [LOCAL]


@pytest.mark.asyncio
async def test_both_are_tried_before_giving_up():
    orchestrator = _orchestrator(succeed_on=None)

    with pytest.raises(Exception):
        await IPv4DirectStrategy().connect(PEER, _endpoints(), orchestrator)

    assert orchestrator.dialled == [LOCAL, EXTERNAL]
