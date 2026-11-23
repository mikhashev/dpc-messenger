"""ADR-052 section 3.3, "S3 and S4 land together": what slice S3b changes for music before S4.

Since S3b a warm Whisper holds a card lease, so the music runner's old card wait
(`default_card_holders`, which reads `card_holders()`) sees it as a holder. Without more, a job would
wait behind an idle Whisper until its idle unload (300 s) or a person's "Stop X". Until S4 moves the
runner onto the lease, the old wait does what the matrix says for a music start (2.1: "waits while
busy, yields while idle"): an idle lease with a cheap yield is asked to let go and the job goes on; a
busy one is waited for and named in `card_wait`, as before. A supervisor row is untouched (no swap:
the old wait stops nothing). The reverse direction (Whisper waiting for a running job) needs the
music lease: S4.
"""

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of
from tests._yue2_helpers import DualRig, install_both, yue2_params

pytestmark = pytest.mark.usefixtures("fast")


def _rig(home):
    install_both(home)
    return DualRig(home, start_runner=False)


async def _queued_job(rig):
    job = rig.store.create_job("generate_from_text", yue2_params())
    assert job["state"] == "queued", job.get("state_reason")
    rig.runner._card_holders = base_mod.card_holders  # the default reader, `default_card_holders`
    return job["id"]


@pytest.mark.asyncio
async def test_an_idle_warm_whisper_is_asked_to_let_go_and_the_job_goes_on(tmp_path, monkeypatch):
    p = gpu_whisper(monkeypatch)
    await asyncio.wait_for(p.transcribe("warm"), BOUND)
    assert holding() == ["whisper:turbo"]
    rig = _rig(tmp_path)
    job_id = await _queued_job(rig)

    assert await asyncio.wait_for(rig.runner._wait_for_card(job_id), BOUND) is True
    assert p.unloads == 1 and not p.model_loaded and not lease_of(p).granted and holding() == []
    assert rig.store.get_job(job_id).get("card_wait") in (None, {})
    await nothing_left()


@pytest.mark.asyncio
async def test_a_busy_whisper_is_waited_for_and_named(tmp_path, monkeypatch):
    gate = asyncio.Event()
    p = gpu_whisper(monkeypatch, gate=gate)
    call = asyncio.create_task(p.transcribe("x", purpose="dictation"))
    await until(lambda: p.ran == ["x"])
    rig = _rig(tmp_path)
    job_id = await _queued_job(rig)

    waiting = asyncio.create_task(rig.runner._wait_for_card(job_id))
    await until(lambda: (rig.store.get_job(job_id).get("card_wait") or {}).get("holders"))
    holders = rig.store.get_job(job_id)["card_wait"]["holders"]
    assert [h["alias"] for h in holders] == ["whisper:turbo"] and p.unloads == 0

    gate.set()
    await asyncio.wait_for(call, BOUND)
    assert await asyncio.wait_for(waiting, BOUND) is True, "idle once the dictation ends: asked, let go"
    assert p.unloads == 1 and holding() == []
    await nothing_left()
