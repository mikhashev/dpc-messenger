"""A collected list arrives in windows, each line carrying its own link.

Two symptoms, one defect, seen live on a results page of ~50 items: the
answer was cut off before the links, and the agent — holding titles with no
links, and no statement that anything had been cut — repeated the identical
call until `guards.py` stopped it with `[LOOP_GUARD]`.

The cause is the output contract. `browser_collect` returned
`json.dumps(items, indent=2)`: every item spent five lines, each item's text
printed above its own link, and nothing said how much of the list was in the
answer. `loop.py:_truncate_tool_result` then cut at TOOL_RESULT_CHAR_CAP, and
what a cut inside that shape leaves is every title down to the cut and the
links of only the items above it. With no way to ask for the rest, the same
call was the only move left.

So: one line per item with the link first, an explicit window with `offset`
and `limit`, and a sentence naming which items these are out of how many and
the exact call that gets the next ones.
"""

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.loop import TOOL_RESULT_CHAR_CAP
from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    COLLECT_LIMIT_DEFAULT,
    COLLECT_LIMIT_MAX,
    AuthBrowser,
    _collect_item_line,
    _collect_window,
    _new_context_kwargs,
)

_ITEM_COUNT = 120

# One item whose text alone would fill a third of the tool-result cap. Under
# the old shape its link was the first thing the cut took.
_SHOUTING_ITEM = 7
_SHOUTING_TEXT = "war and peace " * 400


def _rows() -> str:
    out = []
    for n in range(1, _ITEM_COUNT + 1):
        text = _SHOUTING_TEXT if n == _SHOUTING_ITEM else f"Result number {n}"
        out.append(
            f'<div class="row"><a href="/item/{n}">{text}</a></div>'
        )
    return "\n".join(out)


def _page() -> str:
    return (
        "<!doctype html><html><head><title>Results</title>"
        "<style>#results{height:300px;overflow-y:scroll}</style></head>"
        f'<body><div id="results">{_rows()}</div></body></html>'
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own spelling
        body = _page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def _server():
    """A real origin, so `a.href` in the page resolves `/item/N` to an
    absolute URL the way it does on the site this was measured on."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def _chromium():
    """A real browser, or a clean skip: playwright is an extra, and an
    installed package still has no binary until `playwright install` ran."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - no browser extra
        pytest.skip(f"playwright not installed: {exc}")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"no chromium binary for playwright: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(autouse=True)
def _audit(monkeypatch):
    """The audit rows go nowhere: the real writer appends under the user's own
    ~/.dpc, which a test may not touch."""
    from dpc_client_core import web_auth

    monkeypatch.setattr(web_auth, "log_browser_action", lambda **f: None)


@pytest.fixture(scope="module")
def _session(_chromium, _server):
    """An AuthBrowser driving a real Chromium page — `_open` launches
    Camoufox, which no test here has a binary for."""
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    session._page.goto(f"{_server}/")
    try:
        yield session
    finally:
        context.close()


class _Ctx:
    class agent_root:
        name = "agent_test"


def _step_to_completion(coro):
    """Run a coroutine whose every await completes without yielding.

    Playwright's sync API holds a running loop in this thread, so
    `asyncio.run` refuses outright; with `_run_in_session` replaced by a
    direct call, the tool has nothing to wait for.
    """
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("browser_collect awaited something needing a loop")


def _collect(session, **kwargs):
    """`browser_collect` against the live page. `max_scrolls=0` collects what
    the DOM already holds and scrolls nothing — the paging is what is under
    test here, not the scrolling."""
    import asyncio
    from unittest.mock import patch

    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args)

    args = {
        "container": "#results",
        "item_selector": "#results .row",
        "extract": ["text", "href"],
        "max_scrolls": 0,
    }
    args.update(kwargs)
    with patch.object(browser_mod, "_get_session_or_error", lambda _id: session), \
         patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
         patch.object(browser_mod, "_run_in_session", _direct):
        return _step_to_completion(browser_mod.browser_collect(_Ctx(), **args))


_ITEM_LINE = re.compile(r"^\s*(\d+)\. (\S+)(?: \| (.*))?$", re.M)


def _lines(answer: str) -> list[tuple[int, str, str]]:
    return [
        (int(number), href, text or "")
        for number, href, text in _ITEM_LINE.findall(answer)
    ]


def test_the_first_window_carries_a_link_on_every_line(_session, _server):
    answer = _collect(_session)
    lines = _lines(answer)
    assert lines, answer
    assert len(lines) == COLLECT_LIMIT_DEFAULT
    assert [n for n, _h, _t in lines] == list(range(1, COLLECT_LIMIT_DEFAULT + 1))
    for number, href, _text in lines:
        assert href == f"{_server}/item/{number}", (number, href)


def test_the_window_says_which_items_these_are_and_how_to_get_the_rest(
    _session,
):
    answer = _collect(_session)
    assert f"items 1–{COLLECT_LIMIT_DEFAULT} of {_ITEM_COUNT}" in answer
    assert f"offset={COLLECT_LIMIT_DEFAULT})" in answer
    assert 'container="#results"' in answer
    assert f"{_ITEM_COUNT - COLLECT_LIMIT_DEFAULT} more" in answer


def test_the_answer_stays_under_the_cap_that_would_otherwise_cut_it(_session):
    answer = _collect(_session)
    assert len(answer) < TOOL_RESULT_CHAR_CAP, len(answer)


def test_a_relative_href_arrives_absolute(_session, _server):
    answer = _collect(_session)
    for _number, href, _text in _lines(answer):
        assert href.startswith(f"{_server}/item/")


def test_an_item_with_an_enormous_text_keeps_its_own_link(_session, _server):
    """The shape that lost the links: one item's text was allowed to run to
    thousands of characters, and a cut inside it took every link below."""
    answer = _collect(_session, offset=_SHOUTING_ITEM - 1, limit=3)
    lines = _lines(answer)
    numbers = [n for n, _h, _t in lines]
    assert _SHOUTING_ITEM in numbers, answer
    href = next(h for n, h, _t in lines if n == _SHOUTING_ITEM)
    assert href == f"{_server}/item/{_SHOUTING_ITEM}"
    assert len(answer) < TOOL_RESULT_CHAR_CAP
    # Its neighbours arrived too: one loud item does not spend the window.
    assert _SHOUTING_ITEM + 1 in numbers


def test_walking_offset_reaches_every_item_once(_session, _server):
    """No gaps and no duplicates: the union of the windows is the list."""
    seen: list[str] = []
    offset = 0
    windows = 0
    while True:
        answer = _collect(_session, offset=offset)
        lines = _lines(answer)
        assert lines, (offset, answer)
        seen.extend(href for _n, href, _t in lines)
        windows += 1
        assert windows <= _ITEM_COUNT, "the walk is not advancing"
        nxt = re.search(r"offset=(\d+)\)", answer)
        if nxt is None:
            assert "this is the last window" in answer, answer
            break
        offset = int(nxt.group(1))
    assert len(seen) == _ITEM_COUNT
    assert len(set(seen)) == _ITEM_COUNT
    assert set(seen) == {
        f"{_server}/item/{n}" for n in range(1, _ITEM_COUNT + 1)
    }
    assert windows > 1


def test_a_window_past_the_end_says_so_instead_of_answering_nothing(_session):
    answer = _collect(_session, offset=_ITEM_COUNT + 10)
    assert f"offset={_ITEM_COUNT + 10} is past the end" in answer
    assert f"items 1–{_ITEM_COUNT}" in answer


def test_the_last_window_says_it_is_the_last(_session):
    answer = _collect(_session, offset=_ITEM_COUNT - 5)
    assert f"items {_ITEM_COUNT - 4}–{_ITEM_COUNT} of {_ITEM_COUNT}" in answer
    assert "this is the last window" in answer
    assert "offset=" not in answer


def test_a_limit_above_the_maximum_is_clamped(_session):
    answer = _collect(_session, limit=10_000)
    assert len(_lines(answer)) <= COLLECT_LIMIT_MAX


# ---------------------------------------------------------------------------
# The two shape rules, without a browser.


def test_an_item_line_puts_the_link_before_the_text():
    line = _collect_item_line(3, {"text": "A title", "href": "https://x/1"})
    assert line == "3. https://x/1 | A title"


def test_an_item_line_bounds_the_text_but_never_the_link():
    line = _collect_item_line(1, {"text": "z" * 5000, "href": "https://x/1"})
    assert line.startswith("1. https://x/1 | ")
    assert len(line) < 500


def test_an_item_line_keeps_any_other_attribute_asked_for():
    line = _collect_item_line(
        2, {"text": "T", "href": "https://x/2", "data-id": "abc"},
    )
    assert line == "2. https://x/2 | T | data-id=abc"


def test_an_item_with_no_link_still_renders():
    assert _collect_item_line(9, {"text": "no link here"}) == "9. no link here"


def test_a_window_stops_at_the_character_budget_rather_than_overrun_it():
    items = [{"text": "t" * 200, "href": f"https://x/{n}"} for n in range(50)]
    lines, next_offset = _collect_window(items, 0, 50, budget=1000)
    assert 0 < len(lines) < 50
    assert next_offset == len(lines)
    assert sum(len(ln) + 1 for ln in lines) <= 1000 + len(lines[-1]) + 1


def test_one_oversized_item_still_comes_back_rather_than_stalling_the_walk():
    items = [{"text": "t" * 9000, "href": "https://x/1"}, {"text": "b"}]
    lines, next_offset = _collect_window(items, 0, 10, budget=10)
    assert len(lines) == 1
    assert next_offset == 1


def test_a_window_past_the_end_yields_no_lines():
    items = [{"text": "a", "href": "https://x/1"}]
    lines, next_offset = _collect_window(items, 5, 10, budget=10_000)
    assert lines == []
    assert next_offset == 5
