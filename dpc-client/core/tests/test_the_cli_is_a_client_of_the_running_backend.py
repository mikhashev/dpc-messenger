"""`python -m dpc_client_core.cli` talks to a running backend over the local API and says
plainly what happened. Every test runs against a fake local API on an ephemeral port in a
thread, with a temporary home; the live backend's ports are never touched.
"""
import asyncio
import json
import threading

import pytest
import websockets
from websockets.asyncio.server import serve

from dpc_client_core import cli

TOKEN = "t0ken"
URI = "dpc://203.0.113.5:8888?node_id=dpc-node-abc"
NODE = "dpc-node-abc"


class FakeBackend:
    """Answers `{"id","command","payload"}` frames from a table of command -> payload."""

    def __init__(self, answers, token=TOKEN):
        self.answers = answers
        self.token = token
        self.received = []  # (command, payload), auth included, in arrival order
        self.port = None

    async def _handler(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append((msg["command"], msg.get("payload")))
            if msg["command"] == "auth":
                if msg.get("token") != self.token:
                    await ws.close(code=1008, reason="invalid token")
                    return
                await ws.send(json.dumps({"id": msg["id"], "command": "auth",
                                          "status": "OK", "payload": {}}))
                continue
            await ws.send(json.dumps({"command": "some_event", "payload": {}}))  # noise first
            answer = self.answers.get(msg["command"])
            if answer is None:
                answer = ("ERROR", {"message": f"Unknown or non-async command: {msg['command']}"})
            status, payload = answer
            await ws.send(json.dumps({"id": msg["id"], "command": msg["command"],
                                      "status": status, "payload": payload}))

    def __enter__(self):
        ready = threading.Event()
        self._stop = None

        def run():
            async def main():
                self._stop = asyncio.Event()
                async with serve(self._handler, "127.0.0.1", 0) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await self._stop.wait()
            self._loop = asyncio.new_event_loop()
            self._loop.run_until_complete(main())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10)
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)

    @property
    def commands(self):
        return [c for c, _ in self.received]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("DPC_API_HOST", raising=False)
    monkeypatch.delenv("DPC_DHT_SEED_NODES", raising=False)
    (tmp_path / ".ws_token").write_text(TOKEN, encoding="utf-8")
    return tmp_path


def run(argv, home, backend=None, monkeypatch=None):
    if backend is not None:
        monkeypatch.setenv("DPC_API_PORT", str(backend.port))
    return cli.main(argv, home=home)


def peer_status(strategy):
    return ("OK", {"peer_info": [{"node_id": NODE, "strategy_used": strategy,
                                  "is_connected": True}]})


PORT_OPEN = ("OK", {"success": True, "message": "open", "host": "203.0.113.5", "port": 8888,
                    "node_id": NODE})


def test_no_token_file_says_the_backend_is_not_running(tmp_path, capsys):
    assert cli.main(["gateway", "state"], home=tmp_path) == 1
    err = capsys.readouterr().err
    assert "not running" in err and "uv run python run_service.py" in err
    assert "Traceback" not in err


def test_a_backend_that_does_not_answer_gets_the_start_command(home, monkeypatch, capsys):
    monkeypatch.setenv("DPC_API_PORT", "1")  # nothing listens on port 1
    assert cli.main(["gateway", "state"], home=home) == 1
    err = capsys.readouterr().err
    assert "does not answer" in err and "uv run python run_service.py" in err


def test_a_refused_token_is_named(home, monkeypatch, capsys):
    with FakeBackend({}, token="another") as backend:
        assert run(["gateway", "state"], home, backend, monkeypatch) == 1
    assert "refused the token" in capsys.readouterr().err


def test_auth_is_the_first_frame_and_carries_the_token(home, monkeypatch):
    state = ("OK", {"status": "success", "enabled": True, "running": True, "port": 9997,
                    "bind": "127.0.0.1", "key_masked": "dpc-gw-...abcd", "key_file": "k",
                    "serving_local": ["m"], "serving_vendor": []})
    with FakeBackend({"get_gateway_state": state}) as backend:
        assert run(["gateway", "state"], home, backend, monkeypatch) == 0
    assert backend.received[0] == ("auth", None)
    assert backend.commands == ["auth", "get_gateway_state"]


def test_a_bad_uri_is_named_before_the_backend_is_asked(home, capsys):
    assert cli.main(["connect", "dpc://203.0.113.5:8888"], home=home) == 1  # no node_id
    assert "Bad URI" in capsys.readouterr().err
    assert cli.main(["connect", "http://example.test"], home=home) == 1
    assert "neither a dpc:// URI nor a node id" in capsys.readouterr().err


def test_an_unreachable_port_stops_before_connect(home, monkeypatch, capsys):
    closed = ("OK", {"success": False, "message": "refused", "host": "203.0.113.5", "port": 8888,
                     "node_id": NODE})
    with FakeBackend({"test_port": closed}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "unreachable" in out and "Not connecting" in out
    assert "connect_to_peer" not in backend.commands


def test_a_direct_tls_peer_is_reported_as_proved(home, monkeypatch, capsys):
    with FakeBackend({"test_port": PORT_OPEN, "connect_to_peer": ("OK", None),
                      "get_status": peer_status("ipv4_direct")}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "port check 203.0.113.5:8888: open" in out
    assert "connected." in out and "direct TLS: yes" in out
    assert backend.commands == ["auth", "test_port", "connect_to_peer", "get_status"]
    assert backend.received[2][1] == {"uri": URI}


def test_a_blocked_peer_is_its_own_outcome_not_a_network_failure(home, monkeypatch, capsys):
    blocked = ("OK", {"status": "error", "message": f"{NODE} is blocked. Unblock it first."})
    with FakeBackend({"test_port": PORT_OPEN, "connect_to_peer": blocked}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "blocked" in out and "unblock it first" in out
    assert "get_status" not in backend.commands


def test_a_peer_not_on_direct_tls_gets_the_warning(home, monkeypatch, capsys):
    with FakeBackend({"test_port": PORT_OPEN, "connect_to_peer": ("OK", None),
                      "get_status": peer_status("hub_webrtc")}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "transport: hub_webrtc" in out
    assert "direct TLS: no" in out and "inference through this peer will not work" in out


def test_the_status_rows_peer_proved_decides_over_the_strategy_name(home, monkeypatch, capsys):
    unproved = ("OK", {"peer_info": [{"node_id": NODE, "strategy_used": "ipv4_direct",
                                      "connection_type": "relayed", "peer_proved": False,
                                      "is_connected": True}]})
    with FakeBackend({"test_port": PORT_OPEN, "connect_to_peer": ("OK", None),
                      "get_status": unproved}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "transport: ipv4_direct (relayed)" in out and "direct TLS: no" in out


def test_test_port_is_open_to_the_local_api():
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    assert "test_port" in ALLOWED_COMMANDS


def test_a_backend_without_test_port_still_connects_and_says_the_check_was_skipped(
        home, monkeypatch, capsys):
    with FakeBackend({"connect_to_peer": ("OK", None),
                      "get_status": peer_status("ipv6_direct")}) as backend:
        assert run(["connect", URI], home, backend, monkeypatch) == 0
    assert "port check: skipped" in capsys.readouterr().out


def test_a_node_id_is_searched_by_dht_and_an_empty_seed_list_is_said_aloud(
        home, monkeypatch, capsys):
    found = ("OK", {"status": "success", "method": "ipv4_direct", "message": "Connected"})
    with FakeBackend({"connect_via_dht": found,
                      "get_status": peer_status("ipv4_direct")}) as backend:
        assert run(["connect", NODE], home, backend, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "DHT seed nodes: none" in out
    assert "dpc:// URI" in out and "seed_nodes is empty" in out
    assert backend.received[1] == ("connect_via_dht", {"node_id": NODE})


def test_a_failed_dht_search_exits_non_zero(home, monkeypatch, capsys):
    lost = ("OK", {"status": "error", "method": None, "message": "peer not found"})
    with FakeBackend({"connect_via_dht": lost}) as backend:
        assert run(["connect", NODE], home, backend, monkeypatch) == 1
    assert "peer not found" in capsys.readouterr().out


def test_rotate_without_yes_refuses_and_never_asks_the_backend(home, capsys):
    assert cli.main(["gateway", "rotate"], home=home) == 2  # not even a backend is needed
    out = capsys.readouterr().out
    assert "--yes" in out and "401" in out


def test_rotate_with_yes_prints_the_new_key(home, monkeypatch, capsys):
    rotated = ("OK", {"status": "success", "key": "dpc-gw-NEWKEY", "key_masked": "dpc-gw-...",
                      "key_file": "k"})
    with FakeBackend({"rotate_gateway_key": rotated}) as backend:
        assert run(["gateway", "rotate", "--yes"], home, backend, monkeypatch) == 0
    assert "dpc-gw-NEWKEY" in capsys.readouterr().out


def test_gateway_key_with_no_key_file_says_so_and_creates_nothing(tmp_path, capsys):
    assert cli.main(["gateway", "key"], home=tmp_path) == 1
    assert "No gateway key" in capsys.readouterr().err
    assert not (tmp_path / ".gateway_key").exists()


def test_gateway_key_prints_the_file_in_clear(tmp_path, capsys):
    (tmp_path / ".gateway_key").write_text("dpc-gw-SECRET\n", encoding="utf-8")
    assert cli.main(["gateway", "key"], home=tmp_path) == 0
    assert capsys.readouterr().out.strip() == "dpc-gw-SECRET"


def test_gateway_lines_print_each_client_block(home, monkeypatch, capsys):
    lines = ("OK", {"status": "success", "selected_id": "m1", "menu": [],
                    "lines": [{"client": "curl", "text": "curl http://127.0.0.1:9997/v1/models"},
                              {"client": "claude_code_settings", "text": "{}", "note": "launch"}]})
    with FakeBackend({"get_gateway_client_lines": lines}) as backend:
        assert run(["gateway", "lines", "--model", "m1"], home, backend, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "# curl" in out and "# claude_code_settings - launch" in out
    assert backend.received[1] == ("get_gateway_client_lines", {"selected_id": "m1"})


def test_gateway_state_off_says_how_to_turn_it_on(home, monkeypatch, capsys):
    off = ("OK", {"status": "success", "enabled": False, "running": False, "port": 9997,
                  "bind": "127.0.0.1", "key_masked": None, "key_file": "k",
                  "serving_local": [], "serving_vendor": []})
    with FakeBackend({"get_gateway_state": off}) as backend:
        assert run(["gateway", "state"], home, backend, monkeypatch) == 0
    assert "gateway is off" in capsys.readouterr().out
