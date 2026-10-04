"""browser_scroll answers with the measured movement, not the request.

Ark was told "Scrolled down by 1200px" eight times on 2026-10-04 while the
audit recorded `scrolled 0` (page at its end). No real browser here: the
page is a fake whose evaluate() returns what _SCROLL_VIEWPORT_JS would.
"""

import asyncio
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    _format_scroll_result,
)


def _fake_browser(evaluate_result=None, raises=None):
    ab = object.__new__(AuthBrowser)
    ab._require_open = lambda: None
    ab._audit_action = MagicMock()
    page = MagicMock()
    page.url = "https://example.test/"
    if raises:
        page.evaluate.side_effect = raises
    else:
        page.evaluate.return_value = evaluate_result
    ab._page = page
    return ab


def _info(before, after, height, client=800, target="html"):
    return {"direction": "down", "amount": 0, "scrolled": after - before,
            "target": target, "wheel_ok": True, "before": before,
            "after": after, "height": height, "client": client}


def test_moved_fully_states_no_edge():
    text = _format_scroll_result(_info(0, 500, 5000), "down", 500)
    assert text.startswith("Scrolled down 500 of 500 px (target: html;")
    assert "scrollTop 0 -> 500 of 4200" in text
    assert "reached" not in text


def test_moved_partly_reaches_the_edge():
    text = _format_scroll_result(_info(3680, 4200, 5000), "down", 2000)
    assert "Scrolled down 520 of 2000 px (target: html" in text
    assert "reached the bottom" in text
    assert "will not move this element" in text


def test_did_not_move_at_the_edge():
    text = _format_scroll_result(_info(12340 - 800, 12340 - 800, 12340), "down", 1200)
    assert text.startswith("Did not move: already at the bottom of html")
    assert "scrollTop 11540 of 11540" in text
    assert "Scrolled" not in text


def test_did_not_move_off_the_edge_points_at_something_else():
    text = _format_scroll_result(_info(100, 100, 5000, target="div.modal"), "down", 500)
    assert text.startswith("Did not move: div.modal did not scroll down")
    assert "not at its bottom" in text


def test_up_edge():
    text = _format_scroll_result(_info(300, 0, 5000), "up", 500)
    assert "Scrolled up 300 of 500 px" in text and "reached the top" in text


def test_measurement_unavailable_is_said_honestly():
    for bad in (None, {}, {"before": None, "after": None, "height": None, "client": None}):
        text = _format_scroll_result(bad, "down", 1200)
        assert "could not be measured" in text
        assert "unknown" in text


def test_session_scroll_returns_measurement_and_keeps_audit_shape():
    ab = _fake_browser({"scrolled": 0, "before": 11540, "after": 11540,
                        "height": 12340, "client": 800, "target": "html",
                        "centerX": 10, "centerY": 10})
    info = ab.scroll("down", 1200)
    assert info["scrolled"] == 0 and info["target"] == "html"
    assert info["after"] == 11540 and info["height"] == 12340
    ab._audit_action.assert_called_once_with(
        "scroll", "https://example.test/", "ok",
        direction="down", amount=1200, scrolled=0, target="html",
        wheel_ok=True,
    )


def test_session_scroll_without_measurement_carries_none():
    info = _fake_browser(None).scroll("down", 300)
    assert info["after"] is None
    assert "could not be measured" in _format_scroll_result(info, "down", 300)


def test_tool_answers_with_the_measurement(monkeypatch, tmp_path):
    info = _info(11540, 11540, 12340)
    session = MagicMock()
    monkeypatch.setattr(browser_mod, "_get_session_or_error", lambda a: session)

    async def fake_run(sess, name, *args, **kw):
        return info

    monkeypatch.setattr(browser_mod, "_run_in_session", fake_run)
    ctx = MagicMock()
    ctx.agent_root = tmp_path / "agent_x"
    out = asyncio.run(browser_mod.browser_scroll(ctx, "down", 1200))
    assert out.startswith("Did not move: already at the bottom of html")
