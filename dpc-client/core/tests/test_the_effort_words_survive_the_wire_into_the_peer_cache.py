"""The words have to arrive, not merely be built.

test_a_peer_learns_which_effort_words_its_model_takes.py stops at the builder:
it proves `build_p2p_provider_info` puts the words in the row. That is where
this defect class keeps hiding — the value exists at the producer and never
reaches the consumer — so a builder assertion alone would have passed on the
broken tree too, because the builder was never the half that was wrong.

This file walks the rest of the path with nothing faked in the middle: the row
goes into `create_providers_response`, through a JSON round trip standing in
for the wire, into `handle_providers_response`, and out of
`CoreService.peer_metadata` — the cache the agent's model dialog reads its
remote rows from (agent_service.get_agent_model_config).

The JSON hop is not ceremony. The builder copies the words with `list(...)`,
and a tuple that never met json.dumps would satisfy an in-memory assertion
while arriving as something else.
"""

import asyncio
import json

from types import SimpleNamespace

from dpc_protocol.protocol import create_providers_response

from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService

from tests.test_a_peer_learns_which_effort_words_its_model_takes import (
    TemplateAwareProvider,
    _info_for,
)
from tests.test_provider_metadata import FakeProvider

PEER = "dpc-node-" + "a" * 32


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _receiving_node():
    """A stand-in for the node on the other end, holding only what the handler
    touches: the peer cache, the pending-request map and the UI channel."""
    return SimpleNamespace(
        peer_metadata={},
        _pending_providers_requests={},
        local_api=_Api(),
    )


def _rows_after_the_wire(provider):
    """Serve one provider, ship it, receive it, and return the cached rows."""
    served = _info_for(provider)
    message = create_providers_response([served])

    delivered = json.loads(json.dumps(message))

    node = _receiving_node()
    asyncio.run(
        P2PCoordinator.handle_providers_response(
            SimpleNamespace(service=node),
            PEER,
            delivered["payload"]["providers"],
        )
    )
    return node.peer_metadata[PEER]["providers"]


def test_the_peer_can_offer_the_word_its_own_model_named():
    rows = _rows_after_the_wire(
        TemplateAwareProvider(["xhigh", "medium", "low"], "xhigh", "model")
    )

    assert rows[0]["reasoning_words"] == ["xhigh", "medium", "low"]
    assert rows[0]["reasoning_default"] == "xhigh"


def test_the_fallback_table_still_does_not_arrive_wearing_the_models_name():
    """The guard has to hold at the far end too — a receiver cannot tell a
    constant table from a template once both are just a list of strings."""
    rows = _rows_after_the_wire(
        TemplateAwareProvider(["max", "high", "medium", "low"], None, "fallback")
    )

    assert "reasoning_words" not in rows[0]
    assert "reasoning_default" not in rows[0]


def test_a_provider_with_no_template_crosses_unchanged():
    rows = _rows_after_the_wire(
        FakeProvider("glm-5.1", ptype="zai", context_window=204800)
    )

    assert "reasoning_words" not in rows[0]
    assert rows[0]["model"] == "glm-5.1"
    assert rows[0]["context_window"] == 204800
