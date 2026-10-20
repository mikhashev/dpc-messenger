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

import logging
import threading
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

PURPOSE = "Rendering web pages for agents (browse_page and the browser_* tools)"
CONSEQUENCE_IF_DECLINED = (
    "Agents cannot open a browser: pages that need JavaScript, sign-ins and "
    "browser_* tools stay unavailable; plain HTML pages still load."
)

_install_lock = threading.Lock()
_live_size: Optional[int] = None


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
    """A CamoufoxFetcher that installs `PINNED_RELEASE_TAG` rather than the
    newest release, reports into dpc-client.log rather than a tqdm bar on
    stdout, and remembers the size GitHub lists for the asset it chose. Built
    on demand: the package may be absent."""
    import requests
    from camoufox import pkgman

    class _PinnedFetcher(pkgman.CamoufoxFetcher):
        asset_size: Optional[int] = None
        on_progress: Optional[Callable[[int, Optional[int]], None]] = None

        def get_asset(self):
            # The package reads page 1 of /releases and takes the first match;
            # the pinned tag is read by name, so its age does not matter.
            resp = requests.get(f"{self.api_url}/tags/{PINNED_RELEASE_TAG}", timeout=20)
            resp.raise_for_status()
            want = _pinned_version_release()
            for asset in resp.json().get("assets", []):
                found = self.check_asset(asset)
                if found and (found[0].version, found[0].release) == want:
                    return found
            raise pkgman.MissingRelease(
                f"Release {PINNED_RELEASE_TAG} has no asset for {pkgman.OS_NAME} "
                f"{self.arch} inside the package's supported range "
                f"({pkgman.CONSTRAINTS.as_range()})."
            )

        def check_asset(self, asset):
            found = super().check_asset(asset)
            if found:
                self.asset_size = asset.get("size")
            return found

        def download_file(self, file, url):
            logger.info("Camoufox browser download started: %s", url)
            # The package's own webdl passes no timeout at all.
            response = requests.get(url, stream=True, timeout=(20, 120))
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0)) or self.asset_size or 0
            done = 0
            next_mark = 10
            for chunk in response.iter_content(1 << 20):
                file.write(chunk)
                done += len(chunk)
                if self.on_progress is not None:
                    self.on_progress(done, total or None)
                if total and done * 100 >= next_mark * total:
                    logger.info(
                        "Camoufox browser download: %d%% (%d of %d bytes)",
                        next_mark, done, total,
                    )
                    next_mark += 10
            file.seek(0)
            return file

        def extract_zip(self, zip_file):
            logger.info("Camoufox browser download: extracting to %s", pkgman.INSTALL_DIR)
            super().extract_zip(zip_file)

    return _PinnedFetcher


def size_bytes() -> Optional[int]:
    """The size of the pinned release's zip for this platform: the table row,
    else one GitHub API read of the pinned tag (no binary is downloaded),
    else None."""
    global _live_size
    key = _platform_key()
    row = _PINNED_ASSET_SIZES.get(PINNED_RELEASE_TAG, {})
    if key in row:
        return row[key]
    if _live_size is not None:
        return _live_size
    try:
        _live_size = _fetcher_class()().asset_size
    except Exception as e:
        logger.info("Camoufox release lookup for %s failed: %s", PINNED_RELEASE_TAG, e)
    return _live_size


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


def download(on_progress: Optional[Callable[[int, Optional[int]], None]] = None) -> str:
    """Download and install the pinned release, once; returns its version.

    Blocking — run it in a thread. `on_progress(bytes_done, total_bytes)` is
    called from that thread for every chunk; throttling is the caller's. A
    second call while one runs raises instead of starting a second download:
    the package's `install()` begins by deleting the install directory, so two
    at once would destroy each other's files.
    """
    if not _install_lock.acquire(blocking=False):
        raise RuntimeError("the Camoufox browser is already being downloaded")
    try:
        from camoufox import pkgman

        fetcher = _fetcher_class()()
        fetcher.on_progress = on_progress
        logger.info(
            "Camoufox browser download: release %s, %s bytes, into %s",
            fetcher.verstr, fetcher.asset_size, pkgman.INSTALL_DIR,
        )
        fetcher.install()
        # What `camoufox fetch` also does; otherwise the first launch fetches
        # uBlock Origin from addons.mozilla.org inside a tool call.
        try:
            from camoufox.addons import DefaultAddons, maybe_download_addons
            maybe_download_addons(list(DefaultAddons))
        except Exception as e:
            logger.warning("Camoufox default add-on download failed: %s", e)
        logger.info("Camoufox browser download completed: %s", fetcher.verstr)
        return fetcher.verstr
    finally:
        _install_lock.release()


def refusal_text(reason: str, *, asked: bool, declined: bool) -> str:
    """The sentence a browser tool returns instead of launching."""
    size = size_text()
    if reason == "outdated":
        head = (
            "⚠️ The Camoufox browser on this node is older than the installed "
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
