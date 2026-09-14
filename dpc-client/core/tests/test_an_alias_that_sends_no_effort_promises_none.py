"""`served_effort` names the word the provider sent, and nothing where it sent none.

Two of the ten provider classes read the alias's configured `reasoning_effort`.
For the rest the doors derived a word from that same key and put it on the usage
row and on the guest's response, so a row could name a rung the engine was never
asked for — an Ollama alias reads `think`, not `reasoning_effort`, and a word
configured there is inert.

What is pinned here is the declaration and its three consequences. Each class
states which effort words it can put on the wire
(`AIProvider.reasoning_words_served`, `[]` by default and fail-closed); a class
that states none gets `served_effort: null`, a menu row whose `reasoning_words`
is `[]` and whose `reasoning_default` is null, and a refusal at both doors for
every word a caller names — `off` included, since a provider with no effort
channel has no way of saying no either.

No network: every provider here is built from a config dict, and the one
capability lookup Ollama would make is replaced.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.gateway import Gateway, GatewayError, _effort_the_alias_knows
from dpc_client_core.llm_manager import reported_served_effort
from dpc_client_core.p2p_coordinator import EffortRefused
from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.base import (
    AIProvider,
    REASONING_OFF,
    declared_reasoning_words,
    effective_reasoning_default,
    reasoning_word_for,
)
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.dpc_agent_provider import DpcAgentProvider
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.github_models_provider import GitHubModelsProvider
from dpc_client_core.providers.gigachat_provider import GigaChatProvider
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from dpc_client_core.service import CoreService

from tests.test_a_remote_agent_gets_its_reasoning_channel_back import _coordinator
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    _chat as _gateway_chat,
    _key as _gateway_key,
    _providers as _gateway_providers,
    _request as _gateway_request,
    _running as _gateway_running,
    _service as _gateway_service,
)

# The alias the gateway tests below serve: one of this node's own, behind a real
# provider class whose only effort word is `off`.
SWITCH = "glm_switch"


# --- builders, no network ------------------------------------------------------


def _openai(**config):
    return OpenAICompatibleProvider("openai_test", {"model": "o3", "api_key": "k", **config})


def _zai(**config):
    return ZaiProvider("zai_test", {"model": "glm-5.2", "api_key": "k", **config})


def _deepseek(**config):
    return DeepSeekProvider("ds_test", {"model": "deepseek-v4-flash", "api_key": "k", **config})


def _ollama(monkeypatch, *, thinks=True, **config):
    """An Ollama alias whose daemon is never asked: `supports_thinking` is the
    one capability lookup this class makes off the network."""
    provider = OllamaProvider("ollama_test", {"model": "qwen3:8b", **config})
    monkeypatch.setattr(type(provider), "supports_thinking", lambda self: thinks)
    return provider


# --- (1) what each class declares ----------------------------------------------

# A class that sends no effort at all: the base's `[]` stands, untouched by the
# subclass. Asserted on the class rather than an instance for the three whose
# constructors reach for an agent, a peer or a Whisper model.
NO_EFFORT_CLASSES = [
    AnthropicProvider,
    OpenAICompatibleProvider,
    GeminiProvider,
    GigaChatProvider,
    GitHubModelsProvider,
    DpcAgentProvider,
    RemotePeerProvider,
    LocalWhisperProvider,
]


@pytest.mark.parametrize("cls", NO_EFFORT_CLASSES, ids=lambda c: c.__name__)
def test_a_class_that_sends_no_effort_declares_none(cls):
    assert cls.reasoning_words_served is AIProvider.reasoning_words_served
    assert cls.reasoning_default_served is AIProvider.reasoning_default_served


def test_the_base_declaration_is_empty_and_not_the_shared_scale():
    """`[]` and `None` are different answers: the first is «no effort crosses»,
    the second «the shared scale stands in». A base that answered None would put
    every silent class back on the scale it cannot serve."""
    assert AIProvider.reasoning_words_served(object()) == []
    assert AIProvider.reasoning_default_served(object()) is None


def test_a_silent_class_promises_no_word_and_names_none(monkeypatch):
    monkeypatch.setenv("K", "k")
    provider = _openai()
    assert declared_reasoning_words(provider) == ([], None)
    assert effective_reasoning_default(provider) is None
    assert reasoning_word_for(provider, "high") is None
    assert reasoning_word_for(provider, REASONING_OFF) is None


def test_the_configured_word_is_not_reported_by_a_class_that_never_reads_it():
    """The defect in one line: an alias may carry `reasoning_effort` in
    providers.json, and on a class that reads `think` — or nothing — that word
    is inert. It must not come back as the rung served."""
    provider = _openai(reasoning_effort="high")
    assert provider.config["reasoning_effort"] == "high"
    assert effective_reasoning_default(provider) is None


def test_deepseek_declares_the_shared_scale_and_reads_its_configured_word():
    assert declared_reasoning_words(_deepseek()) == (None, None)
    assert effective_reasoning_default(_deepseek(reasoning_effort="low")) == "low"


def test_llama_server_keeps_its_model_s_own_ladder():
    """The class inherits DeepSeek's declaration — «the shared scale» — which is
    what lets the template's own words, where it read them, still answer."""
    assert LlamaServerProvider.reasoning_words_served is DeepSeekProvider.reasoning_words_served
    assert DeepSeekProvider.reasoning_words_served(SimpleNamespace()) is None


def test_zai_declares_off_and_nothing_else():
    """GLM's thinking is a switch: `off` is `{type: disabled}`, and every level
    lands on the same `enabled` that no word at all lands on."""
    provider = _zai()
    assert declared_reasoning_words(provider) == ([REASONING_OFF], None)
    assert effective_reasoning_default(provider) is None
    assert reasoning_word_for(provider, REASONING_OFF) == REASONING_OFF
    assert reasoning_word_for(provider, "high") is None


def test_ollama_declares_the_shared_scale_where_the_model_can_think(monkeypatch):
    assert declared_reasoning_words(_ollama(monkeypatch)) == (None, None)


def test_ollama_declares_off_alone_where_the_model_cannot_think(monkeypatch):
    """A level sent to such a model is refused 400 by the daemon and therefore
    dropped; `think=False` is the one value every model accepts."""
    assert declared_reasoning_words(_ollama(monkeypatch, thinks=False)) == ([REASONING_OFF], None)


def test_ollama_declares_off_alone_where_the_alias_switched_thinking_off(monkeypatch):
    provider = _ollama(monkeypatch, think=False)
    assert declared_reasoning_words(provider) == ([REASONING_OFF], None)


def test_an_ollama_alias_reports_what_think_carried_not_what_config_named(monkeypatch):
    """The named case of the card: `reasoning_effort` on an Ollama alias is read
    by nobody, so it is not the rung the row names."""
    provider = _ollama(monkeypatch, reasoning_effort="high")
    assert provider._served_effort("low") == "low"
    assert provider._served_effort(REASONING_OFF) == REASONING_OFF
    # `think=True` is «reason», at a depth the daemon chooses and no word names.
    assert provider._served_effort(None) is None
    assert provider.reasoning_default_served() is None


def test_an_ollama_alias_that_cannot_think_serves_off_and_nothing_else(monkeypatch):
    provider = _ollama(monkeypatch, thinks=False)
    assert provider._served_effort("high") is None
    assert provider._served_effort(REASONING_OFF) == REASONING_OFF


def test_an_ollama_alias_reports_the_word_it_clamped_to(monkeypatch):
    """`max` dies in the SDK's own typing, so `high` goes instead — and `high`
    is the word the row must name."""
    assert _ollama(monkeypatch)._served_effort("max") == "high"


def test_zai_reports_off_only_where_it_disabled_thinking():
    provider = _zai()
    assert provider._served_effort(provider._build_extra_body(REASONING_OFF)) == REASONING_OFF
    assert provider._served_effort(provider._build_extra_body("high")) is None
    assert provider._served_effort(provider._build_extra_body(None)) is None
    assert provider._served_effort(None) is None


def test_a_silent_class_reports_no_word_on_its_usage_row(monkeypatch):
    monkeypatch.setenv("K", "k")
    provider = _openai()
    provider._record_last_usage({"prompt_tokens": 12, "completion_tokens": 5})
    assert reported_served_effort(provider) is None


# --- (2) the menu row a guest chooses on ---------------------------------------


def _menu_row(alias, provider):
    stub = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={alias: provider},
                                    lookup_context_window=lambda model: None),
        _provider_supports_voice=lambda p: False,
        firewall=None,
    )
    return CoreService.build_p2p_provider_info(stub, alias, provider)


def test_a_silent_alias_offers_a_guest_no_effort_word(monkeypatch):
    monkeypatch.setenv("K", "k")
    row = _menu_row("openai_test", _openai(reasoning_effort="high"))
    assert row["reasoning_words"] == []
    assert row["reasoning_default"] is None


def test_a_zai_alias_offers_off_alone():
    row = _menu_row("zai_test", _zai())
    assert row["reasoning_words"] == [REASONING_OFF]
    assert row["reasoning_default"] is None


def test_an_alias_on_the_shared_scale_still_quotes_no_words_of_its_own():
    """Absent, not empty: the shared scale is what a receiver falls back to, and
    an empty list would tell it the alias serves nothing."""
    row = _menu_row("ds_test", _deepseek(reasoning_effort="low"))
    assert "reasoning_words" not in row
    assert "reasoning_default" not in row


# --- (3) the peer door ---------------------------------------------------------


@pytest.mark.parametrize("word", ["high", "low", "max", REASONING_OFF, "xhigh"])
def test_the_peer_door_refuses_every_word_for_a_silent_alias(monkeypatch, word):
    monkeypatch.setenv("K", "k")
    coord = _coordinator(alias="quiet", provider=_openai(reasoning_effort="high"))

    with pytest.raises(EffortRefused) as refused:
        coord._effort_for_peer("peer", word, "quiet")

    assert "no reasoning effort at all" in str(refused.value)


def test_the_peer_door_serves_a_silent_alias_under_no_name(monkeypatch):
    """A guest that asks for nothing is still served — at the host's own default,
    which no word here describes."""
    monkeypatch.setenv("K", "k")
    coord = _coordinator(alias="quiet", provider=_openai(reasoning_effort="high"))

    assert coord._effort_for_peer("peer", None, "quiet") is None


def test_the_peer_door_serves_off_on_a_zai_alias():
    coord = _coordinator(alias="glm", provider=_zai())

    assert coord._effort_for_peer("peer", REASONING_OFF, "glm") == REASONING_OFF
    with pytest.raises(EffortRefused):
        coord._effort_for_peer("peer", "high", "glm")


# --- (4) the local door --------------------------------------------------------


def test_the_local_door_refuses_a_word_an_empty_vocabulary_cannot_take():
    with pytest.raises(GatewayError) as refused:
        _effort_the_alias_knows("high", [], serves="model 'quiet'", what="reasoning_effort")

    assert refused.value.status == 400
    assert refused.value.code == "invalid_value"
    assert "no reasoning effort at all" in refused.value.message


def test_the_local_door_refuses_off_too_on_an_empty_vocabulary():
    """`off` is the foot of every ladder, and an alias with no effort channel has
    no ladder: answering `off` would promise a knob nobody turns."""
    with pytest.raises(GatewayError):
        _effort_the_alias_knows(REASONING_OFF, [], serves="model 'quiet'", what="reasoning_effort")


def test_an_absent_vocabulary_is_still_the_shared_scale():
    assert _effort_the_alias_knows("high", None, serves="model 'x'", what="reasoning_effort") == "high"
    assert _effort_the_alias_knows(REASONING_OFF, None, serves="model 'x'", what="reasoning_effort") == REASONING_OFF


def test_a_peer_row_with_an_empty_word_list_refuses_at_this_node(monkeypatch):
    """The same check on the peer route, reading the host's menu row rather than
    a local provider: a host that serves no effort is not asked for one."""
    row = {"alias": "quiet", "reasoning_words": []}
    with pytest.raises(GatewayError) as refused:
        _effort_the_alias_knows(
            "low", row["reasoning_words"],
            serves="peer p's alias 'quiet'", what="reasoning_effort",
        )

    assert refused.value.code == "invalid_value"


# --- (5) the gateway's own door and its model list -----------------------------


def _switch_service(tmp_path):
    """The loopback stand-in behind one real Z.AI alias: a class whose only
    effort word is `off`, configured with a level it never sends."""
    providers = dict(_gateway_providers())
    providers[SWITCH] = ZaiProvider(SWITCH, {
        "type": "zai", "model": "glm-4.7", "api_key": "k", "reasoning_effort": "high",
    })
    service = _gateway_service(
        tmp_path,
        {"serving_local": [], "serving_vendor": [SWITCH], "vendor_quotas": {SWITCH: 2.0}},
        providers=providers,
    )
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        # What `LLMManager` reports for a call the provider named no rung for.
        return dict(await inner(messages, **kwargs),
                    served_effort=None, provider_served_effort=None)

    service.llm_manager.query_messages = query_messages
    return service


def test_the_local_door_names_no_rung_for_a_silent_alias_even_if_a_word_reached_it(monkeypatch):
    """The row's last guard. `_effort_the_alias_knows` refuses such a word before
    the call, so nothing reaches here today with one — and if a second path ever
    does, the row still must not name a rung this alias cannot serve."""
    monkeypatch.setenv("K", "k")
    gateway = Gateway.__new__(Gateway)
    gateway._core = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"quiet": _openai(reasoning_effort="high")})
    )

    assert gateway._served_effort("quiet", {"served_effort": "high"}) is None
    assert gateway._served_effort("quiet", {"served_effort": None}) is None


@pytest.mark.asyncio
async def test_the_model_list_promises_a_switch_only_alias_no_effort_word(tmp_path):
    """The row an IDE client reads: the alias is served, and nothing on it
    offers a level to ask for."""
    async with _gateway_running(tmp_path, _switch_service(tmp_path)) as (server, _):
        status, text = await _gateway_request(server, "GET", "/v1/models", key=_gateway_key(tmp_path))
        assert status == 200, text
        (row,) = [r for r in json.loads(text)["data"] if r["id"] == SWITCH]
        assert row["owned_by"] == "vendor"
        assert "reasoning_words" not in row and "reasoning_default" not in row


@pytest.mark.asyncio
async def test_the_door_refuses_a_level_the_alias_cannot_send(tmp_path):
    async with _gateway_running(tmp_path, _switch_service(tmp_path)) as (server, _):
        status, text = await _gateway_request(
            server, "POST", "/v1/chat/completions", key=_gateway_key(tmp_path),
            body=_gateway_chat(SWITCH, reasoning_effort="high"),
        )
        assert status == 400, text
        error = json.loads(text)["error"]
        assert error["code"] == "invalid_value"
        assert REASONING_OFF in error["message"], "the refusal lists the one word it does serve"


@pytest.mark.asyncio
async def test_the_row_names_no_rung_where_the_configured_word_is_never_sent(tmp_path):
    """The card, end to end: the alias configures `high`, the class never sends
    it, and the row must not say `high`."""
    async with _gateway_running(tmp_path, _switch_service(tmp_path)) as (server, ledger):
        status, text = await _gateway_request(
            server, "POST", "/v1/chat/completions", key=_gateway_key(tmp_path),
            body=_gateway_chat(SWITCH),
        )
        assert status == 200, text

        (row,) = list(ledger.rows())
        assert row["alias"] == SWITCH
        assert row["served_effort"] is None
