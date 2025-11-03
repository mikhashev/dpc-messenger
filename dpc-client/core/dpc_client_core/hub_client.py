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
        Initiates the OAuth 2.0 login flow by starting a local server to
        catch the redirect and opening the user's browser.
        """
        if self.jwt_token:
            print("Already authenticated.")
            return

        token_future = asyncio.Future()
        
        # --- THE CORE FIX ---
        # The handler now expects a path like /callback?access_token=...
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
                    # Handle other requests (like favicon.ico) gracefully
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
            # Wait for the callback handler to set the result on the future
            self.jwt_token = await asyncio.wait_for(token_future, timeout=180) # 3 minute timeout
            print("Authentication successful, JWT received.")
        except asyncio.TimeoutError:
            print("Authentication timed out.")
            raise
        finally:
            # Cleanly shut down the temporary server
            server.shutdown()
            thread.join()
            print("Local callback server stopped.")

    # --- REST API Methods ---

    async def update_profile(self, profile_data: Dict[str, Any]):
        """Pushes the public profile to the Hub."""
        print("Updating profile on the Hub...")
        response = await self.http_client.put(
            "/profile",
            json=profile_data,
            headers=self._get_auth_headers()
        )
        response.raise_for_status() # Will raise an exception for 4xx/5xx responses
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

    # --- WebSocket Signaling Methods ---

    async def connect_signaling_socket(self):
        """Connects to the Hub's WebSocket and authenticates via query parameter."""
        if self.websocket and not self.websocket.close:
            print("Signaling socket is already connected.")
            return
        
        if not self.jwt_token:
            raise PermissionError("Authentication required before connecting to signaling.")

        # --- THE CORE FIX: Pass token as a query parameter ---
        ws_url = self.api_base_url.replace("http", "ws") + f"/ws/signal?token={self.jwt_token}"
        print(f"Connecting to signaling server...")
        
        try:
            # Establish the connection. The server will authenticate based on the URL.
            self.websocket = await websockets.connect(ws_url)
            
            # Wait for the confirmation message from the server
            response_str = await self.websocket.recv()
            response = json.loads(response_str)

            if response.get("type") != "auth_ok":
                await self.websocket.close()
                self.websocket = None
                raise ConnectionError(f"WebSocket authentication failed. Server response: {response}")

            print(f"Signaling server response: {response.get('message')}")
            print("Signaling socket connected and authenticated.")
        except websockets.exceptions.InvalidStatus as e:
            # This will catch rejections like 403 Forbidden
            raise ConnectionError(f"Server rejected WebSocket connection: {e.status_code}") from e

    async def send_signal(self, target_node_id: str, payload: Dict[str, Any]):
        """Sends a signaling message to a target peer via the Hub."""
        if not self.websocket or not self.websocket.open:
            raise ConnectionError("Signaling socket is not connected.")
        
        message = {
            "type": "signal",
            "target_node_id": target_node_id,
            "payload": payload
        }
        await self.websocket.send(json.dumps(message))

    async def receive_signal(self) -> Dict[str, Any]:
        """Waits for and returns the next signaling message from the Hub."""
        if not self.websocket or not self.websocket.open:
            raise ConnectionError("Signaling socket is not connected.")
        
        message_str = await self.websocket.recv()
        return json.loads(message_str)

    async def close(self):
        """Closes all network connections."""
        if self.websocket and not self.websocket.close: # <-- CORRECT CHECK
            await self.websocket.close()
        if not self.http_client.is_closed:
            await self.http_client.aclose()
        print("HubClient connections closed.")

# --- Self-testing block ---
async def main_test():
    print("--- Testing HubClient ---")
    # Make sure the Hub server is running at this address
    hub = HubClient(api_base_url="http://127.0.0.1:8000")

    try:
        # 1. Test Login
        print("\n--- Step 1: Authentication ---")
        print("Please complete the login process in your browser.")
        await hub.login()
        assert hub.jwt_token is not None

        # 2. Test Profile Update
        print("\n--- Step 2: Profile Update ---")
        dummy_profile = {
            "node_id": "dpc-node-test-123456", # This should match the one from the Hub's DB
            "name": "Test User",
            "description": "Testing the HubClient.",
            "expertise": {"testing": 5},
        }
        await hub.update_profile(dummy_profile)

        # 3. Test Search
        print("\n--- Step 3: Discovery Search ---")
        search_results = await hub.search_users(topic="testing")
        print(f"Search results: {search_results}")
        assert len(search_results.get("results", [])) > 0

        # 4. Test Signaling
        print("\n--- Step 4: Signaling WebSocket ---")
        await hub.connect_signaling_socket()
        # In a real scenario, another task would be listening with receive_signal
        print("Signaling test completed (connection was successful).")

    except Exception as e:
        print(f"\nAn error occurred during testing: {e}")
        traceback.print_exc()
    finally:
        await hub.close()
        print("\n--- Test finished ---")

if __name__ == '__main__':
    # To run this test:
    # 1. Make sure the Hub server is running.
    # 2. Make sure your Google OAuth credentials are correct in the Hub's .env.
    # 3. Navigate to `dpc-client/core/`
    # 4. Run: `poetry run python dpc_client_core/hub_client.py`
    import traceback
    asyncio.run(main_test())