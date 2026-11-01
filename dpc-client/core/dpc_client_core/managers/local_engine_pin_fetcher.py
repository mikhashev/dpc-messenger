"""
Local Engine Pin Fetcher — the generic half of a pinned download under `~/.dpc/bin/`.

`llama_server_fetcher.py` (llama.cpp) and, next, `strata_fetcher.py` (Strata)
install a pinned release the same way: stream each asset into a temporary file,
hash it as it lands, extract it into `<root>.staging`, let the caller check what
came out, write the `.dpc-pin.json` marker, then swap staging into place. What
differs per engine (the asset table, the check, the marker's extra fields)
stays with the engine's fetcher; this module holds only the steps they share.
"""

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))


def marker_path(root: Path) -> Path:
    return root / ".dpc-pin.json"


def read_marker(root: Path) -> Optional[Dict[str, Any]]:
    """The marker as a dict; None when missing, unreadable, or not a JSON object."""
    try:
        pinned = json.loads(marker_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return pinned if isinstance(pinned, dict) else None


def download_asset(
    url: str,
    asset: Dict[str, Any],
    dest: Path,
    progress: Optional[Callable[[int, int], None]],
) -> None:
    """Stream one asset to dest.part, hashing as it lands; verify before rename."""
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, asset["size"])
        actual = digest.hexdigest()
        if actual != asset["sha256"]:
            raise ValueError(
                f"{asset['name']}: sha256 mismatch — expected {asset['sha256']}, got {actual}; "
                "the download is not the bytes we pinned"
            )
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract with zip-slip / tar-slip refused, not just hoped away."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(dest.resolve()):
                    raise ValueError(f"archive member escapes install dir: {member.filename}")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def install_with_staging_swap(
    root: Path,
    assets: List[Dict[str, Any]],
    url_base: str,
    marker: Dict[str, Any],
    check: Optional[Callable[[Path], Optional[Dict[str, Any]]]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    temp_prefix: str = "pin-",
) -> None:
    """Download `assets` from `url_base`, extract into `<root>.staging`, swap it into `root`.

    `check(staging)` runs on the extracted tree and may raise to abort; the dict it
    returns is merged into the marker. Nothing is left behind on a failure, and an
    install already at `root` survives any failure before or during the swap.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as tmp:
        tmp_dir = Path(tmp)
        staging = root.with_name(root.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            for asset in assets:
                archive = tmp_dir / asset["name"]
                logger.info("Fetching %s (%d bytes)", asset["name"], asset["size"])
                download_asset(f"{url_base}/{asset['name']}", asset, archive, progress)
                extract_archive(archive, staging)
            extra = check(staging) if check else None
            marker_path(staging).write_text(
                json.dumps({**marker, **(extra or {})}, indent=2), encoding="utf-8"
            )
            # Never pass through "no install": the old root is renamed aside, not deleted,
            # until the new one is in place (the rename can fail on Windows, e.g. antivirus).
            old = root.with_name(root.name + ".old")
            shutil.rmtree(old, ignore_errors=True)
            had_old = root.exists()
            if had_old:
                root.replace(old)
            try:
                staging.replace(root)
            except BaseException:
                if had_old:
                    try:
                        old.replace(root)
                    except OSError:
                        logger.error("Could not restore %s from %s", root, old)
                raise
            if had_old:
                shutil.rmtree(old, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
