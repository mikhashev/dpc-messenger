"""`get_card_lease_status` (Mike, 2026-11-25): the Agent map's read of the card lease's queue.

A read-only `local_api` command on `CoreService` (through `CardLeaseCommandsMixin`), on the UI
allowlist, answering `queue_snapshot()` with `status: "success"`. The service starts the publisher
that writes `<dpc home>/state/card_lease.json` and pushes `card_lease_updated`, and stops it with
the backend.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.card_lease_commands import CardLeaseCommandsMixin
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import card_lease

from tests._card_lease_helpers import BOUND, fast, whisper  # noqa: F401


def _service(tmp_path, pushed=None):
    async def broadcast_event(name, payload):
        if pushed is not None:
            pushed.append((name, payload))

    class Service(CardLeaseCommandsMixin):
        pass

    svc = Service()
    svc.settings = SimpleNamespace(dpc_home_dir=tmp_path / ".dpc")
    svc.local_api = SimpleNamespace(broadcast_event=broadcast_event)
    return svc


def test_the_command_is_on_the_ui_allowlist_and_on_the_core_service():
    from dpc_client_core.service import CoreService

    assert "get_card_lease_status" in ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(CoreService.get_card_lease_status)
    assert not getattr(CoreService.get_card_lease_status, "dpc_slow", False), "it reads memory, it waits for nothing"


@pytest.mark.asyncio
async def test_it_answers_the_snapshot_with_a_success_status(tmp_path):
    svc = _service(tmp_path)

    free = await svc.get_card_lease_status()
    assert free["status"] == "success" and free["free"] is True and free["waiters"] == []

    w = whisper()
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
    held = await svc.get_card_lease_status()

    assert held["holder"]["client"] == "whisper:turbo" and held["free"] is False
    assert {k: v for k, v in held.items() if k not in ("status", "updated_at", "updated_at_iso")} == {
        k: v for k, v in card_lease.queue_snapshot().items() if k not in ("updated_at", "updated_at_iso")}
    json.dumps(held)


@pytest.mark.asyncio
async def test_a_snapshot_that_fails_is_an_error_answer_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(card_lease, "queue_snapshot", lambda: 1 / 0)

    answer = await _service(tmp_path).get_card_lease_status()

    assert answer["status"] == "error" and "could not be read" in answer["message"]


@pytest.mark.asyncio
async def test_the_service_starts_the_file_and_the_push_and_stops_them(tmp_path):
    pushed = []
    svc = _service(tmp_path, pushed)

    svc._start_card_lease_state()
    publisher = svc._card_lease_publisher
    assert publisher is not None
    await asyncio.sleep(0.2)
    await publisher.flush()
    path = tmp_path / ".dpc" / "state" / "card_lease.json"
    assert json.loads(path.read_text(encoding="utf-8"))["free"] is True
    assert [name for name, _ in pushed] == ["card_lease_updated"]

    svc._stop_card_lease_state()

    assert svc._card_lease_publisher is None
    assert json.loads(path.read_text(encoding="utf-8"))["backend_stopped"] is True
    svc._stop_card_lease_state()  # twice is harmless


@pytest.mark.asyncio
async def test_a_failure_to_start_is_logged_and_leaves_the_backend_starting(tmp_path):
    svc = _service(tmp_path)
    svc.settings = None  # no home to read

    svc._start_card_lease_state()

    assert svc._card_lease_publisher is None
