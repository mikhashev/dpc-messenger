"""ADR-052, Confirmation test 20, the parts of slice S3b (L4; section 3.1, queueing condition 4).

The door's `wait_s` (S3a's `card_ask`: `min(caller's remaining - margin, bound)`) is the lease line's
deadline: a transcription behind a busy holder is refused at it (`LeaseWaitedTooLong`, naming the
holder and the bound), nothing loaded. An unknown ETA waits, told as "time unknown", and is not
refused before the deadline. A person's cancel takes the waiter out of `_LEASE_LINE`. The UI's
command timeout for `transcribe_audio` is the dictation bound plus the margin, so the UI never cuts a
wait the backend still runs (`ui/src/lib/coreService.ts`).
"""

import asyncio
import re
import time
from pathlib import Path

import pytest

from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider, card_wait_words
from dpc_client_core.settings import Settings

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")

CORE_SERVICE_TS = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "coreService.ts"


async def _busy_music():
    over = asyncio.Event()
    yue2 = music()
    task = asyncio.create_task(hold_open(yue2.lease, "music job J", over))
    await until(lambda: yue2.lease.granted)
    return over, task


@pytest.mark.asyncio
async def test_the_wait_ends_at_the_doors_bound_naming_the_holder(monkeypatch):
    over, job = await _busy_music()
    p = gpu_whisper(monkeypatch)
    started = time.monotonic()
    with pytest.raises(LeaseWaitedTooLong) as refused:
        await asyncio.wait_for(p.transcribe("d", purpose="dictation", wait_s=0.2), BOUND)
    assert 0.15 <= time.monotonic() - started < BOUND
    assert "yue2.cpp" in str(refused.value) and "0.2 s" in str(refused.value)
    assert p.loads == 0 and not lease_of(p).granted
    over.set()
    await asyncio.wait_for(job, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_unknown_eta_waits_as_time_unknown_and_is_not_refused_early(monkeypatch):
    over, job = await _busy_music()
    p = gpu_whisper(monkeypatch)
    told = []
    call = asyncio.create_task(p.transcribe("d", purpose="dictation", wait_s=BOUND, on_wait=told.append))
    await until(lambda: told)
    await asyncio.sleep(0.2)
    assert not call.done() and waiting() == ["whisper:turbo"]
    assert told[0]["known"] is False and told[0]["wait_s"] is None and told[0]["where"] == "busy_holder"
    assert card_wait_words(told[0]) == "transcription queued behind yue2.cpp (music job J), time unknown"
    over.set()
    assert (await asyncio.wait_for(call, BOUND)) == {"text": "d"}
    await asyncio.wait_for(job, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_persons_cancel_takes_the_waiter_out_of_the_line(monkeypatch):
    over, job = await _busy_music()
    p = gpu_whisper(monkeypatch)
    call = asyncio.create_task(p.transcribe("d", purpose="music Transcribe", wait_s=None))
    await until(lambda: waiting() == ["whisper:turbo"])
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert waiting() == [] and p.loads == 0 and not lease_of(p).granted
    over.set()
    await asyncio.wait_for(job, BOUND)
    await nothing_left()


def test_the_words_say_the_estimate_when_it_is_known():
    assert card_wait_words({"known": True, "wait_s": 12.0, "behind": ["Whisper (turbo) (dictation)"]}) == \
        "transcription queued behind Whisper (turbo) (dictation), about 12 s"
    assert card_wait_words({"known": True, "wait_s": 360.0, "behind": ["yue2.cpp"]}).endswith("about 6 min")
    assert card_wait_words({"known": True, "wait_s": 30.0, "behind": ["acestep.cpp"], "upper_bound": True}) == (
        "transcription queued behind acestep.cpp, at most about 30 s")
    assert card_wait_words({"known": True, "wait_s": 360.0, "behind": ["a"], "upper_bound": True}).endswith(
        "at most about 6 min")
    assert card_wait_words({"known": False, "behind": ["a"], "upper_bound": True}).endswith(", time unknown")
    assert card_wait_words(None) == "transcription queued behind another job on the card, time unknown"


def test_the_ui_waits_for_the_dictation_bound_plus_the_margin(tmp_path):
    source = CORE_SERVICE_TS.read_text(encoding="utf-8")
    bound = int(re.search(r"TRANSCRIBE_QUEUE_WAIT_MS\s*=\s*([\d_]+)", source).group(1).replace("_", ""))
    margin = int(re.search(r"TRANSCRIBE_WORK_MARGIN_MS\s*=\s*([\d_]+)", source).group(1).replace("_", ""))
    assert bound == Settings(tmp_path).get_local_transcription_queue_wait_s() * 1000 == 600_000
    # The margin covers the transcription itself: at least L4's 10 s, and a 300 s dictation (the
    # recorder's cap) loaded cold on the GPU at the start figures (load + 300 s at the >180 s band's
    # warm ratio + the first call's penalty, about 32.7 s; S3 follow-up 2).
    cold_300 = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cuda"}).expected_seconds(300)
    assert margin >= max(10_000, cold_300 * 1000)
    assert re.search(r"command === 'transcribe_audio'\)\s*\{[^}]*timeout = TRANSCRIBE_QUEUE_WAIT_MS \+ "
                     r"TRANSCRIBE_WORK_MARGIN_MS", source)
