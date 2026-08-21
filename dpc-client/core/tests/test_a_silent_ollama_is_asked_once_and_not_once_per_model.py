"""A daemon that does not answer is one fact, not one fact per model.

Pins the distinction the fix rests on — no response is about the host, an
error response is about the model — and that the window expires on its own.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import ollama
import pytest

from dpc_client_core.providers import ollama_provider as OP


ELEVEN = [f"model-{n}:latest" for n in range(11)]


class Answer:
    def __init__(self, capabilities=None):
        self.capabilities = capabilities or ["completion"]


class FakeClient:
    """Stands in for the daemon. `raises` decides what asking it costs."""

    calls = 0
    hosts: list = []
    raises = None

    def __init__(self, host=None, timeout=None):
        self.host = host

    def show(self, model):
        FakeClient.calls += 1
        FakeClient.hosts.append(self.host)
        if FakeClient.raises is not None:
            raise FakeClient.raises
        return Answer()


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves by hand, so the window needs no real waiting."""
    now = [1000.0]
    monkeypatch.setattr(OP, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    OP._MODEL_INFO.clear()
    OP._HOST_SILENT_SINCE.clear()
    FakeClient.calls = 0
    FakeClient.hosts = []
    FakeClient.raises = None
    monkeypatch.setattr(OP.ollama, "Client", FakeClient)
    yield
    OP._MODEL_INFO.clear()
    OP._HOST_SILENT_SINCE.clear()


def _sweep(host=None):
    """What one provider-list read does: ask about every configured model."""
    return [OP._describe(model, host) for model in ELEVEN]


# --- the defect this closes -------------------------------------------------


def test_eleven_models_on_a_silent_host_cost_one_question(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")

    assert _sweep() == [None] * 11
    assert FakeClient.calls == 1


def test_a_second_read_inside_the_window_asks_nothing_at_all(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    _sweep()
    FakeClient.calls = 0

    assert _sweep() == [None] * 11
    assert FakeClient.calls == 0


# --- and the window has to let go -------------------------------------------


def test_the_window_expires_and_the_host_is_asked_again(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    _sweep()
    FakeClient.calls = 0

    clock[0] += OP._HOST_SILENT_SECONDS + 0.001

    _sweep()
    assert FakeClient.calls == 1, "an expired window must cost one probe, not none"


def test_a_daemon_that_came_back_is_believed_immediately(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    _sweep()
    clock[0] += OP._HOST_SILENT_SECONDS + 0.001
    FakeClient.raises = None
    FakeClient.calls = 0

    described = _sweep()

    assert all(d is not None for d in described)
    assert FakeClient.calls == 11, "every model is asked about once the host answers"
    assert OP._HOST_SILENT_SINCE == {}


# --- no response is about the host; an error response is about the model -----


def test_an_error_the_daemon_answered_with_never_silences_the_host(clock):
    FakeClient.raises = ollama.ResponseError("model not found")

    assert _sweep() == [None] * 11
    assert FakeClient.calls == 11, "the daemon answered, so every model is still asked"
    assert OP._HOST_SILENT_SINCE == {}


def test_an_error_answer_clears_a_standing_silence(clock):
    OP._HOST_SILENT_SINCE[""] = clock[0]
    clock[0] += OP._HOST_SILENT_SECONDS + 0.001
    FakeClient.raises = ollama.ResponseError("model not found")

    OP._describe("model-0:latest", None)

    assert OP._HOST_SILENT_SINCE == {}, "a host that answers is available again"


def test_a_real_answer_clears_a_standing_silence(clock):
    OP._HOST_SILENT_SINCE[""] = clock[0]
    clock[0] += OP._HOST_SILENT_SECONDS + 0.001

    assert OP._describe("model-0:latest", None) is not None
    assert OP._HOST_SILENT_SINCE == {}


# --- one host's silence is its own ------------------------------------------


def test_one_silent_host_does_not_silence_another(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    _sweep(host="http://gpu-box:11434")
    FakeClient.calls = 0
    FakeClient.hosts = []

    _sweep(host="http://other-box:11434")

    assert FakeClient.calls == 1
    assert FakeClient.hosts == ["http://other-box:11434"]


def test_the_default_host_is_one_host_whether_it_is_named_or_not():
    assert OP._host_key(None) == OP._host_key("")
    assert OP._host_key("http://gpu-box:11434") != OP._host_key(None)


# --- the model cache still wins ---------------------------------------------


def test_a_model_already_described_is_returned_while_its_host_is_silent(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    OP._describe("model-0:latest", None)  # marks the host silent
    known = Answer(["completion", "vision"])
    OP._MODEL_INFO["model-0:latest"] = known
    FakeClient.calls = 0

    assert OP._describe("model-0:latest", None) is known
    assert FakeClient.calls == 0


# --- the answer a caller gets does not change, only what it costs ------------


def test_a_silent_host_leaves_the_name_list_deciding_exactly_as_before(clock):
    FakeClient.raises = httpx.ConnectTimeout("timed out")
    vision = OP.OllamaProvider("a", {"model": "qwen3-vl:8b"})
    plain = OP.OllamaProvider("b", {"model": "llama3.1:8b"})

    # First reads pay one probe between them; the answers come from the lists.
    assert vision.supports_vision() is True
    assert plain.supports_vision() is False
    assert FakeClient.calls == 1
