---
adr: 030
title: "Add safety guardrails to agent run_shell tool"
status: accepted
date: 2026-06-01
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: []
related: [ADR-022, ADR-026]
session: S177
---

## Context and Problem Statement

The agent `run_shell` tool (`dpc_agent/tools/shell.py`) executes arbitrary shell commands via `subprocess.run(command, shell=True)` with zero content validation. The only existing protections are firewall scope restriction (per-agent toggle with optional group chat checkbox, S176 commit `347550b`), timeout (5-300s), and output truncation (50K chars).

### Chat Context Coverage

These guardrails apply uniformly in **both 1:1 and group chat contexts**. The firewall controls WHO can use `run_shell` (per-agent toggle + group chat checkbox in AgentPanel UI). ADR-030 controls WHAT commands are allowed — regardless of chat context. An agent with `run_shell` enabled in a group chat is subject to the same Tier 0/1/2 classification as in 1:1.

This leaves the system vulnerable to catastrophic damage from agent-initiated commands — whether from hallucination, prompt injection, or design drift. Real-world incidents confirm this risk: CVE-2025-53773 (GitHub Copilot agent rewrote its own approval settings), Replit Jul 2025 (agent deleted production DB without any injection).

## Decision Drivers

- **Safety:** Prevent irrecoverable damage (filesystem destruction, shutdown, privilege escalation)
- **Cross-platform:** Must cover Windows (cmd.exe) and Linux (/bin/sh), plus WSL escape vectors
- **Defense-in-depth:** Blocklist catches patterns, cwd enforcement catches scope — neither alone is sufficient
- **Hardcoded over configurable:** Tier 2 blocklist must not be overridable by agent or config (CVE-2025-53773 lesson)
- **Flexibility for future:** Architecture must support Tier 1 (user approval) without rewriting Tier 2
- **No false negatives on safety-critical patterns:** Regex must handle pipe chains, subshell injection, Unicode obfuscation

## Decision

Implement a 3-tier command classification system in `run_shell()`, adapted from Hermes Agent's `approval.py` (1435 LOC, most mature open-source implementation found).

### Tier Model

**Tier 0 — Auto-approve (default):** Commands not matching Tier 1 or Tier 2 execute immediately. No allowlist — everything is permitted unless explicitly blocked.

**Tier 1 — Require approval (v2):** Dangerous but legitimate commands require user confirmation via UI chat buttons. Configurable — user can move commands between Tier 0 and Tier 1.

**Tier 2 — Hard block (v1):** Catastrophic commands blocked unconditionally. Hardcoded in Python, not overridable by config or agent.

### Validation Pipeline

```
_validate_command(command: str, cwd: str) -> tuple[str, str] | None
  1. Unicode normalize (NFKC) — fullwidth character defense
  2. Strip ANSI escape codes
  3. Split by pipe |, &&, ||, ; — check EACH segment
  4. Match each segment against HARDLINE_PATTERNS (Tier 2)
  5. Match each segment against DANGEROUS_PATTERNS (Tier 1, v2)
  6. Validate cwd against sandbox_paths (agent_root + sandbox_extensions)
  7. Return (tier, reason) or None if allowed
```

### Cross-Platform Blocklist

**HARDLINE_PATTERNS (Tier 2 — unconditional block):**

| Category | Linux | Windows | macOS |
|---|---|---|---|
| Mass delete | `rm -rf /`, `rm -rf ~`, `rm` on system dirs | `rd /s /q`, `rmdir /s /q`, `del /s /q C:\` | `rm -rf /` (shared with Linux) |
| Disk format/erase | `mkfs` | `format [A-Z]:` | `diskutil eraseDisk/partitionDisk/secureErase` |
| Raw device write | `dd of=/dev/sd*`, `> /dev/sd*` | — | `dd of=/dev/disk*` (shared with Linux) |
| Fork bomb | `:(){ :|:& };:` | — | `:(){ :|:& };:` (shared with Linux) |
| Shutdown/reboot | `shutdown`, `reboot`, `halt`, `poweroff`, `init 0/6` | `shutdown /s`, `shutdown /r` | `shutdown` (shared with Linux) |
| Kill all | `kill -9 -1` | — | `kill -9 -1` (shared with Linux) |

**DANGEROUS_PATTERNS (Tier 1 in v2, blocked in v1):**

| Category | Patterns |
|---|---|
| Privilege escalation | `sudo`, `su`, `runas`, `gsudo`, `pkexec` |
| Subshell invocation | `bash -c`, `sh -c`, `cmd /c`, `python -c`, `node -e`, heredoc |
| Encoded commands | `powershell -enc`, `-encodedcommand` |
| Download + execute | `curl \| sh`, `wget \| bash`, process substitution |
| Registry | `reg delete`, `reg add` |
| User management | `net user`, `net localgroup`, `userdel` |
| Git destructive | `git push --force`, `git reset --hard`, `git clean -f`, `git branch -D` |
| WSL escape | `wsl` prefix (full block) |
| Service control | `systemctl stop/disable`, `sc delete`, `net stop` |
| macOS security | `csrutil disable` (SIP), `launchctl unload/remove` |
| Container runtime | `docker` (arbitrary code execution surface) |

### cwd Enforcement

`cwd` parameter restricted to agent sandbox paths as defined by the firewall: agent_root + `sandbox_extensions.indexed_paths` from `privacy_rules.json`. Same paths that govern read/write file access. Commands targeting paths outside these firewall-defined boundaries are denied.

### Logging

- Tier 0 (allowed): `DEBUG` — audit trail without noise
- Tier 2 (blocked): `WARNING` — full command text for incident review

### Rationale

- **Hermes Agent** is the closest reference implementation (Python agent with terminal tool, same attack surface). Their 2-tier (hardline + dangerous) model with NFKC normalization is battle-tested.
- **Blocklist over allowlist** preserves tool flexibility — agents need diverse commands for dev tasks.
- **Hardcoded Tier 2** prevents the CVE-2025-53773 class of attacks where the agent modifies its own safety config.
- **Unified cross-platform blocklist** (both OS patterns always checked) prevents WSL escape vectors and simplifies maintenance.
- **This is defense-in-depth, not a security boundary** — `shell=True` means determined bypass is always possible. The guard catches accidental/hallucinated destructive commands, which is the primary threat model.

## Considered Options

- **Option A: Blocklist only (Tier 0 + Tier 2)** — Hardcoded dangerous command patterns. Simple, immediate protection.
- **Option B: Allowlist only** — Only whitelisted commands execute. Maximum safety but too restrictive for general-purpose agent.
- **Option C: 3-tier with approval UI (full)** — Tier 0 + Tier 1 + Tier 2 with user confirmation flow. Most flexible but requires UI work.
- **Option D: External tool (shellfirm MCP)** — Delegate to shellfirm via MCP server. Mature patterns but external dependency, overkill for DPC scope.

### Pros and Cons of the Options

#### Option A: Blocklist only
- Good: Immediate protection, minimal LOC (~100-120)
- Good: No UI changes needed
- Bad: No user approval path for edge cases
- Neutral: Can be extended to Option C incrementally

#### Option B: Allowlist only
- Good: Maximum safety
- Bad: Breaks agent utility — can't run novel commands
- Bad: Maintenance burden — every new legitimate command needs allowlisting

#### Option C: 3-tier with approval UI
- Good: Matches Claude Code model, maximum flexibility
- Good: User controls what requires approval
- Bad: Requires async approval flow in agent pipeline + UI widget (~200 LOC total)
- Neutral: Can be built incrementally on top of Option A

#### Option D: External tool (shellfirm)
- Good: 100+ battle-tested patterns
- Bad: External dependency, Rust binary, overkill for DPC
- Bad: No integration with DPC firewall/sandbox system

## Consequences

- **Positive:** Agents cannot execute catastrophic commands (filesystem destruction, shutdown, privilege escalation)
- **Positive:** Cross-platform coverage (Windows + Linux + WSL)
- **Positive:** Defense-in-depth via cwd enforcement on top of pattern matching
- **Negative:** Some legitimate commands blocked (e.g. `python -c` one-liners — use `python script.py` instead)
- **Negative:** Regex-based detection can have false negatives for novel obfuscation
- **Neutral:** No approval UI in v1 — commands are either allowed or blocked, no middle ground

## Confirmation

- [ ] `_validate_command()` blocks all HARDLINE_PATTERNS on both Windows and Linux
- [ ] NFKC normalization prevents fullwidth character bypass
- [ ] Pipe chain splitting catches dangerous commands in any segment position
- [ ] cwd enforcement denies execution outside sandbox paths
- [ ] Denied commands logged at WARNING level with full command text
- [ ] Fork bomb detection uses dedicated matcher (bash function syntax is not reliably regex-matchable)
- [ ] Tier 2 patterns are hardcoded in Python source, not loadable from config (verified by test)
- [ ] Tests cover each pattern category with positive and negative cases
- [ ] WSL escape vectors blocked on Windows

## Scope

- `dpc_agent/tools/shell.py` — add `_validate_command()`, HARDLINE_PATTERNS, DANGEROUS_PATTERNS, NFKC normalization, cwd enforcement
- `dpc_agent/tools/shell.py` — integrate validation at top of `run_shell()` before `subprocess.run()`

### v2 Scope (future, not this ADR)
- `dpc_agent/tools/shell.py` — add Tier 1 `pending_approval` return path
- `dpc_client_core/local_api.py` — add `execute_approved` WebSocket command
- `dpc-client/ui/src/lib/panels/ChatPanel.svelte` — approval buttons in chat (1:1 + group)
- `~/.dpc/config.ini` or `privacy_rules.json` — configurable Tier 1 patterns
- Group chat approval routing — Tier 1 approval routes to the **agent owner** (who created the agent on their node), not to the @mention initiator. Owner is always a participant in the group chat and sees full context. Approval UI appears in the same chat.
- Multi-node: when an agent from another node is @mentioned in a group chat, the approval request goes to that agent's owner (who is also in the group). No cross-node approval protocol needed — the owner sees the chat context directly.
- Add-to-whitelist — "Approve + Add to whitelist" button in approval dialog (per-agent persistent whitelist)
- Note: Tier 1 approval assumes agent owner is online. Agent availability management is a separate concern (Group Settings feature, outside ADR-030 scope).
- Whitelist storage: `privacy_rules.json` → `agent_profiles.<id>.tools.run_shell.tier1_whitelist: [...]` (per-agent, empty = all Tier 1 blocked = v1 behavior)

**AgentPermissions Panel — Shell Access section (v2 UI wireframe):**

```
Shell Access
  Execute shell commands  [toggle]

  [ ] Allow in group chats
      By default run_shell is restricted to 1:1 chats only

  ── Safety Guardrails (ADR-030) ──

  Tier 0 — Auto-approved commands:
    git status, git log, ls, cat, echo, pwd...

  Tier 1 — Requires approval (shown in chat):
    [+ Add command to whitelist]
    • python script.py     [x remove]
    • pip install          [x remove]
    • curl                 [x remove]

  Tier 2 — Always blocked:
    rm -rf, shutdown, python -c, docker...
```

Tier 0 and Tier 2 lists are read-only (hardcoded). Tier 1 whitelist is editable — user can add/remove commands proactively or via "Approve + Add to whitelist" button in the chat approval dialog.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| ADR-030 draft | Done | `0cc61e3` |
| Tier 2 blocklist + NFKC + pipe splitting | Done | `d17ee65` |
| macOS patterns (diskutil, csrutil, launchctl) | Done | `8553f36` |
| cwd enforcement (sandbox path validation) | Done | `1fee6a5` |
| ADR documentation (chat coverage, macOS tables, Q1) | Done | `2273c07`, `f578d42`, `82f9333` |
| Tests (98 unit tests + curl\|sh cross-segment fix) | Done | `b5c1bf9`, `1fa982d` |
| v2 backend: Tier 1 approval (Variant B) + whitelist | Done | `836c5eb`, `eab6e8c`, `dbb34ae`, `3244b69` |
| v2 frontend: ShellApprovalDialog + result cards | Done | `4b79277`, `76b4dd7` |
| v2 frontend: AgentPermissions whitelist CRUD | Done | `bb5ee73` |
| v2 broadcast fix (threadsafe from executor thread) | Done | `f2d6f28`, `c26143c`, `a72a08e` |

### v3 Scope (next session)

- **AFTER_TOOL_CALL hook lifecycle event** — canonical path for tool side effects (broadcast approval requests, emit notifications) instead of `run_coroutine_threadsafe` workaround. Current v2 stores `ctx._event_loop` in loop.py before executor dispatch and uses `asyncio.run_coroutine_threadsafe()` from the thread. v3 replaces this with a proper hook that fires after tool execution in the async context of the main event loop. Benefits: cleaner architecture, reusable for other tools that need async side effects (browse_page popup could migrate too).
- **Smoke test** ADR-030 v2 approval dialog in UI (broadcast fix committed but not yet verified with restart)
- **Telegram approval path** (Q3) — deferred from v2

## Open Questions

- **Q1:** Should `python -c` be Tier 1 (approval) or Tier 2 (block) in final version? — @Mike (decided S185: Tier 2, block. Agents use `python script.py` via Tier 1 instead.)
- **Q2:** Approval timeout default — 60s sufficient? — @Mike (decided: yes)
- **Q3:** Telegram approval path — deferred to v3? — @Mike (decided: yes, deferred)

## Authors

- **Mike** — Decision, requirements (cross-platform, flexibility, Claude Code model)
- **Ark** — Research (Hermes Agent, failproof.ai), architecture analysis, cross-platform matrix, 3-tier model design
- **CC** — Research (shellfirm, OWASP, CVE incidents), code analysis (shell.py current state), implementation

## References

- Hermes Agent `tools/approval.py` (local: `Y:\HF_Vault_2\OpenSource\AgneticBots\hermes-agent\tools\approval.py`) — HARDLINE_PATTERNS + DANGEROUS_PATTERNS reference implementation
- Hermes Agent `agent/file_safety.py` — write/read denylist patterns
- [shellfirm](https://github.com/kaplanelad/shellfirm) — 100+ cross-ecosystem patterns
- [OWASP Agentic Skills Top 10](https://owasp.org/www-project-agentic-skills-top-10/) (Dec 2025)
- CVE-2025-53773 — GitHub Copilot agent self-modified approval settings
- ADR-022 — Multi-agent safety governance (related security ADR)
- S176 commit `347550b` — run_shell scope restriction (WHO layer)
