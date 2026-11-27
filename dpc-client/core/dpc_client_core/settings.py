# dpc-client/core/dpc_client_core/settings.py

import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Dict
from urllib.parse import urlparse
import configparser

logger = logging.getLogger(__name__)

# Folders a model files folder is never put in (the folder itself or anything under it).
# Short and explicit on purpose: it stops the obvious mistake, it is not a sandbox.
_SYSTEM_FOLDERS_POSIX = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr",
    "/System", "/Library",
)
_SYSTEM_FOLDERS_WINDOWS = (
    r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
)


def _system_folders() -> List[str]:
    """The refused folders: the static lists plus this Windows install's own variables."""
    folders = list(_SYSTEM_FOLDERS_POSIX) + list(_SYSTEM_FOLDERS_WINDOWS)
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(var)
        if value:
            folders.append(value)
    return folders


def _probe_writable(folder: Path) -> None:
    """Create and delete a temp file in `folder`; raises OSError when it cannot."""
    fd, name = tempfile.mkstemp(prefix=".dpc-write-test-", dir=folder)
    os.close(fd)
    os.unlink(name)


def _figure_text(value: float) -> str:
    """A figure as config.ini text: a whole number drops its `.0` (300.0 -> '300'), the rest
    keeps its decimals (300.5 -> '300.5'). Both read back through `float()` unchanged."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


# A [webrtc]/[turn] server entry as its readers parse it (webrtc_peer.py hands
# each to aiortc's RTCIceServer): scheme stun/turn/turns, then a host with an
# optional port and an optional ?query — no whitespace, no path.
_ICE_URL_RE = re.compile(r"^(?:stun|turn|turns):\S+$", re.IGNORECASE)
# A [file_transfer] chunk_size as its reader parses it (chunk_window.py
# _SIZE_SETTING): a bare byte count or one of the k/kib/m/mib suffixes; 'auto'
# is checked separately.
_CHUNK_SIZE_RE = re.compile(r"^\d+\s*(?:kib|mib|k|m)?$", re.IGNORECASE)


def _ice_urls_text(section: str, key: str, value: Any) -> str:
    """A comma-separated ICE server list as config.ini text, or ValueError.

    The readers (Settings.get_stun_servers/get_turn_servers/get_turn_fallback_servers)
    split on commas and hand each entry to aiortc verbatim, so the writer's job
    is only that each entry is one the peer can parse: a stun:/turn:/turns:
    scheme, a host with no spaces, and a port that is a port when one is given.
    Accepts the same shape the UI sends: one comma-separated string or a list.
    """
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{section}.{key} must be a comma-separated list of "
                         f"stun:, turn: or turns: addresses.")
    entries: List[str] = []
    for item in items:
        for entry in item.split(','):
            entry = entry.strip()
            if not entry:
                continue
            if not _ICE_URL_RE.match(entry):
                raise ValueError(f"{section}.{key} must be a comma-separated list "
                                 f"of stun:, turn: or turns: addresses.")
            host = entry.split(':', 1)[1].split('?', 1)[0]
            if ':' in host:
                port = host.rsplit(':', 1)[1]
                if not port.isdigit() or not 1 <= int(port) <= 65535:
                    raise ValueError(f"{section}.{key} must be a comma-separated list "
                                     f"of stun:, turn: or turns: addresses.")
            entries.append(entry)
    return ', '.join(entries)


def _seed_nodes_text(value: Any) -> str:
    """A [dht] seed_nodes list as config.ini text, or ValueError.

    The reader (get_dht_seed_nodes) splits on commas and takes the part after
    the last colon as the port, silently skipping an entry whose port is not a
    number — the writer refuses those instead, so nothing is saved that the
    reader would quietly drop.
    """
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(item, str) for item in items):
        raise ValueError("dht.seed_nodes must be a comma-separated list of "
                         "host:port entries.")
    entries: List[str] = []
    for item in items:
        for entry in item.split(','):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.rpartition(':')
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError("dht.seed_nodes must be a comma-separated list "
                                 "of host:port entries.")
            entries.append(entry)
    return ', '.join(entries)


def _check_model_files_dir(raw: str) -> Path:
    """The folder `raw` names, created and proved writable, or ValueError (one sentence)."""
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"The model files folder {raw!r} is not an absolute path.")
    folder = Path(os.path.normpath(expanded))
    if folder.parent == folder:
        raise ValueError(f"The model files folder {folder} is a filesystem root.")
    # Compared on the path as typed and on where it really lands (a link into /usr).
    for candidate in {folder, Path(os.path.normpath(folder.resolve()))}:
        here = Path(os.path.normcase(candidate))
        for system in _system_folders():
            root = Path(os.path.normcase(os.path.normpath(system)))
            if here == root or root in here.parents:
                raise ValueError(f"The model files folder {folder} is a system folder ({system}).")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"The model files folder {folder} cannot be created: {e.strerror or e}.") from e
    try:
        _probe_writable(folder)
    except OSError as e:
        raise ValueError(f"The model files folder {folder} is not writable: {e.strerror or e}.") from e
    return folder


# Keys a config.ini written before them gets at start, with the code's default and its comment, so
# the owner sees the value in the file; the file's value then wins, and one the owner wrote is never
# touched (the firewall's `_seed_missing_tools_into_rules` does the same for privacy_rules.json).
# ADR-052, item 7's carrier (Mike, 2026-11-23). {(section, key): (default, comment)}
_SEEDED_KEYS: Dict[tuple, tuple] = {
    ('local_transcription', 'idle_unload_s'): (
        '300', 'Unload the model (frees VRAM) after this many idle seconds; 0 = never'),
    ('local_transcription', 'queue_wait_s'): (
        '600', "How long a person's transcription may wait in line for the GPU; 0 = do not wait"),
    # ADR-052 L12 (S3d): item 9 (cpu_max_s, cpu_contention_factor) and Mike #71 (cpu_max_audio_s).
    # 0 turns the CPU route off (Mike, 2026-11-24, Q3), as 0 means never for `idle_unload_s`.
    ('local_transcription', 'cpu_max_s'): (
        '600', 'The CPU takes a transcription only when its estimate is within this many seconds; '
               '0 = never use the CPU'),
    ('local_transcription', 'cpu_max_audio_s'): (
        '300', 'Audio longer than this many seconds never goes to the CPU; it waits for the GPU; '
               '0 = never use the CPU'),
    ('local_transcription', 'cpu_contention_factor'): (
        '1.0', "The CPU's estimate is multiplied by this while the CPU computes something else "
               "(a busy Strata or llama-server, another CPU transcription)"),
    ('local_transcription', 'max_duration_seconds'): (
        '', 'Max audio duration in seconds for local Whisper; 0 = unlimited, empty = legacy max_file_size_mb'),
    ('music', 'auto_align_lyrics'): (
        'true', 'Align generated vocal takes\' known lyrics to audio with local Whisper to create timed .lrc lyrics'),
    ('music', 'default_engine'): (
        'yue2', 'Default music generation engine for new jobs (yue2 or acestep)'),
}


class Settings:
    """
    Centralized settings management for DPC Client.
    Supports environment variables and config file.
    """

    def __init__(self, dpc_home_dir: Path):
        self.dpc_home_dir = dpc_home_dir
        self.config_file = dpc_home_dir / "config.ini"
        # Support inline comments with # (common in config files)
        self._config = configparser.ConfigParser(inline_comment_prefixes=('#',))

        # Load config if it exists
        if self.config_file.exists():
            try:
                self._config.read(self.config_file)
                # Validate that config has at least one section
                if not self._config.sections():
                    print(f"Warning: Invalid config format in {self.config_file}")
                    self._recreate_config_with_backup()
            except configparser.Error as e:
                print(f"Warning: Failed to parse config file: {e}")
                self._recreate_config_with_backup()
            else:
                self._seed_missing_keys()
        else:
            self._create_default_config()

    def _seed_missing_keys(self) -> None:
        """Write each `_SEEDED_KEYS` entry the loaded config lacks, as one line put under its section
        header (the section appended when absent), so every other line and comment stays as it is.
        Nothing is written when none is missing; a file that cannot be written is left alone, and
        the getters read the same default.

        The file keeps its own line endings (CRLF or LF, read with `newline=''`; a file with any
        CRLF is written back with CRLF), so a seed never rewrites every line of it (Johnny #24).
        Both opens use the locale's default encoding, as `self._config.read` in `__init__` and
        `reload` does: an explicit UTF-8 here would read and write the file differently from them.
        The values in memory are then the file's, read back from it."""
        missing = [(section, key) for (section, key) in _SEEDED_KEYS
                   if not self._config.has_option(section, key)]
        if not missing:
            return
        named = ", ".join(f"[{section}] {key}" for section, key in missing)
        try:
            with open(self.config_file, 'r', newline='') as f:
                text = f.read()
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines()
            for section, key in missing:
                value, comment = _SEEDED_KEYS[(section, key)]
                line = f"{key} = {value}  # {comment}"
                header = next((i for i, text in enumerate(lines) if self._section_named(text) == section), None)
                if header is not None:
                    lines.insert(header + 1, line)
                elif not self._config.has_section(section):
                    lines += ["", f"[{section}]", line]
                else:  # a header this reader does not find: a second one would break the file
                    raise OSError(f"the [{section}] header was not found in the file")
            with open(self.config_file, 'w', newline='') as f:
                f.write(newline.join(lines) + newline)
        except OSError as e:
            logger.warning("config.ini: could not write %s (%s); the defaults apply unwritten", named, e)
            return
        try:
            self._config.read(self.config_file)
        except configparser.Error as e:
            logger.warning("config.ini: %s written but the file does not read back (%s)", named, e)
        for section, key in missing:
            if not self._config.has_option(section, key):
                # Not read back from the file: the default the getters use, said in the log.
                logger.warning("config.ini: [%s] %s did not read back from the file; the default applies",
                               section, key)
                if not self._config.has_section(section):
                    self._config.add_section(section)
                self._config.set(section, key, _SEEDED_KEYS[(section, key)][0])
        logger.info("config.ini: wrote %s with the default", named)

    def _section_named(self, line: str) -> Optional[str]:
        """The section a config.ini line opens, read as configparser reads it, or None."""
        found = self._config.SECTCRE.match(line.split('#', 1)[0].strip())
        return found.group('header') if found else None

    def _recreate_config_with_backup(self):
        """Recreate config file after backing up the invalid one."""
        # Backup old config
        if self.config_file.exists():
            backup_file = self.config_file.with_suffix('.ini.bak')
            try:
                import shutil
                shutil.copy(self.config_file, backup_file)
                print(f"Backed up old config to {backup_file}")
            except Exception as e:
                print(f"Warning: Could not backup config: {e}")

        # Create fresh config
        self._create_default_config()

    def _create_default_config(self):
        """Create a default config file with common settings."""
        self._config['hub'] = {
            'url': 'http://localhost:8000',
            'auto_connect': 'false'
        }

        self._config['oauth'] = {
            'callback_port': '8080',
            'callback_host': '127.0.0.1',
            'default_provider': 'google'
        }

        self._config['p2p'] = {
            'listen_port': '8888',
            'listen_host': 'dual',  # dual-stack (IPv4 + IPv6), can be "0.0.0.0" (IPv4 only) or "::" (IPv6 only)
            'connection_timeout': '30',  # Connection establishment timeout in seconds
            'auto_connect_node_groups': 'true',  # Auto-connect to firewall node group members on startup
            'auto_connect_delay': '5'  # Seconds to wait before attempting (let DHT bootstrap)
        }

        self._config['api'] = {
            'port': '9999',
            'host': '127.0.0.1'
        }

        # The OpenAI-compatible gateway (ADR-041 D1): a second loopback listener
        # for tools on this machine, off until asked for because it is a new port.
        self._config['gateway'] = {
            'enabled': 'false',  # Serve /v1/models and /v1/chat/completions to local tools (ADR-041). This node's own aliases need `compute.enabled` in privacy_rules.json too — open only when both are true — while a peer's `remote:<peer>:<alias>` needs this switch alone
            'port': '9997',  # 9998 is the file server, 9999 the local API
            'host': '127.0.0.1'  # Not configurable: any other value is refused at start (ADR-041 D1)
        }

        # Where the local engines keep their model files. Empty is the default, which
        # is <DPC home>/models: the getter substitutes it, so a config that never
        # had the key and one that has it empty read alike.
        self._config['local_engines'] = {
            'model_files_dir': '',  # Empty = ~/.dpc/models. Set from the Providers editor ("Model files folder"); must be an absolute path to a writable folder
            'swap_when_idle': 'false',  # Off until the card queue is live-checked: an idle-by-call holder was swapped out mid agent loop (ADR-044 amendment 2026-11-02)
            'swap_wait_s': '300',  # How long a swap waits for a busy holder before refusing; 0 = do not wait
            'caller_idle_s': '120'  # A peer or gateway client counts as busy this long after its last call; 0 = no window
        }

        # A fresh config ships with no TURN relay at all: empty here means the same
        # thing the getters already return for a missing key, so "the default" has one
        # answer instead of two. Relaying is opt-in by name — nobody's traffic should
        # traverse a third party's server because a default was left populated.
        # To use a relay, fill username/credential and servers with your provider's
        # values; the public examples that used to be the default are below.
        #   servers:           stun:stun.relay.metered.ca:80,turn:global.relay.metered.ca:80,
        #                      turn:global.relay.metered.ca:80?transport=tcp,
        #                      turn:global.relay.metered.ca:443,
        #                      turns:global.relay.metered.ca:443?transport=tcp
        #   fallback_servers:  turn:openrelay.metered.ca:80,turn:openrelay.metered.ca:443,
        #                      turn:openrelay.metered.ca:443?transport=tcp
        #   fallback_username / fallback_credential: openrelayproject / openrelayproject
        self._config['turn'] = {
            'username': '',  # Leave empty or set via environment variable DPC_TURN_USERNAME
            'credential': '',  # Leave empty or set via environment variable DPC_TURN_CREDENTIAL
            'servers': '',  # Your provider's TURN/STUN URLs; used only when username/credential are set
            'fallback_servers': '',  # A public relay, if you accept that your traffic passes through it
            'fallback_username': '',
            'fallback_credential': ''
        }

        self._config['webrtc'] = {
            # STUN servers for NAT traversal (discovering public IP).
            # Hostname-based entries are tried first; IP-based entries are
            # fallbacks for environments where DNS resolution is unreliable
            # (e.g., Docker with a custom DNS resolver blocking external names).
            'stun_servers': (
                'stun:stun.l.google.com:19302,'
                'stun:stun1.l.google.com:19302,'
                'stun:global.stun.twilio.com:3478,'
                'stun:stun.rtc.yandex.net:3478,'
                'stun:74.125.250.129:19302,'   # stun.l.google.com resolved IP (DNS fallback)
                'stun:74.125.250.127:19302'    # stun1.l.google.com resolved IP (DNS fallback)
            )
        }

        self._config['system'] = {
            'auto_collect_device_info': 'true',  # Automatically collect device/system info for AI context
            'collect_ai_models': 'false',        # Collect locally available AI models (opt-in for compute-sharing)
            'shell_memory_limit_mb': '8192'      # Memory ceiling for an agent command and everything it started, summed, in MB; 0 = off. Read at each command, no restart; DPC_SHELL_MEMORY_LIMIT_MB overrides it
        }

        self._config['dht'] = {
            'enabled': 'true',  # Enable DHT peer discovery
            'port': '8889',  # UDP port for DHT RPCs (TLS port + 1)
            'k': '20',  # Kademlia k parameter (nodes per bucket)
            'alpha': '3',  # Parallelism factor for iterative lookups
            'bootstrap_timeout': '30',  # Bootstrap timeout in seconds
            'lookup_timeout': '10',  # Lookup timeout in seconds
            'bucket_refresh_interval': '3600',  # Bucket refresh interval (1 hour)
            'announce_interval': '3600',  # Re-announce interval (1 hour)
            'seed_nodes': ''  # Comma-separated list of seed nodes (ip:port)
        }

        self._config['connection'] = {
            # Enable/disable individual connection strategies
            'enable_hole_punching': 'false',  # Try DHT-coordinated UDP hole punching (Priority 4) - DISABLED: lacks DTLS encryption (v0.10.0)
            # Timeouts per strategy (seconds) - increased for high-latency networks (mobile, CGNAT)
            'ipv6_timeout': '60',  # Includes 30s pre-flight check + 30s SSL handshake
            'ipv4_timeout': '60',  # Includes 30s pre-flight check + 30s SSL handshake
            'webrtc_timeout': '30',
            'hole_punch_timeout': '15',
            'relay_timeout': '20',
            'gossip_timeout': '5',  # How long to wait before falling back to gossip
            # Remote inference: the host budgets 900s for the work, so a
            # requester that gives up sooner pays for tokens it never sees
            'remote_inference_timeout': '1200',
            # Inbound handshake (ADR-041 D8): a peer that completes TLS and never
            # sends HELLO fails no HELLO, so the rate limiter alone never sees it
            'hello_timeout': '10',  # Seconds the listener waits for the TLS handshake, and again for HELLO after its challenge
            'max_pending_hellos_per_ip': '8',  # Inbound connections one address may hold before HELLO_ACK
            'max_proved_connections_per_ip': '8',  # Inbound connections past the identity check one address may hold; the next new node from it is closed before HELLO_ACK
            # A direct TLS link to a peer that advertised link_ping is ended after
            # this long with no frame from it; LINK_PING goes out after a third of it
            'link_silence_timeout': '90',  # Seconds of silence after which a direct TLS link is declared dead
            # After the five fast reconnect attempts, a kept peer is retried from
            # 60s, doubling up to this ceiling, until it answers
            'reconnect_slow_interval_max_seconds': '900'  # After five fast reconnect attempts to a peer we keep, retry from 60 s, doubling to this ceiling (±20 % jitter), until it answers, the user disconnects it or it leaves every shared group. Off when `[p2p] auto_connect_node_groups` is off
        }

        self._config['hole_punch'] = {
            'udp_punch_port': '8890',  # UDP port for hole punching
            # DTLS encryption settings (v0.10.1+)
            'dtls_handshake_timeout': '3'  # DTLS handshake timeout (seconds)
        }

        self._config['relay'] = {
            # Client mode (use relays for outbound connections)
            'enabled': 'true',  # Enable relay client mode
            # Server mode (volunteer as relay for others)
            'volunteer': 'false',  # Volunteer this node as relay (opt-in)
            'max_peers': '10',  # Max concurrent relay sessions (server mode)
            'bandwidth_limit_mbps': '10.0'  # Bandwidth limit for relaying
        }

        self._config['gossip'] = {
            'enabled': 'true',  # Enable gossip protocol
            'max_hops': '5',  # Maximum hops for message forwarding
            'fanout': '3',  # Number of random peers to forward to
            'ttl_seconds': '86400',  # Message TTL (24 hours)
            'sync_interval': '300'  # Anti-entropy sync interval (5 minutes)
        }

        self._config['knowledge'] = {
            'cultural_perspectives_enabled': 'false',  # Include cultural perspective analysis in knowledge extraction
            # Which provider extracts a conversation nobody has answered in yet.
            # Empty means the first local provider in the registry; extraction
            # refuses rather than reaching for the text default, which is a paid
            # API and would send the transcript off the machine unasked.
            'cold_fallback_provider': ''
        }

        # Background models and where they run. Each keeps its own device key, so
        # embeddings can sit on the CPU while Whisper takes the card. Written so a
        # new user can see them; until 2026-10-27 only Whisper's was.
        self._config['memory'] = {
            'embedding_device': 'auto'  # Where the embedding model (indexing, Active Recall) runs for every agent: auto, cpu, cuda or mps. auto = an agent profile's memory.embedding_device, else cuda, then mps, then cpu; any other value overrides the profiles
        }

        self._config['knowledge_graph'] = {
            'backend': 'sqlite',  # Agent graph store: sqlite or grafeo; an agent's own kg_backend in its config.json overrides it
            'gliner_device': 'auto'  # Where the GLiNER entity model runs: auto, cpu or cuda. auto = an agent's own gliner_device in its config.json, else cuda when torch sees a card; any other value overrides the agents
        }

        self._config['history'] = {
            # Off while history written before ADR-036 is still in circulation:
            # it carries no signature, and a third of what is on disk is that old.
            'reject_unsigned': 'false'  # Refuse a synced record whose signature this node cannot check (stored labelled `verification: legacy` either way)
        }

        self._config['file_transfer'] = {
            'chunk_size': 'auto',  # auto = per file, by size, tier and measured rate (64 KiB / 1 MiB / 4 MiB); a size (65537, 64KiB, 1MiB) fixes every file's, capped by the tier on every link but direct TLS; either way raised to keep the CRC list within 2^17; the legacy 65536 reads as auto (logged once)
            'chunk_delay': '0',  # Pause after each window of chunks in seconds; 0 = none; the legacy 0.001 reads as 0 (logged once), any other value is honoured
            'background_threshold_mb': '50',  # Background transfer threshold in MB
            'direct_tls_only_threshold_mb': '100',  # Direct TLS preference threshold in MB
            'max_concurrent_transfers': '3',  # Max concurrent file transfers
            'verify_hash': 'true'  # Verify file hash after transfer (SHA256)
        }

        self._config['music'] = {
            'poll_interval_s': '60',  # How often the music library re-reads its watched folders, in seconds (ADR-051; polling, no OS file events)
            'stream_port': '9996',  # The loopback port the player reads track bytes from (ADR-051 N8, 127.0.0.1 only, token-guarded). The app's CSP media-src names 9996, so another port also needs tauri.conf.json changed and the app rebuilt
            'stream_ranges_linux': 'false',  # Whether the Linux player asks the track stream for HTTP Range (true) or a plain whole-file 200 (false, default). WebKitGTK 2.50 reports no duration and refuses forward seeks on a Range source and handles the plain one; turn on only to test. Windows and macOS always use Range
            'lrclib_enabled': 'true',  # Allow the explicit "Find on LRCLIB" lookup (sends artist, title, album and duration to lrclib.net); false hides it. The automatic lyrics chain never goes online
            'default_engine': _SEEDED_KEYS[('music', 'default_engine')][0],  # Default music generation engine for new jobs (yue2 or acestep)
            'auto_align_lyrics': _SEEDED_KEYS[('music', 'auto_align_lyrics')][0],  # Align generated vocal takes' known lyrics to audio with local Whisper when available (writes synced .lrc + library lyrics)
            'acestep_binary': '',  # The acestep.cpp server DPC starts for music jobs (ADR-051 D30). Empty = <DPC home>/engines/acestep.cpp/build/ace-server(.exe), or the first one found a few folders down in that build folder
            'acestep_models_dir': '',  # The ACE-Step GGUF folder. Empty = <DPC home>/models/music/ace-step. Needs acestep-5Hz-lm-4B-Q8_0, acestep-v15-turbo-Q8_0, Qwen3-Embedding-0.6B-Q8_0 and vae-BF16 (.gguf)
            'acestep_port': '9995',  # The loopback port of the acestep.cpp server (always 127.0.0.1). 0 = a free port at each start
            'acestep_idle_stop_s': '300',  # Stop the acestep.cpp server after this many idle seconds to free the GPU card; 0 = as soon as a job ends
            'acestep_device': '',  # Passed to acestep.cpp as --device (CUDA0, Vulkan0, MTL0, CPU). Empty = the engine picks the best device
            'yue2_binary': '',  # The yue2.cpp server DPC starts for music jobs, the default engine (ADR-051 D35). Empty = <DPC home>/engines/yue2.cpp/build/yue-server(.exe), or the first one found a few folders down
            'yue2_models_dir': '',  # The YuE2 weights folder. Empty = <DPC home>/models/music/yue2. Needs serveurperso/YuE2-3B-Q8_0.gguf (or rs-tw/YuE2-3B-RS-TW-2.0x0.75-Q8_0.gguf) and serveurperso/YuE2-Vae-F32.gguf
            'yue2_backbone': 'stock',  # The backbone a start by hand loads: stock or rs-tw. A job names its own
            'yue2_port': '9994',  # The loopback port of the yue2.cpp server (always 127.0.0.1). 0 = a free port at each start
            'yue2_idle_stop_s': '300',  # Stop the yue2.cpp server after this many idle seconds to free the GPU card; 0 = as soon as a job ends
            'yue2_adapters_dir': '',  # The LoRA adapters folder (one level: .safetensors files, or folders holding one). Empty = <yue2_models_dir>/loras/ruach-voices/voices. DPC splits each into music and sound halves under <yue2_models_dir>/loras/.split
            'yue2_instrumental_adapter': '',  # The adapter an Instrumental job on YuE2 runs with (a name in the adapters folder or <yue2_models_dir>/loras, read after it). Empty = YuE2-instrumental-cot-full-loras; none = no adapter. Without it Instrumental jobs go to acestep.cpp
            'yue2_device': '',  # Passed to yue2.cpp as --device (CUDA0, Vulkan0, MTL0, CPU). Empty = the engine picks the best device
            'yue2_max_seq': '0',  # Passed as --max-seq when above 0 (KV cache size; less VRAM, shorter songs). 0 = the model context
            'agent_queue_max': '2',  # An agent's music jobs queued or running at once, read at every propose
            'turn_jobs_max': '1',  # An agent's music jobs per turn, read at every propose
            'describe_alias': 'auto',  # The provider that describes a same-style song's source (auto = music_flamingo when loaded; none = off)
            'export_dir': ''  # The MP3/Opus export root. Empty = <DPC home>/exports/music; an absolute path, never a filesystem root
        }

        self._config['conversations'] = {
            # Per-conversation storage settings (v0.21.0)
            'default_persist_p2p_history': 'false',  # Persist P2P chat history by default (false = ephemeral, synced from peer)
            'default_persist_telegram_history': 'true'  # Persist Telegram chat history by default
        }

        self._config['voice_messages'] = {
            'max_duration_seconds': '300',  # Maximum recording duration in seconds (5 minutes)
            'max_size_mb': '10',  # Maximum voice message file size in MB
            'mime_types': 'audio/webm,audio/opus,audio/ogg,audio/mp4,audio/mpeg,audio/wav',  # Supported audio formats (includes WAV for Tauri/Rust backend)
            'default_sample_rate': '48000',  # Default sample rate in Hz (48kHz for quality)
            'default_channels': '1',  # Default audio channels (1 = mono, 2 = stereo)
            'default_codec': 'opus',  # Default audio codec (opus for web compatibility)
            'auto_transcribe': 'true'  # Node-wide auto-transcribe switch; false lets Whisper be unloaded whatever a conversation's own setting says
        }

        self._config['voice_transcription'] = {
            'enabled': 'true',  # Enable auto-transcription of received voice messages (v0.13.2+)
            'sender_transcribes': 'false',  # Should sender transcribe their own voice messages
            'recipient_delay_seconds': '3',  # Wait N seconds before recipients attempt transcription (coordination)
            'timeout_seconds': '240',  # Max wait time for peer's transcription before trying locally (increased to 240s for cold model loads that take 180+s)
            # NOTE: provider_priority overrides voice_provider from providers.json
            # Provider aliases match HuggingFace model names for clarity (e.g., whisper-large-v3)
            'provider_priority': 'whisper-large-v3-turbo,whisper-medium,whisper-small,openai',  # Comma-separated provider priority (aliases from providers.json)
            'show_transcriber_name': 'false',  # Show who transcribed the message in UI
            'cache_transcriptions': 'true',  # Cache transcriptions in memory
            'fallback_to_openai': 'true'  # Fallback to OpenAI API if local Whisper unavailable
        }

        self._config['local_transcription'] = {
            'enabled': 'true',  # Enable local Whisper transcription (v0.13.1+)
            'fallback_to_openai': 'true',  # Fallback to OpenAI API if local fails
            'max_file_size_mb': '25',  # Deprecated fallback when max_duration_seconds is empty: max audio file size in MB for local transcription
            'max_duration_seconds': _SEEDED_KEYS[('local_transcription', 'max_duration_seconds')][0],  # Max audio duration in seconds for local Whisper; 0 = unlimited, empty = legacy max_file_size_mb
            'idle_unload_s': _SEEDED_KEYS[('local_transcription', 'idle_unload_s')][0],  # Unload the model (frees VRAM) after this many idle seconds; 0 = never
            'queue_wait_s': _SEEDED_KEYS[('local_transcription', 'queue_wait_s')][0],  # How long a person's transcription may wait in line for the GPU; 0 = do not wait
            'cpu_max_s': _SEEDED_KEYS[('local_transcription', 'cpu_max_s')][0],  # The CPU takes a transcription only when its estimate is within this many seconds; 0 = never use the CPU
            'cpu_max_audio_s': _SEEDED_KEYS[('local_transcription', 'cpu_max_audio_s')][0],  # Audio longer than this never goes to the CPU; 0 = never use the CPU
            'cpu_contention_factor': _SEEDED_KEYS[('local_transcription', 'cpu_contention_factor')][0]  # The CPU's estimate is multiplied by this while the CPU computes something else
        }

        self._config['vision'] = {
            'max_image_size_mb': '5'  # Maximum image size in MB (clipboard paste and uploads)
        }

        self._config['telegram'] = {
            'enabled': 'false',  # Enable Telegram bot integration (v0.14.0+)
            'bot_token': '',  # Bot token from @BotFather
            'allowed_chat_ids': '[]',  # JSON array of whitelisted chat IDs (private access)
            'use_webhook': 'false',  # Use webhook mode (true) or polling mode (false)
            'webhook_url': '',  # Public URL for webhook (production)
            'webhook_port': '8443',  # Local port for webhook server
            'owner_contact': '',  # Bot owner contact info (shown to unauthorized users)
            'access_denied_message': '',  # Custom access denied message (optional)
            'transcription_enabled': 'true',  # Auto-transcribe Telegram voice messages (uses default voice provider)
            'bridge_to_p2p': 'false',  # Forward Telegram messages to P2P peers (see NOTE below)
            'conversation_links': '{}',  # JSON map of telegram_chat_id -> conversation_id
            'last_update_id': '{}'  # Track last processed update_id per chat (JSON object)
        }
        # NOTE: bridge_to_p2p currently forwards as N separate 1:1 messages (v0.15.0).
        # Future: Will support group chat bridging (single message to DPC group).
        # See telegram_coordinator.py:_forward_to_p2p_peers() for implementation details.

        # NOTE: [dpc_agent_telegram] section removed in v0.15.0 — deprecated.
        # Agent Telegram linking is now per-agent via ~/.dpc/agents/{id}/config.json.
        # Use the main [telegram] bot (same token) and link agents via the UI.
        # See docs/DPC_AGENT_TELEGRAM.md for migration guide.

        self._config['agent_chat'] = {
            'cc_display_name': 'CC'  # The name an external agent (Claude Code, ZCode or another harness) goes by in 1:1 agent chats without --as, and in groups where this node registered no tag (mentions match it)
        }

        # Discord bot (off until enabled). The token is never pre-filled: name an
        # environment variable in bot_token_env, or paste it into bot_token.
        self._config['discord'] = {
            'enabled': 'false',  # Enable the Discord bot integration
            'bot_token_env': '',  # Name of an environment variable holding the bot token (read instead of bot_token when set)
            'bot_token': '',  # Bot token, if not taken from bot_token_env
            'guild_id': '',  # Discord server (guild) ID
            'allowed_channel_ids': '[]',  # JSON array of channel IDs the bot answers in; empty = every channel
            'ark_channel_id': '',  # Channel ID the agent posts its own messages to; empty = it posts nowhere
            'agent_id': '',  # Agent that answers on Discord; empty = the first agent in ~/.dpc/agents/
            'mirror_group_id': ''  # DPC group that mirrors Discord conversations; empty = no mirror
        }

        self._config['discord.guardrails'] = {
            'rate_limit_per_user_max': '10',  # Messages one user may send per window
            'rate_limit_per_user_window': '600',  # Per-user window in seconds
            'rate_limit_global_max': '60',  # Messages all users together may send per window
            'rate_limit_global_window': '3600',  # Global window in seconds
            'response_delay_seconds': '3',  # Pause before the bot answers
            'url_whitelist': ''  # Comma-separated domains whose links are kept in user text, others removed; empty = no filtering
        }

        self._config['discord.conversations'] = {
            'ttl_minutes': '30',  # A user's conversation resets after this many idle minutes
            'max_messages': '50'  # Messages kept per user conversation
        }

        self._config['logging'] = {
            'level': 'INFO',  # Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
            'console': 'true',  # Enable console output
            'console_level': 'INFO',  # Console log level (can differ from file)
            'file': '~/.dpc/logs/dpc-client.log',  # Log file path
            'max_bytes': '10485760',  # Max bytes per log file before rotation (10MB)
            'backup_count': '5'  # Number of backup log files to keep
        }

        # Per-module log level overrides (reduce verbosity of verbose third-party libs)
        self._config['logging.modules'] = {
            # HTTP libraries - very verbose at DEBUG
            'httpcore': 'WARNING',
            'httpx': 'WARNING',
            # Anthropic SDK - verbose request logging
            'anthropic': 'WARNING',
            # Telegram bot - verbose polling
            'telegram.ext.Application': 'INFO',
            # OpenAI SDK
            'openai': 'WARNING',
        }

        # Ensure directory exists
        self.dpc_home_dir.mkdir(parents=True, exist_ok=True)

        # Write config file
        with open(self.config_file, 'w') as f:
            f.write("# D-PC Client Configuration\n")
            f.write("# You can override these settings with environment variables:\n")
            f.write("# DPC_HUB_URL, DPC_OAUTH_CALLBACK_PORT, DPC_TURN_USERNAME, DPC_TURN_CREDENTIAL, etc.\n")
            f.write("# Inline comments are supported: key = value  # comment\n\n")
            self._config.write(f)

        print(f"[OK] Created default config at {self.config_file}")

    def get(self, section: str, key: str, fallback: Optional[str] = None) -> str:
        """
        Get a configuration value.
        Priority: Environment Variable > Config File > Fallback
        """
        # Environment variable format: DPC_SECTION_KEY (uppercase)
        env_key = f"DPC_{section.upper()}_{key.upper()}"
        env_value = os.environ.get(env_key)

        if env_value is not None:
            return env_value

        # Try config file
        try:
            return self._config.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            if fallback is not None:
                return fallback
            raise KeyError(f"Setting not found: [{section}] {key}")

    def get_hub_url(self) -> str:
        """Get the Hub URL."""
        return self.get('hub', 'url', 'http://localhost:8000')

    def get_oauth_callback_port(self) -> int:
        """Get the OAuth callback server port."""
        return int(self.get('oauth', 'callback_port', '8080'))

    def get_oauth_callback_host(self) -> str:
        """Get the OAuth callback server host."""
        return self.get('oauth', 'callback_host', '127.0.0.1')

    def get_oauth_default_provider(self) -> str:
        """Get the default OAuth provider (google or github)."""
        return self.get('oauth', 'default_provider', 'google')

    def get_p2p_listen_port(self) -> int:
        """Get the P2P server listen port."""
        return int(self.get('p2p', 'listen_port', '8888'))

    def get_p2p_listen_host(self) -> str:
        """Get the P2P server listen host."""
        # Fallback must equal what _create_default_config writes, or "the default"
        # is two different values depending on whether the key is in config.ini.
        return self.get('p2p', 'listen_host', 'dual')

    def get_p2p_connection_timeout(self) -> float:
        """Get the P2P connection establishment timeout in seconds."""
        return float(self.get('p2p', 'connection_timeout', '30'))

    def get_p2p_auto_connect_node_groups(self) -> bool:
        """Auto-connect to all node IDs in firewall node groups on startup."""
        value = self.get('p2p', 'auto_connect_node_groups', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_p2p_auto_connect_delay(self) -> float:
        """Seconds to wait before auto-connecting to node group peers (allows DHT bootstrap)."""
        return float(self.get('p2p', 'auto_connect_delay', '5'))

    def get_api_port(self) -> int:
        """Get the local API server port."""
        return int(self.get('api', 'port', '9999'))

    def get_api_host(self) -> str:
        """Get the local API server host."""
        return self.get('api', 'host', '127.0.0.1')

    def get_gateway_enabled(self) -> bool:
        """Whether the OpenAI-compatible gateway listens at all (off by default)."""
        value = self.get('gateway', 'enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_gateway_port(self) -> int:
        """Get the OpenAI-compatible gateway port."""
        return int(self.get('gateway', 'port', '9997'))

    def get_gateway_host(self) -> str:
        """The gateway's bind address as written; the gateway itself refuses
        anything but 127.0.0.1 (ADR-041 D1), so this is read, not chosen."""
        return self.get('gateway', 'host', '127.0.0.1')

    def get_hub_auto_connect(self) -> bool:
        """Check if Hub should auto-connect on startup."""
        value = self.get('hub', 'auto_connect', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_turn_username(self) -> Optional[str]:
        """Get TURN server username (from env var or config)."""
        try:
            username = self.get('turn', 'username', '')
            return username if username else None
        except KeyError:
            return None

    def get_turn_credential(self) -> Optional[str]:
        """Get TURN server credential/password (from env var or config)."""
        try:
            credential = self.get('turn', 'credential', '')
            return credential if credential else None
        except KeyError:
            return None

    def get_auto_collect_device_info(self) -> bool:
        """Check if automatic device/system info collection is enabled."""
        value = self.get('system', 'auto_collect_device_info', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_cultural_perspectives_enabled(self) -> bool:
        """Check if cultural perspective analysis is enabled in knowledge extraction."""
        value = self.get('knowledge', 'cultural_perspectives_enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_reject_unsigned_history(self) -> bool:
        """Whether a history record this node cannot check is refused.

        Two classes, not one — the key names the first and gates both: a record
        carrying no signature fields, and one signed over a `PREIMAGE_VERSION`
        this node cannot recompute.

        Off by default: records written before ADR-036 carry no signature, and
        export_history ships none for them, so refusing would strand history
        that already exists. Turn it on once those have aged out — with it off
        such a record is stored labelled `verification: legacy`, the live
        path's word for the same absence, never as a checked one.
        """
        value = self.get('history', 'reject_unsigned', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_knowledge_cold_fallback_provider(self) -> str:
        """Alias that extracts a conversation with no provenance, or '' for auto."""
        return self.get('knowledge', 'cold_fallback_provider', '').strip()

    def get_hf_offline_mode(self) -> bool:
        """Check if HuggingFace Hub offline mode is enabled (S144).

        When true, run_service.py sets HF_HUB_OFFLINE=1 BEFORE any
        huggingface_hub import so transformers / sentence-transformers /
        gliner skip ETag-refresh HEAD requests on cached models. This
        getter exists for symmetry — the env var is set in
        run_service.py at startup, not via this method, because Settings
        is loaded after the import chain that imports huggingface_hub.

        Default false: first-time installs need the network to download
        BGE-M3 / GLiNER / Whisper. Flip to true after caches are warm
        to remove the per-startup HF HEAD-request log noise.
        """
        value = self.get('hf', 'offline_mode', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_model_download_declined(self, model_name: str) -> bool:
        """Whether `model_name`'s download dialog was dismissed with "don't
        remind me" (`decline_model_download {remember: true}`).

        `[model_downloads]` holds one key per model, value `declined` — the
        only download-consent outcome this file remembers; a cancel, a
        failure or a closed dialog never write here (Ark's asking policy,
        owner-accepted 2026-09-24). Option names go through configparser's
        default lowercasing like every other key here, so callers must pass
        the same `model_name` spelling on every call — which every call site
        in this codebase does, reading it off the provider/agent config.
        """
        return self.get('model_downloads', model_name, '').strip().lower() == 'declined'

    def set_model_download_declined(self, model_name: str) -> None:
        """Persist "don't remind me" for `model_name`."""
        self.set('model_downloads', model_name, 'declined')

    def clear_model_download_declined(self, model_name: str) -> None:
        """Undo a persisted decline (`reset_model_download_decline`)."""
        if self._config.has_section('model_downloads') and self._config.has_option('model_downloads', model_name):
            self._config.remove_option('model_downloads', model_name)
            with open(self.config_file, 'w') as f:
                self._config.write(f)

    def get_kg_backend(self) -> str:
        """Knowledge graph backend selection (ADR-024 Phase 1.5).

        Returns one of "sqlite" (default, stable) or "grafeo" (Phase 1.5
        migration target). Configured via [knowledge_graph] backend in
        config.ini. Unknown values fall back to "sqlite" to preserve
        backward compatibility — Grafeo migration is opt-in until Level 2
        and Level 3 verification close (see backlog KG-GRAFEO-VERIFICATION).
        """
        value = self.get('knowledge_graph', 'backend', 'sqlite').strip().lower()
        return value if value in ('sqlite', 'grafeo') else 'sqlite'

    def get_gliner_device(self) -> str:
        """Where the GLiNER entity model runs: "auto" (default), "cpu" or "cuda".

        It sits beside the backend because it belongs to the same subsystem, and
        it exists for the same reason `embedding_device` does: the card this
        process shares is the binding constraint on the model the agents wait
        for, and entity extraction is background work whose latency nobody
        watches. "auto" is what every install had before this setting existed —
        cuda when torch sees a card, cpu otherwise. An unknown value falls back
        to "auto" rather than being obeyed: a typo must not decide what holds
        VRAM. Any value but "auto" overrides an agent's own `gliner_device` in
        its config.json; under "auto" that agent value still decides
        (knowledge_graph._resolve_gliner_device).
        """
        value = self.get('knowledge_graph', 'gliner_device', 'auto').strip().lower()
        if value in ('auto', 'cpu', 'cuda'):
            return value
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(
            "[knowledge_graph] gliner_device=%r is not a device this build knows "
            "(auto/cpu/cuda) — using auto", value,
        )
        return 'auto'

    def get_embedding_device(self) -> str:
        """Where the embedding model runs for this node: "auto" (default), "cpu", "cuda" or "mps".

        The model is one per process, so its device is the node's, not an agent's.
        Any value but "auto" overrides every agent profile's `memory.embedding_device`;
        "auto" leaves the choice where it was before this key existed — the first
        profile to ask, else cuda, then mps, then cpu. An unknown value falls back to
        "auto", for the reason `get_gliner_device` gives.
        """
        value = self.get('memory', 'embedding_device', 'auto').strip().lower()
        if value in ('auto', 'cpu', 'cuda', 'mps'):
            return value
        import logging
        logging.getLogger(__name__).warning(
            "[memory] embedding_device=%r is not a device this build knows "
            "(auto/cpu/cuda/mps) — using auto", value,
        )
        return 'auto'

    def get_background_model_settings(self) -> Dict[str, str]:
        """The three node-level background-model keys in one read, as the settings form
        shows them (A-NODE-HAS-NO-ONE-SWITCH-THAT-KEEPS-ITS-BACKGROUND-MODELS-OFF-THE-GPU):
        where the embedding model runs, where the GLiNER entity model runs, and which
        store the agent graphs use. `embedding_device` is handed to the memory module once
        at service start, so a written value is in force at the next start; the two
        `[knowledge_graph]` keys are read at each graph open, and the GLiNER model, once
        loaded, stays where it is for the process (`_get_gliner_model` warns and ignores
        a later device) — the form says so per key."""
        return {
            "embedding_device": self.get_embedding_device(),
            "gliner_device": self.get_gliner_device(),
            "backend": self.get_kg_backend(),
        }

    def set_background_model_settings(
        self, embedding_device: Optional[str] = None, gliner_device: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> None:
        """Write the three node-level background-model keys; a left-out key keeps its
        stored value.

        ValueError, one sentence, and nothing written, unless each given value is one its
        reader knows: `embedding_device` auto, cpu, cuda or mps; `gliner_device` auto, cpu
        or cuda (no mps — the reader's set, not a guess); `backend` sqlite or grafeo. The
        readers fall back silently on an unknown stored value, so the writer is where a
        typo is stopped."""
        writes: List[tuple] = []
        if embedding_device is not None:
            if (not isinstance(embedding_device, str)
                    or embedding_device.strip().lower() not in ('auto', 'cpu', 'cuda', 'mps')):
                raise ValueError("embedding_device must be one of auto, cpu, cuda or mps.")
            writes.append(('memory', 'embedding_device', embedding_device.strip().lower()))
        if gliner_device is not None:
            if (not isinstance(gliner_device, str)
                    or gliner_device.strip().lower() not in ('auto', 'cpu', 'cuda')):
                raise ValueError("gliner_device must be one of auto, cpu or cuda.")
            writes.append(('knowledge_graph', 'gliner_device', gliner_device.strip().lower()))
        if backend is not None:
            if not isinstance(backend, str) or backend.strip().lower() not in ('sqlite', 'grafeo'):
                raise ValueError("backend must be one of sqlite or grafeo.")
            writes.append(('knowledge_graph', 'backend', backend.strip().lower()))
        for section, key, text in writes:
            self.set(section, key, text)

    def get_log_level(self) -> str:
        """Get global log level (DEBUG/INFO/WARNING/ERROR/CRITICAL)."""
        return self.get('logging', 'level', 'INFO').upper()

    def get_log_file(self) -> Path:
        """Get log file path."""
        file_path = self.get('logging', 'file', '~/.dpc/logs/dpc-client.log')
        return Path(file_path).expanduser()

    def get_log_console(self) -> bool:
        """Check if console output is enabled."""
        value = self.get('logging', 'console', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_log_console_level(self) -> str:
        """Get console log level."""
        return self.get('logging', 'console_level', 'INFO').upper()

    def get_log_max_bytes(self) -> int:
        """Get max bytes per log file before rotation."""
        return int(self.get('logging', 'max_bytes', '10485760'))

    def get_log_backup_count(self) -> int:
        """Get number of backup log files to keep."""
        return int(self.get('logging', 'backup_count', '5'))

    def get_module_log_levels(self) -> Dict[str, str]:
        """Get per-module log level overrides."""
        overrides = {}
        if self._config.has_section('logging.modules'):
            for module_name, level in self._config.items('logging.modules'):
                overrides[module_name] = level.upper()
        return overrides

    def get_privacy_logging_settings(self) -> Dict[str, Any]:
        """The keys of the privacy and logging form in one read
        (THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI):
        the two [system] collection switches and the level keys of [logging].
        `collect_ai_models` has no getter of its own — its reader parses the raw
        key inline (device_context_collector), so this getter parses it the same
        way. When a written value is in force, per key: both switches are read at
        each device-context collection, and a collection runs at each backend
        start; the levels are applied once at backend start
        (run_service.setup_logging) — the form says so per key."""
        return {
            "auto_collect_device_info": self.get_auto_collect_device_info(),
            "collect_ai_models": self.get('system', 'collect_ai_models', 'false').lower()
            in ('true', '1', 'yes'),
            "level": self.get_log_level(),
            "console_level": self.get_log_console_level(),
            "module_levels": self.get_module_log_levels(),
        }

    def set_privacy_logging_settings(
        self, auto_collect_device_info: Optional[bool] = None,
        collect_ai_models: Optional[bool] = None, level: Optional[str] = None,
        console_level: Optional[str] = None,
        module_levels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Write the privacy and logging form's keys; a left-out key keeps its
        stored value, and a module left out of `module_levels` keeps its stored
        level.

        ValueError, one sentence, and nothing written, unless each given value is
        one its reader knows: the switches must be a real bool (their readers
        test a true set and read everything else as false, so a typo would
        silently turn a switch off — the writer is where that stops); a level
        must be one of DEBUG, INFO, WARNING, ERROR or CRITICAL, the set
        run_service feeds to getattr(logging, ...), in any case the way the
        getters upper-case what they read. No level is applied to the running
        process here: setup_logging runs once at backend start, and no live
        path exists."""
        levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
        writes: List[tuple] = []
        if auto_collect_device_info is not None:
            if not isinstance(auto_collect_device_info, bool):
                raise ValueError("auto_collect_device_info must be true or false.")
            writes.append(('system', 'auto_collect_device_info',
                           'true' if auto_collect_device_info else 'false'))
        if collect_ai_models is not None:
            if not isinstance(collect_ai_models, bool):
                raise ValueError("collect_ai_models must be true or false.")
            writes.append(('system', 'collect_ai_models',
                           'true' if collect_ai_models else 'false'))
        if level is not None:
            if not isinstance(level, str) or level.strip().upper() not in levels:
                raise ValueError("level must be one of DEBUG, INFO, WARNING, ERROR or CRITICAL.")
            writes.append(('logging', 'level', level.strip().upper()))
        if console_level is not None:
            if not isinstance(console_level, str) or console_level.strip().upper() not in levels:
                raise ValueError("console_level must be one of DEBUG, INFO, WARNING, ERROR or CRITICAL.")
            writes.append(('logging', 'console_level', console_level.strip().upper()))
        if module_levels is not None:
            if not isinstance(module_levels, dict):
                raise ValueError(
                    "module_levels must map module names to one of "
                    "DEBUG, INFO, WARNING, ERROR or CRITICAL.")
            for module_name, module_level in module_levels.items():
                name = module_name.strip() if isinstance(module_name, str) else ''
                if not name or not all(part.isidentifier() for part in name.split('.')):
                    raise ValueError(
                        f"module_levels has a module name this build cannot "
                        f"write: {module_name!r}.")
                if (not isinstance(module_level, str)
                        or module_level.strip().upper() not in levels):
                    raise ValueError(
                        f"module_levels[{name}] must be one of "
                        f"DEBUG, INFO, WARNING, ERROR or CRITICAL.")
                writes.append(('logging.modules', name, module_level.strip().upper()))
        for section, key, text in writes:
            self.set(section, key, text)

    def get_stun_servers(self) -> list[str]:
        """
        Get list of STUN server URLs from configuration.

        Returns:
            List of STUN server URLs (e.g., ['stun:stun.l.google.com:19302', ...])
        """
        try:
            servers_str = self.get('webrtc', 'stun_servers')
            # Split by comma and strip whitespace
            servers = [s.strip() for s in servers_str.split(',') if s.strip()]
            return servers if servers else []
        except KeyError:
            # If config is missing, return empty list (no hardcoded defaults)
            print("[Warning] No STUN servers configured in config.ini [webrtc] stun_servers")
            return []

    def get_turn_servers(self) -> list[str]:
        """
        Get list of TURN server URLs from configuration.
        Used when TURN credentials are provided.

        Returns:
            List of TURN server URLs (e.g., ['turn:global.relay.metered.ca:80', ...])
        """
        try:
            servers_str = self.get('turn', 'servers', '')
            servers = [s.strip() for s in servers_str.split(',') if s.strip()]
            return servers
        except KeyError:
            return []

    def get_turn_fallback_servers(self) -> list[str]:
        """
        Get list of fallback TURN server URLs (public servers).

        Returns:
            List of fallback TURN server URLs
        """
        try:
            servers_str = self.get('turn', 'fallback_servers', '')
            servers = [s.strip() for s in servers_str.split(',') if s.strip()]
            return servers
        except KeyError:
            return []

    def get_turn_fallback_username(self) -> Optional[str]:
        """Get fallback TURN server username."""
        try:
            username = self.get('turn', 'fallback_username', '')
            return username if username else None
        except KeyError:
            return None

    def get_turn_fallback_credential(self) -> Optional[str]:
        """Get fallback TURN server credential."""
        try:
            credential = self.get('turn', 'fallback_credential', '')
            return credential if credential else None
        except KeyError:
            return None

    def set(self, section: str, key: str, value: str):
        """Set a configuration value in the config file."""
        if not self._config.has_section(section):
            self._config.add_section(section)

        self._config.set(section, key, value)

        # Save to file
        with open(self.config_file, 'w') as f:
            self._config.write(f)

    def get_model_files_dir(self) -> Path:
        """The model files folder (`[local_engines] model_files_dir`), resolved.

        Empty (the default) is `<DPC home>/models`; `~` is expanded. Only reads: the
        folder is not created here, step 2 of the flow creates it on a first install.
        A hand-edited value that is not absolute cannot name one folder on every
        machine, so it is read as the default rather than against the working directory.
        """
        try:
            raw = self.get('local_engines', 'model_files_dir', '').strip()
        except configparser.Error as e:  # a hand-edited '%' that configparser cannot interpolate
            logger.warning("[local_engines] model_files_dir is unreadable (%s); using the default", e)
            raw = ''
        if raw:
            chosen = Path(raw).expanduser()
            if chosen.is_absolute():
                return Path(os.path.normpath(chosen))
            logger.warning("[local_engines] model_files_dir %r is not an absolute path; using the default", raw)
        return Path(os.path.abspath(self.dpc_home_dir.expanduser() / "models"))

    def set_model_files_dir(self, path: str) -> Path:
        """Write `[local_engines] model_files_dir`; an empty string resets it to the default.

        A non-empty path is checked first and nothing is written when it is refused
        (ValueError, one sentence naming the path): it must be absolute after `~` is
        expanded, not a filesystem root or a system folder, creatable and writable.
        The folder is created if missing. Nothing in it is moved or deleted.
        Returns the resolved folder, as `get_model_files_dir` then reads it.
        """
        raw = (path or "").strip()
        if not raw:
            self.set('local_engines', 'model_files_dir', '')
            return self.get_model_files_dir()
        folder = _check_model_files_dir(raw)
        # '%' is configparser's interpolation mark: a literal one is written doubled.
        self.set('local_engines', 'model_files_dir', str(folder).replace('%', '%%'))
        return self.get_model_files_dir()

    def get_swap_when_idle(self) -> bool:
        """`[local_engines] swap_when_idle`: whether an owner's start of a local alias stops
        an idle card holder that excludes it. Default false (Mike's call, 2026-11-02, after the
        live check cut an agent's loop); read at each start."""
        try:
            raw = self.get('local_engines', 'swap_when_idle', 'false').strip().lower()
        except configparser.Error as e:
            logger.warning("[local_engines] swap_when_idle is unreadable (%s); using the default", e)
            return False
        if raw in ('true', '1', 'yes', 'on'):
            return True
        if raw in ('false', '0', 'no', 'off'):
            return False
        logger.warning("[local_engines] swap_when_idle %r is not a boolean; using the default", raw)
        return False

    def get_swap_wait_s(self) -> int:
        """`[local_engines] swap_wait_s`: seconds a swap waits for a busy holder (0 = do not
        wait). Default 300; a hand-edited value that is not a whole number >= 0 reads as the default."""
        try:
            raw = self.get('local_engines', 'swap_wait_s', '300').strip()
        except configparser.Error as e:
            logger.warning("[local_engines] swap_wait_s is unreadable (%s); using the default", e)
            return 300
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value < 0:
            logger.warning("[local_engines] swap_wait_s %r is not a whole number >= 0; using the default", raw)
            return 300
        return value

    def get_caller_idle_s(self) -> int:
        """`[local_engines] caller_idle_s`: seconds a peer or a gateway client counts as busy
        on an alias after its last call there (0 = no window). Default 120; a hand-edited value
        that is not a whole number >= 0 reads as the default."""
        try:
            raw = self.get('local_engines', 'caller_idle_s', '120').strip()
        except configparser.Error as e:
            logger.warning("[local_engines] caller_idle_s is unreadable (%s); using the default", e)
            return 120
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value < 0:
            logger.warning("[local_engines] caller_idle_s %r is not a whole number >= 0; using the default", raw)
            return 120
        return value

    def set_local_engine_swap(self, enabled: bool, wait_s: int, caller_idle_s: Optional[int] = None) -> None:
        """Write `[local_engines] swap_when_idle`, `swap_wait_s` and, when given, `caller_idle_s`.
        ValueError, one sentence, and nothing written, unless `enabled` is a bool and `wait_s`
        and a given `caller_idle_s` whole numbers >= 0 (a bool is not one)."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false.")
        if isinstance(wait_s, bool) or not isinstance(wait_s, int) or wait_s < 0:
            raise ValueError("wait_s must be a whole number of seconds, 0 or more.")
        if caller_idle_s is not None and (
                isinstance(caller_idle_s, bool) or not isinstance(caller_idle_s, int) or caller_idle_s < 0):
            raise ValueError("caller_idle_s must be a whole number of seconds, 0 or more.")
        self.set('local_engines', 'swap_when_idle', 'true' if enabled else 'false')
        self.set('local_engines', 'swap_wait_s', str(wait_s))
        if caller_idle_s is not None:
            self.set('local_engines', 'caller_idle_s', str(caller_idle_s))

    def get_dht_enabled(self) -> bool:
        """Check if DHT peer discovery is enabled."""
        value = self.get('dht', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_dht_port(self) -> int:
        """Get DHT UDP port."""
        return int(self.get('dht', 'port', '8889'))

    def get_dht_k(self) -> int:
        """Get Kademlia k parameter (nodes per bucket)."""
        return int(self.get('dht', 'k', '20'))

    def get_dht_alpha(self) -> int:
        """Get Kademlia alpha parameter (lookup parallelism)."""
        return int(self.get('dht', 'alpha', '3'))

    def get_dht_bootstrap_timeout(self) -> float:
        """Get DHT bootstrap timeout in seconds."""
        return float(self.get('dht', 'bootstrap_timeout', '30'))

    def get_dht_lookup_timeout(self) -> float:
        """Get DHT lookup timeout in seconds."""
        return float(self.get('dht', 'lookup_timeout', '10'))

    def get_dht_bucket_refresh_interval(self) -> float:
        """Get DHT bucket refresh interval in seconds."""
        return float(self.get('dht', 'bucket_refresh_interval', '3600'))

    def get_dht_announce_interval(self) -> float:
        """Get DHT announce interval in seconds."""
        return float(self.get('dht', 'announce_interval', '3600'))

    def get_dht_seed_nodes(self) -> list[tuple[str, int]]:
        """
        Get list of DHT seed nodes from configuration.

        Returns:
            List of (ip, port) tuples for seed nodes
        """
        try:
            seeds_str = self.get('dht', 'seed_nodes', '')
            if not seeds_str:
                return []

            seeds = []
            for seed in seeds_str.split(','):
                seed = seed.strip()
                if ':' in seed:
                    ip, port_str = seed.rsplit(':', 1)
                    try:
                        port = int(port_str)
                        seeds.append((ip, port))
                    except ValueError:
                        print(f"[Warning] Invalid DHT seed node format: {seed}")
            return seeds
        except KeyError:
            return []

    # ===== Phase 6: Connection Strategy Configuration =====

    def get_connection_strategy_enabled(self, strategy: str) -> bool:
        """
        Check if a connection strategy is enabled.

        Args:
            strategy: One of: ipv6, ipv4, hub_webrtc, hole_punching, relays, gossip
        """
        key = f'enable_{strategy}'
        value = self.get('connection', key, 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_connection_timeout(self, strategy: str) -> float:
        """
        Get timeout for a connection strategy in seconds.

        Args:
            strategy: One of: ipv6, ipv4, webrtc, hole_punch, relay, gossip
        """
        # Per-strategy, matching _create_default_config. A single flat fallback made
        # a config without a [connection] section wait 30s for gossip (written: 5)
        # and 30s for hole punching (written: 15).
        written = {'ipv6': '60', 'ipv4': '60', 'webrtc': '30',
                   'hole_punch': '15', 'relay': '20', 'gossip': '5'}
        key = f'{strategy}_timeout'
        return float(self.get('connection', key, written.get(strategy, '30')))

    def get_remote_inference_timeout(self) -> float:
        """How long to wait for a peer to answer an inference request.

        Named separately from the connection strategies because it bounds work,
        not a handshake: the host's own ceiling is 900 s (ADR-040 D4-0).
        """
        return float(self.get('connection', 'remote_inference_timeout', '1200'))

    def get_hello_timeout(self) -> float:
        """How long the listener waits for HELLO after issuing its challenge."""
        return float(self.get('connection', 'hello_timeout', '10'))

    def get_link_silence_timeout(self) -> float:
        """How long a direct TLS link to a link_ping peer may stay silent before it is ended.

        The LINK_PING interval is a third of it, derived rather than set.
        """
        return float(self.get('connection', 'link_silence_timeout', '90'))

    def get_max_pending_hellos_per_ip(self) -> int:
        """How many inbound connections one address may hold before HELLO_ACK."""
        return int(self.get('connection', 'max_pending_hellos_per_ip', '8'))

    def get_max_proved_connections_per_ip(self) -> int:
        """How many inbound proved connections one address may hold at once."""
        return int(self.get('connection', 'max_proved_connections_per_ip', '8'))

    def get_reconnect_slow_interval_max_seconds(self) -> float:
        """Ceiling of the slow reconnect backoff that follows the fast attempts."""
        return float(self.get('connection', 'reconnect_slow_interval_max_seconds', '900'))

    def get_hole_punch_port(self) -> int:
        """Get UDP port for hole punching."""
        return int(self.get('hole_punch', 'udp_punch_port', '8890'))

    def get_hole_punch_dtls_handshake_timeout(self) -> float:
        """Get DTLS handshake timeout in seconds."""
        return float(self.get('hole_punch', 'dtls_handshake_timeout', '3'))

    def get_relay_enabled(self) -> bool:
        """Check if relay client mode is enabled."""
        value = self.get('relay', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_relay_volunteer(self) -> bool:
        """Check if this node volunteers as relay (server mode)."""
        value = self.get('relay', 'volunteer', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_relay_max_peers(self) -> int:
        """Get max concurrent relay sessions (server mode)."""
        return int(self.get('relay', 'max_peers', '10'))

    def get_relay_bandwidth_limit(self) -> float:
        """Get bandwidth limit for relaying in Mbps."""
        return float(self.get('relay', 'bandwidth_limit_mbps', '10.0'))

    def get_gossip_enabled(self) -> bool:
        """Check if gossip protocol is enabled."""
        value = self.get('gossip', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_gossip_max_hops(self) -> int:
        """Get maximum hops for gossip message forwarding."""
        return int(self.get('gossip', 'max_hops', '5'))

    def get_gossip_fanout(self) -> int:
        """Get number of random peers to forward gossip to."""
        return int(self.get('gossip', 'fanout', '3'))

    def get_gossip_ttl(self) -> int:
        """Get gossip message TTL in seconds."""
        return int(self.get('gossip', 'ttl_seconds', '86400'))

    def get_gossip_sync_interval(self) -> int:
        """Get anti-entropy sync interval in seconds."""
        return int(self.get('gossip', 'sync_interval', '300'))

    # Convenience methods for connection strategies
    def get_enable_hole_punching(self) -> bool:
        """Get whether UDP hole punching is enabled."""
        return self.get_connection_strategy_enabled('hole_punching')

    def get_hole_punch_timeout(self) -> float:
        """Get hole punch connection timeout."""
        return self.get_connection_timeout('hole_punch')

    # Vision API settings (Phase 2.6: Screenshot + Vision Integration)
    def get_vision_max_image_size_mb(self) -> int:
        """Get maximum image size in MB for clipboard paste and uploads."""
        return int(self.get('vision', 'max_image_size_mb', '5'))

    # Voice Transcription Settings (v0.13.2+)

    def get_voice_transcription_enabled(self) -> bool:
        """Check if auto-transcription of voice messages is enabled."""
        value = self.get('voice_transcription', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_voice_transcription_sender_transcribes(self) -> bool:
        """Check if sender should transcribe their own voice messages."""
        value = self.get('voice_transcription', 'sender_transcribes', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_voice_transcription_recipient_delay_seconds(self) -> int:
        """Get delay in seconds before recipients attempt transcription."""
        return int(self.get('voice_transcription', 'recipient_delay_seconds', '3'))

    def get_voice_transcription_timeout_seconds(self) -> int:
        """Get max wait time in seconds for peer's transcription before trying locally (v0.13.3+)."""
        return int(self.get('voice_transcription', 'timeout_seconds', '240'))

    def get_voice_transcription_provider_priority(self) -> list[str]:
        """Get ordered list of transcription provider aliases."""
        # Fallback matches default config (aliases match HuggingFace model names).
        # It did not until 2026-08-10: the fallback led with whisper-large-v3, which
        # _create_default_config never writes, so a config missing the key tried a
        # different first provider than a fresh install.
        priority_str = self.get('voice_transcription', 'provider_priority', 'whisper-large-v3-turbo,whisper-medium,whisper-small,openai')
        return [p.strip() for p in priority_str.split(',') if p.strip()]

    def get_voice_transcription_show_transcriber_name(self) -> bool:
        """Check if transcriber name should be shown in UI."""
        value = self.get('voice_transcription', 'show_transcriber_name', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_voice_transcription_cache_enabled(self) -> bool:
        """Check if transcription caching is enabled."""
        value = self.get('voice_transcription', 'cache_transcriptions', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_voice_transcription_fallback_to_openai(self) -> bool:
        """Check if OpenAI fallback is enabled."""
        value = self.get('voice_transcription', 'fallback_to_openai', 'true')
        return value.lower() in ('true', '1', 'yes')

    # Local Transcription Settings (v0.13.1+)

    def get_local_transcription_enabled(self) -> bool:
        """Check if local Whisper transcription is enabled."""
        value = self.get('local_transcription', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_local_transcription_fallback_to_openai(self) -> bool:
        """Check if fallback to OpenAI API is enabled when local fails."""
        value = self.get('local_transcription', 'fallback_to_openai', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_local_transcription_max_file_size_mb(self) -> int:
        """Get deprecated max file size fallback for local transcription in MB."""
        raw = self.get('local_transcription', 'max_file_size_mb', '25')
        try:
            value = int(str(raw).strip())
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
        logger.warning("[local_transcription] max_file_size_mb %r is invalid; using 25", raw)
        return 25

    def get_local_transcription_max_duration_seconds(self) -> Optional[float]:
        """`[local_transcription] max_duration_seconds`: max audio duration in seconds for local
        Whisper (`0` = unlimited; empty/unset = fall back to legacy `max_file_size_mb`). Invalid or
        negative values log a warning and return None (legacy fallback)."""
        default = _SEEDED_KEYS[('local_transcription', 'max_duration_seconds')][0]
        try:
            raw = self.get('local_transcription', 'max_duration_seconds', default)
        except configparser.Error as e:
            logger.warning("[local_transcription] max_duration_seconds is unreadable (%s); "
                           "falling back to legacy max_file_size_mb", e)
            return None
        text = str(raw).strip() if raw is not None else ""
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            logger.warning("[local_transcription] max_duration_seconds %r is invalid; "
                           "falling back to legacy max_file_size_mb", raw)
            return None
        if not math.isfinite(value) or value < 0:
            logger.warning("[local_transcription] max_duration_seconds %r must be >= 0; "
                           "falling back to legacy max_file_size_mb", raw)
            return None
        return 0.0 if value == 0 else value

    def get_music_auto_align_lyrics(self) -> bool:
        """`[music] auto_align_lyrics`: automatically align known lyrics of generated vocal takes
        against the audio using local Whisper when available (default True)."""
        default = _SEEDED_KEYS[('music', 'auto_align_lyrics')][0]
        try:
            raw = self.get('music', 'auto_align_lyrics', default)
        except configparser.Error as e:
            logger.warning("[music] auto_align_lyrics is unreadable (%s); using %s", e, default)
            return True
        text = str(raw).strip().lower() if raw is not None else default
        if text in ('1', 'true', 'yes', 'on'):
            return True
        if text in ('0', 'false', 'no', 'off'):
            return False
        logger.warning("[music] auto_align_lyrics %r is not a boolean; using true", raw)
        return True

    def get_music_default_engine(self) -> str:
        """`[music] default_engine`: configured default engine for new music jobs
        (``'yue2'`` or ``'acestep'``). Invalid or unreadable values fall back to
        ``'yue2'`` with a logged warning."""
        from .music.job_schemas import DEFAULT_FROM_TEXT_ENGINE, FROM_TEXT_ENGINES
        default = _SEEDED_KEYS[('music', 'default_engine')][0]
        try:
            raw = self.get('music', 'default_engine', default)
        except configparser.Error as e:
            logger.warning("[music] default_engine is unreadable (%s); using %s", e, default)
            return DEFAULT_FROM_TEXT_ENGINE
        text = str(raw).strip().lower() if raw is not None else default
        if text in FROM_TEXT_ENGINES:
            return text
        logger.warning("[music] default_engine %r is unknown (expected one of %s); using %s",
                       raw, ", ".join(FROM_TEXT_ENGINES), DEFAULT_FROM_TEXT_ENGINE)
        return DEFAULT_FROM_TEXT_ENGINE

    def set_music_default_engine(self, engine: str) -> str:
        """Write `[music] default_engine` after validating against `FROM_TEXT_ENGINES`.
        Raises ValueError on an invalid value without modifying config.ini."""
        from .music.job_schemas import FROM_TEXT_ENGINES
        if not isinstance(engine, str):
            raise ValueError(f"default_engine must be one of {', '.join(FROM_TEXT_ENGINES)}.")
        chosen = engine.strip().lower()
        if chosen not in FROM_TEXT_ENGINES:
            raise ValueError(
                f"default_engine must be one of {', '.join(FROM_TEXT_ENGINES)}, got {engine!r}."
            )
        self.set('music', 'default_engine', chosen)
        return chosen

    # -- the [music] keys of the two music forms (config-parity card --------------------------
    # THE-MUSIC-FILE-TRANSFER-VISION-AND-KNOWLEDGE-KEYS-HAVE-NO-CONTROL-IN-THE-UI): the Create
    # panel's engine-start keys, job limits and describer alias, the live lyrics keys, and the
    # player's export/stream/poll keys, in one read and one validate-then-write. Every rule
    # mirrors the reader the key answers to (named per key below); every reader replaces a bad
    # stored value silently, so the writer is where a typo is stopped. `default_engine` keeps
    # its own pair above, whose answer carries the capability report.

    def _music_form_text(self, key: str, default: str = '') -> str:
        """`[music] key` as text (the `SettingsReader.text` rule: stripped, '' when unset)."""
        raw = self.get('music', key, '')
        return str(raw).strip() if raw is not None else default

    def _music_form_port(self, key: str, default: int) -> int:
        """`[music] key` as a port, else `default` — the `SettingsReader.port` rule
        (engine_base.py: 0..65535, 0 = a free port at each start; the form does not offer
        that, and the writer below refuses it)."""
        try:
            port = int(self._music_form_text(key))
        except ValueError:
            return default
        return port if 0 <= port <= 65535 else default

    def _music_form_seconds(self, key: str, default: float) -> float:
        """`[music] key` as a number of at least 0, else `default` (the `SettingsReader.seconds`
        rule)."""
        try:
            value = float(self._music_form_text(key))
        except ValueError:
            return default
        return value if math.isfinite(value) and value >= 0 else default

    def _music_form_whole(self, key: str, default: int) -> int:
        """`[music] key` as a whole number of at least 0, else `default` (the
        `SettingsReader.whole` rule)."""
        try:
            value = int(self._music_form_text(key))
        except ValueError:
            return default
        return value if value >= 0 else default

    def _music_form_flag(self, key: str, default: bool) -> bool:
        """`[music] key` as a boolean, else `default` (the `_music_lrclib_enabled` /
        `_music_stream_ranges_linux` rule: 1/true/yes/on are true, the rest false)."""
        text = self._music_form_text(key).lower()
        if not text:
            return default
        return text in ('1', 'true', 'yes', 'on')

    def _music_form_positive(self, key: str, default: int) -> int:
        """`[music] key` as an integer of at least 1, else `default` (the
        `_music_positive_int` rule the agent job limits are read through)."""
        try:
            value = int(self._music_form_text(key))
        except ValueError:
            return default
        return value if value >= 1 else default

    def get_music_settings(self) -> Dict[str, Any]:
        """The `[music]` keys the two music forms show, as their readers resolve them now.
        Empty strings are the readers' defaults (the built-in binary and model folders, the
        default instrumental adapter, the default export folder); the numbers are the values
        a start, a job or a request would use. When a written value is in force, per key:
        the engine keys are re-read at every engine start (`read_config`, music/acestep.py,
        music/yue2.py) — a resident engine keeps its current value until it is stopped;
        `agent_queue_max` and `turn_jobs_max` are read at every propose; `describe_alias`
        at every describe; `auto_align_lyrics` at every take; `lrclib_enabled` per lyrics
        chain run; `export_dir` and `stream_ranges_linux` per export / stream-info request;
        `stream_port` and `poll_interval_s` only at backend start (the stream server is a
        singleton that early-returns while running, the library holds its poll figure)."""
        from .music.job_schemas import YUE2_BACKBONES, YUE2_BACKBONE_DEFAULT
        backbone = self._music_form_text('yue2_backbone') or YUE2_BACKBONE_DEFAULT
        return {
            # acestep.cpp (read at each start of that engine)
            "acestep_binary": self._music_form_text('acestep_binary'),
            "acestep_models_dir": self._music_form_text('acestep_models_dir'),
            "acestep_port": self._music_form_port('acestep_port', 9995),
            "acestep_idle_stop_s": self._music_form_seconds('acestep_idle_stop_s', 300.0),
            "acestep_device": self._music_form_text('acestep_device'),
            # yue2.cpp (read at each start of that engine)
            "yue2_binary": self._music_form_text('yue2_binary'),
            "yue2_models_dir": self._music_form_text('yue2_models_dir'),
            "yue2_backbone": backbone if backbone in YUE2_BACKBONES else YUE2_BACKBONE_DEFAULT,
            "yue2_port": self._music_form_port('yue2_port', 9994),
            "yue2_idle_stop_s": self._music_form_seconds('yue2_idle_stop_s', 300.0),
            "yue2_adapters_dir": self._music_form_text('yue2_adapters_dir'),
            "yue2_instrumental_adapter": self._music_form_text('yue2_instrumental_adapter'),
            "yue2_device": self._music_form_text('yue2_device'),
            "yue2_max_seq": self._music_form_whole('yue2_max_seq', 0),
            # the agent job limits (read at every propose; `_music_positive_int`: at least 1)
            "agent_queue_max": self._music_form_positive('agent_queue_max', 2),
            "turn_jobs_max": self._music_form_positive('turn_jobs_max', 1),
            # the describer of a same-style song ('auto', 'none', or a loaded provider alias)
            "describe_alias": self._music_form_text('describe_alias') or 'auto',
            # live lyrics keys
            "auto_align_lyrics": self._music_form_flag('auto_align_lyrics', True),
            "lrclib_enabled": self._music_form_flag('lrclib_enabled', True),
            # the player's keys
            "export_dir": self._music_form_text('export_dir'),
            "stream_ranges_linux": self._music_form_flag('stream_ranges_linux', False),
            "stream_port": self._music_form_port('stream_port', 9996),
            "poll_interval_s": self._music_form_seconds('poll_interval_s', 60.0),
        }

    def set_music_settings(
        self, acestep_binary: Optional[str] = None, acestep_models_dir: Optional[str] = None,
        acestep_port: Optional[int] = None, acestep_idle_stop_s: Optional[float] = None,
        acestep_device: Optional[str] = None, yue2_binary: Optional[str] = None,
        yue2_models_dir: Optional[str] = None, yue2_backbone: Optional[str] = None,
        yue2_port: Optional[int] = None, yue2_idle_stop_s: Optional[float] = None,
        yue2_adapters_dir: Optional[str] = None, yue2_instrumental_adapter: Optional[str] = None,
        yue2_device: Optional[str] = None, yue2_max_seq: Optional[int] = None,
        agent_queue_max: Optional[int] = None, turn_jobs_max: Optional[int] = None,
        describe_alias: Optional[str] = None, auto_align_lyrics: Optional[bool] = None,
        lrclib_enabled: Optional[bool] = None, export_dir: Optional[str] = None,
        stream_ranges_linux: Optional[bool] = None, stream_port: Optional[int] = None,
        poll_interval_s: Optional[float] = None,
    ) -> None:
        """Write the `[music]` keys the two music forms cover; a left-out key keeps its
        stored value.

        ValueError, one sentence naming `music.key`, and nothing written, unless each given
        value is one its reader takes: the ports whole numbers 0..65535, 0 being a free port
        at each start (the engine readers take it, and the stream server binds 0 and then
        keeps the port it was given); the idle/poll figures numbers of at least 0 (the poll
        figure above 0: the reader takes any number, but a poll of 0 or less is a busy
        loop); `yue2_backbone` stock or rs-tw and the
        devices CUDA0, Vulkan0, MTL0 or CPU (the readers' own sets — empty lets the engine
        pick); `yue2_max_seq` a whole number of at least 0; the queue limits at least 1
        (`_music_positive_int` replaces a smaller figure with the default, so 0 is refused
        here rather than silently undone); the booleans booleans; `export_dir` empty (the
        default export folder) or an absolute path that is not a filesystem root — the two
        checks `_music_export_root` applies at each export, applied at save instead. The
        alias checks the command that calls this does (it can see the loaded providers and
        the adapters folders)."""
        from .music.job_schemas import YUE2_BACKBONES
        values: Dict[str, str] = {}
        for key, given in (('acestep_binary', acestep_binary), ('acestep_models_dir', acestep_models_dir),
                           ('yue2_binary', yue2_binary), ('yue2_models_dir', yue2_models_dir),
                           ('yue2_adapters_dir', yue2_adapters_dir)):
            if given is not None:
                if not isinstance(given, str):
                    raise ValueError(f"music.{key} must be a path (empty = the default).")
                values[key] = given.strip().replace('%', '%%')
        for key, given in (('acestep_port', acestep_port), ('yue2_port', yue2_port),
                           ('stream_port', stream_port)):
            if given is not None:
                if isinstance(given, bool) or not isinstance(given, int) or not 0 <= given <= 65535:
                    raise ValueError(f"music.{key} must be a whole number from 0 to 65535 "
                                     f"(0 = a free port at each start).")
                values[key] = str(given)
        for key, given in (('acestep_idle_stop_s', acestep_idle_stop_s),
                           ('yue2_idle_stop_s', yue2_idle_stop_s)):
            if given is not None:
                if (isinstance(given, bool) or not isinstance(given, (int, float))
                        or not math.isfinite(given) or given < 0):
                    raise ValueError(f"music.{key} must be a number of idle seconds of at least 0.")
                values[key] = _figure_text(given)
        if poll_interval_s is not None:
            if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                    or not math.isfinite(poll_interval_s) or poll_interval_s <= 0):
                raise ValueError("music.poll_interval_s must be a number of seconds above 0.")
            values['poll_interval_s'] = _figure_text(poll_interval_s)
        for key, given in (('acestep_device', acestep_device), ('yue2_device', yue2_device)):
            if given is not None:
                if not isinstance(given, str) or given.strip() not in ('', 'CUDA0', 'Vulkan0', 'MTL0', 'CPU'):
                    raise ValueError(f"music.{key} must be one of CUDA0, Vulkan0, MTL0, CPU, or empty "
                                     "(the engine picks).")
                values[key] = given.strip()
        if yue2_backbone is not None:
            if not isinstance(yue2_backbone, str) or yue2_backbone.strip() not in YUE2_BACKBONES:
                raise ValueError(f"music.yue2_backbone must be one of {', '.join(YUE2_BACKBONES)}.")
            values['yue2_backbone'] = yue2_backbone.strip()
        if yue2_instrumental_adapter is not None:
            if not isinstance(yue2_instrumental_adapter, str):
                raise ValueError("music.yue2_instrumental_adapter must be an adapter name, 'none', or empty "
                                 "(the default adapter).")
            values['yue2_instrumental_adapter'] = yue2_instrumental_adapter.strip().replace('%', '%%')
        if yue2_max_seq is not None:
            if isinstance(yue2_max_seq, bool) or not isinstance(yue2_max_seq, int) or yue2_max_seq < 0:
                raise ValueError("music.yue2_max_seq must be a whole number of at least 0 (0 = the model "
                                 "context).")
            values['yue2_max_seq'] = str(yue2_max_seq)
        for key, given in (('agent_queue_max', agent_queue_max), ('turn_jobs_max', turn_jobs_max)):
            if given is not None:
                if isinstance(given, bool) or not isinstance(given, int) or given < 1:
                    raise ValueError(f"music.{key} must be a whole number of at least 1.")
                values[key] = str(given)
        if describe_alias is not None:
            if not isinstance(describe_alias, str) or not describe_alias.strip():
                raise ValueError("music.describe_alias must be 'auto', 'none', or a provider alias.")
            values['describe_alias'] = describe_alias.strip()
        for key, given in (('auto_align_lyrics', auto_align_lyrics), ('lrclib_enabled', lrclib_enabled),
                           ('stream_ranges_linux', stream_ranges_linux)):
            if given is not None:
                if not isinstance(given, bool):
                    raise ValueError(f"music.{key} must be true or false.")
                values[key] = 'true' if given else 'false'
        if export_dir is not None:
            if not isinstance(export_dir, str):
                raise ValueError("music.export_dir must be an absolute path (empty = the default export "
                                 "folder).")
            raw = export_dir.strip()
            if raw:
                # The two checks _music_export_root (music/commands.py) applies at each export,
                # applied at save so a bad value is refused here, not at the next export.
                chosen = Path(os.path.expandvars(os.path.expanduser(raw)))
                if not chosen.is_absolute():
                    raise ValueError(f"music.export_dir must be an absolute path: {raw}")
                chosen = Path(os.path.normpath(chosen))
                if chosen.parent == chosen:
                    raise ValueError(f"music.export_dir cannot be a filesystem root: {chosen}")
                raw = str(chosen).replace('%', '%%')
            values['export_dir'] = raw
        for key, text in values.items():
            self.set('music', key, text)

    # -- the feature keys of their own views (same config-parity card): the image size at the
    # composer, the knowledge extraction keys at the commit dialog, the history gate at the
    # conversation view, and the HuggingFace offline mode at the background-models form. The
    # same shape as the [music] block above; `get_hf_offline_mode` above stays what it was
    # (no caller): the three-state pair here mirrors run_service's own parse instead.

    def get_vision_settings(self) -> Dict[str, Any]:
        """`[vision] max_image_size_mb` as the composer's image cap (default 5, the getter's
        own). Checked for every image at once; the gateway's HTTP body cap is four times it,
        fixed when the gateway is built, so that side follows a new figure only after the
        next backend start."""
        try:
            figure = int(str(self.get('vision', 'max_image_size_mb', '5')).strip())
        except (TypeError, ValueError):
            figure = 5
        return {"max_image_size_mb": figure if figure > 0 else 5}

    def set_vision_settings(self, max_image_size_mb: Optional[int] = None) -> None:
        """Write `[vision] max_image_size_mb`; a left-out key keeps its stored value.

        ValueError, one sentence, and nothing written, unless `max_image_size_mb` is a whole
        number of MB above 0 (the getter casts through int, so anything smaller never
        applies)."""
        if max_image_size_mb is not None:
            if isinstance(max_image_size_mb, bool) or not isinstance(max_image_size_mb, int) \
                    or max_image_size_mb <= 0:
                raise ValueError("vision.max_image_size_mb must be a whole number of MB above 0.")
            self.set('vision', 'max_image_size_mb', str(max_image_size_mb))

    def get_knowledge_extraction_settings(self) -> Dict[str, Any]:
        """The two `[knowledge]` keys of the Extract Knowledge action, as their readers
        resolve them now: `cultural_perspectives_enabled` is read at every extraction
        (both prompt builders), `cold_fallback_provider` per extraction of a conversation
        with no provenance — '' is auto (the first local provider)."""
        return {
            "cultural_perspectives_enabled": self.get_cultural_perspectives_enabled(),
            "cold_fallback_provider": self.get_knowledge_cold_fallback_provider(),
        }

    def set_knowledge_extraction_settings(
        self, cultural_perspectives_enabled: Optional[bool] = None,
        cold_fallback_provider: Optional[str] = None,
    ) -> None:
        """Write the two `[knowledge]` keys; a left-out key keeps its stored value.

        ValueError, one sentence, and nothing written, unless
        `cultural_perspectives_enabled` is a bool and `cold_fallback_provider` is '' (auto)
        or a non-empty alias. Whether the alias is one the loaded providers know is the
        command's check (knowledge_routing.cold_fallback refuses an unknown one at
        extraction time; the settings object cannot see the providers)."""
        values: Dict[str, str] = {}
        if cultural_perspectives_enabled is not None:
            if not isinstance(cultural_perspectives_enabled, bool):
                raise ValueError("knowledge.cultural_perspectives_enabled must be true or false.")
            values['cultural_perspectives_enabled'] = 'true' if cultural_perspectives_enabled else 'false'
        if cold_fallback_provider is not None:
            if not isinstance(cold_fallback_provider, str):
                raise ValueError("knowledge.cold_fallback_provider must be an alias, or empty for auto.")
            alias = cold_fallback_provider.strip()
            if not alias:
                values['cold_fallback_provider'] = ''
            else:
                values['cold_fallback_provider'] = alias.replace('%', '%%')
        for key, text in values.items():
            self.set('knowledge', key, text)

    def get_history_sync_settings(self) -> Dict[str, Any]:
        """`[history] reject_unsigned`, as the synced-record gate reads it now (per synced
        record, conversation_monitor's `_reject_unsigned`)."""
        return {"reject_unsigned": self.get_reject_unsigned_history()}

    def set_history_sync_settings(self, reject_unsigned: Optional[bool] = None) -> None:
        """Write `[history] reject_unsigned`; a left-out key keeps its stored value.

        ValueError, one sentence, and nothing written, unless `reject_unsigned` is a bool.
        Off is the safe default for records written before ADR-036 (they carry no
        signature; see `get_reject_unsigned_history`)."""
        if reject_unsigned is not None:
            if not isinstance(reject_unsigned, bool):
                raise ValueError("history.reject_unsigned must be true or false.")
            self.set('history', 'reject_unsigned', 'true' if reject_unsigned else 'false')

    def get_hf_settings(self) -> Dict[str, Any]:
        """`[hf] offline_mode` as run_service reads it at process start (its own
        configparser, before huggingface loads): 'on' when the key parses true, 'off' when
        it parses false, 'auto' when it is absent or does not parse — with 'auto' the
        on-disk cache detection decides per start. An explicit HF_HUB_OFFLINE in the
        environment wins over every state; that is not this key's to show."""
        if not self._config.has_option('hf', 'offline_mode'):
            state = 'auto'
        else:
            try:
                state = 'on' if self._config.getboolean('hf', 'offline_mode') else 'off'
            except (ValueError, configparser.Error):
                state = 'auto'
        return {"offline_mode": state}

    def set_hf_settings(self, offline_mode: Any = None) -> None:
        """Write `[hf] offline_mode`; a left-out value keeps the stored state.

        ValueError, one sentence, and nothing written, unless `offline_mode` is 'on'
        (written `true`), 'off' (written `false`) or 'auto' — the spellings run_service's
        `getboolean` takes, and 'auto' removes the key, because with it absent run_service
        goes offline by itself exactly when every model it needs is already cached."""
        if offline_mode is None:
            return
        if not isinstance(offline_mode, str) or offline_mode.strip().lower() not in ('auto', 'on', 'off'):
            raise ValueError("hf.offline_mode must be one of auto, on or off.")
        state = offline_mode.strip().lower()
        if state == 'auto':
            if self._config.has_section('hf') and self._config.has_option('hf', 'offline_mode'):
                self._config.remove_option('hf', 'offline_mode')
                with open(self.config_file, 'w') as f:
                    self._config.write(f)
            return
        self.set('hf', 'offline_mode', 'true' if state == 'on' else 'false')

    def get_local_transcription_idle_unload_s(self) -> float:
        """`[local_transcription] idle_unload_s`: seconds after the last transcription
        before the loaded model is unloaded to free the card. 0 = never. The default is the
        seed table's (`_SEEDED_KEYS`, 300)."""
        default = _SEEDED_KEYS[('local_transcription', 'idle_unload_s')][0]
        raw = self.get('local_transcription', 'idle_unload_s', default)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning("[local_transcription] idle_unload_s is unreadable (%r); using %s", raw, default)
            return float(default)
        return value if value > 0 else 0.0

    def get_local_transcription_queue_wait_s(self) -> int:
        """`[local_transcription] queue_wait_s`: the dictation bound, seconds a person's
        transcription waits in line for the card (0 = do not wait). The default is the seed
        table's (`_SEEDED_KEYS`, 600; ADR-052 L4, item 6A, Mike 2026-11-23); a value that is not a
        whole number >= 0 reads as the default."""
        default = _SEEDED_KEYS[('local_transcription', 'queue_wait_s')][0]
        try:
            raw = self.get('local_transcription', 'queue_wait_s', default).strip()
        except configparser.Error as e:
            logger.warning("[local_transcription] queue_wait_s is unreadable (%s); using the default", e)
            return int(default)
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value < 0:
            logger.warning("[local_transcription] queue_wait_s %r is not a whole number >= 0; using the default", raw)
            return int(default)
        return value

    def _local_transcription_figure(self, key: str, zero_is_off: bool = False) -> float:
        """A `[local_transcription]` figure above 0 that the seed table defaults (`_SEEDED_KEYS`): the
        file's value first, the code default for one that is missing, unreadable or not above 0. With
        `zero_is_off`, 0 is read as 0 (the CPU route off, Mike 2026-11-24), never as the default; below
        0 still reads as the default."""
        default = float(_SEEDED_KEYS[('local_transcription', key)][0])
        raw = None
        try:
            raw = self.get('local_transcription', key, str(default))
            value = float(str(raw).strip())
        except (configparser.Error, TypeError, ValueError) as e:
            logger.warning("[local_transcription] %s is unreadable (%s); using %g", key, e, default)
            return default
        if zero_is_off and value == 0:
            return 0.0
        if not math.isfinite(value) or value <= 0:
            logger.warning("[local_transcription] %s %r is not a figure above 0%s; using %g", key, raw,
                           " (0 turns the CPU route off)" if zero_is_off else "", default)
            return default
        return value

    def get_local_transcription_cpu_max_s(self) -> float:
        """`[local_transcription] cpu_max_s`: ADR-052 L12, the CPU twin takes a transcription only when
        its estimate is within this (600 s, item 9, decided 2026-11-23). 0 turns the CPU route off
        (Mike, 2026-11-24)."""
        return self._local_transcription_figure('cpu_max_s', zero_is_off=True)

    def get_local_transcription_cpu_max_audio_s(self) -> float:
        """`[local_transcription] cpu_max_audio_s`: ADR-052 L12 point 2, longer audio never goes to the
        CPU twin (300 s, Mike #71); also the cap of an agent's transcription inside its own turn (3.5).
        0 turns the CPU route off (Mike, 2026-11-24)."""
        return self._local_transcription_figure('cpu_max_audio_s', zero_is_off=True)

    def get_local_transcription_cpu_contention_factor(self) -> float:
        """`[local_transcription] cpu_contention_factor`: ADR-052 L12 point 3, the CPU twin's estimate
        times this while the CPU computes something else (L12 point 3: a busy resident Strata or
        llama-server, another CPU transcription; Mike 2026-11-24) (1.0, item 9, a start value tuned
        from operation). A multiplier, not a switch: 0 reads as the default."""
        return self._local_transcription_figure('cpu_contention_factor')

    def get_local_transcription_cpu_route(self) -> Dict[str, float]:
        """L12's three numbers in one read, the source `whisper_provider.set_cpu_route_source` takes."""
        return {"cpu_max_s": self.get_local_transcription_cpu_max_s(),
                "cpu_max_audio_s": self.get_local_transcription_cpu_max_audio_s(),
                "cpu_contention_factor": self.get_local_transcription_cpu_contention_factor()}

    def get_local_transcription_settings(self) -> Dict[str, Any]:
        """The nine live `[local_transcription]` keys in one read, as the settings form shows them
        (THE-VOICE-AND-TRANSCRIPTION-SETTINGS-ARE-EDITABLE-ONLY-BY-HAND-IN-CONFIG-INI). The other
        nine keys of the section are dead (their getter has no caller) and are not here; every key
        here is read live, so a value written through `set_local_transcription_settings` is in force
        without a restart. `max_duration_seconds` is None for the empty legacy fallback."""
        return {
            "enabled": self.get_local_transcription_enabled(),
            "fallback_to_openai": self.get_local_transcription_fallback_to_openai(),
            "max_file_size_mb": self.get_local_transcription_max_file_size_mb(),
            "max_duration_seconds": self.get_local_transcription_max_duration_seconds(),
            "idle_unload_s": self.get_local_transcription_idle_unload_s(),
            "queue_wait_s": self.get_local_transcription_queue_wait_s(),
            "cpu_max_s": self.get_local_transcription_cpu_max_s(),
            "cpu_max_audio_s": self.get_local_transcription_cpu_max_audio_s(),
            "cpu_contention_factor": self.get_local_transcription_cpu_contention_factor(),
        }

    def set_local_transcription_settings(
        self, enabled: Optional[bool] = None, fallback_to_openai: Optional[bool] = None,
        max_file_size_mb: Optional[int] = None, max_duration_seconds: Any = None,
        idle_unload_s: Optional[float] = None, queue_wait_s: Optional[int] = None,
        cpu_max_s: Optional[float] = None, cpu_max_audio_s: Optional[float] = None,
        cpu_contention_factor: Optional[float] = None,
    ) -> None:
        """Write the nine live `[local_transcription]` keys; a left-out key keeps its stored value.

        ValueError, one sentence, and nothing written, unless each given value is of its kind:
        `enabled` and `fallback_to_openai` bools; `max_file_size_mb` and `queue_wait_s` whole
        numbers >= 0; `idle_unload_s`, `cpu_max_s` and `cpu_max_audio_s` figures >= 0 (0 = never /
        the CPU route off); `cpu_contention_factor` a figure above 0 (a multiplier, not a switch);
        `max_duration_seconds` a figure >= 0 (0 = unlimited), or empty (None or '') for the legacy
        `max_file_size_mb` fallback. Every reader of these keys reads them live."""
        values: Dict[str, str] = {}
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false.")
            values['enabled'] = 'true' if enabled else 'false'
        if fallback_to_openai is not None:
            if not isinstance(fallback_to_openai, bool):
                raise ValueError("fallback_to_openai must be true or false.")
            values['fallback_to_openai'] = 'true' if fallback_to_openai else 'false'
        if max_file_size_mb is not None:
            if isinstance(max_file_size_mb, bool) or not isinstance(max_file_size_mb, int) or max_file_size_mb < 0:
                raise ValueError("max_file_size_mb must be a whole number of MB, 0 or more.")
            values['max_file_size_mb'] = str(max_file_size_mb)
        if max_duration_seconds is not None:
            if isinstance(max_duration_seconds, str) and not max_duration_seconds.strip():
                values['max_duration_seconds'] = ''  # the cleared field asks for the legacy fallback
            else:
                if (isinstance(max_duration_seconds, bool) or not isinstance(max_duration_seconds, (int, float))
                        or not math.isfinite(max_duration_seconds) or max_duration_seconds < 0):
                    raise ValueError("max_duration_seconds must be a number of seconds, 0 or more "
                                     "(0 = unlimited), or empty for the legacy max_file_size_mb.")
                values['max_duration_seconds'] = _figure_text(max_duration_seconds)
        if idle_unload_s is not None:
            if (isinstance(idle_unload_s, bool) or not isinstance(idle_unload_s, (int, float))
                    or not math.isfinite(idle_unload_s) or idle_unload_s < 0):
                raise ValueError("idle_unload_s must be a number of seconds, 0 or more (0 = never unload).")
            values['idle_unload_s'] = _figure_text(idle_unload_s)
        if queue_wait_s is not None:
            if isinstance(queue_wait_s, bool) or not isinstance(queue_wait_s, int) or queue_wait_s < 0:
                raise ValueError("queue_wait_s must be a whole number of seconds, 0 or more.")
            values['queue_wait_s'] = str(queue_wait_s)
        if cpu_max_s is not None:
            if (isinstance(cpu_max_s, bool) or not isinstance(cpu_max_s, (int, float))
                    or not math.isfinite(cpu_max_s) or cpu_max_s < 0):
                raise ValueError("cpu_max_s must be a number of seconds, 0 or more (0 = the CPU route off).")
            values['cpu_max_s'] = _figure_text(cpu_max_s)
        if cpu_max_audio_s is not None:
            if (isinstance(cpu_max_audio_s, bool) or not isinstance(cpu_max_audio_s, (int, float))
                    or not math.isfinite(cpu_max_audio_s) or cpu_max_audio_s < 0):
                raise ValueError("cpu_max_audio_s must be a number of seconds, 0 or more (0 = the CPU route off).")
            values['cpu_max_audio_s'] = _figure_text(cpu_max_audio_s)
        if cpu_contention_factor is not None:
            if (isinstance(cpu_contention_factor, bool) or not isinstance(cpu_contention_factor, (int, float))
                    or not math.isfinite(cpu_contention_factor) or cpu_contention_factor <= 0):
                raise ValueError("cpu_contention_factor must be a multiplier above 0.")
            values['cpu_contention_factor'] = _figure_text(cpu_contention_factor)
        for key, text in values.items():
            self.set('local_transcription', key, text)

    def get_voice_messages_settings(self) -> Dict[str, Any]:
        """The seven live `[voice_messages]` keys in one read, as the settings form shows them.
        The defaults mirror the inline readers (service.py, telegram_service.py), which read the
        same keys live at each record and send; `enabled` is dead (no reader) and is not here."""
        try:
            max_duration = float(self.get('voice_messages', 'max_duration_seconds', '300'))
        except (TypeError, ValueError):
            max_duration = 300.0
        if not math.isfinite(max_duration) or max_duration <= 0:
            max_duration = 300.0  # a hand-edited "nan"/"inf" must not mean "no limit"
        try:
            max_size_mb = int(self.get('voice_messages', 'max_size_mb', '10'))
        except (TypeError, ValueError):
            max_size_mb = 10
        mime_types = [m.strip() for m in self.get(
            'voice_messages', 'mime_types',
            'audio/webm,audio/opus,audio/ogg,audio/mp4,audio/mpeg,audio/wav').split(',') if m.strip()]
        try:
            sample_rate = int(self.get('voice_messages', 'default_sample_rate', '48000'))
        except (TypeError, ValueError):
            sample_rate = 48000
        try:
            channels = int(self.get('voice_messages', 'default_channels', '1'))
        except (TypeError, ValueError):
            channels = 1
        codec = self.get('voice_messages', 'default_codec', 'opus').strip() or 'opus'
        raw_auto = self.get('voice_messages', 'auto_transcribe', 'true')
        return {
            "max_duration_seconds": max_duration,
            "max_size_mb": max_size_mb,
            "mime_types": mime_types,
            "default_sample_rate": sample_rate,
            "default_channels": channels,
            "default_codec": codec,
            "auto_transcribe": raw_auto.lower() in ('true', '1', 'yes'),
        }

    def set_voice_messages_settings(
        self, max_duration_seconds: Optional[float] = None, max_size_mb: Optional[int] = None,
        mime_types: Any = None, default_sample_rate: Optional[int] = None,
        default_channels: Optional[int] = None, default_codec: Optional[str] = None,
        auto_transcribe: Optional[bool] = None,
    ) -> None:
        """Write the seven live `[voice_messages]` keys; a left-out key keeps its stored value.

        ValueError, one sentence, and nothing written, unless each given value is of its kind:
        `max_duration_seconds` a figure above 0, `max_size_mb`, `default_sample_rate` and
        `default_channels` whole numbers above 0, `mime_types` a list of mime types (or one
        comma-separated string) with at least one entry, `default_codec` a non-empty name, and
        `auto_transcribe` a bool. Every reader of these keys reads them live."""
        values: Dict[str, str] = {}
        if max_duration_seconds is not None:
            if (isinstance(max_duration_seconds, bool) or not isinstance(max_duration_seconds, (int, float))
                    or not math.isfinite(max_duration_seconds) or max_duration_seconds <= 0):
                raise ValueError("max_duration_seconds must be a number of seconds above 0.")
            values['max_duration_seconds'] = _figure_text(max_duration_seconds)
        if max_size_mb is not None:
            if isinstance(max_size_mb, bool) or not isinstance(max_size_mb, int) or max_size_mb <= 0:
                raise ValueError("max_size_mb must be a whole number of MB above 0.")
            values['max_size_mb'] = str(max_size_mb)
        if mime_types is not None:
            if isinstance(mime_types, str):
                mime_types = mime_types.split(',')
            if (not isinstance(mime_types, list)
                    or not all(isinstance(m, str) for m in mime_types)
                    or not [m for m in (str(m).strip() for m in mime_types) if m]):
                raise ValueError("mime_types must be a list of audio mime types, at least one.")
            values['mime_types'] = ','.join(m.strip() for m in mime_types if m.strip())
        if default_sample_rate is not None:
            if isinstance(default_sample_rate, bool) or not isinstance(default_sample_rate, int) or default_sample_rate <= 0:
                raise ValueError("default_sample_rate must be a whole number of Hz above 0.")
            values['default_sample_rate'] = str(default_sample_rate)
        if default_channels is not None:
            if isinstance(default_channels, bool) or not isinstance(default_channels, int) or default_channels <= 0:
                raise ValueError("default_channels must be a whole number of channels above 0 (1 = mono, 2 = stereo).")
            values['default_channels'] = str(default_channels)
        if default_codec is not None:
            if not isinstance(default_codec, str) or not default_codec.strip():
                raise ValueError("default_codec must be a codec name, e.g. opus.")
            values['default_codec'] = default_codec.strip()
        if auto_transcribe is not None:
            if not isinstance(auto_transcribe, bool):
                raise ValueError("auto_transcribe must be true or false.")
            values['auto_transcribe'] = 'true' if auto_transcribe else 'false'
        for key, text in values.items():
            self.set('voice_messages', key, text)

    # Telegram Bot Integration Settings (v0.14.0+)

    def get_telegram_enabled(self) -> bool:
        """Check if Telegram bot integration is enabled."""
        value = self.get('telegram', 'enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_telegram_bot_token(self) -> str:
        """Get Telegram bot token."""
        return self.get('telegram', 'bot_token', '')

    def get_telegram_allowed_chat_ids(self) -> list:
        """Get list of allowed Telegram chat IDs (whitelist)."""
        import json
        try:
            chat_ids_str = self.get('telegram', 'allowed_chat_ids', '[]')
            return json.loads(chat_ids_str)
        except json.JSONDecodeError:
            return []

    def get_telegram_use_webhook(self) -> bool:
        """Check if webhook mode is enabled (vs polling)."""
        value = self.get('telegram', 'use_webhook', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_telegram_webhook_url(self) -> str:
        """Get Telegram webhook URL."""
        return self.get('telegram', 'webhook_url', '')

    def get_telegram_webhook_port(self) -> int:
        """Get Telegram webhook server port."""
        return int(self.get('telegram', 'webhook_port', '8443'))

    def get_telegram_owner_contact(self) -> str:
        """Get bot owner contact information (shown to unauthorized users)."""
        return self.get('telegram', 'owner_contact', '')

    def get_telegram_access_denied_message(self) -> str:
        """Get custom access denied message (optional)."""
        return self.get('telegram', 'access_denied_message', '')

    def get_telegram_transcription_enabled(self) -> bool:
        """Check if Telegram voice transcription is enabled."""
        value = self.get('telegram', 'transcription_enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_telegram_bridge_to_p2p(self) -> bool:
        """Check if Telegram → P2P bridging is enabled."""
        value = self.get('telegram', 'bridge_to_p2p', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_telegram_config(self) -> dict:
        """Get all Telegram configuration as a dict."""
        import json
        return {
            'enabled': self.get_telegram_enabled(),
            'bot_token': self.get_telegram_bot_token(),
            'allowed_chat_ids': self.get_telegram_allowed_chat_ids(),
            'use_webhook': self.get_telegram_use_webhook(),
            'webhook_url': self.get_telegram_webhook_url(),
            'webhook_port': self.get_telegram_webhook_port(),
            'owner_contact': self.get_telegram_owner_contact(),
            'access_denied_message': self.get_telegram_access_denied_message(),
            'transcription_enabled': self.get_telegram_transcription_enabled(),
            'bridge_to_p2p': self.get_telegram_bridge_to_p2p()
        }

    def get_telegram_last_update_id(self, chat_id: str) -> int:
        """Get last processed update_id for a specific chat."""
        import json
        try:
            all_updates = json.loads(self.get('telegram', 'last_update_id', '{}'))
            return int(all_updates.get(chat_id, 0))
        except (json.JSONDecodeError, ValueError):
            return 0

    def set_telegram_last_update_id(self, chat_id: str, update_id: int):
        """Set last processed update_id for a specific chat."""
        import json
        try:
            all_updates = json.loads(self.get('telegram', 'last_update_id', '{}'))
            all_updates[chat_id] = update_id
            self.set('telegram', 'last_update_id', json.dumps(all_updates))
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to save last_update_id: {e}")

    def remove_telegram_last_update_id(self, chat_id: str):
        """Remove last_update_id entry for a specific Telegram chat.

        This should be called when a Telegram chat is unlinked/deleted to prevent
        stale entries from accumulating in config.ini.
        """
        import json
        import logging
        logger = logging.getLogger(__name__)
        try:
            all_updates = json.loads(self.get('telegram', 'last_update_id', '{}'))
            if chat_id in all_updates:
                del all_updates[chat_id]
                self.set('telegram', 'last_update_id', json.dumps(all_updates))
                logger.info(f"Removed last_update_id for Telegram chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to remove last_update_id for chat {chat_id}: {e}")

    # DPC Agent Telegram Settings

    def get_dpc_agent_telegram_enabled(self) -> bool:
        """Check if Telegram notifications for agent events are enabled."""
        value = self.get('dpc_agent_telegram', 'enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_dpc_agent_telegram_bot_token(self) -> str:
        """Get Telegram bot token for agent notifications."""
        return self.get('dpc_agent_telegram', 'bot_token', '')

    def get_dpc_agent_telegram_allowed_chat_ids(self) -> list[str]:
        """Get list of allowed chat IDs for agent Telegram notifications."""
        import json
        chat_ids_str = self.get('dpc_agent_telegram', 'allowed_chat_ids', '[]')
        try:
            return json.loads(chat_ids_str)
        except json.JSONDecodeError:
            return []

    def get_dpc_agent_telegram_event_filter(self) -> list[str]:
        """Get list of event types to forward to Telegram."""
        filter_str = self.get('dpc_agent_telegram', 'event_filter',
                              'task_completed,task_failed,code_modified,agent_message,sleep_state_changed')
        return [e.strip() for e in filter_str.split(',') if e.strip()]

    def get_dpc_agent_telegram_transcription_enabled(self) -> bool:
        """Check if voice message transcription is enabled for agent Telegram bot."""
        value = self.get('dpc_agent_telegram', 'transcription_enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_dpc_agent_telegram_config(self) -> dict:
        """Get all DPC agent Telegram configuration as a dict."""
        return {
            'enabled': self.get_dpc_agent_telegram_enabled(),
            'bot_token': self.get_dpc_agent_telegram_bot_token(),
            'allowed_chat_ids': self.get_dpc_agent_telegram_allowed_chat_ids(),
            'event_filter': self.get_dpc_agent_telegram_event_filter(),
            'transcription_enabled': self.get_dpc_agent_telegram_transcription_enabled(),
        }

    # ── Discord Integration ──────────────────────────────────

    def get_discord_enabled(self) -> bool:
        value = self.get('discord', 'enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_discord_bot_token(self) -> str:
        env_key = self.get('discord', 'bot_token_env', '')
        if env_key:
            import os
            return os.environ.get(env_key, '')
        return self.get('discord', 'bot_token', '')

    def get_discord_guild_id(self) -> str:
        return self.get('discord', 'guild_id', '')

    def get_discord_allowed_channel_ids(self) -> list[str]:
        import json
        try:
            return json.loads(self.get('discord', 'allowed_channel_ids', '[]'))
        except json.JSONDecodeError:
            return []

    def get_discord_ark_channel_id(self) -> str:
        return self.get('discord', 'ark_channel_id', '')

    def get_discord_config(self) -> dict:
        return {
            'enabled': self.get_discord_enabled(),
            'bot_token': self.get_discord_bot_token(),
            'guild_id': self.get_discord_guild_id(),
            'allowed_channel_ids': self.get_discord_allowed_channel_ids(),
            'ark_channel_id': self.get_discord_ark_channel_id(),
        }

    def get_telegram_discord_settings(self) -> Dict[str, Any]:
        """The keys of the Telegram and Discord form in one read
        (THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI):
        per integration the enable switch and whether a bot token is stored,
        plus the live [discord.guardrails] and [discord.conversations] limits.
        A stored token is answered only as `token_set` — never its value, a
        prefix or a length — and it is read from the stored config, not through
        get(), so an overriding DPC_TELEGRAM_BOT_TOKEN-style variable cannot
        make the form claim a token it cannot clear. `bot_token_env` is the
        environment variable *name* (not a secret) the Discord reader prefers
        when it is set. When a written value is in force, per key: the two
        switches and the tokens are read once, when the backend builds the
        Telegram service (telegram_service.py) and starts the Discord one
        (discord_service.py), so they apply from the next backend start — and
        with no token the Telegram bot logs an error and the Discord one skips
        starting, however the switch is set; the guardrail and conversation
        limits are read as each Discord message arrives
        (discord_coordinator.py), so they apply to the next message."""
        return {
            "telegram": {
                "enabled": self.get_telegram_enabled(),
                "token_set": bool(self._config.get('telegram', 'bot_token', fallback='').strip()),
            },
            "discord": {
                "enabled": self.get_discord_enabled(),
                "token_set": bool(self._config.get('discord', 'bot_token', fallback='').strip()),
                "bot_token_env": self.get('discord', 'bot_token_env', ''),
                "rate_limit_per_user_max": int(self.get(
                    'discord.guardrails', 'rate_limit_per_user_max', '10')),
                "rate_limit_per_user_window": int(self.get(
                    'discord.guardrails', 'rate_limit_per_user_window', '600')),
                "rate_limit_global_max": int(self.get(
                    'discord.guardrails', 'rate_limit_global_max', '60')),
                "rate_limit_global_window": int(self.get(
                    'discord.guardrails', 'rate_limit_global_window', '3600')),
                "response_delay_seconds": int(self.get(
                    'discord.guardrails', 'response_delay_seconds', '3')),
                "ttl_minutes": int(self.get(
                    'discord.conversations', 'ttl_minutes', '30')),
                "max_messages": int(self.get(
                    'discord.conversations', 'max_messages', '50')),
            },
        }

    def set_telegram_discord_settings(
        self, telegram: Optional[Dict[str, Any]] = None,
        discord: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write the Telegram and Discord form's keys; a key left out keeps its
        stored value. A token is never overwritten by accident: `bot_token`
        left out or empty *keeps* the stored token, only a non-empty string
        sets it, and only `clear_bot_token: true` (with no new token in the
        same section) removes it.

        ValueError, one sentence naming the key, and nothing written, unless
        each given value is one its reader knows: the switches must be a real
        bool (their readers test a true set and read everything else as false,
        so a typo would silently turn a bot off — the writer is where that
        stops); a token must be a string (the bots pass it to their libraries
        verbatim); the limits must be a whole number a reader can mean — the
        maxes, windows and conversation limits 1 or more (0 would deny every
        message or reset every conversation at once), the response delay 0 or
        more (0 is an honest "answer at once")."""
        writes: List[tuple] = []
        for section, given in (('telegram', telegram), ('discord', discord)):
            if given is None:
                continue
            if given.get('enabled') is not None:
                if not isinstance(given.get('enabled'), bool):
                    raise ValueError(f"{section}.enabled must be true or false.")
                writes.append((section, 'enabled',
                               'true' if given['enabled'] else 'false'))
            token = given.get('bot_token')
            clearing = given.get('clear_bot_token', False)
            if not isinstance(clearing, bool):
                raise ValueError(f"{section}.clear_bot_token must be true or false.")
            if token is not None and not isinstance(token, str):
                raise ValueError(f"{section}.bot_token must be a string.")
            stripped = token.strip() if isinstance(token, str) else ''
            if clearing and stripped:
                raise ValueError(
                    f"{section}.bot_token cannot be set and cleared in one save.")
            if clearing:
                writes.append((section, 'bot_token', ''))
            elif stripped:
                writes.append((section, 'bot_token', stripped))
        if discord:
            limits = (
                ('discord.guardrails', 'rate_limit_per_user_max', 1),
                ('discord.guardrails', 'rate_limit_per_user_window', 1),
                ('discord.guardrails', 'rate_limit_global_max', 1),
                ('discord.guardrails', 'rate_limit_global_window', 1),
                ('discord.guardrails', 'response_delay_seconds', 0),
                ('discord.conversations', 'ttl_minutes', 1),
                ('discord.conversations', 'max_messages', 1),
            )
            for section_name, key, at_least in limits:
                value = discord.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
                    raise ValueError(
                        f"{section_name}.{key} must be a whole number "
                        f"of {at_least} or more.")
                writes.append((section_name, key, str(value)))
        for section, key, text in writes:
            self.set(section, key, text)

    def get_network_settings(self) -> Dict[str, Any]:
        """The keys of the Network form in one read
        (THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI):
        the live [p2p]/[connection]/[hole_punch]/[turn]/[webrtc] keys, the
        start-only [dht]/[relay]/[gossip]/[hub]/[oauth]/[gateway]/
        [file_transfer] ones, and p2p.listen_port for display only — the port
        is bound when the backend starts and has no writer here (the form
        shows it read-only). A stored TURN credential is answered only as
        `credential_set` / `fallback_credential_set` — never its value — and
        read from the stored config, not through get(), so an overriding
        DPC_TURN_CREDENTIAL-style variable cannot make the form claim a
        secret it cannot clear. The comma-separated lists (seed_nodes, the
        ICE server lists) are answered as the stored text, verbatim. When a
        written value is in force, per key: p2p.connection_timeout,
        connection.remote_inference_timeout,
        connection.reconnect_slow_interval_max_seconds and
        hole_punch.dtls_handshake_timeout are re-read per dial, per request,
        per slow-reconnect start and per punch attempt, so they apply at
        once / to the next use; p2p.auto_connect_node_groups is re-read at
        each reconnect give-up check, so it applies to the next reconnect
        check; the six [turn] keys and webrtc.stun_servers are read per new
        WebRTC peer through a fresh Settings parse (the STUN discovery task
        keeps its own copy for the life of the process, so the
        public-address discovery follows a new stun_servers only after a
        restart); everything else is captured once at start
        (P2PManager.start_server into DHTConfig, the relay/gossip/hole-punch/
        file-transfer managers' __init__, CoreService.start's one-shot reads,
        HubClient's fields, the GatewayServer built in CoreService.__init__),
        so it applies after the next backend start."""
        return {
            "p2p": {
                "listen_port": self.get_p2p_listen_port(),
                "listen_host": self.get_p2p_listen_host(),
                "connection_timeout": self.get_p2p_connection_timeout(),
                "auto_connect_node_groups": self.get_p2p_auto_connect_node_groups(),
                "auto_connect_delay": self.get_p2p_auto_connect_delay(),
            },
            "dht": {
                "enabled": self.get_dht_enabled(),
                "port": self.get_dht_port(),
                "k": self.get_dht_k(),
                "alpha": self.get_dht_alpha(),
                "bootstrap_timeout": self.get_dht_bootstrap_timeout(),
                "lookup_timeout": self.get_dht_lookup_timeout(),
                "bucket_refresh_interval": self.get_dht_bucket_refresh_interval(),
                "announce_interval": self.get_dht_announce_interval(),
                "seed_nodes": self.get('dht', 'seed_nodes', ''),
            },
            "relay": {
                "volunteer": self.get_relay_volunteer(),
                "max_peers": self.get_relay_max_peers(),
                "bandwidth_limit_mbps": self.get_relay_bandwidth_limit(),
            },
            "gossip": {
                "max_hops": self.get_gossip_max_hops(),
                "fanout": self.get_gossip_fanout(),
                "ttl_seconds": self.get_gossip_ttl(),
                "sync_interval": self.get_gossip_sync_interval(),
            },
            "connection": {
                "remote_inference_timeout": self.get_remote_inference_timeout(),
                "reconnect_slow_interval_max_seconds":
                    self.get_reconnect_slow_interval_max_seconds(),
                "hole_punch_timeout": self.get_hole_punch_timeout(),
                "hello_timeout": self.get_hello_timeout(),
                "link_silence_timeout": self.get_link_silence_timeout(),
                "max_pending_hellos_per_ip": self.get_max_pending_hellos_per_ip(),
                "max_proved_connections_per_ip": self.get_max_proved_connections_per_ip(),
                "enable_hole_punching": self.get_enable_hole_punching(),
            },
            "hole_punch": {
                "udp_punch_port": self.get_hole_punch_port(),
                "dtls_handshake_timeout": self.get_hole_punch_dtls_handshake_timeout(),
            },
            "turn": {
                "username": self.get('turn', 'username', ''),
                "credential_set": bool(self._config.get(
                    'turn', 'credential', fallback='').strip()),
                "servers": self.get('turn', 'servers', ''),
                "fallback_servers": self.get('turn', 'fallback_servers', ''),
                "fallback_username": self.get('turn', 'fallback_username', ''),
                "fallback_credential_set": bool(self._config.get(
                    'turn', 'fallback_credential', fallback='').strip()),
            },
            "webrtc": {
                "stun_servers": self.get('webrtc', 'stun_servers', ''),
            },
            "hub": {
                "url": self.get_hub_url(),
                "auto_connect": self.get_hub_auto_connect(),
            },
            "oauth": {
                "callback_host": self.get_oauth_callback_host(),
                "callback_port": self.get_oauth_callback_port(),
                "default_provider": self.get_oauth_default_provider(),
            },
            "gateway": {
                "enabled": self.get_gateway_enabled(),
                "port": self.get_gateway_port(),
            },
            "file_transfer": {
                "chunk_size": self.get('file_transfer', 'chunk_size', 'auto'),
                "chunk_delay": float(self.get('file_transfer', 'chunk_delay', '0')),
                "max_concurrent_transfers": int(self.get(
                    'file_transfer', 'max_concurrent_transfers', '3')),
                "verify_hash": self.get(
                    'file_transfer', 'verify_hash', 'true').lower() in ('true', '1', 'yes'),
            },
        }

    def set_network_settings(
        self, p2p: Optional[Dict[str, Any]] = None,
        dht: Optional[Dict[str, Any]] = None,
        relay: Optional[Dict[str, Any]] = None,
        gossip: Optional[Dict[str, Any]] = None,
        connection: Optional[Dict[str, Any]] = None,
        hole_punch: Optional[Dict[str, Any]] = None,
        turn: Optional[Dict[str, Any]] = None,
        webrtc: Optional[Dict[str, Any]] = None,
        hub: Optional[Dict[str, Any]] = None,
        oauth: Optional[Dict[str, Any]] = None,
        gateway: Optional[Dict[str, Any]] = None,
        file_transfer: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write the Network form's keys; a key left out keeps its stored
        value, and every value is validated before anything is written.

        p2p.listen_port has no writer: the port is bound when the backend
        starts and is not changed from the UI (the owner's decision), and
        gateway.host is not a setting a person can change (the gateway refuses
        any value but 127.0.0.1, ADR-041 D1) — the form's service refuses both
        before this method is reached, and neither has a row here.

        The two TURN credentials follow the bot-token contract: left out or
        empty *keeps* the stored one, only a non-empty string sets it, and
        only `clear_credential` / `clear_fallback_credential` (with no new
        value in the same section) removes it.

        ValueError, one sentence naming the key, and nothing written, unless
        each given value is one its reader knows: the switches must be a real
        bool; the ports (dht.port, hole_punch.udp_punch_port,
        oauth.callback_port, gateway.port) whole numbers 1..65535; the counts
        (dht.k, dht.alpha, the two per-ip ceilings, relay.max_peers, the
        gossip hops/fanout/ttl/sync and file_transfer.max_concurrent_transfers)
        whole numbers of 1 or more; the timeouts numbers of seconds greater
        than 0; the zeros that are honest choices (p2p.auto_connect_delay,
        file_transfer.chunk_delay and the two MB thresholds, and
        relay.bandwidth_limit_mbps as a figure of Mbps) numbers of 0 or more;
        p2p.listen_host one of the three bind hosts p2p_manager.start_server
        documents (dual, 0.0.0.0, ::); oauth.default_provider google or
        github; hub.url an http:// or https:// address (HubClient hands it to
        httpx as a base_url, which refuses anything else at construction);
        oauth.callback_host a host name or address without spaces; the ICE
        server lists (turn.servers, turn.fallback_servers,
        webrtc.stun_servers) comma-separated stun:/turn:/turns: addresses;
        dht.seed_nodes comma-separated host:port entries; and
        file_transfer.chunk_size auto or a size in bytes (64KiB, 1MiB,
        1048576) as chunk_window's parser reads it."""
        # (section, key) -> the validator that answers config.ini text.
        seconds_more_than_zero = (
            ('p2p', 'connection_timeout'),
            ('connection', 'remote_inference_timeout'),
            ('connection', 'reconnect_slow_interval_max_seconds'),
            ('connection', 'hole_punch_timeout'),
            ('connection', 'hello_timeout'),
            ('connection', 'link_silence_timeout'),
            ('hole_punch', 'dtls_handshake_timeout'),
            ('dht', 'bootstrap_timeout'),
            ('dht', 'lookup_timeout'),
            ('dht', 'bucket_refresh_interval'),
            ('dht', 'announce_interval'),
        )
        seconds_zero_or_more = (
            ('p2p', 'auto_connect_delay'),
            ('file_transfer', 'chunk_delay'),
        )
        figures_zero_or_more = (
            ('relay', 'bandwidth_limit_mbps'),
        )
        counts_one_or_more = (
            ('dht', 'k'), ('dht', 'alpha'),
            ('connection', 'max_pending_hellos_per_ip'),
            ('connection', 'max_proved_connections_per_ip'),
            ('relay', 'max_peers'),
            ('gossip', 'max_hops'), ('gossip', 'fanout'),
            ('gossip', 'ttl_seconds'), ('gossip', 'sync_interval'),
            ('file_transfer', 'max_concurrent_transfers'),
        )
        ports = (
            ('dht', 'port'), ('hole_punch', 'udp_punch_port'),
            ('oauth', 'callback_port'), ('gateway', 'port'),
        )
        switches = (
            ('p2p', 'auto_connect_node_groups'),
            ('dht', 'enabled'),
            ('relay', 'volunteer'),
            ('connection', 'enable_hole_punching'),
            ('hub', 'auto_connect'),
            ('gateway', 'enabled'),
            ('file_transfer', 'verify_hash'),
        )
        given = {
            'p2p': p2p, 'dht': dht, 'relay': relay, 'gossip': gossip,
            'connection': connection, 'hole_punch': hole_punch, 'turn': turn,
            'webrtc': webrtc, 'hub': hub, 'oauth': oauth,
            'gateway': gateway, 'file_transfer': file_transfer,
        }
        rows: Dict[tuple, Any] = {}
        for section_name, section_given in given.items():
            if section_given is None:
                continue
            for key, value in section_given.items():
                rows[(section_name, key)] = value

        writes: List[tuple] = []
        # The credential keys follow the token contract and are handled after
        # the plain rows, so the first pass skips them and their clear flags.
        token_contract = {('turn', 'credential'), ('turn', 'clear_credential'),
                          ('turn', 'fallback_credential'),
                          ('turn', 'clear_fallback_credential')}
        for (section, key), value in rows.items():
            if (section, key) in token_contract:
                continue
            if (section, key) in ports:
                if (isinstance(value, bool) or not isinstance(value, int)
                        or not 1 <= value <= 65535):
                    raise ValueError(f"{section}.{key} must be a whole number "
                                     f"from 1 to 65535.")
                writes.append((section, key, str(value)))
            elif (section, key) in counts_one_or_more:
                if (isinstance(value, bool) or not isinstance(value, int)
                        or value < 1):
                    raise ValueError(f"{section}.{key} must be a whole number "
                                     f"of 1 or more.")
                writes.append((section, key, str(value)))
            elif (section, key) in figures_zero_or_more:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or value < 0):
                    raise ValueError(f"{section}.{key} must be a number, "
                                     f"0 or more.")
                writes.append((section, key, _figure_text(value)))
            elif (section, key) in seconds_zero_or_more:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or value < 0):
                    raise ValueError(f"{section}.{key} must be a number of "
                                     f"seconds, 0 or more.")
                writes.append((section, key, _figure_text(value)))
            elif (section, key) in seconds_more_than_zero:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or value <= 0):
                    raise ValueError(f"{section}.{key} must be a number of "
                                     f"seconds, more than 0.")
                writes.append((section, key, _figure_text(value)))
            elif (section, key) in switches:
                if not isinstance(value, bool):
                    raise ValueError(f"{section}.{key} must be true or false.")
                writes.append((section, key, 'true' if value else 'false'))
            elif (section, key) == ('p2p', 'listen_host'):
                if value not in ('dual', '0.0.0.0', '::'):
                    raise ValueError("p2p.listen_host must be one of: "
                                     "dual, 0.0.0.0, ::.")
                writes.append((section, key, value))
            elif (section, key) == ('oauth', 'default_provider'):
                if value not in ('google', 'github'):
                    raise ValueError("oauth.default_provider must be "
                                     "google or github.")
                writes.append((section, key, value))
            elif (section, key) == ('hub', 'url'):
                parsed = urlparse(value if isinstance(value, str) else '')
                if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                    raise ValueError("hub.url must be an http:// or https:// "
                                     "address.")
                writes.append((section, key, value.strip().rstrip('/')))
            elif (section, key) == ('oauth', 'callback_host'):
                if not isinstance(value, str) or not value.strip() or len(value.split()) > 1:
                    raise ValueError("oauth.callback_host must be a host name "
                                     "or address without spaces.")
                writes.append((section, key, value.strip()))
            elif (section, key) in (('turn', 'servers'),
                                    ('turn', 'fallback_servers'),
                                    ('webrtc', 'stun_servers')):
                writes.append((section, key, _ice_urls_text(section, key, value)))
            elif (section, key) == ('dht', 'seed_nodes'):
                writes.append((section, key, _seed_nodes_text(value)))
            elif (section, key) == ('file_transfer', 'chunk_size'):
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise ValueError("file_transfer.chunk_size must be auto "
                                     "or a size in bytes (64KiB, 1MiB, 1048576).")
                text = str(value).strip()
                if text == 'auto' or _CHUNK_SIZE_RE.match(text):
                    writes.append((section, key, text))
                else:
                    raise ValueError("file_transfer.chunk_size must be auto "
                                     "or a size in bytes (64KiB, 1MiB, 1048576).")
            elif (section, key) in (('turn', 'username'),
                                    ('turn', 'fallback_username')):
                if not isinstance(value, str):
                    raise ValueError(f"{section}.{key} must be a string.")
                writes.append((section, key, value.strip()))
            else:
                raise ValueError(f"{section}.{key} is not a key "
                                 f"the Network form writes.")
        for key in ('credential', 'fallback_credential'):
            value = rows.get(('turn', key))
            clearing = rows.get(('turn', f'clear_{key}'), False)
            if value is None and not clearing:
                continue  # neither set nor cleared: the token-contract pass skips it
            if not isinstance(clearing, bool):
                raise ValueError(f"turn.clear_{key} must be true or false.")
            if value is not None and not isinstance(value, str):
                raise ValueError(f"turn.{key} must be a string.")
            stripped = value.strip() if isinstance(value, str) else ''
            if clearing and stripped:
                raise ValueError(f"turn.{key} cannot be set and cleared "
                                 f"in one save.")
            if clearing:
                writes.append(('turn', key, ''))
            elif stripped:
                writes.append(('turn', key, stripped))
        for section, key, text in writes:
            self.set(section, key, text)

    def save_config(self):
        """Save configuration to file."""
        with open(self.config_file, 'w') as f:
            self._config.write(f)

    def reload(self):
        """Reload configuration from file.

        `read()` merges rather than replaces: a key **deleted** from the file on
        disk keeps whatever this object already had for it. Every caller today
        reloads after a value changed — the provider-alias cascade rewrites names
        in place — so the merge is harmless. It stops being harmless the day
        something removes a key and expects the reload to forget it; that needs a
        fresh ConfigParser, not this call (Johnny, 2026-08-23 review of the alias
        inventory).
        """
        if self.config_file.exists():
            self._config.read(self.config_file)


# Self-test
if __name__ == "__main__":
    import tempfile

    # Test in temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(Path(tmpdir))

        print("Testing Settings module...")
        print(f"Hub URL: {settings.get_hub_url()}")
        print(f"OAuth callback port: {settings.get_oauth_callback_port()}")
        print(f"P2P listen port: {settings.get_p2p_listen_port()}")

        # Test environment variable override
        os.environ['DPC_HUB_URL'] = 'https://example.com'
        print(f"Hub URL (with env override): {settings.get_hub_url()}")

        # Test set
        settings.set('hub', 'url', 'https://production.example.com')
        settings.reload()
        del os.environ['DPC_HUB_URL']
        print(f"Hub URL (after set): {settings.get_hub_url()}")

        print("[PASS] All tests passed!")
