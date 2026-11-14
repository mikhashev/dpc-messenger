"""A Strata KV snapshot is restored after an idle unload by loading the model first (ADR-044,
amendment 2026-11-13, point 5 "Where", and the plan's question 7).

On the pin (serve/server.py v0.2.11) `slot_action` answers 503 "the model is not loaded" and
loads nothing, while `POST /load` loads it (JSON from DPC's own page only, as `/slots/`). So
when `/metrics` `live.state` says "unloaded", the restore sends `POST /load` and then the
restore; a loaded engine gets the restore alone; a load that fails leaves the 503, which keeps
the file. The watcher marks the engine empty again when it sees it go from loaded to unloaded,
and only then, so a poll during the next load cannot mark a filled engine empty.

Both requests go through the supervisor's own httpx calls to a mock transport.
"""

import asyncio
import json
import time

import httpx
import pytest

from dpc_client_core.managers import kv_snapshots

from tests.test_an_unplugged_kv_snapshots_disk_starts_strata_with_saving_off import (  # noqa: F401
    ALIAS,
    card,
    kv_state,
    saving_strata,
)

_RealClient = httpx.AsyncClient


class FakeLauncher:
    """`/metrics`, `POST /load` and `POST /slots/0?action=restore` of the pinned launcher."""

    def __init__(self, monkeypatch, sup, state="unloaded", load_status=200, during_load=None):
        self.state, self.load_status, self.during_load = state, load_status, during_load
        self.requests = []
        sup._get = self.get
        del sup._post  # the start smoke's fake: `/load` goes through the real `_post`
        transport = httpx.MockTransport(self.handle)

        class Client(_RealClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def get(self, path):
        return {"engine": {"version": "0.1.46"}, "live": {"state": self.state}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/load":
            if self.during_load is not None:
                self.during_load()
            if self.load_status != 200:
                return httpx.Response(self.load_status, json={"error": {"message": "GPU busy"}})
            self.state = "idle"
            return httpx.Response(200, json={"status": "loaded"})
        if self.state == "unloaded":
            return httpx.Response(503, json={"error": {"code": 503, "message": "the model is not loaded",
                                                       "type": "server_error"}})
        name = json.loads(request.content)["filename"]
        return httpx.Response(200, json={"id_slot": 0, "filename": name, "n_restored": 30_000,
                                         "n_read": 4096, "timings": {"restore_ms": 3}})

    def paths(self):
        return [(r.url.path, r.url.params.get("action")) for r in self.requests]


async def started_with_a_snapshot(tmp_path, monkeypatch, **launcher):
    folder = tmp_path / "kv"
    sup = saving_strata(tmp_path, folder, model_params={"idle_unload_s": 600})
    await sup.ensure_running()
    (folder / "dpc-kv-0a1b.bin").write_bytes(b"\0" * 4096)
    entry = kv_snapshots.new_entry(
        file="dpc-kv-0a1b.bin", alias=ALIAS, engine="strata", identity=sup._kv_entry_identity(),
        record={"agent_id": "a1", "conversation_id": "c1", "caller_kind": "agent",
                "prefix_sha256": "ab" * 32, "prefix_messages": 3},
        n_saved=30_000, nbytes=4096, save_ms=5, now=time.time(),
    )
    assert kv_snapshots.add_entry(folder, entry, cap_bytes=kv_snapshots.cap_bytes_of(sup.config))
    return sup, FakeLauncher(monkeypatch, sup, **launcher), folder, entry


@pytest.mark.asyncio
async def test_an_unloaded_engine_is_loaded_and_then_restored(tmp_path, monkeypatch):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch)

    async with sup.call_slot():
        outcome = await sup._restore_kv_snapshot(entry)

    assert launcher.paths() == [("/load", None), ("/slots/0", "restore")]
    load = launcher.requests[0]
    assert load.method == "POST" and load.headers["content-type"].startswith("application/json")
    assert "origin" not in load.headers
    assert outcome["outcome"] == kv_snapshots.RESTORED
    assert [e["file"] for e in kv_snapshots.read_index(folder)["entries"]] == [entry["file"]]


@pytest.mark.asyncio
async def test_a_loaded_engine_gets_the_restore_alone(tmp_path, monkeypatch):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch, state="idle")

    outcome = await sup._restore_kv_snapshot(entry)

    assert launcher.paths() == [("/slots/0", "restore")]
    assert outcome["outcome"] == kv_snapshots.RESTORED


@pytest.mark.asyncio
async def test_a_load_that_fails_leaves_the_503_and_the_file_is_kept(tmp_path, monkeypatch):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch, load_status=503)

    outcome = await sup._restore_kv_snapshot(entry)

    assert launcher.paths() == [("/load", None), ("/slots/0", "restore")]
    assert outcome["outcome"] == kv_snapshots.NOT_LOADED
    assert (folder / entry["file"]).exists()
    assert [e["file"] for e in kv_snapshots.read_index(folder)["entries"]] == [entry["file"]]


@pytest.mark.asyncio
async def test_a_call_that_reaches_the_engine_during_the_load_keeps_the_restore_out(tmp_path, monkeypatch):
    """Pre-code check 1: a restore beside a live conversation drops it. A call that took a slot
    while `POST /load` ran is that conversation, so the restore is skipped and the file kept."""
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch)
    other = []
    launcher.during_load = lambda: other.append(sup.call_slot())

    async with sup.call_slot():
        outcome = await sup._restore_kv_snapshot(entry)
    async with other[0]:
        pass

    assert launcher.paths() == [("/load", None)]
    assert outcome["reason"] == "another call reached the engine during the load"
    assert [e["file"] for e in kv_snapshots.read_index(folder)["entries"]] == [entry["file"]]
    assert (folder / entry["file"]).exists() and sup._in_flight == 0


@pytest.mark.asyncio
async def test_the_watcher_leaves_the_engine_filled_when_a_call_loads_it_meanwhile(tmp_path):
    """idle -> unloaded with a call sent between the polls: the pin reports "unloaded" while
    that call's own load runs, so the engine is not marked empty."""
    sup = saving_strata(tmp_path, tmp_path / "kv", model_params={"idle_unload_s": 600},
                        engine_watch_interval_s=0.01)
    await sup.ensure_running()
    sup._kv_engine_fresh = False
    states = iter(["idle", "unloaded", "idle", "unloaded"])
    polls = []

    async def metrics(path):
        state = next(states, "unloaded")
        polls.append(state)
        if len(polls) == 2:
            kv_snapshots.note_clearable("c1")  # a ConversationMonitor exists for it
            seq = kv_snapshots.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1",
                                                conversation_id="c1", prefix=("ab" * 32, 3), at=time.time())
            kv_snapshots.mark_sent(ALIAS, seq)
        if len(polls) == 4:
            sup._in_flight += 1  # a call in flight at the change
        return {"live": {"state": state}}

    sup._get = metrics
    sup._watch_task = asyncio.get_running_loop().create_task(sup._watch_engine())
    while len(polls) < 6:
        await asyncio.sleep(0.002)
    sup._in_flight -= 1
    await sup.stop()
    assert sup._kv_engine_fresh is False


@pytest.mark.asyncio
async def test_a_draining_engine_is_neither_loaded_nor_restored(tmp_path, monkeypatch):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch)
    sup._draining = True

    outcome = await sup._restore_kv_snapshot(entry)

    assert launcher.requests == [] and outcome["reason"] == "the engine is draining"
    sup._draining = False


@pytest.mark.asyncio
async def test_the_watcher_marks_the_engine_empty_on_the_unload_and_not_during_the_next_load(tmp_path):
    sup = saving_strata(tmp_path, tmp_path / "kv", model_params={"idle_unload_s": 600},
                        engine_watch_interval_s=0.01)
    await sup.ensure_running()
    states = iter(["unloaded", "idle", "idle", "unloaded", "unloaded", "unloaded"])
    polls, fresh_at_start, fresh_before_the_unload = [], [], []

    async def metrics(path):
        state = next(states, "unloaded")
        polls.append(state)
        if len(polls) == 1:
            fresh_at_start.append(sup._kv_engine_fresh)  # fresh from the launch itself
            sup._kv_engine_fresh = False  # a call served after the start
        if len(polls) == 4:
            fresh_before_the_unload.append(sup._kv_engine_fresh)
        return {"live": {"state": state}}

    sup._get = metrics
    sup._watch_task = asyncio.get_running_loop().create_task(sup._watch_engine())
    while len(polls) < 4:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.03)
    assert fresh_at_start == [True]
    # The first poll saw "unloaded" with nothing before it: no change, no mark.
    assert fresh_before_the_unload == [False]
    # idle -> unloaded at the fourth poll: marked.
    assert sup._kv_engine_fresh is True and sup.failure is None
    sup._kv_engine_fresh = False  # the restoring call clears it before its load
    while len(polls) < 6:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.03)
    assert sup._kv_engine_fresh is False  # still unloaded while it loads: not marked again
    await sup.stop()
