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
- **the model by its files** — for a local llama-server alias, the GGUF and
  mmproj with their SHA-256, and the llama-server binary, its pinned tag and
  the version it reports, because the entry's `model` label can be stale;
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

import hashlib
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


def effort_record(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The effort as resolved, and who chose it. Never a constant for one model.

    Unpinned, nothing is sent and the template answers with its own default —
    read here from the GGUF's embedded template, the same reader the provider
    uses. Where there is no GGUF to read, the value is recorded as unknown.
    """
    effort = entry.get("reasoning_effort")
    if effort:
        return {"value": effort, "source": "pinned by the run"}
    default = None
    try:
        from dpc_client_core.managers.llama_server_supervisor import gguf_effort_dictionary
        found = gguf_effort_dictionary(entry.get("gguf_path") or "")
        default = found[1] if found else None
    except Exception:
        pass
    if default:
        return {"value": default,
                "source": "the model's chat template default, read from the GGUF — nothing was sent"}
    return {"value": None,
            "source": "nothing was sent; the model's template default could not be read"}


_HASH_CACHE = "model-sha256-cache.json"


def file_sha256(path: Path, cache_dir: Path | None = None) -> Dict[str, Any]:
    """Path, size, mtime and SHA-256 of a model file, hashed once per version.

    A GGUF is ~15 GB, so the digest is cached in `cache_dir` keyed by
    (path, size, mtime): a re-run pays a stat, and a file replaced in place
    changes the key and is hashed again.
    """
    try:
        p = Path(path)
        st = p.stat()
    except Exception as exc:
        return {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    record = {"path": str(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    key = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    cache_file = Path(cache_dir) / _HASH_CACHE if cache_dir else None
    cache: Dict[str, str] = {}
    if cache_file is not None:
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if key in cache:
        return {**record, "sha256": cache[key], "sha256_from_cache": True}
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
                digest.update(block)
        sha = digest.hexdigest()
    except Exception as exc:
        return {**record, "error": f"{type(exc).__name__}: {exc}"}
    if cache_file is not None:
        try:
            cache[key] = sha
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except Exception:
            pass
    return {**record, "sha256": sha, "sha256_from_cache": False}


def llama_server_binary(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Which llama-server a `llamacpp_server` alias would start, and its version.

    Resolved the way the provider resolves it (a configured `binary_path` wins,
    else the pinned install), without touching the network. `--version` prints
    and exits; it loads no model.
    """
    try:
        from dpc_client_core.managers import llama_server_fetcher as fetcher
    except Exception as exc:
        return {"error": f"fetcher not importable: {type(exc).__name__}: {exc}"}
    out: Dict[str, Any] = {
        "pinned_tag": fetcher.LLAMA_CPP_TAG,
        "pinned_install_root": str(fetcher.install_root()),
        "binary_path_configured": entry.get("binary_path"),
    }
    try:
        binary = fetcher.resolve_binary(entry)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["binary"] = str(binary) if binary else None
    out["binary_is_the_pin"] = bool(binary) and not entry.get("binary_path")
    if binary is None:
        out["error"] = ("no installed binary at the pinned tag; the provider would "
                        "download it at first use")
        return out
    try:
        # llama-server prints its version on stderr.
        res = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                             timeout=60, errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        text = f"[unavailable: {type(exc).__name__}]"
    lines = [ln.strip() for ln in text.splitlines()
             if "version" in ln.lower() or "built with" in ln.lower()]
    out["version"] = lines[:3] or [text[:200]]
    return out


def model_files(entry: Dict[str, Any], cache_dir: Path | None = None) -> Dict[str, Any]:
    """The model identified by its files, not by the entry's free-text `model` label."""
    try:
        if entry.get("type") != "llamacpp_server":
            return {"note": f"provider type {entry.get('type')!r}: no local model files to record"}
        record: Dict[str, Any] = {
            "model_label": entry.get("model"),
            "note": "the `model` field is a free label and can be stale; the model is "
                    "gguf.path + gguf.sha256",
            "llama_server": llama_server_binary(entry),
        }
        for key in ("gguf_path", "mmproj"):
            if entry.get(key):
                record[key.replace("_path", "")] = file_sha256(Path(entry[key]), cache_dir)
        return record
    except Exception as exc:  # never cost a run
        return {"error": f"{type(exc).__name__}: {exc}"}


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
        return {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "provider": entry,
            "reasoning_effort": effort_record(provider_entry),
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
