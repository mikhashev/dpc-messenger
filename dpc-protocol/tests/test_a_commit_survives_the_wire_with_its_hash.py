"""A commit that crossed the wire must come back as the same commit.

`KnowledgeCommit.from_dict` listed the entry fields by hand and got both halves
wrong. It passed `alternatives` and `flagged_assumptions` — fields of the
*proposal*, not of `KnowledgeEntry` — so every commit carrying an entry raised
`TypeError` and the flagship feature could not be received at all. And it named
four of the entry's thirteen fields, so the other nine came back as defaults;
two of those nine, `cultural_specific` and `alternative_viewpoints`, are inputs
to the commit hash (`commit_integrity.py:77-78`). Fixing only the crash would
have produced a commit that recomputes to a different hash and reads as
tampered — a quieter defect than the one it replaced.

Cross-platform: nothing here touches a path or the filesystem. The canonical
JSON is built with `sort_keys=True` and `ensure_ascii=True`, so the hash is a
property of the content and not of the machine — the non-ASCII case below is
what asserts that rather than assuming it.
"""

import pytest

from dpc_protocol.commit_integrity import compute_commit_hash
from dpc_protocol.knowledge_commit import KnowledgeCommit
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource


def _commit(content="a fact worth keeping"):
    source = KnowledgeSource(
        type="ai_summary",
        conversation_id="conv-1",
        timestamp="2026-08-26T00:00:00+00:00",
        participants=["dpc-node-a", "dpc-node-b"],
    )
    entry = KnowledgeEntry(
        content=content,
        confidence=0.9,
        source=source,
        tags=["protocol", "hash"],
        cultural_specific=True,
        alternative_viewpoints=["a competing reading"],
    )
    commit = KnowledgeCommit(
        topic="protocol",
        summary="one line",
        description="longer text",
        entries=[entry],
        participants=["dpc-node-a", "dpc-node-b"],
        proposal_id="proposal-42",
    )
    commit.compute_hash()
    return commit


def test_a_commit_with_an_entry_can_be_received_at_all():
    """The crash: every commit that carried an entry raised TypeError."""
    sent = _commit()
    received = KnowledgeCommit.from_dict(sent.to_dict())
    assert received.entries[0].content == sent.entries[0].content


def test_the_hash_survives_the_round_trip():
    """The quieter half: the fields the old code dropped are hash inputs.

    Without them the receiver recomputes a different hash, and an untampered
    commit reads as tampered — which is worse than refusing to parse.
    """
    sent = _commit()
    received = KnowledgeCommit.from_dict(sent.to_dict())

    assert received.entries[0].cultural_specific is True
    assert received.entries[0].alternative_viewpoints == ["a competing reading"]
    assert compute_commit_hash(received) == sent.commit_hash


def test_the_hash_is_a_property_of_the_content_not_of_the_machine():
    """Cross-platform: non-ASCII content hashes identically after a round trip.

    Two nodes on different operating systems and locales must agree, so the
    case that would expose an encoding difference is the one worth asserting.
    """
    sent = _commit(content="кириллица, émoji ✅, and a tab\tinside")
    received = KnowledgeCommit.from_dict(sent.to_dict())
    assert compute_commit_hash(received) == sent.commit_hash


def test_proposal_id_is_not_lost():
    """It is how `get_proposal_result` finds the commit its proposal became."""
    sent = _commit()
    assert KnowledgeCommit.from_dict(sent.to_dict()).proposal_id == "proposal-42"


def test_a_field_we_do_not_know_is_dropped_rather_than_fatal():
    """A newer peer must not be able to crash an older one by adding a field."""
    payload = _commit().to_dict()
    payload["entries"][0]["a_field_from_a_future_version"] = 1

    received = KnowledgeCommit.from_dict(payload)
    assert received.entries[0].content == "a fact worth keeping"


def test_an_entry_without_a_source_does_not_raise():
    """`source=None` raised AttributeError before anything else was reached."""
    payload = _commit().to_dict()
    payload["entries"][0]["source"] = None

    received = KnowledgeCommit.from_dict(payload)
    assert isinstance(received.entries[0].source, KnowledgeSource)


def test_an_absent_source_type_still_reads_as_extraction():
    """Provenance must not change quietly when the defaulting moves.

    The hand-written code defaulted `source.type` to `ai_summary` for a commit
    arriving over the wire. The dataclass default is `manual_edit`, and
    `markdown_manager.py:195` prints that field into the document — so letting
    the dataclass default through would have written "Manual Edit" under a
    peer's extracted commit. Caught in review by Ark and Johnny, not by me.
    """
    payload = _commit().to_dict()
    del payload["entries"][0]["source"]["type"]

    received = KnowledgeCommit.from_dict(payload)
    assert received.entries[0].source.type == "ai_summary"


def test_the_proposal_path_also_tolerates_a_field_from_a_future_peer():
    """The same splat lived two functions away and took peer input too."""
    from dpc_protocol.knowledge_commit import KnowledgeCommitProposal

    proposal = KnowledgeCommitProposal(topic="protocol", entries=[
        KnowledgeEntry(content="x", source=KnowledgeSource(type="ai_summary")),
    ])
    payload = proposal.to_dict()
    payload["entries"][0]["source"]["a_field_from_a_future_version"] = 1

    rebuilt = KnowledgeCommitProposal.from_dict(payload)
    assert rebuilt.entries[0].source.type == "ai_summary"
