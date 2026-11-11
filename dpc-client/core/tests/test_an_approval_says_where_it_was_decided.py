"""A shell approval says where it was decided: `resolved_via`.

Until now both entry points passed the same hard-coded "Approved elsewhere." /
"Rejected elsewhere." to the closer, so the UI log told the person who had just
clicked in the dialog that it happened elsewhere, and the agent's tool result
said nothing at all about who approved.

The field has three readers, and each is read here from the real entry point:
the `shell_approval_resolved` payload, the Telegram edit that withdraws the
message when the other surface decided, and the agent's `run_shell` result. The
desktop path is a WebSocket command through `LocalApiServer._handler`; the
Telegram path is `_handle_shell_callback`. A WebSocket payload cannot name the
surface, because the surface is not a parameter of the commands it can reach.
"""

import ast
import asyncio
import inspect
import json
import threading
import types
from pathlib import Path

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.local_api import ALLOWED_COMMANDS, LocalApiServer
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.service import CoreService


@pytest.fixture(autouse=True)
def _no_leftover_requests():
    shell._pending_approvals.clear()
    yield
    shell._pending_approvals.clear()


class FakeLocalApi:
    has_clients = True

    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class FakeBot:
    def __init__(self):
        self.edited = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        return types.SimpleNamespace(message_id=101)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append(text)


class FakeQuery:
    def __init__(self, data, chat_id=77):
        self.data = data
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id, type="private"))
        self.from_user = types.SimpleNamespace(id=chat_id)
        self.texts = []

    async def answer(self, *args, **kwargs):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.texts.append(text)


def _world(owner="Mike"):
    """A real service and a real bridge joined together; only the transports are fake."""
    service = CoreService.__new__(CoreService)
    service.local_api = FakeLocalApi()
    service.p2p_manager = types.SimpleNamespace(get_display_name=lambda: owner)
    bridge = AgentTelegramBridge(bot_token="123:TOKEN", allowed_chat_ids=["77"])
    bridge._bot = FakeBot()
    bridge._enabled = True
    bridge._agent_manager = types.SimpleNamespace(service=service)
    service._get_agent_telegram_bridge = lambda agent_id: bridge
    return service, bridge


def _pending(request_id="r1"):
    entry = {
        "command": "echo hi", "agent_name": "Ark", "agent_id": "agent_a",
        "event": threading.Event(), "result": None,
    }
    shell._pending_approvals[request_id] = entry
    return entry


def _resolved(service):
    return [p for n, p in service.local_api.events if n == "shell_approval_resolved"]


async def _desktop(service, command, request_id="r1", **payload):
    """One WebSocket command through the real dispatcher; returns the answer."""
    server = LocalApiServer(service, port=0)
    server._auth_token = "token"

    class _WS:
        remote_address = ("127.0.0.1", 0)

        def __init__(self, frames):
            self._frames = list(frames)
            self.sent = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return self._frames.pop(0)

        async def recv(self):
            return self._frames.pop(0)

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self, *args, **kwargs):
            pass

    ws = _WS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "c1", "command": command,
                    "payload": {"request_id": request_id, **payload}}),
    ])
    await server._handler(ws)
    return next(m for m in ws.sent if m.get("id") == "c1")


async def _press(bridge, decision, request_id="r1"):
    query = FakeQuery(f"shell:{request_id}:{decision}")
    await bridge._handle_shell_callback(types.SimpleNamespace(callback_query=query), None)
    return query


# --- the desktop decides ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_desktop_approval_says_desktop_to_the_ui_and_to_the_telegram_message():
    service, bridge = _world()
    entry = _pending()
    bridge._pending_shell["r1"] = [("77", 101)]

    answer = await _desktop(service, "shell_approve_command")

    assert answer["status"] == "OK" and answer["payload"]["status"] == "ok"
    (payload,) = _resolved(service)
    assert payload["resolution"] == "approved"
    assert payload["resolved_via"] == "desktop"
    assert payload["outcome"] == "✅ Approved on the desktop."
    assert bridge._bot.edited == ["✅ Approved on the desktop."]
    assert entry["resolved_via"] == "desktop"


@pytest.mark.asyncio
async def test_a_desktop_rejection_says_desktop_to_the_ui_and_to_the_telegram_message():
    service, bridge = _world()
    entry = _pending()
    bridge._pending_shell["r1"] = [("77", 101)]

    await _desktop(service, "shell_reject_command")

    (payload,) = _resolved(service)
    assert (payload["resolution"], payload["resolved_via"]) == ("rejected", "desktop")
    assert payload["outcome"] == "❌ Rejected on the desktop."
    assert bridge._bot.edited == ["❌ Rejected on the desktop."]
    assert entry["resolved_via"] == "desktop"
    assert entry["result"] == "Command rejected by Mike on the desktop. Not run: `echo hi`"


# --- Telegram decides ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_telegram_approval_says_telegram_to_the_ui_and_keeps_the_presser_s_own_label():
    service, bridge = _world()
    entry = _pending()
    bridge._pending_shell["r1"] = [("77", 101)]

    query = await _press(bridge, "approve")

    (payload,) = _resolved(service)
    assert (payload["resolution"], payload["resolved_via"]) == ("approved", "telegram")
    assert payload["outcome"] == "✅ Approved from Telegram."
    assert query.texts == ["✅ Approved — running."]
    assert bridge._bot.edited == []  # the presser's message is not rewritten twice
    assert entry["resolved_via"] == "telegram"


@pytest.mark.asyncio
async def test_a_telegram_rejection_says_telegram_to_the_ui():
    service, bridge = _world()
    entry = _pending()
    bridge._pending_shell["r1"] = [("77", 101)]

    query = await _press(bridge, "reject")

    (payload,) = _resolved(service)
    assert (payload["resolution"], payload["resolved_via"]) == ("rejected", "telegram")
    assert payload["outcome"] == "❌ Rejected from Telegram."
    assert query.texts == ["❌ Rejected."]
    assert entry["result"] == "Command rejected by Mike from Telegram. Not run: `echo hi`"


# --- a payload cannot name the surface ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["shell_approve_command", "shell_reject_command"])
async def test_a_websocket_payload_cannot_claim_telegram(command):
    service, bridge = _world()
    entry = _pending()

    answer = await _desktop(service, command, resolved_via="telegram")

    assert answer["status"] == "ERROR"
    assert "decision" not in entry and "resolved_via" not in entry
    assert _resolved(service) == []
    assert not entry["event"].is_set()


@pytest.mark.asyncio
async def test_the_method_that_takes_the_surface_is_not_a_websocket_command():
    service, bridge = _world()
    entry = _pending()

    assert "resolve_shell_approval" not in ALLOWED_COMMANDS
    answer = await _desktop(service, "resolve_shell_approval", approved=True, resolved_via="telegram")

    assert answer["status"] == "ERROR"
    assert "decision" not in entry
    assert _resolved(service) == []


@pytest.mark.asyncio
async def test_a_surface_that_cannot_decide_is_refused():
    service, bridge = _world()
    entry = _pending()
    for word in ("timeout", "superseded", "elsewhere", ""):
        with pytest.raises(ValueError):
            await service.resolve_shell_approval("r1", approved=True, resolved_via=word)
    assert "decision" not in entry


# --- the agent's tool result --------------------------------------------------

def _run_tool(tmp_path, service, decide, monkeypatch, ttl=5):
    """Run `run_shell`'s approval wait on a thread, decide on the service loop, return its result."""
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", ttl)
    monkeypatch.setattr(shell, "_execute_shell_command", lambda *a, **k: "OUTPUT")
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        ctx = types.SimpleNamespace(
            agent_root=tmp_path, dpc_service=service,
            reply_telegram_chat_id="", _event_loop=loop, _agent=None,
        )
        out = {}
        t = threading.Thread(
            target=lambda: out.update(r=shell._request_approval(ctx, "echo hi", "tier1", "", 10)))
        t.start()
        if decide is not None:
            for _ in range(300):
                if shell._pending_approvals:
                    break
                threading.Event().wait(0.01)
            (request_id,) = list(shell._pending_approvals)
            asyncio.run_coroutine_threadsafe(decide(request_id), loop).result(5)
        t.join(10)
        for _ in range(200):  # the closers run on the loop after the thread has returned
            if not decide and _resolved(service):
                break
            threading.Event().wait(0.01)
        return out["r"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()


def test_the_agent_is_told_who_approved_and_from_where(tmp_path, monkeypatch):
    service, bridge = _world()
    result = _run_tool(tmp_path, service, lambda rid: service.shell_approve_command(rid), monkeypatch)
    assert result == "Approved by Mike on the desktop.\nOUTPUT"

    service, bridge = _world()
    result = _run_tool(tmp_path, service, lambda rid: _press(bridge, "approve", rid), monkeypatch)
    assert result == "Approved by Mike from Telegram.\nOUTPUT"


def test_the_agent_is_told_who_rejected_and_from_where(tmp_path, monkeypatch):
    service, bridge = _world()
    result = _run_tool(tmp_path, service, lambda rid: service.shell_reject_command(rid), monkeypatch)
    assert result == "Command rejected by Mike on the desktop. Not run: `echo hi`"

    service, bridge = _world()
    result = _run_tool(tmp_path, service, lambda rid: _press(bridge, "reject", rid), monkeypatch)
    assert result == "Command rejected by Mike from Telegram. Not run: `echo hi`"


def test_a_timeout_says_timeout_in_the_payload(tmp_path, monkeypatch):
    service, bridge = _world()
    result = _run_tool(tmp_path, service, None, monkeypatch, ttl=0.05)

    assert result.startswith("⏳ Command approval timed out")
    (payload,) = _resolved(service)
    assert (payload["resolution"], payload["resolved_via"]) == ("expired", "timeout")


@pytest.mark.asyncio
async def test_a_request_answered_before_it_arrived_says_superseded():
    service, bridge = _world()

    async def notify(**kwargs):
        pass

    bridge.notify_shell_approval = notify
    await service.announce_shell_approval_request(
        request_id="gone", command="echo hi", reason="tier1", agent_id="agent_a",
        agent_name="Ark", telegram_chat_id="77",
    )

    (payload,) = _resolved(service)
    assert (payload["resolution"], payload["resolved_via"]) == ("superseded", "superseded")


# --- no call site forgets it --------------------------------------------------

def test_every_closer_names_where_it_closed():
    missing = []
    for module in (service_mod, shell):
        path = Path(inspect.getfile(module))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "announce_shell_approval_closed" and not any(
                kw.arg == "resolved_via" for kw in node.keywords
            ):
                missing.append(f"{path.name}:{node.lineno}")
    assert not missing, f"closers without resolved_via: {missing}"
