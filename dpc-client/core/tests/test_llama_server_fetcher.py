"""The pinned llama.cpp binary: resolved without a fetch, fetched only pinned.

ADR-040 route (b2) runs a DPC-owned `llama-server`; its binary comes from the
pinned `b12110` release, verified against sha256 digests taken from the
release API — never «latest». A configured `binary_path` always wins, so an
operator-supplied build is never silently replaced, and a broken download
must leave nothing behind that a later start would mistake for an install.
"""

import hashlib
import io
import json
import zipfile

import pytest

from dpc_client_core.managers import llama_server_fetcher as fetcher
from dpc_client_core.managers import local_engine_pin_fetcher as pin_fetcher


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
    monkeypatch.setattr(
        pin_fetcher.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(b"these are not the bytes we pinned"),
    )
    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetcher.install_pin()
    assert not fetcher.install_root().exists()
    assert not any(tmp_path.rglob("*.part"))


def test_every_asset_name_carries_its_platform_key_and_the_pinned_tag():
    # The download URL is `{BASE}/{LLAMA_CPP_TAG}/{name}` and nothing compares
    # the two: a table where one platform's name kept the old tag is a 404 on
    # that platform alone, with a green suite on every other. The b10964 ->
    # b11146 bump disproved "cudart carries no tag and is the same file across
    # pins": cudart's own name changed too (13.3-x64.zip -> 13.4-x64.zip), so
    # every row -- llama and cudart alike -- must carry its platform's key
    # (the dict key, which is also `platform_tag()`'s return) in its name,
    # and the versioned rows must also carry the pinned tag.
    for plat, rows in fetcher.PLATFORM_ASSETS.items():
        for row in rows:
            assert plat in row["name"], f"{plat}: {row['name']} does not carry {plat}"
            if row["name"].startswith("cudart-"):
                continue
            assert row["name"].startswith(f"llama-{fetcher.LLAMA_CPP_TAG}-"), (
                f"{plat}: {row['name']} does not carry {fetcher.LLAMA_CPP_TAG}"
            )


def test_install_root_without_arguments_is_the_pinned_tag_and_this_platform(tmp_path, monkeypatch):
    # Every pin sits in pins_dir(), which also holds the `.dpc-kv-fit.json` memo.
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    assert fetcher.pins_dir() == tmp_path / "bin" / "llama.cpp"
    assert fetcher.install_root() == (
        fetcher.pins_dir() / fetcher.LLAMA_CPP_TAG / fetcher.platform_tag()
    )


def test_an_unreadable_or_non_object_marker_resolves_to_no_install(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    root = fetcher.install_root()
    root.mkdir(parents=True)
    (root / fetcher.server_binary_name()).write_bytes(b"")
    for text in ("[1, 2]", "not json", '"b11669"'):
        (root / ".dpc-pin.json").write_text(text, encoding="utf-8")
        assert fetcher.resolve_binary({}) is None


def _serve_zip(monkeypatch, members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    payload = buf.getvalue()
    asset = {
        "name": "llama-test.zip",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    monkeypatch.setitem(fetcher.PLATFORM_ASSETS, fetcher.platform_tag(), [asset])
    seen = []
    monkeypatch.setattr(
        pin_fetcher.urllib.request,
        "urlopen",
        lambda url, timeout: seen.append(url) or _FakeResponse(payload),
    )
    return seen


def test_install_pin_installs_the_pinned_assets_and_writes_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    urls = _serve_zip(monkeypatch, {fetcher.server_binary_name(): b"bin"})
    binary = fetcher.install_pin()
    assert binary == fetcher.install_root() / fetcher.server_binary_name()
    assert binary.read_bytes() == b"bin"
    assert urls == [f"{fetcher.LLAMA_CPP_RELEASE_BASE}/{fetcher.LLAMA_CPP_TAG}/llama-test.zip"]
    marker = json.loads((fetcher.install_root() / ".dpc-pin.json").read_text(encoding="utf-8"))
    assert marker["tag"] == fetcher.LLAMA_CPP_TAG and marker["platform"] == fetcher.platform_tag()
    assert fetcher.resolve_binary({}) == binary


def test_assets_without_the_server_binary_install_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DPC_HOME", tmp_path)
    _serve_zip(monkeypatch, {"readme.txt": b"no server here"})
    with pytest.raises(ValueError, match="contain no"):
        fetcher.install_pin()
    root = fetcher.install_root()
    assert not root.exists()
    assert not root.with_name(root.name + ".staging").exists()
