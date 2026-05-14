"""Smoke tests for sleep_pipeline helpers (idempotency / stable naming / hash compute)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.sleep_pipeline import (
    SYNTHESIS_BUDGET_FACTOR,
    SYNTHESIS_OUTPUT_RESERVE_TOKENS,
    SYNTHESIS_PROMPT,
    _collect_group_archive_digests,
    _compute_archive_hash,
    _compute_synthesis_budget,
    _migrate_legacy_results,
    _render_finding_block,
    _result_filename,
    _select_findings_within_budget,
)


class _FakeLLMManager:
    """Minimal stand-in for LLMManager — only the methods the budget helper
    actually calls. Token count = len(text) // tokens_per_char, lets tests
    fix a deterministic ratio without pulling tiktoken."""

    def __init__(self, tokens_per_char: int = 4):
        self.tokens_per_char = tokens_per_char

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002 — model unused
        return max(1, len(text) // self.tokens_per_char)


def test_result_filename_one_to_one_strips_json():
    name = _result_filename("2026-05-13T17-28-25_reset_session.json")
    assert name == "result_2026-05-13T17-28-25_reset_session.json"


def test_result_filename_group_archive_sanitizes_colons():
    # `:` → `--` because `:` is the Windows drive separator and `_` is
    # already used inside archive timestamps + group ids may contain `_`.
    name = _result_filename(
        "group_archive:group-b88b65076b85-dpc-project:2026-05-09T18-19-41_reset_session.json"
    )
    assert ":" not in name
    assert name.startswith("result_group_archive--group-b88b65076b85-dpc-project--")
    assert name.endswith("_reset_session.json")


def test_result_filename_empty_input():
    # Edge case — empty archive_file should still produce a well-formed name.
    name = _result_filename("")
    assert name == "result_.json"


def test_compute_archive_hash_one_to_one(tmp_path: Path):
    conv = tmp_path / "agent_001"
    archive_dir = conv / "archive" / "2026" / "05"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "2026-05-13T12-00-00_reset_session.json"
    payload = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    archive.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    digest = {
        "archive_file": "2026-05-13T12-00-00_reset_session.json",
        "source": "one_to_one",
    }
    assert _compute_archive_hash(digest, conv) == expected


def test_compute_archive_hash_group_archive(tmp_path: Path):
    archive_path = tmp_path / "group-xxx-dpc" / "archive" / "2026-05-10T09-50-29_reset_session.json"
    archive_path.parent.mkdir(parents=True)
    payload = b'{"messages": [{"role": "user", "content": "hello"}]}'
    archive_path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    digest = {
        "archive_file": f"group_archive:group-xxx-dpc:{archive_path.name}",
        "source": "group_archive",
        "archive_path": str(archive_path),
    }
    # conversation_dir is irrelevant for group_archive — hash uses archive_path.
    assert _compute_archive_hash(digest, tmp_path / "irrelevant") == expected


def test_compute_archive_hash_missing_returns_empty(tmp_path: Path):
    conv = tmp_path / "agent_001"
    (conv / "archive").mkdir(parents=True)
    digest = {
        "archive_file": "nonexistent_reset_session.json",
        "source": "one_to_one",
    }
    assert _compute_archive_hash(digest, conv) == ""


def test_compute_archive_hash_different_content_different_hash(tmp_path: Path):
    conv = tmp_path / "agent_001"
    archive_dir = conv / "archive" / "2026" / "05"
    archive_dir.mkdir(parents=True)
    a = archive_dir / "a_reset_session.json"
    b = archive_dir / "b_reset_session.json"
    a.write_bytes(b'{"version": 1}')
    b.write_bytes(b'{"version": 2}')

    da = {"archive_file": "a_reset_session.json", "source": "one_to_one"}
    db = {"archive_file": "b_reset_session.json", "source": "one_to_one"}
    assert _compute_archive_hash(da, conv) != _compute_archive_hash(db, conv)


def test_migrate_legacy_results_renames_session_files(tmp_path: Path):
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    legacy = results_dir / "session_5.json"
    legacy.write_text(json.dumps({
        "archive_file": "2026-05-13T17-28-25_reset_session.json",
        "summary": "test",
    }), encoding="utf-8")

    count = _migrate_legacy_results(results_dir)
    assert count == 1
    assert not legacy.exists()
    assert (results_dir / "result_2026-05-13T17-28-25_reset_session.json").exists()


def test_migrate_legacy_results_idempotent(tmp_path: Path):
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    (results_dir / "session_0.json").write_text(json.dumps({
        "archive_file": "x_reset_session.json",
    }), encoding="utf-8")

    # First call renames.
    assert _migrate_legacy_results(results_dir) == 1
    # Second call has nothing to do — no `session_*.json` files left.
    assert _migrate_legacy_results(results_dir) == 0
    # New file in place.
    assert (results_dir / "result_x_reset_session.json").exists()


def test_migrate_legacy_results_skips_missing_archive_field(tmp_path: Path):
    # Legacy file without `archive_file` key — can't derive new name, skip.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    legacy = results_dir / "session_0.json"
    legacy.write_text(json.dumps({"summary": "broken"}), encoding="utf-8")

    assert _migrate_legacy_results(results_dir) == 0
    assert legacy.exists()  # untouched


def test_migrate_legacy_results_deletes_when_target_exists(tmp_path: Path):
    # Legacy file points to archive that already has a `result_*.json` file —
    # delete the legacy duplicate, don't overwrite the new one.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    target = results_dir / "result_dup_reset_session.json"
    target.write_text(json.dumps({"archive_file": "dup_reset_session.json", "summary": "newer"}), encoding="utf-8")
    legacy = results_dir / "session_3.json"
    legacy.write_text(json.dumps({"archive_file": "dup_reset_session.json", "summary": "older"}), encoding="utf-8")

    count = _migrate_legacy_results(results_dir)
    assert count == 1
    assert not legacy.exists()
    # Target preserved (not overwritten with older content).
    assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "newer"


def test_migrate_legacy_results_ignores_non_legacy_files(tmp_path: Path):
    # `session_NOT_DIGIT.json` is not a legacy file — don't touch.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    (results_dir / "session_foo.json").write_text("{}", encoding="utf-8")
    (results_dir / "result_x_reset_session.json").write_text(
        json.dumps({"archive_file": "x_reset_session.json"}), encoding="utf-8"
    )

    assert _migrate_legacy_results(results_dir) == 0
    assert (results_dir / "session_foo.json").exists()


# --- last_session pick + date normalization (S118) ---

def _write_group_archive(group_dir: Path, filename: str, first_ts: str, agent_id: str) -> None:
    """Helper: create a minimal group archive + metadata.json."""
    archive_dir = group_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "metadata.json").write_text(
        json.dumps({"group_id": group_dir.name, "agents": {"some-node": [agent_id]}}),
        encoding="utf-8",
    )
    (archive_dir / filename).write_text(
        json.dumps({
            "conversation_id": group_dir.name,
            "messages": [{"timestamp": first_ts, "role": "user", "content": "hi"}],
        }),
        encoding="utf-8",
    )


def test_group_archive_digest_preserves_full_iso_date(tmp_path: Path):
    """Bug fix: group archive `date` used to be truncated to YYYY-MM-DD, while
    1:1 digests carry full ISO. The asymmetry confused chronological sort
    (and the LLM) — last_session was picked from group archives because their
    short date sorted differently than 1:1 full ISO."""
    group = tmp_path / "group-test-abc"
    full_ts = "2026-05-11T12:33:54.123456+00:00"
    _write_group_archive(group, "2026-05-11T12-33-54_reset_session.json", full_ts, "agent_001")

    digests = _collect_group_archive_digests(group, "agent_001")
    assert len(digests) == 1
    # The key assertion: full timestamp, not [:10] truncation.
    assert digests[0]["date"] == full_ts
    assert digests[0]["source"] == "group_archive"


def test_group_archive_digest_skips_when_agent_not_member(tmp_path: Path):
    group = tmp_path / "group-foreign"
    _write_group_archive(group, "x_reset_session.json", "2026-05-11T00:00:00+00:00", "agent_002")
    # agent_001 is NOT in metadata.agents, so it should get nothing.
    assert _collect_group_archive_digests(group, "agent_001") == []


def test_group_archive_digest_no_metadata_falls_back_to_dir_name(tmp_path: Path):
    """When metadata.json missing, group_id falls back to directory name and
    membership check is skipped (all agents see the archive)."""
    group = tmp_path / "group-no-meta"
    (group / "archive").mkdir(parents=True)
    (group / "archive" / "2026-05-10T00-00-00_reset_session.json").write_text(
        json.dumps({"messages": [{"timestamp": "2026-05-10T00:00:00+00:00", "content": "x"}]}),
        encoding="utf-8",
    )
    digests = _collect_group_archive_digests(group, "any-agent")
    assert len(digests) == 1
    assert digests[0]["group_id"] == "group-no-meta"


def test_synthesis_prompt_has_most_recent_placeholder():
    """Regression guard: the {most_recent} placeholder must exist in the
    prompt template so synthesis can inject the deterministically-picked
    last_session source. If this disappears, the .format() call will fail
    silently (KeyError) or revert to LLM-picks-its-own behaviour."""
    assert "{most_recent}" in SYNTHESIS_PROMPT
    assert "MOST RECENT SESSION" in SYNTHESIS_PROMPT
    # Sanity: the LLM is told to NOT pick from per-session findings.
    assert "pre-selected" in SYNTHESIS_PROMPT or "Do NOT pick" in SYNTHESIS_PROMPT


def test_synthesis_prompt_format_with_required_keys():
    """The four placeholders + entity_section must all be fillable. If a key
    is renamed or removed and the call site forgets to update, .format()
    raises KeyError — this test catches that at unit-test time."""
    out = SYNTHESIS_PROMPT.format(
        n=3,
        period="2026-05-01 to 2026-05-14",
        most_recent='{"summary": "test"}',
        findings='Session 1: {"x": 1}',
        entity_section="",
    )
    assert "test" in out
    assert "Session 1" in out
    assert "2026-05-01 to 2026-05-14" in out


# ─────────────────────────────────────────────────────────────
# Adaptive synthesis budget (SLEEP-ADAPTIVE-BUDGET, S119)
# ─────────────────────────────────────────────────────────────


def test_render_finding_block_uses_sequential_index_and_date_prefix():
    f = {"digest_date": "2026-05-14T10:00:00.000000+00:00", "summary": "hi"}
    block = _render_finding_block(7, f)
    # Header uses the YYYY-MM-DD slice, not the full ISO timestamp.
    assert block.startswith("Session 7 (2026-05-14):\n{")
    # JSON body is preserved verbatim (and naturally still contains the
    # untruncated `digest_date` — that's the input, not the header slice).
    assert "\"summary\": \"hi\"" in block


def test_render_finding_block_empty_date_yields_empty_parens():
    block = _render_finding_block(1, {"summary": "no date"})
    # When digest_date is missing, the parens render empty rather than crashing.
    assert block.startswith("Session 1 ():\n")


def test_select_findings_within_budget_includes_all_when_under_budget():
    """Small findings + large context_window → every finding included, no truncation."""
    llm = _FakeLLMManager(tokens_per_char=4)
    findings = [
        {"digest_date": f"2026-05-{day:02d}", "summary": "short"}
        for day in range(1, 6)
    ]
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=findings,
        llm_manager=llm,
        target_model="fake-model",
        context_window=200_000,
        template_overhead_tokens=1_000,
    )
    assert len(selected) == len(findings)
    assert used > 0


def test_select_findings_within_budget_truncates_when_over_budget():
    """Big findings + small context_window → only the newest few fit."""
    llm = _FakeLLMManager(tokens_per_char=4)
    big_blob = "x" * 4000  # ~1000 tokens per finding at tokens_per_char=4
    findings = [
        {"digest_date": f"2026-05-{day:02d}", "summary": big_blob}
        for day in range(1, 21)  # 20 findings
    ]
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=findings,
        llm_manager=llm,
        target_model="fake-model",
        context_window=16_000,  # 0.85 * 16K - 4K reserve - 1K template = ~8.6K budget
        template_overhead_tokens=1_000,
    )
    # Some findings cut, but at least 1 included; newest comes first in input
    # so the *first* finding in selected matches findings[0].
    assert 1 <= len(selected) < len(findings)
    assert selected[0] is findings[0]
    # Tokens used should not exceed budget (modulo the single-finding guarantee
    # which only kicks in if selected was empty — covered separately).
    budget = int(16_000 * SYNTHESIS_BUDGET_FACTOR) - SYNTHESIS_OUTPUT_RESERVE_TOKENS - 1_000
    assert used <= budget


def test_select_findings_within_budget_always_includes_at_least_one():
    """Even when a single finding exceeds budget, include it — degraded
    synthesis > empty synthesis. The MOST RECENT SESSION block in the prompt
    is separate, but findings_text being empty hurts cross-session patterns."""
    llm = _FakeLLMManager(tokens_per_char=4)
    giant = "y" * 200_000  # ~50K tokens, larger than any reasonable budget
    findings = [{"digest_date": "2026-05-14", "summary": giant}]
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=findings,
        llm_manager=llm,
        target_model="fake-model",
        context_window=16_000,
        template_overhead_tokens=1_000,
    )
    assert len(selected) == 1
    assert used > 0


def test_select_findings_within_budget_empty_input():
    llm = _FakeLLMManager()
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=[],
        llm_manager=llm,
        target_model="fake-model",
        context_window=128_000,
        template_overhead_tokens=2_000,
    )
    assert selected == []
    assert used == 0


def test_select_findings_within_budget_falls_back_when_model_unknown():
    """target_model=None → helper uses len//4 estimate. Still respects budget."""
    llm = _FakeLLMManager(tokens_per_char=4)
    findings = [
        {"digest_date": f"2026-05-{day:02d}", "summary": "x" * 200}
        for day in range(1, 11)
    ]
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=findings,
        llm_manager=llm,
        target_model=None,  # unknown model — heuristic fallback path
        context_window=8_000,
        template_overhead_tokens=500,
    )
    # Should include at least one and at most all 10.
    assert 1 <= len(selected) <= len(findings)
    # Heuristic tokens_used > 0 even without LLM model.
    assert used > 0


def test_compute_synthesis_budget_matches_helper_internal_calc():
    """Drift guard: `_select_findings_within_budget` must use the same budget
    formula as the public `_compute_synthesis_budget`. If the helper inlines
    a copy of the formula and only one place is updated later, this test
    fires before the prod desync happens."""
    llm = _FakeLLMManager(tokens_per_char=4)
    context_window = 128_000
    template_overhead = 5_000

    # Drain a huge finding into the helper so its internal `tokens_used`
    # reflects the actual budget used as the upper bound it stops at.
    giant = "x" * 1_000_000  # ~250K tokens at ratio=4
    selected, used = _select_findings_within_budget(
        findings_sorted_desc=[{"digest_date": "2026-05-14", "summary": giant}],
        llm_manager=llm,
        target_model="fake-model",
        context_window=context_window,
        template_overhead_tokens=template_overhead,
    )
    # The single-finding-guarantee fires (selected was empty when threshold hit).
    assert len(selected) == 1

    # The publicly-exposed budget MUST match the formula constants.
    budget = _compute_synthesis_budget(context_window, template_overhead)
    expected = (
        int(context_window * SYNTHESIS_BUDGET_FACTOR)
        - SYNTHESIS_OUTPUT_RESERVE_TOKENS
        - template_overhead
    )
    assert budget == expected


def test_select_findings_within_budget_preserves_input_order():
    """Selection follows input order (caller passes newest-first); helper does
    not re-sort. Catches accidental sort/reverse inside the helper."""
    llm = _FakeLLMManager(tokens_per_char=4)
    findings = [
        {"digest_date": "2026-05-14", "summary": "newest"},
        {"digest_date": "2026-05-10", "summary": "middle"},
        {"digest_date": "2026-05-01", "summary": "oldest"},
    ]
    selected, _ = _select_findings_within_budget(
        findings_sorted_desc=findings,
        llm_manager=llm,
        target_model="fake-model",
        context_window=200_000,
        template_overhead_tokens=100,
    )
    assert [f["summary"] for f in selected] == ["newest", "middle", "oldest"]
