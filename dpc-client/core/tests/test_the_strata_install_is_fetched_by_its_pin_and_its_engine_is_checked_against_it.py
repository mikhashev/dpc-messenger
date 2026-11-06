"""DPC's own Strata install: Control by its sha256 row, the engine by Strata's updater, then DPC's own check.

B2 step 2. `strata_fetcher.install_pin` downloads the pinned Control archive,
runs Control's updater (engine only, the pinned release's feed, a stripped
environment and a throwaway home) and refuses an engine that is not the pinned
one signed by the held key. The network and the updater are faked here: the
archive is built in the test and the fake updater writes an engine signed by a
key generated in the test. One test runs the real `run_supervised` to show the
updater's timeout fires and leaves no install and no live child.
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import psutil
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from dpc_client_core import process_registry
from dpc_client_core.dpc_agent.tools import process
from dpc_client_core.managers import local_engine_pin_fetcher as pin
from dpc_client_core.managers import strata_fetcher as sf

PLAT = "windows-x86_64"
COMMIT = sf.PLATFORM_ASSETS[PLAT]["commit"]
FOLDER = f"{sf.STRATA_ENGINE_VERSION}-{PLAT}-cuda-cuda13"
_REAL_RUN_SUPERVISED = process.run_supervised
ENGINE_FILES = {"strata.exe": b"engine bytes", "BUILD.json": b'{"commit": "x"}', "strata-vision.exe": b"reader"}


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _control_zip(strata_py: bytes = b"# Control's CLI\n") -> bytes:
    python = sf.control_python_path(Path("r")).relative_to("r").as_posix()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(python, b"not a real interpreter")
        zf.writestr("strata.py", strata_py)
        zf.writestr("serve/server.py", b"# the launcher\n")
    return buf.getvalue()


def _key_id(public_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_b64)).digest()[:8].hex()


def _sign(key, manifest: dict, public_b64: str) -> dict:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"alg": "ed25519", "key_id": _key_id(public_b64), "value": base64.b64encode(key.sign(payload)).decode()}


def _manifest(**overrides) -> dict:
    manifest = {
        "schema_version": 2, "id": "strata-engine", "name": "strata-engine",
        "version": sf.STRATA_ENGINE_VERSION, "platform": PLAT, "backend": "cuda", "variant": "cuda13",
        "build_id": f"{COMMIT}+cuda13.0", "entrypoint": "strata.exe",
        "files": [{"path": n, "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in ENGINE_FILES.items()],
        # Not ASCII on purpose: the payload is signed with ensure_ascii=False.
        "notes": "Strata engine — für the test ‘pin’",
    }
    manifest.update(overrides)
    return manifest


class _Env:
    """The faked world: DPC_HOME in tmp, a served Control archive, a held key, a fake updater."""

    def __init__(self, monkeypatch, tmp_path):
        self.mp, self.tmp = monkeypatch, tmp_path
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(
            self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        monkeypatch.setattr(sf, "STRATA_UPDATE_PUBLIC_KEY", self.public)
        monkeypatch.setattr(sf, "DPC_HOME", tmp_path / "dpc")
        monkeypatch.setattr(sf, "platform_tag", lambda: PLAT)
        self.serve(_control_zip())
        self.calls = []
        self.folder = FOLDER
        self.manifest = _manifest()
        self.after_sign = {}          # fields changed after signing (a tampered payload)
        self.signature = None         # a signature dict to use instead of a valid one
        self.files = dict(ENGINE_FILES)
        self.exit = None              # (returncode, stdout) for a failing updater
        monkeypatch.setattr(process, "run_supervised", self._run)

    def serve(self, archive: bytes):
        self.asked = []
        row = {**sf.PLATFORM_ASSETS[PLAT], "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive)}
        self.mp.setitem(sf.PLATFORM_ASSETS, PLAT, row)

        def _urlopen(url, timeout):
            self.asked.append(url)
            return _FakeResponse(archive)

        self.mp.setattr(pin.urllib.request, "urlopen", _urlopen)

    def _run(self, cmd, **kw):
        env = kw["env"]
        self.calls.append({"cmd": list(cmd), **kw, "env": dict(env),
                           "home_existed": Path(env["LOCALAPPDATA"]).is_dir()})
        if self.exit is not None:
            code, stdout = self.exit
            return process.SupervisedRun(returncode=code, stdout=stdout, stderr="")
        components = Path(cmd[cmd.index("--components-dir") + 1])
        folder = components / "engine" / self.folder
        folder.mkdir(parents=True)
        for name, data in self.files.items():
            (folder / name).write_bytes(data)
        manifest = dict(self.manifest)
        manifest["signature"] = self.signature or _sign(self.key, manifest, self.public)
        manifest.update(self.after_sign)
        (folder / "engine-component.json").write_bytes(
            json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8"))
        report = {"schema_version": 1, "ok": True,
                  "feed": {"source": cmd[cmd.index("--feed") + 1], "sequence": 121},
                  "results": [{"component": "engine", "status": "updated", "changed": True,
                               "installed_after": {"version": manifest["version"], "folder": self.folder}}],
                  "exit_code": 0}
        return process.SupervisedRun(returncode=0, stdout="note: a line before\n" + json.dumps(report) + "\n")

    @property
    def root(self) -> Path:
        return sf.install_root()

    def leftovers(self) -> list:
        parent = self.root.parent
        return sorted(p.name for p in parent.iterdir()) if parent.is_dir() else []


@pytest.fixture
def world(monkeypatch, tmp_path):
    for name in ("STRATA_UPDATE_TRUSTED_KEYS", "STRATA_NO_ACQUIRE", "STRATA_DATA_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "planted"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "planted"))
    return _Env(monkeypatch, tmp_path)


# --- the rows and the key ---------------------------------------------------------------

def test_one_control_row_per_platform_of_one_control_version():
    assert set(sf.PLATFORM_ASSETS) == {"windows-x86_64", "linux-x86_64", "linux-aarch64", "macos-arm64"}
    versions = set()
    for plat, row in sf.PLATFORM_ASSETS.items():
        assert plat in row["name"]
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) and row["size"] > 0
        assert re.fullmatch(r"[0-9a-f]{9,12}", row["commit"])  # the Mac engines of v0.2.11 write nine digits
        versions.add(re.match(r"strata-control-(\d+\.\d+\.\d+)-", row["name"]).group(1))
    assert versions == {sf.STRATA_ENGINE_VERSION}


def test_the_held_key_derives_strata_s_key_id():
    assert sf._update_key_id() == "5d1bc23abbeffd0d"


@pytest.mark.parametrize("system,machine,expected", [
    ("win32", "AMD64", "windows-x86_64"), ("linux", "x86_64", "linux-x86_64"),
    ("linux", "aarch64", "linux-aarch64"), ("darwin", "arm64", "macos-arm64"),
    ("win32", "ARM64", None), ("darwin", "x86_64", None),
])
def test_platform_words_and_no_silent_fallback(monkeypatch, system, machine, expected):
    monkeypatch.setattr(sf.sys, "platform", system)
    monkeypatch.setattr(sf._platform, "machine", lambda: machine)
    if expected is None:
        with pytest.raises(ValueError, match="no Strata Control for this machine"):
            sf.platform_tag()
    else:
        assert sf.platform_tag() == expected


# --- the install ------------------------------------------------------------------------

def test_the_pin_installs_control_then_the_engine_by_the_updater_and_records_it(world):
    root = sf.install_pin()

    assert root == world.root == sf.DPC_HOME / "bin" / "strata" / sf.STRATA_RELEASE_TAG / PLAT
    assert world.asked == [f"{sf.STRATA_RELEASE_BASE}/{sf.STRATA_RELEASE_TAG}/{sf.PLATFORM_ASSETS[PLAT]['name']}"]
    (call,) = world.calls
    cmd = call["cmd"]
    staging = root.with_name(root.name + ".staging")
    assert cmd[1:3] == [str(staging / "strata.py"), "update"]
    assert "--no-control" in cmd
    feed = cmd[cmd.index("--feed") + 1]
    assert feed == f"https://github.com/Niko1221/Strata/releases/download/{sf.STRATA_RELEASE_TAG}"
    assert "latest" not in feed
    assert cmd[cmd.index("--components-dir") + 1] == str(staging / "components")
    assert cmd[cmd.index("--output") + 1] == "jsonl"
    assert (call["launcher"], call["launcher_kind"], call["timeout"]) == ("strata", "subsystem", 3600)
    assert call["cwd"] == str(staging)

    env = call["env"]
    for planted in ("STRATA_UPDATE_TRUSTED_KEYS", "STRATA_NO_ACQUIRE", "STRATA_DATA_DIR", "PYTHONPATH"):
        assert planted not in env
    assert env["STRATA_NO_HANDOVER"] == "1"
    assert env["STRATA_COMPONENTS_DIR"] == str(staging / "components")
    assert env["PYTHONNOUSERSITE"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    homes = {env[n] for n in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    assert len(homes) == 5 and call["home_existed"]
    assert env["APPDATA"] != os.environ.get("APPDATA") and env["LOCALAPPDATA"] != os.environ.get("LOCALAPPDATA")
    assert not Path(env["LOCALAPPDATA"]).exists()  # the throwaway home is gone

    marker = json.loads(pin.marker_path(root).read_text(encoding="utf-8"))
    assert marker["tag"] == sf.STRATA_RELEASE_TAG and marker["platform"] == PLAT
    assert marker["assets"] == {sf.PLATFORM_ASSETS[PLAT]["name"]: sf.PLATFORM_ASSETS[PLAT]["sha256"]}
    raw = (root / "components" / "engine" / FOLDER / "engine-component.json").read_bytes()
    assert marker["engine"] == {"folder": FOLDER, "version": sf.STRATA_ENGINE_VERSION,
                                "build_id": f"{COMMIT}+cuda13.0", "variant": "cuda13", "backend": "cuda",
                                "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    assert world.leftovers() == [PLAT]

    assert sf.engine_folder() == root / "components" / "engine" / FOLDER
    assert sf.resolve_install({}) == root
    assert sf.ensure_install() == root and sf.install_pin() == root
    assert len(world.calls) == 1 and len(world.asked) == 1


def test_the_engine_folder_is_the_one_the_updater_reports(world):
    world.folder = f"{FOLDER}-1a2b3c4d"
    root = sf.install_pin()
    assert sf.read_marker(root)["engine"]["folder"] == world.folder
    assert sf.engine_folder() == root / "components" / "engine" / world.folder


@pytest.mark.parametrize("code,stdout,words", [
    (5, {"schema_version": 1, "ok": False, "error": {"code": 5, "message": "the package failed its check: x"}},
     "exit 5, refused): the package failed its check"),
    (6, {"schema_version": 1, "ok": False, "feed": {}, "exit_code": 6,
         "results": [{"component": "engine", "status": "failed", "error": "the new engine was not accepted",
                      "exit_code": 6}]},
     "exit 6, failed): the new engine was not accepted"),
])
def test_a_refusing_updater_leaves_no_install_and_no_staging(world, code, stdout, words):
    world.exit = (code, json.dumps(stdout) + "\n")
    with pytest.raises(RuntimeError) as err:
        sf.install_pin()
    assert words in str(err.value)
    assert world.leftovers() == []


# --- the engine's identity --------------------------------------------------------------

def _other_public() -> str:
    return base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)).decode()


@pytest.mark.parametrize("case,field", [
    ("version", "version is 'version 0.1.56, build_id"),
    ("key_id", "signature.key_id"),
    ("bad_signature", "signature is 'one that does not verify'"),
    ("tampered", "signature is 'one that does not verify'"),
    ("platform", "platform is 'linux-x86_64'"),
    ("commit", "build_id is 'version 0.1.57, build_id 4cf0d8cd3809+metal"),
    ("file_hash", "files[strata.exe].sha256"),
])
def test_an_engine_that_is_not_the_pin_is_refused_at_install(world, case, field):
    if case == "version":
        world.manifest = _manifest(version="0.1.56")
    elif case == "key_id":
        world.signature = {**_sign(world.key, world.manifest, world.public), "key_id": _key_id(_other_public())}
    elif case == "bad_signature":
        other = Ed25519PrivateKey.generate()
        world.signature = _sign(other, world.manifest, world.public)  # the held key id, another key's bytes
    elif case == "tampered":
        world.after_sign = {"variant": "cuda12"}
    elif case == "platform":
        world.manifest = _manifest(platform="linux-x86_64")
    elif case == "commit":
        world.manifest = _manifest(build_id="4cf0d8cd3809+metal")
    elif case == "file_hash":
        world.files = {**ENGINE_FILES, "strata.exe": b"engine bytez"}  # same size, other bytes
    with pytest.raises(ValueError) as err:
        sf.install_pin()
    message = str(err.value)
    assert field in message
    assert "delete" in message and "to fetch it again" in message
    if case in ("version", "commit"):
        assert "variant cuda13" in message and f"build commit {COMMIT}" in message
    assert world.leftovers() == []


def test_the_build_commit_counts_as_a_whole_token_wherever_build_id_puts_it(world):
    # linux-aarch64 engines carry the commit at the tail of build_id, the others at the head.
    world.manifest = _manifest(build_id=f"release-0.1.57-cuda13-13.0-aarch64-{COMMIT}")
    assert sf.install_pin().is_dir()


def test_the_build_commit_spelled_in_full_counts_as_that_commit(world):
    # The Linux sycl engine of v0.2.11 writes all 40 hex digits of the commit its siblings shorten to 12.
    world.manifest = _manifest(build_id=f"{COMMIT}{'a' * (40 - len(COMMIT))}-oneapi2026.1.1")
    assert sf.install_pin().is_dir()


_FEED = json.loads((Path(__file__).parent / "fixtures" / f"strata_{sf.STRATA_RELEASE_TAG}_feed_packages.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", _FEED["engine"], ids=lambda row: row["url"])
def test_every_engine_of_the_pinned_feed_carries_the_commit_its_platform_row_names(world, monkeypatch, tmp_path, row):
    # What a bump gets wrong silently: one package spelling the commit another way is refused only after its download.
    monkeypatch.setattr(sf, "platform_tag", lambda: row["platform"])
    folder = tmp_path / "pin" / "components" / "engine" / "one"
    folder.mkdir(parents=True)
    manifest = _manifest(platform=row["platform"], backend=row["backend"], variant=row["variant"] or None, build_id=row["build_id"])
    manifest["signature"] = _sign(world.key, manifest, world.public)
    (folder / "engine-component.json").write_bytes(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    assert sf.check_engine_identity(folder)["build_id"] == row["build_id"]


def test_the_build_commit_inside_a_longer_hex_run_is_not_the_commit(world):
    world.manifest = _manifest(build_id=f"{COMMIT}ff+cuda13.0")
    with pytest.raises(ValueError, match="build_id"):
        sf.install_pin()
    assert world.leftovers() == []


def test_a_marker_whose_manifest_digest_differs_is_refused_not_refetched(world):
    root = sf.install_pin()
    marker = pin.marker_path(root)
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["engine"]["manifest_sha256"] = "0" * 64
    marker.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_sha256"):
        sf.resolve_install({})
    with pytest.raises(ValueError, match="manifest_sha256"):
        sf.ensure_install()
    assert len(world.calls) == 1 and len(world.asked) == 1


def test_a_marker_of_another_tag_is_no_install(world):
    root = sf.install_pin()
    marker = pin.marker_path(root)
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["tag"] = "v0.2.8"
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert sf.resolve_install({}) is None
    assert sf.engine_folder() is None


def test_the_engine_folder_of_an_older_pin_is_read_against_its_own_folders(tmp_path, monkeypatch):
    # B2 step 3 runs a retained pin: its marker names its own tag folder, not the constant.
    monkeypatch.setattr(sf, "DPC_HOME", tmp_path)
    root = sf.pins_dir() / "v0.2.8" / PLAT
    root.mkdir(parents=True)
    marker = {"tag": "v0.2.8", "platform": PLAT, "engine": {"folder": FOLDER}}
    pin.marker_path(root).write_text(json.dumps(marker), encoding="utf-8")
    assert sf.pins_dir() == tmp_path / "bin" / "strata"
    assert sf.engine_folder(root) == root / "components" / "engine" / FOLDER
    for wrong in ({"tag": sf.STRATA_RELEASE_TAG}, {"platform": "linux-x86_64"}):
        pin.marker_path(root).write_text(json.dumps({**marker, **wrong}), encoding="utf-8")
        assert sf.engine_folder(root) is None


def test_a_set_strata_install_dir_skips_the_fetch(world, tmp_path):
    checkout = tmp_path / "Strata"
    checkout.mkdir()
    assert sf.ensure_install({"strata_install_dir": str(checkout)}) == checkout
    with pytest.raises(FileNotFoundError, match="strata_install_dir"):
        sf.ensure_install({"strata_install_dir": str(tmp_path / "missing")})
    assert world.asked == [] and world.calls == []


# --- the timeout, through the real process module ---------------------------------------

def test_the_updater_timeout_fires_and_leaves_no_install_and_no_child(monkeypatch, tmp_path):
    """The real `run_supervised`; only the interpreter is this one instead of Control's."""
    pid_file = tmp_path / "dpc" / "bin" / "strata" / sf.STRATA_RELEASE_TAG / "updater-child.pid"
    sleeper = (b"import os, pathlib, time\n"
               b"p = pathlib.Path(__file__).resolve().parent.parent / 'updater-child.pid'\n"
               b"p.write_text(str(os.getpid()))\n"
               b"time.sleep(120)\n")
    env = _Env(monkeypatch, tmp_path)
    env.serve(_control_zip(sleeper))
    monkeypatch.setattr(process, "run_supervised", _REAL_RUN_SUPERVISED)
    monkeypatch.setattr(sf, "control_python_path", lambda root: Path(sys.executable))
    monkeypatch.setattr(sf, "_UPDATER_TIMEOUT_S", 4)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="ran past 4s and was stopped"):
        sf.install_pin()
    assert time.monotonic() - started < 60
    assert env.leftovers() == ["updater-child.pid"]

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            alive = psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            alive = False
        if not alive:
            break
        time.sleep(0.1)
    assert not alive, f"the updater's child {pid} outlived the timeout"
    (entry,) = [e for e in process_registry.get_registry().entries() if e.launcher == "strata"]
    assert (entry.launcher_kind, entry.exit_reason) == ("subsystem", "timeout")

