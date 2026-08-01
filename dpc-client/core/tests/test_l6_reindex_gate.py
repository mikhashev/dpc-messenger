"""Approving a commit must not index it into an agent forbidden the shared layer.

Found from a live log Mike pasted: the reindex reported success for `iris`, whose
`human_knowledge_access` is False. `L6 reindex skipped` has zero occurrences across
every log file on this machine, against 175 reindexes — the gate read
`getattr(self, '_firewall', None)`, a field nothing ever assigned, so `if firewall and
not …` was always False and the check never ran.

It failed open, which is the part worth a test: a permission that silently stops being
asked is worse than one that refuses.
"""

from __future__ import annotations

import types

import pytest

from dpc_client_core.knowledge_service import KnowledgeService


class _Firewall:
    def __init__(self, allowed: set):
        self.allowed = allowed

    def can_agent_access_context(self, context_type, profile_name=None):
        return profile_name in self.allowed


class _AgentManager:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self._agent = None  # no embedding provider — the loop stops before indexing


def _service(firewall, agent_ids, tmp_path):
    """A KnowledgeService with only what this code path touches."""
    svc = types.SimpleNamespace()
    svc.firewall = firewall
    svc.dpc_home_dir = tmp_path
    provider = types.SimpleNamespace(_managers={a: _AgentManager(a) for a in agent_ids})
    svc.llm_manager = types.SimpleNamespace(providers={"dpc_agent": provider})
    svc.p2p_manager = types.SimpleNamespace()
    svc._reindex_commit_into_agents = types.MethodType(
        KnowledgeService._reindex_commit_into_agents, svc)
    return svc


@pytest.fixture
def commit_file(tmp_path):
    path = tmp_path / "commit.md"
    path.write_text("# shared knowledge\nbody", encoding="utf-8")
    return path


def _run(svc, commit_file, caplog):
    with caplog.at_level("INFO"):
        svc._reindex_commit_into_agents(commit_file.name)
    return [r.getMessage() for r in caplog.records]


def test_an_agent_without_shared_knowledge_access_is_skipped(commit_file, tmp_path, caplog):
    svc = _service(_Firewall({"allowed_agent"}), ["allowed_agent", "denied_agent"], tmp_path)

    messages = _run(svc, commit_file, caplog)

    assert any("skipped for denied_agent" in m for m in messages)
    assert not any("skipped for allowed_agent" in m for m in messages)


def test_no_firewall_skips_every_agent_rather_than_indexing_them_all(commit_file, tmp_path, caplog):
    """The actual production behaviour, inverted. Without something to ask, the answer
    is no — otherwise a dependency that quietly went missing hands the shared layer to
    agents that were refused it."""
    svc = _service(None, ["a", "b"], tmp_path)

    messages = _run(svc, commit_file, caplog)

    assert sum("no firewall to ask" in m for m in messages) == 2


def test_an_all_digit_content_hash_is_not_a_mismatch():
    """A 16-hex-digit hash with no letters is valid YAML for an integer, and the parser
    obliges. Comparing the computed string against that int reported two of 265 commits
    as corrupted while their content was untouched — a false alarm about integrity is
    not harmless: it is the alarm nobody will believe next time.
    """
    computed = "4541343283619917"
    from_frontmatter = 4541343283619917  # what yaml hands back

    assert computed != from_frontmatter
    assert computed == str(from_frontmatter)
