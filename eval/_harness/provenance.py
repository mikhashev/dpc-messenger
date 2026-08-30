"""Everything a later reader needs to know what produced a number.

A benchmark result without its conditions is not a measurement, it is an
anecdote with a decimal point. This session already produced two arguments
that a provenance block would have settled in one line — the same corpus
counted three ways, and a published 69.8 % that turned out to have been run
with hybrid recall on against a second daemon.

Captured, in the order it matters:

- **the provider, verbatim** — the whole alias entry, so the model, the
  quantisation, the context window, the cache types and every sampling
  parameter travel with the score;
- **the reasoning effort as resolved**, and whether we set it or the model's
  own template supplied it;
- **the code** — git sha, branch, and whether the tree was dirty, with the
  dirty files named. `dirty: true` with no list is how a number becomes
  unreproducible three weeks later;
- **the dataset** — repo, the resolved snapshot revision, the split file;
- **the machine** — GPU model, VRAM, driver, CPU count, total RAM, OS;
- **the harness** — its own git sha and the exact flags it was invoked with.

Nothing here may raise: a provenance bug must not cost a three-hour run.
Every collector returns its own error string instead of throwing, because a
recorded «could not read» is worth more than a missing key that a reader will
silently assume something about.

Secrets are removed by key name before anything is written.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_SECRET_HINTS = ("token", "api_key", "apikey", "secret", "password", "credential")


def _redact(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Drop anything whose key smells like a secret. Names of env vars stay.

    A number is never a secret, and matching the key alone hid a live sampling
    knob: `reasoning_budget_tokens` contains "token", so every provenance file
    on disk reads `"[redacted]"` where the reasoning budget was — in the one
    file whose purpose is «everything needed to run this again». Found by
    GLM 5.3 in the 2026-08-30 history audit.
    """
    out = {}
    for k, v in entry.items():
        lowered = k.lower()
        secretish = any(h in lowered for h in _SECRET_HINTS) and not lowered.endswith("_env")
        out[k] = "[redacted]" if secretish and isinstance(v, str) else v
    return out


def _run(cmd, cwd=None, timeout=30, keep_indent=False) -> str:
    """`keep_indent` for output whose leading spaces are data.

    `git status --porcelain` puts the status in columns 1-2, so an unstaged
    edit is ` M path`. Stripping the whole blob eats the first line's leading
    space, and the `[3:]` that removes the columns then eats a character of the
    path: every dirty provenance file on disk names its first dirty file as
    `pc-client/…`. Found by Fable 5 in the 2026-08-30 history audit.
    """
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return res.stdout.rstrip() if keep_indent else res.stdout.strip()
    except Exception as exc:
        return f"[unavailable: {type(exc).__name__}]"


def _git(repo: Path) -> Dict[str, Any]:
    try:
        sha = _run(["git", "rev-parse", "HEAD"], cwd=str(repo))
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo))
        status = _run(["git", "status", "--porcelain"], cwd=str(repo), keep_indent=True)
        dirty_files = [ln[3:] for ln in status.splitlines() if ln.strip()]
        return {
            "sha": sha,
            "branch": branch,
            "dirty": bool(dirty_files),
            # Named, not counted: «dirty: true» alone tells a later reader
            # nothing about whether it mattered.
            "dirty_files": dirty_files[:40],
            "dirty_file_count": len(dirty_files),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _gpu() -> Dict[str, Any]:
    line = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader",
    ])
    if line.startswith("["):
        return {"error": line}
    parts = [p.strip() for p in line.split(",")]
    keys = ["name", "memory_total", "driver_version", "compute_capability"]
    return dict(zip(keys, parts))


def _machine() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
    }
    try:
        import shutil
        info["disk_free_gb"] = round(shutil.disk_usage(Path.home()).free / 1e9, 1)
    except Exception as exc:
        info["disk_free_gb"] = f"[unavailable: {type(exc).__name__}]"
    if platform.system() == "Windows":
        out = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize",
        ])
        try:
            info["ram_gb"] = round(int(out) / 1e6, 1)
        except Exception:
            info["ram_gb"] = f"[unavailable: {out[:40]}]"
    return info


def _packages(names) -> Dict[str, str]:
    versions = {}
    for name in names:
        try:
            from importlib.metadata import version
            versions[name] = version(name)
        except Exception:
            versions[name] = "[not installed]"
    return versions


def snapshot(
    *,
    repo_root: Path,
    provider_entry: Dict[str, Any],
    dataset: Dict[str, Any],
    harness_file: Path,
    argv: list,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """One dict holding everything needed to run this again and to argue about it."""
    try:
        entry = _redact(dict(provider_entry))
        effort = entry.get("reasoning_effort")
        return {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "provider": entry,
            "reasoning_effort": {
                "value": effort or "xhigh",
                "source": "pinned by the run" if effort else
                          "the model's chat template default — nothing was sent",
            },
            "code": {
                "repo": _git(repo_root),
                "harness": str(harness_file.relative_to(repo_root))
                           if harness_file.is_relative_to(repo_root) else str(harness_file),
                "argv": list(argv),
            },
            "dataset": dataset,
            "machine": {**_machine(), "gpu": _gpu()},
            "packages": _packages([
                "huggingface_hub", "pyarrow", "openai", "ollama",
                "sentence-transformers", "torch", "faiss-cpu",
            ]),
            **(extra or {}),
        }
    except Exception as exc:  # never cost a run
        return {"error": f"provenance collection failed: {type(exc).__name__}: {exc}"}


def write_beside(report_path: Path, block: Dict[str, Any]) -> None:
    """Also drop it next to the report, so a stray JSON never travels alone."""
    try:
        target = report_path.with_suffix(".provenance.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(block, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
