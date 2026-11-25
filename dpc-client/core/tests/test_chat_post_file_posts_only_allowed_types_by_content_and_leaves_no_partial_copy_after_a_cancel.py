"""chat_post_file, follow-up 13, items 2 and 3 (Zcode's review of follow-up 12, P3).

- Only an allow-list is posted, checked by the file's first bytes as well as its name: audio (flac, wav,
  mp3, ogg, m4a), image (png, jpg, webp, gif), pdf, plain text (txt, md, json, csv). A program renamed
  to an allowed name, a name off the list, and a name that says one group over content of another are
  refused with the list in the message, and nothing is copied. The copy is read again after it is
  made, so a source swapped between the check and the copy is refused too.
- A copy that is cancelled or fails part way (the hashing and the ffprobe measure come after the
  bytes are copied) leaves no half file in the chat's `files/`; a file that was already there is
  never removed.

The tool's `post_file` runs on a real `ToolContext` and a real files folder; only ffprobe is faked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.dpc_agent.tools.registry import ToolContext

CID = "agent_poster_7c1e0d42"

FLAC = b"fLaC\x00\x00\x00\x22" + b"A" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
MP3_TAGGED = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 64
OGG = b"OggS\x00\x02" + b"\x00" * 64
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"1 0 obj\n" * 8
TEXT = "Привет, мир: a plain note\nwith two lines, and ünïcode.\n".encode("utf-8")
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / ".dpc" / "agents" / CID
    (root / "outputs").mkdir(parents=True)
    files = home / ".dpc" / "conversations" / CID / "files"
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", home / ".dpc")
    monkeypatch.setattr(service_mod, "measure_recording_seconds", lambda path: 12.5)
    monkeypatch.setattr(chat_files, "_chat_files_dir", lambda ctx, conversation_id: files)
    ctx = ToolContext(agent_root=root, turn_class="owner", message_source="ui", conversation_id=CID)

    class World:
        pass

    w = World()
    w.ctx, w.root, w.files = ctx, root, files
    w.put = lambda name, data: _put(root / "outputs" / name, data)
    w.post = lambda path: chat_files.post_file(ctx, path)
    w.in_files = lambda: sorted(p.name for p in files.iterdir()) if files.is_dir() else []
    return w


def _put(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# -- item 2: the allow-list, by name and by content ------------------------------------------------


@pytest.mark.parametrize("name,data", [
    ("take.flac", FLAC), ("take.wav", WAV), ("a.mp3", MP3_TAGGED), ("b.mp3", MP3_FRAME), ("c.ogg", OGG),
    ("d.m4a", M4A), ("e.png", PNG), ("f.jpg", JPG), ("g.JPEG", JPG), ("h.webp", WEBP), ("i.gif", GIF),
    ("j.pdf", PDF), ("k.txt", TEXT), ("l.md", TEXT), ("m.json", b'{"a": "\xd0\x96"}'), ("n.csv", TEXT),
    ("o.txt", b"\xef\xbb\xbf" + TEXT), ("p.txt", b"\xff\xfe" + "hi there".encode("utf-16-le")),
])
def test_an_allowed_type_with_matching_content_is_posted(world, name, data):
    out = world.post(world.put(name, data))
    assert out.startswith(f"Posted {name}"), out
    assert world.in_files() == [name]


@pytest.mark.parametrize("name,data,looks", [
    ("song.flac", EXE, "a program"), ("notes.txt", EXE, "a program"), ("pic.png", EXE, "a program"),
    ("doc.pdf", ELF, "a program"), ("tune.mp3", ELF, "a program"), ("page.md", b"#!/bin/sh\nrm -rf x\n", "a program"),
    ("a.json", b"\x00\x01\x02binary", "not text"), ("b.csv", b"a,b\n\x00\x00", "not text"),
    ("c.wav", b"just words", "not audio"), ("d.gif", FLAC, "audio data"),
])
def test_a_program_or_a_wrong_content_under_an_allowed_name_is_refused_and_nothing_is_copied(
        world, name, data, looks):
    out = world.post(world.put(name, data))
    assert out.startswith("⚠️ chat_post_file refused:"), out
    assert f"the file is named {name} but its content is" in out and "content must match the name" in out, out
    assert "audio (flac, wav, mp3, ogg, m4a)" in out and out.endswith("Nothing was copied.")
    assert world.in_files() == []


def test_a_file_of_another_group_than_its_name_says_is_refused(world):
    out = world.post(world.put("notes.txt", PNG))
    assert "its content is image data, not text" in out, out
    assert world.in_files() == []


@pytest.mark.parametrize("name", ["setup.exe", "run.bat", "x.zip", "a.docx", "archive.tar.gz", "noext", "photo.bmp"])
def test_a_name_off_the_list_is_refused_with_the_list_in_the_message(world, name):
    out = world.post(world.put(name, FLAC))
    assert "is not a type that may be posted" in out, out
    assert "plain text (txt, md, json, csv)" in out and "image (png, jpg, webp, gif), pdf" in out, out
    assert world.in_files() == []


def test_a_name_with_trailing_dots_and_spaces_is_judged_by_its_real_suffix(world):
    try:
        path = world.put("take.flac. ", FLAC)
    except OSError:
        pytest.skip("this file system does not keep a name with a trailing dot and space")
    out = world.post(path)
    assert out.startswith("Posted take.flac"), out


def test_a_source_swapped_for_a_program_after_the_check_is_refused_after_the_copy_and_removed(world, monkeypatch):
    source = world.put("take.flac", FLAC)
    real = service_mod.keep_agent_chat_attachment

    def swap_then_keep(files_dir, attachment, **kw):
        source.write_bytes(EXE)  # between the tool's check and the copy
        return real(files_dir, attachment, **kw)

    monkeypatch.setattr(service_mod, "keep_agent_chat_attachment", swap_then_keep)
    out = world.post(source)
    assert "its content is a program" in out, out
    assert world.in_files() == [], "the program's copy was left in the chat's files"
    assert world.ctx.posted_attachments == [] and world.ctx.posted_new_copies == []


def test_the_tool_description_names_the_allowed_types():
    [entry] = chat_files.get_tools()
    text = entry.schema["description"]
    assert "checked by their content as well as their name" in text
    assert "audio (flac, wav, mp3, ogg, m4a)" in text and "pdf" in text


def test_the_deny_list_is_gone():
    assert not hasattr(chat_files, "REFUSED_EXTENSIONS") and not hasattr(chat_files, "refused_extension")


# -- item 3: no partial copy after a cancel -------------------------------------------------------


def test_a_cancel_after_the_bytes_were_copied_leaves_no_half_file(world, monkeypatch):
    def cancelled(path):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service_mod, "measure_recording_seconds", cancelled)
    with pytest.raises(asyncio.CancelledError):
        world.post(world.put("take.flac", FLAC))
    assert world.in_files() == []
    assert world.ctx.posted_attachments == [] and world.ctx.posted_new_copies == []


def test_a_failure_while_hashing_leaves_no_half_file_and_the_error_is_raised(world, monkeypatch):
    calls = {"n": 0}
    real = service_mod.file_sha256

    def fails_on_the_copy(path):
        calls["n"] += 1
        if calls["n"] == 2:  # the first hash is the source's, the second the kept copy's
            raise OSError("disk went away")
        return real(path)

    monkeypatch.setattr(service_mod, "file_sha256", fails_on_the_copy)
    with pytest.raises(OSError):
        world.post(world.put("take.flac", FLAC))
    assert world.in_files() == []


def test_a_cancel_never_removes_a_file_that_was_already_there_nor_another_name(world, monkeypatch):
    world.files.mkdir(parents=True)
    (world.files / "take.flac").write_bytes(FLAC + b"older, other bytes")
    (world.files / "other.flac").write_bytes(FLAC)
    monkeypatch.setattr(service_mod, "measure_recording_seconds",
                        lambda path: (_ for _ in ()).throw(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        world.post(world.put("take.flac", FLAC))  # lands as take_1.flac and is removed again
    assert world.in_files() == ["other.flac", "take.flac"]
    assert (world.files / "take.flac").read_bytes().endswith(b"older, other bytes")


def test_a_cancel_over_a_reused_copy_keeps_the_copy_that_was_there(world, monkeypatch):
    world.files.mkdir(parents=True)
    (world.files / "take.flac").write_bytes(FLAC)  # the same bytes: reused, not copied
    monkeypatch.setattr(service_mod, "measure_recording_seconds",
                        lambda path: (_ for _ in ()).throw(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        world.post(world.put("take.flac", FLAC))
    assert world.in_files() == ["take.flac"]


def test_discard_partial_copy_removes_only_the_free_name_the_copy_claimed(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    for name in ("take.flac", "take_1.flac", "take_2.flac", "take_x.flac", "other.flac", "take.txt"):
        (folder / name).write_bytes(b"x")
    removed = chat_files.discard_partial_copy(folder, {"take.flac", "take_1.flac"}, "take.flac")
    assert removed == 1
    assert sorted(p.name for p in folder.iterdir()) == [
        "other.flac", "take.flac", "take.txt", "take_1.flac", "take_x.flac"]
    assert chat_files.discard_partial_copy(tmp_path / "missing", set(), "take.flac") == 0
