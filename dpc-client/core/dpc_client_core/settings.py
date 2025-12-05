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
                    self._migrate_or_recreate_config()
            except configparser.Error as e:
                print(f"Warning: Failed to parse config file: {e}")
                self._migrate_or_recreate_config()
        else:
            self._create_default_config()

    def _migrate_or_recreate_config(self):
        """Migrate old config format or recreate if invalid."""
        # Try to read old hub URL if it exists
        old_hub_url = None
        try:
            with open(self.config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('url =') or line.startswith('url='):
                        old_hub_url = line.split('=', 1)[1].strip()
                        break
        except Exception:
            pass

        # Backup old config
        if self.config_file.exists():
            backup_file = self.config_file.with_suffix('.ini.bak')
            try:
                import shutil
                shutil.copy(self.config_file, backup_file)
                print(f"Backed up old config to {backup_file}")
            except Exception as e:
                print(f"Warning: Could not backup config: {e}")

        # Create new config with old URL if found
        self._create_default_config(hub_url=old_hub_url)

    def _create_default_config(self, hub_url: Optional[str] = None):
        """Create a default config file with common settings."""
        self._config['hub'] = {
            'url': hub_url if hub_url else 'http://localhost:8000',
            'auto_connect': 'true'
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

        self._config['knowledge'] = {
            'token_warning_threshold': '0.8',  # Warn when context window reaches 80%
            'auto_extraction_enabled': 'true',  # Automatically suggest knowledge extraction
            'cultural_perspectives_enabled': 'false'  # Include cultural perspective analysis in knowledge extraction
        }

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
        return float(self.get('p2p', 'connection_timeout', '30'))

    def get_api_port(self) -> int:
        """Get the local API server port."""
        return int(self.get('api', 'port', '9999'))

    def get_api_host(self) -> str:
        """Get the local API server host."""
        return self.get('api', 'host', '127.0.0.1')

    def get_hub_auto_connect(self) -> bool:
        """Check if Hub should auto-connect on startup."""
        value = self.get('hub', 'auto_connect', 'true')
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
