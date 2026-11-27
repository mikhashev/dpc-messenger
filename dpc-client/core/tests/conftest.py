"""Shared test constants and fixtures. Override TEST_DOMAIN via DPC_TEST_DOMAIN."""
import os

# The GPU belongs to the live local engines (Strata, Whisper, ComfyUI), and a
# torch CUDA context from a test process takes VRAM from them. Strata's engine
# log says what a full card costs: "when the card is this full the driver can
# page GPU memory to system RAM, decode falls to a few tok/s and a window may
# never finish (#2065)". Tests that need a CUDA answer mock
# torch.cuda.is_available (see test_the_entity_model_has_a_device_setting.py and
# test_the_node_embedding_device_decides_over_an_agent_profile.py). This runs at
# conftest import, before any test module can import torch.
if os.environ.get("DPC_TESTS_ALLOW_GPU") != "1":
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # not "": Windows drops an empty variable

import pytest

TEST_DOMAIN = os.environ.get("DPC_TEST_DOMAIN", "wikipedia.org")
TEST_DOMAIN_WWW = f"www.{TEST_DOMAIN}"
TEST_DOMAIN_URL = f"https://{TEST_DOMAIN}"


@pytest.fixture(scope="session")
def _test_signing_key():
    """One RSA key for the whole session; generating it per test costs seconds."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_identity(_test_signing_key, monkeypatch):
    """A signing key belonging to the test rather than to the machine.

    ConversationMonitor signs with ~/.dpc/node.key when it exists. On a
    developer box it does and on a CI runner it does not, so tests about
    signatures passed here and exercised nothing there: with nothing signed,
    every record took the branch that has no check in it. Two of them failed on
    the runner for exactly that reason and were read as flaky.
    """
    from dpc_protocol.commit_integrity import CommitSigner
    from dpc_client_core.conversation_monitor import ConversationMonitor

    signer = CommitSigner("dpc-node-" + "a" * 32, _test_signing_key)
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: signer)
    return signer


@pytest.fixture(autouse=True)
def _node_ledger_in_tmp(tmp_path, monkeypatch):
    """Every model call leaves a usage row (ADR-041 D3), and the writer's
    default directory is this machine's own ~/.dpc/ledger. A test that drives
    `chat()` or a served peer request would otherwise write into the
    developer's real ledger, so the default is pointed at the test's tmp_path.
    A test that wants the rows hands the adapter or coordinator a ledger of
    its own.
    """
    from dpc_client_core import node_ledger

    monkeypatch.setattr(node_ledger, "ledger_dir", lambda: tmp_path / "ledger")


@pytest.fixture(autouse=True)
def _embedding_cache_in_tmp(tmp_path, monkeypatch):
    """The embedding cache defaults to this machine's ~/.dpc/cache/embeddings, shared by
    every agent. Tests with fake encoders wrote vectors of a foreign length there, so
    every test gets its own cache under tmp_path. Guarded by
    test_a_test_run_never_writes_into_the_real_embedding_cache."""
    from dpc_client_core.dpc_agent import memory

    monkeypatch.setattr(memory, "_embedding_cache_root", lambda: tmp_path / "embedding-cache")
    monkeypatch.setattr(memory, "_disk_cache", None)


# Module-level copies of `Path.home() / ".dpc"` (and paths built on it at import), which a later
# `Path.home` patch cannot reach: (module, attribute, path under the home). Every one found by grep
# across `dpc_client_core` and the `dpc_protocol` modules it imports; the training scripts' `DPC_HOME`
# (CLI only, never reached by the service) and `local_engine_pin_fetcher.DPC_HOME` (its own fixture
# below) are not listed. A new one belongs here.
_HOME_COPIES = (
    ("dpc_client_core.service", "DPC_HOME_DIR", ""),
    ("dpc_client_core.p2p_manager", "DPC_HOME_DIR", ""),
    ("dpc_client_core.device_context_collector", "DPC_HOME_DIR", ""),
    ("dpc_client_core.message_handlers.certificate_handler", "DPC_HOME_DIR", ""),
    ("dpc_client_core.coordinators.discord_coordinator", "DPC_HOME", ""),
    ("dpc_client_core.local_api", "WS_TOKEN_PATH", ".ws_token"),
    ("dpc_client_core.telegram_service", "DPC_HOME_DIR", ""),
    ("dpc_client_core.voice_service", "DPC_HOME_DIR", ""),
    ("dpc_client_core.single_instance", "DEFAULT_LOCK", "run_service.pid"),
    ("dpc_protocol.crypto", "DPC_HOME_DIR", ""),
    ("dpc_protocol.crypto", "KEY_FILE", "node.key"),
    ("dpc_protocol.crypto", "CERT_FILE", "node.crt"),
    ("dpc_protocol.crypto", "NODE_ID_FILE", "node.id"),
    ("dpc_protocol.commit_integrity", "DPC_HOME_DIR", ""),
    ("dpc_protocol.markdown_manager", "DPC_HOME_DIR", ""),
    ("dpc_protocol.pcm_core", "DPC_HOME_DIR", ""),
)
# Functions whose default arguments hold a path under the home, bound when they were defined:
# (module, function path). Each such default is pointed at the same place under the stand-in.
_HOME_DEFAULTS = (
    ("dpc_client_core.llm_manager", "LLMManager.__init__"),
    ("dpc_client_core.single_instance", "acquire"),
    ("dpc_client_core.single_instance", "release"),
)


@pytest.fixture(autouse=True)
def _settings_never_open_the_real_home(tmp_path, monkeypatch):
    """A Settings built on this machine's own ~/.dpc reads and may write its config.ini: a fresh
    config when there is none, the seeded keys a config written before them lacks
    (`_seed_missing_keys`, ADR-052 item 7's carrier), every setter. Code builds one there by
    itself (`DPC_HOME_DIR` in service.py, webrtc_peer, `_dpc_home_for` for a graph root outside
    `agents/`): test_conversation_monitoring's `CoreService()` and the `kg` fixtures of
    test_knowledge_graph, test_graph_snapshot and test_legacy_forms all did, and a run of one of
    them wrote `[local_transcription] idle_unload_s` into the developer's real config.ini
    (2026-11-23). Every Settings asked for the real home is built on
    the test's tmp_path instead: a test sees what a CI runner sees (no config, the defaults) and
    the real file is never opened. Guarded by
    test_a_test_run_never_writes_the_real_config_ini."""
    from pathlib import Path
    from dpc_protocol import paths as dpc_paths
    from dpc_client_core import settings as settings_module

    real_home = (Path.home() / ".dpc").resolve()
    stand_in = tmp_path / "home-stand-in" / ".dpc"
    monkeypatch.setattr(dpc_paths, "_TEST_STAND_INS", dpc_paths._TEST_STAND_INS | {stand_in})
    original_init = settings_module.Settings.__init__

    def init(self, dpc_home_dir, *args, **kwargs):
        try:
            asked_for_the_real_home = Path(dpc_home_dir).expanduser().resolve() == real_home
        except (OSError, TypeError, ValueError):
            asked_for_the_real_home = False
        if asked_for_the_real_home:
            dpc_home_dir = stand_in
        original_init(self, dpc_home_dir, *args, **kwargs)

    monkeypatch.setattr(settings_module.Settings, "__init__", init)

    # The whole home, not only Settings (Johnny #47-2, ADR-052 S3 follow-up 6): `CoreService()`
    # also writes `privacy_rules.json` (`ContextFirewall(DPC_HOME_DIR / ...)`, which seeds and prunes
    # tool keys) and `providers.json` (`LLMManager`'s default config and `save_config`), and builds
    # the identity, the token cache and more under `service.DPC_HOME_DIR`. Each module-level copy of
    # the home is pointed at the same stand-in, which Settings already uses; a test that sets its
    # own home patches after this fixture and wins. Guarded by
    # test_a_test_run_never_writes_the_real_home.
    import importlib

    for module_name, attribute, under in _HOME_COPIES:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, attribute, stand_in / under if under else stand_in)
    for module_name, function_path in _HOME_DEFAULTS:
        function = importlib.import_module(module_name)
        for name in function_path.split("."):
            function = getattr(function, name)

        def moved(value):
            try:
                return stand_in / Path(value).resolve().relative_to(real_home)
            except (TypeError, ValueError, OSError):
                return value

        monkeypatch.setattr(function, "__defaults__", tuple(moved(v) for v in function.__defaults__))


@pytest.fixture(autouse=True)
def _fresh_process_registry():
    """The process registry is module state (ADR-046). A test that begins the
    backend's stop would make every later spawn refuse, and open entries of one
    test's children would be read as the next one's. Each test starts with an
    empty in-memory registry; none touches `~/.dpc/process_registry.json`, which
    only `start_persistent()` (run_service) ever writes."""
    from dpc_client_core import process_registry

    process_registry.reset_registry()
    yield
    process_registry.reset_registry()


@pytest.fixture(autouse=True)
def _no_card_claims_cross_tests():
    """The card-holder registry is module state (ADR-044 point 4). A test whose
    fake launch returns without a child leaves its claim behind, and the next
    test's start would be refused by an alias that only existed in the last one.
    The holds on an alias, the callers' last calls, the card queue, the slot queue and what a swap
    leaves behind are module state beside it, cleared for the same reason; so are the card leases,
    their line and their yields (ADR-052)."""
    from dpc_client_core.managers import card_lease, llama_server_supervisor

    def clear():
        card_lease._CARD_LEASES.clear()
        card_lease._LEASE_LINE.clear()
        card_lease._YIELDS.clear()
        card_lease._CLOSED = None
        card_lease._RECENT.clear()
        card_lease._LISTENERS.clear()
        llama_server_supervisor._CARD_CLAIMS.clear()
        llama_server_supervisor._CARD_HOLDS.clear()
        llama_server_supervisor._last_caller_call_at.clear()
        llama_server_supervisor._owner_request_ended_at.clear()
        llama_server_supervisor._CARD_QUEUE.clear()
        llama_server_supervisor._SLOT_QUEUE.clear()
        llama_server_supervisor._LAST_SWAP = None
        llama_server_supervisor._LAST_BACKGROUND_SKIP.clear()
        llama_server_supervisor._SWAP_STOPS.clear()
        llama_server_supervisor._SWAP_TOASTS.clear()

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_whisper_load_has_run_in_this_process():
    """The (device, model) pairs a Whisper load has run on in this process are module state (ADR-052
    S3 follow-ups 4, 6, 7: they pick the reload figure over the first load's); so are the CPU
    transcriptions running now (L12 point 3's contention, S3/S4 follow-up 8).
    Each test starts as a fresh backend would, with no load run; the module is not imported here for
    a test that never imports it."""
    import sys

    def clear():
        module = sys.modules.get("dpc_client_core.providers.whisper_provider")
        if module is not None and hasattr(module, "_LOADED_WEIGHTS"):
            module._LOADED_WEIGHTS.clear()
        if module is not None and hasattr(module, "_CPU_RUNS"):
            module._CPU_RUNS.clear()

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _the_hosts_ram_is_unknown_unless_a_test_says(monkeypatch):
    """ADR-052 L12 (S3d): a cold CPU twin is taken only when available host RAM covers it, and RAM that
    cannot be read does not. A runner's real RAM would decide a test's route, as a GPU would, so each
    test starts with it unreadable (the CPU route closed, every GPU call to the line as before S3d) and
    a test of the CPU route says how much there is. L12's settings source a `CoreService` registered
    in another test is dropped too: the code defaults apply."""
    from dpc_client_core.providers import whisper_provider

    monkeypatch.setattr(whisper_provider, "available_ram_mib", lambda: None)
    monkeypatch.setattr(whisper_provider, "_cpu_route_source", None)


@pytest.fixture(autouse=True)
def _the_cards_vram_is_unknown_unless_a_test_says(monkeypatch):
    """ADR-052 L11 (S3e): the one shared cell reads the card's free and total VRAM, and no reading is
    `unknown`, never "fits". This machine's real card would decide a test's fit (and spawn nvidia-smi),
    so each test starts with no reading, as a CI runner has none; a fit test says what the card reads."""
    from dpc_client_core.managers import card_lease

    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (None, None))


@pytest.fixture(autouse=True)
def _engine_pins_in_tmp(tmp_path, monkeypatch):
    """A passed start writes its pin's marker and may prune the older pins (B2 step 3).
    The pins folders default to this machine's ~/.dpc/bin, so every test gets its own
    under tmp_path, and starts with no rollback or pass recorded by another test."""
    from dpc_protocol import paths as dpc_paths
    from dpc_client_core.managers import (
        llama_server_fetcher, llama_server_supervisor, local_engine_pin_fetcher, strata_fetcher,
    )

    pins_stand_in = tmp_path / "dpc-pins-home"
    monkeypatch.setattr(dpc_paths, "_TEST_STAND_INS", dpc_paths._TEST_STAND_INS | {pins_stand_in})
    for mod in (local_engine_pin_fetcher, llama_server_fetcher, strata_fetcher):
        monkeypatch.setattr(mod, "DPC_HOME", pins_stand_in)
    llama_server_supervisor._ROLLBACKS.clear()
    llama_server_supervisor._PASSES_NOTED.clear()
    yield
    llama_server_supervisor._ROLLBACKS.clear()
    llama_server_supervisor._PASSES_NOTED.clear()


class ConnectedUi:
    """A UI client at the other end of `local_api`, recording what it is sent.

    It answers nothing: the web-auth approval dialog it used to answer is
    gone, and no browse path asks a question any more. `answer` survives for
    callers that still pass it and is inert.
    """

    def __init__(self, answer: str = "approve"):
        self.answer = answer
        self.has_clients = True
        self.events: list = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def service_with_ui(answer: str = "approve", **extra):
    """`ctx.dpc_service` carrying a connected UI, plus whatever else the
    caller's tool reads off the service."""
    import types

    return types.SimpleNamespace(local_api=ConnectedUi(answer), **extra)


@pytest.fixture(autouse=True)
def _telegram_bursts_start_at_once(monkeypatch):
    """The agent's Telegram bridge waits for a quiet chat before it starts a run
    (BURST_QUIET_SECONDS, 20 s unless the agent sets its own). A test that sends
    one message would sit that wait out; here the default pause is zero, and a
    test of the gathering passes its own `burst_quiet_seconds` to the bridge. A
    test of the real default overrides this fixture by its name."""
    from dpc_client_core.managers import agent_telegram_bridge

    monkeypatch.setattr(agent_telegram_bridge, "BURST_QUIET_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _camoufox_binary_counts_as_present(monkeypatch):
    """browser.py refuses to launch while the Camoufox browser binary is not
    on disk (2026-10-21). CI installs the package and never the binary, and a
    developer box usually has it, so every test that stubs `Camoufox` would
    pass here and refuse there. The answer is fixed to "present"; the tests of
    the refusal itself set it back."""
    from dpc_client_core import camoufox_binary

    monkeypatch.setattr(camoufox_binary, "missing_reason", lambda: None)
