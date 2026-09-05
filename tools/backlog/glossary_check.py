"""The glossary points, it does not define — so every «Defined in» link has to land.

`docs/GLOSSARY.md` is one row per word, and each row's last column links the document
that owns the word. A link that resolves to nothing is the same defect as a stale
reference one document up: the file claims a source it does not have. This module is
the check, kept apart from build.py so it can run without a board: build.py --check
needs backlog.md, which is gitignored and absent from every clone, so CI could never
see a broken glossary link through it (Linus's question, 2026-09-05). The client test
suite imports this file directly, the way it imports the commit hook.

Stdlib only, like build.py.
"""
import re
from pathlib import Path

ROW = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
URL = re.compile(r"^[a-z]+://")


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


def check_glossary(glossary, axes=()):
    """Walk the rows of `glossary`; return (terms, links, link_warnings, axis_warnings).

    `terms` — every row's term, lowercased, in file order.
    `links` — how many «Defined in» links were followed (URLs are skipped: nothing local
              to resolve them against).
    `link_warnings` — one line per link that names no file or no heading.
    `axis_warnings` — one line per token in `axes` that has no row.
    Warnings, never refusals: content, not structure.
    """
    glossary = Path(glossary)
    terms, links, link_warnings, axis_warnings = [], 0, [], []
    cache = {}
    for ln_no, ln in enumerate(glossary.read_text(encoding="utf-8-sig").split("\n"), 1):
        row = ROW.match(ln)
        if not row:
            continue
        term = row.group(1).strip()
        terms.append(term.lower())
        for _text, target in LINK.findall(ln):
            if URL.match(target):
                continue
            links += 1
            rel, _, anchor = target.partition("#")
            f = (glossary.parent / rel).resolve() if rel else glossary
            if not f.exists():
                link_warnings.append(
                    f"{glossary.name}:{ln_no}  {term}\n    «Defined in» points at {rel}, "
                    f"which is not a file — the row defines nothing until it points somewhere")
            elif anchor and anchor.lower() not in anchors(f, cache):
                link_warnings.append(
                    f"{glossary.name}:{ln_no}  {term}\n    «Defined in» names #{anchor} in "
                    f"{rel}, and that file has no heading with that anchor")
    for a in axes:
        if a not in terms:
            axis_warnings.append(
                f"{glossary.name}  {a}\n    axis token with no glossary row — the board "
                f"files work under a word nobody has written down where a reader would look")
    return terms, links, link_warnings, axis_warnings
