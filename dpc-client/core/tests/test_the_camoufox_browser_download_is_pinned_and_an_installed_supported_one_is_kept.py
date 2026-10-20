"""The Camoufox browser download is pinned, and an installed supported one is kept.

Mike's call, 2026-10-21: the node downloads `v152.0.4-beta.28` — the build the
Windows node runs — instead of the newest GitHub release the package picks
(v158.0-beta.40, a prerelease, that day). "Don't break anyone": a binary that is
already installed and inside the package's supported range keeps working
whatever its version; a download is offered only when it is missing or
unsupported. No test here reaches GitHub or downloads anything.
"""

import json
import logging

import pytest

from dpc_client_core import camoufox_binary

pkgman = pytest.importorskip("camoufox.pkgman")


def _assets(tag_version, tag_release, platforms=None):
    platforms = platforms or [
        "lin.arm64", "lin.x86_64", "mac.arm64", "mac.x86_64", "win.i686", "win.x86_64",
    ]
    return [
        {
            "name": f"camoufox-{tag_version}-{tag_release}-{p}.zip",
            "size": 1000 + i,
            "browser_download_url": f"https://example.invalid/{tag_version}-{tag_release}-{p}.zip",
        }
        for i, p in enumerate(platforms)
    ]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def github(monkeypatch):
    """requests.get answering the releases API; records each URL asked."""
    import requests

    urls = []
    answers = {}

    def _get(url, *a, **k):
        urls.append(url)
        return _Resp(answers[url])

    monkeypatch.setattr(requests, "get", _get)
    return urls, answers


_TAGS = "https://api.github.com/repos/daijro/camoufox/releases/tags/"


def _this_platform():
    return f"{pkgman.OS_NAME}.{pkgman.CamoufoxFetcher.get_platform_arch()}"


# --- which release is fetched --------------------------------------------------


def test_the_fetcher_reads_the_pinned_tag_and_not_the_newest_release(github):
    urls, answers = github
    answers[_TAGS + "v152.0.4-beta.28"] = {"assets": _assets("152.0.4", "beta.28")}

    fetcher = camoufox_binary._fetcher_class()()

    assert urls == [_TAGS + "v152.0.4-beta.28"]
    assert fetcher.verstr == "152.0.4-beta.28"
    assert fetcher.url.endswith(f"152.0.4-beta.28-{_this_platform()}.zip")


def test_an_asset_of_another_version_inside_the_tag_is_not_taken(github, monkeypatch):
    urls, answers = github
    answers[_TAGS + "v152.0.4-beta.28"] = {
        "assets": _assets("158.0", "beta.40") + _assets("152.0.4", "beta.28"),
    }

    fetcher = camoufox_binary._fetcher_class()()

    assert fetcher.verstr == "152.0.4-beta.28"


def test_a_pin_the_package_does_not_support_fails_with_its_name(github, monkeypatch):
    urls, answers = github
    monkeypatch.setattr(camoufox_binary, "PINNED_RELEASE_TAG", "v135.0-beta.18")
    answers[_TAGS + "v135.0-beta.18"] = {"assets": _assets("135.0", "beta.18")}

    with pytest.raises(pkgman.MissingRelease, match="v135.0-beta.18"):
        camoufox_binary._fetcher_class()()


# --- the size the consent dialog shows -------------------------------------------


def test_the_size_is_the_pinned_release_for_this_platform_without_a_network_read(monkeypatch):
    def _no_fetcher():
        raise AssertionError("the table row answers; GitHub is not asked")

    monkeypatch.setattr(camoufox_binary, "_fetcher_class", _no_fetcher)
    expected = {
        "lin.arm64": 653_814_950, "lin.x86_64": 663_387_175,
        "mac.arm64": 312_626_286, "mac.x86_64": 319_866_512,
        "win.i686": 478_100_137, "win.x86_64": 492_370_020,
    }[_this_platform()]

    assert camoufox_binary.size_bytes() == expected
    assert camoufox_binary.size_text() == f"about {expected / 1e9:.1f} GB"


def test_a_pin_with_no_table_row_asks_github_for_its_size(monkeypatch):
    monkeypatch.setattr(camoufox_binary, "PINNED_RELEASE_TAG", "v999.0-beta.99")
    monkeypatch.setattr(camoufox_binary, "_live_size", None)

    class _Fetcher:
        asset_size = 777

    monkeypatch.setattr(camoufox_binary, "_fetcher_class", lambda: _Fetcher)
    assert camoufox_binary.size_bytes() == 777


# --- an installed binary of another version ------------------------------------


@pytest.fixture
def install_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pkgman, "INSTALL_DIR", tmp_path)

    def _never_install(self):
        raise AssertionError("the check must not install anything")

    monkeypatch.setattr(pkgman.CamoufoxFetcher, "install", _never_install)

    def _write(version, release):
        (tmp_path / "version.json").write_text(json.dumps({"version": version, "release": release}))
        if pkgman.Version(release=release, version=version).is_supported():
            # get_path is safe here: version.json is written and supported,
            # so camoufox_path() returns without its download branch.
            launch = type(tmp_path)(pkgman.get_path(pkgman.LAUNCH_FILE[pkgman.OS_NAME]))
            launch.parent.mkdir(parents=True, exist_ok=True)
            launch.write_bytes(b"")
    return _write


def test_a_newer_supported_binary_keeps_working(install_dir):
    install_dir("158.0", "beta.40")
    assert camoufox_binary._probe() is None


def test_the_pinned_binary_itself_counts_as_installed(install_dir):
    install_dir("152.0.4", "beta.28")
    assert camoufox_binary._probe() is None


def test_a_binary_below_the_package_range_is_offered_again(install_dir):
    install_dir("135.0", "beta.18")
    assert camoufox_binary._probe() == "outdated"


# --- the line at backend start ---------------------------------------------------


def test_the_start_line_names_installed_pinned_and_the_supported_range(install_dir, caplog):
    install_dir("158.0", "beta.40")
    with caplog.at_level(logging.INFO, logger="dpc_client_core.camoufox_binary"):
        camoufox_binary.log_versions()
    (line,) = [r.getMessage() for r in caplog.records]
    assert "installed 158.0-beta.40" in line
    assert "pinned v152.0.4-beta.28" in line
    assert pkgman.CONSTRAINTS.as_range() in line
    assert "OUTSIDE" not in line


def test_the_start_line_says_when_the_pin_left_the_package_range(install_dir, caplog, monkeypatch):
    install_dir("152.0.4", "beta.28")
    monkeypatch.setattr(camoufox_binary, "PINNED_RELEASE_TAG", "v135.0-beta.18")
    with caplog.at_level(logging.INFO, logger="dpc_client_core.camoufox_binary"):
        camoufox_binary.log_versions()
    (line,) = [r.getMessage() for r in caplog.records]
    assert "OUTSIDE" in line
