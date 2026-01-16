# Contributing to D-PC Messenger

## Branching Strategy

We use a two-branch workflow to maintain a stable codebase:

### Branches

- **`main`** - Stable, production-ready code
  - Always deployable
  - Tested and documented
  - Users run from this branch
  - Protected from direct commits

- **`dev`** - Development and integration branch
  - All new features go here first
  - Testing happens here
  - May contain untested/experimental code
  - Merges to `main` only after verification

### Development Workflow

#### 1. Create Feature Branch (Optional)

For large features, create a branch from `dev`:

```bash
git checkout dev
git pull origin dev
git checkout -b feature/my-feature
# ... make changes ...
git add -A
git commit -m "feat: implement my feature"
git push -u origin feature/my-feature
```

#### 2. Work Directly on `dev` (Small Changes)

For small changes, work directly on `dev`:

```bash
git checkout dev
git pull origin dev
# ... make changes ...
git add -A
git commit -m "fix: resolve bug in X"
git push origin dev
```

#### 3. Testing Checklist (Before Merging to `main`)

Before merging `dev` → `main`, verify:

**Backend Testing:**
```bash
cd dpc-client/core
poetry run pytest -v                    # All tests pass
poetry run python run_service.py        # Service starts without errors
```

**Frontend Testing:**
```bash
cd dpc-client/ui
npm run check                           # TypeScript checks pass
npm run build                           # Build succeeds
npm run tauri dev                       # App launches and functions
npm run test                            # Vitest tests pass
```

**Frontend E2E Testing (Playwright - In Development):**
```bash
cd dpc-client/ui
npm run test:e2e                        # Run Playwright tests (when implemented)
npm run test:e2e:ui                     # Run with UI mode
npm run test:e2e:debug                  # Debug mode with inspector
```

**Status:** Playwright tests are planned but not yet implemented. Current testing uses Vitest for unit tests only.

**Planned Test Coverage:**
- [ ] User authentication flows
- [ ] P2P connection establishment
- [ ] Voice message recording and playback
- [ ] File transfer UI

**Hub Testing (if applicable):**
```bash
cd dpc-hub
poetry run pytest -v                    # All tests pass
poetry run uvicorn dpc_hub.main:app --reload  # Service starts
```

**Manual Testing:**
- [ ] Core functionality works (send messages, connect peers)
- [ ] New features work as expected
- [ ] No regressions in existing features
- [ ] UI displays correctly
- [ ] Configuration changes documented

#### 4. Merge to `main`

When all tests pass and features are verified:

```bash
# Switch to main and merge
git checkout main
git pull origin main
git merge dev --no-ff -m "Merge dev: [description of changes]"

# Tag release (optional)
git tag -a v0.X.Y -m "Release v0.X.Y: [summary]"

# Push to remote
git push origin main
git push origin v0.X.Y
```

#### 5. Update Documentation

After merging to `main`, update:

- **README.md** - If user-facing features changed
- **CHANGELOG.md** - Document all changes in this release
- **docs/** - Update relevant documentation files
- **CLAUDE.md** - Update if architecture or commands changed

### Commit Message Format

Use conventional commits:

```
feat: add new feature
fix: resolve bug in X
docs: update documentation
refactor: restructure code without changing behavior
test: add or update tests
chore: maintenance tasks (dependencies, config)
```

### Example: Current State

**Current Status (2025-01-16):**
- `main`: Stable v0.12.0
- `dev`: v0.15.0 development (92 commits ahead of main)

**Upcoming Release v0.15.0 includes:**
- Voice messages with local Whisper transcription (v0.13.0-v0.13.3)
- Telegram bot integration (v0.14.0+)
- Linux audio recording support via Rust (v0.15.0)
- Z.AI provider for GLM models (v0.14.0+)
- Token usage estimation with progressive warnings
- Whisper VRAM management and model unloading
- WAV format support for cross-platform compatibility
- 40+ bug fixes and improvements

**Release v0.12.0 (Previous):**
- Vision/Image support (screenshot sharing, remote vision inference)
- Session management with voting
- Chat history synchronization
- Token counting system (Phase 3-4) with pre-query validation
- AI instruction sets & wizard (multi-phase complete)
- AI scope field-level filtering for device context
- Pure mode AI chat (zero system instructions)
- 30+ bug fixes (including critical TLS validation, hardcoded instructions leak)
- Breaking changes (migration code removed, provider schema)

## Code Style

### Python
- Follow PEP 8
- Use type hints
- Use Black for formatting: `poetry run black .`
- Use flake8 for linting: `poetry run flake8 .`

### TypeScript/Svelte
- Use TypeScript for type safety
- Follow SvelteKit 5.0 conventions
- Use Prettier for formatting

### General
- Write descriptive commit messages
- Add comments for complex logic
- Update tests when changing behavior
- Keep functions focused and small

## Testing

### Writing Tests

**Python (pytest):**
```python
# tests/test_feature.py
import pytest
from dpc_client_core.feature import MyClass

def test_my_function():
    result = MyClass().my_method()
    assert result == expected_value
```

**Coverage Reports:**
```bash
poetry run pytest --cov=dpc_client_core --cov-report=html
# View in browser: htmlcov/index.html
```

## Questions?

If you have questions about contributing, please:
- Check existing documentation in `docs/`
- Review `CLAUDE.md` for architecture details
- Open an issue on GitHub
