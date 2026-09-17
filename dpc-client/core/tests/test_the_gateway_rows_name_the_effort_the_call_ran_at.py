"""The gateway's two rows name the effort their call ran at.

`served_effort` (91d65dcb) reached the host's row and the agent's requester
row and stopped there: both of the gateway's `usage_row` calls passed nothing,
so a client billed by the token had the column on every row but the
gateway's. Here it is written on both.

The peer route copies the host's word off the wire, which is the only place
this node can learn what depth it paid for. The local route asks the door:
`LLMManager.query` and `query_messages` now report the effort they applied,
beside `output_includes_thinking`, which is the other column set where the
fact is known. Today `query_messages` applies none — the gateway's `complete`
has no effort parameter, so nothing reaches it and the provider's own
configuration decides — and the door says so with None, which is «no effort
control was applied» and not `off`.
"""

import pytest

from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    _chat,
    _key,
    _request,
    _running,
    _service,
)
from tests.test_the_message_shaped_door_reaches_the_provider_unflattened import (
    FLAT,
    MESSAGES,
    SYSTEM,
    _Plain,
    _manager,
)


# --- the door reports what it applied ----------------------------------------------


@pytest.mark.asyncio
async def test_the_prompt_door_reports_the_effort_it_was_given(tmp_path):
    manager = _manager(tmp_path, _Plain())

    result = await manager.query(FLAT, return_metadata=True, reasoning_effort="high")

    assert result["served_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"reasoning_effort": "shallow"}],
                         ids=["nothing asked", "a word off the scale"])
async def test_the_prompt_door_reports_none_when_no_effort_was_applied(tmp_path, kwargs):
    """An unknown word is dropped by `normalize_reasoning_effort` before it
    reaches the provider, so the row must not claim it was served at one."""
    manager = _manager(tmp_path, _Plain())

    result = await manager.query(FLAT, return_metadata=True, **kwargs)

    assert result["served_effort"] is None


@pytest.mark.asyncio
async def test_the_message_door_carries_the_column_and_applies_no_effort_today(tmp_path):
    """A row is built from either door's dict, so both carry the key."""
    manager = _manager(tmp_path, _Plain())

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert "served_effort" in result and result["served_effort"] is None


# --- the gateway's local row -------------------------------------------------------


def _effort_service(tmp_path, word):
    """The loopback stand-in, with a door that names the effort it applied."""
    service = _service(tmp_path, BOTH_LISTS)
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        return dict(await inner(messages, **kwargs), served_effort=word)

    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_a_local_gateway_row_carries_the_word_the_door_applied(tmp_path):
    async with _running(tmp_path, _effort_service(tmp_path, "high")) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert (row["route"], row["caller_kind"]) == ("local", "gateway")
        assert row["served_effort"] == "high"


@pytest.mark.asyncio
async def test_a_local_gateway_row_says_none_when_the_door_applied_nothing(tmp_path):
    async with _running(tmp_path, _effort_service(tmp_path, None)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["served_effort"] is None


# --- the gateway's peer row --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_gateway_row_copies_the_hosts_word_off_the_wire(tmp_path):
    service = _peer_service(tmp_path, result=_priced_result(served_effort="low"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["route"] == "peer"
        assert row["served_effort"] == "low", "the host's word, after the host's clamp"


@pytest.mark.asyncio
async def test_a_host_that_sent_no_word_leaves_the_peer_row_none(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["served_effort"] is None
