"""A blocked id gets nothing from the DHT.

Real packets over real loopback UDP sockets into a real `DHTRPCHandler`, so
the path is datagram -> `handle_rpc` -> handler -> reply. The question "is this
id blocked" is a callable asked per packet; the tests flip a set under it and
change nothing else, which is the hot-reload and Unblock case.

The id in a packet is a claim nothing proves, so this stops a client that keeps
its id; a test of a sender that lies would pass for the wrong reason and is not
written here.
"""

import asyncio
import json

import pytest
import pytest_asyncio

from dpc_client_core.dht.routing import KBucket, DHTNode, RoutingTable
from dpc_client_core.dht.rpc import DHTRPCHandler, RPCConfig

ME = "dpc-node-" + "0" * 32
BAD = "dpc-node-" + "b" * 32
FRIEND = "dpc-node-" + "c" * 32
THIRD_A = "dpc-node-" + "1" + "0" * 31
THIRD_B = "dpc-node-" + "2" + "0" * 31
SILENCE = 0.3


class _Wire(asyncio.DatagramProtocol):
    """A stranger's socket: sends datagrams, keeps what comes back."""

    def __init__(self):
        self.inbox = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.inbox.put_nowait(json.loads(data.decode("utf-8")))


class _Door:
    def __init__(self, handler, port, wire, transport):
        self.handler, self.port, self.wire, self.transport = handler, port, wire, transport

    async def ask(self, **fields):
        """Send one request; the reply, or None after the silence window."""
        fields.setdefault("rpc_id", "r1")
        self.transport.sendto(json.dumps(fields).encode("utf-8"), ("127.0.0.1", self.port))
        try:
            return await asyncio.wait_for(self.wire.inbox.get(), SILENCE)
        except asyncio.TimeoutError:
            return None


@pytest.fixture
def blocked():
    return set()


@pytest_asyncio.fixture
async def door(blocked):
    table = RoutingTable(node_id=ME, k=20)
    handler = DHTRPCHandler(
        table, RPCConfig(timeout=SILENCE, max_retries=1, rate_limit_per_ip=1000),
        is_blocked=lambda nid: nid in blocked,
    )
    await handler.start(host="127.0.0.1", port=0)
    port = handler.transport.get_extra_info("sockname")[1]
    loop = asyncio.get_running_loop()
    wire = _Wire()
    transport, _ = await loop.create_datagram_endpoint(lambda: wire, local_addr=("127.0.0.1", 0))
    yield _Door(handler, port, wire, transport)
    transport.close()
    await handler.stop()


# ----- the controls: a stranger is served -----

@pytest.mark.asyncio
async def test_a_stranger_ping_gets_a_pong_and_an_entry(door):
    reply = await door.ask(type="PING", node_id=FRIEND)

    assert reply["type"] == "PONG" and reply["node_id"] == ME
    assert door.handler.routing_table.get_node(FRIEND) is not None


@pytest.mark.asyncio
async def test_a_stranger_store_is_kept_and_answered(door):
    reply = await door.ask(type="STORE", node_id=FRIEND, key=FRIEND, value="198.51.100.7:8888")

    assert reply["type"] == "STORED" and reply["success"] is True
    assert door.handler._storage[FRIEND] == "198.51.100.7:8888"


# ----- a blocked sender gets nothing -----

@pytest.mark.asyncio
async def test_a_blocked_ping_gets_no_reply_and_no_entry(door, blocked):
    blocked.add(BAD)

    reply = await door.ask(type="PING", node_id=BAD)

    assert reply is None
    assert door.handler.routing_table.get_node(BAD) is None
    assert door.handler.routing_table.get_node_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("request_fields", [
    {"type": "FIND_NODE", "target_id": FRIEND},
    {"type": "FIND_VALUE", "key": FRIEND},
    {"type": "STORE", "key": "k", "value": "198.51.100.8:8888"},
    {"type": "DISCOVER_ENDPOINT"},
])
async def test_a_blocked_sender_gets_no_reply_to_any_request(door, blocked, request_fields):
    blocked.add(BAD)
    door.handler.routing_table.add_node(THIRD_A, "198.51.100.1", 8889)

    reply = await door.ask(node_id=BAD, **request_fields)

    assert reply is None
    assert "k" not in door.handler._storage


@pytest.mark.asyncio
async def test_a_pong_from_a_blocked_id_is_dropped_and_adds_nothing(blocked):
    """Our own PING to a node we have blocked: its PONG is dropped, the ping
    times out, and the table stays empty."""
    mine = RoutingTable(node_id=ME, k=20)
    other_id = BAD
    theirs = DHTRPCHandler(RoutingTable(node_id=other_id, k=20))
    asker = DHTRPCHandler(
        mine, RPCConfig(timeout=SILENCE, max_retries=1), is_blocked=lambda nid: nid in blocked
    )
    await theirs.start(host="127.0.0.1", port=0)
    await asker.start(host="127.0.0.1", port=0)
    try:
        port = theirs.transport.get_extra_info("sockname")[1]
        control = await asker.ping("127.0.0.1", port)
        assert control is not None and mine.get_node(BAD) is not None   # not blocked: added
        mine.remove_node(BAD)

        blocked.add(BAD)
        answer = await asker.ping("127.0.0.1", port)

        assert answer is None
        assert mine.get_node(BAD) is None
    finally:
        await asker.stop()
        await theirs.stop()


# ----- never handed out -----

@pytest.mark.asyncio
async def test_find_node_gives_a_third_node_the_blocked_id_before_and_not_after(door, blocked):
    table = door.handler.routing_table
    table.add_node(BAD, "198.51.100.2", 8889)
    table.add_node(THIRD_A, "198.51.100.3", 8889)

    before = await door.ask(type="FIND_NODE", node_id=FRIEND, target_id=BAD)
    blocked.add(BAD)
    after = await door.ask(type="FIND_NODE", node_id=FRIEND, target_id=BAD)

    assert {n["node_id"] for n in before["nodes"]} == {BAD, THIRD_A}
    assert {n["node_id"] for n in after["nodes"]} == {THIRD_A}


@pytest.mark.asyncio
async def test_find_value_miss_does_not_hand_out_the_blocked_id(door, blocked):
    door.handler.routing_table.add_node(BAD, "198.51.100.2", 8889)
    door.handler.routing_table.add_node(THIRD_A, "198.51.100.3", 8889)
    blocked.add(BAD)

    reply = await door.ask(type="FIND_VALUE", node_id=FRIEND, key=THIRD_B)

    assert reply["type"] == "NODES_FOUND"
    assert {n["node_id"] for n in reply["nodes"]} == {THIRD_A}


@pytest.mark.asyncio
async def test_a_blocked_id_does_not_squeeze_a_live_node_out_of_the_twenty(door, blocked):
    table = door.handler.routing_table
    for bucket in table.buckets:
        bucket.subnet_diversity_limit = 100     # one documentation /24 is all we may use
    ids = [f"dpc-node-{i:02x}" + "0" * 30 for i in range(1, 23)]
    for i, nid in enumerate(ids):
        assert table.add_node(nid, f"198.51.100.{i + 1}", 8889)
    blocked.add(ids[0])

    reply = await door.ask(type="FIND_NODE", node_id=FRIEND, target_id=ids[0])

    got = [n["node_id"] for n in reply["nodes"]]
    assert ids[0] not in got
    assert len(got) == 20      # 22 known, 1 blocked, 21 left, 20 handed out


@pytest.mark.asyncio
async def test_a_value_under_a_blocked_key_is_refused_and_not_served(door, blocked):
    door.handler._storage[BAD] = "198.51.100.9:8888"
    blocked.add(BAD)

    stored = await door.ask(type="STORE", node_id=FRIEND, key=BAD, value="203.0.113.5:8888")
    found = await door.ask(type="FIND_VALUE", node_id=FRIEND, key=BAD)

    assert stored["type"] == "STORED" and stored["success"] is False   # the sender is answered
    assert door.handler._storage[BAD] == "198.51.100.9:8888"            # and nothing was overwritten
    assert found["type"] == "NODES_FOUND"


# ----- hot reload, forget, self -----

@pytest.mark.asyncio
async def test_unblock_takes_effect_on_the_next_packet_without_a_restart(door, blocked):
    blocked.add(BAD)
    assert await door.ask(type="PING", node_id=BAD) is None

    blocked.discard(BAD)

    reply = await door.ask(type="PING", node_id=BAD)
    assert reply["type"] == "PONG"
    assert door.handler.routing_table.get_node(BAD) is not None


@pytest.mark.asyncio
async def test_forget_removes_the_entry_and_the_value(door):
    table = door.handler.routing_table
    table.add_node(BAD, "198.51.100.2", 8889)
    door.handler._storage[BAD] = "198.51.100.2:8888"
    door.handler._storage[FRIEND] = "198.51.100.4:8888"

    door.handler.forget(BAD)

    assert table.get_node(BAD) is None
    assert BAD not in door.handler._storage
    assert door.handler._storage[FRIEND] == "198.51.100.4:8888"


@pytest.mark.asyncio
async def test_our_own_id_is_never_refused(blocked):
    table = RoutingTable(node_id=ME, k=20)
    handler = DHTRPCHandler(table, is_blocked=lambda nid: True)

    assert handler._blocked(ME) is False
    assert handler._blocked(FRIEND) is True


@pytest.mark.asyncio
async def test_a_question_that_fails_blocks_nobody(door):
    def boom(nid):
        raise RuntimeError("rules unreadable")

    door.handler._is_blocked = boom

    reply = await door.ask(type="PING", node_id=FRIEND)
    assert reply["type"] == "PONG"


# ----- the replacement cache -----

def test_remove_also_drops_a_node_waiting_in_the_replacement_cache():
    bucket = KBucket(k=1)
    bucket.add(DHTNode(FRIEND, "198.51.100.1", 8889))
    bucket.add(DHTNode(BAD, "198.51.100.2", 8889))      # full: goes to the cache
    assert [n.node_id for n in bucket.replacement_cache] == [BAD]

    assert bucket.remove(BAD) is True
    assert list(bucket.replacement_cache) == []

    bucket.remove(FRIEND)                                # nothing left to promote
    assert len(bucket) == 0


# ----- the manager passes the question down -----

@pytest.mark.asyncio
async def test_the_dht_manager_hands_the_question_to_its_handler(blocked):
    from dpc_client_core.dht import DHTManager

    manager = DHTManager(node_id=ME, ip="192.0.2.1", port=8888, is_blocked=lambda nid: nid in blocked)
    await manager.start(host="127.0.0.1", port=0)
    try:
        port = manager.rpc_handler.transport.get_extra_info("sockname")[1]
        loop = asyncio.get_running_loop()
        wire = _Wire()
        transport, _ = await loop.create_datagram_endpoint(lambda: wire, local_addr=("127.0.0.1", 0))
        door = _Door(manager.rpc_handler, port, wire, transport)
        try:
            assert (await door.ask(type="PING", node_id=FRIEND))["type"] == "PONG"
            blocked.add(FRIEND)
            assert await door.ask(type="PING", node_id=FRIEND) is None
        finally:
            transport.close()
    finally:
        await manager.stop()


# ----- a FIND_VALUE miss on a key that is not a node id still gets an answer -----

CERT_BAD = "cert:" + BAD


@pytest.mark.asyncio
async def test_a_find_value_miss_on_a_cert_key_gets_the_nodes_close_to_its_id(door):
    """Before: xor_distance raised on "cert:..." and the asker waited out its timeout."""
    door.handler.routing_table.add_node(THIRD_A, "198.51.100.3", 8889)

    reply = await door.ask(type="FIND_VALUE", node_id=FRIEND, key="cert:" + THIRD_B)

    assert reply["type"] == "NODES_FOUND"
    assert {n["node_id"] for n in reply["nodes"]} == {THIRD_A}


@pytest.mark.asyncio
async def test_a_find_value_miss_on_a_key_with_no_node_id_in_it_gets_an_empty_list(door):
    door.handler.routing_table.add_node(THIRD_A, "198.51.100.3", 8889)

    reply = await door.ask(type="FIND_VALUE", node_id=FRIEND, key="nonsense")

    assert reply["type"] == "NODES_FOUND" and reply["nodes"] == []


@pytest.mark.asyncio
async def test_a_stored_cert_is_still_served(door):
    door.handler._storage["cert:" + THIRD_A] = "PEM"

    reply = await door.ask(type="FIND_VALUE", node_id=FRIEND, key="cert:" + THIRD_A)

    assert reply["type"] == "VALUE_FOUND" and reply["value"] == "PEM"


# ----- cert:<id> follows the id it names -----

@pytest.mark.asyncio
async def test_a_cert_under_a_blocked_id_is_refused_and_not_served(door, blocked):
    door.handler._storage[CERT_BAD] = "OLD PEM"
    blocked.add(BAD)

    stored = await door.ask(type="STORE", node_id=FRIEND, key=CERT_BAD, value="NEW PEM")
    found = await door.ask(type="FIND_VALUE", node_id=FRIEND, key=CERT_BAD)

    assert stored["type"] == "STORED" and stored["success"] is False
    assert door.handler._storage[CERT_BAD] == "OLD PEM"
    assert found["type"] == "NODES_FOUND"


@pytest.mark.asyncio
async def test_forget_drops_the_cert_stored_under_the_id_too(door):
    door.handler._storage[CERT_BAD] = "PEM"
    door.handler._storage["cert:" + FRIEND] = "PEM2"

    door.handler.forget(BAD)

    assert CERT_BAD not in door.handler._storage
    assert door.handler._storage["cert:" + FRIEND] == "PEM2"


# ----- nothing is sent to, or asked about, a blocked id -----

@pytest.fixture
def answerer_table():
    """A third node's table, naming BAD among its neighbours."""
    table = RoutingTable(node_id=FRIEND, k=20)
    table.add_node(BAD, "198.51.100.2", 8889)
    table.add_node(THIRD_A, "198.51.100.3", 8889)
    return table


@pytest.mark.asyncio
async def test_an_inbound_node_list_loses_the_blocked_id(answerer_table, blocked):
    answerer = DHTRPCHandler(answerer_table)
    asker = DHTRPCHandler(RoutingTable(node_id=ME, k=20), RPCConfig(timeout=SILENCE, max_retries=1),
                          is_blocked=lambda nid: nid in blocked)
    await answerer.start(host="127.0.0.1", port=0)
    await asker.start(host="127.0.0.1", port=0)
    try:
        port = answerer.transport.get_extra_info("sockname")[1]
        control = await asker.find_node("127.0.0.1", port, THIRD_A)
        assert {n.node_id for n in control} == {BAD, THIRD_A}
        blocked.add(BAD)

        by_find_node = await asker.find_node("127.0.0.1", port, THIRD_A)
        by_find_value = await asker.find_value("127.0.0.1", port, THIRD_B)

        assert {n.node_id for n in by_find_node} == {THIRD_A}
        assert {n.node_id for n in by_find_value["nodes"]} == {THIRD_A}
    finally:
        await asker.stop()
        await answerer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [BAD, CERT_BAD])
async def test_we_ask_nobody_for_and_store_nothing_under_a_blocked_key(blocked, key):
    asker = DHTRPCHandler(RoutingTable(node_id=ME, k=20), RPCConfig(timeout=SILENCE, max_retries=1),
                          is_blocked=lambda nid: nid in blocked)
    await asker.start(host="127.0.0.1", port=0)
    try:
        blocked.add(BAD)
        sent = asker.stats["rpcs_sent"]

        assert await asker.find_value("127.0.0.1", 9, key) is None
        assert await asker.store("127.0.0.1", 9, key, "v") is False
        assert asker.stats["rpcs_sent"] == sent
    finally:
        await asker.stop()


def _manager(blocked):
    from dpc_client_core.dht import DHTManager
    manager = DHTManager(node_id=ME, ip="192.0.2.1", port=8888, is_blocked=lambda nid: nid in blocked)
    manager.routing_table.add_node(BAD, "198.51.100.2", 8889)
    manager.routing_table.add_node(THIRD_A, "198.51.100.3", 8889)
    sent = []

    async def find_node(ip, port, target):
        sent.append(("FIND_NODE", ip))
        return []

    async def store(ip, port, key, value):
        sent.append(("STORE", ip))
        return True

    manager.rpc_handler.find_node = find_node
    manager.rpc_handler.store = store
    return manager, sent


@pytest.mark.asyncio
async def test_a_lookup_sends_nothing_to_a_blocked_id_left_in_the_table(blocked):
    """A hand-edited block list reloaded: the table was never purged."""
    manager, sent = _manager(blocked)
    blocked.add(BAD)

    found = await manager.find_node(THIRD_B)

    assert sent == [("FIND_NODE", "198.51.100.3")]
    assert BAD not in {n.node_id for n in found}


@pytest.mark.asyncio
async def test_an_announce_stores_nothing_on_a_blocked_id(blocked):
    manager, sent = _manager(blocked)
    blocked.add(BAD)

    await manager.announce()

    assert ("STORE", "198.51.100.2") not in sent
    assert ("STORE", "198.51.100.3") in sent


@pytest.mark.asyncio
async def test_a_search_for_a_blocked_peer_asks_nobody(blocked):
    manager, sent = _manager(blocked)
    blocked.add(BAD)

    assert await manager.find_peer(BAD) is None
    assert await manager.find_peer_full(BAD) is None
    assert sent == []


# ----- a blocked entry left in the table is not talked to (hand-edited block list) -----

@pytest.mark.asyncio
async def test_known_peers_hides_a_blocked_entry_and_takes_it_out_of_the_table(blocked):
    """relay discovery, relayed cert lookup and hole punching all walk this list."""
    manager, _ = _manager(blocked)
    blocked.add(BAD)

    peers = manager.get_known_peers()

    assert {p.node_id for p in peers} == {THIRD_A}
    assert manager.routing_table.get_node(BAD) is None


@pytest.mark.asyncio
async def test_a_lookup_takes_a_blocked_entry_out_of_the_table(blocked):
    manager, _ = _manager(blocked)
    blocked.add(BAD)

    await manager.find_node(THIRD_B)

    assert manager.routing_table.get_node(BAD) is None


@pytest.mark.asyncio
async def test_a_blocked_packet_takes_the_sender_out_of_the_table(door, blocked):
    door.handler.routing_table.add_node(BAD, "198.51.100.2", 8889)
    blocked.add(BAD)

    assert await door.ask(type="PING", node_id=BAD) is None

    assert door.handler.routing_table.get_node(BAD) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["relay:" + BAD])
async def test_a_relay_key_follows_the_id_it_names(door, blocked, key):
    door.handler._storage[key] = "ANNOUNCEMENT"
    blocked.add(BAD)

    stored = await door.ask(type="STORE", node_id=FRIEND, key=key, value="NEW")
    found = await door.ask(type="FIND_VALUE", node_id=FRIEND, key=key)
    door.handler.forget(BAD)

    assert stored["success"] is False
    assert found["type"] == "NODES_FOUND"
    assert key not in door.handler._storage


# ----- the block is checked before the rate limit -----

@pytest.mark.asyncio
async def test_blocked_packets_do_not_spend_the_rate_budget_of_a_shared_address(blocked):
    handler = DHTRPCHandler(
        RoutingTable(node_id=ME, k=20), RPCConfig(rate_limit_per_ip=3),
        is_blocked=lambda nid: nid in blocked,
    )
    await handler.start(host="127.0.0.1", port=0)
    port = handler.transport.get_extra_info("sockname")[1]
    wire = _Wire()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: wire, local_addr=("127.0.0.1", 0))
    door = _Door(handler, port, wire, transport)
    try:
        blocked.add(BAD)
        for _ in range(10):
            await door.ask(type="PING", node_id=BAD)

        reply = await door.ask(type="PING", node_id=FRIEND)

        assert reply is not None and reply["type"] == "PONG"
    finally:
        transport.close()
        await handler.stop()


# ----- a seed that answered and is blocked reads as blocked, not as silent -----

@pytest.mark.asyncio
async def test_a_blocked_seed_fails_the_ping_at_once_and_bootstrap_says_it_is_blocked(blocked, caplog):
    import logging
    import time
    from dpc_client_core.dht import DHTManager

    seed = DHTRPCHandler(RoutingTable(node_id=BAD, k=20))
    manager = DHTManager(node_id=ME, ip="192.0.2.1", port=8888, is_blocked=lambda nid: nid in blocked)
    manager.rpc_handler.config = RPCConfig(timeout=2.0, max_retries=3)
    await seed.start(host="127.0.0.1", port=0)
    await manager.start(host="127.0.0.1", port=0)
    try:
        seed_port = seed.transport.get_extra_info("sockname")[1]
        blocked.add(BAD)
        started = time.monotonic()
        with caplog.at_level(logging.INFO):
            ok = await manager.bootstrap([("127.0.0.1", seed_port)])
        took = time.monotonic() - started

        assert ok is False
        assert took < 1.5                       # not three 2 s timeouts
        assert manager.routing_table.get_node(BAD) is None
        said = [r.getMessage() for r in caplog.records]
        assert any("blocked" in m and "seed" in m for m in said)
        assert not any("no responsive seed" in m for m in said)
    finally:
        await manager.stop()
        await seed.stop()
