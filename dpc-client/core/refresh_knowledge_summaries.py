"""Refresh the stored summaries of every agent's knowledge files, once.

record_write set `summary` on a file's first write and never again, so _meta.json
and the _index.md built from it kept describing first drafts. The write path is
fixed; this repairs the stores written before the fix.

    refresh_knowledge_summaries.py            # dry run: counts per agent, writes nothing
    refresh_knowledge_summaries.py --apply    # back up, rewrite, rebuild the index

--apply copies _meta.json and _index.md to *.bak-2026-10-05 beside them first.
Entries whose file is missing are counted and left alone.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dpc_client_core.dpc_agent.memory import refresh_summaries  # noqa: E402

AGENTS_DIR = Path.home() / ".dpc" / "agents"
BACKUP_SUFFIX = ".bak-2026-10-05"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--agents-dir", type=Path, default=AGENTS_DIR)
    args = parser.parse_args()

    total = {"changed": 0, "unchanged": 0, "missing_file": 0}
    for meta_path in sorted(args.agents_dir.glob("*/knowledge/_meta.json")):
        kdir = meta_path.parent
        if args.apply:
            for name in ("_meta.json", "_index.md"):
                src = kdir / name
                if src.exists():
                    shutil.copy2(src, kdir / (name + BACKUP_SUFFIX))
        counts = refresh_summaries(kdir, apply=args.apply)
        for k in total:
            total[k] += counts[k]
        print(f"{kdir.parent.name}: {counts}")
    print(f"{'APPLIED' if args.apply else 'DRY RUN'} total: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
