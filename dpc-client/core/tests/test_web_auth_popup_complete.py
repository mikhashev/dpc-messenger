"""Tests for ADR-028 T9 Step 3 — CoreService.web_auth_popup_complete.

Covers the Q2 URL-safety variants (Ark softer S142):
  - orphan request (no pending) → ok ack, no exception
  - frontend error → propagated as AuthRequiredError
  - missing content_html → AuthRequiredError
  - eTLD+1 domain mismatch → AuthRequiredError
  - eTLD+1 match, path mismatch → accepted with WARN log
  - exact URL match → accepted silently
  - command listed in local_api ALLOWED_COMMANDS allowlist

The Future-resolution path is exercised end-to-end with a fake
PendingPopupRequest registered into the live module-level dict, so
this also smoke-tests the contract between the caller side (Step 2,
browser.py) and the receiver side (Step 3, service.py).
"""
from __future__ import annotations

from .conftest import TEST_DOMAIN, TEST_DOMAIN_WWW, TEST_DOMAIN_URL
import asyncio
import logging

import pytest


# ─────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────


def _make_service_stub():
    """Minimal stand-in for CoreService that only exposes the methods
    web_auth_popup_complete touches: local_api (unused here, the future
    resolution path doesn't broadcast). The handler reads no other
    CoreService state, so a bare object works."""
    import types

    return types.SimpleNamespace()


def _register_pending(request_id: str, expected_url: str, expected_etld1: str):
    """Register a pending popup request directly in the module dict —
    same shape `_request_popup_fallback` would write. Returns the
    future so the test can `await` it."""
    from dpc_client_core.dpc_agent.tools import browser as _browser

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    entry = _browser.PendingPopupRequest(
        future=fut, expected_url=expected_url, expected_etld1=expected_etld1
    )
    _browser.get_pending_popup_requests()[request_id] = entry
    return fut


def _clear_pending():
    """Reset the module dict between tests to keep them independent."""
    from dpc_client_core.dpc_agent.tools import browser as _browser

    _browser.get_pending_popup_requests().clear()


@pytest.fixture(autouse=True)
def _isolated_pending_dict():
    _clear_pending()
    yield
    _clear_pending()


# Bind the handler under test once; the real implementation lives on
# CoreService but takes no `self` state we care about, so we bind it to
# our stub.
def _handler_under_test():
    from dpc_client_core.service import CoreService

    return CoreService.web_auth_popup_complete


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────


def test_orphan_request_returns_ok_and_does_not_raise(caplog):
    """No pending entry for request_id → handler returns orphan status,
    logs a warning, and does NOT raise (frontend should dismiss UI)."""
    handler = _handler_under_test()
    svc = _make_service_stub()

    caplog.set_level(logging.WARNING)
    out = asyncio.run(
        handler(svc, request_id="missing-id", content_html="<html/>",
                current_url=f"https://{TEST_DOMAIN}/my")
    )
    assert out["status"] == "orphan"
    assert out["request_id"] == "missing-id"
    assert any("unknown request_id" in r.message for r in caplog.records)


def test_error_payload_resolves_with_auth_required_exception():
    """Frontend reports a popup error → future raises AuthRequiredError
    on the awaiting caller side."""
    from dpc_client_core.dpc_agent.tools import browser as _browser

    handler = _handler_under_test()
    svc = _make_service_stub()

    async def _run():
        fut = _register_pending("req-1", f"https://{TEST_DOMAIN}/my", f"{TEST_DOMAIN}")
        out = await handler(
            svc, request_id="req-1", error="popup_crashed"
        )
        assert out["status"] == "error_propagated"
        with pytest.raises(_browser.AuthRequiredError, match="popup_crashed"):
            await fut

    asyncio.run(_run())


def test_missing_content_html_resolves_with_auth_required():
    """No content_html and no error → AuthRequiredError ("no content")."""
    from dpc_client_core.dpc_agent.tools import browser as _browser

    handler = _handler_under_test()
    svc = _make_service_stub()

    async def _run():
        fut = _register_pending("req-2", f"https://{TEST_DOMAIN}/my", f"{TEST_DOMAIN}")
        out = await handler(
            svc, request_id="req-2",
            content_html=None, current_url=f"https://{TEST_DOMAIN}/my"
        )
        assert out["status"] == "no_content"
        with pytest.raises(_browser.AuthRequiredError, match="no content_html"):
            await fut

    asyncio.run(_run())


def test_domain_mismatch_rejects_with_auth_required():
    """current_url eTLD+1 differs from expected → AuthRequiredError."""
    from dpc_client_core.dpc_agent.tools import browser as _browser

    handler = _handler_under_test()
    svc = _make_service_stub()

    async def _run():
        fut = _register_pending("req-3", f"https://{TEST_DOMAIN}/my", f"{TEST_DOMAIN}")
        out = await handler(
            svc, request_id="req-3",
            content_html="<html/>", current_url="https://attacker.com/leak"
        )
        assert out["status"] == "domain_mismatch"
        with pytest.raises(_browser.AuthRequiredError, match="different site"):
            await fut

    asyncio.run(_run())


def test_path_mismatch_within_domain_accepts_with_warning(caplog):
    """Q2 softer: same eTLD+1, different path → WARN + accept HTML."""
    handler = _handler_under_test()
    svc = _make_service_stub()
    caplog.set_level(logging.WARNING)

    async def _run():
        fut = _register_pending("req-4", f"https://{TEST_DOMAIN}/my/orders", f"{TEST_DOMAIN}")
        out = await handler(
            svc, request_id="req-4",
            content_html="<html><body>category page</body></html>",
            current_url=f"https://{TEST_DOMAIN}/category/electronics",
        )
        assert out["status"] == "ok"
        assert out["bytes"] == len("<html><body>category page</body></html>")
        result = await fut
        assert "category page" in result

    asyncio.run(_run())
    assert any("user navigated" in r.message for r in caplog.records), (
        "expected WARN log about intra-site navigation"
    )


def test_exact_url_match_accepts_silently(caplog):
    """Exact URL match → ok, no warning."""
    handler = _handler_under_test()
    svc = _make_service_stub()
    caplog.set_level(logging.WARNING)

    async def _run():
        url = f"https://{TEST_DOMAIN}/my/orders"
        fut = _register_pending("req-5", url, f"{TEST_DOMAIN}")
        out = await handler(
            svc, request_id="req-5",
            content_html="<html><body>my orders</body></html>",
            current_url=url,
        )
        assert out["status"] == "ok"
        result = await fut
        assert "my orders" in result

    asyncio.run(_run())
    # No "user navigated" WARN for the exact-match case.
    assert not any("user navigated" in r.message for r in caplog.records)


def test_subdomain_match_accepts():
    """User finished on a subdomain of the auth domain (e.g.
    `lk.yarcheplus.ru` when the request was for `yarcheplus.ru`) — eTLD+1
    matches via the ETLD1_MAP, so accept."""
    handler = _handler_under_test()
    svc = _make_service_stub()

    async def _run():
        fut = _register_pending(
            "req-6", "https://yarcheplus.ru/profile/orders", "yarcheplus.ru"
        )
        out = await handler(
            svc, request_id="req-6",
            content_html="<html/>",
            current_url="https://lk.yarcheplus.ru/profile/orders/group/16659751",
        )
        assert out["status"] == "ok"
        await fut  # no exception

    asyncio.run(_run())


def test_command_is_in_allowed_commands_allowlist():
    """Wiring sanity: the command must be in the local_api allowlist
    or the WS layer will reject it before it ever reaches CoreService."""
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    assert "web_auth_popup_complete" in ALLOWED_COMMANDS
