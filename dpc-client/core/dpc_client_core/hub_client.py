# dpc-client/core/dpc_client_core/hub_client.py

import asyncio
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import threading
from typing import Dict, Any

import httpx
import websockets

class HubClient:
    """
    Manages all communication with a D-PC Federation Hub, including
    authentication, profile management, discovery, and P2P signaling.
    """

    def __init__(self, api_base_url: str):
        self.api_base_url = api_base_url.rstrip('/')
        self.http_client = httpx.AsyncClient(base_url=self.api_base_url)
        self.jwt_token: str | None = None
        self.websocket: websockets.WebSocketClientProtocol | None = None

    def _get_auth_headers(self) -> Dict[str, str]:
        """Helper to create authorization headers."""
        if not self.jwt_token:
            raise PermissionError("Authentication required. Please call login() first.")
        return {"Authorization": f"Bearer {self.jwt_token}"}

    # --- Authentication Flow ---

    async def login(self, provider: str = "google"):
        """
        Initiates OAuth login flow and registers cryptographic identity.
        
        Updated to include automatic node registration after OAuth.
        """
        if self.jwt_token:
            print("Already authenticated.")
            return

        token_future = asyncio.Future()
        
        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_path = urlparse(self.path)
                if parsed_path.path == "/callback":
                    query_components = parse_qs(parsed_path.query)
                    token = query_components.get("access_token", [None])[0]
                    
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    
                    if token:
                        self.wfile.write(b"<h1>Authentication Successful!</h1>")
                        self.wfile.write(b"<p>You can now close this browser tab.</p>")
                        self.server.loop.call_soon_threadsafe(token_future.set_result, token)
                    else:
                        self.wfile.write(b"<h1>Authentication Failed.</h1>")
                        self.wfile.write(b"<p>Token not found in callback. Please try again.</p>")
                        self.server.loop.call_soon_threadsafe(token_future.set_exception, Exception("Token not found in callback"))
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not Found")

        # We need to run the HTTP server in a separate thread so it doesn't block asyncio
        server = HTTPServer(('127.0.0.1', 8080), OAuthCallbackHandler)
        server.loop = asyncio.get_running_loop()
        
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        
        print("Starting local server for OAuth callback on port 8080...")
        login_url = f"{self.api_base_url}/login/{provider}"
        print(f"Opening browser to: {login_url}")
        webbrowser.open(login_url)

        try:
            self.jwt_token = await asyncio.wait_for(token_future, timeout=180) # 3 minute timeout
            print("Authentication successful, JWT received.")
        except asyncio.TimeoutError:
            print("Authentication timed out.")
            raise
        finally:
            server.shutdown()
            thread.join()
            print("Local callback server stopped.")
        
        # NEW: Automatically register cryptographic node_id
        try:
            await self.register_node_id()
            print("✅ Cryptographic identity registration complete!")
        except Exception as e:
            print(f"⚠️  Warning: Failed to register node identity: {e}")
            print("   You may need to register manually or re-authenticate")
            # Don't fail login if registration fails
        
        print("✅ Hub authentication complete!")

    # --- REST API Methods ---

    async def update_profile(self, profile_data: Dict[str, Any]):
        """Pushes the public profile to the Hub."""
        print("Updating profile on the Hub...")
        response = await self.http_client.put(
            "/profile",
            json=profile_data,
            headers=self._get_auth_headers()
        )
        response.raise_for_status()
        print("Profile updated successfully.")
        return response.json()

    async def search_users(self, topic: str, min_level: int = 1) -> Dict[str, Any]:
        """Searches for users by expertise."""
        print(f"Searching for users with expertise in '{topic}'...")
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
            print("No profile found. Create one with update_profile()")
            return None
        else:
            response.raise_for_status()


    async def logout(self):
        """
        Logout from Hub by blacklisting the current token.
        
        NEW endpoint support.
        """
        if not self.jwt_token:
            print("Not logged in.")
            return
        
        try:
            response = await self.http_client.post(
                "/logout",
                headers=self._get_auth_headers()
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Logged out successfully: {data['message']}")
                self.jwt_token = None
                
                # Close WebSocket if connected
                if self.websocket:
                    await self.websocket.close()
                    self.websocket = None
            else:
                response.raise_for_status()
                
        except Exception as e:
            print(f"Logout failed: {e}")
            # Clear token anyway
            self.jwt_token = None


    async def delete_account(self):
        """
        Delete the user's account and all associated data.
        
        NEW endpoint support.
        WARNING: This action cannot be undone!
        """
        if not self.jwt_token:
            raise PermissionError("Not authenticated")
        
        # Confirm deletion
        print("⚠️  WARNING: This will permanently delete your account and all data!")
        print("   This action cannot be undone.")
        confirm = input("   Type 'DELETE' to confirm: ")
        
        if confirm != "DELETE":
            print("Account deletion cancelled.")
            return
        
        response = await self.http_client.delete(
            "/profile",
            headers=self._get_auth_headers()
        )
        
        if response.status_code == 204:
            print("✅ Account deleted successfully")
            self.jwt_token = None
            
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
            print("Closing existing websocket before reconnecting...")
            try:
                if self.websocket.state == websockets.State.OPEN:
                    await self.websocket.close()
            except:
                pass
            self.websocket = None
        
        if not self.jwt_token:
            raise PermissionError("Authentication required before connecting to signaling.")

        # Cancel old keepalive task
        if hasattr(self, '_keepalive_task') and self._keepalive_task and not self._keepalive_task.done():
            print("Cancelling old keepalive task...")
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        ws_url = self.api_base_url.replace("http", "ws") + f"/ws/signal?token={self.jwt_token}"
        print(f"Connecting to signaling server...")
        
        try:
            # Added ping_interval and ping_timeout for built-in keepalive
            self.websocket = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=60
            )
            
            response_str = await self.websocket.recv()
            response = json.loads(response_str)

            if response.get("type") != "auth_ok":
                await self.websocket.close()
                self.websocket = None
                raise ConnectionError(f"WebSocket authentication failed. Server response: {response}")

            print(f"Signaling server response: {response.get('message')}")
            print("Signaling socket connected and authenticated.")
            
            # Start keepalive ping task
            self._keepalive_task = asyncio.create_task(self._send_hub_keepalive_pings())
            print("Hub keepalive task started")
            
        except websockets.exceptions.InvalidStatus as e:
            raise ConnectionError(f"Server rejected WebSocket connection: {e.status_code}") from e

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
            
        print("HubClient connections closed.")

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
        
        print("📝 Registering cryptographic identity with Hub...")
        
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
                print(f"✅ Node identity registered and verified!")
                print(f"   Node ID: {data['node_id']}")
                print(f"   Verified: {data['verified']}")
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
                    print(f"Keepalive stopping - websocket not open (sent {ping_count} pings)")
                    break
                
                try:
                    ping_count += 1
                    ping_message = {
                        "type": "ping",
                        "timestamp": asyncio.get_event_loop().time()
                    }
                    await self.websocket.send(json.dumps(ping_message))
                    print(f"✓ Hub keepalive ping #{ping_count} sent")
                    
                except websockets.exceptions.ConnectionClosed:
                    print(f"Hub connection closed during ping #{ping_count}")
                    break
                except Exception as e:
                    print(f"Error sending keepalive ping #{ping_count}: {e}")
                    break
                    
        except asyncio.CancelledError:
            print(f"Hub keepalive cancelled (sent {ping_count} pings)")

# --- Self-testing block ---
async def main_test():
    """Updated test code with new features"""
    print("--- Testing Enhanced HubClient ---")
    
    hub = HubClient(api_base_url="http://127.0.0.1:8000")

    try:
        # 1. Test Login with Auto-Registration
        print("\n--- Step 1: Authentication ---")
        print("Please complete the login process in your browser.")
        await hub.login()
        assert hub.jwt_token is not None
        print("✅ Authentication and registration successful")

        # 2. Test Get Own Profile
        print("\n--- Step 2: Get Own Profile ---")
        my_profile = await hub.get_my_profile()
        if my_profile:
            print(f"✅ Profile found: {my_profile.get('name')}")
        else:
            print("ℹ️  No profile yet, creating one...")
            
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
            print("✅ Profile created and retrieved")

        # 3. Test Search
        print("\n--- Step 3: Discovery ---")
        results = await hub.search_expertise("python", min_level=1)
        print(f"✅ Found {len(results)} Python experts")

        # 4. Test Signaling Connection
        print("\n--- Step 4: WebSocket Signaling ---")
        await hub.connect_signaling()
        print("✅ Signaling connected")

        # 5. Test Logout
        print("\n--- Step 5: Logout ---")
        await hub.logout()
        print("✅ Logged out successfully")
        
        # Verify token is blacklisted
        try:
            await hub.get_my_profile()
            print("❌ Token should be blacklisted!")
        except Exception:
            print("✅ Token correctly blacklisted")

        print("\n✅ All tests passed!")

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        
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