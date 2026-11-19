"""The recorder's abandoned WAVs are removed from ~/.dpc/temp on start, and nothing else.

NOTHING-CLEANS-THE-DPC-TEMP-FOLDER-SO-ABANDONED-RECORDINGS-PILE-UP: a recording
core never confirmed, a discard whose remove failed, or a crash mid-send stayed
in ~/.dpc/temp forever. On start core removes, in the background, the
recorder's `voice_<digits>.wav` files last modified more than a day ago. Other
files there (agents' working files, a Telegram screenshot the history points
at), directories and symlinks are left alone, and a file held open is skipped.
"""

import asyncio
import logging
import os
import time

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core import temp_sweeper
from dpc_client_core.service import CoreService
from dpc_client_core.temp_sweeper import ABANDONED_RECORDING_AGE_S, sweep_abandoned_recordings

NOW = time.time()
OLD = NOW - ABANDONED_RECORDING_AGE_S - 60
YOUNG = NOW - ABANDONED_RECORDING_AGE_S + 60


def _file(folder, name, mtime, data=b"RIFF....WAVE"):
    f = folder / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    os.utime(f, (mtime, mtime))
    return f


def test_an_old_recording_is_removed_and_a_young_one_kept(tmp_path):
    old = _file(tmp_path, "voice_1700000000.wav", OLD)
    young = _file(tmp_path, "voice_1800000000.wav", YOUNG)

    assert sweep_abandoned_recordings(tmp_path, now=NOW) == 1

    assert not old.exists()
    assert young.exists()


def test_a_recording_with_a_clock_ahead_is_kept(tmp_path):
    ahead = _file(tmp_path, "voice_1900000000.wav", NOW + 3600)

    assert sweep_abandoned_recordings(tmp_path, now=NOW) == 0
    assert ahead.exists()


@pytest.mark.parametrize("name", [
    "notes.md", "probe.py", "paper.pdf", "screenshot_1700000000.png",
    "voice_rec.wav", "voice_1700000000.wav.part", "my_voice_1700000000.wav", "voice_1700000000.WAV.bak",
])
def test_an_old_file_that_is_not_the_recorders_is_kept(tmp_path, name):
    other = _file(tmp_path, name, OLD)

    assert sweep_abandoned_recordings(tmp_path, now=NOW) == 0
    assert other.exists()


def test_a_directory_is_never_entered_or_removed(tmp_path):
    folder = tmp_path / "voice_1700000000.wav"
    inner = _file(folder, "voice_1600000000.wav", OLD)
    os.utime(folder, (OLD, OLD))
    sub = _file(tmp_path / "probe-2026-11-18", "voice_1600000001.wav", OLD)

    assert sweep_abandoned_recordings(tmp_path, now=NOW) == 0
    assert inner.exists() and sub.exists() and folder.is_dir()


def test_a_symlink_is_not_followed_or_removed(tmp_path):
    target = _file(tmp_path / "elsewhere", "kept.wav", OLD)
    temp = tmp_path / "temp"
    temp.mkdir()
    link = temp / "voice_1700000000.wav"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks cannot be created here: {e}")
    try:
        os.utime(link, (OLD, OLD), follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass  # the link's own mtime is now; it is skipped as a link either way

    assert sweep_abandoned_recordings(temp, now=NOW) == 0
    assert target.exists() and target.read_bytes() == b"RIFF....WAVE"
    assert os.path.lexists(link)


def test_a_file_held_open_is_skipped_and_the_rest_removed(tmp_path, monkeypatch):
    locked = _file(tmp_path, "voice_1700000000.wav", OLD)
    free = _file(tmp_path, "voice_1700000001.wav", OLD)
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if os.path.basename(path) == locked.name:
            raise PermissionError(32, "The process cannot access the file", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(temp_sweeper.os, "unlink", unlink)

    assert sweep_abandoned_recordings(tmp_path, now=NOW) == 1
    assert locked.exists() and not free.exists()


def test_a_missing_temp_folder_is_nothing_to_do(tmp_path):
    assert sweep_abandoned_recordings(tmp_path / "nowhere", now=NOW) == 0


def test_the_count_is_logged(tmp_path, caplog):
    _file(tmp_path, "voice_1700000000.wav", OLD)
    _file(tmp_path, "voice_1700000001.wav", OLD)

    with caplog.at_level(logging.INFO, logger=temp_sweeper.__name__):
        sweep_abandoned_recordings(tmp_path, now=NOW)

    assert any("Removed 2 abandoned recording(s)" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_core_sweeps_dpc_home_temp_off_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    old = _file(tmp_path / "home" / "temp", "voice_1700000000.wav", time.time() - ABANDONED_RECORDING_AGE_S - 60)
    keep = _file(tmp_path / "home" / "temp", "notes.md", time.time() - ABANDONED_RECORDING_AGE_S - 60)
    threads = []
    real_to_thread = asyncio.to_thread

    async def to_thread(fn, *args, **kwargs):
        threads.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(service_mod.asyncio, "to_thread", to_thread)

    await CoreService._sweep_abandoned_recordings()

    assert threads == [sweep_abandoned_recordings]
    assert not old.exists() and keep.exists()


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_raise_into_the_start(monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(temp_sweeper, "sweep_abandoned_recordings", boom)

    with caplog.at_level(logging.WARNING):
        await CoreService._sweep_abandoned_recordings()

    assert any("disk gone" in r.getMessage() for r in caplog.records)
