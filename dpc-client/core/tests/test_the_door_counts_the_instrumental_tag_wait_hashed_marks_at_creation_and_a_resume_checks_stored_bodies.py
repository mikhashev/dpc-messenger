"""Zcode #214 on follow-up 22 (follow-up 23).

- 1. The door did not count the instrumental adapter's tag that the `/synth` body carries when there are no
  lyrics, so a text job at the ceiling passed the door and was refused only at the run.
- 2. `wait_hashed` saw a scan only once its coroutine ran: a `rescan_all`, `rescan_folder` or poll-pass task
  created but not yet stepped was invisible.
- 3. A job whose `/synth` bodies were written before the ceiling existed resumed and went to the engine unchecked.
"""

import asyncio
import gc
import json
import threading
import time

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


# -- 4: follow-up 24 (Zcode #217): finalizer wakes loop & resume refuses corrupt /synth bodies ---------------


@pytest.mark.asyncio
async def test_a_scan_coroutine_dropped_on_a_worker_thread_wakes_wait_hashed_immediately(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    loop = asyncio.get_running_loop()
    thread_ids = []
    real_call_soon_threadsafe = loop.call_soon_threadsafe
    threadsafe_calls = []

    def spy_threadsafe(callback, *args, **kwargs):
        threadsafe_calls.append(threading.get_ident())
        return real_call_soon_threadsafe(callback, *args, **kwargs)

    loop.call_soon_threadsafe = spy_threadsafe
    try:
        coro = lib.rescan_all()
        [marker] = list(lib._scans_running)
        real_set_result = marker.set_result

        def spy_set_result(value):
            thread_ids.append(threading.get_ident())
            return real_set_result(value)

        marker.set_result = spy_set_result
        holder = [coro]
        del coro

        wait_task = asyncio.create_task(lib.wait_hashed(timeout=3.0))
        await asyncio.sleep(0.02)  # wait_hashed is now blocked waiting on marker
        released_at = []

        def drop_off_loop():
            time.sleep(0.05)
            c = holder.pop()
            c.close()
            del c
            gc.collect()
            released_at.append(time.monotonic())

        t = threading.Thread(target=drop_off_loop)
        t.start()
        try:
            await wait_task
            woke_at = time.monotonic()
        finally:
            t.join(timeout=2.0)
        assert released_at and (woke_at - released_at[0]) < 0.4
        assert thread_ids == [threading.get_ident()], "marker.set_result must run on the event loop thread"
        assert threadsafe_calls, "off-loop finalizer must wake the loop via call_soon_threadsafe"
        assert not lib._scans_running
    finally:
        loop.call_soon_threadsafe = real_call_soon_threadsafe
        await lib.stop()


def test_a_scan_coroutine_dropped_after_loop_closes_does_not_raise(tmp_path, monkeypatch):
    import sys

    lib = _library(tmp_path / "dpc")
    coro = None
    loop = asyncio.new_event_loop()
    try:
        async def setup():
            nonlocal coro
            await lib.open()
            coro = lib.rescan_all()
            [marker] = list(lib._scans_running)
            marker.add_done_callback(lambda _fut: None)
            assert lib._scans_running
            await lib.stop()

        loop.run_until_complete(setup())
    finally:
        loop.close()

    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    coro.close()
    del coro
    gc.collect()
    assert not unraisable
    assert not lib._scans_running


def _write_corrupt_resume_payload(rig, job_id, name, content):
    path = rig.store.payload_path(job_id, name)
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)


@pytest.mark.parametrize("case, content", [
    ("missing_file", None),
    ("bad_json", b"{not valid json"),
    ("invalid_utf8", b"\xff\xfe\xfd"),
    ("non_object_list", b"[]"),
    ("non_object_str", b'"just a string"'),
    ("non_object_int", b"123"),
    ("non_object_null", b"null"),
    ("non_string_lyrics_int", json.dumps({"style": "indie", "lyrics": 123, "duration": 60, "seed": 42}).encode()),
    ("non_string_lyrics_list", json.dumps({"style": "indie", "lyrics": ["a"], "duration": 60, "seed": 42}).encode()),
    ("missing_seed", json.dumps({"style": "indie", "lyrics": "la", "duration": 60}).encode()),
    ("bad_duration", json.dumps({"style": "indie", "lyrics": "la", "duration": "bad", "seed": 42}).encode()),
])
@pytest.mark.asyncio
async def test_a_resume_with_missing_or_corrupt_stored_synth_body_is_refused_cleanly(tmp_path, monkeypatch, case, content):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.store.create_job("generate_from_text", yue2_params(variants=1))
        name = yue2_request_name(1)
        await rig.runner._call(rig.store.write_payload, job["id"], name,
                               build_yue2_body(job["params"], 42, None, None), endpoint="/synth")
        _write_corrupt_resume_payload(rig, job["id"], name, content)
        resumed = rig.store.get_job(job["id"])
        with pytest.raises(MusicJobError) as err:
            await rig.runner._yue2_payloads(resumed)
        assert err.value.code == "invalid_payload", case
        assert err.value.message.startswith("invalid_payload:"), case

        await rig.runner.run_job(job["id"])
        refused = rig.store.get_job(job["id"])
        assert refused["state"] == js.UNSUPPORTED, case
        assert "invalid_payload" in (refused.get("state_reason") or ""), case
        assert "internal error" not in (refused.get("state_reason") or "").lower(), case


@pytest.mark.asyncio
async def test_a_resume_checks_second_variant_stored_body_too(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.store.create_job("generate_from_text", yue2_params(variants=2))
        for v in (1, 2):
            await rig.runner._call(rig.store.write_payload, job["id"], yue2_request_name(v),
                                   build_yue2_body(job["params"], 41 + v, None, None), endpoint="/synth")
        _write_corrupt_resume_payload(
            rig, job["id"], yue2_request_name(2),
            json.dumps({"style": "indie", "lyrics": ["not", "a", "string"], "duration": 60, "seed": 43}).encode(),
        )
        await rig.runner.run_job(job["id"])
        refused = rig.store.get_job(job["id"])
        assert refused["state"] == js.UNSUPPORTED
        assert "invalid_payload" in (refused.get("state_reason") or "")
        assert "internal error" not in (refused.get("state_reason") or "").lower()
