"""HF offline auto-detection: go offline only when the disk already has
everything this install loads through the HF cache.

The required set is read from where each id is defined — memory config,
the GLiNER constant, providers.json — so there is nothing here to keep in
sync. What does need guarding is the ordering rule the whole block exists
for: HF_HUB_OFFLINE is read when huggingface_hub is first imported, so
none of those sources may drag it in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Importing run_service executes the HF block, which may set HF_HUB_OFFLINE
# for this process. Tests share one interpreter, so restore what was there.
_saved_offline = os.environ.get("HF_HUB_OFFLINE")
import run_service  # noqa: E402
if _saved_offline is None:
    os.environ.pop("HF_HUB_OFFLINE", None)
else:
    os.environ["HF_HUB_OFFLINE"] = _saved_offline


# ─────────────────────────────────────────────────────────────
# The ordering rule the block depends on
# ─────────────────────────────────────────────────────────────


def test_required_model_sources_do_not_import_huggingface_hub():
    """_hf_required_models() imports our own modules to read the ids. That is
    only allowed while none of them pulls huggingface_hub, which would read
    HF_HUB_OFFLINE before we have set it. Runs in a subprocess because the
    test session has long since imported everything."""
    probe = (
        "import sys\n"
        "from dpc_client_core.dpc_agent.memory_config import MemoryConfig\n"
        "from dpc_client_core.dpc_agent.knowledge_graph import GLINER_MODEL_NAME\n"
        "from dpc_client_core.dpc_agent.tools.transcribe import _WHISPER_PROVIDER_TYPE\n"
        "print('LEAKED' if 'huggingface_hub' in sys.modules else 'CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True,
        cwd=str(Path(run_service.__file__).parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("CLEAN"), (
        "a source of the required-model set now imports huggingface_hub at "
        "module level — HF_HUB_OFFLINE would be read before it is set"
    )


# ─────────────────────────────────────────────────────────────
# Where the ids come from
# ─────────────────────────────────────────────────────────────


def test_required_models_include_embedding_and_gliner_defaults():
    from dpc_client_core.dpc_agent.knowledge_graph import GLINER_MODEL_NAME
    from dpc_client_core.dpc_agent.memory_config import MemoryConfig

    required = run_service._hf_required_models()
    assert MemoryConfig.embedding_model in required
    assert GLINER_MODEL_NAME in required


def test_agent_embedding_override_is_collected(monkeypatch, tmp_path):
    agent_dir = tmp_path / ".dpc" / "agents" / "agent_a"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.json").write_text(
        json.dumps({"memory": {"embedding_model": "some-org/other-embed"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert "some-org/other-embed" in run_service._hf_required_models()


def test_whisper_model_is_read_from_providers_config(monkeypatch, tmp_path):
    from dpc_client_core.dpc_agent.tools.transcribe import _WHISPER_PROVIDER_TYPE

    dpc = tmp_path / ".dpc"
    dpc.mkdir()
    (dpc / "providers.json").write_text(
        json.dumps({"providers": [
            {"alias": "w", "type": _WHISPER_PROVIDER_TYPE, "model": "org/whisper-x"},
            # A remote OpenAI-compatible server names a model we never fetch.
            {"alias": "lm", "type": "openai_compatible", "model": "org/served-elsewhere"},
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    required = run_service._hf_required_models()
    assert "org/whisper-x" in required
    assert "org/served-elsewhere" not in required, (
        "a model served by a remote endpoint is not in our HF cache and must "
        "not be able to keep the process online"
    )


def test_unreadable_sources_contribute_nothing(monkeypatch, tmp_path):
    """A broken config decides nothing. It may cost us a model in the set —
    which only keeps us online — but must never raise at startup."""
    dpc = tmp_path / ".dpc"
    (dpc / "agents" / "agent_a").mkdir(parents=True)
    (dpc / "agents" / "agent_a" / "config.json").write_text("{not json", encoding="utf-8")
    (dpc / "providers.json").write_text("{also not json", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    required = run_service._hf_required_models()  # must not raise
    assert "org/whisper-x" not in required


# ─────────────────────────────────────────────────────────────
# What counts as cached
# ─────────────────────────────────────────────────────────────


def _make_cached_model(root: Path, model_id: str, *, incomplete=False, empty_snapshot=False):
    model_dir = root / ("models--" + model_id.replace("/", "--"))
    snapshot = model_dir / "snapshots" / "deadbeef"
    snapshot.mkdir(parents=True)
    if not empty_snapshot:
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
    blobs = model_dir / "blobs"
    blobs.mkdir()
    (blobs / "abc123").write_text("x", encoding="utf-8")
    if incomplete:
        (blobs / "abc123.incomplete").write_text("half", encoding="utf-8")
    return model_dir


def test_complete_model_counts_as_cached(tmp_path):
    _make_cached_model(tmp_path, "BAAI/bge-m3")
    assert run_service._hf_model_fully_cached("BAAI/bge-m3", tmp_path) is True


def test_absent_model_is_not_cached(tmp_path):
    assert run_service._hf_model_fully_cached("BAAI/bge-m3", tmp_path) is False


def test_interrupted_download_is_not_cached(tmp_path):
    """The case a directory-exists check gets wrong: a half-pulled model looks
    complete from outside, and going offline on it turns the next load into an
    error instead of a resumed download."""
    _make_cached_model(tmp_path, "BAAI/bge-m3", incomplete=True)
    assert run_service._hf_model_fully_cached("BAAI/bge-m3", tmp_path) is False


def test_empty_snapshot_is_not_cached(tmp_path):
    _make_cached_model(tmp_path, "BAAI/bge-m3", empty_snapshot=True)
    assert run_service._hf_model_fully_cached("BAAI/bge-m3", tmp_path) is False


# ─────────────────────────────────────────────────────────────
# Where the cache is
# ─────────────────────────────────────────────────────────────


def test_cache_root_prefers_hf_hub_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "explicit"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    assert run_service._hf_cache_root() == tmp_path / "explicit"


def test_cache_root_falls_back_to_hf_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    assert run_service._hf_cache_root() == tmp_path / "home" / "hub"


def test_cache_root_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert run_service._hf_cache_root() == tmp_path / ".cache" / "huggingface" / "hub"


# ─────────────────────────────────────────────────────────────
# The decision itself
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing_one", [True, False])
def test_offline_decision_follows_the_cache(tmp_path, missing_one):
    """All present → offline is safe. One missing → stay online, because
    offline would turn its download into a failure."""
    models = ["org/a", "org/b", "org/c"]
    for model_id in models[1:] if missing_one else models:
        _make_cached_model(tmp_path, model_id)

    missing = [m for m in models if not run_service._hf_model_fully_cached(m, tmp_path)]
    assert bool(missing) is missing_one
    if missing_one:
        assert missing == [models[0]]


# ─────────────────────────────────────────────────────────────
# The startup decision reaches the log, not just the console
# ─────────────────────────────────────────────────────────────


def test_startup_note_is_recorded_for_the_log():
    """The HF block runs before logging exists, so it can only print. The
    note it keeps is what setup_logging() replays into dpc-client.log —
    without it, a log read days later cannot tell which mode a run used."""
    saved = run_service._HF_STARTUP_NOTE
    try:
        run_service._hf_announce("test note")
        assert run_service._HF_STARTUP_NOTE == "test note"
    finally:
        run_service._HF_STARTUP_NOTE = saved


def test_real_startup_recorded_a_note():
    assert run_service._HF_STARTUP_NOTE, (
        "importing run_service must leave a note describing the HF decision"
    )


# ─────────────────────────────────────────────────────────────
# Which loop closes are worth reporting
# ─────────────────────────────────────────────────────────────


def test_lone_self_pipe_op_is_not_reported_during_normal_operation():
    """Observed after the fix landed: every per-call loop closed with exactly
    one op — a cancelled self-pipe read. Reporting it once per tool call is
    noise that buries the case worth seeing."""
    assert run_service._should_report_pending(1, shutting_down=False) is False


def test_more_than_the_self_pipe_is_reported():
    assert run_service._should_report_pending(2, shutting_down=False) is True


def test_shutdown_reports_even_a_single_op():
    """At the close that can hang, the self-pipe is a suspect like any other."""
    assert run_service._should_report_pending(1, shutting_down=True) is True


def test_nothing_pending_is_never_reported():
    assert run_service._should_report_pending(0, shutting_down=True) is False
    assert run_service._should_report_pending(0, shutting_down=False) is False
