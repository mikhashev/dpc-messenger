"""music/lyrics_prep.py: syllables, the meter check, the audit of the author's U+0301 marks and
the stress resolver, against a tiny `forms.sqlite` built here with the real file's layout
(`forms(k, f)`, `cap(k, f)`, `about(k, v)`; key lower-case with e for e-diaeresis, several
forms of a homograph joined by `|`)."""

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from dpc_client_core.music import lyrics_prep as lp

A = "́"

FORMS = {
    "замок": f"за{A}мок|замо{A}к",
    "стекло": f"стекло{A}",
    "любовь": f"любо{A}вь",
    "ладонь": f"ладо{A}нь",
    "елка": "ёлка",
    "кошка": f"ко{A}шка",
    "а-конто": f"а-ко{A}нто",
    "огонь": f"ого{A}нь",
    "тысячи": f"ты{A}сячи",
    "светит": f"све{A}тит",
    "дома": f"до{A}ма|дома{A}",
    "слово": "слово",  # a form with no stress at all: unusable
}
CAP = {"аарон": f"ааро{A}н"}


def build_dictionary(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE forms (k TEXT PRIMARY KEY, f TEXT) WITHOUT ROWID")
    db.execute("CREATE TABLE cap (k TEXT PRIMARY KEY, f TEXT) WITHOUT ROWID")
    db.execute("CREATE TABLE about (k TEXT PRIMARY KEY, v TEXT)")
    db.executemany("INSERT INTO forms VALUES (?, ?)", FORMS.items())
    db.executemany("INSERT INTO cap VALUES (?, ?)", CAP.items())
    db.executemany("INSERT INTO about VALUES (?, ?)", [
        ("lang", "ru"), ("source", "test fixture"), ("licence", "test"), ("built", "2026-01-01"), ("keys", "12")])
    db.commit()
    db.close()
    return path


@pytest.fixture
def dictionary(tmp_path):
    path = build_dictionary(tmp_path / "forms.sqlite")
    d, state, message = lp.open_dictionary(path)
    assert state == "ok" and message is None
    yield d
    lp.close_dictionaries()


def words(report, line):
    entry = next(e for e in report["lines"] if e["n"] == line)
    return {w["text"]: w for w in entry["words"]}


def kinds(report):
    return sorted(i["kind"] for i in report["issues"])


# -- syllables --------------------------------------------------------------------------


def test_a_russian_word_has_one_syllable_per_vowel_letter_and_a_vowel_less_word_has_none():
    assert lp.russian_syllables("любовь") == 2
    assert lp.russian_syllables("ЁЛКА") == 2
    assert lp.russian_syllables(f"ко{A}шка") == 2  # the mark is not a letter
    assert lp.russian_syllables("в") == 0 and lp.russian_syllables("с") == 0
    assert lp.russian_syllables("взгляд") == 1


@pytest.mark.parametrize("word,n", [
    ("love", 1), ("hello", 2), ("table", 2), ("wanted", 2), ("walked", 1), ("boxes", 2), ("makes", 1),
    ("beautiful", 3), ("a", 1), ("rhythm", 1), ("1", 0),
])
def test_an_english_word_is_counted_by_the_heuristic(word, n):
    assert lp.english_syllables(word) == n


def test_each_line_gets_its_syllables_and_a_section_tag_a_number_and_a_blank_add_nothing(dictionary):
    text = "[verse]\nЯ тебя люблю\n\nHello world\n12 слов"
    r = lp.prepare_lyrics(text, "auto", dictionary=dictionary)
    by_n = {e["n"]: e for e in r["lines"]}
    assert by_n[1]["kind"] == "section" and 3 not in by_n
    assert by_n[2]["syllables"] == 1 + 2 + 2  # я, те-бя, лю-блю
    assert by_n[4]["syllables"] == 3  # hel-lo + world
    assert by_n[5]["syllables"] == 1 and by_n[5]["notes"]  # "12" adds none, and says so
    assert r["summary"]["lines"] == 3


def test_the_language_is_read_from_the_script_unless_the_person_names_one(dictionary):
    assert lp.prepare_lyrics("Привет, мир", dictionary=dictionary)["language"]["effective"] == "ru"
    assert lp.prepare_lyrics("Hello, world")["language"]["effective"] == "en"
    named = lp.prepare_lyrics("Hello, world", "de")["language"]
    assert named == {"requested": "de", "detected": "en", "effective": "de"}
    assert lp.prepare_lyrics("...")["language"]["detected"] is None


# -- the meter --------------------------------------------------------------------------


def test_a_line_far_from_its_sections_typical_length_is_flagged_long_or_short():
    text = ("[verse]\nона идёт домой\nона идёт домой\nона идёт домой\n"
            "она идёт домой и поёт про любовь и ночь\nда")
    r = lp.prepare_lyrics(text, "ru")
    sec = r["sections"][0]
    assert sec["checked"] and sec["typical"] == 6 and sec["tolerance"] == 2
    flags = {e["n"]: e["meter"]["flag"] for e in r["lines"] if e["kind"] == "lyric"}
    assert flags == {2: None, 3: None, 4: None, 5: "long", 6: "short"}
    assert r["summary"]["lines_flagged"] == 2


def test_the_tolerance_grows_with_the_line_length_and_can_be_set():
    # typical 12 -> tolerance max(2, ceil(3.0)) = 3: a line 3 syllables off is fine, 4 is flagged
    long12 = "она идёт домой по улице вечер"
    assert lp.prepare_lyrics(long12, "ru")["lines"][0]["syllables"] == 12
    near = "она идёт домой по ночи"  # 9 syllables, 3 short
    far = "она идёт домой по у"  # 8 syllables, 4 short
    text = "\n".join([long12] * 3 + [near, far])
    r = lp.prepare_lyrics(text, "ru")
    assert r["sections"][0]["typical"] == 12 and r["sections"][0]["tolerance"] == 3
    assert [e["meter"]["flag"] for e in r["lines"]] == [None, None, None, None, "short"]
    strict = lp.prepare_lyrics(text, "ru", tolerance=0)
    assert strict["summary"]["lines_flagged"] == 2


def test_a_section_with_fewer_than_three_sung_lines_is_not_checked():
    r = lp.prepare_lyrics("[chorus]\nа\nбольшая длинная строчка песни про всё\n[verse]\n[instrumental]", "ru")
    chorus = r["sections"][0]
    assert chorus["checked"] is False and chorus["typical"] is None and "fewer than 3" in chorus["reason"]
    assert r["summary"]["lines_flagged"] == 0


def test_lines_before_the_first_tag_are_one_section_with_no_name():
    r = lp.prepare_lyrics("раз два три\nраз два три\nраз два три\n[verse]\nа", "ru")
    assert r["sections"][0]["name"] is None and r["sections"][0]["checked"]
    assert r["sections"][1]["name"] == "verse"


# -- the audit --------------------------------------------------------------------------


def test_a_mark_on_a_consonant_is_flagged_and_nothing_is_inserted_into_that_word(dictionary):
    assert kinds(lp.prepare_lyrics(f"ко{A}шка", "ru", dictionary=dictionary)) == []  # valid, in the dictionary
    r = lp.prepare_lyrics(f"кош{A}ка", "ru", dictionary=dictionary)
    assert kinds(r) == ["mark_on_consonant"]
    w = words(r, 1)[f"кош{A}ка"]
    assert w["status"] == "invalid_mark" and w["options"] == [f"ко{A}шка"]
    assert r["marked_text"] == f"кош{A}ка"


def test_two_marks_in_a_word_a_mark_with_a_yo_and_a_spacing_accent_are_flagged(dictionary):
    r = lp.prepare_lyrics(f"ко{A}шк{A}а\nё{A}лка\nко´шка", "ru", dictionary=dictionary)
    assert kinds(r) == ["mark_on_consonant", "mark_with_yo", "spacing_accent", "two_marks"]
    spacing = next(i for i in r["issues"] if i["kind"] == "spacing_accent")
    assert spacing["line"] == 3 and spacing["word"] == "ко´шка"


def test_a_mark_with_no_letter_before_it_is_flagged(dictionary):
    r = lp.prepare_lyrics(f"раз {A}два\n{A}три", "ru", dictionary=dictionary)
    assert [(i["kind"], i["line"], i["column"]) for i in r["issues"]] == [
        ("mark_without_letter", 1, 4), ("mark_without_letter", 2, 0)]


def test_one_valid_mark_that_no_dictionary_form_has_contradicts_the_dictionary(dictionary):
    r = lp.prepare_lyrics(f"ладо{A}нь ла{A}донь", "ru", dictionary=dictionary)
    [issue] = r["issues"]
    assert issue["kind"] == "contradicts_dictionary" and issue["word"] == f"ла{A}донь"
    assert issue["options"] == [f"ладо{A}нь"]
    assert words(r, 1)[f"ла{A}донь"]["status"] == "author_marked"  # the author's mark stays
    assert r["marked_text"] == f"ладо{A}нь ла{A}донь"


def test_a_mark_that_is_one_of_a_homographs_forms_is_not_a_contradiction(dictionary):
    r = lp.prepare_lyrics(f"замо{A}к за{A}мок", "ru", dictionary=dictionary)
    assert r["issues"] == []


# -- the resolver -----------------------------------------------------------------------


def test_a_word_with_one_stress_in_the_dictionary_gets_the_mark_and_the_rest_of_the_text_is_untouched(dictionary):
    text = "[verse]\r\nТысячи ночь, ладонь —  огонь!\r\n\r\n  (стекло)\n"
    r = lp.prepare_lyrics(text, "ru", dictionary=dictionary, engine="yue2")
    assert r["marked_text"] == (
        f"[verse]\r\nТы{A}сячи ночь, ладо{A}нь —  ого{A}нь!\r\n\r\n  (стекло{A})\n")
    w = words(r, 2)
    assert w["Тысячи"]["status"] == "resolved" and w["Тысячи"]["mark"] == f"Ты{A}сячи"
    assert w["ночь"]["status"] == "not_needed"
    assert w["ладонь"]["status"] == "resolved"


def test_a_homograph_is_listed_with_its_options_and_never_guessed(dictionary):
    r = lp.prepare_lyrics("в замке и дома", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w["дома"]["status"] == "ambiguous"
    assert w["дома"]["options"] == [f"до{A}ма", f"дома{A}"]
    assert "mark" not in w["дома"]
    assert r["summary"]["counts"]["ambiguous"] == 1 and r["summary"]["pronunciation"] == "open_spots"
    assert r["marked_text"] == "в замке и дома"


def test_the_persons_pick_for_a_homograph_sets_the_mark(dictionary):
    text = "и дома, и дома"
    first = lp.prepare_lyrics(text, "ru", dictionary=dictionary, engine="yue2")
    keys = [w["key"] for w in first["lines"][0]["words"] if w["text"] == "дома"]
    assert keys == ["1:2", "1:10"]
    picked = lp.prepare_lyrics(text, "ru", dictionary=dictionary, engine="yue2",
                               choices={"1:2": f"дома{A}", "1:10": 0, "1:99": "x"})
    assert picked["marked_text"] == f"и дома{A}, и до{A}ма"
    assert picked["summary"]["marks_chosen"] == 2 and picked["summary"]["counts"]["ambiguous"] == 0
    assert picked["ignored_choices"] == ["1:99"]
    assert words(picked, 1)["дома"]["source"] == "person"
    # a pick that is not one of the options is ignored, the word stays ambiguous
    bad = lp.prepare_lyrics("дома", "ru", dictionary=dictionary, choices={"1:0": f"до{A}м{A}а"})
    assert bad["summary"]["counts"]["ambiguous"] == 1 and bad["ignored_choices"] == ["1:0"]


def test_a_word_the_dictionary_does_not_know_is_unknown_and_a_name_comes_from_the_cap_table(dictionary):
    r = lp.prepare_lyrics("Аарон крокодилище", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w["Аарон"]["status"] == "resolved" and w["Аарон"]["mark"] == f"Ааро{A}н"
    assert w["крокодилище"]["status"] == "unknown" and w["крокодилище"]["reason"] == "not_in_dictionary"
    assert r["marked_text"] == f"Ааро{A}н крокодилище"


def test_the_authors_mark_is_kept_and_a_word_with_a_yo_is_already_marked(dictionary):
    r = lp.prepare_lyrics(f"Замо{A}к ёлка", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w[f"Замо{A}к"]["status"] == "author_marked" and w["ёлка"]["status"] == "author_marked"
    assert r["marked_text"] == f"Замо{A}к ёлка"
    assert r["summary"]["marks_insertable"] == 0


def test_a_dictionary_stress_on_yo_restores_the_letter_instead_of_adding_a_mark(dictionary):
    r = lp.prepare_lyrics("Елка и елка", "ru", dictionary=dictionary, engine="yue2")
    assert r["marked_text"] == "Ёлка и ёлка"
    assert r["summary"]["marks_insertable"] == 2


def test_a_form_with_no_stress_is_not_used_and_a_monosyllable_needs_no_mark(dictionary):
    r = lp.prepare_lyrics("слово и я", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w["слово"]["status"] == "unknown"
    assert w["и"]["status"] == "not_needed" and w["я"]["status"] == "not_needed"


def test_a_hyphenated_word_is_looked_up_whole_then_part_by_part(dictionary):
    r = lp.prepare_lyrics("а-конто светит-светит", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w["а-конто"]["mark"] == f"а-ко{A}нто"
    assert w["светит-светит"]["mark"] == f"све{A}тит-све{A}тит"


def test_a_hyphenated_word_is_audited_against_the_dictionary_whole_or_part_by_part(dictionary):
    """Review #180 3.4: a hyphenated word dropped out of the contradiction audit."""
    whole = lp.prepare_lyrics(f"а-конто{A}", "ru", dictionary=dictionary)          # held whole: а-ко́нто
    [issue] = whole["issues"]
    assert issue["kind"] == "contradicts_dictionary" and issue["options"] == [f"а-ко{A}нто"] and "part" not in issue
    parts = lp.prepare_lyrics(f"ла{A}донь-ко{A}шка", "ru", dictionary=dictionary)  # not held whole
    [issue] = parts["issues"]
    assert issue["kind"] == "contradicts_dictionary" and issue["part"] == "ладонь"
    assert issue["options"] == [f"ладо{A}нь"] and "'ладонь'" in issue["message"]
    assert lp.prepare_lyrics(f"ладо{A}нь-ко{A}шка а-ко{A}нто", "ru", dictionary=dictionary)["issues"] == []


def test_latin_words_and_numbers_are_not_checked_and_a_mixed_line_still_counts(dictionary):
    r = lp.prepare_lyrics("Hello любовь 7", "ru", dictionary=dictionary)
    w = words(r, 1)
    assert w["Hello"]["status"] == "not_checked" and w["7"]["reason"] == "number"
    assert w["любовь"]["status"] == "resolved"
    assert r["marked_text"] == f"Hello любо{A}вь 7"
    assert r["lines"][0]["syllables"] == 2 + 2


def test_a_text_with_no_russian_has_no_pronunciation_to_check():
    r = lp.prepare_lyrics("[verse]\nHello world\nI love you", "en")
    assert r["summary"]["pronunciation"] == "not_applicable"
    assert r["records"]["marked"] is None


# -- without the dictionary -------------------------------------------------------------


def test_without_the_dictionary_the_meter_and_the_audit_still_work_and_the_report_says_so():
    text = (f"[verse]\nона идёт домой\nона идёт домой\nона идёт домой\n"
            f"она идёт домой и поёт про любовь и ночь\nко{A}шк{A}а")
    r = lp.prepare_lyrics(text, "ru", dictionary=None, dictionary_state="missing",
                          dictionary_message="the Russian stress dictionary is not installed")
    assert r["dictionary"]["state"] == "missing" and "not installed" in r["dictionary"]["message"]
    assert r["summary"]["lines_flagged"] == 2  # the long line and the short last line
    assert set(kinds(r)) == {"two_marks", "mark_on_consonant"}
    w = words(r, 3)
    assert w["идёт"]["status"] == "author_marked" and w["домой"]["status"] == "unknown"
    assert w["домой"]["reason"] == "dictionary_missing"
    assert r["summary"]["pronunciation"] == "dictionary_missing"
    assert r["marked_text"] == text
    assert r["records"]["plain"]["dictionary"] is None
    assert r["records"]["plain"]["dictionary_state"] == "missing"


def test_a_file_that_is_not_a_stress_dictionary_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "forms.sqlite"
    bad.write_bytes(b"this is not sqlite")
    try:
        d, state, message = lp.open_dictionary(bad)
        assert d is None and state == "error" and message
        absent, state2, _ = lp.open_dictionary(tmp_path / "nope.sqlite")
        assert absent is None and state2 == "missing"
    finally:
        lp.close_dictionaries()


def test_a_dictionary_is_opened_once_and_describes_itself(tmp_path):
    path = build_dictionary(tmp_path / "forms.sqlite")
    try:
        d1, _, _ = lp.open_dictionary(path)
        d2, _, _ = lp.open_dictionary(path)
        assert d1 is d2
        info = d1.info()
        assert info["source"] == "test fixture" and info["lang"] == "ru"
        assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        lp.close_dictionaries()


def test_the_dictionary_is_hashed_once_per_process_until_its_size_or_mtime_changes(tmp_path, monkeypatch):
    """Review #180 3.1: the 120 MB sha256 ran on every (re)open."""
    path = build_dictionary(tmp_path / "forms.sqlite")
    hashed = []
    real = lp._hash_file
    monkeypatch.setattr(lp, "_hash_file", lambda p: hashed.append(p) or real(p))
    try:
        for _ in range(3):
            d, _, _ = lp.open_dictionary(path)
            first = d.info()["sha256"]
            lp.close_dictionaries()                       # the next open is a fresh StressDictionary
        assert len(hashed) == 1 and first == hashlib.sha256(path.read_bytes()).hexdigest()
        db = sqlite3.connect(path)                        # the file changes: hashed again
        db.execute("INSERT INTO forms VALUES ('мир', 'ми́р')")
        db.commit()
        db.close()
        st = path.stat()                                  # a coarse-mtime file system still sees a change
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        d, _, _ = lp.open_dictionary(path)
        assert d.info()["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() != first
        assert len(hashed) == 2
    finally:
        lp.close_dictionaries()


# -- the records ------------------------------------------------------------------------


def test_the_record_says_what_was_applied_and_which_text_went_to_the_engine(dictionary):
    text = f"Тысячи ночь замок\nладо{A}нь"
    r = lp.prepare_lyrics(text, "auto", dictionary=dictionary, engine="yue2")
    plain, marked = r["records"]["plain"], r["records"]["marked"]
    assert plain["engine_text"] == "plain" and plain["marks_inserted"] == 0
    assert plain["sent_text_sha256"] == plain["author_text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert marked["engine_text"] == "marked" and marked["marks_inserted"] == 1  # only Тысячи
    assert marked["stress_marks_in_sent_text"] == 2  # the one inserted + the author's own
    assert marked["sent_text_sha256"] == hashlib.sha256(r["marked_text"].encode("utf-8")).hexdigest()
    assert marked["sent_text_sha256"] != marked["author_text_sha256"]
    assert marked["counts"]["ambiguous"] == 1 and marked["counts"]["author_marked"] == 1
    assert marked["dictionary"]["source"] == "test fixture" and len(marked["dictionary"]["sha256"]) == 64
    assert marked["engine"] == "yue2" and marked["language"] == "ru" and marked["rules_version"] == 1


def test_acestep_is_offered_no_marked_text_and_says_why(dictionary):
    r = lp.prepare_lyrics("Тысячи огонь", "ru", dictionary=dictionary, engine="acestep")
    assert r["engine"]["accepts_marks"] is False and "acestep" in r["engine"]["why"]
    assert r["records"]["marked"] is None
    assert r["records"]["plain"]["engine_text"] == "plain"
    assert r["summary"]["marks_insertable"] == 2  # still shown to the person


def test_the_text_is_never_changed_by_the_report_and_only_marks_differ_in_the_marked_text(dictionary):
    text = "[verse]\nТысячи умов  \t\n\n"
    r = lp.prepare_lyrics(text, "ru", dictionary=dictionary)
    assert r["marked_text"].replace(A, "") == text
