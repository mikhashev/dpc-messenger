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
