# Changelog

All notable changes to D-PC Messenger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - In `dev` branch

### Added - Knowledge Architecture (Phases 1-6) ⚠️ UNTESTED

#### Phase 1: Instructions.json Separation
- Separated AI instructions into standalone `instructions.json` file (Personal Context v2.0 schema)
- Added in-app **InstructionsEditor** component for editing AI instructions without manual file editing
- Personal context file now references `instructions.json` via `metadata.external_files`

#### Phase 2: Token Monitoring System
- **Token Counter Display** in chat header showing "used / limit tokens (percentage)"
  - Color-coded warning when usage exceeds 80% threshold
  - Live updates as conversation progresses
- **Toast Notification Component** (`Toast.svelte`)
  - Dismissible notifications with auto-dismiss timer
  - Type-based styling (info/warning/error)
  - Used for extraction failures and warnings
- Backend broadcasts `token_limit_warning` events when approaching context limit
- Frontend stores: `tokenWarning`, `extractionFailure`

#### Phase 3: Optional Cultural Perspectives
- Added `cultural_perspectives_enabled` configuration setting (default: `false`)
  - Location: `~/.dpc/config.ini` under `[knowledge]` section
- ConversationMonitor conditionally includes cultural perspective analysis in LLM prompts
- KnowledgeCommitDialog conditionally displays cultural perspectives section
- **Benefit**: Reduces prompt complexity and token usage when cultural analysis not needed

#### Phase 4: Robust JSON Extraction with Repair
- **JSON Repair System** (`_repair_json()` method) with 6 strategies:
  1. Remove markdown code blocks (```json ... ```)
  2. Fix trailing commas before closing braces/brackets
  3. Balance missing opening/closing braces
  4. Add missing commas between array/object elements
  5. Trim whitespace
  6. Count and fix bracket/brace mismatches
- **4-Level Fallback Parsing Strategy**:
  1. Attempt 1: Regex extract JSON object + parse
  2. Attempt 2: Regex extract + repair + parse
  3. Attempt 3: Parse entire response as JSON
  4. Attempt 4: Repair entire response + parse
- Backend broadcasts `knowledge_extraction_failed` event on extraction errors
- UI displays toast notification with error details

#### Phase 5: Inline Editing with Attribution
- Added `edited_by` (peer_id/node_id) and `edited_at` (ISO timestamp) fields to `KnowledgeEntry`
- **Full Edit Mode** in KnowledgeCommitDialog:
  - Edit/Save/Cancel button workflow
  - Textarea editing for knowledge entry content
  - Automatic attribution tracking on save
  - **Visual indicator**: Yellow "✏️ Edited" badge
  - Hover tooltip shows editor and timestamp
- Updated protocol serialization/deserialization to handle edit tracking fields
- **Use case**: Refine AI-extracted knowledge before committing

#### Phase 6: Provider-Specific Context Window Overrides
- Enhanced `get_context_window()` method with priority system:
  1. Check `context_window` field in `providers.toml` (highest priority)
  2. Check hardcoded `MODEL_CONTEXT_WINDOWS` dict
  3. Fall back to default (4096 tokens)
- Updated default `providers.toml` template with documentation and example
- **Use case**: Override context windows for custom models or provider-specific limits
- Example: `context_window = 131072` for custom Llama fine-tune

### Changed

#### Backend
- **conversation_monitor.py**
  - Added `settings` parameter to `__init__()`
  - Added `_repair_json()` helper method
  - Updated `_generate_commit_proposal()` to conditionally include cultural perspectives
  - Enhanced JSON parsing with multi-level fallback strategy
- **service.py**
  - Pass `settings` instance to ConversationMonitor
  - Broadcast `knowledge_extraction_failed` events on errors
- **settings.py**
  - Added `cultural_perspectives_enabled` to `[knowledge]` section
  - Added `get_cultural_perspectives_enabled()` method
- **llm_manager.py**
  - Updated `get_context_window()` to check `providers.toml` first
  - Added documentation to default providers.toml template

#### Protocol
- **pcm_core.py**
  - Added `edited_by: Optional[str]` field to `KnowledgeEntry`
  - Added `edited_at: Optional[str]` field to `KnowledgeEntry`
- **knowledge_commit.py**
  - Updated `KnowledgeCommitProposal.from_dict()` to deserialize edit tracking fields

#### Frontend
- **+page.svelte**
  - Added token counter display in chat header
  - Added extraction failure state and toast handling
- **coreService.ts**
  - Added `extractionFailure` writable store
  - Added event handler for `knowledge_extraction_failed`
- **KnowledgeCommitDialog.svelte**
  - Added edit mode state (`editMode`, `editedEntries`)
  - Added Edit/Save/Cancel button workflow
  - Added textarea editing for entry content
  - Added "✏️ Edited" badge with attribution details
  - Conditionally display cultural perspectives section

#### Documentation
- **KNOWLEDGE_ARCHITECTURE.md**
  - Updated with Phase 1-6 implementation details

### Fixed

**Phase 1: Instructions Editor**
- Fixed "Failed to load instructions: undefined" error on first panel open
- Root cause: InstructionsEditor.svelte incorrectly accessed `result.payload.status` instead of `result.status`
- Fix: Updated loadInstructions and saveChanges to match coreService.ts promise resolution pattern (resolves with payload only)
- Commit: d09a41b

**Phase 2: Token Counter**
- Fixed token counter not appearing after AI messages (only showed at 80% warning)
- Root cause: Frontend only updated tokenUsageMap on warning events, not on every AI response
- Fix: Extract tokens_used and token_limit from execute_ai_query responses immediately
- Also fixed: Missing tiktoken dependency in Poetry environment (was in pyproject.toml but not installed)
- Commit: c382b26

**Phase 6: Context Window for Claude 4.5 Models**
- Fixed token counter showing 247% warning due to incorrect context window
- Root cause: MODEL_CONTEXT_WINDOWS missing shorthand model names (claude-haiku-4-5, claude-opus-4-5)
- Fix: Added Claude 4.5 shorthand model names with 200K token context windows
- Commit: bb7c725

### Automated Testing Status

**✅ Backend Tests:** 58 passed, 2 skipped (pytest)
- All core functionality tests pass
- Conversation monitoring, firewall, device context, and local API tests verified

**✅ Frontend Type Check:** 0 errors, 0 warnings (svelte-check)
- All TypeScript types validated
- No compilation errors

**⚠️ Deprecation Warnings (Non-blocking):**
- `datetime.utcnow()` deprecated → migrate to `datetime.now(datetime.UTC)` in future
- `websockets.legacy` deprecated → upgrade to websockets v14+ API in future

### Manual Testing Required

Before merging to `main`, verify the following Phase 1-6 features:

- [ ] **Phase 2**: Token counter displays correctly and warns at 80%
- [ ] **Phase 2**: Toast notifications appear on extraction failures
- [ ] **Phase 3**: Cultural perspectives toggle works (on/off)
- [ ] **Phase 4**: JSON repair handles malformed LLM responses
- [ ] **Phase 5**: Inline editing saves with correct attribution
- [ ] **Phase 6**: Context window overrides work in providers.toml

**Manual Test Commands:**
```bash
# Start backend
cd dpc-client/core
poetry run python run_service.py

# Start frontend (separate terminal)
cd dpc-client/ui
npm run tauri dev

# Build production bundle
npm run build
npm run tauri build
```

---

## [0.1.0] - 2025-01-XX (Last Stable Release)

### Added
- Initial release of D-PC Messenger
- Peer-to-peer messaging with end-to-end encryption
- Direct TLS connections (local network)
- WebRTC connections (internet-wide with NAT traversal)
- Federation Hub for discovery and signaling
- OAuth 2.0 authentication (Google, GitHub)
- AI integration (Ollama, OpenAI-compatible, Anthropic)
- Personal Context Manager (PCM v1.0)
- Context Firewall for granular access control
- In-app configuration editors (Personal Context, Firewall Rules)
- Device context collection (hardware/software info)

### Documentation
- Quick Start Guide
- Configuration Reference
- WebRTC Setup Guide
- Device Context Specification
- GitHub Auth Setup Guide

---

## Version History

- **v0.2.0** (unreleased): Knowledge Architecture Phases 1-6
- **v0.1.0** (stable): Initial release with core P2P and AI features
