"""Phase A benchmark — query set extraction (KG-GRAFEO-RETRIEVAL-MIGRATION).

Reads user messages from `~/.dpc/conversations/agent_001/history.json`
plus all `archive/**/*.json` sessions, applies basic filters, and
writes a JSON list of candidate queries for the retrieval benchmark.

This is step 1 of Phase A: produce the input query set. Dedup against
near-duplicates happens in a separate pass (BGE-M3 cosine, requires the
embedding pipeline — kept out of this script to keep it dependency-free).

Run:
    python -m tests.perf.extract_query_set --out queries.json
    python -m tests.perf.extract_query_set --out queries.json --max 100
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

# Heuristics tuned for DPC chat-style queries:
#   - keep messages where Mike asked for information / reasoning (not pure commands)
#   - drop super-short pings ("ага", "ok") and command-only verbs ("делай", "коммить")
MIN_LEN_CHARS = 25
COMMAND_ONLY = re.compile(
    r"^\s*(делай|коммить|делаем|пуш|стоп|пробуй|погнали|давай|ок|ага|да|нет)[\s.!?]*$",
    re.IGNORECASE,
)
USER_SENDERS = frozenset({"Mike Windows PC", "User", "Mike"})


def iter_history_files(home: Path) -> Iterator[Path]:
    """Yield current history.json + all archived session files."""
    base = home / ".dpc" / "conversations" / "agent_001"
    current = base / "history.json"
    if current.exists():
        yield current
    archive_root = base / "archive"
    if archive_root.exists():
        yield from sorted(archive_root.rglob("*.json"))


def extract_user_messages(history_file: Path) -> Iterator[str]:
    """Yield raw text of user-role messages from a history file."""
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for msg in data.get("messages", []):
        sender = msg.get("sender_name", "")
        if sender not in USER_SENDERS:
            continue
        content = (msg.get("content") or "").strip()
        if content:
            yield content


def is_query_candidate(text: str) -> bool:
    """True if message looks like a substantive query (not a bare command)."""
    if len(text) < MIN_LEN_CHARS:
        return False
    if COMMAND_ONLY.match(text):
        return False
    return True


def collect_queries(home: Path, max_count: int | None) -> list[dict]:
    """Walk all history files, return deduplicated-by-text candidate queries."""
    seen_texts: set[str] = set()
    out: list[dict] = []
    for hf in iter_history_files(home):
        for text in extract_user_messages(hf):
            if not is_query_candidate(text):
                continue
            # Cheap exact-text dedup. Near-duplicate (BGE-M3 cosine > 0.95)
            # is intentionally deferred to a separate pass.
            normalized = " ".join(text.split())
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            out.append({"text": text, "source_file": str(hf.name)})
            if max_count is not None and len(out) >= max_count:
                return out
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True,
        help="Output JSON path",
    )
    parser.add_argument(
        "--max", type=int, default=None,
        help="Cap on number of queries (default: no cap)",
    )
    parser.add_argument(
        "--home", type=Path,
        default=Path(os.environ.get("DPC_HOME_USER", Path.home())),
        help="User home dir (looks for .dpc/conversations/agent_001). "
             "Override for testing against a fixture.",
    )
    args = parser.parse_args()

    queries = collect_queries(args.home, args.max)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"queries": queries, "count": len(queries)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Extracted {len(queries)} query candidates → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
