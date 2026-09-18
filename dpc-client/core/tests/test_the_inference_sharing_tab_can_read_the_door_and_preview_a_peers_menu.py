"""Five commands the Inference Sharing tab needs and the local API did not have.

Blocks (5) and (6) of that tab — the IDE door and the guest preview — plus the
validate-without-saving call each asked the backend a question nothing
answered: no command read the gateway's state or rotated its key, none said
what one named peer would be sent, and `validate_firewall_rules` took a string
and handed it to a validator that reads a dict, so wired as it stood it would
have answered "invalid" to everything.

The menu command is the one with teeth. It does not describe the wire, it uses
it: `CoreService.menu_for_peer` is the single selection both senders of
`PROVIDERS_RESPONSE` call — the peer's own `GET_PROVIDERS` and the notify
after a firewall save — so a preview that disagreed with the wire would be a
function disagreeing with itself. The two used to disagree for real: the
notify path had no type branch and could never send a transcription row.

Cross-platform: a real `aiohttp` listener on port 0 and a real key file under
`tmp_path`; no sleeps, no fixed ports.
"""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.gateway import (
    GATEWAY_HOST,
    GATEWAY_KEY_NAME,
    GatewayServer,
    MenuEntry,
    client_config_lines,
    mask_gateway_key,
    new_gateway_key,
)
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService

DOCS = Path(__file__).resolve().parents[3] / "docs" / "CONFIGURATION.md"

PEER = "dpc-node-" + "a" * 32
STRANGER = "dpc-node-" + "b" * 32
LOCAL = "ollama_local"
WHISPER = "whisper_local"
GROUP = "friends"


class _Provider:
    def __init__(self, type_, model):
        self.config = {"type": type_, "model": model}
        self.model = model

    def supports_vision(self):
        return False


def _entry(alias: str, **overrides) -> dict:
    """One menu entry as the gateway builds it, for the tests that call the
    renderer directly."""
    entry = MenuEntry(id=alias, owner="local", alias=alias, label=alias)
    return {**entry.as_dict(), **overrides}


def _rules(tmp_path: Path, **blocks) -> ContextFirewall:
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(blocks), encoding="utf-8")
    return ContextFirewall(path)


def _service(tmp_path: Path, firewall: ContextFirewall, *, providers=None,
             gateway=None, peers=None, known=None) -> SimpleNamespace:
    """A stand-in carrying only what the five commands reach for, with the real
    CoreService methods bound to it."""
    service = SimpleNamespace(
        firewall=firewall,
        llm_manager=SimpleNamespace(
            providers=providers if providers is not None else {LOCAL: _Provider("ollama", "gemma3:27b")},
            lookup_context_window=lambda model: None,
        ),
        p2p_manager=SimpleNamespace(
            peers=dict(peers or {}),
            peer_cache=SimpleNamespace(get_all_peers=lambda: dict(known or {})),
        ),
        gateway=gateway,
        settings=SimpleNamespace(
            get_gateway_enabled=lambda: gateway is not None,
            get_gateway_port=lambda: 9997,
            get_vision_max_image_size_mb=lambda: 5,
            get_remote_inference_timeout=lambda: 1.0,
        ),
        hub_client=SimpleNamespace(),
        peer_metadata={},
        _provider_supports_voice=lambda provider: False,
    )
    for name in ("menu_for_peer", "build_p2p_provider_info", "menu_tariff", "get_gateway_state",
                 "rotate_gateway_key", "get_gateway_client_lines",
                 "get_peer_provider_menu", "validate_firewall_rules"):
        setattr(service, name, getattr(CoreService, name).__get__(service, SimpleNamespace))
    service.menu_settings = CoreService.menu_settings
    return service


def _gateway(tmp_path: Path, service_holder: dict) -> GatewayServer:
    """A server whose `_core` is filled in after the service exists."""
    server = GatewayServer(MagicMock(), host=GATEWAY_HOST, port=0,
                           key_path=tmp_path / GATEWAY_KEY_NAME)
    service_holder["server"] = server
    return server


# --- (1) get_gateway_state ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_state_says_configured_and_listening_apart(tmp_path):
    """A door that refused to open is configured and not running, and a card
    that read only one of the two would say the wrong thing about both."""
    holder = {}
    server = _gateway(tmp_path, holder)
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    state = await service.get_gateway_state()

    assert state["enabled"] is True
    assert state["running"] is False, "nothing has started the listener"
    assert state["bind"] == GATEWAY_HOST
    assert state["port"] == 0, "the port as configured; the bound one appears after start"
    assert state["serving_local"] == [LOCAL] and state["serving_vendor"] == []
    assert state["serving_error"] is None
    assert state["compute_enabled"] is True
    assert state["key_file"].endswith(GATEWAY_KEY_NAME)
    assert state["key_masked"] is None, "no key exists before the first start"


@pytest.mark.asyncio
async def test_the_state_carries_a_masked_key_and_never_the_key(tmp_path):
    holder = {}
    server = _gateway(tmp_path, holder)
    (tmp_path / GATEWAY_KEY_NAME).write_text("sekrit-key-1234", encoding="utf-8")
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    state = await service.get_gateway_state()

    assert state["key_masked"] == mask_gateway_key("sekrit-key-1234") == "sekr…1234"
    assert "sekrit-key-1234" not in json.dumps(state)


@pytest.mark.asyncio
async def test_a_serving_list_that_cannot_be_classified_is_named_not_hidden(tmp_path):
    """The card must be able to say why the door serves nothing, so the
    refusal travels as words rather than as an empty list."""
    holder = {}
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": ["gone"]})
    service = _service(tmp_path, firewall, gateway=_gateway(tmp_path, holder))

    state = await service.get_gateway_state()

    assert state["serving_local"] == [] and state["serving_vendor"] == []
    assert "gone" in state["serving_error"]


@pytest.mark.asyncio
async def test_the_state_answers_with_no_gateway_at_all(tmp_path, monkeypatch):
    """`[gateway] enabled` off is the default, and the card still has to render
    something honest rather than an error."""
    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", tmp_path)
    firewall = _rules(tmp_path, compute={"enabled": False})
    service = _service(tmp_path, firewall, gateway=None)

    state = await service.get_gateway_state()

    assert state["enabled"] is False and state["running"] is False
    assert state["port"] == 9997 and state["key_masked"] is None
    assert state["compute_enabled"] is False


# --- (2) rotate_gateway_key ---------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_swaps_the_key_in_the_running_listener(tmp_path):
    """The old key is refused on the next request with no restart, the new one
    is served, the file holds the new one, and a restart reads it back."""
    import aiohttp

    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    core = _service(tmp_path, firewall)
    server = GatewayServer(core, host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    core.gateway = server
    await server.start()
    try:
        old = (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip()
        url = f"http://{GATEWAY_HOST}:{server.port}/v1/models"

        async def get(key):
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {key}"}) as resp:
                    return resp.status

        assert await get(old) == 200

        answer = await core.rotate_gateway_key()
        new = answer["key"]

        assert answer["status"] == "success"
        assert new != old and answer["key_masked"] == mask_gateway_key(new)
        assert await get(old) == 401, "the old key stops working with no restart"
        assert await get(new) == 200
        assert (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip() == new
    finally:
        await server.stop()

    restarted = GatewayServer(core, host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    core.gateway = restarted
    await restarted.start()
    try:
        assert restarted._key == new, "a restart reads the rotated key, it does not mint a third"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_rotation_with_no_listener_still_rewrites_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", tmp_path)
    (tmp_path / GATEWAY_KEY_NAME).write_text("the-old-one", encoding="utf-8")
    service = _service(tmp_path, _rules(tmp_path), gateway=None)

    answer = await service.rotate_gateway_key()

    assert answer["key"] != "the-old-one"
    assert (tmp_path / GATEWAY_KEY_NAME).read_text(encoding="utf-8").strip() == answer["key"]


# --- (3) get_gateway_client_lines --------------------------------------------


@pytest.mark.asyncio
async def test_the_client_lines_carry_the_key_and_the_served_aliases(tmp_path):
    holder = {}
    server = _gateway(tmp_path, holder)
    (tmp_path / GATEWAY_KEY_NAME).write_text("sekrit-key-1234", encoding="utf-8")
    firewall = _rules(tmp_path, compute={"enabled": True, "serving_local": [LOCAL]})
    service = _service(tmp_path, firewall, gateway=server)

    answer = await service.get_gateway_client_lines()

    clients = {row["client"]: row["text"] for row in answer["lines"]}
    assert set(clients) == {"continue", "cursor", "claude_code", "curl"}
    for text in clients.values():
        assert "sekrit-key-1234" in text, "these lines exist to be pasted (Mike's call)"
    assert LOCAL in clients["continue"] and LOCAL in clients["claude_code"]
    assert answer["key_masked"] == "sekr…1234"
    assert [entry["id"] for entry in answer["menu"]] == [LOCAL]
    assert answer["selected_id"] == LOCAL
    json.loads(clients["continue"])


def test_the_documented_snippets_are_this_functions_own_output():
    """The page and the button render from one place, or they drift: the two
    blocks `docs/CONFIGURATION.md` shows are compared byte for byte."""
    doc = io.open(DOCS, encoding="utf-8").read()
    key = "<contents of ~/.dpc/.gateway_key>"
    rendered = {row["client"]: row["text"] for row in
                client_config_lines(9997, key, [_entry(LOCAL)])}
    # The page shows the window exported too, so the block it documents is one
    # rendered for an entry that states one.
    with_window = {row["client"]: row["text"] for row in
                   client_config_lines(9997, key, [_entry(LOCAL, context_window=LOCAL_WINDOW)])}

    assert rendered["continue"] in doc, "the Continue example is no longer what the command renders"
    assert with_window["claude_code"] in doc, "the Claude Code example has drifted from the command"
    assert with_window["claude_code"] != rendered["claude_code"], (
        "the documented block is the one that carries the window"
    )


def test_a_door_that_has_never_started_says_so_where_the_key_would_be():
    (line,) = [row for row in client_config_lines(9997, "", []) if row["client"] == "curl"]
    assert "<alias>" in client_config_lines(9997, "k", [])[0]["text"]
    assert line["text"].endswith('Bearer "')


SPACED = "qwen3.8 27b Mythos"  # this node's own alias, as in the card
QUOTED = "it's mine"  # the other character a shell reads inside a bare word


@pytest.mark.parametrize("alias", [SPACED, QUOTED])
def test_the_shell_block_quotes_an_alias_the_shell_would_otherwise_break_on(alias):
    """`export ANTHROPIC_MODEL=qwen3.8 27b Mythos` sets the variable to
    `qwen3.8` and answers `export: '27b': not a valid identifier` (measured
    2026-09-14). The value is one word to the shell or the block is not
    paste-ready, which is the only thing it is for."""
    (block,) = [row for row in client_config_lines(9997, "k3y", [_entry(alias)])
                if row["client"] == "claude_code"]

    assert f"export ANTHROPIC_MODEL={shlex.quote(alias)}" in block["text"]
    # A value that needs no quoting keeps none: the ordinary line is unchanged.
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:9997\n" in block["text"]
    assert "export ANTHROPIC_API_KEY=k3y\n" in block["text"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell on this machine")
@pytest.mark.parametrize("alias", [SPACED, QUOTED])
def test_the_shell_block_round_trips_through_a_real_shell(alias):
    """Pasted, not parsed: the block is run by `sh` and the variable read back."""
    (block,) = [row for row in client_config_lines(9997, "k3y", [_entry(alias)])
                if row["client"] == "claude_code"]

    done = subprocess.run(
        ["sh", "-c", block["text"] + '\nprintf %s "$ANTHROPIC_MODEL:$ANTHROPIC_API_KEY"'],
        capture_output=True, text=True,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{alias}:k3y"
    assert done.stderr == "", "a shell that set the variables says nothing"


PEER_ROWS = [
    {"alias": "mythos", "model": "qwen3.8-27b-mythos", "type": "llamacpp_server"},
    {"alias": "ears", "model": "whisper-large-v3-turbo", "type": "local_whisper"},
]
PEER_ID = f"remote:{PEER}:mythos"


def _with_a_proved_peer(tmp_path, *, serving_local=(LOCAL,), compute_enabled=True):
    """This node's own aliases, and one peer connected over direct TLS whose
    `PROVIDERS_RESPONSE` rows are in `peer_metadata` — the two halves of the
    menu."""
    firewall = _rules(tmp_path, compute={"enabled": compute_enabled,
                                         "serving_local": list(serving_local)})
    service = _service(tmp_path, firewall,
                       peers={PEER: SimpleNamespace(connection_type="direct_tls")})
    service.peer_metadata = {PEER: {"name": "the Linux node", "providers": PEER_ROWS}}
    return service


def _offered(lines):
    """The model ids the three configurable blocks would put in a client's
    model field, read back out of the rendered text."""
    blocks = {row["client"]: row["text"] for row in lines}
    continued = [model["model"] for model in json.loads(blocks["continue"])["models"]]
    (cursor,) = [line[len("Model: "):] for line in blocks["cursor"].splitlines()
                 if line.startswith("Model: ")]
    (export,) = [line for line in blocks["claude_code"].splitlines()
                 if line.startswith("export ANTHROPIC_MODEL=")]
    claude_code = shlex.split(export.split("        #")[0])[1].split("=", 1)[1]
    return continued, cursor, claude_code


@pytest.mark.asyncio
async def test_every_id_the_blocks_offer_is_an_id_v1_models_would_list(tmp_path):
    """The invariant, asserted against the running door rather than assumed:
    the blocks and `/v1/models` are one menu, so a pasted configuration cannot
    name a model this gateway would refuse."""
    import aiohttp

    service = _with_a_proved_peer(tmp_path)
    server = GatewayServer(service, host=GATEWAY_HOST, port=0, key_path=tmp_path / GATEWAY_KEY_NAME)
    service.gateway = server
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://{GATEWAY_HOST}:{server.port}/v1/models",
                headers={"Authorization": f"Bearer {server._key}"},
            ) as response:
                served = [row["id"] for row in (await response.json())["data"]]
        answer = await service.get_gateway_client_lines()
    finally:
        await server.stop()

    continued, cursor, claude_code = _offered(answer["lines"])
    assert served == [LOCAL, PEER_ID], "the peer's chat alias is on the menu, its whisper row is not"
    assert continued == served, "Continue offers the menu, in the menu's order"
    assert cursor in served and claude_code in served
    assert [entry["id"] for entry in answer["menu"]] == served


@pytest.mark.asyncio
async def test_a_node_serving_nothing_of_its_own_offers_its_peers_models(tmp_path):
    """The card this closes: with `compute.enabled` false the door still
    carries the peer's models — that flag is about what this node gives — and
    the blocks used to read `<alias>` while `/v1/models` answered with them."""
    service = _with_a_proved_peer(tmp_path, serving_local=(), compute_enabled=False)

    answer = await service.get_gateway_client_lines()

    continued, cursor, claude_code = _offered(answer["lines"])
    assert continued == [PEER_ID] and cursor == claude_code == PEER_ID
    assert answer["selected_id"] == PEER_ID
    assert "<alias>" not in json.dumps(answer["lines"])
    assert "DPC mythos (the Linux node)" in answer["lines"][0]["text"], (
        "a dropdown and a title take the label, not the sixty-character id"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asked,expected", [
    (None, LOCAL),
    (PEER_ID, PEER_ID),
    ("remote:" + STRANGER + ":gone", LOCAL),
])
async def test_the_blocks_are_rendered_for_the_entry_the_owner_chose(tmp_path, asked, expected):
    """One value stands where a client names a single model, and which one is
    a choice: the owner's while the menu still carries it, this node's first
    local row otherwise."""
    service = _with_a_proved_peer(tmp_path)

    answer = await service.get_gateway_client_lines(selected_id=asked)

    _, cursor, claude_code = _offered(answer["lines"])
    assert answer["selected_id"] == cursor == claude_code == expected


@pytest.mark.asyncio
async def test_the_single_model_blocks_say_which_entry_they_named(tmp_path):
    """`names[0]` was an iteration order and the block said nothing about it;
    a guest reading the paste now learns what it got and that there is more."""
    service = _with_a_proved_peer(tmp_path)

    answer = await service.get_gateway_client_lines(selected_id=PEER_ID)

    blocks = {row["client"]: row["text"] for row in answer["lines"]}
    for text in (blocks["cursor"], blocks["claude_code"]):
        assert "mythos (the Linux node), one of the 2 models /v1/models lists" in text


def test_the_menu_carries_what_a_dropdown_needs_of_each_half(tmp_path):
    """`label` is short, `id` is what a client is configured with, and a
    peer's row says whose it is."""
    from dpc_client_core.gateway import Gateway

    menu = [entry.as_dict() for entry in Gateway(_with_a_proved_peer(tmp_path)).chat_menu()]

    assert menu == [
        {"id": LOCAL, "owner": "local", "alias": LOCAL, "label": LOCAL,
         "peer_id": None, "peer_name": None, "context_window": None},
        {"id": PEER_ID, "owner": "peer", "alias": "mythos",
         "label": "mythos (the Linux node)", "peer_id": PEER, "peer_name": "the Linux node",
         "context_window": None},
    ]


# --- the window the menu and the Claude Code block carry ----------------------

LOCAL_WINDOW = 215040  # this node's own alias, as configured
PEER_WINDOW = 131072


def _with_windows(tmp_path, *, local=LOCAL_WINDOW, peer=PEER_WINDOW):
    """The two halves of the menu with a window stated on each: this node
    resolves its own through `lookup_context_window`, the peer states its own
    on the row it sent."""
    service = _with_a_proved_peer(tmp_path)
    service.llm_manager.lookup_context_window = lambda model: local
    service.peer_metadata = {PEER: {"name": "the Linux node", "providers": [
        {**row, **({"context_window": peer} if peer is not None else {})}
        for row in PEER_ROWS
    ]}}
    return service


def _exported_window(lines):
    """`CLAUDE_CODE_MAX_CONTEXT_TOKENS` as the block exports it, or None when
    the block carries no such line at all."""
    (block,) = [row["text"] for row in lines if row["client"] == "claude_code"]
    exported = [line for line in block.splitlines()
                if line.startswith("export CLAUDE_CODE_MAX_CONTEXT_TOKENS")]
    if not exported:
        return None
    (name, value), = [shlex.split(line)[1].split("=", 1) for line in exported]
    assert name == "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
    return value


@pytest.mark.asyncio
async def test_the_menu_says_how_wide_each_entry_is_and_says_null_for_what_it_cannot_say(tmp_path):
    """A client that is not told the window guesses it: Claude Code announces
    it keeps the session within 200k for a model name it does not know, while
    this node's alias is 215040. Both halves of the menu carry the number, and
    the key is present and null where nobody knows it, so the UI can tell that
    apart from a backend too old to send it."""
    known = await _with_windows(tmp_path).get_gateway_client_lines()
    unknown = await _with_windows(tmp_path, local=None, peer=None).get_gateway_client_lines()

    assert [(entry["id"], entry["context_window"]) for entry in known["menu"]] == [
        (LOCAL, LOCAL_WINDOW), (PEER_ID, PEER_WINDOW),
    ]
    for entry in unknown["menu"]:
        assert "context_window" in entry, "absent reads as «too old to send it»"
        assert entry["context_window"] is None


@pytest.mark.asyncio
async def test_the_claude_code_block_exports_the_window_and_never_guesses_one(tmp_path):
    """The knob that binary reads, beside the exports it already reads — and
    no line at all where the window is unknown, because the guess it would
    otherwise make is its own and not ours."""
    service = _with_windows(tmp_path)

    answer = await service.get_gateway_client_lines()
    silent = await _with_windows(tmp_path, local=None, peer=None).get_gateway_client_lines()

    assert _exported_window(answer["lines"]) == str(LOCAL_WINDOW)
    assert _exported_window(silent["lines"]) is None
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in json.dumps(silent["lines"])


@pytest.mark.asyncio
@pytest.mark.parametrize("asked,expected", [(LOCAL, LOCAL_WINDOW), (PEER_ID, PEER_WINDOW)])
async def test_the_exported_window_moves_with_the_selection(tmp_path, asked, expected):
    """One entry is named in that block and the window exported under it is
    that entry's, not the menu's first — a number belonging to another model
    is worse than none."""
    service = _with_windows(tmp_path)

    answer = await service.get_gateway_client_lines(selected_id=asked)

    assert answer["selected_id"] == asked
    assert _exported_window(answer["lines"]) == str(expected)


def test_the_mask_shows_the_keys_own_head_and_tail_and_invents_no_prefix():
    """The mask stands beside the clear key in the same tab: a head this key
    does not have made the two read as two different keys
    (THE-MASKED-GATEWAY-KEY-INVENTS-AN-SK-PREFIX-THE-REAL-KEY-DOES-NOT-HAVE)."""
    key = new_gateway_key()

    masked = mask_gateway_key(key)

    assert masked == f"{key[:4]}\u2026{key[-4:]}"
    assert not masked.startswith("sk-"), "no vendor prefix is invented"
    assert key.startswith(masked[:4]) and key.endswith(masked[-4:])
    assert key not in masked and len(key) == 43, "enough to recognise, never enough to use"


@pytest.mark.parametrize("key,masked", [
    (None, None), ("", None), ("12345678", "\u2026"), ("123456789", "1234\u20266789"),
])
def test_what_the_mask_says_where_there_is_too_little_key_to_show(key, masked):
    """Eight characters or fewer would be shown whole by a head and a tail of
    four, so nothing is shown; no key at all stays None, which is the state the
    card reads as 'the door has never started'."""
    assert mask_gateway_key(key) == masked


# --- (4) get_peer_provider_menu ----------------------------------------------


def _sharing_service(tmp_path, **overrides):
    compute = {"enabled": True, "allow_nodes": [PEER], "serving_local": [LOCAL]}
    transcription = {"enabled": True, "allow_nodes": [PEER]}
    compute.update(overrides.pop("compute", {}))
    transcription.update(overrides.pop("transcription", {}))
    firewall = _rules(tmp_path, compute=compute, transcription=transcription,
                      **overrides.pop("blocks", {}))
    providers = overrides.pop("providers", {
        LOCAL: _Provider("ollama", "gemma3:27b"),
        WHISPER: _Provider("local_whisper", "whisper-large-v3-turbo"),
        "deepseek_pro": _Provider("deepseek", "deepseek-v4-pro"),
    })
    return _service(tmp_path, firewall, providers=providers, **overrides)


@pytest.mark.asyncio
async def test_the_preview_is_the_rows_the_peer_would_be_sent(tmp_path):
    service = _sharing_service(tmp_path, peers={PEER: object()})

    answer = await service.get_peer_provider_menu(PEER)

    assert answer["peer_id"] == PEER and answer["connected"] is True and answer["known"] is True
    assert answer["allowed"] is True and answer["reason"] is None
    assert [row["alias"] for row in answer["rows"]] == [LOCAL, WHISPER]
    assert "deepseek_pro" not in json.dumps(answer["rows"]), (
        "an alias this node does not serve is not named to a guest"
    )


@pytest.mark.asyncio
async def test_both_senders_of_the_menu_produce_the_same_rows(tmp_path):
    """The card this fixes: the notify path had no type branch, so a firewall
    save narrowed a connected peer's menu and dropped its transcription row."""
    from dpc_client_core.p2p_coordinator import P2PCoordinator

    service = _sharing_service(tmp_path, peers={PEER: object()})
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service._pending_providers_requests = {}
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service._notify_peers_of_provider_changes = (
        CoreService._notify_peers_of_provider_changes.__get__(service, SimpleNamespace)
    )
    coordinator = P2PCoordinator(service)

    await coordinator.handle_get_providers_request(PEER)
    on_request = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]

    await service._notify_peers_of_provider_changes()
    on_save = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]

    preview = (await service.get_peer_provider_menu(PEER))["rows"]
    assert on_request == on_save == preview
    assert [row["type"] for row in on_save] == ["ollama", "local_whisper"]


@pytest.mark.asyncio
async def test_two_served_local_aliases_reach_the_peer_on_both_senders(tmp_path):
    """Mike's call, 2026-09-18: the owner marks any number of local models as
    served over P2P, and the P2P door serves every one.

    The card this closes: a second alias added to `What I share` was listed as
    shared on the host and never appeared on a peer, because both senders
    admitted only `serving_local[0]`.
    """
    from dpc_client_core.p2p_coordinator import P2PCoordinator

    second = "bonsai"
    service = _sharing_service(
        tmp_path,
        compute={"serving_local": [LOCAL, second]},
        transcription={"enabled": False},
        providers={
            LOCAL: _Provider("ollama", "gemma3:27b"),
            second: _Provider("ollama", "bonsai:4b"),
            "deepseek_pro": _Provider("deepseek", "deepseek-v4-pro"),
        },
        peers={PEER: object()},
    )
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service._pending_providers_requests = {}
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service._notify_peers_of_provider_changes = (
        CoreService._notify_peers_of_provider_changes.__get__(service, SimpleNamespace)
    )

    await P2PCoordinator(service).handle_get_providers_request(PEER)
    on_request = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]
    await service._notify_peers_of_provider_changes()
    on_save = service.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]["providers"]

    assert [row["alias"] for row in on_request] == [LOCAL, second]
    assert [row["alias"] for row in on_save] == [LOCAL, second]
    assert "deepseek_pro" not in json.dumps(on_save), "an unlisted alias is still not named"


@pytest.mark.asyncio
async def test_a_free_peer_and_a_paying_peer_differ_only_in_the_tariff(tmp_path):
    blocks = {"node_groups": {GROUP: [PEER, STRANGER]}}
    service = _sharing_service(
        tmp_path,
        compute={"allow_nodes": [PEER, STRANGER], "free_nodes": [PEER],
                 "currency": "USD",
                 "serving_tariff": {LOCAL: [{"from": "2026-01-01", "in": 1.0, "out": 2.0}]}},
        transcription={"enabled": False},
        blocks=blocks,
    )

    free = (await service.get_peer_provider_menu(PEER))["rows"]
    paid = (await service.get_peer_provider_menu(STRANGER))["rows"]

    assert len(free) == len(paid) == 1
    assert free[0]["tariff"]["in"] == 0.0 and free[0]["tariff"]["out"] == 0.0
    assert free[0]["tariff"]["free"] is True
    assert paid[0]["tariff"]["in"] == 1.0 and paid[0]["tariff"]["out"] == 2.0
    assert paid[0]["tariff"]["free"] is False
    assert {k: v for k, v in free[0].items() if k != "tariff"} == \
           {k: v for k, v in paid[0].items() if k != "tariff"}


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_a_transcription_row_appears_exactly_when_that_permission_allows_it(tmp_path, allowed):
    service = _sharing_service(
        tmp_path, transcription={"enabled": True, "allow_nodes": [PEER] if allowed else []},
    )

    aliases = [row["alias"] for row in (await service.get_peer_provider_menu(PEER))["rows"]]

    assert (WHISPER in aliases) is allowed
    assert LOCAL in aliases, "the inference row is decided by the other permission"


@pytest.mark.asyncio
async def test_a_refused_peer_gets_an_empty_menu_and_a_reason_in_words(tmp_path):
    service = _sharing_service(tmp_path, transcription={"enabled": False})

    answer = await service.get_peer_provider_menu(STRANGER)

    assert answer["rows"] == [] and answer["allowed"] is False
    assert STRANGER in answer["reason"]
    assert "compute.allow_nodes" in answer["reason"] and "transcription" in answer["reason"]
    assert answer["connected"] is False and answer["known"] is False


@pytest.mark.asyncio
async def test_an_allowed_peer_with_nothing_designated_is_told_that_instead(tmp_path):
    """Allowed and served nothing is a different sentence from refused, and the
    owner acts on a different setting."""
    service = _sharing_service(
        tmp_path, compute={"serving_local": []}, transcription={"enabled": False},
    )

    answer = await service.get_peer_provider_menu(PEER)

    assert answer["rows"] == [] and answer["allowed"] is True
    assert "compute.serving_local" in answer["reason"]


@pytest.mark.asyncio
async def test_the_menu_command_needs_a_peer(tmp_path):
    service = _sharing_service(tmp_path)

    assert (await service.get_peer_provider_menu(""))["status"] == "error"


# --- (5) validate_firewall_rules ---------------------------------------------


@pytest.mark.asyncio
async def test_a_good_rules_object_validates_and_a_bad_one_names_its_reasons(tmp_path):
    service = _service(tmp_path, _rules(tmp_path))

    good = await service.validate_firewall_rules(
        {"compute": {"enabled": True, "allow_nodes": [PEER]}}
    )
    assert good["status"] == "success" and good["valid"] is True and good["errors"] == []

    bad = await service.validate_firewall_rules(
        {"compute": {"enabled": "yes", "allow_nodes": "not-a-list"}, "nonsense": {}}
    )
    assert bad["status"] == "success" and bad["valid"] is False
    assert any("compute.enabled" in e for e in bad["errors"])
    assert any("allow_nodes" in e for e in bad["errors"])
    assert any("nonsense" in e for e in bad["errors"])


@pytest.mark.asyncio
async def test_validating_writes_nothing(tmp_path):
    path = tmp_path / "privacy_rules.json"
    firewall = _rules(tmp_path, compute={"enabled": True})
    before = path.read_bytes()
    service = _service(tmp_path, firewall)

    await service.validate_firewall_rules({"compute": {"enabled": False}})

    assert path.read_bytes() == before
    assert firewall.compute_enabled is True


@pytest.mark.asyncio
async def test_a_string_is_refused_by_name_rather_than_answered_invalid(tmp_path):
    """The old signature: a string reached a validator that calls `.keys()`."""
    service = _service(tmp_path, _rules(tmp_path))

    answer = await service.validate_firewall_rules('{"compute": {}}')

    assert answer["status"] == "error" and "object" in answer["message"]


# --- the five are reachable --------------------------------------------------


def test_every_new_command_is_in_the_allowlist_and_on_coreservice():
    for command in ("get_gateway_state", "rotate_gateway_key", "get_gateway_client_lines",
                    "get_peer_provider_menu", "validate_firewall_rules"):
        assert command in ALLOWED_COMMANDS, f"{command} is not reachable from the UI"
        assert hasattr(CoreService, command)
