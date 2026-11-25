"""An audio model (Music Flamingo) as a DPC llama-server engine (backlog card
THE-LLAMA-SERVER-PROVIDER-SENDS-ONLY-IMAGES-SO-AN-AUDIO-MODEL-LIKE-MUSIC-FLAMINGO-CANNOT-BE-A-DPC-ENGINE).

The wire shape is the one the pinned server reads (b12110 tools/server/server-common.cpp,
`oaicompat_content_load_media`): `{"type": "input_audio", "input_audio": {"data": <base64>,
"format": ...}}`. The request is captured by a fake llama-server behind the real OpenAI SDK
(an httpx mock transport), so the body checked is the body the SDK would send.

An audio projector is not a vision one: before a child is up the alias's `mmproj_kind`
decides, after it `/props` `modalities` does, a yes and a no alike.
"""

import base64
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import (
    _ACTIVE_SUPERVISORS, _WIRE_MESSAGES, MAX_AUDIO_BYTES, audio_format_of, audio_part, mmproj_kind_of,
)

GGUF = "D:/models/music-flamingo-hf.Q8_0.gguf"


def _wav_bytes(frames=160):
    data = b"\x00\x00" * frames
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


def _provider(**overrides):
    config = {"type": "llamacpp_server", "gguf_path": GGUF}
    config.update(overrides)
    return LlamaServerProvider("music_flamingo", config)


class _FakeSupervisor:
    def __init__(self, props=None):
        self.port = 8123
        self.props = props if props is not None else {"total_slots": 1}
        self.ensure_calls = 0
        self.slot_enters = 0

    async def ensure_running(self):
        self.ensure_calls += 1
        return self.props

    def call_slot(self):
        self.slot_enters += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def drain(self, timeout=0.0):
        return None


class _FakeLlamaServer:
    """`/v1/chat/completions` of a llama-server, recording each JSON body."""

    def __init__(self, answer="a slow ballad in D minor"):
        self.bodies = []
        self.answer = answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": self.answer}}],
            "usage": {"prompt_tokens": 750, "completion_tokens": 9, "total_tokens": 759},
        })

    def client(self):
        return AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8123/v1", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


@pytest.fixture(autouse=True)
def _clean_registry():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _wired(p, server, props=None):
    sup = _FakeSupervisor(props)
    p.supervisor = sup
    client = server.client()

    async def _ensure():
        await sup.ensure_running()
        return client

    p._ensure = _ensure
    return sup


# -- capability: audio is not vision ----------------------------------------------------


def test_an_alias_with_no_projector_takes_neither():
    p = _provider()
    assert not p.supports_vision() and not p.supports_audio()


def test_an_undeclared_projector_stays_an_image_one():
    p = _provider(mmproj="mm.gguf")
    assert p.supports_vision() and not p.supports_audio()


def test_an_audio_projector_is_audio_and_not_vision_before_the_child_is_up():
    p = _provider(mmproj="mmproj-music-flamingo.gguf", mmproj_kind="audio")
    assert p.supports_audio() and not p.supports_vision()
    both = _provider(mmproj="omni.gguf", mmproj_kind="vision+audio")
    assert both.supports_audio() and both.supports_vision()


def test_a_kind_without_a_projector_takes_nothing():
    assert not _provider(mmproj_kind="audio").supports_audio()


def test_an_unknown_kind_is_refused_at_load():
    with pytest.raises(ValueError, match="mmproj_kind"):
        _provider(mmproj="mm.gguf", mmproj_kind="sound")
    assert mmproj_kind_of("a", {"mmproj": "x"}) == "vision"
    assert mmproj_kind_of("a", {"mmproj_kind": "audio"}) is None


def test_a_live_childs_props_decide_both_ways():
    # The alias says vision (undeclared), the loaded projector says audio only: /props wins.
    p = _provider(mmproj="mm.gguf")
    p.supervisor = _FakeSupervisor({"modalities": {"vision": False, "video": False, "audio": True}})
    assert p.supports_audio() and not p.supports_vision()
    # The alias says audio, a child without the projector answers all-false.
    q = _provider(mmproj="mm.gguf", mmproj_kind="audio")
    q.supervisor = _FakeSupervisor({"modalities": {"vision": False, "video": False, "audio": False}})
    assert not q.supports_audio() and not q.supports_vision()
    # Props with no modalities key fall back to the alias.
    r = _provider(mmproj="mm.gguf", mmproj_kind="audio")
    r.supervisor = _FakeSupervisor({"total_slots": 1})
    assert r.supports_audio() and not r.supports_vision()


def test_a_dead_or_stopped_childs_props_no_longer_speak_for_the_alias():
    """Review #180 2.4: `/props` of an exited child still answered as if it were alive."""
    p = _provider(mmproj="mm.gguf")                          # undeclared: an image projector
    sup = _FakeSupervisor({"modalities": {"vision": False, "video": False, "audio": True}})
    p.supervisor = sup
    sup._proc = SimpleNamespace(returncode=None)              # alive: /props decide
    assert p.supports_audio() and not p.supports_vision()
    sup._proc = SimpleNamespace(returncode=1)                 # exited: the alias decides again
    assert not p.supports_audio() and p.supports_vision()
    sup._proc = None                                          # stopped
    assert not p.supports_audio() and p.supports_vision()


# -- the part ---------------------------------------------------------------------------


def test_the_container_is_read_from_the_header():
    assert audio_format_of(_wav_bytes()) == "wav"
    assert audio_format_of(b"fLaC" + b"\x00" * 20) == "flac"
    assert audio_format_of(b"ID3\x04" + b"\x00" * 20) == "mp3"
    assert audio_format_of(b"\xff\xfb\x90\x00" + b"\x00" * 20) == "mp3"
    assert audio_format_of(b"OggS" + b"\x00" * 20) is None
    assert audio_format_of(b"RIFF") is None


def test_the_part_from_a_path_and_from_bytes(tmp_path):
    wav = _wav_bytes()
    f = tmp_path / "take.wav"
    f.write_bytes(wav)
    part, size = audio_part(f, "a")
    assert part == {"type": "input_audio",
                    "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}}
    assert size == len(wav)
    assert audio_part(wav, "a")[0] == part
    assert audio_part(str(f), "a")[0] == part


def test_bad_audio_is_refused_before_any_call(tmp_path):
    with pytest.raises(ValueError, match="not WAV, MP3 or FLAC"):
        audio_part(b"OggS" + b"\x00" * 40, "a")
    with pytest.raises(ValueError, match="empty"):
        audio_part(b"", "a")
    with pytest.raises(ValueError, match="not a file"):
        audio_part(tmp_path / "missing.wav", "a")
    with pytest.raises(ValueError, match="MiB"):
        audio_part(b"RIFF" + b"\x00" * (MAX_AUDIO_BYTES + 1), "a")


# -- the call ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_clip_rides_as_an_input_audio_part_on_the_wire(tmp_path):
    p = _provider(mmproj="mmproj.gguf", mmproj_kind="audio")
    server = _FakeLlamaServer()
    sup = _wired(p, server)
    wav = _wav_bytes()
    f = tmp_path / "take.wav"
    f.write_bytes(wav)

    out = await p.generate_with_audio("Describe the style.", f)

    assert out == "a slow ballad in D minor"
    body = server.bodies[0]
    content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert content[0] == {"type": "input_audio",
                          "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}}
    assert content[1] == {"type": "text", "text": "Describe the style."}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert sup.ensure_calls == 1 and sup.slot_enters == 1
    assert p.get_last_usage()["prompt_tokens"] == 750


@pytest.mark.asyncio
async def test_the_clip_is_encoded_once_for_every_retry_and_each_attempts_record_sees_only_its_hash(tmp_path, monkeypatch):
    """Review #180 2.3: the clip was serialised again on each attempt (bytes + base64 + text + JSON)."""
    p = _provider(mmproj="mmproj.gguf", mmproj_kind="audio")
    wav = _wav_bytes()
    f = tmp_path / "take.wav"
    f.write_bytes(wav)
    encodes = []
    real = base64.b64encode
    monkeypatch.setattr(base64, "b64encode", lambda data, *a: encodes.append(len(data)) or real(data, *a))
    bodies, seen = [], []

    def flaky(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "loading model"}})
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}})
    client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8123/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(flaky)))
    p.supervisor = _FakeSupervisor()

    async def _ensure():
        seen.append(_WIRE_MESSAGES.get())
        return client

    async def _retry_now(fn, _err):
        return await fn()
    p._ensure = _ensure
    p._retry_with_backoff = _retry_now

    assert await p.generate_with_audio("caption", f) == "ok"
    assert encodes == [len(wav)] and len(bodies) == 2
    assert [b["messages"][0]["content"][0]["input_audio"]["data"] for b in bodies] == [real(wav).decode("ascii")] * 2
    assert len(seen) == 2
    for record in seen:
        clip = record[0]["content"][0]["input_audio"]
        assert "data" not in clip and clip["bytes"] == len(wav) and len(clip["sha256_of_base64"]) == 64


@pytest.mark.asyncio
async def test_an_alias_that_takes_no_audio_refuses_before_the_child_is_asked():
    p = _provider(mmproj="mm.gguf")
    server = _FakeLlamaServer()
    sup = _wired(p, server)
    with pytest.raises(ValueError, match="takes no audio"):
        await p.generate_with_audio("what is this", _wav_bytes())
    assert server.bodies == [] and sup.ensure_calls == 0


@pytest.mark.asyncio
async def test_an_image_on_an_audio_alias_is_refused_with_a_reason():
    # Images reach a llama-server alias through llm_manager.query and the gateway (both ask
    # supports_vision first) or in the turns of the tools path, which refuses here.
    p = _provider(mmproj="mmproj.gguf", mmproj_kind="audio")
    server = _FakeLlamaServer()
    sup = _wired(p, server)
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    with pytest.raises(ValueError, match="music_flamingo.*vision"):
        await p.generate_with_tools([{"role": "user", "content": [{"type": "text", "text": "what is this"}, shot]}],
                                    [{"name": "read_file", "description": "", "input_schema": {"type": "object"}}])
    assert server.bodies == [] and sup.ensure_calls == 0


@pytest.mark.asyncio
async def test_a_server_error_is_reported_as_the_audio_path_failing():
    p = _provider(mmproj="mmproj.gguf", mmproj_kind="audio", max_retry_seconds=0)

    def refuse(request):
        return httpx.Response(400, json={"error": {"message": "audio input is not supported - hint: "
                                                              "you may need to provide the mmproj"}})
    sup = _FakeSupervisor()
    p.supervisor = sup
    client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8123/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))

    async def _ensure():
        return client
    p._ensure = _ensure
    with pytest.raises(RuntimeError, match="audio failed for 'music_flamingo'.*audio input is not supported"):
        await p.generate_with_audio("caption", _wav_bytes())
