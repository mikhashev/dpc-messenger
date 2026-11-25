"""Zcode #214 on follow-up 22 (follow-up 23).

- 1. The door did not count the instrumental adapter's tag that the `/synth` body carries when there are no
  lyrics, so a text job at the ceiling passed the door and was refused only at the run.
- 2. `wait_hashed` saw a scan only once its coroutine ran: a `rescan_all`, `rescan_folder` or poll-pass task
  created but not yet stepped was invisible.
- 3. A job whose `/synth` bodies were written before the ceiling existed resumed and went to the engine unchecked.
"""

import asyncio
import json

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import yue2
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobError
from dpc_client_core.music.library import MusicLibrary
from dpc_client_core.music.runner import build_yue2_body, yue2_request_name
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params


# -- 1: the door counts the instrumental tag ----------------------------------------------------------------


def _longest_style_that_fits(lyrics, duration=180):
    lo, hi = 1, 200_000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if js.yue2_text_overflow("a" * mid, lyrics, duration) is None:
            lo = mid
        else:
            hi = mid - 1
    return lo


def test_the_tag_has_one_source_the_body_and_the_door_share():
    assert yue2.INSTRUMENTAL_LYRICS is js.YUE2_INSTRUMENTAL_LYRICS
    body = json.loads(build_yue2_body({"style": "x", "lyrics": None, "duration_s": 60}, 1, None,
                                      yue2.INSTRUMENTAL_LYRICS))
    assert body["lyrics"] == js.YUE2_INSTRUMENTAL_LYRICS


def test_an_instrumental_text_job_at_the_ceiling_is_refused_at_the_door_not_only_at_the_run():
    n = _longest_style_that_fits("")                       # the style fits with no lyrics and no tag...
    assert js.yue2_text_overflow("a" * n, js.YUE2_INSTRUMENTAL_LYRICS, 180) is not None   # ...but not with the tag
    with pytest.raises(js.SongParamsError) as err:
        js.normalize_params_from_text({"lyrics": None, "style": "a" * n})
    assert err.value.code == "context_exceeded" and err.value.field == "lyrics"
    # the tag's width less leaves the room
    assert js.normalize_params_from_text({"lyrics": None, "style": "a" * (n - len(js.YUE2_INSTRUMENTAL_LYRICS))})
    # a job with lyrics is counted on its own lyrics, as before
    assert js.normalize_params_from_text({"lyrics": "", "style": "a" * 5} | {"lyrics": "la"})["lyrics"] == "la"


# -- 2: wait_hashed marks a scan at the call that creates it ------------------------------------------------


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


@pytest.mark.parametrize("which", ["rescan_all", "rescan_folder", "poll_pass"])
@pytest.mark.asyncio
async def test_wait_hashed_sees_a_scan_task_created_but_not_yet_run(tmp_path, monkeypatch, which):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 64)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.wait_hashed()
        gate = asyncio.Event()
        real = lib._rescan_folder_locked

        async def slow(folder_id):
            await gate.wait()
            return await real(folder_id)

        monkeypatch.setattr(lib, "_rescan_folder_locked", slow)
        if which == "rescan_all":
            scan = asyncio.ensure_future(lib.rescan_all())
        elif which == "rescan_folder":
            scan = asyncio.ensure_future(lib.rescan_folder(folder["id"]))
        else:
            scan = asyncio.ensure_future(lib._poll_pass())
        # no yield since the task was created: its coroutine has not taken a step
        assert lib._scans_running
        with pytest.raises(asyncio.TimeoutError):
            await lib.wait_hashed(timeout=0.05)
        gate.set()
        await lib.wait_hashed()
        assert scan.done()
        assert [e["hash_state"] for e in lib.list_entries()] == ["done"]
        assert not lib._scans_running
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_scan_coroutine_dropped_without_running_does_not_hold_wait_hashed(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        coro = lib.rescan_all()
        assert lib._scans_running
        coro.close()                      # never stepped
        del coro
        import gc
        gc.collect()
        assert not lib._scans_running
        await lib.wait_hashed(timeout=1.0)
    finally:
        await lib.stop()


# -- 3: a resume checks the bodies already on disk ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_with_pre_upgrade_bodies_that_overflow_is_refused_before_anything_is_sent(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.store.create_job("generate_from_text", yue2_params())
        params = job["params"]
        big = dict(params, lyrics="a" * 60_000)           # what a pre-upgrade door let through
        for index in range(int(params["variants"])):
            name = yue2_request_name(index + 1)
            await rig.runner._call(rig.store.write_payload, job["id"], name,
                                   build_yue2_body(big, 100 + index, None, None), endpoint="/synth")
        before = {p["name"]: rig.store.read_payload(job["id"], p["name"]) for p in rig.store.get_job(job["id"])["payloads"]}
        resumed = rig.store.get_job(job["id"])
        with pytest.raises(MusicJobError) as err:
            await rig.runner._yue2_payloads(resumed)
        assert err.value.code == "context_exceeded" and err.value.message.startswith("context_exceeded:")
        # the stored payloads are write-once: nothing was rewritten
        after = {p["name"]: rig.store.read_payload(job["id"], p["name"]) for p in rig.store.get_job(job["id"])["payloads"]}
        assert after == before and len(after) == int(params["variants"])


@pytest.mark.asyncio
async def test_a_resume_with_bodies_that_fit_reuses_them_and_the_seed(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.store.create_job("generate_from_text", yue2_params())
        seed = await rig.runner._yue2_payloads(job)
        again = await rig.runner._yue2_payloads(rig.store.get_job(job["id"]))
        assert again == seed
