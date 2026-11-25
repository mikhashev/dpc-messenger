"""ADR-052, Confirmation test 34 (Ark #64 §5; L10 "Music"; slice S4 records YuE2's factor by length).

A music job that has not started yet is counted by the waiters behind it with its text estimate:
`estimate_yue2_seconds` per variant (audio seconds, never more than the job's duration cap), turned into
wall seconds by `yue2_wall_s` (a fixed overhead plus the audio over a rate, fit to five live points: 10.76 s +
audio / 5.13), times the variants, marked as an estimate. An instrumental counts its cap, so the figure
for YuE2 is an upper bound and is labelled "up to". The expectations below are literal numbers worked
out by hand, not computed with the functions under test. acestep.cpp has no measured factor: unknown. A
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


# The five live points: (audio s, job wall s). 2026-11-24 ADR-052 live run, section 4d, and two instrumentals
# of 2026-11-25.
LIVE_POINTS = [(30, 16.48), (120, 33.73), (162.7, 43.11), (30, 18.0), (60, 21.0)]


@pytest.mark.parametrize("audio_s, wall_s", LIVE_POINTS)
def test_the_formula_reads_each_live_point_within_a_second_and_a_half(audio_s, wall_s):
    assert js.yue2_wall_s(audio_s) == pytest.approx(wall_s, abs=1.5)


def test_wall_time_is_an_overhead_plus_audio_over_a_rate_and_a_longer_song_is_faster_per_audio_second():
    assert js.yue2_wall_s(0) == pytest.approx(10.76)
    assert js.yue2_wall_s(80) == pytest.approx(26.354, abs=0.001)
    assert js.yue2_wall_s(240) == pytest.approx(57.55, abs=0.01)
    # The factor (audio per wall second) rises with length: the old table had it falling.
    assert 30 / js.yue2_wall_s(30) < 120 / js.yue2_wall_s(120) < 240 / js.yue2_wall_s(240)
    # And never the order of 10x off (the first reading: 720 s for a 240 s song).
    assert js.yue2_wall_s(240) < 100


def test_a_queued_jobs_estimate_is_the_wall_time_of_the_text_estimate_times_the_variants():
    lyrics = "[verse]\n" + "\n".join(f"line {i}" for i in range(6)) + "\n"
    params = {"engine": "yue2", "lyrics": lyrics, "duration_s": 360, "variants": 2}
    # 20 s intro + 6 sung lines x 10 s = 80 s of song: 10.76 + 80 / 5.13 = 26.354 s per variant, two variants.
    assert js.music_expected_s(params) == pytest.approx(52.71, abs=0.01)
    # The cap bounds the song: a 30 s cap never reads as a longer one (16.608 s, two variants).
    capped = {**params, "duration_s": 30}
    assert js.music_expected_s(capped) == pytest.approx(33.22, abs=0.01)
    # An instrumental has no lines to count: its song is the nominal length, the job's cap (Mike, 2026-11-24,
    # group #33), not the intro alone. A 360 s cap is 10.76 + 360 / 5.13 = 80.94 s; the form's default cap of
    # 180 s is 45.85 s; with no cap in the params, the default one. The text estimate itself (the short-cap
    # warning's) is unchanged.
    assert js.music_expected_s({**params, "lyrics": None, "variants": 1}) == pytest.approx(80.94, abs=0.01)
    assert js.music_expected_s({**params, "lyrics": None, "variants": 1, "duration_s": 180}) == pytest.approx(45.85, abs=0.01)
    assert js.music_expected_s({"engine": "yue2", "lyrics": None, "variants": 1}) == pytest.approx(45.85, abs=0.01)
    assert js.music_expected_s({**params, "lyrics": None, "variants": 2, "duration_s": 180}) == pytest.approx(91.70, abs=0.01)
    assert js.estimate_yue2_seconds(None) == js.YUE2_INTRO_S
    assert js.music_expected_s({"engine": "acestep", "lyrics": lyrics, "duration_s": 30, "variants": 2}) is None


def test_an_instrumentals_figure_is_its_caps_wall_time_and_is_labelled_an_upper_bound():
    from dpc_client_core.dpc_agent.tools import music as music_tools
    inst = {"engine": "yue2", "style": "piano", "lyrics": None, "variants": 1, "duration_s": 180}
    assert js.music_expected_s(inst) == pytest.approx(js.yue2_wall_s(180))
    assert "up to 46 s on the GPU" in music_tools._approval_about(js.GENERATE_FROM_TEXT, inst)
    parts = music_tools._approval_parts(js.GENERATE_FROM_TEXT, inst)
    assert parts["eta_seconds"] == 46 and parts["eta_is_upper_bound"] is True


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
    # Two sung lines: 20 s intro + 20 s = 40 s of song: 10.76 + 40 / 5.13 = 18.56 s per variant, two variants.
    assert expected == pytest.approx(37.12, abs=0.01)
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
