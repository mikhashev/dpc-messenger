"""A button on the agent bot's messages counts only when the owner presses it.

The vote, shell and schedule buttons act as this node: a vote is this node's vote, a
shell approval lets a command run. They checked only the chat the message sat in, and a
chat id does not say who pressed: in an allowed group any member could. With
`telegram_owner_user_id` set only that user counts, in any allowed chat; with none set,
only a private chat with the presser does. A press that does not count answers the
presser with an alert and touches nothing: the message is not edited, the service is not
called, the pending entry stays, so the owner can still press. Mike's call, 2026-11-08.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

OWNER_ID = 429727247
OTHER_ID = 41783586
GROUP_CHAT = -1001234567890
ALERT = "Only the owner can answer this."

# handler name, callback prefix, the service method it calls, the pending map
HANDLERS = [
    ("_handle_vote_callback", "vote", "vote_knowledge_commit", None),
    ("_handle_shell_callback", "shell", "shell_approve_command", "_pending_shell"),
    ("_handle_schedule_callback", "schedule", "resolve_schedule_approval", "_pending_schedule"),
]


def _bridge(owner_user_id=None, chats=(str(OWNER_ID), str(OTHER_ID), str(GROUP_CHAT))):
    kwargs = {} if owner_user_id is None else {"owner_user_id": owner_user_id}
    bridge = AgentTelegramBridge(bot_token="123:abc", allowed_chat_ids=list(chats), **kwargs)
    service = SimpleNamespace(
        vote_knowledge_commit=AsyncMock(return_value={"status": "success"}),
        shell_approve_command=AsyncMock(return_value={"status": "ok"}),
        shell_reject_command=AsyncMock(return_value={"status": "ok"}),
        resolve_schedule_approval=AsyncMock(return_value={"status": "success"}),
    )
    bridge._agent_manager = SimpleNamespace(service=service)
    return bridge, service


def _query(prefix, user, chat_id, chat_type, *, with_user=True):
    q = SimpleNamespace(
        data=f"{prefix}:req-1:approve",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type)),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    if with_user:
        q.from_user = SimpleNamespace(id=user)
    return q


def _pending(bridge, pending):
    if pending:
        getattr(bridge, pending)["req-1"] = [(str(OWNER_ID), 1)]


def _counted(q, service, method):
    getattr(service, method).assert_awaited_once()
    q.edit_message_text.assert_awaited_once()
    q.answer.assert_awaited_once_with()  # the plain acknowledgement


def _refused(q, service, bridge, pending):
    for name in ("vote_knowledge_commit", "shell_approve_command", "shell_reject_command",
                 "resolve_schedule_approval"):
        getattr(service, name).assert_not_awaited()
    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with(ALERT, show_alert=True)
    if pending:
        assert "req-1" in getattr(bridge, pending)


async def _press(bridge, handler, q):
    await getattr(bridge, handler)(SimpleNamespace(callback_query=q), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
@pytest.mark.parametrize("chat_id,chat_type", [
    (OWNER_ID, "private"), (GROUP_CHAT, "group"), (GROUP_CHAT, "supergroup")],
    ids=["private", "group", "supergroup"])
async def test_with_an_owner_set_the_owner_counts_in_any_allowed_chat(
        handler, prefix, method, pending, chat_id, chat_type):
    bridge, service = _bridge(owner_user_id=str(OWNER_ID))
    _pending(bridge, pending)
    q = _query(prefix, OWNER_ID, chat_id, chat_type)

    await _press(bridge, handler, q)

    _counted(q, service, method)
    if pending:
        assert "req-1" not in getattr(bridge, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
@pytest.mark.parametrize("chat_id,chat_type", [
    (GROUP_CHAT, "group"), (OTHER_ID, "private")], ids=["group", "private"])
async def test_with_an_owner_set_anybody_else_is_refused_and_nothing_is_consumed(
        handler, prefix, method, pending, chat_id, chat_type):
    bridge, service = _bridge(owner_user_id=str(OWNER_ID))
    _pending(bridge, pending)
    q = _query(prefix, OTHER_ID, chat_id, chat_type)

    await _press(bridge, handler, q)

    _refused(q, service, bridge, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
async def test_with_no_owner_set_the_presser_s_own_private_chat_counts(
        handler, prefix, method, pending):
    bridge, service = _bridge()
    _pending(bridge, pending)
    q = _query(prefix, OTHER_ID, OTHER_ID, "private")

    await _press(bridge, handler, q)

    _counted(q, service, method)
    if pending:
        assert "req-1" not in getattr(bridge, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
@pytest.mark.parametrize("user,chat_id,chat_type", [
    (OWNER_ID, GROUP_CHAT, "group"),            # a group, owner or not
    (OTHER_ID, GROUP_CHAT, "supergroup"),
    (OWNER_ID, OTHER_ID, "private"),            # a private chat that is not the presser's
    (OWNER_ID, OWNER_ID, "group"),              # chat id equal, type not private
    (OWNER_ID, OWNER_ID, None),                 # a chat of no stated type
], ids=["group", "supergroup", "private-of-another", "id-equal-not-private", "untyped"])
async def test_with_no_owner_set_anything_but_the_presser_s_private_chat_is_refused(
        handler, prefix, method, pending, user, chat_id, chat_type):
    bridge, service = _bridge()
    _pending(bridge, pending)
    q = _query(prefix, user, chat_id, chat_type)

    await _press(bridge, handler, q)

    _refused(q, service, bridge, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
@pytest.mark.parametrize("owner", [None, str(OWNER_ID)], ids=["no-owner", "owner"])
@pytest.mark.parametrize("who", [True, "429727247", None, "missing"],
                         ids=["bool", "string", "none", "no-from_user"])
async def test_a_presser_with_no_integer_user_id_is_refused(handler, prefix, method, pending,
                                                            owner, who):
    bridge, service = _bridge(owner_user_id=owner)
    _pending(bridge, pending)
    chat = OWNER_ID if owner is None else GROUP_CHAT
    q = _query(prefix, who, chat, "private" if owner is None else "group",
               with_user=who != "missing")

    await _press(bridge, handler, q)

    _refused(q, service, bridge, pending)


@pytest.mark.asyncio
async def test_a_bool_user_id_equal_to_the_owner_id_is_refused():
    """True == 1: an owner id of 1 must not be pressed by `True`."""
    bridge, service = _bridge(owner_user_id="1", chats=("1",))
    q = _query("shell", True, 1, "private")

    await _press(bridge, "_handle_shell_callback", q)

    _refused(q, service, bridge, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
async def test_a_chat_that_is_not_allowed_still_gets_the_unauthorized_edit(
        handler, prefix, method, pending):
    bridge, service = _bridge(owner_user_id=str(OWNER_ID), chats=(str(OWNER_ID),))
    _pending(bridge, pending)
    q = _query(prefix, OWNER_ID, GROUP_CHAT, "group")

    await _press(bridge, handler, q)

    q.edit_message_text.assert_awaited_once_with("⛔ Unauthorized.")
    q.answer.assert_awaited_once_with()
    getattr(service, method).assert_not_awaited()
    if pending:
        assert "req-1" in getattr(bridge, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,prefix,method,pending", HANDLERS)
async def test_a_refused_press_logs_one_info_line_with_ids_and_no_text(
        handler, prefix, method, pending, caplog):
    bridge, _ = _bridge(owner_user_id=str(OWNER_ID))
    q = _query(prefix, OTHER_ID, GROUP_CHAT, "group")

    with caplog.at_level(logging.INFO):
        await _press(bridge, handler, q)

    lines = [r for r in caplog.records if "Refused" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    text = lines[0].getMessage()
    assert prefix in text and str(OTHER_ID) in text and str(GROUP_CHAT) in text
    assert "req-1" not in text


@pytest.mark.asyncio
async def test_a_refused_press_whose_alert_cannot_be_sent_still_touches_nothing():
    bridge, service = _bridge(owner_user_id=str(OWNER_ID))
    bridge._pending_shell["req-1"] = [(str(GROUP_CHAT), 1)]
    q = _query("shell", OTHER_ID, GROUP_CHAT, "group")
    q.answer = AsyncMock(side_effect=RuntimeError("Query is too old"))

    await _press(bridge, "_handle_shell_callback", q)

    q.edit_message_text.assert_not_awaited()
    service.shell_approve_command.assert_not_awaited()
    assert "req-1" in bridge._pending_shell


@pytest.mark.asyncio
async def test_after_a_refusal_the_owner_can_press_the_same_button():
    bridge, service = _bridge(owner_user_id=str(OWNER_ID))
    bridge._pending_shell["req-1"] = [(str(GROUP_CHAT), 1)]

    await _press(bridge, "_handle_shell_callback",
                 _query("shell", OTHER_ID, GROUP_CHAT, "group"))
    owner_press = _query("shell", OWNER_ID, GROUP_CHAT, "group")
    await _press(bridge, "_handle_shell_callback", owner_press)

    service.shell_approve_command.assert_awaited_once_with("req-1")
    assert "req-1" not in bridge._pending_shell
