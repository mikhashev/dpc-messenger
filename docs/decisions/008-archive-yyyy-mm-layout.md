# ADR-008: Archive YYYY/MM Nested Layout + Unlimited Retention

**Status:** Accepted (with modifications from initial proposal)
**Date proposed:** 2026-04-11 (S25)
**Date accepted:** 2026-04-17 (S46)
**Implemented across:** S33 (commit `762bb55`), S45 (commits `8009ae6`, `fc628cf`, `955ac02`)
**Authors:** Mike (direction), CC (code analysis + implementation), Ark (reader contract review)
**Scope:** Session archive storage + reader contract + retention policy

---

## Context

Session archives are written by `ConversationMonitor._archive_current_session()` when a user/system resets a conversation. Each archive captures the current `history.json` plus metadata and was originally dropped into `~/.dpc/conversations/{id}/archive/` as a flat layout:

```
archive/
  2026-04-01T17-27-00_reset_session.json
  2026-04-01T19-19-54_reset_session.json
  ...
  (flat, prune-oldest-when-exceeded)
```

Pre-ADR behaviour had three issues:

1. **Hard cap (pre-Batch 1):** `min(50, ...)` in `firewall.py` + `agent_manager.py` constrained `max_archived_sessions` to 50. Raised to 200 in Batch 1 but still an artificial ceiling.
2. **Prune-by-count:** oldest archive was deleted when limit reached. Mike (S25 [23]) stated explicitly: *"всё без удаления, странно что ты не понимаешь ценность этой информации"* — session history is primary memory for the three-agent workflow and must not be silently lost.
3. **Flat layout scaling:** 39 files in a single flat directory is manageable, but projected ~1,500/year (at ~4 resets/day) makes navigation unusable for humans and inefficient for `ls`/`glob`.

Mike proposed in S25 [19]: *"А почему не archive/YYYY/MM/? как по мне это удобнее есть папка год, внутри папки с месяцами, внутри которых сессии."*

---

## Decision (as implemented)

Session archives live under `archive/YYYY/MM/{ts}_{reason}_session.json` with configurable per-agent retention. Default `max_archived_sessions = 0` means "keep all"; a positive integer sets a per-agent cap. Readers use recursive glob and are agnostic to layout.

### Core design (as shipped)

| Aspect | Choice | Reason |
|---|---|---|
| Folder structure | `archive/YYYY/MM/{ts}_{reason}_session.json` | Mike S25 [19] — Explorer navigation, scales to years, standard for log rotation |
| Reader lookup | Recursive glob `archive_dir.glob('**/*_session.json')` | Layout-agnostic, backward-compatible with flat files during transition |
| `read_session_detail` filename | `archive_dir.glob(f"**/{filename}")` with first match | Preserves flat-filename contract, no breaking API change |
| Retention default | `max_archived_sessions = 0` = unlimited (ARCH-19) | Mike S25 [23] — session history is primary co-evolution memory |
| Retention when user sets >0 | Pruning guarded by `if max_sessions > 0 and len(archives) > max_sessions` | Honours explicit caps without surprising default users |
| Upper bound | Removed (was 200) | Users set whatever limit they want, or leave unlimited |
| UI input | Kept (min=0, no upper cap); shows "Unlimited" when 0; progress bar hidden when unlimited | Transparency over removal; preserves user control |
| `firewall.py` + `agent_manager.py` | Updated to plumb new default/clamp | Required for end-to-end default propagation |
| Digest path | `self.history_path.parent / "digest.jsonl"` | Independent of archive layout; `evolution.py:394` digest reading unbroken |
| Back-compat | Existing configs with `max=40` keep their cap | No destructive config migration, zero breaking change |
| Migration | One-shot inline move in S33, no persistent script | 47-file relocation is one-shot; over-engineering avoided |
| Cross-platform | `pathlib` + `shutil` only, no `os.sep`, no shell | Baseline already cross-platform |

---

## Design

### Writer path

`conversation_monitor.py:_archive_current_session()` writes to nested `YYYY/MM/` and guards the prune loop:

```python
archive_base = path.parent / "archive"
year_month_dir = archive_base / ts[:4] / ts[5:7]   # YYYY/MM
year_month_dir.mkdir(parents=True, exist_ok=True)
archive_path = year_month_dir / f"{ts}_{reason}_session.json"
# ... write ...
archives = sorted(archive_base.glob("**/*_session.json"))
if max_sessions > 0 and len(archives) > max_sessions:
    for old in archives[:len(archives) - max_sessions]:
        old.unlink()
return len(archives)
```

`_generate_session_digest()` uses `self.history_path.parent / "digest.jsonl"` so digest location is independent of archive layout.

### Reader path

`dpc_agent/tools/archive.py`:
- `read_session_archive`: `sorted(archive_dir.glob("**/*_session.json"))` with `offset` support (ARCH-17)
- `read_session_detail`: `archive_dir.glob(f"**/{filename}")` with first match, plus `offset` / `max_message_chars` / `include_thinking` (ARCH-17)
- `search_session_archives`: new tool (ARCH-18) for case-insensitive substring search across archives (scope: `content` | `thinking` | `both`)

### Backend service

`service.py:get_session_archive_info` and `clear_session_archives` use recursive glob + `shutil.rmtree` with the new default/clamp pattern (0 = unlimited).

### Frontend

`AgentPermissionsPanel.svelte`:
- Input field kept for `max_archived_sessions`, `min=0`, no `max` attribute
- Displays "Unlimited" label when value is 0
- Progress bar hidden when unlimited
- `archiveInfo` fallback defaults to 0 (unlimited)

`FirewallEditor.svelte`: `archiveInfo.max_sessions` fallback updated to 0.

---

## Implementation commits

| Sprint | Commit | Scope |
|---|---|---|
| S33 | `762bb55` | YYYY/MM layout (writer + reader + service). Included `archive_migration.py` initially; removed later in same sprint as over-engineering (inline move was sufficient for one-time 47-file relocation). |
| S45 | `8009ae6` | ARCH-17 pagination: `offset`, `max_message_chars`, `include_thinking` on `read_session_detail`; `offset` on `read_session_archive`; `last_n` cap 10→50 |
| S45 | `fc628cf` | ARCH-18 `search_session_archives` tool: case-insensitive substring match across archives, scope `content` / `thinking` / `both`, pagination via `offset` |
| S45 | `955ac02` | ARCH-19 unlimited retention default: default 40→0, remove 200 clamp, prune guard, UI "Unlimited" display, progress bar hidden when unlimited |

---

## Delta from initial proposal

The initial S25 proposal (preserved below) was implemented with three material modifications:

1. **Retention policy.** Proposed: *"remove pruning entirely, `max_archived_sessions` becomes no-op field"*. Implemented: **configurable with default=0=unlimited**; pruning retained behind `max_sessions > 0` guard. Less invasive, back-compat preserved — existing configs with `max=40` keep their cap.

2. **UI treatment.** Proposed: *"remove dropdown, replace with disk usage display"*. Implemented: **input kept with min=0 and no upper cap, showing "Unlimited" when 0**; progress bar hidden when unlimited. Gives users control without forcing a hidden default.

3. **Touch surface.** Proposed: *"`firewall.py` and `agent_manager.py` NOT modified"*. Implemented: **both modified** to plumb the new default and clamp pattern. Required for end-to-end default propagation across all read sites.

Rationale: the ARCH-19 approach is less invasive, preserves user control, and keeps the configurable surface consistent with the rest of the agent permissions UI. The initial proposal's "remove it entirely" framing was structurally clean but traded back-compat for design purity; the implemented design keeps both.

---

## Testing checklist (all passed)

1. ✅ New reset lands in `archive/YYYY/MM/` (S33)
2. ✅ `read_session_archive(last_n=3)` works with nested layout (S33, re-verified ARCH-17)
3. ✅ `read_session_detail(filename)` resolves via recursive glob (S33)
4. ✅ Evolution digest reads `conversations/{id}/digest.jsonl` correctly (S33)
5. ✅ Clear-all-archives wipes tree and recreates empty `archive/` (S33)
6. ✅ Pagination — offset / max_message_chars / include_thinking tested by Ark (ARCH-17, S45)
7. ✅ `search_session_archives` verified by Ark on 494-line code review (ARCH-18, S45)
8. ✅ Unlimited default — Mike UI PASS ("В UI поменял на unlimeted"), Ark backend PASS across 4 paths (ARCH-19, S45)
9. ✅ Back-compat — existing `max=40` configs keep their cap (ARCH-19, S45)

---

## Risks & mitigations (outcome)

| Risk | Severity | Outcome |
|---|---|---|
| `digest.jsonl` written to wrong location after restructure | HIGH | Mitigated in S33 via `self.history_path.parent` digest path |
| `read_session_detail` fails for flat-named filename | HIGH | Mitigated via recursive glob `**/{filename}` with first match |
| Migration race with agent loop | MEDIUM | No persistent migration script shipped; S33 did inline one-shot move before consumers started |
| File lock on Windows during migration | LOW | No issue observed |
| Groups/P2P/AI chat break | LOW | All conversations share same `_archive_current_session` path; single fix covered all |
| Cross-platform path issues | LOW | `pathlib`-only, no regressions |
| Config migration drift | LOW | `max_archived_sessions` remains configurable (not no-op as proposed); default change only, existing configs preserved |

---

## Initial Proposal (2026-04-11, S25 — superseded 2026-04-17)

This section preserves the original ADR-008 proposal for historical reference. The as-implemented decision above supersedes this where they differ — see **Delta from initial proposal**.

### Original core design decisions

| Decision | Choice | Reason |
|---|---|---|
| Folder structure | `archive/YYYY/MM/{ts}_{reason}_session.json` | Mike [19] — Explorer navigation, scales to years |
| Prune policy | **Removed entirely** (no pruning) | Mike [23] — session history is primary co-evolution memory |
| Reader lookup | Recursive glob `archive_dir.glob('**/*_session.json')` | Layout-agnostic |
| `read_session_detail` filename | `archive_dir.glob(f"**/{filename}")` with first match | Preserves existing contract (Ark rec, CC concurred S25 [65]) |
| Migration | One-shot synchronous script at backend startup, walks all `conversations/*/archive/` | Only agent_001 had flat files (39); future-proof for new conversations |
| Migration idempotency | `.archive_layout_v2` marker file created **last** after all moves succeed | Safe to re-run; crash-safe |
| Config migration | `dpc_agent.history.max_archived_sessions` **becomes no-op field** in `privacy_rules.json` | No destructive edit; preserve backward compat; room to repurpose later (prune-by-age) |
| UI dropdown | **Removed**; replaced with disk usage display | Cap meaningless when storage is unlimited; transparency via byte count |
| Digest path | `self.history_path.parent / "digest.jsonl"` | Current calculation breaks with nesting |
| Cross-platform | `pathlib` + `shutil` only | Baseline already cross-platform |

### Original migration plan (as drafted S25)

1. Write ADR-008 — Mike/Ark review pending
2. Implement Writer + Reader + Service changes
3. Write migration script (`scripts/migrate_archive_to_yyyy_mm.py`, ~35 lines)
4. Add startup hook (synchronous `migrate_all()` call before `local_api` binds)
5. Update UI (remove dropdown, add disk usage)
6. Test locally on agent_001 (39 archives → `archive/2026/04/`)
7. Smoke test
8. Commit as single `feat(archive): YYYY/MM layout + unlimited storage (S26 Batch 2)`
9. Mike restarts backend → migration runs → Batch 2 verified
10. Add note to MEMORY.md

### Original scope estimate

~95 lines, 4 files modified + 1 new script.

### Original status

Proposed (2026-04-11, S25). Awaiting Mike + Ark review.

---

## References

- S25 session log: discussion from msg [19] through [72]
- Batch 1 commit: `f94fc6f` (allowlist + search preview + archive cap/default)
- Batch 1.1 commit: `ec2a5e2` (expectsResponse + per-agent override)
- S33 layout commit: `762bb55`
- S45 ARCH-17 pagination: `8009ae6`
- S45 ARCH-18 search: `fc628cf`
- S45 ARCH-19 unlimited default: `955ac02`
- Backlog entries (CLOSED): `ARCH-17`, `ARCH-18`, `ARCH-19` in `backlog.md`
- Related open backlog items: `ARCH-12` (image visibility)
- S24 lesson: "write-side optimized, read-side neglected" — motivated lockstep reader update
