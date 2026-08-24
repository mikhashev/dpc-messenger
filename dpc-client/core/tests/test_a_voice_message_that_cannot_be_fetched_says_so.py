"""A voice message that cannot be fetched must not end in silence.

On 2026-08-24 at 22:46:07 a voice message arrived from Telegram; at 22:46:13
`bot.get_file` gave up with `httpx.ConnectTimeout` — a TLS handshake that never
completed inside python-telegram-bot's 5-second default. Six seconds, one
failed connection, and nothing else wrong that minute: the getUpdates poll had
just delivered the message and no other request failed in the whole window.

Two defects, both covered here:

- **no headroom**: the library default was used unchanged, on a machine that
  was pinned at the time;
- **no word to the sender**: the failure went to a log line, so the person who
  recorded the voice waited for a transcription that was never coming and had
  to ask someone to read the logs.

Whisper is not involved in any of this. It was never reached.
"""

import asyncio

import pytest

from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _File:
    def __init__(self):
        self.downloaded_with = None

    async def download_to_drive(self, path, **kwargs):
        self.downloaded_with = kwargs


class _Bot:
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times=0, error=None):
        self.fail_times = fail_times
        self.calls = []
        self.error = error or TimeoutError("Timed out")
        self.file = _File()

    async def get_file(self, file_id, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return self.file


def _bridge():
    return TelegramBridge.__new__(TelegramBridge)


@pytest.mark.asyncio
async def test_the_fetch_is_given_more_than_the_library_default(tmp_path):
    bot, message = _Bot(), _Message()

    file = await _bridge()._fetch_voice_with_retry(bot, "id", tmp_path / "v.ogg", message)

    assert file is not None
    assert bot.calls[0]["connect_timeout"] >= 30, "5 s was what lost the message"
    assert bot.calls[0]["read_timeout"] >= 30
    assert file.downloaded_with["connect_timeout"] >= 30, "the download needs it too"


@pytest.mark.asyncio
async def test_a_first_failure_is_retried_rather_than_dropped(tmp_path):
    bot, message = _Bot(fail_times=1), _Message()

    file = await _bridge()._fetch_voice_with_retry(bot, "id", tmp_path / "v.ogg", message)

    assert file is not None, "one bad connection must not lose the message"
    assert len(bot.calls) == 2
    assert message.replies == [], "a recovered fetch says nothing"


@pytest.mark.asyncio
async def test_giving_up_tells_the_sender(tmp_path):
    bot, message = _Bot(fail_times=99), _Message()

    file = await _bridge()._fetch_voice_with_retry(bot, "id", tmp_path / "v.ogg", message)

    assert file is None
    assert len(bot.calls) == TelegramBridge.VOICE_FETCH_ATTEMPTS
    assert len(message.replies) == 1, "silence is what made this need a log-reading session"
    assert "голосов" in message.replies[0].lower()


@pytest.mark.asyncio
async def test_a_sender_that_cannot_be_replied_to_does_not_raise(tmp_path):
    class _Mute(_Message):
        async def reply_text(self, text, **kwargs):
            raise RuntimeError("chat gone")

    bot = _Bot(fail_times=99)

    file = await _bridge()._fetch_voice_with_retry(bot, "id", tmp_path / "v.ogg", _Mute())

    assert file is None, "the handler still returns cleanly"
