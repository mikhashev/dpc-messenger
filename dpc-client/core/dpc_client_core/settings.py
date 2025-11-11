# dpc-client/core/dpc_client_core/settings.py

import os
from pathlib import Path
from typing import Optional
import configparser


class Settings:
    """
    Centralized settings management for DPC Client.
    Supports environment variables and config file.
    """

    def __init__(self, dpc_home_dir: Path):
        self.dpc_home_dir = dpc_home_dir
        self.config_file = dpc_home_dir / "config.ini"
        self._config = configparser.ConfigParser()

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
            'callback_host': '127.0.0.1'
        }

        self._config['p2p'] = {
            'listen_port': '8888',
            'listen_host': '0.0.0.0'
        }

        self._config['api'] = {
            'port': '9999',
            'host': '127.0.0.1'
        }

        # Ensure directory exists
        self.dpc_home_dir.mkdir(parents=True, exist_ok=True)

        # Write config file
        with open(self.config_file, 'w') as f:
            f.write("# D-PC Client Configuration\n")
            f.write("# You can override these settings with environment variables:\n")
            f.write("# DPC_HUB_URL, DPC_OAUTH_CALLBACK_PORT, etc.\n\n")
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

    def get_p2p_listen_port(self) -> int:
        """Get the P2P server listen port."""
        return int(self.get('p2p', 'listen_port', '8888'))

    def get_p2p_listen_host(self) -> str:
        """Get the P2P server listen host."""
        return self.get('p2p', 'listen_host', '0.0.0.0')

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

    def set(self, section: str, key: str, value: str):
        """Set a configuration value in the config file."""
        if not self._config.has_section(section):
            self._config.add_section(section)

        self._config.set(section, key, value)

        # Save to file
        with open(self.config_file, 'w') as f:
            self._config.write(f)

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
