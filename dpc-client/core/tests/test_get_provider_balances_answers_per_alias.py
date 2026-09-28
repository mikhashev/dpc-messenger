"""CoreService.get_provider_balances — every balance-capable provider's
get_balance(), by alias, with one failing alias isolated to its own error
entry (A-VENDOR-KEYS-QUOTA-WINDOWS-ARE-READ-AND-NEVER-SHOWN).

Called as an unbound method against a minimal stand-in for `self`: the method
only reads `self.llm_manager.providers`, so a full CoreService (P2P, hub
client, sockets) is not needed to exercise it.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.service import CoreService


class _Provider:
    def __init__(self, *, balance=None, error=None, capable=True):
        self._balance = balance
        self._error = error
        self._capable = capable

    def supports_balance(self):
        return self._capable

    async def get_balance(self):
        if self._error:
            raise self._error
        return self._balance


def _self_with(providers):
    return SimpleNamespace(llm_manager=SimpleNamespace(providers=providers))


@pytest.mark.asyncio
async def test_each_capable_alias_gets_its_own_balance_entry():
    providers = {
        "nd_free": _Provider(balance={"quota": {"tier": "free"}}),
        "nd_paid": _Provider(balance={"quota": {"tier": "paid"}}),
        "local_ollama": _Provider(capable=False),  # supports_balance() False: skipped
    }
    result = await CoreService.get_provider_balances(_self_with(providers))

    assert result["status"] == "success"
    assert set(result["balances"].keys()) == {"nd_free", "nd_paid"}
    assert result["balances"]["nd_free"] == {
        "status": "success", "alias": "nd_free", "balance": {"quota": {"tier": "free"}},
    }
    assert result["balances"]["nd_paid"]["balance"]["quota"]["tier"] == "paid"


@pytest.mark.asyncio
async def test_one_alias_failing_does_not_fail_the_others():
    providers = {
        "nd_ok": _Provider(balance={"quota": {"tier": "free"}}),
        "nd_down": _Provider(error=RuntimeError("gateway unreachable")),
    }
    result = await CoreService.get_provider_balances(_self_with(providers))

    assert result["status"] == "success"
    assert result["balances"]["nd_ok"]["status"] == "success"
    assert result["balances"]["nd_down"] == {
        "status": "error", "alias": "nd_down", "message": "gateway unreachable",
    }


@pytest.mark.asyncio
async def test_no_capable_provider_returns_an_empty_map_not_an_error():
    result = await CoreService.get_provider_balances(_self_with({"only_local": _Provider(capable=False)}))
    assert result == {"status": "success", "balances": {}}
