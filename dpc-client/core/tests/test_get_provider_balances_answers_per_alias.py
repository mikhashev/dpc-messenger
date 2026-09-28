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
    RETRY_LABEL = "TestVendor"

    def __init__(self, *, balance=None, error=None, capable=True,
                 config=None, calls=None):
        self._balance = balance
        self._error = error
        self._capable = capable
        self.config = config or {}
        self._api_key = self.config.get("api_key")
        self._base_url = self.config.get("base_url")
        self._calls = calls if calls is not None else []

    def supports_balance(self):
        return self._capable

    async def get_balance(self):
        self._calls.append(1)
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
    assert result == {"status": "success", "balances": {}, "accounts": []}


@pytest.mark.asyncio
async def test_two_aliases_on_the_same_key_share_one_account_and_one_call():
    """Same provider type, base_url and api_key: one wallet, queried once —
    e.g. two DeepSeek aliases (chat + agent) configured against one key."""
    calls: list = []
    cfg = {"type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "sk-shared"}
    providers = {
        "ds_chat": _Provider(balance={"total_balance": "12.34"}, config=cfg, calls=calls),
        "ds_agent": _Provider(balance={"total_balance": "12.34"}, config=cfg, calls=calls),
    }
    result = await CoreService.get_provider_balances(_self_with(providers))

    assert len(calls) == 1  # one wallet, one read
    assert len(result["accounts"]) == 1
    account = result["accounts"][0]
    assert set(account["aliases"]) == {"ds_chat", "ds_agent"}
    assert account["provider_type"] == "deepseek"
    assert account["label"] == "TestVendor"
    assert account["result"]["status"] == "success"
    # Both aliases still get their own `balances` entry (backward compat).
    assert result["balances"]["ds_chat"]["alias"] == "ds_chat"
    assert result["balances"]["ds_agent"]["alias"] == "ds_agent"
    assert result["balances"]["ds_chat"]["balance"] == result["balances"]["ds_agent"]["balance"]


@pytest.mark.asyncio
async def test_different_keys_never_share_an_account_even_with_the_same_type():
    cfg_a = {"type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "sk-aaa"}
    cfg_b = {"type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "sk-bbb"}
    calls: list = []
    providers = {
        "ds_a": _Provider(balance={"total_balance": "1.00"}, config=cfg_a, calls=calls),
        "ds_b": _Provider(balance={"total_balance": "2.00"}, config=cfg_b, calls=calls),
    }
    result = await CoreService.get_provider_balances(_self_with(providers))
    assert len(calls) == 2
    assert len(result["accounts"]) == 2
    assert result["balances"]["ds_a"]["balance"]["total_balance"] == "1.00"
    assert result["balances"]["ds_b"]["balance"]["total_balance"] == "2.00"


@pytest.mark.asyncio
async def test_account_id_never_exposes_the_api_key():
    cfg = {"type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "sk-super-secret"}
    result = await CoreService.get_provider_balances(_self_with({
        "ds": _Provider(balance={"total_balance": "1.00"}, config=cfg),
    }))
    assert "sk-super-secret" not in result["accounts"][0]["account"]


@pytest.mark.asyncio
async def test_accounts_are_queried_concurrently():
    """Separate accounts run through asyncio.gather, not one-at-a-time."""
    import asyncio

    order: list = []

    class _SlowProvider(_Provider):
        async def get_balance(self):
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            return self._balance

    providers = {
        "a": _SlowProvider(balance={"x": 1}, config={"type": "deepseek", "api_key": "k1"}),
        "b": _SlowProvider(balance={"x": 2}, config={"type": "deepseek", "api_key": "k2"}),
    }
    await CoreService.get_provider_balances(_self_with(providers))
    # If sequential, order would be [start, end, start, end].
    assert order == ["start", "start", "end", "end"]
