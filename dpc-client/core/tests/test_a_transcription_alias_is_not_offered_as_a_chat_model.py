"""A transcription-only alias is not a chat model, on either half of the menu.

The gateway's `/v1/models` is two lists joined: this node's own serving lists
and, under each proved peer's name, the rows that peer's `PROVIDERS_RESPONSE`
left. Both halves may legitimately carry a `local_whisper` alias — the P2P door
offers one to a peer holding transcription permission
(`service.menu_for_peer`), and `firewall.LOCAL_PROVIDER_TYPES` admits the type
to `compute.serving_local` — and neither of those aliases answers a chat
completion. Listed anyway, the guest chooses it from a menu this node published
and learns at the far end that it transcribes (observed 2026-09-10 on the Linux
node, board entry
THE-GATEWAY-OFFERS-A-PEERS-TRANSCRIPTION-MODEL-AS-SOMETHING-TO-CHAT-WITH).

So the row's own `type` decides: a transcription-only type is left off the list
and refused at the door with `model_not_found` — the word already meaning «not
on this menu» — and a message saying it transcribes. A type this node has no
name for is still listed: the last test here pins that choice, because failing
closed would hide a chat alias served by a host newer than this node.

The fixtures are the ones the other gateway test files build: a real `aiohttp`
listener on port 0, a fake `LLMManager`, a fake `p2p_coordinator`.
"""

import json

import pytest

from dpc_client_core.gateway import serves_chat
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_ALIAS,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    LOCAL,
    VENDOR,
    _Provider,
    _chat,
    _key,
    _providers,
    _request,
    _running,
    _service,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _messages,
    _post_messages,
)

WHISPER = "local_whisper_large"
WHISPER_MODEL = "openai/whisper-large-v3-turbo"
# One row as `service.build_p2p_provider_info` builds it for a `local_whisper`
# provider: a peer that shares transcription sends exactly this beside its chat
# alias, and `supports_voice` is the only capability it claims.
PEER_WHISPER_ROW = {
    "alias": "whisper-large-v3-turbo", "model": WHISPER_MODEL, "type": "local_whisper",
    "supports_vision": False, "supports_voice": True, "supports_tools": False,
    "context_window": None,
}
PEER_WHISPER_NAME = f"remote:{PEER}:{PEER_WHISPER_ROW['alias']}"
BOTH_KINDS_LOCAL = {"serving_local": [LOCAL, WHISPER], "serving_vendor": [VENDOR],
                    "vendor_quotas": {VENDOR: 2.0}}


def _with_whisper(tmp_path, **kwargs):
    """This node's own registry, grown a Whisper alias, with both in `serving_local`."""
    providers = _providers()
    providers[WHISPER] = _Provider("local_whisper", WHISPER_MODEL)
    return _service(tmp_path, BOTH_KINDS_LOCAL, providers=providers, **kwargs)


def _peer_with_whisper(tmp_path):
    """The proved peer's menu, grown the transcription row it would really send."""
    service = _peer_service(tmp_path)
    service.peer_metadata[PEER]["providers"].append(dict(PEER_WHISPER_ROW))
    return service


def _ids(text):
    return [model["id"] for model in json.loads(text)["data"]]


# --- (1) the list: the chat rows, and only those -----------------------------------


@pytest.mark.asyncio
async def test_the_models_list_keeps_this_nodes_chat_aliases_and_drops_its_transcription_one(tmp_path):
    async with _running(tmp_path, _with_whisper(tmp_path)) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        assert _ids(text) == [LOCAL, VENDOR], "a serving_local Whisper alias was offered as a chat model"


@pytest.mark.asyncio
async def test_the_models_list_keeps_a_peers_chat_rows_and_drops_its_transcription_row(tmp_path):
    async with _running(tmp_path, _peer_with_whisper(tmp_path)) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        ids = _ids(text)
        assert f"remote:{PEER}:{REMOTE_ALIAS}" in ids, "the peer's chat alias must stay on the menu"
        assert PEER_WHISPER_NAME not in ids, "the peer's Whisper row was offered as something to chat with"


# --- (2) the door: refused by name, before anything runs ---------------------------


@pytest.mark.asyncio
async def test_a_completion_on_this_nodes_transcription_alias_is_404_and_no_provider_is_called(tmp_path):
    service = _with_whisper(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(WHISPER))
        assert status == 404
        error = json.loads(text)["error"]
        assert error["code"] == "model_not_found"
        assert "transcribes and does not chat" in error["message"]
        assert WHISPER in error["message"] and "local_whisper" in error["message"]
        # Refused at the door: nothing reached the manager and no row was written
        # for a call that never ran.
        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_completion_on_a_peers_transcription_alias_is_404_in_both_shapes_and_nothing_crosses_the_wire(tmp_path):
    service = _peer_with_whisper(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=key, body=_chat(PEER_WHISPER_NAME))
        assert status == 404
        error = json.loads(text)["error"]
        assert error["code"] == "model_not_found"
        assert "transcribes and does not chat" in error["message"]
        assert PEER in error["message"], "the refusal names whose alias it is"

        # The Messages form refuses the same alias in its own envelope.
        status, text = await _post_messages(server, _messages(PEER_WHISPER_NAME), key=key)
        assert status == 404
        assert "transcribes and does not chat" in _anthropic_error(text)["message"]

        assert service.peer_calls == [], "a refused alias must not reach the host"
        assert list(ledger.rows()) == []


# --- (3) the choice this filter makes on a type it cannot name ---------------------


def test_a_provider_type_this_node_cannot_name_is_read_as_chat(tmp_path):
    """Fail open, on purpose: a host newer than this node may serve a chat type
    we have no name for, and a false absence from the menu is invisible to the
    caller while a false presence is now a refusal it can read."""
    assert serves_chat("a_type_shipped_after_this_node") is True
    assert serves_chat(None) is True
    assert serves_chat("local_whisper") is False


@pytest.mark.asyncio
async def test_a_peer_row_whose_type_is_unknown_stays_on_the_menu(tmp_path):
    service = _peer_with_whisper(tmp_path)
    service.peer_metadata[PEER]["providers"].append(
        {"alias": "tomorrow", "model": "m", "type": "a_type_shipped_after_this_node"}
    )
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200 and f"remote:{PEER}:tomorrow" in _ids(text)
