"""ADR-052, Confirmation test 34 (Ark #64 §5; L10 "Music"; slice S4 records YuE2's factor by length).

A music job that has not started yet is counted by the waiters behind it with its text estimate:
`estimate_yue2_seconds` per variant (audio seconds, never more than the job's duration cap), times
YuE2's factor for that length, times the variants, marked as an estimate. The factor is wall seconds
per audio second (L10's orientation), from the trial card: 3.5 for a 30 s song, 3.0 for a 4-minute one,
straight between, and the end value beyond either end. acestep.cpp has no measured factor: unknown. A
running job has only per-variant progress, so its ETA is unknown (long for L12), and an instrumental
never reads as "almost done".
"""

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.music import job_schemas as js

from tests._acestep_helpers import wait_until
from tests._card_lease_helpers import BOUND, fast, hold_open, until, whisper  # noqa: F401
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params

pytestmark = pytest.mark.usefixtures("fast")


def test_eighty_audio_seconds_at_factor_three_and_a_half_read_280_s():
    assert js.yue2_wall_s(80, factor=3.5) == 280


@pytest.mark.parametrize("audio_s, factor", [(10, 3.5), (30, 3.5), (135, 3.25), (240, 3.0), (360, 3.0)])
def test_the_factor_follows_the_songs_length(audio_s, factor):
    assert js.yue2_factor(audio_s) == pytest.approx(factor)


def test_a_queued_jobs_estimate_is_the_text_estimate_times_the_factor_times_the_variants():
    lyrics = "[verse]\n" + "\n".join(f"line {i}" for i in range(6)) + "\n"
    audio = js.estimate_yue2_seconds(lyrics)
    params = {"engine": "yue2", "lyrics": lyrics, "duration_s": 360, "variants": 2}
    assert js.music_expected_s(params) == pytest.approx(audio * js.yue2_factor(audio) * 2)
    # The cap bounds the song: a 30 s cap never reads as a longer one.
    capped = {**params, "duration_s": 30}
    assert js.music_expected_s(capped) == pytest.approx(30 * 3.5 * 2)
    # An instrumental is the intro alone, as the text estimate says.
    assert js.music_expected_s({**params, "lyrics": None, "variants": 1}) == pytest.approx(
        js.YUE2_INTRO_S * js.yue2_factor(js.YUE2_INTRO_S))
    assert js.music_expected_s({"engine": "acestep", "lyrics": lyrics, "duration_s": 30, "variants": 2}) is None


@pytest.mark.asyncio
async def test_a_waiting_job_is_counted_by_the_waiter_behind_it_and_a_running_one_is_unknown(tmp_path, monkeypatch):
    yue2_env(monkeypatch, "slow_synth")
    home = tmp_path / "dpc"
    install_both(home)
    first = whisper(eta=10.0)
    over = asyncio.Event()
    holding_it = asyncio.create_task(hold_open(first.lease, "dictation", over))
    await until(lambda: first.lease.granted)
    params = yue2_params(variants=2)
    expected = js.music_expected_s({**params, "engine": "yue2"})
    assert expected is not None
    async with DualRig(home) as rig:
        job = rig.propose(params)
        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)
        later = whisper("other", eta=None)
        behind = asyncio.create_task(later.lease.acquire("dictation", wait_s=20))
        await until(lambda: any(r.get("alias") == "whisper:other" for r in base_mod.card_queue()))
        row = next(r for r in base_mod.card_queue() if r.get("alias") == "whisper:other")
        # The busy holder's 10 s, then the waiting job's own time: known, an estimate.
        assert row["wait"]["known"] is True
        assert row["wait"]["wait_s"] == pytest.approx(10.0 + expected)
        assert row["wait"]["behind"][-1] == f"yue2.cpp (music job {job['id']})"

        over.set()
        await asyncio.wait_for(holding_it, BOUND)
        await wait_until(lambda: rig.store.get_job(job["id"])["state"] == "running" and rig.yue2.busy, timeout=30)
        music_row = next(r for r in base_mod.card_holders() if r["alias"] == "music:yue2")
        assert music_row["busy"] is True and music_row["eta_s"] is None, "running: unknown, never 0"
        rig.store.cancel_job(job["id"])
        rig.runner.wake()
        await asyncio.wait_for(behind, 25)
        later.lease.release("test")
