"""browser_scroll and browser_collect take the click's repaint guard.

A window Windows is not painting delivers requestAnimationFrame about once
a second; mouse.move / mouse.wheel wait on frames. On 2026-10-04 a collect's
scroll steps grew 3 ... 521 s (one 11 625 s). The cause is inferred, so a
slow step also records the frame rate seen at that moment. No real browser:
the page is a fake whose locator("html").evaluate() answers the frame count.
"""

import logging
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    _format_scroll_result,
)

_MOVED = {"scrolled": 800, "before": 0, "after": 800, "height": 9000,
          "client": 800, "target": "html", "centerX": 10, "centerY": 10}


def _fake_browser(raf, scroll_result=None):
    ab = object.__new__(AuthBrowser)
    ab._require_open = lambda: None
    ab._audit_action = MagicMock()
    ab._agent_id = "agent_x"
    page = MagicMock()
    page.url = "https://example.test/"
    page.evaluate.return_value = dict(scroll_result or _MOVED)
    rafs = raf if isinstance(raf, list) else None
    if rafs is not None:
        page.locator.return_value.evaluate.side_effect = [
            {"raf": r} for r in rafs
        ]
    else:
        page.locator.return_value.evaluate.return_value = {"raf": raf}
    ab._page = page
    return ab


def test_repainting_window_keeps_the_wheel_path():
    ab = _fake_browser(60)
    info = ab.scroll("down", 800)
    ab._page.mouse.move.assert_called_once_with(10, 10)
    ab._page.mouse.wheel.assert_called_once_with(0, 800)
    assert info["wheel_ok"] is True and info["starved"] is False
    ab._audit_action.assert_called_once_with(
        "scroll", "https://example.test/", "ok",
        direction="down", amount=800, scrolled=800, target="html",
        wheel_ok=True,
    )
    assert "repaint" not in _format_scroll_result(info, "down", 800)


def test_unpainted_window_scrolls_with_scrollby_only_and_says_so(caplog):
    ab = _fake_browser(1)
    with caplog.at_level(logging.WARNING):
        info = ab.scroll("down", 800)
    ab._page.mouse.move.assert_not_called()
    ab._page.mouse.wheel.assert_not_called()
    ab._page.evaluate.assert_called_once()  # scrollBy ran in the JS
    assert info["starved"] is True and info["raf"] == 1
    assert info["wheel_ok"] is False and info["scrolled"] == 800
    kw = ab._audit_action.call_args.kwargs
    assert kw["starved"] is True and kw["raf"] == 1
    text = _format_scroll_result(info, "down", 800)
    assert "window not repainting (rAF 1/s): scrolled with scrollBy only" in text
    assert text.startswith("Scrolled down 800 of 800 px")
    assert "window not repainting (rAF 1/s)" in caplog.text


def test_slow_step_logs_and_audits_the_frame_rate_then(monkeypatch, caplog):
    monkeypatch.setattr(browser_mod, "_SLOW_SCROLL_STEP_S", 0.0)
    ab = _fake_browser([60, 2])  # before the step, then at the stall
    with caplog.at_level(logging.WARNING):
        ab.scroll("down", 800)
    assert "slow scroll step:" in caplog.text
    assert "raf_before=60, raf_now=2" in caplog.text
    kw = ab._audit_action.call_args.kwargs
    assert kw["raf_before"] == 60 and kw["raf_now"] == 2
    assert "slow_step_s" in kw


def test_fast_step_adds_no_measurement(caplog):
    ab = _fake_browser(60)
    with caplog.at_level(logging.WARNING):
        ab.scroll("down", 800)
    assert "slow scroll step" not in caplog.text
    assert "slow_step_s" not in ab._audit_action.call_args.kwargs


def test_unreadable_probe_leaves_the_wheel_path():
    ab = _fake_browser(None)
    ab._page.locator.return_value.evaluate.side_effect = RuntimeError("boom")
    info = ab.scroll("down", 800)
    ab._page.mouse.wheel.assert_called_once()
    assert info["starved"] is False


def test_collect_switches_to_scrollby_mid_run_and_says_so():
    ab = _fake_browser(0)
    ab._executor = None
    ab._call_abandoned = lambda: False
    seq = iter([60, 60, 1, 1, 60])
    calls = []

    def fake_scroll(direction, amount):
        raf = next(seq)
        calls.append(raf)
        return {"starved": raf < 10, "raf": raf, "scrolled": amount}

    ab.scroll = fake_scroll
    n = iter(range(100))
    ab._page.evaluate.side_effect = lambda js, arg: {
        "items": [{"text": f"i{next(n)}"}], "found": 1, "matched": 1}
    monkey_sleep = browser_mod.time.sleep
    browser_mod.time.sleep = lambda s: None
    try:
        res = ab.collect("div", "a", ["text"], max_scrolls=5, scroll_pause_ms=0)
    finally:
        browser_mod.time.sleep = monkey_sleep
    assert res["scrolls_done"] == 5
    assert res["unpainted_scrolls"] == 2 and res["raf_min"] == 1
    kw = ab._audit_action.call_args.kwargs
    assert kw["unpainted_scrolls"] == 2


def test_collect_answer_mentions_the_unpainted_scrolls(monkeypatch, tmp_path):
    import asyncio

    res = {"items": [{"text": "a"}], "total": 1, "scrolls_done": 4,
           "max_scrolls": 4, "stop_reason": "scroll_budget_exhausted",
           "consecutive_empty": 0, "unpainted_scrolls": 3, "raf_min": 1}
    monkeypatch.setattr(browser_mod, "_get_session_or_error", lambda a: MagicMock())

    async def fake_run(sess, name, *a, **kw):
        return res

    monkeypatch.setattr(browser_mod, "_run_in_session", fake_run)
    ctx = MagicMock()
    ctx.agent_root = tmp_path / "agent_x"
    out = asyncio.run(browser_mod.browser_collect(ctx, "div", "a"))
    assert "window not repainting (rAF 1/s at the lowest): 3 of 4 scrolls used scrollBy only" in out
