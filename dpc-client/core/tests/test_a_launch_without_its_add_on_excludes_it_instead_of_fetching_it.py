"""A launch without its add-on excludes it instead of fetching it.

With uBlock Origin optional in our install, a browser can be on disk without
`addons/UBO`. Left to the package, the launch then fetches AMO's latest inside
the tool call, and a failed fetch leaves an empty folder that every later
launch rejects with InvalidAddonPath. The launch arguments now decide it: a
folder with no manifest is removed and the package's own `exclude_addons`
keeps it from fetching. Nothing here reaches the network.
"""

import json
import logging

import pytest

from dpc_client_core import camoufox_binary
from dpc_client_core.dpc_agent.tools import browser

pkgman = pytest.importorskip("camoufox.pkgman")
from camoufox.addons import DefaultAddons  # noqa: E402


@pytest.fixture
def install(tmp_path, monkeypatch):
    import requests

    def _no_network(*a, **k):
        raise AssertionError("no add-on fetch inside a launch")

    monkeypatch.setattr(requests, "get", _no_network)
    monkeypatch.setattr("camoufox.addons.maybe_download_addons", _no_network)
    monkeypatch.setattr(pkgman, "INSTALL_DIR", tmp_path)
    monkeypatch.setattr(camoufox_binary, "_logged_launch_without_ublock", False)
    return tmp_path


def _ubo(root):
    return camoufox_binary._resources(root) / "addons" / "UBO"


def test_a_missing_add_on_is_excluded_from_the_launch(install):
    kwargs = browser._camoufox_launch_kwargs()
    assert kwargs["exclude_addons"] == [DefaultAddons.UBO]


def test_an_empty_add_on_folder_is_removed_and_excluded(install):
    _ubo(install).mkdir(parents=True)

    kwargs = browser._camoufox_launch_kwargs()

    assert kwargs["exclude_addons"] == [DefaultAddons.UBO]
    assert not _ubo(install).exists()


def test_a_folder_without_its_manifest_is_removed_and_excluded(install):
    _ubo(install).mkdir(parents=True)
    (_ubo(install) / "half.js").write_text("x")

    assert browser._camoufox_launch_kwargs()["exclude_addons"] == [DefaultAddons.UBO]
    assert not _ubo(install).exists()


def test_a_healthy_add_on_leaves_the_launch_unchanged(install):
    _ubo(install).mkdir(parents=True)
    (_ubo(install) / "manifest.json").write_text(json.dumps({"version": "1.72.2"}))

    kwargs = browser._camoufox_launch_kwargs()

    assert "exclude_addons" not in kwargs
    assert (_ubo(install) / "manifest.json").exists()


def test_the_session_browser_launches_with_the_exclusion(install, monkeypatch):
    seen = []

    class _StopAfterKwargs(Exception):
        pass

    def _fake_camoufox(**kwargs):
        seen.append(kwargs)
        raise _StopAfterKwargs

    monkeypatch.setattr("camoufox.sync_api.Camoufox", _fake_camoufox)
    session = browser.AuthBrowser(agent_id="agent_test", domains=[], headed=False, anonymous=True)
    with pytest.raises(_StopAfterKwargs):
        session._open()
    assert seen[0]["exclude_addons"] == [DefaultAddons.UBO]


def test_the_exclusion_is_what_stops_the_package_from_fetching(monkeypatch):
    """Against the installed package: with UBO excluded its launch never
    reaches maybe_download_addons."""
    from camoufox import addons

    calls = []
    monkeypatch.setattr(addons, "maybe_download_addons", lambda *a, **k: calls.append(a))
    addons.add_default_addons([], [DefaultAddons.UBO])
    assert calls == [([], [])]  # asked for nothing


def test_the_launch_without_it_is_logged_once_per_process(install, caplog):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.camoufox_binary"):
        browser._camoufox_launch_kwargs()
        browser._camoufox_launch_kwargs()
    lines = [r.getMessage() for r in caplog.records if "launching without uBlock Origin" in r.getMessage()]
    assert len(lines) == 1


def test_a_folder_that_cannot_be_removed_is_not_logged_as_removed(install, caplog, monkeypatch):
    _ubo(install).mkdir(parents=True)
    monkeypatch.setattr(camoufox_binary.shutil, "rmtree", lambda *a, **k: None)  # a locked file

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.camoufox_binary"):
        kwargs = browser._camoufox_launch_kwargs()

    assert kwargs["exclude_addons"] == [DefaultAddons.UBO]
    messages = [r.getMessage() for r in caplog.records]
    assert any("could not remove the add-on folder" in m for m in messages)
    assert not any("removed the add-on folder" in m for m in messages)
