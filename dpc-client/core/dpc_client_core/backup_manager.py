# dpc-client/core/dpc_client_core/backup_manager.py

"""
Secure backup and restore of the entire .dpc directory.

Philosophy: User-controlled encryption. No backdoors. No key escrow.
If user loses passphrase, data is permanently lost (by design).
"""

import tarfile
import io
import json
import struct
import secrets
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend


class DPCBackupManager:
    """
    Manages secure backup and restore of entire .dpc directory.

    Security properties:
    - Client-side encryption (passphrase never leaves device)
    - AES-256-GCM (authenticated encryption)
    - PBKDF2-HMAC-SHA256 with 600k iterations (OWASP 2023 recommendation)
    - No backdoors (if passphrase lost, data is permanently unrecoverable)
    """

    MAGIC_BYTES = b"DPC_BACKUP_V1"
    VERSION = 1

    # PBKDF2 parameters (for key derivation)
    PBKDF2_ITERATIONS = 600000  # OWASP recommendation (2023)
    PBKDF2_HASH_LEN = 32  # 256-bit key

    def __init__(self, verbose: bool = True):
        """
        Initialize backup manager.

        Args:
            verbose: Print progress messages
        """
        self.verbose = verbose

    def _print(self, message: str):
        """Print message if verbose mode enabled."""
        if self.verbose:
            try:
                print(message)
            except UnicodeEncodeError:
                # Windows console encoding issue - remove emojis
                import re
                clean_message = re.sub(r'[\U00010000-\U0010ffff\u2705\u26A0\uFE0F]', '', message)
                print(clean_message)

    def create_backup(
        self,
        dpc_dir: Path,
        passphrase: str,
        device_name: Optional[str] = None,
        exclude_patterns: Optional[list] = None
    ) -> bytes:
        """
        Create encrypted backup of entire .dpc directory.

        Args:
            dpc_dir: Path to .dpc directory
            passphrase: User-provided passphrase (min 12 chars recommended)
            device_name: Optional device identifier for metadata
            exclude_patterns: Optional list of file patterns to exclude

        Returns:
            Encrypted backup bundle (bytes)

        Raises:
            ValueError: If passphrase too weak or directory invalid
            FileNotFoundError: If .dpc directory doesn't exist
        """
        # Validate inputs
        if not dpc_dir.exists():
            raise FileNotFoundError(f".dpc directory not found: {dpc_dir}")

        if not dpc_dir.is_dir():
            raise ValueError(f"Not a directory: {dpc_dir}")

        if len(passphrase) < 8:
            raise ValueError("Passphrase must be at least 8 characters")

        if len(passphrase) < 12:
            self._print("⚠️  Warning: Passphrase should be at least 12 characters for security")

        # Default exclusions
        if exclude_patterns is None:
            exclude_patterns = [
                '*.bak',  # Backup files
                '*.tmp',  # Temporary files
                '*.log',  # Log files
                '__pycache__',  # Python cache
            ]

        self._print(f"📦 Creating backup of {dpc_dir}")

        # Step 1: Create tar.gz of .dpc directory
        tar_buffer = io.BytesIO()

        with tarfile.open(fileobj=tar_buffer, mode='w:gz') as tar:
            for file_path in dpc_dir.rglob('*'):
                if not file_path.is_file():
                    continue

                # Check exclusion patterns
                if any(file_path.match(pattern) for pattern in exclude_patterns):
                    self._print(f"   Skipping: {file_path.name}")
                    continue

                arcname = file_path.relative_to(dpc_dir)
                tar.add(file_path, arcname=arcname)
                self._print(f"   Adding: {arcname}")

        compressed_data = tar_buffer.getvalue()
        self._print(f"📦 Compressed: {len(compressed_data):,} bytes")

        # Step 2: Derive encryption key from passphrase
        salt = secrets.token_bytes(32)  # 256-bit salt (unique per backup)

        self._print("🔐 Deriving encryption key (this may take a few seconds)...")

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.PBKDF2_HASH_LEN,
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
            backend=default_backend()
        )

        key = kdf.derive(passphrase.encode('utf-8'))
        self._print("🔐 Encryption key derived")

        # Step 3: Prepare metadata (authenticated but not encrypted)
        metadata = {
            "version": self.VERSION,
            "device_name": device_name or "unknown",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "compressed_size": len(compressed_data),
            "num_files": len(list(dpc_dir.rglob('*')))
        }

        associated_data = json.dumps(metadata).encode('utf-8')

        # Step 4: Encrypt with AES-256-GCM
        nonce = secrets.token_bytes(12)  # 96-bit nonce (unique per backup)
        aesgcm = AESGCM(key)

        encrypted_data = aesgcm.encrypt(
            nonce,
            compressed_data,
            associated_data
        )

        self._print(f"🔒 Encrypted: {len(encrypted_data):,} bytes")

        # Step 5: Build final backup bundle
        bundle = io.BytesIO()

        # Header (unencrypted, for compatibility checking)
        bundle.write(self.MAGIC_BYTES)  # 13 bytes
        bundle.write(struct.pack('!H', self.VERSION))  # 2 bytes (version)
        bundle.write(salt)  # 32 bytes
        bundle.write(nonce)  # 12 bytes
        bundle.write(struct.pack('!I', len(associated_data)))  # 4 bytes (metadata length)
        bundle.write(associated_data)  # Variable length

        # Encrypted payload
        bundle.write(encrypted_data)  # Variable length

        backup_bundle = bundle.getvalue()

        self._print(f"✅ Backup created: {len(backup_bundle):,} bytes")
        self._print(f"   Compression ratio: {len(compressed_data) / len(backup_bundle) * 100:.1f}%")

        return backup_bundle

    def restore_backup(
        self,
        backup_bundle: bytes,
        passphrase: str,
        target_dir: Path,
        overwrite: bool = False
    ) -> Dict[str, Any]:
        """
        Restore .dpc directory from encrypted backup.

        Args:
            backup_bundle: Encrypted backup data
            passphrase: User passphrase (must match original)
            target_dir: Where to restore .dpc directory
            overwrite: Allow overwriting existing files

        Returns:
            Dict with restoration metadata (files restored, timestamp, etc.)

        Raises:
            ValueError: If backup invalid or passphrase wrong
            FileExistsError: If target directory exists and overwrite=False
        """
        # Check if target directory already exists
        if target_dir.exists() and not overwrite:
            raise FileExistsError(
                f"Target directory already exists: {target_dir}\n"
                f"Use overwrite=True to force restoration"
            )

        self._print(f"🔓 Restoring backup to {target_dir}")

        try:
            buffer = io.BytesIO(backup_bundle)

            # Step 1: Read and validate header
            magic = buffer.read(len(self.MAGIC_BYTES))
            if magic != self.MAGIC_BYTES:
                raise ValueError("Invalid backup file (wrong magic bytes)")

            version = struct.unpack('!H', buffer.read(2))[0]
            if version != self.VERSION:
                raise ValueError(f"Unsupported backup version: {version}")

            self._print(f"📄 Backup version: {version}")

            # Step 2: Extract encryption parameters
            salt = buffer.read(32)
            nonce = buffer.read(12)
            associated_data_len = struct.unpack('!I', buffer.read(4))[0]
            associated_data = buffer.read(associated_data_len)

            # Parse metadata
            metadata = json.loads(associated_data.decode('utf-8'))
            self._print(f"📄 Backup metadata:")
            self._print(f"   Device: {metadata.get('device_name', 'unknown')}")
            self._print(f"   Created: {metadata.get('timestamp', 'unknown')}")
            self._print(f"   Files: {metadata.get('num_files', 'unknown')}")

            # Step 3: Read encrypted payload
            encrypted_data = buffer.read()

            # Step 4: Derive key from passphrase
            self._print("🔐 Deriving decryption key...")

            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=self.PBKDF2_HASH_LEN,
                salt=salt,
                iterations=self.PBKDF2_ITERATIONS,
                backend=default_backend()
            )

            key = kdf.derive(passphrase.encode('utf-8'))

            # Step 5: Decrypt
            aesgcm = AESGCM(key)

            try:
                compressed_data = aesgcm.decrypt(
                    nonce,
                    encrypted_data,
                    associated_data
                )
                self._print("🔓 Decryption successful")
            except Exception as e:
                raise ValueError(
                    f"Decryption failed: {str(e)}\n"
                    f"⚠️  Wrong passphrase or corrupted backup"
                )

            # Step 6: Extract tar.gz
            self._print("📦 Extracting files...")

            target_dir.mkdir(parents=True, exist_ok=True)

            tar_buffer = io.BytesIO(compressed_data)
            extracted_files = []

            with tarfile.open(fileobj=tar_buffer, mode='r:gz') as tar:
                for member in tar.getmembers():
                    tar.extract(member, path=target_dir)
                    extracted_files.append(member.name)
                    self._print(f"   ✅ {member.name}")

            self._print(f"✅ Restored {len(extracted_files)} files to {target_dir}")

            return {
                "success": True,
                "files_restored": extracted_files,
                "target_dir": str(target_dir),
                "metadata": metadata,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            self._print(f"❌ Restoration failed: {str(e)}")
            raise

    def save_backup_to_file(self, backup_bundle: bytes, file_path: Path):
        """
        Save encrypted backup to file.

        Args:
            backup_bundle: Encrypted backup data
            file_path: Where to save the backup
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, 'wb') as f:
            f.write(backup_bundle)

        self._print(f"💾 Saved backup to: {file_path}")
        self._print(f"   Size: {len(backup_bundle):,} bytes ({len(backup_bundle) / 1024 / 1024:.2f} MB)")

    def load_backup_from_file(self, file_path: Path) -> bytes:
        """
        Load encrypted backup from file.

        Args:
            file_path: Path to backup file

        Returns:
            Encrypted backup data
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Backup file not found: {file_path}")

        with open(file_path, 'rb') as f:
            data = f.read()

        self._print(f"📂 Loaded backup from: {file_path}")
        self._print(f"   Size: {len(data):,} bytes ({len(data) / 1024 / 1024:.2f} MB)")

        return data

    def verify_backup(self, backup_bundle: bytes) -> Dict[str, Any]:
        """
        Verify backup integrity without decrypting (checks header and metadata).

        Args:
            backup_bundle: Encrypted backup data

        Returns:
            Dict with backup information
        """
        try:
            buffer = io.BytesIO(backup_bundle)

            # Read header
            magic = buffer.read(len(self.MAGIC_BYTES))
            if magic != self.MAGIC_BYTES:
                return {"valid": False, "error": "Invalid magic bytes"}

            version = struct.unpack('!H', buffer.read(2))[0]

            # Skip salt and nonce
            buffer.read(32)  # salt
            buffer.read(12)  # nonce

            # Read metadata
            associated_data_len = struct.unpack('!I', buffer.read(4))[0]
            associated_data = buffer.read(associated_data_len)
            metadata = json.loads(associated_data.decode('utf-8'))

            return {
                "valid": True,
                "version": version,
                "metadata": metadata,
                "total_size": len(backup_bundle)
            }

        except Exception as e:
            return {"valid": False, "error": str(e)}


# Self-test
if __name__ == "__main__":
    import tempfile
    import shutil

    print("Testing DPCBackupManager...")
    print("=" * 60)

    # Create temporary test environment
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create mock .dpc directory
        test_dpc = tmpdir / "test_dpc"
        test_dpc.mkdir()

        (test_dpc / "personal.json").write_text('{"test": "data"}')
        (test_dpc / "node.key").write_text("test_private_key")
        (test_dpc / ".dpc_access").write_text("[test]\nallow = all")

        print(f"[OK] Created test .dpc directory: {test_dpc}")

        # Initialize backup manager
        manager = DPCBackupManager(verbose=True)

        # Test 1: Create backup
        print("\n" + "=" * 60)
        print("Test 1: Create backup")
        print("=" * 60)

        passphrase = "test_passphrase_12345"
        backup_bundle = manager.create_backup(
            test_dpc,
            passphrase,
            device_name="Test Device"
        )

        assert len(backup_bundle) > 0, "Backup bundle is empty"
        print("[PASS] Backup created successfully")

        # Test 2: Save to file
        print("\n" + "=" * 60)
        print("Test 2: Save to file")
        print("=" * 60)

        backup_file = tmpdir / "test_backup.dpc"
        manager.save_backup_to_file(backup_bundle, backup_file)

        assert backup_file.exists(), "Backup file not created"
        print("[PASS] Backup saved to file")

        # Test 3: Verify backup
        print("\n" + "=" * 60)
        print("Test 3: Verify backup")
        print("=" * 60)

        verification = manager.verify_backup(backup_bundle)
        assert verification["valid"], f"Verification failed: {verification.get('error')}"
        print(f"[PASS] Backup verified: {verification}")

        # Test 4: Restore backup
        print("\n" + "=" * 60)
        print("Test 4: Restore backup")
        print("=" * 60)

        restore_dir = tmpdir / "restored_dpc"
        result = manager.restore_backup(
            backup_bundle,
            passphrase,
            restore_dir
        )

        assert result["success"], "Restore failed"
        assert (restore_dir / "personal.json").exists(), "personal.json not restored"
        assert (restore_dir / "node.key").exists(), "node.key not restored"
        print("[PASS] Backup restored successfully")

        # Test 5: Wrong passphrase
        print("\n" + "=" * 60)
        print("Test 5: Wrong passphrase (should fail)")
        print("=" * 60)

        try:
            manager.restore_backup(
                backup_bundle,
                "wrong_passphrase",
                tmpdir / "should_fail"
            )
            print("[FAIL] Should have raised ValueError")
            assert False
        except ValueError as e:
            print(f"[PASS] Correctly rejected wrong passphrase: {e}")

        # Test 6: Load from file
        print("\n" + "=" * 60)
        print("Test 6: Load from file")
        print("=" * 60)

        loaded_bundle = manager.load_backup_from_file(backup_file)
        assert loaded_bundle == backup_bundle, "Loaded bundle doesn't match original"
        print("[PASS] Backup loaded from file")

    print("\n" + "=" * 60)
    print("[PASS] All tests passed!")
    print("=" * 60)
