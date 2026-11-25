"""ADR-053: who asked decides what an agent may do (acceptance checks 2-6, 10, 10a, 14).

The room still decides what the agent may say (ADR-048); the asker decides whether a
flagged tool runs at once or goes to the owner as a card first. These run the real gate
(`ToolRegistry.execute` -> `owner_approval.open_gate`) and the real card machinery
(`tools.core._await_schedule_approval`) against a recording service.
"""
import asyncio
import time
import types

import pytest

from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import (
    ASKER_AGENT, ASKER_HUMAN, OTHER_NODE_TURN, OWNER_TURN, Asker, DpcAgent, peer_post_asker,
)
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry

THIS_NODE = "dpc-node-this-0000"
OWN_NODE = "dpc-node-laptop-0001"
FRIEND_NODE = "dpc-node-friend-0002"


class _LocalApi:
    def __init__(self, attached=True):
        self.has_clients = attached

    async def broadcast_event(self, name, payload):
        pass


class _Bridge:
    def __init__(self, owner_user_id):
        self._owner_user_id = owner_user_id


class _Service:
    """Records each card and answers it as `policy` says: approve, refuse or none."""

    def __init__(self, policy="none", attached=True, bridge=None):
        self.policy = policy
        self.local_api = _LocalApi(attached)
        self.requests = []
        self.bridge = bridge
        self.p2p_manager = types.SimpleNamespace(get_display_name=lambda: "Mike", node_id=THIS_NODE)

    def _get_agent_telegram_bridge(self, agent_id):
        return self.bridge

    async def announce_schedule_approval_request(self, **kwargs):
        self.requests.append(kwargs)
        if self.policy in ("approve", "refuse"):
            tools_core.resolve_schedule_approval(kwargs["request_id"], self.policy == "approve")

    async def announce_schedule_approval_closed(self, **kwargs):
        pass


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    return ToolRegistry(agent_root=tmp_path_factory.mktemp("agent_reg"))


@pytest.fixture(autouse=True)
def quick_ttl(monkeypatch):
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.6)
    owner_approval._reset_for_tests()
    yield
    owner_approval._reset_for_tests()


def _ctx(tmp_path, service, asker, *, chat="group-friends", turn_class=OTHER_NODE_TURN,
         telegram="", source="group"):
    root = tmp_path / "agent_ark"
    root.mkdir(exist_ok=True)
    ctx = ToolContext(agent_root=root, message_source=source, turn_class=turn_class,
                      open_layers=frozenset(), asker=asker, conversation_id=chat,
                      conversation_title="Friends", dpc_service=service,
                      reply_telegram_chat_id=telegram or None)
    ctx._event_loop = asyncio.get_running_loop()
    return ctx


async def _run(registry, name, args, ctx):
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: registry.execute(name, args, ctx=ctx))


FRIEND = Asker(ASKER_HUMAN, "Miron", FRIEND_NODE, is_owner=False, verified=True)


@pytest.mark.asyncio
async def test_a_friends_write_shows_one_card_naming_him_and_unanswered_writes_nothing(registry, tmp_path):
    service = _Service("none")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await _run(registry, "write_file", {"path": "notes.txt", "content": "hello"}, ctx)

    assert len(service.requests) == 1
    card = service.requests[0]
    assert card["task_type"] == "owner_approval"
    assert card["tool"] == "write_file"
    assert card["asker"]["name"] == "Miron"
    assert card["asker"]["node_id"] == FRIEND_NODE
    assert card["asker"]["kind"] == "human"
    assert card["conversation_id"] == "group-friends"
    assert str((ctx.agent_root / "notes.txt").resolve()) in card["details"]
    assert not (ctx.agent_root / "notes.txt").exists()
    assert "not in focus" in result


@pytest.mark.asyncio
async def test_an_approved_call_runs_once(registry, tmp_path):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await _run(registry, "write_file", {"path": "notes.txt", "content": "hello"}, ctx)
    assert (ctx.agent_root / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert "✓" in result or "Wrote" in result or "wrote" in result.lower()
    assert not owner_approval.owner_approved_this_call(), "the mark ends with the call"


@pytest.mark.asyncio
async def test_a_refused_call_writes_nothing_and_says_the_owner_refused(registry, tmp_path):
    service = _Service("refuse")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await _run(registry, "write_file", {"path": "notes.txt", "content": "x"}, ctx)
    assert not (ctx.agent_root / "notes.txt").exists()
    assert "refused it for Miron" in result


@pytest.mark.asyncio
async def test_the_owner_asking_in_the_same_mixed_group_gets_no_card(registry, tmp_path):
    service = _Service("none")
    mike = Asker(ASKER_HUMAN, "Mike", THIS_NODE, is_owner=True, verified=True)
    ctx = _ctx(tmp_path, service, mike)  # the room is still other_node (ADR-048 unchanged)
    await _run(registry, "write_file", {"path": "notes.txt", "content": "mine"}, ctx)
    assert service.requests == []
    assert (ctx.agent_root / "notes.txt").read_text(encoding="utf-8") == "mine"


def _agent_with_own_group(tmp_path):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path
    agent._firewall_profile = "agent_ark"
    agent._firewall = types.SimpleNamespace(
        node_groups={"my_machines": [OWN_NODE]},
        get_agent_profile_settings=lambda name: {"memory": {"own_node_group": "my_machines"}},
    )
    agent._service = types.SimpleNamespace(group_manager=types.SimpleNamespace(node_id=THIS_NODE))
    return agent


@pytest.mark.asyncio
async def test_a_node_in_the_agents_own_node_group_gets_no_card(registry, tmp_path):
    agent = _agent_with_own_group(tmp_path)
    routed = peer_post_asker(OWN_NODE, "Mike (laptop)", "verified", False, THIS_NODE)
    asker = agent.resolve_asker(routed, "group")
    assert asker.is_owner
    service = _Service("none")
    ctx = _ctx(tmp_path, service, asker)
    await _run(registry, "write_file", {"path": "notes.txt", "content": "laptop"}, ctx)
    assert service.requests == []


@pytest.mark.asyncio
async def test_a_foreign_agent_card_says_agent(registry, tmp_path):
    service = _Service("none")
    bot = peer_post_asker(FRIEND_NODE, "FriendBot", "verified", True, THIS_NODE)
    assert bot.kind == ASKER_AGENT
    ctx = _ctx(tmp_path, service, bot)
    await _run(registry, "write_file", {"path": "n.txt", "content": "x"}, ctx)
    assert service.requests[0]["asker"]["kind"] == "agent"


@pytest.mark.asyncio
async def test_an_unverified_post_is_foreign_even_when_it_claims_an_own_node(registry, tmp_path):
    agent = _agent_with_own_group(tmp_path)
    for verdict in ("unverified", "legacy"):
        routed = peer_post_asker(OWN_NODE, "Mike?", verdict, False, THIS_NODE)
        asker = agent.resolve_asker(routed, "group")
        assert not asker.is_owner and asker.kind == "unknown"
        assert "not verified" in asker.label()
    service = _Service("none")
    ctx = _ctx(tmp_path, service, asker)
    await _run(registry, "write_file", {"path": "n.txt", "content": "x"}, ctx)
    assert "not verified" in service.requests[0]["asker"]["label"]


@pytest.mark.asyncio
async def test_with_no_interface_and_no_telegram_a_foreign_call_is_refused_at_once(registry, tmp_path):
    service = _Service("none", attached=False)
    ctx = _ctx(tmp_path, service, FRIEND)
    started = time.monotonic()
    result = await _run(registry, "write_file", {"path": "n.txt", "content": "x"}, ctx)
    assert time.monotonic() - started < 0.5
    assert "nobody could be asked" in result
    assert service.requests == []


@pytest.mark.asyncio
async def test_a_telegram_strangers_card_is_not_offered_there_without_the_owners_user_id(registry, tmp_path):
    stranger = Asker(ASKER_HUMAN, "stranger", "", is_owner=False, verified=False)
    service = _Service("none", attached=False, bridge=_Bridge(owner_user_id=None))
    ctx = _ctx(tmp_path, service, stranger, chat="agent_ark", source="telegram",
               turn_class="stranger", telegram="777")
    # A stranger's turn may not write in the agent directory at all (the room, ADR-048), so
    # the flagged tool here is one the room does not close.
    result = await _run(registry, "send_user_message", {"message": "hi"}, ctx)
    assert service.requests == [] and "nobody could be asked" in result

    service = _Service("none", attached=False, bridge=_Bridge(owner_user_id=42))
    ctx = _ctx(tmp_path, service, stranger, chat="agent_ark", source="telegram",
               turn_class="stranger", telegram="777")
    await _run(registry, "send_user_message", {"message": "hi"}, ctx)
    assert service.requests and service.requests[0]["telegram_chat_id"] == "777"


async def _wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_while_a_card_is_up_a_second_flagged_call_in_that_chat_is_refused_at_once(registry, tmp_path):
    service = _Service("none")
    first = asyncio.ensure_future(_run(registry, "write_file", {"path": "a.txt", "content": "x"},
                                       _ctx(tmp_path, service, FRIEND)))
    await _wait_for(lambda: len(service.requests) == 1)
    second = await _run(registry, "write_file", {"path": "b.txt", "content": "x"},
                        _ctx(tmp_path, service, FRIEND))
    assert "already waiting for the owner in this chat" in second
    assert len(service.requests) == 1, "the screen shows one card"
    await first


@pytest.mark.asyncio
async def test_two_cards_across_the_process_and_a_third_chat_is_refused(registry, tmp_path):
    service = _Service("none")
    a = asyncio.ensure_future(_run(registry, "write_file", {"path": "a.txt", "content": "x"},
                                   _ctx(tmp_path, service, FRIEND, chat="group-a")))
    b = asyncio.ensure_future(_run(registry, "write_file", {"path": "b.txt", "content": "x"},
                                   _ctx(tmp_path, service, FRIEND, chat="group-b")))
    await _wait_for(lambda: len(service.requests) == 2)
    third = await _run(registry, "write_file", {"path": "c.txt", "content": "x"},
                       _ctx(tmp_path, service, FRIEND, chat="group-c"))
    assert "another approval is already waiting for the owner" in third
    assert "at most 2" in third
    await asyncio.gather(a, b)
    assert owner_approval.pending_cards() == 0


@pytest.mark.asyncio
async def test_an_approval_that_comes_after_the_call_timed_out_starts_nothing(registry, tmp_path):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: owner_approval.run_with_call_deadline(
            time.monotonic() - 1, registry.execute, "write_file",
            {"path": "late.txt", "content": "x"}, ctx=ctx))
    assert service.requests, "the card was shown"
    assert not (ctx.agent_root / "late.txt").exists()
    assert "after this call had already timed out" in result


@pytest.mark.asyncio
async def test_only_a_context_marked_internal_is_not_gated(registry, tmp_path):
    # Follow-up 13 (Zcode #154): the absence of asker, source and class is no longer enough.
    root = tmp_path / "bare"
    root.mkdir()
    ctx = ToolContext(agent_root=root, internal_call=True)
    await _run(registry, "write_file", {"path": "x.txt", "content": "x"}, ctx)
    assert (root / "x.txt").exists()
    owner_approval._reset_for_tests()
    bare = ToolContext(agent_root=root)
    result = await _run(registry, "write_file", {"path": "y.txt", "content": "y"}, bare)
    assert result.startswith("⚠️") and not (root / "y.txt").exists()


@pytest.mark.asyncio
async def test_a_group_turn_with_no_asker_is_foreign(registry, tmp_path):
    service = _Service("none")
    ctx = _ctx(tmp_path, service, None)
    ctx.asker = None  # an old caller or a stored row without an asker (D-g)
    await _run(registry, "write_file", {"path": "n.txt", "content": "x"}, ctx)
    assert len(service.requests) == 1


def test_the_owner_turn_class_alone_does_not_make_a_foreign_asker_the_owner(tmp_path):
    # A group of only own nodes is owner class for memory, but an unverified post in it is
    # still a foreign asker for actions.
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OWNER_TURN,
                      asker=Asker("unknown", "x", OWN_NODE, False, False))
    assert not owner_approval.asker_is_owner(ctx)
