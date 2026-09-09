"""A provider asked to look at an image never opens a path it was handed.

Six providers fell back to `open(img["path"])` when `base64` was absent —
`anthropic_provider.py`, `deepseek_provider.py`, `openai_provider.py`,
`zai_provider.py`, `ollama_provider.py`, which handed the string to the Ollama
SDK instead of opening it itself, and `llamacpp_server_provider.py`, which
spelled the read `self._read_image_as_base64(img.get("path"))` and then dropped
the image and answered text-only when the read failed — a question about a
picture answered as though no picture had been asked about.

A seventh, `gemini_provider.py`, is under the same promise for a failure of its
own: it read `data` and `media_type`, keys nothing in this project writes, so no
image ever reached it — a provider answering `supports_vision()` yes and raising
`KeyError` on every picture routed to it.

The promise in the title is what these tests check, provider by provider: every
class in `BUILDERS` is driven through `generate_with_vision` with an image
carrying a path to a real local file and no base64, and has to refuse. A
provider absent from `BUILDERS` is outside the promise; the note at the foot of
this file says which those are and why. Two tests below reach past that promise
to google alone, because it is the one API handed the image dict's own keys.

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
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from dpc_client_core.providers import llamacpp_server_provider as lcp
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

    def as_gemini(self):
        # Synchronous on purpose: the google SDK call is blocking, and the
        # provider runs it through `loop.run_in_executor`.
        def generate_content(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(text="a cat")
        return generate_content


class _FakeOllamaClient:
    """The daemon, answering that this model takes images. No network."""

    def __init__(self, host=None, timeout=None):
        pass

    def show(self, model):
        return SimpleNamespace(capabilities=["completion", "vision", "thinking"])


def _anthropic(sent, monkeypatch, tmp_path):
    monkeypatch.setenv("DPC_TEST_ANTHROPIC_KEY", "test-key")
    p = AnthropicProvider("anthropic_vision", {
        "model": "claude-sonnet-4-20250514",
        "api_key_env": "DPC_TEST_ANTHROPIC_KEY",
    })
    p.client = SimpleNamespace(messages=SimpleNamespace(create=sent.as_anthropic()))
    return p


def _openai(sent, monkeypatch, tmp_path):
    p = OpenAICompatibleProvider("openai_vision", {"model": "gpt-4o", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _deepseek(sent, monkeypatch, tmp_path):
    p = DeepSeekProvider("deepseek_vision", {"model": "deepseek-v4-flash", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _zai(sent, monkeypatch, tmp_path):
    p = ZaiProvider("zai_vision", {"model": "glm-4.6v", "api_key": "test-key"})
    p.client.chat.completions.create = sent.as_openai()
    return p


def _ollama(sent, monkeypatch, tmp_path):
    op._MODEL_INFO.clear()
    op._HOST_SILENT_SINCE.clear()
    monkeypatch.setattr(op.ollama, "Client", _FakeOllamaClient)
    p = OllamaProvider("ollama_vision", {"model": "qwen3-vl:8b", "host": "http://127.0.0.1:11434"})
    p.client = SimpleNamespace(chat=sent.as_ollama())
    return p


def _llamacpp(sent, monkeypatch, tmp_path):
    """The DPC-owned llama-server, with no child and no card.

    The registries are swapped for empty ones so a second build of the same
    alias does not reach for the first build's supervisor and retire it off an
    event loop that is not running yet. `gguf_path` need not exist: the effort
    dictionary read from it is optional and falls back to the table.
    """
    monkeypatch.setattr(lcp, "_ACTIVE_SUPERVISORS", {})
    monkeypatch.setattr(lcp, "_RETIRING", {})
    p = LlamaServerProvider("llamacpp_vision", {
        "model": "qwen3-vl:8b",
        "gguf_path": str(tmp_path / "served.gguf"),
        "mmproj": str(tmp_path / "served.mmproj"),
    })
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=sent.as_openai()))
    )

    async def _ensure():
        return client

    p._ensure = _ensure
    return p


def _gemini(sent, monkeypatch, tmp_path):
    """Google AI Studio, with a key from the config and no network.

    `genai.Client` opens nothing at construction, and the SDK's own `types` are
    left in place: `Part.from_bytes` is typed `data: bytes`, so it is where a
    wrong read shows, and stubbing it away would hide exactly what is under
    test. Only the transport is replaced, as with every builder above.
    """
    p = GeminiProvider("gemini_vision", {
        "model": "gemini-2.0-flash",
        "api_key": "test-key",
    })
    p.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=sent.as_gemini())
    )
    return p


BUILDERS = {
    "anthropic_vision": _anthropic,
    "openai_vision": _openai,
    "deepseek_vision": _deepseek,
    "zai_vision": _zai,
    "ollama_vision": _ollama,
    "llamacpp_vision": _llamacpp,
    "gemini_vision": _gemini,
}

# The pixels of this test, in the two forms an API can take them in.
PIXELS_B64 = "aGVsbG8="
PIXELS_RAW = b"hello"

# The one shape each API takes the pixels in: a bare base64 field, exactly one
# data URL, or — google alone — the decoded bytes, which is why `PIXELS_RAW`
# exists. Asserting the exact payload rather than «the base64 is in there
# somewhere» is what makes the `data:` strip in `image_base64` load-bearing for
# the providers that do not re-wrap what they are handed, and what shows a
# decode that ran twice or not at all.
PIXELS_ON_THE_WIRE = {
    "anthropic_vision": PIXELS_B64,
    "ollama_vision": PIXELS_B64,
    "openai_vision": f"data:image/png;base64,{PIXELS_B64}",
    "deepseek_vision": f"data:image/png;base64,{PIXELS_B64}",
    "zai_vision": f"data:image/png;base64,{PIXELS_B64}",
    "llamacpp_vision": f"data:image/png;base64,{PIXELS_B64}",
    "gemini_vision": PIXELS_RAW,
}


def _carrying_the_pixels(node, _seen=None):
    """Every value anywhere in the recorded call that carries these pixels.

    A walk rather than `repr()`: repr flattens the body into one string in which
    a doubled prefix and a correct one are both merely substrings, and the
    question is what each individual field holds.

    Bytes count as well as text, and the walk descends through objects as well
    as containers, because one API takes neither: the google SDK wants the
    decoded bytes and carries them inside a `types.Part`, so whether the decode
    happened exactly once is visible only in there.
    """
    _seen = set() if _seen is None else _seen
    if id(node) in _seen:
        return []
    found = []
    if isinstance(node, str):
        if PIXELS_B64 in node:
            found.append(node)
    elif isinstance(node, (bytes, bytearray)):
        if PIXELS_RAW in node:
            found.append(bytes(node))
    elif isinstance(node, dict):
        _seen.add(id(node))
        for key, value in node.items():
            found += _carrying_the_pixels(key, _seen)
            found += _carrying_the_pixels(value, _seen)
    elif isinstance(node, (list, tuple, set)):
        _seen.add(id(node))
        for value in node:
            found += _carrying_the_pixels(value, _seen)
    elif hasattr(node, "__dict__"):
        _seen.add(id(node))
        for value in vars(node).values():
            found += _carrying_the_pixels(value, _seen)
    return found


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_an_image_with_only_a_path_is_refused_by_name(
    alias, a_real_file, monkeypatch, tmp_path
):
    """The refusal has to say who was asked and what was missing: `llm_manager`
    hands it to the caller, and «failed» alone sends the reader to the wrong
    layer."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(provider.generate_with_vision(
            "what is in this?",
            [{"path": a_real_file, "mime_type": "image/png"}],
        ))

    message = str(excinfo.value)
    assert alias in message
    assert "base64" in message


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_the_file_that_happens_to_sit_there_is_never_sent_to_a_model(
    alias, a_real_file, monkeypatch, tmp_path
):
    """The path resolves on this machine, so the old fallback succeeded — and a
    model answered about a file nobody sent."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(provider.generate_with_vision(
            "what is in this?",
            [{"path": a_real_file, "mime_type": "image/png"}],
        ))

    assert sent.calls == [], f"{alias} sent something to a model without base64"
    assert b64.b64encode(SOMEBODY_ELSES_BYTES).decode() not in repr(sent.calls)


@pytest.mark.parametrize("alias", sorted(BUILDERS))
def test_one_bad_image_stops_the_whole_call(alias, a_real_file, monkeypatch, tmp_path):
    """A partial set would reach the model as the whole set — the same rule the
    sending side took in `38004e0e`."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch, tmp_path)

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
def test_pixels_still_reach_the_model(alias, monkeypatch, tmp_path):
    """The refusal must not cost the working case: an image that carries its
    base64 goes through, and arrives in the one shape this API takes.

    The caller hands in a data URL, which is what the interface produces, so
    both halves are exercised at once — the prefix has to come off in
    `image_base64`, and a provider that needs it back has to put it back exactly
    once."""
    sent = _Sent()
    provider = BUILDERS[alias](sent, monkeypatch, tmp_path)

    out = asyncio.run(provider.generate_with_vision(
        "what is in this?",
        [{"base64": f"data:image/png;base64,{PIXELS_B64}", "mime_type": "image/png"}],
    ))

    assert out == "a cat"
    assert len(sent.calls) == 1
    carriers = _carrying_the_pixels(sent.calls[0])
    assert carriers == [PIXELS_ON_THE_WIRE[alias]], (
        f"{alias} put the pixels on the wire as {carriers!r}, expected exactly "
        f"[{PIXELS_ON_THE_WIRE[alias]!r}]"
    )


def test_the_type_the_sender_declared_reaches_google(monkeypatch, tmp_path):
    """Google is the one API handed the mime key as it stands in the image dict,
    so the key read has to be the key this project writes.

    It read `media_type`, which nothing here writes: DPTP §3.4 puts `mime_type`
    on the wire, and so does every local producer — `service.py`,
    `dpc_agent/tools/vision.py`, `dpc_agent/tools/document.py`,
    `dpc_agent/llm_adapter.py`. A webp was therefore announced to google as the
    default guess. What that default should be when the key really is absent is
    the separate open entry A-RECEIVER-ANNOUNCES-A-PICTURE-OF-UNKNOWN-TYPE-AS-A-PNG,
    and is deliberately not asserted here.
    """
    sent = _Sent()
    provider = _gemini(sent, monkeypatch, tmp_path)

    asyncio.run(provider.generate_with_vision(
        "what is in this?",
        [{"base64": PIXELS_B64, "mime_type": "image/webp"}],
    ))

    part = sent.calls[0]["contents"][0]
    assert part.inline_data.mime_type == "image/webp"


def test_google_is_handed_the_bytes_its_signature_asks_for(monkeypatch, tmp_path):
    """`Part.from_bytes` is typed `data: bytes`, and the guard returns base64
    text, so the decode has to happen in the provider.

    The recorded payload cannot show this: `Blob` carries pydantic's
    `val_json_bytes="base64"`, so text handed in comes out as the same bytes and
    the assertions above stay green either way. This reads the argument instead
    of the result. It matters because the coercion is model config rather than
    the signature, and it answers a malformed value in pydantic's words rather
    than with a refusal naming this provider.
    """
    sent = _Sent()
    provider = _gemini(sent, monkeypatch, tmp_path)
    handed = []
    from_bytes = provider._types.Part.from_bytes

    def spy(*, data, mime_type):
        handed.append(data)
        return from_bytes(data=data, mime_type=mime_type)

    monkeypatch.setattr(provider._types.Part, "from_bytes", spy)

    asyncio.run(provider.generate_with_vision(
        "what is in this?",
        [{"base64": PIXELS_B64, "mime_type": "image/png"}],
    ))

    assert handed == [PIXELS_RAW]


# The two vision entry points outside `BUILDERS`, each checkable in the file
# named. `remote_peer_provider.py`: `generate_with_vision` forwards the images
# to the peer and opens nothing here, so there is no local read to refuse — its
# own risk, a vision claim the peer never made, is pinned by
# `test_a_remote_peer_does_not_claim_vision_it_cannot_do.py`.
# `dpc_agent_provider.py`: `supports_vision()` returns False, so routing hands
# it no image, pinned by
# `test_the_embedded_agent_claims_no_vision_while_it_forwards_a_filename.py`.
