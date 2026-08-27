"""Answer the Tier 1 approval prompt in a headless eval, and nowhere else.

ADR-030 gives `run_shell` three levels: Tier 2 is a hard block written in
Python, Tier 1 asks a person and **blocks the executor thread until they
answer**, Tier 0 passes. In an interactive session a human presses the button.
A benchmark has no human, so every Tier 1 command waits sixty seconds and comes
back as `⏳ Command approval timed out`.

Measured on the first full GAIA attempt: **30 Tier 1 blocks** and no completed
attachment task. The agent tried `python -c`, then `type … | find /c /v ""`,
then `powershell -Command Get-ChildItem`, then a second interpreter path — each
gated, each sixty seconds. That run was not measuring the agent loop. It was
measuring the approval gate.

**What this does not do.** Tier 2 never reaches the queue this watches:
`run_shell` returns `⛔ Command blocked by safety guardrails` from
`shell.py:395-397` before `_request_approval` is ever called. So the hardcoded
block stays hardcoded — which is the ADR-030 invariant, and the CVE-2025-53773
lesson behind it: an agent must not be able to widen its own permissions.

**Why this is safe here and would not be in production.** The eval builds a
throwaway agent root under the system temp directory, its own provider file,
and its own `LLMManager`; nothing of the operator's is in scope. It is opt-in
per run (`--auto-approve`), it records every command it approved so the report
can show them, and it lives in `eval/` where no production path imports it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List

log = logging.getLogger(__name__)


class Tier1AutoApprover:
    """Approves Tier 1 requests as they appear. Start it, run the eval, stop it."""

    _WATCHER = "eval:tier1-auto-approver"

    # The one reason it answers. Everything else — the sandbox boundary above
    # all — is a question it is not entitled to answer.
    _APPROVABLE = "Requires approval:"
    # Tier 1 commands that are destructive on their own terms, listed because
    # "Tier 2 stays blocked" is true and undersells what an unattended yes
    # would otherwise wave through.
    _NEVER = (
        "reset --hard", "clean -f", "clean -d", "reg add", "reg delete",
        "net user", "systemctl stop", "shutdown", "diskpart", "format ",
    )

    def __init__(self, poll_seconds: float = 0.2):
        self.poll_seconds = poll_seconds
        self.approved: List[str] = []
        self.refused: List[tuple] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Tier1AutoApprover":
        if self._thread is not None:
            return self
        # Declare this approver as an answering surface. Without it the gate
        # refuses every Tier 1 command before the queue entry exists, so the
        # watcher below would have nothing to drain — the state this class was
        # written to escape, reached from the other direction.
        from dpc_client_core.dpc_agent.tools import shell
        shell.register_approval_watcher(self._WATCHER)
        self._thread = threading.Thread(
            target=self._watch, name="tier1-auto-approver", daemon=True
        )
        self._thread.start()
        log.warning(
            "Tier 1 auto-approval is ON for this eval run. Tier 2 stays blocked; "
            "every approved command is recorded."
        )
        return self

    def stop(self) -> None:
        from dpc_client_core.dpc_agent.tools import shell
        shell.unregister_approval_watcher(self._WATCHER)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "Tier1AutoApprover":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- the watch ---------------------------------------------------------

    def _watch(self) -> None:
        from dpc_client_core.dpc_agent.tools import shell

        while not self._stop.is_set():
            try:
                self._drain(shell)
            except Exception as exc:  # never let the watcher kill the run
                log.warning("auto-approver hiccup: %s: %s", type(exc).__name__, exc)
            self._stop.wait(self.poll_seconds)

    def verdict(self, entry: dict) -> tuple:
        """(approve, why). A yes needs a reason it recognises; anything else is no.

        Tier 1 is not one thing. `Requires approval: …` is the case this class
        was written for — a command a person would have glanced at. **Leaving
        the sandbox is also only Tier 1**, and answering that the same way
        demotes the sandbox boundary to a prompt and then answers the prompt:
        the overnight run of 2026-08-25 reached the operator's real interpreter,
        `pip install` into it, the network, and Tesseract that way.

        And some Tier 1 commands are destructive on their own terms. A yes with
        nobody watching has no business reaching them.
        """
        reason = entry.get("reason")
        if not reason:
            # An approver that cannot see why cannot judge. Refusing is the
            # only honest answer, and it is also what an old queue entry gets.
            return False, "the queue entry carries no reason"
        if not reason.startswith(self._APPROVABLE):
            return False, reason
        command = (entry.get("command") or "").lower()
        for pattern in self._NEVER:
            if pattern in command:
                return False, f"destructive without a person: {pattern!r}"
        return True, reason

    def _drain(self, shell) -> None:
        # A copy: the waiting thread pops entries out from under us.
        for request_id, entry in list(shell._pending_approvals.items()):
            if entry.get("decision"):
                continue
            command = entry.get("command", "")
            approve, why = self.verdict(entry)
            if approve:
                entry["decision"] = "approved"
                self.approved.append(command)
                log.info("auto-approved Tier 1 (%s): %r", request_id, command[:120])
            else:
                entry["decision"] = "rejected"
                entry["result"] = (
                    f"❌ Refused by the eval approver: {why}. "
                    "It answers «a person would have glanced at this» and nothing else."
                )
                self.refused.append((command, why))
                log.warning("auto-REFUSED Tier 1 (%s): %s — %r", request_id, why, command[:120])
            event = entry.get("event")
            if event is not None:
                event.set()

    # -- for the report ----------------------------------------------------

    def summary(self) -> dict:
        return {
            "tier1_auto_approved": len(self.approved),
            # Truncated: a full command can carry an entire inlined script.
            "tier1_commands": [c[:200] for c in self.approved],
            # Three numbers, not one: what it said yes to, what it said no to,
            # and why — a report with only the yes count cannot be audited.
            "tier1_auto_refused": len(self.refused),
            "tier1_refusals": [{"command": c[:200], "why": w} for c, w in self.refused],
        }
