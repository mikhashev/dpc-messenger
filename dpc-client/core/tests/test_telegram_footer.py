"""The context-window/balance footer appended to an agent reply on Telegram
(card TELEGRAM-AGENT-REPLIES-CARRY-NO-BALANCE-OR-CONTEXT-STATS).

Pure-function coverage of dpc_client_core.telegram_footer, then the wiring
in AgentTelegramBridge._reply_markdown: sent after the reply, omitted on a
failed/slow balance lookup or a local (non-balance) provider, and — when the
reply itself was split into several Telegram messages — sent only once,
after the last part.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.telegram_footer import (
    build_footer,
    build_stats_footer,
    format_balance_line,
    select_balance_for_alias,
)

CHAT = "424242"

SESSION_STATE = {
    "history_tokens": 4818,
    "tokens_after_last_response": 66209,
    "tokens_limit": 1_000_000,
    "messages_count": 6,
    "context_breakdown": None,
}


# --- pure functions ----------------------------------------------------------


def test_stats_footer_matches_the_uis_four_rows():
    text = build_stats_footer(SESSION_STATE)
    lines = text.splitlines()
    assert lines[0].startswith("DIALOG")
    assert "4,818" in lines[0] and "938,609" in lines[0] and "(1%)" in lines[0]
    assert lines[1].startswith("TOTAL")
    assert "66,209" in lines[1] and "1,000,000" in lines[1] and "(7%)" in lines[1]
    assert lines[2].startswith("NON-DIALOG") and "≈61,391" in lines[2]
    assert lines[3] == "MESSAGES    6"


def test_non_dialog_tokens_is_floored_when_the_estimate_overshoots():
    # tokens_after_last_response (measured) smaller than history_tokens (chars/4
    # estimate) — the clamp in nonDialogTokens.ts must floor this at 0, not go
    # negative.
    from dpc_client_core.telegram_footer import non_dialog_tokens
    assert non_dialog_tokens(measured_total=100, estimated_dialogue=500) == 0


def test_stats_footer_is_none_before_any_measured_total():
    assert build_stats_footer({"history_tokens": 10, "tokens_after_last_response": 0, "tokens_limit": 100}) is None


def test_deepseek_wallet_balance_line():
    balance = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "10.42"}]}
    assert format_balance_line("DeepSeek", balance) == "DeepSeek USD 10.42"


def test_neuraldeep_subscription_quota_line():
    balance = {
        "is_available": True,
        "quota": {
            "billing_mode": "subscription",
            "tier": "free",
            "windows": [{"name": "3h", "used": 4, "limit": 100}],
            "daily_capacity": {"pct_used": 0.1},
        },
    }
    assert format_balance_line("NeuralDeep", balance) == "NeuralDeep free · 3h 4% · day 0.1%"


def test_local_provider_without_supports_balance_has_no_balance_line():
    # get_provider_balances() never lists a provider whose supports_balance()
    # is False, so it is simply absent from `accounts` — select_balance_for_alias
    # then returns None and the caller adds no line.
    payload = {"balances": {}, "accounts": []}
    assert select_balance_for_alias(payload, "local-ollama") is None


def test_full_footer_appends_the_balance_line():
    balance = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "10.42"}]}
    footer = build_footer(SESSION_STATE, balance_label="DeepSeek", balance=balance)
    assert footer.splitlines()[-1] == "DeepSeek USD 10.42"
    assert footer.splitlines()[0].startswith("DIALOG")


def test_full_footer_without_a_balance_is_just_the_stats():
    footer = build_footer(SESSION_STATE)
    assert footer == build_stats_footer(SESSION_STATE)


# --- wired into the bridge ----------------------------------------------------


def _update(text="hi"):
    u = MagicMock()
    u.effective_chat.id = CHAT
    u.effective_user.first_name = "Mike"
    u.message.text = text
    u.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=1))
    return u


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


def _bridge_with_agent_manager(get_balances=None):
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT])
    bridge._enabled = True
    bridge._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = SimpleNamespace(get_provider_balances=get_balances) if get_balances else SimpleNamespace()
    agent_manager = SimpleNamespace(
        get_session_state=lambda conv_id: dict(SESSION_STATE),
        config={"provider_alias": "ds1"},
        service=service,
    )
    bridge._agent_manager = agent_manager
    return bridge


def _texts(update):
    return [c.args[0] if c.args else c.kwargs.get("text") for c in update.message.reply_text.await_args_list]


@pytest.mark.asyncio
async def test_reply_carries_the_footer_after_the_text():
    async def balances():
        return {"accounts": [{"aliases": ["ds1"], "label": "DeepSeek",
                               "result": {"status": "success",
                                          "balance": {"is_available": True,
                                                      "balance_infos": [{"currency": "USD", "total_balance": "10.42"}]}}}]}

    bridge = _bridge_with_agent_manager(get_balances=balances)
    bridge._message_handler = AsyncMock(return_value="Done.")
    update = _update()
    await bridge._handle_message(update, _ctx())

    texts = _texts(update)
    assert texts[0] == "Done."
    assert "DIALOG" in texts[-1] and "DeepSeek USD 10.42" in texts[-1]


@pytest.mark.asyncio
async def test_a_failed_balance_lookup_still_sends_the_reply_and_the_stats():
    async def balances():
        raise TimeoutError("slow vendor")

    bridge = _bridge_with_agent_manager(get_balances=balances)
    bridge._message_handler = AsyncMock(return_value="Done.")
    update = _update()
    await bridge._handle_message(update, _ctx())

    texts = _texts(update)
    assert texts[0] == "Done."
    assert "DIALOG" in texts[-1]
    assert "USD" not in texts[-1] and "DeepSeek" not in texts[-1]


@pytest.mark.asyncio
async def test_footer_disabled_sends_only_the_reply():
    bridge = _bridge_with_agent_manager()
    bridge.footer_enabled = False
    bridge._message_handler = AsyncMock(return_value="Done.")
    update = _update()
    await bridge._handle_message(update, _ctx())

    texts = _texts(update)
    assert texts == ["Done."]


@pytest.mark.asyncio
async def test_a_split_reply_carries_the_footer_only_on_the_last_message():
    bridge = _bridge_with_agent_manager()
    long_reply = "x" * 9000  # forces split_telegram_html into more than one part
    bridge._message_handler = AsyncMock(return_value=long_reply)
    update = _update()
    await bridge._handle_message(update, _ctx())

    texts = _texts(update)
    assert len(texts) >= 3  # at least two reply parts plus the footer
    assert "DIALOG" not in texts[0]
    assert "DIALOG" not in texts[-2]
    assert "DIALOG" in texts[-1]
