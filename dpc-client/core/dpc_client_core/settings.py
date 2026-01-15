# dpc-client/core/dpc_client_core/settings.py

import os
from pathlib import Path
from typing import Optional, Dict
import configparser


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
            self._create_default_config()

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
            'connection_timeout': '30'  # Connection establishment timeout in seconds
        }

        self._config['api'] = {
            'port': '9999',
            'host': '127.0.0.1'
        }

        self._config['turn'] = {
            'username': '',  # Leave empty or set via environment variable DPC_TURN_USERNAME
            'credential': '',  # Leave empty or set via environment variable DPC_TURN_CREDENTIAL
            # TURN server URLs (used only when username/credential are set)
            'servers': 'stun:stun.relay.metered.ca:80,turn:global.relay.metered.ca:80,turn:global.relay.metered.ca:80?transport=tcp,turn:global.relay.metered.ca:443,turns:global.relay.metered.ca:443?transport=tcp',
            # Fallback TURN servers (public, may be unreliable)
            'fallback_servers': 'turn:openrelay.metered.ca:80,turn:openrelay.metered.ca:443,turn:openrelay.metered.ca:443?transport=tcp',
            'fallback_username': 'openrelayproject',
            'fallback_credential': 'openrelayproject'
        }

        self._config['webrtc'] = {
            # STUN servers for NAT traversal (discovering public IP)
            'stun_servers': 'stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302,stun:global.stun.twilio.com:3478,stun:stun.rtc.yandex.net:3478'
        }

        self._config['system'] = {
            'auto_collect_device_info': 'true',  # Automatically collect device/system info for AI context
            'collect_hardware_specs': 'true',    # Collect hardware tiers (RAM, CPU, disk, GPU)
            'collect_dev_tools': 'true',         # Collect installed dev tools and versions
            'collect_ai_models': 'false'         # Collect locally available AI models (opt-in for compute-sharing)
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
            'enable_ipv6': 'true',  # Try IPv6 direct connections (Priority 1)
            'enable_ipv4': 'true',  # Try IPv4 direct connections (Priority 2)
            'enable_hub_webrtc': 'true',  # Try Hub WebRTC with STUN/TURN (Priority 3)
            'enable_hole_punching': 'false',  # Try DHT-coordinated UDP hole punching (Priority 4) - DISABLED: lacks DTLS encryption (v0.10.0)
            'enable_relays': 'true',  # Try volunteer relay nodes (Priority 5)
            'enable_gossip': 'true',  # Use gossip store-and-forward fallback (Priority 6)
            # Timeouts per strategy (seconds) - increased for high-latency networks (mobile, CGNAT)
            'ipv6_timeout': '60',  # Includes 30s pre-flight check + 30s SSL handshake
            'ipv4_timeout': '60',  # Includes 30s pre-flight check + 30s SSL handshake
            'webrtc_timeout': '30',
            'hole_punch_timeout': '15',
            'relay_timeout': '20',
            'gossip_timeout': '5'  # How long to wait before falling back to gossip
        }

        self._config['hole_punch'] = {
            'udp_punch_port': '8890',  # UDP port for hole punching
            'nat_detection_enabled': 'true',  # Detect NAT type (cone vs symmetric)
            'stun_timeout': '5',  # Endpoint discovery timeout (seconds)
            'punch_attempts': '3',  # Number of punch attempts before giving up
            # DTLS encryption settings (v0.10.1+)
            'enable_dtls': 'true',  # Enable DTLS encryption for hole-punched connections
            'dtls_handshake_timeout': '3',  # DTLS handshake timeout (seconds)
            'dtls_version': '1.2'  # DTLS protocol version (1.2 or 1.3)
        }

        self._config['relay'] = {
            # Client mode (use relays for outbound connections)
            'enabled': 'true',  # Enable relay client mode
            'prefer_region': 'global',  # Preferred region: us-west, eu-central, global, etc.
            'cache_timeout': '300',  # Relay discovery cache timeout (5 minutes)
            # Server mode (volunteer as relay for others)
            'volunteer': 'false',  # Volunteer this node as relay (opt-in)
            'max_peers': '10',  # Max concurrent relay sessions (server mode)
            'bandwidth_limit_mbps': '10.0',  # Bandwidth limit for relaying
            'region': 'global'  # Geographic region for relay announcements
        }

        self._config['gossip'] = {
            'enabled': 'true',  # Enable gossip protocol
            'max_hops': '5',  # Maximum hops for message forwarding
            'fanout': '3',  # Number of random peers to forward to
            'ttl_seconds': '86400',  # Message TTL (24 hours)
            'sync_interval': '300',  # Anti-entropy sync interval (5 minutes)
            'cleanup_interval': '600',  # Expired message cleanup interval (10 minutes)
            'priority': 'normal'  # Default message priority: low, normal, high
        }

        self._config['knowledge'] = {
            'token_warning_threshold': '0.8',  # Warn when context window reaches 80%
            'auto_extraction_enabled': 'true',  # Automatically suggest knowledge extraction
            'cultural_perspectives_enabled': 'false'  # Include cultural perspective analysis in knowledge extraction
        }

        self._config['file_transfer'] = {
            'chunk_size': '65536',  # Chunk size in bytes (64KB)
            'background_threshold_mb': '50',  # Background transfer threshold in MB
            'direct_tls_only_threshold_mb': '100',  # Direct TLS preference threshold in MB
            'max_concurrent_transfers': '3',  # Max concurrent file transfers
            'verify_hash': 'true',  # Verify file hash after transfer (SHA256)
            # Preparation timeout configuration (v0.11.2+)
            'preparation_timeout_base': '60',  # Base timeout in seconds (for small files)
            'preparation_timeout_per_gb': '40',  # Additional timeout per GB (40s/GB)
            'preparation_progress_interval_mb': '100',  # Emit progress every N MB during SHA256
            'preparation_progress_interval_chunks': '10000'  # Emit progress every N chunks during CRC32
        }

        self._config['voice_messages'] = {
            'enabled': 'true',  # Enable voice message recording and playback (v0.13.0+)
            'max_duration_seconds': '300',  # Maximum recording duration in seconds (5 minutes)
            'max_size_mb': '10',  # Maximum voice message file size in MB
            'mime_types': 'audio/webm,audio/opus,audio/ogg,audio/mp4,audio/mpeg,audio/wav',  # Supported audio formats (includes WAV for Tauri/Rust backend)
            'default_sample_rate': '48000',  # Default sample rate in Hz (48kHz for quality)
            'default_channels': '1',  # Default audio channels (1 = mono, 2 = stereo)
            'default_codec': 'opus'  # Default audio codec (opus for web compatibility)
        }

        self._config['voice_transcription'] = {
            'enabled': 'true',  # Enable auto-transcription of received voice messages (v0.13.2+)
            'sender_transcribes': 'false',  # Should sender transcribe their own voice messages
            'recipient_delay_seconds': '3',  # Wait N seconds before recipients attempt transcription (coordination)
            'timeout_seconds': '240',  # Max wait time for peer's transcription before trying locally (increased to 240s for cold model loads that take 180+s)
            # NOTE: provider_priority overrides voice_provider from providers.json
            # Provider aliases match HuggingFace model names for clarity (e.g., whisper-large-v3)
            'provider_priority': 'whisper-large-v3,whisper-large-v3-turbo,whisper-medium,whisper-small,openai',  # Comma-separated provider priority (aliases from providers.json)
            'show_transcriber_name': 'false',  # Show who transcribed the message in UI
            'cache_transcriptions': 'true',  # Cache transcriptions in memory
            'fallback_to_openai': 'true'  # Fallback to OpenAI API if local Whisper unavailable
        }

        self._config['local_transcription'] = {
            'enabled': 'true',  # Enable local Whisper transcription (v0.13.1+)
            'model': 'openai/whisper-large-v3',  # Model name (HuggingFace)
            'device': 'auto',  # Device: 'cuda', 'cpu', or 'auto' (auto-detects CUDA)
            'compile_model': 'true',  # Use torch.compile for 4.5x speedup (PyTorch 2.4+)
            'use_flash_attention': 'false',  # Use Flash Attention 2 (requires flash-attn package)
            'chunk_length_s': '30',  # Chunk length for long-form transcription (speed vs accuracy)
            'batch_size': '16',  # Batch size for chunked transcription (higher = faster, more VRAM)
            'language': 'auto',  # Language: 'auto' (detect) or ISO 639-1 code (e.g., 'en', 'es')
            'task': 'transcribe',  # Task: 'transcribe' or 'translate' (to English)
            'fallback_to_openai': 'true',  # Fallback to OpenAI API if local fails
            'max_file_size_mb': '25',  # Max audio file size for local transcription (VRAM limit)
            'lazy_loading': 'true'  # Load model on first use (faster startup)
        }

        self._config['vision'] = {
            'enabled': 'true',  # Enable vision API features (screenshot paste, image analysis)
            'default_provider': 'openai',  # Default AI provider for vision: 'openai' or 'anthropic'
            'max_image_size_mb': '5',  # Maximum image size in MB (clipboard paste and uploads)
            'thumbnail_quality': '85'  # Thumbnail JPEG quality (0-100)
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
            'conversation_links': '{}'  # JSON map of telegram_chat_id -> conversation_id
        }
        # NOTE: bridge_to_p2p currently forwards as N separate 1:1 messages (v0.15.0).
        # Future: Will support group chat bridging (single message to DPC group).
        # See telegram_coordinator.py:_forward_to_p2p_peers() for implementation details.

        self._config['logging'] = {
            'level': 'INFO',  # Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
            'console': 'true',  # Enable console output
            'console_level': 'INFO',  # Console log level (can differ from file)
            'file': '~/.dpc/logs/dpc-client.log',  # Log file path
            'max_bytes': '10485760',  # Max bytes per log file before rotation (10MB)
            'backup_count': '5'  # Number of backup log files to keep
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
        return self.get('p2p', 'listen_host', '0.0.0.0')

    def get_p2p_connection_timeout(self) -> float:
        """Get the P2P connection establishment timeout in seconds."""
        return float(self.get('p2p', 'connection_timeout', '60'))

    def get_api_port(self) -> int:
        """Get the local API server port."""
        return int(self.get('api', 'port', '9999'))

    def get_api_host(self) -> str:
        """Get the local API server host."""
        return self.get('api', 'host', '127.0.0.1')

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

    def get_token_warning_threshold(self) -> float:
        """Get the token warning threshold (0.0-1.0)."""
        value = self.get('knowledge', 'token_warning_threshold', '0.8')
        try:
            threshold = float(value)
            # Clamp to valid range
            return max(0.0, min(1.0, threshold))
        except ValueError:
            return 0.8  # Default to 80%

    def get_auto_extraction_enabled(self) -> bool:
        """Check if automatic knowledge extraction is enabled."""
        value = self.get('knowledge', 'auto_extraction_enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_cultural_perspectives_enabled(self) -> bool:
        """Check if cultural perspective analysis is enabled in knowledge extraction."""
        value = self.get('knowledge', 'cultural_perspectives_enabled', 'false')
        return value.lower() in ('true', '1', 'yes')

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
        key = f'{strategy}_timeout'
        return float(self.get('connection', key, '30'))

    def get_hole_punch_port(self) -> int:
        """Get UDP port for hole punching."""
        return int(self.get('hole_punch', 'udp_punch_port', '8890'))

    def get_hole_punch_nat_detection_enabled(self) -> bool:
        """Check if NAT type detection is enabled."""
        value = self.get('hole_punch', 'nat_detection_enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_hole_punch_stun_timeout(self) -> float:
        """Get STUN endpoint discovery timeout in seconds."""
        return float(self.get('hole_punch', 'stun_timeout', '5'))

    def get_hole_punch_attempts(self) -> int:
        """Get number of hole punch attempts before giving up."""
        return int(self.get('hole_punch', 'punch_attempts', '3'))

    def get_hole_punch_dtls_enabled(self) -> bool:
        """Check if DTLS encryption is enabled for hole-punched connections."""
        value = self.get('hole_punch', 'enable_dtls', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_hole_punch_dtls_handshake_timeout(self) -> float:
        """Get DTLS handshake timeout in seconds."""
        return float(self.get('hole_punch', 'dtls_handshake_timeout', '3'))

    def get_hole_punch_dtls_version(self) -> str:
        """Get DTLS protocol version (1.2 or 1.3)."""
        return self.get('hole_punch', 'dtls_version', '1.2')

    def get_relay_enabled(self) -> bool:
        """Check if relay client mode is enabled."""
        value = self.get('relay', 'enabled', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_relay_prefer_region(self) -> str:
        """Get preferred relay region."""
        return self.get('relay', 'prefer_region', 'global')

    def get_relay_cache_timeout(self) -> int:
        """Get relay discovery cache timeout in seconds."""
        return int(self.get('relay', 'cache_timeout', '300'))

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

    def get_relay_region(self) -> str:
        """Get geographic region for relay announcements."""
        return self.get('relay', 'region', 'global')

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

    def get_gossip_cleanup_interval(self) -> int:
        """Get expired message cleanup interval in seconds."""
        return int(self.get('gossip', 'cleanup_interval', '600'))

    def get_gossip_priority(self) -> str:
        """Get default gossip message priority."""
        return self.get('gossip', 'priority', 'normal')

    # Convenience methods for connection strategies
    def get_enable_hole_punching(self) -> bool:
        """Get whether UDP hole punching is enabled."""
        return self.get_connection_strategy_enabled('hole_punching')

    def get_hole_punch_timeout(self) -> float:
        """Get hole punch connection timeout."""
        return self.get_connection_timeout('hole_punch')

    # Vision API settings (Phase 2.6: Screenshot + Vision Integration)
    def get_vision_enabled(self) -> bool:
        """Get whether vision API features are enabled."""
        return self.get('vision', 'enabled', 'true').lower() == 'true'

    def get_vision_default_provider(self) -> str:
        """Get default AI provider for vision analysis ('openai' or 'anthropic')."""
        return self.get('vision', 'default_provider', 'openai')

    def get_vision_max_image_size_mb(self) -> int:
        """Get maximum image size in MB for clipboard paste and uploads."""
        return int(self.get('vision', 'max_image_size_mb', '5'))

    def get_vision_thumbnail_quality(self) -> int:
        """Get thumbnail JPEG quality (0-100)."""
        return int(self.get('vision', 'thumbnail_quality', '85'))

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
        # Fallback matches default config (aliases match HuggingFace model names)
        priority_str = self.get('voice_transcription', 'provider_priority', 'whisper-large-v3,whisper-large-v3-turbo,whisper-medium,whisper-small,openai')
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

    def get_local_transcription_model(self) -> str:
        """Get the Whisper model name for local transcription."""
        return self.get('local_transcription', 'model', 'openai/whisper-large-v3')

    def get_local_transcription_device(self) -> str:
        """Get the device for local transcription ('cuda', 'cpu', or 'auto')."""
        return self.get('local_transcription', 'device', 'auto')

    def get_local_transcription_compile_model(self) -> bool:
        """Check if torch.compile is enabled for local transcription."""
        value = self.get('local_transcription', 'compile_model', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_local_transcription_use_flash_attention(self) -> bool:
        """Check if Flash Attention 2 is enabled for local transcription."""
        value = self.get('local_transcription', 'use_flash_attention', 'false')
        return value.lower() in ('true', '1', 'yes')

    def get_local_transcription_chunk_length_s(self) -> int:
        """Get chunk length for long-form transcription in seconds."""
        return int(self.get('local_transcription', 'chunk_length_s', '30'))

    def get_local_transcription_batch_size(self) -> int:
        """Get batch size for chunked transcription."""
        return int(self.get('local_transcription', 'batch_size', '16'))

    def get_local_transcription_language(self) -> str:
        """Get language for transcription ('auto' for auto-detect)."""
        return self.get('local_transcription', 'language', 'auto')

    def get_local_transcription_task(self) -> str:
        """Get transcription task ('transcribe' or 'translate')."""
        return self.get('local_transcription', 'task', 'transcribe')

    def get_local_transcription_fallback_to_openai(self) -> bool:
        """Check if fallback to OpenAI API is enabled when local fails."""
        value = self.get('local_transcription', 'fallback_to_openai', 'true')
        return value.lower() in ('true', '1', 'yes')

    def get_local_transcription_max_file_size_mb(self) -> int:
        """Get max file size for local transcription in MB."""
        return int(self.get('local_transcription', 'max_file_size_mb', '25'))

    def get_local_transcription_lazy_loading(self) -> bool:
        """Check if lazy model loading is enabled (load on first use)."""
        value = self.get('local_transcription', 'lazy_loading', 'true')
        return value.lower() in ('true', '1', 'yes')

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

    def reload(self):
        """Reload configuration from file."""
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
