"""
ADR-008: Migrate flat archive/ layout to YYYY/MM subdirectory structure.

Atomic migration: copies files first, verifies, then removes originals.
If interrupted, original files remain in place and rglob readers still find them.

Usage:
    python -m dpc_client_core.archive_migration          # dry-run
    python -m dpc_client_core.archive_migration --apply   # actually move files
"""

import logging
import re
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches: 2026-04-12T21-17-18_reset_session.json
ARCHIVE_RE = re.compile(r"^(\d{4})-(\d{2})-\d{2}T.+_session\.json$")


def migrate_archives(dpc_dir: Path | None = None, dry_run: bool = True) -> dict:
    """Migrate flat archive files into YYYY/MM subdirectories.

    Args:
        dpc_dir: Path to ~/.dpc (default: Path.home() / ".dpc")
        dry_run: If True, only report what would be moved

    Returns:
        Dict with moved_count, skipped_count, errors
    """
    if dpc_dir is None:
        dpc_dir = Path.home() / ".dpc"

    conversations_dir = dpc_dir / "conversations"
    if not conversations_dir.exists():
        return {"moved": 0, "skipped": 0, "errors": []}

    moved = 0
    skipped = 0
    errors = []

    for conv_dir in conversations_dir.iterdir():
        if not conv_dir.is_dir():
            continue
        archive_dir = conv_dir / "archive"
        if not archive_dir.exists():
            continue

        # Only process files directly in archive/ (not already in YYYY/MM/)
        for f in sorted(archive_dir.glob("*_session.json")):
            if not f.is_file():
                continue
            m = ARCHIVE_RE.match(f.name)
            if not m:
                skipped += 1
                continue

            year, month = m.group(1), m.group(2)
            dest_dir = archive_dir / year / month
            dest_path = dest_dir / f.name

            if dest_path.exists():
                skipped += 1
                continue

            if dry_run:
                logger.info(f"[DRY-RUN] Would move: {f} -> {dest_path}")
                moved += 1
            else:
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(f), str(dest_path))
                    # Verify copy before removing original
                    if dest_path.exists() and dest_path.stat().st_size == f.stat().st_size:
                        f.unlink()
                        moved += 1
                        logger.info(f"Migrated: {f.name} -> {year}/{month}/")
                    else:
                        dest_path.unlink(missing_ok=True)
                        errors.append(f"Copy verification failed: {f.name}")
                except Exception as e:
                    errors.append(f"{f.name}: {e}")

        # Clean up old/ subdirectory if it exists and is empty
        old_dir = archive_dir / "old"
        if old_dir.exists() and not any(old_dir.iterdir()):
            try:
                old_dir.rmdir()
            except OSError:
                pass

    result = {"moved": moved, "skipped": skipped, "errors": errors}
    logger.info(f"Archive migration {'(dry-run) ' if dry_run else ''}complete: {result}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply = "--apply" in sys.argv
    if not apply:
        print("Dry-run mode. Use --apply to actually move files.\n")
    result = migrate_archives(dry_run=not apply)
    print(f"\nResult: {result}")
