"""
Strata Fetcher — the Strata release DPC is tested against, and DPC's own install of it (ADR-044 point 5, B2).

The pin is two numbers that belong together, on two version lines. `STRATA_RELEASE_TAG`
is the tag of one Strata release, on Strata's app line (0.2.x), published with its
`strata-feed.json`. The assets of that release carry the other line: the **Control**
package (`strata-control-<ver>-<platform>.zip`) and the engine packages are versioned
0.1.x inside it. `STRATA_ENGINE_VERSION` is that engine version (`strata.exe` and
`strata-vision.exe`, launched by the **launcher** `serve/server.py`), the one it reports
at `/metrics` `engine.version`. A person sees both as "Strata v0.2.11 (engine 0.1.57)".

Until now the second number lived on each alias as a hand-raised
`tested_version`. It is in code instead, as `LLAMA_CPP_TAG` is for the llama.cpp
child (`llama_server_fetcher.py`): one place, moved by one reviewed commit, the
same for every alias. The supervisor compares what the engine reports with it
(`StrataSupervisor._check_version`). On an alias with `strata_install_dir` set (the
owner's own checkout) a mismatch only warns, as ADR-044 point 5 says; on DPC's own
install of the pin it is refused, naming what was found against the pin.

To move the pin: update Strata on a node, run DPC's start smoke there live
(version check, `/health`, `/props`, the two forced tool calls), record the
result on the ADR-044 point-5 card, then change both constants in one commit.
Neither is ever read from the alias or from upstream's «latest».

The fetch (B2 step 2) installs the pin under `~/.dpc/bin/strata/<tag>/<platform>/` in
three steps. Control comes as one archive per platform, checked by its sha256 row
below (`local_engine_pin_fetcher`, the same staging swap as llama.cpp). The engine
comes by Strata's own updater run from that Control against the pinned release's
signed feed, into the install's own `components/` folder. Then DPC checks the engine
itself: the manifest's Ed25519 signature by the key held here, its id, version,
platform and build commit, and every file. A set `strata_install_dir` (the owner's
checkout) wins over all of it and is never fetched or replaced.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import platform as _platform
import re
import shutil
import sys
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .local_engine_pin_fetcher import (
    DPC_HOME,
    install_with_staging_swap,
    read_marker,
)

logger = logging.getLogger(__name__)

STRATA_RELEASE_TAG = "v0.2.11"
STRATA_ENGINE_VERSION = "0.1.57"

# The tag's own folder: never releases/latest, which moves with every release.
STRATA_RELEASE_BASE = "https://github.com/Niko1221/Strata/releases/download"

# One Control row per platform, keyed by Strata's platform words. `commit` is the build
# commit every engine of that platform carries in its `build_id` as a whole hex token: at
# the head on Windows, Linux x86_64 and macOS (`0bdcef4682c5+cuda13.0`, on macOS nine hex
# digits, `3e0210663+metal`), at the tail on linux-aarch64
# (`release-0.1.57-cuda13-13.0-aarch64-0bdcef4682c5`). The Linux x86_64 sycl engine spells
# the same commit in full (`0bdcef4682c5371f...d78205-oneapi2026.1.1`).
# A bump fills `name`/`sha256`/`size` from the release's install-manifest.txt (check the
# API digest), `commit` from the build_id of the platform's engines in its strata-feed.json,
# and `installed_bytes` (Control unpacked: its archive's regular-file members) from the
# control entry of that file; on 2026-11-06 the members of all four archives of v0.2.11
# summed to that figure to the byte.
PLATFORM_ASSETS: Dict[str, Dict[str, Any]] = {
    "windows-x86_64": {
        "name": "strata-control-0.1.57-windows-x86_64.zip",
        "sha256": "e93caba92fd69398fa18e4e698c21e798e2e488c2d7ddb77b5272bec6d347d8f",
        "size": 73_411_113,
        "installed_bytes": 229_788_151,
        "commit": "0bdcef4682c5",
    },
    "linux-x86_64": {
        "name": "strata-control-0.1.57-linux-x86_64.tar.gz",
        "sha256": "2ac78cffee4a638b4f32d3020d5a85da8bd1d32a2b12705d8c4fb706b6094d08",
        "size": 99_944_994,
        "installed_bytes": 335_557_257,
        "commit": "0bdcef4682c5",
    },
    "linux-aarch64": {
        "name": "strata-control-0.1.57-linux-aarch64.tar.gz",
        "sha256": "5b4d75b6bc23749fe8140e82db80167d65d17c25a1b42e4f80edd1ee3828fc67",
        "size": 83_864_826,
        "installed_bytes": 273_479_276,
        "commit": "0bdcef4682c5",
    },
    "macos-arm64": {
        "name": "strata-control-0.1.57-macos-arm64.tar.gz",
        "sha256": "e61f9906004cca42108b20752e53362e30791181b587b1ee6d50afcae837004d",
        "size": 51_086_513,
        "installed_bytes": 149_891_272,
        "commit": "3e0210663",
    },
}

# Every engine package of the pinned feed, per platform: (backend, variant, size,
# installed_bytes), the pair strata-feed.json declares per package: download `size` and
# `installed_bytes` (two figures on purpose, not to be folded into one). From the signed
# strata-feed.json of v0.2.11 (-linux-aarch64 for that platform); re-read at every pin
# move. The rows: tests/fixtures/strata_v0.2.11_feed_packages.json, tied by a test.
_ENGINE_PACKAGES: Dict[str, Tuple[Tuple[str, str, int, int], ...]] = {
    "windows-x86_64": (
        ("cuda", "cuda12", 915_324_388, 1_479_343_085),
        ("cuda", "cuda13", 627_812_560, 970_245_089),
        ("hip", "rdna2", 174_926_996, 584_050_448),
        ("hip", "rdna3", 282_636_213, 899_260_647),
        ("hip", "rx9070", 570_803_280, 1_010_309_272),
    ),
    "linux-x86_64": (
        ("cuda", "cuda12", 946_360_848, 1_581_632_104),
        ("cuda", "cuda13", 720_473_300, 1_183_061_759),
        ("hip", "halo", 247_896_848, 1_107_155_781),
        ("hip", "rx6000", 205_633_740, 1_026_389_690),
        ("hip", "rx7900", 329_144_905, 1_424_249_969),
        ("hip", "rx9070", 674_428_112, 2_481_015_812),
        ("sycl", "", 187_499_098, 689_576_353),
    ),
    "linux-aarch64": (
        ("cuda", "cuda13", 659_232_889, 935_801_311),
    ),
    "macos-arm64": (
        ("metal", "", 5_455_515, 15_145_462),
    ),
}


def _largest(rows) -> Tuple[str, str, int, int]:
    return max(rows, key=lambda r: r[2] + r[3])


def _smallest(rows) -> Tuple[str, str, int, int]:
    return min(rows, key=lambda r: r[2] + r[3])


# Strata's updater picks the engine package by the card, so before it runs only the range
# is known. The one package of the platform with the largest size + installed_bytes, both
# figures from it (cuda12 on Windows, hip rx9070 on Linux x86_64, whose download is smaller
# than cuda12's: the sum is what bounds the disk, the download figure is its package's).
ENGINE_SIZE_MAX: Dict[str, int] = {p: _largest(r)[2] for p, r in _ENGINE_PACKAGES.items()}
ENGINE_INSTALLED_BYTES_MAX: Dict[str, int] = {p: _largest(r)[3] for p, r in _ENGINE_PACKAGES.items()}
# The one package with the smallest size + installed_bytes (hip rdna2 on Windows, sycl on Linux x86_64).
ENGINE_SIZE_MIN: Dict[str, int] = {p: _smallest(r)[2] for p, r in _ENGINE_PACKAGES.items()}
ENGINE_INSTALLED_BYTES_MIN: Dict[str, int] = {p: _smallest(r)[3] for p, r in _ENGINE_PACKAGES.items()}

# Strata's BACKEND_MATRIX (strata_control/targets.py of v0.2.11): the families Strata
# commits to per platform. Strata's set, not DPC's; a family is offered only where the
# pinned feed also has a package of it (v0.2.11 has no cpu package anywhere).
_BACKEND_MATRIX: Dict[str, Tuple[str, ...]] = {
    "metal": ("macos-arm64",),
    "cuda": ("windows-x86_64", "linux-x86_64", "linux-aarch64"),
    "hip": ("windows-x86_64", "linux-x86_64"),
    "sycl": ("linux-x86_64",),
    "cpu": ("macos-arm64", "macos-x86_64", "windows-x86_64", "linux-x86_64"),
}

# The ed25519 entry of `keys[]` in Control's strata_control/update_keys.json (NOT
# `app_minisign_public`, which signs the desktop installer). It moves in the same commit
# that moves the pin. Its key id is derived the way Strata's `key_id_of` does.
STRATA_UPDATE_PUBLIC_KEY = "KjnXZB0T1r3dmBNot8h+c4rTicRtllg1LH9fUlTw4ZY="

_ENGINE_MANIFEST = "engine-component.json"
# The updater downloads ~600 MB and hashes ~950 MB; the timeout is the only stop
# besides a backend restart (an agent's kill does not reach a subsystem child).
_UPDATER_TIMEOUT_S = 3600
_UPDATER_EXIT_MEANING = {5: "refused", 6: "failed", 7: "an engine is serving"}
# Strata's own free-space refusal (updater.py `_free_space_check`, exit 6): the exact
# package's download plus its unpacked size, checked before it downloads.
_NO_SPACE = re.compile(r"not enough free space on (?P<drive>.+?): (?P<free>\d+) MiB free, (?P<needed>\d+) MiB needed")
# What to re-read when the pin moves. Each of these reads Strata's own words, which are
# not a contract and can change between releases:
# - the `_NO_SPACE` message above (updater.py `_free_space_check`);
# - setup.py's output, read by `strata_model_fetcher.py`: `_STEP_HEADING` (`step()`),
#   `_FETCH_LINE` and `_CHECKING_LABEL` (the redrawn `label: x / y GB (p%)` lines of
#   `download()`, mtp_fetch.py's `Progress` and `_hash_file`), `_FAILED_LINE` / `_HINT_INDENT`
#   (`fail()`), `_ARGPARSE_ERROR`, the run config name `strata-<id>.json` (`id` of the
#   `model_table` row = `setup.py` `tag.lower()`), and the flags it passes (`--yes --no-start
#   --family --model --vision --data-dir --gguf-dir`, env `STRATA_CONTROL_INSTALL`,
#   `STRATA_INSTALL_DIR`, `STRATA_SETTINGS_FILE`);
# - `strata_models_probe.py`'s use of `Strata(...)`, `hardware()` and the `model_table()`
#   fields in tools/strata_mcp.py (`installed`, `on_this_pc`, `download_gb`, `ram_needed_gb`,
#   ...): Strata's internal API, passed through by `list_strata_models` as they are;
# - what the launcher decides at a start for a start config with no key for it
#   (serve/server.py `engine_args`, `apply_vision_defaults`): the drafter, the reader, the reserve.
_INSTALL_LOCK = threading.Lock()
# The alias of a live Strata launcher running DPC's own install, or None; set by
# strata_supervisor, which owns the launchers. Replacing the install under one is refused.
# One engine per install. Strata's updater no longer asks for it: up to v0.2.9 it pruned
# every engine folder but the new one and the "previous" one; in v0.2.11 it removes none
# (strata_control/updater.py:1217). A start still picks within a family by newest, not by
# build (engine_component.py:1151-1152), and DPC keeps its rule: a different backend replaces
# the install, and only while no launcher runs from it.
_launcher_running: Callable[[], Optional[str]] = lambda: None


def _refuse_if_running(root: Path) -> None:
    alias = _launcher_running()
    if alias is not None and read_marker(root):
        raise RuntimeError(
            f"Strata alias '{alias}' is running from {root}; stop it, then download again: "
            "the install holds one engine and is not replaced under a running launcher"
        )


def _update_key_id() -> str:
    return hashlib.sha256(base64.b64decode(STRATA_UPDATE_PUBLIC_KEY)).digest()[:8].hex()


def platform_tag() -> str:
    """Strata's platform word for this machine; a machine with no Control raises."""
    machine = _platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x86_64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if sys.platform.startswith("linux") and machine in ("aarch64", "arm64"):
        return "linux-aarch64"
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    raise ValueError(
        f"no Strata Control for this machine ({sys.platform} {machine}) in Strata {STRATA_RELEASE_TAG}"
    )


def pins_dir() -> Path:
    """The folder every Strata pin lives in: ~/.dpc/bin/strata/."""
    return DPC_HOME / "bin" / "strata"


def install_root(tag: str = STRATA_RELEASE_TAG, plat: Optional[str] = None) -> Path:
    """Where a fetched pin lives: ~/.dpc/bin/strata/<tag>/<platform>/."""
    return pins_dir() / tag / (plat or platform_tag())


def components_dir(root: Optional[Path] = None) -> Path:
    return (root or install_root()) / "components"


def control_python_path(root: Path) -> Path:
    """Control's own interpreter."""
    if sys.platform == "win32":
        return root / "python" / "python.exe"
    return root / "python" / "bin" / "python3"


def engine_folder(root: Optional[Path] = None) -> Optional[Path]:
    """The engine folder the marker names, when the marker is the one of the folders it
    sits in (`<tag>/<platform>/`, any installed pin, not only the current); no hashing."""
    root = root or install_root()
    pinned = read_marker(root)
    if not pinned or pinned.get("tag") != root.parent.name or pinned.get("platform") != root.name:
        return None
    engine = pinned.get("engine")
    folder = engine.get("folder") if isinstance(engine, dict) else None
    if not isinstance(folder, str) or not folder or Path(folder).name != folder:
        return None
    return components_dir(root) / "engine" / folder


# --- the alias's engine backend (`strata_engine_backend`) -----------------------
# The word Strata's updater takes as `--backend`: a family of Strata's BACKEND_MATRIX, or for
# AMD `hip-<build>`. Empty: Strata picks by the card. CUDA 12 or 13 is not a choice: Strata
# picks it from the driver, so only AMD builds can be named.


def _split_backend(value: Any) -> Tuple[str, str]:
    """(family, build) as Strata's engine_component.split_backend reads a typed name."""
    name = str(value or "").strip().lower()
    if name not in _BACKEND_MATRIX and name.startswith("hip-") and len(name) > 4:
        return "hip", name[4:]
    return name, ""


def engine_backends(plat: Optional[str] = None) -> List[str]:
    """The values `strata_engine_backend` may take on `plat`: each family Strata commits to
    there that the pinned feed has a package of, and `hip-<build>` per AMD package."""
    plat = plat or platform_tag()
    rows = _ENGINE_PACKAGES.get(plat, ())
    out: List[str] = []
    for family, platforms in _BACKEND_MATRIX.items():
        builds = [r[1] for r in rows if r[0] == family]
        if plat not in platforms or not builds:
            continue
        out.append(family)
        if family == "hip":
            out.extend(f"hip-{b}" for b in builds if b)
    return out


def resolve_engine_backend(config: Optional[Dict[str, Any]] = None, plat: Optional[str] = None) -> str:
    """The alias's `strata_engine_backend`, normalised as Strata does (`strip().lower()`),
    "" when unset; a value this platform cannot take raises ValueError with the reason."""
    name = str((config or {}).get("strata_engine_backend") or "").strip().lower()
    if not name:
        return ""
    plat = plat or platform_tag()
    allowed = engine_backends(plat)
    if name in allowed:
        return name
    family, _build = _split_backend(name)
    if family not in _BACKEND_MATRIX:
        reason = f"unknown backend family {family!r}"
    elif plat not in _BACKEND_MATRIX[family]:
        reason = (f"{family} is not a supported combination on {plat}; it is supported on "
                  f"{', '.join(_BACKEND_MATRIX[family])}. Unsupported combinations are not attempted")
    else:
        reason = f"Strata {STRATA_RELEASE_TAG} publishes no {name} engine package for {plat}"
    if name.startswith("cuda"):
        reason += "; CUDA 12 or 13 is picked by Strata from the driver"
    choose = (f"; choose one of {', '.join(allowed)}, or leave it empty for Strata to pick"
              if allowed else "; leave it empty")
    raise ValueError(f"strata_engine_backend {name!r}: {reason}{choose}")


def _chosen_rows(config: Optional[Dict[str, Any]], plat: str) -> Tuple[Tuple[str, str, int, int], ...]:
    """The pinned feed's packages the alias's choice can install: all of the platform when empty."""
    rows = _ENGINE_PACKAGES[plat]
    family, build = _split_backend(resolve_engine_backend(config, plat))
    if not family:
        return rows
    chosen = tuple(r for r in rows if r[0] == family and (not build or r[1] == build))
    if not chosen:  # unreachable while the check above holds; never a max() of nothing
        raise ValueError(f"strata_engine_backend {family}: no package of it for {plat}")
    return chosen


class _NotTheChosenEngine(ValueError):
    """The engine is the pinned one, but not of the backend the alias chose."""


def _check_chosen(identity: Dict[str, Any], backend: str, folder: Path) -> None:
    """Strata's rule: `hip-<build>` names the family and the build, any other word the family only."""
    family, build = _split_backend(backend)
    if not family:
        return
    if identity.get("backend") != family or (build and identity.get("variant") != build):
        found = identity.get("backend") or "no backend"
        if identity.get("variant"):
            found += f" ({identity['variant']})"
        raise _NotTheChosenEngine(
            f"the Strata engine at {folder} is {found}, not the chosen strata_engine_backend {backend}"
        )


# --- one engine of a family: Strata's start cannot be told a build ------------------------
# A start resolves `component:strata-engine` with the family alone (engine_component.py
# expand, :1165-1174) and picks among that family's folders by its own order: a rollback pin,
# then version, driver, AMD preference, arch (resolve, :1145-1152). The updater keeps the older
# engines (updater.py:1217), so hip-rdna2 could
# outrank a chosen hip-rdna3. The install therefore holds one folder per family, and a start
# refuses an install with two. DPC's retained pin is the rollback, not Strata's "previous";
# Strata's own state (`.updates/state.json`) is never written.


def _engines_of_family(engine_dir: Path, family: Any, keep: str) -> List[Path]:
    """The engine folders of `family` under `engine_dir` other than `keep`. A dot folder is
    not one: Strata's scan skips it (engine_component.py:993)."""
    out: List[Path] = []
    if not engine_dir.is_dir():
        return out
    for entry in sorted(engine_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == keep:
            continue
        try:
            manifest = json.loads((entry / _ENGINE_MANIFEST).read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("backend") == family:
            out.append(entry)
    return out


def _prune_family(engine_dir: Path, family: Any, keep: str) -> None:
    """Remove the other engines of `family`: rename aside to a dot name Strata does not
    scan, then delete; a folder that cannot be moved (in use) is kept and logged."""
    for entry in _engines_of_family(engine_dir, family, keep):
        aside = entry.with_name(f".{entry.name}.old")
        shutil.rmtree(aside, ignore_errors=True)
        try:
            entry.replace(aside)
        except OSError as exc:
            logger.warning("Kept Strata engine %s beside %s: it could not be moved aside (%s)", entry, keep, exc)
            continue
        shutil.rmtree(aside, ignore_errors=True)
        logger.info("Removed Strata engine %s: %s is the one %s engine of this install", entry.name, keep, family)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain_relative(name: Any) -> bool:
    if not isinstance(name, str) or not name:
        return False
    for flavour in (PurePosixPath(name), PureWindowsPath(name)):
        if flavour.is_absolute() or flavour.drive or flavour.root or ".." in flavour.parts:
            return False
    return True


def check_engine_identity(
    folder: Path, manifest_sha256: Optional[str] = None, files: bool = False, backend: str = ""
) -> Dict[str, Any]:
    """Refuse an engine that is not the pinned one, signed by the key held here.

    `manifest_sha256` also pins the manifest's bytes (the marker's record); `files`
    hashes every file the manifest lists (at install; Strata does it at every start);
    a set `backend` (the alias's `strata_engine_backend`) must be the engine's
    (`_NotTheChosenEngine`, a ValueError).
    """
    folder = Path(folder)
    root = folder.parents[2] if len(folder.parents) > 2 else folder

    def refuse(field: str, found: Any, pinned: Any) -> ValueError:
        return ValueError(
            f"the Strata engine at {folder} is not the pinned one: {field} is {found!r}, "
            f"the pin says {pinned!r}; delete {root} to fetch it again"
        )

    try:
        raw = (folder / _ENGINE_MANIFEST).read_bytes()
    except OSError as exc:
        raise refuse(_ENGINE_MANIFEST, f"unreadable ({exc})", "present") from None
    actual_sha = hashlib.sha256(raw).hexdigest()
    if manifest_sha256 is not None and actual_sha != manifest_sha256:
        raise refuse("manifest_sha256", actual_sha, manifest_sha256)
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
        raise refuse(_ENGINE_MANIFEST, f"not JSON ({exc})", "a JSON object") from None
    if not isinstance(manifest, dict):
        raise refuse(_ENGINE_MANIFEST, type(manifest).__name__, "a JSON object")

    key_id = _update_key_id()
    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        raise refuse("signature", signature, f"ed25519 by {key_id}")
    if signature.get("alg") != "ed25519":
        raise refuse("signature.alg", signature.get("alg"), "ed25519")
    if signature.get("key_id") != key_id:
        raise refuse("signature.key_id", signature.get("key_id"), key_id)
    # Strata's engine_component.signing_payload, byte for byte.
    body = {k: v for k, v in manifest.items() if k != "signature"}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(STRATA_UPDATE_PUBLIC_KEY)).verify(
            base64.b64decode(str(signature.get("value", ""))), payload
        )
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        raise refuse("signature", "one that does not verify", f"ed25519 by {key_id}") from None

    plat = platform_tag()
    commit = PLATFORM_ASSETS[plat]["commit"]
    version, build_id, variant = manifest.get("version"), manifest.get("build_id"), manifest.get("variant")
    found = f"version {version}, build_id {build_id}, variant {variant}"
    pinned = f"version {STRATA_ENGINE_VERSION}, build commit {commit}"
    if manifest.get("id") != "strata-engine":
        raise refuse("id", manifest.get("id"), "strata-engine")
    if version != STRATA_ENGINE_VERSION:
        raise refuse("version", found, pinned)
    if manifest.get("platform") != plat:
        raise refuse("platform", manifest.get("platform"), plat)
    # A whole hex token: the commit as the row has it, or the same commit in its full 40 digits.
    token = rf"(?<![0-9a-fA-F]){re.escape(commit)}(?:[0-9a-fA-F]{{{40 - len(commit)}}})?(?![0-9a-fA-F])"
    if not isinstance(build_id, str) or not re.search(token, build_id):
        raise refuse("build_id", found, pinned)
    _check_chosen({"backend": manifest.get("backend"), "variant": variant}, backend, folder)

    if files:
        listed = manifest.get("files")
        if not isinstance(listed, list) or not listed:
            raise refuse("files", listed, "a list of the engine's files")
        inside = folder.resolve()
        for item in listed:
            name = item.get("path") if isinstance(item, dict) else None
            if not _plain_relative(name):
                raise refuse("files[].path", name, "a plain relative path inside the folder")
            target = folder / name
            if not target.resolve().is_relative_to(inside) or not target.is_file():
                raise refuse(f"files[{name}]", "missing", "present")
            size = target.stat().st_size
            if size != item.get("bytes"):
                raise refuse(f"files[{name}].bytes", size, item.get("bytes"))
            sha = _sha256_file(target)
            if sha != item.get("sha256"):
                raise refuse(f"files[{name}].sha256", sha, item.get("sha256"))

    return {
        "folder": folder.name,
        "version": version,
        "build_id": build_id,
        "variant": variant,
        "backend": manifest.get("backend"),
        "manifest_sha256": actual_sha,
    }


def _updater_env(root: Path, home: Path) -> Dict[str, str]:
    """The environment Strata's updater runs in: none of the parent's Strata settings,
    and a throwaway per-user home.

    Every STRATA_* is stripped, among them STRATA_UPDATE_TRUSTED_KEYS (extra trusted
    keys), STRATA_UPDATE_FEED, STRATA_UPDATE_ALLOW_HTTP, STRATA_NO_ACQUIRE (refuses the
    update), STRATA_DATA_DIR, STRATA_INSTALL_DIR, STRATA_SETTINGS_FILE, STRATA_HOME,
    STRATA_STATE_DIR, STRATA_COMPONENTS_DIR, STRATA_REQUIRE_SIGNED_COMPONENTS and
    STRATA_KEEP_LEFTOVERS. The home redirect leaves no model
    discoverable, so the updater's test start is `--version` only (no full load on the
    card), and the user's Strata settings, tuning marks and procs records are untouched.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith("STRATA_") and k.upper() not in ("PYTHONHOME", "PYTHONPATH")
    }
    env.update({
        "STRATA_COMPONENTS_DIR": str(components_dir(root)),
        "STRATA_NO_HANDOVER": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        place = home / name.lower()
        place.mkdir(parents=True, exist_ok=True)
        env[name] = str(place)
    return env


def _control_env(root: Path, settings_file: Path, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment a Strata script runs in under Control's own Python. Isolated by:
    every STRATA_* of ours dropped, PYTHONHOME / PYTHONPATH / VIRTUAL_ENV dropped,
    PYTHONNOUSERSITE set (no user site-packages), DPC's own engine folder, and the
    settings file the caller names, so the owner's own Strata settings are not read.
    `extra` is applied last."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith("STRATA_") and k.upper() not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")
    }
    env.update({
        "STRATA_COMPONENTS_DIR": str(components_dir(root)),
        "STRATA_SETTINGS_FILE": str(settings_file),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra or {})
    return env


def _control_python(root: Path, script: str) -> Path:
    """Control's interpreter, after checking that Control (with `script` in it) is installed at `root`."""
    python = control_python_path(root)
    if not python.is_file() or not (root / script).is_file():
        raise RuntimeError(
            f"Strata Control {STRATA_RELEASE_TAG} is not installed at {root}; download it first (the strata download)"
        )
    return python


_MODELS_TABLE_TIMEOUT_S = 120


def _strata_models_table(folder: Path) -> List[Dict[str, Any]]:
    """Strata's own `model_table` for the model files folder `folder`, as `list_strata_models`
    passes it on (families -> sizes). Runs `strata_models_probe.py` under Control's Python
    in a process of its own, with a settings file that does not exist so the owner's own
    Strata data folder does not move the disk figure; writes nothing, and `<folder>/strata/
    run-configs` is not created. Raises RuntimeError naming the reason."""
    from ..dpc_agent.tools import process as _process

    root = install_root()
    python = _control_python(root, "tools/strata_mcp.py")
    probe = Path(__file__).resolve().parent / "strata_models_probe.py"
    env = _control_env(root, folder / "strata" / "no-strata-settings.json")
    run = _process.run_supervised(
        [str(python), "-I", str(probe), str(root), str(folder / "strata" / "run-configs")],
        launcher="strata",
        launcher_kind="subsystem",
        timeout=_MODELS_TABLE_TIMEOUT_S,
        cwd=str(root),
        env=env,
    )
    if run.timed_out:
        raise RuntimeError(f"reading Strata's model list ran past {_MODELS_TABLE_TIMEOUT_S} s and was stopped")
    if run.exceeded_mb:
        raise RuntimeError(f"reading Strata's model list went over the {run.ceiling_mb} MB ceiling and was stopped")
    report = _last_json_line(run.stdout) if run.returncode == 0 else None
    if not isinstance(report, dict) or not isinstance(report.get("families"), list):
        tail = (run.stderr or "").strip()[-600:] or "no message"
        raise RuntimeError(f"Strata's model list could not be read (exit {run.returncode}): {tail}")
    return report["families"]


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    """The updater's result: the last JSON object line that is not a `--output jsonl` event."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "event" not in value:
                return value
    return None


def _engine_progress(line: str) -> Optional[Tuple[int, int]]:
    """`(done, total)` of one engine download line of `--output jsonl`, else None
    (`{"event": "progress", "component": "engine", "done", "total", ...}`, Strata's
    updater.py `_download`)."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("event") != "progress" or event.get("component") != "engine":
        return None
    done, total = event.get("done"), event.get("total")
    if type(done) is not int or type(total) is not int or done < 0 or total <= 0:
        return None
    return min(done, total), total


class _InstallBar:
    """One cumulative bar for the pin: Control's bytes, then the engine's `done`,
    against Control plus the engine's total: the largest package the alias may get until the first
    progress line gives the exact one. Shown bytes never go back (a download
    Strata restarts from 0 holds the bar)."""

    def __init__(self, control_size: int, engine_max: int, report: Callable[[int, int], None]):
        self._control_size = control_size
        self._engine_total = engine_max
        self._exact = False
        self._shown = 0
        self._report = report

    def _send(self, done: int) -> None:
        self._shown = max(self._shown, done)
        self._report(self._shown, self._control_size + self._engine_total)

    def control(self, got: int, _size: int) -> None:
        self._send(min(got, self._control_size))

    def engine_line(self, line: str) -> None:
        progress = _engine_progress(line)
        if progress is None:
            return
        done, total = progress
        if not self._exact:
            self._engine_total, self._exact = total, True
        self._send(self._control_size + min(done, self._engine_total))

    def finished(self) -> None:
        """The engine is in: with no progress line seen the bar jumps to the end here."""
        self._send(self._control_size + self._engine_total)


def _install_engine(
    staging: Path,
    root: Optional[Path] = None,
    on_stdout: Optional[Callable[[str], None]] = None,
    backend: str = "",
) -> Dict[str, Any]:
    """Install the pinned engine into staging's components by Strata's own updater, then check it.

    The updater's throwaway home is a folder inside `<root>.download`, beside the pin
    on DPC's volume and never the system temp folder, removed when the updater ends.
    `on_stdout` gets the updater's `--output jsonl` lines as they arrive. A set
    `backend` reaches the updater as `--backend` (its env carries no STRATA_*)."""
    from ..dpc_agent.tools import process as _process

    if root is None:
        root = staging.with_name(staging.name.removesuffix(".staging"))
    python = control_python_path(staging)
    for need in (python, staging / "strata.py", staging / "serve" / "server.py"):
        if not need.is_file():
            raise ValueError(
                f"the Control of Strata {STRATA_RELEASE_TAG} has no {need.relative_to(staging).as_posix()}"
            )
    components = components_dir(staging)
    cmd = [
        str(python), str(staging / "strata.py"), "update", "--no-control",
        "--feed", f"{STRATA_RELEASE_BASE}/{STRATA_RELEASE_TAG}",
        "--components-dir", str(components),
        "--output", "jsonl",
    ]
    if backend:
        cmd += ["--backend", backend]
    home = root.with_name(root.name + ".download") / "updater-home"
    shutil.rmtree(home, ignore_errors=True)
    try:
        run = _process.run_supervised(
            cmd,
            launcher="strata",
            launcher_kind="subsystem",
            timeout=_UPDATER_TIMEOUT_S,
            cwd=str(staging),
            env=_updater_env(staging, home),
            on_stdout=on_stdout,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if run.timed_out:
        raise RuntimeError(
            f"Strata's updater ran past {_UPDATER_TIMEOUT_S}s and was stopped ({run.killed}); nothing was installed"
        )
    if run.exceeded_mb:
        raise RuntimeError(f"Strata's updater went over the {run.ceiling_mb} MB ceiling and was stopped")
    report = _last_json_line(run.stdout) or {}
    results = report.get("results") if isinstance(report.get("results"), list) else []
    engine = next((r for r in results if isinstance(r, dict) and r.get("component") == "engine"), {})
    if run.returncode != 0 or report.get("ok") is not True:
        error = report.get("error") if isinstance(report.get("error"), dict) else {}
        code = error.get("code") or report.get("exit_code") or engine.get("exit_code") or run.returncode
        message = (error.get("message") or engine.get("error")
                   or (run.stderr or "").strip()[-300:] or "no message")
        space = _NO_SPACE.search(message) if code == 6 else None
        if space:
            raise RuntimeError(
                f"not enough free disk space for the Strata engine: needs about "
                f"{int(space['needed']) * (1 << 20) / 1e9:.1f} GB, "
                f"{int(space['free']) * (1 << 20) / 1e9:.1f} GB free on {space['drive']}"
            )
        meaning = _UPDATER_EXIT_MEANING.get(code)
        raise RuntimeError(
            f"Strata's updater did not install the engine (exit {code}{', ' + meaning if meaning else ''}): {message}"
        )
    folder = (engine.get("installed_after") or {}).get("folder")
    if engine.get("status") != "updated" or not isinstance(folder, str) or Path(folder).name != folder:
        raise RuntimeError(
            f"Strata's updater installed no engine (status {engine.get('status')!r}): "
            f"{engine.get('note') or engine.get('reasons') or 'no reason given'}"
        )
    identity = check_engine_identity(components / "engine" / folder, files=True, backend=backend)
    # In staging, before the swap: no launcher runs from it.
    _prune_family(components / "engine", identity["backend"], folder)
    return {"engine": identity}


def resolve_install(config: Dict[str, Any]) -> Optional[Path]:
    """Find a usable Strata install without touching the network.

    A set `strata_install_dir` wins; set but missing is an error, not a reason to
    fetch. Otherwise the pin's install counts when its marker is this tag and
    platform and Control is complete; its engine is then checked against the
    marker, and a mismatch is refused, never refetched silently. An engine that is
    not the alias's `strata_engine_backend` is not its install (None): it is offered
    for download like a missing pin, never run.
    """
    configured = config.get("strata_install_dir")
    if configured:
        path = Path(configured)
        if not path.is_dir():
            raise FileNotFoundError(
                f"strata_install_dir is set to {path} but there is no folder there; "
                "fix the alias config or clear strata_install_dir to fetch the pin"
            )
        return path

    backend = resolve_engine_backend(config)
    root = install_root()
    pinned = read_marker(root)
    if not pinned or pinned.get("tag") != STRATA_RELEASE_TAG or pinned.get("platform") != platform_tag():
        return None
    if not control_python_path(root).is_file() or not (root / "serve" / "server.py").is_file():
        return None
    engine = pinned.get("engine")
    folder = engine_folder(root)
    if folder is None or not isinstance(engine, dict):
        raise ValueError(f"the marker at {root} names no engine; delete {root} to fetch it again")
    try:
        identity = check_engine_identity(folder, manifest_sha256=engine.get("manifest_sha256") or "", backend=backend)
    except _NotTheChosenEngine:
        return None
    others = _engines_of_family(folder.parent, identity["backend"], folder.name)
    if others:
        # Refused, not refetched: a second engine of the family means Strata's start may
        # run it instead of the checked one, and a fetch would not loop round it.
        raise ValueError(
            f"the Strata install at {root} holds more than one {identity['backend']} engine "
            f"({', '.join(sorted([folder.name] + [p.name for p in others]))}); Strata's start picks "
            f"among them by its own order, not by the pin; delete {root} to fetch it again"
        )
    return root


def _retained_install_ok(root: Path, config: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the retained pin at `root` (another tag than this build's) can run for
    the alias `config`, on its marker's word: Control complete, the engine manifest's
    bytes the ones the marker recorded, its id, version and platform the marker's, the
    alias's backend, one engine of its family. Not re-signed: the key may move with a
    bump, and the marker was written after the install checked the signature."""
    root = Path(root)
    pinned = read_marker(root)
    if not pinned or pinned.get("tag") != root.parent.name or pinned.get("platform") != root.name:
        return False
    if not control_python_path(root).is_file() or not (root / "serve" / "server.py").is_file():
        return False
    engine = pinned.get("engine")
    folder = engine_folder(root)
    if folder is None or not isinstance(engine, dict):
        return False
    try:
        raw = (folder / _ENGINE_MANIFEST).read_bytes()
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return False
    if hashlib.sha256(raw).hexdigest() != engine.get("manifest_sha256") or not isinstance(manifest, dict):
        return False
    if (manifest.get("id") != "strata-engine" or manifest.get("version") != engine.get("version")
            or manifest.get("platform") != root.name):
        return False
    try:
        _check_chosen({"backend": manifest.get("backend"), "variant": manifest.get("variant")},
                      resolve_engine_backend(config, root.name), folder)
    except ValueError:
        return False
    return not _engines_of_family(folder.parent, manifest.get("backend"), folder.name)


def install_pin(
    progress: Optional[Callable[[int, int], None]] = None, config: Optional[Dict[str, Any]] = None
) -> Path:
    """Fetch Control, install the engine through it, check it; one install at a time.

    `progress(done, total)` is one bar across both (`_InstallBar`). The engine is
    the one the alias `config` chooses (`strata_engine_backend`); an installed pin
    with another engine is replaced by the staging swap, never while a launcher runs
    from it (one engine per install: Strata's updater prunes to the new and the
    previous engine, and its start picks the newest of a family, not a build)."""
    with _INSTALL_LOCK:
        backend = resolve_engine_backend(config)
        found = resolve_install({"strata_engine_backend": backend})
        if found:
            return found
        plat = platform_tag()
        row = PLATFORM_ASSETS[plat]
        root = install_root(plat=plat)
        engine_max = _largest(_chosen_rows(config, plat))[2]
        bar = _InstallBar(row["size"], engine_max, progress) if progress else None
        _refuse_if_running(root)

        def _check(staging: Path) -> Dict[str, Any]:
            extra = _install_engine(staging, root, bar.engine_line if bar else None, backend)
            if bar:
                bar.finished()
            _refuse_if_running(root)  # a launcher may have started while it downloaded
            return extra

        install_with_staging_swap(
            root,
            [row],
            f"{STRATA_RELEASE_BASE}/{STRATA_RELEASE_TAG}",
            {"tag": STRATA_RELEASE_TAG, "platform": plat, "assets": {row["name"]: row["sha256"]}},
            check=_check,
            progress=bar.control if bar else None,
        )
        return root


# --- the pin as a download the owner consents to -----------------------------
# After the pattern of `camoufox_binary`: `ModelDownloadService` offers and runs
# it through the `model_download_*` flow; a start only refuses while it is missing.

DOWNLOAD_NAME = "strata"
PURPOSE = "Running Strata models through DPC's own install of Strata (strata aliases)"
CONSEQUENCE_IF_DECLINED = (
    "strata aliases without strata_install_dir cannot start until it is downloaded; "
    "other providers are not affected."
)
# The `size_source` of the consent payload: the engine part of the size is the largest
# package the alias may get, an upper bound (`size_bytes_min` is the lower one). An alias
# whose `strata_engine_backend` names one package (hip-<build>, metal, sycl) has its exact
# size, "package" (`size_source`). Each size reader takes the alias config; None or no
# choice is the platform's whole range.
SIZE_SOURCE = "package_max"


def size_source(config: Optional[Dict[str, Any]] = None) -> str:
    """"package" when the alias's choice names exactly one package, else `SIZE_SOURCE`."""
    if resolve_engine_backend(config) and len(_chosen_rows(config, platform_tag())) == 1:
        return "package"
    return SIZE_SOURCE


def is_present(config: Optional[Dict[str, Any]] = None) -> bool:
    """The pin resolves with the alias's chosen engine (a configured `strata_install_dir`
    is not the pin). An install that does not check out is not present; its download
    then names why."""
    try:
        return resolve_install({"strata_engine_backend": (config or {}).get("strata_engine_backend")}) is not None
    except (ValueError, OSError):
        return False


def size_bytes(config: Optional[Dict[str, Any]] = None) -> int:
    """Control plus the largest engine package the alias may get (`size_source`)."""
    plat = platform_tag()
    return int(PLATFORM_ASSETS[plat]["size"]) + _largest(_chosen_rows(config, plat))[2]


def installed_bytes(config: Optional[Dict[str, Any]] = None) -> int:
    """Control unpacked plus the largest unpacked engine the alias may get."""
    plat = platform_tag()
    return int(PLATFORM_ASSETS[plat]["installed_bytes"]) + _largest(_chosen_rows(config, plat))[3]


def size_bytes_min(config: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Control plus the smallest engine package the alias may get; None when the size is exact."""
    if size_source(config) == "package":
        return None
    plat = platform_tag()
    return int(PLATFORM_ASSETS[plat]["size"]) + _smallest(_chosen_rows(config, plat))[2]


def installed_bytes_min(config: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Control unpacked plus the smallest unpacked engine the alias may get; None when exact."""
    if size_source(config) == "package":
        return None
    plat = platform_tag()
    return int(PLATFORM_ASSETS[plat]["installed_bytes"]) + _smallest(_chosen_rows(config, plat))[3]


def location() -> str:
    return str(install_root())


def download(
    on_progress: Optional[Callable[[int, int], None]] = None, config: Optional[Dict[str, Any]] = None
) -> str:
    """Install the pin with the alias's chosen engine; blocking, run it in a thread.
    Returns the install's root."""
    if config is None:
        return str(install_pin(on_progress))
    return str(install_pin(on_progress, config))


def ensure_install(
    config: Optional[Dict[str, Any]] = None, progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """The Strata root to run, fetching the pin only if nothing resolves."""
    return resolve_install(config or {}) or install_pin(progress, config)


def strata_pin() -> dict:
    """The pin as the status payload carries it: `{"release_tag", "engine_version"}`."""
    return {"release_tag": STRATA_RELEASE_TAG, "engine_version": STRATA_ENGINE_VERSION}
