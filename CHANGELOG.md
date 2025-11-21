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
  1. Check `context_window` field in `providers.json` (highest priority)
  2. Check hardcoded `MODEL_CONTEXT_WINDOWS` dict
  3. Fall back to default (4096 tokens)
- Updated default `providers.json` template with documentation and example
- **Use case**: Override context windows for custom models or provider-specific limits
- Example: `context_window = 131072` for custom Llama fine-tune

#### Phase 7: Configuration System Overhaul (TOML → JSON)
- **Migrated all configuration files to JSON format** for consistency:
  - `providers.toml` → `providers.json` (automatic migration with `.backup` files)
  - `.dpc_access.json` → `privacy_rules.json` (consistent naming without redundant dot prefix)
  - Backward compatibility: automatic one-time migration on service startup
- **In-app AI Providers Editor** (`ProvidersEditor.svelte`):
  - Tab-based UI (Providers List + Add Provider)
  - Edit/Save/Cancel workflow with unsaved changes detection
  - Type-specific forms (Ollama, OpenAI Compatible, Anthropic)
  - API key configuration: `api_key_env` (recommended) or `api_key` (plaintext, local only)
  - Set default provider, delete providers, context window overrides
  - Real-time validation and hot-reload (no service restart needed)
  - Accessibility fixes: dialog tabindex, form-wrapped password fields
- **Backend commands**:
  - `get_providers_config` - Load providers configuration for editor
  - `save_providers_config` - Validate and save with hot-reload
  - Added constants for all config filenames (DRY principle)
- **Comprehensive example files**:
  - `instructions.example.json` - AI instructions with field reference and examples
  - `providers.example.json` - Multi-provider setup examples with security notes
  - `privacy_rules.example.json` - Complete firewall rules guide in JSON format
  - Updated `personal_context_example.json` - Removed inline instruction block, added `metadata.external_files` reference

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
  - Added `get_providers_config()` command for ProvidersEditor
  - Added `save_providers_config()` command with validation
  - Added constants for config filenames: `PROVIDERS_CONFIG`, `PRIVACY_RULES`, `PERSONAL_CONTEXT`, `KNOWN_PEERS`, `NODE_KEY`
- **settings.py**
  - Added `cultural_perspectives_enabled` to `[knowledge]` section
  - Added `get_cultural_perspectives_enabled()` method
- **llm_manager.py**
  - Migrated from TOML to JSON format (`providers.toml` → `providers.json`)
  - Added `_migrate_from_toml_if_needed()` for automatic migration
  - Added `save_config()` method for hot-reload without service restart
  - Updated `get_context_window()` to check `providers.json` first
  - Updated `_ensure_config_exists()` to create JSON template
- **firewall.py**
  - Added `_migrate_from_old_filename()` to migrate `.dpc_access.json` → `privacy_rules.json`
  - Updated docstrings to reference new filename

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
  - Added "🤖 AI Providers" button in sidebar
  - Integrated ProvidersEditor modal component
- **coreService.ts**
  - Added `extractionFailure` writable store
  - Added event handler for `knowledge_extraction_failed`
  - Added `get_providers_config` and `save_providers_config` to `expectsResponse` array
- **KnowledgeCommitDialog.svelte**
  - Added edit mode state (`editMode`, `editedEntries`)
  - Added Edit/Save/Cancel button workflow
  - Added textarea editing for entry content
  - Added "✏️ Edited" badge with attribution details
  - Conditionally display cultural perspectives section
- **ProvidersEditor.svelte** (NEW)
  - Full-featured AI providers configuration editor
  - Tab-based interface (Providers List + Add Provider)
  - Type-specific forms with conditional fields
  - API key source selection (environment variable vs plaintext)
  - Real-time validation and save confirmation
  - Accessibility compliant (ARIA labels, keyboard navigation, form-wrapped password fields)

#### Documentation
- **KNOWLEDGE_ARCHITECTURE.md**
  - Updated with Phase 1-6 implementation details
- **CLAUDE.md**
  - Updated all config file references (`providers.toml` → `providers.json`, `.dpc_access.json` → `privacy_rules.json`)
- **Example Files**:
  - Created `instructions.example.json` - Comprehensive AI instructions template with field reference and use-case examples
  - Created `providers.example.json` - Multi-provider setup guide with security notes
  - Created `privacy_rules.example.json` - Complete firewall rules documentation in JSON format
  - Updated `personal_context_example.json` - Removed inline instruction block, added `metadata.external_files` reference
  - Removed `providers.example.toml` and `.dpc_access.example` (replaced with JSON versions)

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
- Fixed token counter not resetting when "New Chat" button pressed
- Root cause: handleNewChat cleared chat history but didn't delete token usage from map
- Fix: Added tokenUsageMap.delete(chatId) to reset counter for new conversations
- Commit: 093f5d0

**Phase 6: Context Window for Claude 4.5 Models**
- Fixed token counter showing 247% warning due to incorrect context window
- Root cause: MODEL_CONTEXT_WINDOWS missing shorthand model names (claude-haiku-4-5, claude-opus-4-5)
- Fix: Added Claude 4.5 shorthand model names with 200K token context windows
- Commit: bb7c725

**Phase 7: ProvidersEditor Accessibility**
- Fixed Svelte a11y warnings in ProvidersEditor component
- Root causes:
  - Dialog element missing `tabindex="-1"` attribute
  - Modal overlay click handler needed ignore directives
  - Label element without associated control (section heading)
  - Password field not contained in form element
- Fixes:
  - Added `tabindex="-1"` to dialog element for keyboard accessibility
  - Added `svelte-ignore` comments for semantically correct modal backdrop
  - Changed `<label>` to `<strong class="form-label">` for section headings
  - Wrapped password input in `<form on:submit|preventDefault>` to suppress browser warning
- Commit: e7edd87

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

Before merging to `main`, verify the following Phase 1-7 features:

- [ ] **Phase 2**: Token counter displays correctly and warns at 80%
- [ ] **Phase 2**: Toast notifications appear on extraction failures
- [ ] **Phase 3**: Cultural perspectives toggle works (on/off)
- [ ] **Phase 4**: JSON repair handles malformed LLM responses
- [ ] **Phase 5**: Inline editing saves with correct attribution
- [ ] **Phase 6**: Context window overrides work in providers.json
- [ ] **Phase 7**: TOML to JSON migration works automatically on first startup
- [ ] **Phase 7**: ProvidersEditor opens from sidebar button
- [ ] **Phase 7**: Add/edit/delete providers works correctly
- [ ] **Phase 7**: API key configuration (env var vs plaintext) saves correctly
- [ ] **Phase 7**: Set default provider functionality works
- [ ] **Phase 7**: Hot-reload applies changes without service restart
- [ ] **Phase 7**: Validation catches invalid configurations

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
