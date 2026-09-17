"""A menu row carries the price and the dials, so a guest decides before it calls.

The rows of PROVIDERS_RESPONSE (DPTP §3.5) said what a model is and nothing
about what a call on it costs or runs at: a guest learned the tariff from the
answer it had already paid for, and learned the temperature, the output ceiling
and the quantisation not at all. Mike's rule of 2026-09-14 is that a guest
choosing a remote inference sees the host's full effective settings and the
tariff first, the ones it cannot change included.

Two rules the assertions here are about:

* the tariff is the *same resolution the call is priced by* — the firewall's
  own `tariff_for`, per peer, so the menu quotes the rate the receipt will
  carry — and absent means nothing is declared (the v1 gift), which is not the
  same as free;
* the settings are fail-closed: a provider states what it will actually send,
  and a key it cannot vouch for is absent. A temperature that reaches no
  request must not reach a row.

The firewall is the real one over a rules file; the providers are the real
classes with the attributes their `effective_settings` reads, built without
their constructors — a GGUF and a llama-server child are not needed to ask a
provider what it would send, and must not be, since these tests run on three
machines and the model lives on one.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.providers.base import AIProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.service import CoreService
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_ALIAS,
    REMOTE_VISION_ALIAS,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    VENDOR,
    _Provider,
    _key,
    _request,
    _running,
    _service,
)

ALIAS = "local_llama"
PAYING = "dpc-node-" + "b" * 32
FREE = "dpc-node-" + "c" * 32
MODEL = "gpt-oss-120b"
TARIFF = {"from": "2026-09-01", "in": 20.0, "out": 60.0}


class MenuProvider:
    """What the row builder reads off a provider, and nothing else."""

    def __init__(self, settings=None, model=MODEL, ptype="llamacpp_server"):
        self.model = model
        self.config = {"type": ptype}
        self._settings = settings

    def supports_vision(self):
        return False

    def effective_settings(self):
        if self._settings is None:
            raise AssertionError("this double states no settings")
        return dict(self._settings)


def _firewall(tmp_path: Path, **compute) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    block = {
        "enabled": True,
        "serving_local": [ALIAS],
        "allow_nodes": [PAYING, FREE],
    }
    block.update(compute)
    rules.write_text(json.dumps({"compute": block}), encoding="utf-8")
    return ContextFirewall(rules)


def _menu_service(firewall, providers):
    """A stand-in for CoreService with what the two menu builders read."""
    service = SimpleNamespace(
        firewall=firewall,
        llm_manager=SimpleNamespace(
            providers=providers,
            lookup_context_window=lambda model: None,
            get_context_window=lambda model: None,
        ),
        p2p_manager=SimpleNamespace(peers={}, node_id="dpc-node-" + "a" * 32,
                                    send_message_to_peer=AsyncMock()),
        _provider_supports_voice=lambda provider: False,
    )
    service.build_p2p_provider_info = (
        lambda alias, provider, **kwargs:
        CoreService.build_p2p_provider_info(service, alias, provider, **kwargs)
    )
    # The row builder is only half the menu; the selection is CoreService's too,
    # and both senders reach it through this one name.
    service.menu_for_peer = lambda peer_id: CoreService.menu_for_peer(service, peer_id)
    return service


def _row(firewall, peer_id, provider=None):
    provider = provider or MenuProvider()
    service = _menu_service(firewall, {ALIAS: provider})
    return service.build_p2p_provider_info(ALIAS, provider, peer_id=peer_id)


# --- (1) the tariff on the row -------------------------------------------------


def test_a_declared_tariff_reaches_the_row_with_its_unit(tmp_path):
    row = _row(_firewall(tmp_path, currency="RUB", serving_tariff={ALIAS: [TARIFF]}), PAYING)

    assert row["tariff"] == {
        "in": 20.0, "out": 60.0, "currency": "RUB", "from": "2026-09-01",
        "unit": "per_1m_tokens", "free": False,
    }


def test_a_free_listed_peer_is_quoted_zero_and_told_that_it_is_free(tmp_path):
    """The same declaration, resolved for a peer on the free list: the rates the
    receipt will carry are zeros, and `free` names why they are."""
    firewall = _firewall(
        tmp_path, currency="RUB", serving_tariff={ALIAS: [TARIFF]}, free_nodes=[FREE],
    )

    free, paying = _row(firewall, FREE), _row(firewall, PAYING)

    assert free["tariff"]["in"] == 0 and free["tariff"]["out"] == 0
    assert free["tariff"]["free"] is True
    assert paying["tariff"]["in"] == 20.0 and paying["tariff"]["free"] is False
    assert free["tariff"]["from"] == paying["tariff"]["from"] == "2026-09-01"


def test_the_newest_entry_on_or_before_today_is_the_one_quoted(tmp_path):
    firewall = _firewall(tmp_path, currency="USD", serving_tariff={ALIAS: [
        {"from": "2020-01-01", "in": 1.0, "out": 2.0},
        {"from": "2021-01-01", "in": 3.0, "out": 4.0},
        {"from": "2999-01-01", "in": 99.0, "out": 99.0},
    ]})

    assert _row(firewall, PAYING)["tariff"]["from"] == "2021-01-01"
    assert _row(firewall, PAYING)["tariff"]["in"] == 3.0


def test_an_undeclared_alias_carries_no_tariff_at_all(tmp_path):
    """Absence is the v1 gift. A zero here would be a declared price, and a peer
    reading one would believe the host had chosen it."""
    row = _row(_firewall(tmp_path), PAYING)

    assert "tariff" not in row


def test_a_free_listed_peer_of_a_host_that_declared_nothing_is_still_told_nothing(tmp_path):
    row = _row(_firewall(tmp_path, free_nodes=[FREE]), FREE)

    assert "tariff" not in row


def test_a_row_built_for_nobody_carries_no_tariff(tmp_path):
    """The tariff is per peer — there is no free list to resolve without one."""
    row = _row(_firewall(tmp_path, currency="RUB", serving_tariff={ALIAS: [TARIFF]}), None)

    assert "tariff" not in row


def test_a_firewall_that_cannot_price_the_row_still_answers_the_menu(tmp_path):
    """A tariff that fails to resolve must not cost the guest the whole menu:
    the row is unpriced, exactly as the served call would be."""
    class _Raising:
        def tariff_for(self, *args, **kwargs):
            raise RuntimeError("the rules are unreadable")

    row = _row(_Raising(), PAYING)

    assert "tariff" not in row and row["alias"] == ALIAS


# --- (2) the settings on the row ----------------------------------------------


def _llamacpp(**attrs):
    provider = object.__new__(LlamaServerProvider)
    provider.config = {"type": "llamacpp_server", "gguf_path": str(Path("/models/Qwen3-27B-Q5_K_M.gguf"))}
    provider._temperature_explicit = None
    provider.top_p = None
    provider.top_k = None
    provider.max_tokens = 8192
    for key, value in attrs.items():
        setattr(provider, key, value)
    return provider


def test_a_llama_server_alias_states_its_sampling_its_ceiling_and_its_file():
    settings = _llamacpp(_temperature_explicit=0.7, top_p=0.95, top_k=20).effective_settings()

    assert settings == {
        "temperature": 0.7, "top_p": 0.95, "top_k": 20,
        "max_output_tokens": 8192, "variant": "Qwen3-27B-Q5_K_M.gguf",
    }


def test_the_temperature_nobody_configured_is_still_the_one_that_is_sent():
    """`_sampling_params` sends a temperature on every call, the 1.0 default
    included, so the row states it: this is what the call will run at."""
    assert _llamacpp().effective_settings()["temperature"] == 1.0


def test_the_gguf_directory_stays_on_the_host():
    settings = _llamacpp().effective_settings()

    assert settings["variant"] == "Qwen3-27B-Q5_K_M.gguf"
    assert "models" not in settings["variant"]


def _deepseek(thinking: bool):
    provider = object.__new__(DeepSeekProvider)
    provider.config = {"type": "deepseek", "temperature": 0.6}
    provider.thinking_enabled = thinking
    provider._temperature_explicit = 0.6
    provider.top_p = 0.9
    provider.max_tokens = 4096
    return provider


def test_a_vendor_that_ignores_the_dial_while_thinking_does_not_advertise_it():
    """DeepSeek withholds temperature and top_p from a thinking call, measured
    2026-08-15. A row quoting the configured 0.6 there would promise a control
    the request never carries."""
    settings = _deepseek(thinking=True).effective_settings()

    assert settings == {"max_output_tokens": 4096}


def test_the_same_alias_with_thinking_off_states_the_sampling_it_then_sends():
    settings = _deepseek(thinking=False).effective_settings()

    assert settings == {"max_output_tokens": 4096, "temperature": 0.6, "top_p": 0.9}


def test_an_ollama_alias_states_the_options_it_forwards():
    provider = object.__new__(OllamaProvider)
    provider.config = {"type": "ollama", "temperature": 0.8, "top_k": 40, "num_predict": 2048}

    assert provider.effective_settings() == {
        "temperature": 0.8, "top_k": 40, "max_output_tokens": 2048,
    }


def test_an_ollama_ceiling_that_means_no_ceiling_is_not_a_ceiling():
    """`num_predict: -1` is Ollama's «run to the end»; reported as a limit it
    would read as a ceiling of minus one token."""
    provider = object.__new__(OllamaProvider)
    provider.config = {"type": "ollama", "num_predict": -1}

    assert provider.effective_settings() == {}


def test_a_provider_that_sends_no_sampling_states_none_of_it():
    """Gemini's adapter passes neither a temperature nor a ceiling, so a
    configured one reaches no request and must reach no row."""
    provider = object.__new__(GeminiProvider)
    provider.config = {"type": "gemini", "temperature": 0.3}

    assert provider.effective_settings() == {}


def test_the_base_states_a_configured_temperature_and_invents_nothing():
    stated = AIProvider("vendor", {"type": "openai", "temperature": 0.2})
    silent = AIProvider("vendor", {"type": "openai", "max_tokens": 4096})

    assert stated.effective_settings() == {"temperature": 0.2}
    assert silent.effective_settings() == {}


def test_a_dial_that_is_not_a_number_is_not_a_dial():
    for value in ("hot", True, float("nan"), None):
        assert AIProvider("v", {"type": "openai", "temperature": value}).effective_settings() == {}


def test_the_settings_reach_the_row_and_an_empty_set_leaves_no_key(tmp_path):
    firewall = _firewall(tmp_path)
    stated = _row(firewall, PAYING, MenuProvider(settings={"temperature": 1.0}))
    silent = _row(firewall, PAYING, MenuProvider(settings={}))

    assert stated["settings"] == {"temperature": 1.0}
    assert "settings" not in silent


def test_a_provider_that_cannot_state_its_settings_still_gets_a_row(tmp_path):
    row = _row(_firewall(tmp_path), PAYING, MenuProvider())

    assert "settings" not in row and row["model"] == MODEL


# --- (3) both menu paths, one row ---------------------------------------------


@pytest.mark.asyncio
async def test_the_asked_menu_and_the_notified_menu_carry_the_same_row(tmp_path):
    """One builder, two callers (`handle_get_providers_request` and
    `_notify_peers_of_provider_changes`): a guest is told the same price and the
    same dials whether it asked or was told."""
    firewall = _firewall(
        tmp_path, currency="RUB", serving_tariff={ALIAS: [TARIFF]}, free_nodes=[FREE],
    )
    provider = MenuProvider(settings={"temperature": 1.0, "max_output_tokens": 8192})
    service = _menu_service(firewall, {ALIAS: provider})
    service.p2p_manager.peers = {FREE: object()}

    coordinator = P2PCoordinator.__new__(P2PCoordinator)
    coordinator.service = service
    coordinator.p2p_manager = service.p2p_manager
    await coordinator.handle_get_providers_request(FREE)
    asked = service.p2p_manager.send_message_to_peer.call_args[0][1]

    await CoreService._notify_peers_of_provider_changes(service)
    notified = service.p2p_manager.send_message_to_peer.call_args[0][1]

    assert asked["payload"]["providers"] == notified["payload"]["providers"]
    (row,) = asked["payload"]["providers"]
    assert row["tariff"]["free"] is True
    assert row["settings"] == {"temperature": 1.0, "max_output_tokens": 8192}


@pytest.mark.asyncio
async def test_two_peers_asking_one_host_are_quoted_their_own_price(tmp_path):
    firewall = _firewall(
        tmp_path, currency="RUB", serving_tariff={ALIAS: [TARIFF]}, free_nodes=[FREE],
    )
    service = _menu_service(firewall, {ALIAS: MenuProvider(settings={})})
    coordinator = P2PCoordinator.__new__(P2PCoordinator)
    coordinator.service = service
    coordinator.p2p_manager = service.p2p_manager

    quotes = {}
    for peer in (FREE, PAYING):
        await coordinator.handle_get_providers_request(peer)
        (row,) = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]
        quotes[peer] = row["tariff"]

    assert quotes[FREE]["free"] is True and quotes[PAYING]["free"] is False
    assert quotes[PAYING]["in"] == 20.0


# --- (4) the same two keys on the IDE door's model list ------------------------


@pytest.mark.asyncio
async def test_the_gateway_lists_this_nodes_own_alias_with_its_tariff_and_its_settings(tmp_path):
    """`/v1/models` is the same menu on the loopback side. The tariff on a local
    row is what this node declares for the alias — what it would quote a peer —
    since the caller here is this node and pays itself nothing."""
    local = _Provider("ollama", "qwen3:8b")
    local.effective_settings = lambda: {"temperature": 0.4, "max_output_tokens": 1024}
    service = _service(
        tmp_path,
        dict(BOTH_LISTS, currency="RUB", serving_tariff={LOCAL: [TARIFF]}),
        providers={LOCAL: local, VENDOR: _Provider("deepseek", "deepseek-v4-flash")},
    )

    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))

    rows = {row["id"]: row for row in json.loads(text)["data"]}
    assert status == 200
    assert rows[LOCAL]["tariff"] == {
        "in": 20.0, "out": 60.0, "currency": "RUB", "from": "2026-09-01",
        "unit": "per_1m_tokens", "free": False,
    }
    assert rows[LOCAL]["settings"] == {"temperature": 0.4, "max_output_tokens": 1024}
    assert "tariff" not in rows[VENDOR] and "settings" not in rows[VENDOR]


@pytest.mark.asyncio
async def test_a_peers_row_reaches_the_ide_door_carrying_what_the_host_said(tmp_path):
    """Copied, never recomputed: on a `remote:` row the price and the dials are
    the host's own statements, and an OpenAI client ignores the keys it does not
    know."""
    service = _peer_service(tmp_path)
    quoted = {"in": 20.0, "out": 60.0, "currency": "RUB", "from": "2026-09-01",
              "unit": "per_1m_tokens", "free": False}
    rows = service.peer_metadata[PEER]["providers"]
    rows[0]["tariff"] = quoted
    rows[0]["settings"] = {"temperature": 1.0, "variant": "Qwen3-27B-Q5_K_M.gguf"}

    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))

    listed = {row["id"]: row for row in json.loads(text)["data"]}
    assert status == 200
    assert listed[f"remote:{PEER}:{REMOTE_ALIAS}"]["tariff"] == quoted
    assert listed[f"remote:{PEER}:{REMOTE_ALIAS}"]["settings"]["variant"] == "Qwen3-27B-Q5_K_M.gguf"
    assert "tariff" not in listed[f"remote:{PEER}:{REMOTE_VISION_ALIAS}"]
