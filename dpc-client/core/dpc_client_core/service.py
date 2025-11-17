# dpc-client/core/dpc_client_core/service.py

import asyncio
from dataclasses import asdict
import json
import uuid
import websockets
import socket
import sys
from pathlib import Path
from typing import Dict, Any, List

from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer
from .context_cache import ContextCache
from .settings import Settings
from .token_cache import TokenCache
from .peer_cache import PeerCache
from .connection_status import ConnectionStatus, OperationMode
from .consensus_manager import ConsensusManager
from .conversation_monitor import ConversationMonitor, Message as ConvMessage
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri
from datetime import datetime

# Define the path to the user's D-PC configuration directory
DPC_HOME_DIR = Path.home() / ".dpc"

class CoreService:
    """
    The main orchestrating class for the D-PC client's backend.
    Manages all sub-components and the application's lifecycle.
    """
    def __init__(self):
        print("Initializing D-PC Core Service...")

        DPC_HOME_DIR.mkdir(exist_ok=True)

        # Load settings (supports environment variables and config file)
        self.settings = Settings(DPC_HOME_DIR)

        # Initialize offline mode components
        self.token_cache = TokenCache(
            cache_dir=DPC_HOME_DIR,
            node_key_path=DPC_HOME_DIR / "node.key"
        )
        self.peer_cache = PeerCache(DPC_HOME_DIR / "known_peers.json")
        self.connection_status = ConnectionStatus()

        # Set up status change callback
        self.connection_status.set_on_status_change(self._on_connection_status_changed)

        # Initialize all major components
        self.firewall = ContextFirewall(DPC_HOME_DIR / ".dpc_access")
        self.llm_manager = LLMManager(DPC_HOME_DIR / "providers.toml")
        self.hub_client = HubClient(
            api_base_url=self.settings.get_hub_url(),
            oauth_callback_host=self.settings.get_oauth_callback_host(),
            oauth_callback_port=self.settings.get_oauth_callback_port(),
            token_cache=self.token_cache  # Pass token cache for offline mode
        )
        self.p2p_manager = P2PManager(firewall=self.firewall)
        self.cache = ContextCache()

        self.local_api = LocalApiServer(core_service=self)

        # Knowledge Architecture components (Phase 1-6)
        self.pcm_core = PCMCore(DPC_HOME_DIR / "personal.json")

        # Auto-collect device context on startup (if enabled)
        self.device_context = None
        if self.settings.get_auto_collect_device_info():
            try:
                from .device_context_collector import DeviceContextCollector
                device_collector = DeviceContextCollector(settings=self.settings)

                # Generate/update device_context.json
                device_file = device_collector.collect_and_save()
                print(f"[OK] Device context saved to {device_file.name}")

                # Update personal.json with reference to device_context.json
                context = self.pcm_core.load_context()
                context = device_collector.update_personal_context_reference(context)
                self.pcm_core.save_context(context)

                # Load device context for AI use
                with open(device_file, 'r', encoding='utf-8') as f:
                    self.device_context = json.load(f)
                print("[OK] Device context loaded and referenced in personal.json")
            except Exception as e:
                print(f"[Warning] Failed to collect device context: {e}")
                # Continue service startup even if collection fails

        # Set display name from personal context (for P2P handshakes)
        try:
            context = self.pcm_core.load_context()
            if context.profile and context.profile.name:
                self.p2p_manager.set_display_name(context.profile.name)
                print(f"[OK] Display name set from personal context: {context.profile.name}")
        except Exception as e:
            print(f"[Warning] Failed to load display name from personal context: {e}")

        self.consensus_manager = ConsensusManager(
            node_id=self.p2p_manager.node_id,
            pcm_core=self.pcm_core,
            vote_timeout_minutes=10
        )

        # Conversation monitors (per conversation/peer for knowledge extraction)
        # conversation_id -> ConversationMonitor
        self.conversation_monitors: Dict[str, ConversationMonitor] = {}

        # Knowledge extraction settings
        self.auto_knowledge_detection_enabled: bool = True  # Can be toggled by user

        self._is_running = False
        self._background_tasks = set()
        
        # Store peer metadata (names, profiles, etc.)
        self.peer_metadata: Dict[str, Dict[str, Any]] = {}

        # Track pending context requests (for request-response matching)
        self._pending_context_requests: Dict[str, asyncio.Future] = {}

        # Track pending device context requests (for request-response matching)
        self._pending_device_context_requests: Dict[str, asyncio.Future] = {}

        # Track pending inference requests (for request-response matching)
        self._pending_inference_requests: Dict[str, asyncio.Future] = {}

        # Hub reconnection settings
        self._hub_reconnect_attempts = 0
        self._max_hub_reconnect_attempts = 5

        # Set up callbacks AFTER all components are initialized
        self.p2p_manager.set_core_service_ref(self)
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.p2p_manager.set_on_message_received(self.on_p2p_message_received)
        self._processed_message_ids = set()  # Track processed messages
        self._max_processed_ids = 1000  # Limit set size

    def _on_connection_status_changed(self, old_mode: OperationMode, new_mode: OperationMode):
        """Callback when connection status changes."""
        print(f"\n[Connection Status Change] {old_mode.value} -> {new_mode.value}")
        print(f"Status: {self.connection_status.get_status_message()}")

        # Notify UI via local API
        asyncio.create_task(self._notify_ui_status_change(new_mode))

    async def _notify_ui_status_change(self, mode: OperationMode):
        """Notify UI of connection status change."""
        try:
            # Use same field names as get_status() for consistency
            status_info = {
                "operation_mode": mode.value,
                "connection_status": self.connection_status.get_status_message(),
                "available_features": self.connection_status.get_available_features(),
                "cached_peers_count": len(self.peer_cache.get_all_peers())
            }

            # Send to UI via local API
            await self.local_api.broadcast_event("connection_status_changed", {"status": status_info})
        except Exception as e:
            print(f"Error notifying UI of status change: {e}")

    async def start(self):
        """Starts all background services and runs indefinitely."""
        if self._is_running:
            print("Core Service is already running.")
            return

        print("Starting D-PC Core Service...")

        self._shutdown_event = asyncio.Event()

        # Start all background tasks
        p2p_task = asyncio.create_task(self.p2p_manager.start_server())
        p2p_task.set_name("p2p_server")
        self._background_tasks.add(p2p_task)

        api_task = asyncio.create_task(self.local_api.start())
        api_task.set_name("local_api")
        self._background_tasks.add(api_task)

        # Wait a moment for P2P server to start
        await asyncio.sleep(0.5)

        # Update Direct TLS status (always available if P2P server running)
        self.connection_status.update_direct_tls_status(available=True, port=8888)
        print("[OK] Direct TLS server started on port 8888")

        # Try to connect to Hub for WebRTC signaling (with graceful degradation)
        hub_connected = False

        # Check if auto-connect is enabled in config
        if self.settings.get_hub_auto_connect():
            try:
                # Use configured default provider
                default_provider = self.settings.get_oauth_default_provider()
                print(f"[Auto-Connect] Using OAuth provider: {default_provider}")

                await self.hub_client.login(provider=default_provider)
                await self.hub_client.connect_signaling_socket()
                hub_connected = True

                # Update connection status
                self.connection_status.update_hub_status(connected=True)
                self.connection_status.update_webrtc_status(available=True)

                # Start listening for incoming WebRTC signals
                signals_task = asyncio.create_task(self._listen_for_hub_signals())
                signals_task.set_name("hub_signals")
                self._background_tasks.add(signals_task)

                print("[OK] Hub connected - WebRTC available")

            except Exception as e:
                print(f"\n[Offline Mode] Hub connection failed: {e}")
                print(f"   Operating in {self.connection_status.get_operation_mode().value} mode")
                print(f"   Status: {self.connection_status.get_status_message()}")
                print(f"   Direct TLS connections still available via dpc:// URIs\n")

                # Update connection status
                self.connection_status.update_hub_status(connected=False, error=str(e))
                self.connection_status.update_webrtc_status(available=False)
        else:
            print("[Auto-Connect] Disabled in config - use UI to connect to Hub manually")
            print(f"   Operating in {self.connection_status.get_operation_mode().value} mode")
            print(f"   Direct TLS connections available via dpc:// URIs\n")

            # Update connection status
            self.connection_status.update_hub_status(connected=False)
            self.connection_status.update_webrtc_status(available=False)

        self._is_running = True
        print(f"\nD-PC Core Service started")
        print(f"  Node ID: {self.p2p_manager.node_id}")
        print(f"  Operation Mode: {self.connection_status.get_operation_mode().value}")
        print(f"  Available Features:")
        for feature, available in self.connection_status.get_available_features().items():
            status = "[OK]" if available else "[--]"
            print(f"    {status} {feature}")

        # Start hub connection monitor (only if initially connected)
        if hub_connected:
            monitor_task = asyncio.create_task(self._monitor_hub_connection())
            monitor_task.set_name("hub_monitor")
            self._background_tasks.add(monitor_task)

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def stop(self):
        """Signals the service to stop and waits for a clean shutdown."""
        if not self._is_running:
            return
        print("Stopping D-PC Core Service...")
        self._shutdown_event.set()

    async def shutdown(self):
        """Performs a clean shutdown of all components."""
        self._is_running = False
        print("Shutting down components...")
        await self.p2p_manager.shutdown_all()
        await self.local_api.stop()
        await self.hub_client.close()
        print("D-PC Core Service shut down.")
    
    async def _monitor_hub_connection(self):
        """Background task to monitor hub connection and auto-reconnect if needed."""
        while self._is_running:
            try:
                await asyncio.sleep(10)

                if not self.hub_client.websocket or \
                self.hub_client.websocket.state != websockets.State.OPEN:

                    # Check if we've exceeded max reconnection attempts
                    if self._hub_reconnect_attempts >= self._max_hub_reconnect_attempts:
                        print(f"\n[Hub Offline] Max reconnection attempts ({self._max_hub_reconnect_attempts}) reached")
                        print("   Staying in offline mode. Use 'Login to Hub' button to reconnect manually.")
                        self.connection_status.update_hub_status(
                            connected=False,
                            error=f"Max reconnection attempts ({self._max_hub_reconnect_attempts}) reached"
                        )
                        # Broadcast status update to UI
                        await self.local_api.broadcast_event("status_update", await self.get_status())
                        # Exit monitor loop - stop trying
                        break

                    self._hub_reconnect_attempts += 1

                    # Exponential backoff: 2, 4, 8, 16, 32 seconds
                    backoff_delay = min(2 ** self._hub_reconnect_attempts, 32)

                    print(f"Hub connection lost, attempting to reconnect (attempt {self._hub_reconnect_attempts}/{self._max_hub_reconnect_attempts})...")

                    # Update connection status
                    self.connection_status.update_hub_status(connected=False, error="Connection lost")
                    self.connection_status.update_webrtc_status(available=False)

                    # Cancel old listener task
                    old_listener = None
                    for task in list(self._background_tasks):
                        if task.get_name() == "hub_signals":
                            old_listener = task
                            break

                    if old_listener:
                        print("Cancelling old Hub signal listener...")
                        old_listener.cancel()
                        try:
                            await old_listener
                        except asyncio.CancelledError:
                            pass
                        self._background_tasks.discard(old_listener)

                    # Close old websocket BEFORE reconnecting
                    if self.hub_client.websocket:
                        print("Closing old Hub websocket...")
                        try:
                            await self.hub_client.websocket.close()
                        except:
                            pass
                        self.hub_client.websocket = None

                    # Wait before reconnection attempt (exponential backoff)
                    print(f"   Waiting {backoff_delay}s before reconnection...")
                    await asyncio.sleep(backoff_delay)

                    try:
                        await self.hub_client.connect_signaling_socket()

                        task = asyncio.create_task(self._listen_for_hub_signals())
                        task.set_name("hub_signals")
                        self._background_tasks.add(task)

                        # Update connection status
                        self.connection_status.update_hub_status(connected=True)
                        self.connection_status.update_webrtc_status(available=True)

                        # Reset reconnection attempts on success
                        self._hub_reconnect_attempts = 0

                        print("[OK] Hub reconnection successful")
                        await self.local_api.broadcast_event("status_update", await self.get_status())

                    except PermissionError as e:
                        print(f"Hub reconnection failed - authentication expired: {e}")
                        print("Please login again to reconnect to Hub.")
                        self.connection_status.update_hub_status(connected=False, error="Authentication expired")
                        # Don't count auth failures toward reconnect limit
                        self._hub_reconnect_attempts = self._max_hub_reconnect_attempts
                    except Exception as e:
                        print(f"Hub reconnection failed: {e}")
                        self.connection_status.update_hub_status(connected=False, error=str(e))
                else:
                    # Connection is healthy, reset reconnection attempts
                    if self._hub_reconnect_attempts > 0:
                        self._hub_reconnect_attempts = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in hub connection monitor: {e}")

    async def _listen_for_hub_signals(self):
        """Background task that listens for incoming WebRTC signaling messages from Hub."""
        print("Started listening for Hub WebRTC signals...")
        
        try:
            while self._is_running:
                try:
                    if not self.hub_client.websocket or \
                    self.hub_client.websocket.state != websockets.State.OPEN:
                        print("Hub signaling socket disconnected, stopping listener...")
                        break
                    
                    signal = await self.hub_client.receive_signal()
                    await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)
                    
                except asyncio.CancelledError:
                    break
                except ConnectionError as e:
                    print(f"Hub connection lost: {e}")
                    break
                except Exception as e:
                    print(f"Error receiving Hub signal: {e}")
                    await asyncio.sleep(1)
        finally:
            print("Stopped listening for Hub signals.")
            await self.local_api.broadcast_event("status_update", await self.get_status())


    # --- Callback Methods (called by P2PManager) ---

    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager when peer list changes."""
        print("Peer list changed, broadcasting status update to UI.")

        # Cache peer information for offline mode
        for peer_id, peer_conn in self.p2p_manager.peers.items():
            # Get peer metadata
            display_name = self.peer_metadata.get(peer_id, {}).get("name")

            # Determine connection type and extract IP if Direct TLS
            direct_ip = None
            connection_type = None

            if hasattr(peer_conn, 'peer_connection'):  # WebRTC
                connection_type = "webrtc"
            elif hasattr(peer_conn, 'reader'):  # Direct TLS
                connection_type = "direct_tls"
                # Try to extract IP from transport
                try:
                    transport = peer_conn.writer.transport
                    peername = transport.get_extra_info('peername')
                    if peername:
                        direct_ip = peername[0]  # IP address
                except Exception:
                    pass

            # Update peer cache
            self.peer_cache.add_or_update_peer(
                node_id=peer_id,
                display_name=display_name,
                direct_ip=direct_ip,
                supports_webrtc=(connection_type == "webrtc"),
                supports_direct=(connection_type == "direct_tls"),
                metadata={"last_connection_type": connection_type}
            )

        await self.local_api.broadcast_event("status_update", await self.get_status())
    
    async def on_p2p_message_received(self, sender_node_id: str, message: Dict[str, Any]):
        """
        Callback function that is triggered when a P2P message is received.
        Handles different message types and routes them appropriately.
        """
        command = message.get("command")
        payload = message.get("payload", {})
        
        print(f"CoreService received message from {sender_node_id}: {command}")
        
        if command == "SEND_TEXT":
            # Text message from peer - forward to UI with deduplication
            text = payload.get("text")
            
            # Create unique message ID for deduplication
            import hashlib
            import time
            message_id = hashlib.sha256(
                f"{sender_node_id}:{text}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]
            
            # Check if already processed
            if message_id in self._processed_message_ids:
                print(f"Duplicate message detected from {sender_node_id}, skipping")
                return
            
            # Add to processed set
            self._processed_message_ids.add(message_id)
            
            # Clean up old IDs
            if len(self._processed_message_ids) > self._max_processed_ids:
                to_remove = list(self._processed_message_ids)[:self._max_processed_ids // 2]
                for mid in to_remove:
                    self._processed_message_ids.discard(mid)
            
            # Broadcast to UI
            await self.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": sender_node_id,
                "text": text,
                "message_id": message_id
            })

            # Feed to conversation monitor for knowledge extraction (Phase 4.2)
            if self.auto_knowledge_detection_enabled:
                try:
                    monitor = self._get_or_create_conversation_monitor(sender_node_id)

                    # Create ConvMessage object for monitor
                    conv_message = ConvMessage(
                        message_id=message_id,
                        conversation_id=sender_node_id,
                        sender_node_id=sender_node_id,
                        sender_name=self.peer_metadata.get(sender_node_id, {}).get("name", sender_node_id),
                        text=text,
                        timestamp=datetime.utcnow().isoformat()
                    )

                    # Feed to monitor (automatic knowledge detection)
                    proposal = await monitor.on_message(conv_message)

                    # If proposal generated, broadcast to UI and start voting
                    if proposal:
                        print(f"[Auto-detect] Knowledge proposal generated for {sender_node_id}")
                        await self.local_api.broadcast_event(
                            "knowledge_commit_proposed",
                            proposal.to_dict()
                        )
                        await self.consensus_manager.propose_commit(
                            proposal=proposal,
                            broadcast_func=self._broadcast_to_peers
                        )
                except Exception as e:
                    print(f"Error in conversation monitoring: {e}")

        elif command == "REQUEST_CONTEXT":
            # Context request from peer - handle and respond
            request_id = payload.get("request_id")
            query = payload.get("query")
            await self._handle_context_request(sender_node_id, query, request_id)
        
        elif command == "CONTEXT_RESPONSE":
            # Context response from peer - resolve the pending future
            request_id = payload.get("request_id")
            context_dict = payload.get("context")

            if request_id in self._pending_context_requests:
                future = self._pending_context_requests[request_id]
                if not future.done():
                    future.set_result(context_dict)

        elif command == "REQUEST_DEVICE_CONTEXT":
            # Device context request from peer - handle and respond
            request_id = payload.get("request_id")
            await self._handle_device_context_request(sender_node_id, request_id)

        elif command == "DEVICE_CONTEXT_RESPONSE":
            # Device context response from peer - resolve the pending future
            request_id = payload.get("request_id")
            device_context = payload.get("device_context")

            if request_id in self._pending_device_context_requests:
                future = self._pending_device_context_requests[request_id]
                if not future.done():
                    future.set_result(device_context)

        elif command == "REMOTE_INFERENCE_REQUEST":
            # Remote inference request from peer - handle and respond
            request_id = payload.get("request_id")
            prompt = payload.get("prompt")
            model = payload.get("model")
            provider = payload.get("provider")
            await self._handle_inference_request(sender_node_id, request_id, prompt, model, provider)

        elif command == "REMOTE_INFERENCE_RESPONSE":
            # Remote inference response from peer - resolve the pending future
            request_id = payload.get("request_id")
            status = payload.get("status")
            response = payload.get("response")
            error = payload.get("error")

            if request_id in self._pending_inference_requests:
                future = self._pending_inference_requests[request_id]
                if not future.done():
                    if status == "success":
                        future.set_result(response)
                    else:
                        future.set_exception(RuntimeError(error or "Remote inference failed"))

        elif command == "GET_PROVIDERS":
            # Peer is requesting our available AI providers
            await self._handle_get_providers_request(sender_node_id)

        elif command == "PROVIDERS_RESPONSE":
            # Peer is sending their available AI providers
            providers = payload.get("providers", [])
            await self._handle_providers_response(sender_node_id, providers)

        elif command == "HELLO":
            # Name exchange (mainly for WebRTC connections that don't have initial handshake)
            peer_name = payload.get("name")
            if peer_name:
                print(f"Received name from {sender_node_id}: {peer_name}")
                self.set_peer_metadata(sender_node_id, name=peer_name)
                # Notify UI of peer list change so names update
                await self.on_peer_list_change()

        else:
            print(f"Unknown P2P message command: {command}")

    # --- High-level methods (API for the UI) ---

    def _get_local_ips(self) -> List[str]:
        """
        Get local network IP addresses (excluding loopback).
        Returns a list of IP addresses for constructing dpc:// URIs.
        Uses multiple methods for cross-platform compatibility and combines results.
        """
        local_ips = []
        errors = []

        # Method 1: Use socket to external address (most reliable, finds primary interface)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            if local_ip and not local_ip.startswith('127.'):
                local_ips.append(local_ip)
                print(f"✓ Found local IP via socket method: {local_ip}")
        except Exception as e:
            errors.append(f"Socket method: {e}")

        # Method 2: Try hostname resolution (finds all interfaces)
        try:
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)

            for info in addr_info:
                ip = info[4][0]
                # Filter out loopback addresses and duplicates
                if ip and not ip.startswith('127.') and ip not in local_ips:
                    local_ips.append(ip)
                    print(f"✓ Found local IP via hostname method: {ip}")

        except Exception as e:
            errors.append(f"Hostname method: {e}")

        # Method 3: Platform-specific (Linux/Unix only - finds all interfaces)
        if sys.platform.startswith('linux'):
            try:
                import subprocess
                # Use 'ip addr' command on Linux
                result = subprocess.run(['ip', 'addr', 'show'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    import re
                    # Look for inet addresses (not inet6)
                    for match in re.finditer(r'inet\s+(\d+\.\d+\.\d+\.\d+)', result.stdout):
                        ip = match.group(1)
                        if not ip.startswith('127.') and ip not in local_ips:
                            local_ips.append(ip)
                            print(f"✓ Found local IP via 'ip addr' command: {ip}")
            except Exception as e:
                errors.append(f"Linux 'ip addr' method: {e}")

        # Report results
        if not local_ips:
            print("⚠ Warning: Could not determine any local IP addresses")
            print(f"Errors encountered: {'; '.join(errors)}")
        else:
            print(f"✓ Successfully detected {len(local_ips)} local IP(s): {local_ips}")

        return local_ips

    async def get_status(self) -> Dict[str, Any]:
        """Aggregates status from all components."""

        hub_connected = (
            self.hub_client.websocket is not None and
            self.hub_client.websocket.state == websockets.State.OPEN
        )

        # Get peer info with names
        peer_info = []
        for peer_id in self.p2p_manager.peers.keys():
            peer_data = {
                "node_id": peer_id,
                "name": self.peer_metadata.get(peer_id, {}).get("name", None)
            }
            peer_info.append(peer_data)

        # Get active model name safely
        try:
            active_model = self.llm_manager.get_active_model_name()
        except (AttributeError, Exception):
            active_model = None

        # Get local IPs and construct dpc:// URIs
        local_ips = self._get_local_ips()
        dpc_port = 8888  # Default TLS server port
        dpc_uris = []

        for ip in local_ips:
            uri = f"dpc://{ip}:{dpc_port}?node_id={self.p2p_manager.node_id}"
            dpc_uris.append({
                "ip": ip,
                "port": dpc_port,
                "uri": uri
            })

        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
            "peer_info": peer_info,
            "active_ai_model": active_model,
            # Offline mode status
            "operation_mode": self.connection_status.get_operation_mode().value,
            "connection_status": self.connection_status.get_status_message(),
            "available_features": self.connection_status.get_available_features(),
            "cached_peers_count": len(self.peer_cache.get_all_peers()),
            # Direct TLS connection URIs
            "local_ips": local_ips,
            "dpc_uris": dpc_uris,
        }
    
    async def list_providers(self) -> Dict[str, Any]:
        """
        Returns all available AI providers from providers.toml.

        Returns:
            Dictionary with:
            - providers: List of provider objects with alias, type, model
            - default_provider: The default provider alias
        """
        providers_list = []

        for alias, provider in self.llm_manager.providers.items():
            provider_info = {
                "alias": alias,
                "type": provider.config.get("type"),
                "model": provider.model,
                "is_default": alias == self.llm_manager.default_provider
            }
            providers_list.append(provider_info)

        return {
            "providers": providers_list,
            "default_provider": self.llm_manager.default_provider
        }

    def set_peer_metadata(self, node_id: str, **kwargs):
        """Store metadata for a peer (name, etc)."""
        if node_id not in self.peer_metadata:
            self.peer_metadata[node_id] = {}
        self.peer_metadata[node_id].update(kwargs)

    # --- Knowledge Architecture Command Handlers ---

    async def get_personal_context(self) -> Dict[str, Any]:
        """Load and return personal context for UI display.

        UI Integration: Called when user opens ContextViewer component.
        Returns the full v2.0 PersonalContext with all metadata.
        """
        try:
            context = self.pcm_core.load_context()
            return {
                "status": "success",
                "context": asdict(context)
            }
        except Exception as e:
            print(f"Error loading personal context: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def vote_knowledge_commit(
        self,
        proposal_id: str,
        vote: str,
        comment: str = None
    ) -> Dict[str, Any]:
        """Cast vote on a knowledge commit proposal.

        UI Integration: Called when user clicks approve/reject/request_changes
        in KnowledgeCommitDialog component.

        Args:
            proposal_id: ID of the proposal to vote on
            vote: 'approve', 'reject', or 'request_changes'
            comment: Optional comment explaining the vote

        Returns:
            Dict with status and result
        """
        try:
            # Cast vote through ConsensusManager
            success = await self.consensus_manager.cast_vote(
                proposal_id=proposal_id,
                vote=vote,
                comment=comment,
                broadcast_func=self._broadcast_to_peers
            )

            if success:
                return {
                    "status": "success",
                    "message": f"Vote cast: {vote}"
                }
            else:
                return {
                    "status": "error",
                    "message": "Proposal not found or voting session expired"
                }

        except Exception as e:
            print(f"Error voting on knowledge commit: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def _broadcast_to_peers(self, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected peers.

        Used by ConsensusManager to broadcast votes and proposals.
        """
        for peer_id in list(self.p2p_manager.peers.keys()):
            try:
                await self.p2p_manager.send_to_peer(peer_id, message)
            except Exception as e:
                print(f"Failed to broadcast to {peer_id}: {e}")

    def _get_or_create_conversation_monitor(self, conversation_id: str) -> ConversationMonitor:
        """Get or create a conversation monitor for a conversation/peer.

        Args:
            conversation_id: Identifier for the conversation (peer node_id or "local_ai")

        Returns:
            ConversationMonitor instance
        """
        if conversation_id not in self.conversation_monitors:
            # Build participants list
            participants = []

            if conversation_id == "local_ai":
                # Local AI chat: just the user
                participants = [
                    {
                        "node_id": self.p2p_manager.node_id,
                        "name": "User",
                        "context": "local"
                    }
                ]
            else:
                # Peer chat: user + peer
                participants = [
                    {
                        "node_id": self.p2p_manager.node_id,
                        "name": "User",
                        "context": "local"
                    },
                    {
                        "node_id": conversation_id,
                        "name": self.peer_metadata.get(conversation_id, {}).get("name", conversation_id),
                        "context": "peer"
                    }
                ]

            self.conversation_monitors[conversation_id] = ConversationMonitor(
                conversation_id=conversation_id,
                participants=participants,
                llm_manager=self.llm_manager,
                knowledge_threshold=0.7  # 70% confidence threshold
            )
            print(f"Created conversation monitor for {conversation_id} with {len(participants)} participant(s)")

        return self.conversation_monitors[conversation_id]

    async def end_conversation_session(self, conversation_id: str) -> Dict[str, Any]:
        """Manually end a conversation session and extract knowledge.

        UI Integration: Called when user clicks "End Session & Save Knowledge" button.

        Args:
            conversation_id: The conversation/peer ID to end session for

        Returns:
            Dict with status and proposal (if knowledge detected)
        """
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            print(f"[End Session] Attempting manual extraction for {conversation_id}")
            print(f"   Buffer: {len(monitor.message_buffer)} messages")
            print(f"   Current score: {monitor.knowledge_score:.2f}")

            # Force knowledge extraction even if threshold not met
            proposal = await monitor.generate_commit_proposal(force=True)

            if proposal:
                print(f"✓ Knowledge proposal generated for {conversation_id}")
                print(f"   Topic: {proposal.topic}")
                print(f"   Entries: {len(proposal.entries)}")
                print(f"   Confidence: {proposal.avg_confidence:.2f}")

                # Broadcast to UI
                await self.local_api.broadcast_event(
                    "knowledge_commit_proposed",
                    proposal.to_dict()
                )

                # Start consensus voting
                await self.consensus_manager.propose_commit(
                    proposal=proposal,
                    broadcast_func=self._broadcast_to_peers
                )

                return {
                    "status": "success",
                    "message": "Knowledge proposal created",
                    "proposal_id": proposal.proposal_id
                }
            else:
                print(f"✗ No proposal generated - buffer was empty or no knowledge detected")
                return {
                    "status": "success",
                    "message": "No significant knowledge detected in conversation (buffer may be empty)"
                }

        except Exception as e:
            print(f"Error ending conversation session: {e}")
            import traceback
            traceback.print_exc()
            return {
                "status": "error",
                "message": str(e)
            }

    async def toggle_auto_knowledge_detection(self, enabled: bool = None) -> Dict[str, Any]:
        """Toggle automatic knowledge detection on/off.

        UI Integration: Called when user toggles the auto-detection switch.

        Args:
            enabled: True to enable, False to disable, None to toggle current state

        Returns:
            Dict with status and current state
        """
        try:
            if enabled is None:
                # Toggle current state
                self.auto_knowledge_detection_enabled = not self.auto_knowledge_detection_enabled
            else:
                # Set to specific value
                self.auto_knowledge_detection_enabled = enabled

            state_text = "enabled" if self.auto_knowledge_detection_enabled else "disabled"
            print(f"Auto knowledge detection {state_text}")

            return {
                "status": "success",
                "enabled": self.auto_knowledge_detection_enabled,
                "message": f"Automatic knowledge detection {state_text}"
            }

        except Exception as e:
            print(f"Error toggling auto knowledge detection: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    # --- P2P Connection Methods ---

    async def connect_to_peer(self, uri: str):
        """Connect to a peer using a dpc:// URI (Direct TLS)."""
        print(f"Orchestrating direct connection to {uri}...")
        
        # Parse the URI to extract host, port, and node_id
        host, port, target_node_id = parse_dpc_uri(uri)
        
        # Use connect_directly from P2PManager
        await self.p2p_manager.connect_directly(host, port, target_node_id)

    async def connect_to_peer_by_id(self, node_id: str):
        """
        Orchestrates a P2P connection to a peer using its node_id via Hub.
        Uses WebRTC with NAT traversal.
        """
        print(f"Orchestrating WebRTC connection to {node_id} via Hub...")
        
        # Check if Hub is connected
        if not self.hub_client.websocket or self.hub_client.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Not connected to Hub. Cannot establish WebRTC connection.")
        
        # Use WebRTC connection via Hub
        await self.p2p_manager.connect_via_hub(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def disconnect_from_peer(self, node_id: str):
        """Disconnect from a peer."""
        await self.p2p_manager.shutdown_peer_connection(node_id)

    async def send_p2p_message(self, target_node_id: str, text: str):
        """Send a text message to a connected peer."""
        print(f"Sending text message to {target_node_id}: {text}")
        
        message = {
            "command": "SEND_TEXT",
            "payload": {
                "text": text
            }
        }
        
        try:
            await self.p2p_manager.send_message_to_peer(target_node_id, message)
        except Exception as e:
            print(f"Error sending message to {target_node_id}: {e}")
            raise

    async def send_ai_query(self, prompt: str, compute_host: str = None, model: str = None, provider: str = None) -> str:
        """
        Send an AI query, either to local LLM or to a remote peer for inference.

        Args:
            prompt: The prompt to send to the AI
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use

        Returns:
            The AI response as a string

        Raises:
            ValueError: If compute_host is specified but peer is not connected
            RuntimeError: If inference fails
        """
        print(f"AI Query - compute_host: {compute_host or 'local'}, model: {model or 'default'}")

        # Local inference
        if not compute_host:
            try:
                return await self.llm_manager.query(prompt, provider_alias=provider)
            except Exception as e:
                print(f"Local inference failed: {e}")
                raise RuntimeError(f"Local inference failed: {e}") from e

        # Remote inference
        try:
            return await self._request_inference_from_peer(
                peer_id=compute_host,
                prompt=prompt,
                model=model,
                provider=provider
            )
        except ConnectionError as e:
            raise ValueError(f"Compute host {compute_host} is not connected") from e
        except Exception as e:
            print(f"Remote inference failed: {e}")
            raise RuntimeError(f"Remote inference failed: {e}") from e

    # --- Context Request Methods ---

    async def _handle_context_request(self, peer_id: str, query: str, request_id: str):
        """
        Handle incoming context request from a peer.
        Apply firewall rules and return filtered context.
        """
        print(f"  - Handling context request from {peer_id} (request_id: {request_id})")
        
        # Apply firewall to filter context based on peer
        filtered_context = self.firewall.filter_context_for_peer(
            context=self.p2p_manager.local_context,
            peer_id=peer_id,
            query=query
        )
        
        print(f"  - Context filtered by firewall for {peer_id}")
        
        # Convert to dict for JSON serialization
        context_dict = asdict(filtered_context)
        
        # Send response back to peer with request_id
        response = {
            "command": "CONTEXT_RESPONSE",
            "payload": {
                "request_id": request_id,
                "context": context_dict,
                "query": query
            }
        }
        
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            print(f"  - Sent filtered context response to {peer_id}")
        except Exception as e:
            print(f"  - Error sending context response to {peer_id}: {e}")

    async def _handle_device_context_request(self, peer_id: str, request_id: str):
        """
        Handle incoming device context request from a peer.
        Apply firewall rules and return filtered device context.
        """
        print(f"  - Handling device context request from {peer_id} (request_id: {request_id})")

        # Check if device context is available
        if not self.device_context:
            print(f"  - Device context not available, sending empty response")
            response = {
                "command": "DEVICE_CONTEXT_RESPONSE",
                "payload": {
                    "request_id": request_id,
                    "device_context": {},
                    "error": "Device context not available"
                }
            }
        else:
            # Apply firewall to filter device context based on peer
            filtered_device_context = self.firewall.filter_device_context_for_peer(
                device_context=self.device_context,
                peer_id=peer_id
            )

            print(f"  - Device context filtered by firewall for {peer_id}")

            # Send response back to peer with request_id
            response = {
                "command": "DEVICE_CONTEXT_RESPONSE",
                "payload": {
                    "request_id": request_id,
                    "device_context": filtered_device_context
                }
            }

        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            print(f"  - Sent filtered device context response to {peer_id}")
        except Exception as e:
            print(f"  - Error sending device context response to {peer_id}: {e}")

    async def _handle_inference_request(self, peer_id: str, request_id: str, prompt: str, model: str = None, provider: str = None):
        """
        Handle incoming remote inference request from a peer.
        Check firewall permissions, run inference, and send response.
        """
        from dpc_protocol.protocol import create_remote_inference_response

        print(f"  - Handling inference request from {peer_id} (request_id: {request_id})")

        # Check if peer is allowed to request inference
        if not self.firewall.can_request_inference(peer_id, model):
            print(f"  - Access denied: {peer_id} cannot request inference" + (f" for model {model}" if model else ""))
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request inference" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                print(f"  - Error sending inference error response to {peer_id}: {e}")
            return

        # Run inference
        try:
            print(f"  - Running inference for {peer_id} (model: {model or 'default'}, provider: {provider or 'default'})")

            # Determine which provider to use:
            # 1. If model name specified, find provider by model
            # 2. Otherwise use provider alias if specified
            # 3. Otherwise use default provider
            provider_alias_to_use = provider

            if model and not provider:
                # Find provider by model name
                found_alias = self.llm_manager.find_provider_by_model(model)
                if found_alias:
                    provider_alias_to_use = found_alias
                    print(f"  - Found provider '{found_alias}' for model '{model}'")
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            result = await self.llm_manager.query(prompt, provider_alias=provider_alias_to_use)
            print(f"  - Inference completed successfully for {peer_id}")

            # Send success response
            success_response = create_remote_inference_response(
                request_id=request_id,
                response=result
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)
            print(f"  - Sent inference result to {peer_id}")

        except Exception as e:
            print(f"  - Inference failed for {peer_id}: {e}")
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=str(e)
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                print(f"  - Error sending inference error response to {peer_id}: {send_err}")

    async def _handle_get_providers_request(self, peer_id: str):
        """
        Handle GET_PROVIDERS request from a peer.
        Check firewall permissions and send available providers that the peer can use.
        """
        from dpc_protocol.protocol import create_providers_response

        print(f"  - Handling GET_PROVIDERS request from {peer_id}")

        # Check if compute sharing is enabled and peer is authorized
        if not self.firewall.can_request_inference(peer_id):
            print(f"  - Access denied: {peer_id} cannot access compute resources")
            # Send empty provider list (no access)
            response = create_providers_response([])
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, response)
            except Exception as e:
                print(f"  - Error sending providers response to {peer_id}: {e}")
            return

        # Get all available providers
        all_providers = []
        all_models = []

        for alias, provider in self.llm_manager.providers.items():
            model = provider.model
            provider_type = provider.config.get("type", "unknown")

            all_providers.append({
                "alias": alias,
                "model": model,
                "type": provider_type
            })
            all_models.append(model)

        # Filter providers based on firewall allowed_models setting
        allowed_models = self.firewall.get_available_models_for_peer(peer_id, all_models)

        # Only include providers with allowed models
        filtered_providers = [
            p for p in all_providers
            if p["model"] in allowed_models
        ]

        print(f"  - Sending {len(filtered_providers)} providers to {peer_id} (filtered from {len(all_providers)} total)")

        # Send response with filtered providers
        response = create_providers_response(filtered_providers)
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
        except Exception as e:
            print(f"  - Error sending providers response to {peer_id}: {e}")

    async def _handle_providers_response(self, peer_id: str, providers: list):
        """
        Handle PROVIDERS_RESPONSE from a peer.
        Store the providers in peer metadata and broadcast to UI.
        """
        print(f"  - Received {len(providers)} providers from {peer_id}")

        # Update peer metadata with providers
        if peer_id not in self.peer_metadata:
            self.peer_metadata[peer_id] = {}

        self.peer_metadata[peer_id]["providers"] = providers

        # Broadcast to UI
        await self.local_api.broadcast_event("peer_providers_updated", {
            "node_id": peer_id,
            "providers": providers
        })

    async def _request_context_from_peer(self, peer_id: str, query: str) -> PersonalContext:
        """
        Request context from a specific peer for the given query.
        Uses async request-response pattern with Future.
        """
        print(f"  - Requesting context from peer: {peer_id}")
        
        if peer_id not in self.p2p_manager.peers:
            print(f"  - Peer {peer_id} not connected, skipping context request.")
            return None
        
        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())
            
            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_context_requests[request_id] = response_future
            
            # Create context request message
            request_message = {
                "command": "REQUEST_CONTEXT",
                "payload": {
                    "request_id": request_id,
                    "query": query,
                    "requestor_id": self.p2p_manager.node_id
                }
            }
            
            # Send request using P2PManager's method
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)
            
            # Wait for response with timeout
            try:
                context_dict = await asyncio.wait_for(response_future, timeout=5.0)
                
                if context_dict:
                    # Convert dict back to PersonalContext object
                    return PersonalContext(**context_dict)
                
            except asyncio.TimeoutError:
                print(f"  - Timeout waiting for context from {peer_id}")
                return None
            finally:
                # Clean up pending request
                self._pending_context_requests.pop(request_id, None)
            
            return None

        except Exception as e:
            print(f"  - Error requesting context from {peer_id}: {e}")
            return None

    async def _request_device_context_from_peer(self, peer_id: str) -> Dict:
        """
        Request device context from a specific peer.
        Uses async request-response pattern with Future.

        Args:
            peer_id: The node_id of the peer to request device context from

        Returns:
            Dict containing filtered device context, or None if request fails
        """
        print(f"  - Requesting device context from peer: {peer_id}")

        if peer_id not in self.p2p_manager.peers:
            print(f"  - Peer {peer_id} not connected, skipping device context request.")
            return None

        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_device_context_requests[request_id] = response_future

            # Create device context request message
            request_message = {
                "command": "REQUEST_DEVICE_CONTEXT",
                "payload": {
                    "request_id": request_id,
                    "requestor_id": self.p2p_manager.node_id
                }
            }

            # Send request using P2PManager's method
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            # Wait for response with timeout
            try:
                device_context_dict = await asyncio.wait_for(response_future, timeout=5.0)
                return device_context_dict if device_context_dict else None

            except asyncio.TimeoutError:
                print(f"  - Timeout waiting for device context from {peer_id}")
                return None
            finally:
                # Clean up pending request
                self._pending_device_context_requests.pop(request_id, None)

        except Exception as e:
            print(f"  - Error requesting device context from {peer_id}: {e}")
            return None

    async def _request_inference_from_peer(self, peer_id: str, prompt: str, model: str = None, provider: str = None, timeout: float = 60.0) -> str:
        """
        Request remote inference from a specific peer.
        Uses async request-response pattern with Future.

        Args:
            peer_id: The node_id of the peer to request inference from
            prompt: The prompt to send for inference
            model: Optional model name to use
            provider: Optional provider alias to use
            timeout: Timeout in seconds (default 60s for inference)

        Returns:
            The inference result as a string

        Raises:
            ConnectionError: If peer is not connected
            TimeoutError: If request times out
            RuntimeError: If inference fails on remote peer
        """
        from dpc_protocol.protocol import create_remote_inference_request

        print(f"  - Requesting inference from peer: {peer_id}")

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_inference_requests[request_id] = response_future

            # Create inference request message
            request_message = create_remote_inference_request(
                request_id=request_id,
                prompt=prompt,
                model=model,
                provider=provider
            )

            # Send request
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            # Wait for response with timeout
            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                print(f"  - Received inference result from {peer_id}")
                return result

            except asyncio.TimeoutError:
                print(f"  - Timeout waiting for inference from {peer_id}")
                raise TimeoutError(f"Inference request to {peer_id} timed out after {timeout}s")
            finally:
                # Clean up pending request
                self._pending_inference_requests.pop(request_id, None)

        except Exception as e:
            print(f"  - Error requesting inference from {peer_id}: {e}")
            raise

    async def _aggregate_contexts(self, query: str, peer_ids: List[str] = None) -> Dict[str, PersonalContext]:
        """
        Aggregate contexts from local user and connected peers.
        
        Args:
            query: The user's query (used for context relevance)
            peer_ids: Optional list of specific peer IDs to request from.
                     If None, requests from all connected peers.
        
        Returns:
            Dictionary mapping node_id to PersonalContext
        """
        contexts = {}
        
        # Always include local context
        contexts[self.p2p_manager.node_id] = self.p2p_manager.local_context
        
        # Determine which peers to query
        if peer_ids is None:
            peer_ids = list(self.p2p_manager.peers.keys())
        
        # Request context from each peer in parallel
        if peer_ids:
            tasks = [self._request_context_from_peer(peer_id, query) for peer_id in peer_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for peer_id, result in zip(peer_ids, results):
                if isinstance(result, PersonalContext):
                    contexts[peer_id] = result
                elif result is not None:
                    print(f"  - Error getting context from {peer_id}: {result}")
        
        return contexts

    # --- AI Query Methods ---

    async def execute_ai_query(self, command_id: str, prompt: str, context_ids: list = None, compute_host: str = None, model: str = None, provider: str = None, **kwargs):
        """
        Orchestrates an AI query and sends the response back to the UI.

        Args:
            command_id: Unique ID for this command
            prompt: The user's prompt/query
            context_ids: Optional list of peer node_ids to fetch context from
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use
            **kwargs: Additional arguments
        """
        print(f"Orchestrating AI query for command_id {command_id}: '{prompt[:50]}...'")
        print(f"  - Compute host: {compute_host or 'local'}")
        print(f"  - Model: {model or 'default'}")

        # Start with local context
        aggregated_contexts = {'local': self.p2p_manager.local_context}

        # Add device context if available
        device_context_data = None
        if self.device_context:
            device_context_data = self.device_context

        # TODO: Fetch remote contexts if context_ids provided
        if context_ids:
            for node_id in context_ids:
                if node_id in self.p2p_manager.peers:
                    # Fetch context from peer
                    # context = await self.request_context_from_peer(node_id, prompt)
                    # aggregated_contexts[node_id] = context
                    pass

        # Assemble final prompt with context
        final_prompt = self._assemble_final_prompt(aggregated_contexts, prompt, device_context_data)

        response_payload = {}
        status = "OK"

        try:
            # Use send_ai_query to support both local and remote inference
            response_content = await self.send_ai_query(
                prompt=final_prompt,
                compute_host=compute_host,
                model=model,
                provider=provider
            )
            response_payload = {"content": response_content}

        except Exception as e:
            print(f"  - Error during inference: {e}")
            status = "ERROR"
            response_payload = {"message": str(e)}

        # Send response back to UI
        await self.local_api.send_response_to_all(
            command_id=command_id,
            command="execute_ai_query",
            status=status,
            payload=response_payload
        )

        # Feed local AI conversation to monitor (Phase 4.2 - Local AI support)
        if self.auto_knowledge_detection_enabled and status == "OK":
            try:
                monitor = self._get_or_create_conversation_monitor("local_ai")
                print(f"[Monitor] Feeding messages to local_ai monitor (buffer size before: {len(monitor.message_buffer)})")

                # Feed user message
                user_message = ConvMessage(
                    message_id=f"{command_id}-user",
                    conversation_id="local_ai",
                    sender_node_id="user",
                    sender_name="User",
                    text=prompt,
                    timestamp=datetime.utcnow().isoformat()
                )
                await monitor.on_message(user_message)

                # Feed AI response
                ai_message = ConvMessage(
                    message_id=f"{command_id}-ai",
                    conversation_id="local_ai",
                    sender_node_id="ai",
                    sender_name="AI Assistant",
                    text=response_payload.get("content", ""),
                    timestamp=datetime.utcnow().isoformat()
                )
                proposal = await monitor.on_message(ai_message)

                print(f"[Monitor] Buffer size after: {len(monitor.message_buffer)}, Score: {monitor.knowledge_score:.2f}")

                # If proposal generated, broadcast to UI
                if proposal:
                    print(f"[Auto-detect] Knowledge proposal generated for local_ai chat")
                    await self.local_api.broadcast_event(
                        "knowledge_commit_proposed",
                        proposal.to_dict()
                    )
                    await self.consensus_manager.propose_commit(
                        proposal=proposal,
                        broadcast_func=self._broadcast_to_peers
                    )
                else:
                    print(f"[Monitor] No proposal yet (need 5 messages for auto-detect)")
            except Exception as e:
                print(f"Error in local AI conversation monitoring: {e}")
                import traceback
                traceback.print_exc()
        elif not self.auto_knowledge_detection_enabled:
            print(f"[Monitor] Auto-detection is OFF - messages not being monitored")
        elif status != "OK":
            print(f"[Monitor] Query failed (status={status}) - not monitoring")

    def _assemble_final_prompt(self, contexts: dict, clean_prompt: str, device_context: dict = None) -> str:
        """Helper method to assemble the final prompt for the LLM with instruction processing.

        Phase 2: Incorporates InstructionBlock and bias mitigation from PCM v2.0
        """
        # Extract instruction blocks and bias settings from contexts
        instruction_blocks = []
        bias_mitigation_settings = []
        cultural_contexts = []

        for source_id, context_obj in contexts.items():
            # Extract instruction block if present
            if hasattr(context_obj, 'instruction') and context_obj.instruction:
                instruction = context_obj.instruction
                instruction_blocks.append({
                    'source': source_id,
                    'primary': instruction.primary,
                    'verification_protocol': instruction.verification_protocol,
                    'bias_mitigation': instruction.bias_mitigation
                })
                bias_mitigation_settings.append(instruction.bias_mitigation)

            # Extract cultural context if present
            if hasattr(context_obj, 'cognitive_profile') and context_obj.cognitive_profile:
                if context_obj.cognitive_profile.cultural_background:
                    cultural_contexts.append({
                        'source': source_id,
                        'background': context_obj.cognitive_profile.cultural_background
                    })

        # Build system instruction with bias mitigation
        system_instruction = self._build_bias_aware_system_instruction(
            instruction_blocks,
            bias_mitigation_settings,
            cultural_contexts
        )

        # Build context blocks
        context_blocks = []
        for source_id, context_obj in contexts.items():
            context_dict = asdict(context_obj)
            json_string = json.dumps(context_dict, indent=2, ensure_ascii=False)

            # Add peer name if available
            source_label = source_id
            if source_id != 'local' and source_id in self.peer_metadata:
                peer_name = self.peer_metadata[source_id].get('name')
                if peer_name:
                    source_label = f"{peer_name} ({source_id})"

            block = f'<CONTEXT source="{source_label}">\n{json_string}\n</CONTEXT>'
            context_blocks.append(block)

        # Add device context if available
        if device_context:
            device_json = json.dumps(device_context, indent=2, ensure_ascii=False)
            device_block = f'<DEVICE_CONTEXT source="local">\n{device_json}\n</DEVICE_CONTEXT>'
            context_blocks.append(device_block)

        final_prompt = (
            f"{system_instruction}\n\n"
            f"--- CONTEXTUAL DATA ---\n"
            f'{"\n\n".join(context_blocks)}\n'
            f"--- END OF CONTEXTUAL DATA ---\n\n"
            f"USER QUERY: {clean_prompt}"
        )
        return final_prompt

    def _build_bias_aware_system_instruction(
        self,
        instruction_blocks: List[Dict[str, Any]],
        bias_mitigation_settings: List[Dict[str, Any]],
        cultural_contexts: List[Dict[str, str]]
    ) -> str:
        """Build system instruction with bias mitigation and multi-perspective requirements.

        Phase 2: Implements cognitive bias mitigation strategies from KNOWLEDGE_ARCHITECTURE.md
        """
        # Base instruction
        base = (
            "You are a helpful AI assistant with strong bias-awareness training. "
            "Your task is to answer the user's query based on the provided JSON data blobs inside <CONTEXT> tags. "
            "The 'source' attribute of each tag indicates who the context belongs to. The source 'local' refers to the user asking the query. "
            "Other sources are peer nodes who have shared their context to help answer the query. "
            "Analyze all provided contexts to formulate your answer. When relevant, cite which source provided specific information."
        )

        # Add user-specific instructions
        if instruction_blocks:
            base += "\n\nUSER-SPECIFIC INSTRUCTIONS:"
            for block in instruction_blocks:
                source_label = "Local user" if block['source'] == 'local' else f"Peer {block['source']}"
                base += f"\n[{source_label}] {block['primary']}"
                if block.get('verification_protocol'):
                    base += f" ({block['verification_protocol']})"

        # Check if ANY context requires bias mitigation
        requires_bias_mitigation = any(
            settings.get('require_multi_perspective') or
            settings.get('challenge_status_quo')
            for settings in bias_mitigation_settings
        ) if bias_mitigation_settings else False

        # Add bias mitigation rules if required
        if requires_bias_mitigation:
            base += "\n\nBIAS MITIGATION RULES:"

            # Multi-perspective requirement
            if any(s.get('require_multi_perspective') for s in bias_mitigation_settings):
                base += (
                    "\n1. Multi-Perspective Analysis: Consider perspectives from at least 3 different cultural or methodological viewpoints"
                    "\n   - Western individualistic approach"
                    "\n   - Eastern collective approach"
                    "\n   - Indigenous/holistic approach"
                    "\n   - Or other relevant frameworks based on the query"
                )

            # Status quo challenge
            if any(s.get('challenge_status_quo') for s in bias_mitigation_settings):
                base += (
                    "\n2. Challenge Status Quo: Always question existing approaches and common assumptions"
                    "\n   - Don't favor solutions just because they're mentioned in context"
                    "\n   - Consider alternatives to established methods"
                    "\n   - Ask 'What if we approached this differently?'"
                )

            # Cultural sensitivity
            cultural_sensitivity_settings = [
                s.get('cultural_sensitivity')
                for s in bias_mitigation_settings
                if s.get('cultural_sensitivity')
            ]
            if cultural_sensitivity_settings:
                base += f"\n3. Cultural Sensitivity: {cultural_sensitivity_settings[0]}"
                if cultural_contexts:
                    base += "\n   Cultural contexts to consider:"
                    for ctx in cultural_contexts:
                        base += f"\n   - [{ctx['source']}]: {ctx['background']}"

            # Framing neutrality
            if any(s.get('framing_neutrality') for s in bias_mitigation_settings):
                base += (
                    "\n4. Framing Neutrality: Present options without preference or bias"
                    "\n   - Use neutral language"
                    "\n   - Don't anchor on first-mentioned options"
                    "\n   - Present pros and cons equally"
                )

            # Evidence requirement
            evidence_requirements = [
                s.get('evidence_requirement')
                for s in bias_mitigation_settings
                if s.get('evidence_requirement')
            ]
            if evidence_requirements:
                evidence_level = evidence_requirements[0]
                if evidence_level == 'citations_preferred':
                    base += (
                        "\n5. Evidence Requirement: Provide reasoning and sources for claims"
                        "\n   - Cite which context provided specific information"
                        "\n   - Distinguish between facts and opinions"
                        "\n   - Note confidence levels when uncertain"
                    )
                elif evidence_level == 'citations_required':
                    base += (
                        "\n5. Evidence Requirement: MUST provide citations for all factual claims"
                        "\n   - Every claim must be traceable to a context source"
                        "\n   - Flag any unsourced information clearly"
                    )

            # Add cognitive biases to avoid
            base += (
                "\n\nCOGNITIVE BIASES TO AVOID:"
                "\n- Status quo bias (favoring current/mentioned methods)"
                "\n- Anchoring bias (overweighting information presented first)"
                "\n- Cultural bias (assuming Western-centric solutions)"
                "\n- Groupthink (consensus without critical evaluation)"
                "\n- Primacy effect (favoring first-presented options)"
                "\n- Confirmation bias (seeking info that confirms existing beliefs)"
            )

        return base

    def _build_combined_prompt(self, query: str, contexts: Dict[str, PersonalContext]) -> str:
        """Build a prompt that combines the query with multiple contexts."""
        prompt_parts = ["Context information:"]
        
        for node_id, context in contexts.items():
            source = "You" if node_id == self.p2p_manager.node_id else f"Peer {node_id}"
            prompt_parts.append(f"\n[From {source}]")
            prompt_parts.append(f"Name: {context.profile.get('name', 'Unknown')}")
            if context.profile.get('description'):
                prompt_parts.append(f"Description: {context.profile['description']}")
        
        prompt_parts.append(f"\nUser Query: {query}")
        
        return "\n".join(prompt_parts)

    # --- Hub Methods ---

    async def login_to_hub(self, provider: str = "google"):
        """Login to Hub using OAuth."""
        await self.hub_client.login(provider=provider)
        await self.hub_client.connect_signaling_socket()

        # Reset reconnection attempts on manual login
        self._hub_reconnect_attempts = 0

        # Update connection status
        self.connection_status.update_hub_status(connected=True)
        self.connection_status.update_webrtc_status(available=True)

        # Start listening for signals if not already
        if not any(task.get_name() == "hub_signals" for task in self._background_tasks):
            signals_task = asyncio.create_task(self._listen_for_hub_signals())
            signals_task.set_name("hub_signals")
            self._background_tasks.add(signals_task)

        # Start hub monitor if not already running
        if not any(task.get_name() == "hub_monitor" for task in self._background_tasks):
            monitor_task = asyncio.create_task(self._monitor_hub_connection())
            monitor_task.set_name("hub_monitor")
            self._background_tasks.add(monitor_task)

        print("[OK] Successfully logged in to Hub.")

    async def disconnect_from_hub(self):
        """Disconnect from Hub."""
        # Cancel the signal listening task
        for task in list(self._background_tasks):
            if task.get_name() == "hub_signals":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._background_tasks.remove(task)
                break

        # Cancel the hub monitor task
        for task in list(self._background_tasks):
            if task.get_name() == "hub_monitor":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._background_tasks.remove(task)
                break

        await self.hub_client.close()

        # Update connection status
        self.connection_status.update_hub_status(connected=False, error="User disconnected")
        self.connection_status.update_webrtc_status(available=False)

        print("[OK] Disconnected from Hub.")
        await self.local_api.broadcast_event("status_update", await self.get_status())