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
import re
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
    # The gate joins its reasons with "; " (kills first, then this one, then
    # leaving the sandbox), and this reason joins its own pattern list the same
    # way — so a prefix test approved a command that also left the sandbox.
    # A yes now needs the reason to be this prefix followed by nothing but
    # pattern strings from DANGEROUS_PATTERNS, and each of those to be an
    # inline-code wrapper: the one thing a headless task legitimately needs.
    _INLINE_CODE_PROBES = (
        "python -c x", "node -e x", "bash -c x", "cmd /c x", "powershell -Command x",
    )
    # Tier 1 commands that are destructive on their own terms, listed because
    # "Tier 2 stays blocked" is true and undersells what an unattended yes
    # would otherwise wave through.
    _NEVER = (
        "reset --hard", "clean -f", "clean -d", "reg add", "reg delete",
        "net user", "systemctl stop", "shutdown", "diskpart", "format ",
    )
    # The agent must not change the operator's interpreters. The measured runs
    # installed openpyxl, pypdf, pytesseract and curl_cffi into the system
    # Python this way, and read ~/.dpc/providers.json through %USERPROFILE%,
    # $HOME and expanduser — none of which is a literal path the gate checks.
    # Lexical, so a speed bump: a script file walks past it (WRITE-A-SCRIPT...).
    _INSTALLS = re.compile(
        r"\b(?:pip3?(?:\.exe)?\s+install|-m\s+pip\b|ensurepip|uv\s+(?:pip|add|tool)\b"
        r"|conda\s+install|mamba\s+install|npm\s+(?:i|install)\b|winget|choco\s+install)",
        re.I,
    )
    _OPERATOR_HOME = re.compile(
        r"%userprofile%|%appdata%|%localappdata%|%homepath%|\$env:(?:userprofile|appdata"
        r"|localappdata|homepath)|\$home\b|\$\{home\}|expanduser|path\.home|getenv|environ"
        r"|(?<![\w.])~(?=[/\\\s\"']|$)|\.dpc\b|huggingface|gaia-archive|providers\.json",
        re.I,
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
        patterns = self._patterns_in(reason)
        if patterns is None:
            return False, reason
        not_inline = [p for p in patterns if not self._is_inline_code(p)]
        if not_inline:
            return False, f"not an inline-code wrapper: {not_inline[0]}"
        command = entry.get("command") or ""
        for pattern in self._NEVER:
            if pattern in command.lower():
                return False, f"destructive without a person: {pattern!r}"
        install = self._INSTALLS.search(command)
        if install:
            return False, f"installs into an interpreter: {install.group(0)!r}"
        home = self._OPERATOR_HOME.search(command)
        if home:
            return False, f"reaches the operator's home or credentials: {home.group(0)!r}"
        return True, reason

    def _patterns_in(self, reason: str):
        """The DANGEROUS_PATTERNS strings the reason names, or None.

        None when anything else is in it: a kill note, an outside-sandbox
        part, a script part, or text this approver does not recognise.
        Longest pattern first, because a pattern string may itself hold "; ".
        """
        if not reason.startswith(self._APPROVABLE + " "):
            return None
        from dpc_client_core.dpc_agent.tools import shell
        known = sorted({p.pattern for p in shell.DANGEROUS_PATTERNS}, key=len, reverse=True)
        rest, found = reason[len(self._APPROVABLE) + 1:], []
        while rest:
            match = next((k for k in known
                          if rest.startswith(k) and (rest == k or rest[len(k):].startswith("; "))),
                         None)
            if match is None:
                return None
            found.append(match)
            rest = rest[len(match) + 2:]
        return found or None

    def _is_inline_code(self, pattern: str) -> bool:
        compiled = re.compile(pattern, re.I)
        return any(compiled.search(probe) for probe in self._INLINE_CODE_PROBES)

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
