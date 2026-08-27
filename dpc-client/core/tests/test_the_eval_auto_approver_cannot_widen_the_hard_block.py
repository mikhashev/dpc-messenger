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
    entry = {"command": "python -c \"print(1)\"", "event": threading.Event(),
             "reason": "Requires approval: python -c"}
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
            _headless_ctx(tmp_path), "echo probe",
            "Requires approval: echo", "", 10
        )
        assert result == "RAN"
        assert approver.summary()["tier1_auto_approved"] == 1


def test_the_same_run_without_the_approver_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "_execute_shell_command", lambda *a, **k: "RAN")

    result = shell._request_approval(
        _headless_ctx(tmp_path), "echo probe", "Requires approval: echo", "", 10
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

# --- what it is entitled to answer, and what it is not ---


def _pending(command, reason):
    return {"command": command, "reason": reason, "event": threading.Event()}


def test_leaving_the_sandbox_is_not_a_question_it_may_answer():
    """Going outside the sandbox is only Tier 1, so a blanket yes demotes the
    boundary to a prompt and then answers it — that is how the 2026-08-25 run
    reached the operator's own interpreter, pip, the network and Tesseract."""
    entry = _pending("type C:\secrets\answers.parquet",
                     "Command accesses path outside sandbox: C:\secrets")
    approve, why = Tier1AutoApprover().verdict(entry)
    assert approve is False
    assert "outside sandbox" in why


def test_a_command_a_person_would_have_glanced_at_is_approved():
    approve, _ = Tier1AutoApprover().verdict(
        _pending('python -c "print(1)"', "Requires approval: python -c"))
    assert approve is True


def test_a_destructive_tier1_command_is_refused_even_with_the_right_reason():
    """«Tier 2 stays blocked» undersells what an unattended yes waves through."""
    for command in ("git reset --hard HEAD~5", "git clean -fd", "net user bob /add",
                    "reg delete HKLM\Software\X /f"):
        approve, why = Tier1AutoApprover().verdict(
            _pending(command, "Requires approval: " + command.split()[0]))
        assert approve is False, command
        assert "destructive" in why


def test_an_entry_with_no_reason_is_refused_rather_than_guessed():
    approve, why = Tier1AutoApprover().verdict({"command": "python -c 'x'"})
    assert approve is False
    assert "no reason" in why


def test_a_refusal_releases_the_waiting_thread_and_says_why():
    entry = _pending("type C:\etc\gold.parquet",
                     "Command accesses path outside sandbox: C:\etc")
    shell._pending_approvals["eval-test-3"] = entry
    try:
        with Tier1AutoApprover(poll_seconds=0.01) as approver:
            assert entry["event"].wait(timeout=5), "the executor must not hang on a refusal"
            assert entry["decision"] == "rejected"
            assert "Refused by the eval approver" in entry["result"]
            time.sleep(0.05)
            assert approver.summary()["tier1_auto_refused"] == 1
            assert approver.summary()["tier1_auto_approved"] == 0
    finally:
        shell._pending_approvals.pop("eval-test-3", None)


def test_the_report_carries_the_refusals_and_not_only_the_yeses():
    approver = Tier1AutoApprover()
    approver.approved.append("python -c 'ok'")
    approver.refused.append(("type gold.parquet", "Command accesses path outside sandbox"))
    s = approver.summary()
    assert s["tier1_auto_approved"] == 1 and s["tier1_auto_refused"] == 1
    assert s["tier1_refusals"][0]["why"].startswith("Command accesses path")


def test_the_gate_puts_the_reason_where_the_approver_can_read_it(tmp_path, monkeypatch):
    """Read from the live queue while a request is pending, not from the source.

    The whole filter rests on this: an entry with no reason is refused, so if
    the gate stopped storing it the benchmark would refuse everything and the
    tests above would still pass on their hand-built dicts.
    """
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 5)

    class _Api:
        has_clients = True

    class _Svc:
        local_api = _Api()

        async def announce_shell_approval_request(self, **kw):
            pass

        async def announce_shell_approval_closed(self, **kw):
            pass

    ctx = types.SimpleNamespace(agent_root=tmp_path, dpc_service=_Svc(),
                                reply_telegram_chat_id="", _event_loop=None, _agent=None)
    seen = {}

    def _ask():
        shell._request_approval(ctx, "python -c 'x'", "Requires approval: python -c", "", 5)

    asker = threading.Thread(target=_ask, daemon=True)
    asker.start()
    for _ in range(200):
        pending = list(shell._pending_approvals.values())
        if pending:
            seen.update(pending[0])
            for entry in shell._pending_approvals.values():
                entry["decision"] = "rejected"
                entry["event"].set()
            break
        time.sleep(0.01)
    asker.join(timeout=5)
    shell._pending_approvals.clear()

    assert seen.get("reason") == "Requires approval: python -c"
