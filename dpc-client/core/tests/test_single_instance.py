"""Tests for the backend single-instance guard."""

import os

from dpc_client_core import single_instance


def test_no_lock_file_is_free(tmp_path):
    lock = tmp_path / "run_service.pid"
    assert single_instance.find_running_instance(lock, lambda pid: True) is None


def test_stale_lock_dead_pid_is_free(tmp_path):
    lock = tmp_path / "run_service.pid"
    lock.write_text("99999")
    # is_backend returns False → pid is dead / not a backend → free
    assert single_instance.find_running_instance(lock, lambda pid: False) is None


def test_live_other_backend_blocks(tmp_path):
    lock = tmp_path / "run_service.pid"
    lock.write_text("4242")
    # foreign live backend pid → returned so the caller refuses to start
    got = single_instance.find_running_instance(lock, lambda pid: True, own_pid=1)
    assert got == 4242


def test_own_pid_is_not_a_conflict(tmp_path):
    lock = tmp_path / "run_service.pid"
    lock.write_text(str(os.getpid()))
    assert single_instance.find_running_instance(lock, lambda pid: True) is None


def test_garbage_lock_is_free(tmp_path):
    lock = tmp_path / "run_service.pid"
    lock.write_text("not-a-pid")
    assert single_instance.find_running_instance(lock, lambda pid: True) is None


def test_acquire_then_release_roundtrip(tmp_path):
    lock = tmp_path / "run_service.pid"
    single_instance.acquire(lock)
    assert lock.exists() and lock.read_text().strip() == str(os.getpid())
    single_instance.release(lock)
    assert not lock.exists()
