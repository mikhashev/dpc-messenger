"""
Strata Model Fetcher: installing one Strata model through Strata's own `setup.py`.

A download module in the sense of `ModelDownloadService` (`_OTHER_DOWNLOADS`, the name
`strata-model`): the owner asks for a model with `install_strata_model`, the consent
dialog shows the size from Strata's own `model_table`, and the owner's confirm runs
Control's `setup.py` in a process of its own, in Strata's app mode (`STRATA_CONTROL_INSTALL`).
Control's setup downloads the model files, prepares them, and writes the run config
`strata-<id>.json`; DPC reads that file's path back.

What lands where, for the model files folder `F` (`Settings.get_model_files_dir`):
- `F/strata/` is the data folder (`--data-dir`): `models/`, `packs/`, `mtp/` under it;
- `F/strata/run-configs/` is the install folder (`STRATA_INSTALL_DIR`): run configs, logs,
  `third_party/`;
- `F/strata/settings.json` is the settings file (`STRATA_SETTINGS_FILE`): the owner's own
  Strata data folder is neither read nor changed.

The owner's choice reaches this module in `config`, because the download interface passes one
(`{"strata_model": {family, model, images, gguf_dir}, "model_files_dir": "..."}`); the
service builds it from the `strata_model` the consent payload carried.
"""

import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import strata_fetcher

logger = logging.getLogger(__name__)

DOWNLOAD_NAME = "strata-model"
PURPOSE = "Running this model through DPC's own install of Strata"
CONSEQUENCE_IF_DECLINED = "The model is not installed, so no Strata alias can run it until it is."
# The size is Strata's `download_gb` for the size, a rounded figure; with a `gguf_dir` the
# model itself costs nothing to fetch.
SIZE_SOURCE = "model_table"

# The downloads are 23-188 GB and setup continues where it stopped, so the limit is only a
# stop for a hung process, long past the slowest plausible link.
_INSTALL_TIMEOUT_S = 24 * 3600

_EXIT_MEANING = {2: "refused its arguments", 5: "refused to fetch something"}

# Strata's own words, re-read when the pin moves (see strata_fetcher.py):
# `setup.py`'s `step()` heading,
_STEP_HEADING = re.compile(r"^=== Step (?P<n>\d+): (?P<title>.+?) ===$")
# a download's redrawn progress line (`download()`, mtp_fetch.py's `Progress`, `_hash_file`),
_FETCH_LINE = re.compile(
    r"^\s*(?P<label>.+?):\s+(?P<done>\d+(?:\.\d+)?)\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*GB\s*\((?P<percent>\d+)%\)\s*$"
)
# the hashing pass of an existing file, which is not bytes fetched,
_CHECKING_LABEL = "checking "
# and `fail()`'s `[X]` line, with its hint on the next line, indented by seven spaces.
_FAILED_LINE = "[X]"
_HINT_INDENT = "       "
_ARGPARSE_ERROR = re.compile(r"(?:setup|strata)(?:\.py)?: error: (?P<why>.+)")

# (Control root, family, model) -> the model_table row's id, download_gb and images. The
# table is the pin's, so the root (which names the tag) is part of the key.
_ROWS: Dict[Tuple[str, str, str], Dict[str, Any]] = {}


def _choice(config: Optional[Dict[str, Any]]) -> Tuple[str, str, bool, Optional[str]]:
    """(family, model, images, gguf_dir) the owner asked for; ValueError when it is not a request."""
    asked = (config or {}).get("strata_model")
    if not isinstance(asked, dict):
        raise ValueError("no Strata model was chosen")
    family, model, images, gguf_dir = (asked.get(k) for k in ("family", "model", "images", "gguf_dir"))
    if not (isinstance(family, str) and family and isinstance(model, str) and model):
        raise ValueError("a Strata model is chosen by its family and model")
    if not isinstance(images, bool):
        raise ValueError("images is true or false")
    if gguf_dir is not None:
        if not isinstance(gguf_dir, str) or not Path(gguf_dir).is_absolute() or not Path(gguf_dir).is_dir():
            raise ValueError("gguf_dir must be an existing absolute folder")
    return family, model, images, gguf_dir


def _folder(config: Optional[Dict[str, Any]]) -> Path:
    raw = (config or {}).get("model_files_dir")
    if not raw:
        raise ValueError("the model files folder is not known")
    return Path(raw)


def _row(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Strata's table row for the chosen size (`id`, `download_gb`, `images`); ValueError when this
    Strata does not offer it on this PC, RuntimeError when the table cannot be read."""
    family, model, images, _gguf_dir = _choice(config)
    key = (str(strata_fetcher.install_root()), family, model)
    row = _ROWS.get(key)
    if row is None:
        for fam in strata_fetcher._strata_models_table(_folder(config)):
            for size in fam.get("sizes") or []:
                if fam.get("family") == family and size.get("model") == model:
                    row = {"id": str(size["id"]).lower(), "download_gb": float(size["download_gb"]),
                           "images": bool(size.get("images", True))}
        if row is None:
            raise ValueError(f"Strata {strata_fetcher.STRATA_RELEASE_TAG} does not offer {family} {model} on this PC")
        if Path(row["id"]).name != row["id"]:
            raise ValueError(f"Strata named {family} {model} {row['id']!r}, which is not a file name")
        _ROWS[key] = row
    if images and not row["images"]:
        raise ValueError(f"{family} {model} cannot read pictures; choose it without pictures")
    return row


def _run_configs_dir(config: Optional[Dict[str, Any]]) -> Path:
    return _folder(config) / "strata" / "run-configs"


def run_config_path(config: Optional[Dict[str, Any]] = None) -> Path:
    """Where Strata's setup writes the run config of the chosen size: `strata-<id>.json`."""
    return _run_configs_dir(config) / f"strata-{_row(config)['id']}.json"


def size_source(config: Optional[Dict[str, Any]] = None) -> str:
    return SIZE_SOURCE


def is_present(config: Optional[Dict[str, Any]] = None) -> bool:
    """A run config for the chosen size is already in the model files folder's run-configs."""
    return run_config_path(config).is_file()


def size_bytes(config: Optional[Dict[str, Any]] = None) -> int:
    """Strata's `download_gb` for the size, as bytes (a rounded figure); 0 with a `gguf_dir`, whose
    files Strata takes in place."""
    row = _row(config)
    return 0 if _choice(config)[3] else int(round(row["download_gb"] * 1e9))


def location(config: Optional[Dict[str, Any]] = None) -> str:
    return str(_folder(config) / "strata")


class _Tracker:
    """Setup's output as progress: one bar across every file it fetches, in bytes.

    Each fetch line is `<label>: x / y GB (p%)`; a label is one file (a shard, the picture
    reader, the draft layer). The bar is the sum over labels against the larger of the
    table's figure and the sum of the totals seen, so it can only grow, and a file Strata
    finds already complete adds nothing."""

    def __init__(self, expected_total: int, report: Optional[Callable[[int, int], None]]):
        self._lock = threading.Lock()
        self._done: Dict[str, int] = {}
        self._total: Dict[str, int] = {}
        self._expected = expected_total
        self._shown = 0
        self._report = report

    def feed(self, line: str) -> None:
        text = line.strip()
        step = _STEP_HEADING.match(text)
        if step:
            logger.info("Strata setup, step %s: %s", step["n"], step["title"])
            return
        if text.startswith("[!]"):
            logger.warning("Strata setup: %s", text)
            return
        found = _FETCH_LINE.match(line)
        if not found or found["label"].strip().startswith(_CHECKING_LABEL):
            return
        label = found["label"].strip()
        total = int(round(float(found["total"]) * 1e9))
        done = min(int(round(float(found["done"]) * 1e9)), total)
        with self._lock:
            self._done[label] = max(self._done.get(label, 0), done)
            self._total[label] = max(self._total.get(label, 0), total)
            self._shown = max(self._shown, sum(self._done.values()))
            shown, whole = self._shown, max(self._expected, sum(self._total.values()))
        if self._report:
            self._report(shown, whole)

    def finished(self) -> None:
        """Setup ended well: the bar is full, whatever it found already on the disk."""
        with self._lock:
            whole = max(self._expected, sum(self._total.values()))
            self._shown = max(self._shown, whole)
            shown = self._shown
        if self._report and whole:
            self._report(shown, whole)


def _failure_reason(stdout: str, stderr: str) -> str:
    """Why setup stopped, in its own words: the last `[X]` line with the hint line under it; else
    argparse's refusal, a traceback's last line, or the tail of what it printed."""
    lines = (stdout or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith(_FAILED_LINE):
            reason = lines[i].strip()[len(_FAILED_LINE):].strip()
            hint = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].startswith(_HINT_INDENT) else ""
            return f"{reason} ({hint})" if hint else reason
    err_lines = [ln.rstrip() for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in reversed(err_lines):
        refusal = _ARGPARSE_ERROR.match(ln.strip())
        if refusal:
            return refusal["why"].strip()[:300]
    if "Traceback (most recent call last)" in (stderr or "") and err_lines:
        return err_lines[-1].strip()[:300]
    tail = err_lines[-3:] or [ln.rstrip() for ln in lines if ln.strip()][-3:]
    return " | ".join(t.strip() for t in tail)[-400:] or "no message"


def download(
    on_progress: Optional[Callable[[int, int], None]] = None, config: Optional[Dict[str, Any]] = None
) -> str:
    """Run Control's `setup.py` for the chosen size; blocking, run it in a thread. Returns the
    path of the run config it wrote. RuntimeError names why when it did not."""
    from ..dpc_agent.tools import process as _process

    family, model, images, gguf_dir = _choice(config)
    row = _row(config)
    root = strata_fetcher.install_root()
    python = strata_fetcher._control_python(root, "setup.py")
    data = _folder(config) / "strata"
    run_configs = _run_configs_dir(config)
    run_configs.mkdir(parents=True, exist_ok=True)

    # Control's own interpreter, not isolated (-I): setup.py finds `strata_control` beside itself,
    # which isolated mode would not search; no user site, no inherited search paths (_control_env).
    cmd = [
        str(python), str(root / "setup.py"), "--yes", "--no-start",
        "--family", family, "--model", model,
        # `images` is Strata's `--vision`: yes = the reader that suits this card, no = text only
        "--vision", "yes" if images else "no",
        "--data-dir", str(data),
    ]
    if gguf_dir:
        cmd += ["--gguf-dir", gguf_dir]
    env = strata_fetcher._control_env(root, data / "settings.json", {
        "STRATA_CONTROL_INSTALL": "1",
        "STRATA_INSTALL_DIR": str(run_configs),
        "PYTHONUNBUFFERED": "1",
    })
    tracker = _Tracker(size_bytes(config), on_progress)
    logger.info("Installing Strata model %s %s (images %s%s) into %s", family, model, images,
                f", files in {gguf_dir}" if gguf_dir else "", data)
    # ceiling_mb=0: no memory ceiling, on purpose. Setup's pack tools map the multi-GB model
    # files, which counts as the footprint, so the default 8 GiB ceiling would kill a
    # consented install half-way; the owner agreed to the whole install in the dialog.
    run = _process.run_supervised(
        cmd,
        launcher="strata",
        launcher_kind="subsystem",
        timeout=_INSTALL_TIMEOUT_S,
        cwd=str(root),
        env=env,
        ceiling_mb=0,
        on_stdout=tracker.feed,
        on_stderr=tracker.feed,
        cr_ends_line=True,
    )
    if run.timed_out:
        raise RuntimeError(
            f"Strata's setup ran past {_INSTALL_TIMEOUT_S // 3600} hours and was stopped ({run.killed}); "
            "install again to continue: what is downloaded is kept"
        )
    if run.returncode != 0:
        meaning = _EXIT_MEANING.get(run.returncode)
        raise RuntimeError(
            f"Strata's setup did not install {family} {model} (exit {run.returncode}"
            f"{', ' + meaning if meaning else ''}): {_failure_reason(run.stdout, run.stderr)}"
        )
    written = run_configs / f"strata-{row['id']}.json"
    if not written.is_file():
        raise RuntimeError(f"Strata's setup ended well but wrote no {written.name} in {run_configs}")
    tracker.finished()
    return str(written)
