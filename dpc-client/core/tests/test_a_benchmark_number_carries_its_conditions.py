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


def test_an_inherited_reasoning_effort_is_labelled_as_inherited():
    """The trap this exists for: absent is not 'default', it is unrecorded."""
    inherited = _snapshot(provider_entry={"alias": "a"})
    assert inherited["reasoning_effort"]["value"] == "xhigh"
    assert "template" in inherited["reasoning_effort"]["source"]

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
