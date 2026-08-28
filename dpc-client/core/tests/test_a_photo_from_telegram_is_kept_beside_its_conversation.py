"""A photo sent to an agent over Telegram must leave a record, not only pixels.

The image travels to the model as base64 and nothing persists that, so before
this the conversation kept the caption alone: a question about a screenshot
with no screenshot. The conversation-level bridge already solved it — file
under `<conversation>/files`, an attachment dict on the message — and this is
that shape, ported.
"""

from pathlib import Path

import pytest

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge


@pytest.fixture
def bridge():
    return AgentTelegramBridge(bot_token="123456:TESTTOKEN", allowed_chat_ids=["429727247"])


def test_the_photo_lands_where_the_other_bridge_puts_its_own(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    attachment = bridge._keep_incoming_photo("agent_001", b"\xff\xd8\xff\xe0jpegbytes", 4242)

    expected = tmp_path / ".dpc" / "conversations" / "agent_001" / "files" / "telegram_photo_4242.jpg"
    assert expected.exists()
    assert expected.read_bytes() == b"\xff\xd8\xff\xe0jpegbytes"
    assert attachment["file_path"] == str(expected)


def test_the_attachment_carries_what_the_history_and_the_interface_read(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    attachment = bridge._keep_incoming_photo("agent_001", b"0123456789", 7)

    assert attachment["type"] == "image"
    assert attachment["filename"] == "telegram_photo_7.jpg"
    assert attachment["size_bytes"] == 10
    assert attachment["mime_type"] == "image/jpeg"
    assert attachment["source"] == "telegram"
    assert attachment["telegram_message_id"] == 7


def test_two_photos_from_one_chat_do_not_overwrite_each_other(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    first = bridge._keep_incoming_photo("agent_001", b"first", 1)
    second = bridge._keep_incoming_photo("agent_001", b"second", 2)

    assert first["file_path"] != second["file_path"]
    assert Path(first["file_path"]).read_bytes() == b"first"
    assert Path(second["file_path"]).read_bytes() == b"second"


def test_a_write_that_fails_costs_a_log_line_and_not_the_message(bridge, tmp_path, monkeypatch):
    """The model sees the image either way; a full disk must not drop the turn."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _refuse(self, data):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_bytes", _refuse)

    assert bridge._keep_incoming_photo("agent_001", b"bytes", 9) is None
