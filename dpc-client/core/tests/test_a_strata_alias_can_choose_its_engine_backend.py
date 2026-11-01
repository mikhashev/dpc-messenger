"""A Strata alias can accept Strata's own engine choice or name another (B2 step 3, commit 3b).

`strata_engine_backend` holds the word Strata's updater takes as `--backend`: a family of
Strata's BACKEND_MATRIX, or for AMD `hip-<build>`; empty lets Strata pick by the card. The
values offered on a platform are the matrix's families that the pinned feed has a package
of (v0.2.9 has no cpu package), and the AMD builds of those packages; CUDA 12 or 13 stays
Strata's pick from the driver. The word reaches the updater as `--backend`; an installed
engine of another backend is, for that alias, a missing pin: offered for download with the
chosen package's sizes, never run. Nothing here reaches the network, a real updater or the
real ~/.dpc.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import model_download_service as mds
from dpc_client_core.dpc_agent.tools import process
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import strata_fetcher as sf
from dpc_client_core.managers.llama_server_supervisor import PinNotInstalledError
from dpc_client_core.managers.strata_supervisor import STRATA_PATH_KEYS, StrataSupervisor, strata_config_errors
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers import strata_provider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.settings import Settings
from tests.test_strata_provider import _config
from tests.test_strata_supervisor import _install
from tests.test_the_strata_install_is_fetched_by_its_pin_and_its_engine_is_checked_against_it import _Env, _manifest

KEY = "strata_engine_backend"
WIN = "windows-x86_64"
ALIAS = "flash next"
_ROWS = json.loads((Path(__file__).parent / "fixtures" / "strata_v0.2.9_feed_packages.json").read_text(encoding="utf-8"))
_MATRIX = {  # Strata v0.2.9 strata_control/targets.py BACKEND_MATRIX, read from the pinned Control
    "metal": ("macos-arm64",),
    "cuda": ("windows-x86_64", "linux-x86_64", "linux-aarch64"),
    "hip": ("windows-x86_64", "linux-x86_64"),
    "sycl": ("linux-x86_64",),
    "cpu": ("macos-arm64", "macos-x86_64", "windows-x86_64", "linux-x86_64"),
}


def _feed_rows(plat):
    return [r for r in _ROWS["engine"] if r["platform"] == plat]


def _cfg(value):
    return {KEY: value}


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", None)
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


# --- the values a platform may take -----------------------------------------------------


def test_the_package_rows_are_the_feed_rows_of_the_pinned_release():
    for plat in sf.PLATFORM_ASSETS:
        assert sorted(sf._ENGINE_PACKAGES[plat]) == sorted(
            (r["backend"], r["variant"], r["size"], r["installed_bytes"]) for r in _feed_rows(plat)), plat
    assert sf._BACKEND_MATRIX == _MATRIX


@pytest.mark.parametrize("plat", sorted(sf.PLATFORM_ASSETS))
def test_the_allowed_values_are_the_matrix_families_with_a_package_and_the_amd_builds(plat):
    rows = _feed_rows(plat)
    families = {f for f, platforms in _MATRIX.items() if plat in platforms} & {r["backend"] for r in rows}
    builds = {f"hip-{r['variant']}" for r in rows if r["backend"] == "hip"}
    allowed = sf.engine_backends(plat)
    assert set(allowed) == families | builds
    assert len(allowed) == len(set(allowed))
    assert "cpu" not in allowed, "v0.2.9 publishes no cpu package"
    assert not any(v.startswith("cuda") and v != "cuda" for v in allowed), "CUDA 12 or 13 is Strata's pick"


def test_the_allowed_values_on_each_platform_today():
    assert sf.engine_backends(WIN) == ["cuda", "hip", "hip-rdna2", "hip-rdna3", "hip-rx9070"]
    assert sf.engine_backends("linux-x86_64") == [
        "cuda", "hip", "hip-halo", "hip-rx6000", "hip-rx7900", "hip-rx9070", "sycl"]
    assert sf.engine_backends("linux-aarch64") == ["cuda"]
    assert sf.engine_backends("macos-arm64") == ["metal"]


# --- normalisation and refusals ---------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (" CUDA ", "cuda"), ("hip-RDNA3", "hip-rdna3"), ("Hip", "hip"), ("", ""), (None, ""), ("  ", ""),
])
def test_the_value_is_normalised_as_strata_does(raw, expected):
    assert sf.resolve_engine_backend(_cfg(raw), WIN) == expected
    assert sf.resolve_engine_backend(None, WIN) == ""


@pytest.mark.parametrize("raw,words", [
    ("cpu", "Strata v0.2.9 publishes no cpu engine package for windows-x86_64"),
    ("sycl", "sycl is not a supported combination on windows-x86_64; it is supported on linux-x86_64. "
             "Unsupported combinations are not attempted"),
    ("metal", "metal is not a supported combination on windows-x86_64"),
    ("hip-halo", "publishes no hip-halo engine package for windows-x86_64"),
    ("vulkan", "unknown backend family 'vulkan'"),
    ("cuda13", "unknown backend family 'cuda13'; CUDA 12 or 13 is picked by Strata from the driver"),
    ("hip-", "unknown backend family 'hip-'"),
])
def test_a_value_the_platform_cannot_take_is_refused_with_the_reason(raw, words):
    with pytest.raises(ValueError) as err:
        sf.resolve_engine_backend(_cfg(raw), WIN)
    assert words in str(err.value)
    assert "choose one of cuda, hip, hip-rdna2, hip-rdna3, hip-rx9070" in str(err.value)


def _save_config(tmp_path, **extra):
    base = tmp_path / "strata-base.json"
    base.write_text("{}", encoding="utf-8")
    return {"type": "strata", "alias": "flash", "model": "m", "strata_base_config": str(base),
            "conversation_cache_mib": 10240, **extra}


def test_the_save_refuses_a_value_that_is_not_allowed_and_rewrites_nothing(tmp_path, monkeypatch):
    from dpc_client_core.service import CoreService

    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    bad = _save_config(tmp_path, **{KEY: "hip-halo"})
    before = dict(bad)
    errors = CoreService._validate_providers_config(
        SimpleNamespace(), {"default_provider": "flash", "providers": [bad]})
    assert any("strata_engine_backend 'hip-halo'" in e and "publishes no hip-halo engine package" in e
               for e in errors), errors
    assert bad == before
    for good in (" CUDA ", "hip-RDNA3", ""):
        config = _save_config(tmp_path, **{KEY: good})
        assert strata_config_errors(config, check_paths=True) == []
        assert config[KEY] == good, "the save normalises only as it reads, never writes another value"
    # The load (no check_paths) never drops an alias carried from another machine.
    assert strata_config_errors(_save_config(tmp_path, **{KEY: "sycl"})) == []


# --- a changed choice restarts the launcher ---------------------------------------------


def test_the_key_decides_a_restart_and_is_not_a_path():
    assert KEY in strata_provider._STRATA_FLAG_KEYS
    assert KEY not in STRATA_PATH_KEYS


def test_a_reload_with_a_changed_choice_restarts_and_an_unchanged_one_adopts(tmp_path):
    root = _install(tmp_path)
    first = StrataProvider("flash", _config(root, **{KEY: "cuda"}))
    first.supervisor.props = {"x": 1}
    same = StrataProvider("flash", _config(root, **{KEY: "cuda"}))
    assert same.supervisor is first.supervisor
    other = StrataProvider("flash", _config(root, **{KEY: "hip-rdna3"}))
    assert other.supervisor is not first.supervisor


def test_the_status_carries_the_values_this_machine_may_take(monkeypatch):
    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    status = asyncio.run(strata_provider.local_engine_status())
    assert status["engine_backends"] == sf.engine_backends(WIN)


# --- the updater gets --backend exactly when one is chosen ------------------------------


def _set_engine(world, backend, variant):
    world.folder = f"{sf.STRATA_ENGINE_VERSION}-{WIN}-{backend}" + (f"-{variant}" if variant else "")
    world.manifest = _manifest(backend=backend, variant=variant)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATA_ENGINE_BACKEND", "hip-rx9070")  # the parent's, never the updater's
    return _Env(monkeypatch, tmp_path)


def test_no_choice_passes_no_backend(world):
    sf.install_pin()
    (call,) = world.calls
    assert "--backend" not in call["cmd"]
    assert not any(k.upper() == "STRATA_ENGINE_BACKEND" for k in call["env"])


@pytest.mark.parametrize("raw,word", [(" CUDA ", "cuda"), ("hip-RDNA3", "hip-rdna3")])
def test_a_choice_reaches_the_updater_as_backend(world, raw, word):
    if word.startswith("hip"):
        _set_engine(world, "hip", "rdna3")
    sf.install_pin(config=_cfg(raw))
    (call,) = world.calls
    cmd = call["cmd"]
    assert cmd.count("--backend") == 1 and cmd[cmd.index("--backend") + 1] == word
    assert not any(k.upper() == "STRATA_ENGINE_BACKEND" for k in call["env"])


def test_an_engine_the_updater_installed_against_the_choice_is_refused(world):
    with pytest.raises(ValueError, match="is cuda \\(cuda13\\), not the chosen strata_engine_backend hip-rdna3"):
        sf.install_pin(config=_cfg("hip-rdna3"))
    assert world.leftovers() == []


# --- the installed engine is checked against the choice ---------------------------------


@pytest.mark.parametrize("installed,choice,matches", [
    (("hip", "rdna3"), "hip-rdna3", True),
    (("hip", "rdna3"), "HIP-rdna3 ", True),
    (("hip", "rdna3"), "hip-rdna2", False),
    (("hip", "rdna3"), "hip", True),
    (("hip", "rdna3"), "cuda", False),
    (("cuda", "cuda13"), "cuda", True),
    (("cuda", "cuda12"), "cuda", True),
    (("cuda", "cuda13"), "hip", False),
    (("cuda", "cuda13"), "", True),
])
def test_identity_compares_by_strata_s_rule(world, installed, choice, matches):
    _set_engine(world, *installed)
    root = sf.install_pin()
    assert (sf.resolve_install(_cfg(choice)) == root) is matches
    assert sf.is_present(_cfg(choice)) is matches
    folder = sf.engine_folder(root)
    if matches:
        assert sf.check_engine_identity(folder, backend=choice.strip().lower())["backend"] == installed[0]
    else:
        with pytest.raises(ValueError, match="not the chosen strata_engine_backend"):
            sf.check_engine_identity(folder, backend=choice.strip().lower())


def test_a_different_choice_reinstalls_the_chosen_engine(world):
    sf.install_pin()  # cuda13, Strata's pick
    _set_engine(world, "hip", "rdna3")
    root = sf.install_pin(config=_cfg("hip-rdna3"))
    assert len(world.calls) == 2
    assert sf.engine_folder(root).name.endswith("hip-rdna3")
    assert sf.resolve_install({}) == root and sf.resolve_install(_cfg("cuda")) is None


# --- the sizes follow the choice --------------------------------------------------------


def _pkg(plat, backend, variant):
    (row,) = [r for r in _feed_rows(plat) if r["backend"] == backend and r["variant"] == variant]
    return row


@pytest.mark.parametrize("plat,choice,largest,smallest,source", [
    (WIN, "", ("cuda", "cuda12"), ("hip", "rdna2"), "package_max"),
    (WIN, "cuda", ("cuda", "cuda12"), ("cuda", "cuda13"), "package_max"),
    (WIN, "hip", ("hip", "rx9070"), ("hip", "rdna2"), "package_max"),
    (WIN, "hip-rdna3", ("hip", "rdna3"), None, "package"),
    ("linux-x86_64", "hip-rx7900", ("hip", "rx7900"), None, "package"),
    ("linux-x86_64", "sycl", ("sycl", ""), None, "package"),
    ("linux-x86_64", "cuda", ("cuda", "cuda12"), ("cuda", "cuda13"), "package_max"),
    ("macos-arm64", "metal", ("metal", ""), None, "package"),
])
def test_sizes_and_size_source_per_choice(monkeypatch, plat, choice, largest, smallest, source):
    monkeypatch.setattr(sf, "platform_tag", lambda: plat)
    control = sf.PLATFORM_ASSETS[plat]
    config = _cfg(choice)
    big = _pkg(plat, *largest)
    assert sf.size_source(config) == source
    assert sf.size_bytes(config) == control["size"] + big["size"]
    assert sf.installed_bytes(config) == control["installed_bytes"] + big["installed_bytes"]
    if smallest is None:
        assert sf.size_bytes_min(config) is None and sf.installed_bytes_min(config) is None
    else:
        small = _pkg(plat, *smallest)
        assert sf.size_bytes_min(config) == control["size"] + small["size"]
        assert sf.installed_bytes_min(config) == control["installed_bytes"] + small["installed_bytes"]
    if not choice:
        assert sf.size_bytes() == sf.size_bytes(config) and sf.size_source() == "package_max"


def _service(tmp_path, config, monkeypatch):
    llm = SimpleNamespace(providers={ALIAS: SimpleNamespace(config=config)})
    svc = ModelDownloadService(llm_manager=llm, settings=Settings(tmp_path), local_api=_FakeLocalApi())
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)
    return svc


def test_the_consent_carries_the_chosen_packages_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    monkeypatch.setattr(sf, "DPC_HOME", tmp_path / "dpc")
    svc = _service(tmp_path, _cfg("hip-rdna3"), monkeypatch)
    asyncio.run(svc.maybe_emit_required("strata", purpose="p", consequence_if_declined="c",
                                        on_decline="block", provider_alias=ALIAS))
    (_, payload), = svc.local_api.events
    rdna3 = _pkg(WIN, "hip", "rdna3")
    assert payload["size_source"] == "package"
    assert payload["size_bytes"] == sf.PLATFORM_ASSETS[WIN]["size"] + rdna3["size"]
    assert payload["installed_bytes"] == sf.PLATFORM_ASSETS[WIN]["installed_bytes"] + rdna3["installed_bytes"]
    assert payload["size_bytes_min"] is None and payload["installed_bytes_min"] is None


def test_the_download_installs_the_engine_of_the_alias_the_dialog_carried(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    monkeypatch.setattr(sf, "DPC_HOME", tmp_path / "dpc")
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)
    config = _cfg("hip-rdna2")
    svc = _service(tmp_path, config, monkeypatch)
    seen = []
    monkeypatch.setattr(sf, "install_pin", lambda progress=None, config=None: seen.append(config) or Path("C:/s"))

    assert asyncio.run(svc.download_model("strata", ALIAS))["status"] == "success"
    assert seen == [config]


# --- an engine of another backend is offered, never run ---------------------------------


def test_a_mismatched_engine_is_offered_with_the_chosen_sizes_and_not_run(world, tmp_path, monkeypatch):
    sf.install_pin()  # cuda13 installed
    base = tmp_path / "strata-base.json"
    base.write_text("{}", encoding="utf-8")
    sup = StrataSupervisor(ALIAS, {"strata_base_config": str(base), "conversation_cache_mib": 10240,
                                   KEY: "hip-rdna3"})
    claims, launched = [], []
    sup._claim_card = lambda: claims.append(1)

    async def _launch(binary, cache_type=None):
        launched.append(binary)
        return {}

    sup._launch = _launch
    svc = _service(tmp_path, sup.config, monkeypatch)

    with pytest.raises(PinNotInstalledError) as err:
        asyncio.run(sup.ensure_running())

    (name, payload), = svc.local_api.events
    assert name == "model_download_required" and payload["provider_alias"] == ALIAS
    assert payload["size_source"] == "package"
    assert payload["size_bytes"] == sf.PLATFORM_ASSETS[WIN]["size"] + _pkg(WIN, "hip", "rdna3")["size"]
    assert "one-time download of about" in str(err.value), "an exact size is not said 'up to'"
    assert claims == [] and launched == []
    # The same install still runs an alias that leaves the choice to Strata, or chose cuda.
    assert sf.resolve_install({}) is not None and sf.resolve_install(_cfg("cuda")) is not None
    assert len(world.calls) == 1, "no fetch inside the start"


# --- one engine per install: never replaced under a running launcher -------------------
# Strata keeps engine folders side by side, but its updater prunes to the new and the
# previous one (updater.py prune_engines) and a start picks the newest of a family, not a
# build (engine_component.resolve): one engine per install, replaced only when no
# launcher runs from it.


def _running(alias, config):
    from tests.test_strata_supervisor import _FakeProc

    sup = StrataSupervisor(alias, config)
    sup._proc = _FakeProc()
    sup.props = {"x": 1}
    base_mod._CARD_CLAIMS[sup] = "claimed"
    return sup


@pytest.fixture
def two_aliases(world, tmp_path, monkeypatch):
    sf.install_pin()  # cuda13 installed
    base = tmp_path / "strata-base.json"
    base.write_text("{}", encoding="utf-8")
    common = {"strata_base_config": str(base), "conversation_cache_mib": 10240}
    running = _running("cuda alias", {**common, KEY: "cuda"})
    starting = StrataSupervisor(ALIAS, {**common, KEY: "hip-rdna3"})
    yield world, running, starting
    base_mod._CARD_CLAIMS.clear()


def test_the_consent_download_does_not_replace_the_install_under_a_running_launcher(two_aliases, tmp_path, monkeypatch):
    world, running, starting = two_aliases
    root = sf.install_root()
    marker = (root / ".dpc-pin.json").read_bytes()
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)
    svc = _service(tmp_path, starting.config, monkeypatch)

    with pytest.raises(PinNotInstalledError):
        asyncio.run(starting.ensure_running())
    result = asyncio.run(svc.download_model("strata", ALIAS))

    assert result["status"] == "error" and "Strata alias 'cuda alias' is running from" in result["error"]
    assert len(world.calls) == 1, "nothing was downloaded"
    assert (root / ".dpc-pin.json").read_bytes() == marker and sf.resolve_install(_cfg("cuda")) == root


def test_a_launcher_that_starts_during_the_download_stops_the_swap(world, tmp_path):
    sf.install_pin()
    root = sf.install_root()
    marker = (root / ".dpc-pin.json").read_bytes()
    _set_engine(world, "hip", "rdna3")
    real_run = world._run

    def _run_and_start_one(cmd, **kw):
        _running("cuda alias", {"strata_base_config": "x.json", "conversation_cache_mib": 1})
        return real_run(cmd, **kw)

    world.mp.setattr(process, "run_supervised", _run_and_start_one)
    try:
        with pytest.raises(RuntimeError, match="is running from"):
            sf.install_pin(config=_cfg("hip-rdna3"))
    finally:
        base_mod._CARD_CLAIMS.clear()
    assert (root / ".dpc-pin.json").read_bytes() == marker
    assert world.leftovers() == [WIN]


def test_with_no_service_the_other_aliases_launcher_keeps_the_card_and_nothing_is_installed(two_aliases, monkeypatch):
    world, running, starting = two_aliases
    installs = []
    monkeypatch.setattr(sf, "ensure_install", lambda config: installs.append(config))

    with pytest.raises(base_mod.CardHeldError):
        asyncio.run(starting.ensure_running())
    assert installs == [] and len(world.calls) == 1


def test_with_no_service_the_install_runs_only_after_the_card_and_the_predecessor(world, tmp_path, monkeypatch):
    sf.install_pin()  # cuda13; this alias wants hip-rdna3
    base = tmp_path / "strata-base.json"
    base.write_text("{}", encoding="utf-8")
    sup = StrataSupervisor(ALIAS, {"strata_base_config": str(base), "conversation_cache_mib": 10240, KEY: "hip-rdna3"})
    order = []
    real_wait = sup._await_predecessor

    async def _wait():
        order.append("predecessor")
        await real_wait()

    sup._await_predecessor = _wait

    def _ensure(config):
        order.append(("install", sup in base_mod._CARD_CLAIMS))
        raise RuntimeError("stop here")

    monkeypatch.setattr(sf, "ensure_install", _ensure)
    with pytest.raises(base_mod.LlamaServerError, match="stop here"):
        asyncio.run(sup.ensure_running())
    assert order == ["predecessor", ("install", True)]
    assert sup not in base_mod._CARD_CLAIMS, "a failed install lets the card go"


def test_a_start_begun_for_a_peer_never_swaps_the_engine(two_aliases, monkeypatch):
    world, running, starting = two_aliases
    base_mod._CARD_CLAIMS.clear()  # the card is free: only the peer rule stops it
    installs = []
    monkeypatch.setattr(sf, "ensure_install", lambda *a, **k: installs.append(1))
    monkeypatch.setattr(sf, "install_pin", lambda *a, **k: installs.append(1))

    async def run():
        assert starting.start_for_peer("dpc-node-peer")
        await starting.peer_start_task

    asyncio.run(run())
    assert installs == [] and len(world.calls) == 1


# --- the start tells Strata the family --------------------------------------------------


@pytest.mark.parametrize("choice,family", [("hip-rdna3", "hip"), ("cuda", "cuda"), (" CUDA ", "cuda"), ("", None)])
def test_the_rebased_run_config_names_the_chosen_family(tmp_path, monkeypatch, choice, family):
    from tests.test_a_strata_alias_without_an_install_dir_runs_dpcs_pinned_install import _World

    world = _World(tmp_path, monkeypatch)
    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    world.write_base(backend="metal")  # the checkout's own word never decides for DPC's install
    world.control()
    cfg = world.sup(**({KEY: choice} if choice else {})).build_start_config(5000)
    assert cfg["exe"] == "component:strata-engine"
    if family is None:
        assert "backend" not in cfg, "empty: Strata picks by the card"
    else:
        assert cfg["backend"] == family


# --- a value this machine cannot take never crashes the consent -------------------------


def test_a_carried_value_with_no_package_here_is_refused_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "platform_tag", lambda: WIN)
    monkeypatch.setattr(sf, "DPC_HOME", tmp_path / "dpc")
    svc = _service(tmp_path, _cfg("sycl"), monkeypatch)  # saved on Linux, read on Windows

    asked = asyncio.run(svc.maybe_emit_required("strata", purpose="p", consequence_if_declined="c",
                                                on_decline="block", provider_alias=ALIAS))
    result = asyncio.run(svc.download_model("strata", ALIAS))

    assert asked is False
    assert result["status"] == "error" and "sycl is not a supported combination on windows-x86_64" in result["error"]
    names = [n for n, _ in svc.local_api.events]
    assert "model_download_required" not in names and names.count("model_download_failed") == 2
    with pytest.raises(ValueError, match="sycl"):
        sf._chosen_rows(_cfg("sycl"), WIN)


def test_the_llama_pin_says_about_never_up_to_with_an_alias_config(tmp_path, monkeypatch):
    from dpc_client_core.managers import llama_server_fetcher as llama
    from dpc_client_core.managers import local_engine_pin_fetcher as pin_mod
    from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor

    for mod in (llama, pin_mod):
        monkeypatch.setattr(mod, "DPC_HOME", tmp_path / "dpc")
    sup = LlamaServerSupervisor(ALIAS, {"gguf_path": "D:/m.gguf", KEY: "hip"})
    sup._claim_card = lambda: pytest.fail("claimed the card")
    svc = _service(tmp_path, sup.config, monkeypatch)

    with pytest.raises(PinNotInstalledError) as err:
        asyncio.run(sup.ensure_running())

    assert "one-time download of about" in str(err.value) and "up to" not in str(err.value)
    (_, payload), = svc.local_api.events
    assert payload["model_name"] == "llama.cpp" and payload["size_source"] is None
    assert not hasattr(llama, "size_source") and not hasattr(llama, "SIZE_SOURCE")


# --- a peer never waits for the card and never swaps ------------------------------------


def test_a_peer_call_that_needs_a_swap_is_refused_as_unavailable_before_the_card(two_aliases, monkeypatch):
    from dpc_client_core.managers import strata_supervisor as strata_sup
    from dpc_client_core.p2p_coordinator import _engine_unavailable_cause

    world, running, starting = two_aliases
    provider = StrataProvider(ALIAS, {"type": "strata", "model": "m", **starting.config})
    # The policy door: no start begins, the answer says the engine is not installed.
    text = asyncio.run(provider._peer_start_refusal("dpc-node-peer"))
    assert "is not installed on this node yet, and a peer's request does not fetch it" in text
    assert provider.supervisor.peer_start_task is None

    # A start begun for a peer: refused at once, while the other alias holds the card.
    async def run():
        token = strata_sup._STARTED_FOR_PEER.set("dpc-node-peer")
        try:
            return await asyncio.wait_for(starting.ensure_running(), 5)
        finally:
            strata_sup._STARTED_FOR_PEER.reset(token)

    with pytest.raises(PinNotInstalledError) as err:
        asyncio.run(run())
    wrapped = RuntimeError("strata provider failed")
    wrapped.__cause__ = err.value
    assert _engine_unavailable_cause(wrapped, strata=True)  # provider_unavailable on the wire
    assert len(world.calls) == 1 and starting not in base_mod._CARD_CLAIMS


# --- one engine of a family: Strata's start cannot be told the build -------------------


def _leave_beside(world, *folders):
    """The updater also leaves these engine folders (Strata keeps the previous one of the family)."""
    real_run = world._run

    def _run(cmd, **kw):
        out = real_run(cmd, **kw)
        engine = Path(cmd[cmd.index("--components-dir") + 1]) / "engine"
        for backend, variant in folders:
            folder = engine / f"{sf.STRATA_ENGINE_VERSION}-{WIN}-{backend}-{variant}"
            folder.mkdir(parents=True)
            (folder / "engine-component.json").write_text(
                json.dumps(_manifest(backend=backend, variant=variant)), encoding="utf-8")
        return out

    world.mp.setattr(process, "run_supervised", _run)


def test_a_build_choice_leaves_one_engine_of_its_family_and_the_next_start_passes(world):
    _set_engine(world, "hip", "rdna3")
    _leave_beside(world, ("hip", "rdna2"), ("hip", "rx9070"))

    root = sf.install_pin(config=_cfg("hip-rdna3"))

    engines = sorted(p.name for p in (root / "components" / "engine").iterdir())
    assert engines == [f"{sf.STRATA_ENGINE_VERSION}-{WIN}-hip-rdna3"]
    # The next start resolves the install as it is: no reinstall, no loop.
    assert sf.resolve_install(_cfg("hip-rdna3")) == root
    assert sf.install_pin(config=_cfg("hip-rdna3")) == root and len(world.calls) == 1


def test_an_engine_that_cannot_be_moved_is_kept_logged_and_the_start_refuses_not_loops(world, monkeypatch, caplog):
    _set_engine(world, "hip", "rdna3")
    _leave_beside(world, ("hip", "rdna2"))
    real_replace = Path.replace

    def _replace(self, target):
        if self.name.endswith("hip-rdna2"):
            raise PermissionError("in use")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace)
    with caplog.at_level("WARNING", logger="dpc_client_core.managers.strata_fetcher"):
        root = sf.install_pin(config=_cfg("hip-rdna3"))
    monkeypatch.setattr(Path, "replace", real_replace)

    assert "could not be moved aside (in use)" in caplog.text
    with pytest.raises(ValueError, match="holds more than one hip engine"):
        sf.resolve_install(_cfg("hip-rdna3"))
    with pytest.raises(ValueError, match="holds more than one hip engine"):
        sf.install_pin(config=_cfg("hip-rdna3"))
    assert len(world.calls) == 1, "refused, never refetched in a loop"
