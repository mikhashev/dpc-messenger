"""The usage row names the rung the provider ran, not the word a door derived.

Both doors used to read `served_effort` off the request or off the alias's
configuration, and neither asked the provider what it had sent. On 2026-09-14
two gateway calls carrying an image on a llamacpp alias configured
`reasoning_effort: low` wrote `served_effort: low` on four rows — the local
route's and the host's peer rows — while the provider's own usage line for the
same calls read `effort=off, path=vision`: the vision entry point ran with
thinking off, because the caller had named no rung and that path never
consulted the alias's word.

Two halves, and both are here. The provider now reports the rung it ran on in
its usage dict, read off the body it actually sent, and `LLMManager` carries it
out as `provider_served_effort` for both doors to copy; a provider that reports
nothing leaves today's derivation standing, so Ollama and the rest are
unchanged. And the vision path, which *can* take an effort word, now serves the
one the alias names — what stays out of it is the template's own default, which
is what the 2026-08-20 review was about.

Cross-platform: pure asyncio, no engine, no network.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.llm_manager import reported_served_effort
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers import DeepSeekProvider
from tests.test_llamacpp_server_provider import (
    _FakeSupervisor,
    _chat_resp,
    _fake_client,
    _provider,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    _Provider,
    _chat,
    _key,
    _providers,
    _request,
    _running,
    _service,
)
from tests.test_the_message_shaped_door_reaches_the_provider_unflattened import (
    FLAT,
    MESSAGES,
    SYSTEM,
    _Plain,
    _manager,
)
from tests.test_p2p_coordinator import make_coordinator


# --- the provider reports the rung it sent ------------------------------------------


def _wired(provider):
    """A provider whose child is a double, answering one canned completion."""
    provider.supervisor = _FakeSupervisor()
    client, completions = _fake_client(_chat_resp(content="ok"))

    async def _ensure():
        return client

    provider._ensure = _ensure
    return provider, completions


@pytest.mark.asyncio
async def test_the_plain_path_reports_the_word_the_template_was_given():
    p, _ = _wired(_provider(reasoning_effort="low"))

    await p.generate_response("q")

    assert p.get_last_usage()["served_effort"] == "low"


@pytest.mark.asyncio
async def test_a_fleet_word_is_reported_as_the_rung_the_model_has():
    """`high` is not a rung of this model: the row must say the one that ran."""
    p, _ = _wired(_provider())

    await p.generate_response("q", reasoning_effort="high")

    assert p.get_last_usage()["served_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_off_is_reported_as_off_and_not_as_silence():
    p, _ = _wired(_provider())

    await p.generate_response("q", reasoning_effort="off")

    assert p.get_last_usage()["served_effort"] == "off"


@pytest.mark.asyncio
async def test_nobody_naming_a_rung_on_a_template_we_could_not_read_is_none():
    """The fallback table is not the model's word and may not wear its name."""
    p, _ = _wired(_provider())

    await p.generate_response("q")

    assert p.get_last_usage()["served_effort"] is None


@pytest.mark.asyncio
async def test_the_tools_path_reports_the_rung_too():
    p, _ = _wired(_provider())

    out = await p.generate_with_tools([{"role": "user", "content": "q"}], [], reasoning_effort="medium")

    assert out["usage"]["served_effort"] == "medium"
    assert p.get_last_usage()["served_effort"] == "medium"


@pytest.mark.asyncio
async def test_the_streaming_path_reports_the_rung_too():
    p = _provider(reasoning_effort="low")
    p.supervisor = _FakeSupervisor()

    class _Stream:
        def __aiter__(self):
            async def _chunks():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(finish_reason="stop",
                                             delta=SimpleNamespace(content="ok", reasoning_content=None))],
                    usage=None,
                )
                yield SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4),
                )
            return _chunks()

    client, _ = _fake_client(_Stream())

    async def _ensure():
        return client

    p._ensure = _ensure
    chunks = []

    async def _on_chunk(text, conversation_id):
        chunks.append(text)

    await p.generate_response_stream("q", _on_chunk)

    assert chunks == ["ok"]
    assert p.get_last_usage()["served_effort"] == "low"


# --- the vision path takes a rung, and now serves the one the alias names ---------------


@pytest.mark.asyncio
async def test_an_image_on_an_alias_configured_low_runs_at_low_and_says_so():
    """The live case, the other way up: the row said `low` and the engine ran
    `off`. The path can carry the word, so the word is carried."""
    p, completions = _wired(_provider(mmproj="mm.gguf", reasoning_effort="low"))

    await p.generate_with_vision("what is this?", [{"base64": "AAAA"}])

    body = completions.bodies[0]
    assert body["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert p.get_last_usage()["served_effort"] == "low"


@pytest.mark.asyncio
async def test_an_image_on_an_alias_naming_no_rung_still_runs_off_and_says_off():
    """What the vision path keeps out is the template's own default, not the
    owner's word: an alias that names none reads the picture without thinking."""
    p, completions = _wired(_provider(mmproj="mm.gguf", reasoning_budget_tokens=10000))

    await p.generate_with_vision("what is this?", [{"base64": "AAAA"}])

    body = completions.bodies[0]
    assert body["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert p.get_last_usage()["served_effort"] == "off"


# --- the other provider with an effort channel ----------------------------------------


def _deepseek(**overrides):
    config = {"type": "deepseek", "model": "deepseek-v4-flash", "api_key": "k"}
    config.update(overrides)
    return DeepSeekProvider("deepseek_pro", config)


def _deepseek_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None,
                                                         tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )


@pytest.mark.asyncio
async def test_deepseek_reports_the_effort_it_sent():
    p = _deepseek(reasoning_effort="high")
    p.client.chat.completions.create = AsyncMock(return_value=_deepseek_response())

    await p.generate_response("q")

    assert p.get_last_usage()["served_effort"] == "high"


@pytest.mark.asyncio
async def test_deepseek_reports_off_where_the_thinking_block_is_disabled():
    p = _deepseek(reasoning_effort="high")
    p.client.chat.completions.create = AsyncMock(return_value=_deepseek_response())

    await p.generate_response("q", reasoning_effort="off")

    assert p.get_last_usage()["served_effort"] == "off"


# --- the manager carries the word out of both doors -------------------------------------


class _RunningOff(_Plain):
    """A provider whose entry point ran another rung than the one it was asked
    for — llama-server's vision path, in the smallest shape that shows it."""

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage({"prompt_tokens": 4, "completion_tokens": 2, "served_effort": "off"})
        return await super().generate_response(prompt, **kwargs)

    async def generate_with_vision(self, prompt, images, **kwargs):
        return await self.generate_response(prompt, **kwargs)

    def supports_vision(self):
        return True


def test_a_provider_that_reported_nothing_names_no_rung():
    assert reported_served_effort(_Plain()) is None


@pytest.mark.asyncio
async def test_the_prompt_door_carries_the_providers_word_beside_the_one_it_passed(tmp_path):
    manager = _manager(tmp_path, _RunningOff())

    result = await manager.query(FLAT, return_metadata=True, reasoning_effort="low")

    assert result["served_effort"] == "low", "the word this door passed"
    assert result["provider_served_effort"] == "off", "the rung the provider ran"


@pytest.mark.asyncio
async def test_the_message_door_carries_it_too(tmp_path):
    manager = _manager(tmp_path, _RunningOff())

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True,
                                          reasoning_effort="low")

    assert result["provider_served_effort"] == "off"


@pytest.mark.asyncio
async def test_a_silent_provider_leaves_the_key_at_none(tmp_path):
    manager = _manager(tmp_path, _Plain())

    result = await manager.query(FLAT, return_metadata=True, reasoning_effort="low")

    assert result["provider_served_effort"] is None
    assert result["served_effort"] == "low"


# --- the gateway's local row -------------------------------------------------------------


def _configured(word):
    providers = _providers()
    providers[LOCAL] = _Provider("llamacpp_server", "qwen3.8")
    providers[LOCAL].config["reasoning_effort"] = word
    return providers


def _reporting_service(tmp_path, *, configured, reported):
    """The loopback stand-in whose door reports what the provider ran, as the
    real one does once the provider fills its usage dict."""
    service = _service(tmp_path, BOTH_LISTS, providers=_configured(configured))
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        return dict(await inner(messages, **kwargs), provider_served_effort=reported)

    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_a_local_row_names_the_providers_rung_over_the_configured_word(tmp_path):
    service = _reporting_service(tmp_path, configured="low", reported="off")
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = ledger.rows()
        assert row["served_effort"] == "off"


@pytest.mark.asyncio
async def test_a_local_row_keeps_the_derived_word_when_the_provider_says_nothing(tmp_path):
    service = _reporting_service(tmp_path, configured="low", reported=None)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = ledger.rows()
        assert row["served_effort"] == "low", "the alias's configured word, as before"


# --- the peer door: the wire and the host's own row ---------------------------------------


def _host(tmp_path, *, configured, reported):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={"reasoning_effort": configured})}
    answer = {
        "response": "pong", "model": "qwen3.8", "provider": "ollama_local",
        "prompt_tokens": 38, "response_tokens": 2, "tokens_used": 40,
    }
    if reported is not None:
        answer["provider_served_effort"] = reported
    svc.llm_manager.query = AsyncMock(return_value=answer)
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_the_host_tells_the_guest_the_rung_its_provider_ran(tmp_path):
    coord, svc = _host(tmp_path, configured="low", reported="off")

    await coord.handle_inference_request("peer-1", "req-1", "describe this", images=[{"base64": "AAAA"}])

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["served_effort"] == "off"
    (row,) = coord._ledger.rows()
    assert row["served_effort"] == "off"


@pytest.mark.asyncio
async def test_a_host_whose_provider_says_nothing_keeps_its_own_word(tmp_path):
    coord, svc = _host(tmp_path, configured="low", reported=None)

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["served_effort"] == "low"
    (row,) = coord._ledger.rows()
    assert row["served_effort"] == "low"
