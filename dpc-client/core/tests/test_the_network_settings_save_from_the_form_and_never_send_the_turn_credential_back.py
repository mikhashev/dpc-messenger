"""The network, hub and gateway settings: saved from the form, refused when a
value is bad, and a stored TURN credential is never sent back as text
(THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI).

Ports, connection timeouts, DHT, relay, gossip, hole punching, hub, OAuth,
TURN, WebRTC and gateway keys were config-only; the audit
(audit/config_ui_parity_2026-11-27.md) lists them with no UI writer, and the
dead keys of these sections (removed from the template by 1cc35220) as the
dead-key card's. The Network and Gateway form now covers the live keys and
the start-only keys through one command pair whose writer validates every
value before writing anything, and shows p2p.listen_port read-only — the port
is bound when the backend starts and is not changed from the UI (the owner's
decision). What is asserted:

- the two new commands are allowed and are CoreService methods, delegated to
  NetworkService;
- a save of the form's payload lands in config.ini under every section it
  covers, and the getters the readers read answer the new values;
- a stored TURN credential is answered as `credential_set` /
  `fallback_credential_set` only — the credential string is absent from the
  whole serialized get reply, the save reply, the broadcast event and every
  caplog record, and an overriding DPC_TURN_CREDENTIAL environment variable
  cannot make the form claim a credential it cannot clear;
- an empty credential field keeps the stored one (never an accidental empty
  overwrite); only the explicit `clear_credential` / `clear_fallback_credential`
  removes it, and a credential set and cleared in one save is refused;
- the get reply answers p2p.listen_port but no save can write it, and
  gateway.host is refused (the gateway binds 127.0.0.1 only, ADR-041 D1);
- an invalid value (port 0, port 70000, a negative timeout, a malformed URL,
  a malformed STUN/TURN address, a seed node without a port, a bad chunk
  size) is refused with a reason naming its key and writes nothing, not even
  the valid neighbouring key sent in the same command; the neighbour alone
  still passes;
- the dead keys of these sections and the keys this form does not own ([api],
  the per-strategy timeouts, the relay/gossip enable switches) are refused as
  unknown.

Nothing here invents a live path the code does not have: the live keys are
re-read per dial, per request, per punch attempt, per reconnect check and per
new WebRTC peer — the form says so per key — and the start-only keys are
captured when the backend starts. Every home is under tmp_path; `DPC_HOME`
points there, never at `~/.dpc`. No manager, listener or peer is started.
Every credential in this file is an obviously fake test value.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS, _sanitize_payload_for_logging
from dpc_client_core.network_service import NetworkService
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

# Obviously fake, the shape of nothing a real TURN provider would accept.
CRED = "TEST-TURN-CREDENTIAL-NOT-REAL"
FALLBACK_CRED = "TEST-TURN-FALLBACK-CREDENTIAL-NOT-REAL"

# The form's payload: both groups in one command, as the form sends them.
NETWORK_FORM = {
    "p2p": {
        "listen_host": "0.0.0.0",
        "connection_timeout": 45.5,
        "auto_connect_node_groups": False,
        "auto_connect_delay": 0,
    },
    "dht": {
        "enabled": False,
        "port": 9000,
        "k": 12,
        "alpha": 2,
        "bootstrap_timeout": 60,
        "lookup_timeout": 15,
        "bucket_refresh_interval": 7200,
        "announce_interval": 1800,
        "seed_nodes": "203.0.113.7:8889, seed.example.org:8889",
    },
    "relay": {"volunteer": True, "max_peers": 5, "bandwidth_limit_mbps": 20.5},
    "gossip": {"max_hops": 7, "fanout": 4, "ttl_seconds": 3600, "sync_interval": 600},
    "connection": {
        "remote_inference_timeout": 600,
        "reconnect_slow_interval_max_seconds": 1200,
        "hole_punch_timeout": 20,
        "hello_timeout": 15,
        "link_silence_timeout": 120,
        "max_pending_hellos_per_ip": 4,
        "max_proved_connections_per_ip": 6,
        "enable_hole_punching": True,
    },
    "hole_punch": {"udp_punch_port": 8891, "dtls_handshake_timeout": 5},
    "turn": {
        "username": "turnuser",
        "credential": CRED,
        "servers": "turn:a.example.com:80, turn:b.example.com:443?transport=tcp",
        "fallback_servers": ["turn:c.example.com:80"],
        "fallback_username": "openrelay",
        "fallback_credential": FALLBACK_CRED,
    },
    "webrtc": {"stun_servers": "stun:stun.example.com:3478"},
    "hub": {"url": "https://hub.example.com/", "auto_connect": True},
    "oauth": {"callback_host": "localhost", "callback_port": 9090,
              "default_provider": "github"},
    "gateway": {"enabled": True, "port": 9996},
    "file_transfer": {
        "chunk_size": "1MiB",
        "chunk_delay": 0,
        "max_concurrent_transfers": 2,
        "verify_hash": False,
    },
}


class RecordingApi:
    """A local API that records the events a save broadcasts."""

    def __init__(self):
        self.events = []

    async def broadcast_event(self, event_name, payload):
        self.events.append((event_name, payload))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, so no test reads or writes ~/.dpc."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def host(home):
    """A CoreService-shaped host whose network service is real, with a
    recording API."""
    api = RecordingApi()
    service = NetworkService(
        settings=Settings(home),
        local_api=api,
    )
    return SimpleNamespace(network_service=service)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_network_config", "save_network_config"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_command_answers_the_keys_with_their_defaults(self, home, host):
        config = await CoreService.get_network_config(host)
        assert config["status"] == "success"
        # The read-only display key is answered like every other one.
        assert config["p2p"]["listen_port"] == 8888
        assert config["p2p"]["listen_host"] == "dual"
        assert config["p2p"]["connection_timeout"] == 30
        assert config["dht"]["port"] == 8889
        assert config["hole_punch"]["udp_punch_port"] == 8890
        assert config["hub"]["url"] == "http://localhost:8000"
        assert config["oauth"]["default_provider"] == "google"
        assert config["gateway"]["enabled"] is False
        assert config["gateway"]["port"] == 9997
        assert config["file_transfer"]["chunk_size"] == "auto"
        # The credential answers presence only.
        assert config["turn"]["credential_set"] is False
        assert config["turn"]["fallback_credential_set"] is False
        assert "credential" not in config["turn"]


class TestTheTurnCredentialIsAnsweredAsPresenceOnly:
    @pytest.mark.asyncio
    async def test_a_stored_credential_is_never_in_the_get_reply(self, home, host):
        await CoreService.save_network_config(
            host, config={"turn": {"credential": CRED,
                                   "fallback_credential": FALLBACK_CRED}})
        reply = await CoreService.get_network_config(host)
        assert reply["turn"]["credential_set"] is True
        assert reply["turn"]["fallback_credential_set"] is True
        # Not the value, not a prefix, not a length: the whole serialized
        # reply, at any depth, must not contain the credential strings.
        text = json.dumps(reply)
        assert CRED not in text and FALLBACK_CRED not in text
        assert CRED[:9] not in text and FALLBACK_CRED[:9] not in text
        # The section answers exactly presence, not a value-shaped hole.
        assert "credential" not in reply["turn"]
        assert "fallback_credential" not in reply["turn"]

    @pytest.mark.asyncio
    async def test_the_save_reply_and_the_broadcast_carry_no_credential_text(
            self, home, host):
        out = await CoreService.save_network_config(
            host, config={"turn": {"credential": CRED,
                                   "fallback_credential": FALLBACK_CRED,
                                   "servers": "turn:a.example.com:80"}})
        assert out["status"] == "success"
        assert CRED not in json.dumps(out) and FALLBACK_CRED not in json.dumps(out)
        (name, payload) = host.network_service.local_api.events[-1]
        assert name == "network_config_updated"
        text = json.dumps(payload)
        assert CRED not in text and FALLBACK_CRED not in text
        assert payload["turn"]["credential_set"] is True
        assert payload["turn"]["fallback_credential_set"] is True
        assert payload["turn"]["servers"] == "turn:a.example.com:80"

    @pytest.mark.asyncio
    async def test_no_log_record_written_by_a_save_carries_the_credential(
            self, home, host, caplog):
        with caplog.at_level(logging.DEBUG, logger="dpc_client_core"):
            out = await CoreService.save_network_config(
                host, config={"turn": {"credential": CRED,
                                       "fallback_credential": FALLBACK_CRED}})
            assert out["status"] == "success"
            # A refused save too: its reason names the key, never the value.
            refused = await CoreService.save_network_config(
                host, config={"turn": {"credential": 123}})
            assert refused["status"] == "error"
        assert CRED not in caplog.text and FALLBACK_CRED not in caplog.text

    def test_the_typed_credential_is_redacted_in_the_local_api_payload_log(self):
        """The one place a credential travels is the save command itself, and
        local_api logs every UI command's payload at DEBUG
        (test_no_secret_reaches_a_log_file.py pins that layer); this pins that
        the exact shape this form sends — config.turn.credential — is one the
        redactor catches by field name, whatever the credential's shape."""
        sanitized = _sanitize_payload_for_logging({
            "config": {"turn": {"credential": CRED,
                                "fallback_credential": FALLBACK_CRED,
                                "servers": "turn:a.example.com:80"}}})
        text = json.dumps(sanitized)
        assert CRED not in text and FALLBACK_CRED not in text
        assert sanitized["config"]["turn"]["credential"] == "<redacted>"
        assert sanitized["config"]["turn"]["fallback_credential"] == "<redacted>"
        assert sanitized["config"]["turn"]["servers"] == "turn:a.example.com:80"

    @pytest.mark.asyncio
    async def test_an_environment_credential_cannot_make_the_form_claim_one(
            self, home, host, monkeypatch):
        monkeypatch.setenv("DPC_TURN_CREDENTIAL", "TEST-ENV-CREDENTIAL-NOT-REAL")
        monkeypatch.setenv("DPC_TURN_FALLBACK_CREDENTIAL", "TEST-ENV-FALLBACK-NOT-REAL")
        reply = await CoreService.get_network_config(host)
        # Presence is read from the stored config, not through Settings.get,
        # so an overriding variable cannot fake a credential the form cannot
        # clear — and its value never reaches the reply either.
        assert reply["turn"]["credential_set"] is False
        assert reply["turn"]["fallback_credential_set"] is False
        assert "TEST-ENV-CREDENTIAL-NOT-REAL" not in json.dumps(reply)


class TestANetworkSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_network_config(
            host, config=json.loads(json.dumps(NETWORK_FORM)))
        assert out == {"status": "success",
                       "message": "Network settings saved successfully"}
        file = Settings(home)
        assert file.get("p2p", "listen_host") == "0.0.0.0"
        assert file.get("p2p", "connection_timeout") == "45.5"
        assert file.get("p2p", "auto_connect_node_groups") == "false"
        assert file.get("p2p", "auto_connect_delay") == "0"
        assert file.get("dht", "enabled") == "false"
        assert file.get("dht", "port") == "9000"
        assert file.get("dht", "seed_nodes").startswith("203.0.113.7:8889")
        assert file.get("relay", "volunteer") == "true"
        assert file.get("relay", "bandwidth_limit_mbps") == "20.5"
        assert file.get("gossip", "ttl_seconds") == "3600"
        assert file.get("connection", "remote_inference_timeout") == "600"
        assert file.get("connection", "enable_hole_punching") == "true"
        assert file.get("hole_punch", "udp_punch_port") == "8891"
        assert file.get("turn", "username") == "turnuser"
        assert file.get("turn", "credential") == CRED
        assert file.get("turn", "servers").startswith("turn:a.example.com:80")
        assert file.get("turn", "fallback_credential") == FALLBACK_CRED
        assert file.get("webrtc", "stun_servers") == "stun:stun.example.com:3478"
        assert file.get("hub", "url") == "https://hub.example.com"
        assert file.get("oauth", "default_provider") == "github"
        assert file.get("gateway", "enabled") == "true"
        assert file.get("gateway", "port") == "9996"
        assert file.get("file_transfer", "chunk_size") == "1MiB"
        assert file.get("file_transfer", "verify_hash") == "false"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_values(self, home, host):
        await CoreService.save_network_config(
            host, config=json.loads(json.dumps(NETWORK_FORM)))
        file = Settings(home)
        # The live keys, read per use by the readers named in the form's notes.
        assert file.get_p2p_connection_timeout() == 45.5
        assert file.get_remote_inference_timeout() == 600.0
        assert file.get_reconnect_slow_interval_max_seconds() == 1200.0
        assert file.get_hole_punch_dtls_handshake_timeout() == 5.0
        assert file.get_p2p_auto_connect_node_groups() is False
        assert file.get_turn_username() == "turnuser"
        assert file.get_turn_credential() == CRED
        assert file.get_turn_servers() == ["turn:a.example.com:80",
                                           "turn:b.example.com:443?transport=tcp"]
        assert file.get_turn_fallback_servers() == ["turn:c.example.com:80"]
        assert file.get_turn_fallback_username() == "openrelay"
        assert file.get_turn_fallback_credential() == FALLBACK_CRED
        assert file.get_stun_servers() == ["stun:stun.example.com:3478"]
        # The start-only keys, captured when the backend starts.
        assert file.get_dht_enabled() is False
        assert file.get_dht_port() == 9000
        assert file.get_dht_seed_nodes() == [("203.0.113.7", 8889),
                                             ("seed.example.org", 8889)]
        assert file.get_relay_volunteer() is True
        assert file.get_relay_bandwidth_limit() == 20.5
        assert file.get_gossip_max_hops() == 7
        assert file.get_enable_hole_punching() is True
        assert file.get_hole_punch_port() == 8891
        assert file.get_hub_url() == "https://hub.example.com"
        assert file.get_hub_auto_connect() is True
        assert file.get_oauth_callback_host() == "localhost"
        assert file.get_oauth_callback_port() == 9090
        assert file.get_oauth_default_provider() == "github"
        assert file.get_gateway_enabled() is True
        assert file.get_gateway_port() == 9996

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_network_config(
            host, config={"p2p": {"connection_timeout": 45}})
        file = Settings(home)
        assert file.get("p2p", "connection_timeout") == "45"
        assert file.get("p2p", "listen_host") == "dual", "untouched"
        assert file.get("p2p", "listen_port") == "8888", "untouched"
        assert file.get("dht", "port") == "8889", "untouched"
        assert file.get("gateway", "enabled") == "false", "untouched"

    @pytest.mark.asyncio
    async def test_an_empty_credential_field_keeps_the_stored_one(self, home, host):
        await CoreService.save_network_config(
            host, config={"turn": {"credential": CRED,
                                   "fallback_credential": FALLBACK_CRED}})
        # A save without the keys, and a save with explicit empty strings —
        # the two ways a left-empty password field can arrive — both keep them.
        out = await CoreService.save_network_config(
            host, config={"turn": {"username": "turnuser",
                                   "credential": "",
                                   "fallback_credential": ""}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get_turn_credential() == CRED
        assert file.get_turn_fallback_credential() == FALLBACK_CRED
        reply = await CoreService.get_network_config(host)
        assert reply["turn"]["credential_set"] is True
        assert reply["turn"]["fallback_credential_set"] is True

    @pytest.mark.asyncio
    async def test_the_explicit_clear_removes_the_stored_credential(self, home, host):
        await CoreService.save_network_config(
            host, config={"turn": {"credential": CRED,
                                   "fallback_credential": FALLBACK_CRED}})
        out = await CoreService.save_network_config(
            host, config={"turn": {"clear_credential": True,
                                   "clear_fallback_credential": True}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get_turn_credential() is None
        assert file.get_turn_fallback_credential() is None
        reply = await CoreService.get_network_config(host)
        assert reply["turn"]["credential_set"] is False
        assert reply["turn"]["fallback_credential_set"] is False

    @pytest.mark.asyncio
    async def test_a_credential_set_and_cleared_in_one_save_is_refused(
            self, home, host):
        Settings(home)  # the seeds are written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_network_config(
            host, config={"turn": {"credential": CRED, "clear_credential": True}})
        assert out["status"] == "error" and "turn.credential" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"dht": {"port": 0}}, "dht.port"),
        ({"dht": {"port": 70000}}, "dht.port"),
        ({"gateway": {"port": True}}, "gateway.port"),
        ({"hole_punch": {"udp_punch_port": "x"}}, "hole_punch.udp_punch_port"),
        ({"oauth": {"callback_port": 0}}, "oauth.callback_port"),
        ({"p2p": {"connection_timeout": -1}}, "p2p.connection_timeout"),
        ({"p2p": {"connection_timeout": 0}}, "p2p.connection_timeout"),
        ({"connection": {"remote_inference_timeout": "soon"}}, "connection.remote_inference_timeout"),
        ({"connection": {"reconnect_slow_interval_max_seconds": None}}, "connection.reconnect_slow_interval_max_seconds"),
        ({"hole_punch": {"dtls_handshake_timeout": -0.5}}, "hole_punch.dtls_handshake_timeout"),
        ({"p2p": {"auto_connect_node_groups": "no"}}, "p2p.auto_connect_node_groups"),
        ({"p2p": {"listen_host": "1.2.3.4"}}, "p2p.listen_host"),
        ({"hub": {"url": "not a url"}}, "hub.url"),
        ({"hub": {"url": "ftp://hub.example.com"}}, "hub.url"),
        ({"oauth": {"default_provider": "microsoft"}}, "oauth.default_provider"),
        ({"oauth": {"callback_host": "two words"}}, "oauth.callback_host"),
        ({"webrtc": {"stun_servers": "http://stun.example.com"}}, "webrtc.stun_servers"),
        ({"turn": {"servers": "turn:host:99999"}}, "turn.servers"),
        ({"turn": {"fallback_servers": 5}}, "turn.fallback_servers"),
        ({"dht": {"seed_nodes": "nohost"}}, "dht.seed_nodes"),
        ({"dht": {"seed_nodes": "host:notaport"}}, "dht.seed_nodes"),
        ({"file_transfer": {"chunk_size": "big"}}, "file_transfer.chunk_size"),
        ({"file_transfer": {"max_concurrent_transfers": 0}}, "file_transfer.max_concurrent_transfers"),
        ({"relay": {"max_peers": 0}}, "relay.max_peers"),
        ({"gossip": {"fanout": 1.5}}, "gossip.fanout"),
        ({"p2p": {"auto_connect_delay": -1}}, "p2p.auto_connect_delay"),
    ])
    async def test_an_invalid_value_is_refused_with_its_key_and_writes_nothing(
            self, home, host, config, key):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_network_config(host, config=config)
        assert out["status"] == "error" and key in out["message"], out
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_valid_neighbour_is_not_written_on_refusal_then_passes_alone(
            self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_network_config(
            host, config={"p2p": {"connection_timeout": 45},
                          "dht": {"port": 0, "k": 25},
                          "turn": {"credential": CRED}})
        assert out["status"] == "error" and "dht.port" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring keys were not written either"
        file = Settings(home)
        assert file.get("p2p", "connection_timeout") == "30", "the refused payload's neighbour"
        assert file.get("dht", "k") == "20", "ditto"
        assert file.get("turn", "credential") == "", "ditto"
        # Each neighbour alone passes, and the refused key still holds its stored value.
        out = await CoreService.save_network_config(
            host, config={"p2p": {"connection_timeout": 45},
                          "dht": {"k": 25},
                          "turn": {"credential": CRED}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("p2p", "connection_timeout") == "45"
        assert file.get("dht", "k") == "25"
        assert file.get("turn", "credential") == CRED
        assert file.get("dht", "port") == "8889", "the refused value"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, named", [
        # The P2P port is displayed read-only and has no writer path.
        ({"p2p": {"listen_port": 9000}}, "listen_port"),
        # The gateway binds 127.0.0.1 only (ADR-041 D1): not a setting.
        ({"gateway": {"host": "0.0.0.0"}}, "host"),
        # Read by FileTransferManager into attributes nothing then reads: a control
        # would promise routing that does not exist.
        ({"file_transfer": {"background_threshold_mb": 200}}, "background_threshold_mb"),
        ({"file_transfer": {"direct_tls_only_threshold_mb": 200}}, "direct_tls_only_threshold_mb"),
        # The dead keys of these sections (1cc35220).
        ({"connection": {"enable_ipv6": False}}, "enable_ipv6"),
        ({"connection": {"enable_gossip": False}}, "enable_gossip"),
        ({"hole_punch": {"nat_detection_enabled": False}}, "nat_detection_enabled"),
        ({"hole_punch": {"punch_attempts": 5}}, "punch_attempts"),
        ({"relay": {"prefer_region": "eu"}}, "prefer_region"),
        ({"relay": {"cache_timeout": 300}}, "cache_timeout"),
        ({"gossip": {"cleanup_interval": 600}}, "cleanup_interval"),
        ({"file_transfer": {"preparation_progress_interval_mb": 100}},
         "preparation_progress_interval_mb"),
        # Keys another card owns or this form does not cover.
        ({"api": {"port": 10000}}, "api"),
        ({"connection": {"ipv6_timeout": 90}}, "ipv6_timeout"),
        ({"relay": {"enabled": False}}, "enabled"),
        ({"gossip": {"enabled": False}}, "enabled"),
        ({"voice": {"enabled": True}}, "voice"),
        ({"p2p": "off"}, "p2p"),
    ])
    async def test_a_key_the_form_does_not_own_is_refused_as_unknown(
            self, home, host, config, named):
        out = await CoreService.save_network_config(host, config=config)
        assert out["status"] == "error" and named in out["message"]
        assert not (home / "agents").exists(), "nothing but config.ini was touched"

    @pytest.mark.asyncio
    async def test_a_save_broadcasts_the_stored_shape(self, home, host):
        await CoreService.save_network_config(
            host, config={"webrtc": {"stun_servers": "stun:stun.example.com:3478"}})
        (name, payload) = host.network_service.local_api.events[-1]
        assert name == "network_config_updated"
        assert payload["webrtc"]["stun_servers"] == "stun:stun.example.com:3478"
        assert payload["p2p"]["listen_port"] == 8888, "the read-only key rides along"
        assert payload["turn"]["credential_set"] is False
        assert "credential" not in payload["turn"]
