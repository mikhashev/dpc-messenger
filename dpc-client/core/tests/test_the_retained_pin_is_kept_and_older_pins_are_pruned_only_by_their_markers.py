"""One pin that passed a start is retained; the other pins go, and only folders DPC installed.

B2 step 3, commit 1. `local_engine_pin_fetcher` reads what is installed under an
engine's pins folder, picks the retained pin by `start_passed_at` (never by the
tag's name), records a passed start in the marker atomically, and prunes the rest.
A folder is a pin only when its `.dpc-pin.json` names its own tag and platform
folders, so `b10684` (installed before markers) and a hand-made `prism-…` build
are never returned and never removed. The layout below mirrors the llama.cpp
folder on the owner's node, 2026-11-01.
"""

import json
import logging
import os
import sys

import pytest

from dpc_client_core.managers import local_engine_pin_fetcher as pin

OLD_PLAT, NEW_PLAT = "win-cuda-13.3-x64", "win-cuda-13.4-x64"
LAYOUT = {
    "b10472": OLD_PLAT, "b10566": OLD_PLAT, "b10684": OLD_PLAT, "b10809": OLD_PLAT,
    "b10964": OLD_PLAT, "b11146": NEW_PLAT, "b11429": NEW_PLAT, "b11669": NEW_PLAT,
}
NO_MARKER = "b10684"
PRISM = "prism-b10685-7dffb15"


def _marker(tag, plat, **extra):
    return {"tag": tag, "platform": plat, "assets": {f"llama-{tag}-bin-{plat}.zip": "0" * 64}, **extra}


@pytest.fixture
def pins_dir(tmp_path):
    base = tmp_path / "bin" / "llama.cpp"
    for tag, plat in LAYOUT.items():
        root = base / tag / plat
        root.mkdir(parents=True)
        (root / "llama-server.exe").write_bytes(b"x" * 1000)
        if tag != NO_MARKER:
            pin.marker_path(root).write_text(json.dumps(_marker(tag, plat), indent=2), encoding="utf-8")
    prism = base / PRISM
    prism.mkdir()
    (prism / "llama-server.exe").write_bytes(b"hand-made build")
    (base / ".dpc-kv-fit.json").write_text("{}", encoding="utf-8")
    return base


def _root(pins_dir, tag):
    return pins_dir / tag / LAYOUT[tag]


def _pass(pins_dir, tag, when):
    root = _root(pins_dir, tag)
    data = json.loads(pin.marker_path(root).read_text(encoding="utf-8"))
    data["start_passed_at"] = when
    pin.marker_path(root).write_text(json.dumps(data), encoding="utf-8")


# --- installed_pins ----------------------------------------------------------------------

def test_installed_pins_are_the_folders_whose_marker_names_them_on_both_platform_spellings(pins_dir):
    pins = pin.installed_pins(pins_dir)
    assert [p["tag"] for p in pins] == [t for t in LAYOUT if t != NO_MARKER]
    by_tag = {p["tag"]: p for p in pins}
    assert by_tag["b10964"]["platform"] == OLD_PLAT and by_tag["b11146"]["platform"] == NEW_PLAT
    assert by_tag["b11669"]["root"] == _root(pins_dir, "b11669")
    assert by_tag["b11669"]["marker"]["tag"] == "b11669"
    assert NO_MARKER not in by_tag and PRISM not in by_tag


def test_a_marker_that_disagrees_with_its_folders_is_not_a_pin(pins_dir):
    wrong_tag = pins_dir / "b10000" / OLD_PLAT
    wrong_plat = pins_dir / "b10001" / NEW_PLAT
    leftover = pins_dir / "b11669" / (NEW_PLAT + ".old")
    for root, marker in (
        (wrong_tag, _marker("b99999", OLD_PLAT)),
        (wrong_plat, _marker("b10001", OLD_PLAT)),  # built for 13.3, sits in 13.4
        (leftover, _marker("b11669", NEW_PLAT)),
    ):
        root.mkdir(parents=True)
        pin.marker_path(root).write_text(json.dumps(marker), encoding="utf-8")
    tags = {(p["tag"], p["platform"]) for p in pin.installed_pins(pins_dir)}
    assert ("b10000", OLD_PLAT) not in tags and ("b10001", NEW_PLAT) not in tags
    assert ("b11669", NEW_PLAT + ".old") not in tags
    assert pin.installed_pins(pins_dir.parent / "absent") == []


# --- retained_pin ------------------------------------------------------------------------

def test_the_retained_pin_is_the_one_that_passed_last_not_the_highest_tag(pins_dir):
    _pass(pins_dir, "b11429", "2026-09-01T10:00:00+00:00")
    _pass(pins_dir, "b10472", "2026-10-30T10:00:00+00:00")
    _pass(pins_dir, "b10964", "2026-10-29T10:00:00.500000+00:00")
    assert pin.retained_pin(pins_dir, "b11669")["tag"] == "b10472"


def test_the_current_tag_is_never_its_own_retained_pin(pins_dir):
    _pass(pins_dir, "b11669", "2026-11-01T10:00:00+00:00")
    _pass(pins_dir, "b11429", "2026-10-01T10:00:00+00:00")
    assert pin.retained_pin(pins_dir, "b11669")["tag"] == "b11429"
    assert pin.retained_pin(pins_dir, "b11429")["tag"] == "b11669"


def test_no_retained_pin_when_no_other_pin_ever_passed(pins_dir):
    assert pin.retained_pin(pins_dir, "b11669") is None
    _pass(pins_dir, "b11669", "2026-11-01T10:00:00+00:00")
    _pass(pins_dir, "b11429", "not a time")
    assert pin.retained_pin(pins_dir, "b11669") is None


# --- note_start_passed -------------------------------------------------------------------

def test_a_passed_start_is_noted_once_and_keeps_the_rest_of_the_marker(pins_dir):
    root = _root(pins_dir, "b11669")
    before = json.loads(pin.marker_path(root).read_text(encoding="utf-8"))
    assert pin.note_start_passed(root) is True
    after = json.loads(pin.marker_path(root).read_text(encoding="utf-8"))
    first = after.pop("start_passed_at")
    assert after == before
    assert first.endswith("+00:00")
    assert pin.note_start_passed(root) is False
    assert json.loads(pin.marker_path(root).read_text(encoding="utf-8"))["start_passed_at"] == first
    assert sorted(p.name for p in root.iterdir()) == [".dpc-pin.json", "llama-server.exe"]
    assert pin.retained_pin(pins_dir, "b99999")["tag"] == "b11669"


def test_no_marker_means_nothing_noted_and_nothing_written(pins_dir, tmp_path):
    root = _root(pins_dir, NO_MARKER)
    assert pin.note_start_passed(root) is False
    assert sorted(p.name for p in root.iterdir()) == ["llama-server.exe"]
    bad = _root(pins_dir, "b10472")
    pin.marker_path(bad).write_text("[1, 2]", encoding="utf-8")
    assert pin.note_start_passed(bad) is False
    assert pin.marker_path(bad).read_text(encoding="utf-8") == "[1, 2]"


def test_a_marker_write_that_fails_leaves_the_old_marker_and_no_temp_file(pins_dir, monkeypatch):
    root = _root(pins_dir, "b11669")
    before = pin.marker_path(root).read_bytes()

    def _refuse(src, dst):
        raise PermissionError("antivirus holds the marker")

    monkeypatch.setattr(pin.os, "replace", _refuse)
    with pytest.raises(PermissionError):
        pin.note_start_passed(root)
    assert pin.marker_path(root).read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == [".dpc-pin.json", "llama-server.exe"]


# --- prune_pins --------------------------------------------------------------------------

def test_prune_keeps_keep_and_in_use_skips_a_refused_rename_and_never_touches_unmarked_folders(
    pins_dir, monkeypatch, caplog
):
    current, retained = _root(pins_dir, "b11669"), _root(pins_dir, "b11429")
    running, held = _root(pins_dir, "b11146"), _root(pins_dir, "b10809")
    real_replace = pin.Path.replace

    def _refuse_one(self, target):
        if self == held:
            raise PermissionError("llama-server.exe is running")
        return real_replace(self, target)

    monkeypatch.setattr(pin.Path, "replace", _refuse_one)
    with caplog.at_level(logging.INFO, logger=pin.__name__):
        removed = pin.prune_pins(pins_dir, keep=[current, retained], in_use=[running])

    assert sorted(p["tag"] for p in removed) == ["b10472", "b10566", "b10964"]
    for tag in ("b10472", "b10566", "b10964"):
        assert not (pins_dir / tag).exists()  # the emptied tag folder goes too
    for root in (current, retained, running, held):
        assert (root / "llama-server.exe").is_file() and pin.marker_path(root).is_file()
    assert (_root(pins_dir, NO_MARKER) / "llama-server.exe").is_file()
    assert (pins_dir / PRISM / "llama-server.exe").read_bytes() == b"hand-made build"
    assert (pins_dir / ".dpc-kv-fit.json").is_file()
    assert sorted(p.name for p in pins_dir.iterdir()) == sorted(
        [".dpc-kv-fit.json", PRISM, NO_MARKER, "b10809", "b11146", "b11429", "b11669"]
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("b10472" in m and "freeing" in m and "bytes" in m for m in messages)
    assert any(str(held) in m and "Kept" in m for m in messages)


def test_a_tag_folder_with_something_else_in_it_is_kept(pins_dir):
    (pins_dir / "b10472" / "notes.txt").write_text("the owner's", encoding="utf-8")
    pin.prune_pins(pins_dir, keep=[_root(pins_dir, "b11669")], in_use=[])
    assert sorted(p.name for p in (pins_dir / "b10472").iterdir()) == ["notes.txt"]


def test_in_use_is_matched_by_place_not_by_spelling(pins_dir):
    current = _root(pins_dir, "b11669")
    spelled = pins_dir / "b11146" / ".." / "b11146" / NEW_PLAT
    pin.prune_pins(pins_dir, keep=[current], in_use=[spelled])
    assert _root(pins_dir, "b11146").is_dir()


def test_a_pin_reached_through_a_link_outside_the_pins_folder_is_not_removed(pins_dir, tmp_path):
    outside = tmp_path / "elsewhere" / OLD_PLAT
    outside.mkdir(parents=True)
    pin.marker_path(outside).write_text(json.dumps(_marker("b09999", OLD_PLAT)), encoding="utf-8")
    link = pins_dir / "b09999"
    try:
        os.symlink(outside.parent, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            raise
        import _winapi  # a junction needs no privilege, a symlink does

        _winapi.CreateJunction(str(outside.parent), str(link))
    assert (link / OLD_PLAT / ".dpc-pin.json").is_file()
    pin.prune_pins(pins_dir, keep=[_root(pins_dir, "b11669")], in_use=[])
    assert pin.marker_path(outside).is_file()
