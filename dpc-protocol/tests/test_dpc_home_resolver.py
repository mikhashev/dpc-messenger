"""Unit tests for the single DPC_HOME resolver in dpc_protocol.paths."""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def test_dpc_home_unset_returns_default_dot_dpc(monkeypatch):
    from dpc_protocol.paths import dpc_home

    monkeypatch.delenv("DPC_HOME", raising=False)
    assert dpc_home() == Path.home() / ".dpc"


def test_dpc_home_empty_or_whitespace_falls_back_to_default(monkeypatch):
    from dpc_protocol.paths import dpc_home

    for blank in ("", "   ", "\t\n  "):
        monkeypatch.setenv("DPC_HOME", blank)
        assert dpc_home() == Path.home() / ".dpc"


def test_dpc_home_explicit_and_whitespace_padded(monkeypatch, tmp_path):
    from dpc_protocol.paths import dpc_home

    target = tmp_path / "custom-dpc"
    monkeypatch.setenv("DPC_HOME", f"  {target} \t ")
    assert dpc_home() == target


def test_dpc_home_expands_tilde(monkeypatch, tmp_path):
    from dpc_protocol.paths import dpc_home

    fake_home = tmp_path / "user-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    monkeypatch.setenv("DPC_HOME", "~")
    assert dpc_home() == fake_home

    monkeypatch.setenv("DPC_HOME", "~/my-dpc")
    assert dpc_home() == fake_home / "my-dpc"

    monkeypatch.setenv("DPC_HOME", "~\\my-dpc-win")
    assert dpc_home() == fake_home / "my-dpc-win"


def test_dpc_home_normalizes_relative_and_mixed_separators(monkeypatch, tmp_path):
    from dpc_protocol.paths import dpc_home

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DPC_HOME", "./rel/sub/../dpc_root")
    resolved = dpc_home()
    assert resolved.is_absolute()
    assert resolved == tmp_path / "rel" / "dpc_root"

    mixed = str(tmp_path).replace("\\", "/") + "/posix/style/"
    monkeypatch.setenv("DPC_HOME", mixed)
    assert dpc_home() == tmp_path / "posix" / "style"


def test_dpc_home_handles_non_ascii_directory(monkeypatch, tmp_path):
    from dpc_protocol.paths import dpc_home

    non_ascii = tmp_path / "пользователь_тест" / "ノード" / ".dpc"
    monkeypatch.setenv("DPC_HOME", str(non_ascii))
    assert dpc_home() == non_ascii


def test_protocol_modules_follow_dynamic_dpc_home(monkeypatch, tmp_path):
    from dpc_protocol import commit_integrity, crypto, markdown_manager, pcm_core
    from dpc_protocol.paths import dpc_home

    node_home = tmp_path / "узел_1" / ".dpc"
    monkeypatch.setenv("DPC_HOME", str(node_home))

    assert dpc_home() == node_home
    assert crypto.dpc_home_dir() == node_home

    crypto.generate_identity()
    node_id, key_file, cert_file = crypto.load_identity()
    assert node_id.startswith("dpc-node-")
    assert key_file == node_home / "node.key"
    assert cert_file == node_home / "node.crt"
    assert (node_home / "node.id").exists()

    # MarkdownKnowledgeManager, PCMCore, InstructionSetManager all land under DPC_HOME
    md_mgr = markdown_manager.MarkdownKnowledgeManager()
    assert md_mgr.knowledge_dir == node_home / "knowledge"

    pcm = pcm_core.PCMCore()
    assert pcm.file_path == node_home / "personal.json"

    inst = pcm_core.InstructionSetManager()
    assert inst.instructions_file == node_home / "instructions.json"

    # CommitSigner.verify_signature resolves own cert & peers under DPC_HOME
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert priv is not None
    with open(key_file, "rb") as f:
        loaded_priv = serialization.load_pem_private_key(f.read(), password=None)
    signer = commit_integrity.CommitSigner(node_id, loaded_priv)
    commit_hash = "a" * 64
    sig = signer.sign_commit(commit_hash)
    assert commit_integrity.CommitSigner.verify_signature(node_id, commit_hash, sig) is True


def test_resolve_module_home_honors_path_home_monkeypatch_over_stand_in(monkeypatch, tmp_path):
    from dpc_protocol import paths

    real_home = (Path.home() / ".dpc").resolve()
    stand_in = tmp_path / "stand-in" / ".dpc"
    monkeypatch.setattr(paths, "_TEST_STAND_INS", paths._TEST_STAND_INS | {stand_in})
    monkeypatch.setattr(paths, "_TEST_PROTECTED_HOMES", paths._TEST_PROTECTED_HOMES | {real_home})
    monkeypatch.delenv("DPC_HOME", raising=False)

    real_initial = Path.home() / ".dpc"
    # With neither DPC_HOME nor Path.home patched, stand-in protects real ~/.dpc.
    assert paths.resolve_module_home(stand_in, real_initial) == stand_in
    assert paths.resolve_module_home(stand_in / "node.key", real_initial / "node.key", child="node.key") == stand_in / "node.key"

    # Even if import-time initial differed from real ~/.dpc (e.g. DPC_HOME was set at import),
    # deleting DPC_HOME without patching Path.home must still land on stand-in, never real ~/.dpc.
    other_initial = tmp_path / "import-time-dpc"
    assert paths.resolve_module_home(stand_in, other_initial) == stand_in

    # Once a test monkeypatches Path.home() away from the real home, dynamic Path.home()/.dpc wins.
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert paths.resolve_module_home(stand_in, real_initial) == fake_home / ".dpc"
    assert paths.resolve_module_home(stand_in / "node.key", real_initial / "node.key", child="node.key") == fake_home / ".dpc" / "node.key"

    # Explicit DPC_HOME still beats Path.home() and stand-in.
    explicit = tmp_path / "explicit-dpc"
    monkeypatch.setenv("DPC_HOME", str(explicit))
    assert paths.resolve_module_home(stand_in, real_initial) == explicit

    # Explicit module-attribute monkeypatch (not a stand-in) still wins over all.
    direct = tmp_path / "direct-attr"
    assert paths.resolve_module_home(direct, real_initial) == direct
