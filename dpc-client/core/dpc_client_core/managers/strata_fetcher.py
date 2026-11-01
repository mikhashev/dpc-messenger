"""
Strata Fetcher — the Strata release DPC is tested against, and DPC's own install of it (ADR-044 point 5, B2).

The pin is two numbers that belong together, on two version lines. `STRATA_RELEASE_TAG`
is the tag of one Strata release, on Strata's app line (0.2.x), published with its
`strata-feed.json`. The assets of that release carry the other line: the **Control**
package (`strata-control-<ver>-<platform>.zip`) and the engine packages are versioned
0.1.x inside it. `STRATA_ENGINE_VERSION` is that engine version (`strata.exe` and
`strata-vision.exe`, launched by the **launcher** `serve/server.py`), the one it reports
at `/metrics` `engine.version`. A person sees both as "Strata v0.2.9 (engine 0.1.55)".

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
import os
import platform as _platform
import re
import sys
import tempfile
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .local_engine_pin_fetcher import (
    DPC_HOME,
    install_with_staging_swap,
    read_marker,
)

STRATA_RELEASE_TAG = "v0.2.9"
STRATA_ENGINE_VERSION = "0.1.55"

# The tag's own folder: never releases/latest, which moves with every release.
STRATA_RELEASE_BASE = "https://github.com/Niko1221/Strata/releases/download"

# One Control row per platform, keyed by Strata's platform words. `commit` is the build
# commit every engine of that platform carries in its `build_id` as a whole hex token: at
# the head on Windows, Linux x86_64 and macOS (`99866a47a2b8+cuda13.0`), at the tail on
# linux-aarch64 (`release-0.1.55-cuda13-13.0-aarch64-99866a47a2b8`).
# A bump fills `name`/`sha256`/`size` from the release's install-manifest.txt (check the
# API digest) and `commit` from the build_id of the platform's engines in its feed file.
PLATFORM_ASSETS: Dict[str, Dict[str, Any]] = {
    "windows-x86_64": {
        "name": "strata-control-0.1.55-windows-x86_64.zip",
        "sha256": "bdca3a42071704eab33455a4c85d4867c0ecfd6face95a08f13e8967e5594e99",
        "size": 73_121_142,
        "commit": "99866a47a2b8",
    },
    "linux-x86_64": {
        "name": "strata-control-0.1.55-linux-x86_64.tar.gz",
        "sha256": "50783d2fa6c6e98e9bdc8d4f66ad070706b1e1f3e5e2782cc59fd5dabd9488e8",
        "size": 99_498_096,
        "commit": "99866a47a2b8",
    },
    "linux-aarch64": {
        "name": "strata-control-0.1.55-linux-aarch64.tar.gz",
        "sha256": "0f298b915c6bbf71e14fcfcfea96348ea5333a86981c6f9c8398488671203789",
        "size": 83_372_878,
        "commit": "99866a47a2b8",
    },
    "macos-arm64": {
        "name": "strata-control-0.1.55-macos-arm64.tar.gz",
        "sha256": "5c3f8eada00ebfffa0fcca71f5f8a9abf45c8f8e5bb94ef07e9862191626f226",
        "size": 50_763_163,
        "commit": "4cf0d8cd3809",
    },
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
_INSTALL_LOCK = threading.Lock()


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
    folder: Path, manifest_sha256: Optional[str] = None, files: bool = False
) -> Dict[str, Any]:
    """Refuse an engine that is not the pinned one, signed by the key held here.

    `manifest_sha256` also pins the manifest's bytes (the marker's record); `files`
    hashes every file the manifest lists (at install; Strata does it at every start).
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
    token = rf"(?<![0-9a-fA-F]){re.escape(commit)}(?![0-9a-fA-F])"
    if not isinstance(build_id, str) or not re.search(token, build_id):
        raise refuse("build_id", found, pinned)

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


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _install_engine(staging: Path) -> Dict[str, Any]:
    """Install the pinned engine into staging's components by Strata's own updater, then check it."""
    from ..dpc_agent.tools import process as _process

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
        "--output", "json",
    ]
    with tempfile.TemporaryDirectory(prefix="strata-updater-home-", ignore_cleanup_errors=True) as home:
        run = _process.run_supervised(
            cmd,
            launcher="strata",
            launcher_kind="subsystem",
            timeout=_UPDATER_TIMEOUT_S,
            cwd=str(staging),
            env=_updater_env(staging, Path(home)),
        )
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
    identity = check_engine_identity(components / "engine" / folder, files=True)
    return {"engine": identity}


def resolve_install(config: Dict[str, Any]) -> Optional[Path]:
    """Find a usable Strata install without touching the network.

    A set `strata_install_dir` wins; set but missing is an error, not a reason to
    fetch. Otherwise the pin's install counts when its marker is this tag and
    platform and Control is complete; its engine is then checked against the
    marker, and a mismatch is refused, never refetched silently.
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
    check_engine_identity(folder, manifest_sha256=engine.get("manifest_sha256") or "")
    return root


def install_pin(progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Fetch Control, install the engine through it, check it; one install at a time."""
    with _INSTALL_LOCK:
        found = resolve_install({})
        if found:
            return found
        plat = platform_tag()
        row = PLATFORM_ASSETS[plat]
        root = install_root(plat=plat)
        install_with_staging_swap(
            root,
            [row],
            f"{STRATA_RELEASE_BASE}/{STRATA_RELEASE_TAG}",
            {"tag": STRATA_RELEASE_TAG, "platform": plat, "assets": {row["name"]: row["sha256"]}},
            check=_install_engine,
            progress=progress,
        )
        return root


def ensure_install(
    config: Optional[Dict[str, Any]] = None, progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """The Strata root to run, fetching the pin only if nothing resolves."""
    return resolve_install(config or {}) or install_pin(progress)


def strata_pin() -> dict:
    """The pin as the status payload carries it: `{"release_tag", "engine_version"}`."""
    return {"release_tag": STRATA_RELEASE_TAG, "engine_version": STRATA_ENGINE_VERSION}
