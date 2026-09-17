"""(F) THE-AGENT-REASONS-ABOUT-A-SCRATCHPAD-IT-IS-SHOWN-THE-TWO-ENDS-OF.

A marker that says only "a cut happened" leaves the reader treating the part in
hand as the whole. These tests pin the two things that make the cut honest: the
marker names how many characters are missing, and the scratchpad section says
where the missing part can be read.
"""

import re

import pytest

from dpc_client_core.dpc_agent.context import _build_memory_sections
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.utils import clip_text


def _filler(n: int) -> str:
    """n characters holding no digit and no newline.

    No digit, so every digit in the result belongs to the marker. No newline, so
    the length survives the round trip through a text-mode write on Windows.
    """
    unit = "abcdefghij"
    return (unit * (n // len(unit) + 1))[:n]


def _marker_between_the_halves(result: str, half: int) -> str:
    return result[half:len(result) - half]


class TestTheMarkerCarriesTheSizeOfTheLoss:

    def test_the_marker_names_how_many_characters_did_not_arrive(self):
        max_chars = 90000
        text = _filler(177624)

        result = clip_text(text, max_chars)

        half = max_chars // 2
        assert result.startswith(text[:half])
        assert result.endswith(text[-half:])
        marker = _marker_between_the_halves(result, half)
        assert str(len(text) - max_chars) in re.findall(r"\d+", marker)

    def test_the_number_is_counted_from_what_is_kept_not_from_the_limit(self):
        # An odd limit keeps one character fewer than it names, which is where a
        # count taken from max_chars starts lying.
        max_chars = 4001
        text = _filler(10000)

        result = clip_text(text, max_chars)

        half = max_chars // 2
        numbers = re.findall(r"\d+", _marker_between_the_halves(result, half))
        assert str(len(text) - 2 * half) in numbers
        assert str(len(text) - max_chars) not in numbers


class TestATextThatFitsIsHandedOverUntouched:

    @pytest.mark.parametrize("length, max_chars", [(4001, 4001), (300, 90000)])
    def test_a_text_within_the_limit_comes_back_byte_for_byte(self, length, max_chars):
        text = _filler(length)

        assert clip_text(text, max_chars) == text

    def test_a_limit_below_the_two_halves_drops_nothing_and_announces_nothing(self):
        # The halves floor at 200 characters each, so for this text they overlap
        # and cover it whole: there is no middle to lose and none to announce.
        text = _filler(300)

        assert clip_text(text, 100) == text


class TestTheScratchpadSectionSaysWhereTheRestIs:

    def _scratchpad_section(self, tmp_path, content):
        memory = Memory(tmp_path / "agent_001")
        memory.ensure_files()
        memory.save_scratchpad(content)
        sections = [s for s in _build_memory_sections(memory)
                    if s.startswith("## Scratchpad")]
        assert len(sections) == 1
        return sections[0]

    def test_a_clipped_scratchpad_tells_the_agent_to_read_the_file(self, tmp_path):
        content = _filler(177624)

        section = self._scratchpad_section(tmp_path, content)

        assert "scratchpad.md" in section
        assert "read_file" in section
        assert str(len(content)) in section

    def test_a_scratchpad_that_arrived_whole_carries_no_such_line(self, tmp_path):
        section = self._scratchpad_section(tmp_path, _filler(5000))

        assert "read_file" not in section
        assert "incomplete" not in section.lower()
