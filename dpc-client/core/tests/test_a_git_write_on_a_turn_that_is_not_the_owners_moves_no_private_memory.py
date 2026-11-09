"""A git write on a turn that is not the owner's moves no private memory (ADR-048).

The file gate closed the agent's memory to strangers and to groups with another node,
but the git write tools still ran in the agent's own repository, where add, commit,
checkout, reset and merge move the whole tree, memory/ and knowledge/ included. They
now follow the file gate: on such a turn a write in the agent's own repository is
refused, a repository of its own below the root is written only where the file gate
would open it, and an external repository follows the extended-path rule. Owner turns
are unchanged. The reads with a repo_path (git_diff, git_log, git_branch) take the same
gate.
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.tools import git as git_mod
from tests.test_a_turn_that_is_not_the_owners_opens_no_private_file import (  # noqa: F401
    mixed, owner, stranger, world,
)

WRITES = {
    "git_add": lambda c, path=".", repo=None: git_mod.git_add(c, ["notes.txt"], path=path, repo_path=repo),
    "git_commit": lambda c, path=".", repo=None: git_mod.git_commit(c, "chore: x", path=path, repo_path=repo),
    "git_checkout": lambda c, path=".", repo=None: git_mod.git_checkout(c, "main", path=path, repo_path=repo),
    "git_merge": lambda c, path=".", repo=None: git_mod.git_merge(c, "side", path=path, repo_path=repo),
    "git_tag": lambda c, path=".", repo=None: git_mod.git_tag(c, "t1", path=path, repo_path=repo),
    "git_snapshot": lambda c, path=".", repo=None: git_mod.git_snapshot(c, path=path, repo_path=repo),
    "git_reset": lambda c, path=".", repo=None: git_mod.git_reset(c, path=path),
    "git_reset_files": lambda c, path=".", repo=None: git_mod.git_reset(c, files=["a"], path=path),
    "git_init": lambda c, path=".", repo=None: git_mod.git_init(c, path=path),
}
WITH_REPO_PATH = ["git_add", "git_commit", "git_checkout", "git_merge", "git_tag", "git_snapshot"]


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def _git(*a, **kw):
        calls.append(a)
        return {"success": True, "output": "ok"}

    monkeypatch.setattr(git_mod, "_run_git", _git)
    monkeypatch.setattr(git_mod, "_run_git_external", _git)
    return calls


@pytest.fixture
def repos(world):  # noqa: F811
    """work/ is part of the agent's own repository; workrepo/ is a repository of its own."""
    (world.root / "workrepo" / ".git").mkdir(parents=True)
    (world.root / ".git").mkdir()
    return world


def _refused(answer):
    assert answer.startswith("⚠️ Access denied:") and "not the owner's" in answer, answer
    assert "\n" not in answer, answer


@pytest.mark.parametrize("tool", list(WRITES))
@pytest.mark.parametrize("who", ["stranger", "mixed"])
@pytest.mark.parametrize("path", [".", "work", "memory"])
def test_a_git_write_in_the_agent_s_own_repository_is_refused(repos, ran, tool, who, path):
    if tool == "git_init" and path == "work" and who == "mixed":
        pytest.skip("init in an open work folder makes a repository of its own (next test)")
    ctx = stranger(repos) if who == "stranger" else mixed(repos)
    _refused(WRITES[tool](ctx, path=path))
    assert ran == []


@pytest.mark.parametrize("tool", list(WRITES))
def test_a_mixed_group_writes_in_a_repository_of_its_own_and_it_is_logged(repos, ran, tool, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.tools.core"):
        answer = WRITES[tool](mixed(repos), path="workrepo")
    assert not answer.startswith("⚠️"), answer
    assert ran
    assert any("non-owner turn wrote with git in workrepo" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("tool", list(WRITES))
def test_a_stranger_writes_in_no_repository_below_the_root(repos, ran, tool):
    _refused(WRITES[tool](stranger(repos), path="workrepo"))
    assert ran == []


def test_git_init_opens_a_new_work_folder_to_a_mixed_group_but_not_memory(repos, ran):
    assert not git_mod.git_init(mixed(repos), path="work/new").startswith("⚠️")
    assert not git_mod.git_init(mixed(repos), path="work").startswith("⚠️")
    _refused(git_mod.git_init(mixed(repos), path="memory/sub"))
    _refused(git_mod.git_init(mixed(repos), path="."))


@pytest.mark.parametrize("tool", WITH_REPO_PATH)
@pytest.mark.parametrize("who", ["stranger", "mixed"])
def test_an_external_repository_takes_the_extended_path_rule(repos, ran, tool, who):
    ctx = stranger(repos) if who == "stranger" else mixed(repos)
    _refused(WRITES[tool](ctx, repo=str(repos.ext_root)))
    assert ran == []


def test_git_push_takes_the_extended_path_rule(repos, ran):
    _refused(git_mod.git_push(stranger(repos), str(repos.ext_root)))
    _refused(git_mod.git_push(mixed(repos), str(repos.ext_root)))
    assert ran == []


def test_ext_shared_opens_an_external_checkout_to_a_mixed_group(repos, ran):
    ctx = mixed(repos, layers=frozenset({"L6", "EXT"}))
    assert not git_mod.git_checkout(ctx, "main", repo_path=str(repos.ext_root)).startswith("⚠️")
    assert ran


@pytest.mark.parametrize("read", [
    lambda c, r: git_mod.git_diff(c, ".", repo_path=r),
    lambda c, r: git_mod.git_log(c, ".", repo_path=r),
    lambda c, r: git_mod.git_branch(c, repo_path=r),
], ids=["git_diff", "git_log", "git_branch"])
def test_a_git_read_of_an_external_repository_takes_the_same_gate(repos, ran, read):
    _refused(read(stranger(repos), str(repos.ext_root)))
    _refused(read(mixed(repos), str(repos.ext_root)))
    assert ran == []
    assert not read(mixed(repos, layers=frozenset({"L6", "EXT"})), str(repos.ext_root)).startswith("⚠️")
    assert not read(owner(repos), str(repos.ext_root)).startswith("⚠️")


@pytest.mark.parametrize("tool", list(WRITES))
def test_the_owner_s_git_writes_run_as_before(repos, ran, tool):
    assert not WRITES[tool](owner(repos), path=".").startswith("⚠️")
    assert ran


def test_the_owner_s_external_checkout_runs_as_before(repos, ran):
    assert not git_mod.git_checkout(owner(repos), "main", repo_path=str(repos.ext_root)).startswith("⚠️")
    assert ran
