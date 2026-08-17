"""The daemon reports three numbers per call and we were keeping none of them.

An empty vision answer was attributed to a full context window on reasoning
alone — an inference, and three reviewers refused it for the same reason: the
field that separates «cut off» from «stopped by itself» is `done_reason`, it
arrives on every response, and no path read it. Neither were the token counts,
except on one path of three where they were read and never logged.

These assert that the line exists, on every path, and that it carries the field
the dispute turns on.
"""

from __future__ import annotations

import logging

import pytest

from dpc_client_core.providers.ollama_provider import OllamaProvider


class _Msg(dict):
    """Both access shapes, because the real response has both and the paths differ:
    the plain and vision paths do `response['message']['content']` while the tools
    path does `getattr(msg, 'content')`. A double that mirrors only the caller
    under test would pass while the other two paths broke."""

    def __init__(self, content, thinking=None, tool_calls=None):
        super().__init__(content=content)
        self.content = content
        self.thinking = thinking
        self.tool_calls = tool_calls or []


class _Resp(dict):
    def __init__(self, message, **attrs):
        super().__init__(message=message)
        self.__dict__.update(attrs)


def _make(config=None):
    cfg = {"model": "qwen3-vl:8b", "host": "http://127.0.0.1:11434"}
    if config:
        cfg.update(config)
    return OllamaProvider("ollama_vision", cfg)


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    from dpc_client_core.providers import ollama_provider as op
    op._MODEL_INFO.clear()
    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    yield
    op._MODEL_INFO.clear()


def _answered(content, thinking, done_reason, prompt=1200, completion=9000):
    async def _chat(*_a, **_k):
        return _Resp(_Msg(content, thinking),
                     prompt_eval_count=prompt, eval_count=completion, done_reason=done_reason)
    return _chat


@pytest.mark.asyncio
async def test_an_empty_vision_answer_says_whether_it_was_cut_off(caplog):
    """The whole dispute in one assertion: `length` and `stop` are different
    defects with different fixes, and the log could not tell them apart."""
    provider = _make()
    provider.client.chat = _answered("", "x" * 34085, "length")

    with caplog.at_level(logging.INFO):
        answer = await provider.generate_with_vision("describe", [{"base64": "aGk="}])

    assert answer == ""
    assert "done=length" in caplog.text


@pytest.mark.asyncio
async def test_the_vision_line_carries_the_daemons_own_counts(caplog):
    """Not our tokenizer's estimate taken before the call — the daemon's report
    of what the call actually was, which is the only figure that includes the
    tokens an image costs."""
    provider = _make()
    provider.client.chat = _answered("a page", None, "stop", prompt=4836, completion=1445)

    with caplog.at_level(logging.INFO):
        await provider.generate_with_vision("describe", [{"base64": "aGk="}])

    assert "prompt=4836" in caplog.text
    assert "completion=1445" in caplog.text
    assert "path=vision" in caplog.text


@pytest.mark.asyncio
async def test_the_plain_path_reports_too(caplog):
    provider = _make()
    provider.client.chat = _answered("hello", None, "stop")

    with caplog.at_level(logging.INFO):
        await provider.generate_response("hi")

    assert "path=plain" in caplog.text
    assert "done=stop" in caplog.text


@pytest.mark.asyncio
async def test_the_tools_path_reports_too(caplog):
    provider = _make()
    provider.client.chat = _answered("ok", None, "stop")

    with caplog.at_level(logging.INFO):
        await provider.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])

    assert "path=tools" in caplog.text


@pytest.mark.asyncio
async def test_the_thinking_length_is_named_as_characters_not_tokens(caplog):
    """Ollama gives no token figure for the reasoning. Calling the character
    count a token count is how an estimate becomes a measurement in a retro."""
    provider = _make()
    provider.client.chat = _answered("", "y" * 500, "length")

    with caplog.at_level(logging.INFO):
        await provider.generate_with_vision("describe", [{"base64": "aGk="}])

    assert "thinking_chars=500" in caplog.text
