# Backup and Restore Guide

**Status:** Phase 1 Implementation (v0.6)
**Feature:** Encrypted Local Backup

---

## Overview

DPC Messenger includes secure backup and restore functionality to protect your digital identity and personal knowledge. This feature allows you to:

- **Create encrypted backups** of your entire `.dpc` directory
- **Restore on new devices** without losing your knowledge, contacts, or identity
- **Control your data** with user-managed encryption (no backdoors)

**Philosophy:** You own your backup. You control the passphrase. If you lose it, no one (including us) can recover your data.

---

## Quick Start

### Create a Backup

```bash
cd dpc-client/core
poetry run python -m dpc_client_core.cli_backup create
```

You'll be prompted for a passphrase. The backup will be saved to `~/dpc_backup_TIMESTAMP.dpc`.

### Restore a Backup

```bash
poetry run python -m dpc_client_core.cli_backup restore --input ~/dpc_backup_20251112.dpc
```

Enter your passphrase when prompted, and your data will be restored to `~/.dpc/`.

---

## What Gets Backed Up

The backup includes your entire `.dpc` directory:

```
~/.dpc/
├── personal.json        # Your knowledge graph (AI's understanding of you)
├── .dpc_access          # Social graph (who can access what)
├── providers.toml       # AI provider preferences
├── node.key             # Cryptographic identity (private key)
├── node.crt             # Certificate
├── node.id              # Node identifier
└── config.ini           # Settings
```

**Excluded by default:**
- `*.bak` (backup files)
- `*.tmp` (temporary files)
- `*.log` (log files)
- `__pycache__` (Python cache)

---

## Security

### Encryption Details

- **Algorithm:** AES-256-GCM (authenticated encryption)
- **Key Derivation:** PBKDF2-HMAC-SHA256 with 600,000 iterations (OWASP 2023 recommendation)
- **Salt:** 256-bit random salt (unique per backup)
- **Nonce:** 96-bit random nonce (unique per backup)
- **Tamper Detection:** Built into AES-GCM (authentication tag)

### Security Properties

1. **Client-Side Encryption**
   - Encryption happens on YOUR device before any storage
   - Passphrase never leaves your device
   - Backup is useless without passphrase

2. **No Backdoors**
   - No "password reset" feature
   - No "master key" for authorities
   - If you lose passphrase, data is PERMANENTLY UNRECOVERABLE

3. **Forward Secrecy**
   - Each backup uses new random salt and nonce
   - Compromising one backup doesn't compromise others

4. **Tamper Detection**
   - AES-GCM provides authenticated encryption
   - Any modification to backup will be detected

### Passphrase Recommendations

**Minimum:** 8 characters (enforced)
**Recommended:** 12+ characters
**Best:** Use a passphrase generator or password manager

**Good passphrases:**
- `correct-horse-battery-staple` (4 random words)
- `MyD0g!sN@med-Max` (personal + symbols)
- Generated: `KpQ2x7#vN@9mL5wR`

**Bad passphrases:**
- `password123`
- `qwerty`
- Your birthday
- Your name

---

## CLI Reference

### Create Backup

```bash
python -m dpc_client_core.cli_backup create [options]
```

**Options:**
- `--output`, `-o`: Output file path (default: `~/dpc_backup_TIMESTAMP.dpc`)
- `--dpc-dir`: Path to .dpc directory (default: `~/.dpc`)
- `--device-name`: Device name for metadata (optional)
- `--passphrase`: Passphrase (insecure, will prompt if not provided)

**Examples:**

```bash
# Basic backup (prompts for passphrase)
python -m dpc_client_core.cli_backup create

# Backup to specific file
python -m dpc_client_core.cli_backup create --output ~/my_backup.dpc

# Backup with device name
python -m dpc_client_core.cli_backup create --device-name "Alice's Laptop"

# Backup from custom .dpc directory
python -m dpc_client_core.cli_backup create --dpc-dir /custom/path/.dpc
```

### Restore Backup

```bash
python -m dpc_client_core.cli_backup restore --input <backup_file> [options]
```

**Options:**
- `--input`, `-i`: Input backup file (required)
- `--target`, `-t`: Target directory (default: `~/.dpc`)
- `--force`, `-f`: Overwrite existing files without prompting
- `--passphrase`: Passphrase (insecure, will prompt if not provided)

**Examples:**

```bash
# Basic restore (prompts for passphrase)
python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc

# Restore to custom location
python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc --target ~/test_dpc

# Force overwrite without prompting
python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc --force
```

### Verify Backup

```bash
python -m dpc_client_core.cli_backup verify --input <backup_file>
```

**Example:**

```bash
python -m dpc_client_core.cli_backup verify --input ~/my_backup.dpc
```

This checks the backup file integrity (header, metadata) without decrypting it. Useful for verifying a backup file is not corrupted.

---

## Storage Options

### Option 1: USB Drive (Recommended for Maximum Security)

```bash
# Create backup
python -m dpc_client_core.cli_backup create --output ~/my_backup.dpc

# Copy to USB drive
cp ~/my_backup.dpc /media/usb/dpc_backups/

# Store USB drive in safe place (fire-proof safe, bank deposit box)
```

**Pros:**
- Offline (cannot be hacked remotely)
- Physical control
- No ongoing costs

**Cons:**
- Can be lost, damaged, or stolen
- Must remember where you stored it
- No automatic backups

---

### Option 2: Cloud Storage (Encrypted Before Upload)

```bash
# Create backup
python -m dpc_client_core.cli_backup create --output ~/my_backup.dpc

# Upload to cloud (backup is already encrypted)
# Dropbox:
mv ~/my_backup.dpc ~/Dropbox/

# Google Drive (if using Google Drive sync):
mv ~/my_backup.dpc ~/Google\ Drive/

# Or use cloud CLI tools:
aws s3 cp ~/my_backup.dpc s3://my-bucket/backups/
```

**Pros:**
- Accessible from anywhere
- Redundant storage (cloud providers have backups)
- Easy to automate

**Cons:**
- Trust cloud provider not to lose data
- Monthly fees (usually minimal)
- Provider could deny access (rare)

**Security Note:** The backup is encrypted BEFORE upload. The cloud provider sees only encrypted gibberish. They cannot decrypt your data.

---

### Option 3: Hub Storage (Future Phase 2)

**Status:** Not yet implemented (planned for v0.7)

```bash
# Future syntax:
python -m dpc_client_core.cli_backup create --upload hub
```

This will encrypt your backup and upload it to your DPC Hub. The Hub stores the encrypted blob but cannot decrypt it.

---

### Option 4: QR Code Transfer (Future Phase 2)

**Status:** Not yet implemented (planned for v0.7)

```bash
# Future syntax:
python -m dpc_client_core.cli_backup create --qr
```

This will display a QR code that you can scan with another device on the same local network. Useful for transferring to a new phone/tablet.

---

## Best Practices

### 1. Regular Backups

```bash
# Create a backup after significant knowledge changes
python -m dpc_client_core.cli_backup create
```

**Recommended frequency:**
- Daily: If actively using DPC for work
- Weekly: For regular personal use
- After major changes: New contacts, important knowledge updates

### 2. Test Your Backups

```bash
# Periodically verify your backup works
python -m dpc_client_core.cli_backup verify --input ~/my_backup.dpc
```

Don't wait until disaster strikes to discover your backup is corrupted!

### 3. Multiple Storage Locations (3-2-1 Rule)

- **3** copies of your data (original + 2 backups)
- **2** different storage types (USB + cloud)
- **1** offsite backup (cloud or remote USB)

**Example:**
```
Original: ~/.dpc/ (your device)
Backup 1: USB drive at home
Backup 2: Cloud storage (Dropbox, Google Drive)
```

### 4. Secure Passphrase Storage

**Do:**
- ✅ Use a password manager (1Password, Bitwarden, LastPass)
- ✅ Write it down and store in a safe
- ✅ Share with trusted family member (for emergency recovery)

**Don't:**
- ❌ Store passphrase in plaintext on same device as backup
- ❌ Use same passphrase for multiple backups (if one is compromised, all are)
- ❌ Store passphrase in email or cloud notes

### 5. Backup Before Major Changes

```bash
# Before updating DPC
python -m dpc_client_core.cli_backup create --output ~/pre_update_backup.dpc

# Before deleting old device
python -m dpc_client_core.cli_backup create --output ~/old_device_backup.dpc
```

---

## Multi-Device Considerations

**Important:** Restoring a backup to a new device will copy the OLD device's `node_id`. This creates conflicts if both devices connect to the Hub.

**Recommended workflow:**

```bash
# 1. On old device: Create backup
python -m dpc_client_core.cli_backup create --output ~/my_backup.dpc

# 2. Transfer backup to new device (USB, cloud, etc.)

# 3. On new device: Restore backup
python -m dpc_client_core.cli_backup restore --input ~/my_backup.dpc

# 4. Generate NEW node_id for new device
dpc init --force

# Result: New device has your knowledge and contacts, but unique identity
```

**Why generate new node_id?**
- Each device should have unique cryptographic identity
- Hub currently supports one device per email
- Having same node_id on multiple devices causes conflicts

See [CONFIGURATION.md](./CONFIGURATION.md#device-identity-and-multi-device-considerations) for details on the single-device limitation.

---

## Troubleshooting

### Issue: "Wrong passphrase or corrupted backup"

**Cause:** Incorrect passphrase or backup file is damaged

**Solutions:**
1. Double-check passphrase (case-sensitive!)
2. Try copying passphrase from password manager (avoid typos)
3. Verify backup file integrity: `python -m dpc_client_core.cli_backup verify --input <file>`
4. Try older backup if available

---

### Issue: "Target directory already exists"

**Cause:** Attempting to restore to directory that already has data

**Solutions:**
1. Allow overwrite: Use `--force` flag
2. Restore to different location: Use `--target /tmp/test_restore`
3. Backup existing .dpc first, then restore

---

### Issue: Backup file too large

**Cause:** Large personal.json or many files in .dpc

**Current state:** No differential backups yet (planned for Phase 2)

**Workarounds:**
1. Clean up old/unnecessary data in personal.json
2. Remove log files manually before backup
3. Use compression-friendly cloud storage

---

## Python API

You can also use the backup manager directly in Python code:

```python
from pathlib import Path
from dpc_client_core.backup_manager import DPCBackupManager

# Initialize
manager = DPCBackupManager(verbose=True)

# Create backup
backup_bundle = manager.create_backup(
    dpc_dir=Path.home() / ".dpc",
    passphrase="your-secure-passphrase",
    device_name="Alice's Laptop"
)

# Save to file
manager.save_backup_to_file(backup_bundle, Path("~/my_backup.dpc"))

# Later: Restore
backup_data = manager.load_backup_from_file(Path("~/my_backup.dpc"))
result = manager.restore_backup(
    backup_bundle=backup_data,
    passphrase="your-secure-passphrase",
    target_dir=Path.home() / ".dpc",
    overwrite=True
)

print(f"Restored {len(result['files_restored'])} files")
```

---

## Future Enhancements (Roadmap)

### Phase 2 (v0.7): Hub-Assisted Backup
- Upload encrypted backup to Hub
- Automatic periodic backups
- Version history

### Phase 3 (v0.8): Social Recovery
- Shamir Secret Sharing (split backup into N shares)
- Recover with M-of-N shares from trusted contacts

### Phase 4 (v1.0): Hardware Security
- Hardware wallet integration (Ledger, YubiKey)
- TPM-based key storage
- Secure Enclave support (macOS/iOS)

### Phase 5 (v1.1): Advanced Features
- Differential/incremental backups
- Automated backup scheduling
- Cloud provider integrations (native APIs)

---

## See Also

- [User Sovereignty Philosophy](./USER_SOVEREIGNTY.md)
- [Configuration Guide](./CONFIGURATION.md)
- [Multi-Device Considerations](./CONFIGURATION.md#device-identity-and-multi-device-considerations)
- [Security Best Practices](./USER_SOVEREIGNTY.md#privacy-threat-model)

---

**Questions or issues?** [Open an issue](https://github.com/mikhashev/dpc-messenger/issues)

**Remember:** Your backup, your responsibility. Keep your passphrase safe!
