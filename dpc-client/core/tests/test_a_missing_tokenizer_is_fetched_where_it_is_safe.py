"""A tokenizer is metadata, and metadata may be fetched — but not anywhere.

The counter falls back to one token per four characters, which measured 18%
low on a page of Russian. Fetching the real tokenizer is a few megabytes and
fixes that permanently; doing it on the event loop would stop Telegram, the
WebSocket and P2P for the duration, and doing it in offline mode would
overrule a decision somebody made on purpose.
"""

from __future__ import annotations

import asyncio

import pytest

from dpc_client_core.managers.token_count_manager import TokenCountManager


@pytest.fixture
def counter(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return TokenCountManager()


def test_off_the_loop_and_online_it_fetches(counter, monkeypatch):
    fetched = []

    class _Fake:
        @staticmethod
        def from_pretrained(repo, **kw):
            fetched.append((repo, kw))
            return "tokenizer"

    import transformers

    monkeypatch.setattr(transformers, "AutoTokenizer", _Fake)
    assert counter._fetch_tokenizer("Qwen/Qwen2.5-7B", "qwen3-vl:8b") == "tokenizer"
    assert fetched == [("Qwen/Qwen2.5-7B", {})], "a fetch must not be local-only"


def test_on_the_loop_it_refuses_and_says_why(counter, monkeypatch, caplog):
    def _boom(*a, **kw):
        raise AssertionError("the fetch should not have been attempted")

    import transformers

    monkeypatch.setattr(transformers, "AutoTokenizer", type("F", (), {"from_pretrained": _boom}))

    async def on_loop():
        with caplog.at_level("WARNING"):
            return counter._fetch_tokenizer("Qwen/Qwen2.5-7B", "qwen3-vl:8b")

    assert asyncio.run(on_loop()) is None
    assert "Not fetching it here: this call is on the event loop" in caplog.text
    assert "could not be fetched" not in caplog.text, "it tried anyway"


def test_offline_mode_is_not_overruled(counter, monkeypatch, caplog):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    def _boom(*a, **kw):
        raise AssertionError("the fetch should not have been attempted")

    import transformers

    monkeypatch.setattr(transformers, "AutoTokenizer", type("F", (), {"from_pretrained": _boom}))
    with caplog.at_level("WARNING"):
        assert counter._fetch_tokenizer("Qwen/Qwen2.5-7B", "qwen3-vl:8b") is None
    assert "Not fetching it here: the process is in offline mode" in caplog.text
    assert "could not be fetched" not in caplog.text, "it tried anyway"


def test_a_failed_fetch_costs_the_count_and_not_the_call(counter, monkeypatch, caplog):
    import transformers

    monkeypatch.setattr(
        transformers, "AutoTokenizer",
        type("F", (), {"from_pretrained": staticmethod(lambda *a, **kw: (_ for _ in ()).throw(OSError("no route to host")))}),
    )
    with caplog.at_level("WARNING"):
        assert counter._fetch_tokenizer("Qwen/Qwen2.5-7B", "qwen3-vl:8b") is None
    assert "could not be fetched" in caplog.text
