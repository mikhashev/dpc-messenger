"""An image the vision model could not read must not arrive as an analysis.

The provider stopped handing reasoning back in the answer's place
(`5a192a4f`); this is the same substitution one layer up — an empty
description announced as "here is the visual analysis" tells the agent it has
seen something it has not.
"""

from __future__ import annotations

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


def _inject(description):
    messages = [{"role": "user", "content": "what is in this picture?"}]
    out = DpcLlmAdapter._inject_image_description_into_messages(
        None, messages, description
    )
    return out[0]["content"]


def test_a_description_is_passed_through():
    text = _inject("A red bicycle against a wall.")
    assert "A red bicycle against a wall." in text
    assert "here is the visual analysis" in text.lower()


def test_an_empty_description_says_the_image_was_not_seen():
    text = _inject("")
    assert "returned no description" in text
    assert "you have not seen it" in text
    assert "here is the visual analysis" not in text.lower()


def test_whitespace_counts_as_empty():
    """A model that answers with a newline has still answered with nothing."""
    assert "returned no description" in _inject("   \n  ")


def test_the_users_own_message_survives_either_way():
    for d in ("a chart", ""):
        assert "what is in this picture?" in _inject(d)
