"""A new pin that fails its first start falls back once to the retained pin (B2 step 3, commit 4).

The retained pin is the other pin that last passed a start (`start_passed_at` in its
marker). A start that passed records that, once per root per process, off the loop;
after the current pin's pass the other marker-carrying pins are pruned, keeping the
retained one. A pin bumped but not downloaded runs the retained pin meanwhile, and
only the owner is asked, with "degrade" (Mike's call, 2026-11-01). A first start of
the current pin that fails for the build's fault rolls back once, inside the same
`ensure_running`, never to an older pin, and later starts run the retained pin until
Strata's Reset or a backend restart. llama.cpp's version check reads the pin's build
number as a whole token of `/props` `build_info`.

The children are fakes, the pins are folders under tmp_path; nothing is started or
fetched, and the real ~/.dpc is never read or written.
"""

import asyncio
import gc
import hashlib
import json
import logging
import shutil
import threading
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_fetcher as lf
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import local_engine_pin_fetcher as pin
from dpc_client_core.managers import strata_fetcher as sf
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    LlamaServerError,
    LlamaServerSupervisor,
    PinNotInstalledError,
    serving_a_peer,
)
from dpc_client_core.managers.strata_supervisor import StrataSupervisor
from dpc_client_core.providers import strata_provider

PLAT = lf.platform_tag()
CUR = lf.LLAMA_CPP_TAG
RET, OLDER, OLDEST, NO_MARKER = "b11429", "b11146", "b10472", "b10684"
PRISM = "prism-b10685-7dffb15"
RET_PASSED, OLDER_PASSED = "2026-10-20T10:00:00+00:00", "2026-09-20T10:00:00+00:00"


class _FakeProc:
    pid = None  # nothing for the process registry to record

    def __init__(self, returncode=None):
        self.returncode = returncode

    def send_signal(self, _sig):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _FakeService:
    """The consent service as the supervisor reads it."""

    def __init__(self, state="missing", error=None):
        self.asked = []
        self.local_api = _FakeLocalApi()
        self.state, self.error = state, error

    async def maybe_emit_required(self, name, **kw):
        self.asked.append((name, kw))
        return True

    def get_status(self, name):
        return {"state": self.state, **({"error": self.error} if self.error else {})}

    def toasts(self):
        return [p for n, p in self.local_api.events if n == "error_toast"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc"
    for mod in (base_mod, lf, pin, sf):
        monkeypatch.setattr(mod, "DPC_HOME", dpc)
    monkeypatch.setattr(pin.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network reached"))
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", None)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    monkeypatch.setattr(base_mod, "_free_port", lambda: 5999)
    monkeypatch.setattr(base_mod, "install_pin", lambda *a, **k: pytest.fail("install_pin reached"))
    return dpc


@pytest.fixture
def service(home, monkeypatch):
    svc = _FakeService()
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)
    return svc


def _marker(root):
    return json.loads(pin.marker_path(root).read_text(encoding="utf-8"))


def _llama_pin(tag, plat=PLAT, passed=None, marker=True):
    root = lf.pins_dir() / tag / plat
    root.mkdir(parents=True)
    (root / lf.server_binary_name()).write_bytes(b"x" * 1000)
    if marker:
        data = {"tag": tag, "platform": plat, "assets": {}}
        if passed:
            data["start_passed_at"] = passed
        pin.marker_path(root).write_text(json.dumps(data), encoding="utf-8")
    return root


def _layout(current=True, current_passed=None):
    """The owner's llama.cpp folder: the current pin, a retained one, an older passed
    one, an old marked one, one with no marker and a hand-made build."""
    roots = {
        RET: _llama_pin(RET, passed=RET_PASSED),
        OLDER: _llama_pin(OLDER, passed=OLDER_PASSED),
        OLDEST: _llama_pin(OLDEST, plat="win-cuda-13.3-x64"),
        NO_MARKER: _llama_pin(NO_MARKER, plat="win-cuda-13.3-x64", marker=False),
    }
    if current:
        roots[CUR] = _llama_pin(CUR, passed=current_passed)
    prism = lf.pins_dir() / PRISM
    prism.mkdir()
    (prism / lf.server_binary_name()).write_bytes(b"hand-made")
    return roots


def _left(roots):
    return sorted(t for t, r in roots.items() if r.exists()) + (
        [PRISM] if (lf.pins_dir() / PRISM).exists() else []
    )


def _llama(tmp_path, outcomes, alias="local_qwen", **config):
    """A supervisor whose child is faked per pin tag: "ok", "died", "silent", "no_props",
    "wrong_build", "oom"; the real `_launch` and `_wait_healthy` run around it."""
    sup = LlamaServerSupervisor(alias, {
        "gguf_path": str(tmp_path / "m.gguf"), "cache_type_k": "q8_0", "cache_type_v": "q8_0",
        "start_timeout_s": 0.3, **config,
    })
    sup.launched = []

    def outcome():
        return outcomes.get(sup._current_tag, "ok")

    async def _spawn(cmd, env):
        sup._current_tag = Path(cmd[0]).parent.parent.name
        sup.launched.append(sup._current_tag)
        kind = outcome()
        if kind == "oom":
            sup._log_fh.write(b"ggml_cuda: CUDA error: out of memory\n")
            sup._log_fh.flush()
        hook = outcomes.get("on_spawn")
        if hook:
            hook(sup)
        return _FakeProc(1 if kind in ("died", "oom") else None)

    async def _health_ok():
        if outcome() == "cancel":
            raise asyncio.CancelledError()
        return outcome() != "silent"

    async def _get(path):
        kind = outcome()
        if kind == "no_props":
            raise ConnectionError("connection refused")
        number = sup._current_tag[1:] if kind != "wrong_build" else "99999"
        return {"build_info": f"b{number}-620d2225c", "total_slots": 1}

    sup._spawn, sup._health_ok, sup._get = _spawn, _health_ok, _get
    return sup


def _run(coro):
    return asyncio.run(coro)


async def _start(sup):
    props = await sup.ensure_running()
    if sup._prune_task is not None:
        await sup._prune_task
    return props


async def _start_and_stop(sup):
    props = await _start(sup)
    await sup.stop()
    return props


# --- a passed start is marked ----------------------------------------------------------------


def test_a_passed_start_is_marked_off_the_loop_once_per_root_per_process(home, tmp_path, monkeypatch):
    root = _llama_pin(CUR)
    calls = []
    real = base_mod.note_start_passed

    def _note(r):
        calls.append((Path(r), threading.get_ident()))
        return real(r)

    monkeypatch.setattr(base_mod, "note_start_passed", _note)

    async def run():
        loop_thread = threading.get_ident()
        await _start_and_stop(_llama(tmp_path, {}))
        await _start_and_stop(_llama(tmp_path, {}, alias="second"))
        return loop_thread

    loop_thread = _run(run())
    assert [c[0] for c in calls] == [root]
    assert calls[0][1] != loop_thread
    assert pin._start_passed_at(_marker(root)) is not None


def test_a_configured_binary_is_never_marked(home, tmp_path):
    root = _llama_pin(CUR)
    sup = _llama(tmp_path, {}, binary_path=str(root / lf.server_binary_name()))
    _run(_start_and_stop(sup))
    assert "start_passed_at" not in _marker(root)
    assert sup._pin_root is None


# --- retention -------------------------------------------------------------------------------


def test_the_current_pins_first_pass_keeps_it_and_the_retained_and_removes_the_other_marked_pins(
    home, tmp_path, caplog,
):
    roots = _layout()
    caplog.set_level(logging.INFO, logger=pin.logger.name)
    _run(_start_and_stop(_llama(tmp_path, {})))
    assert _left(roots) == sorted([CUR, RET, NO_MARKER]) + [PRISM]
    removed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Removed pin")]
    assert len(removed) == 2 and all("freeing" in m and "bytes" in m for m in removed)


def test_a_pin_a_live_child_runs_from_is_skipped(home, tmp_path):
    roots = _layout()
    other = LlamaServerSupervisor("other", {"gguf_path": "m.gguf"})
    other._pin_root, other._proc = roots[OLDEST], _FakeProc()
    try:
        _run(_start_and_stop(_llama(tmp_path, {})))
    finally:
        other._proc = None
    assert roots[OLDEST].exists() and not roots[OLDER].exists()


def test_with_no_other_pin_passed_every_older_pin_is_kept(home, tmp_path):
    # Today's node: no marker carries start_passed_at; b11669's first pass marks it
    # and removes nothing — the old pins go when the next new pin passes.
    roots = {t: _llama_pin(t) for t in (OLDEST, OLDER, RET)}
    roots[CUR] = _llama_pin(CUR)
    _run(_start_and_stop(_llama(tmp_path, {})))
    assert _left(roots) == sorted([CUR, OLDER, OLDEST, RET])
    assert "start_passed_at" in _marker(roots[CUR])


def test_nothing_is_pruned_when_the_retained_pin_is_what_passed(home, tmp_path, service):
    # A rollback's pass, and a pass of the retained pin while the new one is missing,
    # are not the current pin's: every pin stays.
    roots = _layout()
    _run(_start_and_stop(_llama(tmp_path, {CUR: "died"})))
    assert _left(roots) == sorted([CUR, RET, OLDER, OLDEST, NO_MARKER]) + [PRISM]
    base_mod._ROLLBACKS.clear()
    base_mod._PASSES_NOTED.clear()
    shutil.rmtree(roots[CUR])
    _run(_start_and_stop(_llama(tmp_path, {})))
    assert _left(roots) == sorted([RET, OLDER, OLDEST, NO_MARKER]) + [PRISM]


# --- a pin bumped but not downloaded yet (Q1-B) ----------------------------------------------


def test_the_owners_start_runs_the_retained_pin_and_asks_for_the_new_one_with_degrade(home, tmp_path, service):
    _layout(current=False)
    sup = _llama(tmp_path, {})

    async def run():
        props = await sup.ensure_running()
        status = base_mod._pins_status()["llama.cpp"]
        await sup.stop()
        return props, status

    props, status = _run(run())
    assert props["build_info"].startswith(f"b{RET[1:]}")
    assert sup.launched == [RET]
    (name, kw), = service.asked
    assert name == "llama.cpp" and kw["on_decline"] == "degrade" and kw["provider_alias"] == sup.alias
    assert RET in kw["consequence_if_declined"]
    assert status["running_pin"] == RET and status["retained_pin"] == RET and status["pin"] == CUR


def test_a_peers_start_runs_the_retained_pin_but_never_asks_or_fetches(home, tmp_path, service):
    _layout(current=False)
    sup = _llama(tmp_path, {})
    with serving_a_peer("dpc-node-peer"):
        _run(_start_and_stop(sup))
    assert sup.launched == [RET]
    assert service.asked == []


def test_with_no_retained_pin_the_missing_pin_is_still_refused(home, tmp_path, service):
    sup = _llama(tmp_path, {})
    with pytest.raises(PinNotInstalledError):
        _run(sup.ensure_running())
    assert service.asked[0][1]["on_decline"] == "block"


def test_a_failed_download_names_its_error_and_where_to_start_it_again(home, tmp_path, monkeypatch):
    svc = _FakeService(state="failed", error="HTTP Error 404: Not Found")
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)
    with pytest.raises(PinNotInstalledError) as info:
        _run(_llama(tmp_path, {}).ensure_running())
    text = str(info.value)
    assert "the download failed: HTTP Error 404: Not Found; it can be started again from the D-PC Messenger app" in text
    assert "Do not retry" not in text and "has been asked" not in text


# --- rollback once ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["died", "silent", "no_props", "wrong_build"])
def test_a_first_start_that_fails_for_the_build_rolls_back_once_to_the_retained_pin(
    home, tmp_path, service, caplog, kind,
):
    roots = _layout()
    caplog.set_level(logging.INFO)
    sup = _llama(tmp_path, {CUR: kind})

    async def run():
        props = await sup.ensure_running()
        status = base_mod._pins_status()["llama.cpp"]
        await sup.stop()
        return props, status

    props, status = _run(run())
    assert sup.launched == [CUR, RET]
    assert props["build_info"].startswith(f"b{RET[1:]}")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "failed its first start" in r.getMessage()]
    assert len(errors) == 1
    (toast,) = service.toasts()
    assert CUR in toast["message"] and RET in toast["message"]
    assert "start_passed_at" not in _marker(roots[CUR])
    rolled = status["rolled_back_to"]
    assert (rolled["from"], rolled["to"]) == (CUR, RET) and rolled["reason"] and rolled["at"]
    assert status["running_pin"] == RET
    by_tag = {i["tag"]: i for i in status["installed_pins"]}
    assert by_tag[RET]["start_passed_at"] == RET_PASSED and by_tag[CUR]["start_passed_at"] is None
    assert NO_MARKER not in by_tag


def test_later_starts_run_the_retained_pin_until_the_backend_restarts(home, tmp_path, service):
    _layout()
    first = _llama(tmp_path, {CUR: "died"})
    _run(_start_and_stop(first))
    second = _llama(tmp_path, {}, alias="another")
    _run(_start_and_stop(second))
    _run(_start_and_stop(first))
    assert second.launched == [RET] and first.launched == [CUR, RET, RET]
    assert len(service.toasts()) == 1
    base_mod._ROLLBACKS.clear()  # what a backend restart leaves
    _run(_start_and_stop(second))
    assert second.launched == [RET, CUR]


def _held_on_the_second_claim(sup):
    real, calls = sup._claim_card, []

    def _claim():
        calls.append(1)
        if len(calls) == 2:
            raise CardHeldError("held by strata alias 'flash'", "flash", "strata")
        real()

    sup._claim_card = _claim


def _drain_on_spawn(sup):
    sup._draining = True


@pytest.mark.parametrize("kind", ["card_held", "oom", "admission", "cancel", "draining", "config", "passed_before"])
def test_no_rollback_for_a_failure_the_retained_pin_would_meet_too(home, tmp_path, service, monkeypatch, kind):
    _layout(current_passed=RET_PASSED if kind == "passed_before" else None)
    outcomes, config = {CUR: "died"}, {}
    if kind == "oom":
        outcomes[CUR] = "oom"
    elif kind == "cancel":
        outcomes[CUR] = "cancel"
    elif kind == "draining":
        outcomes["on_spawn"] = _drain_on_spawn
    elif kind == "config":
        config["image_max_tokens"] = "lots"
    elif kind == "admission":
        config = {"cache_type_k": None, "cache_type_v": None}
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda *a, **k: 100)
        monkeypatch.setattr(base_mod, "_gguf_mib", lambda path: 20000)
    sup = _llama(tmp_path, outcomes, **config)
    if kind == "card_held":
        _held_on_the_second_claim(sup)
    raised = asyncio.CancelledError if kind == "cancel" else LlamaServerError
    with pytest.raises(raised):
        _run(sup.ensure_running())
    assert RET not in sup.launched
    assert base_mod._ROLLBACKS == {} and service.toasts() == []


def test_a_rollback_never_chains_to_an_older_pin(home, tmp_path, service):
    _layout()
    sup = _llama(tmp_path, {CUR: "died", RET: "died"})
    with pytest.raises(LlamaServerError):
        _run(sup.ensure_running())
    assert sup.launched == [CUR, RET]
    assert len(service.toasts()) == 1


def test_the_retained_pin_running_for_a_missing_one_does_not_roll_back(home, tmp_path, service):
    _layout(current=False)
    sup = _llama(tmp_path, {RET: "died"})
    with pytest.raises(LlamaServerError):
        _run(sup.ensure_running())
    assert sup.launched == [RET] and service.toasts() == []


def test_a_retained_pin_without_its_binary_is_no_rollback_and_no_older_one_stands_in(home, tmp_path, service):
    roots = _layout()
    (roots[RET] / lf.server_binary_name()).unlink()
    sup = _llama(tmp_path, {CUR: "died"})
    with pytest.raises(LlamaServerError):
        _run(sup.ensure_running())
    assert sup.launched == [CUR]


# --- llama.cpp's version check: the build number in /props build_info -------------------------


@pytest.mark.parametrize("build, ok", [
    ("b11669-620d2225c", True),
    ("version: 0.7.0-dev (build 11669, commit 620d2225c)", True),
    ("11669 (620d2225c)", True),
    ("b116690-620d2225c", False),
    ("b1166-620d2225c", False),
    ("a11669f", False),
    ("", False),
    (None, False),
])
def test_the_build_number_must_be_a_whole_token_of_build_info(home, tmp_path, build, ok, caplog):
    sup = _llama(tmp_path, {})
    sup._pin_root = _llama_pin(CUR)
    caplog.set_level(logging.INFO, logger=base_mod.logger.name)
    if ok:
        sup._check_build_info({"build_info": build})
    else:
        with pytest.raises(LlamaServerError) as info:
            sup._check_build_info({"build_info": build})
        assert getattr(info.value, "_build_fault", False)
    assert any(f"/props build_info {build!r}" in r.getMessage() for r in caplog.records)


def test_a_configured_binary_is_not_version_checked(home, tmp_path):
    sup = _llama(tmp_path, {})
    sup._pin_root = None
    sup._check_build_info({"build_info": "b1-anything"})


# --- Strata -----------------------------------------------------------------------------------

SPLAT = "windows-x86_64"
S_CUR, S_RET = sf.STRATA_RELEASE_TAG, "v0.2.8"


def _strata_pin(tag, version, passed=None):
    root = sf.pins_dir() / tag / SPLAT
    python = sf.control_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / "serve").mkdir()
    (root / "serve" / "server.py").write_text("# launcher\n", encoding="utf-8")
    folder = f"{version}-{SPLAT}-cuda-cuda13-5d1bc23a"
    engine = sf.components_dir(root) / "engine" / folder
    engine.mkdir(parents=True)
    raw = json.dumps({"id": "strata-engine", "version": version, "platform": SPLAT,
                      "backend": "cuda", "variant": "cuda13"}).encode()
    (engine / "engine-component.json").write_bytes(raw)
    data = {"tag": tag, "platform": SPLAT, "assets": {},
            "engine": {"folder": folder, "version": version, "backend": "cuda", "variant": "cuda13",
                       "manifest_sha256": hashlib.sha256(raw).hexdigest()}}
    if passed:
        data["start_passed_at"] = passed
    pin.marker_path(root).write_text(json.dumps(data), encoding="utf-8")
    return root


@pytest.fixture
def strata_world(home, monkeypatch):
    monkeypatch.setattr(sf, "platform_tag", lambda: SPLAT)
    cur = _strata_pin(S_CUR, sf.STRATA_ENGINE_VERSION)
    ret = _strata_pin(S_RET, "0.1.50", passed=RET_PASSED)
    current = {"root": cur}
    monkeypatch.setattr(sf, "resolve_install", lambda config: current["root"])
    monkeypatch.setattr(base_mod.LlamaServerSupervisor, "_launch", _strata_base_launch)
    return {"cur": cur, "ret": ret, "current": current}


async def _strata_base_launch(self, binary, cache_type=None):
    """The base `_launch` of a Strata start, faked: the launcher answers or dies."""
    tag = self.install_dir().parent.name
    self.launched.append(tag)
    if self.outcomes.get(tag) == "died":
        raise base_mod._build_fault(LlamaServerError("strata[x] exited with 1 before becoming healthy"))
    self._proc = _FakeProc()
    self.props = {"total_slots": 1}
    return self.props


def _strata(outcomes, alias="flash"):
    sup = StrataSupervisor(alias, {"strata_base_config": "C:/base.json", "conversation_cache_mib": 1024})
    sup.launched, sup.outcomes = [], outcomes
    versions = {S_CUR: sf.STRATA_ENGINE_VERSION, S_RET: "0.1.50"}

    async def _get(path):
        tag = sup.install_dir().parent.name
        kind = outcomes.get(tag)
        if kind == "no_version":
            return {}
        return {"engine": {"version": "0.0.1" if kind == "version" else versions[tag]}}

    async def _smoke():
        return "no tool call came back" if outcomes.get(sup.install_dir().parent.name) == "smoke" else None

    async def _stop():
        sup._proc, sup.props = None, None
        base_mod._CARD_CLAIMS.pop(sup, None)

    sup._get, sup._smoke, sup.stop = _get, _smoke, _stop
    sup.write_start_config = lambda port: None
    sup._start_watch = lambda: None
    sup.props = None
    return sup


@pytest.fixture(autouse=True)
def _no_vram_probe(monkeypatch):
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda *a, **k: None)
    gc.collect()  # supervisors of earlier tests leave the registry the status reads


def test_the_retained_strata_pin_is_checked_on_its_markers_word(strata_world):
    ret = strata_world["ret"]
    assert sf._retained_install_ok(ret, {})
    assert not sf._retained_install_ok(ret, {"strata_engine_backend": "hip"})
    folder = sf.engine_folder(ret)
    (folder / "engine-component.json").write_bytes(b'{"id": "strata-engine"}')
    assert not sf._retained_install_ok(ret, {})


@pytest.mark.parametrize("kind", ["died", "version", "no_version", "smoke"])
def test_a_strata_first_start_that_fails_rolls_back_once(strata_world, service, kind):
    sup = _strata({S_CUR: kind})
    props = _run(_start(sup))
    assert props and sup.launched == [S_CUR, S_RET]
    assert sup.failure is None and sup.install_dir() == strata_world["ret"]
    assert len(service.toasts()) == 1
    assert "start_passed_at" not in _marker(strata_world["cur"])


def test_a_strata_retained_pin_that_fails_too_latches_as_today(strata_world, service):
    sup = _strata({S_CUR: "smoke", S_RET: "smoke"})
    with pytest.raises(LlamaServerError):
        _run(sup.ensure_running())
    assert sup.launched == [S_CUR, S_RET] and sup.failure is not None


def test_strata_reset_sends_the_next_start_to_the_new_pin_again(strata_world, service):
    sup = _strata({S_CUR: "smoke"})
    _run(_start_and_stop(sup))
    _run(_start_and_stop(sup))
    assert sup.launched == [S_CUR, S_RET, S_RET]
    sup.outcomes.clear()
    sup.reset_failure()
    _run(_start_and_stop(sup))
    assert sup.launched[-1] == S_CUR


def test_a_strata_pin_that_passed_before_does_not_roll_back(strata_world, service):
    pin.note_start_passed(strata_world["cur"])
    sup = _strata({S_CUR: "smoke"})
    with pytest.raises(LlamaServerError):
        _run(sup.ensure_running())
    assert sup.launched == [S_CUR] and service.toasts() == []


def test_strata_runs_the_retained_pin_while_the_new_one_is_missing(strata_world, service):
    strata_world["current"]["root"] = None
    sup = _strata({})
    _run(_start(sup))
    assert sup.launched == [S_RET]
    (name, kw), = service.asked
    assert name == "strata" and kw["on_decline"] == "degrade"


def test_a_strata_start_begun_for_a_peer_runs_the_retained_pin_and_never_asks(strata_world, service):
    strata_world["current"]["root"] = None
    sup = _strata({})

    async def run():
        assert sup.start_for_peer("dpc-node-peer")
        await sup.peer_start_task

    _run(run())
    assert sup.launched == [S_RET] and service.asked == []


def test_the_strata_row_names_its_running_pin_and_the_status_carries_pins(strata_world, service, monkeypatch):
    sup = _strata({S_CUR: "smoke"})
    monkeypatch.setattr(strata_provider, "_ACTIVE_SUPERVISORS", {"flash": sup})

    async def run():
        await _start(sup)
        return await strata_provider.local_engine_status()

    status = _run(run())
    (row,) = status["strata"]
    assert row["running_pin"] == S_RET
    block = status["pins"]["strata"]
    assert block["pin"] == S_CUR and block["retained_pin"] == S_RET and block["running_pin"] == S_RET
    assert (block["rolled_back_to"]["from"], block["rolled_back_to"]["to"]) == (S_CUR, S_RET)
    assert sorted(i["tag"] for i in block["installed_pins"]) == sorted([S_CUR, S_RET])
    assert "llama.cpp" in status["pins"]


@pytest.mark.parametrize("how", ["rolled_back", "not_downloaded"])
def test_a_running_retained_strata_pin_matches_its_own_engine_version(strata_world, service, monkeypatch, how):
    # Its engine is the retained marker's 0.1.50, not the pin's 0.1.55: not a mismatch.
    if how == "not_downloaded":
        strata_world["current"]["root"] = None
    sup = _strata({S_CUR: "smoke"})
    monkeypatch.setattr(strata_provider, "_ACTIVE_SUPERVISORS", {"flash": sup})

    async def run():
        await _start(sup)
        return await strata_provider.local_engine_status()

    (row,) = _run(run())["strata"]
    assert row["engine_version"] == "0.1.50" != sf.STRATA_ENGINE_VERSION
    assert row["version_matches"] is True and row["running_pin"] == S_RET
    assert row["pin"]["release_tag"] == S_CUR
