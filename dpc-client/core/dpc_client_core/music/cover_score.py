# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Based on Ruach Studio by Viktor Zhuromskyy · https://github.com/igrbible/Ruach_Studio · GNU AGPL v3 or later
#
# Ported from Ruach Studio (Copyright 2026 Viktor Zhuromskyy), checkout 200fe5ad, build/tools/console/app.js:
# the recording's level before its score is read (lines 11066-11120), scoreParts (7034-7055), barSeconds
# (10181-10187) and Match sections (11199-11305). MODIFIED version, not the original Ruach Studio: ported to
# Python and changed by CC (Claude Code, for DPC) on 2026-11-26 (line times from the stored synced lyrics, not
# Whisper; untagged lyrics cut at section boundaries first, `pre_tag`; the native ABC read without HeresyAbc;
# applied by the cover job, not proposed in a dialog). Ruach Studio's section 7 additional terms apply
# (LICENSE-ADDITIONAL-TERMS.md there): keep the attribution, mark changes, no rights to its name or logo.
"""What a YuE2 cover does to the source's score and the lyrics before `/synth` (music/similar.py).

**The level** (`transcriber_gain_db`). A recording louder than the transcriber hears cleanly (above
-14 LUFS integrated, or a peak above -1 dBFS) is brought down by one gain to -16 LUFS, its peak held at
-1.5 dBFS; no limiter, so what clipping cut off stays cut. Within bounds it goes as it is. With no
loudness reading, the peak half of the rule still applies (a peak above -1 dBFS comes down to -1.5).

**The vocal line** (`keep_vocal_voice`): Ruach's "Vocal melody only" transcription keeps the score's
Vocal voice. Here the Ins voice's notes become rests of the same length instead of the voice being
removed, the way YuE2 Studio's `abc_tools.strip_chords(keep_voice=...)` silences a voice, so the score
keeps the native two-voice form yue2.cpp writes and reads (`src/notation.h` writes both voices).

**The parts** (`score_parts`): each run of `% name` lines opens a part; its start is the Vocal voice's
time there (bars counted by the meter, at the score's `Q:`), measured from the score's first bar.

**Match sections** (`match_sections`): each block of the lyrics takes the tag of the score section it is
sung in, by where its first line is sung (a bar's pickup counted in); a block heard late in a section
(after 75 %) is unsure unless it continues the section before; a section the voice is silent in becomes
an instrumental tag; a sung section the lyrics skip gets the last block of its kind again (a chorus
written once and sung again). Words never change. Blocks sung where the line times do not say stay as
they were ("unsure"). Untagged lyrics are cut into blocks at the section boundaries first (`pre_tag`).

**The style against the score** (`agree_style`): the yue2-style rule of Ruach's `heresy/skills/
yue2-style/SKILL.md` (lines 64-65, 126): the tempo tag takes the score's `Q:`, a key stays only if it
agrees with `K:`, and every change is reported.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# -- the level (app.js:11066-11120, HERESY 2651) -------------------------------------------------------

LEVEL_LUFS_MAX = -14.0
LEVEL_PEAK_MAX_DB = -1.0
LEVEL_TARGET_LUFS = -16.0
LEVEL_PEAK_CEILING_DB = -1.5


def transcriber_gain_db(lufs: Optional[float], peak_db: Optional[float],
                        mix_peak_db: Optional[float] = None) -> Optional[float]:
    """The one gain (dB) that brings a recording too loud for the transcriber to -16 LUFS with its peak
    at -1.5 dBFS at most, or None when it goes as it is (within bounds, or nothing known). A reading with
    no loudness is judged on its peak alone: over -1 dBFS, the gain that brings the peak to -1.5 dBFS (the
    rule's peak half is not lost on a partial reading; follow-up 18, Zcode #195). A non-finite peak is no peak.
    With a loudness and no peak, `mix_peak_db` (the measured sample peak of the very mix the gain is applied to)
    stands in as the ceiling's reference: the gain never lifts it over -1.5 dBFS, and a mix with no peak at all
    (`-inf`, digital silence) gets no gain (follow-up 20, Zcode #201). Without `mix_peak_db` the gain follows
    the loudness alone."""
    lufs_ok = isinstance(lufs, (int, float)) and not isinstance(lufs, bool) and math.isfinite(lufs)
    peak_ok = isinstance(peak_db, (int, float)) and not isinstance(peak_db, bool) and math.isfinite(peak_db)
    if not (lufs_ok and lufs > LEVEL_LUFS_MAX or peak_ok and peak_db > LEVEL_PEAK_MAX_DB):
        return None
    if not lufs_ok:
        return round(LEVEL_PEAK_CEILING_DB - peak_db, 2)
    gain = LEVEL_TARGET_LUFS - lufs
    if not peak_ok and isinstance(mix_peak_db, (int, float)) and not isinstance(mix_peak_db, bool):
        if not math.isfinite(mix_peak_db):
            return None
        peak_db, peak_ok = float(mix_peak_db), True
    if peak_ok and peak_db + gain > LEVEL_PEAK_CEILING_DB:
        gain = LEVEL_PEAK_CEILING_DB - peak_db
    return round(gain, 2)


# -- the native dialect ----------------------------------------------------------------------------

_NOTE = re.compile(r"(?P<acc>\^\^|__|\^|_|=)?(?P<note>[A-Ga-g])(?P<oct>[,']*)(?P<duration>[0-9]*)(?P<tie>-?)")
_QUOTED = re.compile(r'"[^"\n]*"')
_INLINE = re.compile(r"\[[A-Za-z]:[^\]\n]*\]")


def _header_end(lines: Sequence[str]) -> int:
    """The index after the header's `K:` line (the first one), or -1."""
    for i, line in enumerate(lines):
        if line.startswith("K:"):
            return i + 1
    return -1


def keep_vocal_voice(abc: str) -> str:
    """The score with the Ins voice's notes turned into rests of the same length (the Vocal voice, the chord
    symbols, the bars and the sections as they were). A score without a `K:` line is returned unchanged."""
    lines = abc.replace("\r", "").split("\n")
    start = _header_end(lines)
    if start < 0:
        return abc
    voice = None
    out = lines[:start]
    for line in lines[start:]:
        s = line.strip()
        if s.startswith("V:"):
            voice = s[2:].strip().split()[0] if s[2:].strip() else None
        elif voice == "Ins" and s.endswith("|") and not s.startswith("%"):
            # Inline fields (`[K:Am]`) stay as they are: only the notes between them become rests.
            pieces = re.split(r"(\[[A-Za-z]:[^\]\n]*\])", _QUOTED.sub("", line))
            line = "".join(p if _INLINE.fullmatch(p) else _NOTE.sub(lambda m: "z" + m.group("duration"), p)
                           for p in pieces)
        out.append(line)
    return "\n".join(out)


def _meter(text: str) -> Optional[float]:
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    return 1.0 if text.strip().startswith("C") else None


def score_head(abc: str) -> Dict[str, Any]:
    """`{bpm, meter, key}` of the score's header (`Q:1/4=N`, `M:n/d`, `K:`), each None when absent."""
    q = re.search(r"^Q:\s*(?:(\d+)\s*/\s*(\d+)\s*=\s*)?(\d+(?:\.\d+)?)", abc, re.M)
    m = re.search(r"^M:\s*(C\|?|\d+\s*/\s*\d+)", abc, re.M)
    k = re.search(r"^K:\s*(\S+)", abc, re.M)
    bpm = None
    if q and float(q.group(3)) > 0:
        unit = int(q.group(1)) / int(q.group(2)) if q.group(1) and int(q.group(2)) else 0.25
        bpm = float(q.group(3)) * unit / 0.25
    return {"bpm": bpm, "meter": _meter(m.group(1)) if m else None, "key": k.group(1) if k else None}


def bar_seconds(abc: str) -> float:
    """A bar's seconds by the score's head (M:, Q:), or 0 when it cannot be read (barSeconds)."""
    head = score_head(abc)
    if not head["bpm"] or head["meter"] is None:
        return 0.0
    sec = head["meter"] * 4 * 60 / head["bpm"]
    return round(sec, 3) if 0.3 <= sec <= 8 else 0.0


def _bars_in(line: str) -> int:
    count = 0
    for bar in line.strip()[:-1].split("|"):
        z = re.fullmatch(r"\s*Z(\d+)?\s*", bar)
        count += int(z.group(1) or 1) if z else (1 if bar.strip() else 0)
    return count


def _sung(text: str) -> bool:
    return bool(re.search(r"[A-Ga-g]", _INLINE.sub("", re.sub(r"![^!]*!", "", _QUOTED.sub("", text)))))


def score_parts(abc: str) -> Dict[str, Any]:
    """`{bpm, parts: [{name, label, start_s, end_s, sung}]}`: a part from each run of `% name` lines (names of
    one run joined by " · ", `label` the last), its start the Vocal voice's time there; the last runs to the
    score's end. `sung`: the Vocal voice has a note in the part. Empty parts when the score has no tempo."""
    head = score_head(abc)
    lines = abc.replace("\r", "").split("\n")
    start = _header_end(lines)
    if start < 0 or not head["bpm"]:
        return {"bpm": head["bpm"], "parts": []}
    sec_per_quarter = 60.0 / head["bpm"]
    meter = head["meter"] or 1.0
    t = 0.0
    voice = None
    parts: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("% "):
            if cur is None or cur["music"]:
                cur = {"names": [], "start_s": t, "music": False, "sung": False}
                parts.append(cur)
            cur["names"].append(s[2:].strip())
            continue
        if cur is None:
            cur = {"names": [], "start_s": 0.0, "music": False, "sung": False}
            parts.append(cur)
        cur["music"] = True
        if s.startswith("V:"):
            voice = s[2:].strip().split()[0] if s[2:].strip() else None
            continue
        if s.startswith("M:") and voice == "Vocal":
            meter = _meter(s[2:]) or meter
            continue
        if voice == "Vocal" and s.endswith("|"):
            t += _bars_in(s) * meter * 4 * sec_per_quarter
            if _sung(s):
                cur["sung"] = True
    out = []
    for j, p in enumerate(parts):
        name = " · ".join(p["names"]) or "Start"
        out.append({"name": name, "label": "" if name == "Start" else name.split(" · ")[-1],
                    "start_s": round(p["start_s"], 3),
                    "end_s": round(parts[j + 1]["start_s"] if j + 1 < len(parts) else t, 3), "sung": p["sung"]})
    return {"bpm": head["bpm"], "parts": out}


# -- match sections (app.js:11199-11305, HERESY 1719) --------------------------------------------------

# SheetSage2's labels to lyrics tags; anything else a verse.
MATCH_TAGS = {
    "intro": "Intro", "verse": "Verse", "pre-chorus": "Pre-Chorus", "chorus": "Chorus", "post-chorus": "Chorus",
    "bridge": "Bridge", "outro": "Outro", "rap": "Verse", "interlude": "Interlude", "instrumental": "Instrumental",
    "solo": "Instrumental", "fade-out": "Outro", "pre-outro": "Bridge", "loop": "Chorus", "intro and verse": "Verse",
    "pre-chorus and chorus": "Chorus", "verse and pre-chorus": "Verse", "theme": "Verse", "development": "Bridge",
    "variation": "Verse", "irregular": "Verse", "preshot": "Intro", "silence": "Intro",
}
LATE_SHARE = 0.75          # a block first heard after this share of a section is unsure there
MATCH_WINDOW = 4           # a sung line is looked for among this many timed rows ahead
_TAG_LINE = re.compile(r"^\[([^\]]+)\]$")


def match_kind(label: Any) -> str:
    """A section's kind: «Verse 2» a verse, SheetSage2's «intro and verse» too; an unknown name stays itself."""
    k = re.sub(r"\s+", " ", re.sub(r"[\d#.:()]+", " ", str(label or "").lower())).strip()
    return MATCH_TAGS[k].lower() if k in MATCH_TAGS else k


def match_blocks(lines: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The lyrics' blocks `[{tag, body, first, lines}]` (`first`: the index of its first sung line) and the
    sung lines (not empty, not a `(cue)` or a `[tag]`)."""
    blocks: List[Dict[str, Any]] = []
    sung: List[str] = []
    for line in lines:
        t = line.strip()
        m = _TAG_LINE.match(t)
        if m and m.group(1).strip():
            blocks.append({"tag": m.group(1).strip(), "body": "", "first": len(sung), "lines": 0})
            continue
        if not blocks:
            blocks.append({"tag": None, "body": "", "first": len(sung), "lines": 0})
        b = blocks[-1]
        if b["body"] or t:
            b["body"] += ("\n" if b["body"] else "") + line
        if t and t[0] not in "([":
            b["lines"] += 1
            sung.append(t)
    for b in blocks:
        b["body"] = b["body"].rstrip()
    return [b for b in blocks if b["tag"] or b["body"].strip()], sung


def _spans(parts: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [{"tag": MATCH_TAGS.get(p["label"].lower(), "Verse"), "label": p["label"], "start": p["start_s"],
             "end": p["end_s"], "sung": p["sung"]} for p in parts]


def _containing(spans: Sequence[Mapping[str, Any]], at: float) -> int:
    k = 0
    for i, x in enumerate(spans):
        if x["start"] <= at:
            k = i
    return k


def pre_tag(lines: Sequence[str], times: Sequence[Optional[float]], abc: str) -> Optional[List[str]]:
    """DPC's addition: lyrics with no `[tag]` line at all, cut into blocks where their timed lines cross a
    section boundary of the score, each block tagged with its section's tag (as Match sections would name
    it). None when the lyrics already have tags, a line has no time, or the score has no named sections."""
    if any(_TAG_LINE.match(x.strip()) for x in lines):
        return None
    spans = _spans(score_parts(abc)["parts"])
    if not any(x["label"] for x in spans) or len(times) != len(lines):
        return None
    # Half a bar of pickup per line (Match sections counts a whole bar for a block's first line only).
    pickup = (bar_seconds(abc) or 2.0) / 2
    out: List[str] = []
    current = None
    for line, t in zip(lines, times):
        if line.strip():
            if t is None:
                return None
            i = _containing(spans, t + pickup)
            if i != current:
                if out and out[-1].strip():
                    out.append("")
                out.append(f"[{spans[i]['tag']}]")
                current = i
        out.append(line)
    return out


def match_sections(abc: str, lines: Sequence[str], rows: Sequence[Mapping[str, Any]], *,
                   copy_bodies: bool = True) -> Dict[str, Any]:
    """`{lyrics, blocks: [{index, tag, new_tag, section, at, status}], filled: [{section, tag, copied_from}],
    unchanged}` of the lyrics `lines` against the score's named sections; `rows` are the timed sung lines in
    the lyrics' order (`{text, start}`, a `start` of None for a line with no time). `ValueError` when the score
    has no named sections. A sung section the lyrics do not reach is filled with the last block of its kind (a
    whole chorus, as Ruach does); with `copy_bodies` False it gets its tag alone, and `filled` still names the
    block it would have copied (the run's fallback when the copies do not fit, `similar._fit_without_copies`)."""
    spans = _spans(score_parts(abc)["parts"])
    bar = bar_seconds(abc) or 2.0
    if not any(x["label"] for x in spans):
        raise ValueError("the score has no named sections")
    blocks, sung = match_blocks(lines)
    at: List[Optional[float]] = []
    j = 0
    for line in sung:
        k = j
        while k < len(rows) and k < j + MATCH_WINDOW and str(rows[k].get("text") or "").strip() != line:
            k += 1
        if k < len(rows) and k < j + MATCH_WINDOW:
            start = rows[k].get("start")
            ok = isinstance(start, (int, float)) and math.isfinite(start) and not rows[k].get("guessed")
            at.append(float(start) if ok else None)
            j = k + 1
        else:
            at.append(None)
    tags = [b["tag"] for b in blocks if b["tag"]]
    lower = sum(1 for t in tags if t[0] != t[0].upper() and t[0] == t[0].lower()) * 2 > len(tags)

    def tag_of(i: int) -> str:
        return spans[i]["tag"].lower() if lower else spans[i]["tag"]

    last = None
    placed = []
    for b in blocks:
        heard = at[b["first"]] if b["lines"] > 0 and b["first"] < len(at) and at[b["first"]] is not None else None
        section = None
        if heard is not None:
            i = _containing(spans, heard + bar)
            late = (heard - spans[i]["start"]) / max(1e-6, spans[i]["end"] - spans[i]["start"]) > LATE_SHARE
            section = i if (i == last or not late) else None
        if section is not None and last is not None and section < last:
            section = None
        if section is not None:
            last = section
        placed.append({"heard": heard, "section": section})

    out: List[str] = []
    report: List[Dict[str, Any]] = []
    filled: List[Dict[str, Any]] = []
    state = {"current": None, "unsure": False, "placed": False}
    last_of_kind: Dict[str, int] = {}
    emitted: set = set()

    def fill(up_to: int, instrumental_only: bool) -> None:
        begin = 0 if state["current"] is None else state["current"] + 1
        for i in range(begin, up_to):
            if i in emitted or (instrumental_only and spans[i]["sung"]):
                continue
            src = last_of_kind.get(match_kind(spans[i]["label"])) if spans[i]["sung"] and not state["unsure"] else None
            if spans[i]["sung"] and src is None:
                continue
            body = blocks[src]["body"] if src is not None and copy_bodies else ""
            out.append(f"[{tag_of(i)}]\n{body}" if body else f"[{tag_of(i)}]")
            emitted.add(i)
            state["placed"] = False
            filled.append({"section": i + 1, "tag": tag_of(i), "copied_from": src + 1 if src is not None else None})

    first_sung = next((i for i, x in enumerate(spans) if x["sung"]), -1)
    if first_sung >= 0:
        fill(first_sung, True)
    for n, b in enumerate(blocks):
        pl = placed[n]
        if not b["body"].strip():
            report.append({"index": n + 1, "tag": b["tag"], "new_tag": None, "section": None, "at": None,
                           "status": "dropped"})
            continue
        mergeable = pl["section"] is not None and pl["section"] == state["current"] and state["placed"]
        if pl["section"] is None or (pl["section"] == state["current"] and not mergeable):
            if pl["heard"] is not None:
                fill(_containing(spans, pl["heard"]), True)
            out.append(f"[{b['tag']}]\n{b['body']}" if b["tag"] else b["body"])
            report.append({"index": n + 1, "tag": b["tag"], "new_tag": b["tag"], "section": None, "at": pl["heard"],
                           "status": "unsure"})
            state["unsure"] = True
            state["placed"] = False
            continue
        i = pl["section"]
        if mergeable:
            out[-1] += "\n\n" + b["body"]
            report.append({"index": n + 1, "tag": b["tag"], "new_tag": None, "section": i + 1, "at": pl["heard"],
                           "status": "merged"})
            continue
        fill(i, False)
        keep = bool(b["tag"]) and (match_kind(b["tag"]) == match_kind(spans[i]["label"])
                                   or match_kind(b["tag"]) == match_kind(spans[i]["tag"]))
        new_tag = b["tag"] if keep else tag_of(i)
        out.append(f"[{new_tag}]\n{b['body']}")
        report.append({"index": n + 1, "tag": b["tag"], "new_tag": new_tag, "section": i + 1, "at": pl["heard"],
                       "status": "kept" if keep else "renamed"})
        last_of_kind[match_kind(spans[i]["label"])] = n
        state["current"] = i
        state["unsure"] = False
        state["placed"] = True
    fill(len(spans), False)
    result = "\n\n".join(out)
    return {"lyrics": result, "blocks": report, "filled": filled,
            "unchanged": result == "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n").strip()}


def counts_of(report: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """How many blocks were kept, renamed, merged, unsure or dropped."""
    out = {k: 0 for k in ("kept", "renamed", "merged", "unsure", "dropped")}
    for b in report:
        out[b["status"]] = out.get(b["status"], 0) + 1
    return out


# -- the style against the score (heresy/skills/yue2-style/SKILL.md:64-65, 126) ----------------------

_BPM = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*bpm\b", re.I)
_KEY = re.compile(r"(?<![\w#])([A-G])(#|b|♯|♭)?\s*((?i:major|minor|maj|min))\b")


def _key_word(letter: str, acc: Optional[str], mode: str) -> str:
    acc = {"♯": "#", "♭": "b"}.get(acc or "", acc or "")
    return letter.upper() + acc + ("m" if mode.lower().startswith("min") else "")


def agree_style(style: str, abc: str) -> Tuple[str, List[str]]:
    """The style with its tempo set to the score's `Q:` (added when it names none) and a key that disagrees
    with `K:` dropped; and the changes, in words. A score without a tempo leaves the style as it is."""
    head = score_head(abc)
    changes: List[str] = []
    out = style
    if head["bpm"]:
        q = int(round(head["bpm"]))
        found = _BPM.search(out)
        if found is None:
            out = f"{out.rstrip().rstrip(',')}, {q} BPM"
            changes.append(f"added the score's tempo {q} BPM")
        elif abs(float(found.group(1)) - q) >= 1:
            out = out[:found.start()] + f"{q} BPM" + out[found.end():]
            changes.append(f"tempo {found.group(1)} BPM set to the score's {q} BPM")
    key = head["key"]
    if key:
        def drop(m: "re.Match[str]") -> str:
            said = _key_word(m.group(1), m.group(2), m.group(3))
            if said == key:
                return m.group(0)
            changes.append(f"key {m.group(0)} dropped: the score is in {key}")
            return ""
        out = _KEY.sub(drop, out)
        out = re.sub(r"\s*,\s*(,\s*)+", ", ", out).strip().strip(",").strip()
    return out, changes
