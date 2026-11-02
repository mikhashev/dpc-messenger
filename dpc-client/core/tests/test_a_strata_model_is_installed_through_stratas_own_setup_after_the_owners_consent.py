"""`install_strata_model`: a Strata model installed by Control's own `setup.py`, after the owner agrees.

Step 3b of the model files folder flow. The command does not download: it raises the model
download consent dialog (`model_download_required` for `strata-model`, with `strata_model`
= {family, model, images, gguf_dir} and Strata's `download_gb` as the size). The owner's confirm
(`download_model`) runs `setup.py` under the pinned Control's Python in a process of its own,
and ends in `model_download_completed` with the path of the run config it wrote.

No real Strata, no network, nothing under `~/.dpc`. A fake Control root under tmp_path carries
`tests/fixtures/fake_strata_mcp.py` (the table) and `tests/fixtures/fake_strata_setup.py` (a
`setup.py` that prints Strata's own kinds of line, writes `strata-<id>.json` into
`STRATA_INSTALL_DIR` and records its argv and environment). The current interpreter stands in
for Control's Python.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import process as process_module
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import strata_fetcher, strata_model_fetcher
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TABLE = [
    {"family": "qwen", "title": "Qwen3.8-Flash-Next", "about": None, "experimental": False, "images": True,
     "sizes": [
         {"model": "IQ3_S", "about": "3.5-bit", "download_gb": 6.0, "images": True, "id": "iq3_s",
          "installed": False, "on_this_pc": "fits"},
         {"model": "TXT", "about": "text only", "download_gb": 2.0, "images": False, "id": "txt",
          "installed": False, "on_this_pc": "fits"},
     ]},
    {"family": "swift", "title": "Swift 1.5", "about": None, "experimental": False, "images": True,
     "sizes": [
         {"model": "IQ2_XS", "about": "2-bit", "download_gb": 3.0, "images": True, "id": "swift-iq2_xs",
          "installed": False, "on_this_pc": "fits"},
     ]},
]


class FakeApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def named(self, name):
        return [p for n, p in self.events if n == name]


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    strata_model_fetcher._ROWS.clear()
    yield
    strata_model_fetcher._ROWS.clear()


@pytest.fixture
def control(tmp_path, monkeypatch):
    """A fake Control root; the pin resolves to it, and the current Python is its Python."""
    root = tmp_path / "control"
    (root / "tools").mkdir(parents=True)
    shutil.copy(FIXTURES / "fake_strata_mcp.py", root / "tools" / "strata_mcp.py")
    shutil.copy(FIXTURES / "fake_strata_setup.py", root / "setup.py")
    (root / "table.json").write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: root)
    monkeypatch.setattr(strata_fetcher, "control_python_path", lambda r: Path(sys.executable))
    return root


@pytest.fixture
def host(tmp_path):
    settings = Settings(tmp_path / "home")
    folder = tmp_path / "gguf"
    settings.set_model_files_dir(str(folder))
    api = FakeApi()
    service = ModelDownloadService(llm_manager=None, settings=settings, local_api=api)
    return SimpleNamespace(settings=settings, model_download_service=service, api=api, folder=folder)


def _seen(control: Path):
    return json.loads((control / "seen.json").read_text(encoding="utf-8"))


async def _ask(host, family="qwen", model="IQ3_S", images=True, gguf_dir=None):
    return await CoreService.install_strata_model(host, family, model, images, gguf_dir)


def _config(host, family="qwen", model="IQ3_S", images=True, gguf_dir=None):
    return {"strata_model": {"family": family, "model": model, "images": images, "gguf_dir": gguf_dir},
            "model_files_dir": str(host.folder)}


def _run_configs(host) -> Path:
    return host.folder / "strata" / "run-configs"


class TestTheCommand:
    def test_the_command_is_allowed_and_is_a_slow_service_method(self):
        assert "install_strata_model" in ALLOWED_COMMANDS
        assert getattr(CoreService.install_strata_model, "dpc_slow", False), "it reads the table off the read loop"

    def test_strata_model_is_a_download_module_of_the_consent_flow(self):
        from dpc_client_core import model_download_service as mds

        assert mds._OTHER_DOWNLOADS["strata-model"] is strata_model_fetcher
        assert strata_model_fetcher.DOWNLOAD_NAME == "strata-model"
        assert strata_model_fetcher.size_source() == "model_table"

    @pytest.mark.asyncio
    async def test_it_asks_and_downloads_nothing(self, host, control):
        out = await _ask(host)
        assert out == {"status": "success", "asked": True}
        assert not (control / "seen.json").exists(), "setup.py was not run"
        assert not (host.folder / "strata").exists(), "nothing was created"
        assert host.api.named("model_download_started") == []

    @pytest.mark.asyncio
    async def test_the_consent_payload_carries_the_choice_and_the_table_size(self, host, control):
        await _ask(host, images=False)
        [payload] = host.api.named("model_download_required")
        assert payload["model_name"] == "strata-model"
        assert payload["strata_model"] == {"family": "qwen", "model": "IQ3_S", "images": False, "gguf_dir": None}
        assert payload["size_source"] == "model_table"
        assert payload["size_bytes"] == 6_000_000_000
        assert payload["installed_bytes"] is None and payload["size_bytes_min"] is None
        assert payload["cache_path"] == str(host.folder / "strata")
        assert payload["provider_alias"] is None and payload["on_decline"] == "degrade"

    @pytest.mark.asyncio
    async def test_files_given_in_place_cost_nothing_to_download(self, host, control, tmp_path):
        own = tmp_path / "own-files"
        own.mkdir()
        await _ask(host, gguf_dir=str(own))
        [payload] = host.api.named("model_download_required")
        assert payload["size_bytes"] == 0
        assert payload["strata_model"]["gguf_dir"] == str(own)

    @pytest.mark.asyncio
    async def test_every_choice_is_asked_not_once_per_session_and_a_dont_remind_me_does_not_hide_it(
            self, host, control):
        host.settings.set_model_download_declined("strata-model")
        await _ask(host)
        await _ask(host, family="swift", model="IQ2_XS")
        asked = [p["strata_model"]["model"] for p in host.api.named("model_download_required")]
        assert asked == ["IQ3_S", "IQ2_XS"]

    @pytest.mark.asyncio
    async def test_a_model_already_installed_is_answered_with_no_dialog(self, host, control):
        _run_configs(host).mkdir(parents=True)
        written = _run_configs(host) / "strata-iq3_s.json"
        written.write_text("{}")
        out = await _ask(host)
        assert out["status"] == "success" and out["run_config"] == str(written)
        assert host.api.named("model_download_required") == []

    @pytest.mark.asyncio
    async def test_a_second_install_while_one_runs_gets_the_already_downloading_answer(self, host, control):
        assert host.model_download_service.claim("strata-model")
        out = await _ask(host)
        assert out["status"] == "error" and "already being downloaded" in out["error"]
        assert host.api.named("model_download_required") == []
        host.model_download_service.release("strata-model")
        assert (await _ask(host))["asked"] is True

    @pytest.mark.asyncio
    async def test_control_not_installed_is_refused_with_the_reason_and_creates_nothing(
            self, host, tmp_path, monkeypatch):
        missing = tmp_path / "bin" / "strata" / "v0.2.9" / "x"
        monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: missing)
        out = await _ask(host)
        assert out["status"] == "error" and "not installed" in out["message"] and str(missing) in out["message"]
        assert host.api.named("model_download_required") == []
        assert not missing.exists() and not (host.folder / "strata").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs, words", [
        ({"model": "NOPE"}, "does not offer"),
        ({"model": "TXT", "images": True}, "cannot read pictures"),
        ({"gguf_dir": "relative/dir"}, "absolute folder"),
        ({"gguf_dir": "C:/definitely/not/here" if os.name == "nt" else "/definitely/not/here"}, "absolute folder"),
    ])
    async def test_a_request_the_table_or_the_folder_cannot_back_is_refused_before_the_dialog(
            self, host, control, kwargs, words):
        out = await _ask(host, **kwargs)
        assert out["status"] == "error" and words in out["message"]
        assert host.api.named("model_download_required") == []


class TestWhatIsRun:
    @pytest.mark.asyncio
    async def test_the_confirm_runs_setup_with_exactly_these_arguments(self, host, control):
        await _ask(host)
        assert (await host.model_download_service.download_model("strata-model"))["status"] == "success"
        seen = _seen(control)
        assert seen["argv0"] == str(control / "setup.py")
        assert seen["argv"] == ["--yes", "--no-start", "--family", "qwen", "--model", "IQ3_S",
                                "--vision", "yes", "--data-dir", str(host.folder / "strata")]
        assert Path(seen["cwd"]).resolve() == control.resolve()
        assert seen["stdin_at_eof"] is True
        assert seen["isolated"] == 0, "setup.py finds strata_control beside itself; -I would hide it"

    @pytest.mark.asyncio
    async def test_pictures_off_is_vision_no_and_a_folder_of_files_is_gguf_dir(self, host, control, tmp_path):
        own = tmp_path / "own-files"
        own.mkdir()
        await _ask(host, family="swift", model="IQ2_XS", images=False, gguf_dir=str(own))
        await host.model_download_service.download_model("strata-model")
        assert _seen(control)["argv"] == ["--yes", "--no-start", "--family", "swift", "--model", "IQ2_XS",
                                          "--vision", "no", "--data-dir", str(host.folder / "strata"),
                                          "--gguf-dir", str(own)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("images, own", [(True, False), (False, False), (True, True), (False, True)])
    async def test_the_draft_layer_is_never_switched_off_whatever_the_choice(
            self, host, control, tmp_path, images, own):
        folder = tmp_path / "own-files"
        folder.mkdir()
        await _ask(host, images=images, gguf_dir=str(folder) if own else None)
        await host.model_download_service.download_model("strata-model")
        argv = _seen(control)["argv"]
        assert "--mtp" not in argv and "off" not in argv, "DPC leaves the draft layer on, Strata's default"

    @pytest.mark.asyncio
    async def test_the_environment_has_none_of_ours_and_exactly_the_four_strata_settings(
            self, host, control, tmp_path, monkeypatch):
        monkeypatch.setenv("STRATA_BAIT", "leak")
        monkeypatch.setenv("strata_no_acquire", "1")  # Windows names are case-blind
        monkeypatch.setenv("STRATA_INSTALL_DIR", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venv"))
        await _ask(host)
        await host.model_download_service.download_model("strata-model")
        seen = _seen(control)
        assert {k.upper() for k in seen["strata_env"]} == {
            "STRATA_COMPONENTS_DIR", "STRATA_SETTINGS_FILE", "STRATA_CONTROL_INSTALL", "STRATA_INSTALL_DIR"}
        env = {k.upper(): v for k, v in seen["strata_env"].items()}
        assert env["STRATA_CONTROL_INSTALL"] == "1"
        assert env["STRATA_INSTALL_DIR"] == str(_run_configs(host))
        assert env["STRATA_SETTINGS_FILE"] == str(host.folder / "strata" / "settings.json")
        assert env["STRATA_COMPONENTS_DIR"] == str(strata_fetcher.components_dir(control))
        assert seen["env"]["PYTHONPATH"] is None and seen["env"]["VIRTUAL_ENV"] is None
        assert seen["env"]["PYTHONNOUSERSITE"] == "1" and seen["env"]["PYTHONUNBUFFERED"] == "1"

    @pytest.mark.asyncio
    async def test_the_install_folder_is_made_for_the_run_and_only_under_the_model_files_folder(
            self, host, control):
        await _ask(host)
        await host.model_download_service.download_model("strata-model")
        assert (_run_configs(host) / "strata-iq3_s.json").is_file()
        assert not (control / "strata-iq3_s.json").exists()

    @pytest.mark.asyncio
    async def test_the_install_has_no_memory_ceiling_and_a_long_timeout(self, host, control, monkeypatch):
        await _ask(host)
        calls = []
        real = process_module.run_supervised

        def spy(cmd, **kwargs):
            calls.append(kwargs)
            return real(cmd, **kwargs)

        monkeypatch.setattr(process_module, "run_supervised", spy)
        await host.model_download_service.download_model("strata-model")
        [kwargs] = calls
        assert kwargs["ceiling_mb"] == 0 and kwargs["timeout"] >= 6 * 3600
        assert kwargs["launcher"] == "strata" and kwargs["launcher_kind"] == "subsystem"
        assert kwargs["cr_ends_line"] is True


class TestProgressAndOutcome:
    def test_progress_is_read_from_the_redrawn_lines_of_both_pipes_and_only_grows(self, host, control):
        reports = []
        strata_model_fetcher.download(lambda done, total: reports.append((done, total)), _config(host))
        assert reports[0] == (1_000_000_000, 6_000_000_000), "the first redrawn line, against the table's figure"
        done = [d for d, _t in reports]
        totals = [t for _d, t in reports]
        assert done == sorted(done) and totals == sorted(totals)
        assert 4_000_000_000 in done, "the first shard reached its end, after a restart that went back to 2.0"
        assert reports[-1] == (11_900_000_000, 11_900_000_000), "shards 4+2, the draft layer 5, the picture reader 0.9"

    def test_the_hashing_of_a_file_is_not_bytes_fetched(self, host, control):
        reports = []
        strata_model_fetcher.download(lambda done, total: reports.append((done, total)), _config(host))
        assert not any(d == 8_000_000_000 for d, _t in reports), "the checking line would have added the shard again"
        assert max(t for _d, t in reports) == 11_900_000_000

    def test_files_in_place_have_no_table_total_so_the_total_is_what_setup_fetches(self, host, control, tmp_path):
        own = tmp_path / "own-files"
        own.mkdir()
        reports = []
        strata_model_fetcher.download(lambda d, t: reports.append((d, t)), _config(host, gguf_dir=str(own)))
        assert reports[0] == (1_000_000_000, 4_000_000_000)
        assert reports[-1] == (11_900_000_000, 11_900_000_000)

    @pytest.mark.asyncio
    async def test_success_ends_in_started_progress_and_completed_with_the_run_config(self, host, control):
        await _ask(host)
        out = await host.model_download_service.download_model("strata-model")
        assert out == {"status": "success", "model_name": "strata-model"}
        written = str(_run_configs(host) / "strata-iq3_s.json")
        assert host.api.named("model_download_started") == [{"model_name": "strata-model"}]
        assert host.api.named("model_download_completed") == [{"model_name": "strata-model", "run_config": written}]
        assert host.api.named("model_download_failed") == []
        assert host.model_download_service.get_status("strata-model")["state"] == "cached"
        progress = host.api.named("model_download_progress")
        assert progress and [p["bytes_done"] for p in progress] == sorted(p["bytes_done"] for p in progress)
        assert all(p["bytes_done"] <= p["total_bytes"] for p in progress)

    @pytest.mark.asyncio
    async def test_another_family_names_its_run_config_by_the_tag_rule(self, host, control):
        await _ask(host, family="swift", model="IQ2_XS")
        await host.model_download_service.download_model("strata-model")
        [done] = host.api.named("model_download_completed")
        assert Path(done["run_config"]).name == "strata-swift-iq2_xs.json"

    @pytest.mark.asyncio
    async def test_a_failure_gives_the_last_x_line_with_its_hint_and_the_exit_code(self, host, control):
        (control / "mode.txt").write_text("fail")
        await _ask(host)
        out = await host.model_download_service.download_model("strata-model")
        assert out["status"] == "error"
        [failed] = host.api.named("model_download_failed")
        assert "not enough free disk space in D:/models: need ~90 GB, 40 GB free" in failed["error"]
        assert "use --data-dir on a bigger drive" in failed["error"]
        assert "exit 1" in failed["error"] and "qwen IQ3_S" in failed["error"]
        assert host.api.named("model_download_completed") == []
        assert host.model_download_service.get_status("strata-model")["state"] == "failed"

    @pytest.mark.asyncio
    async def test_exit_5_is_a_refusal_with_setups_own_words(self, host, control):
        (control / "mode.txt").write_text("refused")
        await _ask(host)
        await host.model_download_service.download_model("strata-model")
        [failed] = host.api.named("model_download_failed")
        assert "exit 5" in failed["error"] and "refused" in failed["error"]
        assert "an install or build step (pip) is not allowed here" in failed["error"]

    def test_argparse_and_a_traceback_are_read_from_stderr(self, host, control):
        (control / "mode.txt").write_text("argparse")
        with pytest.raises(RuntimeError) as refused:
            strata_model_fetcher.download(None, _config(host))
        assert "exit 2" in str(refused.value) and "argument --model: invalid choice: 'x'" in str(refused.value)
        (control / "mode.txt").write_text("crash")
        with pytest.raises(RuntimeError) as crashed:
            strata_model_fetcher.download(None, _config(host))
        assert "RuntimeError: boom in setup" in str(crashed.value) and "exit 1" in str(crashed.value)

    @pytest.mark.asyncio
    async def test_setup_ending_well_without_its_run_config_is_a_failure_not_a_success(self, host, control):
        (control / "mode.txt").write_text("nofile")
        await _ask(host)
        out = await host.model_download_service.download_model("strata-model")
        assert out["status"] == "error" and "strata-iq3_s.json" in out["error"]
        assert host.api.named("model_download_completed") == []

    def test_setup_that_hangs_is_stopped_at_the_limit_and_says_so(self, host, control, monkeypatch):
        (control / "mode.txt").write_text("hang")
        monkeypatch.setattr(strata_model_fetcher, "_INSTALL_TIMEOUT_S", 2)
        with pytest.raises(RuntimeError) as stopped:
            strata_model_fetcher.download(None, _config(host))
        assert "ran past" in str(stopped.value) and "what is downloaded is kept" in str(stopped.value)

    @pytest.mark.asyncio
    async def test_control_missing_at_the_confirm_is_a_failed_event(self, host, control, tmp_path, monkeypatch):
        await _ask(host)
        monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: tmp_path / "gone")
        out = await host.model_download_service.download_model("strata-model")
        assert out["status"] == "error"
        [failed] = host.api.named("model_download_failed")
        assert "not installed" in failed["error"]

    @pytest.mark.asyncio
    async def test_the_confirm_runs_the_choice_of_its_own_dialog_not_a_default(self, host, control):
        await _ask(host, family="swift", model="IQ2_XS", images=False)
        await host.model_download_service.download_model("strata-model")
        argv = _seen(control)["argv"]
        assert argv[argv.index("--family") + 1] == "swift" and argv[argv.index("--vision") + 1] == "no"

    @pytest.mark.asyncio
    async def test_a_confirm_with_no_dialog_before_it_installs_nothing(self, host, control):
        out = await host.model_download_service.download_model("strata-model")
        assert out["status"] == "error" and "no Strata model was chosen" in out["error"]
        assert not (control / "seen.json").exists()


class TestIsPresent:
    def test_present_means_the_run_config_of_that_size_is_in_the_run_configs_folder(self, host, control):
        config = _config(host)
        assert strata_model_fetcher.is_present(config) is False
        _run_configs(host).mkdir(parents=True)
        (_run_configs(host) / "strata-txt.json").write_text("{}")
        assert strata_model_fetcher.is_present(config) is False, "another size's run config"
        (_run_configs(host) / "strata-iq3_s.json").write_text("{}")
        assert strata_model_fetcher.is_present(config) is True
        assert strata_model_fetcher.is_present(_config(host, images=False)) is True, "the size, not the picture choice"

    def test_a_family_with_a_tag_is_found_by_tag_plus_size(self, host, control):
        _run_configs(host).mkdir(parents=True)
        (_run_configs(host) / "strata-swift-iq2_xs.json").write_text("{}")
        assert strata_model_fetcher.is_present(_config(host, family="swift", model="IQ2_XS")) is True
        assert strata_model_fetcher.is_present(_config(host)) is False

    def test_a_moved_pin_is_read_again_not_answered_from_the_old_table(self, host, control, tmp_path, monkeypatch):
        assert strata_model_fetcher.size_bytes(_config(host)) == 6_000_000_000
        newer = tmp_path / "control-newer"
        shutil.copytree(control, newer)
        moved = json.loads(json.dumps(TABLE))
        moved[0]["sizes"][0]["download_gb"] = 9.5
        (newer / "table.json").write_text(json.dumps(moved), encoding="utf-8")
        monkeypatch.setattr(strata_fetcher, "install_root", lambda *a, **k: newer)
        assert strata_model_fetcher.size_bytes(_config(host)) == 9_500_000_000

    def test_size_and_location_come_from_the_table_and_the_model_files_folder(self, host, control):
        assert strata_model_fetcher.size_bytes(_config(host)) == 6_000_000_000
        assert strata_model_fetcher.size_bytes(_config(host, model="TXT", images=False)) == 2_000_000_000
        assert strata_model_fetcher.location(_config(host)) == str(host.folder / "strata")


class TestTheFailureReasonIsReadFromWhatSetupPrinted:
    def test_the_last_x_line_wins_and_a_hint_needs_its_indent(self):
        out = "  [X]  first\n       first hint\n\n  [X]  second\nnot a hint\n"
        assert strata_model_fetcher._failure_reason(out, "") == "second"
        assert strata_model_fetcher._failure_reason("  [X]  a\n       b\n", "") == "a (b)"

    def test_without_an_x_line_the_tail_of_stderr_then_stdout_is_given(self):
        assert strata_model_fetcher._failure_reason("a\nb\nc\nd\n", "") == "b | c | d"
        assert strata_model_fetcher._failure_reason("a\n", "e1\ne2\n") == "e1 | e2"
        assert strata_model_fetcher._failure_reason("", "") == "no message"
