"""`compute.enabled` governs both doors: the peer one and the loopback gateway.

Mike's call, 2026-09-13 (board: THE-GATEWAY-SERVES-ITS-OWN-MACHINE-WHATEVER-
COMPUTE-SHARING-SAYS), option (b). Two switches now stand on one door and the
table between them is AND (Ark's review, 2026-09-13): the gateway serves only
while `compute.enabled` in `privacy_rules.json` and `[gateway] enabled` in
`config.ini` are both true. Either one off shuts it, and `compute.enabled` off
shuts the peer door as well — which `[gateway] enabled` never touches.

The flag is asked of the live firewall on every request, not read once at
start: the rules reload on save (`firewall_rules_updated`), and a door that
kept a copy would serve for the rest of the session after the owner turned
sharing off. The refusal is `404` — the status this surface already answers
for an alias it does not serve — and it names `compute.enabled`, so the reason
is in the client's own error message rather than in a log the IDE never sees.

The listener is a real `aiohttp` `TCPSite` on port 0; the stand-in service,
the running listener and the key are the ones the OpenAI-shape file builds.
Cross-platform: pure asyncio.
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.settings import Settings
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_MODEL,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    VENDOR,
    _chat,
    _key,
    _request,
    _running,
    _service,
    _write_rules,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _messages,
    _post_messages,
)

SHARING = dict(BOTH_LISTS, enabled=True)
NOT_SHARING = dict(BOTH_LISTS, enabled=False)
# The example file the repository ships beside the client.
EXAMPLE_RULES = Path(__file__).resolve().parents[2] / "privacy_rules.example.json"


def _openai_error(text):
    error = json.loads(text)["error"]
    assert set(error) == {"message", "type", "code"}, error
    return error


# --- (1) the three routes with the flag off -------------------------------------------


@pytest.mark.asyncio
async def test_with_compute_sharing_off_models_lists_nothing_though_both_lists_are_full(tmp_path):
    """The lists are full and classify cleanly; the flag alone empties the menu."""
    service = _service(tmp_path, NOT_SHARING)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        assert json.loads(text) == {"object": "list", "data": []}


@pytest.mark.asyncio
async def test_with_compute_sharing_off_a_completion_is_404_naming_compute_enabled(tmp_path):
    """Both shapes refuse in their own envelope, reaching neither provider nor ledger."""
    service = _service(tmp_path, NOT_SHARING)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)

        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=key, body=_chat(LOCAL))
        error = _openai_error(text)
        assert status == 404 and error["type"] == "invalid_request_error"
        assert "compute.enabled" in error["message"] and LOCAL in error["message"]

        status, text = await _post_messages(server, _messages(VENDOR), key=key)
        error = _anthropic_error(text)
        assert status == 404 and error["type"] == "not_found_error"
        assert "compute.enabled" in error["message"] and VENDOR in error["message"]

        # A stream is refused before the first byte: nothing opens and falls over.
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=key, body=_chat(LOCAL, stream=True))
        assert status == 404 and "compute.enabled" in _openai_error(text)["message"]

        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_with_compute_sharing_off_a_peer_alias_is_refused_before_any_p2p_call(tmp_path):
    """The peer route is already shut on the host's side (`can_request_inference`);
    this shuts it on the requester's, so nothing leaves this machine at all."""
    service = _peer_service(tmp_path, compute=NOT_SHARING)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=key, body=_chat(REMOTE_MODEL))
        error = _openai_error(text)
        assert status == 404 and "compute.enabled" in error["message"]
        assert service.peer_calls == [], "the guard stands before request_inference_from_peer"
        assert list(ledger.rows()) == []

        # And the peer's menu is not advertised from a shut door either.
        status, text = await _request(server, "GET", "/v1/models", key=key)
        assert (status, json.loads(text)["data"]) == (200, [])
        assert PEER not in text


# --- (2) the truth table: two switches, one door, AND ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("compute_enabled, gateway_enabled, door_open", [
    (True, True, True),
    (True, False, False),
    (False, True, False),
    (False, False, False),
])
async def test_the_door_is_open_only_when_both_switches_are_on(
    tmp_path, monkeypatch, compute_enabled, gateway_enabled, door_open,
):
    """`[gateway] enabled` decides whether there is a listener at all, and
    `compute.enabled` decides whether that listener serves anything."""
    from dpc_client_core import service as service_module

    (tmp_path / "config.ini").write_text(
        f"[gateway]\nenabled = {str(gateway_enabled).lower()}\n", encoding="utf-8")
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    svc = service_module.CoreService.__new__(service_module.CoreService)
    svc.settings = Settings(tmp_path)
    svc.p2p_coordinator = types.SimpleNamespace(_peer_inference_lock=asyncio.Semaphore(1))
    assert (svc._build_gateway() is not None) is gateway_enabled
    if not gateway_enabled:
        assert not door_open, "no listener is a shut door whatever compute says"
        return

    service = _service(tmp_path, dict(BOTH_LISTS, enabled=compute_enabled))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        status, text = await _request(server, "GET", "/v1/models", key=key)
        served = [model["id"] for model in json.loads(text)["data"]]
        completion, _ = await _request(server, "POST", "/v1/chat/completions",
                                       key=key, body=_chat(LOCAL))
        assert (served == [LOCAL, VENDOR]) is door_open
        assert (completion == 200) is door_open


# --- (3) the flag is read live, on every request -----------------------------------------


@pytest.mark.asyncio
async def test_flipping_the_flag_on_the_live_firewall_changes_the_next_request(tmp_path):
    """No restart: the rules reload on save and the door asks the firewall each time."""
    service = _service(tmp_path, SHARING)
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)

        async def completion():
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=key, body=_chat(LOCAL))
            return status, text

        assert (await completion())[0] == 200

        _write_rules(tmp_path, NOT_SHARING)
        assert service.firewall.reload()[0] is True
        status, text = await completion()
        assert status == 404 and "compute.enabled" in _openai_error(text)["message"]
        _, listed = await _request(server, "GET", "/v1/models", key=key)
        assert json.loads(listed)["data"] == []

        _write_rules(tmp_path, SHARING)
        assert service.firewall.reload()[0] is True
        assert (await completion())[0] == 200
        assert server.is_running, "the listener never restarted"


# --- (4) the rules file says it, in both places the owner reads -------------------------


def test_the_rules_template_and_the_example_say_the_flag_closes_both_doors(tmp_path):
    """A rule nobody can read from the file is a rule discovered by surprise."""
    ContextFirewall(tmp_path / "privacy_rules.json")  # writes the default template
    written = json.loads((tmp_path / "privacy_rules.json").read_text(encoding="utf-8"))
    for comment in (written["compute"]["_comment"],
                    json.loads(EXAMPLE_RULES.read_text(encoding="utf-8"))["compute"]["_comment"]):
        lowered = comment.lower()
        assert "gateway" in lowered, comment
        assert "peer" in lowered, comment
