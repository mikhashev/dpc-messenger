# ADR-011: Poetry to uv Migration

**Status:** Proposed
**Date:** 2026-04-18
**Authors:** CC (research + draft), Ark (review), Mike (direction)
**Session:** S51

---

## Context

dpc-messenger uses Poetry 2.2.1 as its Python package manager across 3 packages (dpc-client/core, dpc-hub, dpc-protocol). Poetry cannot conditionally resolve hardware-specific dependencies — the torch CUDA vs CPU problem has been a recurring pain point since sentence-transformers was added in S50.

The root issue: Poetry's `pyproject.toml` sources are static. There is no mechanism for conditional index selection based on hardware (NVIDIA/AMD/Apple/CPU). This limitation is a 5-year-old architectural constraint (GitHub issues #697, #5330, #4991 — all open since 2021, none scheduled). Poetry 2.3.x releases focus on PEP compliance, not hardware-aware resolution.

The current workaround (`setup_gpu_support.py`) uses pip inside the Poetry venv, which breaks lock file consistency on every `poetry install`.

`uv` from Astral (Ruff team) solves the torch problem natively via `[tool.uv.sources]` + `[[tool.uv.index]]` with explicit priority. It also brings 10-100x faster resolution, PEP 621 compliance, workspace support, and built-in Python version management.

## Decision

Migrate all 3 Python packages from Poetry to uv.

### Migration Order

1. **dpc-protocol** — already uses PEP 621 `[project]` format; only `[build-system]` needs changing
2. **dpc-hub** — straightforward dependency conversion, no torch
3. **dpc-client/core** — highest complexity: torch CUDA index, local editable deps, optional MLX extras

### pyproject.toml Format Change

Poetry format (`[tool.poetry.dependencies]`) converts to PEP 621 (`[project.dependencies]`). Key mappings:

- `[tool.poetry.dependencies]` → `[project.dependencies]`
- `[tool.poetry.extras]` → `[project.optional-dependencies]`
- `[tool.poetry.group.dev.dependencies]` → `[dependency-groups]`
- `path = "...", develop = true` → `[tool.uv.sources]` with `editable = true`
- `poetry-core` build backend → `hatchling`

### PyTorch CUDA Resolution

torch is declared as a standard dependency in pyproject.toml **without** a custom index:

```toml
[project.dependencies]
torch = ">=2.4.0,<3.0.0"   # CPU default from PyPI
```

The GPU-specific index is provided via `.env` file (see ADR-012 for the device-aware automation layer). This clean separation means:

- pyproject.toml is hardware-agnostic (safe for all platforms)
- `.env` absent → CPU torch from PyPI (safe default)
- `.env` with `UV_EXTRA_INDEX_URL` → CUDA/ROCm torch from PyTorch index

This eliminates the pip-inside-poetry hack entirely.

### Build Backend

`poetry-core` is replaced by `hatchling` — the recommended PEP 517 build backend in the uv ecosystem. hatchling is faster, PEP 621 native, and maintained by the PyPA. poetry-core also works with uv but is Poetry-specific tooling we no longer need.

### Workspace Setup

Root `pyproject.toml` with uv workspace for monorepo coordination:

```toml
[tool.uv.workspace]
members = ["dpc-client/core", "dpc-hub", "dpc-protocol"]
```

### Command Equivalents

| Poetry | uv |
|--------|-----|
| `poetry install` | `uv sync` |
| `poetry install -E mlx` | `uv sync --extra mlx` |
| `poetry run python X` | `uv run python X` |
| `poetry run pytest` | `uv run pytest` |
| `poetry add package` | `uv add package` |
| `poetry lock` | `uv lock` |

## Consequences

### Positive

- Torch CUDA problem solved declaratively in pyproject.toml
- Lock file always consistent (no pip override breaking it)
- 10-100x faster dependency resolution
- PEP 621 standard format (not Poetry-specific)
- Built-in Python version management
- `setup_gpu_support.py` eliminated

### Negative

- No conditional index by environment marker — device-aware layer still needed (see ADR-012)
- 32 files need `poetry` → `uv` command updates in docs
- Contributors need uv installed (single binary, cross-platform)
- poetry.lock files become obsolete (replaced by uv.lock)

### Risks

- Complex dependency tree (aiortc + torch + transformers) may have resolution edge cases with uv
- dpc-protocol as workspace member needs verification with monorepo layout

## Scope

- 3 pyproject.toml conversions
- 3 lock file regenerations
- 32 documentation files updated
- `setup_gpu_support.py` deleted
- `check_gpu_support()` simplified
- `.gitignore` updated (uv.lock replaces poetry.lock)

## References

- Research: `ideas/uv-migration-research.md` (prompt)
- Results: `ideas/uv-migration-research-results.md` (full analysis)
- uv PyTorch guide: docs.astral.sh/uv/guides/integration/pytorch/
- Poetry issues: #697, #5330, #4991 (transitive dep exclusion — open since 2021)
