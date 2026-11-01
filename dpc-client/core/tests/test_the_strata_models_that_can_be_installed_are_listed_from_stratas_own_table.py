"""`list_strata_models`: Strata's own `model_table`, read by Control's Python, passed through.

Step 3a of the model files folder flow. The backend runs `managers/strata_models_probe.py`
under the pinned Control's interpreter in a process of its own, with none of our STRATA_*
settings, and answers `{"status": "success", "families": <Strata's table, unchanged>}`.
It writes nothing: `installed` is read from `<model files folder>/strata/run-configs`,
which is not created when it is absent.

No real Strata here. A fake Control root under tmp_path carries a fake `tools/strata_mcp.py`
(a `Strata` class with `configs`, `hardware`, `model_table`), and the current interpreter
stands in for Control's Python, so the real probe script runs in a real child process.
Nothing touches `~/.dpc`.
"""

import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import strata_fetcher
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

# What the fake `model_table` returns before `installed` is decided from the run configs:
# unknown keys, a null, a float, a non-ASCII word, a verdict with punctuation - none of it
# is ours to touch.
TABLE = [
    {
        "family": "qwen", "title": "Qwen3.8-Flash-Next", "about": None, "experimental": False, "images": True,
        "sizes": [
            {"model": "Q2_0", "about": "2-bit, the fastest", "download_gb": 66.4, "ram_needed_gb": 48,
             "experts_gb": 34.0, "on_this_pc": "fits", "installed": False, "id": "q2_0",
             "gpu_options": None, "engine_min": None},
            {"model": "IQ3_S", "about": "3.5-bit \u2014 the best", "download_gb": 83.6, "ram_needed_gb": 62,
             "experts_gb": 50.3, "on_this_pc": "does not fit", "installed": False, "id": "iq3_s",
             "gpu_options": {"recommended": "all"}, "engine_min": "0.1.45", "a_key_strata_adds_later": [1, 2]},
        ],
    },
    {"family": "gyro", "title": "Gyro", "about": "small", "experimental": True, "images": False, "sizes": []},
]

FAKE_MCP = textwrap.dedent('''
    import json, os, subprocess, sys
    from pathlib import Path

    print("noise printed while strata_mcp is imported")
    os.write(1, b"raw noise from the import\\n")

    class Strata:
        def __init__(self, root, src=None):
            self.root = Path(root)
            self.src = Path(src)

        def configs(self):
            return sorted(self.root.glob("strata-*.json"))

        def hardware(self, fresh=False):
            print("noise printed by hardware()")
            subprocess.run([sys.executable, "-c", "print('noise from a child of hardware()')"])
            mode = (self.src / "mode.txt").read_text().strip() if (self.src / "mode.txt").exists() else ""
            if mode == "explode":
                raise RuntimeError("table exploded")
            if mode == "exit":
                raise SystemExit("strata asked to stop")
            if mode == "silent":
                os._exit(0)
            return {"ram_gb": 64.0}

        def model_table(self, hw):
            table = json.loads((self.src / "table.json").read_text(encoding="utf-8"))
            have = {c.stem[len("strata-"):] for c in self.configs()}
            for family in table:
                for size in family["sizes"]:
                    size["installed"] = size["id"] in have
            seen = {
                "bait": os.environ.get("STRATA_BAIT"),
                "install_dir": os.environ.get("STRATA_INSTALL_DIR"),
                "components": os.environ.get("STRATA_COMPONENTS_DIR"),
                "settings": os.environ.get("STRATA_SETTINGS_FILE"),
                "pythonpath": os.environ.get("PYTHONPATH"),
                "isolated": sys.flags.isolated,
                "no_bytecode": sys.dont_write_bytecode,
                "root": str(self.root),
                "src": str(self.src),
                "cwd": os.getcwd(),
            }
            (self.src / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
            os.write(1, b"a partial line, no newline ")
            return table
''')


@pytest.fixture
def control(tmp_path, monkeypatch):
    """A fake Control root; the pin resolves to it, and the current Python is its Python."""
    root = tmp_path / "control"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "strata_mcp.py").write_text(FAKE_MCP, encoding="utf-8")
    (root / "table.json").write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: root)
    monkeypatch.setattr(strata_fetcher, "control_python_path", lambda r: Path(sys.executable))
    return root


def _host(tmp_path, folder=None):
    settings = Settings(tmp_path / "home")
    if folder is not None:
        settings.set_model_files_dir(str(folder))
    return SimpleNamespace(settings=settings)


def _tree(path: Path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else None


def _seen(control: Path):
    return json.loads((control / "seen.json").read_text(encoding="utf-8"))


class TestTheCommand:
    def test_the_command_is_allowed_and_is_a_service_method(self):
        assert "list_strata_models" in ALLOWED_COMMANDS
        assert callable(getattr(CoreService, "list_strata_models", None))
        assert getattr(CoreService.list_strata_models, "dpc_slow", False), "slow_command, like list_model_files"

    @pytest.mark.asyncio
    async def test_strata_table_is_passed_through_unchanged(self, tmp_path, control):
        folder = tmp_path / "gguf"
        out = await CoreService.list_strata_models(_host(tmp_path, folder))
        assert out == {"status": "success", "families": TABLE}

    @pytest.mark.asyncio
    async def test_installed_is_decided_from_the_run_configs_of_the_model_files_folder(self, tmp_path, control):
        folder = tmp_path / "gguf"
        configs = folder / "strata" / "run-configs"
        configs.mkdir(parents=True)
        (configs / "strata-iq3_s.json").write_text("{}", encoding="utf-8")
        out = await CoreService.list_strata_models(_host(tmp_path, folder))
        sizes = {s["id"]: s["installed"] for f in out["families"] for s in f["sizes"]}
        assert sizes == {"q2_0": False, "iq3_s": True}
        assert _seen(control)["root"] == str(configs.resolve())
        assert _seen(control)["src"] == str(control.resolve())


class TestItWritesNothing:
    @pytest.mark.asyncio
    async def test_a_folder_without_run_configs_gives_every_size_not_installed_and_is_not_created(
            self, tmp_path, control):
        folder = tmp_path / "gguf"
        host = _host(tmp_path, folder)
        before = _tree(folder)
        out = await CoreService.list_strata_models(host)
        assert out["status"] == "success"
        assert [s["installed"] for f in out["families"] for s in f["sizes"]] == [False, False]
        assert _tree(folder) == before == []
        assert not (folder / "strata").exists()

    @pytest.mark.asyncio
    async def test_a_model_files_folder_that_does_not_exist_is_not_created_either(self, tmp_path, control):
        host = _host(tmp_path)  # the default, <home>/models, never made
        out = await CoreService.list_strata_models(host)
        assert out["status"] == "success"
        assert not (tmp_path / "home" / "models").exists()

    @pytest.mark.asyncio
    async def test_the_probe_leaves_no_bytecode_in_controls_folder(self, tmp_path, control):
        await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert _seen(control)["no_bytecode"] is True
        assert [p for p in control.rglob("__pycache__")] == []


class TestTheProbesEnvironment:
    @pytest.mark.asyncio
    async def test_no_strata_setting_of_ours_reaches_the_probe(self, tmp_path, control, monkeypatch):
        monkeypatch.setenv("STRATA_BAIT", "leak")
        monkeypatch.setenv("strata_install_dir", "C:/somewhere")  # Windows names are case-blind
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))
        folder = tmp_path / "gguf"
        await CoreService.list_strata_models(_host(tmp_path, folder))
        seen = _seen(control)
        assert seen["bait"] is None and seen["install_dir"] is None
        assert seen["pythonpath"] is None
        # the two it sets itself: DPC's own engine folder, and a settings file that is not there
        assert seen["components"] == str(strata_fetcher.components_dir(control))
        assert not Path(seen["settings"]).exists()
        assert Path(seen["settings"]).parent == folder / "strata"
        assert seen["isolated"] == 1
        assert Path(seen["cwd"]).resolve() == control.resolve()


class TestWhatStrataPrints:
    @pytest.mark.asyncio
    async def test_stdout_pollution_from_the_import_the_call_and_a_child_does_not_break_the_json(
            self, tmp_path, control):
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out == {"status": "success", "families": TABLE}


class TestFailures:
    @pytest.mark.asyncio
    async def test_a_probe_that_fails_gives_an_error_with_the_tail_of_its_stderr(self, tmp_path, control):
        (control / "mode.txt").write_text("explode")
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error"
        assert "table exploded" in out["message"] and "exit 1" in out["message"]
        assert "families" not in out

    @pytest.mark.asyncio
    async def test_strata_stopping_the_interpreter_is_an_error_not_an_empty_list(self, tmp_path, control):
        (control / "mode.txt").write_text("exit")
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error" and "strata asked to stop" in out["message"]

    @pytest.mark.asyncio
    async def test_a_probe_that_ends_clean_with_no_table_is_an_error(self, tmp_path, control):
        (control / "mode.txt").write_text("silent")
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error" and "could not be read" in out["message"]

    @pytest.mark.asyncio
    async def test_control_not_installed_is_an_error_that_says_so(self, tmp_path, monkeypatch):
        missing = tmp_path / "bin" / "strata" / strata_fetcher.STRATA_RELEASE_TAG / "x"
        monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: missing)
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error"
        assert "not installed" in out["message"] and str(missing) in out["message"]
        assert not missing.exists()

    @pytest.mark.asyncio
    async def test_a_control_without_stratas_table_script_is_not_installed_either(self, tmp_path, control):
        (control / "tools" / "strata_mcp.py").unlink()
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error" and "not installed" in out["message"]

    @pytest.mark.asyncio
    async def test_a_machine_with_no_control_for_its_platform_is_an_error_reply_not_a_raise(
            self, tmp_path, monkeypatch):
        def no_control(*a, **k):
            raise ValueError("no Strata Control for this machine")

        monkeypatch.setattr(strata_fetcher, "install_root", no_control)
        out = await CoreService.list_strata_models(_host(tmp_path, tmp_path / "gguf"))
        assert out["status"] == "error" and "no Strata Control" in out["message"]


class TestTheProbeScriptItself:
    def test_it_wants_two_arguments(self):
        import subprocess

        probe = Path(strata_fetcher.__file__).with_name("strata_models_probe.py")
        done = subprocess.run([sys.executable, "-I", str(probe)], capture_output=True, text=True)
        assert done.returncode == 2 and "usage" in done.stderr and done.stdout == ""

    def test_a_folder_that_is_not_a_control_is_refused(self, tmp_path):
        import subprocess

        probe = Path(strata_fetcher.__file__).with_name("strata_models_probe.py")
        done = subprocess.run([sys.executable, "-I", str(probe), str(tmp_path), str(tmp_path / "rc")],
                              capture_output=True, text=True)
        assert done.returncode == 3 and "strata_mcp.py" in done.stderr
        assert not (tmp_path / "rc").exists()

    def test_the_probe_is_stdlib_only(self):
        import ast

        probe = Path(strata_fetcher.__file__).with_name("strata_models_probe.py")
        tree = ast.parse(probe.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        assert roots <= set(sys.stdlib_module_names), roots
