"""The card queue's file for CC (Mike, 2026-11-25): `<dpc home>/state/card_lease.json`.

`CardLeaseStatePublisher` follows the lease's changes and writes `queue_snapshot()` there: atomically
(a reader sees the old file or the new one), debounced (at most every `debounce_s`, off the owner
loop, a snapshot that did not change in content is not written again), with `updated_at` and the
backend pid so that a file a crash left behind is recognisable, and once more with
`backend_stopped` when the backend stops. The UI gets the same snapshot as `card_lease_updated`.
Nothing is written until the publisher is started, and nothing under the real home: the paths here
are the test's tmp_path.
"""

import asyncio
import json
import os
import threading

import pytest

from dpc_client_core.managers import card_lease, card_lease_state
from dpc_client_core.managers.card_lease_state import (
    CardLeaseStatePublisher,
    content_key,
    state_file_path,
    write_atomically,
)

from tests._card_lease_helpers import BOUND, fast, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _publisher(tmp_path, pushed=None, **kw):
    async def broadcast(name, payload):
        pushed.append((name, payload))

    kw.setdefault("debounce_s", 0.05)
    kw.setdefault("poll_s", 60.0)
    kw.setdefault("heartbeat_s", 60.0)
    return CardLeaseStatePublisher(asyncio.get_running_loop(), state_file_path(tmp_path),
                                   broadcast if pushed is not None else None, **kw)


class TestThePath:
    def test_is_state_card_lease_json_under_the_dpc_home(self, tmp_path):
        path = state_file_path(tmp_path / ".dpc")
        assert path == tmp_path / ".dpc" / "state" / "card_lease.json"


class TestAtomicWrite:
    def test_writes_json_creates_the_folder_and_leaves_no_temp_file(self, tmp_path):
        path = state_file_path(tmp_path)

        assert write_atomically(path, {"a": 1, "word": "café"}) is True

        assert _read(path) == {"a": 1, "word": "café"}
        assert [p.name for p in path.parent.iterdir()] == ["card_lease.json"]

    def test_a_reader_during_many_writes_never_sees_half_a_file(self, tmp_path):
        path = state_file_path(tmp_path)
        write_atomically(path, {"n": 0, "pad": "x" * 20000})
        stop = threading.Event()
        torn = []

        def reader():
            while not stop.is_set():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    assert len(data["pad"]) == 20000
                except (OSError, PermissionError):
                    continue  # Windows: the replace had the file for a moment
                except Exception as exc:  # a torn or empty file
                    torn.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for n in range(1, 50):
                write_atomically(path, {"n": n, "pad": "x" * 20000})
        finally:
            stop.set()
            thread.join(5)

        assert torn == []
        assert _read(path)["n"] == 49

    def test_a_replace_the_platform_refuses_is_retried_then_reported_not_raised(self, tmp_path, monkeypatch):
        path = state_file_path(tmp_path)
        calls = []

        def refuse(src, dst):
            calls.append(src)
            raise PermissionError("a reader holds it")

        monkeypatch.setattr(os, "replace", refuse)

        assert write_atomically(path, {"a": 1}) is False

        assert len(calls) == card_lease_state._REPLACE_TRIES
        assert list(path.parent.iterdir()) == [], "the temp file is removed"

    def test_an_unwritable_place_is_reported_not_raised(self, tmp_path):
        blocker = tmp_path / "state"
        blocker.write_text("a file where the folder should be", encoding="utf-8")

        assert write_atomically(state_file_path(tmp_path), {"a": 1}) is False


class TestThePublisher:
    @pytest.mark.asyncio
    async def test_writes_the_free_card_at_start_with_pid_and_time(self, tmp_path):
        pub = _publisher(tmp_path)
        pub.start()
        await asyncio.sleep(0.2)
        await pub.flush()

        data = _read(pub.path)
        assert data["free"] is True and data["backend_pid"] == os.getpid() and data["updated_at"] > 0
        assert "backend_stopped" not in data
        pub.stop()

    @pytest.mark.asyncio
    async def test_a_grant_and_a_release_are_written_and_pushed(self, tmp_path):
        pushed = []
        pub = _publisher(tmp_path, pushed)
        pub.start()
        await asyncio.sleep(0.15)
        await pub.flush()
        pushed.clear()
        w = whisper()

        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
        await asyncio.sleep(0.2)
        await pub.flush()
        held = _read(pub.path)
        assert held["holder"]["client"] == "whisper:turbo" and held["holder"]["purpose"] == "dictation"
        assert [name for name, _ in pushed] == ["card_lease_updated"]
        assert pushed[-1][1]["holder"]["client"] == "whisper:turbo"

        w.lease.release("done")
        await asyncio.sleep(0.2)
        await pub.flush()
        free = _read(pub.path)
        assert free["free"] is True and [r["event"] for r in free["recent"]] == ["granted", "released"]
        assert len(pushed) == 2
        pub.stop()

    @pytest.mark.asyncio
    async def test_a_burst_of_changes_is_one_write_per_debounce_not_one_per_change(self, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr(card_lease_state, "write_atomically", lambda path, snap: written.append(snap) or True)
        pub = _publisher(tmp_path, debounce_s=0.3)
        pub.start()
        await asyncio.sleep(0.1)
        await pub.flush()
        written.clear()
        w = whisper()

        for i in range(10):
            await asyncio.wait_for(w.lease.acquire(f"call {i}", wait_s=BOUND), BOUND)
            w.lease.release("done")
        await asyncio.sleep(0.6)
        await pub.flush()

        assert len(written) <= 2, "40 notes of change, at most one write per 0.3 s window"
        assert written[-1]["free"] is True and len(written[-1]["recent"]) == 10, "the last write is the latest state"
        pub.stop()

    @pytest.mark.asyncio
    async def test_a_snapshot_that_did_not_change_is_not_written_again_until_the_heartbeat(self, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr(card_lease_state, "write_atomically", lambda path, snap: written.append(snap) or True)
        pub = _publisher(tmp_path, debounce_s=0.02, poll_s=0.03, heartbeat_s=0.4)
        pub.start()
        await asyncio.sleep(0.25)
        await pub.flush()
        assert len(written) == 1, "polling a card that did not change writes nothing"

        await asyncio.sleep(0.4)
        await pub.flush()
        assert len(written) == 2, "the heartbeat refreshes updated_at"
        assert written[1]["updated_at"] > written[0]["updated_at"]
        pub.stop()

    @pytest.mark.asyncio
    async def test_a_change_from_a_worker_thread_is_noticed(self, tmp_path):
        pub = _publisher(tmp_path)
        pub.start()
        await asyncio.sleep(0.15)
        await pub.flush()
        w = whisper()
        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
        await asyncio.sleep(0.15)
        await pub.flush()

        threading.Thread(target=lambda: w.lease.release("from a thread")).start()
        await asyncio.sleep(0.4)
        await pub.flush()

        assert _read(pub.path)["free"] is True
        pub.stop()

    @pytest.mark.asyncio
    async def test_stop_writes_once_more_as_a_stopped_backend_and_stops_following(self, tmp_path):
        pub = _publisher(tmp_path)
        pub.start()
        await asyncio.sleep(0.15)
        await pub.flush()

        pub.stop()

        final = _read(pub.path)
        assert final["backend_stopped"] is True and final["backend_pid"] == os.getpid()
        assert card_lease._LISTENERS == []
        w = whisper()
        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
        await asyncio.sleep(0.2)
        assert _read(pub.path)["free"] is True, "nothing is written after stop"

    @pytest.mark.asyncio
    async def test_a_failing_listener_or_write_never_breaks_a_grant(self, tmp_path, monkeypatch):
        def boom():
            raise RuntimeError("a listener that fails")

        card_lease.add_change_listener(boom)
        monkeypatch.setattr(card_lease_state, "write_atomically", lambda path, snap: 1 / 0)
        pub = _publisher(tmp_path)
        pub.start()
        w = whisper()

        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
        await asyncio.sleep(0.2)
        await pub.flush()

        assert w.lease.granted
        pub.stop()


class TestTheContentKey:
    def test_ignores_times_and_estimates_and_notices_who_holds_and_waits(self):
        base = {"holders": [{"client": "a", "state": "running", "busy": True, "purpose": "p", "agent_id": None,
                             "conversation_id": None, "beside": None, "since": 1, "eta_s": 30}],
                "waiters": [{"client": "b", "purpose": "q", "where": "busy_holder", "waits_for": ["a"],
                             "wait_known": True, "wait_s": 30, "since": 2}],
                "recent": [], "closed": None, "updated_at": 5}
        later = json.loads(json.dumps(base))
        later["updated_at"] = 99
        later["holders"][0]["eta_s"] = 12
        later["waiters"][0]["wait_s"] = 12
        assert content_key(base) == content_key(later)

        other = json.loads(json.dumps(base))
        other["waiters"][0]["purpose"] = "r"
        assert content_key(base) != content_key(other)


class TestTheFileIsTheOwnersAlone:
    """Follow-up 14 (Zcode #176): 0600 on POSIX, and the folder fsynced after the replace."""

    def test_the_file_is_created_with_owner_only_mode(self, tmp_path, monkeypatch):
        modes = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(".tmp"):
                modes.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", recording_open)
        assert write_atomically(state_file_path(tmp_path), {"a": 1}) is True
        assert modes == [0o600]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_on_posix_the_written_file_is_0600(self, tmp_path):
        path = state_file_path(tmp_path)
        assert write_atomically(path, {"a": 1}) is True
        assert path.stat().st_mode & 0o777 == 0o600

    def test_the_folder_is_synced_after_the_replace(self, tmp_path, monkeypatch):
        order = []
        real_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda s, d: (order.append("replace"), real_replace(s, d)))
        monkeypatch.setattr(card_lease_state, "_fsync_directory",
                            lambda folder: order.append(("fsync", folder)))
        path = state_file_path(tmp_path)
        assert write_atomically(path, {"a": 1}) is True
        assert order == ["replace", ("fsync", path.parent)]

    def test_a_folder_that_cannot_be_synced_is_not_an_error(self, tmp_path):
        card_lease_state._fsync_directory(tmp_path / "missing")  # never raises
