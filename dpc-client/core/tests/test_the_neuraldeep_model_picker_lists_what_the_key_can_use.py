"""The providers editor's Model picker for a NeuralDeep alias.

`query_provider_models` reads GET {base_url}/models with the key resolved on the
service side and answers [{id, kind}], chat first. The id list below is the shape
of the 2026-09-26 probe (chat, -noreason twins, embeddings, a reranker, STT); the
network is an httpx MockTransport.
"""

from types import SimpleNamespace

import httpx
import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS, _sanitize_payload_for_logging
from dpc_client_core.providers.neuraldeep_provider import model_kind, sorted_models
from dpc_client_core.service import CoreService

IDS = ["bge-m3", "whisper-1", "qwen3.8-27b-noreason", "bge-reranker-v2-m3", "gpt-oss-120b",
       "qwen3.8-27b", "gigaam-v3", "qwen3-embedding-8b", "dots.ocr", "kimi-k2.6"]


def _serve(monkeypatch, handler):
    """Every httpx.AsyncClient built from here on answers through `handler`."""
    real = httpx.AsyncClient
    seen = []

    def _handler(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(_handler), **kw))
    return seen


def _service(providers=None):
    return SimpleNamespace(llm_manager=SimpleNamespace(providers=providers or {}))


async def _query(svc, **kw):
    return await CoreService.query_provider_models(svc, "neuraldeep", **kw)


def test_kinds_are_read_conservatively_from_the_id():
    assert model_kind("qwen3.8-27b") == "chat"
    assert model_kind("qwen3.8-27b-noreason") == "chat"
    assert model_kind("gpt-oss-20b") == "chat"
    assert model_kind("qwen3-embedding-8b") == "embedding"
    assert model_kind("bge-m3") == "embedding"
    assert model_kind("frida") == "embedding"
    assert model_kind("bge-reranker-v2-m3") == "rerank"
    assert model_kind("whisper-1") == "stt"
    assert model_kind("gigaam-v3") == "stt"
    assert model_kind("dots.ocr") == "other"
    assert model_kind("some-new-thing") == "other"


def test_chat_comes_first_and_a_noreason_twin_follows_its_base():
    rows = sorted_models(IDS + ["qwen3.8-27b"])  # a duplicate is listed once
    assert [r["id"] for r in rows] == [
        "gpt-oss-120b", "kimi-k2.6", "qwen3.8-27b", "qwen3.8-27b-noreason",  # chat
        "bge-m3", "qwen3-embedding-8b",                                      # embedding
        "bge-reranker-v2-m3",                                                # rerank
        "gigaam-v3", "whisper-1",                                            # stt
        "dots.ocr",                                                          # other
    ]


@pytest.mark.asyncio
async def test_success_calls_models_with_the_env_key_and_returns_no_key(monkeypatch):
    monkeypatch.setenv("ND_TEST_KEY", "sk-secret-value")
    seen = _serve(monkeypatch, lambda r: httpx.Response(
        200, json={"object": "list", "data": [{"id": i, "object": "model"} for i in IDS]}))

    result = await _query(_service(), base_url="https://nd.example/v1", api_key_env="ND_TEST_KEY")

    assert result["status"] == "success"
    assert [m["kind"] for m in result["models"]][:4] == ["chat"] * 4
    assert str(seen[0].url) == "https://nd.example/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-secret-value"
    assert "sk-secret-value" not in repr(result)


@pytest.mark.asyncio
async def test_a_typed_key_wins_over_the_env_variable(monkeypatch):
    monkeypatch.setenv("NEURALDEEP_API_KEY", "sk-env")
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    result = await _query(_service(), api_key="sk-typed")
    assert result == {"status": "success", "models": []}
    assert seen[0].headers["authorization"] == "Bearer sk-typed"
    assert str(seen[0].url) == "https://api.neuraldeep.ru/v1/models"


@pytest.mark.asyncio
async def test_a_missing_key_names_the_variable_and_calls_nothing(monkeypatch):
    monkeypatch.delenv("NEURALDEEP_API_KEY", raising=False)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    result = await _query(_service())
    assert result["status"] == "error"
    assert "NEURALDEEP_API_KEY" in result["message"]
    assert seen == []


@pytest.mark.asyncio
async def test_a_401_says_the_key_was_rejected(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(401, json={"error": "bad key"}))
    result = await _query(_service(), api_key="sk-bad")
    assert result["status"] == "error"
    assert "key rejected" in result["message"]
    assert "sk-bad" not in result["message"]


@pytest.mark.asyncio
async def test_an_unreachable_host_is_named(monkeypatch):
    def _down(request):
        raise httpx.ConnectTimeout("")
    _serve(monkeypatch, _down)
    result = await _query(_service(), api_key="sk", base_url="https://nd.example/v1")
    assert result["status"] == "error"
    assert result["message"] == "Could not reach https://nd.example/v1: ConnectTimeout"


@pytest.mark.asyncio
async def test_other_types_are_unsupported():
    result = await CoreService.query_provider_models(_service(), "ollama")
    assert result["status"] == "unsupported"


def test_the_command_is_allowed_and_a_typed_key_is_not_logged():
    assert "query_provider_models" in ALLOWED_COMMANDS
    logged = _sanitize_payload_for_logging({"provider_type": "neuraldeep", "api_key": "sk-x"})
    assert logged["api_key"] == "<redacted>"


@pytest.mark.asyncio
async def test_a_key_pasted_as_the_variable_name_is_never_echoed(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    pasted = "sk-L0-pasted-where-a-name-belongs"
    result = await _query(_service(), api_key_env=pasted)
    assert result["status"] == "error"
    assert "not valid" in result["message"]
    assert pasted not in repr(result) and "sk-" not in repr(result)
    assert seen == []


@pytest.mark.asyncio
async def test_a_typed_key_is_used_even_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("NEURALDEEP_API_KEY", raising=False)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "kimi-k2.6"}]}))
    result = await _query(_service(), api_key="sk-typed-only", api_key_env="NEURALDEEP_API_KEY")
    assert result["status"] == "success"
    assert seen[0].headers["authorization"] == "Bearer sk-typed-only"


def test_a_key_in_the_variable_field_or_a_nested_config_is_not_logged():
    logged = _sanitize_payload_for_logging(
        {"provider_type": "neuraldeep", "api_key_env": "sk-L0-abcdef123"})
    assert logged["api_key_env"] == "<redacted>"
    assert _sanitize_payload_for_logging({"api_key_env": "NEURALDEEP_API_KEY"}) == {
        "api_key_env": "NEURALDEEP_API_KEY"}

    # save_providers_config sends whole provider entries, one level down.
    payload = {"config_dict": {"providers": [
        {"alias": "nd", "api_key": "plain-secret", "api_key_env": "sk-L0-abcdef123"},
        {"alias": "tg", "bot_token": "sk-token-value", "model": "sk-looking-model-name"},
    ]}}
    logged = _sanitize_payload_for_logging(payload)
    rows = logged["config_dict"]["providers"]
    assert rows[0]["api_key"] == rows[0]["api_key_env"] == rows[1]["bot_token"] == "<redacted>"
    assert rows[1]["model"] == "sk-looking-model-name"  # not a secret-named field
    assert payload["config_dict"]["providers"][0]["api_key"] == "plain-secret"  # input untouched


def test_an_error_message_holding_a_key_is_masked_in_the_log(caplog):
    from dpc_client_core.local_api import _log_error_under_ok
    with caplog.at_level("WARNING"):
        _log_error_under_ok("query_provider_models",
                            {"status": "error", "message": "variable sk-L0-abcdef123 is not set"})
    assert "sk-L0-abcdef123" not in caplog.text
    assert "sk-<redacted>" in caplog.text
