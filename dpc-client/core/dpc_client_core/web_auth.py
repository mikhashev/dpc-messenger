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

import encodings.idna
import hashlib
import ipaddress
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import keyring
from cryptography.fernet import Fernet, InvalidToken


SERVICE = "dpc-web-auth"


# Mozilla Public Suffix List, vendored: the resolver needs no network and
# the client gains no dependency. Refresh by re-downloading the same URL
# over the same path; no code here changes with it.
#   https://publicsuffix.org/list/public_suffix_list.dat
#   fetched 2026-09-11, list version 2026-09-08_12-18-37_UTC
#   MPL 2.0 (the file's own header) — data, distributable under §3.3
#   alongside this GPL-3.0-or-later client.
_PSL_PATH = Path(__file__).parent / "data" / "public_suffix_list.dat"

# Parsed on first use, never at import: 334 KB / 16k lines, and most
# importers of this module (audit writers, firewall) resolve no domain.
_PSL_INDEX: tuple[frozenset[str], frozenset[str], frozenset[str]] | None = None


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


def _to_punycode(rule: str) -> str | None:
    """A-label form of an IDN rule, or None when it is ASCII already or
    will not encode. A rule that will not encode is simply not aliased —
    its Unicode spelling stays in the index, so nothing is lost."""
    if rule.isascii():
        return None
    try:
        return ".".join(
            encodings.idna.ToASCII(label).decode("ascii")
            for label in rule.split(".")
        )
    except (UnicodeError, ValueError):
        return None


def _psl_index() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Parse the vendored list into (plain rules, wildcard tails, exceptions).

    Wildcards are keyed by the part to the RIGHT of the star — rule `*.ck`
    is stored as `ck` — because that is what the match loop has in hand.
    Exceptions keep their spelling minus the bang (`!www.ck` → `www.ck`).

    An IDN rule is indexed twice, Unicode and punycode: the list is UTF-8
    (`公司.cn`) while a hostname off a URL is usually already A-label
    (`xn--55qx5d.cn`), and the matcher compares strings.
    """
    global _PSL_INDEX
    if _PSL_INDEX is not None:
        return _PSL_INDEX
    rules: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    with open(_PSL_PATH, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()  # .strip() also absorbs a CRLF checkout
            if not line or line.startswith("//"):
                continue
            # The format ends a rule at the first whitespace; the rest of
            # the line is commentary.
            line = line.split()[0].lower()
            if line.startswith("!"):
                bucket, value = exceptions, line[1:]
            elif line.startswith("*."):
                bucket, value = wildcards, line[2:]
            else:
                bucket, value = rules, line
            bucket.add(value)
            ascii_form = _to_punycode(value)
            if ascii_form is not None:
                bucket.add(ascii_form)
    _PSL_INDEX = (frozenset(rules), frozenset(wildcards), frozenset(exceptions))
    return _PSL_INDEX


def _is_ip_literal(hostname: str) -> bool:
    """True for `192.168.1.1` and `::1` alike.

    Left to the label matcher an IPv4 literal yields a registrable domain
    anyway: `1` is in no rule, so the implicit `*` would call `1.1` the
    eTLD+1 of `192.168.1.1`.
    """
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def resolve_etld1(domain: str) -> str | None:
    """Map a hostname or URL to its registrable domain (eTLD+1) — the vault key.

    Robust to URL inputs (strips scheme, port, path) so the same key falls
    out whether the caller passes `example.com`, `www.example.com`, or
    `https://www.example.com/orders`. Input is lowercased; a trailing root
    dot names the same host and is dropped.

    Returns **None**, never the input, when there is no registrable domain.
    Callers spend this value as a security boundary — the route gate in
    `browser.py::_domain_matches` admits `host.endswith("." + etld1)`, and
    the vault uses it as a jar key — so an unresolvable input must not come
    back spendable as a domain. None for:

      - input that parses to no hostname (empty, whitespace, `http://`);
      - a public suffix standing alone: `com`, `co.uk`, `foo.ck` under the
        wildcard rule `*.ck`. Nobody can register it, so it is not a domain;
      - a single label — `localhost`, or the `javascript` that
        `javascript:alert(1)` parses to. Same case: under the implicit `*`
        rule the lone label IS the public suffix;
      - an IPv4 or IPv6 literal.

    `www.ck` does resolve, to itself: the exception rule `!www.ck` shortens
    the prevailing suffix to `ck`.
    """
    hostname = _extract_hostname(domain)
    if not hostname:
        return None
    hostname = hostname.rstrip(".")
    if not hostname or _is_ip_literal(hostname):
        return None
    labels = hostname.split(".")
    if any(not label for label in labels):
        return None  # `a..b`, `.a` — not a hostname

    rules, wildcards, exceptions = _psl_index()
    n = len(labels)

    # Public Suffix List algorithm (https://publicsuffix.org/list/).
    # An exception beats every other match and shortens the prevailing
    # suffix by its own leftmost label.
    for length in range(1, n + 1):
        if ".".join(labels[n - length:]) in exceptions:
            suffix_len = length - 1
            break
    else:
        # Otherwise the longest matching rule prevails, hence the walk from
        # the longest candidate down; no match at all means the implicit
        # `*`, where the rightmost label alone is the public suffix.
        suffix_len = 1
        for length in range(n, 0, -1):
            if ".".join(labels[n - length:]) in rules:
                suffix_len = length
                break
            # `*.ck` matches `<anything>.ck`: the star eats one label, so a
            # wildcard tail of k labels answers a suffix of k+1.
            if length >= 2 and ".".join(labels[n - length + 1:]) in wildcards:
                suffix_len = length
                break

    if suffix_len >= n:
        return None  # the hostname IS the suffix; there is nothing under it
    return ".".join(labels[n - suffix_len - 1:])


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


APPROVAL_VIA_LOGIN_WINDOW = "login_window"


# A credential is a secret, not a setting: `lang=en` is httpOnly on some
# sites and identical for every account, so a short value must not become
# the anchor that makes two accounts look like one.
IDENTITY_MIN_VALUE_LEN = 8


def _is_identity_bearing(cookie: dict) -> bool:
    """True for a cookie that could be carrying the login itself.

    `httponly` is the site-independent mark of one: the server sets it and
    the page's own JavaScript cannot read it, which is what a session
    credential is built to be. A property of the cookie, not a name in a
    list — a list of names is wrong for every site not on it.
    """
    if not cookie.get("httponly"):
        return False
    return len(str(cookie.get("value") or "")) >= IDENTITY_MIN_VALUE_LEN


def identity_marks(cookies: list[dict] | None) -> list[str]:
    """Sorted hashes of the identity-bearing (name, value) pairs — the
    identity an approval was granted for, stored beside it.

    Hashed rather than plain: this is copied forward on every writeback,
    and a session token is the one thing here worth stealing."""
    marks = {
        hashlib.sha256(
            f"{c.get('name', '')}\x00{c.get('value', '')}".encode("utf-8")
        ).hexdigest()
        for c in cookies or []
        if _is_identity_bearing(c)
    }
    return sorted(marks)


def _identity_changed(previous: dict, cookies: list[dict]) -> bool:
    """Did the login these cookies carry stop being the approved one?

    Intersection, not equality, and that is the design: anything in common
    is the same identity refreshed, nothing in common is a different one.
    Equality would read a rotation as a switch — a site re-issuing one of
    its own session cookies leaves the long-lived one alone, so a mark
    survives, while a sign-in as another account comes through a login
    window that starts empty, so no mark does.

    Three things it does not catch. A site rotating every identity-bearing
    cookie at once reads as a switch and costs one extra dialog — the
    direction this is optimised to fail in. A jar with no identity-bearing
    cookie on either side yields no signal and the approval is carried as
    before: the empty/empty branch, which must stay, or such a site would
    demand re-approval on every writeback. And two accounts handed the same
    httpOnly value read as one identity, which no jar can see from
    outside."""
    before = set(previous.get("approved_identity") or [])
    after = set(identity_marks(cookies))
    if not before and not after:
        return False
    return not (before & after)


def save_cookies(
    agent_id: str,
    domain: str,
    cookies: list[dict],
    *,
    approved_via: str | None = None,
) -> None:
    """Persist cookies for agent+domain. Replaces any existing jar for
    that domain — partial merges are not supported (cookies arrive as a
    full snapshot).

    Cookies are data; approval is a decision, and the two live in separate
    fields. `authenticated_at` says when these cookie *bytes* were written
    — the ordinary navigate/close writeback moves it, so it can never mean
    "a human approved this login". The `approved` block, `{"at", "via"}`,
    is written only when `approved_via` is passed. Without it none is
    created, and an existing one is carried forward only while these
    cookies still carry the identity it was granted for — refreshed bytes
    are neither a re-approval nor a revocation, but a sign-in as somebody
    else is not a refresh. See `_identity_changed` for what that can and
    cannot see.

    One production path passes `approved_via`: `commit_login_cookies`, the
    single writer reached from the branch where a person answered yes,
    which lands the cookies and the decision in this one call. Every writer
    of cookie *bytes* alone — navigate writeback, close — leaves it None,
    which is what makes "cookies appeared" incapable of authorising
    anything. A login window's poll is no longer among them: it snapshots
    into memory, so an unanswered window writes nothing here at all.

    Raises ValueError when `domain` has no registrable domain: a jar keyed
    `com` is one jar holding every `.com` login, handed to any `.com` host
    by the read side."""
    key = resolve_etld1(domain)
    if key is None:
        raise ValueError(
            f"{domain!r} has no registrable domain (eTLD+1) — refusing to "
            f"store cookies under a public suffix or an address"
        )
    vault = _load_vault(agent_id)
    now = _now_iso()
    previous = vault["domains"].get(key) or {}
    entry: dict[str, Any] = {
        "cookies": cookies,
        "authenticated_at": now,
        "last_used_at": now,
    }
    if approved_via is not None:
        entry["approved"] = {"at": now, "via": approved_via}
        entry["approved_identity"] = identity_marks(cookies)
    elif previous.get("approved") is not None:
        if _identity_changed(previous, cookies):
            logging.getLogger(__name__).info(
                "dropping the approval for %s (agent %s): these cookies carry "
                "a different identity from the one that was approved",
                key, agent_id,
            )
            # Into the audit as well, because the next thing that happens is
            # a person being asked to approve a site they already approved.
            # Without a row, the only answer available to them is a guess.
            _append_audit(agent_id, {
                "timestamp": now,
                "agent_id": agent_id,
                "domain": key,
                "action": "approval_dropped",
                "url": "",
                "result": "ok",
                "reason": "identity_changed",
            })
        else:
            entry["approved"] = previous["approved"]
            # Re-stamped, not copied: the marks describe the cookies now in
            # the jar, so a rotation that keeps one mark also keeps the new
            # ones for the next comparison.
            entry["approved_identity"] = identity_marks(cookies)
    vault["domains"][key] = entry
    _save_vault(agent_id, vault)


def get_approval(agent_id: str, domain: str) -> dict | None:
    """The recorded human approval for the eTLD+1 of `domain`, or None.

    None covers no jar, an unresolvable input, and a jar written before
    approvals existed — all three mean "no human has been observed logging
    in here", which is what the gate asks. Pre-existing jars are
    deliberately not grandfathered: grandfathering re-imports the defect
    that the jar's mere presence was the approval."""
    key = resolve_etld1(domain)
    if key is None:
        return None
    entry = _load_vault(agent_id)["domains"].get(key)
    if entry is None:
        return None
    approved = entry.get("approved")
    return approved if isinstance(approved, dict) else None


def is_approved(agent_id: str, domain: str) -> bool:
    """True iff a human login for this eTLD+1 was recorded as approved."""
    return get_approval(agent_id, domain) is not None


def record_approval(
    agent_id: str,
    domain: str,
    via: str = APPROVAL_VIA_LOGIN_WINDOW,
) -> dict | None:
    """Write a human's yes onto an existing jar, touching no cookie.

    It does read them: the identity marks of the jar as it stands are
    recorded beside the approval, so a later sign-in as a different account
    cannot inherit this yes.

    Takes no cookie argument on purpose: an approval that arrives together
    with a cookie snapshot is one a browser can mint by loading a page. The
    caller must already hold an explicit answer from a person.

    This is the path for a yes that arrives over a jar already on disk. A
    login window is not one: it holds its cookies in memory and a yes puts
    both down at once, through `save_cookies(approved_via=...)`.

    Returns the stored `{"at", "via"}` block, or None when `domain` has no
    registrable domain or no jar — an approval over no cookies passes the
    gate only to fail at the first request."""
    key = resolve_etld1(domain)
    if key is None:
        return None
    vault = _load_vault(agent_id)
    entry = vault["domains"].get(key)
    if entry is None:
        return None
    approved = {"at": _now_iso(), "via": via}
    entry["approved"] = approved
    entry["approved_identity"] = identity_marks(entry.get("cookies"))
    vault["domains"][key] = entry
    _save_vault(agent_id, vault)
    return approved


def load_cookies(agent_id: str, domain: str) -> list[dict] | None:
    """Return cookies for the eTLD+1 of `domain`, or None if no jar
    exists.

    Side effect: writes the vault back with an updated last_used_at
    timestamp so audit + UI can show recency. This is a read-on-disk +
    write-on-disk op, not a pure read — acceptable in a single-process
    desktop context but worth flagging if the vault ever moves to a
    shared store (e.g. multi-agent service mode in a future phase).

    An unresolvable `domain` reads as "no jar" rather than raising: the
    read side is reached from route handling and audit paths where the
    honest answer is that no such jar can exist."""
    key = resolve_etld1(domain)
    if key is None:
        return None
    vault = _load_vault(agent_id)
    entry = vault["domains"].get(key)
    if entry is None:
        return None
    entry["last_used_at"] = _now_iso()
    _save_vault(agent_id, vault)
    return entry["cookies"]


def get_auth_status(agent_id: str, domain: str) -> dict:
    """Return {has_cookies, expires, authenticated_at, approved} for the
    eTLD+1 jar. `expires` is the earliest cookie expiry (Unix epoch
    seconds), or None for session-only jars or empty jars. `approved` is
    the human-approval block or None — having cookies and being approved
    are independent facts. An unresolvable `domain` reports the same shape
    as an absent jar — no jar can exist for it."""
    key = resolve_etld1(domain)
    vault = _load_vault(agent_id) if key is not None else {"domains": {}}
    entry = vault["domains"].get(key)
    if entry is None:
        return {
            "has_cookies": False, "expires": None,
            "authenticated_at": None, "approved": None,
        }
    cookies = entry.get("cookies", [])
    expires = min(
        (c["expires"] for c in cookies
         if c.get("expires") is not None and c["expires"] > 0),
        default=None,
    )
    return {
        "has_cookies": bool(cookies),
        "expires": expires,
        "authenticated_at": entry.get("authenticated_at"),
        "approved": entry.get("approved"),
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
            "approved": entry.get("approved"),
            "authenticated_at": entry.get("authenticated_at"),
            "last_used_at": entry.get("last_used_at"),
        })
    rows.sort(key=lambda r: r["authenticated_at"] or "")
    return rows


def revoke(agent_id: str, domain: str) -> None:
    """Remove the jar for the eTLD+1 of `domain`. Silent no-op if absent,
    or if `domain` has no registrable domain. Does NOT delete the per-agent
    Fernet key (other domains may still be encrypted with it)."""
    key = resolve_etld1(domain)
    if key is None:
        return
    vault = _load_vault(agent_id)
    if key in vault["domains"]:
        del vault["domains"][key]
        _save_vault(agent_id, vault)


# ── Audit log: one file per agent, and it has to stay openable ────────
#
# The route gate audits per *request*, not per page, so a single page load
# can contribute hundreds of rows. Unbounded, the file that exists to make
# `domain_blocked` findable becomes the reason nobody opens it. Rotation is
# by size at write time — there is no long-lived handle to reopen, every
# append is its own `open`, so checking the size costs one stat.
WEB_AUDIT_MAX_BYTES = 5 * 1024 * 1024
# Generations kept beside the live file (`.1` newest). A security record
# should not vanish on a schedule; four files bound the cost at ~20 MB and
# keep roughly the last of it.
WEB_AUDIT_KEEP = 3


def audit_path(agent_id: str) -> Path:
    """`~/.dpc/agents/{agent_id}/web_audit.jsonl` — the one place both
    audit writers resolve, so rotation cannot apply to one and not the
    other."""
    return _vault_path(agent_id).parent / "web_audit.jsonl"


def _rotate_audit_if_needed(path: Path) -> None:
    """Roll `web_audit.jsonl` → `.1` → `.2` → `.3` when it outgrows the
    cap. Best-effort by construction: a rotation that fails must not cost
    the caller its audit row, so every error here is swallowed and the
    append goes to the oversized file instead of nowhere."""
    try:
        if path.stat().st_size < WEB_AUDIT_MAX_BYTES:
            return
    except OSError:
        return  # not there yet, or unreadable — nothing to roll
    try:
        oldest = path.with_suffix(path.suffix + f".{WEB_AUDIT_KEEP}")
        if oldest.exists():
            oldest.unlink()
        for gen in range(WEB_AUDIT_KEEP - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{gen}")
            if src.exists():
                os.replace(src, path.with_suffix(path.suffix + f".{gen + 1}"))
        os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError as exc:
        # Windows: another process holding the file open defeats the
        # rename. Losing the roll is survivable; losing the row is not.
        import logging
        logging.getLogger(__name__).warning(
            "web audit rotation failed at %s: %s", path, exc
        )


def _append_audit(agent_id: str, entry: dict[str, Any]) -> None:
    path = audit_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_audit_if_needed(path)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    # 'a' mode is atomic for small writes on POSIX + Windows (single
    # write() syscall under the OS buffer flush). For audit entries
    # which are one short JSON line each, no separate lock is needed.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


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

    Append-only — opens in `'a'` mode each call, no handle kept.
    Rotated by size at write time (`WEB_AUDIT_MAX_BYTES`), because the
    route gate writes per request and an unbounded file is one nobody
    opens.

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
    entry: dict[str, Any] = {
        "timestamp": _now_iso(),
        "agent_id": agent_id,
        "domain": domain,
        "url": url,
        "status": status,
    }
    if bytes_size is not None:
        entry["bytes"] = bytes_size
    _append_audit(agent_id, entry)


def log_browser_action(
    agent_id: str,
    domain: str,
    action: str,
    url: str,
    result: str = "ok",
    **extra: Any,
) -> None:
    """Append browser-action entry to web_audit.jsonl. result ∈ {ok, failed, denied}."""
    entry: dict[str, Any] = {
        "timestamp": _now_iso(),
        "agent_id": agent_id,
        "domain": domain,
        "action": action,
        "url": url,
        "result": result,
    }
    if extra:
        entry.update(extra)
    _append_audit(agent_id, entry)


# ── Per-site CDN manifest, and the refusals it is grown from ──────────
#
# Two files, deliberately not one. The manifest is the authority and sits
# at node level, outside every agent's sandbox root (`~/.dpc/agents/<id>/`):
# an agent that can write its own permission has none. The refusals are
# evidence, per agent, beside the audit log. The gate reads the manifest and
# never the refusals — that separation is the security property, so a
# page that probes a thousand hosts widens nothing and only fills the list a
# human is later shown.
CDN_MANIFEST_FILENAME = "web_cdn_manifest.json"
CDN_REFUSALS_FILENAME = "web_cdn_refusals.json"
# The name this file carried before the rename. Read as a fallback and folded
# forward on the next write: the record's only purpose is to be promoted from
# by a human, so a rename that drops it destroys exactly the thing it is for.
CDN_REFUSALS_LEGACY_FILENAME = "web_cdn_observed.json"

# In code, not shipped as a file: a fresh install has no manifest on disk
# and x.com without these three hosts renders blank. Exact hosts, never
# suffixes — `*.twimg.com` would re-open by pattern what the eTLD+1
# allowlist closes by name.
CDN_MANIFEST_SEED: dict[str, tuple[str, ...]] = {
    "x.com": ("abs.twimg.com", "pbs.twimg.com", "video.twimg.com"),
}

_cdn_manifest_cache: tuple[tuple[str, float, int], dict[str, frozenset[str]]] | None = None


def cdn_manifest_path() -> Path:
    home = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
    return home / CDN_MANIFEST_FILENAME


def cdn_refusals_path(agent_id: str) -> Path:
    return _vault_path(agent_id).parent / CDN_REFUSALS_FILENAME


def cdn_refusals_legacy_path(agent_id: str) -> Path:
    """Where the same record lived before the rename. Never written to."""
    return _vault_path(agent_id).parent / CDN_REFUSALS_LEGACY_FILENAME


def _normalise_manifest(raw: Any) -> dict[str, frozenset[str]]:
    """Lowercase and drop anything that is not a host string.

    A malformed manifest yields an empty entry rather than an exception:
    this is read on the request path, so a typo in the file must fail
    closed, not fail the browser."""
    out: dict[str, set[str]] = {}
    if not isinstance(raw, dict):
        return {}
    sites = raw.get("sites") if isinstance(raw.get("sites"), dict) else raw
    if not isinstance(sites, dict):
        return {}
    for site, hosts in sites.items():
        if not isinstance(site, str) or not isinstance(hosts, (list, tuple, set)):
            continue
        clean = {
            h.strip().lower().rstrip(".")
            for h in hosts
            if isinstance(h, str) and h.strip()
        }
        if clean:
            out.setdefault(site.strip().lower(), set()).update(clean)
    return {site: frozenset(hosts) for site, hosts in out.items()}


def load_cdn_manifest(force: bool = False) -> dict[str, frozenset[str]]:
    """Seed merged with `~/.dpc/web_cdn_manifest.json`.

    Cached on the file's path, mtime and size, because one page load asks
    once per refused subresource. The path is part of the key because
    `DPC_HOME` moves — two homes whose manifests happen to share a size and
    a coarse mtime must not read as one file. `force=True` for tests and for
    the promotion path, which must not read its own stale cache back."""
    global _cdn_manifest_cache
    path = cdn_manifest_path()
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime, st.st_size)
    except OSError:
        stamp = (str(path), 0.0, 0)
    if not force and _cdn_manifest_cache is not None and _cdn_manifest_cache[0] == stamp:
        return _cdn_manifest_cache[1]

    merged: dict[str, set[str]] = {
        site: set(hosts) for site, hosts in CDN_MANIFEST_SEED.items()
    }
    if stamp[1:] != (0.0, 0):
        try:
            on_disk = _normalise_manifest(
                json.loads(path.read_text(encoding="utf-8"))
            )
            for site, hosts in on_disk.items():
                merged.setdefault(site, set()).update(hosts)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "CDN manifest at %s is unreadable (%s) — falling back to the "
                "built-in seed only", path, exc
            )
    frozen = {site: frozenset(hosts) for site, hosts in merged.items()}
    _cdn_manifest_cache = (stamp, frozen)
    return frozen


def cdn_manifest_hosts(site: str) -> frozenset[str]:
    """Hosts `site` may pull subresources from. Empty for a site nobody has
    approved anything for — which is the default, and the point."""
    if not site:
        return frozenset()
    return load_cdn_manifest().get(site.strip().lower(), frozenset())


def _write_json_atomic(path: Path, data: Any) -> None:
    """Serialise first, then replace: `open(path, "w")` truncates before the
    encoder has produced a byte, so a failure there leaves an empty file
    where the record used to be."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)


REFUSAL_REASON_UNLISTED = "unlisted"
REFUSAL_REASON_BLOCKED = "blocked"


def _read_refusals_doc(path: Path) -> dict | None:
    """The `{"sites": {...}}` document at `path`, or None when there is none
    to be had. None and an empty document differ: only None falls back."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
        return None
    return raw


def _read_refusals_doc_with_legacy(agent_id: str) -> dict:
    """The current file, else the pre-rename one, else empty. An unreadable
    current file falls back too: this record exists to be shown to a human,
    and a stale list beats a blank one."""
    doc = _read_refusals_doc(cdn_refusals_path(agent_id))
    if doc is None:
        doc = _read_refusals_doc(cdn_refusals_legacy_path(agent_id))
    return doc if doc is not None else {"sites": {}}


def load_cdn_refusals(agent_id: str) -> dict[str, dict[str, dict]]:
    """`{site: {host: {first_seen, last_seen, count, reasons}}}` — what was
    refused and might be worth allowing. Read by the UI that asks the human,
    and by nothing on the request path."""
    return _read_refusals_doc_with_legacy(agent_id)["sites"]


def record_cdn_refusals(
    agent_id: str, attempts: "list[tuple]"
) -> None:
    """Fold `(site, host, count)` or `(site, host, count, reason)` attempts
    into the refusal file.

    Batched because the caller coalesces: one write when a destination is
    first refused, one more at session close carrying the rest of the count.
    `first_seen` is written once and never moved — it is the field that
    answers "since when", and a later flush must not overwrite it.

    `reasons` counts refusals per branch: `unlisted` is a host a promotion
    would admit, `blocked` one it would not, because that branch refuses on
    method or navigation before the manifest is consulted.

    The base may come from the pre-rename file, so an upgrade carries the old
    counts forward rather than restarting them; the write always lands on the
    current name, and the old file is left alone rather than deleted."""
    if not attempts:
        return
    path = cdn_refusals_path(agent_id)
    raw = _read_refusals_doc_with_legacy(agent_id)
    now = _now_iso()
    for attempt in attempts:
        site, host, count = attempt[0], attempt[1], attempt[2]
        reason = attempt[3] if len(attempt) > 3 else REFUSAL_REASON_UNLISTED
        if not site or not host or count <= 0:
            continue
        entry = raw["sites"].setdefault(site, {}).setdefault(host, {})
        entry.setdefault("first_seen", now)
        entry["last_seen"] = now
        entry["count"] = int(entry.get("count", 0)) + int(count)
        reasons = entry.setdefault("reasons", {})
        reasons[reason] = int(reasons.get(reason, 0)) + int(count)
    _write_json_atomic(path, raw)


def promote_cdn_refusals(site: str, hosts: "list[str]") -> frozenset[str]:
    """Move refused hosts into the manifest — the one-click "x.com wants 6
    domains — allow?" lands here, and nowhere else does anything become
    allowed. Returns the site's hosts after the promotion.

    Takes an explicit host list rather than "everything refused for this
    site": the human approves what they were shown, and a refusal that
    arrived between the render and the click is not part of that answer."""
    site = (site or "").strip().lower()
    clean = sorted({
        h.strip().lower().rstrip(".")
        for h in hosts or []
        if isinstance(h, str) and h.strip()
    })
    if not site or not clean:
        return cdn_manifest_hosts(site)
    path = cdn_manifest_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
            raw = {"sites": {}}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {"sites": {}}
    existing = raw["sites"].get(site)
    merged = sorted(set(existing if isinstance(existing, list) else []) | set(clean))
    raw["sites"][site] = merged
    _write_json_atomic(path, raw)
    load_cdn_manifest(force=True)
    return cdn_manifest_hosts(site)


def is_expired(cookies: list[dict]) -> bool:
    """True if ALL cookies with an `expires` field have elapsed.
    Returns False if at least one non-session cookie is still valid —
    the server will refresh short-lived tokens (CSRF, session) when
    presented with a valid long-lived auth cookie."""
    now = time.time()
    has_any_expiring = False
    for c in cookies:
        exp = c.get("expires")
        if exp is not None and exp > 0:
            has_any_expiring = True
            if exp > now:
                return False
    return has_any_expiring


def filter_expired(cookies: list[dict]) -> list[dict]:
    """Remove cookies whose `expires` has elapsed. Session cookies
    (expires=None or expires<=0) are kept."""
    now = time.time()
    return [
        c for c in cookies
        if not (c.get("expires") is not None and c["expires"] > 0 and c["expires"] <= now)
    ]
