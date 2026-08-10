"""Render backlog.md into a scannable board and a link graph, validate it, and write it.

    uv run python tools/backlog/build.py                 # rebuild board + graph
    uv run python tools/backlog/build.py --check         # validate, write nothing
    uv run python tools/backlog/build.py add NAME    --desc=… --priority=… --origin=…
    uv run python tools/backlog/build.py move NAME   --to='IN PROGRESS' [--by=CC]
    uv run python tools/backlog/build.py rename OLD NEW
    uv run python tools/backlog/build.py close NAME  --session=S72 --resolution=fixed \\
                                                     --evidence='…' [--by=CC]

The board and the graph are written in one pass, so the two artefacts can never disagree
about how fresh they are.

Rendering and `--check` never touch backlog.md. The four verbs do (ADR-039): each writes
the file, re-runs `--check` over the result in a scratch copy first, and refuses to keep a
write that would introduce a refusal. Add `--dry-run` to validate without writing.

The verbs are convenience, not the guarantee — the file opens in any editor, so `--check`
is what actually holds the format. See docs/BACKLOG_FORMAT.md.
"""
import html
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]   # tools/backlog/build.py -> repo root

# ADR-039 item 1: the four mutations are subcommands of this script rather than a sibling,
# so the thing that writes an entry and the thing that validates it can never drift apart.
# A verb is only ever argv[1]; anywhere else the word is an entry name, not a command.
VERBS = ("add", "close", "move", "rename")
VERB = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in VERBS else ""

# An explicit path lets --check run against any project's backlog (and against a fixture,
# which is how the rules below are tested). Without one it reads this project's.
# A positional is a file only if it ends in `.md`; the rest are the verb's own arguments
# (an entry name, a new name), which must not be mistaken for a source path.
_paths = [a for a in sys.argv[1 + bool(VERB):] if not a.startswith("-")]
_md = [a for a in _paths if a.endswith(".md")]
ARGS = [a for a in _paths if not a.endswith(".md")]
SRC = Path((_md or _paths or [""])[0]).resolve() if (_md or (_paths and not VERB)) \
    else ROOT / "backlog.md"
# Where the rendered views land. Defaults beside this script, which is right for the
# project the script lives in and wrong for every other one: without `--out`, running
# `build.py ../other-project/backlog.md` would have quietly overwritten this project's
# board with another project's entries. `--check` never writes, so the hazard only exists
# on a plain run — and a plain run against a foreign file now refuses instead (see below).
_out = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--out=")]
OUT = Path(_out[0]).resolve() if _out else Path(__file__).resolve().parent
DST = OUT / "backlog.html"
GRAPH_DST = OUT / "graph.html"
JSON_DST = OUT / "graph.json"
# Entries retire to a second file. An open entry that leans on a closed one is worth
# seeing, so the archive is read for names only — never rendered as a board.
ARCHIVE = SRC.parent / "backlog_closed.md"
# Read for its decisions only. The roadmap is where a decision's phase is written down,
# and the backlog is where the same decisions are cited — so it is the far bank of the
# bridge between the two documents, not a second backlog.
ROADMAP = SRC.parent / "ROADMAP.md"

PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "RESEARCH", "—"]
CUTOFF = "2026-08-10"          # BACKLOG_FORMAT.md §6 — envelope required from here on
RESOLUTIONS = {"fixed", "disproved", "moot", "superseded", "duplicate", "wontfix"}
# §2: the section an entry sits under *is* its status, and the heading duplicates it. Both
# the checker and the write verbs read this one map, so a `move` cannot write a status the
# checker would then refuse.
SECTION_STATUS_BASE = {
    "OPEN": "open",
    "IN PROGRESS": "in-progress",
    "DONE": "done-awaiting-observation",
    "BLOCKED": "open",
    "BACKLOG": "open",
    "IDEAS": "open",
}


def env_span(head):
    """Index range of the LAST top-level (...) group in a heading, or None.

    Walks back over balanced parens. `\\(([^()]*)\\)` cannot nest, so on a heading whose
    origin quotes someone verbatim and that quote carries its own parentheses, it returned
    the inner aside and lost the envelope entirely — reporting a complete entry as missing
    its priority and origin. Found by Warren on the first real entry written to the new
    standard; the seven-entry fixture had no nested parens to catch it.

    One function, two callers: the parser reads the envelope through it and the write verbs
    rewrite the status inside it. A second copy of this walk is a second place to fix.
    """
    s = head.rstrip()
    if not s.endswith(")"):
        return None
    depth = 0
    for k in range(len(s) - 1, -1, -1):
        if s[k] == ")":
            depth += 1
        elif s[k] == "(":
            depth -= 1
            if depth == 0:
                return k, len(s)
    return None


def section_status_map(all_lines):
    """§5 — a file with its own section names declares the mapping in front matter."""
    m = dict(SECTION_STATUS_BASE)
    fm = re.match(r"^---\n(.*?)\n---\n", "\n".join(all_lines), re.DOTALL)
    if fm:
        in_sections = False
        for raw in fm.group(1).split("\n"):
            if re.match(r"^sections:\s*$", raw):
                in_sections = True
                continue
            if in_sections:
                pair = re.match(r'^\s+"?([^":]+)"?\s*:\s*(\S+)\s*$', raw)
                if pair:
                    m[pair.group(1).strip().upper()] = pair.group(2)
                    continue
                in_sections = False
    return m

# An entry name is a SCREAMING-KEBAB sentence (§1). The same shape appearing in a body is
# a reference to another entry — that is the whole edge model, and it needs no new field.
NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+")   # leading run, not whole string:
# a heading may carry an aside — "SHUTDOWN-PIPE-DRAIN (original triage, S143)" — and that
# entry is still linkable by its name. Requiring the whole segment to match lost it, and
# with it every edge pointing at it.
TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
ADR_RE = re.compile(r"\bADR-\d{2,3}\b")
# A bare mention and a stated relation are different claims. Only the second is refusable
# evidence that a link rotted; the first is reported separately and quietly.
#
# `blocks ` used to sit in this list, meaning "A blocks B". Measured 2026-08-10: it fired
# 21 times across the backlog — the second most frequent marker here — and essentially
# never as a relation. It was reading "image content blocks", "the guard blocks". It now
# has to be followed by something name-shaped to count. `blocked by` needs no such guard.
#
# The phrase is kept, not just the fact that there was one. Measured 2026-08-10: of 223
# links in the graph, exactly three carried dependency semantics and the rest meant "see
# also" — so "what do I fix first" could not be asked of this data at all. Keeping the
# phrase costs nothing (it was already being matched and thrown away) and makes the
# question answerable the day somebody writes `blocked by [[NAME]]`.
REL_TYPES = [
    (r"blocked by",                        "blocked_by"),
    (r"blocks (?=[A-Z][A-Z0-9]*-)",        "blocks"),
    (r"depends on|builds on|needs (?=[A-Z][A-Z0-9]*-)", "depends_on"),
    (r"superseded by",                     "superseded_by"),
    (r"supersedes|subsumed by",            "supersedes"),
    (r"duplicate of",                      "duplicate"),
    (r"parent task",                       "parent"),
    (r"child task",                        "child"),
    (r"sibling of|same class as|same defect", "sibling"),
    (r"follow-?up to",                     "followup"),
    (r"cross-ref|related to|see also", "related"),
]
# `[[` is deliberately NOT in this list. It marks a reference, not a kind of one, and it
# always sits flush against the name — so as a competing phrase it won every time and
# "parent task: [[NAME]]" was recorded as a plain "related". Brackets are read separately.
REL_RX = [(re.compile(p, re.I), rel) for p, rel in REL_TYPES]
# Which relations answer "what do I fix first". The rest are "see also" — useful to read,
# useless to walk.
DEPENDENCY_RELS = {"blocked_by", "blocks", "depends_on", "parent", "child"}
# How far after a relation phrase a token still counts as covered by it. Bound the claim
# to the phrase's own neighbourhood: applying it to the whole line let a marker at
# character 257 vouch for a token at character 68, which is not corroboration by any
# reading. Found by Fable 5.
STATED_WINDOW = 80
# A bracketed name is a reference by construction, so it skips every heuristic below it —
# no stoplist, no shape guard, no relation-phrase test. Both external reviews (Fable 5,
# GLM 5.2, 2026-08-10) recommended this as the one convention worth adopting from the
# note-graph tools. Encouraged, never required: a scan that only saw brackets would trade
# visible false positives for invisible missing edges.
WIKILINK_RE = re.compile(r"\[\[\s*([^\]]+?)\s*\]\]")
# Vocabulary that merely looks like a name. Kept small on purpose: everything here is a
# word the standard itself defines, not a guess about what a token might have meant.
VOCAB = {"CRITICAL", "CRIT", "HIGH", "MEDIUM", "LOW", "RESEARCH", "NORMAL",
         "OPEN", "CLOSED", "DONE", "AWAITING", "OBSERVATION", "IN", "PROGRESS",
         "BLOCKED", "BACKLOG", "IDEAS", "FIXED", "DISPROVED", "MOOT", "SUPERSEDED",
         "DUPLICATE", "WONTFIX", "TODO", "WIP", "NOT", "AND", "OR", "THE"}
# Standards bodies and wire formats share our shape and are not entries.
FOREIGN_RE = re.compile(r"^(?:CVE|RFC|ISO|IEEE|UTF|SHA|AES|RSA|TLS|DTLS|HTTP|IPV|X)-")
# Line spans (L451-L462) and session spans (S111-S113) are how this backlog cites itself
# and its own history. They are never entry names.
RANGE_RE = re.compile(r"^[A-Z]{1,4}\d+-[A-Z]{0,4}\d+$")
PRI_CLASS = {"CRITICAL": "crit", "HIGH": "high", "MEDIUM": "med",
             "LOW": "low", "RESEARCH": "res", "—": "none"}

lines = SRC.read_text(encoding="utf-8-sig").split("\n")

section = None
entries = []
for i, line in enumerate(lines):
    m2 = re.match(r"^## (.+)", line)
    if m2:
        section = m2.group(1).strip()
        continue
    m3 = re.match(r"^### (.+)", line)
    if not (m3 and section):
        continue
    head = m3.group(1)

    name = re.split(r"[:—]", head)[0].strip()
    rest = head[len(name):].lstrip(" :—").strip()

    _sp = env_span(head)
    env = head.rstrip()[_sp[0] + 1:_sp[1] - 1] if _sp else ""

    # Markdown emphasis at the head of the envelope hid a real priority for three weeks:
    # `(**HIGH — Mike 2026-07-17 …` reads as no priority at all, because every match below
    # anchors at the first character. The value is present and only decoration covers it,
    # so decoration is stripped before reading rather than the entry being refused. New
    # entries are refused on it separately (§8) so nothing comes to rely on the tolerance.
    env_raw = env
    env = re.sub(r"^[\s*_`]+", "", env)

    # Priority is read only from that envelope. Scanning the whole heading once picked
    # up prose like "P0 needs Mike's verb" and turned a MEDIUM entry into a CRITICAL one.
    pri = "—"
    pri_typo = ""
    m = re.match(r"\s*(CRIT|CRITICAL|HIGH|MEDIUM|LOW|RESEARCH|NORMAL)\b", env)
    if m:
        pri = {"CRIT": "CRITICAL", "NORMAL": "MEDIUM"}.get(m.group(1), m.group(1))
    else:
        # A misspelled priority and an absent one are different mistakes and deserve
        # different messages: "MED" is a typo to correct, an empty slot is a field to fill.
        first = re.match(r"\s*([A-Za-z]{3,12})\s*[,)]", env)
        if first:
            pri_typo = first.group(1)

    # STATUS rides in the same trailing block as the priority (docs/BACKLOG_FORMAT.md §2).
    # It duplicates the section on purpose: the duplication is what lets --check catch an
    # entry whose heading and section disagree, and what keeps a retrieval chunk legible
    # once it has been torn away from its section heading.
    # Status is read from the metadata part of the envelope only — everything before the
    # em dash. Searching the whole envelope read the word "closed" out of a quoted origin
    # ("Voting already closed (approved)") and reported a status the author never wrote.
    # Warren flagged it twice before it was fixed.
    meta = env.split("—", 1)[0]
    sm = re.search(r"\b(open|in-progress|done-awaiting-observation|closed)\b", meta)
    status = sm.group(1) if sm else ""

    # Origin is whatever follows the em dash inside the envelope: who raised it.
    om = re.search(r"—\s*(.+)$", env, re.DOTALL)
    origin = om.group(1).strip() if om else ""

    dm = re.search(r"(20\d\d-\d\d-\d\d)", head)
    when = dm.group(1) if dm else ""

    # Strip the trailing envelope so the description reads clean. Uses the balanced-paren
    # result above, so a quoted aside inside the origin no longer survives into the desc.
    desc = rest
    if env and desc.rstrip().endswith(f"({env})"):
        desc = desc.rstrip()[: -len(env) - 2]
    elif pri != "—":
        desc = re.sub(r"\s*\((?:CRIT|CRITICAL|HIGH|MEDIUM|LOW|RESEARCH|NORMAL)[^)]*\)\s*$", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    # First body bullet — the entry's own opening line, not a summary I invent.
    first = ""
    for j in range(i + 1, min(i + 8, len(lines))):
        s = lines[j].strip()
        if s.startswith("###") or s.startswith("##"):
            break
        if s.startswith("- "):
            first = re.sub(r"\s+", " ", s[2:]).strip()
            break

    # The body runs to the next heading, not to a fixed window. A 40-line window silently
    # truncated long entries, so a closure line — or a reference — living past line 40 was
    # invisible to every rule below.
    end = i + 1
    while end < len(lines) and not re.match(r"^#{2,3} ", lines[end]):
        end += 1

    _nm = NAME_RE.match(name)

    entries.append({
        "ref": _nm.group(0) if _nm else "",
        "section": section, "name": name, "desc": desc, "pri": pri, "pri_typo": pri_typo,
        "when": when, "first": first, "line": i + 1,
        "status": status, "origin": origin, "head": head, "env": env, "env_raw": env_raw,
        "body": "\n".join(lines[i + 1:end]),
        "is_name": bool(_nm),
        "done": "✅" in head, "part": "🟡" in head,
    })

# ----------------------------------------------------------------------- roadmap
# "Where are the links between ROADMAP, ADR and backlog?" — Mike, 2026-08-10. Half of that
# chain already existed: an entry naming ADR-024 is joined to the decision. The other half
# was sitting unread in ROADMAP.md, which says which phase each decision belongs to and,
# on its Dependencies lines, which decision builds on which. Both halves are prose nobody
# has to start writing; the parser just had to be pointed at the second file.
def read_roadmap(path):
    """(phase -> ADR) coverage and (ADR -> ADR) dependencies, both as written."""
    covers, deps, phases = [], [], {}
    if not path.exists():
        return covers, deps, phases
    section = None
    for i, ln in enumerate(path.read_text(encoding="utf-8-sig").split("\n"), 1):
        h = re.match(r"^(#{2,4}) (.+)", ln)
        if h:
            section = re.sub(r"\s*[—-]\s*(COMPLETE|DONE).*$", "", h.group(2)).strip()
            phases.setdefault(section, i)
            continue
        if not section:
            continue
        found = list(ADR_RE.finditer(ln))
        for m in found:
            covers.append((section, m.group(0), "covers"))
        # "ADR-024 builds on ADR-010, ADR-018, ADR-019" — subject is the last decision
        # named before the phrase, targets are the ones named after it.
        for rx, rel in REL_RX:
            if rel == "related":
                continue
            for pm in rx.finditer(ln):
                # Both ends have to be near the phrase. Without a limit on the subject the
                # rule read "**Memory Upgrade (ADR-010)** — … model_swap superseded by
                # ADR-018" as a claim about ADR-010, when the sentence is about model_swap.
                before = [m for m in found
                          if m.end() <= pm.start() and pm.start() - m.end() <= 40]
                after = [m for m in found if m.start() >= pm.end()
                         and m.start() - pm.end() <= 120]
                if before and after:
                    for t in after:
                        deps.append((before[-1].group(0), t.group(0), rel))
    return covers, deps, phases


road_covers, road_deps, road_phases = read_roadmap(ROADMAP)


# --------------------------------------------------------------------- freshness
# Both artefacts are gitignored, so nothing in git status ever says they went stale.
# The one signal that exists is their age against the source; it is printed, and that is
# all — §8 of the standard says this script never rewrites a file, and a checker that
# quietly rebuilt the board to hide its own warning would be exactly that.
def _ago(sec: float) -> str:
    sec = int(abs(sec))
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60} min"
    if sec < 172800:
        return f"{sec // 3600} h"
    return f"{sec // 86400} d"


def freshness():
    if SRC != ROOT / "backlog.md":
        return [f"freshness: not checked — reading {SRC}, not this project's backlog.md"]
    out, src_m = [], SRC.stat().st_mtime
    for p in (DST, GRAPH_DST, JSON_DST):
        if not p.exists():
            out.append(f"STALE   {p.name} has never been built")
        elif src_m - p.stat().st_mtime > 60:
            out.append(f"STALE   {p.name} is {_ago(src_m - p.stat().st_mtime)} older than "
                       f"{SRC.name} — rebuild: uv run python tools/backlog/build.py")
        else:
            out.append(f"fresh   {p.name} (built {_ago(time.time() - p.stat().st_mtime)} ago)")
    return out


# --------------------------------------------------------------------- references
# One pass, two consumers: the stale-reference report in --check and the graph at the end.
# No new field is asked of anyone — every edge here is already written in the prose, which
# is why 78 of 217 entries have one on the day this shipped and none had to be edited.
archive_names = set()
archive_heads, archive_unparsed = 0, []
if ARCHIVE.exists():
    for h in re.findall(r"^### (.+)$", ARCHIVE.read_text(encoding="utf-8-sig"), re.M):
        archive_heads += 1
        # The archive strikes entries through — `### ~~NAME~~ — CLOSED S191`. Matching the
        # raw heading lost every one of those names, so an open entry citing a struck-out
        # one was reported as a broken reference instead of drawn as an archive link. This
        # is the drift the counter below was added to catch, found by the counter on its
        # first run.
        m = NAME_RE.match(re.split(r"[:—]", h.replace("~~", "").replace("**", ""))[0]
                          .strip().strip("`"))
        if m:
            archive_names.add(m.group(0))
        else:
            # The archive is parsed by a second, simpler path than the live file, and it is
            # edited under different circumstances. If its headings drift, entry→archive
            # links evaporate with nothing to show for it — so the misses are counted.
            archive_unparsed.append(h[:70])

live = {e["ref"]: e for e in entries if e["is_name"]}
known = set(live) | archive_names

edges = []        # (src, dst, rel)          — both entries live in this file
arc_edges = []    # (src, archived, rel)     — leans on something already closed
adr_edges = []    # (src, ADR-0xx, rel)      — the bridge to ROADMAP, already in the prose
road_edges = []   # (phase, ADR-0xx, rel)    — the other half of that bridge
short_refs = []   # (src, token, full, line) — a prefix of exactly one real name
dangling = []     # (src, token, line, strong) — resolves to nothing at all


def _vocabulary(tok: str) -> bool:
    return all(part in VOCAB for part in tok.split("-"))


def _ambiguous(tok: str) -> bool:
    """True for shapes a model name wears as readily as an entry name.

    GLM-5, BGE-M3, D-PC and MEM-3 are indistinguishable by shape from the legacy
    identifiers this report is meant to find (MENTION-1, DEDUP-1). Rather than guess,
    such a token is only counted when the line states a relation — corroboration
    supplied by the author, not by this script.
    """
    return any(len(p) <= 2 or p[0].isdigit() for p in tok.split("-"))


for e in entries:
    if not e["is_name"]:
        continue
    src = e["ref"]
    hits = {}                        # token -> [stated relation?, line, bracketed?]
    for off, ln in enumerate(e["body"].split("\n")):
        # An entry that writes *about* broken references quotes the broken names, and the
        # report then flags the entry that exists to fix them — permanently, since the
        # examples never go away. One escape hatch, invisible in rendered markdown, and
        # deliberately per-line so it cannot silence a whole entry by accident.
        # Backticks were tried as the signal first and rejected by measurement: 53 of the
        # 123 mentions of live entry names are inside code spans, so that rule would have
        # thrown away nearly half the real graph.
        if "<!-- no-refs -->" in ln:
            continue
        marks = [(m.end(), rel) for rx, rel in REL_RX for m in rx.finditer(ln)]
        exact = {t for w in WIKILINK_RE.findall(ln) for t in TOKEN_RE.findall(w)}
        for m in TOKEN_RE.finditer(ln):
            tok = m.group(0)
            if tok == src:
                continue
            # Corroboration has to sit in front of the token it vouches for, not merely
            # somewhere on the same line. The nearest preceding phrase wins, so on
            # "blocked by X, related to Y" each name keeps its own relation.
            near = [(m.start() - end, rel) for end, rel in marks
                    if 0 <= m.start() - end <= STATED_WINDOW]
            rel = min(near)[1] if near else ""
            hit = hits.setdefault(tok, ["", e["line"] + 1 + off, False])
            hit[0] = hit[0] or rel
            hit[2] = hit[2] or tok in exact
    for tok, (rel, ln_no, bracketed) in sorted(hits.items()):
        stated = bool(rel) or bracketed
        # A bracketed name with no phrase in front of it is still an assertion — the author
        # marked it up as a link — so it records as "related" rather than as a bare mention.
        kind = rel or ("related" if bracketed else "mention")
        # Entry first, ADR second: ADR-022 and ADR-024 are themselves entries here, and
        # counting them as decision nodes would split one node into two.
        if tok in live:
            edges.append((src, tok, kind))
        elif ADR_RE.fullmatch(tok):
            adr_edges.append((src, tok, kind))
        elif tok in archive_names:
            arc_edges.append((src, tok, kind))
        elif bracketed:
            # Written as [[NAME]] and resolving to nothing: the author asserted a link and
            # the target is gone. No heuristic gets to soften that.
            dangling.append((src, tok, ln_no, True))
        elif (_vocabulary(tok) or FOREIGN_RE.match(tok) or RANGE_RE.match(tok)
                or tok.startswith("ADR-")):
            pass                                  # ADR-NNN is a placeholder, not a link
        elif _ambiguous(tok) and not stated:
            pass
        else:
            # A token that is the head of exactly one real name is a shortened reference,
            # not a broken one. Naming both as "dangling" would bury the real breakage.
            full = [k for k in known if k.startswith(tok + "-") and k != src]
            if len(full) == 1:
                short_refs.append((src, tok, full[0], ln_no))
            else:
                dangling.append((src, tok, ln_no, stated))

# Only decisions the backlog actually cites get a phase drawn behind them: a roadmap
# section listing twenty ADRs nobody has an entry for would bury the graph in scaffolding.
# Computed here rather than with the rest of the graph so --check can report both the raw
# extraction and what survives the filter — reporting only the raw count let a reader
# expect 46 new links where 22 are drawn (Warren, 2026-08-10).
cited_adr = {b for _, b, _ in adr_edges}
road_edges = [(p, a, r) for p, a, r in road_covers if a in cited_adr]
adr_dep_edges = [(a, b, r) for a, b, r in road_deps if a in cited_adr or b in cited_adr]
# One list, two consumers, one number: the summary line and graph.json must not be able to
# disagree about how many dependencies exist.
dependencies = sorted({(a, b, r) for a, b, r in edges + arc_edges + adr_edges + adr_dep_edges
                       if r in DEPENDENCY_RELS})

# ------------------------------------------------------------------------ verbs
# ADR-039 items 1, 5 and 7. Four mutations that write the file, then re-run this same
# checker against the result and refuse to keep the write if the result carries a refusal.
# Warnings never block: the live file carries 99 of them, and a `close` that recites them
# every time is how people learn to stop reading the output.
#
# The script is convenience, not the guarantee (item 2). The file still opens in any
# editor; what these verbs buy is that the common path is correct by construction, and
# that a rename cannot leave the inbound references behind.

def _flag(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def _die(*msg):
    for m in msg:
        print(m)
    sys.exit(2)


def _find(name):
    hits = [e for e in entries if e["ref"] == name or e["name"] == name]
    if not hits:
        near = [e["ref"] for e in entries if e["ref"] and name.upper() in e["ref"]][:5]
        _die(f"no entry named «{name}» in {SRC.name}.",
             *([f"  closest by name: {', '.join(near)}"] if near else []))
    if len(hits) > 1:
        _die(f"«{name}» matches {len(hits)} entries (lines "
             f"{', '.join(str(h['line']) for h in hits)}). Two entries under one name is "
             f"itself a refusal — fix the duplicate before moving either.")
    return hits[0]


def _span(e):
    """The entry's own lines: heading through the last non-blank before the next heading.

    Blank separators belong to the file's layout, not to the entry, so they stay behind
    when the block moves and are not duplicated where it lands.
    """
    start = e["line"] - 1
    end = start + 1
    while end < len(lines) and not re.match(r"^#{2,3} ", lines[end]):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _body_of(block):
    """Entry lines without the heading and without the blank that follows it."""
    body = block[1:]
    while body and not body[0].strip():
        body.pop(0)
    return body


def _rewrite_status(head, status):
    """Put `status` into the heading's envelope, leaving the rest of it byte-identical.

    §2 says the section is the status and the heading duplicates it, so a verb that moves
    an entry between sections has to write both — otherwise the very check that catches
    drift would fire on the move that was supposed to be correct.
    """
    sp = env_span(head)
    if not sp:
        return head
    s = head.rstrip()
    inner = s[sp[0] + 1:sp[1] - 1]
    meta, sep, rest = inner.partition("—")
    new_meta, n = re.subn(r"\b(open|in-progress|done-awaiting-observation|closed)\b",
                          status, meta, count=1)
    if not n:
        # No status token yet (a legacy heading). Write it after the priority if there is
        # one, and at the front if there is not — never silently leave it absent.
        m = re.match(r"^(\s*(?:CRIT|CRITICAL|HIGH|MEDIUM|LOW|RESEARCH|NORMAL))\b", meta)
        new_meta = (f"{m.group(1)}, {status}{meta[m.end():]}" if m
                    else f"{status}, {meta.lstrip()}")
    return s[:sp[0]] + "(" + new_meta + sep + rest + ")"


def _section_at(all_lines, wanted):
    """(actual heading text, index to insert at) for the section whose name starts with
    `wanted`. New entries land at the top of their section: newest first is how every
    reader of this file already scans it."""
    for i, ln in enumerate(all_lines):
        m = re.match(r"^## (.+)", ln)
        if m and m.group(1).strip().upper().startswith(wanted.strip().upper()):
            j = i + 1
            while j < len(all_lines) and not all_lines[j].strip():
                j += 1
            return m.group(1).strip(), j
    have = [re.match(r"^## (.+)", ln).group(1).strip()
            for ln in all_lines if re.match(r"^## (.+)", ln)]
    _die(f"no section starting «{wanted}» in {SRC.name}.",
         "  sections: " + " · ".join(have))


def _validate(src_text, arc_text):
    """Run this same script's --check over the candidate files, in a scratch directory.

    Validating a copy rather than the real file is the whole point: a mutation that would
    introduce a refusal never reaches disk, so the file on disk is never briefly invalid.
    """
    import shutil, subprocess, tempfile          # only this path pays for them
    tmp = Path(tempfile.mkdtemp(prefix="backlog-verb-"))
    try:
        (tmp / SRC.name).write_text(src_text, encoding="utf-8")
        (tmp / ARCHIVE.name).write_text(arc_text, encoding="utf-8")
        if ROADMAP.exists():
            shutil.copyfile(ROADMAP, tmp / ROADMAP.name)
        # The decisions come along even though no verb touches them: without them the
        # validation run reports a different warning count than a plain --check on the
        # same content, and a number that moves for no reason is a number nobody trusts.
        _dec = SRC.parent / "docs" / "decisions"
        if _dec.is_dir():
            shutil.copytree(_dec, tmp / "docs" / "decisions")
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                            "--check", str(tmp / SRC.name)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _commit(src_text, arc_text, announcement):
    code, out = _validate(src_text, arc_text)
    if code != 0:
        print("refused — the result would not pass --check, so nothing was written:\n")
        keep = False
        for ln in out.split("\n"):
            if ln.startswith("REFUSE"):
                keep = True
            elif ln and not ln.startswith(" "):
                keep = False
            if keep:
                print("  " + ln)
        sys.exit(1)
    if "--dry-run" in sys.argv:
        print("dry run — validated, nothing written.")
        print("ANNOUNCE  " + announcement)
        return
    SRC.write_text(src_text, encoding="utf-8")
    ARCHIVE.write_text(arc_text, encoding="utf-8")
    summary = next((ln for ln in out.split("\n") if " entries · " in ln), "")
    print(f"written   {SRC.name}" + (f" + {ARCHIVE.name}" if arc_text != _ARC_TEXT else ""))
    if summary:
        print("check     " + summary)
    # Item 7: the announcement is a text line in the closure-line grammar and nothing else.
    # No JSON payload, deliberately — a payload is an invitation to grow these lines into
    # the sync channel ADR-039 declines to build. Sending it is the caller's job.
    print("ANNOUNCE  " + announcement)
    print("rebuild   uv run python tools/backlog/build.py")


_ARC_TEXT = ARCHIVE.read_text(encoding="utf-8-sig") if ARCHIVE.exists() else ""
_TODAY = date.today().isoformat()

if VERB:
    _when = _flag("date", _TODAY)
    _by = (_flag("by") or "").strip()
    _smap = section_status_map(lines)

    def _status_for(sec_name):
        for key, st in _smap.items():
            if sec_name.upper().startswith(key):
                return st
        _die(f"section «{sec_name}» maps to no status — declare it in front matter (§5).")

if VERB == "close":
    if not ARGS:
        _die("usage: build.py close NAME --session=S72 --resolution=fixed --evidence='…' "
             "[--by=CC] [--date=YYYY-MM-DD] [--dry-run]")
    e = _find(ARGS[0])
    res = (_flag("resolution") or "").strip().lower()
    if res not in RESOLUTIONS:
        _die(f"--resolution must be one of {'/'.join(sorted(RESOLUTIONS))} (§3); "
             f"got «{_flag('resolution') or ''}».",
             "The resolution says why the entry left, which is the thing 83 of 85 archived "
             "closure lines do not say.")
    ev = (_flag("evidence") or "").strip()
    if not ev:
        _die("--evidence is mandatory (§3), and its type follows the resolution: a commit "
             "hash and the observation for `fixed`; the measurement that falsified it for "
             "`disproved`; what changed for `moot`; the id or ADR for `superseded` and "
             "`duplicate`; Mike's verb for `wontfix`.")
    ses = (_flag("session") or "").strip()
    if not ses:
        _die("--session is mandatory: the closure line opens with it (§3).")
    ses = ses if ses.upper().startswith("S") else "S" + ses
    closure = (f"**Closed:** {ses.upper()} · {_when} · {res} · {ev}"
               + (f" · closed by {_by}" if _by else ""))

    start, end = _span(e)
    block = lines[start:end]
    # An archived entry keeps its heading, but its status token now lies: it sits in a date
    # batch, not in a status section, and retrieval hands an agent the heading without
    # either. Writing `closed` into it is the same §2 argument that put status there.
    head_closed = _rewrite_status(block[0], "closed")
    new_lines = lines[:start] + lines[end:]

    arc = _ARC_TEXT.split("\n") if _ARC_TEXT else ["# Closed entries", ""]
    idx = next((i for i, ln in enumerate(arc)
                if re.match(rf"^## {re.escape(_when)}\b", ln)), -1)
    if idx < 0:
        top = 0
        while top < len(arc) and not arc[top].startswith("## "):
            top += 1
        arc[top:top] = [f"## {_when} — closed with build.py", ""]
        idx = top
    j = idx + 1
    while j < len(arc) and not arc[j].strip():
        j += 1
    arc[j:j] = [head_closed, "", closure, ""] + _body_of(block) + [""]

    _commit("\n".join(new_lines), "\n".join(arc),
            f"close {e['ref'] or e['name']} · {res} · {ev[:60]}"
            + (f" · {_by}" if _by else ""))
    sys.exit(0)

if VERB == "move":
    if not ARGS or not _flag("to"):
        _die("usage: build.py move NAME --to='IN PROGRESS' [--by=CC] [--dry-run]")
    e = _find(ARGS[0])
    start, end = _span(e)
    block = lines[start:end]
    sec_name, _ = _section_at(lines, _flag("to"))
    if e["section"].upper() == sec_name.upper():
        _die(f"«{e['ref'] or e['name']}» already sits under «{sec_name}».")
    status = _status_for(sec_name)
    block[0] = _rewrite_status(block[0], status)
    # Item 5: people are recorded as events. The line is appended and never replaced, so
    # what the file holds is a history of who picked it up rather than a field that goes
    # stale the way `Updated:` did. The current assignee is the last such line, derived.
    if _by:
        block = block + [f"- **taken:** {_by} · {_when}"]
    rest = lines[:start] + lines[end:]
    _, at_idx = _section_at(rest, sec_name)
    rest[at_idx:at_idx] = block + [""]
    _commit("\n".join(rest), _ARC_TEXT,
            f"move {e['ref'] or e['name']} · {status}" + (f" · {_by}" if _by else ""))
    sys.exit(0)

if VERB == "rename":
    if len(ARGS) < 2:
        _die("usage: build.py rename OLD-NAME NEW-NAME [--dry-run]")
    old, new = ARGS[0], ARGS[1]
    e = _find(old)
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+", new):
        _die(f"«{new}» is not a name (§1): SCREAMING-KEBAB, at least two segments, "
             f"no lowercase and no punctuation inside the name.")
    if any(x["ref"] == new for x in entries):
        _die(f"«{new}» is already an entry — a rename that collides is a merge, and this "
             f"verb will not guess which body survives.")
    # The reference model is the token itself, so a rename that does not rewrite the
    # inbound references manufactures exactly the dangling links this tool reports. Both
    # files, one edit — the archive cites live entries too.
    tok = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(old)}(?![A-Za-z0-9-])")
    hits = sum(len(tok.findall(ln)) for ln in lines) + len(tok.findall(_ARC_TEXT))
    new_lines = [tok.sub(new, ln) for ln in lines]
    start, end = _span(e)
    # The trace quotes a name that no longer exists, which is precisely the shape this
    # tool reports as a dangling reference — so the line carries the standard's own
    # opt-out. Without it the rename verb manufactures one stale reference per rename.
    new_lines.insert(end, f"- **Renamed** {_when}: was `{old}`. Inbound references were "
                          f"rewritten in the same edit; a reference to the old name in a "
                          f"commit message or a chat log will not resolve. <!-- no-refs -->")
    _commit("\n".join(new_lines), tok.sub(new, _ARC_TEXT),
            f"rename {old} → {new} · {hits} references rewritten"
            + (f" · {_by}" if _by else ""))
    sys.exit(0)

if VERB == "add":
    if not ARGS:
        _die("usage: build.py add NAME --desc='claim, not topic' --priority=HIGH "
             "--origin=\"Mike: '…'\" [--section=OPEN] [--observed='…'] "
             "[--first-step='…'] [--dry-run]")
    name = ARGS[0]
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+", name):
        _die(f"«{name}» is not a name (§1): SCREAMING-KEBAB, at least two segments.",
             "Names are claims, not topics: FILE-NOTE-HAS-NO-TIME-AND-NO-AUTHOR, not "
             "FILE-NOTE-BUG. And keep counts out of it — a count in a handle starts lying "
             "the day the measurement moves.")
    if any(x["ref"] == name for x in entries):
        _die(f"«{name}» is already an entry (line {_find(name)['line']}).")
    desc = (_flag("desc") or (ARGS[1] if len(ARGS) > 1 else "")).strip()
    if not desc:
        _die("--desc is mandatory (§1): one line, a claim rather than a topic.")
    pri = (_flag("priority") or "").strip().upper()
    pri = {"CRIT": "CRITICAL", "NORMAL": "MEDIUM", "MED": "MEDIUM"}.get(pri, pri)
    if pri not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "RESEARCH"):
        _die(f"--priority must be CRITICAL/HIGH/MEDIUM/LOW/RESEARCH; got «{pri}».")
    origin = (_flag("origin") or "").strip()
    if not origin:
        _die("--origin is mandatory (§1): who raised it, in their words when there are "
             "words. An entry with no origin cannot be taken back to the person who "
             "wanted it.")
    sec_name, _ = _section_at(lines, _flag("section", "OPEN"))
    status = _status_for(sec_name)
    body = []
    if _flag("observed"):
        body.append(f"- **Observed.** {_flag('observed')}")
    if _flag("inferred"):
        body.append(f"- **Inferred.** {_flag('inferred')}")
    if _flag("first-step"):
        body.append(f"- **First step:** {_flag('first-step')}")
    if _flag("body"):
        body.extend(_flag("body").split("\\n"))
    if not body:
        _die("an entry with no body is a title. Give at least --observed: what was seen, "
             "with a file:line, a log line or a measurement.")
    head = f"### {name}: {desc} ({pri}, {status}, {_when} — {origin})"
    rest = list(lines)
    _, at_idx = _section_at(rest, sec_name)
    rest[at_idx:at_idx] = [head, ""] + body + [""]
    _commit("\n".join(rest), _ARC_TEXT,
            f"add {name} · {pri.lower()} · {sec_name.lower()}"
            + (f" · {_by}" if _by else ""))
    sys.exit(0)

if "--check" in sys.argv:
    # Validate the file against docs/BACKLOG_FORMAT.md. Reports, never rewrites:
    # every automated classifier in this repo's history has documented its own false
    # positives, so the script's job is to point, not to edit.
    # BACKLOG_FORMAT.md §5 promises that a file with its own section names declares the
    # mapping in front matter rather than renaming. Without this the checker only ever
    # worked for one project, which is not what the standard says.
    SECTION_STATUS = section_status_map(lines)

    def canonical(sec: str) -> str | None:
        for key, st in SECTION_STATUS.items():
            if sec.upper().startswith(key):
                return st
        return None

    # The non-English rule cannot be proved from the markdown fixture: an entry
    # demonstrating it would have to carry the very thing these files must not carry.
    # Assert it here instead, against a string built from codepoint escapes, so the rule
    # is watched to fire without a single non-Latin character living in the repository.
    _NON_LATIN = "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"   # "description", Cyrillic
    assert re.search(r"[\u0400-\u04ff]", _NON_LATIN), "non-English detector is broken"
    assert not re.search(r"[\u0400-\u04ff]", "plain ascii description"), \
        "non-English detector fires on Latin text"

    refusals, warnings = [], []

    def at(e, msg, bucket):
        bucket.append(f"{SRC.name}:{e['line']}  {e['name']}\n    {msg}")

    seen = {}
    for e in entries:
        sec_status = canonical(e["section"])
        # Migration is new-entries-only (§7), so a refusal on legacy content would leave
        # exit 1 permanently on and mean nothing. Post-cutoff entries are refused;
        # everything older warns, and the warning count is the migration debt meter.
        new = bool(e["when"]) and e["when"] >= CUTOFF
        hard = refusals if new else warnings

        # ADR-039: refusals split by defect class, not only by date. A malformed or
        # duplicated name corrupts the graph for every reader today, whatever year the
        # entry was written in; envelope incompleteness is migration debt and stays
        # date-gated. `structural` is therefore not `hard`.
        structural = refusals

        if sec_status is None:
            at(e, f"section «{e['section']}» is not one of the recognised lifecycle "
                  f"sections and no front-matter mapping covers it (§2, §5)", structural)

        # Keyed on the NAME_RE run, not on the whole parsed name. Keyed on the name, an
        # aside on one copy — `NAME (original triage, S143 …)` — made two copies of one
        # entry compare unequal, and the duplicate went unseen for months.
        key = e["ref"] or e["name"]
        if key in seen:
            at(e, f"duplicate name «{key}» — already used at {SRC.name}:{seen[key]}", structural)
        else:
            seen[key] = e["line"]

        # A name the parser cannot round-trip. Two shapes, both measured live:
        #   NAME (aside …)   — the aside survives into the name and hides duplicates
        #   NAME:1-MORE-NAME — the split eats the tail, and references to the full name
        #                      resolve to nothing
        # A heading with no name run at all is a rubric, not a malformed entry: it warns,
        # because the sweep that turns rubrics into entries is a different job.
        if e["ref"]:
            tail = e["head"][len(e["ref"]):]
            if e["name"] != e["ref"]:
                at(e, f"name carries more than the name — parsed «{e['name']}», "
                      f"which is «{e['ref']}» plus an aside. Move the aside after a colon "
                      f"so it lands in the description (§1)", structural)
            elif tail.startswith(":") and not tail[1:2].isspace():
                at(e, f"colon inside the name — «{e['head'][:60]}». The envelope uses «:» "
                      f"to separate name from description, so the name parses as "
                      f"«{e['ref']}» and every reference to the full name resolves to "
                      f"nothing (§1)", structural)

        if e["status"] and sec_status and e["status"] != sec_status:
            at(e, f"heading says status «{e['status']}» but the section implies "
                  f"«{sec_status}» — one of the two is wrong", hard)

        # A closure line anywhere in the body must carry a known resolution (§3).
        for cl in re.findall(r"\*\*Closed:\*\*(.+)", e["body"]):
            tokens = {t.strip().lower() for t in cl.split("·")}
            if not tokens & RESOLUTIONS:
                at(e, f"closure line carries no known resolution "
                      f"({'/'.join(sorted(RESOLUTIONS))}): «{cl.strip()[:70]}»", hard)

        if new and re.match(r"^\s*[*_`]", e["env_raw"]):
            at(e, "markdown emphasis at the start of the envelope — the priority is read "
                  "from the first character, so «(**HIGH …» reads as no priority at all. "
                  "The value is recovered on read, but new entries write it plain", refusals)

        if new:
            missing = [f for f, v in (("priority", e["pri"] != "—" or e["pri_typo"]),
                                      ("status", e["status"]),
                                      ("origin", e["origin"])) if not v]
            if e["pri_typo"]:
                at(e, f"priority token «{e['pri_typo']}» is not in the vocabulary "
                      f"(CRITICAL / HIGH / MEDIUM / LOW / RESEARCH) — a misspelled "
                      f"priority reads as no priority at all", refusals)
            if missing:
                # Name the expected shape. "missing origin" on its own reads as a puzzle
                # when the real cause is a hyphen where the envelope wants an em dash.
                at(e, f"entry dated {e['when']} (on/after the {CUTOFF} cutoff) is missing: "
                      f"{', '.join(missing)}. Expected "
                      f"(PRIORITY, STATUS, YYYY-MM-DD — origin), with an em dash "
                      f"before the origin", refusals)
            # A refusal, not a warning: Mike, 2026-08-10 - "Cyrillic in new entries is a
            # refusal". Only the name and description are checked. The origin quotes whoever
            # raised the entry in their own words (§1), and those words are often Russian -
            # checking them would have the standard contradict itself.
            checked = e["head"][: len(e["head"]) - len(e["env"])] if e["env"] else e["head"]
            if re.search(r"[\u0400-\u04ff]", checked):
                at(e, "Cyrillic in the name or description of a post-cutoff entry — new "
                      "entries are written in English (§6); the origin quote is exempt",
                   refusals)
        else:
            missing = [f for f, v in (("priority", e["pri"] != "—"),
                                      ("date", e["when"]),
                                      ("origin", e["origin"])) if not v]
            if missing:
                at(e, f"missing {', '.join(missing)} (pre-cutoff entry, not required)", warnings)

    # ADR-039 item 6: one validator, two formats. The decisions sit next to the backlog and
    # are cited by 86 of its links, and nothing checked them at all — 26 of 39 carry no
    # front matter and the drift was invisible. The record boundary differs (a whole file
    # per decision rather than a heading), so the metadata placement differs: ADRs keep
    # per-file front matter, the backlog keeps its heading envelope. The rules are the same
    # rules: required fields, a closed vocabulary, and references that resolve.
    DECISIONS = SRC.parent / "docs" / "decisions"
    # `implemented` is not in TEMPLATE.md's lifecycle and is written in two files anyway.
    # It says something the enum cannot — accepted-and-built, as against accepted-and-owed
    # — so the vocabulary is widened to match what the corpus means rather than the corpus
    # rewritten to match a list. TEMPLATE.md carries the same five words.
    ADR_STATUS = {"proposed", "rejected", "accepted", "deprecated", "implemented"}
    # Front matter is required from this number on, the way the envelope is required from a
    # date on: 001–026 predate the template, 027 is where it starts. A rule that refuses
    # what the migration policy forbids rewriting is a permanently red light.
    ADR_FM_FROM = 27
    adr_meta, adr_nums, adr_dupes = {}, set(), []

    def _adr_fm(text):
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
        if not m:
            return None
        fm = {}
        for raw in m.group(1).split("\n"):
            kv = re.match(r"^([a-z_]+):\s*(.*)$", raw)
            if kv:
                fm[kv.group(1)] = kv.group(2).strip()
        return fm

    adr_no_fm = 0
    if DECISIONS.is_dir():
        _files = sorted(DECISIONS.glob("[0-9]*.md"))
        for f in _files:
            m = re.match(r"^(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$", f.name)
            if not m:
                refusals.append(f"{f.name}\n    filename is not NNN-kebab-title.md, so the "
                                f"decision's number cannot be read from where it is cited")
                continue
            num = int(m.group(1))
            if num in adr_nums:
                adr_dupes.append((adr_meta[num][0].name, f.name))
            adr_nums.add(num)
            adr_meta[num] = (f, _adr_fm(f.read_text(encoding="utf-8-sig")))
        for first, second in adr_dupes:
            refusals.append(f"{second}  ADR-{first[:3]}\n    also claimed by {first} — two "
                            f"files under one number means every reference to it resolves "
                            f"to whichever is read first, and only one of them is checked")

        for num, (f, fm) in sorted(adr_meta.items()):
            tag = f"ADR-{num:03d}"
            bucket = refusals if num >= ADR_FM_FROM else warnings
            text = f.read_text(encoding="utf-8-sig")

            def _adr(msg, into=None):
                (into if into is not None else bucket).append(f"{f.name}  {tag}\n    {msg}")

            if fm is None:
                adr_no_fm += 1
                _adr(f"no YAML front matter. Required from {ADR_FM_FROM:03d} on "
                     f"(TEMPLATE.md): adr, title, status, date")
            else:
                missing = [k for k in ("adr", "title", "status", "date") if not fm.get(k)]
                if missing:
                    _adr(f"front matter is missing {', '.join(missing)}")
                if fm.get("adr") and fm["adr"].strip().lstrip("0") != str(num):
                    # Structural, and refused whatever the number: the file says it is one
                    # decision and its name says another, so a citation lands on neither.
                    _adr(f"front matter says adr: {fm['adr']} but the filename says "
                         f"{num:03d} — one of the two is wrong", refusals)
                st = (fm.get("status") or "").strip().strip('"').lower()
                head = st.split()[0] if st else ""
                if head and not (head in ADR_STATUS or re.fullmatch(r"superseded-by-\d{3}", head)):
                    _adr(f"status «{head}» is outside the vocabulary "
                         f"({'/'.join(sorted(ADR_STATUS))}/superseded-by-NNN)")
                elif st != head:
                    _adr(f"status carries a qualifier — «{st[:60]}». The field is the "
                         f"machine-readable one; put the qualification in the body", warnings)
                if fm.get("date") and not re.fullmatch(r"20\d\d-\d\d-\d\d", fm["date"].strip()):
                    _adr(f"date «{fm['date'][:20]}» is not YYYY-MM-DD")
                for key in ("depends_on", "related", "supersedes"):
                    for ref in re.findall(r"ADR-(\d{2,3})", fm.get(key, "")):
                        if int(ref) not in adr_nums:
                            _adr(f"{key} points at ADR-{int(ref):03d}, which is not a file "
                                 f"in {DECISIONS.name}/", warnings)
            if not re.search(r"^## Decision\b", text, re.M):
                _adr("no `## Decision` section — Context + Decision is the minimum an ADR "
                     "has to carry (TEMPLATE.md, RFC 3)")

    for line in refusals:
        print(f"REFUSE  {line}")
    for line in warnings:
        print(f"warn    {line}")

    # A reference that resolves to nothing is the same defect the standard exists to
    # prevent, one level up: the document points at something that is not there. It is
    # reported, never refused — the entries predate the rule, and the classifier's own
    # false-positive shape (shortened names) is printed next to it rather than hidden.
    if dangling or short_refs:
        print("\n-- references that resolve to nothing --")
        for src, tok, ln, stated in sorted(dangling, key=lambda d: (not d[3], d[0])):
            kind = "stated relation" if stated else "mention"
            print(f"stale   {SRC.name}:{ln}  {src}\n"
                  f"    {kind} to «{tok}», which is not an entry in {SRC.name} "
                  f"or {ARCHIVE.name}")
        for src, tok, full, ln in sorted(short_refs):
            print(f"short   {SRC.name}:{ln}  {src}\n"
                  f"    «{tok}» is the head of exactly one real name — write «{full}»")

    if archive_unparsed:
        print(f"\narchive  {ARCHIVE.name}: {len(archive_unparsed)} of {archive_heads} "
              f"headings carry no parseable entry name, so nothing can link to them:")
        for h in archive_unparsed[:10]:
            print(f"    «{h}»")
        if len(archive_unparsed) > 10:
            print(f"    … and {len(archive_unparsed) - 10} more")

    # ADR-039: the closure-line rule read only the live file, and the live file has no
    # closure lines — they all sit in the archive, which was parsed for names and nothing
    # else. 83 of them carry no resolution token. They are pre-standard and §3 forbids
    # backfilling one, so this reports rather than refuses; a closure written on or after
    # the cutoff is a different matter and is refused above once it lands in the archive.
    arc_closures, arc_tokenless = 0, []
    if ARCHIVE.exists():
        for cl in re.findall(r"\*\*Closed:\*\*(.+)", ARCHIVE.read_text(encoding="utf-8-sig")):
            arc_closures += 1
            if not {t.strip().lower() for t in cl.split("·")} & RESOLUTIONS:
                arc_tokenless.append(cl.strip()[:70])
    if arc_tokenless:
        print(f"\nclosure  {ARCHIVE.name}: {len(arc_tokenless)} of {arc_closures} closure "
              f"lines carry no resolution from the six (§3), so the archive cannot say why "
              f"an entry left. Pre-standard; §3 forbids backfilling one, and this counts "
              f"rather than refuses:")
        for cl in arc_tokenless[:5]:
            print(f"    «{cl}»")
        if len(arc_tokenless) > 5:
            print(f"    … and {len(arc_tokenless) - 5} more")

    stated_n = sum(1 for d in dangling if d[3])
    # The dependency count rides in the headline next to the warning count, and for the
    # same reason: it is a debt meter. Warnings measure how much of the format has not
    # been migrated; this measures how much of "what blocks what" has never been written
    # down at all. Nobody was going to open graph.json to find out.
    dep_all = len(dependencies)
    print(f"\n{len(entries)} entries · {len(refusals)} refusals · {len(warnings)} warnings"
          f" · {len(dangling)} stale references · {len(short_refs)} shortened"
          f" · {dep_all} dependencies")
    print(f"Of the {len(dangling)} stale, {stated_n} sit next to a stated relation and "
          f"{len(dangling) - stated_n} are bare mentions. The first number is the one to "
          f"drive to zero; the total is an upper bound on real breakage, not a count of it.")
    dep_n = sum(1 for _, _, r in edges + arc_edges + adr_edges if r in DEPENDENCY_RELS)
    print(f"Links found in prose: {len(edges)} entry→entry, {len(arc_edges)} entry→archive, "
          f"{len(adr_edges)} entry→ADR"
          + (". Drawn by the same script into graph.html." if SRC == ROOT / "backlog.md"
             else " (no graph is drawn for a file other than this project's backlog)."))
    rels = Counter(r for _, _, r in edges + arc_edges + adr_edges)
    print("Relations as written: "
          + " · ".join(f"{k} {v}" for k, v in rels.most_common()))
    print(f"Of those, {dep_n} carry dependency semantics "
          f"({'/'.join(sorted(DEPENDENCY_RELS))}) — the only ones worth walking to answer "
          f"«what do I fix first». Everything else says «see also».")
    if ROADMAP.exists():
        print(f"{ROADMAP.name}: {len(road_covers)} phase→ADR and {len(road_deps)} ADR→ADR "
              f"links across {len(road_phases)} sections, of which {len(set(road_edges))} "
              f"and {len(set(adr_dep_edges))} are drawn — a phase reaches the graph only "
              f"if the "
              f"backlog actually cites one of its decisions.")
    # ADR-039: the trigger for revisiting identifiers reads "if the false-positive share
    # rises across two consecutive monthly checks" — and nothing stored a history, so the
    # trigger could never fire. One appended line per run turns it into two lines of a file
    # instead of two months of somebody's memory. Local, gitignored, never read by the tool.
    if SRC == ROOT / "backlog.md":
        try:
            hist = OUT / "check_history.log"
            hist.open("a", encoding="utf-8").write(
                f"{date.today().isoformat()} · entries {len(entries)} · refusals "
                f"{len(refusals)} · warnings {len(warnings)} · stale {len(dangling)} · "
                f"stated {stated_n} · shortened {len(short_refs)} · deps {dep_all}\n")
        except OSError as exc:                      # a log that cannot be written is not
            print(f"note   could not append to check_history.log: {exc}")   # a failed check

    if adr_meta:
        print(f"\ndecisions  {DECISIONS.parent.name}/{DECISIONS.name}: {len(adr_meta)} "
              f"files, {adr_no_fm} without front matter — required from "
              f"{ADR_FM_FROM:03d} on, so the older ones warn rather than refuse. The "
              f"backlog cites {len(cited_adr)} of them.")
    print(f"Rules: docs/BACKLOG_FORMAT.md §8. Cutoff for the required envelope: {CUTOFF}.")
    print("Stale and shortened references do not set the exit code: they are content, not "
          "structure, and this checker points rather than edits.")
    for line in freshness():
        print(line)
    sys.exit(1 if refusals else 0)

# Past this point the script writes. Rendering a foreign backlog into this project's
# output directory would replace one project's board with another's, silently and with no
# way to tell from the file which project it describes.
if SRC != ROOT / "backlog.md" and not _out:
    print(f"{SRC} is not this project's backlog, and no --out=DIR was given.\n"
          f"Refusing to overwrite {OUT} with another project's entries.\n"
          f"  read-only report:  build.py --check {SRC}\n"
          f"  render elsewhere:  build.py {SRC} --out=<that project's dir>")
    sys.exit(2)

order = ["OPEN", "IN PROGRESS", "DONE", "BACKLOG", "IDEAS"]
def sec_rank(s):
    for k, key in enumerate(order):
        if s.startswith(key):
            return k
    return len(order)

sections = sorted({e["section"] for e in entries}, key=sec_rank)
by_pri = Counter(e["pri"] for e in entries)
shelf = sum(1 for e in entries if e["section"].startswith("DONE"))
open_n = sum(1 for e in entries if e["section"] == "OPEN")
crit_high = sum(1 for e in entries if e["pri"] in ("CRITICAL", "HIGH")
                and e["section"] in ("OPEN", "IN PROGRESS"))
dated = [e["when"] for e in entries if e["when"]]
oldest = min(dated) if dated else "—"

esc = html.escape


def md(text: str) -> str:
    """Escape, then honour the only two markdown marks the backlog uses in prose."""
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def chip(pri):
    return f'<span class="chip {PRI_CLASS.get(pri, "none")}">{esc(pri)}</span>'


rows = []
for s in sections:
    items = [e for e in entries if e["section"] == s]
    items.sort(key=lambda e: (PRIORITIES.index(e["pri"]) if e["pri"] in PRIORITIES else 9,
                              e["when"] or "0000"), reverse=False)
    rows.append(f'<section class="sec" data-sec="{esc(s)}">')
    rows.append(f'<h2>{esc(s)} <span class="cnt">{len(items)}</span></h2>')
    mix = Counter(e["pri"] for e in items)
    bar = "".join(
        f'<span class="seg {PRI_CLASS.get(p, "none")}" style="flex:{mix[p]}" '
        f'title="{esc(p)}: {mix[p]}"></span>'
        for p in PRIORITIES if mix.get(p)
    )
    legend = " · ".join(f"{esc(p)} {mix[p]}" for p in PRIORITIES if mix.get(p))
    rows.append(f'<div class="bar" role="img" aria-label="{esc(legend)}">{bar}</div>')
    rows.append(f'<p class="legend">{legend}</p>')
    rows.append('<ul class="list">')
    for e in items:
        mark = ' <span class="mark done">done</span>' if e["done"] else (
               ' <span class="mark part">partial</span>' if e["part"] else "")
        when = f'<time>{esc(e["when"])}</time>' if e["when"] else ""
        desc = f'<p class="d">{md(e["desc"])}</p>' if e["desc"] else ""
        cut = e["first"][:300] + ("…" if len(e["first"]) > 300 else "")
        first = f'<p class="f">{md(cut)}</p>' if e["first"] else ""
        rows.append(
            f'<li class="item" data-pri="{esc(e["pri"])}" '
            f'data-q="{esc((e["name"] + " " + e["desc"]).lower())}">'
            f'<div class="hd">{chip(e["pri"])}<code>{esc(e["name"])}</code>{mark}{when}</div>'
            f'{desc}{first}</li>'
        )
    rows.append("</ul></section>")

body = "\n".join(rows)
today = date.today().isoformat()

CSS = """
:root{--ground:#eff1f5;--surface:#fff;--sunk:#e6e9ef;--line:#ccd0da;--soft:#dce0e8;
--ink:#4c4f69;--ink-str:#2f3145;--ink-mut:#6c6f85;--ink-faint:#8c8fa1;
--accent:#1976d2;--accent-sunk:#e3edf9;
--crit:#c11135;--crit-sunk:#f8e2e7;--high:#b3730f;--high-sunk:#f6ecda;
--med:#1976d2;--med-sunk:#e3edf9;--low:#7c7f93;--low-sunk:#e6e7ee;
--res:#7847b0;--res-sunk:#eee6f6;--none:#9ca0b0;--none-sunk:#e9eaef;
--ok:#2f8f22;--ok-sunk:#e2f0de;
--sans:"Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,"Noto Sans",sans-serif;
--mono:ui-monospace,"Cascadia Mono","JetBrains Mono",Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#1e1e2e;--surface:#252537;--sunk:#181825;--line:#3b3b52;--soft:#2e2e42;
--ink:#cdd6f4;--ink-str:#e6ebf7;--ink-mut:#a6adc8;--ink-faint:#7f849c;
--accent:#89b4fa;--accent-sunk:#24304a;
--crit:#f38ba8;--crit-sunk:#3a2029;--high:#f9e2af;--high-sunk:#33301c;
--med:#89b4fa;--med-sunk:#24304a;--low:#9399b2;--low-sunk:#2a2a3c;
--res:#cba6f7;--res-sunk:#2e2540;--none:#7f849c;--none-sunk:#282838;
--ok:#a6e3a1;--ok-sunk:#22321f}}
:root[data-theme="dark"]{
--ground:#1e1e2e;--surface:#252537;--sunk:#181825;--line:#3b3b52;--soft:#2e2e42;
--ink:#cdd6f4;--ink-str:#e6ebf7;--ink-mut:#a6adc8;--ink-faint:#7f849c;
--accent:#89b4fa;--accent-sunk:#24304a;
--crit:#f38ba8;--crit-sunk:#3a2029;--high:#f9e2af;--high-sunk:#33301c;
--med:#89b4fa;--med-sunk:#24304a;--low:#9399b2;--low-sunk:#2a2a3c;
--res:#cba6f7;--res-sunk:#2e2540;--none:#7f849c;--none-sunk:#282838;
--ok:#a6e3a1;--ok-sunk:#22321f}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
font-size:1rem;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(1.4rem,4vw,3rem) clamp(1rem,4vw,2.2rem) 5rem}
header.mast{border-bottom:2px solid var(--ink-str);padding-bottom:1.1rem}
h1{margin:0;font-size:clamp(1.8rem,1.5rem+1.2vw,2.7rem);letter-spacing:-.02em;color:var(--ink-str)}
.meta{margin-top:.5rem;font-family:var(--mono);font-size:.8rem;color:var(--ink-mut);
font-variant-numeric:tabular-nums;display:flex;flex-wrap:wrap;gap:.3rem 1.3rem}
.meta b{color:var(--ink-str)}
.stats{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:1.6rem 0 0}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:1rem 1.1rem}
.stat .n{display:block;font-size:clamp(2.1rem,1.8rem+1.2vw,2.9rem);line-height:1;font-weight:300;
letter-spacing:-.03em;color:var(--ink-str);font-variant-numeric:tabular-nums}
.stat .n.warnval{color:var(--high)}.stat .n.critval{color:var(--crit)}
.stat .lab{display:block;margin-top:.45rem;font-size:.85rem;color:var(--ink-mut)}
.controls{position:sticky;top:0;z-index:5;background:var(--ground);padding:1rem 0 .8rem;
margin-top:1.6rem;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.controls input{flex:1 1 210px;min-width:170px;padding:.45rem .65rem;border:1px solid var(--line);
border-radius:5px;background:var(--surface);color:var(--ink);font:inherit;font-size:.9rem}
.fbtn{font-family:var(--mono);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
padding:.35rem .6rem;border-radius:4px;border:1px solid var(--line);background:var(--surface);
color:var(--ink-mut);cursor:pointer}
.fbtn[aria-pressed="true"]{background:var(--accent-sunk);border-color:var(--accent);color:var(--accent);font-weight:600}
.sec{margin-top:2.6rem}
.sec h2{font-size:1.35rem;margin:0 0 .5rem;color:var(--ink-str);letter-spacing:-.01em}
.sec h2 .cnt{font-family:var(--mono);font-size:.9rem;color:var(--ink-faint);font-variant-numeric:tabular-nums}
.bar{display:flex;gap:2px;height:8px;border-radius:2px;overflow:hidden;margin:.2rem 0 .4rem}
.seg{display:block;min-width:3px}
.seg.crit{background:var(--crit)}.seg.high{background:var(--high)}.seg.med{background:var(--med)}
.seg.low{background:var(--low)}.seg.res{background:var(--res)}.seg.none{background:var(--none)}
.legend{margin:0 0 .9rem;font-family:var(--mono);font-size:.72rem;color:var(--ink-faint);
font-variant-numeric:tabular-nums}
.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.55rem}
.item{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:.7rem .9rem}
.item .hd{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem}
.item code{font-family:var(--mono);font-size:.84rem;color:var(--ink-str);font-weight:600;
background:none;padding:0;word-break:break-word}
.item time{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--ink-faint);
font-variant-numeric:tabular-nums}
.item .d{margin:.35rem 0 0;font-size:.9rem;color:var(--ink)}
.item .f{margin:.3rem 0 0;font-size:.82rem;color:var(--ink-mut)}
.chip{font-family:var(--mono);font-size:.66rem;letter-spacing:.07em;font-weight:700;
padding:.16rem .42rem;border-radius:3px;border:1px solid;white-space:nowrap}
.chip.crit{color:var(--crit);background:var(--crit-sunk);border-color:var(--crit)}
.chip.high{color:var(--high);background:var(--high-sunk);border-color:var(--high)}
.chip.med{color:var(--med);background:var(--med-sunk);border-color:var(--med)}
.chip.low{color:var(--low);background:var(--low-sunk);border-color:var(--low)}
.chip.res{color:var(--res);background:var(--res-sunk);border-color:var(--res)}
.chip.none{color:var(--none);background:var(--none-sunk);border-color:var(--none)}
.mark{font-family:var(--mono);font-size:.66rem;letter-spacing:.06em;text-transform:uppercase;
padding:.16rem .42rem;border-radius:3px}
.mark.done{color:var(--ok);background:var(--ok-sunk)}
.mark.part{color:var(--high);background:var(--high-sunk)}
.note{border-left:3px solid var(--accent);background:var(--accent-sunk);padding:.9rem 1.1rem;
border-radius:0 5px 5px 0;margin-top:1.4rem;font-size:.92rem}
.note p{margin:0}.note p+p{margin-top:.5rem}
footer{margin-top:3.5rem;padding-top:1.2rem;border-top:1px solid var(--line);
font-size:.82rem;color:var(--ink-faint)}
.hidden{display:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
const q=document.getElementById('q'),btns=[...document.querySelectorAll('.fbtn')];
let pri=new Set();
function apply(){
  const t=q.value.trim().toLowerCase();
  document.querySelectorAll('.sec').forEach(sec=>{
    let shown=0;
    sec.querySelectorAll('.item').forEach(it=>{
      const okP=pri.size===0||pri.has(it.dataset.pri);
      const okQ=!t||it.dataset.q.includes(t);
      const ok=okP&&okQ; it.classList.toggle('hidden',!ok); if(ok)shown++;
    });
    sec.classList.toggle('hidden',shown===0);
  });
}
q.addEventListener('input',apply);
btns.forEach(b=>b.addEventListener('click',()=>{
  const p=b.dataset.pri;
  if(pri.has(p)){pri.delete(p);b.setAttribute('aria-pressed','false');}
  else{pri.add(p);b.setAttribute('aria-pressed','true');}
  apply();
}));
"""

filters = "".join(
    f'<button class="fbtn" type="button" data-pri="{esc(p)}" aria-pressed="false">'
    f'{esc(p)} {by_pri.get(p, 0)}</button>'
    for p in PRIORITIES if by_pri.get(p)
)

doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>D-PC Messenger — Backlog board</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Ctext y=%2714%27 font-size=%2714%27%3E%F0%9F%93%8B%3C/text%3E%3C/svg%3E">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="mast">
  <h1>Backlog board</h1>
  <div class="meta">
    <span>built <b>{today}</b> from <b>backlog.md</b></span>
    <span><b>{len(entries)}</b> entries</span>
    <span>oldest <b>{esc(oldest)}</b></span>
  </div>
</header>

<div class="stats">
  <div class="stat"><span class="n critval">{crit_high}</span>
    <span class="lab"><b>CRITICAL and HIGH</b> entries open or in progress — what is burning</span></div>
  <div class="stat"><span class="n warnval">{shelf}</span>
    <span class="lab">on the <b>done, awaiting observation</b> shelf — code ran ahead of proof</span></div>
  <div class="stat"><span class="n">{open_n}</span>
    <span class="lab">open and not started</span></div>
</div>

<div class="note">
  <p><strong>The observation shelf is this board's headline number.</strong> It holds work that
  is committed and covered by tests but has never once been watched running on a live system.
  An entry moves to <code>backlog_closed.md</code> only after it has been observed in production —
  "the code is written" is not grounds.</p>
</div>

<div class="controls">
  <input id="q" type="search" placeholder="search name and description" aria-label="Search entries">
  {filters}
</div>

{body}

<footer>
  <p>Built by <code>tools/backlog/build.py</code> from <code>backlog.md</code>; priority, date
  and the done / partial marks are read from entry headings, nothing is inferred.
  Rebuild after editing the backlog: <code>uv run python tools/backlog/build.py</code>.
  Check it against the standard: <code>build.py --check</code> (docs/BACKLOG_FORMAT.md).</p>
  <p>Priority was recognised on {sum(v for k, v in by_pri.items() if k != '—')} of {len(entries)}
  entries; the rest carry none in their heading and are marked "—" rather than guessed into a level.</p>
</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""

DST.write_text(doc, encoding="utf-8")

# ===================================================================== graph.html
# The board answers "what is open". This answers "what leans on what" — the one thing a
# flat list cannot show. Only nodes with at least one link are drawn: on the day this
# shipped, 116 of 217 entries referenced nothing and nothing referenced them, and a
# force layout with half its points floating in space hides the structure it exists to
# reveal. Those entries are listed underneath instead, by priority, because "18 HIGH
# entries no one has connected to anything" is a finding in its own right.
g_deg = Counter()
node_kind = {}
for a, b, _ in edges:
    node_kind[a] = node_kind[b] = "task"
for a, b, _ in adr_edges:
    node_kind[a] = "task"
    node_kind.setdefault(b, "adr")
for a, b, _ in arc_edges:
    node_kind[a] = "task"
    node_kind.setdefault(b, "arc")
for a, b, _ in road_edges:
    node_kind.setdefault(a, "phase")
    node_kind.setdefault(b, "adr")
for a, b, _ in adr_dep_edges:
    node_kind.setdefault(a, "adr")
    node_kind.setdefault(b, "adr")
for a, b, _ in edges + adr_edges + arc_edges + road_edges + adr_dep_edges:
    g_deg[a] += 1
    g_deg[b] += 1

ids = sorted(node_kind)
idx = {n: k for k, n in enumerate(ids)}
g_nodes = []
for n in ids:
    e = live.get(n)
    g_nodes.append({
        "id": n,
        "k": node_kind[n],
        "p": e["pri"] if e else "—",
        "sec": e["section"].split(" ")[0] if e else
               {"adr": "decision", "phase": "roadmap"}.get(node_kind[n], "closed"),
        "d": (e["desc"] or e["first"])[:220] if e else
             (f"ROADMAP.md:{road_phases.get(n, 0)}" if node_kind[n] == "phase" else ""),
        "ln": e["line"] if e else 0,
        "deg": g_deg[n],
    })

# A link keeps the words that made it: `rel` is the phrase the author used, so the picture
# can draw "blocks" differently from "see also" and a reader can walk only the first kind.
def _links(pairs, kind):
    seen, out = set(), []
    for a, b, rel in pairs:
        key = (a, b, rel)
        if key in seen:
            continue
        seen.add(key)
        out.append({"s": idx[a], "t": idx[b], "k": kind, "rel": rel})
    return out


g_links = (_links(edges, 0) + _links(adr_edges, 1) + _links(arc_edges, 2)
           + _links(road_edges, 3) + _links(adr_dep_edges, 4))

# ------------------------------------------------------------------------- layout
# The layout is computed here, not in the browser, for two reasons. It makes the picture an
# artifact of the build rather than of whoever opened it — the coordinates land in
# graph.json and can be checked. And it makes the previous run's positions available as a
# starting point, which is the only thing that actually delivers spatial stability.
#
# The obvious fix — seed each node from a hash of its name instead of its list position —
# was implemented and then MEASURED, because Fable 5 offered it as a one-liner and I
# believed it. Adding a single entry still moved the median node 303 px. A force
# simulation has many near-equivalent minima and one extra body re-routes the whole
# descent; deterministic and stable are different properties, and only the second is the
# one a human's spatial memory needs. Warm-starting from the last layout delivers it.
def layout(nodes, links, prev):
    n = len(nodes)
    warm = sum(1 for d in nodes if d["id"] in prev)
    steps = 90 if warm > n * 0.8 else 420          # refine an old picture, or draw a new one
    xs, ys = [0.0] * n, [0.0] * n
    for i, d in enumerate(nodes):
        if d["id"] in prev:
            xs[i], ys[i] = prev[d["id"]]
        else:
            # A new node starts where its name says, not where its index says: the same
            # entry lands in the same place whatever else was added alongside it.
            h = 2166136261
            for ch in d["id"]:
                h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
            ang = (h / 0x100000000) * 6.283185
            rad = 30 + ((h >> 8) % 1000) / 1000 * 430
            xs[i], ys[i] = 600 + rad * math.cos(ang), 410 + rad * math.sin(ang)
    vx, vy = [0.0] * n, [0.0] * n
    ln = [(l["s"], l["t"]) for l in links]
    for s in range(steps):
        cool = 1 - s / steps
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy = xs[j] - xs[i], ys[j] - ys[i]
                d2 = dx * dx + dy * dy or 1.0
                if d2 > 90000:
                    continue
                d = math.sqrt(d2)
                f = 2600 / d2
                fx, fy = f * dx / d, f * dy / d
                vx[i] -= fx
                vy[i] -= fy
                vx[j] += fx
                vy[j] += fy
        for a, b in ln:
            dx, dy = xs[b] - xs[a], ys[b] - ys[a]
            d = math.hypot(dx, dy) or 1.0
            f = (d - 95) * 0.045
            fx, fy = f * dx / d, f * dy / d
            vx[a] += fx
            vy[a] += fy
            vx[b] -= fx
            vy[b] -= fy
        for i in range(n):
            vx[i] += (600 - xs[i]) * 0.004
            vy[i] += (410 - ys[i]) * 0.004
            xs[i] += vx[i] * cool
            ys[i] += vy[i] * cool
            vx[i] *= 0.82
            vy[i] *= 0.82
    for i, d in enumerate(nodes):
        d["x"], d["y"] = round(xs[i], 1), round(ys[i], 1)
    return steps


previous = {}
if JSON_DST.exists():
    try:
        for d in json.loads(JSON_DST.read_text(encoding="utf-8")).get("nodes", []):
            if "x" in d and "y" in d:
                previous[d["id"]] = (d["x"], d["y"])
    except (ValueError, OSError):
        previous = {}                    # a corrupt or hand-edited file just means cold start

layout_steps = layout(g_nodes, g_links, previous)

orphans = [e for e in entries if e["is_name"] and e["ref"] not in idx]
orph_by_pri = Counter(e["pri"] for e in orphans)
_orph_rows = []
for e in sorted(orphans, key=lambda x: (PRIORITIES.index(x["pri"])
                                        if x["pri"] in PRIORITIES else 9, x["ref"])):
    _d = f'<p class="d">{md(e["desc"])}</p>' if e["desc"] else ""
    _orph_rows.append(
        f'<li class="item" data-pri="{esc(e["pri"])}">'
        f'<div class="hd">{chip(e["pri"])}<code>{esc(e["ref"])}</code>'
        f'<time>{esc(e["section"].split(" ")[0])}</time></div>{_d}</li>')
orph_html = "".join(_orph_rows)

GRAPH_CSS = """
/* The board's 1080px column is sized for prose. A graph is not prose — on a wide screen
   that column was most of why the canvas felt cramped. */
.wrap{max-width:min(1680px,96vw)}
.gwrap{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:1rem;margin-top:1rem;
align-items:start}
@media (max-width:900px){.gwrap{grid-template-columns:1fr}}
/* Native resize handle, bottom-right. `overflow` must not be visible for it to appear;
   max-width keeps a stretched box from pushing the page into a sideways scroll. */
.stagebox{background:var(--surface);border:1px solid var(--line);border-radius:6px;
height:min(80vh,900px);min-height:300px;min-width:320px;max-width:100%;
resize:both;overflow:hidden;padding:0 14px 14px 0}
#stage{touch-action:none;cursor:grab;display:block;width:100%;height:100%}
#stage.drag{cursor:grabbing}
.side{background:var(--surface);border:1px solid var(--line);border-radius:6px;
padding:.9rem 1rem;font-size:.9rem;position:sticky;top:1rem}
.side h3{margin:0 0 .4rem;font-size:.95rem;color:var(--ink-str)}
.side code{font-family:var(--mono);font-size:.8rem;word-break:break-word;color:var(--ink-str)}
.side p{margin:.5rem 0 0}
.side .k{font-family:var(--mono);font-size:.72rem;color:var(--ink-faint);
text-transform:uppercase;letter-spacing:.06em}
.side ul{margin:.5rem 0 0;padding-left:1.1rem}
.side li{margin:.15rem 0}
.side li em{font-family:var(--mono);font-size:.68rem;font-style:normal;color:var(--crit);
text-transform:uppercase;letter-spacing:.04em}
.side a{color:var(--accent);cursor:pointer;text-decoration:none;border-bottom:1px dotted}
.gkey{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin:.6rem 0 0;font-family:var(--mono);
font-size:.72rem;color:var(--ink-mut)}
.gkey span{display:inline-flex;align-items:center;gap:.35rem}
.sw{width:12px;height:12px;border-radius:50%;display:inline-block}
.sw.adr{border-radius:2px}
.sw.arc{background:none;border:1.5px dashed var(--ink-faint)}
.node{cursor:pointer}
.node text{font-family:var(--mono);font-size:9.5px;fill:var(--ink-mut);pointer-events:none;
paint-order:stroke;stroke:var(--surface);stroke-width:3.5px;stroke-linejoin:round}
.node text.q{display:none}
.node:hover text.q,.node.sel text.q,.node.nbr text.q{display:block}
.node.sel text{fill:var(--ink-str);font-weight:700}
.node.sel circle,.node.sel rect{stroke:var(--ink-str);stroke-width:2.5}
.node.dim{opacity:.13}
.link{stroke:var(--line);stroke-width:1.1;fill:none}
.link.k1{stroke:var(--accent);stroke-opacity:.55}
.link.k2{stroke:var(--ink-faint);stroke-dasharray:3 3;stroke-opacity:.6}
.link.k3{stroke:var(--ok);stroke-opacity:.5;stroke-width:1.4}
.link.k4{stroke:var(--res);stroke-opacity:.7;stroke-width:1.6}
/* A relation somebody actually stated as a dependency. Two of them today — which is the
   finding, not a rendering detail. */
.link.dep{stroke:var(--crit);stroke-opacity:1;stroke-width:2.2;stroke-dasharray:none}
.link.hot{stroke:var(--ink-str);stroke-width:2;stroke-opacity:1}
.link.dim{opacity:.07}
"""

GRAPH_JS = """
const NODES=DATA.nodes,LINKS=DATA.links;
const PC={CRITICAL:'crit',HIGH:'high',MEDIUM:'med',LOW:'low',RESEARCH:'res','—':'none'};
const svg=document.getElementById('stage');
const W=1200,H=820;
// Coordinates arrive already computed — the layout is part of the build (see layout() in
// build.py), warm-started from the previous run so the picture does not reshuffle when an
// entry is added. This page only draws, drags and queries.
const adj=NODES.map(()=>[]);
LINKS.forEach(l=>{adj[l.s].push(l.t);adj[l.t].push(l.s);});
// The drawing sits wherever the simulation left it. Rather than squeeze it into a fixed
// viewBox (which left a third of the canvas empty, because the content's aspect is not the
// element's), frame the content: the starting view IS the bounding box. Extra room on the
// right is label space.
const BB=(function(){const xs=NODES.map(n=>n.x),ys=NODES.map(n=>n.y);
  const x0=Math.min(...xs)-40,x1=Math.max(...xs)+150,y0=Math.min(...ys)-30,y1=Math.max(...ys)+30;
  return{x:x0,y:y0,w:x1-x0,h:y1-y0};})();
const R=n=>4+Math.sqrt(n.deg)*2.6;
const lg=document.getElementById('links'),ng=document.getElementById('nodes');
// Arrowheads: the semantics were always directed — src wrote dst's name, not the reverse —
// and the picture used to draw a plain line, so "A cites B" and "B cites A" looked alike
// (GLM 5.2). The head stops short of the target node's radius so it points at the circle
// rather than sitting under it.
const DEP=new Set(['blocked_by','blocks','depends_on','parent','child']);
lg.innerHTML=LINKS.map((l,i)=>{const dep=DEP.has(l.rel);
  return `<line class="link k${l.k}${dep?' dep':''}" data-i="${i}" data-rel="${l.rel||''}"`
    +` marker-end="url(#ah${dep?'D':l.k})"><title>${l.rel||'mention'}</title></line>`;}).join('');
// Every node carries its name, but only hubs and decisions show it standing still: at 190
// nodes the labels collided into a grey mat and hid the very structure they annotate.
// The rest appear on hover and on selection — the information is not removed, it is asked
// for. Names are drawn with a halo so they stay readable where they cross a link.
ng.innerHTML=NODES.map((n,i)=>{
  const r=R(n),full=n.id.length>24?n.id.slice(0,23)+'…':n.id;
  const quiet=(n.deg>=5||n.k==='adr'||n.k==='phase')?'':' class="q"';
  const lab=`<text${quiet} x="${r+4}" y="3.5">${full}</text>`;
  const shape=n.k==='phase'
    ? `<rect x="${-r*1.7}" y="${-r*0.9}" width="${3.4*r}" height="${1.8*r}" rx="3" fill="var(--ok)" stroke="var(--ok)"></rect>`
    : n.k==='adr'
    ? `<rect x="${-r}" y="${-r}" width="${2*r}" height="${2*r}" rx="2" fill="var(--accent)" stroke="var(--accent)"></rect>`
    : n.k==='arc'
    ? `<circle r="${r}" fill="none" stroke="var(--ink-faint)" stroke-dasharray="2 2"></circle>`
    : `<circle r="${r}" fill="var(--${PC[n.p]||'none'})" stroke="var(--surface)" stroke-width="1"></circle>`;
  return `<g class="node" data-i="${i}" transform="translate(${n.x},${n.y})">${shape}${lab}</g>`;
}).join('');
const lines=[...lg.children],gs=[...ng.children];
function draw(){
  LINKS.forEach((l,i)=>{const a=NODES[l.s],b=NODES[l.t],e=lines[i];
    const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,off=R(b)+6;
    e.setAttribute('x1',a.x);e.setAttribute('y1',a.y);
    e.setAttribute('x2',b.x-dx/d*off);e.setAttribute('y2',b.y-dy/d*off);});
  NODES.forEach((n,i)=>gs[i].setAttribute('transform',`translate(${n.x},${n.y})`));
}
draw();
// pan and zoom
let vb={...BB};
const setvb=()=>svg.setAttribute('viewBox',`${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
setvb();
svg.addEventListener('wheel',ev=>{ev.preventDefault();
  const f=ev.deltaY>0?1.12:0.89,pt=xy(ev);
  vb.x=pt.x-(pt.x-vb.x)*f;vb.y=pt.y-(pt.y-vb.y)*f;vb.w*=f;vb.h*=f;setvb();},{passive:false});
// Screen -> user space through the SVG's own matrix. Doing the arithmetic by hand from
// getBoundingClientRect is off by the letterboxing that preserveAspectRatio adds whenever
// the element's aspect differs from the viewBox — which is always, at any window width.
function xy(ev){const p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;
  const q=p.matrixTransform(svg.getScreenCTM().inverse());return{x:q.x,y:q.y};}
// One press does one thing: a press that moves the pointer drags, a press that does not
// selects — Mike, 2026-08-10. When the same press both moved a node and re-drew the panel,
// every attempt to untangle the picture threw away what you were reading.
//
// Selection deliberately does NOT read ev.target: setPointerCapture retargets the events
// that follow to the capturing element, so `ev.target.closest('.node')` on the dblclick
// resolves to the <svg> and finds nothing — which is exactly why nothing selected in the
// first version of this. The node under the press is remembered instead.
let drag=null,pan=null;
svg.addEventListener('pointerdown',ev=>{
  const g=ev.target.closest('.node'),p=xy(ev);
  if(g)drag={i:+g.dataset.i,x:p.x,y:p.y,sx:ev.clientX,sy:ev.clientY,moved:false};
  else{pan={x:p.x,y:p.y,vx:vb.x,vy:vb.y};svg.classList.add('drag');}
  svg.setPointerCapture(ev.pointerId);});
svg.addEventListener('dblclick',ev=>{
  const g=ev.target.closest('.node');
  if(g)select(+g.dataset.i);else if(drag)select(drag.i);});
svg.addEventListener('pointermove',ev=>{
  if(drag){if(Math.hypot(ev.clientX-drag.sx,ev.clientY-drag.sy)>4)drag.moved=true;
    const p=xy(ev);NODES[drag.i].x=p.x;NODES[drag.i].y=p.y;draw();}
  else if(pan){const p=xy(ev);vb.x=pan.vx+(pan.x-p.x);vb.y=pan.vy+(pan.y-p.y);setvb();}});
// Threshold in screen pixels, not user units: at 4x zoom a 3px twitch is a large number of
// viewBox units, and every click would have counted as a drag.
addEventListener('pointerup',()=>{
  if(drag&&!drag.moved)select(drag.i);
  drag=null;pan=null;svg.classList.remove('drag');});
// selection
const info=document.getElementById('info');
function select(i){
  const near=new Set([i,...adj[i]]);
  gs.forEach((g,j)=>{g.classList.toggle('sel',j===i);
    g.classList.toggle('nbr',near.has(j));
    g.classList.toggle('dim',!near.has(j));});
  lines.forEach((e,j)=>{const on=LINKS[j].s===i||LINKS[j].t===i;
    e.classList.toggle('hot',on);e.classList.toggle('dim',!on);});
  const n=NODES[i];
  const out=LINKS.filter(l=>l.s===i).map(l=>[NODES[l.t].id,l.rel]);
  const inn=LINKS.filter(l=>l.t===i).map(l=>[NODES[l.s].id,l.rel]);
  const list=(t,a)=>a.length?`<p class="k">${t}</p><ul>${a.map(([x,r])=>
    `<li><a data-jump="${x}">${x}</a>${r&&r!=='mention'?` <em>${r}</em>`:''}</li>`).join('')}</ul>`:'';
  info.innerHTML=`<h3><code>${n.id}</code></h3>
    <p class="k">${n.k==='adr'?'decision record':n.k==='arc'?'closed — in the archive':n.sec+' · '+n.p}${n.ln?' · backlog.md:'+n.ln:''}</p>
    ${n.d?`<p>${n.d}</p>`:''}${list('references',out)}${list('referenced by',inn)}`;
  info.querySelectorAll('[data-jump]').forEach(a=>a.addEventListener('click',()=>{
    const j=NODES.findIndex(x=>x.id===a.dataset.jump);if(j>=0)select(j);}));
}
document.getElementById('gq').addEventListener('input',ev=>{
  const t=ev.target.value.trim().toLowerCase();
  const hit=j=>NODES[j].id.toLowerCase().includes(t);
  gs.forEach((g,j)=>g.classList.toggle('dim',!!t&&!hit(j)));
  lines.forEach((e,j)=>e.classList.toggle('dim',!!t&&!hit(LINKS[j].s)&&!hit(LINKS[j].t)));
});
document.getElementById('reset').addEventListener('click',()=>{
  vb={...BB};setvb();
  gs.forEach(g=>g.classList.remove('dim','sel','nbr'));
  lines.forEach(e=>e.classList.remove('dim','hot'));
  info.innerHTML=START;});
const START=info.innerHTML;
"""

gdoc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>D-PC Messenger — Backlog graph</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Ctext y=%2714%27 font-size=%2714%27%3E%F0%9F%95%B8%3C/text%3E%3C/svg%3E">
<style>{CSS}{GRAPH_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="mast">
  <h1>Backlog graph</h1>
  <div class="meta">
    <span>built <b>{today}</b> from <b>backlog.md</b></span>
    <span><b>{len(g_nodes)}</b> linked nodes</span>
    <span><b>{len(g_links)}</b> links</span>
    <span><b>{len(orphans)}</b> entries link to nothing</span>
  </div>
</header>

<div class="note">
  <p><strong>Every link here was already written in the prose.</strong> An entry name is a
  SCREAMING-KEBAB sentence, so a name appearing in another entry's body is a reference —
  no <code>depends_on</code> field was added and no entry was edited to produce this
  picture. That also bounds what it can claim: it shows that two entries mention each
  other, not that one blocks the other.</p>
  <p>ADR nodes are drawn because the backlog and the roadmap already speak the same
  language — decisions. They are the join between the two documents, and they cost nobody
  a new habit.</p>
</div>

<div class="controls">
  <input id="gq" type="search" placeholder="highlight by name" aria-label="Highlight nodes by name">
  <button class="fbtn" type="button" id="reset">reset view</button>
</div>

<div class="gkey">
  <span><i class="sw" style="background:var(--crit)"></i>CRITICAL</span>
  <span><i class="sw" style="background:var(--high)"></i>HIGH</span>
  <span><i class="sw" style="background:var(--med)"></i>MEDIUM</span>
  <span><i class="sw" style="background:var(--low)"></i>LOW</span>
  <span><i class="sw" style="background:var(--res)"></i>RESEARCH</span>
  <span><i class="sw adr" style="background:var(--accent)"></i>ADR</span>
  <span><i class="sw adr" style="background:var(--ok)"></i>ROADMAP phase</span>
  <span><i class="sw arc"></i>closed, in the archive</span>
  <span><i class="sw" style="background:var(--crit);border-radius:1px;height:3px"></i>stated dependency</span>
  <span>size = number of links</span>
  <span>drag the bottom-right corner of the canvas to resize it</span>
</div>

<div class="gwrap">
  <div class="stagebox">
    <svg id="stage" viewBox="0 0 1200 820" role="img"
         aria-label="Force-directed graph of backlog entries and the decisions they cite">
      <defs>
        <marker id="ah0" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
                markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--line)"></path></marker>
        <marker id="ah1" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
                markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--accent)" fill-opacity=".6"></path></marker>
        <marker id="ah2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
                markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--ink-faint)" fill-opacity=".6"></path></marker>
        <marker id="ah3" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
                markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--ok)" fill-opacity=".6"></path></marker>
        <marker id="ah4" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
                markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--res)" fill-opacity=".8"></path></marker>
        <marker id="ahD" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7"
                markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--crit)"></path></marker>
      </defs>
      <g id="links"></g><g id="nodes"></g>
    </svg>
  </div>
  <aside class="side" id="info">
    <h3>Nothing selected</h3>
    <p class="k">click a node</p>
    <p><strong>Click</strong> a node to select it — or double-click, both work: everything
    it is not connected to dims, its neighbours show their names, and both directions are
    listed here, what it cites and what cites it. <strong>Drag</strong> a node to pull it
    out of the crowd; a press that moves never selects, so untangling the picture never
    throws away what you are reading. Drag the background to pan, wheel to zoom, hover any
    node to read its name without changing anything.</p>
  </aside>
</div>

<section class="sec">
  <h2>Linked to nothing <span class="cnt">{len(orphans)}</span></h2>
  <p class="legend">{" · ".join(f"{p} {orph_by_pri[p]}" for p in PRIORITIES if orph_by_pri.get(p))}</p>
  <p class="legend">No other entry mentions these by name, and they mention none. That is
  either genuine independence or a missing Cross-ref — the graph cannot tell which, so it
  lists them rather than drawing them as dust.</p>
  <ul class="list">{orph_html}</ul>
</section>

<footer>
  <p>Built by <code>tools/backlog/build.py</code> in the same pass as
  <code>backlog.html</code>, so the board and the graph are always the same age.
  Rebuild both: <code>uv run python tools/backlog/build.py</code>.
  <code>build.py --check</code> prints how old each artefact is and lists references that
  resolve to nothing.</p>
  <p>{len([l for l in g_links if l["k"] == 0])} entry→entry ·
  {len([l for l in g_links if l["k"] == 1])} entry→ADR ·
  {len([l for l in g_links if l["k"] == 2])} entry→archive.
  Layout is deterministic: the same backlog produces the same picture.</p>
</footer>
</div>
<script>const DATA={json.dumps({"nodes": g_nodes, "links": g_links}, ensure_ascii=False)};</script>
<script>{GRAPH_JS}</script>
</body>
</html>
"""

GRAPH_DST.write_text(gdoc, encoding="utf-8")

# The same graph, for readers who cannot click. Agents receive backlog entries as retrieval
# chunks; an entry's own outgoing references are in the prose it was handed, but "what
# references this" exists nowhere it can see — and the pipeline already computes it and was
# throwing it away. Both external reviews ranked emitting it above every rendering change.
backlinks = defaultdict(list)
for a, b, _ in edges + arc_edges + adr_edges:
    backlinks[b].append(a)

JSON_DST.write_text(json.dumps({
    "built": today,
    "source": SRC.name,
    "entries": len(entries),
    "nodes": g_nodes,
    "links": g_links,
    # Name-keyed, not index-keyed: an index is only meaningful next to this exact node list,
    # and the whole point is that something else reads this.
    "backlinks": {k: sorted(set(v)) for k, v in sorted(backlinks.items())},
    # The walkable subgraph, split out because it is the only part that answers "what do I
    # fix first" — and because how small it is, is itself the finding.
    "dependencies": [{"from": a, "to": b, "rel": r} for a, b, r in dependencies],
    "unlinked": sorted(e["ref"] for e in orphans),
    "stale_references": [{"from": s, "token": t, "line": ln, "stated": bool(st)}
                         for s, t, ln, st in dangling],
}, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"entries: {len(entries)}  ->  {DST}  ({len(doc)} chars)")
print("sections:", {s: sum(1 for e in entries if e['section'] == s) for s in sections})
print("priorities:", dict(by_pri))
print(f"graph:   {len(g_nodes)} nodes, {len(g_links)} links, {len(orphans)} unlinked "
      f"->  {GRAPH_DST}  ({len(gdoc)} chars)")
print(f"references: {len(dangling)} resolve to nothing, {len(short_refs)} shortened "
      f"(listed by: build.py --check)")
