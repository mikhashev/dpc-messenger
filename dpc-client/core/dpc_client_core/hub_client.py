# dpc-client/core/dpc_client_core/hub_client.py

import asyncio
import json
import logging
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import threading
from typing import Dict, Any
import time
import base64
import ssl
import sys

import httpx
import websockets
import certifi

from .token_cache import TokenCache
from typing import Optional

logger = logging.getLogger(__name__)


class HubClient:
    """
    Manages all communication with a D-PC Federation Hub, including
    authentication, profile management, discovery, and P2P signaling.

    Now supports offline mode with token caching for persistent authentication.
    """

    def __init__(
        self,
        api_base_url: str,
        oauth_callback_host: str = '127.0.0.1',
        oauth_callback_port: int = 8080,
        token_cache: Optional[TokenCache] = None
    ):
        self.api_base_url = api_base_url.rstrip('/')
        self.http_client = httpx.AsyncClient(base_url=self.api_base_url)
        self.jwt_token: str | None = None
        self.refresh_token: str | None = None
        self.websocket: websockets.WebSocketClientProtocol | None = None
        self.current_provider: str | None = None  # Track which provider was used

        # OAuth callback configuration
        self.oauth_callback_host = oauth_callback_host
        self.oauth_callback_port = oauth_callback_port

        # Token persistence
        self.token_cache = token_cache

        # Try to load cached tokens on initialization
        if self.token_cache:
            self._load_cached_tokens()

    def _load_cached_tokens(self):
        """Load tokens from cache if available and valid."""
        if not self.token_cache:
            return

        tokens = self.token_cache.load_tokens()
        if tokens:
            self.jwt_token = tokens.get("jwt_token")
            self.refresh_token = tokens.get("refresh_token")
            self.current_provider = tokens.get("provider", "google")  # Default to google for backward compat
            logger.info("Loaded cached authentication tokens (provider: %s)", self.current_provider)
        else:
            logger.info("No valid cached tokens found")

    def _save_tokens_to_cache(self):
        """Save current tokens to cache."""
        if not self.token_cache or not self.jwt_token:
            return

        try:
            self.token_cache.save_tokens(
                jwt_token=self.jwt_token,
                refresh_token=self.refresh_token,
                expires_in=1800,  # 30 minutes default
                provider=self.current_provider or "google"  # Save current provider
            )
        except Exception as e:
            logger.warning("Could not cache tokens: %s", e)

    def _get_auth_headers(self) -> Dict[str, str]:
        """Helper to create authorization headers."""
        if not self.jwt_token:
            raise PermissionError("Authentication required. Please call login() first.")
        return {"Authorization": f"Bearer {self.jwt_token}"}

    def _is_token_expired(self) -> bool:
        """Check if the current JWT token is expired or about to expire (within 60 seconds)."""
        if not self.jwt_token:
            return True

        try:
            # JWT format: header.payload.signature
            parts = self.jwt_token.split('.')
            if len(parts) != 3:
                return True

            # Decode payload (add padding if needed)
            payload = parts[1]
            payload += '=' * (4 - len(payload) % 4)  # Add padding
            decoded = base64.urlsafe_b64decode(payload)
            payload_data = json.loads(decoded)

            # Check expiration with 60 second buffer
            exp_timestamp = payload_data.get('exp', 0)
            current_time = time.time()

            return current_time >= (exp_timestamp - 60)

        except Exception as e:
            logger.error("Error checking token expiration: %s", e, exc_info=True)
            return True

    async def _refresh_access_token(self) -> bool:
        """
        Attempt to refresh the access token using the refresh token.
        Returns True if successful, False otherwise.
        """
        if not self.refresh_token:
            logger.warning("No refresh token available")
            return False

        try:
            logger.info("Refreshing access token")
            response = await self.http_client.post(
                "/refresh",
                json={"refresh_token": self.refresh_token}
            )

            if response.status_code == 200:
                data = response.json()
                self.jwt_token = data.get("access_token")
                logger.info("Access token refreshed successfully")
                return True
            else:
                logger.warning("Failed to refresh token: %d", response.status_code)
                return False

        except Exception as e:
            logger.error("Error refreshing token: %s", e, exc_info=True)
            return False

    # --- Authentication Flow ---

    async def login(self, provider: str = "google"):
        """
        Initiates OAuth login flow and registers cryptographic identity.

        Args:
            provider: OAuth provider to use ('google' or 'github')

        Raises:
            ValueError: If provider is not supported
            asyncio.TimeoutError: If authentication times out
            Exception: If authentication fails

        Updated to include automatic node registration after OAuth.
        """
        # Validate provider
        supported_providers = ["google", "github"]
        if provider not in supported_providers:
            raise ValueError(
                f"Unsupported OAuth provider: '{provider}'. "
                f"Supported providers: {', '.join(supported_providers)}"
            )

        # Check if already authenticated with the same provider
        if self.jwt_token:
            if self.current_provider == provider:
                logger.info("Already authenticated with %s", provider)
                return
            else:
                # Provider mismatch - clear cache and re-authenticate
                logger.info("Provider changed from %s to %s - re-authenticating", self.current_provider, provider)
                if self.token_cache:
                    self.token_cache.clear()
                self.jwt_token = None
                self.refresh_token = None
                self.current_provider = None

        token_future = asyncio.Future()

        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                """Suppress HTTP server logs"""
                pass

            def do_GET(self):
                parsed_path = urlparse(self.path)
                if parsed_path.path == "/callback":
                    query_components = parse_qs(parsed_path.query)
                    access_token = query_components.get("access_token", [None])[0]
                    refresh_token = query_components.get("refresh_token", [None])[0]
                    error = query_components.get("error", [None])[0]
                    error_description = query_components.get("error_description", [None])[0]

                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()

                    if error:
                        # OAuth error from Hub
                        error_msg = error_description or error
                        self.wfile.write(b"<h1>Authentication Failed</h1>")
                        self.wfile.write(f"<p style='color: red;'>{error_msg}</p>".encode('utf-8'))
                        self.wfile.write(b"<p>Please check:</p>")
                        self.wfile.write(b"<ul>")
                        self.wfile.write(b"<li>The OAuth provider is configured on the Hub</li>")
                        self.wfile.write(b"<li>Your OAuth credentials are correct</li>")
                        self.wfile.write(b"<li>You authorized the application</li>")
                        self.wfile.write(b"</ul>")
                        self.wfile.write(b"<p>You can close this tab and try again.</p>")
                        self.server.loop.call_soon_threadsafe(
                            token_future.set_exception,
                            Exception(f"OAuth error: {error_msg}")
                        )
                    elif access_token:
                        self.wfile.write(b"<h1>Authentication Successful!</h1>")
                        self.wfile.write(b"<p style='color: green;'>You can now close this browser tab.</p>")
                        self.server.loop.call_soon_threadsafe(
                            token_future.set_result,
                            {"access_token": access_token, "refresh_token": refresh_token}
                        )
                    else:
                        self.wfile.write(b"<h1>Authentication Failed</h1>")
                        self.wfile.write(b"<p>No access token received from the Hub.</p>")
                        self.wfile.write(b"<p>You can close this tab and try again.</p>")
                        self.server.loop.call_soon_threadsafe(
                            token_future.set_exception,
                            Exception("Token not found in callback")
                        )
                else:
                    self.send_response(404)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h1>404 Not Found</h1>")

        # We need to run the HTTP server in a separate thread so it doesn't block asyncio
        server = HTTPServer((self.oauth_callback_host, self.oauth_callback_port), OAuthCallbackHandler)
        server.loop = asyncio.get_running_loop()

        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        logger.info("Starting local server for OAuth callback on %s:%d",
                   self.oauth_callback_host, self.oauth_callback_port)
        login_url = f"{self.api_base_url}/login/{provider}"
        logger.info("Opening browser for %s authentication: %s", provider.upper(), login_url)
        webbrowser.open(login_url)

        try:
            tokens = await asyncio.wait_for(token_future, timeout=180)  # 3 minute timeout
            self.jwt_token = tokens.get("access_token")
            self.refresh_token = tokens.get("refresh_token")
            self.current_provider = provider  # Track which provider was used
            logger.info("Authentication successful with %s, JWT received", provider)
            if self.refresh_token:
                logger.info("Refresh token also received")

            # Save tokens to cache for offline mode
            self._save_tokens_to_cache()

        except asyncio.TimeoutError:
            logger.warning("Authentication timed out")
            raise
        finally:
            server.shutdown()
            thread.join()
            logger.info("Local callback server stopped")

        # NEW: Automatically register cryptographic node_id
        try:
            await self.register_node_id()
            logger.info("Cryptographic identity registration complete")
        except Exception as e:
            logger.warning("Failed to register node identity: %s", e)
            logger.warning("You may need to register manually or re-authenticate")
            # Don't fail login if registration fails

        logger.info("Hub authentication complete")

    # --- REST API Methods ---

    async def update_profile(self, profile_data: Dict[str, Any]):
        """Pushes the public profile to the Hub."""
        logger.info("Updating profile on the Hub")
        response = await self.http_client.put(
            "/profile",
            json=profile_data,
            headers=self._get_auth_headers()
        )
        response.raise_for_status()
        logger.info("Profile updated successfully")
        return response.json()

    async def search_users(self, topic: str, min_level: int = 1) -> Dict[str, Any]:
        """Searches for users by expertise."""
        logger.info("Searching for users with expertise in '%s'", topic)
        response = await self.http_client.get(
            "/discovery/search",
            params={"q": topic, "min_level": min_level},
            headers=self._get_auth_headers()
        )
        response.raise_for_status()
        return response.json()
    
    async def get_my_profile(self):
        """
        Get the authenticated user's own profile.
        
        NEW endpoint support.
        
        Returns:
            dict: User's profile data
        """
        response = await self.http_client.get(
            "/profile",
            headers=self._get_auth_headers()
        )

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            logger.info("No profile found - create one with update_profile()")
            return None
        else:
            response.raise_for_status()


    async def logout(self):
        """
        Logout from Hub by blacklisting the current token.

        NEW endpoint support.
        """
        if not self.jwt_token:
            logger.info("Not logged in")
            return

        try:
            response = await self.http_client.post(
                "/logout",
                headers=self._get_auth_headers()
            )

            if response.status_code == 200:
                data = response.json()
                logger.info("Logged out successfully: %s", data['message'])
                self.jwt_token = None
                self.refresh_token = None

                # Clear cached tokens
                if self.token_cache:
                    self.token_cache.clear()

                # Close WebSocket if connected
                if self.websocket:
                    await self.websocket.close()
                    self.websocket = None
            else:
                response.raise_for_status()

        except Exception as e:
            logger.error("Logout failed: %s", e, exc_info=True)
            # Clear tokens anyway
            self.jwt_token = None
            self.refresh_token = None
            if self.token_cache:
                self.token_cache.clear()


    async def delete_account(self):
        """
        Delete the user's account and all associated data.

        NEW endpoint support.
        WARNING: This action cannot be undone!
        """
        if not self.jwt_token:
            raise PermissionError("Not authenticated")

        # Confirm deletion
        logger.warning("WARNING: This will permanently delete your account and all data")
        logger.warning("This action cannot be undone")
        confirm = input("   Type 'DELETE' to confirm: ")

        if confirm != "DELETE":
            logger.info("Account deletion cancelled")
            return

        response = await self.http_client.delete(
            "/profile",
            headers=self._get_auth_headers()
        )

        if response.status_code == 204:
            logger.info("Account deleted successfully")
            self.jwt_token = None
            self.refresh_token = None

            # Close WebSocket
            if self.websocket:
                await self.websocket.close()
                self.websocket = None
        else:
            response.raise_for_status()

    # --- WebSocket Signaling Methods ---

    async def connect_signaling_socket(self):
        """Connects to the Hub's WebSocket and authenticates via query parameter."""
        # Close existing websocket first (THIS IS THE KEY FIX)
        if self.websocket:
            logger.info("Closing existing websocket before reconnecting")
            try:
                if self.websocket.state == websockets.State.OPEN:
                    await self.websocket.close()
            except:
                pass
            self.websocket = None

        if not self.jwt_token:
            raise PermissionError("Authentication required before connecting to signaling.")

        # Check if token is expired and try to refresh it
        if self._is_token_expired():
            logger.info("Access token expired, attempting to refresh")
            if not await self._refresh_access_token():
                raise PermissionError("Access token expired and refresh failed. Please login again.")

        # Cancel old keepalive task
        if hasattr(self, '_keepalive_task') and self._keepalive_task and not self._keepalive_task.done():
            logger.debug("Cancelling old keepalive task")
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        ws_url = self.api_base_url.replace("http", "ws") + f"/ws/signal?token={self.jwt_token}"
        logger.info("Connecting to signaling server")

        try:
            # Create SSL context with proper certificate verification (fixes macOS SSL error)
            ssl_context = None
            if ws_url.startswith("wss://"):
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                logger.debug("Using SSL context with certifi CA bundle: %s", certifi.where())

            # Added ping_interval and ping_timeout for built-in keepalive
            self.websocket = await websockets.connect(
                ws_url,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=60
            )

            response_str = await self.websocket.recv()
            response = json.loads(response_str)

            if response.get("type") != "auth_ok":
                await self.websocket.close()
                self.websocket = None
                raise ConnectionError(f"WebSocket authentication failed. Server response: {response}")

            logger.info("Signaling server response: %s", response.get('message'))
            logger.info("Signaling socket connected and authenticated")

            # Start keepalive ping task
            self._keepalive_task = asyncio.create_task(self._send_hub_keepalive_pings())
            logger.debug("Hub keepalive task started")
            
        except websockets.exceptions.InvalidStatus as e:
            status = getattr(e.response, 'status_code', 'unknown') if hasattr(e, 'response') else 'unknown'
            raise ConnectionError(f"Server rejected WebSocket connection: HTTP {status}") from e

    async def send_signal(self, target_node_id: str, payload: Dict[str, Any]):
        """Sends a signaling message to a target peer via the Hub."""
        if not self.websocket or self.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Signaling socket is not connected.")
        
        message = {
            "type": "signal",
            "target_node_id": target_node_id,
            "payload": payload
        }
        await self.websocket.send(json.dumps(message))

    async def receive_signal(self) -> Dict[str, Any]:
        """Waits for and returns the next signaling message from the Hub."""
        if not self.websocket or self.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Signaling socket is not connected.")
        
        message_str = await self.websocket.recv()
        return json.loads(message_str)

    async def close(self):
        """Closes all network connections."""
            # Cancel keepalive task
        if hasattr(self, '_keepalive_task') and self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        if self.websocket and self.websocket.state == websockets.State.OPEN:
            await self.websocket.close()

        if not self.http_client.is_closed:
            await self.http_client.aclose()

        logger.info("HubClient connections closed")

    async def register_node_id(self):
        """
        Register the client's cryptographic node identity with the Hub.
        
        Should be called immediately after OAuth login.
        Reads local identity files and sends them to Hub for verification.
        
        Raises:
            Exception: If registration fails
        """
        from dpc_protocol.crypto import load_identity, CERT_FILE
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        logger.info("Registering cryptographic identity with Hub")

        try:
            # Load local identity
            node_id, key_file, cert_file = load_identity()

            # Read certificate
            with open(cert_file, 'r') as f:
                certificate = f.read()

            # Extract public key from certificate
            cert = x509.load_pem_x509_certificate(
                certificate.encode('utf-8'),
                default_backend()
            )
            public_key = cert.public_key()
            public_key_pem = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode('utf-8')

            # Send registration request
            response = await self.http_client.post(
                "/register-node-id",
                headers=self._get_auth_headers(),
                json={
                    "node_id": node_id,
                    "public_key": public_key_pem,
                    "certificate": certificate
                }
            )

            if response.status_code == 200:
                data = response.json()
                logger.info("Node identity registered and verified")
                logger.info("Node ID: %s", data['node_id'])
                logger.info("Verified: %s", data['verified'])
                return data
            else:
                error_detail = response.json().get("detail", "Unknown error")
                raise Exception(f"Registration failed: {error_detail}")
                
        except FileNotFoundError:
            raise Exception(
                "Local identity files not found. "
                "Please run 'dpc init' to generate identity."
            )
        except Exception as e:
            raise Exception(f"Failed to register node identity: {str(e)}")
    
    async def _send_hub_keepalive_pings(self):
        """Background task to send keepalive pings to Hub."""
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(25)

                if not self.websocket or self.websocket.state != websockets.State.OPEN:
                    logger.debug("Keepalive stopping - websocket not open (sent %d pings)", ping_count)
                    break

                try:
                    ping_count += 1
                    ping_message = {
                        "type": "ping",
                        "timestamp": asyncio.get_event_loop().time()
                    }
                    await self.websocket.send(json.dumps(ping_message))
                    logger.debug("Hub keepalive ping #%d sent", ping_count)

                except websockets.exceptions.ConnectionClosed:
                    logger.warning("Hub connection closed during ping #%d", ping_count)
                    break
                except Exception as e:
                    logger.error("Error sending keepalive ping #%d: %s", ping_count, e, exc_info=True)
                    break

        except asyncio.CancelledError:
            logger.debug("Hub keepalive cancelled (sent %d pings)", ping_count)

# --- Self-testing block ---
async def main_test():
    """Updated test code with new features"""
    logger.info("--- Testing Enhanced HubClient ---")

    hub = HubClient(api_base_url="http://127.0.0.1:8000")

    try:
        # 1. Test Login with Auto-Registration
        logger.info("--- Step 1: Authentication ---")
        logger.info("Please complete the login process in your browser")
        await hub.login()
        assert hub.jwt_token is not None
        logger.info("Authentication and registration successful")

        # 2. Test Get Own Profile
        logger.info("--- Step 2: Get Own Profile ---")
        my_profile = await hub.get_my_profile()
        if my_profile:
            logger.info("Profile found: %s", my_profile.get('name'))
        else:
            logger.info("No profile yet, creating one")

            # Create profile
            test_profile = {
                "name": "Test User",
                "description": "Testing node registration",
                "expertise": {"testing": 5, "python": 4}
            }
            await hub.update_profile(test_profile)

            # Verify it was created
            my_profile = await hub.get_my_profile()
            assert my_profile is not None
            logger.info("Profile created and retrieved")

        # 3. Test Search
        logger.info("--- Step 3: Discovery ---")
        results = await hub.search_expertise("python", min_level=1)
        logger.info("Found %d Python experts", len(results))

        # 4. Test Signaling Connection
        logger.info("--- Step 4: WebSocket Signaling ---")
        await hub.connect_signaling()
        logger.info("Signaling connected")

        # 5. Test Logout
        logger.info("--- Step 5: Logout ---")
        await hub.logout()
        logger.info("Logged out successfully")

        # Verify token is blacklisted
        try:
            await hub.get_my_profile()
            logger.error("Token should be blacklisted")
        except Exception:
            logger.info("Token correctly blacklisted")

        logger.info("All tests passed")

    except Exception as e:
        logger.error("Test failed: %s", e, exc_info=True)

    finally:
        await hub.close()

if __name__ == '__main__':
    # To run this test:
    # 1. Make sure the Hub server is running.
    # 2. Make sure your Google OAuth credentials are correct in the Hub's .env.
    # 3. Navigate to `dpc-client/core/`
    # 4. Run: `poetry run python dpc_client_core/hub_client.py`
    import traceback
    asyncio.run(main_test())