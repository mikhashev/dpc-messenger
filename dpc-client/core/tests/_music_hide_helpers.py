"""Shared setup for the hide / remove / delete music tests."""

import os

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


def take(folder, name="take1.wav", size=700):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(os.urandom(size))
    return path


def generated_dir(home, job="job_1"):
    return home / "music" / "generated" / job


async def folder_with_tracks(lib, folder, names=("a.mp3", "b.mp3")):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(os.urandom(300) + n.encode())
    f = await lib.add_folder(str(folder), scan=False)
    await lib.rescan_folder(f["id"])
    await lib.wait_hashed()
    return f, {e["source"]["name"]: e["id"] for e in lib.list_entries()}
