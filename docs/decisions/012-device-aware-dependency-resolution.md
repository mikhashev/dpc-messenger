# ADR-012: Device-Aware Dependency Resolution

**Status:** Accepted (Revised S69)
**Date:** 2026-04-18 (revised 2026-04-24)
**Authors:** CC (draft), Ark (architecture proposal), Mike (direction + original idea)
**Session:** S51, revised S69
**Depends on:** ADR-011 (Poetry to uv migration)

---

## Context

DPC Messenger runs on diverse hardware: Windows/Linux/macOS, with NVIDIA/AMD/Apple/no GPU. Hardware-dependent packages (torch, potential future GPU libraries) require different variants per device. The current approach — a manual `setup_gpu_support.py` script that duplicates GPU detection already done by `device_context_collector.py` — is fragile and breaks lock file consistency.

Mike's insight (S51 [8]): DPC already collects device context at every startup. This data should drive dependency installation automatically.

After ADR-011, uv handles the package management side cleanly. But uv's index configuration is static in `pyproject.toml` — it cannot conditionally select CUDA vs CPU vs ROCm based on detected hardware. A runtime layer is needed to bridge device context to uv's configuration.

## Decision

~~Original (S51): `.env` with `UV_EXTRA_INDEX_URL` approach — abandoned.~~

**Revised (S69):** Use uv platform markers in `pyproject.toml` `[tool.uv.sources]` with `explicit = true` indexes. This is the [officially recommended approach](https://docs.astral.sh/uv/guides/integration/pytorch/) for cross-platform PyTorch with `uv sync`.

### Why `.env` Was Abandoned

The original ADR proposed generating a `.env` file with `UV_EXTRA_INDEX_URL` for per-GPU torch resolution. Testing in S69 proved this **does not work**: uv resolves torch from PyPI (CPU) first regardless of `UV_EXTRA_INDEX_URL`, even with `--index-strategy unsafe-best-match`. The `explicit = true` flag in `[tool.uv.sources]` is the only mechanism that forces uv to use a specific index for a specific package.

### Current Implementation

```toml
[tool.uv.sources]
torch = { index = "pytorch-cu124", marker = "sys_platform != 'darwin'" }
torchvision = { index = "pytorch-cu124", marker = "sys_platform != 'darwin'" }

[[tool.uv.index]]
name = "pytorch-cu124"
url = "https://download.pytorch.org/whl/cu124"
explicit = true
```

### Platform Coverage

| Platform | torch variant | Automatic? |
|----------|--------------|------------|
| Windows + NVIDIA | CUDA cu124 | Yes (marker) |
| Linux + NVIDIA | CUDA cu124 | Yes (marker) |
| macOS (Intel/Apple) | CPU (PyPI) | Yes (marker excludes darwin) |
| Linux without GPU | CUDA cu124 (works, larger download) | Yes |
| Linux + AMD (ROCm) | CPU (PyPI) | No — manual: `uv pip install torch --torch-backend=auto` |

### What Was Removed

- **`setup_gpu_support.py`** (279 lines) — deleted in S51
- **`uv.lock` from git** — lockfiles are platform-specific, generated locally (S69)

### What Remains

- **`dependency_setup()`** in `run_service.py` — status check only (warns if NVIDIA detected but torch lacks CUDA)
- **Platform markers** in `pyproject.toml` — the actual resolution mechanism

## Consequences

### Positive

- Zero manual steps on Windows/Linux (NVIDIA) and macOS — `uv sync` resolves correct torch automatically
- Platform markers are a standard uv mechanism, not a custom workaround
- `uv.lock` removed from git — each platform generates its own lockfile

### Negative

- CUDA wheels install on Linux without GPU (~2GB vs ~200MB CPU) — no failure, just wasted bandwidth
- AMD ROCm requires manual step (`uv pip install torch --torch-backend=auto`)
- `--torch-backend=auto` not available in `uv sync` (only `uv pip`) — limits full automation

## User Experience

### New User Setup

```bash
# 1. Install uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
curl -LsSf https://astral.sh/uv/install.sh | sh              # Linux/macOS

# 2. Clone and install — automatic GPU detection
git clone https://github.com/mikhashev/dpc-messenger
cd dpc-messenger/dpc-client/core
uv sync                    # CUDA on Windows/Linux, CPU on macOS

# 3. Run
uv run python run_service.py

# AMD ROCm users (manual):
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.2
```

## References

- ADR-011: Poetry to uv migration (prerequisite)
- [uv PyTorch guide](https://docs.astral.sh/uv/guides/integration/pytorch/) — official documentation
- S69 testing: confirmed `.env` approach does not work for per-package index override
- `setup_gpu_support.py`: current workaround being replaced
- uv .env support: docs.astral.sh/uv/configuration/environment/
- S51 discussion: messages [8]-[50] in agent chat
