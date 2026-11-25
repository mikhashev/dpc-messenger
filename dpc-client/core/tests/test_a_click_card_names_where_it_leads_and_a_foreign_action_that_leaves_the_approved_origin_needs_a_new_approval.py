"""Follow-up 15 (Zcode #184 P3): click blindness.

- The owner-approval cards of browser_click, browser_fill and browser_select name where the
  action leads (a link's resolved href, a form's action and method), or say "destination
  unknown (script)" when it cannot be read.
- After an action that moved the page, the per-navigation mark is re-read: a foreign turn's
  action that left the origin the owner approved needs a fresh approval before the page can
  be read; the owner's own action resets the mark as owner.
"""
from concurrent.futures import Future

import pytest

from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, Asker
from dpc_client_core.dpc_agent.tools import browser
from dpc_client_core.dpc_agent.tools.registry import ToolContext

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)
INBOX = "https://example.com/inbox"
PAGE_TEXT = "<html>the inbox</html>"


def _ctx(tmp_path, asker):
    root = tmp_path / "agent_ark"
    root.mkdir(exist_ok=True)
    if asker.is_owner:
        return ToolContext(agent_root=root, message_source="ui", turn_class="owner",
                           open_layers=frozenset({"L5", "L6", "EXT"}), asker=asker)
    return ToolContext(agent_root=root, message_source="group", turn_class=OTHER_NODE_TURN,
                       open_layers=frozenset(), asker=asker)


class _Executor:
    def submit(self, fn, *args):
        fut = Future()
        try:
            fut.set_result(fn(*args))
        except Exception as e:
            fut.set_exception(e)
        return fut


class _Session:
    def __init__(self, url=INBOX):
        self._etld1s = {"example.com"}
        self._anonymous = False
        self._page = object()
        self._headed = True
        self.url = url
        self.destination = {}
        self.after_click = None  # where a click moves the page

    def _get_executor(self):
        return _Executor()

    def page_url(self):
        return self.url

    def action_destination(self, ref, intent):
        if isinstance(self.destination, Exception):
            raise self.destination
        return dict(self.destination, page=self.url)


@pytest.fixture
def session(monkeypatch):
    sess = _Session()

    async def _run(s, method, *args, **kwargs):
        if method == "navigate":
            s.url = args[0]
            return "tree"
        if method == "click":
            if s.after_click is not None:
                s.url = s.after_click
            return {"delivery": "mouse", "raf": 60, "starved": False}
        if method == "extract":
            return PAGE_TEXT
        if method == "page_url":
            if s.url is None:
                raise RuntimeError("no page")
            return s.url
        return None

    async def _plain(tree, ctx, agent_id):
        return tree

    monkeypatch.setattr(browser, "_run_in_session", _run)
    monkeypatch.setattr(browser, "_maybe_summarize_snapshot", _plain)
    saved = dict(browser._active_browser_sessions)
    browser._active_browser_sessions.clear()
    browser._active_browser_sessions["agent_ark"] = sess
    yield sess
    browser._active_browser_sessions.clear()
    browser._active_browser_sessions.update(saved)


# -- the cards say where the action leads -----------------------------------------------------

def test_a_click_on_a_link_names_its_resolved_href_and_a_foreign_origin(tmp_path, session):
    session.destination = {"kind": "link", "href": "https://evil.example.net/steal?x=1",
                           "target": ""}
    ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e4"})
    assert "https://evil.example.net/steal?x=1" in ask.details
    assert "ANOTHER origin" in ask.details and "https://evil.example.net" in ask.details


def test_a_click_on_a_same_origin_link_says_so(tmp_path, session):
    session.destination = {"kind": "link", "href": "https://example.com/settings",
                           "target": "_blank"}
    ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e4"})
    assert "https://example.com/settings" in ask.details
    assert "same origin" in ask.details and "new tab" in ask.details


def test_a_click_on_a_submit_button_names_the_form_action_and_method(tmp_path, session):
    session.destination = {"kind": "form", "action": "https://pay.other.test/charge",
                           "method": "post", "submits": True}
    ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e9"})
    assert "submits the form to https://pay.other.test/charge by POST" in ask.details
    assert "ANOTHER origin" in ask.details


def test_a_click_that_runs_a_script_says_the_destination_is_unknown(tmp_path, session):
    session.destination = {"kind": "script"}
    ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e2"})
    assert browser.UNKNOWN_DESTINATION in ask.details
    assert browser.UNKNOWN_DESTINATION == "destination unknown (script)"


def test_a_destination_that_cannot_be_read_is_unknown(tmp_path, session):
    session.destination = ValueError("ref is stale")
    ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e2"})
    assert browser.UNKNOWN_DESTINATION in ask.details


def test_a_card_with_no_open_page_says_unknown(tmp_path):
    saved = dict(browser._active_browser_sessions)
    browser._active_browser_sessions.clear()
    try:
        ask = browser._browser_click_approval(_ctx(tmp_path, FRIEND), {"ref_or_selector": "@e2"})
    finally:
        browser._active_browser_sessions.update(saved)
    assert browser.UNKNOWN_DESTINATION in ask.details


def test_a_fill_card_names_the_text_and_the_form_action(tmp_path, session):
    session.destination = {"kind": "form", "action": "https://example.com/login",
                           "method": "post", "submits": False}
    ask = browser._browser_fill_approval(
        _ctx(tmp_path, FRIEND), {"ref_or_selector": "@e3", "text": "hunter2"})
    assert "hunter2" in ask.details
    assert "https://example.com/login by POST" in ask.details


def test_a_fill_card_for_a_field_outside_any_form_says_so(tmp_path, session):
    session.destination = {"kind": "noform"}
    ask = browser._browser_fill_approval(
        _ctx(tmp_path, FRIEND), {"ref_or_selector": "@e3", "text": "x"})
    assert "not inside a form" in ask.details


def test_a_select_card_names_the_option_and_the_form_action(tmp_path, session):
    session.destination = {"kind": "form", "action": "https://elsewhere.test/go",
                           "method": "get", "submits": False, "onchange": True}
    ask = browser._browser_select_approval(
        _ctx(tmp_path, FRIEND), {"ref_or_selector": "@e5", "label": "Delete all"})
    assert "label='Delete all'" in ask.details
    assert "https://elsewhere.test/go by GET" in ask.details and "onchange" in ask.details


def test_the_three_action_tools_carry_these_card_builders():
    entries = {e.name: e for e in browser.get_tools()}
    assert entries["browser_click"].approval_check is browser._browser_click_approval
    assert entries["browser_fill"].approval_check is browser._browser_fill_approval
    assert entries["browser_select"].approval_check is browser._browser_select_approval


# -- the mark is re-read after an action that moves the page ----------------------------------

@pytest.mark.asyncio
async def test_a_foreign_click_that_leaves_the_approved_origin_blocks_reading_the_page(
        tmp_path, session):
    turn = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(turn, INBOX)
    assert "the inbox" in await browser.browser_extract(turn)
    session.after_click = "https://evil.example.net/landing"
    await browser.browser_click(turn, "@e4")
    result = await browser.browser_extract(turn)
    assert result.startswith("⚠️") and "https://evil.example.net" in result
    assert "browser_navigate" in result


@pytest.mark.asyncio
async def test_a_foreign_click_inside_the_approved_origin_keeps_the_page_readable(
        tmp_path, session):
    turn = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(turn, INBOX)
    session.after_click = "https://example.com/inbox/42"
    await browser.browser_click(turn, "@e4")
    assert "the inbox" in await browser.browser_extract(turn)


@pytest.mark.asyncio
async def test_a_fresh_approved_navigation_clears_the_moved_mark(tmp_path, session):
    turn = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(turn, INBOX)
    session.after_click = "https://evil.example.net/landing"
    await browser.browser_click(turn, "@e4")
    await browser.browser_navigate(turn, "https://evil.example.net/landing")
    assert "the inbox" in await browser.browser_extract(turn)
    # the approval covers the origin it was given for, so a move off it marks again
    session.after_click = "https://third.example.org/"
    await browser.browser_click(turn, "@e4")
    assert (await browser.browser_extract(turn)).startswith("⚠️")


@pytest.mark.asyncio
async def test_a_foreign_action_whose_page_url_cannot_be_read_fails_closed(tmp_path, session):
    turn = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(turn, INBOX)
    session.url = None
    await browser.browser_click(turn, "@e4")
    session.url = INBOX
    assert (await browser.browser_extract(turn)).startswith("⚠️")


@pytest.mark.asyncio
async def test_the_owners_own_click_resets_the_mark_as_owner(tmp_path, session):
    foreign = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(foreign, INBOX)
    await browser.browser_click(_ctx(tmp_path, MIKE), "@e4")
    result = await browser.browser_extract(foreign)
    assert result.startswith("⚠️") and "owner's own turn" in result


@pytest.mark.asyncio
async def test_the_owners_click_off_the_origin_does_not_mark_the_page_for_the_owner(
        tmp_path, session):
    owner = _ctx(tmp_path, MIKE)
    await browser.browser_navigate(owner, INBOX)
    session.after_click = "https://elsewhere.test/"
    await browser.browser_click(owner, "@e4")
    assert session._moved_to_origin is None
    assert "the inbox" in await browser.browser_extract(owner)


# -- the page-side reader, run in a real browser when one is installed ------------------------

_READER_HTML = """<base href="https://example.com/app/">
<a id=a1 href="/x?y=1"><span id=s1>go</span></a>
<a id=a2 href="javascript:void(0)">js</a>
<div id=d1 onclick="x()">div</div>
<form action="https://pay.test/c" method="POST"><input id=f1 name=action>
<button id=b1><b id=b2>pay</b></button><button id=b3 type=button>plain</button>
<button id=b4 formaction="/alt" formmethod=get>alt</button>
<select id=sel onchange="this.form.submit()"><option>1</option></select></form>
<input id=lonely>"""


@pytest.fixture(scope="module")
def reader_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        chromium = pw.chromium.launch()
    except Exception as e:
        pytest.skip(f"no browser to run the page-side reader in: {type(e).__name__}")
    page = chromium.new_page()
    page.set_content(_READER_HTML)
    yield page
    chromium.close()
    pw.stop()


@pytest.mark.parametrize("selector, intent, expected", [
    ("#s1", "click", {"kind": "link", "href": "https://example.com/x?y=1"}),
    ("#a2", "click", {"kind": "script"}),
    ("#d1", "click", {"kind": "script"}),
    ("#b3", "click", {"kind": "script"}),
    ("#b2", "click", {"kind": "form", "action": "https://pay.test/c", "method": "post"}),
    ("#b4", "click", {"kind": "form", "action": "https://example.com/alt", "method": "get"}),
    ("#f1", "fill", {"kind": "form", "action": "https://pay.test/c", "method": "post"}),
    ("#sel", "select", {"kind": "form", "action": "https://pay.test/c", "onchange": True}),
    ("#lonely", "fill", {"kind": "noform"}),
])
def test_the_page_side_reader_resolves_links_and_form_actions(reader_page, selector, intent,
                                                              expected):
    got = reader_page.locator(selector).evaluate(browser._ACTION_DESTINATION_JS, intent)
    assert {k: got.get(k) for k in expected} == expected


# -- a session that cannot carry the marks must not break a browse ---------------------------

def test_a_session_that_cannot_carry_the_marks_does_not_break_the_binding(
        tmp_path):
    bare = object()
    browser._note_turn_of_binding(bare, _ctx(tmp_path, FRIEND), INBOX)  # must not raise
    browser._reevaluate_mark_after_action(bare, _ctx(tmp_path, FRIEND), "https://x.test/")


class _Marked:
    """A scoped session stand-in that reports the etld1 scope but refuses every mark."""
    __slots__ = ("_etld1s", "_anonymous")

    def __init__(self, _inner):
        self._etld1s = {"example.com"}
        self._anonymous = False


def test_a_page_whose_marks_cannot_be_written_is_refused_to_a_foreign_turn_and_read_by_the_owner(
        tmp_path):
    session = _Marked(None)
    browser._note_turn_of_binding(session, _ctx(tmp_path, FRIEND), INBOX)
    refusal = browser._owner_page_refusal(_ctx(tmp_path, FRIEND), session, "browser_extract")
    assert refusal is not None and refusal.startswith("⚠️")
    assert browser._owner_page_refusal(_ctx(tmp_path, MIKE), session, "browser_extract") is None
