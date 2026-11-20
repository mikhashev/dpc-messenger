"""The service builds one job store, opened once even under concurrent first calls; with
no engine adapter built the capabilities say so and a proposed job is `unsupported`.
"""

import asyncio

import pytest

from dpc_client_core.music import commands
from tests._music_job_commands_helpers import Service
from tests._music_jobs_helpers import from_text_params


@pytest.mark.asyncio
async def test_the_store_opens_once_under_concurrent_first_calls(tmp_path, monkeypatch):
    opens = []
    original = commands.MusicJobStore.open

    def counting_open(self):
        opens.append(1)
        original(self)
    monkeypatch.setattr(commands.MusicJobStore, "open", counting_open)
    service = Service(tmp_path / "dpc")
    answers = await asyncio.gather(*(service.list_music_jobs() for _ in range(8)),
                                   service.get_music_capabilities())
    assert all(a["status"] == "ok" for a in answers)
    assert len(opens) == 1


@pytest.mark.asyncio
async def test_today_the_engine_is_not_built_and_a_proposed_job_is_unsupported(tmp_path):
    service = Service(tmp_path / "dpc")
    caps = (await service.get_music_capabilities())["capabilities"]
    assert caps["engine"]["state"] == "not_built"
    proposed = await service.propose_music_job(type="generate_from_text", params=from_text_params())
    assert proposed["status"] == "ok"
    job = proposed["job"]
    assert job["state"] == "unsupported"
    assert "engine adapter not built" in str(job)
