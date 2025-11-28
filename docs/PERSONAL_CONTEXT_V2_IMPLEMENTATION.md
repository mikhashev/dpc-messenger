# Personal Context Schema v2.0 - Implementation Plan

**Status:** Ready for Implementation
**Version:** 2.0
**Date:** 2025-11-26
**Prerequisites:** [COMMIT_INTEGRITY_IMPLEMENTATION.md](COMMIT_INTEGRITY_IMPLEMENTATION.md) must be implemented first
**Architecture:** Modular File System with Versioned Markdown Knowledge

---

## Prerequisites

**⚠️ IMPORTANT:** This implementation assumes [COMMIT_INTEGRITY_IMPLEMENTATION.md](COMMIT_INTEGRITY_IMPLEMENTATION.md) has already been completed.

**Required from COMMIT_INTEGRITY:**
- ✅ Hash-based `commit_id` generation (`compute_commit_hash()`)
- ✅ `KnowledgeCommit` has `commit_hash` and `signatures` fields
- ✅ `ConsensusManager` creates hash-based commit_ids
- ✅ Versioned markdown file structure established

**This document covers:**
- Schema cleanup (remove `instruction` field)
- Export knowledge to markdown files (using hash-based commit_ids)
- Minimal `personal.json` structure
- Knowledge loading from markdown

---

## Table of Contents

1. [Overview](#overview)
2. [Target Architecture](#target-architecture)
3. [Minimal Schema Design](#minimal-schema-design)
4. [Schema Cleanup](#schema-cleanup)
5. [Markdown Integration](#markdown-integration)
6. [Implementation Details](#implementation-details)
7. [File Structure](#file-structure)
8. [Testing Plan](#testing-plan)
9. [Multi-Device Sync](#multi-device-sync)

---

## Overview

### Problem Statement

Current `personal.json` files are in a **hybrid state**:
- ❌ **Legacy `instruction` field** - Should be in `instructions.json`
- ❌ **Massive embedded knowledge** - 26KB file with hundreds of JSON entries
- ❌ **No markdown storage** - `markdown_file` field always `null`
- ❌ **Inconsistent structure** - Different devices have different schemas

### Solution

**Modular v2.0 schema** with clean separation:
- ✅ **Remove `instruction` field** - Reference `instructions.json` instead
- ✅ **Versioned markdown files** - `knowledge/{topic}_{commit-hash}.md` (using hash-based IDs from COMMIT_INTEGRITY)
- ✅ **Minimal personal.json** - Profile + metadata + references only (~3-5 KB)
- ✅ **External file references** - Track all related files in metadata

---

## Target Architecture

### File System Structure

```
~/.dpc/
├── personal.json          # 3-5 KB: Profile + knowledge metadata
├── instructions.json      # 1 KB: AI instructions (already exists)
├── device_context.json    # 5 KB: Hardware/software specs
│
└── knowledge/             # Versioned markdown files (hash-based IDs)
    ├── windows_networking_commit-a05808d8.md
    ├── astronomy_commit-a05808d8.md
    ├── astronomy_commit-def12345.md  (newer version)
    └── alice_collaborative_ai_commit-2153be27.md
```

**Note:** Commit IDs are hash-based (from COMMIT_INTEGRITY), not random UUIDs.

---

## Minimal Schema Design

### personal.json v2.0 (Target)

```json
{
  "profile": {
    "name": "Mike Windows 10",
    "description": "My personal context for D-PC.",
    "core_values": ["Windows"]
  },

  "knowledge": {
    "astronomy": {
      "summary": "The Andromeda Galaxy is approximately 2.5 million light-years away from Earth.",
      "markdown_file": "knowledge/astronomy_commit-def12345.md",
      "commit_id": "commit-def12345",
      "mastery_level": "beginner",
      "version": 2,
      "created_at": "2025-11-14T17:08:20.136705",
      "last_modified": "2025-11-18T10:30:00.000000"
      // NO entries array - stored in markdown
    }
  },

  "preferences": null,
  "cognitive_profile": null,

  // Git-like versioning
  "version": 3,
  "last_commit_id": "commit-2153be27",
  "last_commit_message": "Alice establishes a win-win AI collaboration model...",
  "last_commit_timestamp": "2025-11-18T08:38:11.243695",
  "commit_history": [
    {
      "commit_id": "commit-a05808d8",
      "commit_hash": "a05808d8f1e2a3b4...",  // From COMMIT_INTEGRITY
      "timestamp": "2025-11-14T17:08:20.135700",
      "message": "The Andromeda Galaxy...",
      "participants": ["dpc-node-e07fb59e46f34940"],
      "consensus": "unanimous",
      "approved_by": ["dpc-node-e07fb59e46f34940"],
      "signatures": {  // From COMMIT_INTEGRITY
        "dpc-node-e07fb59e46f34940": "MEUCIQDXvK..."
      }
    }
  ],

  "metadata": {
    "created": "2025-11-14T10:25:22.068011",
    "last_updated": "2025-11-25T16:44:59.025719",
    "storage": "local",
    "format_version": "2.0",

    // External file references
    "external_files": {
      "instructions": {
        "file": "instructions.json",
        "description": "AI behavior instructions",
        "last_updated": "2025-11-24T18:52:00.000000"
      }
    },

    "external_contexts": {
      "device_context": {
        "file": "device_context.json",
        "schema_version": "1.1",
        "last_updated": "2025-11-25T16:44:59.025719+00:00"
      }
    }
  }
}
```

### Fields Removed

```json
// ❌ REMOVED - moved to instructions.json
"instruction": {
  "primary": "...",
  "bias_mitigation": {...}
}

// ❌ REMOVED - moved to markdown files
"knowledge": {
  "topic_name": {
    "entries": [...]  // Hundreds of lines
  }
}
```

---

## Schema Cleanup

### Fix Migration Function

**File:** `dpc-protocol/dpc_protocol/pcm_core.py`

**Current Bug (lines 460-463):**

```python
if instructions_json_path.exists():
    print(f"Instructions file already exists. No migration needed.")
    return False  # ⚠️ BUG: Never cleans personal.json
```

**Fixed Version:**

```python
def migrate_instructions_from_personal_context():
    """
    Migrate instructions from personal.json to instructions.json.

    IDEMPOTENT: Safe to run multiple times.
    """

    from pathlib import Path
    import json
    from datetime import datetime

    config_dir = Path.home() / ".dpc"
    personal_json_path = config_dir / "personal.json"
    instructions_json_path = config_dir / "instructions.json"

    if not personal_json_path.exists():
        print("No personal.json found, skipping migration")
        return False

    with open(personal_json_path, 'r', encoding='utf-8') as f:
        personal_data = json.load(f)

    changes_made = False

    # CASE 1: instructions.json exists
    if instructions_json_path.exists():
        print(f"Instructions file exists at {instructions_json_path}")

        # Check if personal.json still has instruction field
        if 'instruction' in personal_data:
            print("  → Cleaning up legacy instruction field")

            # Remove instruction field
            del personal_data['instruction']
            changes_made = True

            # Add external_files reference
            if 'external_files' not in personal_data.get('metadata', {}):
                personal_data.setdefault('metadata', {})['external_files'] = {}

            personal_data['metadata']['external_files']['instructions'] = {
                "file": "instructions.json",
                "description": "AI behavior instructions",
                "last_updated": datetime.utcnow().isoformat()
            }

            print("  ✓ Removed instruction field")
            print("  ✓ Added external_files reference")
        else:
            print("  ✓ Already clean")
            return False

    # CASE 2: instructions.json doesn't exist
    else:
        if 'instruction' not in personal_data:
            print("No instruction field found")
            return False

        print("Migrating instructions to separate file...")

        # Extract instruction
        instruction_data = personal_data['instruction']

        # Create instructions.json
        with open(instructions_json_path, 'w', encoding='utf-8') as f:
            json.dump(instruction_data, f, indent=2, ensure_ascii=False)

        print(f"  ✓ Created {instructions_json_path}")

        # Remove from personal.json
        del personal_data['instruction']

        # Add external_files reference
        personal_data.setdefault('metadata', {})['external_files'] = {
            'instructions': {
                "file": "instructions.json",
                "description": "AI behavior instructions",
                "last_updated": datetime.utcnow().isoformat()
            }
        }

        changes_made = True

    # Save cleaned personal.json
    if changes_made:
        # Backup original
        import shutil
        backup_path = personal_json_path.with_suffix('.json.backup')
        shutil.copy(personal_json_path, backup_path)
        print(f"  ✓ Backed up to {backup_path}")

        # Save cleaned version
        with open(personal_json_path, 'w', encoding='utf-8') as f:
            json.dump(personal_data, f, indent=2, ensure_ascii=False)

        print(f"  ✓ Updated {personal_json_path}")

    return changes_made
```

### One-Time Schema Cleanup

**File:** `dpc-client/core/dpc_client_core/service.py`

**Add new method:**

```python
async def _cleanup_schema_v2(self):
    """
    One-time cleanup to migrate to v2.0 schema.

    Prerequisites:
        - COMMIT_INTEGRITY must be implemented first
        - Hash-based commit_ids already in use

    Steps:
        1. Fix instruction field (via migrate_instructions)
        2. Export knowledge to versioned markdown files
        3. Clear entries arrays from JSON
        4. Update format_version to 2.0
    """

    from pathlib import Path
    import json
    import hashlib
    from datetime import datetime
    from dpc_protocol.markdown_manager import MarkdownKnowledgeManager
    from dpc_protocol.pcm_core import PersonalContext, migrate_instructions_from_personal_context

    config_dir = self.settings.config_dir
    personal_json_path = config_dir / "personal.json"

    if not personal_json_path.exists():
        logger.info("No personal.json found, skipping cleanup")
        return

    logger.info("=== Schema v2.0 Cleanup ===")

    # Load current data
    with open(personal_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Check if already v2.0 and clean
    if data.get('metadata', {}).get('format_version') == '2.0':
        has_instruction = 'instruction' in data
        has_entries = any(
            len(topic.get('entries', [])) > 0
            for topic in data.get('knowledge', {}).values()
        )

        if not has_instruction and not has_entries:
            logger.info("✓ Already v2.0 and clean")
            return

    changes_made = False

    # STEP 1: Fix instruction field
    logger.info("Step 1: Checking instruction field...")
    if migrate_instructions_from_personal_context():
        logger.info("  ✓ Instructions migrated")
        changes_made = True

        # Reload data
        with open(personal_json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        logger.info("  ✓ Instructions already clean")

    # STEP 2: Export knowledge to markdown
    logger.info("Step 2: Exporting knowledge to markdown...")

    markdown_manager = MarkdownKnowledgeManager()
    context = PersonalContext.from_dict(data)

    for topic_name, topic in context.knowledge.items():
        # Skip if already has markdown_file
        if hasattr(topic, 'markdown_file') and topic.markdown_file:
            markdown_path = config_dir / topic.markdown_file
            if markdown_path.exists():
                logger.info(f"  ✓ {topic_name}: Already has markdown")
                continue

        # Get commit_id (should be hash-based from COMMIT_INTEGRITY)
        commit_id = getattr(topic, 'commit_id', None) or context.last_commit_id

        if not commit_id:
            # Fallback: generate hash-based commit_id
            logger.warning(f"  ⚠️ {topic_name}: No commit_id, generating new one")

            from dpc_protocol.knowledge_commit import KnowledgeCommit
            from dpc_protocol.commit_integrity import compute_commit_hash

            temp_commit = KnowledgeCommit(
                topic=topic_name,
                summary=topic.summary,
                entries=topic.entries
            )
            commit_hash = compute_commit_hash(temp_commit)
            commit_id = f"commit-{commit_hash[:16]}"

        # Create versioned markdown file
        safe_name = topic_name.lower().replace(' ', '_').replace("'", '').replace('"', '')
        markdown_filename = f"{safe_name}_{commit_id}.md"
        markdown_path = markdown_manager.knowledge_dir / markdown_filename

        # Convert topic to markdown
        markdown_content = markdown_manager.topic_to_markdown_content(topic)

        # Compute content hash
        content_hash = hashlib.sha256(markdown_content.encode('utf-8')).hexdigest()[:16]

        # Create frontmatter
        frontmatter = {
            'topic': topic_name,
            'commit_id': commit_id,
            'content_hash': content_hash,
            'version': topic.version,
            'created_at': topic.created_at,
            'last_modified': topic.last_modified,
            'mastery_level': topic.mastery_level
        }

        # If this commit has integrity data (from COMMIT_INTEGRITY)
        for commit in context.commit_history:
            if commit.get('commit_id') == commit_id:
                frontmatter['commit_hash'] = commit.get('commit_hash', '')
                frontmatter['parent_commit'] = commit.get('parent_commit_id', '')
                frontmatter['participants'] = commit.get('participants', [])
                frontmatter['approved_by'] = commit.get('approved_by', [])
                frontmatter['consensus'] = commit.get('consensus', 'unanimous')
                frontmatter['signatures'] = commit.get('signatures', {})
                break

        # Write markdown file
        full_content = markdown_manager.build_markdown_with_frontmatter(
            frontmatter,
            markdown_content
        )
        markdown_path.write_text(full_content, encoding='utf-8')

        # Update JSON metadata
        data['knowledge'][topic_name]['markdown_file'] = f"knowledge/{markdown_filename}"
        data['knowledge'][topic_name]['commit_id'] = commit_id

        # Clear entries
        entry_count = len(data['knowledge'][topic_name].get('entries', []))
        data['knowledge'][topic_name]['entries'] = []

        logger.info(f"  ✓ {topic_name}: Exported {entry_count} entries to {markdown_filename}")
        changes_made = True

    # STEP 3: Update format_version
    if 'metadata' not in data:
        data['metadata'] = {}

    data['metadata']['format_version'] = '2.0'
    data['metadata']['last_updated'] = datetime.utcnow().isoformat()

    # STEP 4: Save cleaned personal.json
    if changes_made:
        # Backup original
        import shutil
        backup_path = personal_json_path.with_suffix('.json.v1_backup')
        shutil.copy(personal_json_path, backup_path)
        logger.info(f"✓ Backed up to {backup_path}")

        # Save cleaned version
        with open(personal_json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Log results
        original_size = backup_path.stat().st_size
        new_size = personal_json_path.stat().st_size
        reduction = ((original_size - new_size) / original_size) * 100

        logger.info(f"✓ Schema v2.0 cleanup complete")
        logger.info(f"  File size: {original_size} → {new_size} bytes ({reduction:.1f}% reduction)")
    else:
        logger.info("No changes needed")
```

**Call on startup:**

```python
async def start(self):
    """Start the core service."""

    logger.info("Starting DPC Client Core Service...")

    # ... existing initialization ...

    # Run schema cleanup (after COMMIT_INTEGRITY is active)
    try:
        await self._cleanup_schema_v2()
    except Exception as e:
        logger.error(f"Schema cleanup error: {e}")
        # Don't fail startup

    # ... rest of startup ...
```

---

## Markdown Integration

**Note:** This section assumes `MarkdownKnowledgeManager` has been updated with frontmatter support from COMMIT_INTEGRITY.

### Load Knowledge from Markdown

**File:** `dpc-client/core/dpc_client_core/service.py`

**Update `get_personal_context()`:**

```python
async def get_personal_context(self):
    """Load personal context with knowledge from markdown files."""

    # Load JSON
    context = self.pcm_core.load_context()

    # Load knowledge from markdown files
    from dpc_protocol.markdown_manager import MarkdownKnowledgeManager
    markdown_manager = MarkdownKnowledgeManager()

    for topic_name, topic in context.knowledge.items():
        if topic.markdown_file:
            filepath = self.settings.config_dir / topic.markdown_file

            if filepath.exists():
                # Parse markdown with frontmatter
                frontmatter, content = markdown_manager.parse_markdown_with_frontmatter(filepath)

                # Verify integrity (from COMMIT_INTEGRITY)
                if 'content_hash' in frontmatter:
                    actual_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
                    if actual_hash != frontmatter['content_hash']:
                        logger.warning(
                            f"⚠️ Content hash mismatch for {topic_name}: "
                            f"{frontmatter['commit_id']}"
                        )

                # Convert markdown to entries
                entries = markdown_manager.markdown_to_entries(content)
                topic.entries = entries  # In-memory only
            else:
                logger.warning(f"Markdown file not found: {topic.markdown_file}")

    return context
```

### Save Knowledge to Markdown

**Update `save_personal_context()`:**

```python
async def save_personal_context(self, context_data: dict):
    """Save personal context with markdown synchronization."""

    # Validate
    context = PersonalContext.from_dict(context_data)

    # Sync to markdown (if knowledge changed)
    from dpc_protocol.markdown_manager import MarkdownKnowledgeManager
    markdown_manager = MarkdownKnowledgeManager()

    for topic_name, topic in context.knowledge.items():
        # Only sync if entries exist (new/modified knowledge)
        if topic.entries:
            # Use existing commit_id or create new one
            # (New commit_id generation handled by ConsensusManager in COMMIT_INTEGRITY)
            commit_id = topic.commit_id or context.last_commit_id

            if not commit_id:
                logger.error(f"No commit_id for {topic_name}, skipping markdown save")
                continue

            # Create versioned markdown
            markdown_path = markdown_manager.create_versioned_file(
                topic_name=topic_name,
                topic=topic,
                commit_id=commit_id,
                parent_commit_id=None  # TODO: Extract from history
            )

            # Update metadata
            topic.markdown_file = markdown_path
            topic.commit_id = commit_id
            topic.entries = []  # Clear (markdown is source of truth)

    # Save JSON
    context_dict = context.to_dict()
    self.pcm_core.save_context(context)

    # Broadcast update
    await self._broadcast_context_updated()
```

---

## Implementation Details

### File Updates

**1. `dpc-protocol/dpc_protocol/pcm_core.py`**
- Fix `migrate_instructions_from_personal_context()` (lines 435-511)
- Make idempotent, add cleanup logic

**2. `dpc-protocol/dpc_protocol/markdown_manager.py`**
- Already updated in COMMIT_INTEGRITY with:
  - `create_versioned_file()`
  - `parse_markdown_with_frontmatter()`
  - `build_markdown_with_frontmatter()`
  - `markdown_to_entries()`

**3. `dpc-client/core/dpc_client_core/service.py`**
- Add `_cleanup_schema_v2()` method
- Update `get_personal_context()` to load from markdown
- Update `save_personal_context()` to save to markdown
- Call cleanup on startup

**4. `dpc-client/ui/src/lib/components/ContextViewer.svelte`**
- Display markdown file paths
- Show commit_id badges
- Add "View Markdown" button (optional)

---

## File Structure

### Before Cleanup

```
~/.dpc/
├── personal.json (26 KB)
│   └── Contains:
│       - instruction field ❌
│       - huge entries arrays ❌
│
└── instructions.json (1 KB)
```

### After Cleanup

```
~/.dpc/
├── personal.json (3-5 KB)
│   └── Profile + metadata only ✅
│
├── personal.json.v1_backup (26 KB)
├── instructions.json (1 KB)
│
└── knowledge/
    ├── astronomy_commit-a05808d8.md
    ├── astronomy_commit-def12345.md
    └── alice_collaborative_ai_commit-2153be27.md
```

**Note:** Commit IDs in filenames are hash-based (from COMMIT_INTEGRITY).

---

## Testing Plan

### Unit Tests

**File:** `dpc-protocol/tests/test_schema_v2.py`

```python
def test_migration_removes_instruction_field():
    """Test instruction field removal."""
    # ... test implementation ...

def test_migration_idempotent():
    """Test migration can run multiple times."""
    # ... test implementation ...

def test_markdown_export_uses_hash_based_ids():
    """Test markdown files use hash-based commit_ids."""

    # Create topic with commit_id from COMMIT_INTEGRITY
    commit = KnowledgeCommit(...)
    commit.compute_hash()  # Generates hash-based ID

    topic = Topic(commit_id=commit.commit_id, ...)

    # Export to markdown
    markdown_path = markdown_manager.create_versioned_file(...)

    # Verify filename contains hash
    assert commit.commit_id in markdown_path
    assert len(commit.commit_id) == 23  # "commit-" + 16 hex chars
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_cleanup_preserves_integrity_data():
    """Test cleanup preserves commit hashes and signatures."""

    # Create context with integrity data (from COMMIT_INTEGRITY)
    context = PersonalContext(...)
    context.commit_history = [
        {
            'commit_id': 'commit-abc123def456',
            'commit_hash': 'abc123def456...',
            'signatures': {'node-1': 'MEU...'}
        }
    ]

    # Run cleanup
    await service._cleanup_schema_v2()

    # Verify markdown has integrity data
    markdown = Path.home() / ".dpc" / "knowledge" / "topic_commit-abc123def456.md"
    frontmatter, _ = parse_markdown_with_frontmatter(markdown)

    assert frontmatter['commit_hash'] == 'abc123def456...'
    assert 'signatures' in frontmatter
```

### Manual Tests

1. **Cleanup Test:**
   - Start with hybrid personal.json
   - Run service
   - Verify `instruction` removed
   - Verify markdown files created with hash-based IDs
   - Verify entries cleared from JSON

2. **Roundtrip Test:**
   - Load context (entries populated from markdown)
   - Modify knowledge
   - Save context
   - Verify markdown updated
   - Restart service
   - Verify knowledge loaded correctly

---

## Multi-Device Sync

### Recommended Method: Backup/Restore

```bash
# Device A (after cleanup):
python -m dpc_client_core.cli_backup create --output v2_sync.dpc

# Transfer to Device B
scp v2_sync.dpc deviceB:~/

# Device B:
python -m dpc_client_core.cli_backup restore --input ~/v2_sync.dpc
```

**Result:** Both devices have identical v2.0 schema with hash-based commit_ids.

---

## Cross-References

**This document depends on:**
- [COMMIT_INTEGRITY_IMPLEMENTATION.md](COMMIT_INTEGRITY_IMPLEMENTATION.md)
  - Hash-based commit_id generation
  - `commit_hash` and `signatures` fields
  - Markdown frontmatter structure
  - Integrity verification

**Related documents:**
- `CLAUDE.md` - Update with v2.0 architecture after implementation
- `docs/CONFIGURATION.md` - Document new file structure

---

## Implementation Checklist

- [ ] **COMMIT_INTEGRITY implemented** (prerequisite)
- [ ] Fix `migrate_instructions_from_personal_context()`
- [ ] Add `_cleanup_schema_v2()` to service
- [ ] Update `get_personal_context()` to load from markdown
- [ ] Update `save_personal_context()` to save to markdown
- [ ] Update UI to show markdown references
- [ ] Test cleanup on Windows device
- [ ] Test cleanup on Ubuntu device
- [ ] Verify multi-device sync
- [ ] Update documentation

---

**End of Implementation Plan**
