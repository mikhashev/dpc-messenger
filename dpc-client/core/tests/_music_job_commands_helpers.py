"""Shared setup for the music job command tests: a service whose job store reports an
installed engine (so jobs queue), and a library with two real tracks."""

import os

from dpc_client_core.music import commands
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from tests._music_jobs_helpers import LM_BODY, from_text_params, store_at


class Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return "0.05" if (section, key) == ("music", "poll_interval_s") else fallback


class Service(MusicCommandsMixin):
    def __init__(self, home):
        self.settings = Settings(home)


def without_tools(monkeypatch):
    original = commands.MusicLibrary.__init__

    def init(self, home, **kw):
        kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init)


def service_with_engine(home):
    """A service whose store believes an engine is installed."""
    service = Service(home)
    service._music_jobs_instance = store_at(home)
    return service


async def library_tracks(service, folder, names=("a.mp3", "b.mp3")):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(os.urandom(200) + n.encode())
    added = await service.add_music_folder(path=str(folder), watch=False)
    await service.rescan_music_folder(folder_id=added["folder"]["id"])
    lib = await service._music()
    await lib.wait_hashed()
    return {e["source"]["name"]: e["id"] for e in lib.list_entries()}


def review_job(store, library_ids):
    """A job in review whose takes point at the given library entries (None: unlinked)."""
    job = store.create_job("generate_from_text", from_text_params(variants=len(library_ids)))
    store.write_payload(job["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    store.start_job(job["id"])
    takes = []
    for v, lib_id in enumerate(library_ids, start=1):
        takes.append(store.add_take(
            job["id"], variant=v, file_uri=f"file:///music/generated/{job['id']}/v{v}.flac",
            content_hash=f"sha256:{v:064x}", title=f"variant {v}", seed=42, synth_batch_size=2,
            synth_batch_index=v - 1, payload="lm.request.json", sample_rate=48000, channels=2,
            duration_s=30.0, library_track_id=lib_id))
    return store.finish_job(job["id"]), takes
