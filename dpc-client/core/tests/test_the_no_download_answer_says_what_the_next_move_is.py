"""When a click starts no download, the answer names the next move.

No browser here: `_download_answer` is given the status dict `download()`
returns, which is the whole input the sentence is built from. What is under
test is the advice, and the advice differs on one fact only — whether the
page moved under the click.
"""

from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.registry import ToolContext


class _Firewall:
    def get_extended_write_enabled(self, profile_name=None):
        return True

    def get_extended_read_enabled(self, profile_name=None):
        return True

    def is_extended_path_allowed(self, path, require_write=False, profile_name=None):
        return False


@pytest.fixture()
def _ctx(tmp_path):
    return ToolContext(agent_root=tmp_path, firewall=_Firewall())


def _result(before: str, now: str, **extra) -> dict:
    base = {
        "status": "no_download",
        "url_before": before,
        "page_url": now,
        "tab_count": 1,
        "timeout_ms": 3000,
        "error": "TimeoutError",
        "cross_host_links": [],
    }
    base.update(extra)
    return base


def _answer(ctx, result) -> str:
    return browser_mod._download_answer(ctx, Path(ctx.agent_root), result, "")


def test_a_page_that_advanced_is_told_to_snapshot_it_and_take_the_new_link(
    _ctx,
):
    answer = _answer(
        _ctx, _result("http://site.test/page", "http://site.test/step2"),
    )
    assert "the URL changed" in answer
    assert "browser_snapshot" in answer
    assert "browser_download" in answer
    # Site-neutral: the advice may not name a host, an element or a step.
    assert "site.test/step2" in answer  # only as the fact, in the URL line
    advice = next(line for line in answer.splitlines() if "browser_snapshot" in line)
    assert "site.test" not in advice


def test_a_page_that_did_not_move_is_told_the_repeat_is_what_works(_ctx):
    answer = _answer(
        _ctx, _result("http://site.test/page", "http://site.test/page"),
    )
    assert "the URL did not change" in answer
    assert "browser_snapshot" in answer
    assert "again" in answer
    assert "browser_download" not in answer


def test_the_advice_is_one_or_two_lines_and_the_answer_stays_short(_ctx):
    for now in ("http://site.test/page", "http://site.test/step2"):
        answer = _answer(_ctx, _result("http://site.test/page", now))
        advice = [line for line in answer.splitlines() if "browser_snapshot" in line]
        assert 1 <= len(advice) <= 2, answer
        assert len(answer.splitlines()) <= 7, answer


def test_the_probes_a_stuck_click_ran_are_part_of_the_answer(_ctx):
    answer = _answer(
        _ctx,
        _result(
            "http://site.test/page", "http://site.test/page",
            probes=[
                "evaluate: passed",
                "raf: 1 in 250ms (starved — the window is not being painted)",
                "js_click: failed(TimeoutError)",
            ],
        ),
    )
    for line in ("evaluate: passed", "js_click: failed(TimeoutError)"):
        assert line in answer.splitlines() or any(
            line in row for row in answer.splitlines()
        ), answer


def test_an_unpainted_window_is_named_as_the_thing_to_fix(_ctx):
    answer = _answer(
        _ctx,
        _result(
            "http://site.test/p", "http://site.test/p", window_not_painted=True,
        ),
    )
    assert "minimised or off-screen" in answer
    assert "Restore it on screen" in answer


def test_a_click_that_reached_the_page_says_nothing_about_probes(_ctx):
    answer = _answer(_ctx, _result("http://site.test/p", "http://site.test/p"))
    assert "evaluate:" not in answer
    assert "js_click" not in answer
