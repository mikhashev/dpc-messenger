"""chat_post_file, music tools follow-up 12, items 3, 5 and 6 (Zcode #68 P2, P3, surviving mutants).

- A take is posted by its id only when its job is done (review or accepted), the take is not
  discarded and its library entry is not hidden: a take of a running or cancelled job, a
  discarded take and a take whose library entry is hidden are refused with the reason.
- A file that runs or opens something when clicked is never posted (follow-up 12's deny-list; follow-up 13
  made it an allow-list by name and content, pinned in the follow-up 13 test of chat_post_file).
- A failed turn leaves no copy behind in the chat's files/: a loop that raises, and a record
  that is not written.
- The DPC-home rule holds for more than the token: the config, another agent's folder and
  another chat's files/, with an extended path over the whole DPC home; and a home that cannot
  be resolved refuses (fail closed).
- An answer with an empty response and a posted file is recorded, with no "empty" toast.

Live path: the real `DpcAgentManager.process_message` turn over a real `DpcAgent`, its loop,
registry and firewall, a real `ConversationMonitor` on disk, and for takes the real music
command mixin over a real job store, runner and library (the world of the part 3 tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.music import job_schemas as js
from tests._acestep_helpers import Rig, from_text
from tests.test_chat_post_file_copies_a_file_into_the_agents_own_chat_and_the_answer_carries_it import (
    CID, FLAC, _a_take_in_the_library, _music_world, _take, _tool_results, _World, home, music_home,  # noqa: F401
)


# -- item 3: only a finished take the person kept ------------------------------------------------


def _take_of_a_running_job(home, rig):
    """A take recorded while its job runs, through the store's own transitions."""
    from dpc_client_core.music.runner import LM_PAYLOAD
    job = rig.store.create_job("generate_from_text", from_text(variants=2))
    rig.store.write_payload(job["id"], LM_PAYLOAD, b"{}", endpoint="/lm")
    rig.store.start_job(job["id"])
    audio = home / ".dpc" / "music" / "generated" / "partial.flac"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(FLAC)
    take = rig.store.add_take(job["id"], variant=1, file_uri=audio.as_uri(), content_hash=None, title="t",
                              seed=1, synth_batch_size=2, synth_batch_index=0, payload=LM_PAYLOAD)
    return job, take


@pytest.mark.asyncio
async def test_a_take_of_a_running_or_cancelled_job_is_refused(tmp_path, music_home, monkeypatch):
    async with Rig(music_home / ".dpc", start_runner=False) as rig:
        job, take = _take_of_a_running_job(music_home, rig)
        w = _music_world(tmp_path, music_home, monkeypatch, rig)
        await w.turn([{"take_id": take["id"]}])
        assert f"take '{take['id']}' belongs to a job that is running" in _tool_results(w)
        rig.store.cancel_job(job["id"])
        await w.turn([{"take_id": take["id"]}])
        assert f"take '{take['id']}' belongs to a job that is cancelled" in _tool_results(w)
    assert w.files() == []
    assert all("attachments" not in a for a in w.answers())


@pytest.mark.asyncio
async def test_a_discarded_take_and_a_take_whose_library_entry_is_hidden_are_refused(
    tmp_path, music_home, monkeypatch,
):
    async with Rig(music_home / ".dpc") as rig:
        take = await _a_take_in_the_library(music_home, monkeypatch, rig)
        w = _music_world(tmp_path, music_home, monkeypatch, rig)
        await rig.library.set_hidden(take["library_track_id"], True)
        await w.turn([{"take_id": take["id"]}])
        assert "its library entry is hidden (discarded)" in _tool_results(w)

        await rig.library.set_hidden(take["library_track_id"], False)
        answer = await w.agent._service.discard_music_take(take_id=take["id"], reason_text="not this one")
        assert answer["status"] == "ok", answer
        await w.turn([{"take_id": take["id"]}])
        out = _tool_results(w)
        assert "belongs to a job that is rejected" in out or "was discarded" in out, out
    assert w.files() == []


def test_the_postable_job_states_are_the_finished_ones():
    assert chat_files.POSTABLE_TAKE_JOB_STATES == {js.REVIEW, js.ACCEPTED}


# -- item 5: files that run, a failed turn's copies, a home that cannot be resolved ------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "link.lnk", "script.ps1", "go.sh", "a.cmd. ",
                                  "x.msi", "y.vbs", "z.scr", "w.com", "App.app", "img.dmg"])
async def test_a_file_that_runs_when_clicked_is_never_posted(tmp_path, home, monkeypatch, name):
    w = _World(tmp_path, home, monkeypatch)
    f = w.agent.agent_root / "outputs" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        f.write_bytes(b"MZ not really")
    except OSError:
        pytest.skip(f"this file system does not keep the name {name!r}")
    await w.turn([{"path": str(f)}])
    assert "is not a type that may be posted" in _tool_results(w), _tool_results(w)[-800:]
    assert w.all_chat_files() == []


def test_the_allowed_suffixes_are_one_table_compared_lower_case():
    assert chat_files._suffix_of("Take.FLAC") == ".flac"
    assert chat_files._suffix_of("a.png. ") == ".png"
    assert chat_files._suffix_of("notes.sh.txt") == ".txt"
    assert ".exe" not in chat_files.ALLOWED_SUFFIXES and ".flac" in chat_files.ALLOWED_SUFFIXES


class _Boom(BaseException):
    """A failure the loop does not turn into an answer: the turn raises."""


@pytest.mark.asyncio
async def test_a_turn_whose_loop_raises_leaves_no_copy_in_files(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    real = w.provider.generate_with_tools

    async def boom_after_the_call(messages, tools, **kw):
        if w.provider._round == 1:
            assert w.files() == ["take.flac"], "the copy was made before the failure"
            raise _Boom("the provider went away")
        return await real(messages, tools, **kw)

    monkeypatch.setattr(w.provider, "generate_with_tools", boom_after_the_call)
    with pytest.raises(_Boom):
        await w.turn([{"path": str(_take(home))}])
    assert w.files() == [], "a failed turn left its copy with no record"


@pytest.mark.asyncio
async def test_a_record_that_is_not_written_takes_the_copy_with_it(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)

    def no_record(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(w.manager, "_record_answer", no_record)
    with pytest.raises(OSError):
        await w.turn([{"path": str(_take(home))}])
    assert w.files() == []


@pytest.mark.asyncio
async def test_a_file_already_in_the_chat_is_not_removed_by_a_failed_turn(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    take = _take(home)
    await w.turn([{"path": str(take)}])
    assert w.files() == ["take.flac"]

    def no_record(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(w.manager, "_record_answer", no_record)
    with pytest.raises(OSError):
        await w.turn([{"path": str(take)}])  # the same bytes: the existing copy is reused
    assert w.files() == ["take.flac"], "the earlier record's file was removed"


def test_a_dpc_home_that_cannot_be_resolved_refuses(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def unresolvable():
        raise OSError("the home is on a drive that went away")

    monkeypatch.setattr(chat_files, "_dpc_home", unresolvable)
    ctx = SimpleNamespace(agent_root=tmp_path / "agents" / CID)
    out = chat_files._dpc_home_refusal(ctx, tmp_path / "anything.flac")
    assert out is not None and "could not be resolved" in out and "Nothing was copied" in out


# -- item 6: the DPC-home rule has more than one case -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("where", [
    ".ws_token", "config.ini", "agents/agent_someone_else/outputs/mix.flac",
    "conversations/agent_someone_else/files/sent.flac", "conversations/group-not-mine/files/sent.flac",
])
async def test_the_dpc_home_outside_the_agents_own_folder_and_chats_is_never_posted(
    tmp_path, home, monkeypatch, where,
):
    w = _World(tmp_path, home, monkeypatch, read_only=[home / ".dpc"])
    f = home / ".dpc" / where
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"secret-ish")
    await w.turn([{"path": str(f)}])
    out = _tool_results(w)
    assert "inside the DPC home, outside the agent's own directory" in out or "SANDBOX_VIOLATION" in out, out[-800:]
    assert "Posted" not in out
    assert not [p for p in w.all_chat_files() if p.startswith(CID)]


@pytest.mark.asyncio
async def test_the_agents_own_chat_files_are_posted_under_the_same_extension(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch, read_only=[home / ".dpc"])
    own = w.conv_dir() / "files" / "sent.flac"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(FLAC)
    await w.turn([{"path": str(own)}])
    assert "Posted sent.flac" in _tool_results(w)


# -- item 6: an empty response with a posted file -------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_response_with_a_posted_file_is_recorded_and_no_toast_is_raised(
    tmp_path, home, monkeypatch,
):
    w = _World(tmp_path, home, monkeypatch)
    real_process = w.agent.process

    async def empty_answer(*a, **kw):
        # The real turn posts the file; the run then hands back no text at all (the loop's own
        # retries turn a provider's empty text into its stand-in, so this is the only way here).
        await real_process(*a, **kw)
        return ""

    monkeypatch.setattr(w.agent, "process", empty_answer)
    await w.turn([{"path": str(_take(home))}])
    (record,) = w.answers()
    assert record["content"] == "" and [a["filename"] for a in record["attachments"]] == ["take.flac"]
    assert not [p for n, p in w.events if n == "error_toast"]
    assert Path(record["attachments"][0]["file_path"]).is_file()
