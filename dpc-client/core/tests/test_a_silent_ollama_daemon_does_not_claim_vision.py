"""A daemon that cannot be reached has not said its model takes images.

`OllamaProvider.supports_vision()` asked `/api/show` and, when nothing answered,
fell back to `OLLAMA_VISION_MODELS` — a substring list of model names. The
connection error was swallowed on the way, so a node with no Ollama at all still
advertised itself as a working vision provider on the strength of its own
configuration file.

Measured on the Linux node 2026-09-09 03:02:33: `Auto-selected vision provider
'ollama_vision'` → `Failed to connect to Ollama` → `describe_for_agents (VL)
failed for group image`. Ollama is not installed on that node — no binary, no
unit, 11434 refuses — while a peer that had served two vision turns eighteen
minutes earlier sat connected and idle. The name list answered for a daemon
nobody could ask, and the group's image went undescribed.

Three answers have to stay three (`_reported_capabilities`), and this is the one
that was folded into the wrong neighbour:

  * the daemon answered and named its capabilities  → the daemon decides;
  * the daemon answered without the field (too old) → the name list decides;
  * nobody could be asked                           → no. Not the list.

The third reads as no for the reason `eae3fe66` gives: a refusal names the
provider, and a yes we cannot honour becomes a failure somewhere further down.
"""

import httpx
import pytest

from dpc_client_core.providers import ollama_provider as OP


class _Daemon:
    """Stands in for `/api/show`. `answer` says how the host behaves."""

    answer = ["completion", "vision"]

    def __init__(self, host=None, timeout=None):
        pass

    def show(self, model):
        if _Daemon.answer == "unreachable":
            raise httpx.ConnectError("Failed to connect to Ollama")
        if _Daemon.answer == "no-field":
            class _OldAnswer:
                pass
            return _OldAnswer()
        return type("_Answer", (), {"capabilities": list(_Daemon.answer)})()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    OP._MODEL_INFO.clear()
    OP._HOST_SILENT_SINCE.clear()
    _Daemon.answer = ["completion", "vision"]
    monkeypatch.setattr(OP.ollama, "Client", _Daemon)
    yield
    OP._MODEL_INFO.clear()
    OP._HOST_SILENT_SINCE.clear()


def _provider(model="qwen3-vl:8b"):
    return OP.OllamaProvider("ollama_vision", {"model": model, "host": "http://127.0.0.1:11434"})


def test_a_daemon_that_never_answered_is_not_a_vision_claim():
    """The live case: the model name is on the list, and there is no Ollama."""
    _Daemon.answer = "unreachable"
    assert _provider("qwen3-vl:8b").supports_vision() is False


def test_every_name_on_the_list_answers_the_same_way_with_no_daemon():
    _Daemon.answer = "unreachable"
    for model in ("qwen3.5:9b", "llava:13b", "llama3.2-vision:11b", "moondream:latest"):
        assert _provider(model).supports_vision() is False


def test_a_live_daemon_still_decides_both_ways():
    _Daemon.answer = ["completion", "vision"]
    assert _provider("muse-glimmer:latest").supports_vision() is True

    OP._MODEL_INFO.clear()
    _Daemon.answer = ["completion"]
    assert _provider("qwen3-vl-textonly:8b").supports_vision() is False


def test_a_daemon_too_old_for_the_field_still_leaves_the_list_in_charge():
    """The list is a fallback for an answer that lacks the field — not for the
    absence of an answer. That distinction is the whole fix."""
    _Daemon.answer = "no-field"
    assert _provider("qwen3-vl:8b").supports_vision() is True
    assert _provider("ornith:9b-q8_0").supports_vision() is False
