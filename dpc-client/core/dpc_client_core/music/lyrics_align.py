"""Deterministic forced alignment of known lyric lines against Whisper word timestamps.

Pure Python, standard library only, no I/O or model loading. Never edits the lyric text:
line order, wording, stress marks (`U+0301`), capitalization, and section headers like
`[Verse 1]` / `[Chorus]` are preserved verbatim.

How alignment works:
1. Extract timed words from Whisper results (top-level `words` from HF pipelines, nested
   `segments[*].words` from MLX pipelines, or char-proportional word expansion from timed
   segments when word timestamps are unavailable), merging split BPE contractions
   (`"don"` + `"'t"`).
2. Parse non-empty lyric lines while recognizing and skipping `[Section]` header lines so
   their words never consume sung timestamps.
3. Normalize tokens (`NFC`, strip U+0301 acute stress marks without altering Cyrillic `й`,
   `ё -> е`, casefold, split hyphens, strip outer punctuation) for matching while keeping
   original strings untouched.
4. Anchor on unique shared tokens/blocks first so repeated choruses cannot cross verses or
   jump backward, then run bounded similarity-weighted Needleman-Wunsch DP inside each
   interval (supporting exact matches, fuzzy matches for mishearings, and 1-to-2 / 2-to-1
   compound word splits/merges, without quadratic blowup on long songs).
5. Optional `section_anchors` (in seconds or 25 fps yue2 frame indices, or extracted from a
   yue2 ABC score via `cover_score.score_parts`) gently bias alignments toward matching
   section windows and anchor unmatched sections when available.
6. Assign each matched lyric line the start time of its first matched Whisper word, then
   monotonically interpolate unmatched lines between neighboring anchored lines and cleanly
   distribute leading/trailing unmatched lines within `[0, duration_s]`.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

YUE2_FRAME_RATE = 25

# Stress-mark code points that generated Russian lyrics may contain (U+0301 etc.).
# Removed individually in NFC form so precomposed Cyrillic 'й' (U+0439) is kept intact.
_STRESS_MARKS = "\u0301\u0300\u0341\u00b4\u02ca\u0384"
_STRESS_TRANS = str.maketrans("", "", _STRESS_MARKS)
_APOSTROPHES = re.compile(r"[’ʼ‘`]")
_HYPHENS = re.compile(r"[-–—]+")
_SECTION_HEADER = re.compile(r"^\s*\[([^\[\]:]+)\]\s*$")
_WORD_CHARS = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)

# Minimum size / DP bounds to avoid quadratic blowup on pathological long inputs.
_DP_MAX_CELLS = 100_000
_BAND_HALF = 64

# Thresholds determining whether an alignment found enough real matches.
_MIN_COVERAGE = 0.15
_MIN_MATCHED_WORDS_LONG = 2


@dataclass(frozen=True)
class _ParsedLine:
    index: int
    text: str
    is_header: bool
    section_label: Optional[str]
    tokens: Tuple[str, ...]


@dataclass(frozen=True)
class AlignmentResult:
    """Aligned `mp.lyrics/1` lines plus match diagnostics."""

    lines: List[Dict[str, Any]]
    matched_words: int
    total_lyric_words: int
    matched_lines: int
    total_sung_lines: int
    whisper_words: int
    coverage: float
    sufficient: bool
    used_segment_fallback: bool = False

    def stats(self) -> Dict[str, Any]:
        return {
            "matched_words": self.matched_words,
            "total_lyric_words": self.total_lyric_words,
            "matched_lines": self.matched_lines,
            "total_sung_lines": self.total_sung_lines,
            "whisper_words": self.whisper_words,
            "coverage": round(self.coverage, 4),
            "sufficient": self.sufficient,
            "used_segment_fallback": self.used_segment_fallback,
        }


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def is_section_header(line: str) -> bool:
    """True for bracketed section markers such as `[Verse 1]`, `[Chorus]`, `[Bridge]`, `[Outro]`."""
    return _SECTION_HEADER.match(str(line or "")) is not None


def extract_section_label(line: str) -> Optional[str]:
    """Normalized lower-case section label (e.g., `'verse 1'` -> `'verse 1'`), or None."""
    m = _SECTION_HEADER.match(str(line or ""))
    if not m:
        return None
    label = " ".join(m.group(1).strip().casefold().split())
    return label or None


def normalize_token(word: str) -> str:
    """Normalize one token for matching while preserving letters across Latin & Cyrillic."""
    text = unicodedata.normalize("NFC", str(word or ""))
    text = text.translate(_STRESS_TRANS)
    text = _APOSTROPHES.sub("'", text)
    text = text.casefold().replace("ё", "е").replace("+", "")
    parts = _WORD_CHARS.findall(text)
    if not parts:
        return ""
    joined = "".join(parts).replace("'", "")
    return joined


def tokenize_line(line: str) -> List[str]:
    """Tokenize a sung lyric line into normalized words (hyphens split cleanly)."""
    text = unicodedata.normalize("NFC", str(line or ""))
    text = text.translate(_STRESS_TRANS)
    text = _APOSTROPHES.sub("'", text)
    text = _HYPHENS.sub(" ", text)
    text = text.casefold().replace("ё", "е").replace("+", "")
    out: List[str] = []
    for raw in _WORD_CHARS.findall(text):
        tok = raw.replace("'", "")
        if tok:
            out.append(tok)
    return out


def parse_lyric_lines(lyrics: Any) -> List[_ParsedLine]:
    """Parse raw text or line sequence into structured `_ParsedLine`s preserving exact line text."""
    if isinstance(lyrics, str):
        raw_lines: Iterable[str] = lyrics.splitlines()
    elif isinstance(lyrics, Sequence):
        raw_lines = [
            str(item.get("text") if isinstance(item, Mapping) else item or "")
            for item in lyrics
        ]
    else:
        return []
    parsed: List[_ParsedLine] = []
    for raw in raw_lines:
        clean = str(raw or "").strip()
        if not clean:
            continue
        label = extract_section_label(clean)
        if label is not None:
            parsed.append(_ParsedLine(index=len(parsed), text=clean, is_header=True,
                                      section_label=label, tokens=()))
        else:
            parsed.append(_ParsedLine(index=len(parsed), text=clean, is_header=False,
                                      section_label=None, tokens=tuple(tokenize_line(clean))))
    return parsed


def has_alignable_words(lyrics: Any) -> bool:
    """True when `lyrics` contains at least one non-header alignable word token."""
    return any((not ln.is_header) and ln.tokens for ln in parse_lyric_lines(lyrics))


def _merge_contractions(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join BPE-split leading-apostrophe tokens like `'t`, `'re`, `'s` onto the preceding word."""
    out: List[Dict[str, Any]] = []
    for item in words:
        raw = item["word"]
        norm_raw = _APOSTROPHES.sub("'", raw.strip())
        if (out and norm_raw.startswith("'") and len(norm_raw) > 1
                and item["start"] - out[-1]["end"] <= 0.5):
            prev = out[-1]
            prev["word"] = prev["word"] + raw.strip()
            prev["end"] = max(prev["end"], item["end"])
            continue
        out.append(dict(item))
    return out


def _sanitize_word_list(raw_words: Iterable[Any]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for w in raw_words:
        if not isinstance(w, Mapping):
            continue
        text = str(w.get("word") or "").strip()
        start = _finite(w.get("start"))
        end = _finite(w.get("end"))
        if not text or start is None:
            continue
        start = max(0.0, start)
        if end is None or end < start:
            end = start
        if cleaned and start < cleaned[-1]["start"]:
            start = cleaned[-1]["start"]
            end = max(start, end)
        cleaned.append({"word": text, "start": round(start, 3), "end": round(end, 3)})
    return _merge_contractions(cleaned)


def _expand_segments_to_words(segments: Iterable[Any]) -> List[Dict[str, Any]]:
    """Fallback when Whisper returns segments without word timestamps: distribute words
    proportionally by character offset within each segment."""
    out: List[Dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, Mapping):
            continue
        start = _finite(seg.get("start"))
        end = _finite(seg.get("end"))
        text = str(seg.get("text") or "").strip()
        if start is None or not text:
            continue
        start = max(0.0, start)
        if end is None or end <= start:
            end = start + max(0.2, 0.25 * len(text.split()))
        pieces = text.split()
        total_chars = sum(len(p) for p in pieces) or 1
        span = end - start
        cursor = start
        for p in pieces:
            step = span * (len(p) / total_chars)
            w_end = min(end, cursor + step)
            out.append({"word": p, "start": round(cursor, 3), "end": round(w_end, 3)})
            cursor = w_end
    return _sanitize_word_list(out)


def extract_whisper_words_with_fallback(result: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """Extract timed word dicts `[{word, start, end}]` from a Whisper result dict or word sequence.

    Returns `(words, used_segment_fallback)`:
    - Handles direct sequence of `{word, start, end}`.
    - Handles HF pipeline result `{"words": [...]}`.
    - Handles MLX result `{"segments": [{"words": [...]}, ...]}`.
    - Falls back to segment word expansion only when no word-level timestamps exist.
    """
    if isinstance(result, (list, tuple)):
        return _sanitize_word_list(result), False
    if not isinstance(result, Mapping):
        return [], False
    top_words = _sanitize_word_list(result.get("words") or [])
    if top_words:
        return top_words, False
    seg_words_raw: List[Any] = []
    segments = result.get("segments") or []
    for seg in segments:
        if isinstance(seg, Mapping) and seg.get("words"):
            seg_words_raw.extend(seg["words"])
    seg_words = _sanitize_word_list(seg_words_raw)
    if seg_words:
        return seg_words, False
    expanded = _expand_segments_to_words(segments)
    return expanded, bool(expanded)


def extract_whisper_words(result: Any) -> List[Dict[str, Any]]:
    """Extract normalized `[{word, start, end}]` timed words from a Whisper result."""
    words, _ = extract_whisper_words_with_fallback(result)
    return words


def anchors_from_yue2_result(replay: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Extract optional section start anchors (`[{"label", "start_s"}]`) from a yue2.cpp replay.

    Note: `yue2.cpp` `request_replay` currently emits `abc` and `semantic_tokens` (at 25 Hz), not
    an explicit `section_frames` array. This helper reads:
    1. Explicit `section_frames` / `section_start_frames` (`frame / 25.0`), if present;
    2. Explicit `section_anchors` / `section_starts_s` (seconds), if present;
    3. `cover_score.score_parts(abc)` when `abc` carries labeled `% <section>` parts and tempo.
    """
    if not isinstance(replay, Mapping):
        return []
    anchors: List[Dict[str, Any]] = []

    def _add(label: Optional[str], start_s: Optional[float]) -> None:
        if start_s is None or start_s < 0:
            return
        if anchors and start_s < anchors[-1]["start_s"]:
            return
        clean_label = " ".join(str(label or "").strip().casefold().split())
        anchors.append({"label": clean_label, "start_s": round(start_s, 3)})

    frames = replay.get("section_frames") or replay.get("section_start_frames")
    if isinstance(frames, Sequence) and not isinstance(frames, (str, bytes)):
        for item in frames:
            if isinstance(item, Mapping):
                fr = _finite(item.get("frame") if item.get("frame") is not None else item.get("start_frame"))
                sec = _finite(item.get("start_s"))
                t = sec if sec is not None else (fr / YUE2_FRAME_RATE if fr is not None else None)
                _add(item.get("label") or item.get("name"), t)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                fr = _finite(item[1])
                _add(str(item[0]), fr / YUE2_FRAME_RATE if fr is not None else None)
            else:
                fr = _finite(item)
                if fr is not None:
                    _add("", fr / YUE2_FRAME_RATE)
        if anchors:
            return anchors

    explicit = replay.get("section_anchors") or replay.get("section_starts_s")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        for item in explicit:
            if isinstance(item, Mapping):
                sec = _finite(item.get("start_s") if item.get("start_s") is not None else item.get("time_s"))
                _add(item.get("label") or item.get("name"), sec)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                _add(str(item[0]), _finite(item[1]))
            else:
                sec = _finite(item)
                if sec is not None:
                    _add("", sec)
        if anchors:
            return anchors

    abc = replay.get("abc")
    if isinstance(abc, str) and abc.strip():
        try:
            from .cover_score import score_parts
            parts = score_parts(abc).get("parts") or []
            for p in parts:
                if p.get("label"):
                    _add(p["label"], _finite(p.get("start_s")))
        except Exception:
            return []
    return anchors


def _normalize_section_anchors(section_anchors: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
    if not section_anchors:
        return []
    out: List[Dict[str, Any]] = []
    for item in section_anchors:
        label = ""
        start_s: Optional[float] = None
        if isinstance(item, Mapping):
            label = str(item.get("label") or item.get("name") or "")
            start_s = _finite(item.get("start_s") if item.get("start_s") is not None else item.get("time_s"))
            if start_s is None and item.get("frame") is not None:
                fr = _finite(item.get("frame"))
                start_s = fr / YUE2_FRAME_RATE if fr is not None else None
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label = str(item[0] or "")
            start_s = _finite(item[1])
        else:
            start_s = _finite(item)
        if start_s is not None and start_s >= 0:
            if not out or start_s >= out[-1]["start_s"]:
                out.append({"label": " ".join(label.strip().casefold().split()), "start_s": round(start_s, 3)})
    return out


def _map_section_headers(parsed: Sequence[_ParsedLine], anchors: Sequence[Mapping[str, Any]]) -> Dict[int, float]:
    """Monotonically match lyric `[Section]` header line indices to anchor start times."""
    if not anchors:
        return {}
    headers = [ln for ln in parsed if ln.is_header]
    if not headers:
        return {}

    def _base(lbl: str) -> str:
        return re.sub(r"[\s_\d-]+$", "", lbl).strip() or lbl

    # Match labels first; fall back to positional alignment if anchors are unlabeled.
    mapped: Dict[int, float] = {}
    a_idx = 0
    for hdr in headers:
        if a_idx >= len(anchors):
            break
        h_lbl = hdr.section_label or ""
        h_base = _base(h_lbl)
        hit = None
        for j in range(a_idx, len(anchors)):
            a_lbl = str(anchors[j].get("label") or "")
            if not a_lbl:
                hit = j
                break
            a_base = _base(a_lbl)
            if a_lbl == h_lbl or (h_base and a_base and (a_base == h_base or h_base in a_base or a_base in h_base)):
                hit = j
                break
        if hit is not None:
            mapped[hdr.index] = float(anchors[hit]["start_s"])
            a_idx = hit + 1
    return mapped


def _token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    # Fast length pre-check before edit ratio.
    la, lb = len(a), len(b)
    if min(la, lb) / max(la, lb) < 0.45:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _token_match_score(a: str, b: str) -> float:
    if a == b:
        return 3.0
    sim = _token_similarity(a, b)
    if sim >= 0.60:
        return round(2.8 * sim - 0.5, 4)
    # Single-char words only match on exact match to avoid 'a' == 'i'.
    if len(a) > 1 and len(b) > 1 and (a[:3] == b[:3] and min(len(a), len(b)) >= 3):
        return 0.9
    return -1.5


def _dp_align(
    l_tokens: Sequence[str],
    w_tokens: Sequence[str],
    l_offset: int,
    w_offset: int,
    w_times: Sequence[float],
    l_hints: Sequence[Optional[Tuple[float, float]]],
) -> List[Tuple[int, int]]:
    """Exact Needleman-Wunsch / compound DP on small bounded interval `[0..m) x [0..n)`."""
    m, n = len(l_tokens), len(w_tokens)
    if m == 0 or n == 0:
        return []

    gap_l = -1.0   # lyric word dropped by Whisper
    gap_w = -0.6   # extra/filler Whisper word
    neg_inf = -1e18

    # dp[i][j] and backpointer op codes:
    # 1: diag (1-1), 2: skip_l (1-0), 3: skip_w (0-1), 4: compound 2-1, 5: compound 1-2
    dp = [[neg_inf] * (n + 1) for _ in range(m + 1)]
    bp = [[0] * (n + 1) for _ in range(m + 1)]
    dp[0][0] = 0.0
    for i in range(1, m + 1):
        dp[i][0] = i * gap_l
        bp[i][0] = 2
    for j in range(1, n + 1):
        dp[0][j] = j * gap_w
        bp[0][j] = 3

    for i in range(1, m + 1):
        li = l_tokens[i - 1]
        hint = l_hints[l_offset + i - 1]
        row = dp[i]
        prev_row = dp[i - 1]
        prev2_row = dp[i - 2] if i >= 2 else None
        bp_row = bp[i]
        for j in range(1, n + 1):
            wj = w_tokens[j - 1]
            # Standard 1-1 alignment score
            score = _token_match_score(li, wj)
            if hint is not None and score > 0:
                wt = w_times[w_offset + j - 1]
                lo_s, hi_s = hint
                if lo_s - 4.0 <= wt <= hi_s + 4.0:
                    score += 0.35
                elif wt < lo_s - 12.0 or wt > hi_s + 12.0:
                    score -= 0.5
            best = prev_row[j - 1] + score
            op = 1

            sc_skip_l = prev_row[j] + gap_l
            if sc_skip_l > best:
                best = sc_skip_l
                op = 2

            sc_skip_w = row[j - 1] + gap_w
            if sc_skip_w > best:
                best = sc_skip_w
                op = 3

            # 2 lyric tokens -> 1 whisper word (e.g. "every body" vs "everybody")
            if prev2_row is not None and len(li) + len(l_tokens[i - 2]) == len(wj) and l_tokens[i - 2] + li == wj:
                sc_21 = prev2_row[j - 1] + 2.8
                if sc_21 > best:
                    best = sc_21
                    op = 4

            # 1 lyric token -> 2 whisper words (e.g. "everybody" vs "every body")
            if j >= 2 and len(w_tokens[j - 2]) + len(wj) == len(li) and w_tokens[j - 2] + wj == li:
                sc_12 = prev_row[j - 2] + 2.8
                if sc_12 > best:
                    best = sc_12
                    op = 5

            row[j] = best
            bp_row[j] = op

    pairs: List[Tuple[int, int]] = []
    i, j = m, n
    while i > 0 or j > 0:
        op = bp[i][j]
        if op == 1:
            if _token_match_score(l_tokens[i - 1], w_tokens[j - 1]) > 0:
                pairs.append((l_offset + i - 1, w_offset + j - 1))
            i -= 1
            j -= 1
        elif op == 2:
            i -= 1
        elif op == 3:
            j -= 1
        elif op == 4:
            pairs.append((l_offset + i - 2, w_offset + j - 1))
            pairs.append((l_offset + i - 1, w_offset + j - 1))
            i -= 2
            j -= 1
        elif op == 5:
            pairs.append((l_offset + i - 1, w_offset + j - 2))
            i -= 1
            j -= 2
        else:
            break
    pairs.reverse()
    return pairs


def _banded_align(
    l_tokens: Sequence[str],
    w_tokens: Sequence[str],
    l_offset: int,
    w_offset: int,
    band_half: int,
) -> List[Tuple[int, int]]:
    """Linear-space/time banded DP fallback when an unanchored sub-interval is very large."""
    m, n = len(l_tokens), len(w_tokens)
    if m == 0 or n == 0:
        return []
    band = max(band_half, abs(m - n) + 32)
    ratio = n / max(1, m)
    gap_l, gap_w = -1.0, -0.6
    neg_inf = -1e18

    row_starts: List[int] = [0]
    row_ends: List[int] = [min(n, band)]
    dp_rows: List[List[float]] = []
    bp_rows: List[List[int]] = []

    row0 = [j * gap_w for j in range(row_ends[0] + 1)]
    dp_rows.append(row0)
    bp_rows.append([3 if j > 0 else 0 for j in range(row_ends[0] + 1)])

    for i in range(1, m + 1):
        center = int(round(i * ratio))
        j_lo = max(0, center - band)
        j_hi = min(n, center + band)
        if j_hi < j_lo:
            j_lo = j_hi = min(n, max(0, center))
        row_starts.append(j_lo)
        row_ends.append(j_hi)
        width = j_hi - j_lo + 1
        cur = [neg_inf] * width
        cur_bp = [0] * width
        p_lo, p_hi = row_starts[i - 1], row_ends[i - 1]
        prev = dp_rows[i - 1]
        li = l_tokens[i - 1]

        for j in range(j_lo, j_hi + 1):
            idx = j - j_lo
            best = neg_inf
            op = 0
            if j > 0 and p_lo <= j - 1 <= p_hi:
                sc = prev[(j - 1) - p_lo] + _token_match_score(li, w_tokens[j - 1])
                if sc > best:
                    best, op = sc, 1
            if p_lo <= j <= p_hi:
                sc_l = prev[j - p_lo] + gap_l
                if sc_l > best:
                    best, op = sc_l, 2
            if j - 1 >= j_lo:
                sc_w = cur[(j - 1) - j_lo] + gap_w
                if sc_w > best:
                    best, op = sc_w, 3
            cur[idx] = best
            cur_bp[idx] = op
        dp_rows.append(cur)
        bp_rows.append(cur_bp)

    i, j = m, min(n, row_ends[m])
    # Snap to best end column in last row if j < n
    if j < row_ends[m]:
        j = row_ends[m]
    pairs: List[Tuple[int, int]] = []
    while i > 0 or j > 0:
        j_lo = row_starts[i]
        if not (j_lo <= j <= row_ends[i]):
            break
        op = bp_rows[i][j - j_lo]
        if op == 1:
            if _token_match_score(l_tokens[i - 1], w_tokens[j - 1]) > 0:
                pairs.append((l_offset + i - 1, w_offset + j - 1))
            i -= 1
            j -= 1
        elif op == 2:
            i -= 1
        elif op == 3:
            j -= 1
        else:
            break
    pairs.reverse()
    return pairs


def _unique_anchors(l_tokens: Sequence[str], w_tokens: Sequence[str]) -> List[Tuple[int, int]]:
    """Find monotonic `(l_idx, w_idx)` anchors using tokens unique to both sequences.

    Anchoring on unique tokens first prevents a repeated chorus from latching across verses
    when a verse contains dropped/misheard lines.
    """
    l_counts = Counter(l_tokens)
    w_counts = Counter(w_tokens)
    l_uniq_pos: Dict[str, int] = {}
    w_uniq_pos: Dict[str, int] = {}
    for i, t in enumerate(l_tokens):
        if len(t) >= 3 and l_counts[t] == 1 and w_counts.get(t) == 1:
            l_uniq_pos[t] = i
    for j, t in enumerate(w_tokens):
        if t in l_uniq_pos:
            w_uniq_pos[t] = j
    seq_a = [t for t in l_tokens if t in w_uniq_pos]
    seq_b = [t for t in w_tokens if t in l_uniq_pos]
    sm = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)
    anchors: List[Tuple[int, int]] = []
    for block in sm.get_matching_blocks():
        for k in range(block.size):
            tok = seq_a[block.a + k]
            anchors.append((l_uniq_pos[tok], w_uniq_pos[tok]))
    return anchors


def _align_interval(
    l_tokens: Sequence[str],
    w_tokens: Sequence[str],
    l_start: int,
    l_end: int,
    w_start: int,
    w_end: int,
    w_times: Sequence[float],
    l_hints: Sequence[Optional[Tuple[float, float]]],
) -> List[Tuple[int, int]]:
    l_len = l_end - l_start
    w_len = w_end - w_start
    if l_len <= 0 or w_len <= 0:
        return []
    sub_l = l_tokens[l_start:l_end]
    sub_w = w_tokens[w_start:w_end]
    if l_len * w_len <= _DP_MAX_CELLS:
        return _dp_align(sub_l, sub_w, l_start, w_start, w_times, l_hints)

    # Split large intervals further using SequenceMatcher matching blocks before DP/banded fallback.
    sm = difflib.SequenceMatcher(None, sub_l, sub_w, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    if not blocks:
        return _banded_align(sub_l, sub_w, l_start, w_start, _BAND_HALF)

    pairs: List[Tuple[int, int]] = []
    cur_l, cur_w = l_start, w_start
    for b in blocks:
        bl, bw = l_start + b.a, w_start + b.b
        pairs.extend(_align_interval(l_tokens, w_tokens, cur_l, bl, cur_w, bw, w_times, l_hints))
        for k in range(b.size):
            pairs.append((bl + k, bw + k))
        cur_l, cur_w = bl + b.size, bw + b.size
    pairs.extend(_align_interval(l_tokens, w_tokens, cur_l, l_end, cur_w, w_end, w_times, l_hints))
    return pairs


def align_lyrics(
    lyrics: Any,
    whisper_words: Any,
    *,
    duration_s: Optional[float] = None,
    section_anchors: Optional[Sequence[Any]] = None,
    used_segment_fallback: bool = False,
) -> AlignmentResult:
    """Align known lyric lines to Whisper timed words and return `AlignmentResult`.

    Never alters lyric wording or formatting on any non-empty line. Recognizes `[Verse]`-style
    section header lines, skips them during token alignment, preserves them in `lines`, and
    assigns valid monotonic timestamps in `[0, duration_s]`.
    """
    parsed = parse_lyric_lines(lyrics)
    sung_lines = [ln for ln in parsed if (not ln.is_header) and ln.tokens]
    # Also handle symbol-only non-header lines cleanly as unmatched sung lines if any exist.
    all_sung = [ln for ln in parsed if not ln.is_header]
    total_lyric_words = sum(len(ln.tokens) for ln in sung_lines)

    w_list = _sanitize_word_list(whisper_words) if isinstance(whisper_words, Sequence) else []
    if not all_sung or not w_list or total_lyric_words == 0:
        return AlignmentResult(
            lines=[],
            matched_words=0,
            total_lyric_words=total_lyric_words,
            matched_lines=0,
            total_sung_lines=len(all_sung),
            whisper_words=len(w_list),
            coverage=0.0,
            sufficient=False,
            used_segment_fallback=used_segment_fallback,
        )

    norm_anchors = _normalize_section_anchors(section_anchors)
    header_anchor_times = _map_section_headers(parsed, norm_anchors)

    # Build soft per-lyric-token section time windows when section anchors are available.
    line_windows: Dict[int, Tuple[float, float]] = {}
    if header_anchor_times:
        ordered_hdrs = sorted(header_anchor_times.items())
        for idx_pos, (hdr_line_idx, sec_start) in enumerate(ordered_hdrs):
            next_hdr_idx = ordered_hdrs[idx_pos + 1][0] if idx_pos + 1 < len(ordered_hdrs) else len(parsed)
            sec_end = (ordered_hdrs[idx_pos + 1][1] if idx_pos + 1 < len(ordered_hdrs)
                       else (float(duration_s) if duration_s is not None else sec_start + 120.0))
            for li in range(hdr_line_idx + 1, next_hdr_idx):
                line_windows[li] = (sec_start, max(sec_start, sec_end))

    # Flatten lyric word tokens across non-header lines.
    l_tokens: List[str] = []
    l_line_idx: List[int] = []
    l_hints: List[Optional[Tuple[float, float]]] = []
    for ln in parsed:
        if ln.is_header:
            continue
        win = line_windows.get(ln.index)
        for tok in ln.tokens:
            l_tokens.append(tok)
            l_line_idx.append(ln.index)
            l_hints.append(win)

    w_tokens: List[str] = []
    w_starts: List[float] = []
    for w in w_list:
        parts = tokenize_line(w["word"])
        if not parts:
            parts = [normalize_token(w["word"])]
        tok = parts[0] if len(parts) == 1 else "".join(parts)
        w_tokens.append(tok)
        w_starts.append(float(w["start"]))

    anchors = _unique_anchors(l_tokens, w_tokens)
    aligned_pairs: List[Tuple[int, int]] = []
    cur_l, cur_w = 0, 0
    for al, aw in anchors:
        aligned_pairs.extend(_align_interval(l_tokens, w_tokens, cur_l, al, cur_w, aw, w_starts, l_hints))
        aligned_pairs.append((al, aw))
        cur_l, cur_w = al + 1, aw + 1
    aligned_pairs.extend(_align_interval(l_tokens, w_tokens, cur_l, len(l_tokens), cur_w, len(w_tokens), w_starts, l_hints))

    # Deduplicate / guarantee strict monotonic order of aligned pairs.
    clean_pairs: List[Tuple[int, int]] = []
    matched_word_set = set()
    last_l, last_w = -1, -1
    for li, wi in aligned_pairs:
        if li > last_l and wi >= last_w:
            clean_pairs.append((li, wi))
            matched_word_set.add(li)
            last_l, last_w = li, wi

    # Map each sung line to its first matched Whisper word timestamp.
    first_match_for_line: Dict[int, float] = {}
    for li, wi in clean_pairs:
        line_i = l_line_idx[li]
        if line_i not in first_match_for_line:
            first_match_for_line[line_i] = w_starts[wi]

    matched_words = len(matched_word_set)
    matched_lines = len(first_match_for_line)
    coverage = matched_words / max(1, total_lyric_words)
    if total_lyric_words <= 4:
        sufficient = matched_words >= 1
    else:
        sufficient = matched_words >= _MIN_MATCHED_WORDS_LONG and (
            coverage >= _MIN_COVERAGE or matched_lines >= 2
        )

    if not matched_lines:
        return AlignmentResult(
            lines=[],
            matched_words=0,
            total_lyric_words=total_lyric_words,
            matched_lines=0,
            total_sung_lines=len(all_sung),
            whisper_words=len(w_list),
            coverage=0.0,
            sufficient=False,
            used_segment_fallback=used_segment_fallback,
        )

    raw_cap = float(duration_s) if duration_s is not None and math.isfinite(duration_s) and duration_s >= 0 else None
    max_w_end = max((float(w["end"]) for w in w_list), default=0.0)
    # Never crush actual Whisper word timestamps below their decoded timestamps if container/stub length is shorter.
    dur_cap = max(raw_cap, max_w_end) if raw_cap is not None else None

    # Build monotonic timestamps for sung lines (`times[line_index]`).
    sung_indices = [ln.index for ln in all_sung]
    sung_times: Dict[int, float] = {}
    for idx in sung_indices:
        if idx in first_match_for_line:
            t_val = max(0.0, first_match_for_line[idx])
            if dur_cap is not None:
                t_val = min(t_val, dur_cap)
            sung_times[idx] = round(t_val, 3)

    # Ensure anchored lines are non-decreasing.
    prev_anchor_t = 0.0
    for idx in sung_indices:
        if idx in sung_times:
            if sung_times[idx] < prev_anchor_t:
                sung_times[idx] = prev_anchor_t
            prev_anchor_t = sung_times[idx]

    anchored_positions = [pos for pos, idx in enumerate(sung_indices) if idx in sung_times]
    first_pos = anchored_positions[0]
    last_pos = anchored_positions[-1]

    def _weight_of(pos: int) -> float:
        ln = parsed[sung_indices[pos]]
        return float(max(1, len(ln.tokens)))

    def _distribute(start_pos: int, end_pos: int, lo_t: float, hi_t: float, *, include_lo: bool) -> None:
        count = end_pos - start_pos
        if count <= 0:
            return
        span = max(0.0, hi_t - lo_t)
        weights = [_weight_of(p) for p in range(start_pos, end_pos)]
        # Pad the tail (and head unless include_lo) so interpolated lines stay strictly inside endpoints.
        pad_head = 0.0 if include_lo else weights[0]
        pad_tail = weights[-1] * 0.8
        total_w = pad_head + sum(weights) + pad_tail
        cum = pad_head
        prev = lo_t
        for offset, w in enumerate(weights):
            frac = (cum - weights[0] + w if include_lo else cum + w) / total_w if total_w > 0 else 0.0
            if include_lo and offset == 0:
                frac = 0.0
            t_here = round(lo_t + span * min(1.0, max(0.0, frac)), 3)
            if t_here < prev:
                t_here = prev
            sung_times[sung_indices[start_pos + offset]] = t_here
            prev = t_here
            cum += w

    # 1. Leading unmatched sung lines -> [0.0 (or section anchor), first_anchor_t)
    if first_pos > 0:
        first_t = sung_times[sung_indices[first_pos]]
        first_hdr_lo = 0.0
        for hdr_idx, anc_t in sorted(header_anchor_times.items()):
            if hdr_idx < sung_indices[first_pos] and anc_t <= first_t:
                first_hdr_lo = max(0.0, anc_t)
                break
        _distribute(0, first_pos, first_hdr_lo, first_t, include_lo=(first_hdr_lo > 0.0 or first_pos > 1))

    # 2. Middle gaps between anchored sung lines
    for g in range(len(anchored_positions) - 1):
        a_pos = anchored_positions[g]
        b_pos = anchored_positions[g + 1]
        if b_pos - a_pos > 1:
            t_a = sung_times[sung_indices[a_pos]]
            t_b = sung_times[sung_indices[b_pos]]
            _distribute(a_pos + 1, b_pos, t_a, t_b, include_lo=False)

    # 3. Trailing unmatched sung lines -> (last_anchor_t, duration_s]
    if last_pos + 1 < len(sung_indices):
        last_t = sung_times[sung_indices[last_pos]]
        if dur_cap is not None and dur_cap >= last_t:
            hi_t = dur_cap
        else:
            est_tail = sum(max(1.5, 0.45 * _weight_of(p)) for p in range(last_pos + 1, len(sung_indices)))
            hi_t = last_t + est_tail
        _distribute(last_pos + 1, len(sung_indices), last_t, hi_t, include_lo=False)

    # Assign section headers the timestamp of the next sung line (or previous if trailing)
    # so LRC output keeps exact line order/text with valid non-decreasing stamps.
    all_times: List[float] = [0.0] * len(parsed)
    next_sung_t: Optional[float] = None
    for i in range(len(parsed) - 1, -1, -1):
        if not parsed[i].is_header:
            t_i = sung_times[i]
            all_times[i] = t_i
            next_sung_t = t_i
        else:
            anchor_t = header_anchor_times.get(i)
            if anchor_t is not None and (next_sung_t is None or anchor_t <= next_sung_t):
                all_times[i] = round(max(0.0, anchor_t), 3)
            elif next_sung_t is not None:
                all_times[i] = next_sung_t
            else:
                all_times[i] = dur_cap if dur_cap is not None else 0.0

    # Final monotonic clamp across all lines (handles trailing headers & rounding ties).
    prev_t = 0.0
    for i in range(len(parsed)):
        t_i = max(prev_t, all_times[i])
        if dur_cap is not None:
            t_i = min(t_i, dur_cap)
        all_times[i] = round(max(0.0, t_i), 3)
        prev_t = all_times[i]

    lines_out = [{"t_start": all_times[ln.index], "text": ln.text} for ln in parsed]
    return AlignmentResult(
        lines=lines_out,
        matched_words=matched_words,
        total_lyric_words=total_lyric_words,
        matched_lines=matched_lines,
        total_sung_lines=len(all_sung),
        whisper_words=len(w_list),
        coverage=coverage,
        sufficient=sufficient,
        used_segment_fallback=used_segment_fallback,
    )


def align_result_to_lyrics(
    lyrics: Any,
    whisper_result: Any,
    *,
    duration_s: Optional[float] = None,
    section_anchors: Optional[Sequence[Any]] = None,
) -> AlignmentResult:
    """Convenience wrapper extracting words/duration from a Whisper provider result dict."""
    words, used_fallback = extract_whisper_words_with_fallback(whisper_result)
    dur = duration_s
    if dur is None and isinstance(whisper_result, Mapping):
        dur = _finite(whisper_result.get("duration"))
    return align_lyrics(
        lyrics,
        words,
        duration_s=dur,
        section_anchors=section_anchors,
        used_segment_fallback=used_fallback,
    )
