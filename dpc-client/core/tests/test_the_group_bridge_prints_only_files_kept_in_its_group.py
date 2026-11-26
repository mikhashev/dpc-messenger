"""The group bridge prints an attachment's path only when the file is kept in this group's files/.

THE-GROUP-BRIDGE-PRINTS-AN-ATTACHMENTS-FILE-PATH-AS-THE-PEER-SENT-IT:
`attachment_lines` used to print `att["file_path"]` after stripping only CR and LF.
Because `file_path` is outside the signed preimage and `_remap_attachment_paths` keeps
a peer's path until the file arrives, the bridge printed a peer's own path, paths
outside `files/`, traversal or UNC paths, and control characters raw.
"""

import json
import runpy
import sys

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    BRIDGE, GROUP, NODE, bridge,
)


def _msg(attachments, content="look"):
    return {
        "sender_name": "Peer",
        "content": content,
        "timestamp": "2026-11-26T10:15:00",
        "sender_type": "human",
        "sender_node_id": NODE,
        "msg_index": 1,
        "attachments": attachments,
    }


def _att(file_path, filename="clip.wav", kind="file", mime_type="audio/wav"):
    return {
        "type": kind,
        "filename": filename,
        "mime_type": mime_type,
        "size_mb": 0.25,
        "file_path": file_path,
    }


def test_legitimate_file_inside_group_files_dir_prints_at_path(bridge):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    kept = gdir / "files" / "clip.wav"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"RIFF")

    shot = gdir / "files" / "screenshots" / "shot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"PNG")

    lines = bridge.attachment_lines(
        _msg([_att(str(kept)), _att(str(shot), filename="shot.png", kind="image", mime_type="image/png")]),
        group_dir=gdir,
    )

    assert lines == [
        f"[file: clip.wav, audio/wav, 0.25 MB] at {kept}",
        f"[image: shot.png, image/png, 0.25 MB] at {shot}",
    ]


def test_peer_path_and_existing_file_outside_group_files_print_not_kept(bridge, tmp_path):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    (gdir / "files").mkdir(parents=True, exist_ok=True)

    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    sibling_files_old = gdir / "files-old" / "clip.wav"
    sibling_files_old.parent.mkdir(parents=True, exist_ok=True)
    sibling_files_old.write_bytes(b"old")

    other_group_file = bridge.DPC_HOME / "conversations" / "group-other" / "files" / "other.wav"
    other_group_file.parent.mkdir(parents=True, exist_ok=True)
    other_group_file.write_bytes(b"other")

    for bad_path in (
        "/home/user/.dpc/conversations/group-123/files/clip.wav",
        str(outside_file),
        str(sibling_files_old),
        str(other_group_file),
        str(gdir / "files"),  # directory itself is not a file
    ):
        (line,) = bridge.attachment_lines(_msg([_att(bad_path)]), group_dir=gdir)
        assert line == "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node", bad_path


@pytest.mark.parametrize("bad_path", [
    "../secret.txt",
    "../../etc/passwd",
    r"..\secret.txt",
    r"\\evil\share\a.wav",
    "//evil/share/a.wav",
    r"\\?\C:\Windows\System32\drivers\etc\hosts",
    "/\\evil/share/a.wav",
    r"\/evil\share\a.wav",
])
def test_traversal_and_unc_paths_print_not_kept(bridge, tmp_path, bad_path):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    (gdir / "files").mkdir(parents=True, exist_ok=True)
    (gdir / "secret.txt").write_text("secret", encoding="utf-8")

    (line,) = bridge.attachment_lines(_msg([_att(bad_path)]), group_dir=gdir)
    assert line == "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node"


def test_traversal_from_inside_files_dir_prints_not_kept(bridge):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    files_dir = gdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    secret = gdir / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    traversal = str(files_dir / ".." / "secret.txt")
    (line,) = bridge.attachment_lines(_msg([_att(traversal)]), group_dir=gdir)
    assert line == "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node"


def test_control_characters_in_path_and_filename_are_never_printed_raw(bridge):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    files_dir = gdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    kept = files_dir / "safe.wav"
    kept.write_bytes(b"RIFF")

    evil_chars = ["\u2028", "\u2029", "\x1b", "\x00", "\x07", "\x7f", "\x85", "\x9b"]

    # Control characters in filename and mime_type (even when the file itself is kept)
    dirty_filename = "a\u2028b\u2029c\x1b[31md\x00e\x7ff\x9bg.wav"
    dirty_mime = "audio/wav\x1b[0m\u2028\x85"
    (line,) = bridge.attachment_lines(
        _msg([_att(str(kept), filename=dirty_filename, mime_type=dirty_mime)]),
        group_dir=gdir,
    )
    for ch in evil_chars:
        assert ch not in line, repr(ch)
    assert f" at {kept}" in line

    # Control characters in a non-existent / hostile file_path -> refused as not kept
    for ch in evil_chars:
        hostile_path = str(files_dir / f"fake{ch}name.wav")
        (line,) = bridge.attachment_lines(_msg([_att(hostile_path)]), group_dir=gdir)
        assert ch not in line, repr(ch)
        assert line == "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node"


def test_path_unsafe_cleans_printed_path_when_kept_path_contains_brackets(bridge):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    files_dir = gdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    bracketed = files_dir / "take[1].wav"
    bracketed.write_bytes(b"RIFF")
    song = files_dir / "Song [live].mp3"
    song.write_bytes(b"ID3")
    normal = files_dir / "clip.wav"
    normal.write_bytes(b"RIFF")

    (line_take,) = bridge.attachment_lines(_msg([_att(str(bracketed), filename="take[1].wav")]), group_dir=gdir)
    assert line_take.endswith(f" at {files_dir / 'take?1?.wav'} (name sanitized)")

    (line_song,) = bridge.attachment_lines(
        _msg([_att(str(song), filename="Song [live].mp3", mime_type="audio/mpeg")]),
        group_dir=gdir,
    )
    assert line_song.endswith(f" at {files_dir / 'Song ?live?.mp3'} (name sanitized)")

    (line_normal,) = bridge.attachment_lines(_msg([_att(str(normal))]), group_dir=gdir)
    assert line_normal == f"[file: clip.wav, audio/wav, 0.25 MB] at {normal}"
    assert "(name sanitized)" not in line_normal


def test_cli_main_wires_group_dir_and_rejects_other_groups_files(bridge, monkeypatch, capsys):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    kept = gdir / "files" / "mine.wav"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"RIFF")

    other = bridge.DPC_HOME / "conversations" / "group-other" / "files" / "theirs.wav"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"RIFF")

    history = {"messages": [
        _msg([
            _att(str(kept), filename="mine.wav"),
            _att(str(other), filename="theirs.wav"),
            _att("/home/user/.dpc/conversations/x/files/missing.wav", filename="missing.wav"),
        ])
    ]}
    (gdir / "history.json").write_text(json.dumps(history), encoding="utf-8")

    monkeypatch.setenv("DPC_HOME", str(bridge.DPC_HOME))
    monkeypatch.setattr(sys, "argv", ["cc_group_chat_bridge.py", "--group", GROUP, "--last", "5"])
    runpy.run_path(str(BRIDGE), run_name="__main__")
    out = capsys.readouterr().out

    assert f"[file: mine.wav, audio/wav, 0.25 MB] at {kept}" in out
    assert "[file: theirs.wav, audio/wav, 0.25 MB] not kept on this node" in out
    assert "[file: missing.wav, audio/wav, 0.25 MB] not kept on this node" in out


def test_no_group_dir_fails_closed_and_explicit_dirs_work(bridge, tmp_path):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    kept = gdir / "files" / "clip.wav"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"RIFF")

    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF")

    # Calling attachment_lines(msg) or format_message(i, msg, node_id) without
    # group_dir or allowed_dir must fail closed even when the file exists in DPC_HOME.
    assert bridge.attachment_lines(_msg([_att(str(kept))])) == [
        "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node",
    ]
    assert bridge.attachment_lines(_msg([_att(str(outside))])) == [
        "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node",
    ]
    formatted_no_dir = bridge.format_message(0, _msg([_att(str(kept))]), NODE)
    assert formatted_no_dir.endswith("[file: clip.wav, audio/wav, 0.25 MB] not kept on this node")

    # Passing group_dir prints the kept file path (without sanitization suffix for normal names).
    assert bridge.attachment_lines(_msg([_att(str(kept))]), group_dir=gdir) == [
        f"[file: clip.wav, audio/wav, 0.25 MB] at {kept}",
    ]
    formatted_with_dir = bridge.format_message(0, _msg([_att(str(kept))]), NODE, group_dir=gdir)
    assert formatted_with_dir.endswith(f"[file: clip.wav, audio/wav, 0.25 MB] at {kept}")

    # explicit allowed_dir
    custom = tmp_path / "custom_files"
    custom.mkdir()
    custom_file = custom / "clip.wav"
    custom_file.write_bytes(b"RIFF")
    assert bridge.attachment_lines(_msg([_att(str(custom_file))]), allowed_dir=custom) == [
        f"[file: clip.wav, audio/wav, 0.25 MB] at {custom_file}",
    ]


def test_symlink_inside_files_pointing_outside_is_not_kept(bridge, tmp_path):  # noqa: F811
    gdir = bridge._find_group_dir(GROUP)
    files_dir = gdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = files_dir / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not permitted in this environment")

    (line,) = bridge.attachment_lines(_msg([_att(str(link))]), group_dir=gdir)
    assert line == "[file: clip.wav, audio/wav, 0.25 MB] not kept on this node"
