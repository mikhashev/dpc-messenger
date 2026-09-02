"""The balance panel must not answer a dead route with the word "error".

Measured 2026-09-02: with api.deepseek.com unreachable, the Account balance panel
showed a bare "error" and nothing else. The backend does return the reason —
`{"status": "error", "message": str(e)}` — but the exception it stringifies is
`httpx.ConnectTimeout`, whose `str()` is the empty string, so the frontend's
`message || 'error'` fell through to the placeholder.

Why it is worth a test rather than a shrug: this project's standing decision is to
spend the DeepSeek balance down to zero. In that state an unexplained failure of
the *balance* call reads as "the money ran out" — which is the one reading the
screen must not invite when the real cause is that nothing answered.
"""

import httpx
import pytest


def test_the_transport_exception_this_is_about_carries_no_message():
    """The premise, pinned: if httpx ever starts filling this in, the guard below
    becomes redundant rather than wrong, and this test says which it is."""
    assert str(httpx.ConnectTimeout("")) == ""


def test_a_message_less_exception_still_names_its_class():
    """What `service.get_provider_balance` now returns instead of an empty string.

    The expression is reproduced rather than the whole method called: the method
    needs a provider registry, a configured alias and a live client to reach its
    own except branch, and none of those is what this is about.
    """
    e = httpx.ConnectTimeout("")
    assert (str(e) or type(e).__name__) == "ConnectTimeout"


def test_an_exception_that_does_have_a_message_keeps_it():
    """The control: the class name is a fallback, not a replacement."""
    e = RuntimeError("Insufficient balance")
    assert (str(e) or type(e).__name__) == "Insufficient balance"


def test_the_two_causes_are_distinguishable_by_what_the_panel_shows():
    """The property that matters, stated as one assertion.

    'No money' and 'no route' must not render as the same string.
    """
    no_route = httpx.ConnectTimeout("")
    no_money = RuntimeError("Insufficient balance")

    shown = lambda e: (str(e) or type(e).__name__)

    assert shown(no_route) != shown(no_money)
