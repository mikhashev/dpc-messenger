"""Tier 1 can be answered automatically in an eval; Tier 2 cannot be answered at all.

ADR-030's invariant is that the hard level is not overridable by config and not
overridable by the agent — the CVE-2025-53773 lesson, where an agent rewrote
its own approval settings. The eval harness answers Tier 1 prompts so a
headless benchmark measures the loop instead of the approval gate; this pins
the boundary that makes that safe.
"""

import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval"))

from _harness.auto_approve import Tier1AutoApprover  # noqa: E402
from dpc_client_core.dpc_agent.tools import shell  # noqa: E402


def test_a_tier2_command_never_reaches_the_approval_queue():
    """The queue the approver watches is only ever fed by Tier 1.

    `run_shell` returns the block for Tier 2 before `_request_approval` is
    called, so there is nothing for an approver — ours or anyone's — to say yes
    to. Read from the dispatch itself rather than asserted from memory.
    """
    source = (Path(shell.__file__)).read_text(encoding="utf-8")
    dispatch = source[source.index("def run_shell("):]
    tier2_at = dispatch.index('if tier == "tier2"')
    tier1_at = dispatch.index('elif tier == "tier1"')
    approval_at = dispatch.index("_request_approval(")

    assert tier2_at < tier1_at < approval_at, "the tier2 arm must return first"
    tier2_arm = dispatch[tier2_at:tier1_at]
    assert "return" in tier2_arm and "_request_approval" not in tier2_arm


def test_the_approver_answers_a_waiting_tier1_request():
    entry = {"command": "python -c \"print(1)\"", "event": threading.Event()}
    shell._pending_approvals["eval-test-1"] = entry
    try:
        with Tier1AutoApprover(poll_seconds=0.01) as approver:
            assert entry["event"].wait(timeout=5), "the blocked thread must be released"
            assert entry["decision"] == "approved"
            time.sleep(0.05)
            assert approver.approved == ['python -c "print(1)"']
            assert approver.summary()["tier1_auto_approved"] == 1
    finally:
        shell._pending_approvals.pop("eval-test-1", None)


def test_an_already_decided_request_is_left_alone():
    """A human decision in flight must not be overwritten by the watcher."""
    entry = {"command": "rm -rf /", "decision": "rejected", "event": threading.Event()}
    shell._pending_approvals["eval-test-2"] = entry
    try:
        with Tier1AutoApprover(poll_seconds=0.01) as approver:
            time.sleep(0.1)
            assert entry["decision"] == "rejected"
            assert approver.approved == []
    finally:
        shell._pending_approvals.pop("eval-test-2", None)


# --- the gate has to let the approver be asked in the first place ---


def _headless_ctx(tmp_path):
    """What run_gaia_eval builds: an agent with no service and no Telegram."""
    return types.SimpleNamespace(
        agent_root=tmp_path, dpc_service=None, reply_telegram_chat_id="",
        _event_loop=None, _agent=None,
    )


@pytest.fixture(autouse=True)
def _clean_watchers():
    shell._approval_watchers.clear()
    yield
    shell._approval_watchers.clear()


def test_a_headless_eval_run_is_asked_rather_than_refused(tmp_path, monkeypatch):
    """The gate refuses when nobody could answer, and the approver is somebody.

    Without this the refusal lands before the queue entry exists, the watcher
    has nothing to drain, and the benchmark goes back to measuring the gate —
    the state this class was written to escape, reached from the other side.
    """
    monkeypatch.setattr(shell, "_execute_shell_command", lambda *a, **k: "RAN")

    with Tier1AutoApprover(poll_seconds=0.01) as approver:
        result = shell._request_approval(
            _headless_ctx(tmp_path), "echo probe", "tier1", "", 10
        )
        assert result == "RAN"
        assert approver.summary()["tier1_auto_approved"] == 1


def test_the_same_run_without_the_approver_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "_execute_shell_command", lambda *a, **k: "RAN")

    result = shell._request_approval(
        _headless_ctx(tmp_path), "echo probe", "tier1", "", 10
    )
    assert "nobody could be asked" in result
    assert shell._pending_approvals == {}


def test_the_approver_declares_itself_and_takes_it_back():
    """A watcher outliving its approver would leave the gate open for the run."""
    approver = Tier1AutoApprover(poll_seconds=0.01)
    assert shell._approval_watchers == set()
    approver.start()
    assert shell._approval_watchers == {"eval:tier1-auto-approver"}
    approver.stop()
    assert shell._approval_watchers == set()


def test_the_approver_is_off_unless_the_run_asks_for_it():
    """Nothing in production imports it, and no default turns it on."""
    harness = (Path(__file__).resolve().parents[3] / "eval" / "gaia" / "run_gaia_eval.py")
    text = harness.read_text(encoding="utf-8")
    assert '"--auto-approve", action="store_true"' in text, "opt-in flag, no default"
    assert "if args.auto_approve:" in text, "constructed only when asked"

    core = Path(__file__).resolve().parents[1] / "dpc_client_core"
    hits = [p for p in core.rglob("*.py") if "auto_approve" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], f"production must not import the eval approver: {hits}"
