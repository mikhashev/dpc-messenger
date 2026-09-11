"""2026-09-11: `resolve_etld1` became a real Public Suffix List resolver.

What it was: `ETLD1_MAP`, a dict of 12 test hostnames, read as
`.get(host, host)`. No real site was in it, so every real hostname came
back unchanged — and the value is a security boundary on two paths:

- `browser.py::_domain_matches` admits `host == etld1 or
  host.endswith("." + etld1)`, so `resolve_etld1("com") == "com"` opened the
  route gate to every host in `.com`;
- `browser.py::_sync_cookies_to_vault` keys the vault by the same value, so
  every `.com` cookie filed under one jar named `"com"`.

The map also could not collapse a subdomain it had never seen, so
`st.ozone.ru` kept its own jar apart from `ozone.ru`, and the no-jar
refusal rendered `'www.www.<host>'` for a `www.` input.

Pinned here: the suffix cases return None rather than passing through, the
collapse cases land on the registrable domain, wildcard and exception rules
are honoured, and the refusal message reads correctly.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

# The vault fixture (temp DPC_HOME + in-memory keyring) lives beside the
# audit tests; importing it is how pytest shares a fixture across modules.
from .test_web_audit import vault_home  # noqa: F401


# ─────────────────────────────────────────────────────────────
# A public suffix is not a domain — the passthrough is gone
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("suffix", [
    # An ICANN TLD, and the case that motivated the whole change: with
    # `"com"` in hand, `_domain_matches` admitted all of `.com`.
    "com",
    "org",
    # Two-label suffixes: nobody registers `co.uk` itself.
    "co.uk",
    "com.au",
    # Wildcard rule `*.ck` — every single-label child of `ck` is a suffix.
    "foo.ck",
    # Private-section suffix: registrable *under*, not *as*.
    "github.io",
    "s3.amazonaws.com",
])
def test_a_public_suffix_resolves_to_none_instead_of_itself(suffix):
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1(suffix) is None


@pytest.mark.parametrize("not_a_domain", [
    "localhost",          # single label, no dot
    "intranet",           # ditto — an internal short name
    "192.168.1.1",        # IPv4 literal: `1.1` is not its registrable domain
    "8.8.8.8",
    "[::1]",              # IPv6 literal, bracketed as a URL authority
    "2001:db8::1",
    "",                   # empty
    "   ",                # whitespace only
    "http://",            # scheme with no authority
])
def test_an_input_that_names_no_site_resolves_to_none(not_a_domain):
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1(not_a_domain) is None


# ─────────────────────────────────────────────────────────────
# Real hostnames collapse onto their registrable domain
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostname,expected", [
    # The CDN case named in the route handler's own comment: ozon.ru styles
    # from st.ozone.ru, and `ozone.ru` is the jar both belong to.
    ("st.ozone.ru", "ozone.ru"),
    ("ozone.ru", "ozone.ru"),
    # x.com is in no hardcoded map anywhere.
    ("www.x.com", "x.com"),
    ("x.com", "x.com"),
    ("abs.twimg.com", "twimg.com"),
    # Multi-label suffix, and more than one subdomain above it.
    ("a.b.example.co.uk", "example.co.uk"),
    ("example.co.uk", "example.co.uk"),
    # The wikipedia rows the old 12-entry map spelled out by hand; a real
    # list makes them fall out without being named.
    ("www.wikipedia.org", "wikipedia.org"),
    ("ru.wikipedia.org", "wikipedia.org"),
    # A TLD the list has never heard of: the implicit `*` rule still gives
    # one label of suffix, so the host below it is registrable.
    ("unmapped.example", "unmapped.example"),
    ("deep.sub.unmapped.example", "unmapped.example"),
    # Private-section suffix: the child IS registrable.
    ("myproject.github.io", "myproject.github.io"),
    ("sub.myproject.github.io", "myproject.github.io"),
])
def test_a_hostname_collapses_onto_its_registrable_domain(hostname, expected):
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1(hostname) == expected


def test_a_wildcard_rule_makes_the_child_a_suffix_and_the_grandchild_a_domain():
    """`*.ck` is a wildcard rule: it makes every `<x>.ck` a public suffix,
    which pushes the registrable domain one label further left. A resolver
    that only did exact lookups would call `foo.ck` a domain."""
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1("foo.ck") is None
    assert web_auth.resolve_etld1("bar.foo.ck") == "bar.foo.ck"
    assert web_auth.resolve_etld1("www.bar.foo.ck") == "bar.foo.ck"


def test_an_exception_rule_pulls_one_label_back_out_of_the_suffix():
    """`!www.ck` overrides the `*.ck` wildcard above it, so `www.ck` — alone
    among the children of `ck` — is registrable. The bang rules are the half
    of the algorithm a naive longest-match implementation drops."""
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1("www.ck") == "www.ck"
    assert web_auth.resolve_etld1("anything.www.ck") == "www.ck"
    # `!city.kobe.jp` against `*.kobe.jp`, the same shape one level deeper.
    assert web_auth.resolve_etld1("city.kobe.jp") == "city.kobe.jp"
    assert web_auth.resolve_etld1("www.city.kobe.jp") == "city.kobe.jp"
    assert web_auth.resolve_etld1("c.kobe.jp") is None


def test_the_input_is_normalised_before_it_is_matched():
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1("WWW.X.COM") == "x.com"
    # A trailing dot names the DNS root explicitly; same host.
    assert web_auth.resolve_etld1("www.x.com.") == "x.com"
    assert web_auth.resolve_etld1("https://www.x.com:8443/i/flow?x=1") == "x.com"


def test_the_list_is_read_once_and_only_when_a_domain_is_resolved():
    """Lazy by contract, not by accident: the .dat is 16k lines and most
    importers of `web_auth` (audit writers, the firewall) never resolve
    anything. Re-reading it per call would also be a per-request file read
    on the browse path."""
    from dpc_client_core import web_auth

    web_auth._PSL_INDEX = None
    assert web_auth._PSL_INDEX is None, "import alone must not build the index"

    first = web_auth._psl_index()
    assert web_auth.resolve_etld1("www.x.com") == "x.com"
    # Same object, so the file was opened once, not once per resolution.
    assert web_auth._psl_index() is first


# ─────────────────────────────────────────────────────────────
# The boundary the resolver feeds
# ─────────────────────────────────────────────────────────────


def test_the_route_gate_no_longer_admits_a_whole_tld():
    """The consequence that made this a security change rather than a
    tidy-up: `_domain_matches(url, "com")` is True for every `.com` host,
    so a session opened on an unresolvable domain must end up with an EMPTY
    allowlist — which the route handler treats as block-everything — rather
    than one holding `"com"`."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser, _domain_matches

    # Unchanged, and the reason the resolver may not pass `com` through.
    assert _domain_matches("https://unrelated-attacker.com/", "com") is True

    ab = AuthBrowser(agent_id="agent_a", domains=["com", "localhost", "1.2.3.4"])
    assert ab._etld1s == set()
    assert ab._etld1 is None

    # A real site alongside them still lands in the allowlist, once.
    ab2 = AuthBrowser(agent_id="agent_a", domains=["com", "www.x.com", "x.com"])
    assert ab2._etld1s == {"x.com"}


def test_a_vault_write_refuses_a_public_suffix_as_a_jar_key(vault_home):
    """A jar keyed `com` is one jar holding every `.com` login, handed back
    to any `.com` host by the read side. The write side is where that
    becomes durable, so it refuses loudly."""
    from dpc_client_core import web_auth

    cookies = [{"name": "s", "value": "v", "domain": ".com", "path": "/",
                "expires": int(time.time()) + 3600, "secure": True,
                "httponly": True, "samesite": "Lax"}]

    with pytest.raises(ValueError):
        web_auth.save_cookies("agent_a", "com", cookies)
    with pytest.raises(ValueError):
        web_auth.save_cookies("agent_a", "co.uk", cookies)
    with pytest.raises(ValueError):
        web_auth.save_cookies("agent_a", "192.168.1.1", cookies)

    assert web_auth.list_domains("agent_a") == []
    # And the read side answers "no jar" rather than raising, because it is
    # reached from route handling where the honest answer is absence.
    assert web_auth.load_cookies("agent_a", "com") is None
    assert web_auth.get_auth_status("agent_a", "com")["has_cookies"] is False


def _fresh_cookies(domain):
    return [{"name": "s", "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True,
             "httponly": True, "samesite": "Lax"}]


def test_the_no_jar_refusal_no_longer_doubles_the_www(vault_home):
    """The old message interpolated `'www.{resolved}'` on a value that was
    the unresolved input, so a `www.` request read back `'www.www.host'` —
    and it told the agent to sign in a second time at a spelling that shares
    the jar it already has."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    web_auth.save_cookies(
        "agent_a", "real-site.net", _fresh_cookies("real-site.net"),
    )

    root = vault_home / "agents" / "agent_a"
    root.mkdir(parents=True, exist_ok=True)
    ctx = types.SimpleNamespace(agent_root=root)

    answer = asyncio.run(browser_mod.browse_page(
        ctx, url="https://www.other-site.net/my", use_auth="www.other-site.net",
    ))

    assert "www.www." not in answer
    assert "other-site.net" in answer
    assert "real-site.net" in answer, (
        "the refusal must name the sites that do have cookies"
    )


def test_a_public_suffix_as_use_auth_is_refused_before_a_browser_opens(vault_home):
    """`use_auth="com"` used to sail through the jar check on the passthrough
    value and open a session whose allowlist was the whole TLD. It is now
    refused by name, headed or headless."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    root = vault_home / "agents" / "agent_a"
    root.mkdir(parents=True, exist_ok=True)
    ctx = types.SimpleNamespace(agent_root=root)

    for keep_open in (False, True):
        answer = asyncio.run(browser_mod.browse_page(
            ctx, url="https://anything.com/", use_auth="com", keep_open=keep_open,
        ))
        assert "not a registrable domain" in answer, f"keep_open={keep_open}"

    audit = root / "web_audit.jsonl"
    statuses = [
        json.loads(line)["status"]
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert statuses == ["auth_denied:not_a_domain", "auth_denied:not_a_domain"]
