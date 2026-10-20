# dpc_client_core/camoufox_binary.py
"""The Camoufox browser binary as a download the owner consents to (2026-10-21).

`uv sync --extra browser` installs the camoufox Python package and no browser.
Left to itself, the package fetches the browser on the first `Camoufox(...)`
(`pkgman.camoufox_path(download_if_missing=True)`): about 1.3 GB of the newest
GitHub release, with a tqdm bar on stdout only, inside an agent's tool call.
This module is the other half of that bargain: browser.py asks `missing_reason`
before every launch and refuses while the binary is absent, and
`ModelDownloadService` offers and runs the download through the same
`model_download_*` consent flow Whisper and the embedding model use.

Which release is fetched is still the package's choice (`CamoufoxFetcher`: the
first asset of the GitHub releases list inside the package's supported range);
nothing here pins one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# The key this download is known by in the `model_download_*` events, the
# `[model_downloads]` section of config.ini and the UI's status map — the
# package's own name for its browser (`camoufox fetch`, `camoufox path`).
DOWNLOAD_NAME = "camoufox"

# Used only when the release lookup has not answered: the zip sizes GitHub
# lists for v158.0-beta.40 (2026-10-19) run from 1 292 898 414 bytes
# (lin.arm64) to 1 315 506 159 (win.x86_64), read from the releases API on
# 2026-10-21. The 152.x builds were 0.3-0.66 GB, so this is a fact about
# today's release and not about Camoufox.
FALLBACK_SIZE_TEXT = "about 1.3 GB"

PURPOSE = "Rendering web pages for agents (browse_page and the browser_* tools)"
CONSEQUENCE_IF_DECLINED = (
    "Agents cannot open a browser: pages that need JavaScript, sign-ins and "
    "browser_* tools stay unavailable; plain HTML pages still load."
)

_install_lock = threading.Lock()
_offer_cache: Optional[Tuple[int, str]] = None
# A failed lookup is not repeated for this long: the offer is built on every
# refused browser call, and offline each attempt waits out the 20 s timeout.
_OFFER_RETRY_SECONDS = 600
_offer_failed_at: Optional[float] = None


class CamoufoxBinaryMissing(RuntimeError):
    """Raised instead of launching when the browser binary is not on disk.

    A RuntimeError so that every handler already written for a failed launch
    still catches it; the message is the sentence the agent is meant to read.
    """


def is_downloading() -> bool:
    return _install_lock.locked()


def _probe() -> Optional[str]:
    """None when the binary is installed and the package supports its
    version; otherwise `package_missing`, `outdated` or `missing`.

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


def _fetcher_class():
    """A CamoufoxFetcher that reports into dpc-client.log rather than a tqdm
    bar on stdout, and remembers the size GitHub lists for the asset it
    chose. Built on demand: the package may be absent."""
    from camoufox import pkgman

    class _LoggedFetcher(pkgman.CamoufoxFetcher):
        asset_size: Optional[int] = None

        def check_asset(self, asset):
            found = super().check_asset(asset)
            if found:
                self.asset_size = asset.get("size")
            return found

        def download_file(self, file, url):
            import requests

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

    return _LoggedFetcher


def release_offer() -> Tuple[Optional[int], Optional[str]]:
    """`(size_bytes, version)` of the release the package would install now,
    from one GitHub API read (no binary is downloaded), or `(None, None)`
    offline. A success is kept for the process."""
    global _offer_cache, _offer_failed_at
    if _offer_cache is not None:
        return _offer_cache
    if _offer_failed_at is not None and time.monotonic() - _offer_failed_at < _OFFER_RETRY_SECONDS:
        return None, None
    try:
        fetcher = _fetcher_class()()
        offer = (fetcher.asset_size, fetcher.verstr)
    except Exception as e:
        logger.info("Camoufox release lookup failed: %s", e)
        _offer_failed_at = time.monotonic()
        return None, None
    if offer[0]:
        _offer_cache = offer
    return offer


def size_bytes() -> Optional[int]:
    return release_offer()[0]


def size_text(size: Optional[int] = None) -> str:
    if size is None and _offer_cache is not None:
        size = _offer_cache[0]
    return f"about {size / 1e9:.1f} GB" if size else FALLBACK_SIZE_TEXT


def download() -> str:
    """Download and install the binary, once; returns the installed version.

    Blocking — run it in a thread. A second call while one runs raises
    instead of starting a second download: the package's `install()` begins
    by deleting the install directory, so two at once would destroy each
    other's files.
    """
    if not _install_lock.acquire(blocking=False):
        raise RuntimeError("the Camoufox browser is already being downloaded")
    try:
        from camoufox import pkgman

        fetcher = _fetcher_class()()
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
            f"declined; it can be started from the D-PC Messenger app or with "
            f"`uv run camoufox fetch` in dpc-client/core."
        )
    elif asked:
        tail = (
            f" It is a one-time download of {size}; the owner has been asked "
            f"to confirm it in the D-PC Messenger app. Do not retry until it "
            f"has finished."
        )
    else:
        tail = (
            f" It is a one-time download of {size}: the owner can start it "
            f"with `uv run camoufox fetch` in dpc-client/core."
        )
    return head + tail
