"""A tier-1 approval names the agent and the chat it was working in.

Filed 2026-06-11 by Mike: «когда появляется попап Shell Command Approval нужно
показывать какой агент и в каком чате запрашивает разрешение». Until now the
payload was `{request_id, command, reason, agent_name}` — with four agents
working in four chats, "Johnny wants to run rm -rf" is not an answerable
question, and two agents can raise the same command in the same second.
"""

import asyncio
import types

import pytest


class FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _service(group=None, peers=None, agent_names=None):
    from dpc_client_core.service import CoreService

    service = CoreService.__new__(CoreService)
    service.local_api = FakeLocalApi()
    service.llm_manager = types.SimpleNamespace(providers={})
    service.peer_metadata = peers or {}
    service.group_manager = types.SimpleNamespace(get_group=lambda gid: group)
    if agent_names is not None:
        service._get_agent_display_name = lambda aid: agent_names.get(aid, aid)
    return service


class TestTheContextCarriesTheConversation:
    def test_tool_context_has_the_two_fields_and_they_default_to_nothing(self, tmp_path):
        from dpc_client_core.dpc_agent.tools.registry import ToolContext

        bare = ToolContext(agent_root=tmp_path)
        assert bare.conversation_id is None
        assert bare.conversation_title is None

        named = ToolContext(
            agent_root=tmp_path,
            conversation_id="group-b88b65076b85",
            conversation_title="DPC project",
        )
        assert named.conversation_id == "group-b88b65076b85"
        assert named.conversation_title == "DPC project"


class TestTheNameTheOperatorSees:
    def test_a_group_reads_as_its_name(self):
        service = _service(group=types.SimpleNamespace(name="DPC project"))
        assert service._conversation_display_name("group-b88b") == "DPC project"

    def test_a_one_to_one_reads_as_the_agent_and_says_so(self):
        service = _service(agent_names={"agent_johnny_f309700d": "Johnny"})
        assert (
            service._conversation_display_name("agent_johnny_f309700d")
            == "Johnny (1:1)"
        )

    def test_a_peer_reads_as_its_name(self):
        service = _service(peers={"dpc-node-abc": {"name": "Mike (linux)"}})
        assert service._conversation_display_name("dpc-node-abc") == "Mike (linux)"

    def test_an_unknown_conversation_reads_as_its_id_not_as_nothing(self):
        """An id in front of the operator still tells two live requests apart."""
        service = _service()
        assert service._conversation_display_name("whatever-42") == "whatever-42"

    def test_no_conversation_stays_empty(self):
        """A schedule or a sleep has no chat behind it, and must not invent one."""
        service = _service()
        assert service._conversation_display_name("") == ""

    def test_a_broken_group_manager_does_not_take_the_approval_down(self):
        from dpc_client_core.service import CoreService

        service = CoreService.__new__(CoreService)
        service.peer_metadata = {}
        service.group_manager = types.SimpleNamespace(
            get_group=lambda gid: (_ for _ in ()).throw(RuntimeError("db gone"))
        )
        assert service._conversation_display_name("group-x") == "group-x"


class TestThePayloadTheInterfaceReceives:
    @pytest.mark.asyncio
    async def test_it_carries_the_conversation_and_its_resolved_name(self):
        service = _service(group=types.SimpleNamespace(name="DPC project"))

        await service.announce_shell_approval_request(
            request_id="r1", command="rm -rf ./x", reason="Requires approval",
            agent_id="agent_007", agent_name="Ark", timeout_seconds=60,
            conversation_id="group-b88b65076b85",
        )

        name, payload = service.local_api.events[0]
        assert name == "shell_approval_request"
        assert payload["agent_name"] == "Ark"
        assert payload["conversation_id"] == "group-b88b65076b85"
        assert payload["conversation_title"] == "DPC project"

    @pytest.mark.asyncio
    async def test_a_title_the_caller_already_knew_is_not_looked_up_again(self):
        """Group runs carry chat_context; the resolver is for everything else."""
        service = _service(group=types.SimpleNamespace(name="stale name"))

        await service.announce_shell_approval_request(
            request_id="r2", command="ls", reason="", agent_id="a", agent_name="Ark",
            conversation_id="group-x", conversation_title="DPC project",
        )

        _, payload = service.local_api.events[0]
        assert payload["conversation_title"] == "DPC project"

    @pytest.mark.asyncio
    async def test_a_run_with_no_chat_behind_it_says_nothing_rather_than_guessing(self):
        service = _service()

        await service.announce_shell_approval_request(
            request_id="r3", command="ls", reason="", agent_id="a", agent_name="Ark",
        )

        _, payload = service.local_api.events[0]
        assert payload["conversation_id"] == ""
        assert payload["conversation_title"] == ""


class TestTheToolHandsItOver:
    @pytest.mark.asyncio
    async def test_the_conversation_reaches_the_service_from_the_context(self, tmp_path):
        from dpc_client_core.dpc_agent.tools import shell as shell_tool

        announced = []

        class RecordingService:
            # A watched desktop, which the gate now asks for by name.
            local_api = types.SimpleNamespace(has_clients=True)

            async def announce_shell_approval_request(self, **kwargs):
                announced.append(kwargs)

            async def announce_shell_approval_closed(self, **kwargs):
                pass

        agent_root = tmp_path / "agent_007"
        agent_root.mkdir()
        ctx = types.SimpleNamespace(
            agent_root=agent_root,
            dpc_service=RecordingService(),
            _event_loop=asyncio.get_running_loop(),
            _agent=types.SimpleNamespace(display_name="Johnny", _firewall_profile="agent_007"),
            conversation_id="group-b88b65076b85",
            conversation_title="DPC project",
        )

        task = asyncio.create_task(
            asyncio.to_thread(
                shell_tool._request_approval, ctx, "rm -rf ./x", "Requires approval", "", 5
            )
        )
        for _ in range(200):
            if announced:
                break
            await asyncio.sleep(0.01)

        assert announced, "the request was never announced"
        call = announced[0]
        assert call["conversation_id"] == "group-b88b65076b85"
        assert call["conversation_title"] == "DPC project"

        shell_tool._pending_approvals.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestTheNameAnUnknownAgentGets:
    """`getattr(agent, "display_name", "Agent")` reached production twice —
    the shell dialog on 2026-08-14 and the schedule card on 2026-08-16 — as
    «Agent wants …». "We do not know who is asking" must not read as a name."""

    def test_a_named_agent_reads_as_its_name(self, tmp_path):
        from dpc_client_core.dpc_agent.tools.registry import agent_display_name

        ctx = types.SimpleNamespace(
            agent_root=tmp_path / "agent_johnny_f309700d",
            _agent=types.SimpleNamespace(display_name="Johnny"),
        )
        assert agent_display_name(ctx) == "Johnny"

    def test_the_name_comes_from_the_agent_config_because_the_object_has_none(self, tmp_path):
        """`DpcAgent` defines no `display_name`, so the object never had one to
        give: the popup read «Agent» for two months and «agent_001» after the
        default was fixed. The name lives in the agent's own config, which is
        where AgentManager and the service have always read it."""
        import json

        from dpc_client_core.dpc_agent.tools.registry import agent_display_name

        root = tmp_path / "agent_001"
        root.mkdir()
        (root / "config.json").write_text(
            json.dumps({"agent_id": "default", "name": "Ark"}), encoding="utf-8"
        )
        ctx = types.SimpleNamespace(agent_root=root, _agent=None)
        assert agent_display_name(ctx) == "Ark"

    def test_reading_the_config_never_creates_the_directory_it_reads(self, tmp_path):
        """load_agent_config resolves through get_agent_root, which mkdirs."""
        from dpc_client_core.dpc_agent.tools.registry import agent_display_name

        root = tmp_path / "agent_ghost"
        assert agent_display_name(types.SimpleNamespace(agent_root=root, _agent=None)) == "agent_ghost"
        assert not root.exists()

    def test_without_a_config_it_falls_back_to_the_directory(self, tmp_path):
        from dpc_client_core.dpc_agent.tools.registry import agent_display_name

        root = tmp_path / "agent_johnny_f309700d"
        root.mkdir()
        ctx = types.SimpleNamespace(agent_root=root, _agent=None)
        assert agent_display_name(ctx) == "agent_johnny_f309700d"

    def test_with_nothing_at_all_it_says_so(self):
        from dpc_client_core.dpc_agent.tools.registry import agent_display_name

        assert agent_display_name(types.SimpleNamespace()) == "Unknown agent"


class TestTheSharedOriginHelper:
    def test_a_title_the_caller_knew_is_used_as_is(self, tmp_path):
        from dpc_client_core.dpc_agent.tools.registry import conversation_origin

        ctx = types.SimpleNamespace(
            conversation_id="group-x", conversation_title="DPC project",
        )
        assert conversation_origin(ctx) == ("group-x", "DPC project")

    def test_an_unnamed_conversation_is_named_by_the_service(self):
        from dpc_client_core.dpc_agent.tools.registry import conversation_origin

        service = types.SimpleNamespace(
            _conversation_display_name=lambda cid: "DPC project",
        )
        ctx = types.SimpleNamespace(conversation_id="group-x", dpc_service=service)
        assert conversation_origin(ctx) == ("group-x", "DPC project")

    def test_a_run_with_no_chat_stays_empty(self):
        from dpc_client_core.dpc_agent.tools.registry import conversation_origin

        assert conversation_origin(types.SimpleNamespace()) == ("", "")

    def test_a_name_is_never_worth_failing_a_gate_over(self):
        from dpc_client_core.dpc_agent.tools.registry import conversation_origin

        def _boom(cid):
            raise RuntimeError("group store is gone")

        ctx = types.SimpleNamespace(
            conversation_id="group-x",
            dpc_service=types.SimpleNamespace(_conversation_display_name=_boom),
        )
        assert conversation_origin(ctx) == ("group-x", "")


class TestTheScheduleCard:
    """The second of the three surfaces. Its payload did carry a
    `conversation_id` — `ctx.current_task_id`, the id of the task being
    scheduled — so the one field shaped like an answer held the wrong quantity,
    and the card rendered it nowhere."""

    def _run_gate(self, ctx, broadcast):
        import threading
        from dpc_client_core.dpc_agent.tools import core as tools_core

        out = {}

        def _call():
            out["result"] = tools_core._await_schedule_approval(
                ctx, task_type="check_back", when="in 1200s", about="re-read the log",
            )

        worker = threading.Thread(target=_call)
        worker.start()
        for _ in range(200):
            if broadcast:
                break
            import time
            time.sleep(0.01)
        for rid in list(tools_core._pending_schedule_approvals):
            tools_core.resolve_schedule_approval(rid, True)
        worker.join(timeout=5)
        return out.get("result")

    @pytest.mark.asyncio
    async def test_the_payload_names_the_chat_and_keeps_the_task_id_apart(self, tmp_path):
        broadcast = []

        class FakeApi:
            has_clients = True

            async def broadcast_event(self, name, payload):
                broadcast.append((name, payload))

        ctx = types.SimpleNamespace(
            agent_root=tmp_path / "agent_johnny_f309700d",
            _agent=types.SimpleNamespace(display_name="Johnny"),
            dpc_service=types.SimpleNamespace(local_api=FakeApi()),
            _event_loop=asyncio.get_running_loop(),
            current_task_id="task-42",
            conversation_id="group-b88b65076b85",
            conversation_title="DPC project",
        )

        result = await asyncio.to_thread(self._run_gate, ctx, broadcast)

        assert result == (True, "")
        name, payload = broadcast[0]
        assert name == "schedule_approval_request"
        assert payload["conversation_id"] == "group-b88b65076b85"
        assert payload["conversation_title"] == "DPC project"
        assert payload["task_id"] == "task-42", "the task id is kept, under its own name"
        assert payload["agent_name"] == "Johnny"

    @pytest.mark.asyncio
    async def test_an_unnamed_agent_is_not_signed_as_agent(self, tmp_path):
        broadcast = []

        class FakeApi:
            has_clients = True

            async def broadcast_event(self, name, payload):
                broadcast.append((name, payload))

        ctx = types.SimpleNamespace(
            agent_root=tmp_path / "agent_johnny_f309700d",
            _agent=None,
            dpc_service=types.SimpleNamespace(local_api=FakeApi()),
            _event_loop=asyncio.get_running_loop(),
        )

        await asyncio.to_thread(self._run_gate, ctx, broadcast)

        _, payload = broadcast[0]
        assert payload["agent_name"] != "Agent"
        assert payload["agent_name"] == "agent_johnny_f309700d"
        assert payload["conversation_id"] == ""


class TestTheHeadlessLoginGate:
    """The third surface, and the one that asked the least: its payload was
    `{request_id, agent_id, domain, url}` — no chat, and not even a name, so a
    person was asked to let something use their logged-in account without
    being told who or from where.

    The call sits inside `browse_page` behind a real browser, so this reads the
    payload the module builds rather than driving Camoufox for it.
    """

    def _payload_keys(self):
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1]
            / "dpc_client_core" / "dpc_agent" / "tools" / "browser.py"
        ).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "broadcast_event"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and first.value == "web_auth_headless_approval_request"):
                continue
            payload = node.args[1]
            assert isinstance(payload, ast.Dict), "payload is no longer a literal — read it another way"
            return {k.value for k in payload.keys if isinstance(k, ast.Constant)}
        return None

    def test_the_broadcast_is_where_this_test_thinks_it_is(self):
        """A source check that finds nothing passes for every program."""
        keys = self._payload_keys()
        assert keys is not None, "the web-auth approval broadcast was not found"
        assert {"request_id", "domain", "url"} <= keys

    def test_it_names_the_agent_and_the_chat(self):
        keys = self._payload_keys()
        assert "agent_name" in keys, "the card could only show a raw agent id"
        assert {"conversation_id", "conversation_title"} <= keys
