"""The two pure functions of the K-quantisation A/B, because they decide
whether its verdict means anything.

The probe compares `-ctk q8_0` against `-ctk q4_0` with V held at q4_0. Two
things can make such a comparison flattering rather than true: a needle matcher
that accepts a substring (512 found inside 3512), and corpora that are not
identical between the arms, which turns a cache difference into a text
difference. Both are cheap to pin and neither is visible in the output.
"""

import importlib.util
import random
from pathlib import Path

PROBE = Path(__file__).resolve().parents[3] / "eval" / "kv" / "ab_key_quant.py"


def _module():
    spec = importlib.util.spec_from_file_location("ab_key_quant", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _module()


class TestTheNeedleMatcher:
    def test_a_number_inside_a_longer_number_is_not_a_hit(self):
        assert not ab._hit("512", "the constant is 3512")
        assert not ab._hit("512", "5123")

    def test_the_number_on_its_own_is_a_hit(self):
        assert ab._hit("512", "512")
        assert ab._hit("512", "The calibration constant is 512.")

    def test_a_hex_checksum_needs_its_own_boundary(self):
        assert ab._hit("22c612095f5f", "checksum: 22c612095f5f")
        assert not ab._hit("22c612095f5f", "022c612095f5fa")

    def test_an_empty_reply_never_scores(self):
        assert not ab._hit("512", "")


class TestTheCorpus:
    def test_both_arms_get_byte_identical_text(self):
        """The arms run in separate processes; only the seed carries between
        them, so the corpus must be a function of the seed and nothing else."""
        a, na = ab._corpus(random.Random(42_000 + 32_000), 128_000)
        b, nb = ab._corpus(random.Random(42_000 + 32_000), 128_000)
        assert a == b
        assert na == nb

    def test_a_different_depth_is_a_different_corpus(self):
        a, _ = ab._corpus(random.Random(1), 128_000)
        b, _ = ab._corpus(random.Random(1), 32_000)
        assert a != b

    def test_the_needles_are_spread_and_not_all_at_the_end(self):
        """A defect that eats the oldest region and one that eats the middle
        look identical when every needle sits near the last token."""
        _, needles = ab._corpus(random.Random(7), 32_000)
        positions = [n["position"] for n in needles]
        assert positions == [0.25, 0.5, 0.75]

    def test_every_needle_is_stated_once_in_the_text(self):
        corpus, needles = ab._corpus(random.Random(7), 32_000)
        for n in needles:
            assert corpus.count(f"{n['fact']} is {n['answer']}") == 1

    def test_the_filler_carries_no_answer_by_accident(self):
        """The filler is random digits; a needle value appearing in it would
        be scored as a hit the model never earned."""
        corpus, needles = ab._corpus(random.Random(7), 32_000)
        for n in needles:
            stated = f"NOTE: {n['fact']} is {n['answer']}.\n"
            assert ab._hit(n["answer"], corpus.replace(stated, "")) is False

    def test_the_character_budget_is_honoured(self):
        """The depth itself is calibrated against the model's tokeniser at run
        time; what this function owes the caller is the character budget."""
        corpus, _ = ab._corpus(random.Random(3), 128_000)
        assert 128_000 <= len(corpus) <= 128_000 * 1.02
