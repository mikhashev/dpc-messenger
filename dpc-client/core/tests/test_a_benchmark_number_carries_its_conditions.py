"""A score without its conditions is an anecdote with a decimal point.

Two arguments in one day turned on exactly this: the same corpus counted three
ways, and a published 69.8 % that had been run with hybrid recall on against a
second daemon — neither knowable from the number.
"""

import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))

from _harness import provenance  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _snapshot(**over):
    args = dict(
        repo_root=REPO_ROOT,
        provider_entry={"alias": "a", "model": "m", "temperature": 0.7},
        dataset={"repo": "r", "revision": "abc"},
        harness_file=EVAL / "gaia" / "run_gaia_eval.py",
        argv=["--temperature", "0.7"],
    )
    args.update(over)
    return provenance.snapshot(**args)


def test_a_secret_never_reaches_the_report():
    block = _snapshot(provider_entry={
        "alias": "a", "api_key": "sk-live-1234", "HF_TOKEN": "hf_abc",
        "api_key_env": "ANTHROPIC_API_KEY",
    })

    assert block["provider"]["api_key"] == "[redacted]"
    assert block["provider"]["HF_TOKEN"] == "[redacted]"
    assert block["provider"]["api_key_env"] == "ANTHROPIC_API_KEY", (
        "the *name* of an env var is not a secret and is needed to reproduce"
    )


def test_an_inherited_reasoning_effort_is_labelled_as_inherited(monkeypatch):
    """The trap this exists for: absent is not 'default', it is unrecorded.

    Changed 2026-09-23: the value used to be the constant "xhigh" — one model's
    default written down for every model. It is now read from the GGUF's own
    template, and recorded as unknown where there is none to read.
    """
    inherited = _snapshot(provider_entry={"alias": "a"})
    assert inherited["reasoning_effort"]["value"] is None
    assert "template" in inherited["reasoning_effort"]["source"]

    import dpc_client_core.managers.llama_server_supervisor as sup
    monkeypatch.setattr(sup, "gguf_effort_dictionary",
                        lambda path: (("low", "high"), "low") if path == "m.gguf" else None)
    read = _snapshot(provider_entry={"alias": "a", "gguf_path": "m.gguf"})
    assert read["reasoning_effort"]["value"] == "low"
    assert "read from the GGUF" in read["reasoning_effort"]["source"]

    pinned = _snapshot(provider_entry={"alias": "a", "reasoning_effort": "high"})
    assert pinned["reasoning_effort"]["value"] == "high"
    assert "pinned" in pinned["reasoning_effort"]["source"]


def test_a_dirty_tree_names_its_files():
    block = _snapshot()
    repo = block["code"]["repo"]

    assert repo["sha"] and len(repo["sha"]) >= 7
    assert isinstance(repo["dirty"], bool)
    if repo["dirty"]:
        assert repo["dirty_file_count"] > 0
        assert repo["dirty_files"], "'dirty: true' with no list is unreproducible"


def test_the_sampling_parameters_travel_with_the_score():
    block = _snapshot(provider_entry={
        "alias": "a", "model": "m", "temperature": 1.0, "top_p": 0.95,
        "top_k": 20, "context_window": 215040, "cache_type_k": "q4_0",
    })

    for key in ("temperature", "top_p", "top_k", "context_window", "cache_type_k"):
        assert key in block["provider"], f"{key} decides the number and must be recorded"


def test_collection_never_raises_and_says_so_when_it_cannot_read():
    block = _snapshot(repo_root=Path("/definitely/not/a/repo/anywhere"))

    assert isinstance(block, dict), "a provenance bug must not cost a run"
    repo = block["code"]["repo"]
    assert repo.get("error") or repo.get("sha", "").startswith("["), (
        "an unreadable repo is recorded as unreadable, not silently omitted"
    )


def test_the_machine_is_recorded_because_the_score_depends_on_it():
    block = _snapshot()

    assert "gpu" in block["machine"]
    assert block["machine"]["cpu_count"]
    assert block["machine"]["python"]


# --- what the 2026-08-30 history audit found in the provenance itself -------


def test_a_number_is_never_a_secret():
    """`reasoning_budget_tokens` carries "token" in its name and is a count.

    Every provenance file on disk reads `"[redacted]"` where the reasoning
    budget was, because the filter matched the key and never looked at the
    value. (GLM 5.3, 2026-08-30.)
    """
    block = _snapshot(provider_entry={
        "alias": "a", "reasoning_budget_tokens": 10000, "api_key": "sk-live-1",
    })

    assert block["provider"]["reasoning_budget_tokens"] == 10000
    assert block["provider"]["api_key"] == "[redacted]", "a string secret still goes"


def test_the_first_dirty_file_keeps_its_first_character(tmp_path, monkeypatch):
    """`git status --porcelain` puts the status in columns 1-2, so an unstaged
    edit is ` M path`; stripping the blob ate that leading space and the `[3:]`
    that removes the columns then ate a character of the path. Measured in the
    2026-08-29 provenance files, which all name `pc-client/...`. (Fable 5.)
    """
    calls = {}

    def _fake_run(cmd, cwd=None, timeout=30, keep_indent=False):
        calls[tuple(cmd[:3])] = keep_indent
        if cmd[1] == "status":
            out = " M dpc-client/core/tests/test_x.py\n?? eval/gaia/results/\n"
            return out.rstrip() if keep_indent else out.strip()
        return "deadbeef" if cmd[1] == "rev-parse" else ""

    monkeypatch.setattr(provenance, "_run", _fake_run)
    repo = provenance._git(REPO_ROOT)

    assert repo["dirty_files"][0] == "dpc-client/core/tests/test_x.py"
    assert repo["dirty_files"][1] == "eval/gaia/results/"
    assert calls[("git", "status", "--porcelain")] is True


# --- the model by its files, 2026-09-23 --------------------------------------
# The alias `qwen3.8 27b` carried the label "qwen3.8 27b Mythos" after the
# alias had been renamed: the label is free text. The file and its digest are
# what identify a model.


def test_a_model_file_is_hashed_once_and_then_read_from_the_cache(tmp_path, monkeypatch):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 1000)
    cache = tmp_path / "results"

    first = provenance.file_sha256(gguf, cache)
    assert first["sha256_from_cache"] is False
    assert len(first["sha256"]) == 64

    def _must_not_hash(*a, **k):
        raise AssertionError("a cache hit must not read the file again")

    monkeypatch.setattr(provenance.hashlib, "sha256", _must_not_hash)
    second = provenance.file_sha256(gguf, cache)
    assert second["sha256_from_cache"] is True
    assert second["sha256"] == first["sha256"]


def test_a_changed_file_is_hashed_again(tmp_path):
    import os
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"one")
    first = provenance.file_sha256(gguf, tmp_path)
    gguf.write_bytes(b"two!")
    os.utime(gguf, ns=(first["mtime_ns"] + 10**9, first["mtime_ns"] + 10**9))

    second = provenance.file_sha256(gguf, tmp_path)
    assert second["sha256_from_cache"] is False
    assert second["sha256"] != first["sha256"]


def test_a_llama_server_alias_records_its_files_binary_and_pin(tmp_path, monkeypatch):
    from dpc_client_core.managers import llama_server_fetcher as fetcher

    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    binary = tmp_path / "llama-server.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(fetcher, "resolve_binary", lambda entry: binary)
    monkeypatch.setattr(provenance.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "", "stderr": "version: 0.4.1 (build 10964)\nbuilt with X\n"})())

    record = provenance.model_files(
        {"type": "llamacpp_server", "model": "stale label", "gguf_path": str(gguf)}, tmp_path)

    assert record["gguf"]["sha256"] and record["gguf"]["path"] == str(gguf)
    assert record["model_label"] == "stale label" and "free label" in record["note"]
    server = record["llama_server"]
    assert server["pinned_tag"] == fetcher.LLAMA_CPP_TAG
    assert server["binary"] == str(binary) and server["binary_is_the_pin"] is True
    assert any("10964" in line for line in server["version"])


def test_a_provider_without_local_files_says_so():
    record = provenance.model_files({"type": "ollama", "model": "x"})
    assert "no local model files" in record["note"]
