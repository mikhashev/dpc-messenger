"""The LRC dialect of the music player (SPEC §4; the prototype's parseLrc / toLrc),
read by `music/lyrics.py` for sidecars, tags, LRCLIB and manual edits."""

import pytest

from dpc_client_core.music import schemas
from dpc_client_core.music.lyrics import (
    format_lrc, lines_from_text, lrc_stamp, make_lyrics, parse_artist_title, parse_lrc, pick_lyrics_tag,
    plain_lines,
)


def test_a_plain_stamped_text_gives_sorted_timed_lines():
    parsed = parse_lrc("[00:12.50] Second\n[00:01.00] First\n[01:02.345]Third")
    assert parsed.lines == [
        {"t_start": 1.0, "text": "First"},
        {"t_start": 12.5, "text": "Second"},
        {"t_start": 62.345, "text": "Third"},
    ]
    assert parsed.untimed == [] and parsed.language is None


def test_every_stamp_form_is_read():
    parsed = parse_lrc("[00:05] a\n[0:06.5] b\n[00:07:25] c\n[100:00.00] d")
    assert [l["t_start"] for l in parsed.lines] == [5.0, 6.5, 7.25, 6000.0]


def test_several_leading_stamps_repeat_the_line():
    parsed = parse_lrc("[00:10.00][00:40.00] Chorus\n[00:20.00] Verse")
    assert parsed.lines == [
        {"t_start": 10.0, "text": "Chorus"},
        {"t_start": 20.0, "text": "Verse"},
        {"t_start": 40.0, "text": "Chorus"},
    ]


def test_a_trailing_mark_is_the_end_and_inner_word_times_are_dropped():
    parsed = parse_lrc("[00:01.00]<00:01.00>One <00:01.50>two <00:02.00>three <00:03.20>")
    assert parsed.lines == [{"t_start": 1.0, "t_end": 3.2, "text": "One two three"}]


def test_an_end_not_after_the_start_is_ignored():
    assert parse_lrc("[00:05.00] x <00:04.00>").lines == [{"t_start": 5.0, "text": "x"}]


def test_an_empty_stamped_line_ends_the_previous_line():
    parsed = parse_lrc("[00:01.00] Sing\n[00:04.00]\n[00:09.00] Again")
    assert parsed.lines == [
        {"t_start": 1.0, "t_end": 4.0, "text": "Sing"},
        {"t_start": 9.0, "text": "Again"},
    ]


def test_metadata_is_skipped_language_kept_and_untimed_lines_counted():
    parsed = parse_lrc("﻿[ar:Someone]\n[ti:Song]\n[la:ru]\n[length: 03:20]\nno stamp here\n[00:01.00] Привет")
    assert parsed.language == "ru"
    assert parsed.untimed == ["no stamp here"]
    assert parsed.lines == [{"t_start": 1.0, "text": "Привет"}]


def test_a_positive_offset_shows_the_lines_earlier_and_never_below_zero():
    parsed = parse_lrc("[offset:+500]\n[00:00.20] a\n[00:10.00] b <00:12.00>")
    assert parsed.offset_ms == 500
    assert parsed.lines == [{"t_start": 0.0, "text": "a"}, {"t_start": 9.5, "t_end": 11.5, "text": "b"}]
    later = parse_lrc("[offset:-250]\n[00:10.00] b")
    assert later.lines == [{"t_start": 10.25, "text": "b"}]


def test_windows_line_ends_and_blank_lines_are_fine():
    assert parse_lrc("[00:01.00] a\r\n\r\n[00:02.00] b\r\n").lines == [
        {"t_start": 1.0, "text": "a"}, {"t_start": 2.0, "text": "b"}]


@pytest.mark.parametrize("t,text", [(0, "00:00.00"), (5.004, "00:05.00"), (59.999, "01:00.00"),
                                    (61.25, "01:01.25"), (None, "00:00.00"), (-3, "00:00.00")])
def test_a_stamp_is_minutes_seconds_hundredths(t, text):
    assert lrc_stamp(t) == text


def test_format_writes_language_ends_and_unsynced_lines_without_a_stamp():
    lyrics = {"language": "en", "lines": [
        {"t_start": 1.0, "text": "a"}, {"t_start": 2.5, "t_end": 4.0, "text": "b"}]}
    text = format_lrc(lyrics)
    assert text == "[la:en]\n[00:01.00] a\n[00:02.50] b <00:04.00>"
    again = parse_lrc(text)
    assert again.lines == lyrics["lines"] and again.language == "en"
    assert format_lrc({"lines": plain_lines("x\n\n y ")}) == "x\ny"
    assert format_lrc(None) == ""


def test_a_text_without_stamps_becomes_unsynced_lines():
    lines, synced, _ = lines_from_text("one\n\ntwo")
    assert synced is False and lines == [{"t_start": None, "text": "one"}, {"t_start": None, "text": "two"}]
    lines, synced, _ = lines_from_text("[00:01.00] one")
    assert synced is True and lines == [{"t_start": 1.0, "text": "one"}]


def test_the_lyrics_record_is_checked_against_its_schema():
    record = make_lyrics("sha256:x", "lrclib", plain_lines("a"), synced=False,
                         lrclib={"id": 1, "artist_name": "A", "track_name": "T", "album_name": None,
                                 "duration": 200, "synced": False})
    assert record["synced"] is False and record["schema"] == "mp.lyrics/1"
    with pytest.raises(schemas.SchemaError):
        make_lyrics("h", "lrc", plain_lines("a"))  # untimed lines in synced lyrics
    with pytest.raises(schemas.SchemaError):
        make_lyrics("h", "somewhere", [])
    with pytest.raises(schemas.SchemaError):
        schemas.validate_lyrics({**record, "extra": 1})


def test_the_lyrics_tag_with_stamps_is_preferred():
    assert pick_lyrics_tag({"title": "x"}) is None
    assert pick_lyrics_tag({"UNSYNCEDLYRICS": "plain", "lyrics-eng": "[00:01.00] timed"}) == ("lyrics-eng", "[00:01.00] timed")
    assert pick_lyrics_tag({"lyrics-eng": "b", "LYRICS": "a"}) == ("LYRICS", "a")
    assert pick_lyrics_tag({"LYRICS": "  "}) is None


@pytest.mark.parametrize("name,expected", [
    ("Queen - Bohemian Rhapsody.mp3", ("Queen", "Bohemian Rhapsody")),
    ("Кино – Группа крови.flac", ("Кино", "Группа крови")),
    ("A—B - C.ogg", ("A—B", "C")),
    ("just a title.wav", ("", "just a title")),
    ("", ("", "")),
])
def test_artist_and_title_come_from_the_file_name(name, expected):
    assert parse_artist_title(name) == expected
