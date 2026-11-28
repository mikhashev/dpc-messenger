"""A test run never writes the developer's real home (ADR-052 S3 follow-up 6, Johnny #47-2).

The Settings guard (`test_a_test_run_never_writes_the_real_config_ini.py`) covered `config.ini` only.
`CoreService()` also writes `privacy_rules.json` (`ContextFirewall(DPC_HOME_DIR / PRIVACY_RULES)`,
which creates it and seeds and prunes its tool keys) and `providers.json` (`LLMManager`, which writes a
default config), and builds the identity and more under `service.DPC_HOME_DIR`, a copy of the home
taken at import. The conftest guard points every module-level copy of the home at the test's
stand-in (`_HOME_COPIES`), and `LLMManager`'s default path too.

The sentinel reads the three real files only to hash them, before and after a full `CoreService()`.
"""

import ast
import hashlib
from pathlib import Path

import pytest

from tests.conftest import _HOME_COPIES, _HOME_DEFAULTS

REAL_HOME = Path.home() / ".dpc"
WATCHED = ("config.ini", "privacy_rules.json", "providers.json")
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "dpc_client_core"
PROTOCOL_ROOT = Path(__file__).resolve().parents[3] / "dpc-protocol" / "dpc_protocol"
# Module-level copies the guard leaves alone on purpose: CLI scripts the service never imports, and a
# module whose copy another conftest fixture already redirects.
NOT_GUARDED = {
    "dpc_client_core.managers.local_engine_pin_fetcher",  # `_engine_pins_in_tmp`
    "dpc_client_core.dpc_agent.training.audit_data",
    "dpc_client_core.dpc_agent.training.extract_triples_from_kg",
    "dpc_client_core.dpc_agent.training.extract_triples_from_markdown",
    "dpc_client_core.dpc_agent.training.paraphrase_corpus",
    "dpc_client_core.dpc_agent.training.prepare_corpus",
}


def _fingerprint(path: Path):
    """The file's SHA-256 and size, or None when it does not exist. Read-only."""
    if not path.exists():
        return None
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def test_building_the_service_leaves_the_real_config_privacy_rules_and_providers_as_they_were(tmp_path):
    from dpc_client_core import service

    before = {name: _fingerprint(REAL_HOME / name) for name in WATCHED}
    built = service.CoreService()
    after = {name: _fingerprint(REAL_HOME / name) for name in WATCHED}

    assert after == before, "a test run changed the developer's real home"
    home = service.DPC_HOME_DIR
    assert tmp_path in home.parents, f"the service's home is the test's stand-in, not {home}"
    assert built.settings.config_file.parent == home
    # The writers ran, on the stand-in.
    assert (home / "privacy_rules.json").exists() and (home / "providers.json").exists()
    assert built.llm_manager.config_path == home / "providers.json"


def test_every_module_level_copy_points_at_the_stand_in(tmp_path):
    import importlib

    for module_name, attribute, _under in _HOME_COPIES:
        value = getattr(importlib.import_module(module_name), attribute)
        assert tmp_path in Path(value).parents, f"{module_name}.{attribute} is {value}"
    for module_name, function_path in _HOME_DEFAULTS:
        function = importlib.import_module(module_name)
        for name in function_path.split("."):
            function = getattr(function, name)
        paths = [v for v in function.__defaults__ if isinstance(v, Path)]
        assert paths and all(tmp_path in v.parents for v in paths), f"{function_path}: {paths}"


def test_the_rejected_records_store_writes_under_the_stand_in(tmp_path):
    """The refused records a test keeps must land under the stand-in too: the
    store's default is `<home>/state/rejected_records.json`, and a refusal
    written into the developer's real ~/.dpc/state would outlive the run."""
    from dpc_client_core import rejected_records

    real_state = REAL_HOME / "state" / rejected_records.FILE_NAME
    before = _fingerprint(real_state)

    path = rejected_records.default_path()
    assert tmp_path in path.parents, f"the store's live file is the stand-in's, not {path}"
    store = rejected_records.get_store()
    store.record(conversation_id="group-guard", reason="unsigned", arrived="live")
    assert store.file_path == path
    assert path.exists(), "the store wrote its live file under the stand-in"
    assert _fingerprint(real_state) == before


def _module_level_home_copies(root: Path, package: str):
    """(module, name) for each module-level assignment whose value builds `Path.home() / ".dpc"`."""
    found = set()
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if ".dpc" not in source:
            continue
        module = ".".join((package, *path.relative_to(root).with_suffix("").parts))
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            text = ast.get_source_segment(source, node.value) or ""
            if "home()" in text and ".dpc" in text:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                found.update((module, t.id) for t in targets if isinstance(t, ast.Name))
    return found


@pytest.mark.skipif(not PROTOCOL_ROOT.is_dir(), reason="dpc-protocol is not beside dpc-client here")
def test_no_module_level_copy_of_the_home_is_missing_from_the_guard():
    guarded = {(module, attribute) for module, attribute, _ in _HOME_COPIES}
    found = (_module_level_home_copies(PACKAGE_ROOT, "dpc_client_core")
             | _module_level_home_copies(PROTOCOL_ROOT, "dpc_protocol"))
    missing = {(module, name) for module, name in found
               if (module, name) not in guarded and module not in NOT_GUARDED}
    assert not missing, f"add these to conftest._HOME_COPIES: {sorted(missing)}"
