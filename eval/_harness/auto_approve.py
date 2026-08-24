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

    def __init__(self, poll_seconds: float = 0.2):
        self.poll_seconds = poll_seconds
        self.approved: List[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Tier1AutoApprover":
        if self._thread is not None:
            return self
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

    def _drain(self, shell) -> None:
        # A copy: the waiting thread pops entries out from under us.
        for request_id, entry in list(shell._pending_approvals.items()):
            if entry.get("decision"):
                continue
            entry["decision"] = "approved"
            command = entry.get("command", "")
            self.approved.append(command)
            log.info("auto-approved Tier 1 (%s): %r", request_id, command[:120])
            event = entry.get("event")
            if event is not None:
                event.set()

    # -- for the report ----------------------------------------------------

    def summary(self) -> dict:
        return {
            "tier1_auto_approved": len(self.approved),
            # Truncated: a full command can carry an entire inlined script.
            "tier1_commands": [c[:200] for c in self.approved],
        }
