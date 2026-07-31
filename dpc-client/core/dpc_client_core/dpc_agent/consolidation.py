"""Memory consolidation (ADR-010, MEM-X.1 + MEM-X.3).

Tier 1 (auto): refresh _meta.json stats + reshuffle _index.md.
Tier 2 (manual): propose merges/archives for user approval.
"""

from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone, timedelta
from typing import List

from .memory import (
    read_all_meta, write_all_meta, read_file_meta, write_file_meta,
    FileMeta, generate_smart_index, last_touched,
)

log = logging.getLogger(__name__)

STALE_DAYS = 30


def tier1_consolidate(knowledge_dir: pathlib.Path) -> dict:
    """Auto consolidation: mark stale files, refresh _index.md."""
    all_meta = read_all_meta(knowledge_dir)
    if not all_meta:
        return {"stale_marked": 0, "total": 0}

    now = datetime.now(timezone.utc)
    stale_count = 0

    for fname, entry in all_meta.items():
        touched = last_touched(entry)
        if touched is None:
            continue
        if (now - touched).days > STALE_DAYS:
            entry["stale"] = True
            stale_count += 1
        else:
            entry["stale"] = False

    write_all_meta(knowledge_dir, all_meta)
    generate_smart_index(knowledge_dir)

    log.info("Tier 1 consolidation: %d stale of %d files", stale_count, len(all_meta))
    return {"stale_marked": stale_count, "total": len(all_meta)}


def tier2_propose(knowledge_dir: pathlib.Path) -> List[dict]:
    """Manual consolidation: propose merges/archives for user review."""
    all_meta = read_all_meta(knowledge_dir)
    proposals = []

    now = datetime.now(timezone.utc)

    for fname, entry in all_meta.items():
        touched = last_touched(entry)
        read_count = entry.get("access_count", 0)

        if touched is None:
            proposals.append({
                "file": fname, "action": "archive",
                "reason": "never read and never written",
            })
            continue

        days = (now - touched).days
        # Reads decide, and age is measured from the last sign of interest of either
        # kind. A document read often is not a candidate however long ago it was
        # written — which is exactly the case this code got wrong, because the number
        # it called reads was counting writes.
        if days > STALE_DAYS and read_count <= 1:
            proposals.append({
                "file": fname, "action": "archive",
                "reason": f"untouched for {days} days, read {read_count} time(s)",
            })

    return proposals
