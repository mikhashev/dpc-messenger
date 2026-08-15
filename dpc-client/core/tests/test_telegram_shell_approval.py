"""A tier-1 shell command must be answerable from the linked Telegram chat.

The desktop can answer the same request, so these also cover what happens to
the buttons when the answer arrives from the other side, and what a button
pressed after the window closed reports back.
"""

import asyncio
import types

import pytest

from dpc_client_core.managers.agent_telegram_bridge import (
    AgentTelegramBridge,
    get_agent_telegram_bridge,
)


class FakeSentMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return FakeSentMessage(self._next_id)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})


class FakeService:
    def __init__(self, status="ok"):
        self.status = status
        self.approved = []
        self.rejected = []

    async def shell_approve_command(self, request_id, add_to_whitelist=False):
        self.approved.append(request_id)
        return {"status": self.status, "request_id": request_id}

    async def shell_reject_command(self, request_id):
        self.rejected.append(request_id)
        return {"status": self.status, "request_id": request_id}


class FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id))
        self.answered = False
        self.texts = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.texts.append(text)


def make_bridge(service=None, chat_ids=("77",)):
    bridge = AgentTelegramBridge(bot_token="123:TOKEN", allowed_chat_ids=list(chat_ids))
    bridge._bot = FakeBot()
    bridge._enabled = True
    if service is not None:
        bridge._agent_manager = types.SimpleNamespace(service=service)
    return bridge


@pytest.mark.asyncio
async def test_request_is_offered_with_two_buttons():
    bridge = make_bridge()

    await bridge.notify_shell_approval(
        request_id="abc123",
        command="rm -rf ./build",
        reason="Requires approval: rm -rf",
        agent_name="Ark",
        timeout_seconds=60,
    )

    assert len(bridge._bot.sent) == 1
    sent = bridge._bot.sent[0]
    assert sent["chat_id"] == "77"
    assert "rm -rf ./build" in sent["text"]
    assert "Ark" in sent["text"]

    rows = sent["markup"].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2
    assert [b.callback_data for b in rows[0]] == [
        "shell:abc123:approve",
        "shell:abc123:reject",
    ]
    assert bridge._pending_shell["abc123"] == [("77", 101)]


@pytest.mark.asyncio
async def test_a_disabled_bridge_sends_nothing():
    bridge = make_bridge()
    bridge._enabled = False

    await bridge.notify_shell_approval(request_id="x", command="ls")

    assert bridge._bot.sent == []
    assert bridge._pending_shell == {}


@pytest.mark.asyncio
async def test_yes_approves_through_the_service():
    service = FakeService()
    bridge = make_bridge(service)
    bridge._pending_shell["abc123"] = [("77", 101)]

    query = FakeQuery("shell:abc123:approve", 77)
    await bridge._handle_shell_callback(types.SimpleNamespace(callback_query=query), None)

    assert service.approved == ["abc123"]
    assert service.rejected == []
    assert query.texts and "Approved" in query.texts[-1]
    assert "abc123" not in bridge._pending_shell


@pytest.mark.asyncio
async def test_no_rejects_through_the_service():
    service = FakeService()
    bridge = make_bridge(service)

    query = FakeQuery("shell:abc123:reject", 77)
    await bridge._handle_shell_callback(types.SimpleNamespace(callback_query=query), None)

    assert service.rejected == ["abc123"]
    assert service.approved == []
    assert query.texts and "Rejected" in query.texts[-1]


@pytest.mark.asyncio
async def test_a_button_pressed_from_another_chat_decides_nothing():
    service = FakeService()
    bridge = make_bridge(service, chat_ids=("77",))

    query = FakeQuery("shell:abc123:approve", 999)
    await bridge._handle_shell_callback(types.SimpleNamespace(callback_query=query), None)

    assert service.approved == [] and service.rejected == []
    assert query.texts == ["⛔ Unauthorized."]


@pytest.mark.asyncio
async def test_a_button_pressed_after_the_window_closed_says_so():
    service = FakeService(status="error")
    bridge = make_bridge(service)

    query = FakeQuery("shell:abc123:approve", 77)
    await bridge._handle_shell_callback(types.SimpleNamespace(callback_query=query), None)

    assert service.approved == ["abc123"]
    assert "already closed" in query.texts[-1]


@pytest.mark.asyncio
async def test_an_answer_elsewhere_takes_the_buttons_away_once():
    bridge = make_bridge()
    bridge._pending_shell["abc123"] = [("77", 101)]

    await bridge.close_shell_approval("abc123", "✅ Approved on the desktop.")
    await bridge.close_shell_approval("abc123", "✅ Approved on the desktop.")

    assert len(bridge._bot.edited) == 1
    assert bridge._bot.edited[0]["message_id"] == 101


def test_the_bot_actually_listens_for_the_shell_buttons():
    """The callback is useless unless it is registered — and a missing
    registration shows up nowhere until someone presses the button."""
    bridge = make_bridge()
    registered = []

    bridge._register_handlers(types.SimpleNamespace(add_handler=registered.append))

    patterns = {
        h.pattern.pattern: h.callback
        for h in registered
        if getattr(h, "pattern", None) is not None
    }
    assert patterns.get("^shell:") == bridge._handle_shell_callback
    assert patterns.get("^vote:") == bridge._handle_vote_callback


@pytest.mark.asyncio
async def test_the_service_offers_the_request_to_both_surfaces():
    """The fan-out is the whole point: the socket keeps the desktop toast and
    the bridge puts the same request on the phone."""
    from dpc_client_core.service import CoreService

    events = []

    class FakeLocalApi:
        async def broadcast_event(self, name, payload):
            events.append((name, payload))

    offered = []
    withdrawn = []
    bridge = types.SimpleNamespace(
        notify_shell_approval=lambda **kw: _record(offered, kw),
        close_shell_approval=lambda *a: _record(withdrawn, a),
    )
    manager = types.SimpleNamespace(_telegram_bridge=bridge)
    provider = types.SimpleNamespace(_managers={"agent_007": manager})

    service = CoreService.__new__(CoreService)
    service.local_api = FakeLocalApi()
    service.llm_manager = types.SimpleNamespace(providers={"dpc_agent": provider})

    await service.announce_shell_approval_request(
        request_id="r1", command="rm -rf ./x", reason="why",
        agent_id="agent_007", agent_name="Ark", timeout_seconds=60,
    )
    assert events[0][0] == "shell_approval_request"
    assert events[0][1]["command"] == "rm -rf ./x"
    assert offered[0]["command"] == "rm -rf ./x"
    assert offered[0]["timeout_seconds"] == 60

    await service.announce_shell_approval_closed(
        request_id="r1", agent_id="agent_007", outcome="✅ Approved on the desktop.",
    )
    assert events[1][0] == "shell_approval_expired"
    assert withdrawn[0] == ("r1", "✅ Approved on the desktop.")


async def _noop():
    return None


def _record(sink, value):
    sink.append(value)
    return _noop()


def test_the_bridge_lookup_walks_the_provider_and_refuses_a_stranger():
    bridge = object()
    manager = types.SimpleNamespace(_telegram_bridge=bridge)
    provider = types.SimpleNamespace(_managers={"agent_001": manager})
    llm_manager = types.SimpleNamespace(providers={"dpc_agent": provider})

    assert get_agent_telegram_bridge(llm_manager, "agent_001") is bridge
    assert get_agent_telegram_bridge(llm_manager, "agent_002") is None
    assert get_agent_telegram_bridge(llm_manager, "local_ai") is None
    assert get_agent_telegram_bridge(None, "agent_001") is None


@pytest.mark.asyncio
async def test_the_tool_announces_through_the_service_not_the_socket(tmp_path):
    """_request_approval must hand the request to the service, which owns the
    list of surfaces — the tool itself knows nothing about Telegram."""
    from dpc_client_core.dpc_agent.tools import shell as shell_tool

    announced = []
    closed = []

    class RecordingService:
        async def announce_shell_approval_request(self, **kwargs):
            announced.append(kwargs)

        async def announce_shell_approval_closed(self, **kwargs):
            closed.append(kwargs)

    agent_root = tmp_path / "agent_007"
    agent_root.mkdir()
    ctx = types.SimpleNamespace(
        agent_root=agent_root,
        dpc_service=RecordingService(),
        _event_loop=asyncio.get_running_loop(),
        _agent=types.SimpleNamespace(display_name="Ark", _firewall_profile="agent_007"),
    )

    task = asyncio.create_task(
        asyncio.to_thread(shell_tool._request_approval, ctx, "rm -rf ./x", "Requires approval", "", 5)
    )

    for _ in range(200):
        if announced:
            break
        await asyncio.sleep(0.01)

    assert announced, "the request was never announced"
    call = announced[0]
    assert call["command"] == "rm -rf ./x"
    assert call["agent_id"] == "agent_007"
    assert call["agent_name"] == "Ark"
    assert call["timeout_seconds"] == shell_tool.APPROVAL_TTL_SECONDS

    request_id = call["request_id"]
    entry = shell_tool._pending_approvals[request_id]
    assert entry["agent_id"] == "agent_007"
    entry["decision"] = "rejected"
    entry["result"] = "❌ Command rejected by user."
    entry["event"].set()

    assert await task == "❌ Command rejected by user."
