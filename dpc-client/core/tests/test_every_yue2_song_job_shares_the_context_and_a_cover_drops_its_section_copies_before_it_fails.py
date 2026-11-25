"""Zcode #207 on the yue2 song door (follow-up 21).

- I. A new song on yue2.cpp (`generate_from_lyrics`) had no context ceiling: tens of KB of lyrics passed every
  door and failed inside the engine. The door now counts its text, the frames its duration cap lets run and the
  end token against the 24576 positions, and the run checks again before its `/synth` bodies are written.
- J. The 1 MiB text bound was checked on the raw parts; it is now checked on the text as sent (joiners and the
  theme's words count).
- K. Match sections fills an unsung section with a whole earlier block. The door now reserves bytes for the
  section tags; at run time, when only the copied bodies make a cover not fit, they are dropped (the tags stay)
  and the drop is recorded, instead of failing after the transcription.
"""

import math

import pytest

from dpc_client_core.music import cover_score as cs
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import similar
from dpc_client_core.music.runner import _JobFailure
from tests.test_a_song_from_a_library_track_is_a_cover_a_same_style_song_or_a_song_from_its_lyrics import (
    LOCAL, TRACK)


def _lyrics_job(lyrics, **params):
    return js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": lyrics, "engine": "yue2", **params},
                                    track_id=TRACK, source=LOCAL)


def _bytes_for(tokens_left):
    """A lyric whose sent text takes about `tokens_left` prompt tokens."""
    return "a" * int((tokens_left - js.YUE2_PROMPT_FIXED_TOKENS) / js.YUE2_TEXT_TOKENS_PER_BYTE)


# -- I: the door and the run of a new song on yue2 ------------------------------------------------------


def test_a_new_yue2_song_whose_text_and_cap_overflow_the_context_is_refused_at_the_door():
    fits = js.YUE2_CONTEXT - 1 - js.yue2_semantic_budget({"duration": 360})
    too_long = _bytes_for(fits) + "a" * 200
    with pytest.raises(js.SongParamsError) as err:
        _lyrics_job(too_long, duration_s=360)
    assert err.value.code == "context_exceeded" and err.value.field == "lyrics"
    assert "360 s cap" in err.value.message and "acestep" in err.value.message
    # a shorter cap leaves the room, and so does a shorter text
    assert _lyrics_job(too_long, duration_s=300)["duration_s"] == 300
    assert _lyrics_job(_bytes_for(fits) [:-400], duration_s=360)["duration_s"] == 360
    # acestep has no such shared context
    p = js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": too_long, "engine": "acestep"},
                                 track_id=TRACK, source=LOCAL)
    assert p["engine"] == "acestep"


def test_the_run_checks_the_new_songs_context_again_before_its_synth_bodies():
    params = _lyrics_job("[verse]\nla la la", duration_s=180)
    numbers = similar.check_song_context(params)
    assert numbers["left"] >= 0 and numbers["budget_tokens"] == 180 * js.YUE2_FRAME_RATE
    grown = dict(params, lyrics=[_bytes_for(js.YUE2_CONTEXT - 4000)], duration_s=180)   # as if edited after the door
    with pytest.raises(_JobFailure, match="context_exceeded"):
        similar.check_song_context(grown)


def test_the_run_check_sits_before_the_new_songs_bodies_are_written():
    import inspect
    src = inspect.getsource(similar._prepare)
    branch = src[src.index("elif key == js.ENGINE_YUE2 and job_type != js.COVER:"):]
    assert branch.index("check_song_context(params)") < branch.index("build_yue2_song_body")


# -- J: the 1 MiB bound counts the text as sent ---------------------------------------------------------


def test_the_text_bound_counts_the_joiners_of_the_text_as_sent():
    lines = ["ab"] * (380 * 1024)                     # 760 KiB of lines, plus a newline between each: over 1 MiB
    raw = sum(len(x) for x in lines) + 1
    assert raw < js.MAX_SONG_TEXT_BYTES < len(js.song_text_sent("x", "\n".join(lines)).encode("utf-8"))
    with pytest.raises(js.ParamsError, match="as sent"):
        js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": lines, "engine": "acestep"},
                                 track_id=TRACK, source=LOCAL)


# -- K(a): the door keeps room for the section tags ------------------------------------------------------


def test_the_door_reserves_section_tags_by_the_sources_length():
    assert js.yue2_cover_tag_reserve_bytes(120.0) == math.ceil(120.0 / js.YUE2_SECTION_MIN_S) * js.YUE2_SECTION_TAG_BYTES
    assert js.yue2_cover_tag_reserve_bytes(0.0) == 0
    sent = len("x\nla la la") + js.YUE2_COVER_TEMPO_RESERVE_BYTES
    without = js.yue2_cover_door_max_s(sent)
    cover = lambda s: js.normalize_params_song("cover", {"caption": "x", "lyrics": ["la la la"]}, track_id=TRACK,  # noqa: E731
                                               source=dict(LOCAL, duration_s=s))
    with pytest.raises(js.SongParamsError) as err:
        cover(without)                                 # the old ceiling no longer leaves room for the tags
    assert err.value.code == "source_too_long"
    top = js.yue2_cover_door_max_s(sent + js.yue2_cover_tag_reserve_bytes(without))
    assert cover(top)["duration_s"] == top


# -- K(b): the copies go before the cover fails ----------------------------------------------------------

TWICE_ABC = "\n".join([
    "X:1", "T:", "M:4/4", "L:1/16", "Q:1/4=120",
    'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"', 'V: Ins clef=treble name="Ins Melody" snm="Inst."',
    "K:C", "% intro", "V: Vocal", "Z|", "V: Ins", "c4d4e4f4|",
    "% verse", "V: Vocal", '"C"C4D4E4F4|"G"G4A4B4c4|', "V: Ins", "Z2|",
    "% chorus", "V: Vocal", '"F"c4B4A4G4|"C"F4E4D4C4|', "V: Ins", "e4d4c4B4|Z|",
    "% verse", "V: Vocal", '"C"C4D4E4F4|"G"G4A4B4c4|', "V: Ins", "Z2|",
    "% chorus", "V: Vocal", '"F"c4B4A4G4|"C"F4E4D4C4|', "V: Ins", "e4d4c4B4|Z|", ""])
LINES = ["[Verse]", "night drive, city lights", "neon in the rain", "", "[Chorus]", "we never stop",
         "we never stop, no we never stop"]
TIMES = [None, 2.1, 4.0, None, None, 6.1, 8.0]
PARAMS = {"caption": "synthwave", "lyrics": LINES, "lyrics_times": TIMES, "lyrics_theme": None}


def test_match_sections_copies_a_whole_block_into_an_unsung_section_and_can_give_the_tag_alone():
    rows = similar._timed_rows(LINES, TIMES)
    full = cs.match_sections(TWICE_ABC, LINES, rows)
    bare = cs.match_sections(TWICE_ABC, LINES, rows, copy_bodies=False)
    copied = [f for f in full["filled"] if f["copied_from"] is not None]
    assert len(copied) == 2                                              # the second verse and the second chorus
    assert full["lyrics"].count("we never stop, no we never stop") == 2
    assert bare["lyrics"].count("we never stop, no we never stop") == 1
    assert bare["filled"] == full["filled"]                              # the same sections, tagged
    assert bare["lyrics"].count("[Chorus]") == full["lyrics"].count("[Chorus]") == 2


def _context_by_size(limit):
    def numbers(params, source_s, style, lyrics, score):
        size = len((lyrics or "").encode("utf-8"))
        return {"left": limit - size}
    return numbers


def test_when_only_the_copies_overflow_they_are_dropped_and_the_drop_is_recorded(monkeypatch):
    lyrics, matched = similar._lyrics_against_score(PARAMS, TWICE_ABC)
    bare, _ = similar._lyrics_against_score(PARAMS, TWICE_ABC, copy_bodies=False)
    full_size, bare_size = len(lyrics.encode("utf-8")), len(bare.encode("utf-8"))
    assert matched["sections_copied"] == 2 and bare_size < full_size
    monkeypatch.setattr(similar, "cover_context", _context_by_size(bare_size + 1))   # fits only without the copies
    out, said = similar._fit_without_copies(PARAMS, 20.0, "synthwave", TWICE_ABC, lyrics, matched)
    assert out == bare and said["sections_copied"] == 0
    assert said["copies_dropped"] == {"sections": 2, "bytes": full_size - bare_size,
                                      "reason": said["copies_dropped"]["reason"]}
    assert "tags stay" in said["copies_dropped"]["reason"]


def test_the_copies_stay_when_the_cover_fits_or_does_not_fit_even_without_them(monkeypatch):
    lyrics, matched = similar._lyrics_against_score(PARAMS, TWICE_ABC)
    monkeypatch.setattr(similar, "cover_context", _context_by_size(10 ** 6))         # fits as it is
    assert similar._fit_without_copies(PARAMS, 20.0, "s", TWICE_ABC, lyrics, matched) == (lyrics, matched)
    monkeypatch.setattr(similar, "cover_context", _context_by_size(10))              # nothing helps: the check fails
    out, said = similar._fit_without_copies(PARAMS, 20.0, "s", TWICE_ABC, lyrics, matched)
    assert (out, said) == (lyrics, matched) and "copies_dropped" not in said


def test_the_cover_path_tries_without_the_copies_before_its_context_check():
    import inspect
    src = inspect.getsource(similar._cover_on_yue2)
    assert src.index("_fit_without_copies(") < src.index("check_cover_context(")
