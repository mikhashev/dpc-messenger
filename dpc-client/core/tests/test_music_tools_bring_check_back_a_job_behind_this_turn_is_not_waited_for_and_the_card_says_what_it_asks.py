"""Music tools follow-up (ADR-051 D47, D49, D51; ADR-052 section 3.5).

1. `schedule_task` comes with the music tools (D49): an agent whose firewall enables the music
   tools and not `schedule_task` is offered `schedule_task` on the owner's turn, because
   check_back is the only way back to a job; on an `other_node` turn it is offered neither the
   music tools nor `schedule_task`. Real `DpcAgent.process`, firewall, registry and loop; only
   the provider is a double that records what it is offered.
2. The self-wait rule covers a job waiting behind this turn: a `running` job whose engine ended
   and waits for the card again, with `card_wait` naming this turn's local alias, is answered at
   once ("waits for the card behind this turn"), not waited for until the timeout. A `card_wait`
   naming another alias is waited for as before.
3. A music job's approval card says what it asks: "Run a music job on the GPU: ..." with
   Approve / Refuse, on Telegram (the real bridge's card and button press) and in the payload
   the interface reads (`heading`, `approve_label`, `refuse_label`, `music`). A check_back's
   card keeps its own words.

Live path: the tools run through the loop's dispatcher over the real music command mixin, job
store and library; the approval goes through `_await_schedule_approval`, the real
`CoreService.announce_schedule_approval_request` / `resolve_schedule_approval` and the real
`AgentTelegramBridge`; only the transports (the bot, the UI socket) are fakes.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.managers.llama_server_supervisor import card_held_for
from dpc_client_core.music import job_schemas as js
from dpc_client_core.service import CoreService
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (
    AGENT, CHAT, MusicService, Person, call, make_ctx, registry_for,
)

ALIAS = "cloud"
PROFILE = "agent_x"
THIS_NODE = "dpc-node-this0000000000000000"
FOREIGN_NODE = "dpc-node-foreign0000000000000"
LOCAL = "strata-27b"


@pytest.fixture(autouse=True)
def _clean():
    core_tools._pending_schedule_approvals.clear()
    yield
    core_tools._pending_schedule_approvals.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)
    return h


# -- 1. schedule_task comes with the music tools ---------------------------------------------


class _Provider:
    model = "m"

    def __init__(self):
        self.offered = None

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None, conversation_id=None, **_):
        self.offered = sorted(t.get("name") or (t.get("function") or {}).get("name") for t in tools or [])
        return {"content": "the answer", "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}

    async def generate_response(self, prompt, **_):
        return "the answer"


class _Manager:
    token_count_manager = None
    agent_provider = None
    vision_provider = None
    _alias_for_query = LLMManager._alias_for_query

    def __init__(self, provider):
        self.providers = {ALIAS: provider}
        self.default_provider = ALIAS

    def alias_context_window(self, alias):
        return 128000

    async def query(self, prompt, provider_alias=None, images=None, return_metadata=False, **_):
        return {"response": "the answer", "provider": ALIAS} if return_metadata else "the answer"


class _Groups:
    node_id = THIS_NODE

    def __init__(self):
        mixed = SimpleNamespace(group_id="group-mixed", name="group-mixed",
                                members=[THIS_NODE, FOREIGN_NODE], is_discord_bridge=False)
        self._groups = {"group-mixed": mixed}

    def get_group(self, gid):
        return self._groups.get(gid)

    def get_all_groups(self):
        return list(self._groups.values())


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "home" / ".dpc"))
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    return tmp_path


def _music_agent(tmp_path, provider):
    """A real agent whose firewall enables every music tool, in groups too, and not schedule_task."""
    tools = {name: True for name in music_tools.MUSIC_TOOL_NAMES}
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {"enabled": True, "tools": tools},
                                 "agent_profiles": {PROFILE: {"tools": tools}}}), encoding="utf-8")
    return DpcAgent(llm_manager=_Manager(provider), config=AgentConfig(max_rounds=3),
                    agent_root=tmp_path / "agents" / PROFILE, provider_alias=ALIAS,
                    firewall=ContextFirewall(rules), firewall_profile=PROFILE,
                    service=SimpleNamespace(group_manager=_Groups(), p2p_manager=None))


@pytest.mark.asyncio
async def test_an_owner_turn_with_music_tools_is_offered_schedule_task_and_an_other_node_turn_is_not(agent_home):
    provider = _Provider()
    agent = _music_agent(agent_home, provider)
    # The firewall alone does not enable it: the music tools bring it.
    assert "schedule_task" not in agent._firewall_allowed_tools("ui", "conv-ui")

    assert await asyncio.wait_for(agent.process("make a song", "conv-ui", message_source="ui"), 30) == "the answer"
    assert music_tools.MUSIC_TOOL_NAMES <= set(provider.offered), provider.offered
    assert "schedule_task" in provider.offered

    provider.offered = None
    assert await asyncio.wait_for(
        agent.process("make a song", "group-mixed", message_source="group"), 30) == "the answer"
    assert not music_tools.MUSIC_TOOL_NAMES & set(provider.offered), provider.offered
    assert "schedule_task" not in provider.offered


# -- 2. a job waiting for the card behind this turn ------------------------------------------


async def _running_job_waiting_behind(rig, home, holder_alias):
    """A job the agent proposed, started, whose engine ended and waits for the card again behind
    `holder_alias`: the store's own transitions, as the runner and its _CardWait write them."""
    ctx = make_ctx(home, MusicService(home, rig, Person(True)))
    reg = registry_for(ctx)
    await call(reg, ctx, "music_propose_job", params=from_text())
    [job] = rig.store.list_jobs()
    from dpc_client_core.music.runner import LM_PAYLOAD
    rig.store.write_payload(job["id"], LM_PAYLOAD, b"{}", endpoint="/lm")
    rig.store.start_job(job["id"])
    rig.store.set_waiting_for_card(job["id"], [{"alias": holder_alias, "engine": "llama-server"}])
    job = rig.store.get_job(job["id"])
    assert job["state"] == js.RUNNING and job["card_wait"]["holders"][0]["alias"] == holder_alias
    return ctx, reg, job


@pytest.mark.asyncio
async def test_a_running_job_waiting_for_the_card_behind_this_turns_alias_is_answered_at_once(home):
    async with Rig(home, start_runner=False) as rig:
        ctx, reg, job = await _running_job_waiting_behind(rig, home, LOCAL)
        async with card_held_for(LOCAL, "accepted_request", agent_id=AGENT, conversation_id=CHAT):
            began = time.monotonic()
            out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=600)
            took = time.monotonic() - began
        assert out.startswith(f"job {job['id']}: running; it waits for the card behind this turn"), out
        assert f"'{LOCAL}'" in out and "check back after this turn ends" in out
        assert f"check_back in about {music_tools.check_back_delay_s(job)} s" in out
        assert took < 2.0
        assert ctx._music_wait_spent.get("turn-1", 0.0) == 0.0, "an answer at once spends no wait budget"


@pytest.mark.asyncio
async def test_a_running_job_waiting_behind_another_alias_is_waited_for_as_before(home):
    async with Rig(home, start_runner=False) as rig:
        ctx, reg, job = await _running_job_waiting_behind(rig, home, "another-model")
        async with card_held_for(LOCAL, "accepted_request", agent_id=AGENT, conversation_id=CHAT):
            out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=1)
        assert out.startswith(f"job {job['id']}: running"), out
        assert "behind this turn" not in out
        assert "Still not finished at the end of this wait" in out


# -- 3. the approval card says what it asks ----------------------------------------------------


class FakeLocalApi:
    has_clients = True

    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class FakeBot:
    """The Telegram transport: keeps what was sent and presses the card's first or second button
    the way a person does, through the bridge's own callback handler."""

    def __init__(self, press):
        self.press = press
        self.sent = []
        self.bridge = None
        self.answered = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((text, [b.text for b in reply_markup.inline_keyboard[0]]))
        data = reply_markup.inline_keyboard[0][0 if self.press == "approve" else 1].callback_data
        asyncio.get_running_loop().create_task(self._press(data, chat_id))
        return types.SimpleNamespace(message_id=101)

    async def _press(self, data, chat_id):
        bot = self

        class Query:
            def __init__(self):
                self.data = data
                self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=int(chat_id), type="private"))
                self.from_user = types.SimpleNamespace(id=int(chat_id))

            async def answer(self, *a, **k):
                pass

            async def edit_message_text(self, text, **k):
                bot.answered.append(text)

        await self.bridge._handle_schedule_callback(types.SimpleNamespace(callback_query=Query()), None)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        pass


class ServiceWithCard(MusicService):
    """The music command mixin with CoreService's own schedule-approval methods and a real bridge."""

    announce_schedule_approval_request = CoreService.announce_schedule_approval_request
    announce_schedule_approval_closed = CoreService.announce_schedule_approval_closed
    resolve_schedule_approval = CoreService.resolve_schedule_approval

    def __init__(self, home, rig, press):
        super().__init__(home, rig, Person(True))
        self.local_api = FakeLocalApi()
        self.bot = FakeBot(press)
        bridge = AgentTelegramBridge(bot_token="123:TOKEN", allowed_chat_ids=["77"])
        bridge._bot = self.bot
        bridge._enabled = True
        bridge._agent_manager = types.SimpleNamespace(service=self)
        self.bot.bridge = bridge
        self._get_agent_telegram_bridge = lambda agent_id: bridge

    def _conversation_display_name(self, conversation_id):
        return conversation_id


def _requests(service):
    return [p for n, p in service.local_api.events if n == "schedule_approval_request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("press", ["approve", "refuse"])
async def test_a_music_job_card_asks_to_run_a_music_job_with_approve_and_refuse(home, press):
    async with Rig(home, start_runner=False) as rig:
        service = ServiceWithCard(home, rig, press)
        ctx = make_ctx(home, service)
        ctx.reply_telegram_chat_id = "77"
        ctx._event_loop = asyncio.get_running_loop()
        out = await call(registry_for(ctx), ctx, "music_propose_job",
                         params=from_text(style="slow piano", variants=2))

        [(text, buttons)] = service.bot.sent
        assert text.startswith("🎵 ") and "asks to run a music job on the GPU:" in text, text
        assert 'Run a music job on the GPU: generate_from_text, "slow piano", 2 takes, with lyrics' in text
        assert "come back" not in text and "scheduled" not in text, text
        assert buttons == ["✅ Approve", "❌ Refuse"]

        [payload] = _requests(service)
        assert payload["task_type"] == "music_job" and payload["when"] == "now"
        assert payload["heading"] == "asks to run a music job on the GPU"
        assert (payload["approve_label"], payload["refuse_label"]) == ("Approve", "Refuse")
        assert payload["about"].startswith('Run a music job on the GPU: generate_from_text, "slow piano", 2 takes')
        music = payload["music"]
        assert (music["type"], music["style"], music["variants"], music["instrumental"]) == \
            ("generate_from_text", "slow piano", 2, False)
        assert music["eta_seconds"] is None or isinstance(music["eta_seconds"], int)

        if press == "approve":
            assert service.bot.answered == ["✅ Approved: the music job is queued."]
            assert out.startswith("Proposed (approved by the person)."), out
            assert len(rig.store.list_jobs()) == 1
        else:
            assert service.bot.answered == ["❌ Refused: no music job was made."]
            assert out == "⚠️ The music job was not proposed: the user declined. No job was made."
            assert rig.store.list_jobs() == []


@pytest.mark.asyncio
async def test_a_check_back_card_keeps_its_own_words(home):
    async with Rig(home, start_runner=False) as rig:
        service = ServiceWithCard(home, rig, "approve")
        ctx = make_ctx(home, service)
        ctx.reply_telegram_chat_id = "77"
        ctx._event_loop = asyncio.get_running_loop()
        approved, _ = await asyncio.to_thread(
            core_tools._await_schedule_approval, ctx, task_type="check_back", when="in 5 minutes",
            about="see if the render is done")
        assert approved
        [(text, buttons)] = service.bot.sent
        assert "wants to come back to this in 5 minutes:" in text
        assert buttons == ["✅ Schedule it", "❌ Not now"]
        assert service.bot.answered == ["✅ Scheduled."]
        [payload] = _requests(service)
        assert (payload["heading"], payload["approve_label"], payload["refuse_label"]) == \
            ("wants to come back to this later", "Schedule it", "Not now")
        assert payload["music"] is None
