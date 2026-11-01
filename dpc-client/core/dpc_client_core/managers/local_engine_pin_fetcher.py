"""
Local Engine Pin Fetcher — the generic half of a pinned download under `~/.dpc/bin/`.

`llama_server_fetcher.py` (llama.cpp) and, next, `strata_fetcher.py` (Strata)
install a pinned release the same way: stream each asset into `<root>.download`,
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
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))

_MARKER_LOCK = threading.Lock()


def marker_path(root: Path) -> Path:
    return root / ".dpc-pin.json"


def read_marker(root: Path) -> Optional[Dict[str, Any]]:
    """The marker as a dict; None when missing, unreadable, or not a JSON object."""
    try:
        pinned = json.loads(marker_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return pinned if isinstance(pinned, dict) else None


def refusal_text(
    download_name: str,
    alias: str,
    size_bytes: Optional[int],
    reason: str,
    *,
    percent: Optional[float] = None,
    at_most: bool = False,
) -> str:
    """The sentence a start returns instead of fetching a missing pin, after the
    pattern of `camoufox_binary.refusal_text`. `reason` is `asked`, `downloading`,
    `declined` or `peer`; `at_most` when `size_bytes` is an upper bound. It names
    the app, never a dialog: it reaches Telegram and peers too."""
    size = (f"{'up to' if at_most else 'about'} {size_bytes / 1e9:.1f} GB"
            if size_bytes else "unknown size")
    head = (
        f"⚠️ The {download_name} engine is not downloaded on this node, so "
        f"'{alias}' was not started for this call."
    )
    if reason == "downloading":
        done = f"{percent:.0f}% of {size}" if percent is not None else size
        tail = f" It is being downloaded now ({done}); try again after it finishes."
    elif reason == "declined":
        tail = (
            f" It is a one-time download of {size}, which the owner has declined; "
            f"it can be started from the D-PC Messenger app."
        )
    elif reason == "peer":
        tail = (
            f" It is a one-time download of {size}, and a peer's request does not "
            f"fetch it."
        )
    else:
        tail = (
            f" It is a one-time download of {size}; the owner has been asked to "
            f"confirm it in the D-PC Messenger app. Do not retry until it has finished."
        )
    return head + tail


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
) -> None:
    """Download `assets` from `url_base`, extract into `<root>.staging`, swap it into `root`.

    Archives land in `<root>.download`, beside the root on its volume (never the
    system temp folder), and each is deleted once extracted. `check(staging)` runs
    on the extracted tree and may raise to abort; the dict it returns is merged
    into the marker. Nothing is left behind on a failure, and an install already
    at `root` survives any failure before or during the swap.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    downloads = root.with_name(root.name + ".download")
    staging = root.with_name(root.name + ".staging")
    shutil.rmtree(downloads, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    downloads.mkdir(parents=True)
    staging.mkdir(parents=True)
    # Progress is one bar across all assets: bytes landed so far against their sum.
    total = sum(int(a["size"]) for a in assets)
    done = 0
    try:
        for asset in assets:
            archive = downloads / asset["name"]
            logger.info("Fetching %s (%d bytes)", asset["name"], asset["size"])
            landed = [0]

            def _report(got: int, _size: int, base: int = done) -> None:
                landed[0] = got
                progress(base + got, total)

            download_asset(
                f"{url_base}/{asset['name']}", asset, archive, _report if progress else None
            )
            done += landed[0]
            extract_archive(archive, staging)
            archive.unlink()
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
        shutil.rmtree(downloads, ignore_errors=True)


# --- installed pins: the retained one and the rest ---------------------------------------
# Each engine keeps `<pins_dir>/<tag>/<platform>/`. A folder is a pin only when its marker
# names that same tag and platform; anything else there (a hand-made build, a folder from
# before markers, a `.staging`, `.download` or `.old` leftover) is not DPC's to run or to remove.


def installed_pins(pins_dir: Path) -> List[Dict[str, Any]]:
    """Every pin under `pins_dir`: `{"root", "tag", "platform", "marker"}`, sorted by root.

    The platform folder is read from disk, never built: its name changes between
    builds (win-cuda-13.3-x64, then 13.4).
    """
    pins: List[Dict[str, Any]] = []
    try:
        tag_dirs = sorted(p for p in Path(pins_dir).iterdir() if p.is_dir())
    except OSError:
        return pins
    for tag_dir in tag_dirs:
        try:
            plat_dirs = sorted(p for p in tag_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for root in plat_dirs:
            marker = read_marker(root)
            if marker and marker.get("tag") == tag_dir.name and marker.get("platform") == root.name:
                pins.append({"root": root, "tag": tag_dir.name, "platform": root.name, "marker": marker})
    return pins


def _start_passed_at(marker: Dict[str, Any]) -> Optional[datetime]:
    value = marker.get("start_passed_at")
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def retained_pin(pins_dir: Path, current_tag: str) -> Optional[Dict[str, Any]]:
    """The other pin that last passed a start (newest `start_passed_at`); None when none did."""
    passed = [
        (when, pin) for pin in installed_pins(pins_dir)
        if pin["tag"] != current_tag and (when := _start_passed_at(pin["marker"])) is not None
    ]
    return max(passed, key=lambda item: item[0])[1] if passed else None


def note_start_passed(root: Path) -> bool:
    """Record in `root`'s marker that a start passed; True only the first time.

    The marker is rewritten whole through a temporary file and `os.replace`, so a
    crash leaves the old marker or the new one, never half of one.
    """
    root = Path(root)
    with _MARKER_LOCK:
        marker = read_marker(root)
        if marker is None or "start_passed_at" in marker:
            return False
        marker["start_passed_at"] = datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(prefix=".dpc-pin-", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(json.dumps(marker, indent=2))
            os.replace(tmp, marker_path(root))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True


def _same_path(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def prune_pins(pins_dir: Path, keep: Iterable[Path], in_use: Iterable[Path]) -> List[Dict[str, Any]]:
    """Remove every installed pin whose root is in neither `keep` nor `in_use`; return those removed.

    Each removal renames `<root>` to `<root>.old` first and deletes that: Windows
    refuses the rename while an exe inside runs, and such a pin is skipped. Only
    `installed_pins` are candidates, so a folder without a matching marker is never
    touched, nor anything that resolves outside `pins_dir`.
    """
    pins_dir = Path(pins_dir)
    spared = {_same_path(p) for p in (*keep, *in_use)}
    inside = pins_dir.resolve()
    removed: List[Dict[str, Any]] = []
    for pin in installed_pins(pins_dir):
        root = pin["root"]
        if _same_path(root) in spared or root.is_symlink() or not root.resolve().is_relative_to(inside):
            continue
        size = _tree_bytes(root)
        old = root.with_name(root.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        try:
            root.replace(old)
        except OSError as exc:
            logger.info("Kept pin %s: it could not be moved aside (%s); probably still running", root, exc)
            continue
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            logger.warning("Pin %s removed, but some of %s could not be deleted", root, old)
        logger.info("Removed pin %s %s at %s, freeing %d bytes", pin["tag"], pin["platform"], root, size)
        removed.append(pin)
        tag_dir = root.parent
        try:
            if not any(tag_dir.iterdir()):
                tag_dir.rmdir()
        except OSError:
            pass
    return removed
