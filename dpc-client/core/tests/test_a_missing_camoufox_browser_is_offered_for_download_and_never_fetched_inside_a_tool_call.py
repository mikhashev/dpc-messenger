"""A missing Camoufox browser is offered for download, never fetched inside a tool call.

Observed 2026-10-21 on the Linux node: the first browse_page made the camoufox
package call `camoufox_path(download_if_missing=True)`, which pulled 1.30 GB of
the newest GitHub release with a progress bar on stdout only; the pooled
browser_start waited out its 120 s and the one-shot fallback started the same
`Camoufox(...)` again. Nothing reached dpc-client.log, the UI or the agent.

Now every launch asks first, refuses in a sentence while the binary is absent,
and the owner is asked through the same `model_download_*` flow as Whisper.
No test here touches the network or the real install directory.
"""

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from dpc_client_core import camoufox_binary
from dpc_client_core.camoufox_binary import CamoufoxBinaryMissing
from dpc_client_core.dpc_agent.tools import browser
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.settings import Settings

pkgman = pytest.importorskip("camoufox.pkgman")


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _download_service(tmp_path):
    return ModelDownloadService(
        llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi()
    )


@pytest.fixture
def binary_missing(monkeypatch):
    """Undo conftest's "present" for this file, and make sure nothing below
    can reach the real fetcher or the real Camoufox."""
    monkeypatch.setattr(camoufox_binary, "missing_reason", lambda: "missing")
    monkeypatch.setattr(camoufox_binary, "size_bytes", lambda: 492_370_020)
    launched = []

    class _NeverLaunch:
        def __init__(self, **kwargs):
            launched.append(kwargs)
            raise AssertionError("Camoufox(...) must not be constructed")

    monkeypatch.setattr("camoufox.sync_api.Camoufox", _NeverLaunch)

    def _no_fetcher():
        raise AssertionError("the fetcher must not be built inside a tool call")

    monkeypatch.setattr(camoufox_binary, "_fetcher_class", _no_fetcher)
    return launched


def _ctx(tmp_path, service=None):
    return SimpleNamespace(
        agent_root=tmp_path / "agent_test",
        repo_path=lambda rel: tmp_path / rel,
        firewall=None,
        dpc_service=SimpleNamespace(model_download_service=service),
    )


# --- the check never downloads ----------------------------------------------


def test_the_check_asks_the_package_without_permission_to_download(monkeypatch):
    calls = []

    def _path(download_if_missing=True):
        calls.append(download_if_missing)
        raise FileNotFoundError("no camoufox here")

    monkeypatch.setattr(pkgman, "camoufox_path", _path)
    assert camoufox_binary._probe() == "missing"
    assert calls == [False]


def test_an_outdated_binary_is_named_as_outdated(monkeypatch):
    def _path(download_if_missing=True):
        assert download_if_missing is False
        raise pkgman.UnsupportedVersion("Camoufox executable is outdated.")

    monkeypatch.setattr(pkgman, "camoufox_path", _path)
    assert camoufox_binary._probe() == "outdated"


def test_an_installed_binary_lets_the_launch_through(monkeypatch, tmp_path):
    monkeypatch.setattr(pkgman, "camoufox_path", lambda download_if_missing=True: tmp_path)
    monkeypatch.setattr(pkgman, "launch_path", lambda: str(tmp_path / "camoufox-bin"))
    assert camoufox_binary._probe() is None


def test_a_download_in_progress_is_reported_as_such(monkeypatch):
    # conftest fixes missing_reason to "present"; take the module's own back.
    monkeypatch.undo()
    monkeypatch.setattr(camoufox_binary, "_probe", lambda: "missing")
    assert camoufox_binary.missing_reason() == "missing"
    assert camoufox_binary._install_lock.acquire(blocking=False)
    try:
        assert camoufox_binary.missing_reason() == "downloading"
    finally:
        camoufox_binary._install_lock.release()


# --- every launch path refuses before Camoufox(...) -------------------------


def test_the_session_browser_refuses_before_launching(binary_missing):
    session = browser.AuthBrowser(agent_id="agent_test", domains=[], headed=False, anonymous=True)
    with pytest.raises(CamoufoxBinaryMissing, match="not downloaded"):
        session._open()
    assert binary_missing == []


def test_the_one_shot_browser_refuses_before_launching(binary_missing):
    with pytest.raises(CamoufoxBinaryMissing):
        browser._browse_with_camoufox("https://example.org", "agent_test")
    assert binary_missing == []


def test_the_pooled_browser_does_not_fall_back_to_the_one_shot(binary_missing, monkeypatch):
    """The Linux node's log: browser_start waited 120 s on the download, then
    the fallback started a second Camoufox(...) that would wait again."""
    one_shot = []
    monkeypatch.setattr(browser, "_browse_with_camoufox", lambda *a: one_shot.append(a))
    browser._fetch_sessions.pop("agent_pool", None)

    with pytest.raises(CamoufoxBinaryMissing):
        asyncio.run(browser._fetch_js_text("https://example.org", "agent_pool"))

    assert one_shot == []
    assert binary_missing == []
    assert "agent_pool" not in browser._fetch_sessions


def test_with_no_agent_the_one_shot_path_refuses_too(binary_missing):
    with pytest.raises(CamoufoxBinaryMissing):
        asyncio.run(browser._fetch_js_text("https://example.org", None))
    assert binary_missing == []


# --- what the agent reads and what the owner is asked ------------------------


def test_a_visible_window_call_is_refused_and_the_owner_is_asked_once(binary_missing, tmp_path):
    service = _download_service(tmp_path)
    ctx = _ctx(tmp_path, service)
    browser._active_browser_sessions.pop("agent_test", None)

    async def run():
        first = await browser.browse_page(ctx, "https://example.org", keep_open=True)
        second = await browser.browse_page(ctx, "https://example.org", keep_open=True)
        return first, second

    first, second = asyncio.run(run())

    for answer in (first, second):
        assert answer.startswith("⚠️ The Camoufox browser is not downloaded")
        assert "about 0.5 GB" in answer
        assert "asked to confirm it in the D-PC Messenger app" in answer
    required = [p for name, p in service.local_api.events if name == "model_download_required"]
    assert len(required) == 1
    assert required[0]["model_name"] == "camoufox"
    assert required[0]["size_bytes"] == 492_370_020
    assert binary_missing == []


def test_a_declined_download_is_said_to_be_declined(binary_missing, tmp_path):
    service = _download_service(tmp_path)
    service.decline("camoufox", remember=True)
    ctx = _ctx(tmp_path, service)
    browser._active_browser_sessions.pop("agent_test", None)

    answer = asyncio.run(browser.browse_page(ctx, "https://example.org", keep_open=True))

    assert "the owner has declined" in answer
    assert "started from the D-PC Messenger app" in answer
    assert service.local_api.events == []


def test_a_page_that_needs_javascript_keeps_its_static_text_and_says_why(
    binary_missing, tmp_path, monkeypatch,
):
    service = _download_service(tmp_path)
    ctx = _ctx(tmp_path, service)
    monkeypatch.setattr(
        browser, "_browse_sync",
        lambda url: {
            "success": True, "text": "static shell text", "needs_js": True,
            "signals": {"html_chars": 500, "document_closed": True, "js_capable": True},
        },
    )
    from dpc_client_core import web_auth
    monkeypatch.setattr(web_auth, "log_browser_action", lambda *a, **k: None)
    browser._fetch_sessions.pop("agent_test", None)

    answer = asyncio.run(browser.browse_page(ctx, "https://example.org"))

    assert "static shell text" in answer
    assert "⚠️ The Camoufox browser is not downloaded" in answer
    assert [n for n, _ in service.local_api.events] == ["model_download_required"]
    assert binary_missing == []


def test_an_authenticated_fetch_is_refused_in_words(binary_missing, tmp_path, monkeypatch):
    from dpc_client_core import web_auth
    audit = []
    monkeypatch.setattr(web_auth, "has_session", lambda *a: True)
    monkeypatch.setattr(web_auth, "audit_append", lambda *a, **k: audit.append(k.get("status")))
    ctx = _ctx(tmp_path, _download_service(tmp_path))

    answer = asyncio.run(
        browser.browse_page(ctx, "https://example.org/x", use_auth="example.org")
    )

    assert answer.startswith("⚠️ The Camoufox browser is not downloaded")
    assert audit == ["camoufox_missing"]
    assert binary_missing == []


# --- the download itself ------------------------------------------------------


def test_the_offer_carries_the_install_directory_and_never_asks_hugging_face(
    tmp_path, monkeypatch,
):
    from dpc_client_core import model_download_service as mds

    def _no_hf(*a, **k):
        raise AssertionError("camoufox is not a Hugging Face repository")

    monkeypatch.setattr(mds, "model_size_bytes", _no_hf)
    monkeypatch.setattr(mds, "hf_cache_path", _no_hf)
    monkeypatch.setattr(camoufox_binary, "size_bytes", lambda: 42)
    monkeypatch.setattr(camoufox_binary, "location", lambda: "/cache/camoufox")
    service = _download_service(tmp_path)

    asyncio.run(service.maybe_emit_required(
        "camoufox", purpose="p", consequence_if_declined="c",
    ))

    (name, payload), = service.local_api.events
    assert name == "model_download_required"
    assert payload["cache_path"] == "/cache/camoufox"
    assert payload["size_bytes"] == 42


def test_the_confirmed_download_runs_once_with_its_events(tmp_path, monkeypatch):
    release = threading.Event()
    calls = []

    def _download(on_progress=None):
        calls.append(1)
        release.wait(5)
        return "158.0-beta.40"

    monkeypatch.setattr(camoufox_binary, "download", _download)
    service = _download_service(tmp_path)

    async def run():
        first = asyncio.create_task(service.download_model("camoufox"))
        await asyncio.sleep(0.05)
        second = await service.download_model("camoufox")
        release.set()
        return await first, second

    first, second = asyncio.run(run())

    assert calls == [1]
    assert first == {"status": "success", "model_name": "camoufox"}
    assert second["status"] == "error" and "already being downloaded" in second["error"]
    assert [n for n, _ in service.local_api.events] == [
        "model_download_started", "model_download_completed",
    ]
    assert service.get_status("camoufox") == {"state": "cached"}


def test_a_failed_download_is_reported_and_can_be_tried_again(tmp_path, monkeypatch):
    def _boom(on_progress=None):
        raise OSError("connection reset")

    monkeypatch.setattr(camoufox_binary, "download", _boom)
    service = _download_service(tmp_path)

    result = asyncio.run(service.download_model("camoufox"))

    assert result["status"] == "error"
    assert service.local_api.events[-1] == (
        "model_download_failed", {"model_name": "camoufox", "error": "connection reset"},
    )
    assert "camoufox" not in service._in_flight


def test_two_installs_at_once_cannot_both_run(monkeypatch):
    """Both would stage into the same folder beside the install."""
    started = threading.Event()
    release = threading.Event()
    installs = []

    class _Fetcher:
        verstr = "152.0.4-beta.28"
        asset_size = 1

    def _install(fetcher, on_progress):
        installs.append(1)
        started.set()
        release.wait(5)

    monkeypatch.setattr(camoufox_binary, "_fetcher_class", lambda: _Fetcher)
    monkeypatch.setattr(camoufox_binary, "_install_pinned", _install)

    worker = threading.Thread(target=camoufox_binary.download)
    worker.start()
    assert started.wait(5)
    try:
        with pytest.raises(RuntimeError, match="already being downloaded"):
            camoufox_binary.download()
    finally:
        release.set()
        worker.join(5)
    assert installs == [1]


def test_the_download_reports_its_progress_to_the_log(monkeypatch, caplog, tmp_path):
    import hashlib

    import requests

    body = b"x" * (10 * 1024 * 1024)

    class _Response:
        headers = {"content-length": str(len(body))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(body), size):
                yield body[i:i + size]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())
    dest = tmp_path / "c.zip"

    with caplog.at_level(logging.INFO, logger="dpc_client_core.camoufox_binary"):
        camoufox_binary._fetch_verified(
            "https://example.invalid/camoufox.zip", dest, label="Camoufox browser",
            expected_size=len(body),
            expected_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        )

    assert dest.read_bytes() == body
    marks = [r.getMessage() for r in caplog.records if "%" in r.getMessage()]
    assert marks[0].startswith("Camoufox browser download: 10%")
    assert marks[-1].startswith("Camoufox browser download: 100%")
