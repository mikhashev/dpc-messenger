"""A Strata alias with no `strata_install_dir` runs DPC's own install of the pin (B2 step 2).

The supervisor then takes Control's python and launcher from
`~/.dpc/bin/strata/<tag>/<platform>/`, fetches the pin before the card is
claimed, hands the launcher `STRATA_COMPONENTS_DIR`, refuses an engine that is
not the pinned version, and moves the base config's checkout paths with
`_base_config_for_the_pin`. A set `strata_install_dir` keeps today's start byte
for byte. Nothing is fetched or started: DPC_HOME, Control and the checkout are
built in tmp_path, the fetch and the launcher are faked.
"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import local_engine_pin_fetcher as pin
from dpc_client_core.managers import strata_fetcher as sf
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import StrataSupervisor, strata_chat_template, strata_config_errors
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from tests.test_strata_supervisor import _FakeProc, _install, _started_through_the_real_launch

PLAT = "windows-x86_64"
COMMIT = sf.PLATFORM_ASSETS[PLAT]["commit"]
FOLDER = f"{sf.STRATA_ENGINE_VERSION}-{PLAT}-cuda-cuda13-5d1bc23a"
BUILD_ID = f"{COMMIT}+cuda13.0"


class _World:
    """A checkout `Strata` beside `Strata-data`, as on the owner's machine, and the pin's root."""

    def __init__(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
        monkeypatch.setattr(sf, "DPC_HOME", tmp_path / "dpc")
        monkeypatch.setattr(sf, "platform_tag", lambda: PLAT)
        monkeypatch.delenv("STRATA_COMPONENTS_DIR", raising=False)
        self.mp = monkeypatch
        research = tmp_path / "dpc-research"
        self.checkout = research / "Strata"
        self.data = research / "Strata-data"
        self.root = sf.install_root()
        (self.checkout / "data").mkdir(parents=True)
        (self.checkout / "data" / "expert-profile.bin").write_bytes(b"checkout profile")
        (self.data / "packs" / "iq3_xxs" / "tokenizer").mkdir(parents=True)
        self.cvec = research / "refusal-direction.gguf"
        self.base = self.checkout / "strata-iq3_xxs-lowmem-park.json"
        self.write_base()

    def write_base(self, **overrides) -> None:
        c, d = self.checkout, self.data
        native = str(d / "models" / "IQ3_XXS" / "flash-next-00001-of-00002.gguf")
        base = {
            "exe": str(c / "engine" / "strata.exe"),
            "args": [
                "--pack", str(d / "packs" / "iq3_xxs"),
                "--native", native,
                "--ple-gguf", str(d / "models" / "IQ3_XXS" / "flash-next-00002-of-00002.gguf"),
                "--expert-profile", str(c / "data" / "expert-profile.bin"),
                "--expert-cache", "auto",
                "--mtp", str(d / "mtp" / "rt"),
                "--max-context", "261120",
                "--vision",
                "--vram-reserve-mib", "1536",
                "--control-vector-scaled", f"{self.cvec}:1.0",
                "--control-vector-layer-range", "4", "44",
                "--conversation-cache-mib", "4096",
            ],
            "cwd": str(c),
            "tokenizer": str(d / "packs" / "iq3_xxs" / "tokenizer"),
            "model_name": "qwen3.8-flash-next-iq3_xxs",
            "log": str(c / "strata-iq3_xxs-lowmem-park.log"),
            "lib_dirs": [str(c / ".venv" / "Lib" / "site-packages" / "nvidia" / "cu13" / "bin" / "x86_64")],
            "port": 8080,
            "vision": {
                "exe": str(c / "engine" / "strata-vision.exe"),
                "mmproj": str(d / "models" / "mmproj-BF16.gguf"),
                "model": native,
                "gpu": True,
            },
        }
        base.update(overrides)
        self.base.write_text(json.dumps(base, indent=1), encoding="utf-8")

    def control(self, profile: bool = True, marker: bool = True) -> Path:
        """The pin's root as `install_pin` leaves it: Control, the engine folder, the marker."""
        root = self.root
        python = sf.control_python_path(root)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
        (root / "serve").mkdir(parents=True, exist_ok=True)
        (root / "serve" / "server.py").write_text("# launcher\n", encoding="utf-8")
        if profile:
            (root / "data").mkdir(exist_ok=True)
            (root / "data" / "expert-profile.bin").write_bytes(b"control profile")
        (sf.components_dir(root) / "engine" / FOLDER).mkdir(parents=True, exist_ok=True)
        if marker:
            pin.marker_path(root).write_text(json.dumps({
                "tag": sf.STRATA_RELEASE_TAG, "platform": PLAT, "assets": {},
                "engine": {"folder": FOLDER, "version": sf.STRATA_ENGINE_VERSION, "build_id": BUILD_ID,
                           "variant": "cuda13", "backend": "cuda", "manifest_sha256": "0" * 64},
            }), encoding="utf-8")
        return root

    def sup(self, **overrides) -> StrataSupervisor:
        config = {
            "strata_base_config": str(self.base),
            "conversation_cache_mib": 10240,
            "engine_watch_interval_s": 0.01,
            "vram_release_timeout_s": 0.05,
        }
        config.update(overrides)
        return StrataSupervisor("flash next", config)


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    return _World(tmp_path, monkeypatch)


def _value(args, flag):
    return args[args.index(flag) + 1]


class TestThePinsPaths:
    def test_python_launcher_and_cwd_come_from_control(self, world, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        root = world.control()
        sup = world.sup()
        assert sup.install_is_the_pin()
        assert sup.install_dir() == root == sf.DPC_HOME / "bin" / "strata" / sf.STRATA_RELEASE_TAG / PLAT
        assert sup.python_path() == root / "python" / "python.exe"
        assert sup.server_script() == root / "serve" / "server.py"
        cmd = sup.build_command(sup.python_path(), 5000)
        assert cmd[:2] == [str(root / "python" / "python.exe"), str(root / "serve" / "server.py")]
        assert sup.build_start_config(5000)["cwd"] == str(root)

    def test_off_windows_control_python_is_bin_python3(self, world, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert world.sup().python_path() == world.root / "python" / "bin" / "python3"

    @pytest.mark.asyncio
    async def test_the_launcher_runs_in_the_control_root(self, world, monkeypatch):
        sup = world.sup()
        sup._log_fh = None
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen.update(kw)
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await sup._spawn(["python", "server.py"], {})
        assert seen["cwd"] == str(world.root)

    def test_an_empty_install_dir_is_the_pin_and_a_set_one_is_not(self, world, tmp_path):
        assert world.sup(strata_install_dir="").install_is_the_pin()
        assert not world.sup(strata_install_dir=str(_install(tmp_path))).install_is_the_pin()


class TestTheRuntimeEnvironment:
    def test_the_pin_names_its_components_folder(self, world):
        env = world.sup().build_env(Path("python"))
        assert env["STRATA_COMPONENTS_DIR"] == str(world.root / "components")
        assert env.get("LOCALAPPDATA") == os.environ.get("LOCALAPPDATA")  # not redirected

    def test_a_checkout_gets_todays_environment(self, world, tmp_path):
        sup = StrataSupervisor("checkout", {"strata_install_dir": str(_install(tmp_path)),
                                            "strata_base_config": "strata-base.json",
                                            "conversation_cache_mib": 10240})
        expected = dict(os.environ)
        expected.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        assert sup.build_env(Path("python")) == expected


class TestTheBaseConfigForThePin:
    @pytest.mark.parametrize("reader", ["strata-vision", "strata-vision-hip.exe"])
    def test_the_reader_keeps_the_bases_own_name_in_the_engine_folder(self, world, reader):
        # Off Windows the reader has no .exe, and an AMD base names the HIP reader.
        native = str(world.data / "models" / "IQ3_XXS" / "flash-next-00001-of-00002.gguf")
        world.write_base(vision={"exe": str(world.checkout / "engine" / reader),
                                 "mmproj": str(world.data / "models" / "mmproj-BF16.gguf"),
                                 "model": native, "gpu": True})
        root = world.control()
        cfg = world.sup().build_start_config(5000)
        assert cfg["vision"]["exe"] == str(root / "components" / "engine" / FOLDER / reader)

    def test_the_owners_base_moves_exactly_what_lies_in_the_checkout(self, world):
        root = world.control()
        cfg = world.sup().build_start_config(5000)
        args = cfg["args"]
        assert cfg["exe"] == "component:strata-engine"
        assert cfg["vision"]["exe"] == str(root / "components" / "engine" / FOLDER / "strata-vision.exe")
        assert cfg["cwd"] == str(root)
        assert "lib_dirs" not in cfg
        assert _value(args, "--expert-profile") == str(root / "data" / "expert-profile.bin")
        # Strata-data is a sibling of the checkout, not inside it.
        assert _value(args, "--pack") == str(world.data / "packs" / "iq3_xxs")
        assert _value(args, "--mtp") == str(world.data / "mtp" / "rt")
        assert cfg["tokenizer"] == str(world.data / "packs" / "iq3_xxs" / "tokenizer")
        assert cfg["vision"]["mmproj"] == str(world.data / "models" / "mmproj-BF16.gguf")
        assert cfg["vision"]["model"] == _value(args, "--native")
        assert _value(args, "--control-vector-scaled") == f"{world.cvec}:1.0"
        assert cfg["log"] == str(base_mod.DPC_HOME / "logs" / "strata-flash_next-engine.log")

    def test_a_scaled_control_vector_in_the_checkout_moves_with_its_scale(self, world):
        root = world.control()
        (world.checkout / "cv").mkdir()
        (world.checkout / "cv" / "r.gguf").write_bytes(b"v")
        (root / "cv").mkdir()
        (root / "cv" / "r.gguf").write_bytes(b"v")
        base = json.loads(world.base.read_text(encoding="utf-8"))
        i = base["args"].index("--control-vector-scaled")
        base["args"][i + 1] = f"{world.checkout / 'cv' / 'r.gguf'}:0.75"
        world.write_base(args=base["args"])
        args = world.sup().build_start_config(5000)["args"]
        assert _value(args, "--control-vector-scaled") == f"{root / 'cv' / 'r.gguf'}:0.75"

    def test_a_file_control_lacks_is_refused_by_its_key(self, world):
        world.control(profile=False)
        with pytest.raises(LlamaServerError) as excinfo:
            world.sup().build_start_config(5000)
        text = str(excinfo.value)
        assert "args --expert-profile" in text
        assert str(world.checkout / "data" / "expert-profile.bin") in text
        assert str(world.root / "data" / "expert-profile.bin") in text
        assert "set the alias's expert_profile to a file outside the checkout" in text

    def test_an_alias_path_into_the_checkout_is_kept_because_typed_fields_come_after(self, world):
        world.control(profile=False)
        own = world.checkout / "data" / "mine.bin"
        own.write_bytes(b"mine")
        args = world.sup(expert_profile=str(own)).build_start_config(5000)["args"]
        assert _value(args, "--expert-profile") == str(own)

    def test_an_extra_arg_into_the_checkout_is_the_aliases_own_and_kept(self, world):
        world.control()
        own = world.checkout / "data" / "draft_vocab.bin"
        own.write_bytes(b"vocab")
        args = world.sup(extra_args=["--draft-vocab", str(own)]).build_start_config(5000)["args"]
        assert args[-2:] == ["--draft-vocab", str(own)]

    def test_before_the_pin_is_installed_the_vision_exe_stays_and_the_write_is_refused(self, world):
        world.control(marker=False)
        sup = world.sup()
        cfg = sup.build_start_config(5000)
        assert cfg["vision"]["exe"] == str(world.checkout / "engine" / "strata-vision.exe")
        with pytest.raises(LlamaServerError, match="names no engine folder"):
            sup.write_start_config(5000)

    def test_a_relative_base_config_is_refused_without_an_install_dir(self, world):
        sup = world.sup(strata_base_config="strata-iq3_xxs-lowmem-park.json")
        with pytest.raises(LlamaServerError, match="needs an absolute strata_base_config when strata_install_dir is empty"):
            sup.base_config_path()
        with pytest.raises(LlamaServerError, match="needs an absolute strata_base_config"):
            sup.build_start_config(5000)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths fold case")
    def test_a_checkout_path_in_another_case_is_inside_it_on_windows(self, world):
        root = world.control()
        base = json.loads(world.base.read_text(encoding="utf-8"))
        i = base["args"].index("--expert-profile")
        base["args"][i + 1] = str(world.checkout / "DATA" / "Expert-Profile.bin").upper()
        world.write_base(args=base["args"])
        moved = _value(world.sup().build_start_config(5000)["args"], "--expert-profile")
        assert Path(moved).parent.parent == root

    def test_relative_paths_are_left_to_resolve_against_the_control_root(self, world):
        world.control()
        world.write_base(tokenizer="tokenizer")
        assert world.sup().build_start_config(5000)["tokenizer"] == "tokenizer"


def _checkout_golden(t: Path) -> dict:
    """`build_start_config(5555)` of a checkout alias as the code wrote it before B2 step 2."""
    root, data = t / "Strata", t / "Strata-data"
    return {
        "exe": str(root / "engine" / "strata.exe"),
        "args": [
            "--pack", str(data / "packs" / "iq3"),
            "--expert-profile", str(root / "mine.bin"),
            "--max-context", "261120", "--vision", "--vram-reserve-mib", "1536",
            "--control-vector-scaled", f"{t / 'cv.gguf'}:1.0",
            "--conversation-cache-mib", "10240", "--conversation-cache-slots", "2",
            "--conversation-cache-min-free-mib", "2560", "--conversation-cache-min-tokens", "0",
            "--conversation-cache-park-gain", "0", "--foo", "1",
        ],
        "cwd": str(root),
        "tokenizer": str(data / "packs" / "iq3" / "tokenizer"),
        "model_name": "m",
        "log": str(t / "dpc" / "logs" / "strata-golden-engine.log"),
        "lib_dirs": [str(root / ".venv" / "nv")],
        "port": 5555,
        "vision": {"exe": str(root / "engine" / "strata-vision.exe"), "mmproj": str(data / "mm.gguf"),
                   "model": str(data / "m1.gguf")},
        "sampling": {"top_k": 20},
        "idle_unload_s": 0,
        "host": "127.0.0.1",
        "open_browser": False,
        "_dpc": {"written_by": "DPC Messenger (ADR-044)", "alias": "golden",
                 "base_config": str(root / "base.json"),
                 "pin": {"release_tag": sf.STRATA_RELEASE_TAG, "engine_version": sf.STRATA_ENGINE_VERSION}},
    }


def test_a_checkout_alias_builds_the_start_config_it_built_before(tmp_path, monkeypatch):
    t = tmp_path
    monkeypatch.setattr(base_mod, "DPC_HOME", t / "dpc")
    root, data = t / "Strata", t / "Strata-data"
    (root / "data").mkdir(parents=True)
    (root / "data" / "expert-profile.bin").write_bytes(b"x")
    base = {
        "exe": str(root / "engine" / "strata.exe"),
        "args": ["--pack", str(data / "packs" / "iq3"), "--expert-profile", str(root / "data" / "expert-profile.bin"),
                 "--max-context", "261120", "--vision", "--vram-reserve-mib", "1536",
                 "--control-vector-scaled", f"{t / 'cv.gguf'}:1.0", "--conversation-cache-mib", "4096"],
        "cwd": str(root), "tokenizer": str(data / "packs" / "iq3" / "tokenizer"), "model_name": "m",
        "log": str(root / "x.log"), "lib_dirs": [str(root / ".venv" / "nv")], "port": 8080,
        "vision": {"exe": str(root / "engine" / "strata-vision.exe"), "mmproj": str(data / "mm.gguf"),
                   "model": str(data / "m1.gguf")},
    }
    (root / "base.json").write_text(json.dumps(base), encoding="utf-8")
    sup = StrataSupervisor("golden", {
        "strata_install_dir": str(root), "strata_base_config": "base.json",
        "conversation_cache_mib": 10240, "conversation_cache_slots": 2, "expert_profile": str(root / "mine.bin"),
        "model_params": {"sampling.top_k": 20, "idle_unload_s": 0}, "extra_args": ["--foo", "1"],
    })
    assert json.dumps(sup.build_start_config(5555), indent=1) == json.dumps(_checkout_golden(t), indent=1)


class TestTheFetchRunsWithTheCardHeld:
    def _fake_launch(self, sup, launches):
        async def fake_launch(binary, cache_type=None):
            launches.append(binary)
            sup._proc = _FakeProc()
            sup.props = {"modalities": {"vision": True}}
            return sup.props
        return fake_launch

    @pytest.mark.asyncio
    async def test_the_pin_is_fetched_in_a_thread_after_the_card_is_claimed(self, world, monkeypatch):
        # B2 step 3, 3b: a fetch may replace the install, so no launcher may run from it.
        sup = world.sup()
        fetched, launches = [], []

        def fake_ensure(config):
            fetched.append({
                "thread": threading.current_thread() is not threading.main_thread(),
                "card_held": sup in base_mod._CARD_CLAIMS,
                "config": config,
            })
            return world.control()

        monkeypatch.setattr(sf, "ensure_install", fake_ensure)
        monkeypatch.setattr(sup, "_launch", self._fake_launch(sup, launches))
        await sup.ensure_running()
        assert fetched == [{"thread": True, "card_held": True, "config": sup.config}]
        assert launches == [world.root / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")]
        assert sup in base_mod._CARD_CLAIMS

    @pytest.mark.asyncio
    async def test_a_checkout_alias_fetches_nothing(self, world, monkeypatch, tmp_path):
        sup = StrataSupervisor("checkout", {"strata_install_dir": str(_install(tmp_path)),
                                            "strata_base_config": "strata-base.json",
                                            "conversation_cache_mib": 10240})
        launches = []
        monkeypatch.setattr(sf, "ensure_install", lambda config: pytest.fail("a checkout alias fetched"))
        monkeypatch.setattr(sup, "_launch", self._fake_launch(sup, launches))
        await sup.ensure_running()
        assert len(launches) == 1

    @pytest.mark.asyncio
    async def test_a_failed_fetch_leaves_the_card_unclaimed(self, world, monkeypatch):
        sup = world.sup()

        def fake_ensure(config):
            raise RuntimeError("Strata's updater did not install the engine (exit 6, failed): no network")

        monkeypatch.setattr(sf, "ensure_install", fake_ensure)
        monkeypatch.setattr(sup, "_launch", lambda *a, **k: pytest.fail("launched without an install"))
        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        assert str(excinfo.value).startswith("strata[flash next]: DPC's own install of Strata v0.2.11")
        assert "exit 6, failed" in str(excinfo.value)
        assert sup not in base_mod._CARD_CLAIMS

    @pytest.mark.asyncio
    async def test_draining_that_begins_during_the_fetch_refuses_the_start(self, world, monkeypatch):
        sup = world.sup()

        def fake_ensure(config):
            sup._draining = True  # superseded while the download ran
            return world.control()

        monkeypatch.setattr(sf, "ensure_install", fake_ensure)
        monkeypatch.setattr(sup, "_launch", lambda *a, **k: pytest.fail("started while draining"))
        with pytest.raises(LlamaServerError, match="is draining"):
            await sup.ensure_running()
        assert sup not in base_mod._CARD_CLAIMS


class TestThePinsEngineVersion:
    @pytest.mark.asyncio
    async def test_another_version_on_the_pin_fails_closed_naming_both_fingerprints(self, world, monkeypatch):
        sup = world.sup()
        root = world.control()
        monkeypatch.setattr(sf, "resolve_install", lambda config: root)
        spawned = _started_through_the_real_launch(sup, monkeypatch, metrics={"engine": {"version": "0.1.46"}})
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        reason = sup.failure["reason"]
        assert "the engine reports version 0.1.46" in reason
        assert f"build_id {BUILD_ID}" in reason and "variant cuda13" in reason
        assert f"pinned: Strata {sf.STRATA_RELEASE_TAG}, engine {sf.STRATA_ENGINE_VERSION}, build commit {COMMIT}" in reason
        assert sup.smoke_posts == [], "no smoke after a refused version"
        assert len(spawned) == 1 and sup._proc is None

    @pytest.mark.asyncio
    async def test_the_pinned_version_on_the_pin_starts(self, world, monkeypatch):
        sup = world.sup()
        root = world.control()
        monkeypatch.setattr(sf, "resolve_install", lambda config: root)
        _started_through_the_real_launch(sup, monkeypatch, metrics={"engine": {"version": sf.STRATA_ENGINE_VERSION}})
        await sup.ensure_running()
        assert sup.failure is None
        written = json.loads(sup.start_config_path().read_text(encoding="utf-8"))
        assert written["exe"] == "component:strata-engine" and written["cwd"] == str(root)
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_checkout_with_another_version_still_warns_and_starts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
        sup = StrataSupervisor("checkout", {"strata_install_dir": str(_install(tmp_path)),
                                            "strata_base_config": "strata-base.json",
                                            "conversation_cache_mib": 10240,
                                            "vram_release_timeout_s": 0.05})
        monkeypatch.setattr(sf, "ensure_install", lambda config: pytest.fail("a checkout alias fetched"))
        _started_through_the_real_launch(sup, monkeypatch, metrics={"engine": {"version": "0.1.46"}})
        await sup.ensure_running()
        assert sup.failure is None and sup.engine_version == "0.1.46"
        await sup.stop()


class TestAPeerDoesNotFetch:
    @pytest.fixture(autouse=True)
    def _registry(self):
        _ACTIVE_SUPERVISORS.clear()
        yield
        _ACTIVE_SUPERVISORS.clear()

    def _provider(self, world, **extra) -> StrataProvider:
        return StrataProvider("flash", {
            "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
            "strata_base_config": str(world.base), "conversation_cache_mib": 10240,
            "context_window": 200000, **extra,
        })

    @pytest.mark.asyncio
    async def test_a_peer_meets_a_pin_not_installed_yet_and_starts_nothing(self, world, monkeypatch):
        p = self._provider(world)
        monkeypatch.setattr(sf, "ensure_install", lambda config: pytest.fail("a peer set off the fetch"))
        monkeypatch.setattr(p.supervisor, "start_for_peer", lambda peer: pytest.fail("a peer began a start"))
        refusal = await p.peer_refusal("peer-1")
        assert refusal == (
            "'flash' is not running: Strata v0.2.11 is not installed on this node yet, "
            "and a peer's request does not fetch it"
        )

    @pytest.mark.asyncio
    async def test_an_installed_pin_goes_on_to_the_card_and_vram_rules(self, world, monkeypatch):
        p = self._provider(world)
        asked = []
        monkeypatch.setattr(sf, "resolve_install", lambda config: asked.append(config) or world.root)
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: None)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
        refusal = await p.peer_refusal("peer-1")
        assert asked and "free VRAM cannot be read" in refusal

    @pytest.mark.asyncio
    async def test_a_checkout_alias_does_not_ask_for_the_pin(self, world, monkeypatch, tmp_path):
        p = self._provider(world, strata_install_dir=str(_install(tmp_path)))
        monkeypatch.setattr(sf, "resolve_install", lambda config: pytest.fail("asked for the pin"))
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: None)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
        assert "free VRAM cannot be read" in await p.peer_refusal("peer-1")


class TestTheSaveWithoutAnInstallDir:
    def test_an_absolute_base_config_is_enough(self, world):
        config = {"strata_base_config": str(world.base), "conversation_cache_mib": 10240}
        assert strata_config_errors(config) == []
        assert strata_config_errors(config, check_paths=True) == []

    def test_a_relative_base_config_is_refused_without_the_disk(self):
        errors = strata_config_errors({"strata_base_config": "strata-base.json", "conversation_cache_mib": 1})
        assert errors == [
            "strata_base_config: strata-base.json must be an absolute path when strata_install_dir is "
            "empty — DPC's own install of Strata v0.2.11 holds no run config of yours to read it against"
        ]
        assert strata_config_errors({"strata_base_config": "strata-base.json", "conversation_cache_mib": 1},
                                    check_paths=True) == errors

    def test_a_missing_base_config_names_only_that_key(self):
        errors = strata_config_errors({"conversation_cache_mib": 1})
        assert errors == [
            "needs strata_base_config: an existing Strata run config to start from (strata_install_dir "
            "may stay empty: empty means DPC's own install of Strata v0.2.11 (engine 0.1.57))"
        ]

    def test_the_chat_template_of_a_relative_tokenizer_is_read_from_the_pin(self, world):
        (world.root / "tok").mkdir(parents=True)
        (world.root / "tok" / "chat_template.jinja").write_text("pin template", encoding="utf-8")
        base = json.loads(world.base.read_text(encoding="utf-8"))
        base.pop("cwd")
        base["tokenizer"] = "tok"
        world.base.write_text(json.dumps(base), encoding="utf-8")
        assert strata_chat_template({"strata_base_config": str(world.base)}) == "pin template"

