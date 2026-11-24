"""ADR-052, Confirmation test 4: **the Done-when** of the Whisper card ("a transcription during a
running YuE2 job waits and names the holder"), slices S1 + S3 + S4 together (section 3.3).

A YuE2 job runs on the real supervisor (`_fake_yue2_server.py`, its synth held until cancelled), so its
lease `music:yue2` is granted and busy. A dictation on the local Whisper provider, with the card's
reading faked to "does not fit" (L11), stands in the lease line before `_load_lock`: the log line and
`card_queue` name `yue2.cpp` busy with the music job, the door's `on_wait` is told, nothing loads. When
the job ends the engine is idle, the dictation asks it to let go (`stop("swap")`), is granted, loads and
transcribes. At its deadline the refusal names the engine and the job. With the reading faked to
"fits", L11's one shared cell is live: the dictation is granted beside the busy job at once.

The reverse: a music job asks an idle warm Whisper to let go and runs; it waits for a busy one, queued,
its `card_wait` naming `whisper:turbo` ("waiting for the card: whisper:turbo holds it").
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong

from tests._acestep_helpers import wait_until
from tests._card_lease_helpers import BOUND, fast, holding, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params

pytestmark = pytest.mark.usefixtures("fast")

DONE = ("review", "failed", "cancelled")
WHISPER = "Whisper (whisper-large-v3-turbo)"


def reading(monkeypatch, free, total=32623):
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (free, total))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "dpc"
    install_both(h)
    return h


async def a_running_job(rig):
    job = rig.propose(yue2_params(variants=1))
    await wait_until(lambda: rig.store.get_job(job["id"])["state"] == "running" and rig.yue2.state == "running"
                     and rig.yue2.busy, timeout=30)
    assert holding() == ["music:yue2"] and rig.yue2.card_lease.who_uses() == f"music job {job['id']}"
    return job["id"]


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_a_dictation_waits_behind_the_running_job_names_it_and_goes_when_it_ends(home, monkeypatch, caplog):
    yue2_env(monkeypatch, "slow_synth")
    reading(monkeypatch, free=1000)
    p = gpu_whisper(monkeypatch)
    told = []
    async with DualRig(home) as rig:
        job_id = await a_running_job(rig)
        with caplog.at_level(logging.INFO):
            call = asyncio.create_task(p.transcribe("long", purpose="dictation", wait_s=20, on_wait=told.append))
            await until(lambda: waiting() == ["whisper:turbo"])
            await asyncio.sleep(0.1)
            assert p.loads == 0 and holding() == ["music:yue2"]
            said = messages(caplog)
            assert any(m.startswith(f"card: L11 says does_not_fit for {WHISPER} beside yue2.cpp") for m in said)
            assert any(m.startswith(f"card: {WHISPER} waits for yue2.cpp (busy: music job {job_id}); wait unknown")
                       for m in said)
            row = next(r for r in base_mod.card_queue() if r.get("alias") == "whisper:turbo")
            assert row["wait"]["behind"] == [f"yue2.cpp (music job {job_id})"] and row["wait"]["wait_s"] is None
            assert told and told[-1]["behind"] == [f"yue2.cpp (music job {job_id})"]

            rig.store.cancel_job(job_id)
            rig.runner.wake()
            assert await asyncio.wait_for(call, 25) == {"text": "long"}
        assert p.loads == 1
        assert not rig.yue2.alive() and rig.yue2._last_exit["why"] == "swap", "the idle engine was asked to let go"
        assert holding() == ["whisper:turbo"]
        assert any(m == f"card: asked yue2.cpp to let go for whisper:turbo" for m in messages(caplog))


@pytest.mark.asyncio
async def test_at_its_deadline_the_refusal_names_the_engine_and_the_job(home, monkeypatch):
    yue2_env(monkeypatch, "slow_synth")
    reading(monkeypatch, free=1000)
    p = gpu_whisper(monkeypatch)
    async with DualRig(home) as rig:
        job_id = await a_running_job(rig)
        with pytest.raises(LeaseWaitedTooLong) as err:
            await asyncio.wait_for(p.transcribe("x", purpose="dictation", wait_s=0.3), BOUND)
        assert "yue2.cpp" in str(err.value) and f"busy: music job {job_id}" in str(err.value)
        assert err.value.holder_alias == "music:yue2"
        assert p.loads == 0 and waiting() == []
        rig.store.cancel_job(job_id)
        rig.runner.wake()


@pytest.mark.asyncio
async def test_when_it_fits_the_dictation_is_granted_beside_the_busy_job(home, monkeypatch, caplog):
    yue2_env(monkeypatch, "slow_synth")
    reading(monkeypatch, free=30000)
    p = gpu_whisper(monkeypatch)
    async with DualRig(home) as rig:
        job_id = await a_running_job(rig)
        with caplog.at_level(logging.INFO):
            assert await asyncio.wait_for(p.transcribe("d", purpose="dictation", wait_s=20), BOUND) == {"text": "d"}
        rows = {r["alias"]: r for r in base_mod.card_holders()}
        assert set(rows) == {"music:yue2", "whisper:turbo"}
        assert rows["whisper:turbo"]["beside"] == "music:yue2" and rows["music:yue2"]["busy"] is True
        assert rows["music:yue2"]["vram_mib"] == 5600 and rows["music:yue2"]["eta_s"] is None
        # need = 4447 (turbo, unknown length: the worst band) + 5600 (yue2) + 2048 (no engine on the card)
        assert any(m.startswith(f"card: {WHISPER} loaded beside yue2.cpp (fits: 12095 of 32623 MiB declared, "
                                "30000 MiB free") for m in messages(caplog))
        assert rig.yue2.alive() and rig.store.get_job(job_id)["state"] == "running", "the job is untouched"
        rig.store.cancel_job(job_id)
        rig.runner.wake()


@pytest.mark.asyncio
async def test_a_music_job_asks_an_idle_warm_whisper_to_let_go_and_runs(home, monkeypatch):
    yue2_env(monkeypatch)
    p = gpu_whisper(monkeypatch)
    await asyncio.wait_for(p.transcribe("warm"), BOUND)
    assert holding() == ["whisper:turbo"]
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params(variants=1))
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == "review", done["state_reason"]
        assert p.unloads == 1 and not p.model_loaded and not lease_of(p).granted
        assert holding() == ["music:yue2"]


@pytest.mark.asyncio
async def test_a_music_job_waits_queued_for_a_busy_whisper_and_names_it(home, monkeypatch):
    yue2_env(monkeypatch)
    gate = asyncio.Event()
    p = gpu_whisper(monkeypatch, gate=gate)
    call = asyncio.create_task(p.transcribe("x", purpose="dictation"))
    await until(lambda: p.ran == ["x"])
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params(variants=1))
        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)
        waiting_job = rig.store.get_job(job["id"])
        assert waiting_job["state"] == "queued"
        assert waiting_job["card_wait"]["holders"] == [{"alias": "whisper:turbo", "engine": "whisper"}]
        assert waiting_job["state_reason"] == "waiting for the card: whisper:turbo holds it"
        assert not rig.yue2.alive() and p.unloads == 0

        gate.set()
        await asyncio.wait_for(call, BOUND)
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == "review", done["state_reason"]
        assert done["card_wait"] is None and p.unloads == 1
