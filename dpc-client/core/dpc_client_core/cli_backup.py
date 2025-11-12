# dpc-client/core/dpc_client_core/cli_backup.py

"""
CLI commands for backup and restore operations.

Usage:
    python -m dpc_client_core.cli_backup create [options]
    python -m dpc_client_core.cli_backup restore [options]
    python -m dpc_client_core.cli_backup verify [options]
"""

import argparse
import sys
import getpass
from pathlib import Path

from .backup_manager import DPCBackupManager


def get_dpc_dir() -> Path:
    """Get default .dpc directory location."""
    return Path.home() / ".dpc"


def get_passphrase(confirm: bool = False) -> str:
    """
    Prompt user for passphrase securely.

    Args:
        confirm: Ask for confirmation (for new backups)

    Returns:
        Passphrase string
    """
    passphrase = getpass.getpass("🔐 Enter passphrase (min 12 chars recommended): ")

    if confirm:
        confirm_pass = getpass.getpass("🔐 Confirm passphrase: ")
        if passphrase != confirm_pass:
            print("❌ Passphrases don't match!")
            sys.exit(1)

    return passphrase


def cmd_create(args):
    """Create encrypted backup."""
    dpc_dir = Path(args.dpc_dir) if args.dpc_dir else get_dpc_dir()

    if not dpc_dir.exists():
        print(f"❌ .dpc directory not found: {dpc_dir}")
        print("   Make sure D-PC Client is initialized")
        sys.exit(1)

    # Get output path
    if args.output:
        output_path = Path(args.output)
    else:
        # Default: ~/dpc_backup_YYYYMMDD_HHMMSS.dpc
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.home() / f"dpc_backup_{timestamp}.dpc"

    # Get passphrase
    if args.passphrase:
        passphrase = args.passphrase
        print("⚠️  Warning: Passphrase provided via command line (insecure)")
    else:
        passphrase = get_passphrase(confirm=True)

    # Check passphrase strength
    if len(passphrase) < 8:
        print("❌ Passphrase too short (minimum 8 characters)")
        sys.exit(1)

    if len(passphrase) < 12:
        print("⚠️  Warning: Passphrase should be at least 12 characters")
        response = input("   Continue anyway? [y/N]: ")
        if response.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    # Create backup
    print()
    print("=" * 60)
    print("Creating Encrypted Backup")
    print("=" * 60)
    print()

    manager = DPCBackupManager(verbose=True)

    try:
        backup_bundle = manager.create_backup(
            dpc_dir=dpc_dir,
            passphrase=passphrase,
            device_name=args.device_name
        )

        manager.save_backup_to_file(backup_bundle, output_path)

        print()
        print("=" * 60)
        print("✅ Backup Created Successfully!")
        print("=" * 60)
        print(f"📂 Backup saved to: {output_path}")
        print(f"📏 Size: {len(backup_bundle):,} bytes ({len(backup_bundle) / 1024 / 1024:.2f} MB)")
        print()
        print("⚠️  IMPORTANT:")
        print("   1. Save your passphrase in a secure location")
        print("   2. Without it, the backup is PERMANENTLY UNRECOVERABLE")
        print("   3. Consider using a password manager (e.g., 1Password, Bitwarden)")
        print()
        print("💡 Next steps:")
        print(f"   - Copy to USB drive: cp {output_path} /media/usb/")
        print(f"   - Upload to cloud: your-cloud-sync-tool {output_path}")
        print(f"   - Restore on new device: dpc backup restore --input {output_path}")
        print()

    except Exception as e:
        print()
        print(f"❌ Backup failed: {e}")
        sys.exit(1)


def cmd_restore(args):
    """Restore from encrypted backup."""
    # Get input path
    if not args.input:
        print("❌ No input file specified. Use --input <backup_file>")
        sys.exit(1)

    input_path = Path(args.input)

    if not input_path.exists():
        print(f"❌ Backup file not found: {input_path}")
        sys.exit(1)

    # Get target directory
    if args.target:
        target_dir = Path(args.target)
    else:
        target_dir = get_dpc_dir()

    # Check if target exists
    if target_dir.exists() and not args.force:
        print(f"⚠️  Target directory already exists: {target_dir}")
        print()
        response = input("   Overwrite existing files? [y/N]: ")
        if response.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    # Get passphrase
    if args.passphrase:
        passphrase = args.passphrase
        print("⚠️  Warning: Passphrase provided via command line (insecure)")
    else:
        passphrase = getpass.getpass("🔐 Enter backup passphrase: ")

    # Restore backup
    print()
    print("=" * 60)
    print("Restoring from Encrypted Backup")
    print("=" * 60)
    print()

    manager = DPCBackupManager(verbose=True)

    try:
        # Load backup
        backup_bundle = manager.load_backup_from_file(input_path)

        # Show metadata
        verification = manager.verify_backup(backup_bundle)
        if verification["valid"]:
            meta = verification.get("metadata", {})
            print()
            print("📄 Backup Information:")
            print(f"   Device: {meta.get('device_name', 'unknown')}")
            print(f"   Created: {meta.get('timestamp', 'unknown')}")
            print(f"   Files: {meta.get('num_files', 'unknown')}")
            print()

        # Restore
        result = manager.restore_backup(
            backup_bundle=backup_bundle,
            passphrase=passphrase,
            target_dir=target_dir,
            overwrite=True
        )

        print()
        print("=" * 60)
        print("✅ Restoration Complete!")
        print("=" * 60)
        print(f"📂 Restored to: {target_dir}")
        print(f"📁 Files restored: {len(result['files_restored'])}")
        print()

        # List restored files
        print("Restored files:")
        for filename in result['files_restored']:
            print(f"   ✅ {filename}")

        print()
        print("⚠️  Important Notes:")
        print("   - Your knowledge, contacts, and identity have been restored")
        print("   - This device now has the SAME node_id as your old device")
        print("   - If both devices connect to Hub, conflicts will occur")
        print()
        print("💡 Recommendation:")
        print("   Generate new node_id for this device to avoid conflicts:")
        print("   $ dpc init --force")
        print()

    except ValueError as e:
        print()
        print(f"❌ Restoration failed: {e}")
        if "Wrong passphrase" in str(e):
            print()
            print("💡 Troubleshooting:")
            print("   1. Double-check your passphrase (case-sensitive)")
            print("   2. Try copying from password manager")
            print("   3. Verify backup file isn't corrupted")
        sys.exit(1)

    except Exception as e:
        print()
        print(f"❌ Restoration failed: {e}")
        sys.exit(1)


def cmd_verify(args):
    """Verify backup integrity without restoring."""
    if not args.input:
        print("❌ No input file specified. Use --input <backup_file>")
        sys.exit(1)

    input_path = Path(args.input)

    if not input_path.exists():
        print(f"❌ Backup file not found: {input_path}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("Verifying Backup Integrity")
    print("=" * 60)
    print()

    manager = DPCBackupManager(verbose=True)

    try:
        backup_bundle = manager.load_backup_from_file(input_path)
        verification = manager.verify_backup(backup_bundle)

        print()
        if verification["valid"]:
            print("✅ Backup is VALID")
            print()
            print("Backup Information:")
            print(f"   Version: {verification['version']}")
            print(f"   Total size: {verification['total_size']:,} bytes")

            meta = verification.get("metadata", {})
            print(f"   Device: {meta.get('device_name', 'unknown')}")
            print(f"   Created: {meta.get('timestamp', 'unknown')}")
            print(f"   Files: {meta.get('num_files', 'unknown')}")
            print()
            print("💡 This backup can be restored with the correct passphrase")
        else:
            print("❌ Backup is INVALID")
            print(f"   Error: {verification.get('error', 'unknown')}")
            sys.exit(1)

    except Exception as e:
        print()
        print(f"❌ Verification failed: {e}")
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="D-PC Backup and Restore Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create backup with default settings
  python -m dpc_client_core.cli_backup create

  # Create backup to specific file
  python -m dpc_client_core.cli_backup create --output ~/my_backup.dpc

  # Create backup with device name
  python -m dpc_client_core.cli_backup create --device-name "Alice's Laptop"

  # Restore backup
  python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc

  # Restore to custom location
  python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc --target ~/test_dpc

  # Verify backup integrity
  python -m dpc_client_core.cli_backup verify --input ~/my_backup.dpc
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Create backup command
    create_parser = subparsers.add_parser('create', help='Create encrypted backup')
    create_parser.add_argument(
        '--output', '-o',
        help='Output file path (default: ~/dpc_backup_TIMESTAMP.dpc)'
    )
    create_parser.add_argument(
        '--dpc-dir',
        help='Path to .dpc directory (default: ~/.dpc)'
    )
    create_parser.add_argument(
        '--device-name',
        help='Device name for metadata (default: hostname)'
    )
    create_parser.add_argument(
        '--passphrase',
        help='Passphrase (insecure, prompts if not provided)'
    )

    # Restore backup command
    restore_parser = subparsers.add_parser('restore', help='Restore from encrypted backup')
    restore_parser.add_argument(
        '--input', '-i',
        required=True,
        help='Input backup file path'
    )
    restore_parser.add_argument(
        '--target', '-t',
        help='Target directory (default: ~/.dpc)'
    )
    restore_parser.add_argument(
        '--force', '-f',
        action='store_true',
        help='Overwrite existing files without prompting'
    )
    restore_parser.add_argument(
        '--passphrase',
        help='Passphrase (insecure, prompts if not provided)'
    )

    # Verify backup command
    verify_parser = subparsers.add_parser('verify', help='Verify backup integrity')
    verify_parser.add_argument(
        '--input', '-i',
        required=True,
        help='Input backup file path'
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Execute command
    if args.command == 'create':
        cmd_create(args)
    elif args.command == 'restore':
        cmd_restore(args)
    elif args.command == 'verify':
        cmd_verify(args)


if __name__ == "__main__":
    main()
