"""ADR-052, Confirmation test 24 (slice S3b; section 3.1, queueing condition 1, Ark #53): the lease
use is taken before `_load_lock`, so a dictation waiting in the lease line holds no Whisper lock. A
peer's request that cannot be shown to fit meanwhile is refused at once (well under 0.5 s), never
after waiting on the lock; the player's Transcribe takes its own place in the line, not a place on
the lock.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")


@pytest.mark.asyncio
async def test_a_dictation_waiting_behind_music_holds_no_lock_and_a_peer_is_refused_at_once(monkeypatch):
    job_over = asyncio.Event()
    yue2 = music()  # a running job, its ETA unknown (L10)
    job = asyncio.create_task(hold_open(yue2.lease, "music job J", job_over))
    await until(lambda: yue2.lease.granted)

    p = gpu_whisper(monkeypatch)
    dictation = asyncio.create_task(p.transcribe("d", purpose="dictation", wait_s=BOUND, expected_s=2.0))
    await until(lambda: waiting() == ["whisper:turbo"])
    assert not p._load_lock.locked() and p.loads == 0

    started = time.monotonic()
    with serving_a_peer("node-guest"):
        with pytest.raises(LeaseRefusedForPeer) as refused:
            await asyncio.wait_for(p.transcribe("peer", purpose="peer node-guest", expected_s=2.0), BOUND)
    assert time.monotonic() - started < 0.5
    assert "right now" in str(refused.value) and "yue2" not in str(refused.value)
    assert p.loads == 0 and not p._load_lock.locked()

    transcribe_click = asyncio.create_task(p.transcribe("t", purpose="music Transcribe", wait_s=None))
    await until(lambda: waiting() == ["whisper:turbo", "whisper:turbo"])
    assert not p._load_lock.locked(), "two places in the line, none on the lock"

    job_over.set()
    await asyncio.wait_for(job, BOUND)
    assert (await asyncio.wait_for(dictation, BOUND)) == {"text": "d"}
    assert (await asyncio.wait_for(transcribe_click, BOUND)) == {"text": "t"}
    assert p.loads == 1 and lease_of(p).granted
    await nothing_left()
