"""The `local_api` door to the music engine (ADR-051 D30): `get_music_engine_status`,
`start_music_engine`, `stop_music_engine`; the job store's probe is the engine's real
state; `start_music_jobs` runs a proposed job to review and `stop_music_jobs` stops the
server. Every answer is `ok` or a mapped error. yue2 is the default engine since
2026-11-21, so these calls name acestep with `engine`.
"""

import asyncio

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music import commands
from dpc_client_core.music.commands import MUSIC_COMMANDS
from tests._acestep_helpers import FakeAceStepServer, fake_flac, free_env, from_text, install_fake_engine, wait_until
from tests._music_job_commands_helpers import Service, without_tools


class Events:
    def __init__(self):
        self.sent = []

    async def broadcast_event(self, name, payload):
        self.sent.append((name, payload))


def service_at(home, monkeypatch):
    without_tools(monkeypatch)
    monkeypatch.setattr(commands, "AceStepServer", FakeAceStepServer)
    service = Service(home)
    service.settings.get = lambda section, key, fallback=None: "0" if key == "acestep_port" else fallback
    service.local_api = Events()
    return service


def test_the_engine_commands_are_on_the_door():
    for name in ("get_music_engine_status", "start_music_engine", "stop_music_engine"):
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS
    assert getattr(commands.MusicCommandsMixin.start_music_engine, "dpc_slow", False)
    assert getattr(commands.MusicCommandsMixin.stop_music_engine, "dpc_slow", False)


@pytest.mark.asyncio
async def test_not_installed_is_reported_and_start_is_refused(tmp_path, monkeypatch):
    service = service_at(tmp_path / "dpc", monkeypatch)
    status = await service.get_music_engine_status(engine="acestep")
    assert status["status"] == "ok"
    assert status["engine"]["state"] == "not_installed"
    assert status["engine"]["host"] == "127.0.0.1"
    assert status["runner"] == {"running": False, "current_job_id": None, "current_engine": None}
    started = await service.start_music_engine(engine="acestep")
    assert started["status"] == "error" and started["code"] == "not_installed"


@pytest.mark.asyncio
async def test_start_and_stop_by_hand_and_a_job_through_the_runner(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    service = service_at(home, monkeypatch)
    monkeypatch.setattr(commands, "MusicJobRunner", _runner_with_fake_flac(commands.MusicJobRunner))
    try:
        started = await service.start_music_engine(engine="acestep")
        assert started["status"] == "ok", started
        assert started["engine"]["state"] == "running" and isinstance(started["engine"]["pid"], int)
        caps = (await service.get_music_capabilities())["capabilities"]
        assert caps["engines"]["acestep"]["state"] == "running"
        stopped = await service.stop_music_engine(engine="acestep")
        assert stopped["status"] == "ok" and stopped["stopped"] is True
        assert stopped["engine"]["state"] == "stopped"

        await service.start_music_jobs()
        proposed = await service.propose_music_job(type="generate_from_text", params=from_text(variants=1))
        assert proposed["job"]["state"] == "queued"
        job_id = proposed["job"]["id"]

        async def state():
            return (await service.get_music_job(job_id=job_id))["job"]["state"]
        for _ in range(3000):
            if await state() in ("review", "failed"):
                break
            await asyncio.sleep(0.02)
        job = (await service.get_music_job(job_id=job_id))["job"]
        assert job["state"] == "review", job["state_reason"]
        await asyncio.sleep(0.05)
        names = [n for n, _ in service.local_api.sent]
        assert "music_job_updated" in names and "music_engine_status" in names
        last = [p["job"] for n, p in service.local_api.sent if n == "music_job_updated"][-1]
        assert last["id"] == job_id and last["state"] == "review"
        takes = (await service.list_music_takes(job_id=job_id))["takes"]
        assert len(takes) == 1 and takes[0]["library_track_id"]
        library = (await service.list_music_library())["entries"]
        assert [e["id"] for e in library] == [takes[0]["library_track_id"]]
    finally:
        await service.stop_music_jobs()
        await service.stop_music_library()
    assert not service._music_engine("acestep").alive()


@pytest.mark.asyncio
async def test_stop_is_refused_while_a_job_uses_the_engine_unless_forced(tmp_path, monkeypatch):
    free_env(monkeypatch, "slow_synth")
    home = tmp_path / "dpc"
    install_fake_engine(home)
    service = service_at(home, monkeypatch)
    monkeypatch.setattr(commands, "MusicJobRunner", _runner_with_fake_flac(commands.MusicJobRunner))
    try:
        await service.start_music_jobs()
        job_id = (await service.propose_music_job(type="generate_from_text", params=from_text()))["job"]["id"]
        runner = service._music_runner_instance
        await wait_until(lambda: runner.current_job_id == job_id and service._music_engine("acestep").alive())
        refused = await service.stop_music_engine(engine="acestep")
        assert refused["status"] == "error" and refused["code"] == "engine_busy"
        assert job_id in refused["message"]
        cancelled = await service.cancel_music_job(job_id=job_id)
        assert cancelled["job"]["state"] == "cancelled"
        await wait_until(lambda: runner.current_job_id is None)
        assert (await service.stop_music_engine(engine="acestep"))["status"] == "ok"
    finally:
        await service.stop_music_jobs()
        await service.stop_music_library()


def _runner_with_fake_flac(original):
    class Runner(original):
        def __init__(self, *a, **kw):
            kw.setdefault("converter", fake_flac)
            kw.setdefault("card_poll_s", 0.05)
            kw.setdefault("engine_poll_s", 0.02)
            super().__init__(*a, **kw)
    return Runner
