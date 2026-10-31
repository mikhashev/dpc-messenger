"""The machine-learning stack is the optional `voice-memory` extra. When it is
absent the startup log, the download consent and the embedding loader each name
the extra and the command that adds it (Mike's call, 2026-10-31).
"""

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_service  # noqa: E402

from dpc_client_core.dpc_agent import memory  # noqa: E402
from dpc_client_core.dpc_agent import faiss_index  # noqa: E402
from dpc_client_core.managers.token_count_manager import TokenCountManager  # noqa: E402
from dpc_client_core.model_download_service import ModelDownloadService  # noqa: E402
from dpc_client_core.settings import Settings  # noqa: E402

COMMAND = "uv sync --extra voice-memory --inexact"
_real_find_spec = importlib.util.find_spec


def _without(*names):
    def fake(name, *a, **k):
        if name in names:
            return None
        return _real_find_spec(name, *a, **k)
    return fake


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def test_startup_warns_once_with_the_command_when_torch_is_missing(monkeypatch, caplog):
    monkeypatch.setattr(importlib.util, "find_spec", _without("torch"))
    with caplog.at_level(logging.WARNING):
        run_service._warn_if_voice_memory_missing()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert COMMAND in warnings[0]
    assert "voice transcription" in warnings[0] and "memory search" in warnings[0]


def test_startup_is_silent_when_the_extra_is_installed(monkeypatch, caplog):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    with caplog.at_level(logging.WARNING):
        run_service._warn_if_voice_memory_missing()
    assert not caplog.records


def test_consent_is_not_offered_for_bge_m3_without_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", _without("sentence_transformers"))
    api = _FakeLocalApi()
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=api)

    offered = asyncio.run(svc.maybe_emit_required(
        "BAAI/bge-m3", purpose="p", consequence_if_declined="c",
    ))

    assert offered is False
    names = [n for n, _ in api.events]
    assert "model_download_required" not in names
    assert names == ["model_download_failed"]
    assert COMMAND in api.events[0][1]["error"]
    status = svc.get_status("BAAI/bge-m3")
    assert status["state"] == "failed" and COMMAND in status["error"]


def test_consent_is_still_offered_when_the_package_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    api = _FakeLocalApi()
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=api)
    offered = asyncio.run(svc.maybe_emit_required(
        "BAAI/bge-m3", purpose="p", consequence_if_declined="c",
    ))
    assert offered is True


def test_faiss_index_names_the_extra_when_faiss_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)  # import raises ImportError
    with pytest.raises(ImportError) as e:
        faiss_index._import_faiss()
    assert COMMAND in str(e.value)


def test_ollama_token_count_logs_the_command_once_without_transformers(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "transformers", None)
    mgr = TokenCountManager.__new__(TokenCountManager)
    mgr._tokenizer_cache = {}
    mgr._hf_tokenizer_cache = {}
    mgr._tokenizer_failures = set()
    with caplog.at_level(logging.WARNING):
        assert mgr._get_tokenizer_for_ollama("llama3.1:8b") is None
        assert mgr._get_tokenizer_for_ollama("llama3.1:70b") is None
    hits = [r for r in caplog.records if COMMAND in r.getMessage()]
    assert len(hits) == 1
