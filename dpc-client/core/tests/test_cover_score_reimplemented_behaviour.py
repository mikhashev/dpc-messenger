"""cover_score as DPC's own code (Mike 2026-11-26: reimplemented, not copied): the behaviour that differs from the
earlier module on purpose.

- Numbered section labels (`Chorus 2`, `% Bridge 1`, `Instrumental 1`) are tagged by their kind, not as a Verse
  (backlog COVER-SCORE-SPANS-TURNS-NUMBERED-SECTIONS-INTO-VERSE-AND-BREAKS-THE-YUE2-COVER-STRUCTURE, audit P2-16).
- A chord in the silenced Ins voice becomes one rest of the chord's length.
- A blank style gets the score's tempo with no leading comma.
- A closing `|]` bar line, a cue line with no time, a `K:Cmaj` key, a marker named `Start`, a score with no tempo.
"""

import pytest

from dpc_client_core.music import cover_score as cs

HEAD = ["X:1", "T:", "M:4/4", "L:1/16", "Q:1/4=120",
        'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"', 'V: Ins clef=treble name="Ins Melody" snm="Inst."',
        "K:C"]


def _score(*sections):
    """A two-voice score: each section is (marker, vocal line, ins line)."""
    body = []
    for marker, vocal, ins in sections:
        body += [marker, "V: Vocal", vocal, "V: Ins", ins]
    return "\n".join(HEAD + body + [""])


# A bar is 2 s: intro 0-2 s, verse 2-6 s, chorus 6-10 s, bridge 10-14 s, instrumental 14-16 s.
NUMBERED = _score(("% Intro 1", "Z|", "c4d4e4f4|"),
                  ("% Verse 1", "C4D4E4F4|C4D4E4F4|", "Z2|"),
                  ("% Chorus 2", "c4B4A4G4|c4B4A4G4|", "Z2|"),
                  ("% Bridge 1", "E4E4E4E4|E4E4E4E4|", "Z2|"),
                  ("% Instrumental 1", "Z|", "c4c4c4c4|"))


# -- numbered section labels --------------------------------------------------------------------------


def test_numbered_section_labels_are_tagged_by_their_kind_not_as_a_verse():
    labels = [p["label"] for p in cs.score_parts(NUMBERED)["parts"]]
    assert labels == ["Intro 1", "Verse 1", "Chorus 2", "Bridge 1", "Instrumental 1"]
    lines = ["one", "two", "three"]
    tagged = cs.pre_tag(lines, [2.1, 6.1, 10.1], NUMBERED)
    assert tagged == ["[Verse]", "one", "", "[Chorus]", "two", "", "[Bridge]", "three"]


def test_match_sections_keeps_the_structure_of_a_score_with_numbered_sections():
    lines = ["[Verse]", "one", "", "[Chorus]", "two", "", "[Bridge]", "three"]
    rows = [{"text": t, "start": s} for t, s in zip(["one", "two", "three"], [2.1, 6.1, 10.1])]
    m = cs.match_sections(NUMBERED, lines, rows)
    assert [b["status"] for b in m["blocks"]] == ["kept", "kept", "kept"]
    assert m["lyrics"] == "[Intro]\n\n[Verse]\none\n\n[Chorus]\ntwo\n\n[Bridge]\nthree\n\n[Instrumental]"
    assert [f["tag"] for f in m["filled"]] == ["Intro", "Instrumental"]
    assert cs.match_kind("Chorus 2") == "chorus" and cs.match_kind("Instrumental 1") == "instrumental"


# -- the Vocal voice alone ----------------------------------------------------------------------------


def test_a_chord_in_the_silenced_voice_becomes_one_rest_of_its_length():
    abc = "\n".join(HEAD + ["% verse", "V: Vocal", "C4D4E4F4|", "V: Ins", '"C"[CEG]4[C2E2]2[CE]/2 c3/2-d4|'])
    ins = cs.keep_vocal_voice(abc).split("\n")[-1]
    assert ins == "z4z4z/2 z3/2z4|"


def test_a_closing_bar_line_is_silenced_and_counted_like_any_other():
    abc = "\n".join(HEAD + ["% verse", "V: Vocal", "C4D4E4F4|C4D4E4F4|]", "V: Ins", "c4d4e4f4|c16|]"])
    assert cs.keep_vocal_voice(abc).split("\n")[-1] == "z4z4z4z4|z16|]"
    assert [(p["start_s"], p["end_s"]) for p in cs.score_parts(abc)["parts"]] == [(0.0, 4.0)]


def test_decorations_and_inline_fields_in_the_silenced_voice_stay():
    abc = "\n".join(HEAD + ["% verse", "V: Vocal", "C16|", "V: Ins", "!fermata!c16|[K:Am]a16|"])
    assert cs.keep_vocal_voice(abc).split("\n")[-1] == "!fermata!z16|[K:Am]z16|"


# -- the style ----------------------------------------------------------------------------------------


def test_a_blank_style_gets_the_tempo_with_no_leading_comma_with_or_without_a_key():
    assert cs.agree_style("", "X:1\nQ:1/4=120\nK:C") == ("120 BPM", ["added the score's tempo 120 BPM"])
    assert cs.agree_style("  ", "X:1\nQ:1/4=120\nK:C")[0] == "120 BPM"
    assert cs.agree_style("", "X:1\nQ:1/4=120\nM:4/4") == ("120 BPM", ["added the score's tempo 120 BPM"])


def test_a_score_key_written_with_its_mode_still_agrees_with_the_same_key_in_the_style():
    assert cs.agree_style("pop, C major", "X:1\nK:Cmaj") == ("pop, C major", [])
    assert cs.agree_style("pop, A minor", "X:1\nK:Amin") == ("pop, A minor", [])
    style, changes = cs.agree_style("pop, A minor", "X:1\nK:Cmaj")
    assert style == "pop" and changes == ["key A minor dropped: the score is in Cmaj"]
    assert cs.agree_style("pop, A minor", "X:1\nK:none") == ("pop, A minor", [])   # no key to disagree with


def test_a_style_that_drops_nothing_is_left_exactly_as_it_was():
    assert cs.agree_style("rock,, pop", "X:1\nK:C") == ("rock,, pop", [])


# -- pre-tagging and sections -------------------------------------------------------------------------


def test_a_cue_line_with_no_time_rides_along_with_its_block():
    abc = _score(("% intro", "Z|", "c4d4e4f4|"), ("% verse", "C16|C16|", "Z2|"), ("% chorus", "c16|c16|", "Z2|"))
    lines = ["(ooh)", "night drive", "(yeah)", "neon", "we never stop"]
    times = [None, 2.1, None, 4.0, 6.1]
    assert cs.pre_tag(lines, times, abc) == ["[Verse]", "(ooh)", "night drive", "(yeah)", "neon", "",
                                             "[Chorus]", "we never stop"]
    assert cs.pre_tag(lines, [None, 2.1, None, None, 6.1], abc) is None          # a sung line with no time


def test_a_section_literally_named_start_keeps_its_label():
    abc = "\n".join(HEAD + ["% Start", "V: Vocal", "C16|"])
    assert [(p["name"], p["label"]) for p in cs.score_parts(abc)["parts"]] == [("Start", "Start")]
    music_first = "\n".join(HEAD + ["V: Vocal", "C16|", "% verse", "C16|"])
    assert [(p["name"], p["label"]) for p in cs.score_parts(music_first)["parts"]] == [("Start", ""),
                                                                                       ("verse", "verse")]


def test_a_score_with_sections_but_no_tempo_says_so():
    with pytest.raises(ValueError, match="no tempo"):
        cs.match_sections("X:1\nM:4/4\nK:C\n% verse\nV: Vocal\nC16|", ["la"], [{"text": "la", "start": 0.0}])
    with pytest.raises(ValueError, match="the score has no named sections"):
        cs.match_sections("X:1\nQ:1/4=100\nK:C\nV: Vocal\nC4|", ["la"], [{"text": "la", "start": 0.0}])


def test_counts_of_has_the_five_statuses_and_any_other_it_meets():
    assert cs.counts_of([{"status": "kept"}, {"status": "kept"}, {"status": "odd"}]) == {
        "kept": 2, "renamed": 0, "merged": 0, "unsure": 0, "dropped": 0, "odd": 1}
