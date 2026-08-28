"""Two agents' sleep must not run at once on one device.

2026-08-19: three Sleep buttons pressed together fired three independent
`asyncio.create_task` pipelines against one llama-server, and their
92-170K-token consolidation prompts summed past the unified 262 144-token
KV pool two at a time — the server killed both jobs in each of three
waves ("Context size has been exceeded") and two agents lost their briefs.
The queue lives inside `run_sleep` itself, not in the callers, because
there are two of them (agent sleep and group sleep) and a third would
have to remember the rule too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent.sleep_pipeline import run_sleep


class _Manager:
    """Stand-in never reached on the no-digests path; the shape is what matters."""

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return 1


def _agent_dir(tmp_path: Path) -> Path:
    conv = tmp_path / "agent_001"
    (conv / "archive").mkdir(parents=True)
    return conv


@pytest.fixture(autouse=True)
def _fresh_lock():
    # A module-level asyncio.Lock binds itself to the first loop that uses it,
    # and pytest-asyncio gives every test a fresh loop — without this fixture
    # the second test dies on "bound to a different event loop". In production
    # the backend runs one loop for its whole life, so the binding is stable.
    sleep_pipeline._SLEEP_PIPELINE_LOCK = asyncio.Lock()


@pytest.mark.asyncio
async def test_a_second_sleep_waits_until_the_running_one_releases(tmp_path):
    conv = _agent_dir(tmp_path)
    await sleep_pipeline._SLEEP_PIPELINE_LOCK.acquire()  # a running pipeline holds it
    try:
        second = asyncio.create_task(run_sleep(conv, _Manager(), agent_id="agent_001"))
        await asyncio.sleep(0.05)
        assert not second.done(), "second sleep must not run while the first holds the lock"
    finally:
        sleep_pipeline._SLEEP_PIPELINE_LOCK.release()
    result = await second
    assert result["status"] == "no_new_sessions"  # waited, then ran and finished


@pytest.mark.asyncio
async def test_a_queued_sleep_reports_the_queue_before_running(tmp_path):
    conv = _agent_dir(tmp_path)
    phases: list = []

    async def _progress(current, total, phase, archive_file):
        phases.append(phase)

    await sleep_pipeline._SLEEP_PIPELINE_LOCK.acquire()
    try:
        second = asyncio.create_task(
            run_sleep(conv, _Manager(), agent_id="agent_001", progress_callback=_progress)
        )
        await asyncio.sleep(0.05)
        assert "queued" in phases, "the waiting agent must tell the UI it is queued"
    finally:
        sleep_pipeline._SLEEP_PIPELINE_LOCK.release()
    await second


@pytest.mark.asyncio
async def test_the_lock_is_free_again_after_a_pipeline_ends(tmp_path):
    conv = _agent_dir(tmp_path)
    await run_sleep(conv, _Manager(), agent_id="agent_001")
    assert not sleep_pipeline._SLEEP_PIPELINE_LOCK.locked()
