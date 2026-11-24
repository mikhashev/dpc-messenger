"""The glossary points, it does not define — so every «Defined in» link has to land.

`docs/GLOSSARY.md` is one `###` heading per word (2026-11-24, Mike: headings per term, so
agents and people can find their way), and each term's `- **Defined in:**` line links the
document that owns the word. A link that resolves to nothing is the same defect as a stale
reference one document up: the file claims a source it does not have. This module is
the check, kept apart from build.py so it can run without a board: build.py --check
needs backlog.md, which is gitignored and absent from every clone, so CI could never
see a broken glossary link through it (Linus's question, 2026-09-05). The client test
suite imports this file directly, the way it imports the commit hook.

The layout is the check's input, so a file in another layout must not read as an empty
glossary: the file used to be six tables, and a reader of rows that meets headings finds
no terms, no links and no axes, and reports nothing wrong (Zcode's warning, 2026-11-24).
`check_glossary` refuses instead (`GlossaryLayoutError`): a table row, no term heading at
all, or a term with no «Defined in» line is structure, and structure refuses; a link that
lands nowhere is content, and content warns.

Stdlib only, like build.py.
"""
import re
from pathlib import Path

TERM = re.compile(r"^###\s+(.+?)\s*$")
ANY_HEADING = re.compile(r"^#{1,6}\s")
DEFINED_IN = re.compile(r"^-\s+\*\*Defined in:\*\*\s*(.*?)\s*$")
OLD_ROW = re.compile(r"^\|\s*\*\*")
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
URL = re.compile(r"^[a-z]+://")


class GlossaryLayoutError(ValueError):
    """The file is not in the layout the check reads: refuse, never pass silently."""


def slug(heading):
    """GitHub's anchor: lowercase, drop everything but letters, digits, spaces, hyphens
    and underscores, then spaces become hyphens. Consecutive hyphens survive, which is
    why «4a. … — `axis:`» anchors as «…-anyway--axis»."""
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", s)


def anchors(path, cache=None):
    """The set of anchors the headings of `path` produce; empty for an unreadable file."""
    cache = cache if cache is not None else {}
    if path not in cache:
        try:
            txt = path.read_text(encoding="utf-8-sig")
        except OSError:
            txt = ""
        cache[path] = {slug(m.group(1)) for m in HEADING.finditer(txt)}
    return cache[path]


def read_terms(glossary):
    """Split `glossary` into term blocks: [(heading, line number, [(line no, text)])].

    A term is a `###` heading; its block runs to the next heading of any level. Prose
    under a `##` section before its first term belongs to no term, and so does the index.
    Lines inside a code fence are not headings. Raises GlossaryLayoutError listing every
    structural defect: a table row of the old layout, no term at all, a term without
    exactly one non-empty `- **Defined in:**` line.
    """
    name = Path(glossary).name
    blocks, current, fenced, old_rows = [], None, False, []
    for ln_no, ln in enumerate(Path(glossary).read_text(encoding="utf-8-sig").split("\n"), 1):
        if FENCE.match(ln):
            fenced = not fenced
        if fenced:
            if current is not None:
                current[2].append((ln_no, ln))
            continue
        if OLD_ROW.match(ln):
            old_rows.append(ln_no)
        if ANY_HEADING.match(ln):
            m = TERM.match(ln)
            current = (m.group(1), ln_no, []) if m else None
            if current is not None:
                blocks.append(current)
        elif current is not None:
            current[2].append((ln_no, ln))
    problems = []
    if old_rows:
        problems.append(
            f"{name}:{old_rows[0]}  {len(old_rows)} table row(s) in the old layout\n    a term is "
            f"a `### heading` with `- **Defined in:** …` under it now, and a table row is read "
            f"by nothing")
    if not blocks:
        problems.append(
            f"{name}  no `###` term heading at all\n    this check reads one heading per "
            f"term; a file without any would pass as an empty glossary")
    for heading, ln_no, body in blocks:
        defined = [DEFINED_IN.match(t) for _n, t in body if DEFINED_IN.match(t)]
        if not defined:
            problems.append(
                f"{name}:{ln_no}  {heading}\n    no `- **Defined in:**` line — this file "
                f"points, it does not define, so a term without a source does not belong here")
        elif len(defined) > 1:
            problems.append(
                f"{name}:{ln_no}  {heading}\n    {len(defined)} «Defined in» lines — "
                f"one source line per term")
        elif not defined[0].group(1):
            problems.append(f"{name}:{ln_no}  {heading}\n    the «Defined in» line is empty")
    if problems:
        raise GlossaryLayoutError("\n".join(problems))
    return blocks


def check_glossary(glossary, axes=()):
    """Walk the terms of `glossary`; return (terms, links, link_warnings, axis_warnings).

    `terms` — every term heading, lowercased, in file order.
    `links` — how many links inside a term's block were followed (URLs are skipped:
              nothing local to resolve them against). The whole block is walked, as the
              old row walk read the whole row, so a link in «Meaning» is checked too.
    `link_warnings` — one line per link that names no file or no heading.
    `axis_warnings` — one line per token in `axes` that has no term.
    Warnings, never refusals: content, not structure. Structure raises
    GlossaryLayoutError (see read_terms).
    """
    glossary = Path(glossary)
    terms, links, link_warnings, axis_warnings = [], 0, [], []
    cache = {}
    for term, _start, body in read_terms(glossary):
        terms.append(term.lower())
        for ln_no, ln in body:
            for _text, target in LINK.findall(ln):
                if URL.match(target):
                    continue
                links += 1
                rel, _, anchor = target.partition("#")
                f = (glossary.parent / rel).resolve() if rel else glossary
                if not f.exists():
                    link_warnings.append(
                        f"{glossary.name}:{ln_no}  {term}\n    «Defined in» points at {rel}, "
                        f"which is not a file — the term defines nothing until it points "
                        f"somewhere")
                elif anchor and anchor.lower() not in anchors(f, cache):
                    link_warnings.append(
                        f"{glossary.name}:{ln_no}  {term}\n    «Defined in» names #{anchor} in "
                        f"{rel}, and that file has no heading with that anchor")
    for a in axes:
        if a not in terms:
            axis_warnings.append(
                f"{glossary.name}  {a}\n    axis token with no glossary term — the board "
                f"files work under a word nobody has written down where a reader would look")
    return terms, links, link_warnings, axis_warnings
