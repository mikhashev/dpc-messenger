#!/usr/bin/env python3
"""
PCM Migration Script - Upgrade personal.json from v1.0 to v2.0

Usage:
    python migrate_pcm.py --from v1 --to v2
    python migrate_pcm.py --file /path/to/personal.json
    python migrate_pcm.py --dry-run  # Preview changes without saving
"""

import argparse
import json
import shutil
from pathlib import Path
from datetime import datetime
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.crypto import DPC_HOME_DIR


def backup_file(file_path: Path) -> Path:
    """Create a backup of the original file"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = file_path.parent / f"{file_path.stem}_backup_{timestamp}{file_path.suffix}"
    shutil.copy2(file_path, backup_path)
    print(f"[OK] Backup created: {backup_path}")
    return backup_path


def detect_version(file_path: Path) -> str:
    """Detect the format version of a PCM file"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    metadata = data.get('metadata', {})
    version = metadata.get('format_version', '1.0')

    # Check for v2 indicators
    has_instruction = 'instruction' in data
    has_cognitive_profile = 'cognitive_profile' in data
    has_commit_history = 'commit_history' in data

    if has_instruction or has_cognitive_profile or has_commit_history:
        return '2.0'

    return version


def migrate_v1_to_v2(file_path: Path, dry_run: bool = False) -> None:
    """Migrate PCM file from v1.0 to v2.0"""

    print(f"\nMigrating: {file_path}")
    print(f"   Format: v1.0 -> v2.0")

    # Detect current version
    current_version = detect_version(file_path)
    if current_version == '2.0':
        print(f"Warning: File is already v2.0 format. No migration needed.")
        return

    # Load using PCMCore (handles v1 compatibility)
    core = PCMCore(file_path)

    try:
        context = core.load_context()
        print(f"[OK] Loaded v1.0 context successfully")
        print(f"   - Profile: {context.profile.name}")
        print(f"   - Topics: {len(context.knowledge)}")
    except Exception as e:
        print(f"[ERROR] Error loading context: {e}")
        return

    # Show what will be added
    print(f"\n v2.0 Enhancements:")
    print(f"   + InstructionBlock (AI behavior rules)")
    print(f"   + CognitiveProfile (learning style, bias awareness)")
    print(f"   + Version tracking (commit history)")
    print(f"   + Enhanced KnowledgeEntry (provenance, confidence)")
    print(f"   + Metadata (format version, timestamps)")

    if dry_run:
        print(f"\n DRY RUN - No changes made")
        print(f"\n Preview of v2.0 structure:")
        print(json.dumps({
            "profile": {"name": context.profile.name, "description": context.profile.description},
            "knowledge": f"<{len(context.knowledge)} topics>",
            "instruction": {
                "primary": context.instruction.primary,
                "bias_mitigation": context.instruction.bias_mitigation
            },
            "cognitive_profile": "null (can be added manually)",
            "version": context.version,
            "metadata": context.metadata
        }, indent=2))
        return

    # Create backup
    backup_path = backup_file(file_path)

    # Save as v2.0
    try:
        core.save_context(context)
        print(f"\n[SUCCESS] Migration complete!")
        print(f"   - Format version: 2.0")
        print(f"   - Backup: {backup_path}")
        print(f"\nTIP: Next steps:")
        print(f"   1. Review the migrated file: {file_path}")
        print(f"   2. Customize instruction blocks in the JSON")
        print(f"   3. Add cognitive profile if desired")
        print(f"   4. Original backup kept at: {backup_path}")
    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        print(f"   Restoring from backup...")
        shutil.copy2(backup_path, file_path)
        print(f"   File restored successfully")


def main():
    parser = argparse.ArgumentParser(
        description='Migrate PCM files between format versions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Migrate default personal.json
  python migrate_pcm.py --from v1 --to v2

  # Migrate specific file
  python migrate_pcm.py --file /path/to/personal.json

  # Preview migration without saving
  python migrate_pcm.py --dry-run

  # Migrate all PCM files in a directory
  python migrate_pcm.py --dir ~/.dpc --recursive
        """
    )

    parser.add_argument(
        '--from',
        dest='from_version',
        choices=['v1', 'v1.0'],
        default='v1',
        help='Source format version (default: v1)'
    )

    parser.add_argument(
        '--to',
        dest='to_version',
        choices=['v2', 'v2.0'],
        default='v2',
        help='Target format version (default: v2)'
    )

    parser.add_argument(
        '--file',
        type=Path,
        help='Path to PCM file to migrate (default: ~/.dpc/personal.json)'
    )

    parser.add_argument(
        '--dir',
        type=Path,
        help='Directory containing PCM files to migrate'
    )

    parser.add_argument(
        '--recursive',
        action='store_true',
        help='Recursively migrate files in subdirectories'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview migration without making changes'
    )

    args = parser.parse_args()

    print("=" * 60)
    print("PCM Migration Tool - v1.0 -> v2.0")
    print("=" * 60)

    # Determine files to migrate
    files_to_migrate = []

    if args.file:
        if not args.file.exists():
            print(f"[ERROR] Error: File not found: {args.file}")
            return
        files_to_migrate.append(args.file)
    elif args.dir:
        if not args.dir.exists():
            print(f"[ERROR] Error: Directory not found: {args.dir}")
            return
        pattern = '**/*.json' if args.recursive else '*.json'
        files_to_migrate = list(args.dir.glob(pattern))
    else:
        # Default: migrate ~/.dpc/personal.json
        default_file = DPC_HOME_DIR / 'personal.json'
        if not default_file.exists():
            print(f"[ERROR] Error: Default file not found: {default_file}")
            print(f"   Use --file to specify a custom path")
            return
        files_to_migrate.append(default_file)

    if not files_to_migrate:
        print("[ERROR] No files found to migrate")
        return

    print(f"\n Files to migrate: {len(files_to_migrate)}")
    for file_path in files_to_migrate:
        print(f"   - {file_path}")

    if args.dry_run:
        print(f"\n[WARNING]  DRY RUN MODE - No changes will be saved")

    # Migrate each file
    for file_path in files_to_migrate:
        try:
            migrate_v1_to_v2(file_path, dry_run=args.dry_run)
        except Exception as e:
            print(f"\n[ERROR] Error migrating {file_path}: {e}")
            continue

    print("\n" + "=" * 60)
    print("Migration process complete")
    print("=" * 60)


if __name__ == '__main__':
    main()
