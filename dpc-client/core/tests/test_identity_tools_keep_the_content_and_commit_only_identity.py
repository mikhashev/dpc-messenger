"""The identity tools keep the content and commit only the identity file.

Observed live 2026-11-10 (Ark, agent_001): update_identity timed out because its commit
ran `git add -A` over ~4,900 sandbox files and swept 3,657 unrelated ones into the
commit; the retry doubled the section; deduplicate_identity then reported "removed 3" and
deleted a 21,667-char lessons block and another section's body, because the ranges of the
second heading group were taken after the first group's deletion had moved the lines.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from dpc_client_core.dpc_agent.tools import core as core_mod
from dpc_client_core.dpc_agent.tools.core import deduplicate_identity, update_identity
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.utils import AUTO_COMMIT_GIT_TIMEOUT_SEC, auto_commit_agent_change

LESSONS = "## Lessons\n" + "".join(f"lesson {i}: a thing learned the hard way\n" for i in range(60))


@pytest.fixture
def ctx(tmp_path):
    root = tmp_path / "agent_001"
    (root / "memory").mkdir(parents=True)
    return ToolContext(agent_root=root)


def _identity(ctx):
    return ctx.agent_root / "memory" / "identity.md"


def _sections(text):
    return [line for line in text.split("\n") if line.startswith("## ")]


def _ark_shaped_file():
    # Group 1 (Values) is repeated further down; group 2 (Gender) has an EMPTY first copy
    # right before the copy that carries the text; the lessons block follows both groups.
    return (
        "# Agent Identity\n\n"
        "## Last Updated\n2026-01-01T00:00:00+00:00\n\n"
        "## Values\nhonesty\n\n"
        "## Gender\n\n"
        "## Gender\nshe, by choice\n\n"
        "## Values\nhonesty\n\n"
        f"{LESSONS}\n"
        "## Goals\nstay useful\n"
    )


class TestDeduplicate:
    def test_it_keeps_the_copy_with_content_and_the_rest_of_the_file(self, ctx):
        _identity(ctx).write_text(_ark_shaped_file(), encoding="utf-8")

        result = deduplicate_identity(ctx)

        after = _identity(ctx).read_text(encoding="utf-8")
        assert "removed 2 duplicate section(s)" in result
        assert _sections(after) == ["## Last Updated", "## Values", "## Gender", "## Lessons", "## Goals"]
        assert after.count("she, by choice") == 1
        assert LESSONS in after                      # the block after the second group survives whole
        assert "stay useful" in after
        # Everything but the timestamp and the two removed copies is as it was.
        expected = (
            "# Agent Identity\n\n"
            "## Last Updated\nSTAMP\n\n"
            "## Values\nhonesty\n\n"
            "## Gender\nshe, by choice\n\n"
            f"{LESSONS}\n"
            "## Goals\nstay useful\n"
        )
        stamp = after.split("## Last Updated\n", 1)[1].split("\n", 1)[0]
        assert after == expected.replace("STAMP", stamp)

    def test_the_count_is_the_number_of_ranges_really_deleted(self, ctx):
        text = "# T\n\n## A\none\n\n## A\n\n## A\ntwo\n\n## B\nb\n\n## B\n\n## C\nc\n"
        _identity(ctx).write_text(text, encoding="utf-8")

        result = deduplicate_identity(ctx)

        after = _identity(ctx).read_text(encoding="utf-8")
        assert "removed 3 duplicate section(s)" in result
        assert _sections(after) == ["## A", "## B", "## C"]
        assert "one" in after and "two" not in after   # the first copy with text wins, no merging
        assert "\nb\n" in after and "c" in after

    def test_with_nothing_duplicated_it_changes_nothing(self, ctx):
        _identity(ctx).write_text("# T\n\n## A\na\n\n## B\nb\n", encoding="utf-8")
        before = _identity(ctx).read_bytes()
        assert "No duplicate sections" in deduplicate_identity(ctx)
        assert _identity(ctx).read_bytes() == before

    def test_it_refuses_a_cut_over_the_share_and_writes_nothing(self, ctx):
        body = "x" * 400
        _identity(ctx).write_text(f"# T\n\n## A\n{body}\n\n## A\n{body}\n\n## A\n{body}\n", encoding="utf-8")
        before = _identity(ctx).read_bytes()

        result = deduplicate_identity(ctx)

        assert "Not deduplicated" in result and "nothing was written" in result
        assert _identity(ctx).read_bytes() == before
        assert 0 < core_mod.DEDUPLICATE_MAX_REMOVED_SHARE <= 0.5


class TestUpdateIdentity:
    def test_a_repeated_call_whose_content_carries_its_heading_leaves_one_heading_and_one_block(self, ctx):
        _identity(ctx).write_text(
            "# Agent Identity\n\n## Last Updated\n2026-01-01\n\n## Values\nhonesty\n", encoding="utf-8")
        content = "## Gender\nshe, by choice"

        first = update_identity(ctx, "Gender", content)
        second = update_identity(ctx, "Gender", content)

        after = _identity(ctx).read_text(encoding="utf-8")
        assert _sections(after).count("## Gender") == 1
        assert after.count("she, by choice") == 1
        assert "honesty" in after
        assert "dropped the content's own heading" in first and "dropped the content's own heading" in second
        assert "differs" not in first

    def test_a_heading_that_differs_from_the_section_is_stripped_and_mentioned(self, ctx):
        _identity(ctx).write_text("# Agent Identity\n\n## Values\nhonesty\n", encoding="utf-8")

        result = update_identity(ctx, "Gender", "## Pronouns\nshe")

        after = _identity(ctx).read_text(encoding="utf-8")
        assert "Pronouns" not in after
        assert _sections(after).count("## Gender") == 1 and "she" in after
        assert "differs from section 'Gender'" in result

    def test_creating_the_file_strips_the_heading_too(self, ctx):
        update_identity(ctx, "Gender", "## Gender\nshe")
        assert _sections(_identity(ctx).read_text(encoding="utf-8")).count("## Gender") == 1

    def test_a_heading_inside_the_content_is_refused_and_the_file_is_untouched(self, ctx):
        _identity(ctx).write_text("# Agent Identity\n\n## Values\nhonesty\n", encoding="utf-8")
        before = _identity(ctx).read_bytes()

        result = update_identity(ctx, "Gender", "she\n\n## Notes\nmore")

        assert "Not written" in result and "once per section" in result
        assert _identity(ctx).read_bytes() == before

    def test_content_that_is_only_a_heading_is_refused(self, ctx):
        _identity(ctx).write_text("# Agent Identity\n\n## Gender\nshe\n", encoding="utf-8")
        before = _identity(ctx).read_bytes()
        assert "Not written" in update_identity(ctx, "Gender", "## Gender")
        assert _identity(ctx).read_bytes() == before

    def test_the_schema_tells_the_agent_content_carries_no_headings(self):
        entries = {e.name: e for e in core_mod.get_tools()}
        assert "## " in entries["update_identity"].schema["parameters"]["properties"]["content"]["description"]

    def test_both_tools_outlast_the_git_work_they_start(self):
        entries = {e.name: e for e in core_mod.get_tools()}
        for name in ("update_identity", "deduplicate_identity"):
            assert entries[name].timeout_sec >= 2 * AUTO_COMMIT_GIT_TIMEOUT_SEC


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=True).stdout


@pytest.fixture
def repo(ctx):
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH here")
    root = ctx.agent_root
    _git(root, "init", "-q")
    for cfg in (("user.name", "a"), ("user.email", "a@dpc-local"), ("commit.gpgsign", "false"),
                ("core.hooksPath", str(root.parent / "no_hooks"))):
        _git(root, "config", *cfg)
    (root / "memory" / "identity.md").write_text("# Agent Identity\n", encoding="utf-8")
    (root / "notes.txt").write_text("v1", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


class TestTheCommit:
    def test_paths_stage_and_commit_only_those_files(self, repo):
        (repo / "memory" / "identity.md").write_text("# Agent Identity\n\n## Gender\nshe\n", encoding="utf-8")
        (repo / "notes.txt").write_text("v2 unrelated edit", encoding="utf-8")
        (repo / "stray.txt").write_text("untracked", encoding="utf-8")

        auto_commit_agent_change(repo, "identity: one file", paths=["memory/identity.md"])

        assert _git(repo, "show", "--name-only", "--format=%s", "HEAD").split() == ["identity:", "one", "file", "memory/identity.md"]
        status = _git(repo, "status", "--porcelain")
        assert " M notes.txt" in status and "?? stray.txt" in status

    def test_a_repeat_with_nothing_new_in_the_path_is_not_a_warning(self, repo, caplog):
        auto_commit_agent_change(repo, "again", paths=["memory/identity.md"])
        assert "Auto-commit failed" not in caplog.text

    def test_without_paths_the_whole_sandbox_is_still_committed(self, repo):
        (repo / "notes.txt").write_text("v2", encoding="utf-8")
        (repo / "stray.txt").write_text("untracked", encoding="utf-8")
        auto_commit_agent_change(repo, "everything")
        assert _git(repo, "status", "--porcelain").strip() == ""

    def test_update_identity_commits_the_identity_file_and_leaves_other_edits(self, repo):
        (repo / "notes.txt").write_text("v2 unrelated edit", encoding="utf-8")
        ctx = ToolContext(agent_root=repo)

        update_identity(ctx, "Gender", "she", commit_message="chore(identity): gender")

        assert _git(repo, "show", "--name-only", "--format=%s", "HEAD").split() == ["chore(identity):", "gender", "memory/identity.md"]
        assert " M notes.txt" in _git(repo, "status", "--porcelain")

    def test_deduplicate_identity_commits_so_it_can_be_rolled_back(self, repo):
        (repo / "memory" / "identity.md").write_text("# T\n\n## A\none\n\n## A\n\n## B\nb\n", encoding="utf-8")
        (repo / "notes.txt").write_text("v2 unrelated edit", encoding="utf-8")

        deduplicate_identity(ToolContext(agent_root=repo))

        assert _git(repo, "show", "--name-only", "--format=%s", "HEAD").split() == ["chore(identity):", "deduplicate", "sections", "memory/identity.md"]
        assert " M notes.txt" in _git(repo, "status", "--porcelain")
