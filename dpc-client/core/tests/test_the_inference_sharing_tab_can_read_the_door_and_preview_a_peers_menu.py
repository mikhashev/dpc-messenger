"""Five commands the Inference Sharing tab needs and the local API did not have.

Blocks (5) and (6) of that tab — the IDE door and the guest preview — plus the
validate-without-saving call each asked the backend a question nothing
answered: no command read the gateway's state or rotated its key, none said
what one named peer would be sent, and `validate_firewall_rules` took a string
and handed it to a validator that reads a dict, so wired as it stood it would
have answered "invalid" to everything.

The menu command is the one with teeth. It does not describe the wire, it uses
it: `CoreService.menu_for_peer` is the single selection both senders of
`PROVIDERS_RESPONSE` call — the peer's own `GET_PROVIDERS` and the notify
after a firewall save — so a preview that disagreed with the wire would be a
function disagreeing with itself. The two used to disagree for real: the
notify path had no type branch and could never send a transcription row.

Cross-platform: a real `aiohttp` listener on port 0 and a real key file under
`tmp_path`; no sleeps, no fixed ports.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.gateway import (
    GATEWAY_HOST,
    GATEWAY_KEY_NAME,
    GatewayServer,
    client_config_lines,
    mask_gateway_key,
)
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService

DOCS = Path(__file__).resolve().parents[3] / "docs" / "CONFIGURATION.md"

PEER = "dpc-node-" + "a" * 32
STRANGER = "dpc-node-" + "b" * 32
LOCAL = "ollama_local"
WHISPER = "whisper_local"
GROUP = "friends"


class _Provider:
    def __init__(self, type_, model):
        self.config = {"type": type_, "model": model}
        self.model = model

    def supports_vision(self):
        return False


def _rules(tmp_path: Path, **blocks) -> ContextFirewall:
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(blocks), encoding="utf-8")
    return ContextFirewall(path)


def _service(tmp_path: Path, firewall: ContextFirewall, *, providers=None,
             gateway=None, peers=None, known=None) -> SimpleNamespace:
    """A stand-in carrying only what the five commands reach for, with the real
    CoreService methods bound to it."""
    service = SimpleNamespace(
        firewall=firewall,
        llm_manager=SimpleNamespace(
            providers=providers if providers is not None else {LOCAL: _Provider("ollama", "gemma3:27b")},
            lookup_context_window=lambda model: None,
        ),
        p2p_manager=SimpleNamespace(
            peers=dict(peers or {}),
            peer_cache=SimpleNamespace(get_all_peers=lambda: dict(known or {})),
        ),
        gateway=gateway,
        settings=SimpleNamespace(
            get_gateway_enabled=lambda: gateway is not None,
            get_gateway_port=lambda: 9997,
            get_vision_max_image_size_mb=lambda: 5,
            get_remote_inference_timeout=lambda: 1.0,
        ),
        hub_client=SimpleNamespace(),
        peer_metadata={},
        _provider_supports_voice=lambda provider: False,
    )
    for name in ("menu_for_peer", "build_p2p_provider_info", "menu_tariff", "get_gateway_state",
                 "rotate_gateway_key", "get_gateway_client_lines",
                 "get_peer_provider_menu", "validate_firewall_rules"):
        setattr(service, name, getattr(CoreService, name).__get__(service, SimpleNamespace))
    service.menu_settings = CoreService.menu_settings
    return service


def _gateway(tmp_path: Path, service_holder: dict) -> GatewayServer:
    """A server whose `_core` is filled in after the service exists."""
    server = GatewayServer(MagicMock(), host=GATEWAY_HOST, port=0,
                           key_path=tmp_path / GATEWAY_KEY_NAME)
    service_holder["server"] = server
    return server


# --- (1) get_gateway_state ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_state_says_configured_and_listening_apart(tmp_path):
    """A door that refused to open is configured and not running, and a card
    that read only one of the two would say the wrong thing about both."""
    holder = {}
    server = _gateway(tmp_path, holder)
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    state = await service.get_gateway_state()

    assert state["enabled"] is True
    assert state["running"] is False, "nothing has started the listener"
    assert state["bind"] == GATEWAY_HOST
    assert state["port"] == 0, "the port as configured; the bound one appears after start"
    assert state["serving_local"] == [LOCAL] and state["serving_vendor"] == []
    assert state["serving_error"] is None
    assert state["compute_enabled"] is True
    assert state["key_file"].endswith(GATEWAY_KEY_NAME)
    assert state["key_masked"] is None, "no key exists before the first start"


@pytest.mark.asyncio
async def test_the_state_carries_a_masked_key_and_never_the_key(tmp_path):
    holder = {}
    server = _gateway(tmp_path, holder)
    (tmp_path / GATEWAY_KEY_NAME).write_text("sekrit-key-1234", encoding="utf-8")
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    state = await service.get_gateway_state()

    assert state["key_masked"] == mask_gateway_key("sekrit-key-1234") == "sk-…1234"
    assert "sekrit-key-1234" not in json.dumps(state)


@pytest.mark.asyncio
async def test_a_serving_list_that_cannot_be_classified_is_named_not_hidden(tmp_path):
    """The card must be able to say why the door serves nothing, so the
    refusal travels as words rather than as an empty list."""
    holder = {}
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": ["gone"]})
    service = _service(tmp_path, firewall, gateway=_gateway(tmp_path, holder))

    state = await service.get_gateway_state()

    assert state["serving_local"] == [] and state["serving_vendor"] == []
    assert "gone" in state["serving_error"]


@pytest.mark.asyncio
async def test_the_state_answers_with_no_gateway_at_all(tmp_path, monkeypatch):
    """`[gateway] enabled` off is the default, and the card still has to render
    something honest rather than an error."""
    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", tmp_path)
    firewall = _rules(tmp_path, compute={"enabled": False})
    service = _service(tmp_path, firewall, gateway=None)

    state = await service.get_gateway_state()

    assert state["enabled"] is False and state["running"] is False
    assert state["port"] == 9997 and state["key_masked"] is None
    assert state["compute_enabled"] is False


# --- (2) rotate_gateway_key ---------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_swaps_the_key_in_the_running_listener(tmp_path):
    """The old key is refused on the next request with no restart, the new one
    is served, the file holds the new one, and a restart reads it back."""
    import aiohttp

    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    core = _service(tmp_path, firewall)
    server = GatewayServer(core, host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    core.gateway = server
    await server.start()
    try:
        old = (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip()
        url = f"http://{GATEWAY_HOST}:{server.port}/v1/models"

        async def get(key):
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {key}"}) as resp:
                    return resp.status

        assert await get(old) == 200

        answer = await core.rotate_gateway_key()
        new = answer["key"]

        assert answer["status"] == "success"
        assert new != old and answer["key_masked"] == mask_gateway_key(new)
        assert await get(old) == 401, "the old key stops working with no restart"
        assert await get(new) == 200
        assert (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip() == new
    finally:
        await server.stop()

    restarted = GatewayServer(core, host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    core.gateway = restarted
    await restarted.start()
    try:
        assert restarted._key == new, "a restart reads the rotated key, it does not mint a third"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_rotation_with_no_listener_still_rewrites_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", tmp_path)
    (tmp_path / GATEWAY_KEY_NAME).write_text("the-old-one", encoding="utf-8")
    service = _service(tmp_path, _rules(tmp_path), gateway=None)

    answer = await service.rotate_gateway_key()

    assert answer["key"] != "the-old-one"
    assert (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip() == answer["key"]


# --- (3) get_gateway_client_lines --------------------------------------------


@pytest.mark.asyncio
async def test_the_client_lines_carry_the_key_and_the_served_aliases(tmp_path):
    holder = {}
    server = _gateway(tmp_path, holder)
    (tmp_path / GATEWAY_KEY_NAME).write_text("sekrit-key-1234", encoding="utf-8")
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    answer = await service.get_gateway_client_lines()

    clients = {row["client"]: row["text"] for row in answer["lines"]}
    assert set(clients) == {"continue", "cursor", "claude_code", "curl"}
    for text in clients.values():
        assert "sekrit-key-1234" in text, "these lines exist to be pasted (Mike's call)"
    assert LOCAL in clients["continue"] and LOCAL in clients["claude_code"]
    assert answer["key_masked"] == "sk-…1234"
    json.loads(clients["continue"])


def test_the_documented_snippets_are_this_functions_own_output():
    """The page and the button render from one place, or they drift: the two
    blocks `docs/CONFIGURATION.md` shows are compared byte for byte."""
    doc = io.open(DOCS, encoding="utf-8").read()
    rendered = {row["client"]: row["text"] for row in
                client_config_lines(9997, "<contents of ~/.dpc/.gateway_key>", [LOCAL])}

    assert rendered["continue"] in doc, "the Continue example is no longer what the command renders"
    assert rendered["claude_code"] in doc, "the Claude Code example has drifted from the command"


def test_a_door_that_has_never_started_says_so_where_the_key_would_be():
    (line,) = [row for row in client_config_lines(9997, "", []) if row["client"] == "curl"]
    assert "<alias>" in client_config_lines(9997, "k", [])[0]["text"]
    assert line["text"].endswith('Bearer "')


# --- (4) get_peer_provider_menu ----------------------------------------------


def _sharing_service(tmp_path, **overrides):
    compute = {"enabled": True, "allow_nodes": [PEER], "serving_local": [LOCAL]}
    transcription = {"enabled": True, "allow_nodes": [PEER]}
    compute.update(overrides.pop("compute", {}))
    transcription.update(overrides.pop("transcription", {}))
    firewall = _rules(tmp_path, compute=compute, transcription=transcription,
                      **overrides.pop("blocks", {}))
    providers = overrides.pop("providers", {
        LOCAL: _Provider("ollama", "gemma3:27b"),
        WHISPER: _Provider("local_whisper", "whisper-large-v3-turbo"),
        "deepseek_pro": _Provider("deepseek", "deepseek-v4-pro"),
    })
    return _service(tmp_path, firewall, providers=providers, **overrides)


@pytest.mark.asyncio
async def test_the_preview_is_the_rows_the_peer_would_be_sent(tmp_path):
    service = _sharing_service(tmp_path, peers={PEER: object()})

    answer = await service.get_peer_provider_menu(PEER)

    assert answer["peer_id"] == PEER and answer["connected"] is True and answer["known"] is True
    assert answer["allowed"] is True and answer["reason"] is None
    assert [row["alias"] for row in answer["rows"]] == [LOCAL, WHISPER]
    assert "deepseek_pro" not in json.dumps(answer["rows"]), (
        "an alias this node does not serve is not named to a guest"
    )


@pytest.mark.asyncio
async def test_both_senders_of_the_menu_produce_the_same_rows(tmp_path):
    """The card this fixes: the notify path had no type branch, so a firewall
    save narrowed a connected peer's menu and dropped its transcription row."""
    from dpc_client_core.p2p_coordinator import P2PCoordinator

    service = _sharing_service(tmp_path, peers={PEER: object()})
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service._pending_providers_requests = {}
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service._notify_peers_of_provider_changes = (
        CoreService._notify_peers_of_provider_changes.__get__(service, SimpleNamespace)
    )
    coordinator = P2PCoordinator(service)

    await coordinator.handle_get_providers_request(PEER)
    on_request = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]

    await service._notify_peers_of_provider_changes()
    on_save = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]

    preview = (await service.get_peer_provider_menu(PEER))["rows"]
    assert on_request == on_save == preview
    assert [row["type"] for row in on_save] == ["ollama", "local_whisper"]


@pytest.mark.asyncio
async def test_a_free_peer_and_a_paying_peer_differ_only_in_the_tariff(tmp_path):
    blocks = {"node_groups": {GROUP: [PEER, STRANGER]}}
    service = _sharing_service(
        tmp_path,
        compute={"allow_nodes": [PEER, STRANGER], "free_nodes": [PEER],
                 "currency": "USD",
                 "serving_tariff": {LOCAL: [{"from": "2026-01-01", "in": 1.0, "out": 2.0}]}},
        transcription={"enabled": False},
        blocks=blocks,
    )

    free = (await service.get_peer_provider_menu(PEER))["rows"]
    paid = (await service.get_peer_provider_menu(STRANGER))["rows"]

    assert len(free) == len(paid) == 1
    assert free[0]["tariff"]["in"] == 0.0 and free[0]["tariff"]["out"] == 0.0
    assert free[0]["tariff"]["free"] is True
    assert paid[0]["tariff"]["in"] == 1.0 and paid[0]["tariff"]["out"] == 2.0
    assert paid[0]["tariff"]["free"] is False
    assert {k: v for k, v in free[0].items() if k != "tariff"} == \
           {k: v for k, v in paid[0].items() if k != "tariff"}


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_a_transcription_row_appears_exactly_when_that_permission_allows_it(tmp_path, allowed):
    service = _sharing_service(
        tmp_path, transcription={"enabled": True, "allow_nodes": [PEER] if allowed else []},
    )

    aliases = [row["alias"] for row in (await service.get_peer_provider_menu(PEER))["rows"]]

    assert (WHISPER in aliases) is allowed
    assert LOCAL in aliases, "the inference row is decided by the other permission"


@pytest.mark.asyncio
async def test_a_refused_peer_gets_an_empty_menu_and_a_reason_in_words(tmp_path):
    service = _sharing_service(tmp_path, transcription={"enabled": False})

    answer = await service.get_peer_provider_menu(STRANGER)

    assert answer["rows"] == [] and answer["allowed"] is False
    assert STRANGER in answer["reason"]
    assert "compute.allow_nodes" in answer["reason"] and "transcription" in answer["reason"]
    assert answer["connected"] is False and answer["known"] is False


@pytest.mark.asyncio
async def test_an_allowed_peer_with_nothing_designated_is_told_that_instead(tmp_path):
    """Allowed and served nothing is a different sentence from refused, and the
    owner acts on a different setting."""
    service = _sharing_service(
        tmp_path, compute={"serving_local": []}, transcription={"enabled": False},
    )

    answer = await service.get_peer_provider_menu(PEER)

    assert answer["rows"] == [] and answer["allowed"] is True
    assert "compute.serving_local" in answer["reason"]


@pytest.mark.asyncio
async def test_the_menu_command_needs_a_peer(tmp_path):
    service = _sharing_service(tmp_path)

    assert (await service.get_peer_provider_menu(""))["status"] == "error"


# --- (5) validate_firewall_rules ---------------------------------------------


@pytest.mark.asyncio
async def test_a_good_rules_object_validates_and_a_bad_one_names_its_reasons(tmp_path):
    service = _service(tmp_path, _rules(tmp_path))

    good = await service.validate_firewall_rules(
        {"compute": {"enabled": True, "allow_nodes": [PEER]}}
    )
    assert good["status"] == "success" and good["valid"] is True and good["errors"] == []

    bad = await service.validate_firewall_rules(
        {"compute": {"enabled": "yes", "allow_nodes": "not-a-list"}, "nonsense": {}}
    )
    assert bad["status"] == "success" and bad["valid"] is False
    assert any("compute.enabled" in e for e in bad["errors"])
    assert any("allow_nodes" in e for e in bad["errors"])
    assert any("nonsense" in e for e in bad["errors"])


@pytest.mark.asyncio
async def test_validating_writes_nothing(tmp_path):
    path = tmp_path / "privacy_rules.json"
    firewall = _rules(tmp_path, compute={"enabled": True})
    before = path.read_bytes()
    service = _service(tmp_path, firewall)

    await service.validate_firewall_rules({"compute": {"enabled": False}})

    assert path.read_bytes() == before
    assert firewall.compute_enabled is True


@pytest.mark.asyncio
async def test_a_string_is_refused_by_name_rather_than_answered_invalid(tmp_path):
    """The old signature: a string reached a validator that calls `.keys()`."""
    service = _service(tmp_path, _rules(tmp_path))

    answer = await service.validate_firewall_rules('{"compute": {}}')

    assert answer["status"] == "error" and "object" in answer["message"]


# --- the five are reachable --------------------------------------------------


def test_every_new_command_is_in_the_allowlist_and_on_coreservice():
    for command in ("get_gateway_state", "rotate_gateway_key", "get_gateway_client_lines",
                    "get_peer_provider_menu", "validate_firewall_rules"):
        assert command in ALLOWED_COMMANDS, f"{command} is not reachable from the UI"
        assert hasattr(CoreService, command)
