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
