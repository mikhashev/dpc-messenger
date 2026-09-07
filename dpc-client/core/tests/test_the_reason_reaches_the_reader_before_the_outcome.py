"""An abstention's reason must leave before the vote it explains is cast.

Casting can end the vote, and finalising broadcasts `knowledge_commit_result`
from inside that call, so emitting the reason afterwards put the outcome on the
wire 19 ms ahead of the explanation — a reader that clears its banner on the
result had nothing to clear yet, and the stale text then sat over an unrelated
vote in another group.

The same file also pins the L6 reindex out of the vote request: awaited there it
held the answer to a click for twenty-two seconds while the dialog still offered
a live button.
"""

import asyncio
import inspect
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.knowledge_service import KnowledgeService

PROPOSAL = "proposal-02972996"
GROUP = "group-970e5c7006a0"


def _service(vote_result):
    """A KnowledgeService with just the two collaborators these paths touch."""
    trace = []

    async def _emit(event, payload):
        trace.append(("emit", event, payload.get("reason"), payload.get("abstention_failed")))

    async def _vote(proposal_id, vote, comment=None, _allow_defer=True):
        # What casting really does when it completes the tally.
        trace.append(("cast", vote, None, None))
        trace.append(("emit", "knowledge_commit_result", None, None))
        return vote_result

    service = KnowledgeService.__new__(KnowledgeService)
    service._emit_vote_event = _emit
    service.vote_knowledge_commit = _vote
    service.trace = trace
    return service


@pytest.mark.asyncio
async def test_the_reason_is_emitted_before_the_vote_is_cast():
    service = _service({"status": "success"})

    await KnowledgeService._abstain_with_reason(
        service, PROPOSAL, GROUP, "unverifiable_record", "read from text this node cannot confirm"
    )

    kinds = [(t[0], t[1]) for t in service.trace]
    assert kinds[0] == ("emit", "knowledge_vote_resolved"), service.trace
    assert kinds[1] == ("cast", "abstain"), service.trace
    assert kinds[2] == ("emit", "knowledge_commit_result"), service.trace


@pytest.mark.asyncio
async def test_a_successful_abstention_says_it_once():
    service = _service({"status": "success"})

    await KnowledgeService._abstain_with_reason(
        service, PROPOSAL, GROUP, "unverifiable_record", "cannot confirm"
    )

    resolved = [t for t in service.trace if t[1] == "knowledge_vote_resolved"]
    assert len(resolved) == 1
    assert resolved[0][2] == "unverifiable_record"
    assert resolved[0][3] is None


@pytest.mark.asyncio
async def test_an_abstention_that_did_not_land_is_corrected_afterwards():
    """The reason still stands; the correction says the vote itself failed."""
    service = _service({"status": "error", "message": "Voting already closed"})

    await KnowledgeService._abstain_with_reason(
        service, PROPOSAL, GROUP, "unverifiable_record", "cannot confirm"
    )

    resolved = [t for t in service.trace if t[1] == "knowledge_vote_resolved"]
    assert len(resolved) == 2
    assert resolved[0][3] is None
    assert resolved[1][3] == "Voting already closed"
    assert resolved[1][2] == "unverifiable_record"


@pytest.mark.asyncio
async def test_the_reindex_does_not_hold_the_answer_to_a_vote():
    """The approval handler returns while the index is still being rebuilt."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = []

    service = KnowledgeService.__new__(KnowledgeService)
    service._reindex_tasks = set()

    async def _slow_reindex(markdown_file):
        started.set()
        await release.wait()
        finished.append(markdown_file)

    service._reindex_commit_into_agents = _slow_reindex

    KnowledgeService._start_reindex(service, "commit.md", "commit-6eae9e3b")

    await asyncio.wait_for(started.wait(), timeout=1)
    assert finished == []          # the caller is already back
    assert len(service._reindex_tasks) == 1

    release.set()
    await asyncio.gather(*service._reindex_tasks)
    assert finished == ["commit.md"]


@pytest.mark.asyncio
async def test_a_failing_reindex_is_logged_and_not_raised_at_the_voter(caplog):
    service = KnowledgeService.__new__(KnowledgeService)
    service._reindex_tasks = set()

    async def _explode(markdown_file):
        raise RuntimeError("index writer is busy")

    service._reindex_commit_into_agents = _explode

    with caplog.at_level(logging.WARNING):
        KnowledgeService._start_reindex(service, "commit.md", "commit-6eae9e3b")
        await asyncio.gather(*service._reindex_tasks)

    assert "index writer is busy" in caplog.text


def test_without_a_running_loop_nothing_is_scheduled():
    """A synchronous caller must not crash on create_task."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._reindex_tasks = set()
    service._reindex_commit_into_agents = None

    KnowledgeService._start_reindex(service, "commit.md", "commit-6eae9e3b")

    assert service._reindex_tasks == set()


def test_the_approval_handler_hands_the_reindex_off_rather_than_waiting():
    """The call site, not only the helper: an await here is the whole defect."""
    source = inspect.getsource(KnowledgeService._on_commit_approved)

    assert "_start_reindex(" in source
    assert "await self._reindex_commit_into_agents" not in source
