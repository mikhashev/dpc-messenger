"""A missing Strata pin is offered for download, with its progress, never fetched inside a start.

B2 step 3, after the llama.cpp pin (test_a_missing_engine_pin_is_offered_…). A
`strata` alias with no `strata_install_dir` and no pin on disk used to run
`ensure_install` from `ensure_running`: Control and an engine, ~1 GB, while the
call waited. Now the owner is asked once under the name `strata` and the start
refuses before the card; a peer's call, or a start begun for a peer, neither
asks nor fetches. The size is Control plus the largest engine package of the
platform in the pinned feed (`size_source` "package_max"), because Strata's updater
picks the package by the card. The download runs the updater with
`--output jsonl` and turns its progress lines into one bar; the updater's home
lies inside `<root>.download`, never in the system temp folder. Nothing here
reaches the network, a real updater or the real ~/.dpc.
"""

import asyncio
import json
import logging
import sys
import tempfile
import time
import threading
from pathlib import Path

import pytest

from dpc_client_core import model_download_service as mds
from dpc_client_core.dpc_agent.tools import process
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import local_engine_pin_fetcher as pin_mod
from dpc_client_core.managers import strata_fetcher as sf
from dpc_client_core.managers.llama_server_supervisor import PinNotInstalledError
from dpc_client_core.managers.strata_supervisor import PeerStartRefused, StrataSupervisor, serving_a_peer
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.p2p_coordinator import _engine_unavailable_cause
from dpc_client_core.providers.strata_provider import StrataProvider
from dpc_client_core.settings import Settings
from tests.test_the_strata_install_is_fetched_by_its_pin_and_its_engine_is_checked_against_it import _Env

ALIAS = "flash next"
NAME = "strata"
PLAT = "windows-x86_64"
EXACT = 627_812_560  # the cuda13 package of v0.2.11 on Windows: what the updater would report
LABEL = f"engine {sf.STRATA_ENGINE_VERSION} {PLAT}-cuda-cuda13"


def _event(**fields) -> str:
    """One line exactly as Strata's `update --output jsonl` prints it (update_cmd.py `_emit_factory`)."""
    return json.dumps({"schema_version": 1, **fields}, ensure_ascii=False)


def _progress(done: int, total: int = EXACT) -> str:
    return _event(event="progress", phase="download", component="engine", label=LABEL,
                  done=done, total=total, percent=int(done * 100 / total), bytes_per_second=52_000_000)


FIXTURE_LINES = [
    _event(event="phase", phase="download", message=f"Downloading {LABEL} (621 MB)", component="engine"),
    _progress(0),
    "not json at all",
    _progress(100_000_000),
    "{broken",
    _event(event="progress", phase="download", component="control", label="Control 0.1.57",
           done=99, total=100, percent=99, bytes_per_second=1),
    _event(event="progress", phase="download", component="engine", label=LABEL,
           done="300000000", total=EXACT, percent=48, bytes_per_second=1),
    _progress(300_000_000),
    _progress(50_000_000),  # a download Strata restarted from 0: the bar holds
    _progress(EXACT),
    _event(event="phase", phase="done", message="Engine 0.1.57 is installed", component="engine"),
]


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty DPC_HOME for the supervisors and the fetchers, this platform's rows, no network."""
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    for mod in (base_mod, sf, pin_mod):
        monkeypatch.setattr(mod, "DPC_HOME", dpc)
    monkeypatch.setattr(sf, "platform_tag", lambda: PLAT)

    def _no_network(*a, **kw):
        raise AssertionError("urlopen reached inside the test")

    monkeypatch.setattr(pin_mod.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", None)
    return dpc


@pytest.fixture
def service(tmp_path, home, monkeypatch):
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)
    return svc


@pytest.fixture
def sup(home, tmp_path):
    """A pin-mode supervisor whose card claim and launch are recorded, never real."""
    s = StrataSupervisor(ALIAS, {"strata_base_config": "strata-base.json", "conversation_cache_mib": 10240})
    s.claims, s.launched = [], []
    real_claim = s._claim_card
    python = tmp_path / "python.exe"
    python.write_bytes(b"")

    def _claim():
        s.claims.append(1)
        real_claim()

    async def _launch(binary, cache_type=None):
        s.launched.append(binary)
        return {"modalities": {}}

    s._claim_card = _claim
    s._launch = _launch
    s.python_path = lambda: python
    s.server_script = lambda: python
    return s


def _max_size() -> int:
    return sf.PLATFORM_ASSETS[PLAT]["size"] + sf.ENGINE_SIZE_MAX[PLAT]


def _refusal(sup):
    with pytest.raises(PinNotInstalledError) as info:
        asyncio.run(sup.ensure_running())
    return info.value


# --- the sizes ---------------------------------------------------------------


def test_the_size_is_control_plus_the_largest_engine_of_the_platform():
    for plat, row in sf.PLATFORM_ASSETS.items():
        assert sf.ENGINE_SIZE_MAX[plat] > 0 and sf.ENGINE_INSTALLED_BYTES_MAX[plat] > sf.ENGINE_SIZE_MAX[plat]
        assert row["installed_bytes"] > row["size"]
    # The largest Windows package (cuda12), not the one this card would get (cuda13).
    assert sf.ENGINE_SIZE_MAX[PLAT] == 915_324_388 > EXACT
    assert sf.SIZE_SOURCE == "package_max"


# --- the owner's start asks once and refuses before the card -----------------


def test_the_owners_start_asks_once_and_refuses_before_the_card(sup, service):
    err = _refusal(sup)

    (name, payload), = service.local_api.events
    assert name == "model_download_required"
    assert payload["model_name"] == NAME
    assert payload["size_bytes"] == _max_size() == sf.size_bytes()
    assert payload["installed_bytes"] == (
        sf.PLATFORM_ASSETS[PLAT]["installed_bytes"] + sf.ENGINE_INSTALLED_BYTES_MAX[PLAT]) == sf.installed_bytes()
    assert payload["size_source"] == "package_max"
    assert payload["size_measured_on"] is None  # the feed's own sizes, held with the pin
    assert payload["on_decline"] == "block"
    assert payload["cache_path"] == str(sf.install_root())
    assert payload["provider_alias"] == ALIAS
    assert isinstance(payload["free_bytes"], int)
    assert err.download_name == NAME
    assert "one-time download of up to 1.0 GB" in str(err)
    assert "the owner has been asked" in str(err) and "Do not retry until it has finished" in str(err)
    assert "dialog" not in str(err).lower()
    assert sup.claims == [] and sup not in base_mod._CARD_CLAIMS
    assert sup.launched == []

    _refusal(sup)
    assert len(service.local_api.events) == 1, "a second start sends no second event"


def test_a_running_download_is_named_with_its_percent(sup, service):
    service._emitted_this_session.add(NAME)
    service._set_status(NAME, "downloading")
    service._status[NAME]["progress_percent"] = 42.0

    err = _refusal(sup)

    assert "being downloaded now (42% of up to 1.0 GB)" in str(err)
    assert service.local_api.events == []


def test_a_declined_download_says_where_it_can_be_started(sup, service):
    _refusal(sup)
    service.decline(NAME, remember=False)

    err = _refusal(sup)

    assert "the owner has declined; it can be started from the D-PC Messenger app" in str(err)


def test_a_restart_can_ask_again_for_the_pin(service):
    service.decline(NAME, remember=True)
    assert asyncio.run(service.reset_decline(NAME)) is True
    (name, payload), = service.local_api.events
    assert name == "model_download_required" and payload["on_decline"] == "block"


def test_draining_that_begins_while_it_resolves_refuses_the_start(sup, home, monkeypatch):
    def _resolve(config):
        sup._draining = True
        return None

    monkeypatch.setattr(sf, "resolve_install", _resolve)
    monkeypatch.setattr(sf, "ensure_install", lambda config: pytest.fail("installed while draining"))
    with pytest.raises(base_mod.LlamaServerError, match="is draining"):
        asyncio.run(sup.ensure_running())
    assert sup.claims == []


# --- a peer never asks and never fetches ------------------------------------


def _no_install(monkeypatch):
    fetched = []
    monkeypatch.setattr(sf, "ensure_install", lambda *a, **k: fetched.append(1))
    monkeypatch.setattr(sf, "install_pin", lambda *a, **k: fetched.append(1))
    return fetched


def test_a_peers_call_neither_asks_nor_fetches(sup, service, monkeypatch):
    fetched = _no_install(monkeypatch)

    with serving_a_peer("dpc-node-peer"):
        with pytest.raises(PeerStartRefused):
            asyncio.run(sup.ensure_running())

    assert service.local_api.events == [] and fetched == [] and sup.claims == []


@pytest.mark.parametrize("bound", [True, False])
def test_a_start_begun_for_a_peer_neither_asks_nor_fetches(sup, home, tmp_path, monkeypatch, caplog, bound):
    fetched = _no_install(monkeypatch)
    svc = None
    if bound:
        svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())
        monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)

    async def run():
        assert sup.start_for_peer("dpc-node-peer")
        await sup.peer_start_task

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.managers.strata_supervisor"):
        asyncio.run(run())

    assert fetched == [] and sup.claims == [] and sup.launched == []
    if svc is not None:
        assert svc.local_api.events == []
    assert "a peer's request does not fetch it" in caplog.text


def test_the_refusal_counts_as_the_engine_being_unavailable_to_a_peer():
    err = PinNotInstalledError("not installed", NAME)
    wrapped = RuntimeError("strata provider failed")
    wrapped.__cause__ = err
    assert _engine_unavailable_cause(wrapped, strata=True)
    assert StrataProvider._is_retryable(err) is False


# --- no consent service: the install runs in a thread ------------------------


def test_with_no_service_bound_the_install_runs_off_the_loop_thread(sup, home, monkeypatch):
    seen = {}

    def _ensure(config):
        seen["thread"] = threading.get_ident()
        return home / "root"

    monkeypatch.setattr(sf, "ensure_install", _ensure)

    async def run():
        seen["loop"] = threading.get_ident()
        return await sup.ensure_running()

    assert asyncio.run(run()) == {"modalities": {}}
    assert seen["thread"] != seen["loop"]
    assert len(sup.launched) == 1 and sup.claims


# --- the download: progress, the updater's home -----------------------------


class _ProgressEnv(_Env):
    """The faked install world of the step-2 test; the updater also prints `FIXTURE_LINES`."""

    def __init__(self, monkeypatch, tmp_path, lines):
        super().__init__(monkeypatch, tmp_path)
        self.lines = lines
        self.homes = []

    def _run(self, cmd, **kw):
        self.homes.append(Path(kw["env"]["LOCALAPPDATA"]).parent)
        assert self.homes[-1].is_dir()
        hook = kw.get("on_stdout")
        if hook is not None:
            for line in self.lines:
                hook(line)
        return super()._run(cmd, **kw)


@pytest.fixture
def no_system_temp(monkeypatch):
    def _refuse(*a, **k):
        raise AssertionError("the system temp folder was used")

    for name in ("TemporaryDirectory", "mkdtemp", "mkstemp", "NamedTemporaryFile"):
        monkeypatch.setattr(tempfile, name, _refuse)


def test_progress_lines_make_one_bar_that_reaches_the_exact_total(monkeypatch, tmp_path, no_system_temp):
    env = _ProgressEnv(monkeypatch, tmp_path, FIXTURE_LINES)
    control = sf.PLATFORM_ASSETS[PLAT]["size"]  # the test's archive, served by _Env
    bar = []

    root = sf.install_pin(lambda done, total: bar.append((done, total)))

    assert root == sf.install_root()
    cmd = env.calls[0]["cmd"]
    assert cmd[cmd.index("--output") + 1] == "jsonl"
    dones = [d for d, _ in bar]
    assert dones == sorted(dones), "the bar never goes back"
    assert bar[0][1] == control + sf.ENGINE_SIZE_MAX[PLAT], "Control counts against the feed's maximum"
    assert all(t == control + EXACT for d, t in bar if d > control), "the engine counts against its exact total"
    assert (control + 100_000_000, control + EXACT) in bar
    assert (control + 300_000_000, control + EXACT) in bar
    assert bar[-1] == (control + EXACT, control + EXACT)


def test_with_no_progress_line_the_bar_jumps_at_the_end(monkeypatch, tmp_path):
    _ProgressEnv(monkeypatch, tmp_path, ["note: nothing machine-readable"])
    control = sf.PLATFORM_ASSETS[PLAT]["size"]
    bar = []

    sf.install_pin(lambda done, total: bar.append((done, total)))

    assert bar[-1] == (control + sf.ENGINE_SIZE_MAX[PLAT],) * 2
    assert all(t == control + sf.ENGINE_SIZE_MAX[PLAT] for _, t in bar)


def test_the_updaters_home_is_inside_the_pins_download_folder_and_gone_after(monkeypatch, tmp_path, no_system_temp):
    env = _ProgressEnv(monkeypatch, tmp_path, [])

    root = sf.install_pin()

    (home_dir,) = env.homes
    assert home_dir.parent == root.with_name(root.name + ".download")
    assert not home_dir.exists() and not home_dir.parent.exists()
    assert env.leftovers() == [PLAT]


def test_a_failed_updater_leaves_no_home_behind(monkeypatch, tmp_path, no_system_temp):
    env = _ProgressEnv(monkeypatch, tmp_path, [])
    env.exit = (6, _event(ok=False, error={"code": 6, "message": "no network"}) + "\n")

    with pytest.raises(RuntimeError, match="no network"):
        sf.install_pin()

    assert not env.homes[0].exists()
    assert env.leftovers() == []


def test_a_progress_line_is_not_read_for_the_result():
    stdout = "\n".join([_progress(5)]) + "\n"
    assert sf._last_json_line(stdout) is None
    assert sf._last_json_line(stdout + _event(ok=True) + "\n") == {"schema_version": 1, "ok": True}


def test_the_download_sends_throttled_progress_to_the_exact_total(tmp_path, monkeypatch):
    env = _ProgressEnv(monkeypatch, tmp_path, FIXTURE_LINES)
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc")
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)
    svc._clock = lambda: 100.0  # one instant: only the first report and the total pass
    control = sf.PLATFORM_ASSETS[PLAT]["size"]

    async def run():
        result = await svc.download_model(NAME)
        await asyncio.sleep(0.05)
        return result

    assert asyncio.run(run()) == {"status": "success", "model_name": NAME}
    names = [n for n, _ in svc.local_api.events]
    assert names[0] == "model_download_started" and names[-1] == "model_download_completed"
    progress = [(p["bytes_done"], p["total_bytes"]) for n, p in svc.local_api.events
                if n == "model_download_progress"]
    assert len(progress) == 2
    assert progress[-1] == (control + EXACT, control + EXACT)
    assert sf.is_present() and len(env.calls) == 1


# --- the line hook, through the real process module -------------------------


def test_run_supervised_hands_each_stdout_line_over_while_the_child_runs(tmp_path):
    """The child prints a progress line, then waits for the file the hook writes:
    only a hook called while the child still runs lets it see that file."""
    go = tmp_path / "go"
    script = tmp_path / "child.py"
    script.write_text(
        "import pathlib, sys, time\n"
        f"print({_progress(10)!r}, flush=True)\n"
        f"go = pathlib.Path({str(go)!r})\n"
        "deadline = time.monotonic() + 20\n"
        "while not go.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "sys.stdout.write('saw go\\r\\n' if go.exists() else 'no go\\n')\n"
        "sys.stdout.write('tail without newline')\n",
        encoding="utf-8",
    )
    lines = []

    def hook(line):
        lines.append(line)
        if sf._engine_progress(line):
            go.write_text("1", encoding="utf-8")

    run = process.run_supervised([sys.executable, str(script)], launcher="test(stdout lines)", timeout=60, on_stdout=hook)

    assert run.returncode == 0
    assert lines == [_progress(10), "saw go", "tail without newline"]
    assert "saw go" in run.stdout and _progress(10) in run.stdout


# --- the sizes are the feed rows' maxima and minima --------------------------

_ROWS = json.loads((Path(__file__).parent / "fixtures" / "strata_v0.2.11_feed_packages.json").read_text(encoding="utf-8"))


def test_the_pinned_sizes_are_the_extremes_of_the_feed_rows_of_the_pinned_release():
    assert _ROWS["release_tag"] == sf.STRATA_RELEASE_TAG
    engines = {}
    for row in _ROWS["engine"]:
        assert row["version"] == sf.STRATA_ENGINE_VERSION
        engines.setdefault(row["platform"], []).append(row)
    assert set(engines) == set(sf.PLATFORM_ASSETS)
    for plat, rows in engines.items():
        # Both bounds are one row each, so "up to" names a package that exists.
        largest = max(rows, key=lambda r: r["size"] + r["installed_bytes"])
        assert (sf.ENGINE_SIZE_MAX[plat], sf.ENGINE_INSTALLED_BYTES_MAX[plat]) == (
            largest["size"], largest["installed_bytes"]), plat
        # The minima: the package with the smallest download plus unpacked size.
        smallest = min(rows, key=lambda r: r["size"] + r["installed_bytes"])
        pair = (sf.ENGINE_SIZE_MIN[plat], sf.ENGINE_INSTALLED_BYTES_MIN[plat])
        assert pair == (smallest["size"], smallest["installed_bytes"]), plat
        assert pair in [(r["size"], r["installed_bytes"]) for r in rows], plat
    for row in _ROWS["control"]:
        pinned = sf.PLATFORM_ASSETS[row["platform"]]
        assert (pinned["name"], pinned["sha256"], pinned["size"], pinned["installed_bytes"]) == (
            row["url"], row["sha256"], row["size"], row["installed_bytes"])
    # "package_max" is true of the size only while some platform offers more than one package.
    assert any(len(rows) > 1 for rows in engines.values())
    assert sf.SIZE_SOURCE == "package_max"


# --- before the download: only the smallest package can refuse ---------------


def _least_and_most():
    least = (sf.size_bytes_min() + sf.installed_bytes_min()) * 1.1
    most = (sf.size_bytes() + sf.installed_bytes()) * 1.1
    return int(least), int(most)


def test_the_payload_carries_the_smallest_package_too(service):
    asyncio.run(service.maybe_emit_required(NAME, purpose="p", consequence_if_declined="c", on_decline="block"))
    (_, payload), = service.local_api.events
    assert payload["size_bytes_min"] == sf.PLATFORM_ASSETS[PLAT]["size"] + sf.ENGINE_SIZE_MIN[PLAT]
    assert payload["installed_bytes_min"] == (
        sf.PLATFORM_ASSETS[PLAT]["installed_bytes"] + sf.ENGINE_INSTALLED_BYTES_MIN[PLAT])


def test_free_space_below_the_smallest_package_refuses_the_download(service, monkeypatch):
    least, _ = _least_and_most()
    monkeypatch.setattr(mds, "_free_bytes", lambda path: least - 1)
    monkeypatch.setattr(sf, "install_pin", lambda *a, **k: pytest.fail("downloaded without the space"))

    result = asyncio.run(service.download_model(NAME))

    assert result["status"] == "error" and "not enough free disk space" in result["error"]
    assert [n for n, _ in service.local_api.events] == ["model_download_failed"]


def test_free_space_between_the_smallest_and_the_largest_package_starts_the_download(service, monkeypatch):
    least, most = _least_and_most()
    assert least < most
    monkeypatch.setattr(mds, "_free_bytes", lambda path: (least + most) // 2)
    installs = []
    monkeypatch.setattr(sf, "install_pin", lambda progress=None: installs.append(1) or Path("C:/strata"))

    assert asyncio.run(service.download_model(NAME))["status"] == "success"
    assert installs == [1]


def test_the_llama_pin_keeps_its_one_size_check(service, monkeypatch):
    from dpc_client_core.managers import llama_server_fetcher as llama

    assert mds._stated_minima(llama) == (None, None)
    asyncio.run(service.maybe_emit_required(llama.DOWNLOAD_NAME, purpose="p", consequence_if_declined="c"))
    payload = service.local_api.events[-1][1]
    assert payload["size_bytes_min"] is None and payload["installed_bytes_min"] is None


# --- Strata's own exact space check: exit 6, said in DPC's words -------------

# Strata's report for a package that does not fit (update_cmd.py `run` prints the
# run_update result; updater.py `_free_space_check` words the error, exit code 6).
NO_SPACE_ERROR = ("not enough free space on C:\\: 1210 MiB free, 1498 MiB needed. "
                  "Free up some space on C:\\ and run the update again")
NO_SPACE_REPORT = _event(
    ok=False, feed={"source": "https://github.com/Niko1221/Strata/releases/download/v0.2.11", "sequence": 121,
                    "channel": "stable", "key_id": "5d1bc23abbeffd0d"},
    results=[{"component": "engine", "status": "failed", "changed": False, "error": NO_SPACE_ERROR,
              "exit_code": 6, "installed_before": None}],
    exit_code=6,
)


def _no_space_env(monkeypatch, tmp_path):
    env = _ProgressEnv(monkeypatch, tmp_path, [])
    phase = _event(event="phase", phase="update", message="Checking the update feed", component="update")
    env.exit = (6, phase + "\n" + NO_SPACE_REPORT + "\n")
    return env


def test_strata_s_own_space_refusal_is_said_plainly_and_leaves_nothing(monkeypatch, tmp_path):
    env = _no_space_env(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as err:
        sf.install_pin(lambda done, total: None)

    assert str(err.value) == (
        "not enough free disk space for the Strata engine: needs about 1.6 GB, 1.3 GB free on C:\\")
    assert env.leftovers() == [], "no .download, .staging or root left behind"
    assert not env.homes[0].exists()


def test_another_exit_6_keeps_strata_s_own_words(monkeypatch, tmp_path):
    env = _ProgressEnv(monkeypatch, tmp_path, [])
    report = json.loads(NO_SPACE_REPORT)
    report["results"][0]["error"] = "the download of x was interrupted"
    env.exit = (6, json.dumps(report) + "\n")

    with pytest.raises(RuntimeError, match=r"exit 6, failed\): the download of x was interrupted"):
        sf.install_pin()


def test_the_space_refusal_reaches_the_failed_event(tmp_path, monkeypatch):
    _no_space_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)  # DPC's pre-check passes
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())

    result = asyncio.run(svc.download_model(NAME))

    assert result["status"] == "error" and "not enough free disk space for the Strata engine" in result["error"]
    (payload,) = [p for n, p in svc.local_api.events if n == "model_download_failed"]
    assert payload["error"] == result["error"]
    assert svc.get_status(NAME)["state"] == "failed"


def test_unknown_free_space_refuses_nothing(service, monkeypatch):
    monkeypatch.setattr(mds, "_free_bytes", lambda path: None)
    installs = []
    monkeypatch.setattr(sf, "install_pin", lambda progress=None: installs.append(1) or Path("C:/strata"))

    assert asyncio.run(service.download_model(NAME))["status"] == "success"
    assert installs == [1]
