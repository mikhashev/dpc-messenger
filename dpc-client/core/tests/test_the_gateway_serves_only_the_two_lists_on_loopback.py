"""The gateway serves only the two lists, on loopback, with one row per call.

An outside tool reaches this node's models over HTTP on 127.0.0.1 — the
consumer's own loopback (ADR-041 D1) — and the door is opened by a static key
on disk rather than by whoever can reach the port. What it serves is exactly
`compute.serving_local` and `compute.serving_vendor` (D5): a local alias is
bounded by the card, a vendor alias by a daily per-caller ceiling read from
the node ledger, and nothing falls back to the default provider. Every
completion leaves one usage row with `caller_kind=gateway` (D3).

The listener is a real `aiohttp` `TCPSite` on port 0; the provider layer is
stood in for by a fake `LLMManager.query` returning the `return_metadata`
dict the real one does. Cross-platform: pure asyncio, no socket options.
"""

import asyncio
import contextlib
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.gateway import (
    GATEWAY_HOST,
    GATEWAY_KEY_NAME,
    GatewayConfigError,
    GatewayServer,
)
from dpc_client_core.node_ledger import NodeLedger, usage_row
from dpc_client_core.settings import Settings

NODE_ID = "dpc-node-" + "a" * 32
OTHER_NODE = "dpc-node-" + "b" * 32
LOCAL = "ollama_local"
VENDOR = "ds_flash"
UNLISTED = "paid_default"
ANSWER = "hello from the card"
INFERENCE_TIMEOUT_S = 0.2


class _Provider:
    """What the gateway reads off a built provider: its config and its model."""

    def __init__(self, type_, model):
        self.config = {"type": type_, "model": model}
        self.model = model


def _providers():
    return {
        LOCAL: _Provider("ollama", "qwen3:8b"),
        VENDOR: _Provider("deepseek", "deepseek-v4-flash"),
        UNLISTED: _Provider("anthropic", "claude-sonnet-4-5"),
    }


def _service(tmp_path: Path, compute: dict, *, providers=None, fail=None):
    """A stand-in for CoreService with only what the gateway reads: a real
    firewall over a rules file in tmp_path, a fake registry and query."""
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    providers = _providers() if providers is None else providers
    calls = []

    async def query(prompt, provider_alias=None, return_metadata=False, **kwargs):
        calls.append({"prompt": prompt, "alias": provider_alias, "kwargs": kwargs})
        if fail is not None:
            raise fail
        provider = providers[provider_alias]
        return {
            "response": ANSWER, "provider": provider_alias, "model": provider.model,
            "tokens_used": 17, "prompt_tokens": 12, "response_tokens": 5,
            "model_max_tokens": 4096, "vision_used": False,
            "thinking": None, "thinking_tokens": None,
        }

    return types.SimpleNamespace(
        firewall=ContextFirewall(rules),
        llm_manager=types.SimpleNamespace(providers=providers, query=query),
        p2p_manager=types.SimpleNamespace(node_id=NODE_ID),
        settings=types.SimpleNamespace(get_remote_inference_timeout=lambda: INFERENCE_TIMEOUT_S),
        calls=calls,
    )


@contextlib.asynccontextmanager
async def _running(tmp_path: Path, service, *, ledger=None, inference_lock=None):
    ledger = ledger or NodeLedger(tmp_path / "ledger")
    server = GatewayServer(
        service, host=GATEWAY_HOST, port=0,
        key_path=tmp_path / GATEWAY_KEY_NAME, ledger=ledger, inference_lock=inference_lock,
    )
    await server.start()
    try:
        yield server, ledger
    finally:
        await server.stop()


def _key(tmp_path: Path) -> str:
    return (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip()


async def _request(server, method, path, *, key=None, body=None, headers=None):
    hdrs = dict(headers or {})
    if key is not None:
        hdrs["Authorization"] = f"Bearer {key}"
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=body, headers=hdrs) as resp:
            return resp.status, await resp.text()


def _chat(model, text="hi", **extra):
    body = {"model": model, "messages": [{"role": "system", "content": "be brief"},
                                         {"role": "user", "content": text}]}
    body.update(extra)
    return body


BOTH_LISTS = {"serving_local": [LOCAL], "serving_vendor": [VENDOR], "vendor_quotas": {VENDOR: 2.0}}


# --- (1) loopback ------------------------------------------------------------


def test_the_listener_refuses_to_bind_anything_but_loopback(tmp_path):
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    with pytest.raises(GatewayConfigError) as refused:
        GatewayServer(service, host="0.0.0.0", port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    assert "0.0.0.0" in str(refused.value) and "127.0.0.1" in str(refused.value)


@pytest.mark.asyncio
async def test_the_listener_binds_loopback_and_no_other_interface(tmp_path):
    async with _running(tmp_path, _service(tmp_path, {"serving_local": [LOCAL]})) as (server, _):
        address, port = server.bound_address()
        assert address == "127.0.0.1"
        assert port == server.port and port != 0


# --- (2) the key ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_key_or_a_wrong_key_is_401_and_the_right_key_is_200(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        assert (await _request(server, "GET", "/v1/models"))[0] == 401
        assert (await _request(server, "GET", "/v1/models", key="not-" + key))[0] == 401
        assert (await _request(server, "GET", "/v1/models", headers={"Authorization": key}))[0] == 401
        status, _ = await _request(server, "GET", "/v1/models", key=key)
        assert status == 200


@pytest.mark.asyncio
async def test_the_key_is_written_once_and_survives_a_restart(tmp_path):
    """Continue needs a fixed apiKey: a restart must not mint a new key."""
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service):
        first = _key(tmp_path)
    assert len(first) >= 32
    async with _running(tmp_path, service) as (server, _):
        assert _key(tmp_path) == first
        assert (await _request(server, "GET", "/v1/models", key=first))[0] == 200


# --- Host --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_foreign_host_header_is_400_and_the_two_loopback_names_pass(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        status, text = await _request(server, "GET", "/v1/models", key=key,
                                      headers={"Host": "gateway.example:9997"})
        assert status == 400 and "gateway.example:9997" in text
        for host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}"):
            status, _ = await _request(server, "GET", "/v1/models", key=key, headers={"Host": host})
            assert status == 200, host


# --- (3) /v1/models ------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_lists_exactly_the_two_lists_with_their_owner(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        body = json.loads(text)
        assert body["object"] == "list"
        assert {(m["id"], m["owned_by"]) for m in body["data"]} == {(LOCAL, "local"), (VENDOR, "vendor")}
        assert all(m["object"] == "model" for m in body["data"])


@pytest.mark.asyncio
async def test_an_alias_outside_both_lists_is_404_and_empty_lists_serve_nothing(tmp_path):
    """The registry holds a paid default; the gateway never falls back to it."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(UNLISTED))
        assert status == 404 and UNLISTED in json.loads(text)["error"]["message"]

    service = _service(tmp_path, {"enabled": False})
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert (status, json.loads(text)["data"]) == (200, [])
        status, _ = await _request(server, "POST", "/v1/chat/completions",
                                   key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 404
        assert service.calls == [] and list(ledger.rows()) == []


# --- (4) a local completion -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_local_completion_is_openai_shaped_and_leaves_one_gateway_row(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200
        body = json.loads(text)
        assert body["object"] == "chat.completion"
        assert body["model"] == LOCAL
        assert body["choices"] == [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": ANSWER}}]
        assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}

        # One call, flattened with the adapter's role markers, on the alias asked for.
        assert [c["alias"] for c in service.calls] == [LOCAL]
        assert service.calls[0]["prompt"] == "[SYSTEM]\nbe brief\n\n[USER]\nhi"

        rows = list(ledger.rows())
        assert len(rows) == 1
        row = rows[0]
        assert (row["caller"], row["caller_kind"], row["route"]) == (NODE_ID, "gateway", "local")
        assert (row["alias"], row["model"]) == (LOCAL, "qwen3:8b")
        assert (row["prompt_tokens"], row["completion_tokens"], row["thinking_tokens"]) == (12, 5, None)
        assert row["counts_source"] == "ours"
        assert (row["billing"], row["cost_usd"]) == ("subscription", 0.0)
        assert body["id"] == "chatcmpl-" + row["request_id"]


# --- (6) the vendor quota -----------------------------------------------------------


def _spend(ledger, *, cost, started_at, caller=NODE_ID, caller_kind="gateway", alias=VENDOR, request_id):
    ledger.append(usage_row(
        request_id=request_id, caller=caller, caller_kind=caller_kind, alias=alias,
        model="deepseek-v4-flash", route="local", prompt_tokens=1, completion_tokens=1,
        thinking_tokens=None, counts_source="ours", started_at=started_at, duration_s=0.1,
        billing="pay_per_use", cost_usd=cost,
    ))


@pytest.mark.asyncio
async def test_a_vendor_alias_over_its_daily_quota_is_429_and_under_it_the_row_adds_to_the_day(tmp_path):
    quota = 0.01
    compute = {"serving_vendor": [VENDOR], "vendor_quotas": {VENDOR: quota}}
    service = _service(tmp_path, compute)
    ledger = NodeLedger(tmp_path / "ledger")
    now = datetime.now(timezone.utc)
    _spend(ledger, cost=0.004, started_at=now, request_id="earlier-today")
    # Another caller's spend on the same alias is not ours: the ceiling is per caller.
    _spend(ledger, cost=5.0, started_at=now, caller=OTHER_NODE, caller_kind="peer", request_id="a-peers")
    # And yesterday's is yesterday's.
    _spend(ledger, cost=5.0, started_at=now - timedelta(days=1), request_id="yesterday")

    async with _running(tmp_path, service, ledger=ledger) as (server, _):
        key = _key(tmp_path)
        before = ledger.spent_today(VENDOR, caller=NODE_ID, caller_kind="gateway")
        assert before == pytest.approx(0.004)

        status, text = await _request(server, "POST", "/v1/chat/completions", key=key, body=_chat(VENDOR))
        assert status == 200
        seeded = {"earlier-today", "a-peers", "yesterday"}
        (row,) = [r for r in ledger.rows() if r["request_id"] not in seeded]
        assert row["caller_kind"] == "gateway" and row["billing"] == "pay_per_use"
        assert row["cost_usd"] > 0
        assert ledger.spent_today(VENDOR, caller=NODE_ID, caller_kind="gateway") == pytest.approx(before + row["cost_usd"])

        _spend(ledger, cost=quota, started_at=now, request_id="the-rest-of-the-day")
        status, text = await _request(server, "POST", "/v1/chat/completions", key=key, body=_chat(VENDOR))
        assert status == 429
        message = json.loads(text)["error"]["message"]
        assert VENDOR in message and "0.01" in message and NODE_ID in message
        assert len(service.calls) == 1, "the refused call must not reach the provider"


# --- (7) stream: true ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_true_yields_one_data_chunk_and_done(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, ledger):
        url = f"http://127.0.0.1:{server.port}/v1/chat/completions"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=_chat(LOCAL, stream=True),
                                    headers={"Authorization": f"Bearer {_key(tmp_path)}"}) as resp:
                assert resp.status == 200
                assert resp.content_type == "text/event-stream"
                text = await resp.text()
        events = [line[len("data: "):] for line in text.split("\n") if line.startswith("data: ")]
        assert len(events) == 2 and events[1] == "[DONE]"
        chunk = json.loads(events[0])
        assert chunk["object"] == "chat.completion.chunk" and chunk["model"] == LOCAL
        assert chunk["choices"] == [{"index": 0, "finish_reason": "stop",
                                     "delta": {"role": "assistant", "content": ANSWER}}]
        assert len(list(ledger.rows())) == 1


# --- (8) an alias that is itself remote ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("list_key, type_", [
    ("serving_local", "remote_peer"), ("serving_local", "dpc_agent"),
    ("serving_vendor", "remote_peer"), ("serving_vendor", "dpc_agent"),
])
async def test_an_alias_that_is_itself_remote_is_refused_at_load_in_either_list(tmp_path, list_key, type_):
    providers = {"relay": _Provider(type_, None)}
    compute = {list_key: ["relay"], "vendor_quotas": {"relay": 1.0}}
    server = GatewayServer(_service(tmp_path, compute, providers=providers),
                           host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    with pytest.raises(GatewayConfigError) as refused:
        await server.start()
    reason = str(refused.value)
    assert "relay" in reason and type_ in reason
    assert "shared onward" in reason, "refused for the D7 reason, not as an unclassified type"
    assert not server.is_running
    assert not (tmp_path / GATEWAY_KEY_NAME).exists(), "a refused gateway writes no key"


# --- failures the provider layer hands up -------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_error_is_502_with_its_message_and_a_missing_provider_503(tmp_path):
    service = _service(tmp_path, BOTH_LISTS, fail=RuntimeError("Ollama is not running"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 502 and "Ollama is not running" in json.loads(text)["error"]["message"]
        assert list(ledger.rows()) == [], "a call that produced nothing is not a row"

    listed_but_unloaded = _service(tmp_path, {"serving_local": ["gone"]}, providers={})
    async with _running(tmp_path, listed_but_unloaded) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat("gone"))
        assert status == 503 and "gone" in json.loads(text)["error"]["message"]


@pytest.mark.asyncio
async def test_a_request_that_would_wait_past_the_inference_timeout_is_503_the_card_is_busy(tmp_path):
    """A local alias is bounded by the card: the peer door's lock is the queue,
    and a wait longer than remote_inference_timeout is refused rather than held."""
    lock = asyncio.Semaphore(1)
    await lock.acquire()  # a peer is on the card
    service = _service(tmp_path, BOTH_LISTS)
    try:
        async with _running(tmp_path, service, inference_lock=lock) as (server, ledger):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(LOCAL))
            assert status == 503 and "busy" in json.loads(text)["error"]["message"]
            assert service.calls == [] and list(ledger.rows()) == []
            # A vendor alias does not queue on the card: money, not silicon, bounds it.
            status, _ = await _request(server, "POST", "/v1/chat/completions",
                                       key=_key(tmp_path), body=_chat(VENDOR))
            assert status == 200
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_a_body_without_model_or_messages_is_400(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        for body in ({"messages": [{"role": "user", "content": "hi"}]},
                     {"model": LOCAL}, {"model": LOCAL, "messages": []},
                     {"model": LOCAL, "messages": [{"role": "user", "content": ""}]}):
            status, _ = await _request(server, "POST", "/v1/chat/completions", key=key, body=body)
            assert status == 400, body


# --- (9) off by default -------------------------------------------------------------


def test_the_default_config_leaves_the_gateway_off_and_the_service_builds_none(tmp_path, monkeypatch):
    from dpc_client_core import service as service_module

    settings = Settings(tmp_path)
    assert settings.get_gateway_enabled() is False
    assert settings.get_gateway_port() == 9997
    assert settings.get_gateway_host() == GATEWAY_HOST

    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    svc = service_module.CoreService.__new__(service_module.CoreService)
    svc.settings = settings
    assert svc._build_gateway() is None

    (tmp_path / "config.ini").write_text("[gateway]\nenabled = true\n", encoding="utf-8")
    svc.settings = Settings(tmp_path)
    svc.p2p_coordinator = types.SimpleNamespace(_peer_inference_lock=asyncio.Semaphore(1))
    built = svc._build_gateway()
    assert isinstance(built, GatewayServer)
    assert (built.host, built.port, built.key_path) == (GATEWAY_HOST, 9997, tmp_path / GATEWAY_KEY_NAME)

    # A host other than loopback keeps the door shut and the messenger running (D1).
    (tmp_path / "config.ini").write_text("[gateway]\nenabled = true\nhost = 0.0.0.0\n", encoding="utf-8")
    svc.settings = Settings(tmp_path)
    assert svc._build_gateway() is None


@pytest.mark.asyncio
async def test_with_the_gateway_off_the_service_starts_and_stops_without_it(tmp_path):
    from dpc_client_core.service import CoreService

    svc = CoreService.__new__(CoreService)
    svc.settings = Settings(tmp_path)
    svc.gateway = None
    await svc._start_gateway()
    await svc._stop_gateway()
    assert svc.gateway is None
