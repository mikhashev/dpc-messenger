"""A proposal names the history it was read from, and voting checks it.

Voting runs for minutes while the conversation keeps moving. Without an anchor
each voter judges whatever its own history happens to say, and a divergence is
indistinguishable from agreement — which is what a consensus mechanism exists
to rule out.

**The anchor changed on 2026-08-08 and these expectations changed with it.** The
first form was `(msg_index, chain_hash)` of the last message, and it could not
work across nodes: `chain_hash` covers `role`, a per-reader rendering, so three
honest copies of the same five messages hashed three different ways. Measured on
the stand — `855c…` / `1a05…` / `903d…` for message #3 — and the consequence was
not a weak check but an inverted one: every vote except the proposer's own was
refused, on every node, permanently.

The anchor is now the `content_hash` of every message the extraction read: the
author's own signed value, identical wherever the message is held, and a set
rather than a position so that a hole in the middle of the window is caught too
(Fable 5, B9).

These tests keep their shape on purpose — the same questions, asked of the
mechanism that can actually answer them. Unit-level window and drift semantics
live in `test_the_anchor_names_the_window_it_read.py`; this file uses a real
monitor so the hashes are the ones production writes.
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


def _window(monitor):
    """What the proposer would stamp: the whole conversation it just read."""
    return [m["content_hash"] for m in monitor.message_history if m.get("content_hash")]


def _service(monitor, proposal):
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.conversation_monitors = {CONV: monitor}
    svc.consensus_manager = SimpleNamespace(
        sessions={"p1": SimpleNamespace(proposal=proposal, status="voting")}
    )
    return svc


def _proposal(window):
    return SimpleNamespace(
        proposal_id="p1",
        conversation_id=CONV,
        based_on_content_hashes=window,
    )


def test_the_window_is_made_of_values_every_node_can_reproduce():
    """The property the old anchor lacked: nothing per-reader inside it."""
    m = _monitor()
    window = _window(m)

    assert len(window) == 3
    assert all(len(h) == 64 for h in window)
    # `role` is what made the old chain local; it is not in this value.
    m.message_history[0]["role"] = "peer"
    assert _window(m) == window


def test_matching_history_lets_the_vote_through():
    m = _monitor()
    svc = _service(m, _proposal(_window(m)))
    assert svc._history_drift("p1") is None


def test_a_rewritten_message_is_refused():
    """The defect this exists for: voting on text you are not looking at."""
    m = _monitor()
    svc = _service(m, _proposal(_window(m)))

    m.message_history[-1]["content_hash"] = "0" * 64  # history diverged under us

    drift = svc._history_drift("p1")
    assert drift is not None
    assert drift["reason"] == "history_drift"
    assert drift["missing_messages"] == 1


def test_new_messages_after_the_anchor_do_not_block_the_vote():
    """Appending is not divergence — that is the whole point of not locking."""
    m = _monitor()
    svc = _service(m, _proposal(_window(m)))

    m.add_message(role="user", content="written during the vote",
                  sender_node_id="n1", sender_name="Mike")

    assert svc._history_drift("p1") is None


def test_a_proposal_without_an_anchor_is_not_treated_as_a_mismatch():
    """Older peers send none; refusing those would break voting, not protect it."""
    m = _monitor()
    svc = _service(m, _proposal(None))
    assert svc._history_drift("p1") is None


# ── wiring ──────────────────────────────────────────────────────────────
# The checks above prove the two helpers behave. They passed unchanged with
# both helpers disconnected, which is the failure mode this whole change is
# about: a check that exists, travels, and is never asked. These assert the
# call actually happens.


@pytest.mark.asyncio
async def test_vote_is_refused_when_the_history_drifted(monkeypatch):
    m = _monitor()
    svc = _service(m, _proposal(_window(m)))
    svc.llm_manager = SimpleNamespace(providers={})

    cast = SimpleNamespace(called=False)

    async def _never(**_kw):
        cast.called = True
        return True

    svc.consensus_manager.cast_vote = _never
    m.message_history[-1]["content_hash"] = "0" * 64

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")

    assert result["status"] == "error"
    assert result["reason"] == "history_drift"
    assert not cast.called, "the vote reached consensus despite a divergent history"


def test_the_proposal_is_stamped_with_the_window():
    """The anchor has to be read from the monitor, not left as a default."""
    import inspect
    from dpc_client_core.conversation_monitor import ConversationMonitor as CM

    src = inspect.getsource(CM._generate_commit_proposal)
    assert "self.window_content_hashes(" in src, "the window is never read"
    assert src.count("based_on_content_hashes=_anchor_hashes") >= 1, (
        "proposals go out unstamped"
    )
    assert "based_on_chain_hash" not in src, (
        "the local-only anchor is still being stamped"
    )
