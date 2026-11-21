"""ADR-051 D35 (SPEC §12 decision 40): only one music engine is on the card at a time.

Before a music engine starts, an idle other one is stopped (`swap`); a busy one (a job holds
it) is never stopped: the runner waits for it, a start by hand is refused with
`engine_busy`. The engine commands take an optional `engine` and default to yue2. Both
servers are real child processes (`_fake_yue2_server.py`, `_fake_acestep_server.py`).
"""

import asyncio

import pytest

from dpc_client_core.music import commands
from dpc_client_core.music.engine_base import MusicEngineError
from tests._acestep_helpers import FakeAceStepServer, fake_flac, wait_until
from tests._music_job_commands_helpers import Service, without_tools
from tests._yue2_helpers import DualRig, FakeYue2Server, install_both, yue2_env, yue2_params


@pytest.mark.asyncio
async def test_an_idle_acestep_server_is_stopped_before_a_yue2_job_starts(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        await rig.ace.start()
        job = rig.propose(yue2_params(variants=1))
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert not rig.ace.alive() and rig.ace._last_exit["why"] == "swap"
        assert "swap" in rig.ace.status().reason
        assert rig.yue2.alive()

        # And back: an acestep job stops the idle yue2 server.
        ace_job = rig.propose(yue2_params(engine="acestep", duration_s=30, variants=1))
        ace_job = await rig.wait_for(ace_job["id"], ("review", "failed"))
        assert ace_job["state"] == "review", ace_job["state_reason"]
        assert not rig.yue2.alive() and rig.yue2._last_exit["why"] == "swap"


@pytest.mark.asyncio
async def test_a_busy_engine_is_waited_for_never_stopped(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        await rig.ace.start()
        release = asyncio.Event()

        async def acestep_job():
            async with rig.ace.in_use():
                await release.wait()
        holder = asyncio.ensure_future(acestep_job())
        await wait_until(lambda: rig.ace.busy)
        job = rig.propose(yue2_params(variants=1))
        await wait_until(lambda: rig.store.get_job(job["id"])["state"] == "running")
        await asyncio.sleep(0.4)
        assert rig.ace.alive() and not rig.yue2.alive()   # waiting, nothing stopped
        assert rig.store.get_job(job["id"])["state"] == "running"
        with pytest.raises(MusicEngineError) as err:
            async with rig.engines.exclusive("yue2", wait=False):
                pass
        assert err.value.code == "engine_busy" and "acestep.cpp" in err.value.message
        release.set()
        await holder
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert not rig.ace.alive() and rig.yue2.alive()


@pytest.mark.asyncio
async def test_a_job_cancelled_while_waiting_for_the_other_engine_ends_cancelled(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        await rig.ace.start()
        release = asyncio.Event()

        async def acestep_job():
            async with rig.ace.in_use():
                await release.wait()
        holder = asyncio.ensure_future(acestep_job())
        await wait_until(lambda: rig.ace.busy)
        job = rig.propose(yue2_params(variants=1))
        await wait_until(lambda: rig.store.get_job(job["id"])["state"] == "running")
        rig.store.cancel_job(job["id"])
        await wait_until(lambda: rig.runner.current_job_id is None)
        assert rig.store.get_job(job["id"])["state"] == "cancelled"
        assert rig.ace.alive() and not rig.yue2.alive()
        release.set()
        await holder


def service_at(home, monkeypatch):
    without_tools(monkeypatch)
    monkeypatch.setattr(commands, "AceStepServer", FakeAceStepServer)
    monkeypatch.setattr(commands, "Yue2Server", FakeYue2Server)
    service = Service(home)
    service.settings.get = lambda section, key, fallback=None: "0" if key in ("acestep_port", "yue2_port") else fallback
    return service


@pytest.mark.asyncio
async def test_the_engine_commands_default_to_yue2_and_switch_engines(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    service = service_at(home, monkeypatch)
    try:
        status = await service.get_music_engine_status()
        assert status["status"] == "ok" and status["default_engine"] == "yue2"
        assert status["engine"]["key"] == "yue2" and status["engine"]["state"] == "stopped"
        assert set(status["engines"]) == {"yue2", "acestep"}
        ace = await service.get_music_engine_status(engine="acestep")
        assert ace["engine"]["key"] == "acestep"
        bad = await service.get_music_engine_status(engine="suno")
        assert bad["status"] == "error" and bad["code"] == "invalid_engine"

        started = await service.start_music_engine()
        assert started["status"] == "ok", started
        assert started["engine"]["key"] == "yue2" and started["engine"]["state"] == "running"
        started = await service.start_music_engine(engine="acestep")
        assert started["status"] == "ok", started
        assert not service._music_engine("yue2").alive()      # stopped for the swap
        refused = await service.start_music_engine(engine="acestep", backbone="rs-tw")
        assert refused["status"] == "error" and refused["code"] == "invalid_params"
        stopped = await service.stop_music_engine(engine="acestep")
        assert stopped["status"] == "ok" and stopped["stopped"] is True
        caps = (await service.get_music_capabilities())["capabilities"]
        assert caps["default_engine"] == "yue2" and set(caps["engines"]) == {"yue2", "acestep"}
        assert caps["engines"]["yue2"]["adapters"]
    finally:
        await service.stop_music_jobs()
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_start_by_hand_is_refused_while_the_other_engine_runs_a_job(tmp_path, monkeypatch):
    yue2_env(monkeypatch, "slow_synth")
    home = tmp_path / "dpc"
    install_both(home)
    service = service_at(home, monkeypatch)
    original = commands.MusicJobRunner

    class Runner(original):
        def __init__(self, *a, **kw):
            kw.update(converter=fake_flac, card_holders=lambda: [], card_poll_s=0.05, engine_poll_s=0.02)
            super().__init__(*a, **kw)
    monkeypatch.setattr(commands, "MusicJobRunner", Runner)
    try:
        await service.start_music_jobs()
        proposed = await service.propose_music_job(type="generate_from_text", params=yue2_params())
        job_id = proposed["job"]["id"]
        assert proposed["job"]["params"]["engine"] == "yue2"
        runner = service._music_runner_instance
        await wait_until(lambda: runner.current_engine == "yue2" and service._music_engine("yue2").alive())
        refused = await service.start_music_engine(engine="acestep")
        assert refused["status"] == "error" and refused["code"] == "engine_busy"
        refused = await service.stop_music_engine()
        assert refused["status"] == "error" and refused["code"] == "engine_busy"
        assert (await service.stop_music_engine(engine="acestep"))["status"] == "ok"   # not the busy one
        await service.cancel_music_job(job_id=job_id)
        await wait_until(lambda: runner.current_job_id is None)
    finally:
        await service.stop_music_jobs()
        await service.stop_music_library()
