"""A native `<select>` takes the option the agent names, and a secret select
takes it without saying which one it was.

Chromium against a local `http.server`, the way the download tests do: only a
real engine says whether `select_option` dispatches `input` and `change` on
its own, and what a change handler that navigates leaves behind.
Camoufox/Firefox, the production engine, is NOT exercised here.

The secrecy half is the snapshot's own rule (`isSecretField`) asked of the
same element: a select the snapshot prints as `(N options)` must not have its
options read back out through a tool that sets one of them.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    _new_context_kwargs,
)

_PAGE = """<!doctype html>
<html><head><title>The search form</title></head><body>
  <form id="search" action="/results.html" method="get">
    <select name="SearchFF" id="format">
      <option value="all" selected>Any format</option>
      <option value="pdf">PDF</option>
      <option value="djvu">DjVu</option>
      <option value="fb2">FB2</option>
      <option value="epub" disabled>EPUB, not stocked</option>
    </select>
    <select id="grouped">
      <optgroup label="Text">
        <option value="txt" selected>Plain text</option>
        <option value="md">Markdown</option>
      </optgroup>
      <optgroup label="Binary">
        <option value="bin">Raw bytes</option>
      </optgroup>
    </select>
    <select id="watched">
      <option value="w1" selected>First</option>
      <option value="w2">Second</option>
    </select>
    <select id="jumps">
      <option value="j1" selected>Stay here</option>
      <option value="j2">Go elsewhere</option>
    </select>
    <select id="locked" disabled>
      <option value="l1" selected>Nothing doing</option>
      <option value="l2">Nor this</option>
    </select>
    <select id="many" multiple>
      <option value="m1">One</option>
      <option value="m2">Two</option>
    </select>
    <input id="title" type="text" aria-label="Title">
    <select name="cc-number" id="card">
      <option value="card-visa-4021" selected>Visa 1234</option>
      <option value="card-mc-5533">Mastercard 5678</option>
    </select>
    <select id="expiry" autocomplete="cc-exp-month" aria-label="Expiry month">
      <option value="exp-month-06" selected>June</option>
      <option value="exp-month-07">July</option>
    </select>
    <label for="third">Security code length</label>
    <select id="third">
      <option value="len-3" selected>Three digits</option>
      <option value="len-4">Four digits</option>
    </select>
    <button id="submit" type="submit">Search</button>
  </form>
  <div id="log"></div>
  <script>
    const log = document.getElementById('log');
    const watched = document.getElementById('watched');
    watched.addEventListener('input', () => {
      log.textContent += ' input:' + watched.value;
    });
    watched.addEventListener('change', () => {
      log.textContent += ' change:' + watched.value;
    });
    document.getElementById('jumps').addEventListener('change', (e) => {
      window.location.href = '/results.html?f=' + e.target.value;
    });
  </script>
</body></html>
"""

_RESULTS = (
    "<!doctype html><html><head><title>Results</title></head>"
    "<body>the results page</body></html>"
)

# Every value and every label of the three selects the predicate withholds.
# One tuple, so a sweep over a whole answer cannot fall behind the fixture.
_SECRET_OPTION_STRINGS = (
    "card-visa-4021", "card-mc-5533", "Visa 1234", "Mastercard 5678",
    "exp-month-06", "exp-month-07", "June", "July",
    "len-3", "len-4", "Three digits", "Four digits",
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own spelling
        body = _RESULTS if self.path.startswith("/results.html") else _PAGE
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def _server():
    """A local origin: a change handler that navigates needs somewhere to go,
    and `set_content` leaves a relative URL pointing nowhere."""
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


@pytest.fixture()
def _audit(monkeypatch):
    """The audit rows in memory: the real writer appends under the user's own
    ~/.dpc, which a test may not touch."""
    rows: list[dict] = []
    from dpc_client_core import web_auth

    monkeypatch.setattr(web_auth, "log_browser_action", lambda **f: rows.append(f))
    return rows


@pytest.fixture()
def _session(_chromium, _server, _audit):
    """An AuthBrowser driving a real Chromium page. `_open` launches Camoufox,
    which no test here has a binary for, so the context is built with the
    production kwargs and handed to the same object."""
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    session._page.goto(f"{_server}/page.html")
    try:
        yield session
    finally:
        context.close()


class _Ctx:
    """The slice of ToolContext the select path reads."""

    def __init__(self, agent_root: Path = Path("agent_test")):
        self.agent_root = agent_root
        self.firewall = None


def _step_to_completion(coro):
    """Run a coroutine whose every await completes without yielding.

    Playwright's sync API holds a running loop in this thread, so
    `asyncio.run` refuses outright; and with `_run_in_session` replaced by a
    direct call, nothing in the tool has anything to wait for. An await that
    does need a loop surfaces here rather than as a hang.
    """
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("browser_select awaited something needing a loop")


def _select(session, ref_or_selector, **kwargs):
    """`browser_select` with the session wired in and the pinned executor out
    of the way: the Chromium page belongs to this thread."""

    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args, **kw)

    with patch.object(browser_mod, "_get_session_or_error", lambda _id: session), \
         patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
         patch.object(browser_mod, "_run_in_session", _direct):
        return _step_to_completion(
            browser_mod.browser_select(_Ctx(), ref_or_selector, **kwargs)
        )


def _value(session, selector: str) -> str:
    return session._page.eval_on_selector(selector, "el => el.value")


def _ref_for(session, selector: str) -> str:
    """The `@eN` ref the snapshot gives this element — the address the agent
    has, rather than a CSS selector only a test knows."""
    _tree, refs = session.a11y_snapshot()
    el_id = session._page.eval_on_selector(
        selector, "el => el.getAttribute('data-dpc-el')",
    )
    assert el_id, f"{selector} carries no element mark"
    matches = [ref for ref, node in refs.items() if node.get("el") == el_id]
    assert len(matches) == 1, f"expected one ref for {selector}, got {matches}"
    return matches[0]


def test_a_select_takes_the_value_the_agent_names(_session):
    answer = _select(_session, "#format", value="pdf")
    assert _value(_session, "#format") == "pdf"
    assert "pdf" in answer
    assert "PDF" in answer


def test_a_select_takes_the_visible_label_the_agent_names(_session):
    answer = _select(_session, "#format", label="DjVu")
    assert _value(_session, "#format") == "djvu"
    assert "DjVu" in answer


def test_a_select_takes_an_index_counted_over_every_option(_session):
    """0-based over every `<option>` in document order — the list the
    snapshot's `(N options)` counts, placeholders included."""
    _select(_session, "#format", index=1)
    assert _value(_session, "#format") == "pdf"
    _select(_session, "#format", index=0)
    assert _value(_session, "#format") == "all"


def test_a_ref_from_the_snapshot_reaches_the_same_select(_session):
    ref = _ref_for(_session, "#format")
    _select(_session, ref, value="fb2")
    assert _value(_session, "#format") == "fb2"


def test_an_option_inside_an_optgroup_is_selectable(_session):
    answer = _select(_session, "#grouped", label="Markdown")
    assert _value(_session, "#grouped") == "md"
    assert "Markdown" in answer


def test_the_pages_own_change_handler_fires_without_our_help(_session):
    """`select_option` dispatches `input` and `change` itself; nothing in the
    tool fires them by hand, and a page that reacts must still react."""
    _select(_session, "#watched", value="w2")
    log = _session._page.eval_on_selector("#log", "el => el.textContent")
    assert "change:w2" in log, log
    assert "input:w2" in log, log


def test_a_change_that_navigates_is_reported_as_a_url_change(_session):
    answer = _select(_session, "#jumps", value="j2")
    assert "results.html" in answer
    assert "changed" in answer
    assert _session._page.url.endswith("/results.html?f=j2")


def test_a_select_that_changes_nothing_says_the_url_did_not_change(_session):
    answer = _select(_session, "#format", value="djvu")
    assert "did not change" in answer


def test_the_form_is_not_submitted(_session):
    _select(_session, "#format", value="pdf")
    assert _session._page.url.endswith("/page.html")


def test_a_non_select_is_refused_with_its_own_tag_named(_session):
    answer = _select(_session, "#submit", value="pdf")
    assert "button" in answer
    assert "select" in answer
    assert "browser_click" in answer
    assert "browser_fill" in answer


def test_a_text_input_is_refused_and_pointed_at_browser_fill(_session):
    answer = _select(_session, "#title", value="pdf")
    assert "input" in answer
    assert "browser_fill" in answer


def test_a_disabled_select_is_refused_and_left_alone(_session):
    answer = _select(_session, "#locked", value="l2")
    assert "disabled" in answer
    assert _value(_session, "#locked") == "l1"


def test_a_disabled_option_is_refused_and_leaves_the_value_alone(_session):
    before = _value(_session, "#format")
    answer = _select(_session, "#format", value="epub")
    assert "disabled" in answer
    assert _value(_session, "#format") == before


def test_a_multi_select_is_refused_plainly(_session):
    answer = _select(_session, "#many", value="m1")
    assert "multiple" in answer


def test_no_criterion_at_all_is_refused(_session):
    answer = _select(_session, "#format")
    assert "exactly one" in answer
    assert "value" in answer and "label" in answer and "index" in answer


def test_two_criteria_at_once_are_refused(_session):
    answer = _select(_session, "#format", value="pdf", index=2)
    assert "exactly one" in answer
    assert _value(_session, "#format") != "pdf"


def test_a_stale_ref_gets_the_snapshots_own_message(_session):
    answer = _select(_session, "@e9999", value="pdf")
    assert "unknown ref" in answer
    assert "a11y_snapshot" in answer


def test_an_ordinary_select_lists_its_options_when_none_matches(_session):
    answer = _select(_session, "#format", value="mobi")
    assert "mobi" in answer
    assert "pdf" in answer
    assert "djvu" in answer
    assert "DjVu" in answer


@pytest.mark.parametrize(
    "selector,by,named,expected",
    [
        ("#card", "index", 1, "card-mc-5533"),
        ("#expiry", "value", "exp-month-07", "exp-month-07"),
        ("#third", "label", "Four digits", "len-4"),
    ],
)
def test_a_secret_select_is_set_without_its_option_being_echoed(
    _session, selector, by, named, expected,
):
    """The agent may legitimately fill a card-expiry field it was given, so
    the value is set — but the answer says nothing the snapshot withheld."""
    answer = _select(_session, selector, **{by: named})
    assert _value(_session, selector) == expected
    leaked = [s for s in _SECRET_OPTION_STRINGS if s in answer]
    assert not leaked, f"a withheld option reached the answer: {leaked}\n{answer}"
    assert "withheld" in answer or "secret" in answer, answer


def test_a_secret_select_lists_nothing_when_no_option_matches(_session):
    before = _value(_session, "#card")
    answer = _select(_session, "#card", value="card-nobody-has-this")
    leaked = [s for s in _SECRET_OPTION_STRINGS if s in answer]
    assert not leaked, f"a withheld option reached a refusal: {leaked}\n{answer}"
    assert _value(_session, "#card") == before


def test_a_secret_selects_audit_row_carries_no_chosen_value(_session, _audit):
    _select(_session, "#expiry", value="exp-month-07")
    rows = [r for r in _audit if r.get("action") == "select"]
    assert rows, _audit
    printed = " ".join(str(v) for r in rows for v in r.values())
    leaked = [s for s in _SECRET_OPTION_STRINGS if s in printed]
    assert not leaked, f"a withheld option reached the audit: {leaked}\n{rows}"


def test_an_ordinary_selects_audit_row_names_what_was_chosen(_session, _audit):
    _select(_session, "#format", value="pdf")
    rows = [r for r in _audit if r.get("action") == "select"]
    assert rows, _audit
    printed = " ".join(str(v) for v in rows[-1].values())
    assert "pdf" in printed, rows[-1]


def test_the_tool_is_registered_and_off_until_someone_turns_it_on():
    entry = next(
        t for t in browser_mod.get_tools() if t.name == "browser_select"
    )
    assert entry.default_enabled is False
    assert entry.handler is browser_mod.browser_select
    desc = entry.schema["description"]
    assert "select" in desc
    assert "browser_click" in desc
    assert "browser_snapshot" in desc
    assert set(entry.schema["parameters"]["properties"]) == {
        "ref_or_selector", "value", "label", "index",
    }
    assert entry.schema["parameters"]["required"] == ["ref_or_selector"]


# --- two guards, each tested alone -------------------------------------------
# The page-side probe reads no options of a withheld select, and the sentence
# writer prints none for one. Either hides the other's failure, so a test that
# goes through both passes with one of them broken.


@pytest.mark.parametrize("selector", ["#card", "#expiry", "#third"])
def test_the_probe_reads_no_option_of_a_withheld_select(_session, selector):
    probed = _session._page.eval_on_selector(selector, browser_mod._SELECT_PROBE_JS)

    assert probed["secret"] is True, probed
    assert probed["options"] == [], probed
    assert probed["optionCount"] > 0, "the fixture select has options to withhold"


def test_the_probe_does_read_the_options_of_an_ordinary_select(_session):
    probed = _session._page.eval_on_selector("#format", browser_mod._SELECT_PROBE_JS)

    assert probed["secret"] is False, probed
    assert [o["value"] for o in probed["options"]] == ["all", "pdf", "djvu", "fb2", "epub"]


def test_the_sentence_prints_no_option_of_a_withheld_select_even_if_handed_some():
    """The probe is what keeps options out of `result`; this is the writer
    holding the line on its own, should the probe ever hand some over."""
    handed = {
        "status": "no_such_option", "secret": True, "by": "value",
        "named": "typed-by-the-agent", "option_count": 2,
        "options": [
            {"index": 0, "value": "card-visa-4111", "label": "Visa ending 4111", "disabled": False},
            {"index": 1, "value": "card-mc-5533", "label": "MC ending 5533", "disabled": False},
        ],
    }

    answer = browser_mod._select_answer("@e7", handed)

    for leaked in ("card-visa-4111", "Visa ending 4111", "card-mc-5533", "MC ending 5533"):
        assert leaked not in answer, answer


@pytest.mark.parametrize("selector", ["#card", "#expiry", "#third"])
def test_the_read_back_carries_nothing_of_a_withheld_select_out_of_the_page(_session, selector):
    """`select()` blanks a secret option before answering, so this guard is
    invisible from the tool's answer. It is what keeps the chosen value from
    reaching this process at all."""
    chosen = _session._page.eval_on_selector(selector, browser_mod._SELECT_CHOSEN_JS)

    assert chosen == {"secret": True}, chosen


def test_the_read_back_does_name_the_option_of_an_ordinary_select(_session):
    chosen = _session._page.eval_on_selector("#format", browser_mod._SELECT_CHOSEN_JS)

    assert chosen == {"secret": False, "value": "all", "label": "Any format"}, chosen
