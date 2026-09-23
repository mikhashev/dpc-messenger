"""On a peer route, who pays per token follows the tariff the peer quoted, not the route.

ADR-041 (amendment 2026-09-13): the payer is the caller. The route alone said
"peer" for every call through another node, so an agent on a peer's own model
read a payer where its twin on this node read `nobody`, and a peer whose model
nobody could classify was still named as the payer. The tariff arrives in the
peer's menu row before any call: no key is the v1 gift, `free: true` is a
declared zero, and rates above zero are paid.
"""

import types

import pytest

from dpc_client_core.dpc_agent.provider_facts import provider_facts_for

PEER = "dpc-node-bob"
PAID = {"in": 0.5, "out": 1.5, "currency": "USD", "unit": "per_1m", "free": False}
FREE = {"in": 0, "out": 0, "currency": "USD", "unit": "per_1m", "free": True}


def _llm():
    return types.SimpleNamespace(providers={}, agent_provider=None, default_provider=None)


def _facts(row_type, tariff=None, *, no_row=False):
    row = {"alias": "m", "type": row_type}
    if tariff is not None:
        row["tariff"] = tariff
    peers = {PEER: {"providers": [] if no_row else [row]}}
    return provider_facts_for(_llm(), "m", compute_host=PEER, peer_metadata=peers)


@pytest.mark.parametrize("row_type, tariff, payer", [
    ("llamacpp_server", PAID, "this_node"),
    ("deepseek", PAID, "this_node"),
    ("llamacpp_server", None, "nobody"),
    ("llamacpp_server", FREE, "nobody"),
    ("deepseek", None, "peer"),
    ("deepseek", FREE, "peer"),
    ("something_new", None, "unknown"),
    ("something_new", PAID, "this_node"),
])
def test_the_payer_is_read_from_the_quoted_tariff_and_the_model(row_type, tariff, payer):
    assert _facts(row_type, tariff)["tokens_paid_by"] == payer


def test_a_peer_whose_menu_row_we_do_not_have_names_no_payer():
    facts = _facts("llamacpp_server", no_row=True)
    assert facts["provider_kind"] == "unknown"
    assert facts["tokens_paid_by"] == "unknown"


def test_the_same_self_hosted_situation_answers_the_same_on_either_route():
    local = provider_facts_for(
        types.SimpleNamespace(
            providers={"q": types.SimpleNamespace(config={"type": "llamacpp_server"})},
            agent_provider=None, default_provider=None,
        ),
        "q",
    )
    assert local["tokens_paid_by"] == _facts("llamacpp_server")["tokens_paid_by"] == "nobody"


def test_an_openai_compatible_row_without_its_address_is_not_called_a_vendor():
    """The menu carries no base_url, so a peer's own server behind the OpenAI
    wire format cannot be told from a vendor; guessing vendor would name the
    peer as payer for a model on its own card."""
    facts = _facts("openai_compatible")
    assert facts["provider_kind"] == "unknown"
    assert facts["tokens_paid_by"] == "unknown"
