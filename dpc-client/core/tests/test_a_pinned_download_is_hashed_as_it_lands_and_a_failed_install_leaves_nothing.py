"""The shared pinned-download steps: hashed as they land, swapped in whole, or not at all.

`local_engine_pin_fetcher` serves every pinned local engine (llama.cpp now,
Strata next). A download is verified before it is renamed, an archive cannot
write outside its staging folder, a failed check leaves neither staging nor a
half install, and the swap never passes through a state with no install: an
older root survives a failure at any point, with the files DPC wrote into it.
"""

import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from dpc_client_core.managers import local_engine_pin_fetcher as pin


class _FakeResponse:
    def __init__(self, payload: bytes, step: int = 1 << 20):
        self._stream = io.BytesIO(payload)
        self._step = step

    def read(self, n: int) -> bytes:
        return self._stream.read(min(n, self._step))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _serve(monkeypatch, payloads: dict) -> list:
    """Fake urlopen: `payloads` maps an asset name to its bytes; returns the URLs asked."""
    asked = []

    def _urlopen(url, timeout):
        asked.append(url)
        return _FakeResponse(payloads[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(pin.urllib.request, "urlopen", _urlopen)
    return asked


def _siblings(root):
    return sorted(p.name for p in root.parent.iterdir())


def _install(monkeypatch, root, **kwargs):
    payload = _zip({"engine.bin": b"new engine"})
    asset = {"name": "pkg.zip", "sha256": _sha(payload), "size": len(payload)}
    asked = _serve(monkeypatch, {"pkg.zip": payload})
    pin.install_with_staging_swap(
        root, [asset], "https://example.test/rel/v1", {"tag": "v1"}, **kwargs
    )
    return asked


def _old_install(root):
    root.mkdir()
    (root / "engine.bin").write_bytes(b"old engine")


def test_the_download_hashes_the_whole_stream_and_reports_progress(tmp_path, monkeypatch):
    asset = {"name": "x.zip", "sha256": _sha(b"payload"), "size": 7}
    dest = tmp_path / "x.zip"
    # The fake server hands out the payload three bytes at a time; the hash
    # must still be of the whole stream, and progress must see every byte.
    monkeypatch.setattr(
        pin.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(b"payload", 3)
    )
    seen = []
    pin.download_asset(
        "https://example.test/x.zip", asset, dest, lambda got, total: seen.append((got, total))
    )
    assert dest.read_bytes() == b"payload"
    assert seen[-1] == (7, 7) and seen[0] == (3, 7)
    assert not dest.with_suffix(".zip.part").exists()


def test_progress_across_two_assets_is_one_rising_count_that_ends_at_their_sum(tmp_path, monkeypatch):
    # One bar for the whole pin: the second asset continues where the first ended.
    first, second = _zip({"a.bin": b"a" * 5000}), _zip({"b.bin": b"b" * 3000})
    assets = [
        {"name": "one.zip", "sha256": _sha(first), "size": len(first)},
        {"name": "two.zip", "sha256": _sha(second), "size": len(second)},
    ]

    def _urlopen(url, timeout):
        return _FakeResponse({"one.zip": first, "two.zip": second}[url.rsplit("/", 1)[1]], 64)

    monkeypatch.setattr(pin.urllib.request, "urlopen", _urlopen)
    seen = []
    pin.install_with_staging_swap(
        tmp_path / "plat", assets, "https://example.test/rel", {"tag": "v1"},
        progress=lambda done, total: seen.append((done, total)),
    )
    total = len(first) + len(second)
    assert {t for _, t in seen} == {total}
    dones = [d for d, _ in seen]
    assert dones == sorted(dones) and dones[-1] == total
    assert len(first) in dones and any(d > len(first) for d in dones)


def test_a_sha256_mismatch_leaves_no_destination_and_no_part_file(tmp_path, monkeypatch):
    asset = {"name": "x.zip", "sha256": _sha(b"pinned"), "size": 6}
    dest = tmp_path / "x.zip"
    monkeypatch.setattr(
        pin.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(b"tampered")
    )
    with pytest.raises(ValueError, match="sha256 mismatch"):
        pin.download_asset("https://example.test/x.zip", asset, dest, None)
    assert not dest.exists()
    assert not any(tmp_path.rglob("*.part"))


def test_extraction_refuses_archive_members_that_escape(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.txt", "zip slip")
    with pytest.raises(ValueError, match="escapes"):
        pin.extract_archive(archive, tmp_path / "dest")
    assert not (tmp_path / "outside.txt").exists()


def test_extraction_refuses_a_tar_member_that_escapes(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("../outside.txt")
        data = b"tar slip"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(tarfile.TarError):
        pin.extract_archive(archive, dest)
    assert not (tmp_path / "outside.txt").exists()


def test_a_marker_is_read_only_when_it_is_a_json_object(tmp_path):
    assert pin.read_marker(tmp_path) is None
    for text, expected in (
        ("not json", None),
        ("[1]", None),
        ('"x"', None),
        ('{"tag": "t"}', {"tag": "t"}),
    ):
        pin.marker_path(tmp_path).write_text(text, encoding="utf-8")
        assert pin.read_marker(tmp_path) == expected


def test_a_fresh_install_lands_with_its_marker_and_no_leftovers(tmp_path, monkeypatch):
    root = tmp_path / "bin" / "engine" / "v1" / "plat"
    asked = _install(monkeypatch, root)
    assert asked == ["https://example.test/rel/v1/pkg.zip"]
    assert (root / "engine.bin").read_bytes() == b"new engine"
    assert json.loads(pin.marker_path(root).read_text(encoding="utf-8")) == {"tag": "v1"}
    assert _siblings(root) == ["plat"]


def test_the_marker_carries_what_the_check_returns(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _install(monkeypatch, root, check=lambda staging: {"engine": {"folder": "f"}, "tag": "v1b"})
    marker = json.loads(pin.marker_path(root).read_text(encoding="utf-8"))
    assert marker == {"tag": "v1b", "engine": {"folder": "f"}}


def test_the_check_sees_the_extracted_staging_tree(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    seen = []
    _install(
        monkeypatch, root, check=lambda staging: seen.append(sorted(p.name for p in staging.iterdir()))
    )
    assert seen == [["engine.bin"]]


def test_a_failing_check_leaves_neither_root_nor_staging(tmp_path, monkeypatch):
    root = tmp_path / "plat"

    def _refuse(staging):
        raise ValueError("not what the pin says")

    with pytest.raises(ValueError, match="not what the pin says"):
        _install(monkeypatch, root, check=_refuse)
    assert not root.exists()
    assert _siblings(root) == []


def test_a_failing_check_leaves_an_older_install_untouched(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _old_install(root)

    def _refuse(staging):
        raise ValueError("refused")

    with pytest.raises(ValueError):
        _install(monkeypatch, root, check=_refuse)
    assert (root / "engine.bin").read_bytes() == b"old engine"
    assert _siblings(root) == ["plat"]


def test_a_download_failure_leaves_nothing_and_keeps_the_older_install(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _old_install(root)
    payload = _zip({"engine.bin": b"new"})
    bad = {"name": "pkg.zip", "sha256": _sha(b"something else"), "size": len(payload)}
    _serve(monkeypatch, {"pkg.zip": payload})
    with pytest.raises(ValueError, match="sha256 mismatch"):
        pin.install_with_staging_swap(root, [bad], "https://example.test/rel", {"tag": "v1"})
    assert (root / "engine.bin").read_bytes() == b"old engine"
    assert _siblings(root) == ["plat"]


def test_a_swap_replaces_an_older_install_and_removes_the_old_copy(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _old_install(root)
    (root / "stale.txt").write_text("from the old install", encoding="utf-8")
    _install(monkeypatch, root)
    assert (root / "engine.bin").read_bytes() == b"new engine"
    assert not (root / "stale.txt").exists()
    assert _siblings(root) == ["plat"]


def test_a_stale_old_copy_from_an_earlier_crash_does_not_block_the_swap(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _old_install(root)
    stale = tmp_path / "plat.old"
    stale.mkdir()
    (stale / "junk").write_text("x", encoding="utf-8")
    _install(monkeypatch, root)
    assert (root / "engine.bin").read_bytes() == b"new engine"
    assert _siblings(root) == ["plat"]


def test_a_staging_rename_that_fails_leaves_the_old_install_intact(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _old_install(root)
    # The old marker, with what a passed start wrote into it (the KV-fit memo used to be the example).
    old_marker = '{"tag": "v0", "start_passed_at": "2026-11-01T00:00:00+00:00"}'
    pin.marker_path(root).write_text(old_marker, encoding="utf-8")

    real_replace = pin.Path.replace

    def _refuse_the_staging_rename(self, target):
        if self.name == "plat.staging":
            raise PermissionError("antivirus holds the folder")
        return real_replace(self, target)

    monkeypatch.setattr(pin.Path, "replace", _refuse_the_staging_rename)
    with pytest.raises(PermissionError, match="antivirus"):
        _install(monkeypatch, root)
    assert (root / "engine.bin").read_bytes() == b"old engine"
    assert pin.marker_path(root).read_text(encoding="utf-8") == old_marker
    assert _siblings(root) == ["plat"]


# --- archives land beside the root, never in the system temp folder ---------------------


def _no_system_temp(monkeypatch):
    def _refuse(*a, **kw):
        raise AssertionError("an archive went to the system temp folder")

    monkeypatch.setattr(pin.tempfile, "TemporaryDirectory", _refuse)


def test_archives_land_in_the_download_folder_beside_the_root(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _no_system_temp(monkeypatch)
    real_download = pin.download_asset
    dests = []

    def _record(url, asset, dest, progress):
        dests.append(dest)
        real_download(url, asset, dest, progress)

    monkeypatch.setattr(pin, "download_asset", _record)
    _install(monkeypatch, root)
    assert dests == [tmp_path / "plat.download" / "pkg.zip"]
    assert (root / "engine.bin").read_bytes() == b"new engine"
    assert _siblings(root) == ["plat"]


def test_a_failed_install_removes_the_download_folder(tmp_path, monkeypatch):
    root = tmp_path / "plat"
    _no_system_temp(monkeypatch)
    stale = tmp_path / "plat.download"
    stale.mkdir()
    (stale / "half.zip.part").write_bytes(b"from an earlier crash")

    def _refuse(staging):
        assert not (stale / "half.zip.part").exists()
        raise ValueError("refused")

    with pytest.raises(ValueError, match="refused"):
        _install(monkeypatch, root, check=_refuse)
    assert _siblings(root) == []


def test_each_archive_is_deleted_once_extracted(tmp_path, monkeypatch):
    # Disk holds one archive at a time: the second download finds the first gone.
    root = tmp_path / "plat"
    _no_system_temp(monkeypatch)
    first, second = _zip({"a.bin": b"a"}), _zip({"b.bin": b"b"})
    assets = [
        {"name": "one.zip", "sha256": _sha(first), "size": len(first)},
        {"name": "two.zip", "sha256": _sha(second), "size": len(second)},
    ]
    _serve(monkeypatch, {"one.zip": first, "two.zip": second})
    real_download = pin.download_asset
    present_at_second = []

    def _observe(url, asset, dest, progress):
        if asset["name"] == "two.zip":
            present_at_second.append((dest.parent / "one.zip").exists())
        real_download(url, asset, dest, progress)

    monkeypatch.setattr(pin, "download_asset", _observe)
    pin.install_with_staging_swap(root, assets, "https://example.test/rel", {"tag": "v1"})
    assert present_at_second == [False]
    assert sorted(p.name for p in root.iterdir()) == [".dpc-pin.json", "a.bin", "b.bin"]
    assert _siblings(root) == ["plat"]
