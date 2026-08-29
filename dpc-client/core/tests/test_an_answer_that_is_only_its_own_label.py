"""A reply carrying nothing but the history marker is not a reply.

Measured 2026-08-29: an agent answered a question in the project room with
`[#74 | 06:42:57 | Johnny]` and nothing else — 25 characters, one round, no
tool calls, 1454 completion tokens spent. The loop's empty-content retry
tests `content.strip()`, so those 25 characters read as an answer: the run
completed, the message was signed and saved into the room's history, and
nothing anywhere said the reply was empty.

The marker itself comes from `context.history_prefix`, which puts
`[#idx | HH:MM:SS | sender] ` on every history line the model reads.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.loop import _is_answerless


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
