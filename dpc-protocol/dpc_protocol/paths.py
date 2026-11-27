"""Single-source resolver for the DPC home directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Populated only by test isolation fixtures (such as dpc-client/core/tests/conftest.py)
# so tests with DPC_HOME unset do not touch the real ~/.dpc while explicit per-test
# DPC_HOME env overrides or module-attribute monkeypatches continue to take precedence.
_TEST_STAND_INS: set[Path] = set()


def _normalize_home_path(raw: str) -> Path:
    """Expand ``~`` and normalize separators/relative components into an absolute Path."""
    if raw == "~":
        expanded = Path.home()
    elif raw.startswith(("~/", "~\\")):
        # Honor monkeypatched Path.home() as well as HOME/USERPROFILE.
        rest = raw[2:].replace("\\", "/")
        parts = [p for p in rest.split("/") if p]
        expanded = Path.home().joinpath(*parts) if parts else Path.home()
    else:
        expanded = Path(os.path.expanduser(raw))
    # os.path.abspath normalizes separators, cleans '.'/'..', and resolves relative
    # paths against cwd without dereferencing symlinks.
    return Path(os.path.abspath(expanded))


def dpc_home() -> Path:
    """Return the active DPC home directory.

    Reads ``DPC_HOME`` from the environment on every call:
    - If set and non-empty after stripping whitespace, expands ``~`` and returns
      the normalized absolute ``Path``.
    - Otherwise returns ``Path.home() / ".dpc"``.
    """
    raw = os.environ.get("DPC_HOME")
    if raw is not None:
        stripped = raw.strip()
        if stripped:
            return _normalize_home_path(stripped)
    return Path.home() / ".dpc"


def resolve_module_home(
    current: Path,
    initial: Path,
    *,
    child: Optional[str] = None,
) -> Path:
    """Resolve the home (or a child path under it) for modules keeping a legacy module global.

    Priority order:
    1. An explicit per-test monkeypatch on the module attribute (a value that differs
       from both the import-time value and any conftest global stand-in).
    2. A non-empty ``DPC_HOME`` environment variable (resolved via ``dpc_home()``).
    3. A conftest global stand-in patched onto the module attribute.
    4. Dynamic ``dpc_home()``.
    """
    current_path = Path(current)
    is_stand_in = any(
        current_path == (stand_in / child if child else stand_in)
        for stand_in in _TEST_STAND_INS
    )
    if current_path != initial and not is_stand_in:
        return current_path
    raw = os.environ.get("DPC_HOME")
    if raw is not None and raw.strip():
        base = dpc_home()
        return base / child if child else base
    if current_path != initial:
        return current_path
    base = dpc_home()
    return base / child if child else base
