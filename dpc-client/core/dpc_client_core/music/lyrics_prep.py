"""Lyrics preparation for a vocal music job (backlog card A-VOCAL-JOB-SENDS-THE-AUTHORS-
LYRICS-TO-THE-ENGINE-UNCHECKED-...; SPEC section 3 "Lyrics preparation and pronunciation",
section 12 decision 39).

`prepare_lyrics(text, language)` is a pure function: it reads the author's lyrics and
returns a report, and changes nothing. It does four things, none of which needs a GPU.

1. **Syllables per line.** Russian (and the other Cyrillic letters): one vowel letter is one
   syllable, stress marks and a vowel-less word (в, с, к: sung with the next word) count 0.
   Latin words: a heuristic (vowel groups, a silent final "e", "es" and "ed" endings), good
   to about one syllable per word; it is stated in the record as a heuristic. A number adds
   0 and is listed, since how it is read is the person's choice.
2. **A meter check per section.** A section is the lines under a `[verse]`-style tag (lines
   before the first tag are one section without a name). The typical length of a section is
   the median syllable count of its lines (`median_low`, whole syllables); a line is flagged
   `long` or `short` when it differs from it by more than the tolerance,
   `max(2, ceil(0.25 * typical))` syllables. A section of fewer than 3 sung lines has no
   typical length and is not checked. These numbers are CC's proposal: no ADR or SPEC text
   fixes them; they are in the record (`rules`) so a reader can see what was applied.
3. **An audit of the U+0301 marks the author wrote**, per Cyrillic word: `mark_without_letter`
   (a mark with no letter before it), `mark_on_consonant`, `spacing_accent` (a look-alike
   such as the acute sign U+00B4 inside a word, which sets no stress), `two_marks` (two marks
   in one word or one hyphen part), `mark_with_yo` (a mark in a word that has e-diaeresis,
   which is always stressed) and `contradicts_dictionary` (one mark per word, or per hyphen part when
   the dictionary does not hold the hyphenated word whole, in a place none of the
   dictionary's forms of the word has).
4. **A stress resolver for Russian words.** The order is SPEC decision 39's chain, as far
   as it is built: the author's own mark is kept; then the large dictionary; a word with one
   stress in the dictionary gets the mark; a homograph (за́мок / замо́к) is named with its
   options and **never guessed**; a word the dictionary does not know is counted as
   unknown. (The per-song dictionary and the person's own dictionary of the chain are not
   built yet: the person's pick for a homograph, `choices`, is the per-text form of it.)
   A word with one vowel needs no mark. A word the author wrote with e-diaeresis is
   already marked. Each word ends with one **pronunciation status**: `resolved`, `ambiguous`,
   `unknown`, `author_marked`, plus `invalid_mark` (the author's mark is not usable),
   `not_needed` (one vowel or none) and `not_checked` (a Latin word or a number: no
   dictionary for them).

The marked text adds U+0301 after the stressed vowel only where a word is `resolved` (by the
dictionary or by the person's pick) and the author did not mark it; a dictionary stress on
e-diaeresis restores the letter instead. Nothing else in the text moves.

**The dictionary** is the Zaliznyak `forms.sqlite` the person downloaded
(`<model files folder>/lyrics/stress/ru/forms.sqlite`; CC BY-NC 4.0, never in this repository,
SPEC decision 39). Layout, read from the file: `forms(k TEXT PRIMARY KEY, f TEXT)`, key
the lower-case form with e-diaeresis written as plain e, value the stressed form with U+0301
after the stressed vowel (or e-diaeresis), several forms for a homograph joined by `|`;
`cap(k, f)` the same for names, consulted when `forms` has no entry; `about(k, v)` with
`source`, `licence`, `built`, `lang`, `keys`. Opened read-only, once per file and cached.
Without the file everything but the stress resolver still works, and the report says
`dictionary.state = "missing"`.

**What the engine receives** (SPEC section 1 flow "Lyrics -> new song", decision 40 and the
yue2.cpp trial `trial.md` (c)): yue2.cpp's tokenizer applies NFC, which leaves U+0301 as a
separate code point, so the model sees the marks; YuE2 is the only engine for which U+0301
is documented to set the stress (`ENGINES_WITH_STRESS_MARKS`). The effect on the sung result
is not established (one stock-backbone pair got better, the RS-TW pair worse; listening
pending). So the marked text goes to the engine only when the person presses "Use marked
lyrics"; ACE-Step is offered no marked text. The record (`build_record`) says which text
went.

Idea of the rules from Ruach Studio's lyrics tools (docs/RUACH-BORROW.md); written from the
description of the rules, no code copied.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import statistics
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

RULES_VERSION = 1
RECORD_SCHEMA = "dpc.lyrics_prep/1"
REPORT_SCHEMA = "dpc.lyrics_prep.report/1"

MARK = "́"
# What the engines do with U+0301: see the module docstring.
ENGINES_WITH_STRESS_MARKS = frozenset({"yue2"})

DICTIONARY_RELATIVE_PATH = Path("lyrics") / "stress" / "ru" / "forms.sqlite"

# -- the meter rule ----------------------------------------------------------------------
METER_MIN_LINES = 3
METER_TOLERANCE_MIN = 2
METER_TOLERANCE_FRACTION = 0.25
MAX_OPTIONS = 8

CYRILLIC_VOWELS = frozenset("аеёиоуыэюяіїє")
_YO = "ё"
_SPACING_ACCENTS = "´ˊ΄"

_LETTER = r"[^\W\d_]"
_WORD_CHAR = rf"(?:{_LETTER}|{MARK}|[{_SPACING_ACCENTS}])"
_TOKEN = re.compile(rf"\d+|{_LETTER}{_WORD_CHAR}*(?:[-'’]{_LETTER}{_WORD_CHAR}*)*")
_ORPHAN_MARK = re.compile(rf"(?<!{_LETTER}){MARK}")
_SECTION = re.compile(r"^\[[^\[\]]*\]$")


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


# -- syllables ---------------------------------------------------------------------------


def russian_syllables(word: str) -> int:
    """Vowel letters of `word`: one per syllable (a word without a vowel has none)."""
    return sum(1 for ch in word.lower() if ch in CYRILLIC_VOWELS)


def english_syllables(word: str) -> int:
    """A heuristic: vowel groups, less a silent final e (not "-le" after a consonant) and a
    silent "-es"/"-ed"; at least 1 for any word with a letter. Good to about one syllable."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    n = len(re.findall(r"[aeiouy]+", w))
    if n > 1:
        if w.endswith("e") and not (w.endswith("le") and len(w) > 2 and w[-3] not in "aeiouy") \
                and not w.endswith(("ee", "ye", "ie")):
            n -= 1
        elif w.endswith("ed") and not w.endswith(("ted", "ded")):
            n -= 1
        elif w.endswith("es") and not w.endswith(("ses", "xes", "zes", "ches", "shes", "ces", "ges")):
            n -= 1
    return max(n, 1)


# -- the dictionary ----------------------------------------------------------------------


class DictionaryError(Exception):
    pass


class StressDictionary:
    """One `forms.sqlite`, opened read-only. Thread-safe (one lock around the connection)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._memo: Dict[str, Optional[str]] = {}
        self._info: Optional[Dict[str, Any]] = None
        self._db: Optional[sqlite3.Connection] = None
        try:
            st = self.path.stat()
            self._stamp = (st.st_size, st.st_mtime_ns)
            self._db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                       check_same_thread=False)
            self._db.execute("SELECT k, f FROM forms LIMIT 1").fetchall()
            self.has_cap = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cap'").fetchone() is not None
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise DictionaryError(f"the stress dictionary cannot be read: {exc}") from exc

    def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def lookup(self, key: str) -> Optional[str]:
        """The stored value for `key` (lower case, e-diaeresis folded to e): `forms` first,
        then `cap`. None when neither has it."""
        with self._lock:
            if key in self._memo:
                return self._memo[key]
            found: Optional[str] = None
            if self._db is not None:
                for table in ("forms", "cap") if self.has_cap else ("forms",):
                    row = self._db.execute(f"SELECT f FROM {table} WHERE k=?", (key,)).fetchone()
                    if row and isinstance(row[0], str) and row[0]:
                        found = row[0]
                        break
            if len(self._memo) > 50000:
                self._memo.clear()
            self._memo[key] = found
            return found

    def info(self) -> Dict[str, Any]:
        """What the record says about the dictionary: its `about` rows and its sha256
        (`_dictionary_sha256`: hashed once per process for a given size and mtime)."""
        if self._info is None:
            about: Dict[str, str] = {}
            with self._lock:
                try:
                    about = {str(k): str(v) for k, v in self._db.execute("SELECT k, v FROM about").fetchall()}
                except sqlite3.Error:
                    about = {}
            digest = _dictionary_sha256(self.path, self._stamp)
            self._info = {
                "lang": about.get("lang", "ru"),
                "source": about.get("source"),
                "licence": about.get("licence"),
                "built": about.get("built"),
                "keys": about.get("keys"),
                "sha256": digest,
            }
        return dict(self._info)


_cache_lock = threading.Lock()
_cache: Dict[str, StressDictionary] = {}
_sha_lock = threading.Lock()
_sha_by_stamp: Dict[Tuple[str, int, int], str] = {}


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dictionary_sha256(path: Path, stamp: Optional[Tuple[int, int]]) -> str:
    """The file's sha256, hashed once per process for each (path, size, mtime): the dictionary
    is ~120 MB and is reopened whenever the cache is closed or the file is touched."""
    if stamp is None:
        st = Path(path).stat()
        stamp = (st.st_size, st.st_mtime_ns)
    key = (str(Path(path).resolve()), int(stamp[0]), int(stamp[1]))
    with _sha_lock:
        known = _sha_by_stamp.get(key)
    if known is not None:
        return known
    digest = _hash_file(Path(path))
    with _sha_lock:
        if len(_sha_by_stamp) > 16:
            _sha_by_stamp.clear()
        _sha_by_stamp[key] = digest
    return digest


def open_dictionary(path: Path) -> Tuple[Optional[StressDictionary], str, Optional[str]]:
    """`(dictionary, state, message)`: state `ok`, `missing` (no file) or `error` (a file
    that is not a stress dictionary). One open dictionary per file, reopened when the file
    changed."""
    p = Path(path)
    if not p.is_file():
        return None, "missing", "the Russian stress dictionary is not installed"
    key = str(p.resolve())
    with _cache_lock:
        cached = _cache.get(key)
        try:
            st = p.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError as exc:
            return None, "error", str(exc)
        if cached is not None and cached._stamp == stamp:
            return cached, "ok", None
        if cached is not None:
            cached.close()
            _cache.pop(key, None)
        try:
            d = StressDictionary(p)
        except DictionaryError as exc:
            return None, "error", str(exc)
        _cache[key] = d
        return d, "ok", None


def close_dictionaries() -> None:
    """Close every cached dictionary (tests on Windows, which cannot delete an open file)."""
    with _cache_lock:
        for d in _cache.values():
            d.close()
        _cache.clear()


# -- stress forms ------------------------------------------------------------------------

# A stress: (acute positions, e-diaeresis positions), indexes into the word's characters.
Stress = Tuple[frozenset, frozenset]


def _fold(word: str) -> str:
    return word.lower().replace(_YO, "е")


def _parse_form(form: str) -> Optional[Tuple[str, Stress]]:
    """(folded letters, stress) of one stored form, or None when it has no letters."""
    letters: List[str] = []
    acute: set = set()
    yo: set = set()
    for ch in form.lower():
        if ch == MARK:
            if letters:
                acute.add(len(letters) - 1)
        elif ch == _YO:
            yo.add(len(letters))
            letters.append("е")
        else:
            letters.append(ch)
    if not letters:
        return None
    return "".join(letters), (frozenset(acute), frozenset(yo))


def _stress_options(dictionary: StressDictionary, folded: str) -> List[Stress]:
    """The distinct usable stresses the dictionary gives `folded`, in its order. A form whose
    letters differ from the key, with a mark on a non-vowel, or with no stress in a word of
    two or more vowels is dropped."""
    value = dictionary.lookup(folded)
    if not value:
        return []
    out: List[Stress] = []
    for form in value.split("|"):
        parsed = _parse_form(form)
        if parsed is None or parsed[0] != folded:
            continue
        stress = parsed[1]
        positions = stress[0] | stress[1]
        if any(p >= len(folded) or folded[p] not in CYRILLIC_VOWELS for p in positions):
            continue
        if not positions and russian_syllables(folded) > 1:
            continue
        if stress not in out:
            out.append(stress)
    return out


def _apply_stress(word: str, stress: Stress, offset: int = 0) -> str:
    """`word` (no marks of its own) with `stress` applied: U+0301 after each acute position,
    e for e-diaeresis at each yo position; positions are shifted by `offset`."""
    acute, yo = stress
    out: List[str] = []
    for i, ch in enumerate(word):
        j = i + offset
        if j in yo and _fold(ch) == "е":
            out.append("Ё" if ch.isupper() else _YO)
        else:
            out.append(ch)
        if j in acute:
            out.append(MARK)
    return "".join(out)


# -- one word ----------------------------------------------------------------------------


def _strip_marks(raw: str) -> str:
    return "".join(ch for ch in raw if ch != MARK and ch not in _SPACING_ACCENTS)


def _script_of(plain: str) -> str:
    cyr = any(_is_cyrillic(c) for c in plain)
    lat = any(c.isalpha() and not _is_cyrillic(c) for c in plain)
    if cyr and lat:
        return "mixed"
    return "cyrillic" if cyr else "latin"


def _audit_word(raw: str, plain_lower: str, dictionary: Optional[StressDictionary],
                has_hyphen: bool) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Issues of a Cyrillic word's own marks, the number of marks that sit on a vowel, and
    whether the word has a mark that is not usable."""
    issues: List[Dict[str, Any]] = []
    valid = 0
    invalid = False
    for i, ch in enumerate(raw):
        if ch in _SPACING_ACCENTS:
            issues.append({"kind": "spacing_accent",
                           "message": f"'{ch}' looks like a stress mark but is a separate sign; the engine does not read it as stress. Use U+0301 right after the vowel."})
            invalid = True
        elif ch == MARK:
            prev = raw[i - 1] if i else ""
            if not prev or not prev.isalpha():
                continue  # after another mark: counted by two_marks
            if prev.lower() in CYRILLIC_VOWELS:
                valid += 1
            else:
                issues.append({"kind": "mark_on_consonant",
                               "message": f"the stress mark is on '{prev}', which is not a vowel"})
                invalid = True
    # two marks in one hyphen part
    for part in raw.split("-"):
        n = part.count(MARK)
        if n >= 2:
            issues.append({"kind": "two_marks", "message": f"{n} stress marks in one word"})
            invalid = True
        if n >= 1 and (_YO in part.lower()):
            issues.append({"kind": "mark_with_yo",
                           "message": "a stress mark in a word with ё, which is always stressed"})
            invalid = True
    if not invalid and valid >= 1 and dictionary is not None and _YO not in raw.lower():
        whole = _stress_options(dictionary, _fold(plain_lower))
        if whole and (valid == 1 or has_hyphen):
            issues.extend(_contradiction(raw, plain_lower, whole))
        elif has_hyphen and not whole:
            # Not held whole: each hyphen part with its one mark against that part's own forms.
            for part in raw.split("-"):
                if part.count(MARK) != 1:
                    continue
                part_lower = _strip_marks(part).lower()
                issues.extend(_contradiction(part, part_lower, _stress_options(dictionary, _fold(part_lower)),
                                             part=part_lower))
    return issues, valid, invalid


def _contradiction(raw: str, plain_lower: str, options: List[Stress],
                   part: Optional[str] = None) -> List[Dict[str, Any]]:
    """A `contradicts_dictionary` issue when the author's marks in `raw` are none of `options`."""
    author = frozenset(i for i, ch in enumerate(_strip_marks_keep_index(raw)) if ch[1])
    if not options or any((o[0] | o[1]) == author for o in options):
        return []
    shown = [_apply_stress(plain_lower, o) for o in options][:MAX_OPTIONS]
    issue: Dict[str, Any] = {"kind": "contradicts_dictionary", "options": shown,
                             "message": ("the dictionary does not have this stress for "
                                         + (f"the part '{part}': " if part else "the word: ") + ", ".join(shown))}
    if part:
        issue["part"] = part
    return [issue]


def _strip_marks_keep_index(raw: str) -> List[Tuple[str, bool]]:
    """`(letter, followed by U+0301)` per letter of a word with its spacing accents removed."""
    out: List[Tuple[str, bool]] = []
    for ch in raw:
        if ch == MARK:
            if out:
                out[-1] = (out[-1][0], True)
        elif ch in _SPACING_ACCENTS:
            continue
        else:
            out.append((ch, False))
    return out


def _resolve_options(dictionary: StressDictionary, plain: str) -> Tuple[List[Tuple[str, Stress]], str]:
    """`(options, reason)`: the stresses of a mark-free Cyrillic `plain` word as
    `(rendered word, stress)` pairs; none gives a `reason` (`not_in_dictionary`,
    `too_many_combinations`). A hyphenated word the dictionary does not hold whole is
    resolved part by part; the options are the combinations."""
    folded = _fold(plain)
    whole = _stress_options(dictionary, folded)
    if whole:
        return [(_apply_stress(plain, s), s) for s in whole], ""
    if "-" not in plain:
        return [], "not_in_dictionary"
    parts = plain.split("-")
    combos: List[Stress] = [(frozenset(), frozenset())]
    offset = 0
    for part in parts:
        if russian_syllables(part) <= 1 and part:
            offset += len(part) + 1
            continue
        opts = _stress_options(dictionary, _fold(part)) if part else []
        if not opts:
            return [], "not_in_dictionary"
        shifted = [(frozenset(p + offset for p in a), frozenset(p + offset for p in y)) for a, y in opts]
        combos = [(a1 | a2, y1 | y2) for (a1, y1) in combos for (a2, y2) in shifted]
        if len(combos) > MAX_OPTIONS:
            return [], "too_many_combinations"
        offset += len(part) + 1
    return [(_apply_stress(plain, s), s) for s in combos], ""


# -- the report --------------------------------------------------------------------------


def _detect_language(words: Iterable[str]) -> Optional[str]:
    cyr = lat = 0
    for w in words:
        for ch in w:
            if _is_cyrillic(ch):
                cyr += 1
            elif ch.isalpha():
                lat += 1
    if not cyr and not lat:
        return None
    return "ru" if cyr >= lat else "en"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tolerance_for(typical: int, override: Optional[int]) -> int:
    if override is not None:
        return override
    return max(METER_TOLERANCE_MIN, math.ceil(METER_TOLERANCE_FRACTION * typical))


def prepare_lyrics(text: str, language: str = "auto", *, dictionary: Optional[StressDictionary] = None,
                   dictionary_state: str = "missing", dictionary_message: Optional[str] = None,
                   choices: Optional[Mapping[str, Any]] = None, engine: Optional[str] = None,
                   tolerance: Optional[int] = None) -> Dict[str, Any]:
    """The report on `text` (see the module docstring); `text` is never changed.

    `language` is `auto` (read from the script), or a code the person names; Cyrillic words
    are resolved when it is `auto` or `ru`. `dictionary` is an open `StressDictionary`, or
    None with `dictionary_state` (`missing`, `error`). `choices` maps a word's `key`
    (`"<line>:<column>"`) to one of its `options`, the person's pick for a homograph.
    `engine` (`yue2`, `acestep`) decides whether marked lyrics are offered."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    requested = (language or "auto").strip().lower() or "auto"
    raw_lines = text.split("\n")
    # Pass 1: tokens of every line, to detect the language.
    parsed: List[Dict[str, Any]] = []
    all_words: List[str] = []
    for n, raw_line in enumerate(raw_lines, 1):
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            kind = "blank"
            tokens: List[Any] = []
        elif _SECTION.match(stripped):
            kind = "section"
            tokens = []
        else:
            kind = "lyric"
            tokens = list(_TOKEN.finditer(line))
            all_words.extend(t.group(0) for t in tokens)
        parsed.append({"n": n, "line": line, "kind": kind, "tokens": tokens})
    detected = _detect_language(all_words)
    effective = detected if requested == "auto" else requested
    resolve_ru = requested in ("auto", "ru")
    picks = dict(choices or {})
    used_picks: set = set()

    sections: List[Dict[str, Any]] = [{"index": 0, "name": None, "lines": []}]
    lines_out: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    marked_lines: List[str] = []
    counts = {"resolved": 0, "ambiguous": 0, "unknown": 0, "author_marked": 0, "invalid_mark": 0,
              "not_needed": 0, "not_checked": 0}
    marks_inserted = 0
    marks_chosen = 0
    resolver_wanted = 0  # Cyrillic words that need a stress

    for item in parsed:
        n, line, kind = item["n"], item["line"], item["kind"]
        if kind == "section":
            name = line.strip()[1:-1].strip()
            sections.append({"index": len(sections), "name": name, "lines": []})
            lines_out.append({"n": n, "kind": "section", "text": line, "section": len(sections) - 1})
            marked_lines.append(raw_lines[n - 1])
            continue
        if kind == "blank":
            marked_lines.append(raw_lines[n - 1])
            continue
        sec = sections[-1]
        words: List[Dict[str, Any]] = []
        syllables = 0
        replacements: List[Tuple[int, int, str]] = []
        notes: List[str] = []
        covered: List[Tuple[int, int]] = []
        for tok in item["tokens"]:
            raw = tok.group(0)
            start, end = tok.span()
            covered.append((start, end))
            key = f"{n}:{start}"
            word: Dict[str, Any] = {"key": key, "text": raw, "start": start, "end": end}
            if raw.isdigit():
                word.update(script="number", syllables=0, status="not_checked", reason="number")
                counts["not_checked"] += 1
                notes.append(f"the number {raw} is not counted and its reading is not checked")
                words.append(word)
                continue
            plain = _strip_marks(raw)
            script = _script_of(plain)
            word["script"] = script
            if script == "cyrillic":
                syl = russian_syllables(plain)
            elif script == "latin":
                syl = english_syllables(plain)
            else:  # mixed letters: count as Russian vowels plus English groups is a guess
                syl = russian_syllables(plain)
            word["syllables"] = syl
            syllables += syl
            if script != "cyrillic" or not resolve_ru:
                word.update(status="not_checked",
                            reason="no dictionary for this script" if script != "cyrillic" else "language is not Russian")
                counts["not_checked"] += 1
                words.append(word)
                continue
            plain_lower = plain.lower()
            has_hyphen = "-" in plain
            word_issues, valid_marks, has_invalid = _audit_word(raw, plain_lower, dictionary, has_hyphen)
            for wi in word_issues:
                issues.append({"line": n, "column": start, "word": raw, "key": key, **wi})
            word["audit"] = [wi["kind"] for wi in word_issues]
            if valid_marks and not has_invalid:
                word.update(status="author_marked", source="author")
                counts["author_marked"] += 1
                words.append(word)
                continue
            if _YO in plain_lower and syl > 1 and not has_invalid and MARK not in raw:
                word.update(status="author_marked", source="yo")
                counts["author_marked"] += 1
                words.append(word)
                continue
            if syl <= 1 and not has_invalid:
                word.update(status="not_needed", reason="one vowel" if syl else "no vowel")
                counts["not_needed"] += 1
                words.append(word)
                continue
            resolver_wanted += 1
            if has_invalid or MARK in raw:
                # the author's mark is not usable: list what the dictionary says, insert nothing
                options = []
                if dictionary is not None:
                    options = [r for r, _ in _resolve_options(dictionary, _strip_marks(raw))[0]][:MAX_OPTIONS]
                word.update(status="invalid_mark", options=options,
                            reason="the author's mark is not usable; nothing was added")
                counts["invalid_mark"] += 1
                words.append(word)
                continue
            if dictionary is None:
                word.update(status="unknown", reason="dictionary_missing")
                counts["unknown"] += 1
                words.append(word)
                continue
            options, why = _resolve_options(dictionary, plain)
            if not options:
                word.update(status="unknown", reason=why or "not_in_dictionary")
                counts["unknown"] += 1
                words.append(word)
                continue
            rendered = [r for r, _ in options][:MAX_OPTIONS]
            pick = picks.get(key)
            if pick is not None:
                chosen = rendered[pick] if isinstance(pick, int) and not isinstance(pick, bool) \
                    and 0 <= pick < len(rendered) else (pick if pick in rendered else None)
                if chosen is not None:
                    used_picks.add(key)
                    word.update(status="resolved", source="person", options=rendered, mark=chosen)
                    counts["resolved"] += 1
                    marks_chosen += 1
                    marks_inserted += _marks_in(chosen)
                    replacements.append((start, end, chosen))
                    words.append(word)
                    continue
            if len(rendered) == 1:
                word.update(status="resolved", source="dictionary", mark=rendered[0])
                counts["resolved"] += 1
                marks_inserted += _marks_in(rendered[0])
                replacements.append((start, end, rendered[0]))
            else:
                word.update(status="ambiguous", options=rendered, source="dictionary")
                counts["ambiguous"] += 1
            words.append(word)
        # marks outside any token (a mark with no letter before it)
        for m in _ORPHAN_MARK.finditer(line):
            if any(s <= m.start() < e for s, e in covered):
                continue
            issues.append({"line": n, "column": m.start(), "word": "", "key": f"{n}:{m.start()}",
                           "kind": "mark_without_letter",
                           "message": "a stress mark with no letter before it"})
        out_line = line
        for start, end, new in sorted(replacements, reverse=True):
            out_line = out_line[:start] + new + out_line[end:]
        marked_lines.append(out_line + raw_lines[n - 1][len(line):])
        entry = {"n": n, "kind": "lyric", "text": line, "section": sec["index"], "syllables": syllables,
                 "words": words, "notes": notes, "meter": {"flag": None, "delta": 0}}
        lines_out.append(entry)
        sec["lines"].append(entry)

    # The meter, per section.
    sections_out: List[Dict[str, Any]] = []
    flagged = 0
    for sec in sections:
        sung = [e for e in sec["lines"] if e["syllables"] > 0]
        info: Dict[str, Any] = {"index": sec["index"], "name": sec["name"], "line_count": len(sung)}
        if sec["name"] is None and not sec["lines"]:
            continue
        if len(sung) < METER_MIN_LINES:
            info.update(checked=False, typical=None, tolerance=None,
                        reason=f"fewer than {METER_MIN_LINES} sung lines")
        else:
            typical = int(statistics.median_low(e["syllables"] for e in sung))
            tol = _tolerance_for(typical, tolerance)
            info.update(checked=True, typical=typical, tolerance=tol, reason=None)
            for e in sung:
                delta = e["syllables"] - typical
                e["meter"] = {"delta": delta, "flag": ("long" if delta > 0 else "short") if abs(delta) > tol else None}
                if e["meter"]["flag"]:
                    flagged += 1
        sections_out.append(info)

    marked_text = "\n".join(marked_lines)
    ignored = sorted(k for k in picks if k not in used_picks)
    n_issues: Dict[str, int] = {}
    for i in issues:
        n_issues[i["kind"]] = n_issues.get(i["kind"], 0) + 1

    if resolver_wanted == 0 and not counts["author_marked"]:
        pron = "not_applicable"
    elif dictionary is None and counts["unknown"] and not counts["resolved"]:
        pron = "dictionary_missing"
    elif counts["ambiguous"] or counts["unknown"] or counts["invalid_mark"]:
        pron = "open_spots"
    else:
        pron = "checked"

    accepts = bool(engine and engine in ENGINES_WITH_STRESS_MARKS)
    dict_block: Dict[str, Any] = {"state": dictionary_state if dictionary is None else "ok",
                                  "message": dictionary_message if dictionary is None else None}
    if dictionary is not None:
        dict_block.update(dictionary.info())
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "language": {"requested": requested, "detected": detected, "effective": effective},
        "dictionary": dict_block,
        "engine": {"key": engine, "accepts_marks": accepts,
                   "why": ("YuE2 reads U+0301 after a vowel as stress (SPEC decision 39)" if accepts else
                           "no engine has marked lyrics offered" if not engine else
                           f"{engine} is not documented to read stress marks; its lyrics stay as written")},
        "lines": lines_out,
        "sections": sections_out,
        "issues": issues,
        "summary": {
            "lines": sum(1 for e in lines_out if e["kind"] == "lyric"),
            "syllables": sum(e["syllables"] for e in lines_out if e["kind"] == "lyric"),
            "sections_checked": sum(1 for s in sections_out if s["checked"]),
            "lines_flagged": flagged,
            "counts": counts,
            "audit_issues": n_issues,
            "marks_insertable": marks_inserted,
            "marks_chosen": marks_chosen,
            "pronunciation": pron,
        },
        "ignored_choices": ignored,
        "author_text_sha256": _sha(text),
        "marked_text": marked_text,
        "rules": {
            "version": RULES_VERSION,
            "syllables": "cyrillic: vowel letters; latin: heuristic (about one syllable per word); numbers: 0",
            "meter": ("typical = median of the section's sung lines; flag when |syllables - typical| > "
                      + (str(tolerance) if tolerance is not None else
                         f"max({METER_TOLERANCE_MIN}, ceil({METER_TOLERANCE_FRACTION} * typical))")
                      + f"; sections of fewer than {METER_MIN_LINES} sung lines are not checked"),
        },
    }
    report["records"] = {
        "plain": build_record(report, text, use_marked=False),
        "marked": build_record(report, text, use_marked=True) if accepts and marks_inserted else None,
    }
    return report


def _marks_in(rendered: str) -> int:
    return rendered.count(MARK) + rendered.lower().count(_YO)


def build_record(report: Mapping[str, Any], author_text: str, *, use_marked: bool) -> Dict[str, Any]:
    """`params.lyrics_prep` of a job: what was applied to the text the engine gets. The text
    sent is the author's, or the marked text when `use_marked`; its sha256 is in the record so
    the job's `payload.json` can be matched to it."""
    summary = report["summary"]
    sent = report["marked_text"] if use_marked else author_text
    d = report["dictionary"]
    dictionary = None if d["state"] != "ok" else {
        k: d.get(k) for k in ("lang", "source", "licence", "built", "keys", "sha256")}
    return {
        "schema": RECORD_SCHEMA,
        "rules_version": RULES_VERSION,
        "language": report["language"]["effective"],
        "language_requested": report["language"]["requested"],
        "engine": report["engine"]["key"],
        "engine_text": "marked" if use_marked else "plain",
        "author_text_sha256": report["author_text_sha256"],
        "sent_text_sha256": _sha(sent),
        "marks_inserted": summary["marks_insertable"] if use_marked else 0,
        "marks_chosen": summary["marks_chosen"] if use_marked else 0,
        "stress_marks_in_sent_text": sent.count(MARK),
        "counts": dict(summary["counts"]),
        "pronunciation": summary["pronunciation"],
        "meter": {"sections_checked": summary["sections_checked"], "lines_flagged": summary["lines_flagged"]},
        "audit": dict(summary["audit_issues"]),
        "dictionary_state": d["state"],
        "dictionary": dictionary,
        "rules": dict(report["rules"]),
    }


def default_dictionary_path(model_files_dir: Path) -> Path:
    """Where the Russian dictionary lives under the model files folder."""
    return Path(model_files_dir) / DICTIONARY_RELATIVE_PATH
