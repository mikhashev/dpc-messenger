# dpc-client/core/dpc_client_core/token_cache.py

import json
import base64
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import hashlib


class TokenCache:
    """
    Securely stores and retrieves authentication tokens with encryption.

    Tokens are encrypted using a key derived from the node's identity
    to prevent unauthorized access if the cache file is compromised.
    """

    def __init__(self, cache_dir: Path, node_key_path: Path):
        """
        Initialize token cache.

        Args:
            cache_dir: Directory to store cache file
            node_key_path: Path to node private key (used for encryption key derivation)
        """
        self.cache_dir = cache_dir
        self.cache_file = cache_dir / "auth_cache.enc"
        self.node_key_path = node_key_path

        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Initialize encryption key
        self._cipher = None
        if self.node_key_path.exists():
            self._initialize_cipher()

    def _initialize_cipher(self):
        """Initialize Fernet cipher using node key as seed."""
        try:
            # Read node private key
            with open(self.node_key_path, 'rb') as f:
                node_key_data = f.read()

            # Derive encryption key from node key
            # Use PBKDF2HMAC to create a Fernet-compatible key
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'dpc_token_cache_salt_v1',  # Static salt (acceptable for this use case)
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(node_key_data))

            self._cipher = Fernet(key)
        except Exception as e:
            print(f"Warning: Could not initialize token cache encryption: {e}")
            self._cipher = None

    def save_tokens(
        self,
        jwt_token: str,
        refresh_token: Optional[str] = None,
        node_id: Optional[str] = None,
        expires_in: int = 1800  # 30 minutes default
    ) -> bool:
        """
        Save tokens to encrypted cache.

        Args:
            jwt_token: JWT access token
            refresh_token: Optional refresh token
            node_id: Node ID associated with tokens
            expires_in: Token lifetime in seconds

        Returns:
            True if saved successfully
        """
        if not self._cipher:
            print("Warning: Token cache encryption not available")
            return False

        try:
            # Calculate expiration time
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

            # Prepare cache data
            cache_data = {
                "jwt_token": jwt_token,
                "refresh_token": refresh_token,
                "node_id": node_id,
                "expires_at": expires_at,
                "cached_at": datetime.utcnow().isoformat()
            }

            # Serialize and encrypt
            json_data = json.dumps(cache_data).encode('utf-8')
            encrypted_data = self._cipher.encrypt(json_data)

            # Write to file
            with open(self.cache_file, 'wb') as f:
                f.write(encrypted_data)

            print(f"[OK] Tokens cached to {self.cache_file}")
            return True

        except Exception as e:
            print(f"Error saving tokens to cache: {e}")
            return False

    def load_tokens(self) -> Optional[Dict[str, Any]]:
        """
        Load tokens from encrypted cache.

        Returns:
            Dictionary with token data if valid, None otherwise
        """
        if not self._cipher:
            return None

        if not self.cache_file.exists():
            return None

        try:
            # Read encrypted data
            with open(self.cache_file, 'rb') as f:
                encrypted_data = f.read()

            # Decrypt and deserialize
            decrypted_data = self._cipher.decrypt(encrypted_data)
            cache_data = json.loads(decrypted_data.decode('utf-8'))

            # Check if token is expired
            expires_at = datetime.fromisoformat(cache_data.get("expires_at", ""))
            if datetime.utcnow() >= expires_at:
                print("Cached tokens expired")
                self.clear()
                return None

            print("[OK] Loaded cached tokens")
            return cache_data

        except Exception as e:
            print(f"Error loading cached tokens: {e}")
            # Clear corrupted cache
            self.clear()
            return None

    def clear(self) -> bool:
        """
        Clear cached tokens.

        Returns:
            True if cleared successfully
        """
        try:
            if self.cache_file.exists():
                self.cache_file.unlink()
                print("[OK] Token cache cleared")
            return True
        except Exception as e:
            print(f"Error clearing token cache: {e}")
            return False

    def is_valid(self) -> bool:
        """
        Check if cached tokens exist and are valid.

        Returns:
            True if valid tokens are cached
        """
        tokens = self.load_tokens()
        return tokens is not None

    def get_jwt_token(self) -> Optional[str]:
        """Get cached JWT token if valid."""
        tokens = self.load_tokens()
        return tokens.get("jwt_token") if tokens else None

    def get_refresh_token(self) -> Optional[str]:
        """Get cached refresh token if valid."""
        tokens = self.load_tokens()
        return tokens.get("refresh_token") if tokens else None

    def get_node_id(self) -> Optional[str]:
        """Get node ID from cached tokens."""
        tokens = self.load_tokens()
        return tokens.get("node_id") if tokens else None


# Self-test
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    print("Testing TokenCache...")

    # Create temporary directories
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir) / "cache"
        key_dir = Path(tmpdir) / "keys"
        key_dir.mkdir()

        # Create a dummy node key
        node_key_file = key_dir / "node.key"
        node_key_file.write_bytes(b"dummy_private_key_data_for_testing")

        # Initialize cache
        cache = TokenCache(cache_dir, node_key_file)

        # Test save
        success = cache.save_tokens(
            jwt_token="test_jwt_token_123",
            refresh_token="test_refresh_token_456",
            node_id="dpc-node-test-123",
            expires_in=3600
        )
        assert success, "Failed to save tokens"
        print("[PASS] Save tokens")

        # Test load
        tokens = cache.load_tokens()
        assert tokens is not None, "Failed to load tokens"
        assert tokens["jwt_token"] == "test_jwt_token_123"
        assert tokens["refresh_token"] == "test_refresh_token_456"
        assert tokens["node_id"] == "dpc-node-test-123"
        print("[PASS] Load tokens")

        # Test helper methods
        assert cache.is_valid() == True
        assert cache.get_jwt_token() == "test_jwt_token_123"
        assert cache.get_refresh_token() == "test_refresh_token_456"
        assert cache.get_node_id() == "dpc-node-test-123"
        print("[PASS] Helper methods")

        # Test clear
        cache.clear()
        assert cache.is_valid() == False
        assert cache.get_jwt_token() is None
        print("[PASS] Clear cache")

        # Test expired tokens
        cache.save_tokens(
            jwt_token="expired_token",
            expires_in=-10  # Already expired
        )
        assert cache.is_valid() == False
        print("[PASS] Expired token handling")

    print("\n[PASS] All TokenCache tests passed!")
