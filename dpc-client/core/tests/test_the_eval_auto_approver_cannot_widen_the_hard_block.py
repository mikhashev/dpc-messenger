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


def test_the_approver_is_off_unless_the_run_asks_for_it():
    """Nothing in production imports it, and no default turns it on."""
    harness = (Path(__file__).resolve().parents[3] / "eval" / "gaia" / "run_gaia_eval.py")
    text = harness.read_text(encoding="utf-8")
    assert '"--auto-approve", action="store_true"' in text, "opt-in flag, no default"
    assert "if args.auto_approve:" in text, "constructed only when asked"

    core = Path(__file__).resolve().parents[1] / "dpc_client_core"
    hits = [p for p in core.rglob("*.py") if "auto_approve" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], f"production must not import the eval approver: {hits}"
