"""The backend's command-line client: `uv run python -m dpc_client_core.cli ...`.

It is a client of a backend that is already running on this machine, not a second
backend. It reads the local API token from `~/.dpc/.ws_token`, opens the local API
WebSocket, sends `auth` first and then ordinary `{"id", "command", "payload"}` frames,
the way the Tauri UI and `cc_agent_bridge.py` do. Nothing here touches a peer or the
gateway directly; every verb is one or two existing local API commands.

    connect <dpc://host:port?node_id=...>   test_port, connect_to_peer, then which transport
    connect <dpc-node-...>                  connect_via_dht, then which transport
    gateway state | key | rotate --yes | lines [--model ID]

The home is `Path.home() / ".dpc"` because that is where the backend writes
`.ws_token` (local_api.py `WS_TOKEN_PATH`); the API host and port come from
`[api]` in `config.ini` or `DPC_API_HOST` / `DPC_API_PORT`, as `Settings.get` reads them.
"""
import argparse
import asyncio
import configparser
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

START_COMMAND = "uv run python run_service.py"
AUTH_TIMEOUT_S = 5.0
FAST_TIMEOUT_S = 30.0
PORT_TEST_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 120.0
GATEWAY_KEY_NAME = ".gateway_key"  # gateway.py GATEWAY_KEY_NAME
DIRECT_TLS_STRATEGIES = ("ipv4_direct", "ipv6_direct")
# `get_status` rows carry `peer_proved` (p2p_manager.peer_proof). A backend from before
# 2026-11-01 sends only `strategy_used`; there the two direct strategies are the ones that
# hand back a p2p_manager.PeerConnection, whose type is "direct_tls".


class CliError(Exception):
    """An outcome to print plainly and exit non-zero on, never a stack trace."""


def dpc_home() -> Path:
    return Path.home() / ".dpc"


def _setting(home: Path, section: str, key: str, fallback: str) -> str:
    env = os.environ.get(f"DPC_{section.upper()}_{key.upper()}")
    if env is not None:
        return env
    config = configparser.ConfigParser()
    try:
        config.read(home / "config.ini", encoding="utf-8")
        return config.get(section, key)
    except (configparser.Error, OSError):
        return fallback


def api_url(home: Path) -> str:
    host = _setting(home, "api", "host", "127.0.0.1")
    port = _setting(home, "api", "port", "9999")
    return f"ws://{host}:{port}"


class Api:
    """One authenticated local API connection."""

    def __init__(self, home: Path):
        self.home = home
        self._ws = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> "Api":
        import websockets

        token_path = self.home / ".ws_token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise CliError(
                f"No local API token at {token_path}. The backend writes it when it starts, "
                f"so it is not running here. Start it from dpc-client/core: {START_COMMAND}"
            )
        url = api_url(self.home)
        try:
            self._ws = await websockets.connect(url, open_timeout=AUTH_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            raise CliError(
                f"The backend does not answer at {url}. Start it from dpc-client/core: "
                f"{START_COMMAND} (this command is a client of a running backend)"
            )
        except Exception as e:  # a server that is up but is not the local API
            raise CliError(f"Could not open {url}: {e}")
        try:
            await self._ws.send(json.dumps({"id": "cli-auth", "command": "auth", "token": token}))
            answer = json.loads(await asyncio.wait_for(self._ws.recv(), AUTH_TIMEOUT_S))
        except Exception:
            await self._close()
            raise CliError(
                f"The backend at {url} refused the token in {token_path} (stale after a "
                "restart, or from another user's home). Restart the backend and try again."
            )
        if answer.get("status") != "OK":
            await self._close()
            raise CliError(f"The backend at {url} did not accept the token: {answer}")
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def call(self, command: str, payload: Optional[dict] = None,
                   timeout: float = FAST_TIMEOUT_S) -> Dict[str, Any]:
        """Send one command and return its response frame. Events that arrive between are skipped."""
        request_id = f"cli-{next(self._ids)}"
        await self._ws.send(json.dumps({"id": request_id, "command": command,
                                        "payload": payload or {}}))

        async def _wait() -> Dict[str, Any]:
            while True:
                message = json.loads(await self._ws.recv())
                if message.get("id") == request_id:
                    return message

        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            raise CliError(f"The backend did not answer {command} within {timeout:.0f} s.")
        except Exception as e:
            raise CliError(f"The connection to the backend dropped during {command}: {e}")


def _error_text(response: Dict[str, Any]) -> str:
    payload = response.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    return str(payload)


async def _transport_report(api: Api, node_id: str, emit) -> bool:
    """Say which transport the connection to `node_id` uses; True when it is direct TLS."""
    peer = None
    for attempt in range(5):
        response = await api.call("get_status")
        if response.get("status") != "OK":
            raise CliError(f"get_status failed: {_error_text(response)}")
        for row in (response.get("payload") or {}).get("peer_info") or []:
            if row.get("node_id") == node_id:
                peer = row
                break
        if peer is not None:
            break
        if attempt < 4:
            await asyncio.sleep(0.5)
    if peer is None:
        raise CliError(f"{node_id} is not in the backend's list of connected peers, "
                       "so there is no transport to report.")
    strategy = peer.get("strategy_used")
    emit(f"transport: {strategy or 'unknown'}"
         + (f" ({peer['connection_type']})" if peer.get("connection_type") else ""))
    proved = peer.get("peer_proved")
    if proved is None:
        proved = strategy in DIRECT_TLS_STRATEGIES
    if proved:
        emit("direct TLS: yes. This is the proved connection the gateway's peer route needs.")
        return True
    emit("direct TLS: no. The gateway's peer route (ADR-041) serves a peer's models only "
         "over a proved direct TLS connection, so inference through this peer will not work.")
    return False


def _check_target(target: str) -> None:
    if target.startswith("dpc://"):
        _parse_uri(target)
    elif not target.startswith("dpc-node-"):
        raise CliError(f"'{target}' is neither a dpc:// URI nor a node id (dpc-node-...).")


def _parse_uri(uri: str):
    from dpc_protocol.utils import parse_dpc_uri
    try:
        return parse_dpc_uri(uri)
    except Exception as e:
        raise CliError(f"Bad URI: {e} Expected dpc://<host>:<port>?node_id=dpc-node-...")


async def cmd_connect(api: Api, target: str, emit) -> int:
    _check_target(target)
    if target.startswith("dpc://"):
        return await _connect_uri(api, target, emit)
    return await _connect_node_id(api, target, emit)


async def _connect_uri(api: Api, uri: str, emit) -> int:
    host, port, node_id = _parse_uri(uri)

    probe = await api.call("test_port", {"uri": uri}, PORT_TEST_TIMEOUT_S)
    if probe.get("status") == "OK" and isinstance(probe.get("payload"), dict):
        result = probe["payload"]
        ok = bool(result.get("success"))
        emit(f"port check {host}:{port}: {'open' if ok else 'unreachable'} - {result.get('message', '')}")
        if not ok:
            emit("Not connecting: nothing answers at that address. Check the host's address, "
                 "that its backend runs, and that the port is forwarded.")
            return 1
    elif "Unknown or non-async command" in _error_text(probe):
        # local_api.ALLOWED_COMMANDS does not list test_port yet; the connect below runs
        # the same preflight inside p2p_manager.connect_directly.
        emit("port check: skipped (this backend does not expose test_port over the local API)")
    else:
        raise CliError(f"test_port failed: {_error_text(probe)}")

    emit(f"connecting to {node_id} ...")
    response = await api.call("connect_to_peer", {"uri": uri}, CONNECT_TIMEOUT_S)
    if response.get("status") != "OK":
        emit(f"connect failed: {_error_text(response)}")
        return 1
    payload = response.get("payload")
    if isinstance(payload, dict) and payload.get("status") == "error":
        message = str(payload.get("message", ""))
        if "is blocked" in message:
            emit(f"blocked: {message} This peer is blocked; unblock it first "
                 "(the Unblock button, or unblock_peer), then connect again.")
        else:
            emit(f"connect failed: {message}")
        return 1
    emit("connected.")
    await _transport_report(api, node_id, emit)
    return 0


def dht_seeds(home: Path) -> List[str]:
    raw = _setting(home, "dht", "seed_nodes", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


async def _connect_node_id(api: Api, node_id: str, emit) -> int:
    seeds = dht_seeds(api.home)
    emit("searching: the DHT, the peer cache, then the other connection tiers "
         f"(DHT seed nodes: {', '.join(seeds) if seeds else 'none'})")
    if not seeds:
        emit("[dht] seed_nodes is empty, so a fresh node has no one to ask. Unless the peer "
             "cache already knows this node, the search will fail: connect with a dpc:// URI "
             "(dpc://<host>:<port>?node_id=<id>) or add a seed to [dht] seed_nodes.")
    response = await api.call("connect_via_dht", {"node_id": node_id}, CONNECT_TIMEOUT_S)
    if response.get("status") != "OK":
        emit(f"connect failed: {_error_text(response)}")
        return 1
    payload = response.get("payload") or {}
    if payload.get("status") != "success":
        message = str(payload.get("message", ""))
        if "is blocked" in message:
            emit(f"blocked: {message} This peer is blocked; unblock it first "
                 "(the Unblock button, or unblock_peer), then connect again.")
        else:
            emit(f"connect failed: {message or payload}")
        return 1
    emit(f"connected via {payload.get('method') or 'unknown'}.")
    await _transport_report(api, node_id, emit)
    return 0


async def cmd_gateway(args, home: Path, emit) -> int:
    action = args.gateway_action
    if action == "key":
        # Read only, as gateway.py's _load_or_create_key reads it, but never creating one.
        path = home / GATEWAY_KEY_NAME
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise CliError(f"No gateway key at {path}. The gateway has not started yet: set "
                           "[gateway] enabled = true in config.ini and start the backend once.")
        if not key:
            raise CliError(f"The gateway key file {path} is empty.")
        emit(key)
        return 0
    if action == "rotate" and not args.yes:
        emit("Refusing to rotate without --yes. Rotating the gateway key makes the old one "
             "401 from the next request on: every IDE or tool configured with it stops working "
             "until you paste the new key. Run again with --yes to rotate.")
        return 2
    async with Api(home) as api:
        if action == "state":
            return _print_state(await _ok(api, "get_gateway_state"), emit)
        if action == "rotate":
            result = await _ok(api, "rotate_gateway_key")
            emit(f"new gateway key: {result.get('key')}")
            emit(f"key file: {result.get('key_file')}")
            emit("The old key no longer works; update every client that used it.")
            return 0
        payload = {"selected_id": args.model} if args.model else {}
        result = await _ok(api, "get_gateway_client_lines", payload)
        emit(f"model: {result.get('selected_id')}")
        for row in result.get("lines") or []:
            emit(f"\n# {row.get('client')}" + (f" - {row['note']}" if row.get("note") else ""))
            emit(row.get("text", ""))
        return 0


async def _ok(api: Api, command: str, payload: Optional[dict] = None) -> Dict[str, Any]:
    response = await api.call(command, payload)
    result = response.get("payload")
    if response.get("status") != "OK" or not isinstance(result, dict) \
            or result.get("status") == "error":
        raise CliError(f"{command} failed: {_error_text(response)}")
    return result


def _print_state(state: Dict[str, Any], emit) -> int:
    emit(f"enabled: {state.get('enabled')}  (restart re-reads [gateway] enabled)")
    emit(f"running: {state.get('running')}")
    emit(f"listens on: {state.get('bind')}:{state.get('port')}")
    emit(f"key: {state.get('key_masked') or '(none yet)'}")
    emit(f"key file: {state.get('key_file')}")
    emit(f"serving local: {', '.join(state.get('serving_local') or []) or '-'}")
    emit(f"serving vendor: {', '.join(state.get('serving_vendor') or []) or '-'}")
    if state.get("serving_error"):
        emit(f"serving lists refused: {state['serving_error']}")
    if not state.get("enabled"):
        emit("The gateway is off. Set [gateway] enabled = true in config.ini and restart the backend.")
    elif not state.get("running"):
        emit("The gateway is enabled but no listener holds the port; read the backend's log.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dpc_client_core.cli",
        description="Command-line client of a D-PC backend that is already running on this "
                    f"machine (start it with: {START_COMMAND}).")
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect", help="connect to a peer by dpc:// URI or node id")
    connect.add_argument("target", help="dpc://host:port?node_id=dpc-node-... or dpc-node-...")
    gateway = sub.add_parser("gateway", help="the OpenAI-compatible gateway (IDE door)")
    gsub = gateway.add_subparsers(dest="gateway_action", required=True)
    gsub.add_parser("state", help="enabled, running, port, masked key")
    gsub.add_parser("key", help="print the gateway key in clear (reads ~/.dpc/.gateway_key)")
    rotate = gsub.add_parser("rotate", help="make a new key; the old one stops working")
    rotate.add_argument("--yes", action="store_true", help="confirm: every client with the old key breaks")
    lines = gsub.add_parser("lines", help="paste-ready client configuration, key in clear")
    lines.add_argument("--model", help="menu id the single-model blocks are written for")
    return parser


async def amain(argv: Sequence[str], home: Optional[Path] = None, emit=print) -> int:
    args = build_parser().parse_args(list(argv))
    home = home or dpc_home()
    try:
        if args.command == "gateway":
            return await cmd_gateway(args, home, emit)
        _check_target(args.target)  # a malformed target is named before the backend is asked
        async with Api(home) as api:
            return await cmd_connect(api, args.target, emit)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def main(argv: Optional[Sequence[str]] = None, home: Optional[Path] = None) -> int:
    try:
        return asyncio.run(amain(sys.argv[1:] if argv is None else argv, home))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
