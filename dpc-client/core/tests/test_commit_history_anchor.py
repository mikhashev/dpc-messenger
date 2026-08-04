"""A proposal names the history it was read from, and voting checks it.

Voting runs for minutes while the conversation keeps moving. Without an anchor
each voter judges whatever its own history happens to say, and a divergence is
indistinguishable from agreement — which is what a consensus mechanism exists
to rule out.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.knowledge_service import KnowledgeService

CONV = "group-anchor"


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: False))


def _monitor(texts=("one", "two", "three")):
    m = ConversationMonitor(
        conversation_id=CONV,
        participants=[{"node_id": "n1", "name": "User", "context": "local"}],
        llm_manager=None,
    )
    for text in texts:
        m.add_message(role="user", content=text, sender_node_id="n1", sender_name="Mike")
    return m


def _service(monitor, proposal):
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.conversation_monitors = {CONV: monitor}
    svc.consensus_manager = SimpleNamespace(
        sessions={"p1": SimpleNamespace(proposal=proposal, status="voting")}
    )
    return svc


def _proposal(index, chain_hash):
    return SimpleNamespace(
        proposal_id="p1",
        conversation_id=CONV,
        based_on_msg_index=index,
        based_on_chain_hash=chain_hash,
    )


def test_anchor_points_at_the_last_message():
    m = _monitor()
    index, chain_hash = m.history_anchor()
    assert index == m.message_history[-1]["msg_index"]
    assert chain_hash == m.message_history[-1]["chain_hash"]


def test_matching_history_lets_the_vote_through():
    m = _monitor()
    svc = _service(m, _proposal(*m.history_anchor()))
    assert svc._history_drift("p1") is None


def test_a_rewritten_message_is_refused():
    """The defect this exists for: voting on text you are not looking at."""
    m = _monitor()
    index, chain_hash = m.history_anchor()
    svc = _service(m, _proposal(index, chain_hash))

    m.message_history[-1]["chain_hash"] = "0" * 64  # history diverged under us

    drift = svc._history_drift("p1")
    assert drift is not None
    assert drift["reason"] == "history_drift"
    assert str(index) in drift["message"]


def test_new_messages_after_the_anchor_do_not_block_the_vote():
    """Appending is not divergence — that is the whole point of not locking."""
    m = _monitor()
    svc = _service(m, _proposal(*m.history_anchor()))

    m.add_message(role="user", content="written during the vote",
                  sender_node_id="n1", sender_name="Mike")

    assert svc._history_drift("p1") is None


def test_a_proposal_without_an_anchor_is_not_treated_as_a_mismatch():
    """Older peers send none; refusing those would break voting, not protect it."""
    m = _monitor()
    svc = _service(m, _proposal(None, None))
    assert svc._history_drift("p1") is None


# ── wiring ──────────────────────────────────────────────────────────────
# The checks above prove the two helpers behave. They passed unchanged with
# both helpers disconnected, which is the failure mode this whole change is
# about: a check that exists, travels, and is never asked. These assert the
# call actually happens.


@pytest.mark.asyncio
async def test_vote_is_refused_when_the_history_drifted(monkeypatch):
    m = _monitor()
    index, chain_hash = m.history_anchor()
    svc = _service(m, _proposal(index, chain_hash))
    svc.llm_manager = SimpleNamespace(providers={})

    cast = SimpleNamespace(called=False)

    async def _never(**_kw):
        cast.called = True
        return True

    svc.consensus_manager.cast_vote = _never
    m.message_history[-1]["chain_hash"] = "0" * 64

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")

    assert result["status"] == "error"
    assert result["reason"] == "history_drift"
    assert not cast.called, "the vote reached consensus despite a divergent history"


def test_the_proposal_is_stamped_with_the_anchor():
    """The anchor has to be read from the monitor, not left as a default."""
    import inspect
    from dpc_client_core.conversation_monitor import ConversationMonitor as CM

    src = inspect.getsource(CM._generate_commit_proposal)
    assert "self.history_anchor()" in src, "the anchor is never read"
    assert src.count("based_on_msg_index=_anchor_index") >= 1, "proposals go out unstamped"
