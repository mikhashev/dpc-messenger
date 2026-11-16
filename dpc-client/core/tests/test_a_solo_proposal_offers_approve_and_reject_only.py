"""A solo proposal is answered with approve or reject; Request Changes is not offered.

In a solo chat nobody but an agent with a comment could revise, and a Telegram button
carries no comment, so request_changes ended in revision_needed for good. Mike's call,
2026-11-16: solo chats offer approve, reject and (in the app) Edit.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

DIALOG = (
    Path(__file__).resolve().parents[2]
    / "ui" / "src" / "lib" / "components" / "KnowledgeCommitDialog.svelte"
)


def test_the_telegram_proposal_keyboard_has_no_request_changes():
    bridge = AgentTelegramBridge.__new__(AgentTelegramBridge)
    proposal = SimpleNamespace(topic="t", summary="s", avg_confidence=0.9, entries=[])
    _, keyboard = bridge._build_proposal_message("p1", proposal)
    votes = [b.callback_data.rsplit(":", 1)[1] for row in keyboard.inline_keyboard for b in row]
    assert votes == ["approve", "reject"]


def test_no_telegram_keyboard_offers_request_changes():
    # The fallback keyboards are built inline in two more places.
    source = inspect.getsource(AgentTelegramBridge)
    assert ':request_changes"' not in source


def test_the_dialog_hides_request_changes_on_a_solo_proposal():
    text = DIALOG.read_text(encoding="utf-8")
    button = text.index("handleVote('request_changes')")
    guard = text.rfind("{#if !canEdit}", 0, button)
    assert guard != -1 and "{/if}" not in text[guard:button]
