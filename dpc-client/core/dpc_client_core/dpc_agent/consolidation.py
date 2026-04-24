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
    FileMeta, generate_smart_index,
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
        ts = entry.get("last_accessed", "")
        if not ts:
            continue
        try:
            accessed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if (now - accessed).days > STALE_DAYS:
                entry["stale"] = True
                stale_count += 1
            else:
                entry["stale"] = False
        except (ValueError, TypeError):
            pass

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
        ts = entry.get("last_accessed", "")
        access_count = entry.get("access_count", 0)

        if not ts:
            proposals.append({
                "file": fname, "action": "archive",
                "reason": "never accessed",
            })
            continue

        try:
            accessed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            days = (now - accessed).days
        except (ValueError, TypeError):
            continue

        if days > STALE_DAYS and access_count <= 1:
            proposals.append({
                "file": fname, "action": "archive",
                "reason": f"stale ({days} days, accessed {access_count} time(s))",
            })

    return proposals
