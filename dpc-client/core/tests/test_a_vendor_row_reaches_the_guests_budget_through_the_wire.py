"""A vendor menu row reaches the guest's runtime budget as the host built it.

The builder is tested on its own (`test_the_peer_door_serves_a_vendor_alias_the_owner_allows`)
and so is the guest's reader (`_peer_facts`); this file walks the path between
them with nothing faked in the middle. The host's real `ContextFirewall` over a
rules file and the real `CoreService.menu_for_peer` build the row; it goes into
`create_providers_response`, through a JSON round trip standing in for the
wire, into `P2PCoordinator.handle_providers_response`, and out through
`provider_facts_for(..., compute_host=HOST)` — what an agent pinned to that
host prints in its budget.

The alias is `openai_compatible` on purpose: the guest's own type table cannot
class it without a base_url, which a row never carries, so `vendor` on the
guest's side can only have come from the host's statement on the row. And the
money half rides on it: with a tariff the guest pays (`this_node`), without
one the host bears the vendor's cost (`peer`) — `_peer_payer`, which needs the
kind to say so.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from dpc_protocol.protocol import create_providers_response

from dpc_client_core.dpc_agent.provider_facts import provider_facts_for
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService

HOST = "dpc-node-" + "b" * 32
GUEST = "dpc-node-" + "c" * 32
OA = "vendor_oa"


class _OpenAICompatibleVendor:
    """An `openai_compatible` alias on a vendor's endpoint, as the host loads it."""

    def __init__(self):
        self.config = {
            "type": "openai_compatible", "model": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
        }
        self.model = "gpt-4.1-mini"

    def supports_vision(self):
        return False


def _host_service(tmp_path: Path, *, tariff: bool):
    compute = {
        "enabled": True,
        "allow_nodes": [GUEST],
        "serving_vendor": [OA],
        "vendor_quotas": {OA: 1.0},
        "currency": "USD",
    }
    if tariff:
        compute["serving_tariff"] = {OA: [{"from": "2026-01-01", "in": 2.0, "out": 8.0}]}
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    service = SimpleNamespace(
        firewall=ContextFirewall(rules),
        llm_manager=SimpleNamespace(
            providers={OA: _OpenAICompatibleVendor()},
            lookup_context_window=lambda model: None,
            get_context_window=lambda model: None,
        ),
        _provider_supports_voice=lambda provider: False,
    )
    service.build_p2p_provider_info = (
        lambda alias, provider, **kwargs:
        CoreService.build_p2p_provider_info(service, alias, provider, **kwargs)
    )
    return service


class _Api:
    async def broadcast_event(self, name, payload):
        pass


def _guest_budget_after_the_wire(tmp_path: Path, *, tariff: bool) -> dict:
    host = _host_service(tmp_path, tariff=tariff)
    rows, reason = CoreService.menu_for_peer(host, GUEST)
    assert [row["alias"] for row in rows] == [OA], reason

    delivered = json.loads(json.dumps(create_providers_response(rows)))

    guest = SimpleNamespace(peer_metadata={}, _pending_providers_requests={}, local_api=_Api())
    asyncio.run(P2PCoordinator.handle_providers_response(
        SimpleNamespace(service=guest), HOST, delivered["payload"]["providers"],
    ))
    return provider_facts_for(
        SimpleNamespace(providers={}), OA, compute_host=HOST, peer_metadata=guest.peer_metadata,
    )


def test_a_tariffed_vendor_row_tells_the_guest_it_pays(tmp_path):
    facts = _guest_budget_after_the_wire(tmp_path, tariff=True)

    assert facts["route"] == "peer" and facts["served_by"] == HOST
    assert facts["provider_kind"] == "vendor"
    assert facts["tokens_paid_by"] == "this_node"


def test_an_untariffed_vendor_row_tells_the_guest_the_host_bears_the_cost(tmp_path):
    facts = _guest_budget_after_the_wire(tmp_path, tariff=False)

    assert facts["provider_kind"] == "vendor"
    assert facts["tokens_paid_by"] == "peer"
