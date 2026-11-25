"""ADR-053 acceptance checks 7, 8 and 9: run_shell on a foreign request.

Every tier asks (Tier 0 `git push`, `copy`, redirects included) with the command and the
resolved working directory; a Tier 1 command asks once, on the shell's own card, which
names the asker (D-b); a path closed to the turn is refused before any card (2.7); and
Active Recall still reaches a granted extended path (validate_extended_path untouched).
"""
import asyncio
import os
import types
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, Asker, DpcAgent
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)


class _LocalApi:
    has_clients = True

    async def broadcast_event(self, name, payload):
        pass


class _Service:
    def __init__(self):
        self.local_api = _LocalApi()
        self.schedule_cards = []
        self.shell_cards = []
        self.p2p_manager = types.SimpleNamespace(get_display_name=lambda: "Mike")

    def _get_agent_telegram_bridge(self, agent_id):
        return None

    async def announce_schedule_approval_request(self, **kwargs):
        self.schedule_cards.append(kwargs)

    async def announce_schedule_approval_closed(self, **kwargs):
        pass

    async def announce_shell_approval_request(self, **kwargs):
        self.shell_cards.append(kwargs)

    async def announce_shell_approval_closed(self, **kwargs):
        pass


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    return ToolRegistry(agent_root=tmp_path_factory.mktemp("agent_reg"))


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.3)
    owner_approval._reset_for_tests()
    yield
    owner_approval._reset_for_tests()


def _ctx(tmp_path, service, asker):
    root = tmp_path / "agent_ark"
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "memory" / "scratchpad.md").write_text("private", encoding="utf-8")
    ctx = ToolContext(agent_root=root, message_source="group", turn_class=OTHER_NODE_TURN,
                      open_layers=frozenset(), asker=asker, conversation_id="group-friends",
                      dpc_service=service)
    ctx._event_loop = asyncio.get_running_loop()
    return ctx


async def _run(registry, command, ctx):
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: registry.execute("run_shell", {"command": command}, ctx=ctx))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["git push", "copy a.txt b.txt", "echo x > y.txt"])
async def test_a_tier_0_command_raises_one_card_with_the_command_and_the_resolved_cwd(
        registry, tmp_path, command):
    service = _Service()
    ctx = _ctx(tmp_path, service, FRIEND)
    assert shell._validate_command(command, ctx, "") is None, "the case is a Tier 0 command"
    result = await _run(registry, command, ctx)
    assert len(service.schedule_cards) == 1 and service.shell_cards == []
    card = service.schedule_cards[0]
    assert card["tool"] == "run_shell"
    assert f"command: {command}" in card["details"]
    assert f"cwd: {ctx.agent_root.resolve()}" in card["details"]
    assert card["asker"]["name"] == "Miron"
    assert "not in focus" in result
    assert not (ctx.agent_root / "y.txt").exists()


@pytest.mark.asyncio
async def test_a_tier_1_command_asks_once_on_the_shells_own_card_which_names_the_asker(registry, tmp_path):
    service = _Service()
    ctx = _ctx(tmp_path, service, FRIEND)
    assert shell._validate_command("git push --force", ctx, "")[0] == "tier1"
    await _run(registry, "git push --force", ctx)
    assert service.schedule_cards == [], "no second card from the gate"
    assert len(service.shell_cards) == 1
    card = service.shell_cards[0]
    assert card["asker"]["name"] == "Miron" and card["asker"]["kind"] == "human"
    assert card["cwd"] == str(ctx.agent_root.resolve())


@pytest.mark.asyncio
async def test_the_owners_own_tier_1_card_names_no_asker(registry, tmp_path):
    service = _Service()
    ctx = _ctx(tmp_path, service, MIKE)
    await _run(registry, "git push --force", ctx)
    assert len(service.shell_cards) == 1 and "asker" not in service.shell_cards[0]


@pytest.mark.asyncio
async def test_a_closed_path_is_refused_with_one_line_and_no_card(registry, tmp_path):
    service = _Service()
    ctx = _ctx(tmp_path, service, FRIEND)
    command = f"type {Path('memory') / 'scratchpad.md'}"
    result = await _run(registry, command, ctx)
    assert result.startswith("⚠️ Not run:") and "private memory" in result
    assert "\n" not in result
    assert service.schedule_cards == [] and service.shell_cards == []


@pytest.mark.asyncio
async def test_the_owner_in_a_mixed_room_is_refused_a_closed_path_too(registry, tmp_path):
    service = _Service()
    ctx = _ctx(tmp_path, service, MIKE)
    result = await _run(registry, "cat memory/scratchpad.md", ctx)
    assert "private memory" in result and service.shell_cards == []


def test_a_closed_working_directory_is_refused(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                      open_layers=frozenset(), asker=FRIEND)
    (tmp_path / "memory").mkdir()
    reason = shell._closed_shell_path(ctx, "dir", str(tmp_path / "memory"))
    assert "private memory" in reason


def test_words_that_are_not_paths_are_not_read_as_paths(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                      open_layers=frozenset(), asker=FRIEND)
    for command in ("echo hello", "git push origin main", "curl https://example.com/memory/x",
                    "python -m pip list", "echo x > " + os.devnull):
        assert shell._closed_shell_path(ctx, command, "") == "", command


def test_an_owner_room_is_not_checked(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, message_source="ui", turn_class="owner",
                      open_layers=frozenset({"L5", "L6", "EXT"}), asker=MIKE)
    assert shell._closed_shell_path(ctx, "cat memory/scratchpad.md", "") == ""


def test_active_recall_still_reaches_a_granted_extended_path(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "notes.md").write_text("x", encoding="utf-8")
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path / "agent_ark"
    agent.agent_root.mkdir()
    agent._firewall_profile = "agent_ark"
    agent._firewall = types.SimpleNamespace(
        is_extended_path_allowed=lambda path, require_write=False, profile_name=None:
            str(path).startswith(str(granted.resolve())),
    )
    assert agent._extended_path_allowed(str(granted / "notes.md")) is True
    assert agent._extended_path_allowed(str(tmp_path / "elsewhere.md")) is False


def test_validate_extended_path_knows_nothing_of_the_asker_or_the_shell_check():
    import inspect
    from dpc_client_core.dpc_agent.tools.registry import ToolContext as TC
    source = inspect.getsource(TC.validate_extended_path)
    assert "asker" not in source and "closed_to_this_turn" not in source
    assert "_closed_shell_path" not in inspect.getsource(DpcAgent._extended_path_allowed)
