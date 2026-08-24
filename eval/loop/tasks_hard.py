"""The harder tier, added because the first one scored 10/10 and said so.

A first run that looks good means the instrument is not touching the thing it
claims to measure (`eval/README.md`). The easy tier stays as a floor — a
regression there is a real alarm — and everything below asks for something the
easy tier never did:

- **more than one hop**: a file that names the file that holds the answer;
- **more than one file**: an aggregate nothing can answer by reading one;
- **a write that must be correct**, not merely present;
- **an edit** to a file that already exists, leaving the rest of it alone;
- **an absence**: which of these does not exist, answered without inventing;
- **a contradiction**: two files disagree, and the answer has to say so;
- **a refusal**: a key that is not in the file, where a number invented to
  please is the failure mode being tested.

Every check is still deterministic. Nothing here is judged by a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def build_fixture(root: Path) -> None:
    """A world with two hops, a disagreement, and an absence in it."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    (docs / "config.txt").write_text(
        "host=example.internal\nport=8443\nretries=7\nmode=strict\n", encoding="utf-8"
    )
    (docs / "notes.md").write_text(
        "# Release notes\n\n- shipped the drain watchdog\n- fixed the split history\n"
        "- the build number is 4172\n",
        encoding="utf-8",
    )
    (docs / "empty.log").write_text("", encoding="utf-8")

    # Hop one: names the file that holds the answer, and nothing else.
    (docs / "pointer.txt").write_text(
        "The current deployment record is in deploy-b.json. Read that one.\n",
        encoding="utf-8",
    )
    (docs / "deploy-a.json").write_text(
        json.dumps({"release": {"channel": "old", "revision": 1101}}, indent=2),
        encoding="utf-8",
    )
    (docs / "deploy-b.json").write_text(
        json.dumps({"release": {"channel": "stable", "revision": 2314}}, indent=2),
        encoding="utf-8",
    )

    # Two files that disagree about the same field, on purpose.
    (docs / "limits-old.ini").write_text("[limits]\nmax_users=50\n", encoding="utf-8")
    (docs / "limits-new.ini").write_text("[limits]\nmax_users=200\n", encoding="utf-8")


def tasks_for(root: Path) -> List[Dict[str, Any]]:
    docs = root / "docs"
    out = root / "out"
    return [
        {
            "id": "hard-two-hops",
            "prompt": (
                f"Read {docs / 'pointer.txt'}. It names another file in the same folder. "
                f"Open that file and tell me the release revision. Answer with the number only."
            ),
            "expect_in_answer": ["2314"],
            "reject_in_answer": ["1101"],
        },
        {
            "id": "hard-arithmetic-across-two-files",
            "prompt": (
                f"Add the `port` from {docs / 'config.txt'} to the build number in "
                f"{docs / 'notes.md'}. Answer with the sum only."
            ),
            "expect_in_answer": ["12615"],
        },
        {
            "id": "hard-which-file-holds-it",
            "prompt": (
                f"Which file in {docs} contains the word `strict`? Answer with the file name only."
            ),
            "expect_in_answer": ["config.txt"],
        },
        {
            "id": "hard-aggregate-over-files",
            "prompt": (
                f"How many lines in total do {docs / 'config.txt'} and {docs / 'notes.md'} "
                f"have together? Count every line including blank ones. Answer with a number."
            ),
            # 4 + 5. The first version of this task expected 10 and marked a
            # correct answer of 9 as a failure — a harness that grades against a
            # wrong gold answer reports regressions that are not there, which is
            # worse than having no harness. Counted from the fixture bytes now.
            "expect_in_answer": ["9"],
        },
        {
            "id": "hard-edit-in-place",
            "prompt": (
                f"In {docs / 'config.txt'}, change `retries` from 7 to 9. Leave every other "
                f"line exactly as it is. Then say done."
            ),
            "expect_file": {
                "path": str(docs / "config.txt"),
                "contains": "retries=9",
                "still_contains": ["host=example.internal", "port=8443", "mode=strict"],
                "must_not_contain": ["retries=7"],
            },
        },
        {
            "id": "hard-derive-a-written-file",
            "prompt": (
                f"Write a file at {out / 'summary.txt'} whose only content is the value of "
                f"`host` from {docs / 'config.txt'}. Then say done."
            ),
            "expect_file": {"path": str(out / "summary.txt"), "contains": "example.internal"},
        },
        {
            "id": "hard-the-one-that-is-missing",
            "prompt": (
                f"Of these three — {docs / 'notes.md'}, {docs / 'changelog.md'}, "
                f"{docs / 'config.txt'} — exactly one does not exist. Name it. "
                f"Answer with the file name only."
            ),
            "expect_in_answer": ["changelog.md"],
            "reject_in_answer": ["notes.md", "config.txt"],
        },
        {
            "id": "hard-two-files-disagree",
            "prompt": (
                f"{docs / 'limits-old.ini'} and {docs / 'limits-new.ini'} both set `max_users`. "
                f"Do they agree? Answer with both values and the word AGREE or DISAGREE."
            ),
            "expect_in_answer": ["50", "200", "disagree"],
        },
        {
            "id": "hard-refuse-to-invent",
            "prompt": (
                f"What is the value of `timeout` in {docs / 'config.txt'}? "
                f"If the key is not there, answer exactly NOT PRESENT."
            ),
            "expect_in_answer": ["not present"],
            # The failure being tested is a plausible number offered to please.
            "reject_in_answer": ["timeout=", "30", "60"],
        },
        {
            "id": "hard-order-by-size",
            "prompt": (
                f"Sort these by file size, smallest first: {docs / 'empty.log'}, "
                f"{docs / 'config.txt'}, {docs / 'notes.md'}. Answer with the three names "
                f"in order, separated by commas."
            ),
            "expect_ordered": ["empty.log", "config.txt", "notes.md"],
        },
        {
            "id": "hard-nested-json-field",
            "prompt": (
                f"From {docs / 'deploy-b.json'}, what is the release channel? One word."
            ),
            "expect_in_answer": ["stable"],
            "reject_in_answer": ["old"],
        },
        {
            "id": "hard-count-with-a-filter",
            "prompt": (
                f"How many lines in {docs / 'notes.md'} start with a dash? Answer with a number."
            ),
            "expect_in_answer": ["3"],
        },
    ]
