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
```

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

**Current Status (2025-12-25):**
- `main`: Stable v0.12.0 (Vision & Image Support complete)
- `dev`: v0.12.1+ development (future features)

**Release v0.12.0 includes:**
- Vision/Image support (screenshot sharing, remote vision inference)
- Session management with voting
- Chat history synchronization
- 11 critical bug fixes
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
