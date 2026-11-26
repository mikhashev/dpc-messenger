"""The score side of a yue2.cpp cover: what SheetSage2's transcription is used for before the song is sung.

A cover on yue2.cpp (`similar._cover_on_yue2`) sends the engine a score of the source, written by yue2.cpp's
`/transcribe` in its native two-voice ABC dialect (a `Vocal` voice for the sung line, an `Ins` voice for the
lead instrument, `% <name>` comment lines that name the sections). This module does the pure text and number
work around that score:

- `transcriber_gain_db`: whether a recording is too loud for the transcriber, and the one gain that brings it
  into bounds (the audio side is `similar._level_for_transcriber`).
- `keep_vocal_voice`: the score with the Ins voice silenced, for "Vocal melody only".
- `score_head`, `bar_seconds`, `score_parts`: the score's tempo, meter and key, the length of a bar, and its
  sections with their times and whether the voice sings in them.
- `pre_tag` and `match_sections` (Match sections): the lyrics' blocks tagged with the section of the score they
  are sung in, by the times of the track's synced lyrics; tags added for sections the lyrics do not reach; the
  words never change. `counts_of` sums up its report.
- `agree_style`: the style caption made to agree with the score's tempo and key.

The approach follows the ideas of Ruach Studio's cover chain; it is reimplemented from a description of the
rules, no code copied. Pure functions, standard library only, no I/O.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# -- the level the transcriber hears ----------------------------------------------------------------------

LEVEL_LUFS_MAX = -14.0          # louder than this integrated loudness: level it
LEVEL_PEAK_MAX_DB = -1.0        # or a peak above this
LEVEL_TARGET_LUFS = -16.0       # the loudness one gain aims at
LEVEL_PEAK_CEILING_DB = -1.5    # and the peak it may leave at most


def _reading(value: Any) -> Optional[float]:
    """A measured number, or None when the value is no usable reading (None, text, bool, NaN, +-inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def transcriber_gain_db(lufs: Any, peak_db: Any, mix_peak_db: Any = None) -> Optional[float]:
    """The one gain in dB (2 decimals) that brings a recording into the transcriber's bounds, or None to leave
    it as it is.

    It acts only when the loudness is above `LEVEL_LUFS_MAX` or the peak above `LEVEL_PEAK_MAX_DB`. With a
    loudness, the gain aims at `LEVEL_TARGET_LUFS` but never leaves the peak above `LEVEL_PEAK_CEILING_DB`; the
    peak is `peak_db`, or, when that is unknown, `mix_peak_db` (the mix's own sample peak; a non-finite one,
    such as -inf for digital silence, means no gain at all). With no loudness, the peak alone is brought to the
    ceiling."""
    loud, peak = _reading(lufs), _reading(peak_db)
    too_loud = loud is not None and loud > LEVEL_LUFS_MAX
    too_hot = peak is not None and peak > LEVEL_PEAK_MAX_DB
    if not (too_loud or too_hot):
        return None
    if loud is None:                                     # only the peak can have triggered
        return round(LEVEL_PEAK_CEILING_DB - peak, 2)
    gain = LEVEL_TARGET_LUFS - loud
    reference = peak
    if reference is None and isinstance(mix_peak_db, (int, float)) and not isinstance(mix_peak_db, bool):
        if not math.isfinite(mix_peak_db):
            return None
        reference = float(mix_peak_db)
    if reference is not None and reference + gain > LEVEL_PEAK_CEILING_DB:
        gain = LEVEL_PEAK_CEILING_DB - reference
    return round(gain, 2)


# -- reading the score ------------------------------------------------------------------------------------

_INLINE_FIELD = re.compile(r"\[[A-Za-z]:[^\]\n]*\]")
_CHORD_SYMBOL = re.compile(r'"[^"\n]*"')
_DECORATION = re.compile(r"![^!\n]*!")
_FIELD_LINE = re.compile(r"^[A-Za-z]:")
_MULTI_REST = re.compile(r"Z(\d*)")
_TEMPO = re.compile(r"\s*(?:(\d+)\s*/\s*(\d+)\s*=\s*)?(\d+(?:\.\d+)?)")
_METER = re.compile(r"(\d+)\s*/\s*(\d+)")


def _lines_of(abc: str) -> List[str]:
    return abc.replace("\r", "").split("\n")


def _key_line_index(lines: Sequence[str]) -> Optional[int]:
    """The index of the line that ends the header (the first one starting with `K:`), or None: not a score."""
    for i, line in enumerate(lines):
        if line.startswith("K:"):
            return i
    return None


def _first_field(lines: Sequence[str], letter: str) -> Optional[str]:
    prefix = letter + ":"
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _tempo_of(value: Optional[str]) -> Optional[float]:
    """Quarter notes a minute from a `Q:` value (`1/4=120`, `120`, `3/8=60`), or None."""
    if value is None:
        return None
    m = _TEMPO.match(value)
    if not m:
        return None
    beats = float(m.group(3))
    if beats <= 0:
        return None
    if m.group(1) is None:
        return beats
    num, den = int(m.group(1)), int(m.group(2))
    if num <= 0 or den <= 0:
        return None
    return beats * num * 4 / den


def _meter_of(value: Optional[str]) -> Optional[float]:
    """A bar as a fraction of a whole note from an `M:` value (`3/4` is 0.75, `C` and `C|` are 1.0), or None."""
    if value is None:
        return None
    text = value.strip()
    if text in ("C", "C|"):
        return 1.0
    m = _METER.fullmatch(text)
    if not m:
        return None
    num, den = int(m.group(1)), int(m.group(2))
    if num <= 0 or den <= 0:
        return None
    return num / den


def score_head(abc: str) -> Dict[str, Any]:
    """The score's tempo (`bpm`, quarter notes a minute), bar length (`meter`, of a whole note) and key token
    (`key`), from the first `Q:`, `M:` and `K:` line; each None when absent or unreadable."""
    lines = _lines_of(abc)
    key_value = _first_field(lines, "K")
    tokens = key_value.split() if key_value is not None else []
    return {"bpm": _tempo_of(_first_field(lines, "Q")), "meter": _meter_of(_first_field(lines, "M")),
            "key": tokens[0] if tokens else None}


def bar_seconds(abc: str) -> float:
    """Seconds in one bar of the score (3 decimals), or 0.0 when unknown or outside 0.3-8 s."""
    head = score_head(abc)
    if not head["bpm"] or not head["meter"]:
        return 0.0
    seconds = round(head["meter"] * 240.0 / head["bpm"], 3)
    return seconds if 0.3 <= seconds <= 8.0 else 0.0


def _voice_switch(trimmed: str) -> Tuple[bool, Optional[str]]:
    """Whether a trimmed line is a voice switch (`V: Vocal`), and the voice it names (None for a bare `V:`)."""
    if not trimmed.startswith("V:"):
        return False, None
    words = trimmed[2:].split()
    return True, (words[0] if words else None)


def _section_name(trimmed: str) -> Optional[str]:
    """The name of a `% name` section line, or None for any other line (`%%...` and a bare `%` included)."""
    return trimmed[2:].strip() if trimmed.startswith("% ") else None


def _is_music_line(trimmed: str) -> bool:
    """A line of bars: it ends with a bar line (`|`, or the closing `|]`) and is neither a comment nor a field."""
    if not trimmed or trimmed.startswith("%") or _FIELD_LINE.match(trimmed):
        return False
    return trimmed.endswith("|") or trimmed.endswith("|]")


def _bars_in(trimmed: str) -> int:
    """How many bars a music line holds: one per non-empty piece between bar lines, `Zn` counting n."""
    text = _INLINE_FIELD.sub("", _CHORD_SYMBOL.sub("", trimmed))
    if text.endswith("|]"):
        text = text[:-2]
    elif text.endswith("|"):
        text = text[:-1]
    total = 0
    for piece in text.split("|"):
        piece = piece.strip().strip(":[]").strip()
        if not piece:
            continue
        rest = _MULTI_REST.fullmatch(piece)
        total += (int(rest.group(1)) if rest.group(1) else 1) if rest else 1
    return total


def _has_notes(trimmed: str) -> bool:
    text = _INLINE_FIELD.sub("", _DECORATION.sub("", _CHORD_SYMBOL.sub("", trimmed)))
    return re.search(r"[A-Ga-g]", text) is not None


def score_parts(abc: str) -> Dict[str, Any]:
    """The score's sections timed by its Vocal voice.

    Returns `{"bpm", "parts"}`, each part `{name, label, start_s, end_s, sung}`: a run of `% name` lines with no
    other line between them opens one part (names joined by " · ", the label is the last one), music before the
    first such line makes a part named "Start" with an empty label. Only Vocal bars move the clock; a Vocal
    `M:` line changes the bar length from there on. `parts` is empty when the score has no `K:` line or no
    tempo."""
    head = score_head(abc)
    bpm = head["bpm"]
    lines = _lines_of(abc)
    end_of_header = _key_line_index(lines)
    if end_of_header is None or not bpm:
        return {"bpm": bpm, "parts": []}
    meter = head["meter"] or 1.0
    clock = 0.0
    voice: Optional[str] = None
    parts: List[Dict[str, Any]] = []
    names: List[str] = []           # the run of section names being read
    run_start = 0.0
    open_part: Optional[Dict[str, Any]] = None

    def open_run() -> None:
        nonlocal open_part, names
        if names:
            open_part = {"name": " · ".join(names), "label": names[-1], "start_s": run_start, "sung": False}
            parts.append(open_part)
            names = []

    for raw in lines[end_of_header + 1:]:
        trimmed = raw.strip()
        if not trimmed:
            continue
        name = _section_name(trimmed)
        if name is not None:
            if not names:
                run_start = clock
            names.append(name)
            continue
        open_run()
        if open_part is None:                          # music before any section line
            open_part = {"name": "Start", "label": "", "start_s": 0.0, "sung": False}
            parts.append(open_part)
        switch, named = _voice_switch(trimmed)
        if switch:
            voice = named
            continue
        if voice != "Vocal":
            continue
        if trimmed.startswith("M:"):
            meter = _meter_of(trimmed[2:]) or meter
            continue
        if _is_music_line(trimmed):
            clock += _bars_in(trimmed) * meter * 240.0 / bpm
            if _has_notes(trimmed):
                open_part["sung"] = True
    open_run()                                         # section lines at the very end
    out = []
    for i, part in enumerate(parts):
        end = parts[i + 1]["start_s"] if i + 1 < len(parts) else clock
        out.append({"name": part["name"], "label": part["label"], "start_s": round(part["start_s"], 3),
                    "end_s": round(end, 3), "sung": part["sung"]})
    return {"bpm": bpm, "parts": out}


# -- the Vocal voice alone ----------------------------------------------------------------------------------

_NOTE_HEAD = r"(?:\^\^|\^|__|_|=)?[A-Ga-g][,']*"
_LENGTH = r"\d*/*\d*"
_NOTE = re.compile(_NOTE_HEAD + r"(" + _LENGTH + r")-?")
_INS_TOKEN = re.compile(
    r'(?P<symbol>"[^"\n]*")'
    r"|(?P<deco>![^!\n]*!)"
    r"|(?P<field>\[[A-Za-z]:[^\]\n]*\])"
    r"|(?P<chord>\[(?P<inner>(?:\s*" + _NOTE_HEAD + _LENGTH + r"-?)+)\s*\](?P<outer>" + _LENGTH + r")-?)"
    r"|(?P<note>" + _NOTE_HEAD + r"(?P<length>" + _LENGTH + r")-?)")


def _length_value(text: str) -> Optional[Fraction]:
    """A written ABC length (`4`, `3/2`, `/`, `//`) as a multiple of the unit length, or None if unreadable."""
    m = re.fullmatch(r"(\d*)(/*)(\d*)", text)
    if not m:
        return None
    num = int(m.group(1)) if m.group(1) else 1
    slashes = len(m.group(2))
    if not slashes:
        return Fraction(num) if not m.group(3) else None
    den = int(m.group(3)) * 2 ** (slashes - 1) if m.group(3) else 2 ** slashes
    return Fraction(num, den) if den else None


def _length_text(value: Fraction) -> str:
    if value == 1:
        return ""
    if value.denominator == 1:
        return str(value.numerator)
    return (f"/{value.denominator}" if value.numerator == 1 else f"{value.numerator}/{value.denominator}")


def _chord_rest(inner: str, outer: str) -> str:
    """One rest as long as a chord: the chord's first note's length times the length written after it."""
    first = _NOTE.search(inner)
    own = first.group(1) if first else ""
    if not own or not outer:
        return "z" + (own or outer)
    a, b = _length_value(own), _length_value(outer)
    if a is None or b is None:
        return "z" + outer
    return "z" + _length_text(a * b)


def _silence(trimmed_line: str) -> str:
    def swap(m: re.Match) -> str:
        if m.group("symbol") is not None:
            return ""                                  # a silenced voice carries no harmony
        if m.group("deco") is not None or m.group("field") is not None:
            return m.group(0)
        if m.group("chord") is not None:
            return _chord_rest(m.group("inner"), m.group("outer"))
        return "z" + m.group("length")

    return _INS_TOKEN.sub(swap, trimmed_line)


def keep_vocal_voice(abc: str) -> str:
    """The score with every note of the Ins voice turned into a rest of the same written length; the Vocal
    voice, the header, the bar lines and the section lines stay. A text with no `K:` line comes back as it is."""
    if re.search(r"(?m)^K:", abc) is None:
        return abc
    lines = _lines_of(abc)
    end_of_header = _key_line_index(lines)
    if end_of_header is None:
        return abc
    out = lines[:end_of_header + 1]
    voice: Optional[str] = None
    for raw in lines[end_of_header + 1:]:
        trimmed = raw.strip()
        switch, named = _voice_switch(trimmed)
        if switch:
            voice = named
            out.append(raw)
        elif voice == "Ins" and _is_music_line(trimmed):
            lead = raw[:len(raw) - len(raw.lstrip())]
            tail = raw[len(raw.rstrip()):]
            out.append(lead + _silence(trimmed) + tail)
        else:
            out.append(raw)
    return "\n".join(out)


# -- section tags and kinds -------------------------------------------------------------------------------

# SheetSage2's section labels and the lyrics tag each one is sung under. A label not here is a Verse.
MATCH_TAGS: Dict[str, str] = {
    "intro": "Intro", "verse": "Verse", "pre-chorus": "Pre-Chorus", "chorus": "Chorus", "post-chorus": "Chorus",
    "bridge": "Bridge", "outro": "Outro", "rap": "Verse", "interlude": "Interlude", "instrumental": "Instrumental",
    "solo": "Instrumental", "fade-out": "Outro", "pre-outro": "Bridge", "loop": "Chorus",
    "intro and verse": "Verse", "pre-chorus and chorus": "Chorus", "verse and pre-chorus": "Verse",
    "theme": "Verse", "development": "Bridge", "variation": "Verse", "irregular": "Verse", "preshot": "Intro",
    "silence": "Intro",
}
LATE_SHARE = 0.75       # a block heard past this share of its section is late for it
MATCH_WINDOW = 4        # rows looked ahead for a sung line's time

_LABEL_NOISE = re.compile(r"[0-9#.:()]+")


def _clean_label(text: Optional[str]) -> str:
    """A label or tag without numbering and punctuation: `Chorus 2` -> `chorus`, `% Bridge (1)` -> `bridge`."""
    lowered = _LABEL_NOISE.sub(" ", (text or "").lower())
    return " ".join(lowered.split())


def _tag_for(label: Optional[str]) -> str:
    return MATCH_TAGS.get(_clean_label(label), "Verse")


def match_kind(label: Optional[str]) -> str:
    """The kind of a section label or lyrics tag, the key two of them are compared by: `Verse 2` -> `verse`,
    `Post-Chorus` -> `chorus`, an unknown label its cleaned text (`Weird Name` -> `weird name`)."""
    cleaned = _clean_label(label)
    return MATCH_TAGS[cleaned].lower() if cleaned in MATCH_TAGS else cleaned


# -- lyrics blocks ----------------------------------------------------------------------------------------

_TAG_LINE = re.compile(r"\[([^\]]*)\]")


def _tag_of(line: str) -> Optional[str]:
    """The inner text of a tag line (`[Verse]` -> `Verse`), or None for any other line."""
    m = _TAG_LINE.fullmatch(line.strip())
    if not m or not m.group(1).strip():
        return None
    return m.group(1).strip()


def _is_sung(line: str) -> bool:
    text = line.strip()
    return bool(text) and text[0] not in "(["


def _split_text(lines: Sequence[str]) -> List[str]:
    return "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _section_at(parts: Sequence[Mapping[str, Any]], seconds: float) -> int:
    """The index of the last part that has started by `seconds` (the first part when none has)."""
    found = 0
    for i, part in enumerate(parts):
        if part["start_s"] <= seconds:
            found = i
    return found


# -- pre-tagging untagged lyrics --------------------------------------------------------------------------


def pre_tag(lines: Sequence[str], times: Sequence[Any], abc: str) -> Optional[List[str]]:
    """Untagged lyrics cut into blocks at the score's section boundaries by their line times, each block under
    its section's tag; or None when they should not be pre-tagged (a tag line already, no named section in the
    score, times not one per line, or a sung line without a time).

    Half a bar of pickup is allowed. A line that is not sung (a `(cue)`) uses its time when it has one, and
    otherwise stays with the block around it (before the first timed line, with the first block)."""
    if any(_tag_of(line) is not None for line in lines):
        return None
    if times is None or len(times) != len(lines):
        return None
    parts = score_parts(abc)["parts"]
    if not any(p["label"] for p in parts):
        return None
    stamps = [_reading(t) for t in times]
    if not any(_is_sung(line) for line in lines):
        return None
    if any(_is_sung(line) and stamp is None for line, stamp in zip(lines, stamps)):
        return None
    pickup = (bar_seconds(abc) or 2.0) / 2
    out: List[str] = []
    waiting: List[str] = []            # lines before the first one with a time
    section: Optional[int] = None
    for line, stamp in zip(lines, stamps):
        if not line.strip() or stamp is None:
            (waiting if section is None else out).append(line)
            continue
        here = _section_at(parts, stamp + pickup)
        if here != section:
            if section is None:
                lead = 0
                while lead < len(waiting) and not waiting[lead].strip():
                    lead += 1
                out.extend(waiting[:lead])
                waiting = waiting[lead:]
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{_tag_for(parts[here]['label'])}]")
            out.extend(waiting)
            waiting = []
            section = here
        out.append(line)
    out.extend(waiting)
    return out


# -- Match sections ---------------------------------------------------------------------------------------


def _blocks_of(lines: Sequence[str]) -> List[Dict[str, Any]]:
    """The lyrics' blocks: `tag` (None before the first tag line), `body`, `lines` (sung lines in it) and `first`
    (the index of its first sung line among all sung lines). An untagged block with a blank body is left out."""
    groups: List[Tuple[Optional[str], List[str]]] = [(None, [])]
    for line in _split_text(lines):
        tag = _tag_of(line)
        if tag is not None:
            groups.append((tag, []))
        else:
            groups[-1][1].append(line)
    blocks: List[Dict[str, Any]] = []
    sung_so_far = 0
    for tag, body_lines in groups:
        start = 0
        while start < len(body_lines) and not body_lines[start].strip():
            start += 1
        body = "\n".join(body_lines[start:]).rstrip()
        if tag is None and not body.strip():
            continue
        count = sum(1 for line in body_lines if _is_sung(line))
        blocks.append({"tag": tag, "body": body, "lines": count, "first": sung_so_far if count else None})
        sung_so_far += count
    return blocks


def _line_times(blocks: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> List[Optional[float]]:
    """The time each sung line is sung at, found in `rows` within `MATCH_WINDOW` rows of the last match."""
    sung = [line.strip() for block in blocks for line in block["body"].split("\n") if _is_sung(line)]
    times: List[Optional[float]] = []
    cursor = 0
    for text in sung:
        hit = None
        for j in range(cursor, min(cursor + MATCH_WINDOW, len(rows))):
            row = rows[j]
            if isinstance(row, Mapping) and str(row.get("text") or "").strip() == text:
                hit = j
                break
        if hit is None:
            times.append(None)
            continue
        row = rows[hit]
        cursor = hit + 1
        times.append(None if row.get("guessed") else _reading(row.get("start")))
    return times


def _assign_sections(heard: Sequence[Optional[float]], parts: Sequence[Mapping[str, Any]],
                     bar: float) -> List[Optional[int]]:
    """The section each block belongs to: where it is heard, a bar of pickup allowed; a block heard late in a
    section belongs there only when it continues the block before; never back to an earlier section."""
    out: List[Optional[int]] = []
    last: Optional[int] = None
    for h in heard:
        if h is None:
            out.append(None)
            continue
        candidate = _section_at(parts, h + bar)
        part = parts[candidate]
        share = (h - part["start_s"]) / max(1e-6, part["end_s"] - part["start_s"])
        if share > LATE_SHARE and candidate != last:
            out.append(None)
            continue
        if last is not None and candidate < last:
            out.append(None)
            continue
        out.append(candidate)
        last = candidate
    return out


def match_sections(abc: str, lines: Sequence[str], rows: Sequence[Mapping[str, Any]], *,
                   copy_bodies: bool = True) -> Dict[str, Any]:
    """The lyrics' blocks retagged with the score section each is sung in, and tags added for the sections the
    lyrics do not reach; the words never change.

    `rows` are the sung lines of the original lyrics with their start in the source (`{"text", "start"}`, a
    truthy `guessed` makes a row untimed). An unsung section the lyrics skip gets its tag alone; a sung one gets
    the tag and, with `copy_bodies`, the body of the last block placed in a section of the same kind (reported
    in `filled` either way). Returns `{"lyrics", "blocks", "filled", "unchanged"}`.

    Raises ValueError, with a text meant for the person, when the score cannot be split into sections."""
    found = score_parts(abc)
    parts = found["parts"]
    if found["bpm"] is None and _key_line_index(_lines_of(abc)) is not None and "\n% " in "\n" + abc:
        raise ValueError("the score has no tempo (Q:), so its sections have no times")
    if not any(p["label"] for p in parts):
        raise ValueError("the score has no named sections")
    blocks = _blocks_of(lines)
    times = _line_times(blocks, rows)
    heard = [times[b["first"]] if b["first"] is not None else None for b in blocks]
    bar = bar_seconds(abc) or 2.0
    sections = _assign_sections(heard, parts, bar)

    tagged = [b["tag"] for b in blocks if b["tag"]]
    lower = sum(1 for t in tagged if t[0].islower()) * 2 > len(tagged)

    def written_tag(index: int) -> str:
        tag = _tag_for(parts[index]["label"])
        return tag.lower() if lower else tag

    chunks: List[str] = []
    filled: List[Dict[str, Any]] = []
    report: List[Dict[str, Any]] = []
    done: set = set()                           # sections already written
    by_kind: Dict[str, int] = {}                # section kind -> index of the last block placed in one
    state = {"current": None, "placed": False, "unsure": False}

    def fill(up_to: int, instrumental_only: bool) -> None:
        start = 0 if state["current"] is None else state["current"] + 1
        for s in range(start, up_to):
            if s in done:
                continue
            part = parts[s]
            source = None
            if part["sung"]:
                if instrumental_only or state["unsure"]:
                    continue
                source = by_kind.get(match_kind(part["label"]))
                if source is None:
                    continue
            tag = written_tag(s)
            if source is not None and copy_bodies:
                chunks.append(f"[{tag}]\n{blocks[source]['body']}")
            else:
                chunks.append(f"[{tag}]")
            filled.append({"section": s + 1, "tag": tag, "copied_from": None if source is None else source + 1})
            done.add(s)
            state["placed"] = False

    sung_sections = [i for i, p in enumerate(parts) if p["sung"]]
    if sung_sections:
        fill(sung_sections[0], True)

    for index, block in enumerate(blocks):
        entry: Dict[str, Any] = {"index": index + 1, "tag": block["tag"]}
        h, section = heard[index], sections[index]
        current = state["current"]
        if not block["body"].strip():
            entry.update(new_tag=None, section=None, at=None, status="dropped")
        elif section is not None and section == current and state["placed"]:
            chunks[-1] += "\n\n" + block["body"]
            entry.update(new_tag=None, section=section + 1, at=h, status="merged")
        elif section is None or (current is not None and section <= current):
            if h is not None:
                fill(_section_at(parts, h), True)
            chunks.append(f"[{block['tag']}]\n{block['body']}" if block["tag"] else block["body"])
            entry.update(new_tag=block["tag"], section=None, at=h, status="unsure")
            state["unsure"], state["placed"] = True, False
        else:
            fill(section, False)
            label = parts[section]["label"]
            own = block["tag"]
            if own and match_kind(own) in (match_kind(label), match_kind(_tag_for(label))):
                new_tag, status = own, "kept"
            else:
                new_tag, status = written_tag(section), "renamed"
            chunks.append(f"[{new_tag}]\n{block['body']}")
            entry.update(new_tag=new_tag, section=section + 1, at=h, status=status)
            by_kind[match_kind(label)] = index
            done.add(section)
            state.update(current=section, unsure=False, placed=True)
        report.append(entry)

    fill(len(parts), False)
    lyrics = "\n\n".join(chunks)
    original = "\n".join(_split_text(lines)).strip()
    return {"lyrics": lyrics, "blocks": report, "filled": filled, "unchanged": lyrics == original}


_STATUSES = ("kept", "renamed", "merged", "unsure", "dropped")


def counts_of(report: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """How many blocks of a `match_sections` report have each status; the five known ones always present."""
    counts = {status: 0 for status in _STATUSES}
    for item in report:
        status = item.get("status") if isinstance(item, Mapping) else None
        if status is not None:
            counts[status] = counts.get(status, 0) + 1
    return counts


# -- the style caption --------------------------------------------------------------------------------------

_BPM_MENTION = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*bpm\b", re.IGNORECASE)
_KEY_MENTION = re.compile(r"(?<![\w#])([A-G])([#b♯♭]?)\s*((?i:major|minor|maj|min))\b")
_SCORE_KEY = re.compile(r"([A-G])([#b]?)(.*)")
_MAJOR_MODES = {"", "maj", "major", "ion", "ionian"}
_MINOR_MODES = {"m", "min", "minor", "aeo", "aeolian"}


def _score_key(token: Optional[str]) -> Optional[str]:
    """The score's key token in the form a style mention is normalised to (`Cmaj` -> `C`, `Amin` -> `Am`), the
    token itself for another mode (`Ddor`), or None when it names no key (`none`, `HP`)."""
    if not token:
        return None
    m = _SCORE_KEY.fullmatch(token)
    if not m:
        return None
    mode = m.group(3).lower()
    if mode in _MAJOR_MODES:
        return m.group(1) + m.group(2)
    if mode in _MINOR_MODES:
        return m.group(1) + m.group(2) + "m"
    return token


def _tidy_commas(style: str) -> str:
    text = re.sub(r",(?:\s*,)+", ", ", style)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip().strip(",").strip()


def agree_style(style: str, abc: str) -> Tuple[str, List[str]]:
    """The style caption made to agree with the score's tempo and key, and each change as a sentence.

    A score's tempo is added when the style names none and replaces one that differs by 1 BPM or more; a key the
    style names (`A minor`, `F#min`, upper-case letter only) is dropped when it is not the score's key."""
    head = score_head(abc)
    text = style if isinstance(style, str) else ""
    changes: List[str] = []
    if head["bpm"]:
        q = int(round(head["bpm"]))
        mention = _BPM_MENTION.search(text)
        if mention is None:
            base = re.sub(r"[\s,]+$", "", text)
            text = f"{base}, {q} BPM" if base.strip() else f"{q} BPM"
            changes.append(f"added the score's tempo {q} BPM")
        elif abs(float(mention.group(1)) - q) >= 1:
            text = text[:mention.start()] + f"{q} BPM" + text[mention.end():]
            changes.append(f"tempo {mention.group(1)} BPM set to the score's {q} BPM")
    key = _score_key(head["key"])
    if key is not None:
        kept: List[str] = []
        cursor = 0
        dropped = False
        for m in _KEY_MENTION.finditer(text):
            accidental = {"♯": "#", "♭": "b"}.get(m.group(2), m.group(2))
            minor = m.group(3).lower() in ("min", "minor")
            if m.group(1) + accidental + ("m" if minor else "") == key:
                continue
            kept.append(text[cursor:m.start()])
            cursor = m.end()
            dropped = True
            changes.append(f"key {m.group(0)} dropped: the score is in {head['key']}")
        if dropped:
            kept.append(text[cursor:])
            text = _tidy_commas("".join(kept))
    return text, changes
