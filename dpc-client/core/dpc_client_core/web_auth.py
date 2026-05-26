"""Per-agent encrypted web credential vault (ADR-028 T3).

Stores per-agent, per-domain HTTP cookies obtained from the Tauri WebView
popup login flow (T2). Encryption uses the OS-native keyring for key
storage and `cryptography.fernet` (AES-128-CBC + HMAC-SHA256) for the
vault blob:

  - keyring stores a per-agent Fernet key under SERVICE:{agent_id}
  - the vault blob (JSON) is encrypted with that key and written to
    `~/.dpc/agents/{agent_id}/web_credentials.enc`

The two libraries together form the encryption layer; neither alone is
sufficient — keyring does not encrypt files, cryptography has no native
key storage. On Windows the key sits in DPAPI; macOS = Keychain (Phase 2),
Linux = Secret Service (Phase 2). No code change is required from this
module for cross-platform support — keyring abstracts the backend.

Imported by: local_api.py (handles web_auth_login_complete WebSocket
command from T2), browser.py AuthBrowser (T4), firewall.py (T5), audit
hook (T6), tools/web_auth_tools.py (T7).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import keyring
from cryptography.fernet import Fernet, InvalidToken


SERVICE = "dpc-web-auth"


# eTLD+1 resolution map. Hardcoded MVP coverage; Phase 2 wires in the
# Mozilla Public Suffix List for general resolution. When an agent
# requests use_auth="example.com" we map subdomains to the same jar.
ETLD1_MAP = {
    "example.com": "example.com",
    "www.example.com": "example.com",
    "login.example.com": "example.com",
    "api.example.com": "example.com",
    "example.org": "example.org",
    "www.example.org": "example.org",
    "lk.example.org": "example.org",
}


def _extract_hostname(raw: str) -> str:
    """Extract bare lowercase hostname from user-typed input.

    Accepts bare hostnames (`example.com`, `www.example.com`) and full URLs
    (`https://www.example.com/`, `http://example.com:8080/path`). Strips
    scheme, port, path, query, fragment. Returns empty string when the
    input cannot be parsed to a hostname (e.g. `javascript:alert(1)`,
    `http://`, `://garbage`).
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    # urlsplit needs a scheme to populate `.hostname`; for bare hostname
    # input prepend `//` to coerce the rest into the authority slot.
    if "://" not in s:
        s = "//" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    return parts.hostname or ""


def resolve_etld1(domain: str) -> str:
    """Map a hostname or URL to its eTLD+1 vault key.

    Robust to URL inputs (strips scheme, port, path) so the same key
    falls out whether the caller passes `example.com`, `www.example.com`, or
    `https://www.example.com/orders`. Empty input → empty result so callers
    can reject it.
    """
    hostname = _extract_hostname(domain)
    if not hostname:
        return ""
    return ETLD1_MAP.get(hostname, hostname)


def _vault_path(agent_id: str) -> Path:
    home = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
    return home / "agents" / agent_id / "web_credentials.enc"


def _get_or_create_key(agent_id: str) -> bytes:
    """Return the per-agent Fernet key, generating + persisting if missing."""
    stored = keyring.get_password(SERVICE, agent_id)
    if stored:
        return stored.encode("ascii")
    new_key = Fernet.generate_key()
    keyring.set_password(SERVICE, agent_id, new_key.decode("ascii"))
    return new_key


def _load_vault(agent_id: str) -> dict[str, Any]:
    """Decrypt + parse the vault blob. Returns empty schema if absent or
    if the on-disk ciphertext cannot be decrypted with the current key
    (e.g. keyring entry was wiped — start fresh rather than crash)."""
    path = _vault_path(agent_id)
    if not path.exists():
        return {"domains": {}}
    key = _get_or_create_key(agent_id)
    fernet = Fernet(key)
    try:
        plaintext = fernet.decrypt(path.read_bytes())
    except InvalidToken:
        return {"domains": {}}
    return json.loads(plaintext.decode("utf-8"))


def _save_vault(agent_id: str, vault: dict[str, Any]) -> None:
    path = _vault_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _get_or_create_key(agent_id)
    fernet = Fernet(key)
    blob = json.dumps(vault, ensure_ascii=False).encode("utf-8")
    path.write_bytes(fernet.encrypt(blob))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def save_cookies(agent_id: str, domain: str, cookies: list[dict]) -> None:
    """Persist cookies for agent+domain. Replaces any existing jar for
    that domain — partial merges are not supported (cookies arrive as a
    full snapshot from the WebView popup)."""
    key = resolve_etld1(domain)
    vault = _load_vault(agent_id)
    now = _now_iso()
    vault["domains"][key] = {
        "cookies": cookies,
        "authenticated_at": now,
        "last_used_at": now,
    }
    _save_vault(agent_id, vault)


def load_cookies(agent_id: str, domain: str) -> list[dict] | None:
    """Return cookies for the eTLD+1 of `domain`, or None if no jar
    exists.

    Side effect: writes the vault back with an updated last_used_at
    timestamp so audit + UI can show recency. This is a read-on-disk +
    write-on-disk op, not a pure read — acceptable in a single-process
    desktop context but worth flagging if the vault ever moves to a
    shared store (e.g. multi-agent service mode in a future phase)."""
    key = resolve_etld1(domain)
    vault = _load_vault(agent_id)
    entry = vault["domains"].get(key)
    if entry is None:
        return None
    entry["last_used_at"] = _now_iso()
    _save_vault(agent_id, vault)
    return entry["cookies"]


def get_auth_status(agent_id: str, domain: str) -> dict:
    """Return {has_cookies, expires, authenticated_at} for the eTLD+1 jar.
    `expires` is the earliest cookie expiry (Unix epoch seconds), or None
    for session-only jars or empty jars."""
    key = resolve_etld1(domain)
    vault = _load_vault(agent_id)
    entry = vault["domains"].get(key)
    if entry is None:
        return {"has_cookies": False, "expires": None, "authenticated_at": None}
    cookies = entry.get("cookies", [])
    expires = min(
        (c["expires"] for c in cookies if c.get("expires") is not None),
        default=None,
    )
    return {
        "has_cookies": bool(cookies),
        "expires": expires,
        "authenticated_at": entry.get("authenticated_at"),
    }


def list_domains(agent_id: str) -> list[dict]:
    """All eTLD+1 jars for an agent. Used by T7 (list_auth_domains tool)
    and the per-agent UI. Returns oldest-first by authenticated_at."""
    vault = _load_vault(agent_id)
    rows = []
    for domain, entry in vault["domains"].items():
        rows.append({
            "domain": domain,
            "has_cookies": bool(entry.get("cookies")),
            "authenticated_at": entry.get("authenticated_at"),
            "last_used_at": entry.get("last_used_at"),
        })
    rows.sort(key=lambda r: r["authenticated_at"] or "")
    return rows


def revoke(agent_id: str, domain: str) -> None:
    """Remove the jar for the eTLD+1 of `domain`. Silent no-op if absent.
    Does NOT delete the per-agent Fernet key (other domains may still be
    encrypted with it)."""
    key = resolve_etld1(domain)
    vault = _load_vault(agent_id)
    if key in vault["domains"]:
        del vault["domains"][key]
        _save_vault(agent_id, vault)


def audit_append(
    agent_id: str,
    domain: str,
    url: str,
    status: int | str,
    bytes_size: int | None = None,
) -> None:
    """ADR-028 T6 — append a JSONL entry to the per-agent audit log.

    Path: ~/.dpc/agents/{agent_id}/web_audit.jsonl

    Called by `browse_page` (T4) after every authenticated request,
    success OR failure. `status` is the HTTP status integer on success,
    or a string token on failure (`auth_required`, `expired`,
    `firewall_denied`, `error`). `bytes_size` is the response body
    size when known.

    Append-only — opens in `'a'` mode each call, no handle kept, no
    rotation in MVP.

    Write boundary (per Ark S140 [#64] review): the file is written
    by the DPC core process via a direct `open(path, 'a')` that
    bypasses the agent's `file_write` tool and its sandbox extensions
    — the agent itself cannot append to or truncate this log.

    Read visibility: the file lives under the agent's own storage
    root (`~/.dpc/agents/{agent_id}/`), which IS readable by the
    agent's `file_read` tool by default. This is intentional —
    transparency rather than security boundary. The agent SHOULD be
    able to introspect its own audit history. Cross-agent isolation
    is preserved: agent_b's sandbox is rooted at agent_b's directory,
    so it cannot read agent_a's audit log.
    """
    path = _vault_path(agent_id).parent / "web_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "timestamp": _now_iso(),
        "agent_id": agent_id,
        "domain": domain,
        "url": url,
        "status": status,
    }
    if bytes_size is not None:
        entry["bytes"] = bytes_size
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    # 'a' mode is atomic for small writes on POSIX + Windows (single
    # write() syscall under the OS buffer flush). For audit entries
    # which are one short JSON line each, no separate lock is needed.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def is_expired(cookies: list[dict]) -> bool:
    """True if any cookie with an `expires` field has elapsed.
    Session cookies (expires=None) are treated as not-expired here —
    expiry detection for session cookies needs out-of-band signal
    (HTTP 401, login redirect, etc), handled by the AuthBrowser path."""
    now = time.time()
    for c in cookies:
        exp = c.get("expires")
        if exp is not None and exp <= now:
            return True
    return False
