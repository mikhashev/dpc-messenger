"""Where run output lives, resolved in one place.

It sits outside the working tree because the traces carry the machine that
made them — a disk serial, `~/.dpc` listings, and in four files the previews
of our own chat that a run read back through `read_session_archive`. Mike's
call, 2026-08-31 20:37 UTC — the zone is written because this box runs at
UTC+7 and the same moment is already the next day here.
`DPC_EVAL_RESULTS` overrides the base for a scratch run.
"""

from __future__ import annotations

import os
from pathlib import Path


def results_root(benchmark: str) -> Path:
    """The directory this benchmark writes its reports and traces into."""
    base = os.environ.get("DPC_EVAL_RESULTS")
    base_path = Path(base).expanduser() if base else Path.home() / ".dpc" / "eval-results"
    return base_path / benchmark
