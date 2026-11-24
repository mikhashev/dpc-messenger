"""Clean-up of a Whisper transcript before it becomes lyrics (music-player item 2).

Whisper on a song invents text over music without voice (subtitle credits, "thanks for
watching", note symbols), loops a phrase, and returns one segment for several sung lines.
`clean_transcript` takes the provider's result and gives back the same shape with

1. **hallucinations dropped** (`drop_reason`), and
2. **long segments split** (`split_segment`), at word timestamps when the backend gave them,
   proportionally to the characters when it did not.

The rules are CC's proposal (backlog card TRANSCRIBED-LYRICS-CARRY-WHISPER-HALLUCINATIONS-
AND-MERGE-LONG-SEGMENTS-INTO-ONE-LINE; no ADR or SPEC text fixes the numbers). They are
text and timing rules plus whatever confidence figures the backend reported: the HF
pipeline reports none, mlx-whisper reports `no_speech_prob`, `avg_logprob` and
`compression_ratio` per segment. Nothing here loads a model or does I/O.

**Never silently empty a song.** If every segment would be dropped, the raw segments are
kept and the report says `kept_raw`. The raw result is also stored beside the lyrics by the
caller (`MusicLyrics`), so a filter mistake is recoverable.

Bump `FILTER_VERSION` when a rule or a threshold changes: the stored lyrics record it.
"""

from __future__ import annotations

import math
import re
import zlib
from typing import Any, Dict, List, Optional, Tuple

FILTER_VERSION = 2

# -- thresholds ------------------------------------------------------------------------------
# Whisper's own rule for "this segment is silence": probably no speech AND an unconfident decode.
NO_SPEECH_PROB_MAX = 0.6
AVG_LOGPROB_MIN = -1.0
# Whisper's own compression-ratio gate for a looping decode; zlib's ratio is only meaningful on
# a text of some length, so shorter texts are judged by the repeated-n-gram rule instead.
COMPRESSION_RATIO_MAX = 2.4
COMPRESSION_MIN_CHARS = 120
# The ratio alone never drops a segment (follow-up 11B, Zcode #27): a verse sung three times in one
# segment compresses to about 3.1 and is honest. The ratio only counts when a looped group of up to
# CONFIRM_NGRAM_MAX_SIZE words confirms it: repeated back to back CONFIRM_NGRAM_RUN times or more,
# covering CONFIRM_NGRAM_COVERAGE of the text. Half of the stand-alone bar below (6 against 12,
# 0.5 against 0.8) because the ratio has already agreed, and a group is allowed to be longer (8
# words against 4) so a looped phrase is caught as well as a looped word. A verse is longer than 8
# words or comes back fewer than 6 times, so a sung verse or chorus never reaches it. Unchecked
# against real singing, like every number here.
CONFIRM_NGRAM_MAX_SIZE = 8
CONFIRM_NGRAM_RUN = 6
CONFIRM_NGRAM_COVERAGE = 0.5
# The same 1-4 word group repeated back to back this many times, covering most of the segment.
REPEAT_NGRAM_RUN = 12
REPEAT_NGRAM_COVERAGE = 0.8
# The same text as this many segments in a row is a hallucination only where the TIMES say so
# (`_time_evidence`): the first of the run always stays, a later one goes only with evidence.
REPEAT_SEGMENT_RUN = 4
# The fastest a segment can plausibly be sung or rapped, in characters per second. 30 is five words a
# second of five letters and a space, about seven syllables: faster than a fast verse, so a segment
# above it holds more text than its time can carry. No figure in this code or any measured song fixes
# it; the code's own MAX_LINE_CHARS / MAX_LINE_S (80 / 7, about 11) is the density it expects of one
# displayed line, and 30 is about 2.6 times that, chosen so an honest fast verse is not caught.
MAX_SUNG_CHARS_PER_S = 30.0
# How many dropped lines the report lists (for the UI's "what was filtered"); the count is complete.
MAX_REPORTED_DROPS = 20
MAX_REPORTED_TEXT = 160

# A line longer than this (seconds or characters) is split. About what a two-line karaoke
# display holds at the player's font size; the UI has no stated limit to follow.
MAX_LINE_S = 7.0
MAX_LINE_CHARS = 80
# A split never leaves a piece shorter than this many words.
MIN_PIECE_WORDS = 2
# A silence between two words at least this long is a good place to break a line, and a
# silence this long between words starts a new segment when words arrive without segments.
PAUSE_BREAK_S = 0.6
PAUSE_NEW_SEGMENT_S = 1.0
# Lines never share a stamp: a start not after the previous one is moved to this step after it.
MIN_STEP_S = 0.01

_SENTENCE_END = ".!?…"
_CLAUSE_END = ",;:—–-"

_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NOTES = "♪♫♬♩#*"

# Known Whisper hallucinations on music or silence, from its subtitle training data. Each is a
# normalised substring (lower case, ё as е, punctuation to spaces). Kept short on purpose: a
# phrase a real lyric could contain ("thank you", "спасибо") is not here.
#
# Two kinds, told apart in follow-up 11B (Zcode #27):
# - CREDIT phrases name who made the subtitles. No song line says "subtitles by"; the credit also
#   carries names after it, so a segment that CONTAINS one is dropped.
CREDIT_PHRASES: Tuple[str, ...] = (
    # Russian
    "редактор субтитров", "корректор а", "субтитры сделал", "субтитры создавал", "субтитры подогнал",
    "субтитры добавил", "dimatorzok",
    # English
    "subtitles by", "subtitles made by", "subtitled by", "captions by", "transcribed by", "amara org",
)
# - STOCK sign-off phrases are what a video says at its end, but a lyric can use the same words
#   ("до новых встреч, моя любовь", "to be continued in my dreams"). A segment is dropped only when it
#   is nothing but these phrases (and punctuation). The alternative, dropping one at the song's end
#   when the backend reports silence, needs a figure the HF pipeline does not give and a song length
#   the filter does not always have, so it is not used; a sign-off hallucinated as one line among real
#   words is kept, and costs the person one line they can see and delete.
STOCK_PHRASES: Tuple[str, ...] = (
    "продолжение следует", "спасибо за просмотр", "подписывайтесь на канал", "ставьте лайки",
    "до новых встреч", "до свидания друзья",
    "thanks for watching", "thank you for watching", "thank you so much for watching",
    "please subscribe to my channel", "please subscribe to the channel", "please subscribe",
    "don t forget to subscribe", "like and subscribe", "see you in the next video", "to be continued",
)
HALLUCINATION_PHRASES: Tuple[str, ...] = CREDIT_PHRASES + STOCK_PHRASES
_STOCK_LONGEST_FIRST: Tuple[str, ...] = tuple(sorted(STOCK_PHRASES, key=len, reverse=True))

# A whole segment that only names a sound, "[Music]" or "(аплодисменты)".
_SOUND_TAG = re.compile(
    r"^[\[\(\*]\s*(music|musica|musik|singing|applause|laughter|silence|blank audio|"
    r"музыка|пение|аплодисменты|смех|тишина)\s*[\]\)\*]$")


def normalise(text: str) -> str:
    """Lower case, ё as е, punctuation and symbols as spaces, spaces collapsed."""
    lowered = str(text).lower().replace("ё", "е")
    return " ".join(_WORD.findall(lowered))


def _clean_text(text: Any) -> str:
    """Spaces collapsed and the note symbols a model draws around sung text removed."""
    return _SPACES.sub(" ", str(text or "")).strip().strip(_NOTES + " ").strip()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _is_only_stock_phrases(norm: str) -> bool:
    """True when the normalised text is nothing but `STOCK_PHRASES` (one or several in a row)."""
    rest = f" {norm} "
    for phrase in _STOCK_LONGEST_FIRST:
        rest = rest.replace(f" {phrase} ", " ")
    return rest.strip() == "" and rest != f" {norm} "


def _repeated_ngram_run(tokens: List[str], max_size: int = 4) -> Tuple[int, float]:
    """`(repeats, coverage)` of the longest back-to-back repeat of a 1-`max_size` token group."""
    best = (0, 0.0)
    n_tokens = len(tokens)
    for size in range(1, max_size + 1):
        i = 0
        while i + size <= n_tokens:
            group = tokens[i:i + size]
            run = 1
            while tokens[i + run * size:i + (run + 1) * size] == group:
                run += 1
            if run > best[0]:
                best = (run, run * size / n_tokens)
            i += max(1, run * size) if run > 1 else 1
    return best


def drop_reason(segment: Dict[str, Any]) -> Optional[str]:
    """Why a segment is a hallucination, or None when it stays. One of `empty`, `sound_tag`,
    `phrase` (a credit, or a segment that is only stock sign-offs), `silence` (the backend's
    no-speech figures), `loop` (a repeated group, or Whisper's compression-ratio gate confirmed by
    a repeated group)."""
    text = _clean_text(segment.get("text"))
    norm = normalise(text)
    if not norm:
        return "empty"
    if _SOUND_TAG.match(_SPACES.sub(" ", str(segment.get("text") or "")).strip().strip("♪♫♬♩ ").lower()):
        return "sound_tag"
    padded = f" {norm} "
    if any(f" {phrase} " in padded for phrase in CREDIT_PHRASES) or _is_only_stock_phrases(norm):
        return "phrase"
    no_speech, logprob = _number(segment.get("no_speech_prob")), _number(segment.get("avg_logprob"))
    if no_speech is not None and logprob is not None and no_speech > NO_SPEECH_PROB_MAX and logprob < AVG_LOGPROB_MIN:
        return "silence"
    ratio = _number(segment.get("compression_ratio"))
    if ratio is None and len(text) >= COMPRESSION_MIN_CHARS:
        raw = text.encode("utf-8")
        ratio = len(raw) / len(zlib.compress(raw))
    if ratio is not None and ratio > COMPRESSION_RATIO_MAX and len(text) >= COMPRESSION_MIN_CHARS:
        confirmed, confirmed_cover = _repeated_ngram_run(norm.split(), CONFIRM_NGRAM_MAX_SIZE)
        if confirmed >= CONFIRM_NGRAM_RUN and confirmed_cover >= CONFIRM_NGRAM_COVERAGE:
            return "loop"
    repeats, coverage = _repeated_ngram_run(norm.split())
    if repeats >= REPEAT_NGRAM_RUN and coverage >= REPEAT_NGRAM_COVERAGE:
        return "loop"
    return None


def is_hallucination_text(text: str) -> bool:
    """The text-only rules, for lines that carry no times or figures (a text-only provider)."""
    return drop_reason({"text": text}) is not None


# -- words ----------------------------------------------------------------------------------

def segments_from_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Segments for a run of words that came without any: a new one after a sentence end or a
    pause of `PAUSE_NEW_SEGMENT_S`."""
    segments: List[Dict[str, Any]] = []
    group: List[Dict[str, Any]] = []

    def close() -> None:
        if group:
            segments.append({"start": group[0]["start"], "end": group[-1]["end"],
                             "text": " ".join(w["word"] for w in group), "words": list(group)})
            group.clear()

    for word in words:
        if group and (word["start"] - group[-1]["end"] >= PAUSE_NEW_SEGMENT_S
                      or group[-1]["word"].rstrip("\"')»”").endswith(tuple(_SENTENCE_END))):
            close()
        group.append(word)
    close()
    return segments


def _usable_words(words: Any) -> List[Dict[str, Any]]:
    """The words with finite, ordered times and some text; nothing else survives."""
    out: List[Dict[str, Any]] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        start, end, text = _number(w.get("start")), _number(w.get("end")), str(w.get("word") or "").strip()
        if text and start is not None and end is not None and end >= start and (not out or start >= out[-1]["start"]):
            out.append({"word": text, "start": start, "end": end})
    return out


def _words_in(words: List[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    slack = 0.05
    return [w for w in words if start - slack <= w["start"] < end + slack]


def _too_long(start: float, end: float, chars: int) -> bool:
    return end - start > MAX_LINE_S or chars > MAX_LINE_CHARS


def _split_by_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pieces `{start, end, text}` cut at the best word boundary that keeps each piece within
    the limits: a sentence end, then a clause end, then the longest pause; the latest on a tie."""
    pieces: List[Dict[str, Any]] = []
    i = 0
    n = len(words)

    def piece(a: int, b: int) -> Dict[str, Any]:
        part = words[a:b]
        return {"start": part[0]["start"], "end": part[-1]["end"], "text": " ".join(w["word"] for w in part)}

    while i < n:
        chars = 0
        j = i
        while j < n:
            chars += len(words[j]["word"]) + (1 if j > i else 0)
            if j > i and _too_long(words[i]["start"], words[j]["end"], chars):
                break
            j += 1
        if j >= n:
            pieces.append(piece(i, n))
            break
        # words[i:j] fit; the cut is somewhere in (i + MIN_PIECE_WORDS .. j), leaving a usable rest.
        lowest = min(i + MIN_PIECE_WORDS, j)
        best, best_score = j, -1.0
        for k in range(lowest, j + 1):
            if n - k < 1:
                break
            last = words[k - 1]["word"].rstrip("\"')»”")
            score = max(0.0, words[k]["start"] - words[k - 1]["end"]) if k < n else 0.0
            if score >= PAUSE_BREAK_S:
                score += 0.5
            if last.endswith(tuple(_SENTENCE_END)):
                score += 2.0
            elif last.endswith(tuple(_CLAUSE_END)):
                score += 1.0
            if score >= best_score:
                best, best_score = k, score
        pieces.append(piece(i, best))
        i = best
    return pieces


def _cut_by_characters(words: List[str], count: int) -> List[List[str]]:
    """`count` groups of consecutive words with about equal characters, cut at a sentence or clause
    end when one is near the ideal place, else at the nearest word boundary."""
    # Boundary k is "after word k"; its character offset is where that word ends.
    offsets, running = [], 0
    for word in words:
        running += len(word) + 1
        offsets.append(running - 1)
    target = offsets[-1] / count
    cuts: List[int] = []
    for n in range(1, count):
        ideal = n * target
        low = (cuts[-1] if cuts else 0) + 1
        high = len(words) - (count - n)
        candidates = range(low, high + 1)
        if not candidates:
            break
        nearest = min(candidates, key=lambda k: abs(offsets[k - 1] - ideal))
        clause = [k for k in candidates if abs(offsets[k - 1] - ideal) <= 0.25 * target
                  and words[k - 1].rstrip("\"')»”").endswith(tuple(_SENTENCE_END + _CLAUSE_END))]
        cuts.append(min(clause, key=lambda k: abs(offsets[k - 1] - ideal)) if clause else nearest)
    bounds = [0] + cuts + [len(words)]
    return [g for g in (words[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)) if g]


def _timed_pieces(start: float, end: float, groups: List[List[str]]) -> List[Dict[str, Any]]:
    """Each group given a share of `start`..`end` equal to its share of the characters."""
    total = sum(len(" ".join(g)) for g in groups) or 1
    pieces, cursor = [], start
    for g in groups:
        piece_text = " ".join(g)
        piece_end = cursor + (end - start) * len(piece_text) / total
        pieces.append({"start": cursor, "end": piece_end, "text": piece_text})
        cursor = piece_end
    pieces[-1]["end"] = end
    return pieces


def _within_limits(pieces: List[Dict[str, Any]]) -> bool:
    # Half a millisecond of slack: stamps are rounded to the millisecond afterwards.
    return all(p["end"] - p["start"] <= MAX_LINE_S + 0.0005 and len(p["text"]) <= MAX_LINE_CHARS for p in pieces)


def _split_proportionally(start: float, end: float, text: str) -> List[Dict[str, Any]]:
    """Pieces cut at word boundaries of the text, each given a share of the segment's time
    equal to its share of the characters. An approximation: no word time is known here, so
    the stamps are not an alignment to the singing.

    The same limits as the word path (`MAX_LINE_S`, `MAX_LINE_CHARS`) are held by trying more
    pieces until every one is inside them: the first count is the one the limits imply, and since
    the time follows the characters an uneven cut can still leave a piece over, so the count rises
    (down to one word a piece, with `MIN_PIECE_WORDS` preferred) until none is. They cannot always
    hold: a segment of a few words over a long time (three words over thirty seconds) has no cut
    that gives a piece under 7 s without inventing times for words nobody timed, so it is cut as
    far as `MIN_PIECE_WORDS` allows and may stay over the time limit."""
    words = text.split()
    base = max(math.ceil((end - start) / MAX_LINE_S), math.ceil(len(text) / MAX_LINE_CHARS), 1)
    preferred_max = max(1, len(words) // MIN_PIECE_WORDS)
    first = min(base, preferred_max)
    attempts = list(range(first, preferred_max + 1)) + list(range(preferred_max + 1, len(words) + 1))
    for count in attempts:
        if count <= 1:
            pieces = [{"start": start, "end": end, "text": text}]
        else:
            pieces = _timed_pieces(start, end, _cut_by_characters(words, count))
        if _within_limits(pieces):
            return pieces
    if first <= 1:
        return [{"start": start, "end": end, "text": text}]
    return _timed_pieces(start, end, _cut_by_characters(words, first))


def split_segment(segment: Dict[str, Any], words: Optional[List[Dict[str, Any]]] = None
                  ) -> Tuple[List[Dict[str, Any]], str]:
    """`(pieces, how)`: `how` is `none` (short enough, untouched), `words` (cut at word
    timestamps) or `proportional` (no usable word times, cut by characters)."""
    start, end = _number(segment.get("start")), _number(segment.get("end"))
    text = _clean_text(segment.get("text"))
    if start is None or end is None or not text:
        return [dict(segment, text=text)], "none"
    if not _too_long(start, end, len(text)):
        return [{"start": start, "end": end, "text": text}], "none"
    usable = _words_in(_usable_words(segment.get("words") or words), start, end)
    spoken = len(_WORD.findall(text))
    if len(usable) >= 2 and spoken and 0.7 <= len(usable) / spoken <= 1.3:
        pieces = _split_by_words(usable)
        if len(pieces) > 1:
            # The words carry the real timing; the segment's text is Whisper's own wording.
            return pieces, "words"
        return [{"start": start, "end": end, "text": text}], "none"
    pieces = _split_proportionally(start, end, text)
    return pieces, ("proportional" if len(pieces) > 1 else "none")


# -- the whole transcript -----------------------------------------------------------------

def _strictly_increasing(segments: List[Dict[str, Any]]) -> None:
    previous: Optional[float] = None
    for seg in segments:
        if previous is not None and seg["start"] <= previous:
            seg["start"] = round(previous + MIN_STEP_S, 3)
            if seg["end"] <= seg["start"]:
                seg["end"] = round(seg["start"] + MIN_STEP_S, 3)
        previous = seg["start"]


def clean_transcript(result: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """`(cleaned result, report)` for a transcription result `{text, segments, words?, ...}`.

    The cleaned result has `segments` as `[{start, end, text}]` (confidence figures and words
    are gone, they were the rules' input) and `text` rebuilt from them when anything was
    dropped. The report: `version`, `ran`, `dropped` (a count), `dropped_reasons` ({reason: n}),
    `kept_raw` (everything would have gone, so nothing did), `split_by_words`,
    `split_proportional` (counts of segments cut), `words` (whether word times were there),
    `words_unavailable` (the provider tried for word times and the model refused; only then),
    `dropped_lines` (up to `MAX_REPORTED_DROPS` of the dropped segments as `{start, text, reason}`,
    for a "what was filtered" view; `dropped` is the full count)."""
    segments = [dict(s) for s in (result.get("segments") or []) if isinstance(s, dict)]
    words = _usable_words(result.get("words"))
    if not segments and words:
        segments = segments_from_words(words)
    report: Dict[str, Any] = {"version": FILTER_VERSION, "ran": True, "dropped": 0, "dropped_reasons": {},
                              "kept_raw": False, "split_by_words": 0, "split_proportional": 0,
                              "words": bool(words) or any(s.get("words") for s in segments)}
    if result.get("words_unavailable"):
        report["words_unavailable"] = True
    if not segments:
        return dict(result), report

    duration = _number(result.get("duration"))
    kept: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}
    dropped: List[Tuple[Dict[str, Any], str]] = []
    for seg in segments:
        reason = drop_reason(seg)
        if reason is None:
            kept.append(seg)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
            dropped.append((seg, reason))
    kept = _collapse_runs(kept, reasons, dropped, duration)

    if not kept:
        kept = segments
        report["kept_raw"] = True
        reasons = {}
        dropped = []
    report["dropped"] = sum(reasons.values())
    report["dropped_reasons"] = reasons
    report["dropped_lines"] = [
        {"start": _number(seg.get("start")), "text": _clean_text(seg.get("text"))[:MAX_REPORTED_TEXT], "reason": why}
        for seg, why in dropped[:MAX_REPORTED_DROPS]]

    out: List[Dict[str, Any]] = []
    for seg in kept:
        pieces, how = split_segment(seg, words)
        if how == "words":
            report["split_by_words"] += 1
        elif how == "proportional":
            report["split_proportional"] += 1
        out.extend(pieces)
    out = [p for p in out if _clean_text(p.get("text"))]
    for piece in out:
        piece["text"] = _clean_text(piece["text"])
        for key in ("start", "end"):
            value = _number(piece.get(key))
            piece[key] = round(value, 3) if value is not None else piece.get(key)
    _strictly_increasing([p for p in out if _number(p.get("start")) is not None])

    cleaned = dict(result)
    cleaned["segments"] = [{"start": p["start"], "end": p["end"], "text": p["text"]} for p in out]
    cleaned.pop("words", None)
    if report["dropped"]:
        cleaned["text"] = " ".join(p["text"] for p in out)
    return cleaned, report


def _time_evidence(seg: Dict[str, Any], previous_start: Optional[float],
                   duration: Optional[float]) -> Optional[str]:
    """Why a repeated segment's TIMES say it is not a sung repeat, or None. Three kinds:
    `same_stamp` (it starts no later than the repeat before it: Whisper stamping a loop with one
    time), `squeezed` (it has less time than its text needs at `MAX_SUNG_CHARS_PER_S`, zero length
    included), `past_audio` (it starts at or after the end of the audio). A segment without usable
    times has no evidence either way and stays."""
    start, end = _number(seg.get("start")), _number(seg.get("end"))
    if start is None or end is None:
        return None
    if previous_start is not None and start <= previous_start:
        return "same_stamp"
    if end - start < len(_clean_text(seg.get("text"))) / MAX_SUNG_CHARS_PER_S:
        return "squeezed"
    if duration is not None and start >= duration:
        return "past_audio"
    return None


def _collapse_runs(segments: List[Dict[str, Any]], reasons: Dict[str, int],
                   dropped: Optional[List[Tuple[Dict[str, Any], str]]] = None,
                   duration: Optional[float] = None) -> List[Dict[str, Any]]:
    """Of `REPEAT_SEGMENT_RUN` or more identical segments in a row, keep the first and drop each
    later one only when its times show it is not sung (`_time_evidence`). The same words sung four
    times in a row are a chorus; what makes a hallucinated loop is its timing, not its text. A
    hallucinated loop that is stamped as plausible singing time is therefore kept: the filter
    cannot tell it from a chorus, and the raw result is stored for a person who wants to look."""
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(segments):
        norm = normalise(segments[i].get("text") or "")
        j = i + 1
        while j < len(segments) and normalise(segments[j].get("text") or "") == norm:
            j += 1
        if j - i >= REPEAT_SEGMENT_RUN:
            out.append(segments[i])
            for k in range(i + 1, j):
                why = _time_evidence(segments[k], _number(segments[k - 1].get("start")), duration)
                if why is None:
                    out.append(segments[k])
                else:
                    reasons["repeat"] = reasons.get("repeat", 0) + 1
                    if dropped is not None:
                        dropped.append((segments[k], "repeat"))
        else:
            out.extend(segments[i:j])
        i = j
    return out
