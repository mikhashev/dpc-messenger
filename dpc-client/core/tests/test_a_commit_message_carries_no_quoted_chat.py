"""The hook that would have stopped ten commits from carrying somebody's chat.

Mike found his own words in the body of a public commit on 2026-08-28 — in
Russian, profanity included. Ten of the 432 commits on `dev` were like it, the
oldest three weeks old. The rule was written down the same evening; this is the
part that does not depend on anyone remembering it.

The tests are here rather than beside the hook so CI runs them: the hook lives
in `tools/git-hooks/`, which no package imports.
"""
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[3] / "tools" / "git-hooks"
sys.path.insert(0, str(HOOKS))

import commit_msg_check as hook  # noqa: E402


GOOD = """fix(group): the history doors ask the roster

Mike's call, 2026-08-28: take the gate to every door at once. The three
GROUP_HISTORY_* handlers refused nothing before this.
"""


class TestWhatIsRefused:
    def test_a_quoted_russian_line_is_refused(self):
        refusals, _ = hook.check(
            "feat(ui): one control instead of two\n\n"
            "Mike, 2026-08-28: «нахуя там два».\n"
        )
        assert len(refusals) == 1
        assert "line 3" in refusals[0]

    def test_every_offending_line_is_named_not_just_the_first(self):
        refusals, _ = hook.check(
            "docs: something\n\nтекст one\nplain english\nтекст two\n"
        )
        assert len(refusals) == 2

    def test_an_ordinary_english_message_passes(self):
        refusals, warnings = hook.check(GOOD)
        assert refusals == []
        assert warnings == []

    def test_typography_is_not_language(self):
        """Em dashes, guillemets and § are how this repo writes English."""
        refusals, _ = hook.check(
            "docs(adr): keep the evidence — drop what rots\n\n"
            "The rule sits in §3, and the phrase «measured before ordered» is ours.\n"
        )
        assert refusals == []

    def test_git_s_own_commentary_is_not_the_message(self):
        """Everything below the scissors is stripped before the message is stored."""
        refusals, _ = hook.check(
            GOOD + "\n# Please enter the commit message\n# на русском\n"
        )
        assert refusals == []


class TestWhatOnlyWarns:
    def test_a_name_beside_a_quotation_warns_but_does_not_refuse(self):
        refusals, warnings = hook.check(
            "fix(x): y\n\nMike, reading the gate: \"why is this comment a poem?\"\n"
        )
        assert refusals == []
        assert len(warnings) == 1

    def test_attribution_without_the_sentence_is_silent(self):
        refusals, warnings = hook.check(
            "fix(x): y\n\nMike's call, 2026-08-28: one control here is enough.\n"
        )
        assert (refusals, warnings) == ([], [])


class TestTheHookItself:
    def test_it_exits_nonzero_on_a_refused_message(self, tmp_path):
        path = tmp_path / "COMMIT_EDITMSG"
        path.write_text("docs: x\n\nпривет\n", encoding="utf-8")
        assert hook.main(["commit-msg", str(path)]) == 1

    def test_it_exits_zero_on_an_acceptable_one(self, tmp_path):
        path = tmp_path / "COMMIT_EDITMSG"
        path.write_text(GOOD, encoding="utf-8")
        assert hook.main(["commit-msg", str(path)]) == 0

    def test_a_missing_argument_is_an_error_not_a_pass(self):
        assert hook.main(["commit-msg"]) == 1
