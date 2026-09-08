"""A cut that names no continuation is a dead end.

`browse_page` was the one content tool with nothing to continue to: the loop
caps every tool result at `TOOL_RESULT_CHAR_CAP` and its marker tells the agent
to follow the continuation the tool named — and this tool named none. The
header's own advice was worse than silence, because it offered a bigger preset,
which the same cap takes away again, and it offered 'l' to a caller who had
just passed 'l'.

The observed cost, 2026-09-08 (agent_001, article of 35 618 chars): the page
was fetched whole, the model saw 15 000 of it, and the third part of the
article was written from a Russian retelling on another site.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.loop import TOOL_RESULT_CHAR_CAP
from dpc_client_core.dpc_agent.tools import browser


def _ctx(tmp_path):
    return SimpleNamespace(
        agent_root=SimpleNamespace(name="agent_test"),
        repo_path=lambda rel: tmp_path / rel,
        firewall=None,
    )


def _sig(html_chars=1000):
    return {"html_chars": html_chars, "document_closed": True, "js_capable": False}


# --- the file ---------------------------------------------------------------


def test_the_file_holds_the_page_and_not_the_window(tmp_path):
    """Saving after the preset cut would write the answer back to disk."""
    ctx = _ctx(tmp_path)
    text = "x" * 40000

    saved, warning = browser._save_page_markdown(ctx, "page.md", text)

    assert warning is None
    assert (tmp_path / "page.md").read_text(encoding="utf-8") == text
    assert saved.endswith("page.md")


def test_no_save_to_writes_nothing(tmp_path):
    ctx = _ctx(tmp_path)

    assert browser._save_page_markdown(ctx, None, "body") == (None, None)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_is_reported_and_does_not_lose_the_page(tmp_path):
    """The fetch succeeded; a bad path must not turn that into an error."""
    def _boom(rel):
        raise PermissionError("read-only")

    ctx = SimpleNamespace(
        agent_root=SimpleNamespace(name="agent_test"), repo_path=_boom, firewall=None,
    )

    saved, warning = browser._save_page_markdown(ctx, "nope.md", "body")

    assert saved is None
    assert "save_to 'nope.md' failed" in warning
    assert "PermissionError" in warning


# --- what the header offers next --------------------------------------------


def test_the_header_names_the_file_when_there_is_one():
    header = browser._completeness_header(
        "http://x", _sig(), "static", None, 10000, 40000, "m",
        saved_to="/agents/a/page.md",
    )

    assert "saved: all 40000 chars written to /agents/a/page.md" in header
    assert "read_file(path, offset=, limit=)" in header
    assert "use size=" not in header


def test_it_does_not_offer_the_preset_the_caller_already_passed():
    """«use size='l' or 'f'» answered 'l' with 'l'."""
    header = browser._completeness_header(
        "http://x", _sig(), "static", None, 25000, 40000, "l",
    )

    assert "use size='f'" in header
    assert "'l' or 'f'" not in header
    assert "save_to='page.md'" in header


def test_the_largest_preset_is_offered_no_bigger_one():
    header = browser._completeness_header(
        "http://x", _sig(), "static", None, 40000, 90000, "f",
    )

    assert "use size=" not in header
    assert "save_to='page.md'" in header


def test_a_full_preset_still_warns_about_the_cap_below_it():
    """This is the case that produced the incident: nothing was cut here."""
    total = TOOL_RESULT_CHAR_CAP + 1
    header = browser._completeness_header(
        "http://x", _sig(), "static", None, total, total, "f",
    )

    assert f"preset f did not cut this: all {total} chars are here" in header
    assert f"cut again at {TOOL_RESULT_CHAR_CAP} chars" in header
    assert "save_to='page.md'" in header


def test_a_short_page_is_not_given_a_warning_it_does_not_need():
    header = browser._completeness_header(
        "http://x", _sig(), "static", None, 500, 500, "m",
    )

    assert "preset m did not cut this: all 500 chars are here" in header
    assert "save_to" not in header
    assert "cut again" not in header


# --- the rendered paths carry it too ----------------------------------------


def test_the_browser_path_answer_names_the_file():
    answer = browser._rendered_page_answer(
        "http://x", "<html></html>", "y" * 30000, "m",
        session="headed browser, no auth domain named",
        saved_to="/agents/a/page.md",
    )

    assert answer.startswith("[browse_page http://x")
    assert "saved: all 30000 chars written to /agents/a/page.md" in answer


@pytest.mark.parametrize("preset,expected_body", [("m", 10000), ("f", 30000)])
def test_saving_does_not_change_the_body_that_is_returned(preset, expected_body):
    """save_to is additive: the answer is what it always was, plus a header."""
    answer = browser._rendered_page_answer(
        "http://x", "<html></html>", "y" * 30000, preset,
        session="headed browser, no auth domain named",
        saved_to="/agents/a/page.md",
    )
    body = answer.split("]\n\n", 1)[1]

    assert len(body) == expected_body


# --- the wiring, where the ordering bug would live --------------------------


@pytest.mark.asyncio
async def test_the_anonymous_path_saves_the_page_not_the_preset_window(tmp_path, monkeypatch):
    """The save has to happen before the preset cut, and the test drives the
    real tool rather than the helper, because that ordering is the whole bug."""
    page = "z" * 40000
    monkeypatch.setattr(
        browser, "_browse_sync",
        lambda url: {
            "success": True, "text": page, "html": "<html></html>",
            "signals": _sig(), "needs_js": False,
        },
    )

    answer = await browser.browse_page(
        _ctx(tmp_path), "http://x", size="m", save_to="page.md",
    )

    assert (tmp_path / "page.md").read_text(encoding="utf-8") == page
    assert "saved: all 40000 chars written to" in answer
    assert len(answer.split("]\n\n", 1)[1]) == 10000
