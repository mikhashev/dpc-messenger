"""The pinned llama.cpp binary: resolved without a fetch, fetched only pinned.

ADR-040 route (b2) runs a DPC-owned `llama-server`; its binary comes from the
pinned `b10566` release, verified against sha256 digests taken from the
release API — never «latest». A configured `binary_path` always wins, so an
operator-supplied build is never silently replaced, and a broken download
must leave nothing behind that a later start would mistake for an install.
"""

import io
import json
import zipfile

import pytest

from dpc_client_core.managers import llama_server_fetcher as fetcher


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Chunked(_FakeResponse):
    """A response that never hands over more than `step` bytes per read."""

    def __init__(self, payload: bytes, step: int):
        super().__init__(payload)
        self._step = step

    def read(self, n: int) -> bytes:
        return super().read(min(n, self._step))


def _asset_for_this_platform():
    return fetcher.PLATFORM_ASSETS[fetcher.platform_tag()][0]


def test_a_configured_binary_path_wins_and_no_fetch_happens(tmp_path, monkeypatch):
    binary = tmp_path / "llama-server.exe"
    binary.write_bytes(b"operator build")

    def _no_network(*a, **kw):
        raise AssertionError("a configured binary_path must skip the fetch entirely")

    monkeypatch.setattr(fetcher, "install_pin", _no_network)
    assert fetcher.ensure_binary({"binary_path": str(binary)}) == binary


def test_a_configured_path_that_is_missing_is_an_error_not_a_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fetcher, "install_pin", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetched"))
    )
    with pytest.raises(FileNotFoundError, match="binary_path"):
        fetcher.ensure_binary({"binary_path": str(tmp_path / "absent.exe")})


def test_an_installed_pin_resolves_only_when_the_marker_matches_the_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    root = fetcher.install_root()
    root.mkdir(parents=True)
    (root / fetcher.server_binary_name()).write_bytes(b"")
    _marker = root / ".dpc-pin.json"

    # Right tag: resolves.
    _marker.write_text(json.dumps({"tag": fetcher.LLAMA_CPP_TAG}), encoding="utf-8")
    assert fetcher.resolve_binary({}) == root / fetcher.server_binary_name()

    # A directory left by another tag is not an install of this pin.
    _marker.write_text(json.dumps({"tag": "b00000"}), encoding="utf-8")
    assert fetcher.resolve_binary({}) is None


def test_a_sha256_mismatch_aborts_and_leaves_no_install(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    asset = _asset_for_this_platform()
    monkeypatch.setattr(
        fetcher.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(b"these are not the bytes we pinned"),
    )
    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetcher.install_pin()
    assert not fetcher.install_root().exists()
    assert not any(tmp_path.rglob("*.part"))


def test_the_download_hashes_the_whole_stream_and_reports_progress(tmp_path, monkeypatch):
    asset = {"name": "x.zip", "sha256": _sha(b"payload"), "size": 7}
    dest = tmp_path / "x.zip"
    # The fake server hands out the payload three bytes at a time; the hash
    # must still be of the whole stream, and progress must see every byte.
    monkeypatch.setattr(
        fetcher.urllib.request, "urlopen", lambda url, timeout: _Chunked(b"payload", 3)
    )
    seen = []
    fetcher._download_asset(asset, dest, lambda got, total: seen.append((got, total)))
    assert dest.read_bytes() == b"payload"
    assert seen[-1] == (7, 7) and seen[0] == (3, 7)
    assert not dest.with_suffix(".zip.part").exists()


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_extraction_refuses_archive_members_that_escape(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.txt", "zip slip")
    with pytest.raises(ValueError, match="escapes"):
        fetcher._extract(archive, tmp_path / "dest")
    assert not (tmp_path / "outside.txt").exists()
