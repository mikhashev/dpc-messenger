"""The other half of BROWSE-PAGE-SILENTLY-TRUNCATES: three browser tools that
either cut, substituted or under-collected without saying which.

Measured 2026-08-23, before the fix:
  - `_truncate_snapshot` advised "use browser_snapshot for full content" while
    printing from inside browser_snapshot — the advice pointed at the call that
    had just truncated; the handle that returns the tree is `raw=True`.
  - the LLM summarisation path returned the auxiliary model's prose with no
    marker at all, the only silent substitution in the tool set.
  - `browser_collect` printed "Collected 120 items (30 scrolls)" whether the
    list had ended or the scroll budget had.
  - `fetch_json` printed a bare "... (truncated)": no size, no continuation,
    and the cut landed mid-structure so the remainder would not parse.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    _summary_notice,
    _truncate_snapshot,
    fetch_json,
)


def _tree(lines: int) -> str:
    return "\n".join(f"text 'row number {i} of the page'" for i in range(lines))


class TestTheSnapshotMarkerNamesTheHandleThatWorks:
    def test_it_no_longer_points_at_the_call_that_truncated(self):
        marker = _truncate_snapshot(_tree(4000), max_chars=5000)
        assert "browser_snapshot(raw=True)" in marker
        assert "use browser_snapshot for full content" not in marker

    def test_it_names_how_many_lines_of_how_many_and_the_cap(self):
        out = _truncate_snapshot(_tree(4000), max_chars=5000)
        tail = out.rsplit("[", 1)[1]
        assert " of 4000 lines truncated at" in tail
        assert "5000 chars" in tail

    def test_a_snapshot_under_the_cap_is_untouched(self):
        small = _tree(3)
        assert _truncate_snapshot(small, max_chars=5000) == small


class TestTheSummarisedSnapshotSaysItIsASummary:
    def test_the_notice_names_the_provider_both_sizes_and_the_raw_handle(self):
        notice = _summary_notice(120_000, 900, "local_qwen")
        assert "local_qwen" in notice
        assert "120000" in notice and "900" in notice
        assert "browser_snapshot(raw=True)" in notice
        assert "not the tree" in notice

    def test_a_model_rewrite_is_marked_before_it_reaches_the_agent(self):
        class _LLM:
            async def query(self, prompt, provider_alias=None):
                return "The page lists three products with prices."

        out = asyncio.run(browser_mod._llm_summarize_snapshot(
            _tree(4000), None, _LLM(), provider_alias="aux", max_chars=5000,
        ))
        assert out.startswith("[snapshot summarised by aux:")

    def test_the_fallback_path_is_not_dressed_up_as_a_summary(self):
        # No llm_manager -> line truncation, which must not claim a model
        # rewrote anything.
        out = asyncio.run(browser_mod._llm_summarize_snapshot(
            _tree(4000), None, None, provider_alias="aux", max_chars=5000,
        ))
        assert "summarised by" not in out
        assert "browser_snapshot(raw=True)" in out


class TestCollectSaysWhyItStopped:
    def _run(self, result: dict) -> str:
        class _Ctx:
            class agent_root:
                name = "agent_x"

        async def _fake_run(session, verb, *args):
            return result

        with patch.object(browser_mod, "_get_session_or_error", lambda _id: object()), \
             patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
             patch.object(browser_mod, "_run_in_session", _fake_run):
            return asyncio.run(browser_mod.browser_collect(
                _Ctx(), container="#feed", item_selector=".card",
            ))

    def test_the_budget_running_out_is_not_reported_as_a_finished_list(self):
        out = self._run({
            "items": [{"text": "a"}], "total": 120, "scrolls_done": 30,
            "max_scrolls": 30, "stop_reason": "scroll_budget_exhausted",
        })
        assert "INCOMPLETE" in out
        assert "max_scrolls=30" in out

    def test_a_finished_list_says_so(self):
        out = self._run({
            "items": [{"text": "a"}], "total": 120, "scrolls_done": 7,
            "max_scrolls": 30, "stop_reason": "list_exhausted",
        })
        assert "this is the whole list" in out
        assert "INCOMPLETE" not in out

    def test_a_scroll_that_threw_is_not_silent(self):
        out = self._run({
            "items": [], "total": 0, "scrolls_done": 3,
            "max_scrolls": 30, "stop_reason": "scroll_failed: TimeoutError",
        })
        assert "INCOMPLETE" in out
        assert "TimeoutError" in out

    def test_the_two_endings_do_not_render_identically(self):
        common = {"items": [], "total": 120, "scrolls_done": 30, "max_scrolls": 30}
        ended = self._run({**common, "stop_reason": "list_exhausted"})
        ran_out = self._run({**common, "stop_reason": "scroll_budget_exhausted"})
        assert ended.split("\n")[0] != ran_out.split("\n")[0]


_INERT_PAGE = (
    "<html><head><title>t</title></head><body>"
    "<div><h1>Example Domain</h1><p>This domain is for use in examples.</p></div>"
    "</body></html>"
)
_APP_SHELL = (
    '<html><head><script src="/bundle.js"></script></head>'
    '<body><div id="root"></div><script>window.start()</script></body></html>'
)


class TestPageSignals:
    def test_a_closed_document_is_distinguished_from_a_cut_one(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        assert _page_signals(_INERT_PAGE, "text")["document_closed"] is True
        cut = _INERT_PAGE[: len(_INERT_PAGE) // 2]
        assert _page_signals(cut, "text")["document_closed"] is False

    def test_a_page_with_no_scripts_cannot_render_anything_more(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        sig = _page_signals(_INERT_PAGE, "short")
        assert sig["script_tags"] == 0
        assert sig["app_shell_markers"] == []
        assert sig["js_capable"] is False

    def test_an_app_shell_is_recognised(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        sig = _page_signals(_APP_SHELL, "")
        assert sig["script_tags"] == 2
        assert 'id="root"' in sig["app_shell_markers"]
        assert sig["js_capable"] is True


class TestNeedsJsNoLongerFiresOnCompleteShortPages:
    def _sync(self, html, monkeypatch):
        from dpc_client_core.dpc_agent.tools import browser as b
        monkeypatch.setattr(b, "_fetch_url", lambda url, timeout=30: {
            "success": True, "content": html, "content_type": "text/html",
        })
        return b._browse_sync("https://example.test/")

    def test_a_short_but_inert_page_does_not_buy_a_browser_launch(self, monkeypatch):
        # Measured on the live example.com: 113 chars of text, 0 script tags,
        # a complete document. The old rule (text < 200) spent 7-10s on
        # Camoufox for a page no browser could add anything to.
        res = self._sync(_INERT_PAGE, monkeypatch)
        assert len(res["text"] or "") < 200
        assert res["needs_js"] is False

    def test_a_short_app_shell_still_asks_for_the_browser(self, monkeypatch):
        res = self._sync(_APP_SHELL, monkeypatch)
        assert res["needs_js"] is True


class TestTheCompletenessHeader:
    def _header(self, **kw):
        from dpc_client_core.dpc_agent.tools.browser import (
            _completeness_header, _page_signals,
        )
        args = dict(
            url="https://example.test/a",
            sig=_page_signals(_APP_SHELL, "x" * 400),
            renderer="static",
            rendered_chars=None,
            shown=100,
            total=400,
            preset="m",
        )
        args.update(kw)
        return _completeness_header(**args)

    def test_it_states_the_preset_cut_separately_from_everything_else(self):
        assert "preset m kept 100 of 400 chars" in self._header()

    def test_it_says_when_the_preset_cut_nothing(self):
        assert "did not cut this" in self._header(shown=400)

    def test_a_js_page_is_never_reported_as_complete(self):
        h = self._header()
        assert "JS NOT executed" in h
        assert "absence is NOT established" in h
        assert "verify=true" in h

    def test_an_inert_page_says_nothing_more_can_render(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        h = self._header(sig=_page_signals(_INERT_PAGE, "x" * 400))
        assert "nothing further can render" in h
        assert "verify=true" not in h

    def test_a_closed_document_is_not_sold_as_the_right_document(self):
        h = self._header()
        assert "was not cut short" in h
        assert "login wall is also a complete document" in h

    def test_an_unclosed_document_is_flagged(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        h = self._header(sig=_page_signals(_APP_SHELL[:80], "x" * 400))
        assert "does NOT end with </html>" in h

    def test_a_rendered_page_carries_the_snapshot_caveat(self):
        h = self._header(renderer="camoufox", rendered_chars=9000)
        assert "JS executed" in h
        assert "can still load more on scroll" in h

    def test_it_never_invents_a_percentage_of_the_page(self):
        assert "%" not in self._header()


class TestBrowsePageAnonymousOutput:
    def _run(self, monkeypatch, html, text, verify=False, js_text=None):
        from dpc_client_core.dpc_agent.tools import browser as b

        def _sync(url):
            sig = b._page_signals(html, text)
            return {
                "success": True, "text": text, "signals": sig,
                "needs_js": len(text) < 200 and sig["js_capable"],
            }

        async def _fake_js(url, agent_id):
            return js_text

        monkeypatch.setattr(b, "_browse_sync", _sync)
        monkeypatch.setattr(b, "_fetch_js_text", _fake_js)

        class _Ctx:
            agent_root = None  # no agent -> no audit row in this unit test

        return asyncio.run(b.browse_page(_Ctx(), url="https://example.test/a",
                                         size="s", verify=verify))

    def test_the_header_comes_before_the_content(self, monkeypatch):
        out = self._run(monkeypatch, _APP_SHELL, "body text here")
        assert out.startswith("[browse_page https://example.test/a |")

    def test_verify_renders_and_reports_what_the_browser_saw(self, monkeypatch):
        rendered = "a much longer rendered body"
        out = self._run(monkeypatch, _APP_SHELL, "short",
                        verify=True, js_text=rendered)
        head = out.split("\n", 1)[0]
        assert "JS executed" in head
        assert f"{len(rendered)} chars" in head
        assert rendered in out

    def test_verify_that_finds_nothing_more_does_not_claim_a_render(self, monkeypatch):
        out = self._run(monkeypatch, _APP_SHELL, "x" * 500,
                        verify=True, js_text="tiny")
        head = out.split("\n", 1)[0]
        assert "JS NOT executed" in head
        assert "verify ran: browser saw 4 chars" in head


class TestFetchJsonWindows:
    BIG = json.dumps(
        {"items": [{"i": i, "text": "y" * 80} for i in range(400)]},
        indent=2, ensure_ascii=False,
    )

    def _fetch(self, **kw) -> str:
        with patch.object(
            browser_mod, "_fetch_url",
            lambda url, timeout=30: {"success": True, "content": self.BIG, "error": None},
        ):
            return fetch_json(None, "https://example.test/api", **kw)

    def test_the_first_window_names_the_total_and_the_next_offset(self):
        out = self._fetch()
        head = out.split("\n", 1)[0]
        assert f"{len(self.BIG)} chars pretty-printed" in head
        assert "next window: fetch_json(url, offset=10000)" in head

    def test_it_says_what_the_document_is_without_asking_the_network_again(self):
        # The offset says how much is missing; the shape says what is
        # missing. json.loads has already built the whole object by this
        # point, so the shape costs nothing.
        head = self._fetch().split("\n", 1)[0]
        assert "object with 1 top-level keys [items]" in head

    def test_the_shape_line_covers_arrays_and_scalars_too(self):
        from dpc_client_core.dpc_agent.tools.browser import _json_shape
        assert _json_shape([1, 2, 3]) == "array of 3 items, first is int"
        assert _json_shape([]) == "empty array"
        assert _json_shape(7) == "scalar (int)"
        assert _json_shape({"a": 1, "b": 2}).startswith("object with 2 top-level keys [a, b]")

    def test_it_warns_that_a_window_is_not_parseable_json(self):
        head = self._fetch().split("\n", 1)[0]
        assert "not parseable JSON" in head
        with pytest.raises(json.JSONDecodeError):
            json.loads(self._fetch().split("\n\n", 1)[1])

    def test_it_names_the_price_of_the_next_window(self):
        assert "RE-FETCHES" in self._fetch().split("\n", 1)[0]

    def test_the_named_offset_returns_the_next_slice(self):
        first = self._fetch()
        second = self._fetch(offset=10000)
        body_1 = first.split("\n\n", 1)[1]
        body_2 = second.split("\n\n", 1)[1]
        assert body_2 == self.BIG[10000:20000]
        assert not body_2.startswith(body_1[:200])

    def test_the_final_window_says_it_is_final(self):
        last_start = (len(self.BIG) // 10000) * 10000
        head = self._fetch(offset=last_start).split("\n", 1)[0]
        assert "final window" in head
        assert "next window" not in head

    def test_an_offset_past_the_end_says_where_the_last_window_starts(self):
        out = self._fetch(offset=len(self.BIG) + 5000)
        assert "past the" in out
        assert "the last window starts at offset=" in out

    def test_a_document_that_fits_comes_back_whole_and_parseable(self):
        small = json.dumps({"ok": True}, indent=2)
        with patch.object(
            browser_mod, "_fetch_url",
            lambda url, timeout=30: {"success": True, "content": small, "error": None},
        ):
            out = fetch_json(None, "https://example.test/api")
        assert out.startswith("JSON from ")
        assert json.loads(out.split("\n\n", 1)[1]) == {"ok": True}


class TestCollectScrollsNoMoreThanItWasAsked:
    """`for _ in range(max_scrolls + 1)` scrolled once past the budget and
    reported it: Ark, live on Hacker News 2026-08-23, saw «6 scrolls» against
    `max_scrolls=5`. The extra scroll also moved the page with nothing left to
    read it — the loop collects, then decides whether another scroll is worth
    making."""

    def _run(self, max_scrolls, pages):
        """`pages[i]` is what the collector JS returns on pass i."""
        from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

        calls = {"scrolls": 0, "evaluates": 0}

        class _Page:
            url = "https://example.test/feed"

            def evaluate(self, js, arg):
                i = min(calls["evaluates"], len(pages) - 1)
                calls["evaluates"] += 1
                return pages[i]

        class _Self:
            _page = _Page()

            def _require_open(self):
                pass

            def scroll(self, direction, amount):
                calls["scrolls"] += 1

            def _audit_action(self, *a, **kw):
                pass

        res = AuthBrowser.collect(
            _Self(), "#feed", ".card", ["text"],
            max_scrolls=max_scrolls, scroll_pause_ms=0, dedup_by="text",
        )
        return res, calls

    def _items(self, *texts):
        return {"items": [{"text": t} for t in texts]}

    def test_it_never_scrolls_more_times_than_the_budget(self):
        growing = [self._items(*[f"item{i}" for i in range(n)]) for n in range(1, 12)]
        res, calls = self._run(5, growing)
        assert calls["scrolls"] == 5
        assert res["scrolls_done"] == 5
        assert res["scrolls_done"] <= res["max_scrolls"]

    def test_the_last_pass_collects_after_the_last_scroll(self):
        growing = [self._items(*[f"item{i}" for i in range(n)]) for n in range(1, 12)]
        res, calls = self._run(3, growing)
        # one collect per pass, and the passes are budget + 1: collect, scroll,
        # collect, scroll, collect, scroll, collect.
        assert calls["evaluates"] == calls["scrolls"] + 1

    def test_a_page_with_nothing_more_to_load_is_not_told_to_raise_the_budget(self):
        static = [self._items("a", "b", "c")] * 12
        res, _ = self._run(5, static)
        assert res["stop_reason"] == "scroll_budget_exhausted"
        assert res["consecutive_empty"] > 0

    def test_a_growing_list_reports_no_idle_scrolls(self):
        growing = [self._items(*[f"item{i}" for i in range(n)]) for n in range(1, 12)]
        res, _ = self._run(5, growing)
        assert res["consecutive_empty"] == 0


class TestCollectHeaderSeparatesTwoWaysOfRunningOut:
    def _header(self, **over):
        import asyncio as _a
        from unittest.mock import patch
        from dpc_client_core.dpc_agent.tools import browser as b

        result = {"items": [], "total": 30, "scrolls_done": 5, "max_scrolls": 5,
                  "stop_reason": "scroll_budget_exhausted", "consecutive_empty": 0}
        result.update(over)

        async def _fake_run(session, verb, *args):
            return result

        class _Ctx:
            class agent_root:
                name = "agent_x"

        with patch.object(b, "_get_session_or_error", lambda _id: object()), \
             patch.object(b, "_get_session_lock", lambda _id: _a.Lock()), \
             patch.object(b, "_run_in_session", _fake_run):
            return _a.run(b.browser_collect(_Ctx(), container="#f", item_selector=".c"))

    def test_a_still_growing_list_is_told_to_raise_the_budget(self):
        h = self._header(consecutive_empty=0).split("\n")[0]
        assert "still growing" in h
        assert "raise max_scrolls to collect more" in h

    def test_a_page_that_stopped_giving_is_not(self):
        h = self._header(consecutive_empty=4).split("\n")[0]
        assert "last 4 scroll(s) added nothing" in h
        assert "raising max_scrolls changes nothing" in h
        assert "raise max_scrolls to collect more" not in h


class TestTheOtherTwoBrowsePagePathsAnnounceTheSameThings:
    """The header closed BROWSE-PAGE-SILENTLY-TRUNCATES on one return path of
    three. `use_auth` (:2874) and `keep_open` (:2889) kept the pre-08-23 shape:
    `Content from … (markdown, auth=…/headed, N chars)` — no transport
    statement, no renderer statement, no preset statement, and the cut notice
    in the TAIL, which is exactly where `_truncate_tool_result` eats it.

    Observed 2026-08-24 in agent_001's own stored tool calls:
      `Content from https://tomsk.hh.ru/… (markdown, headed, 407 chars)`
    — 407 characters of a login wall, on the one path where "is this the page
    you asked for or a wall" is the whole question.
    """

    def _ctx(self):
        class _Root:
            name = "agent_x"

        class _Ctx:
            agent_root = _Root()

        return _Ctx()

    def _headed(self, monkeypatch, html, size="s"):
        from dpc_client_core.dpc_agent.tools import browser as b

        async def _sess(agent_id, domains, headed):
            return object()

        async def _nav(session, agent_id, url, domains):
            return session

        async def _run(session, verb, *args):
            return html

        monkeypatch.setattr(b, "_get_or_create_session_async", _sess)
        monkeypatch.setattr(b, "_navigate_with_recovery", _nav)
        monkeypatch.setattr(b, "_run_in_session", _run)
        return asyncio.run(b.browse_page(
            self._ctx(), url="https://example.test/a", size=size, keep_open=True,
        ))

    def _auth(self, monkeypatch, html, size="s"):
        from dpc_client_core import web_auth as wa
        from dpc_client_core.dpc_agent.tools import browser as b

        monkeypatch.setattr(wa, "audit_append", lambda *a, **k: None)
        monkeypatch.setattr(
            b, "_auth_browse_html", lambda agent_id, domain, url, headed: html,
        )
        return asyncio.run(b.browse_page(
            self._ctx(), url="https://example.test/a", size=size,
            use_auth="example.test",
        ))

    # --- the header is there at all, and it is a prefix -------------------

    def test_the_headed_path_leads_with_the_header(self, monkeypatch):
        out = self._headed(monkeypatch, _APP_SHELL)
        assert out.startswith("[browse_page https://example.test/a |")

    def test_the_auth_path_leads_with_the_header(self, monkeypatch):
        out = self._auth(monkeypatch, _APP_SHELL)
        assert out.startswith("[browse_page https://example.test/a |")

    # --- the three statements the entry was opened for --------------------

    def test_the_headed_path_states_the_transport(self, monkeypatch):
        head = self._headed(monkeypatch, _APP_SHELL).split("\n", 1)[0]
        assert "transport: body ends with </html>" in head

    def test_the_auth_path_flags_a_body_that_was_cut(self, monkeypatch):
        cut = _APP_SHELL[: len(_APP_SHELL) // 2]
        head = self._auth(monkeypatch, cut).split("\n", 1)[0]
        assert "transport: body does NOT end with </html>" in head

    def test_both_paths_say_a_real_browser_rendered_the_page(self, monkeypatch):
        # Both are a live browser, so the honest sentence is the snapshot
        # caveat, not "JS NOT executed" — that one belongs to a static fetch.
        for out in (self._headed(monkeypatch, _APP_SHELL),
                    self._auth(monkeypatch, _APP_SHELL)):
            head = out.split("\n", 1)[0]
            assert "JS executed" in head
            assert "visible at the moment of the snapshot" in head

    def test_both_paths_state_the_preset_in_the_head(self, monkeypatch):
        long_html = _APP_SHELL.replace("</body>", "<p>" + "y" * 9000 + "</p></body>")
        for out in (self._headed(monkeypatch, long_html),
                    self._auth(monkeypatch, long_html)):
            head = out.split("\n", 1)[0]
            assert "preset s kept " in head

    # --- and the cut notice is no longer in the tail ----------------------

    def test_neither_path_leaves_the_cut_notice_where_the_harness_eats_it(
        self, monkeypatch,
    ):
        long_html = _APP_SHELL.replace("</body>", "<p>" + "y" * 9000 + "</p></body>")
        for out in (self._headed(monkeypatch, long_html),
                    self._auth(monkeypatch, long_html)):
            assert "... (truncated," not in out

    # --- and what the old line did carry is not lost ----------------------

    def test_the_session_that_served_the_page_is_still_named(self, monkeypatch):
        assert "headed browser, no auth domain named" in self._headed(monkeypatch, _APP_SHELL)
        assert "auth domain example.test" in self._auth(monkeypatch, _APP_SHELL)


class TestTheTransportDetectorDoesNotReportOnBytesItNeverRead:
    """Half the only signal this instrument produced was its own artefact.

    Measured 2026-08-24 over the 41 `fetch_anonymous` audit rows collected
    since the line landed: 8 carried `document_closed=false`, and 4 of those 8
    had `html_chars=0` — rows where no HTML body was read at all. A body that
    was never read cannot end in `</html>`, so the detector was answering
    "cut" to a question nobody had asked, and the count that was supposed to
    say how often a transport is cut was 50 % noise.
    """

    def test_no_body_means_unknown_rather_than_cut(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        assert _page_signals("", "clean text arrived another way")[
            "document_closed"
        ] is None

    def test_a_body_that_was_read_still_answers(self):
        from dpc_client_core.dpc_agent.tools.browser import _page_signals
        assert _page_signals(_INERT_PAGE, "t")["document_closed"] is True
        half = _INERT_PAGE[: len(_INERT_PAGE) // 2]
        assert _page_signals(half, "t")["document_closed"] is False

    def test_the_header_says_nothing_about_a_transport_it_cannot_see(self):
        from dpc_client_core.dpc_agent.tools.browser import (
            _completeness_header, _page_signals,
        )
        head = _completeness_header(
            "https://example.test/a", _page_signals("", "text"),
            "static", None, 4, 4, "m",
        )
        assert "transport:" not in head
