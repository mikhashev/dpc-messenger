"""A Strata save answered 500 suspends saving for that launch, and nothing else does (ADR-044,
amendment 2026-11-13, points 4 and 8).

dflash2 answers every save 500 "invalid K/V extent" (upstream #5153): one WARNING, "the engine
refuses saves" as the off reason, no second save in that launch, saving back at the next launch.
A 415, 403, 501 or 400 is DPC's or the configuration's fault and leaves saving on. A 500 naming
the engine's restart is the engine ending during the save (serve/server.py v0.2.11, the
`/slots/` handler: `EngineDied`, `EngineSilent` included, "; the next request starts the engine
again"): logged as a death, never as a refusal, the stop goes on and nothing is latched.

The save goes out through the real `_slot_action` to an httpx mock transport, so the request's
own shape (path, JSON content type, no Origin) is checked too.
"""

import asyncio
import json
import logging
import time

import httpx
import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers.llama_server_supervisor import stop_by_hand

from tests.test_an_unplugged_kv_snapshots_disk_starts_strata_with_saving_off import (  # noqa: F401
    ALIAS,
    card,
    kv_state,
    saving_strata,
)
from tests.test_strata_supervisor import _FakeProc

_RealClient = httpx.AsyncClient


class FakeLauncher:
    """The pinned launcher's `GET /slots` and `POST /slots/0?action=…`, at the HTTP level."""

    def __init__(self, monkeypatch, sup, folder, status=200, body=None, held=30_000):
        self.folder, self.status, self.body = folder, status, body
        self.requests = []
        self.held = held
        sup._get = self.get

        async def no_cold_rows(tokens):
            return None

        sup._kv_cold_rate = no_cold_rows
        transport = httpx.MockTransport(self.handle)

        class Client(_RealClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def get(self, path):
        if path == "/slots":
            return [{"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": self.held}]
        if path == "/metrics":
            return {"engine": {"version": "0.1.46"}, "live": {"state": "idle"}}
        return {"total_slots": 1}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(request)
        if self.status == 200:
            (self.folder / body["filename"]).write_bytes(b"\0" * 4096)
            return httpx.Response(200, json={"id_slot": 0, "filename": body["filename"], "n_saved": 30_000,
                                             "n_written": 4096, "timings": {"save_ms": 5}})
        return httpx.Response(self.status, json=self.body)


def note_owner_call(prompt=30_000, reply=500):
    kv_snapshots.note_clearable("c1")  # a ConversationMonitor exists for it
    seq = kv_snapshots.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                                        prefix=("ab" * 32, 3), at=time.time())
    kv_snapshots.mark_sent(ALIAS, seq)  # the provider marks it as the call goes on the wire
    kv_snapshots.note_engine_tokens(ALIAS, seq, prompt, reply)


async def started(tmp_path, monkeypatch, **launcher):
    folder = tmp_path / "kv"
    sup = saving_strata(tmp_path, folder)
    await sup.ensure_running()
    return sup, FakeLauncher(monkeypatch, sup, folder, **launcher), folder


_DFLASH2 = {"error": {"code": 500, "message": "invalid K/V extent", "type": "server_error", "kind": "invalid"}}


@pytest.mark.asyncio
async def test_the_save_request_is_json_to_slot_0_with_no_origin(tmp_path, monkeypatch):
    sup, launcher, folder = await started(tmp_path, monkeypatch)
    note_owner_call()

    outcome = await stop_by_hand(sup)

    assert outcome["outcome"] == kv_snapshots.SAVED and sup._proc is None
    request, = launcher.requests
    assert request.method == "POST" and request.url.path == "/slots/0"
    assert request.url.params["action"] == "save"
    assert request.headers["content-type"].startswith("application/json")
    assert "origin" not in request.headers
    assert json.loads(request.content)["filename"].endswith(".staging")
    entry, = kv_snapshots.read_index(folder)["entries"]
    assert entry["n_saved"] == 30_000 and entry["engine"] == "strata"


@pytest.mark.asyncio
async def test_a_500_warns_once_suspends_the_launch_and_the_next_launch_saves_again(tmp_path, monkeypatch, caplog):
    sup, launcher, folder = await started(tmp_path, monkeypatch, status=500, body=_DFLASH2)
    note_owner_call()

    with caplog.at_level(logging.INFO):
        first = await stop_by_hand(sup)

    assert first["outcome"] == kv_snapshots.ENGINE_REFUSES and "off for this launch" in first["reason"]
    assert kv_snapshots.off_reason(ALIAS) == kv_snapshots.ENGINE_REFUSES_SAVES
    assert kv_snapshots.status_of(ALIAS)["off_reason"] == "the engine refuses saves"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "not saved" in r.getMessage()]
    assert len(warnings) == 1 and sup._proc is None and sup.failure is None

    # The same launch, asked again: suspended, and the engine is not called.
    sup._proc = _FakeProc()
    second = await sup._save_live_conversation(None)
    assert second["reason"] == f"suspended for this launch: {kv_snapshots.ENGINE_REFUSES_SAVES}"
    assert len(launcher.requests) == 1
    sup._proc = None

    await sup.ensure_running()
    assert kv_snapshots.off_reason(ALIAS) is None
    note_owner_call()
    await stop_by_hand(sup)
    assert len(launcher.requests) == 2


@pytest.mark.parametrize("status,words", [
    (415, "application/json"),
    (403, "foreign Origin"),
    (501, "no folder or runs parallel requests"),
    (400, "the engine does not take"),
])
@pytest.mark.asyncio
async def test_a_request_fault_logs_its_own_reason_and_leaves_saving_on(tmp_path, monkeypatch, caplog, status, words):
    sup, launcher, folder = await started(
        tmp_path, monkeypatch, status=status, body={"error": {"code": status, "message": "x"}})
    note_owner_call()

    with caplog.at_level(logging.INFO):
        outcome = await sup.drain(timeout=None)

    assert outcome["outcome"] == kv_snapshots.TRANSPORT and f"({status})" in outcome["reason"]
    assert words in outcome["reason"] and "refuses saves" not in outcome["reason"]
    assert kv_snapshots.off_reason(ALIAS) is None and sup._proc is None
    assert any(r.levelno == logging.WARNING and words in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_engine_that_ends_during_the_save_is_a_death_and_the_stop_goes_on(tmp_path, monkeypatch, caplog):
    died = {"error": {"code": 500, "type": "server_error",
                      "message": "the engine stopped (exit code 3221226505); the next request starts the engine again"}}
    sup, launcher, folder = await started(tmp_path, monkeypatch, status=500, body=died)
    note_owner_call()

    with caplog.at_level(logging.INFO):
        outcome = await stop_by_hand(sup)

    assert outcome["outcome"] == kv_snapshots.ENGINE_DIED
    assert outcome["reason"] == "the engine ended during the call"
    assert kv_snapshots.off_reason(ALIAS) is None  # not "the engine refuses saves"
    assert sup._proc is None and sup.failure is None  # stopped, and the next launch is a new one
    assert sup._kv_save_in_flight is False
    assert kv_snapshots.read_index(folder)["entries"] == [] and not list(folder.glob("*.staging"))
    await sup.ensure_running()
    assert sup._proc is not None


@pytest.mark.asyncio
async def test_the_watcher_does_not_latch_an_engine_the_save_ended(tmp_path, monkeypatch):
    sup, launcher, folder = await started(tmp_path, monkeypatch)
    polls = []

    async def unloaded(path):
        polls.append(path)
        return {"live": {"state": "unloaded"}}

    sup._get = unloaded
    sup.config["engine_watch_interval_s"] = 0.01
    sup._kv_save_in_flight = True  # a save was sent; the drain's stop has not run
    sup._watch_task = asyncio.get_running_loop().create_task(sup._watch_engine())
    while len(polls) < 5:
        await asyncio.sleep(0.01)

    assert sup.failure is None and sup._proc is not None
    await sup.stop()
    assert sup._kv_save_in_flight is False and sup._watch_task is None



_STORAGE = {"error": {"code": 507, "message": "not enough disk", "type": "server_error", "kind": "storage"}}


@pytest.mark.asyncio
async def test_three_507_in_a_row_suspend_the_launch_and_any_other_outcome_resets_the_streak(
        tmp_path, monkeypatch, caplog):
    sup, launcher, folder = await started(tmp_path, monkeypatch, status=507, body=_STORAGE)

    async def attempt():
        note_owner_call()
        sup._proc = _FakeProc()
        return await sup._save_live_conversation(None)

    # Two refusals, then a save that lands: the streak starts over.
    assert (await attempt())["outcome"] == kv_snapshots.FAILED
    assert (await attempt())["outcome"] == kv_snapshots.FAILED
    launcher.status = 200
    assert (await attempt())["outcome"] == kv_snapshots.SAVED
    launcher.status = 507
    assert (await attempt())["outcome"] == kv_snapshots.FAILED
    assert (await attempt())["outcome"] == kv_snapshots.FAILED
    assert kv_snapshots.off_reason(ALIAS) is None

    with caplog.at_level(logging.INFO):
        third = await attempt()
    assert third["outcome"] == kv_snapshots.FAILED and "off for this launch" in third["reason"]
    assert kv_snapshots.off_reason(ALIAS) == "the engine refused 3 saves in a row for lack of disk (507)"
    sent = len(launcher.requests)
    assert (await attempt())["reason"].startswith("suspended for this launch: the engine refused 3 saves")
    assert len(launcher.requests) == sent

    # A new launch clears the suspension and the streak.
    kv_snapshots.launch_began(ALIAS)
    assert kv_snapshots.off_reason(ALIAS) is None
    assert (await attempt())["outcome"] == kv_snapshots.FAILED
    assert kv_snapshots.off_reason(ALIAS) is None
