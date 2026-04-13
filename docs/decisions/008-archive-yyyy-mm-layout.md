# ADR-008: Archive Restructure to YYYY/MM Nested Layout

**Status:** Proposed
**Date:** 2026-04-11
**Authors:** Mike (direction), CC (code analysis), Ark (reader territory)
**Scope:** Batch 2 — session archive storage + reader contract + UI simplification
**Prerequisite:** Batch 1 + Batch 1.1 (S25) — archive stats panel functional

---

## Context

Session archives are written by `ConversationMonitor._archive_current_session()` when a user/system resets a conversation. Each archive captures the current `history.json` plus metadata and is dropped into `~/.dpc/conversations/{id}/archive/` as a flat layout:

```
archive/
  2026-04-01T17-27-00_reset_session.json
  2026-04-01T19-19-54_reset_session.json
  ...
  2026-04-10T22-06-23_reset_session.json
  (flat, prune-oldest-when-exceeded)
```

Current behavior has three issues:

1. **Hard cap (pre-Batch 1):** `min(50, ...)` in firewall.py + agent_manager.py constrained max_archived_sessions to 50. Raised to 200 in Batch 1 but still an artificial ceiling.
2. **Prune-by-count:** oldest archive is deleted when limit reached. Mike (S25 [23]) stated explicitly: *"всё без удаления, странно что ты не понимаешь ценность этой информации"* — session history is primary memory for the three-agent workflow and must not be lost.
3. **Flat layout scaling:** 39 files in a single flat directory is manageable, but projected ~1,500/year (at ~4 resets/day) makes navigation unusable for humans and inefficient for `ls`/`glob`.

Mike proposed in S25 [19]: *"А почему не archive/YYYY/MM/? как по мне это удобнее есть папка год, внутри папки с месяцами, внутри которых сессии."*

### Why now

Batch 1 + 1.1 restored archive stats functionality (S25 `f94fc6f`, `ec2a5e2`), exposing the artificial cap issue visibly. Moving to unlimited+nested is the natural next step, small enough for a single sprint but with enough cross-component impact to require ADR-level design capture.

---

## Decision

Migrate session archive storage from flat layout to nested `YYYY/MM/` subfolders, remove prune-by-count logic, add one-shot migration for existing conversations.

### Core design decisions

| Decision | Choice | Reason |
|---|---|---|
| Folder structure | `archive/YYYY/MM/{ts}_{reason}_session.json` | Mike [19] — Explorer navigation, scales to years, standard for log rotation |
| Prune policy | Removed entirely (no pruning) | Mike [23] — session history is primary co-evolution memory |
| Reader lookup | Recursive glob `archive_dir.glob('**/*_session.json')` | Layout-agnostic, backward-compatible with flat files during transition |
| `read_session_detail` filename resolution | `archive_dir.glob(f"**/{filename}")` with first match | Preserves existing flat-filename contract, no breaking API change (Ark rec, CC concurred S25 [65]) |
| Migration | One-shot synchronous script at backend startup, walks all `conversations/*/archive/` | Only agent_001 has flat files today (39); future-proof for new conversations |
| Migration idempotency | `.archive_layout_v2` marker file created **last** after all moves succeed | Safe to re-run; if crash mid-migration, next start resumes |
| Config migration | `dpc_agent.history.max_archived_sessions` becomes no-op field in `privacy_rules.json` | No destructive edit; preserve backward compat; room to repurpose later (prune-by-age) |
| UI dropdown | Removed; replaced with disk usage display | Cap is meaningless when storage is unlimited; transparency via byte count |
| Digest path | Change `archive_path.parent.parent` → `self.history_path.parent / "digest.jsonl"` | Current calculation breaks with nesting; digest must always sit at conversation level, not inside archive/ |
| Cross-platform | Pathlib + `shutil` only; no `os.sep`, no shell commands | Baseline already cross-platform; Batch 2 preserves this |

---

## Design

### Writer path

**`conversation_monitor.py:_archive_current_session()`** (L1482-1534 pre-change):

```python
# Before (flat):
archive_dir = path.parent / "archive"
archive_dir.mkdir(parents=True, exist_ok=True)
archive_path = archive_dir / f"{ts}_{reason}_session.json"
# ... write ...
# Then prune:
archives = sorted(archive_dir.glob("*_session.json"))
if len(archives) > max_sessions:
    for old in archives[:len(archives) - max_sessions]:
        old.unlink()
return len(archives)

# After (nested, no prune):
archive_base = path.parent / "archive"
year_month_dir = archive_base / ts[:4] / ts[5:7]   # YYYY/MM
year_month_dir.mkdir(parents=True, exist_ok=True)
archive_path = year_month_dir / f"{ts}_{reason}_session.json"
# ... write ...
# Count all (across subdirs) for return value, no prune:
return sum(1 for _ in archive_base.glob("**/*_session.json"))
```

**`conversation_monitor.py:_generate_session_digest()`** (L1536+): fix digest path calculation.

```python
# Before:
digest_path = archive_path.parent.parent / "digest.jsonl"

# After:
digest_path = self.history_path.parent / "digest.jsonl"
```

This also makes digest.jsonl location independent of archive layout — evolution.py:394 continues to find it.

### Reader path

**`dpc_agent/tools/archive.py:read_session_archive`** (L55):

```python
# Before:
archive_files = sorted(archive_dir.glob("*_session.json"))

# After:
archive_files = sorted(archive_dir.glob("**/*_session.json"))
```

**`dpc_agent/tools/archive.py:read_session_detail`** (L135):

```python
# Before:
archive_path = archive_dir / filename

# After:
matches = list(archive_dir.glob(f"**/{filename}"))
if not matches:
    return f"Archive file not found: {filename}"
archive_path = matches[0]
```

The security check (`archive_path.resolve().relative_to(archive_dir.resolve())`) at L140 already works recursively — no change needed.

### Backend service

**`service.py:get_session_archive_info`** (L3935) and **`clear_session_archives`** (L3980):

```python
# Before:
archives = sorted(archive_dir.glob("*_session.json"))

# After:
archives = sorted(archive_dir.glob("**/*_session.json"))
```

`clear_session_archives` additionally uses `shutil.rmtree(archive_dir); archive_dir.mkdir(exist_ok=True)` for clean wipe across subdirectories.

### Frontend

**`AgentPermissionsPanel.svelte`:**

- Remove the `<input type="number" max="200">` dropdown for `max_archived_sessions`.
- Keep the "Preserve session history on reset" toggle.
- Add disk usage display: `Archive: 39 sessions, 9.2 MB`.
- Keep `View archive` and `Clear all archives` buttons.

**`FirewallEditor.svelte`:** no changes — cap is still in privacy_rules.json as no-op field, dropdown simply hidden by panel.

### Migration

**`scripts/migrate_archive_to_yyyy_mm.py`** (new file, ~35 lines):

```python
#!/usr/bin/env python
"""One-shot migration: flat archive/*_session.json → archive/YYYY/MM/*_session.json"""
import shutil
from pathlib import Path

MARKER = ".archive_layout_v2"

def migrate_one(archive_dir: Path) -> int:
    if (archive_dir / MARKER).exists():
        return 0  # already migrated
    if not archive_dir.is_dir():
        return 0
    flat_files = list(archive_dir.glob("*_session.json"))
    if not flat_files:
        (archive_dir / MARKER).write_text("2", encoding="utf-8")
        return 0
    moved = 0
    for src in flat_files:
        ts = src.name[:10]  # YYYY-MM-DD
        year, month = ts[:4], ts[5:7]
        dst_dir = archive_dir / year / month
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst_dir / src.name))
        moved += 1
    (archive_dir / MARKER).write_text("2", encoding="utf-8")
    return moved

def migrate_all() -> dict:
    conversations_root = Path.home() / ".dpc" / "conversations"
    results = {}
    for conv_dir in conversations_root.iterdir():
        if not conv_dir.is_dir():
            continue
        archive_dir = conv_dir / "archive"
        if archive_dir.is_dir():
            results[conv_dir.name] = migrate_one(archive_dir)
    return results

if __name__ == "__main__":
    for name, moved in migrate_all().items():
        print(f"{name}: moved {moved} files")
```

### Startup hook

**`service.py:CoreService.start()`** — add synchronous call before local_api server binds:

```python
# At the top of start(), before anything else:
from pathlib import Path
from scripts.migrate_archive_to_yyyy_mm import migrate_all
results = migrate_all()
if any(moved for moved in results.values()):
    logger.info("Archive migration moved files: %s", results)
```

Migration runs once per upgrade, idempotent, synchronous. Guarantees no race with agent loop or UI commands (both start later in the same `start()` sequence).

---

## Migration plan

1. Write ADR-008 (this file) — Mike/Ark review pending.
2. Implement Writer + Reader + Service changes.
3. Write migration script.
4. Add startup hook.
5. Update UI (remove dropdown, add disk usage).
6. Test locally on agent_001 (39 archives → move to `archive/2026/04/`).
7. Smoke test 6 steps (below).
8. Commit as single `feat(archive): YYYY/MM layout + unlimited storage (S26 Batch 2)`.
9. Mike restarts backend → migration runs → Batch 2 verified.
10. Add note to MEMORY.md: flat layout deprecated after S26.

---

## Testing checklist (pre-merge)

1. **Migration idempotent:** run backend twice, second start does nothing (marker file stops it).
2. **New reset:** reset conversation in agent_001 → new archive lands in `archive/2026/04/` not flat.
3. **Ark reader:** `read_session_archive(last_n=3)` returns 3 files from `archive/2026/04/` correctly.
4. **Ark reader detail:** `read_session_detail("2026-04-10T22-06-23_reset_session.json")` resolves the file via recursive glob and returns body.
5. **UI stats panel:** `Firewall Editor → Agent Permissions → agent_001` shows correct count (`Archive: 39 sessions, ~9 MB`).
6. **Evolution digest:** next evolution cycle reads `conversations/agent_001/digest.jsonl` — not empty, not inside `archive/2026/`.
7. **Clear all archives:** button wipes the entire tree and recreates empty `archive/` + marker.

---

## Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| `digest.jsonl` written to wrong location after restructure | HIGH | Fix `_generate_session_digest` to use `history_path.parent`, not `archive_path.parent.parent` |
| `read_session_detail` fails for flat-named filename | HIGH | Use recursive glob `**/{filename}` with first match (Ark recommendation, backward compat) |
| Migration race with agent loop | MEDIUM | Run migration synchronously before `local_api` server binds and before agent `start()` |
| File lock on Windows during migration | LOW | Migration runs before any consumer opens files; no shared handles |
| Partial migration after crash | LOW | Marker file written last; resume on next start picks up remaining files |
| Groups/P2P/AI chat break | LOW | All conversations share the same `ConversationMonitor._archive_current_session` path — single fix covers all |
| Cross-platform path issues | LOW | Baseline already uses `pathlib.Path` exclusively; Batch 2 preserves this |
| Config file mutation drift | LOW | `dpc_agent.history.max_archived_sessions` kept as no-op field; no destructive privacy_rules.json edit |

---

## Scope estimate

| Component | Lines | Files |
|---|---|---|
| `conversation_monitor.py:_archive_current_session` (writer + prune removal + count) | ~20 | 1 |
| `conversation_monitor.py:_generate_session_digest` (digest path fix) | ~2 | (same) |
| `dpc_agent/tools/archive.py` (recursive glob + filename resolver) | ~5 | 1 |
| `service.py:get_session_archive_info` + `clear_session_archives` (recursive glob + rmtree) | ~10 | 1 |
| `AgentPermissionsPanel.svelte` (remove dropdown, add disk usage) | ~15 | 1 |
| `scripts/migrate_archive_to_yyyy_mm.py` (new file) | ~35 | 1 (new) |
| `service.py` startup hook (synchronous call to migrate_all) | ~8 | (same) |

**Total:** ~95 lines, 4 files modified + 1 new script.

Note: `firewall.py` and `agent_manager.py` are NOT modified — `max_archived_sessions` field stays no-op for backward compat. Cap semantics frozen at Batch 1 values (1-200 range in UI input, which is now hidden anyway).

---

## Out of scope (deferred)

- **Prune by age** (e.g. delete months older than 12) — can be added later as opt-in feature using the no-op config field. Not needed now; Mike chose "всё без удаления".
- **Disk usage warning thresholds** — frontend could warn if usage exceeds N GB. Cosmetic, defer.
- **Cross-platform CI** — backlog `INFRA-1` candidate, separate work.
- **Archive compression** (gzip old files) — backlog candidate if disk usage becomes an issue. Projected 2MB/month × 12 = 24 MB/year, negligible.

---

## Status

**Proposed (2026-04-11, S25).** Awaiting Mike + Ark review.

**Next steps:**
1. Mike review of this ADR.
2. Ark review (especially reader contract preservation).
3. Execution in next session (S26 or later) once review cleared.
4. Post-merge: this ADR moves to Accepted.

---

## References

- S25 session log: discussion from msg [19] through [72]
- Batch 1 commit: `f94fc6f` (allowlist + search preview + archive cap/default)
- Batch 1.1 commit: `ec2a5e2` (expectsResponse + per-agent override)
- S24 lesson: "write-side optimized, read-side neglected" — motivated lockstep reader update
- Backlog items: `UI-2` (retry transparency), `UI-3` (message indices), `ARCH-12` (image visibility), `INFRA-1` (cross-platform CI)
