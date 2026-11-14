"""Two aliases cannot name one KV snapshots folder, and the providers save refuses it.

ADR-044, KV snapshots amendment, point 6: one folder, one index, one writer.
The refusal is made where the editor's save validates the providers list, on
the llama-server alias type here (the check is by folder, not by engine); a
refusal writes nothing.
"""

import asyncio
from types import SimpleNamespace

from dpc_client_core.service import CoreService


def _alias(name, folder, **extra):
    cfg = {"alias": name, "type": "llamacpp_server", "model": f"{name}.gguf"}
    if folder is not None:
        cfg["slot_save_path"] = str(folder)
    cfg.update(extra)
    return cfg


def _errors(*providers):
    return CoreService._validate_providers_config(
        SimpleNamespace(), {"default_provider": providers[0]["alias"], "providers": list(providers)})


def test_the_second_alias_naming_a_folder_is_refused(tmp_path):
    shared = tmp_path / "snaps"
    errors = _errors(_alias("a", shared), _alias("b", shared))
    assert len(errors) == 1
    assert errors[0].startswith("b: slot_save_path:")
    assert "'a'" in errors[0]


def test_a_spelling_of_the_same_folder_is_the_same_folder(tmp_path):
    shared = tmp_path / "snaps"
    errors = _errors(_alias("a", shared), _alias("b", tmp_path / "other" / ".." / "snaps"))
    assert len(errors) == 1 and errors[0].startswith("b: ")


def test_separate_folders_and_no_folder_pass(tmp_path):
    assert _errors(_alias("a", tmp_path / "one"), _alias("b", tmp_path / "two"),
                   _alias("c", None), _alias("d", None)) == []


def test_the_ttl_and_cap_fields_are_checked_in_the_same_save(tmp_path):
    errors = _errors(_alias("a", tmp_path / "one", kv_snapshot_ttl_s=0, kv_snapshot_max_gib=-1))
    assert any("kv_snapshot_ttl_s" in e for e in errors)
    assert any("kv_snapshot_max_gib" in e for e in errors)
    assert _errors(_alias("a", tmp_path / "one", kv_snapshot_ttl_s=3600, kv_snapshot_max_gib=8)) == []


def test_the_providers_save_refuses_a_shared_folder_and_writes_nothing(tmp_path):
    shared = tmp_path / "snaps"
    saved = []
    service = SimpleNamespace(llm_manager=SimpleNamespace(save_config=saved.append))
    service._validate_providers_config = lambda config: CoreService._validate_providers_config(service, config)
    config = {"default_provider": "a", "providers": [_alias("a", shared), _alias("b", shared)]}
    result = asyncio.run(CoreService.save_providers_config(service, config))
    assert result["status"] == "error"
    assert any(e.startswith("b: slot_save_path:") for e in result["errors"])
    assert saved == []
