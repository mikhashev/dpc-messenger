"""A provider asked to look at an image never opens a path it was handed.

Five providers fell back to `open(img["path"])` when `base64` was absent —
`anthropic_provider.py`, `deepseek_provider.py`, `openai_provider.py`,
`zai_provider.py`, and `ollama_provider.py`, which handed the string to the
Ollama SDK instead of opening it itself.

That path is the *sender's*. DPTP §3.4 makes `base64` required and documents
`path` as the original filename; nothing promises the receiver it can open it.
An image reaches these methods straight off the wire — `REMOTE_INFERENCE_REQUEST`
→ `inference_handler` → `inference_orchestrator` → `llm_manager.query(images=…)`
→ `provider.generate_with_vision(images)` — so on another operating system the
path resolves nowhere, and on a like one it can resolve to a *different* file
that happens to sit there. The second case is the dangerous one: the answer
would describe a file nobody sent, and read as a description of the image.

The sender stopped shipping the path in `75d8b851` / `38004e0e`. This is the
receiving half: with no base64 there is nothing to look at, and the honest
answer is a refusal that names the provider and the missing field, not a file
picked up off the local disk.

The image below therefore points at a real file with real bytes: a provider
that reaches for it succeeds, which is exactly the failure being pinned.
"""

import asyncio
import base64 as b64
from types import SimpleNamespace

import pytest

from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from dpc_client_core.providers import ollama_provider as op

# Not the image anybody sent — the bytes of whatever sits at that place on the
# receiver's disk.
SOMEBODY_ELSES_BYTES = b"\x89PNG\r\n\x1a\n-a-different-file-entirely"


@pytest.fixture
def a_real_file(tmp_path):
    """A file that exists here, at the place the sender named."""
    p = tmp_path / "screenshot.png"
    p.write_bytes(SOMEBODY_ELSES_BYTES)
    return str(p)


class _Sent:
    """Records whether anything left for a model, and what."""

    def __init__(self):
        self.calls = []

    def as_openai(self):
        async def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="a cat",
                                                                 reasoning_content=None))],
                usage=None,
            )
        return create

    def as_anthropic(self):
        async def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text="a cat", type="text")])
        return create

    def as_ollama(self):
        # The vision path reads `response['message'].thinking` and
        # `response['message']['content']`, so the message answers both.
        class _Msg(dict):
            def __init__(self):
                super().__init__(content="a cat")
                self.thinking = None

        async def chat(**kwargs):
            self.calls.append(kwargs)
            return dict(message=_Msg())
        return chat


class _FakeOllamaClient:
    """The daemon, answering that this model takes images. No network."""

    def __init__(self, host=None, timeout=None):
        pass

    def show(self, model):
        return SimpleNamespace(capabilities=["completion", "vision", "thinking"])


def _anthropic(sent, monkeypatch):
    monkeypatch.setenv("DPC_TEST_ANTHROPIC_KEY", "test-key")
    p = AnthropicProvider("anthropic_vision", {
        "model": "claude-sonnet-4-20250514",
        "api_key_env": "DPC_TEST_ANTHROPIC_KEY",
    })
    p.client = SimpleNamespace(messages=SimpleNamespace(create=sent.as_anthropic()))
    return p


def _openai(sent, monkeypatch):
    p = OpenAICompatibleProvider("openai_vision", {"model": "gpt-4o", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _deepseek(sent, monkeypatch):
    p = DeepSeekProvider("deepseek_vision", {"model": "deepseek-v4-flash", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _zai(sent, monkeypatch):
    p = ZaiProvider("zai_vision", {"model": "glm-4.6v", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _ollama(sent, monkeypatch):
    op._MODEL_INFO.clear()
    op._HOST_SILENT_SINCE.clear()
    monkeypatch.setattr(op.ollama, "Client", _FakeOllamaClient)
    p = OllamaProvider("ollama_vision", {"model": "qwen3-vl:8b", "host": "http://127.0.0.1:11434"})
    p.client = SimpleNamespace(chat=sent.as_ollama())
    return p


BUILDERS = {
    "anthropic_vision": _anthropic,
    "openai_vision": _openai,
    "deepseek_vision": _deepseek,
    "zai_vision": _zai,
    "ollama_vision": _ollama,
}


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_an_image_with_only_a_path_is_refused_by_name(alias, a_real_file, monkeypatch):
    """The refusal has to say who was asked and what was missing: `llm_manager`
    hands it to the caller, and «failed» alone sends the reader to the wrong
    layer."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(provider.generate_with_vision(
            "what is in this?",
            [{"path": a_real_file, "mime_type": "image/png"}],
        ))

    message = str(excinfo.value)
    assert alias in message
    assert "base64" in message


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_the_file_that_happens_to_sit_there_is_never_sent_to_a_model(alias, a_real_file, monkeypatch):
    """The path resolves on this machine, so the old fallback succeeded — and a
    model answered about a file nobody sent."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch)

    with pytest.raises(ValueError):
        asyncio.run(provider.generate_with_vision(
            "what is in this?",
            [{"path": a_real_file, "mime_type": "image/png"}],
        ))

    assert sent.calls == [], f"{alias} sent something to a model without base64"
    assert b64.b64encode(SOMEBODY_ELSES_BYTES).decode() not in repr(sent.calls)


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_one_bad_image_stops_the_whole_call(alias, a_real_file, monkeypatch):
    """A partial set would reach the model as the whole set — the same rule the
    sending side took in `38004e0e`."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch)

    with pytest.raises(ValueError):
        asyncio.run(provider.generate_with_vision(
            "what is in these?",
            [
                {"base64": "aGVsbG8=", "mime_type": "image/png"},
                {"path": a_real_file, "mime_type": "image/png"},
            ],
        ))

    assert sent.calls == []


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_pixels_still_reach_the_model(alias, monkeypatch):
    """The refusal must not cost the working case: an image that carries its
    base64 goes through, data-URL prefix stripped as before."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch)

    out = asyncio.run(provider.generate_with_vision(
        "what is in this?",
        [{"base64": "data:image/png;base64,aGVsbG8=", "mime_type": "image/png"}],
    ))

    assert out == "a cat"
    assert len(sent.calls) == 1
    body = repr(sent.calls[0])
    assert "aGVsbG8=" in body
    assert "data:image/png;base64,data:" not in body
