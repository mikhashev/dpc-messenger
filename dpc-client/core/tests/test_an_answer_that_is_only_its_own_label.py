"""A reply carrying nothing but the history marker is not a reply.

Measured 2026-08-29: an agent answered a question in the project room with
`[#74 | 06:42:57 | Johnny]` and nothing else — 25 characters, one round, no
tool calls, 1454 completion tokens spent. The loop's empty-content retry
tests `content.strip()`, so those 25 characters read as an answer: the run
completed, the message was signed and saved into the room's history, and
nothing anywhere said the reply was empty.

The marker itself comes from `context.history_prefix`, which puts
`[#idx | HH:MM:SS | sender] ` on every history line the model reads —
including the reader's own past turns, which is the whole reason a model
writes it: it has never seen its own words without one.

**2026-09-03, and this is why the shape matters.** The August fix caught the
one shape it had seen. The cause stayed, and five days later the same agent
posted 13 516 characters that were 436 copies of the marker and nothing else,
ending mid-token on the max_tokens cap. It got past this guard three ways at
once: the copies were bold-wrapped, there were many of them rather than one,
and what the cap left at the end was an unterminated opener. The count had
been climbing in plain sight for four turns — 2, 3, 4, then 436 — because the
marker a model writes is stored, and next turn the runtime prepends its own on
top of it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.loop import _is_answerless, _strip_history_markers


class TestWhatCountsAsAnswerless:
    @pytest.mark.parametrize("content", ["", "   ", "\n\n"])
    def test_nothing_at_all(self, content):
        assert _is_answerless(content) is True

    @pytest.mark.parametrize(
        "content",
        [
            "[#74 | 06:42:57 | Johnny]",
            "[#12 | 10:00:00 | Ark]  ",
            "[#5]",
            "  [#5 | 08:00:00 | Muse]\n",
        ],
    )
    def test_the_marker_alone_is_a_label_not_an_answer(self, content):
        assert _is_answerless(content) is True

    @pytest.mark.parametrize(
        "content",
        [
            "[#74 | 06:42:57 | Johnny]\nHere is the answer",
            "Real answer",
            "[not a marker]",
            "[#74 | 06:42:57 | Johnny] one line, with an answer on it",
        ],
    )
    def test_anything_behind_the_marker_is_an_answer(self, content):
        assert _is_answerless(content) is False

    @pytest.mark.parametrize(
        "content",
        [
            "**[#76 | 07:45:29 | Johnny]**",
            "[#76 | 07:45:29 | Johnny]\n\n**[#76 | 07:45:41 | Johnny]**",
            "**[#76 | 1 | J]**\n\n**[#76 | 2 | J]**\n\n**[#76 | 3 | J]**\n\n**[#76 | 4 | J]**",
        ],
    )
    def test_a_run_of_markers_is_no_more_an_answer_than_one(self, content):
        """Bold, and repeated. Both were true of the 436."""
        assert _is_answerless(content) is True

    def test_what_the_token_cap_leaves_is_not_an_answer_either(self):
        """The tail of the real message: generation stopped inside a marker, so
        five characters of an opener are all that survives the strip. Without
        this the runaway is stored as `**[#7` and counted as a reply."""
        assert _is_answerless("**[#76 | 07:45:29 | Johnny]**\n\n**[#7") is True


class TestStrippingKeepsTheAnswer:
    """The guard must not eat the reply it is guarding."""

    def test_the_markers_go_and_the_answer_stays(self):
        content = ("[#71 | 07:28:15 | Johnny] **[#71 | 07:29:31 | Johnny]**\n\n"
                   "My verdict: the revert is correct.")
        assert _strip_history_markers(content) == "My verdict: the revert is correct."

    def test_a_marker_being_talked_about_is_left_alone(self):
        """This project discusses its own format constantly, and a marker in the
        middle of a sentence is the agent quoting it, not wearing it. Stripping
        anywhere but the front would silently edit that sentence."""
        content = "The runtime writes [#12 | 00:00:00 | Ark] on every history line."
        assert _strip_history_markers(content) == content

    def test_an_answer_with_no_marker_is_returned_unchanged(self):
        assert _strip_history_markers("Plain answer.") == "Plain answer."
