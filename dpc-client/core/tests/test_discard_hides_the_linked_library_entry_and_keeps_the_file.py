"""SPEC decision 35: Discard removes the library entry (hides it), the file stays, and
a Discard needs a reason. A hide that fails does not undo the take's decision.
"""

import pytest

from tests._music_job_commands_helpers import library_tracks, review_job, service_with_engine, without_tools


@pytest.mark.asyncio
async def test_discard_without_a_reason_answers_reason_required_and_changes_nothing(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "dpc")
    ids = await library_tracks(service, tmp_path / "Music")
    _, takes = review_job(service._music_jobs_instance, [ids["a.mp3"], ids["b.mp3"]])
    answer = await service.discard_music_take(take_id=takes[0]["id"])
    assert answer["status"] == "error" and answer["code"] == "reason_required"
    assert service._music_jobs_instance.get_take(takes[0]["id"])["decision"] == "pending"
    entries = {e["id"]: e for e in (await service.list_music_library())["entries"]}
    assert not entries[ids["a.mp3"]].get("hidden")
    await service.stop_music_library()


@pytest.mark.asyncio
async def test_discard_hides_the_linked_entry_and_keeps_the_file(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "dpc")
    ids = await library_tracks(service, tmp_path / "Music")
    _, takes = review_job(service._music_jobs_instance, [ids["a.mp3"], ids["b.mp3"]])
    answer = await service.discard_music_take(take_id=takes[0]["id"], reason_text="muddy")
    assert answer["status"] == "ok" and answer["library_hidden"] is True
    entries = {e["id"]: e for e in (await service.list_music_library())["entries"]}
    assert entries[ids["a.mp3"]]["hidden"] is True
    assert not entries[ids["b.mp3"]].get("hidden")
    assert (tmp_path / "Music" / "a.mp3").exists()
    await service.stop_music_library()


@pytest.mark.asyncio
async def test_keep_does_not_touch_the_library(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "dpc")
    ids = await library_tracks(service, tmp_path / "Music")
    _, takes = review_job(service._music_jobs_instance, [ids["a.mp3"], ids["b.mp3"]])
    assert (await service.keep_music_take(take_id=takes[0]["id"]))["status"] == "ok"
    entries = (await service.list_music_library())["entries"]
    assert not any(e.get("hidden") for e in entries)
    await service.stop_music_library()


@pytest.mark.asyncio
async def test_reject_all_hides_every_rejected_take_and_keeps_the_files(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "dpc")
    ids = await library_tracks(service, tmp_path / "Music")
    job, _ = review_job(service._music_jobs_instance, [ids["a.mp3"], ids["b.mp3"]])
    answer = await service.reject_all_music_takes(job_id=job["id"], reason_text="none fit")
    assert answer["status"] == "ok"
    assert [t["library_hidden"] for t in answer["takes"]] == [True, True]
    entries = (await service.list_music_library())["entries"]
    assert all(e["hidden"] for e in entries)
    assert (tmp_path / "Music" / "a.mp3").exists() and (tmp_path / "Music" / "b.mp3").exists()
    await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_hide_that_cannot_happen_leaves_the_decision_standing_with_a_reason(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "dpc")
    # one take points at an entry the library does not know, one has no entry at all
    _, takes = review_job(service._music_jobs_instance, ["trk_gone", None])
    first = await service.discard_music_take(take_id=takes[0]["id"], reason_text="bad")
    assert first["status"] == "ok" and first["take"]["decision"] == "rejected"
    assert first["library_hidden"] is False and first["library_reason"]
    second = await service.discard_music_take(take_id=takes[1]["id"], reason_text="bad")
    assert second["status"] == "ok" and second["library_hidden"] is False and second["library_reason"]
    await service.stop_music_library()
