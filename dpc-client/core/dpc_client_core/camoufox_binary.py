# dpc_client_core/camoufox_binary.py
"""The Camoufox browser binary as a download the owner consents to (2026-10-21).

`uv sync --extra browser` installs the camoufox Python package and no browser.
Left to itself, the package fetches the browser on the first `Camoufox(...)`
(`pkgman.camoufox_path(download_if_missing=True)`): the newest GitHub release,
inside an agent's tool call, with a tqdm bar on stdout only. This module is the
other half of that bargain: browser.py asks `missing_reason` before every
launch and refuses while the binary is absent, and `ModelDownloadService`
offers and runs the download through the same `model_download_*` consent flow
Whisper and the embedding model use.

What is downloaded is the pinned release, `PINNED_RELEASE_TAG` (Mike's call,
2026-10-21), not the package's newest. An installed binary of another version
is left alone while the package supports it: the download is offered only when
the binary is missing or outside the package's supported range.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# The key this download is known by in the `model_download_*` events, the
# `[model_downloads]` section of config.ini and the UI's status map — the
# package's own name for its browser (`camoufox fetch`, `camoufox path`).
DOWNLOAD_NAME = "camoufox"

# The one release this node downloads. The build the Windows node has run
# since 2026-07 (its version.json reads 152.0.4 / beta.28); GitHub does not
# mark it a prerelease. Mike's call, 2026-10-21.
PINNED_RELEASE_TAG = "v152.0.4-beta.28"

# Zip sizes GitHub lists for the pinned tag, read from
# /repos/daijro/camoufox/releases/tags/v152.0.4-beta.28 on 2026-10-21, keyed
# `<pkgman.OS_NAME>.<arch>` as in the asset names. Lets the consent dialog name
# the size without a network read; a pin with no row here is looked up live.
_PINNED_ASSET_SIZES: Dict[str, Dict[str, int]] = {
    "v152.0.4-beta.28": {
        "lin.arm64": 653_814_950,
        "lin.x86_64": 663_387_175,
        "mac.arm64": 312_626_286,
        "mac.x86_64": 319_866_512,
        "win.i686": 478_100_137,
        "win.x86_64": 492_370_020,
    },
}

# SHA-256 GitHub lists for each asset of the pinned tag (the releases API's
# `digest` field), read on 2026-10-21. The download is refused unless it
# matches; a pin with no row here falls back to the digest the API returns.
_PINNED_ASSET_SHA256: Dict[str, Dict[str, str]] = {
    "v152.0.4-beta.28": {
        "lin.arm64": "sha256:3a105a2fc929e80a79b4b7fce2c93ed62c4fb2c877f3c1ed2a5d66a1c4fe968f",
        "lin.x86_64": "sha256:924f3109ccd6d47cd6a0384d67a345fadf975d48b6319f8dbbd5954c588982bd",
        "mac.arm64": "sha256:8b7680a61818245cf4eb0150edab811d4ed937b6723514569e74c3e7df9685bd",
        "mac.x86_64": "sha256:6e0efd66f6db46bece072827cb2c5a4851bca822ff4fed95b8a0c2fb6f61a3d4",
        "win.i686": "sha256:a713aca14f4ef0429eab502bacf42548fe6202bb4e29da1ca64761ed08d409af",
        "win.x86_64": "sha256:386fc2f41139685f9a1a9cef0d024bc041d899c315ea538d561171b5b282e57d",
    },
}

# The uBlock Origin add-on the package puts into every browser (its
# DefaultAddons.UBO), which the package itself fetches as AMO's `latest`.
# Pinned to the build the Windows node has run since 2026-07-21 (its
# addons/UBO/manifest.json); AMO's latest was 1.75.0 on 2026-10-21. URL, size
# and hash from addons.mozilla.org/api/v5/addons/addon/ublock-origin/versions/
# 1.72.2/, read 2026-10-21. Installed into the browser's addons/UBO folder,
# where the package looks first and then fetches nothing.
UBLOCK_ORIGIN_VERSION = "1.72.2"
_UBLOCK_ORIGIN_FILES: Dict[str, Dict[str, object]] = {
    "1.72.2": {
        "url": "https://addons.mozilla.org/firefox/downloads/file/4888680/ublock_origin-1.72.2.xpi",
        "size": 4_585_322,
        "sha256": "sha256:40c315b0da7871868155ecfae7a50a58dfa0920aebd865e008214986f1b7c578",
    },
}

PURPOSE = "Rendering web pages for agents (browse_page and the browser_* tools)"
CONSEQUENCE_IF_DECLINED = (
    "Agents cannot open a browser: pages that need JavaScript, sign-ins and "
    "browser_* tools stay unavailable; plain HTML pages still load."
)

_install_lock = threading.Lock()
_live_size: Optional[int] = None
# A failed lookup is not retried for this long: the size is asked on every
# refused browser call, and offline each attempt waits out the 20 s timeout.
_OFFER_RETRY_SECONDS = 600
_offer_failed_at: Optional[float] = None


class CamoufoxBinaryMissing(RuntimeError):
    """Raised instead of launching when the browser binary is not on disk.

    A RuntimeError so that every handler already written for a failed launch
    still catches it; the message is the sentence the agent is meant to read.
    """


def _pinned_version_release() -> Tuple[str, str]:
    """('152.0.4', 'beta.28') from 'v152.0.4-beta.28'."""
    version, _, release = PINNED_RELEASE_TAG.lstrip("v").partition("-")
    return version, release


def is_downloading() -> bool:
    return _install_lock.locked()


def _probe() -> Optional[str]:
    """None when a binary is installed that the package supports — the pinned
    release or any other inside the package's range; otherwise
    `package_missing`, `outdated` or `missing`.

    Asks with `download_if_missing=False`, so this never fetches anything.
    """
    try:
        from camoufox import pkgman
    except ImportError:
        return "package_missing"
    except Exception:  # UnsupportedOS is raised at import time
        logger.debug("camoufox.pkgman could not be imported", exc_info=True)
        return "package_missing"
    try:
        pkgman.camoufox_path(download_if_missing=False)
        # Safe only after the line above returned: launch_path() calls
        # camoufox_path() with the package's default, which downloads.
        pkgman.launch_path()
    except pkgman.UnsupportedVersion:
        return "outdated"
    except Exception:
        # FileNotFoundError (no directory, or no version.json in a half-
        # extracted one) and CamoufoxNotInstalled (no executable).
        return "missing"
    return None


def missing_reason() -> Optional[str]:
    """What stops a launch now: None, `downloading`, `package_missing`,
    `outdated` or `missing`."""
    if is_downloading():
        return "downloading"
    return _probe()


def is_present() -> bool:
    return missing_reason() is None


def location() -> str:
    """Where the package keeps the binary (platformdirs' user cache dir)."""
    try:
        from camoufox import pkgman
        return str(pkgman.INSTALL_DIR)
    except Exception:
        return ""


def _platform_key() -> Optional[str]:
    try:
        from camoufox import pkgman
        return f"{pkgman.OS_NAME}.{pkgman.CamoufoxFetcher.get_platform_arch()}"
    except Exception:
        return None


def _fetcher_class():
    """A CamoufoxFetcher that finds `PINNED_RELEASE_TAG` rather than the
    newest release and remembers the size and digest GitHub lists for the
    asset it chose. Only its lookup is used: the download, the check and the
    install are this module's (`_install_pinned`). Built on demand: the
    package may be absent."""
    import requests
    from camoufox import pkgman

    class _PinnedFetcher(pkgman.CamoufoxFetcher):
        asset_size: Optional[int] = None
        asset_digest: Optional[str] = None

        def get_asset(self):
            # The package reads page 1 of /releases and takes the first match;
            # the pinned tag is read by name, so its age does not matter.
            resp = requests.get(f"{self.api_url}/tags/{PINNED_RELEASE_TAG}", timeout=20)
            resp.raise_for_status()
            want = _pinned_version_release()
            for asset in resp.json().get("assets", []):
                found = self.check_asset(asset)
                if found and (found[0].version, found[0].release) == want:
                    self.asset_size = asset.get("size")
                    self.asset_digest = asset.get("digest")
                    return found
            raise pkgman.MissingRelease(
                f"Release {PINNED_RELEASE_TAG} has no asset for {pkgman.OS_NAME} "
                f"{self.arch} inside the package's supported range "
                f"({pkgman.CONSTRAINTS.as_range()})."
            )

    return _PinnedFetcher


def _zip_size() -> Optional[int]:
    """The pinned release's zip for this platform: the table row, else one
    GitHub API read of the pinned tag (no binary is downloaded), else None."""
    global _live_size, _offer_failed_at
    key = _platform_key()
    row = _PINNED_ASSET_SIZES.get(PINNED_RELEASE_TAG, {})
    if key in row:
        return row[key]
    if _live_size is not None:
        return _live_size
    if _offer_failed_at is not None and time.monotonic() - _offer_failed_at < _OFFER_RETRY_SECONDS:
        return None
    try:
        _live_size = _fetcher_class()().asset_size
    except Exception as e:
        _offer_failed_at = time.monotonic()
        logger.info("Camoufox release lookup for %s failed: %s", PINNED_RELEASE_TAG, e)
    return _live_size


def size_bytes() -> Optional[int]:
    """Everything one consent downloads: the browser zip and the pinned
    uBlock Origin add-on."""
    size = _zip_size()
    return size + int(_ublock_file()["size"]) if size else None


def pin_supported() -> bool:
    """Whether the installed camoufox package accepts `PINNED_RELEASE_TAG`.
    False after a package upgrade that moved its range past the pin: then a
    download would be refused on the staged copy, so none is offered."""
    try:
        from camoufox import pkgman
    except Exception:
        return True  # no package: the import that follows says so itself
    version, release = _pinned_version_release()
    return pkgman.Version(release=release, version=version).is_supported()


def size_text() -> str:
    size = size_bytes()
    return f"about {size / 1e9:.1f} GB" if size else "unknown size"


def log_versions() -> None:
    """One INFO line at backend start: what is installed, what is pinned, and
    the range the package supports — so a package upgrade that leaves the pin
    outside its range shows here, not first as a tool refusal."""
    try:
        from camoufox import pkgman
    except Exception:
        logger.info("Camoufox browser: camoufox package not installed (pinned %s)", PINNED_RELEASE_TAG)
        return
    try:
        installed = pkgman.installed_verstr()
    except Exception:
        installed = "not installed"
    version, release = _pinned_version_release()
    pin_supported = pkgman.Version(release=release, version=version).is_supported()
    logger.info(
        "Camoufox browser: installed %s, pinned %s, package supports %s%s",
        installed, PINNED_RELEASE_TAG, pkgman.CONSTRAINTS.as_range(),
        "" if pin_supported else " — the pinned release is OUTSIDE that range",
    )


# --- the install: fetched, checked and extracted beside the old one ----------


def _ublock_file() -> Dict[str, object]:
    return _UBLOCK_ORIGIN_FILES[UBLOCK_ORIGIN_VERSION]


def _resources(root: Path) -> Path:
    """Where the package's get_path() resolves inside an install root."""
    from camoufox import pkgman
    if pkgman.OS_NAME == "mac":
        return root / "Camoufox.app" / "Contents" / "Resources"
    return root


_logged_launch_without_ublock = False


def addon_launch_kwargs(install_dir: Optional[Path] = None) -> Dict[str, object]:
    """The add-on half of the launch arguments, decided here rather than by
    the package: with `addons/UBO` absent the package's launch fetches AMO's
    latest inside the tool call, and when that fails it leaves an empty
    folder that every later launch rejects (`InvalidAddonPath`). So a folder
    without its manifest is removed and the launch excludes the add-on; the
    only fetch of it is our install."""
    global _logged_launch_without_ublock
    try:
        from camoufox import pkgman
        from camoufox.addons import DefaultAddons
    except Exception:
        return {}
    folder = _resources(Path(install_dir or pkgman.INSTALL_DIR)) / "addons" / "UBO"
    if (folder / "manifest.json").is_file():
        return {}
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
        logger.warning("Camoufox: removed the add-on folder %s, which held no manifest", folder)
    if not _logged_launch_without_ublock:
        logger.info("Camoufox: launching without uBlock Origin: not installed (%s)", folder)
        _logged_launch_without_ublock = True
    return {"exclude_addons": [DefaultAddons.UBO]}


def _fetch_verified(
    url: str,
    dest: Path,
    *,
    label: str,
    expected_size: Optional[int],
    expected_sha256: str,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> None:
    """Stream `url` into `dest` and refuse it unless its SHA-256 (and size,
    where known) match. INFO line at every 10%; `on_bytes(n)` per chunk."""
    import requests

    logger.info("%s download started: %s", label, url)
    # The package's own webdl passes no timeout at all.
    with requests.get(url, stream=True, timeout=(20, 120)) as response, open(dest, "wb") as f:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0)) or expected_size or 0
        digest = hashlib.sha256()
        done = 0
        next_mark = 10
        for chunk in response.iter_content(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
            if total and done * 100 >= next_mark * total:
                # One line for the highest mark this chunk crossed.
                while next_mark <= 100 and done * 100 >= next_mark * total:
                    next_mark += 10
                logger.info("%s download: %d%% (%d of %d bytes)", label, next_mark - 10, done, total)
    if expected_size is not None and done != expected_size:
        raise ValueError(f"{label}: received {done} bytes, expected {expected_size}")
    got = "sha256:" + digest.hexdigest()
    if got != expected_sha256:
        # The size matched, so the file arrived whole: what changed is the
        # file at the source. A GitHub release is not immutable (the pinned
        # one reads "immutable": false), so an asset can be replaced in place.
        raise ValueError(
            f"{label}: checksum {got} does not match the pinned {expected_sha256}. "
            f"The file published at {url} has most likely been replaced since it was "
            f"pinned (GitHub release assets can be re-uploaded); the pin table needs "
            f"updating. This is not a disk problem."
        )
    logger.info("%s download: %d bytes, %s verified", label, done, got)


def _extract(archive: Path, dest: Path, *, label: str) -> None:
    """Unzip with INFO lines at every 10% of the members — the package's
    own unzip draws a tqdm bar on stdout."""
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        logger.info("%s: extracting %d files to %s", label, len(members), dest)
        next_mark = 10
        for i, member in enumerate(members, 1):
            zf.extract(member, dest)
            if i * 100 >= next_mark * len(members):
                while next_mark <= 100 and i * 100 >= next_mark * len(members):
                    next_mark += 10
                logger.info("%s: extracted %d%%", label, next_mark - 10)


def _install_pinned(fetcher, on_progress: Optional[Callable[[int, Optional[int]], None]]) -> None:
    """Download, verify and extract into a staging folder beside the install
    folder, then swap the two; the old install is restored on any failure.

    The package's `install()` deletes the old browser before fetching the new
    one, so a failed download left no browser at all. Every step that can fail
    here runs before the swap; the swap is two renames on one volume. Between
    those two renames the target folder does not exist, and another process
    calling `camoufox_path(download_if_missing=True)` in that instant would make
    the package install its newest release there; every launch path of ours is
    gated on `missing_reason`, and a manual `camoufox fetch` is not supported.

    The browser is fail-closed: no checksum, no install. The uBlock Origin
    add-on is not: if it cannot be fetched or checked, the browser is installed
    without `addons/UBO`, and the package fetches its own copy at first launch
    (`addons.maybe_download_addons` does so whenever that folder is missing).
    """
    from camoufox import pkgman

    install_dir = Path(pkgman.INSTALL_DIR)
    parent = install_dir.parent
    staging = parent / (install_dir.name + ".staging")
    previous = parent / (install_dir.name + ".previous")
    zip_part = parent / (install_dir.name + ".download.zip")
    xpi_part = parent / (install_dir.name + ".ublock.xpi")
    parent.mkdir(parents=True, exist_ok=True)

    # A swap interrupted between its two renames left the old browser here.
    if previous.exists() and not install_dir.exists():
        logger.warning("Camoufox browser: restoring %s left by an interrupted install", previous)
        os.replace(previous, install_dir)
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
            if leftover.exists():
                raise OSError(
                    f"Camoufox browser: cannot clear {leftover} left by an earlier install — "
                    f"most likely a browser started from it is still running, or an "
                    f"antivirus holds a file in it. Close it and try again; the installed "
                    f"browser was not touched."
                )

    key = _platform_key()
    expected_sha = _PINNED_ASSET_SHA256.get(PINNED_RELEASE_TAG, {}).get(key) or fetcher.asset_digest
    if not expected_sha:
        raise ValueError(f"no SHA-256 known for {PINNED_RELEASE_TAG} {key}; not installing an unchecked browser")
    expected_size = _PINNED_ASSET_SIZES.get(PINNED_RELEASE_TAG, {}).get(key) or fetcher.asset_size
    ublock = _ublock_file()
    ublock_size = int(ublock["size"])
    # Unknown zip size: no total at all, rather than the add-on's alone.
    total = [expected_size + ublock_size if expected_size else None]
    done = [0]

    def _bytes(n: int) -> None:
        done[0] += n
        if on_progress is not None:
            on_progress(done[0], total[0])

    try:
        _fetch_verified(fetcher.url, zip_part, label="Camoufox browser",
                        expected_size=expected_size, expected_sha256=expected_sha, on_bytes=_bytes)
        staging.mkdir(exist_ok=True)
        if any(staging.iterdir()):
            raise OSError(f"Camoufox browser: staging folder {staging} is not empty; the installed browser was not touched.")
        _extract(zip_part, staging, label="Camoufox browser")

        ublock_label = f"uBlock Origin {UBLOCK_ORIGIN_VERSION}"
        addon_dir = _resources(staging) / "addons" / "UBO"
        ublock_installed = False
        try:
            _fetch_verified(str(ublock["url"]), xpi_part, label=ublock_label,
                            expected_size=ublock_size, expected_sha256=str(ublock["sha256"]),
                            on_bytes=_bytes)
            _extract(xpi_part, addon_dir, label=ublock_label)
            ublock_installed = True
        except Exception as e:
            # An empty addons/UBO would be taken for an installed add-on.
            shutil.rmtree(addon_dir, ignore_errors=True)
            logger.warning(
                "%s not installed (%s); the browser is installed without it and the "
                "camoufox package fetches its own copy at first launch", ublock_label, e,
            )
            if on_progress is not None:
                # The bar ends where the download ended.
                total[0] = done[0]
                on_progress(done[0], done[0])
        version, release = _pinned_version_release()
        (staging / "version.json").write_text(json.dumps({"version": version, "release": release}))
        if pkgman.OS_NAME != "win":
            for root, dirs, files in os.walk(staging):
                for name in dirs + files:
                    os.chmod(os.path.join(root, name), 0o755)
            os.chmod(staging, 0o755)

        # Checked on the staged copy, before anything is replaced.
        staged = pkgman.Version.from_path(staging)
        if (staged.version, staged.release) != (version, release) or not staged.is_supported():
            raise ValueError(f"staged Camoufox reads {staged.full_string}, not a supported {PINNED_RELEASE_TAG}")
        launch = os.path.normpath(_resources(staging) / pkgman.LAUNCH_FILE[pkgman.OS_NAME])
        if not os.path.exists(launch):
            raise ValueError(f"staged Camoufox has no executable at {launch}")

        had_old = install_dir.exists()
        if had_old:
            # On Windows this fails while a Camoufox from the old folder runs;
            # nothing has been replaced yet, and the error says why.
            os.replace(install_dir, previous)
        try:
            os.replace(staging, install_dir)
        except Exception:
            if had_old:
                os.replace(previous, install_dir)
            raise
        if had_old:
            shutil.rmtree(previous, ignore_errors=True)
            if previous.exists():
                logger.warning("Camoufox browser: could not remove the old install at %s", previous)
        logger.info("Camoufox browser installed: %s-%s %s uBlock Origin %s in %s",
                    version, release, "with" if ublock_installed else "without",
                    UBLOCK_ORIGIN_VERSION, install_dir)
    finally:
        for part in (zip_part, xpi_part):
            try:
                part.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Camoufox browser: could not remove %s", part)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def download(on_progress: Optional[Callable[[int, Optional[int]], None]] = None) -> str:
    """Download and install the pinned release, once; returns its version.

    Blocking — run it in a thread. `on_progress(bytes_done, total_bytes)` is
    called from that thread for every chunk; throttling is the caller's. A
    second call while one runs raises instead of starting a second download,
    since both would stage into the same folder.
    """
    if not _install_lock.acquire(blocking=False):
        raise RuntimeError("the Camoufox browser is already being downloaded")
    try:
        from camoufox import pkgman

        fetcher = _fetcher_class()()
        logger.info(
            "Camoufox browser download: release %s, %s bytes, uBlock Origin %s, into %s",
            fetcher.verstr, fetcher.asset_size, UBLOCK_ORIGIN_VERSION, pkgman.INSTALL_DIR,
        )
        _install_pinned(fetcher, on_progress)
        logger.info("Camoufox browser download completed: %s", fetcher.verstr)
        return fetcher.verstr
    finally:
        _install_lock.release()


def refusal_text(reason: str, *, asked: bool, declined: bool) -> str:
    """The sentence a browser tool returns instead of launching."""
    size = size_text()
    if reason != "downloading" and not pin_supported():
        return (
            f"⚠️ The Camoufox browser this app pins ({PINNED_RELEASE_TAG}) is outside "
            f"the range the installed camoufox package supports, so no browser was "
            f"opened and no download is offered: D-PC Messenger needs an update."
        )
    if reason == "outdated":
        head = (
            "⚠️ The Camoufox browser on this node is outside the range the installed "
            "camoufox package supports, so no browser was opened for this call."
        )
    else:
        head = (
            "⚠️ The Camoufox browser is not downloaded on this node, so no "
            "browser was opened for this call."
        )
    if reason == "downloading":
        tail = (
            f" It is being downloaded now ({size}, once); try again after it "
            f"finishes."
        )
    elif declined:
        tail = (
            f" It is a one-time download of {size}, which the owner has "
            f"declined; it can be started from the D-PC Messenger app."
        )
    elif asked:
        tail = (
            f" It is a one-time download of {size}; the owner has been asked "
            f"to confirm it in the D-PC Messenger app. Do not retry until it "
            f"has finished."
        )
    else:
        tail = (
            f" It is a one-time download of {size}, which the owner starts from "
            f"the D-PC Messenger app."
        )
    return head + tail
