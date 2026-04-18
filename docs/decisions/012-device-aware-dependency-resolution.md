# ADR-012: Device-Aware Dependency Resolution

**Status:** Accepted
**Date:** 2026-04-18
**Authors:** CC (draft), Ark (architecture proposal), Mike (direction + original idea)
**Session:** S51
**Depends on:** ADR-011 (Poetry to uv migration)

---

## Context

DPC Messenger runs on diverse hardware: Windows/Linux/macOS, with NVIDIA/AMD/Apple/no GPU. Hardware-dependent packages (torch, potential future GPU libraries) require different variants per device. The current approach — a manual `setup_gpu_support.py` script that duplicates GPU detection already done by `device_context_collector.py` — is fragile and breaks lock file consistency.

Mike's insight (S51 [8]): DPC already collects device context at every startup. This data should drive dependency installation automatically.

After ADR-011, uv handles the package management side cleanly. But uv's index configuration is static in `pyproject.toml` — it cannot conditionally select CUDA vs CPU vs ROCm based on detected hardware. A runtime layer is needed to bridge device context to uv's configuration.

## Decision

Implement a device-aware dependency resolution layer that reads `device_context.json` and generates a `.env` file with the correct `UV_EXTRA_INDEX_URL`. uv natively reads `.env` files during `uv sync`, completing the automation chain.

### Architecture

```
device_context_collector.py (existing, runs at DPC startup)
         ↓
    device_context.json (GPU type, CUDA version, OS)
         ↓
    dependency_setup() (new, in run_service.py)
      - reads device_context.json
      - maps GPU type → PyTorch index URL
      - writes .env with UV_EXTRA_INDEX_URL
         ↓
    uv sync (reads .env automatically)
         ↓
    correct torch variant installed
```

### GPU → Index Mapping

| GPU Type | CUDA Version | UV_EXTRA_INDEX_URL |
|----------|-------------|-------------------|
| nvidia | 11.x | `https://download.pytorch.org/whl/cu118` |
| nvidia | 12.0-12.3 | `https://download.pytorch.org/whl/cu121` |
| nvidia | 12.4+ | `https://download.pytorch.org/whl/cu124` |
| amd (ROCm) | any | `https://download.pytorch.org/whl/rocm6.2` (update URL when new ROCm versions ship) |
| apple (MPS) | n/a | (not set — PyPI default, MPS built-in) |
| none | n/a | (not set — PyPI default, CPU torch) |

This mapping reuses the logic already in `setup_gpu_support.py:map_cuda_to_pytorch()`, but reads from `device_context.json` instead of running nvidia-smi independently.

### What Gets Removed

- **`setup_gpu_support.py`** (279 lines) — entire file deleted; functionality replaced by dependency_setup() + .env
- **`check_gpu_support()`** in `run_service.py` (55 lines) — replaced by dependency_setup() which both detects and fixes
- **Duplicate GPU detection** — `device_context_collector.py` becomes the single source of truth

### What Gets Added

- **`dependency_setup()`** function (~40 lines) in `run_service.py`:
  - Reads `~/.dpc/device_context.json`
  - Extracts `hardware.gpu.type` and `hardware.gpu.cuda_version`
  - Maps to index URL using table above
  - Writes/updates `.env` in workspace root (`dpc-messenger/.env`) with `UV_EXTRA_INDEX_URL`
  - Staleness check: hashes `device_context.json` content, compares with hash stored in `.env` comment line; regenerates if mismatch
  - After generating/updating `.env`, calls `subprocess.run(["uv", "sync"])` to install correct torch variant automatically
  - Runs on first startup (no `.env` exists) and on subsequent startups if device context hash changed

- **`.env` file** in workspace root (`dpc-messenger/.env`, gitignored, device-specific):
  ```
  # device_context_hash: a1b2c3d4e5f6
  UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cu124
  ```

### Extensibility

The `.env` approach scales to future hardware-dependent packages. If a new package needs a device-specific index, add another line to the mapping and another env var. The pattern is: device context → mapping table → env var → uv reads it.

The mapping table can later be externalized to a JSON config file (`hardware_packages.json`) if the number of hardware-dependent packages grows beyond torch.

## Consequences

### Positive

- Zero manual steps for GPU setup — fully automatic on first DPC startup
- Single source of hardware detection (device_context_collector.py)
- Cross-platform: works on Windows/Linux/macOS with NVIDIA/AMD/Apple/CPU
- `.env` is a standard mechanism — uv, pip, and other tools understand it
- Net code reduction: delete 279 + 55 lines, add ~40 lines

### Negative

- `.env` is per-machine, not committed to git — new contributors need first startup to generate it
- If device context is stale (hardware changed between startups), wrong index may persist until restart
- uv must be installed before `.env` is useful (ordering: install uv → first startup generates .env → uv sync uses it)

### Risks

- CUDA wheels from pytorch index install successfully on CPU-only machines (just larger ~2GB vs ~200MB) — no install failure, but wasted bandwidth if .env generation fails to detect missing GPU

## User Experience

### New User Setup (after ADR-011 + ADR-012)

```bash
# 1. Install uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
curl -LsSf https://astral.sh/uv/install.sh | sh              # Linux/macOS

# 2. Clone and install
git clone https://github.com/mikhashev/dpc-messenger
cd dpc-messenger/dpc-client/core
uv sync                    # installs CPU torch by default

# 3. First run — fully automatic
uv run python run_service.py
# → device_context.json generated
# → .env generated with UV_EXTRA_INDEX_URL=.../cu124
# → dependency_setup() calls "uv sync" automatically
# → CUDA torch installed, service starts with GPU acceleration
```

No manual re-sync needed — `dependency_setup()` handles it programmatically.

## References

- ADR-011: Poetry to uv migration (prerequisite)
- `device_context_collector.py`: existing hardware detection
- `setup_gpu_support.py`: current workaround being replaced
- uv .env support: docs.astral.sh/uv/configuration/environment/
- S51 discussion: messages [8]-[50] in agent chat
