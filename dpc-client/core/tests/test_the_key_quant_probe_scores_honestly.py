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


class TestTheEvidenceRecomputesItself:
    """The write-up says nine comparable cells, identical in both arms, and the
    comparator on the base files alone says seven — because the three
    computational cells were re-run at 4096 tokens into `{arm}-compute.json`
    and nothing read that file. Two artefacts of one measurement, and no tool
    joining them; the 22% was an empty reply, not a different answer.
    """

    def _arm(self, root, name, compute_got, max_tokens=None, only_needle=None):
        rows = {
            str(depth): [
                {"position": 0.25, "expected": "295", "got": "295", "hit": True},
                {"position": 0.50, "expected": "abc", "got": "abc", "hit": True},
                {"position": 0.75, "expected": "5139", "got": compute_got,
                 "hit": compute_got == "5139"},
            ]
            for depth in ab.DEPTHS
        }
        doc = {"ctk": "x", "ctv": "y", "depths": rows, "corpus_tokens": {}}
        if max_tokens is not None:
            doc["max_tokens"] = max_tokens
            doc["only_needle"] = only_needle
            for depth in rows:
                rows[depth] = [r for r in rows[depth] if r["position"] == 0.75]
        (root / f"{name}.json").write_text(__import__("json").dumps(doc), encoding="utf-8")

    def test_the_compute_rerun_is_read_and_named(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab, "RESULTS", tmp_path)
        monkeypatch.setattr(ab, "LEGACY_RESULTS", tmp_path)
        name = ab.ARMS[0]
        self._arm(tmp_path, name, compute_got="")
        self._arm(tmp_path, f"{name}-compute", compute_got="5139",
                  max_tokens=4096, only_needle=2)

        arm, source, replaced = ab._arm_with_the_rerun(name)

        assert replaced == 3, "the computational cells were not substituted"
        assert "compute.json" in source
        assert all(
            row["got"] == "5139" and row["from"].startswith("re-run")
            for rows in arm["depths"].values()
            for row in rows if row["position"] == 0.75
        )

    def test_an_arm_with_no_rerun_beside_it_is_still_read(self, tmp_path, monkeypatch):
        """Non-regression: the substitution is an addition, not a requirement."""
        monkeypatch.setattr(ab, "RESULTS", tmp_path)
        monkeypatch.setattr(ab, "LEGACY_RESULTS", tmp_path)
        name = ab.ARMS[0]
        self._arm(tmp_path, name, compute_got="5139")

        arm, source, replaced = ab._arm_with_the_rerun(name)

        assert replaced == 0
        assert "no compute re-run" in source
        assert len(arm["depths"][str(ab.DEPTHS[0])]) == 3

    def test_the_committed_artefacts_are_found_after_the_root_moved(self, tmp_path, monkeypatch):
        """`--compare` exited on «run that arm first» while the files sat in the
        tree: the results root moved out and the artefacts stayed behind."""
        legacy = tmp_path / "in-the-tree"
        legacy.mkdir()
        monkeypatch.setattr(ab, "RESULTS", tmp_path / "moved-away")
        monkeypatch.setattr(ab, "LEGACY_RESULTS", legacy)
        self._arm(legacy, ab.ARMS[0], compute_got="5139")

        assert ab._arm_file(f"{ab.ARMS[0]}.json").parent == legacy
