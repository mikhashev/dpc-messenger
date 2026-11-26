"""ADR-053 step 4, acceptance check 11: music_*, ComfyUI and chat_post_file were owner_only
(ADR-051 D43); on a foreign request they now ask the owner, call by call.

music_propose_job asks on its own card (D-b), which names the asker and states the GPU cost
(2.6); a refusal makes no job, an approval makes one, and the next call asks again. The
other tools get the gate's card. Their second owner checks accept only the owner or a call
the owner approved, never a remembered one.
"""
import asyncio
import types

import pytest

from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, Asker
from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)


class _LocalApi:
    has_clients = True

    async def broadcast_event(self, name, payload):
        pass


class _Service:
    def __init__(self, policy):
        self.policy = policy
        self.local_api = _LocalApi()
        self.cards = []
        self.p2p_manager = types.SimpleNamespace(get_display_name=lambda: "Mike")

    def _get_agent_telegram_bridge(self, agent_id):
        return None

    async def announce_schedule_approval_request(self, **kwargs):
        self.cards.append(kwargs)
        if self.policy in ("approve", "refuse"):
            tools_core.resolve_schedule_approval(kwargs["request_id"], self.policy == "approve")

    async def announce_schedule_approval_closed(self, **kwargs):
        pass


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    return ToolRegistry(agent_root=tmp_path_factory.mktemp("agent_reg"))


@pytest.fixture
def music_calls(monkeypatch):
    calls = []

    def fake_service_call(ctx, method, **kwargs):
        calls.append(method)
        if method == "list_music_jobs":
            return {"status": "ok", "jobs": []}
        if method == "check_music_job":
            return {"status": "ok", "params": kwargs.get("params")}
        if method == "propose_music_job":
            # As core does: the job records who asked (music_propose_job reads it back).
            return {"status": "ok", "job": {"id": "job_0123456789abcdef", "state": "running",
                                            "params": kwargs.get("params"),
                                            "requested_by": kwargs.get("requested_by")}}
        if method == "get_music_job":
            return {"status": "ok", "job": {"id": kwargs.get("job_id"), "state": "running"}}
        return {"status": "ok"}

    monkeypatch.setattr(music_tools, "_service_call", fake_service_call)
    monkeypatch.setattr(music_tools, "_turn_id", lambda: "turn-1")
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    owner_approval._reset_for_tests()
    return calls


def _ctx(tmp_path, service, asker, chat="group-friends"):
    root = tmp_path / "agent_ark"
    root.mkdir(exist_ok=True)
    ctx = ToolContext(agent_root=root, message_source="group", turn_class=OTHER_NODE_TURN,
                      open_layers=frozenset(), asker=asker, conversation_id=chat,
                      dpc_service=service)
    ctx._event_loop = asyncio.get_running_loop()
    ctx.agent_event_loop = asyncio.get_running_loop()
    return ctx


async def _run(registry, name, args, ctx):
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: registry.execute(name, args, ctx=ctx))


PARAMS = {"style": "slow piano", "duration_s": 30}


@pytest.mark.asyncio
async def test_a_foreign_music_job_shows_one_card_with_the_asker_and_the_cost_and_refused_makes_none(
        registry, tmp_path, music_calls):
    service = _Service("refuse")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await _run(registry, "music_propose_job", {"params": PARAMS}, ctx)
    assert len(service.cards) == 1
    card = service.cards[0]
    assert card["task_type"] == "music_job", "the tool's own card, not a second one from the gate"
    assert card["asker"]["name"] == "Miron"
    assert "GPU card" in card["cost"] and "25 s" in card["cost"]
    assert "propose_music_job" not in music_calls
    assert "No job was made" in result


@pytest.mark.asyncio
async def test_an_approved_music_job_runs_once_and_the_next_call_asks_again(registry, tmp_path, music_calls):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, FRIEND)
    first = await _run(registry, "music_propose_job", {"params": PARAMS}, ctx)
    assert "Proposed" in first and music_calls.count("propose_music_job") == 1
    second = await _run(registry, "music_propose_job", {"params": PARAMS, "idempotency_key": "k2"}, ctx)
    assert len(service.cards) == 2, "an approval holds for one call"
    assert music_calls.count("propose_music_job") == 2 and "Proposed" in second


@pytest.mark.asyncio
async def test_the_owners_music_card_states_the_cost_and_names_no_asker(registry, tmp_path, music_calls):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, MIKE)
    await _run(registry, "music_propose_job", {"params": PARAMS}, ctx)
    assert "asker" not in service.cards[0] and "GPU card" in service.cards[0]["cost"]


@pytest.mark.asyncio
async def test_another_music_tool_gets_the_gates_card(registry, tmp_path, music_calls):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, FRIEND)
    result = await _run(registry, "music_check", {"job_id": "job_0123456789abcdef"}, ctx)
    assert service.cards[0]["task_type"] == "owner_approval"
    assert service.cards[0]["tool"] == "music_check"
    assert "get_music_job" in music_calls and "running" in result


def test_the_second_owner_check_refuses_a_call_nobody_approved(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                      asker=FRIEND)
    assert "only after the owner approves" in music_tools._not_owner(ctx, "music_check")
    # In the agent's own 1:1 chat, where only the owner check is left to refuse it.
    own = tmp_path / "agent_ark"
    own.mkdir()
    one_to_one = ToolContext(agent_root=own, message_source="ui", turn_class="owner",
                             asker=FRIEND, conversation_id="agent_ark")
    assert "did not approve this call" in chat_files.turn_refusal(one_to_one)


def test_the_second_owner_check_lets_the_owner_through_in_a_mixed_room(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                      asker=MIKE)
    assert music_tools._not_owner(ctx, "music_check") is None


def test_an_approval_mark_lives_only_while_its_call_runs(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                      asker=FRIEND)
    token = owner_approval._CALL_MARK.set(owner_approval.MARK_APPROVED)
    try:
        assert music_tools._not_owner(ctx, "music_check") is None
    finally:
        owner_approval._CALL_MARK.reset(token)
    assert music_tools._not_owner(ctx, "music_check") is not None


@pytest.mark.asyncio
async def test_comfyui_on_a_foreign_request_takes_no_path_outside_the_sandbox_and_no_other_server(
        registry, tmp_path, music_calls):
    service = _Service("approve")
    ctx = _ctx(tmp_path, service, FRIEND)
    outside = tmp_path / "elsewhere" / "out.mp4"
    result = await _run(registry, "comfyui_convert",
                        {"input_path": str(ctx.agent_root / "in.webp"), "output_path": str(outside)}, ctx)
    assert result.startswith("⚠️ Not run:") and service.cards == []
    result = await _run(registry, "comfyui_queue_status", {"api_url": "http://203.0.113.5:8188"}, ctx)
    assert "only the local ComfyUI server" in result and service.cards == []


@pytest.mark.asyncio
async def test_a_comfyui_submit_card_states_the_cost(registry, tmp_path, music_calls):
    service = _Service("none")
    ctx = _ctx(tmp_path, service, FRIEND)
    await _run(registry, "comfyui_submit", {"prompt": "a cat"}, ctx)
    assert service.cards and "GPU card" in service.cards[0]["cost"]


def test_no_turn_class_withholds_these_tools_any_more(tmp_path):
    from dpc_client_core.dpc_agent.agent import DpcAgent
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path
    agent._firewall = None
    agent.tools = ToolRegistry(agent_root=tmp_path)
    assert agent.tools.owner_only_names() == set()
