"""A commit message is public and permanent; a chat line is neither.

On 2026-08-28 Mike found his own words in the body of `28982c3b` — quoted from
the team room, in Russian, profanity included, in a repository anyone can read.
Counted after: 10 of the 432 commits on `dev` carried Cyrillic, nearly all of
them somebody's chat line. One of them was three weeks old, so this was a habit
and not a slip, and a habit is what a hook is for.

Two rules, and they are deliberately different in strength.

**Cyrillic is refused.** The project writes English; a Russian sentence in a
commit message is either a quote from the room or a note to ourselves, and both
belong somewhere else. Punctuation is not script: em dashes, guillemets and §
pass, because the rule is about language, not about typography.

**A name followed by a quotation warns.** «Mike: "…"» is the shape of a
republished chat line even when it is in English, and the warning names the
alternative rather than blocking: attribute the decision, do not transcribe the
sentence.

Escape hatch: `git commit --no-verify`. It exists, it is not hidden, and using
it deliberately is a different act from never having been asked.
"""
import re
import sys
from typing import List, Tuple

CYRILLIC = re.compile(r"[Ѐ-ӿԀ-ԯ]")

# A teammate's name at the head of a line, then something in quotes.
QUOTED_CHAT = re.compile(
    r"^\s*(Mike|Ark|Johnny|Warren|Iris|Chado)\b[^\n]{0,40}?[\"«“]",
    re.MULTILINE,
)


def strip_comments(message: str) -> str:
    """Git's own commentary is not part of what gets stored."""
    return "\n".join(l for l in message.splitlines() if not l.startswith("#"))


def check(message: str) -> Tuple[List[str], List[str]]:
    """Return (refusals, warnings) for a commit message."""
    body = strip_comments(message)
    refusals: List[str] = []
    warnings: List[str] = []

    for number, line in enumerate(body.splitlines(), start=1):
        if CYRILLIC.search(line):
            refusals.append(f"line {number}: {line.strip()[:80]}")

    for match in QUOTED_CHAT.finditer(body):
        line = body[: match.start()].count("\n") + 1
        warnings.append(f"line {line}: {match.group(0).strip()[:60]}…")

    return refusals, warnings


def main(argv: List[str]) -> int:
    # The message being refused is not ASCII by definition, and a Windows console
    # is not UTF-8 by default — without this the reason for the refusal prints as
    # escapes, which is a poor way to explain a rule.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if len(argv) < 2:
        print("commit-msg: no message file given", file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8", errors="replace") as handle:
        message = handle.read()

    refusals, warnings = check(message)

    for warning in warnings:
        print(f"commit-msg: a teammate's name beside a quotation — {warning}", file=sys.stderr)
    if warnings:
        print("commit-msg: attribute the decision rather than transcribing the sentence.\n",
              file=sys.stderr)

    if not refusals:
        return 0

    print("commit-msg: refused — the message carries Cyrillic, and a commit message is\n"
          "public and permanent. Write the reason in English; a verbatim line from the\n"
          "room belongs in the backlog entry or the ADR.\n", file=sys.stderr)
    for refusal in refusals:
        print(f"  {refusal}", file=sys.stderr)
    print("\n  (deliberate exception: git commit --no-verify)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
