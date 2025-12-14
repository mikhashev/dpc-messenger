# dpc-client/core/dpc_client_core/service.py

import asyncio
from dataclasses import asdict
import json
import logging
import uuid
import websockets
import socket
import sys
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

from .__version__ import __version__
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
from .stun_discovery import discover_external_ip
from .inference_orchestrator import InferenceOrchestrator
from .context_coordinator import ContextCoordinator
from .p2p_coordinator import P2PCoordinator
from .coordinators.connection_orchestrator import ConnectionOrchestrator
from .message_router import MessageRouter
from .managers.hole_punch_manager import HolePunchManager
from .managers.relay_manager import RelayManager
from .managers.gossip_manager import GossipManager
from .message_handlers.hello_handler import HelloHandler
from .message_handlers.text_handler import SendTextHandler
from .message_handlers.context_handler import (
    RequestContextHandler, ContextResponseHandler,
    RequestDeviceContextHandler, DeviceContextResponseHandler
)
from .message_handlers.inference_handler import (
    RemoteInferenceRequestHandler, RemoteInferenceResponseHandler
)
from .message_handlers.provider_handler import GetProvidersHandler, ProvidersResponseHandler
from .message_handlers.knowledge_handler import (
    ContextUpdatedHandler, ProposeKnowledgeCommitHandler, VoteKnowledgeCommitHandler,
    KnowledgeCommitResultHandler
)
from .message_handlers.gossip_handler import GossipSyncHandler, GossipMessageHandler
from .message_handlers.relay_register_handler import RelayRegisterHandler
from .message_handlers.relay_message_handler import RelayMessageHandler
from .message_handlers.relay_disconnect_handler import RelayDisconnectHandler
from .message_handlers.file_offer_handler import FileOfferHandler
from .message_handlers.file_accept_handler import FileAcceptHandler
from .message_handlers.file_chunk_handler import FileChunkHandler
from .message_handlers.file_complete_handler import FileCompleteHandler
from .message_handlers.file_cancel_handler import FileCancelHandler
from .message_handlers.file_chunk_retry_handler import FileChunkRetryHandler
from .managers.file_transfer_manager import FileTransferManager
from dpc_protocol.pcm_core import (
    PCMCore, PersonalContext, InstructionBlock,
    load_instructions, save_instructions, migrate_instructions_from_personal_context
)
from dpc_protocol.utils import parse_dpc_uri
from datetime import datetime

# Define the path to the user's D-PC configuration directory
DPC_HOME_DIR = Path.home() / ".dpc"

# Configuration file names
PROVIDERS_CONFIG = "providers.json"
PRIVACY_RULES = "privacy_rules.json"
PERSONAL_CONTEXT = "personal.json"
KNOWN_PEERS = "known_peers.json"
NODE_KEY = "node.key"

class CoreService:
    """
    The main orchestrating class for the D-PC client's backend.
    Manages all sub-components and the application's lifecycle.
    """
    def __init__(self):
        logger.info("Initializing D-PC Core Service")

        DPC_HOME_DIR.mkdir(exist_ok=True)

        # Load settings (supports environment variables and config file)
        self.settings = Settings(DPC_HOME_DIR)

        # Initialize offline mode components
        self.token_cache = TokenCache(
            cache_dir=DPC_HOME_DIR,
            node_key_path=DPC_HOME_DIR / NODE_KEY
        )
        self.peer_cache = PeerCache(DPC_HOME_DIR / KNOWN_PEERS)
        self.connection_status = ConnectionStatus()

        # Set up status change callback
        self.connection_status.set_on_status_change(self._on_connection_status_changed)

        # Initialize all major components
        self.firewall = ContextFirewall(DPC_HOME_DIR / PRIVACY_RULES)
        self.llm_manager = LLMManager(DPC_HOME_DIR / PROVIDERS_CONFIG)
        self.hub_client = HubClient(
            api_base_url=self.settings.get_hub_url(),
            oauth_callback_host=self.settings.get_oauth_callback_host(),
            oauth_callback_port=self.settings.get_oauth_callback_port(),
            token_cache=self.token_cache  # Pass token cache for offline mode
        )
        self.p2p_manager = P2PManager(firewall=self.firewall, settings=self.settings)
        self.cache = ContextCache()

        self.local_api = LocalApiServer(core_service=self)

        # Knowledge Architecture components (Phase 1-6)
        self.pcm_core = PCMCore(DPC_HOME_DIR / PERSONAL_CONTEXT)

        # Migrate instructions to separate file (one-time operation)
        migrate_instructions_from_personal_context()

        # Load AI instructions from instructions.json
        self.instructions = load_instructions()
        logger.info("AI instructions loaded from instructions.json")

        # Auto-collect device context on startup (if enabled)
        self.device_context = None
        if self.settings.get_auto_collect_device_info():
            try:
                from .device_context_collector import DeviceContextCollector
                device_collector = DeviceContextCollector(settings=self.settings)

                # Generate/update device_context.json
                device_file = device_collector.collect_and_save()
                logger.info("Device context saved to %s", device_file.name)

                # Update personal.json with reference to device_context.json
                context = self.pcm_core.load_context()
                context = device_collector.update_personal_context_reference(context)
                self.pcm_core.save_context(context)

                # Load device context for AI use
                with open(device_file, 'r', encoding='utf-8') as f:
                    self.device_context = json.load(f)
                logger.info("Device context loaded and referenced in personal.json")
            except Exception as e:
                logger.warning("Failed to collect device context: %s", e)
                # Continue service startup even if collection fails

        # Set display name from personal context (for P2P handshakes)
        try:
            context = self.pcm_core.load_context()
            if context.profile and context.profile.name:
                self.p2p_manager.set_display_name(context.profile.name)
                logger.info("Display name set from personal context: %s", context.profile.name)
        except Exception as e:
            logger.warning("Failed to load display name from personal context: %s", e)

        self.consensus_manager = ConsensusManager(
            node_id=self.p2p_manager.node_id,
            pcm_core=self.pcm_core,
            vote_timeout_minutes=10
        )

        # Register callback to reload context and notify peers after commit
        self.consensus_manager.on_commit_applied = self._on_commit_applied

        # Register callback to broadcast peer proposals to UI
        self.consensus_manager.on_proposal_received = self._on_proposal_received_from_peer

        # Register callback to broadcast voting results to participants
        self.consensus_manager.on_result_broadcast = self._broadcast_commit_result

        # Phase 6 managers will be initialized in start() after DHT is created by P2PManager
        self.hole_punch_manager = None
        self.relay_manager = None
        self.gossip_manager = None
        self.connection_orchestrator = None

        # Inference orchestrator (coordinates local and remote AI inference)
        self.inference_orchestrator = InferenceOrchestrator(self)

        # Context coordinator (coordinates context requests and responses)
        self.context_coordinator = ContextCoordinator(self)

        # P2P coordinator (coordinates P2P connection lifecycle)
        self.p2p_coordinator = P2PCoordinator(self)

        # File transfer manager (handles P2P file transfers)
        self.file_transfer_manager = FileTransferManager(
            p2p_manager=self.p2p_manager,
            firewall=self.firewall,
            settings=self.settings,
            local_api=self.local_api,
            service=self
        )

        # Conversation monitors (per conversation/peer for knowledge extraction)
        # conversation_id -> ConversationMonitor
        self.conversation_monitors: Dict[str, ConversationMonitor] = {}

        # Knowledge extraction settings
        self.auto_knowledge_detection_enabled: bool = False  # Can be toggled by user (matches UI default)

        # External IP discovery (via STUN)
        self._external_ip: str | None = None  # Will be populated by STUN discovery

        self._is_running = False
        self._background_tasks = set()
        
        # Store peer metadata (names, profiles, etc.)
        self.peer_metadata: Dict[str, Dict[str, Any]] = {}

        # Track pending inference requests (for request-response matching)
        self._pending_inference_requests: Dict[str, asyncio.Future] = {}

        # Hub reconnection settings
        self._hub_reconnect_attempts = 0
        self._max_hub_reconnect_attempts = 5

        # Set up callbacks AFTER all components are initialized
        self.p2p_manager.set_core_service_ref(self)
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.p2p_manager.set_on_message_received(self.on_p2p_message_received)
        self.p2p_manager.set_on_peer_disconnected(self._handle_peer_disconnected)
        self._processed_message_ids = set()  # Track processed messages
        self._max_processed_ids = 1000  # Limit set size

        # Initialize message router and register handlers
        self.message_router = MessageRouter()
        self._register_message_handlers()

    def _register_message_handlers(self):
        """Register all P2P message handlers with the router."""
        # Text messaging
        self.message_router.register_handler(SendTextHandler(self))

        # Context sharing
        self.message_router.register_handler(RequestContextHandler(self))
        self.message_router.register_handler(ContextResponseHandler(self))
        self.message_router.register_handler(RequestDeviceContextHandler(self))
        self.message_router.register_handler(DeviceContextResponseHandler(self))

        # Remote inference (compute sharing)
        self.message_router.register_handler(RemoteInferenceRequestHandler(self))
        self.message_router.register_handler(RemoteInferenceResponseHandler(self))

        # Provider discovery
        self.message_router.register_handler(GetProvidersHandler(self))
        self.message_router.register_handler(ProvidersResponseHandler(self))

        # Knowledge commits and context updates
        self.message_router.register_handler(ContextUpdatedHandler(self))
        self.message_router.register_handler(ProposeKnowledgeCommitHandler(self))
        self.message_router.register_handler(VoteKnowledgeCommitHandler(self))
        self.message_router.register_handler(KnowledgeCommitResultHandler(self))

        # Peer handshake
        self.message_router.register_handler(HelloHandler(self))

        # Gossip protocol handlers
        self.message_router.register_handler(GossipSyncHandler(self))  # Anti-entropy sync
        self.message_router.register_handler(GossipMessageHandler(self))  # Epidemic routing

        # Volunteer relay handlers (server mode)
        self.message_router.register_handler(RelayRegisterHandler(self))  # Relay session registration
        self.message_router.register_handler(RelayMessageHandler(self))  # Message forwarding
        self.message_router.register_handler(RelayDisconnectHandler(self))  # Session cleanup

        # File transfer handlers
        self.message_router.register_handler(FileOfferHandler(self))
        self.message_router.register_handler(FileAcceptHandler(self))
        self.message_router.register_handler(FileChunkHandler(self))
        self.message_router.register_handler(FileCompleteHandler(self))
        self.message_router.register_handler(FileCancelHandler(self))
        self.message_router.register_handler(FileChunkRetryHandler(self))  # v0.11.1

        logger.info("Registered %d message handlers", len(self.message_router.get_registered_commands()))

    def _on_connection_status_changed(self, old_mode: OperationMode, new_mode: OperationMode):
        """Callback when connection status changes."""
        logger.info("Connection Status Change: %s -> %s", old_mode.value, new_mode.value)
        logger.info("Status: %s", self.connection_status.get_status_message())

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
            logger.error("Error notifying UI of status change: %s", e, exc_info=True)

    async def _startup_integrity_check(self):
        """Verify all knowledge commits on startup."""
        from dpc_protocol.commit_integrity import verify_markdown_integrity
        from dpc_protocol.crypto import DPC_HOME_DIR

        knowledge_dir = DPC_HOME_DIR / "knowledge"
        warnings = []
        verified_count = 0

        # CHECK 1: Verify existing markdown files
        if knowledge_dir.exists():
            for markdown_file in knowledge_dir.glob("*_commit-*.md"):
                result = verify_markdown_integrity(markdown_file, knowledge_dir)

                if result['valid']:
                    verified_count += 1
                else:
                    warnings.extend(result['warnings'])

        # CHECK 2: Detect orphaned markdown references in personal.json
        context = self.pcm_core.load_context()
        orphaned_count = 0

        for topic_name, topic in context.knowledge.items():
            if topic.markdown_file:
                markdown_path = DPC_HOME_DIR / topic.markdown_file

                if not markdown_path.exists():
                    orphaned_count += 1
                    warnings.append({
                        'file': topic.markdown_file,
                        'type': 'missing_markdown',
                        'severity': 'warning',
                        'topic': topic_name,
                        'commit_id': getattr(topic, 'commit_id', 'unknown'),
                        'message': f'Topic "{topic_name}" references deleted markdown file'
                    })

        # Report results
        if warnings:
            # Broadcast warning to UI
            if self.local_api:
                await self.local_api.broadcast_event("integrity_warnings", {
                    'count': len(warnings),
                    'warnings': warnings
                })

            logger.warning("Knowledge integrity: %d issues found", len(warnings))
            if orphaned_count > 0:
                logger.warning("%d topics with missing markdown files", orphaned_count)
            for w in warnings:
                logger.warning("[%s] %s: %s", w['severity'].upper(), w.get('file', w.get('topic')), w['message'])
        else:
            logger.info("Knowledge integrity verified (%d commits)", verified_count)

    async def start(self):
        """Starts all background services and runs indefinitely."""
        if self._is_running:
            logger.warning("Core Service is already running")
            return

        logger.info("D-PC Messenger v%s initializing...", __version__)

        self._shutdown_event = asyncio.Event()

        # Run knowledge integrity check
        logger.info("Running knowledge integrity check")
        await self._startup_integrity_check()

        # Start all background tasks
        listen_host = self.settings.get_p2p_listen_host()
        listen_port = self.settings.get_p2p_listen_port()
        p2p_task = asyncio.create_task(self.p2p_manager.start_server(host=listen_host, port=listen_port))
        p2p_task.set_name("p2p_server")
        self._background_tasks.add(p2p_task)

        api_task = asyncio.create_task(self.local_api.start())
        api_task.set_name("local_api")
        self._background_tasks.add(api_task)

        # Start STUN discovery task (background, non-blocking)
        stun_task = asyncio.create_task(self._discover_external_ip())
        stun_task.set_name("stun_discovery")
        self._background_tasks.add(stun_task)

        # Wait a moment for P2P server to start
        await asyncio.sleep(0.5)

        # Update Direct TLS status (always available if P2P server running)
        p2p_port = self.settings.get_p2p_listen_port()
        self.connection_status.update_direct_tls_status(available=True, port=p2p_port)
        logger.info(f"Direct TLS server started on port {p2p_port}")

        # Initialize Phase 6 managers (after P2PManager has created DHT Manager)
        # DHT Manager is created and started by P2PManager.start_server()
        dht_manager = self.p2p_manager.dht_manager
        if dht_manager:
            logger.info("DHT Manager available from P2PManager")
            self.connection_status.update_dht_status(True)

            # Hole Punch Manager (Phase 6 - UDP NAT traversal)
            if self.settings.get_enable_hole_punching():
                hole_punch_port = self.settings.get_hole_punch_port()
                self.hole_punch_manager = HolePunchManager(
                    dht_manager=dht_manager,
                    punch_port=hole_punch_port,
                    discovery_peers=3,
                    punch_timeout=self.settings.get_hole_punch_timeout()
                )
                logger.info("Hole Punch Manager initialized on port %d", hole_punch_port)
                self.connection_status.update_hole_punch_status(True)
            else:
                logger.info("Hole Punch Manager disabled (requires DTLS encryption in v0.11.0+)")
                self.connection_status.update_hole_punch_status(False)

            # Relay Manager (Phase 6 - Volunteer relay nodes)
            relay_volunteer = self.settings.get_relay_volunteer()
            self.relay_manager = RelayManager(
                dht_manager=dht_manager,
                p2p_manager=self.p2p_manager,
                hole_punch_manager=self.hole_punch_manager,
                volunteer=relay_volunteer,
                max_peers=self.settings.get_relay_max_peers(),
                bandwidth_limit_mbps=self.settings.get_relay_bandwidth_limit(),
                region="global"
            )
            logger.info("Relay Manager initialized (volunteer=%s)", relay_volunteer)
            self.connection_status.update_relay_status(True)

            # Gossip Manager (Phase 6 - Store-and-forward messaging)
            self.gossip_manager = GossipManager(
                p2p_manager=self.p2p_manager,
                node_id=self.p2p_manager.node_id,
                message_router=self.message_router,
                fanout=self.settings.get_gossip_fanout(),
                max_hops=self.settings.get_gossip_max_hops(),
                ttl_seconds=self.settings.get_gossip_ttl(),
                sync_interval=self.settings.get_gossip_sync_interval()
            )
            logger.info("Gossip Manager initialized with message router integration")
            self.connection_status.update_gossip_status(True)

            # Connection Orchestrator (Phase 6 - 6-tier connection fallback)
            self.connection_orchestrator = ConnectionOrchestrator(
                p2p_manager=self.p2p_manager,
                dht_manager=dht_manager,
                hub_client=self.hub_client,
                hole_punch_manager=self.hole_punch_manager,
                relay_manager=self.relay_manager,
                gossip_manager=self.gossip_manager
            )
            logger.info("Connection Orchestrator initialized with 6-tier fallback")
        else:
            logger.warning("DHT Manager not available - Phase 6 features disabled")
            self.connection_status.update_dht_status(False)
            self.connection_status.update_hole_punch_status(False)
            self.connection_status.update_relay_status(False)
            self.connection_status.update_gossip_status(False)

        # Start Hole Punch Manager (if enabled)
        if self.hole_punch_manager:
            logger.info("Starting Hole Punch Manager...")
            hole_punch_task = asyncio.create_task(self.hole_punch_manager.start())
            hole_punch_task.set_name("hole_punch_manager")
            self._background_tasks.add(hole_punch_task)

        # Start Relay Manager (if available)
        if self.relay_manager:
            logger.info("Starting Relay Manager...")
            # Relay manager doesn't have a start() method, it's on-demand
            if self.relay_manager.volunteer:
                # Announce relay availability after DHT bootstrap
                await asyncio.sleep(1.0)  # Wait for DHT bootstrap
                relay_announce_task = asyncio.create_task(self.relay_manager.announce_relay_availability())
                relay_announce_task.set_name("relay_announce")
                self._background_tasks.add(relay_announce_task)

        # Start Gossip Manager (if available)
        if self.gossip_manager:
            logger.info("Starting Gossip Manager...")
            gossip_task = asyncio.create_task(self.gossip_manager.start())
            gossip_task.set_name("gossip_manager")
            self._background_tasks.add(gossip_task)

        if dht_manager:
            logger.info("Phase 6 managers started (DHT, Hole Punch, Relay, Gossip)")
        else:
            logger.warning("Phase 6 managers unavailable (DHT not initialized)")

        # Try to connect to Hub for WebRTC signaling (with graceful degradation)
        hub_connected = False

        # Check if auto-connect is enabled in config
        if self.settings.get_hub_auto_connect():
            try:
                # Use configured default provider
                default_provider = self.settings.get_oauth_default_provider()
                logger.info("Auto-Connect using OAuth provider: %s", default_provider)

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

                logger.info("Hub connected - WebRTC available")

            except Exception as e:
                logger.warning("Offline Mode - Hub connection failed: %s", e)
                logger.info("Operating in %s mode", self.connection_status.get_operation_mode().value)
                logger.info("Status: %s", self.connection_status.get_status_message())
                logger.info("Direct TLS connections still available via dpc:// URIs")

                # Update connection status
                self.connection_status.update_hub_status(connected=False, error=str(e))
                self.connection_status.update_webrtc_status(available=False)
        else:
            logger.info("Auto-Connect disabled in config - use UI to connect to Hub manually")
            logger.info("Operating in %s mode", self.connection_status.get_operation_mode().value)
            logger.info("Direct TLS connections available via dpc:// URIs")

            # Update connection status
            self.connection_status.update_hub_status(connected=False)
            self.connection_status.update_webrtc_status(available=False)

        self._is_running = True
        logger.info("D-PC Core Service started")
        logger.info("Node ID: %s", self.p2p_manager.node_id)
        logger.info("Operation Mode: %s", self.connection_status.get_operation_mode().value)
        logger.info("Available Features:")
        for feature, available in self.connection_status.get_available_features().items():
            status = "available" if available else "unavailable"
            logger.info("  %s: %s", feature, status)

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
        logger.info("Stopping D-PC Core Service")
        self._shutdown_event.set()

    async def shutdown(self):
        """Performs a clean shutdown of all components."""
        self._is_running = False
        logger.info("Shutting down components")

        # Cancel all background tasks first
        logger.debug("Cancelling %d background tasks", len(self._background_tasks))
        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        # Wait for all background tasks to finish
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Shutdown Phase 6 managers
        if hasattr(self, 'dht_manager'):
            logger.info("Stopping DHT Manager...")
            await self.dht_manager.stop()

        if hasattr(self, 'hole_punch_manager') and self.hole_punch_manager:
            logger.info("Stopping Hole Punch Manager...")
            await self.hole_punch_manager.stop()

        if hasattr(self, 'gossip_manager') and self.gossip_manager:
            logger.info("Stopping Gossip Manager...")
            await self.gossip_manager.stop()

        # Shutdown core components
        await self.p2p_manager.shutdown_all()
        await self.local_api.stop()
        await self.hub_client.close()
        logger.info("D-PC Core Service shut down")

    async def _discover_external_ip(self):
        """
        Background task to discover external IP address via STUN servers.
        Runs on startup and periodically refreshes.

        Features network resilience:
        - Checks internet connectivity before STUN attempts
        - Retries with exponential backoff on failure
        - Handles service startup before network is ready
        """
        try:
            # Get STUN servers from configuration
            stun_servers = self.settings.get_stun_servers()

            # Perform initial discovery with retry logic (3 attempts: 0s, 5s, 15s)
            logger.info("Discovering external IP address via STUN (network-resilient)")
            external_ip = await discover_external_ip(stun_servers, timeout=3.0, retry_count=3)

            if external_ip:
                self._external_ip = external_ip
                logger.info("External IP discovered: %s", external_ip)

                # Update DHT to announce with external IP (for internet-wide discovery)
                await self.p2p_manager.update_dht_ip(external_ip)

                # Notify UI of status update (to refresh external URIs)
                await self.local_api.broadcast_event("status_update", await self.get_status())
            else:
                logger.warning("Could not discover external IP via STUN servers (will retry periodically)")

            # Periodically refresh external IP (every 5 minutes)
            while self._is_running:
                await asyncio.sleep(300)  # 5 minutes

                # Periodic check uses single retry (faster)
                logger.debug("Running periodic STUN re-discovery")
                external_ip = await discover_external_ip(stun_servers, timeout=3.0, retry_count=1)

                if external_ip:
                    if external_ip != self._external_ip:
                        logger.info("External IP changed: %s -> %s",
                                  self._external_ip or "(none)", external_ip)
                        self._external_ip = external_ip

                        # Update DHT with new external IP
                        await self.p2p_manager.update_dht_ip(external_ip)

                        # Notify UI
                        await self.local_api.broadcast_event("status_update", await self.get_status())
                    else:
                        logger.debug("Periodic discovery: External IP unchanged (%s)", external_ip)
                else:
                    logger.debug("Periodic STUN re-discovery failed (network may be down)")

        except asyncio.CancelledError:
            logger.debug("STUN discovery task cancelled")
        except Exception as e:
            logger.error("STUN discovery error: %s", e, exc_info=True)

    async def _monitor_hub_connection(self):
        """Background task to monitor hub connection and auto-reconnect if needed."""
        while self._is_running:
            try:
                await asyncio.sleep(10)

                if not self.hub_client.websocket or \
                self.hub_client.websocket.state != websockets.State.OPEN:

                    # Check if we've exceeded max reconnection attempts
                    if self._hub_reconnect_attempts >= self._max_hub_reconnect_attempts:
                        logger.warning("Hub offline - max reconnection attempts (%d) reached", self._max_hub_reconnect_attempts)
                        logger.info("Staying in offline mode - use Login to Hub button to reconnect manually")
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

                    logger.info("Hub connection lost, attempting to reconnect (attempt %d/%d)", self._hub_reconnect_attempts, self._max_hub_reconnect_attempts)

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
                        logger.debug("Cancelling old Hub signal listener")
                        old_listener.cancel()
                        try:
                            await old_listener
                        except asyncio.CancelledError:
                            pass
                        self._background_tasks.discard(old_listener)

                    # Close old websocket BEFORE reconnecting
                    if self.hub_client.websocket:
                        logger.debug("Closing old Hub websocket")
                        try:
                            await self.hub_client.websocket.close()
                        except:
                            pass
                        self.hub_client.websocket = None

                    # Wait before reconnection attempt (exponential backoff)
                    logger.info("Waiting %ds before reconnection", backoff_delay)
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

                        logger.info("Hub reconnection successful")
                        await self.local_api.broadcast_event("status_update", await self.get_status())

                    except PermissionError as e:
                        logger.warning("Hub reconnection failed - authentication expired: %s", e)
                        logger.info("Please login again to reconnect to Hub")
                        self.connection_status.update_hub_status(connected=False, error="Authentication expired")
                        # Don't count auth failures toward reconnect limit
                        self._hub_reconnect_attempts = self._max_hub_reconnect_attempts
                    except Exception as e:
                        logger.error("Hub reconnection failed: %s", e, exc_info=True)
                        self.connection_status.update_hub_status(connected=False, error=str(e))
                else:
                    # Connection is healthy, reset reconnection attempts
                    if self._hub_reconnect_attempts > 0:
                        self._hub_reconnect_attempts = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in hub connection monitor: %s", e, exc_info=True)

    async def _listen_for_hub_signals(self):
        """Background task that listens for incoming WebRTC signaling messages from Hub."""
        logger.info("Started listening for Hub WebRTC signals")

        try:
            while self._is_running:
                try:
                    if not self.hub_client.websocket or \
                    self.hub_client.websocket.state != websockets.State.OPEN:
                        logger.info("Hub signaling socket disconnected, stopping listener")
                        break

                    signal = await self.hub_client.receive_signal()
                    await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)

                except asyncio.CancelledError:
                    break
                except ConnectionError as e:
                    logger.warning("Hub connection lost: %s", e)
                    break
                except Exception as e:
                    logger.error("Error receiving Hub signal: %s", e, exc_info=True)
                    await asyncio.sleep(1)
        finally:
            logger.info("Stopped listening for Hub signals")
            await self.local_api.broadcast_event("status_update", await self.get_status())


    # --- Callback Methods (called by P2PManager) ---

    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager when peer list changes."""
        logger.debug("Peer list changed, broadcasting status update to UI")

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
        Routes messages to appropriate handlers via message router.
        """
        command = message.get("command")
        logger.debug("Received message from %s: %s", sender_node_id, command)

        # Route message to registered handler
        await self.message_router.route_message(sender_node_id, message)

    async def _handle_peer_disconnected(self, peer_id: str):
        """
        Callback triggered when a peer disconnects (intentionally or connection lost).
        Cleans up all active file transfers with the disconnected peer.
        """
        logger.info(f"Handling peer disconnection cleanup for {peer_id}")

        # Find all active transfers with this peer
        transfers_to_cancel = [
            (transfer_id, transfer)
            for transfer_id, transfer in self.file_transfer_manager.active_transfers.items()
            if transfer.node_id == peer_id
        ]

        # Cancel each transfer and notify UI
        for transfer_id, transfer in transfers_to_cancel:
            logger.info(f"Cancelling transfer {transfer_id} due to peer disconnection")

            # Delete transfer locally (don't send FILE_CANCEL since peer is already disconnected)
            del self.file_transfer_manager.active_transfers[transfer_id]

            # Broadcast cancellation event to UI
            await self.local_api.broadcast_event("file_transfer_cancelled", {
                "transfer_id": transfer_id,
                "node_id": peer_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": "peer_disconnected",
                "status": "cancelled"
            })

        if transfers_to_cancel:
            logger.info(f"Cancelled {len(transfers_to_cancel)} active transfer(s) with {peer_id}")

    # --- High-level methods (API for the UI) ---

    def _is_global_ipv6(self, ip: str) -> bool:
        """
        Check if an IPv6 address is globally routable (not private/ULA).

        Global IPv6 ranges:
        - 2000::/3 - Global Unicast
        - 2001:0::/32 - Teredo (globally routable IPv6-over-IPv4 tunnel)

        Excludes:
        - fe80::/10 - Link-local (already filtered out)
        - fc00::/7  - Unique Local Addresses (ULA) - private
        - fd00::/8  - ULA (subset of fc00::/7)
        - ::1       - Loopback (already filtered out)
        - ff00::/8  - Multicast

        Args:
            ip: IPv6 address string (e.g., "2001:db8::1234" or "2001:0:1234:5678:90ab:cdef:1234:5678")

        Returns:
            True if globally routable, False otherwise
        """
        import ipaddress
        try:
            addr = ipaddress.IPv6Address(ip)

            # Special case: Teredo addresses (2001:0::/32) are globally routable
            # Python's ipaddress incorrectly marks them as private
            teredo_network = ipaddress.IPv6Network("2001:0::/32")
            if addr in teredo_network:
                return True

            # Check if it's global unicast (2000::/3)
            if addr.is_global:
                return True

            # Exclude ULA (fc00::/7 and fd00::/8) and other private ranges
            if addr.is_private:
                return False

            # Exclude multicast
            if addr.is_multicast:
                return False

            return False

        except Exception:
            return False

    def _is_usable_ipv4(self, ip_str: str) -> bool:
        """
        Check if IPv4 address is usable for P2P connections.

        Filters out:
        - Loopback addresses (127.0.0.0/8)
        - Link-local addresses (169.254.0.0/16 - APIPA/DHCP failure)

        Args:
            ip_str: IPv4 address string

        Returns:
            True if address is usable for P2P, False otherwise
        """
        try:
            import ipaddress
            addr = ipaddress.IPv4Address(ip_str)

            # Filter loopback (127.x.x.x)
            if addr.is_loopback:
                logger.debug("Filtering IPv4 loopback address: %s", ip_str)
                return False

            # Filter link-local (169.254.x.x - APIPA addresses when DHCP fails)
            if addr.is_link_local:
                logger.debug("Filtering IPv4 link-local (APIPA) address: %s", ip_str)
                return False

            return True

        except ValueError:
            # Invalid IPv4 address
            logger.warning("Invalid IPv4 address format: %s", ip_str)
            return False

    def _get_local_ips(self) -> List[str]:
        """
        Get local network IP addresses (excluding loopback and link-local).
        Returns a list of IP addresses (IPv4 and IPv6) for constructing dpc:// URIs.
        Uses multiple methods for cross-platform compatibility and combines results.

        Filters:
        - IPv4: Loopback (127.x) and link-local (169.254.x - APIPA)
        - IPv6: Loopback (::1) and link-local (fe80::)
        """
        local_ips = []
        errors = []

        # Method 1: Use socket to external address (most reliable, finds primary interface)
        # Try IPv4
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            # Use new filtering method
            if local_ip and self._is_usable_ipv4(local_ip):
                local_ips.append(local_ip)
                logger.debug("Found local IPv4 via socket method: %s", local_ip)
        except Exception as e:
            errors.append(f"Socket method (IPv4): {e}")

        # Try IPv6
        try:
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            # Use Google's IPv6 DNS server
            s6.connect(("2001:4860:4860::8888", 80))
            local_ip6 = s6.getsockname()[0]
            s6.close()
            # Filter out link-local (fe80::) and loopback (::1) addresses
            if local_ip6 and not local_ip6.startswith('fe80:') and local_ip6 != '::1':
                local_ips.append(local_ip6)
                logger.debug("Found local IPv6 via socket method: %s", local_ip6)
        except Exception as e:
            errors.append(f"Socket method (IPv6): {e}")

        # Method 2: Try hostname resolution (finds all interfaces)
        # IPv4
        try:
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)

            for info in addr_info:
                ip = info[4][0]
                # Use new filtering method for loopback and link-local
                if ip and self._is_usable_ipv4(ip) and ip not in local_ips:
                    local_ips.append(ip)
                    logger.debug("Found local IPv4 via hostname method: %s", ip)

        except Exception as e:
            errors.append(f"Hostname method (IPv4): {e}")

        # IPv6
        try:
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET6)

            for info in addr_info:
                ip = info[4][0]
                # Filter out link-local (fe80::), loopback (::1), and duplicates
                if ip and not ip.startswith('fe80:') and ip != '::1' and ip not in local_ips:
                    local_ips.append(ip)
                    logger.debug("Found local IPv6 via hostname method: %s", ip)

        except Exception as e:
            errors.append(f"Hostname method (IPv6): {e}")

        # Method 3: Platform-specific (Linux/Unix only - finds all interfaces)
        if sys.platform.startswith('linux'):
            try:
                import subprocess
                # Use 'ip addr' command on Linux
                result = subprocess.run(['ip', 'addr', 'show'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    import re
                    # Look for inet addresses (IPv4)
                    for match in re.finditer(r'inet\s+(\d+\.\d+\.\d+\.\d+)', result.stdout):
                        ip = match.group(1)
                        # Use new filtering method
                        if self._is_usable_ipv4(ip) and ip not in local_ips:
                            local_ips.append(ip)
                            logger.debug("Found local IPv4 via 'ip addr' command: %s", ip)

                    # Look for inet6 addresses (IPv6)
                    for match in re.finditer(r'inet6\s+([0-9a-f:]+)', result.stdout):
                        ip = match.group(1)
                        # Filter out link-local (fe80::) and loopback (::1)
                        if not ip.startswith('fe80:') and ip != '::1' and ip not in local_ips:
                            local_ips.append(ip)
                            logger.debug("Found local IPv6 via 'ip addr' command: %s", ip)
            except Exception as e:
                errors.append(f"Linux 'ip addr' method: {e}")

        # Report results
        if not local_ips:
            logger.warning("Could not determine any local IP addresses")
            logger.warning("Errors encountered: %s", '; '.join(errors))
        else:
            logger.info("Successfully detected %d local IP(s): %s", len(local_ips), local_ips)

        return local_ips

    async def get_status(self) -> Dict[str, Any]:
        """Aggregates status from all components."""

        hub_connected = (
            self.hub_client.websocket is not None and
            self.hub_client.websocket.state == websockets.State.OPEN
        )

        # Get peer info with names
        peer_info = []
        for peer_id, peer_conn in self.p2p_manager.peers.items():
            peer_data = {
                "node_id": peer_id,
                "name": self.peer_metadata.get(peer_id, {}).get("name", None),
                "strategy_used": getattr(peer_conn, 'strategy_used', None)
            }
            peer_info.append(peer_data)

        # Get active model name safely
        try:
            active_model = self.llm_manager.get_active_model_name()
        except (AttributeError, Exception):
            active_model = None

        # Get local IPs and construct dpc:// URIs
        local_ips = self._get_local_ips()
        dpc_port = self.settings.get_p2p_listen_port()
        dpc_uris = []

        for ip in local_ips:
            # IPv6 addresses need brackets in URIs: dpc://[2001:db8::1]:8888
            formatted_host = f"[{ip}]" if ":" in ip else ip
            uri = f"dpc://{formatted_host}:{dpc_port}?node_id={self.p2p_manager.node_id}"
            dpc_uris.append({
                "ip": ip,
                "port": dpc_port,
                "uri": uri,
                "ip_version": "IPv6" if ":" in ip else "IPv4"
            })

        # Get external IPs discovered via STUN servers
        # Priority: 1) Standalone STUN discovery (always available)
        #           2) WebRTC connection STUN discovery (only when WebRTC active)
        #           3) Global IPv6 addresses from local detection (Teredo, native IPv6)
        external_ips = []

        # Add standalone STUN-discovered IP first (if available)
        if self._external_ip:
            external_ips.append(self._external_ip)

        # Add WebRTC-discovered IPs (avoid duplicates)
        webrtc_ips = self.p2p_manager.get_external_ips()
        for ip in webrtc_ips:
            if ip not in external_ips:
                external_ips.append(ip)

        # Add global IPv6 addresses from local detection
        # These are assigned to local interfaces but are globally routable
        for ip in local_ips:
            if ":" in ip and self._is_global_ipv6(ip) and ip not in external_ips:
                external_ips.append(ip)

        # Build external URIs
        external_uris = []
        for ip in external_ips:
            # IPv6 addresses need brackets in URIs: dpc://[2001:db8::1]:8888
            formatted_host = f"[{ip}]" if ":" in ip else ip
            uri = f"dpc://{formatted_host}:{dpc_port}?node_id={self.p2p_manager.node_id}"
            external_uris.append({
                "ip": ip,
                "port": dpc_port,
                "uri": uri,
                "ip_version": "IPv6" if ":" in ip else "IPv4"
            })

        # Get connection orchestrator stats (if available)
        orchestrator_stats = None
        if self.connection_orchestrator:
            orchestrator_stats = self.connection_orchestrator.get_stats()

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
            # External URIs (from STUN server discovery)
            "external_ips": external_ips,
            "external_uris": external_uris,
            # Connection orchestrator stats (6-tier fallback metrics)
            "orchestrator_stats": orchestrator_stats,
        }
    
    async def list_providers(self) -> Dict[str, Any]:
        """
        Returns all available AI providers from providers.json.

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

    async def get_providers_config(self) -> Dict[str, Any]:
        """
        Get full providers configuration for editor.

        Returns:
            Dictionary with:
            - status: "success" or "error"
            - config: Full providers configuration dict
        """
        try:
            import json

            # Ensure config file exists (creates default if missing)
            self.llm_manager._ensure_config_exists()

            # Read from file
            with open(self.llm_manager.config_path, 'r') as f:
                config = json.load(f)

            return {
                "status": "success",
                "config": config
            }
        except Exception as e:
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_providers_config(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save and validate providers configuration.

        Args:
            config_dict: Full providers configuration dictionary

        Returns:
            Dictionary with status and message/errors
        """
        # Validate structure
        errors = self._validate_providers_config(config_dict)
        if errors:
            return {
                "status": "error",
                "errors": errors
            }

        try:
            # Save to JSON and reload providers
            self.llm_manager.save_config(config_dict)

            # Broadcast event
            await self.local_api.broadcast_event("providers_updated", {
                "message": "AI providers configuration updated"
            })

            return {
                "status": "success",
                "message": "Providers saved successfully"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": str(e)
            }

    async def query_ollama_model_info(self, provider_alias: str) -> Dict[str, Any]:
        """
        Query Ollama provider for model parameters and info.

        Args:
            provider_alias: Alias of the Ollama provider to query

        Returns:
            Dictionary with:
            - status: "success" or "error"
            - model_info: Dict with modelfile, parameters, num_ctx, details
            - message: Error message if failed
        """
        try:
            # Check if provider exists
            if provider_alias not in self.llm_manager.providers:
                return {
                    "status": "error",
                    "message": f"Provider '{provider_alias}' not found"
                }

            provider = self.llm_manager.providers[provider_alias]

            # Check if it's an Ollama provider
            from dpc_client_core.llm_manager import OllamaProvider
            if not isinstance(provider, OllamaProvider):
                return {
                    "status": "error",
                    "message": f"Provider '{provider_alias}' is not an Ollama provider (type: {type(provider).__name__})"
                }

            # Query model info
            model_info = await provider.get_model_info()

            return {
                "status": "success",
                "model_info": model_info,
                "provider_alias": provider_alias,
                "model": provider.model
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to query model info: {str(e)}"
            }

    def _validate_providers_config(self, config_dict: Dict[str, Any]) -> list:
        """
        Validate providers configuration structure.

        Args:
            config_dict: Configuration dictionary to validate

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        # Required top-level fields
        if "default_provider" not in config_dict:
            errors.append("Missing 'default_provider' field")
        if "providers" not in config_dict:
            errors.append("Missing 'providers' array")
            return errors

        # Validate each provider
        provider_aliases = []
        for i, provider in enumerate(config_dict["providers"]):
            prefix = f"Provider {i+1}"

            # Required fields for all providers
            if "alias" not in provider:
                errors.append(f"{prefix}: Missing 'alias'")
            else:
                alias = provider["alias"]
                if alias in provider_aliases:
                    errors.append(f"{prefix}: Duplicate alias '{alias}'")
                provider_aliases.append(alias)

            if "type" not in provider:
                errors.append(f"{prefix}: Missing 'type'")
            elif provider["type"] not in ["ollama", "openai_compatible", "anthropic"]:
                errors.append(f"{prefix}: Invalid type '{provider['type']}'")

            if "model" not in provider:
                errors.append(f"{prefix}: Missing 'model'")

            # Type-specific required fields
            provider_type = provider.get("type")
            if provider_type == "ollama" and "host" not in provider:
                errors.append(f"{prefix}: Ollama provider missing 'host'")
            if provider_type == "openai_compatible" and "base_url" not in provider:
                errors.append(f"{prefix}: OpenAI provider missing 'base_url'")

            # Optional context_window validation
            if "context_window" in provider:
                try:
                    cw = int(provider["context_window"])
                    if cw <= 0:
                        errors.append(f"{prefix}: context_window must be positive")
                except (ValueError, TypeError):
                    errors.append(f"{prefix}: context_window must be an integer")

        # Check default_provider exists
        default = config_dict.get("default_provider")
        if default and default not in provider_aliases:
            errors.append(f"Default provider '{default}' not found in providers list")

        return errors

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

        Loads knowledge from markdown files when markdown_file is set (v2.0 schema).
        """
        try:
            import hashlib
            from dpc_protocol.markdown_manager import MarkdownKnowledgeManager

            # Load JSON
            context = self.pcm_core.load_context()

            # Load knowledge from markdown files
            markdown_manager = MarkdownKnowledgeManager()

            for topic_name, topic in context.knowledge.items():
                if topic.markdown_file:
                    filepath = DPC_HOME_DIR / topic.markdown_file

                    if filepath.exists():
                        # Parse markdown with frontmatter
                        frontmatter, content = markdown_manager.parse_markdown_with_frontmatter(filepath)

                        # Verify integrity
                        if 'content_hash' in frontmatter:
                            actual_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
                            if actual_hash != frontmatter['content_hash']:
                                logger.warning(
                                    "Content hash mismatch for %s: %s",
                                    topic_name,
                                    frontmatter['commit_id']
                                )

                        # Convert markdown to entries
                        entries = markdown_manager.markdown_to_entries(content)
                        topic.entries = entries  # In-memory only
                    else:
                        logger.warning("Markdown file not found: %s", topic.markdown_file)

            return {
                "status": "success",
                "context": asdict(context)
            }
        except Exception as e:
            logger.error("Error loading personal context: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_personal_context(self, context_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Save updated personal context from UI editor.

        UI Integration: Called when user clicks 'Save' in ContextViewer.

        Args:
            context_dict: Dictionary representation of PersonalContext

        Returns:
            Dict with status and message
        """
        try:
            from dpc_protocol.pcm_core import PersonalContext
            from datetime import datetime, timezone

            # Load current context to preserve metadata
            current = self.pcm_core.load_context()

            # Ensure current is a PersonalContext object, not a dict
            if isinstance(current, dict):
                current = PersonalContext.from_dict(current)

            # Update fields from the editor
            # Note: We're doing a simple update here. For production, you might want
            # more sophisticated merging logic
            if "profile" in context_dict:
                current.profile.__dict__.update(context_dict["profile"])

            if "instruction" in context_dict:
                current.instruction.__dict__.update(context_dict["instruction"])

            if "knowledge" in context_dict:
                # For knowledge, we need to be more careful with the structure
                # This is a simplified version - full implementation would handle topics properly
                pass  # Knowledge editing is complex, leave for future enhancement

            # Update timestamp (metadata might be a dict)
            if isinstance(current.metadata, dict):
                current.metadata['last_updated'] = datetime.now(timezone.utc).isoformat()
            else:
                current.metadata.last_updated = datetime.now(timezone.utc).isoformat()

            # Save to disk
            self.pcm_core.save_context(current)

            # Reload in P2PManager if it exists
            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                self.p2p_manager.local_context = current
                # Also update display name cache for HELLO messages
                if current.profile and current.profile.name:
                    self.p2p_manager.set_display_name(current.profile.name)
                    # Notify all connected peers of the name change
                    logger.info("Notifying connected peers of name change")
                    await self._notify_peers_of_name_change(current.profile.name)

            # Phase 7: Compute new context hash after saving
            new_context_hash = self._compute_context_hash()

            # Emit event to UI with new hash (Phase 7: for status indicators)
            await self.local_api.broadcast_event("personal_context_updated", {
                "message": "Personal context saved successfully",
                "context_hash": new_context_hash
            })

            # Phase 7: Broadcast CONTEXT_UPDATED to all connected peers
            # This notifies peers to invalidate their cache and re-fetch on next query
            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                await self._broadcast_context_updated_to_peers(new_context_hash)

            return {
                "status": "success",
                "message": "Personal context saved successfully"
            }

        except Exception as e:
            logger.error("Error saving personal context: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reload_personal_context(self) -> Dict[str, Any]:
        """Reload personal context from disk.

        UI Integration: Called when user clicks 'Reload' or when external changes detected.

        Returns:
            Dict with status, message, and updated context
        """
        try:
            context = self.pcm_core.load_context()

            # Update in P2PManager
            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                self.p2p_manager.local_context = context
                # Also update display name cache for HELLO messages
                if context.profile and context.profile.name:
                    self.p2p_manager.set_display_name(context.profile.name)
                    # Notify all connected peers of the name change
                    logger.info("Notifying connected peers of name change")
                    await self._notify_peers_of_name_change(context.profile.name)

            # Emit event to UI
            await self.local_api.broadcast_event("personal_context_reloaded", {
                "context": asdict(context)
            })

            return {
                "status": "success",
                "message": "Personal context reloaded from disk",
                "context": asdict(context)
            }

        except Exception as e:
            logger.error("Error reloading personal context: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_instructions(self) -> Dict[str, Any]:
        """Load and return AI instructions for UI display.

        UI Integration: Called when user opens InstructionsEditor component.
        Returns the InstructionBlock with all settings.
        """
        try:
            instructions = load_instructions()
            return {
                "status": "success",
                "instructions": asdict(instructions)
            }
        except Exception as e:
            logger.error("Error loading instructions: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_instructions(self, instructions_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Save updated AI instructions from UI editor.

        UI Integration: Called when user clicks 'Save' in InstructionsEditor.

        Args:
            instructions_dict: Dictionary representation of InstructionBlock

        Returns:
            Dict with status and message
        """
        try:
            # Create InstructionBlock from dict
            instructions = InstructionBlock(
                primary=instructions_dict.get('primary', InstructionBlock().primary),
                context_update=instructions_dict.get('context_update', InstructionBlock().context_update),
                verification_protocol=instructions_dict.get('verification_protocol', InstructionBlock().verification_protocol),
                learning_support=instructions_dict.get('learning_support', InstructionBlock().learning_support),
                bias_mitigation=instructions_dict.get('bias_mitigation', InstructionBlock().bias_mitigation),
                collaboration_mode=instructions_dict.get('collaboration_mode', 'individual'),
                consensus_required=instructions_dict.get('consensus_required', True),
                ai_curation_enabled=instructions_dict.get('ai_curation_enabled', True),
                dissent_encouraged=instructions_dict.get('dissent_encouraged', True)
            )

            # Save to disk
            save_instructions(instructions)

            # Update in memory
            self.instructions = instructions

            # Emit event to UI
            await self.local_api.broadcast_event("instructions_updated", {
                "message": "AI instructions saved successfully"
            })

            return {
                "status": "success",
                "message": "AI instructions saved successfully"
            }

        except Exception as e:
            logger.error("Error saving instructions: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reload_instructions(self) -> Dict[str, Any]:
        """Reload AI instructions from disk.

        UI Integration: Called when user clicks 'Reload' or when external changes detected.

        Returns:
            Dict with status, message, and updated instructions
        """
        try:
            instructions = load_instructions()

            # Update in memory
            self.instructions = instructions

            # Emit event to UI
            await self.local_api.broadcast_event("instructions_reloaded", {
                "instructions": asdict(instructions)
            })

            return {
                "status": "success",
                "message": "AI instructions reloaded from disk",
                "instructions": asdict(instructions)
            }

        except Exception as e:
            logger.error("Error reloading instructions: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_token_usage(self, conversation_id: str) -> Dict[str, Any]:
        """Get token usage statistics for a conversation

        UI Integration: Called to display token counter for a chat.

        Args:
            conversation_id: ID of the conversation to get token usage for

        Returns:
            Dict with token usage statistics
        """
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            usage = monitor.get_token_usage()

            return {
                "status": "success",
                **usage
            }

        except Exception as e:
            logger.error("Error getting token usage for %s: %s", conversation_id, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
                "tokens_used": 0,
                "token_limit": 100000,
                "usage_percent": 0.0
            }

    async def get_firewall_rules(self) -> Dict[str, Any]:
        """Get current firewall rules as JSON dict for editor.

        UI Integration: Called when user opens Firewall Editor.

        Returns:
            Dict with status and rules as JSON object
        """
        try:
            import json
            rules_text = self.firewall.access_file_path.read_text()
            rules_dict = json.loads(rules_text)
            return {
                "status": "success",
                "rules": rules_dict,
                "file_path": str(self.firewall.access_file_path)
            }
        except Exception as e:
            logger.error("Error reading firewall rules: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_firewall_rules(self, rules_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Save updated firewall rules from UI editor.

        UI Integration: Called when user clicks 'Save' in Firewall Editor.

        Args:
            rules_dict: New firewall rules as JSON dict

        Returns:
            Dict with status and message
        """
        try:
            import json
            from dpc_client_core.firewall import ContextFirewall

            # Validate before saving
            is_valid, errors = ContextFirewall.validate_config(rules_dict)

            if not is_valid:
                return {
                    "status": "error",
                    "message": "Validation failed",
                    "errors": errors
                }

            # Save to file
            rules_text = json.dumps(rules_dict, indent=2)
            self.firewall.access_file_path.write_text(rules_text)

            # Reload the firewall
            success, message = self.firewall.reload()

            if success:
                # Notify all connected peers of updated providers (compute sharing settings may have changed)
                logger.info("Notifying connected peers of provider changes")
                await self._notify_peers_of_provider_changes()

                # Emit event to UI
                await self.local_api.broadcast_event("firewall_rules_updated", {
                    "message": message
                })

                return {
                    "status": "success",
                    "message": message
                }
            else:
                return {
                    "status": "error",
                    "message": message
                }

        except Exception as e:
            logger.error("Error saving firewall rules: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reload_firewall(self) -> Dict[str, Any]:
        """Reload firewall rules from disk.

        UI Integration: Called when user clicks 'Reload' or when external changes detected.

        Returns:
            Dict with status and message
        """
        try:
            success, message = self.firewall.reload()

            if success:
                # Notify all connected peers of updated providers
                logger.info("Notifying connected peers of provider changes")
                await self._notify_peers_of_provider_changes()

                # Emit event to UI
                await self.local_api.broadcast_event("firewall_reloaded", {
                    "message": message
                })

                return {
                    "status": "success",
                    "message": message
                }
            else:
                return {
                    "status": "error",
                    "message": message
                }

        except Exception as e:
            logger.error("Error reloading firewall: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def validate_firewall_rules(self, rules_text: str) -> Dict[str, Any]:
        """Validate firewall rules without saving.

        UI Integration: Called on-the-fly while user edits rules.

        Args:
            rules_text: Firewall rules text to validate

        Returns:
            Dict with validation status and errors if any
        """
        try:
            from dpc_client_core.firewall import ContextFirewall

            is_valid, errors = ContextFirewall.validate_config(rules_text)

            return {
                "status": "success",
                "is_valid": is_valid,
                "errors": errors
            }

        except Exception as e:
            logger.error("Error validating firewall rules: %s", e, exc_info=True)
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
            logger.error("Error voting on knowledge commit: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def _broadcast_to_peers(self, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected peers.

        Used by ConsensusManager to broadcast votes and proposals.
        """
        await self.p2p_coordinator.broadcast_to_peers(message)

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
                knowledge_threshold=0.7,  # 70% confidence threshold
                settings=self.settings,  # Pass settings for config (e.g., cultural_perspectives_enabled)
                ai_query_func=self.send_ai_query,  # Enable both local and remote inference for knowledge detection
                auto_detect=self.auto_knowledge_detection_enabled  # Pass auto-detection setting
            )
            logger.info("Created conversation monitor for %s with %d participant(s) (auto_detect=%s)", conversation_id, len(participants), self.auto_knowledge_detection_enabled)

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
            logger.info("End Session - attempting manual extraction for %s", conversation_id)
            logger.info(
                "Full conversation: %d messages (incremental buffer: %d), Score: %.2f",
                len(monitor.full_conversation),
                len(monitor.message_buffer),
                monitor.knowledge_score
            )

            # Force knowledge extraction even if threshold not met
            proposal = await monitor.generate_commit_proposal(force=True)

            if proposal:
                logger.info("Knowledge proposal generated for %s", conversation_id)
                logger.info("Topic: %s, Entries: %d, Confidence: %.2f", proposal.topic, len(proposal.entries), proposal.avg_confidence)

                # Broadcast to UI
                await self.local_api.broadcast_event(
                    "knowledge_commit_proposed",
                    proposal.to_dict()
                )

                # For local_ai conversations, don't broadcast knowledge to peers (privacy)
                # For peer conversations, broadcast for collaborative consensus
                if conversation_id == "local_ai":
                    logger.info("Local AI - private conversation, knowledge will not be shared with peers")
                    # Use no-op broadcast function (local-only approval)
                    async def _no_op_broadcast(message: Dict[str, Any]) -> None:
                        pass  # Don't send to peers for private conversations

                    await self.consensus_manager.propose_commit(
                        proposal=proposal,
                        broadcast_func=_no_op_broadcast
                    )
                else:
                    # Peer conversation - broadcast for collaborative knowledge building
                    logger.info("Peer Chat - broadcasting knowledge proposal to peers for consensus")
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
                logger.info("No proposal generated - buffer was empty or no knowledge detected")
                return {
                    "status": "success",
                    "message": "No significant knowledge detected in conversation (buffer may be empty)"
                }

        except Exception as e:
            logger.error("Error ending conversation session: %s", e, exc_info=True)
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
            logger.info("Auto knowledge detection %s", state_text)

            # Update all existing conversation monitors to reflect the new setting
            for monitor in self.conversation_monitors.values():
                monitor.auto_detect = self.auto_knowledge_detection_enabled

            return {
                "status": "success",
                "enabled": self.auto_knowledge_detection_enabled,
                "message": f"Automatic knowledge detection {state_text}"
            }

        except Exception as e:
            logger.error("Error toggling auto knowledge detection: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reset_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Reset conversation history and context tracking for "New Chat" button.

        UI Integration: Called when user clicks "New Chat" button.

        Args:
            conversation_id: The conversation/chat ID to reset

        Returns:
            Dict with status
        """
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            monitor.reset_conversation()
            logger.info("Reset Conversation - cleared history for %s", conversation_id)

            # Broadcast to UI
            await self.local_api.broadcast_event(
                "conversation_reset",
                {"conversation_id": conversation_id}
            )

            return {
                "status": "success",
                "message": "Conversation reset successfully"
            }

        except Exception as e:
            logger.error("Error resetting conversation: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    # --- P2P Connection Methods ---

    async def connect_to_peer(self, uri: str):
        """
        Connect to a peer using a dpc:// URI (Direct TLS).

        Supports local network and external IP connections:
        - Local: dpc://192.168.1.100:8888?node_id=dpc-node-abc123...
        - External: dpc://203.0.113.5:8888?node_id=dpc-node-abc123...

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter
        """
        await self.p2p_coordinator.connect_via_uri(uri)

    async def test_port(self, uri: str) -> dict:
        """
        Test port connectivity before attempting full connection.

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter

        Returns:
            Dict with keys:
            - success (bool): Whether port is accessible
            - message (str): Diagnostic message
            - host (str): Target host
            - port (int): Target port
        """
        return await self.p2p_coordinator.test_port_connectivity(uri)

    async def connect_to_peer_by_id(self, node_id: str):
        """
        Orchestrates a P2P connection to a peer using its node_id via Hub.
        Uses WebRTC with NAT traversal.
        """
        await self.p2p_coordinator.connect_via_hub(node_id)

    async def connect_via_dht(self, node_id: str) -> dict:
        """
        Connect to a peer using DHT-first strategy.

        Tries discovery methods in order:
        1. DHT lookup (finds peer's current IP/port)
        2. Peer cache (last known connection)
        3. Hub WebRTC (future - Phase 5)

        Args:
            node_id: Target peer's node identifier (e.g., dpc-node-abc123...)

        Returns:
            Dict with connection result:
            - status: "success" or "error"
            - method: Discovery method used ("dht", "cache", "hub", or None)
            - message: Human-readable result message

        Example:
            result = await core_service.connect_via_dht("dpc-node-abc123...")
            # {"status": "success", "method": "dht", "message": "Connected via DHT"}
        """
        try:
            # Attempt connection using DHT-first strategy
            success = await self.p2p_manager.connect_via_node_id(node_id)

            if success:
                # Determine which method was used
                # Check if peer is now in connected peers
                if node_id in self.p2p_manager.peers:
                    # Try to determine discovery method from logs/state
                    # For now, assume DHT if it succeeded
                    return {
                        "status": "success",
                        "method": "dht",  # Could be "dht", "cache", or "hub"
                        "message": f"Connected to {node_id}"
                    }

            return {
                "status": "error",
                "method": None,
                "message": f"Failed to connect to {node_id} - peer not found via DHT, cache, or Hub"
            }

        except Exception as e:
            logger.error("DHT connection error for %s: %s", node_id, e, exc_info=True)
            return {
                "status": "error",
                "method": None,
                "message": f"Connection error: {str(e)}"
            }

    async def disconnect_from_peer(self, node_id: str):
        """Disconnect from a peer."""
        await self.p2p_coordinator.disconnect(node_id)

    async def send_p2p_message(self, target_node_id: str, text: str):
        """Send a text message to a connected peer."""
        await self.p2p_coordinator.send_message(target_node_id, text)

        # Track outgoing message in conversation monitor (v0.9.3 fix)
        try:
            from datetime import datetime
            from .conversation_monitor import Message as ConvMessage
            import hashlib
            import time

            monitor = self._get_or_create_conversation_monitor(target_node_id)

            # Create message object for outgoing message
            message_id = hashlib.sha256(
                f"{self.p2p_manager.node_id}:{text}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]

            outgoing_message = ConvMessage(
                message_id=message_id,
                conversation_id=target_node_id,
                sender_node_id=self.p2p_manager.node_id,
                sender_name="You",  # Outgoing messages from local user
                text=text,
                timestamp=datetime.utcnow().isoformat()
            )

            # Buffer the outgoing message (conversation monitor handles both directions)
            await monitor.on_message(outgoing_message)
        except Exception as e:
            logger.error("Error tracking outgoing message in conversation monitor: %s", e, exc_info=True)

    async def send_file(self, node_id: str, file_path: str):
        """
        Send a file to a peer via P2P file transfer.

        Args:
            node_id: Target peer's node ID
            file_path: Absolute path to file to send

        Returns:
            Dict with transfer_id and status
        """
        from pathlib import Path

        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Initiate file transfer (sends FILE_OFFER to peer)
        transfer_id = await self.file_transfer_manager.send_file(node_id, file)

        # Prepare file metadata
        size_bytes = file.stat().st_size
        size_mb = round(size_bytes / (1024 * 1024), 2)
        message_content = f"Sent file: {file.name} ({size_mb} MB)"

        attachments = [{
            "type": "file",
            "filename": file.name,
            "size_bytes": size_bytes,
            "size_mb": size_mb,
            "transfer_id": transfer_id,
            "status": "sending"
        }]

        # Note: Don't add to conversation history or broadcast message yet
        # We'll do that when FILE_COMPLETE is received (in file_complete_handler.py)
        # This prevents phantom messages if the receiver rejects the transfer

        return {
            "transfer_id": transfer_id,
            "status": "pending",
            "filename": file.name,
            "size_bytes": file.stat().st_size
        }

    async def accept_file_transfer(self, transfer_id: str):
        """
        Accept an incoming file transfer offer.

        Args:
            transfer_id: Transfer ID from FILE_OFFER

        Returns:
            Dict with transfer_id and status
        """
        transfer = self.file_transfer_manager.active_transfers.get(transfer_id)
        if not transfer:
            raise ValueError(f"Unknown transfer: {transfer_id}")

        if transfer.direction != "download":
            raise ValueError(f"Transfer {transfer_id} is not a download")

        # Send FILE_ACCEPT to peer
        await self.p2p_manager.send_message_to_peer(transfer.node_id, {
            "command": "FILE_ACCEPT",
            "payload": {"transfer_id": transfer_id}
        })

        return {
            "transfer_id": transfer_id,
            "status": "accepted"
        }

    async def cancel_file_transfer(self, transfer_id: str, reason: str = "user_cancelled"):
        """
        Cancel an active file transfer.

        Args:
            transfer_id: Transfer ID to cancel
            reason: Cancellation reason

        Returns:
            Dict with transfer_id and status
        """
        # Get transfer info BEFORE deletion (for UI notification)
        transfer = self.file_transfer_manager.active_transfers.get(transfer_id)

        # Cancel the transfer (sends FILE_CANCEL to peer and deletes locally)
        await self.file_transfer_manager.cancel_transfer(transfer_id, reason)

        # Broadcast cancellation event to local UI (so Active Transfers panel updates)
        if transfer:
            await self.local_api.broadcast_event("file_transfer_cancelled", {
                "transfer_id": transfer_id,
                "node_id": transfer.node_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": reason,
                "status": "cancelled"
            })

        return {
            "transfer_id": transfer_id,
            "status": "cancelled",
            "reason": reason
        }

    async def send_ai_query(self, prompt: str, compute_host: str = None, model: str = None, provider: str = None):
        """
        Send an AI query, either to local LLM or to a remote peer for inference.

        Delegates to InferenceOrchestrator for execution.

        Args:
            prompt: The prompt to send to the AI
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use

        Returns:
            Dict with 'response', 'model', 'provider', and 'compute_host' keys

        Raises:
            ValueError: If compute_host is specified but peer is not connected
            RuntimeError: If inference fails
        """
        return await self.inference_orchestrator.execute_inference(
            prompt=prompt,
            compute_host=compute_host,
            model=model,
            provider=provider
        )

    # --- Context Request Methods ---

    async def _handle_context_request(self, peer_id: str, query: str, request_id: str):
        """
        Handle incoming context request from a peer.

        Delegates to ContextCoordinator.
        """
        await self.context_coordinator.handle_context_request(peer_id, query, request_id)

    async def _handle_device_context_request(self, peer_id: str, request_id: str):
        """
        Handle incoming device context request from a peer.

        Delegates to ContextCoordinator.
        """
        await self.context_coordinator.handle_device_context_request(peer_id, request_id)

    async def _handle_inference_request(self, peer_id: str, request_id: str, prompt: str, model: str = None, provider: str = None):
        """
        Handle incoming remote inference request from a peer.
        Check firewall permissions, run inference, and send response.
        """
        from dpc_protocol.protocol import create_remote_inference_response

        logger.debug("Handling inference request from %s (request_id: %s)", peer_id, request_id)

        # Check if peer is allowed to request inference
        if not self.firewall.can_request_inference(peer_id, model):
            logger.warning("Access denied: %s cannot request inference%s", peer_id, f" for model {model}" if model else "")
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request inference" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        # Run inference
        try:
            logger.info("Running inference for %s (model: %s, provider: %s)", peer_id, model or 'default', provider or 'default')

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
                    logger.debug("Found provider '%s' for model '%s'", found_alias, model)
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            result = await self.llm_manager.query(prompt, provider_alias=provider_alias_to_use, return_metadata=True)
            logger.info("Inference completed successfully for %s", peer_id)

            # Send success response with token metadata
            success_response = create_remote_inference_response(
                request_id=request_id,
                response=result["response"],
                tokens_used=result.get("tokens_used"),
                prompt_tokens=result.get("prompt_tokens"),
                response_tokens=result.get("response_tokens"),
                model_max_tokens=result.get("model_max_tokens")
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)
            logger.debug("Sent inference result to %s", peer_id)

        except Exception as e:
            logger.error("Inference failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=str(e)
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending inference error response to %s: %s", peer_id, send_err, exc_info=True)

    async def _handle_get_providers_request(self, peer_id: str):
        """
        Handle GET_PROVIDERS request from a peer.
        Check firewall permissions and send available providers that the peer can use.
        """
        from dpc_protocol.protocol import create_providers_response

        logger.debug("Handling GET_PROVIDERS request from %s", peer_id)

        # Check if compute sharing is enabled and peer is authorized
        if not self.firewall.can_request_inference(peer_id):
            logger.warning("Access denied: %s cannot access compute resources", peer_id)
            # Send empty provider list (no access)
            response = create_providers_response([])
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, response)
            except Exception as e:
                logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)
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

        logger.debug("Sending %d providers to %s (filtered from %d total)", len(filtered_providers), peer_id, len(all_providers))

        # Send response with filtered providers
        response = create_providers_response(filtered_providers)
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
        except Exception as e:
            logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)

    async def _handle_providers_response(self, peer_id: str, providers: list):
        """
        Handle PROVIDERS_RESPONSE from a peer.
        Store the providers in peer metadata and broadcast to UI.
        """
        logger.debug("Received %d providers from %s", len(providers), peer_id)

        # Update peer metadata with providers
        if peer_id not in self.peer_metadata:
            self.peer_metadata[peer_id] = {}

        self.peer_metadata[peer_id]["providers"] = providers

        # Broadcast to UI
        await self.local_api.broadcast_event("peer_providers_updated", {
            "node_id": peer_id,
            "providers": providers
        })

    async def _notify_peers_of_name_change(self, new_name: str):
        """
        Notify all connected peers of name change (e.g., after personal context edit).
        Sends HELLO message to each peer so they can update their cached display name.
        """
        from dpc_protocol.protocol import create_hello_message

        connected_peers = list(self.p2p_manager.peers.keys())
        if not connected_peers:
            logger.debug("No connected peers to notify")
            return

        for peer_id in connected_peers:
            try:
                # Send HELLO message with updated name
                hello_msg = create_hello_message(self.p2p_manager.node_id, new_name)
                await self.p2p_manager.send_message_to_peer(peer_id, hello_msg)
                logger.debug("Notified %s of name change: %s", peer_id, new_name)
            except Exception as e:
                logger.error("Error notifying %s of name change: %s", peer_id, e, exc_info=True)

    async def _on_proposal_received_from_peer(self, proposal):
        """Callback when knowledge proposal received from peer.

        Broadcasts proposal to UI so user can review and vote.

        Args:
            proposal: The knowledge commit proposal from peer
        """
        logger.info("Broadcasting peer proposal to UI: %s (topic: %s)",
                    proposal.proposal_id, proposal.topic)
        await self.local_api.broadcast_event(
            "knowledge_commit_proposed",
            proposal.to_dict()
        )

    async def _on_commit_applied(self, commit):
        """Callback called after a knowledge commit is applied

        This reloads the local context in p2p_manager and notifies peers.

        Args:
            commit: The KnowledgeCommit that was applied
        """
        try:
            logger.info("Commit Applied - reloading local context after commit %s", commit.commit_id)

            # Reload context from disk
            context = self.pcm_core.load_context()

            # Update in P2PManager so context requests return latest data
            if hasattr(self, 'p2p_manager') and self.p2p_manager:
                self.p2p_manager.local_context = context
                logger.info("Updated p2p_manager.local_context with new knowledge")

            # Compute new context hash
            new_context_hash = self._compute_context_hash()

            # Broadcast CONTEXT_UPDATED to all connected peers
            await self._broadcast_context_updated_to_peers(new_context_hash)

            # Also emit event to UI
            await self.local_api.broadcast_event("personal_context_updated", {
                "message": f"Knowledge commit applied: {commit.topic}",
                "context_hash": new_context_hash
            })

        except Exception as e:
            logger.error("Error in _on_commit_applied: %s", e, exc_info=True)
            import traceback
            traceback.print_exc()

    async def _broadcast_context_updated_to_peers(self, context_hash: str):
        """
        Phase 7: Broadcast CONTEXT_UPDATED to all connected peers.
        Notifies peers that personal context has changed so they can invalidate their cache.

        Args:
            context_hash: New hash of the updated context
        """
        connected_peers = list(self.p2p_manager.peers.keys())
        if not connected_peers:
            logger.debug("No connected peers to notify of context update")
            return

        logger.info("Broadcasting CONTEXT_UPDATED to %d peer(s)", len(connected_peers))
        for peer_id in connected_peers:
            try:
                # Send CONTEXT_UPDATED message
                message = {
                    "command": "CONTEXT_UPDATED",
                    "payload": {
                        "node_id": self.p2p_manager.node_id,
                        "context_hash": context_hash
                    }
                }
                await self.p2p_manager.send_message_to_peer(peer_id, message)
                logger.debug("Notified %s of context update", peer_id[:20])
            except Exception as e:
                logger.error("Error notifying %s of context update: %s", peer_id[:20], e, exc_info=True)

    async def _notify_peers_of_provider_changes(self):
        """
        Notify all connected peers of provider changes (e.g., after firewall settings change).
        Sends updated PROVIDERS_RESPONSE to each peer so they can update their cached providers list.
        """
        from dpc_protocol.protocol import create_providers_response

        connected_peers = list(self.p2p_manager.peers.keys())
        logger.debug("Found %d connected peer(s) to notify", len(connected_peers))

        if not connected_peers:
            logger.debug("No connected peers to notify")
            return

        for peer_id in connected_peers:
            logger.debug("Processing notification for %s", peer_id)
            try:
                # Check if compute sharing is enabled and peer is authorized
                can_access = self.firewall.can_request_inference(peer_id)
                logger.debug("Firewall check for %s: can_access=%s", peer_id, can_access)

                if not can_access:
                    # Send empty provider list (access was revoked or never granted)
                    response = create_providers_response([])
                    logger.debug("Notifying %s: access denied, sending empty providers list", peer_id)
                else:
                    # Build provider list (same as _handle_get_providers_request)
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

                    logger.debug("Found %d total providers", len(all_providers))

                    # Filter providers based on firewall allowed_models setting
                    allowed_models = self.firewall.get_available_models_for_peer(peer_id, all_models)

                    # Only include providers with allowed models
                    filtered_providers = [
                        p for p in all_providers
                        if p["model"] in allowed_models
                    ]

                    logger.debug("Filtered to %d providers (from %d total)", len(filtered_providers), len(all_providers))
                    response = create_providers_response(filtered_providers)
                    logger.debug("Notifying %s: sending %d providers", peer_id, len(filtered_providers))

                # Send the updated providers response
                logger.debug("Sending PROVIDERS_RESPONSE to %s", peer_id)
                await self.p2p_manager.send_message_to_peer(peer_id, response)
                logger.debug("Successfully sent providers to %s", peer_id)

            except Exception as e:
                logger.error("Error notifying %s of provider changes: %s", peer_id, e, exc_info=True)

    async def _broadcast_commit_result(self, result_payload: dict, participants: List[str]):
        """Broadcast KNOWLEDGE_COMMIT_RESULT to all participants

        Args:
            result_payload: Complete voting result data (status, vote_tally, votes, etc.)
            participants: List of participant node_ids who should receive the notification
        """
        message = {
            "command": "KNOWLEDGE_COMMIT_RESULT",
            "payload": result_payload
        }

        # Send to all participants (who are currently connected)
        for node_id in participants:
            if node_id in self.p2p_manager.peers:
                try:
                    await self.p2p_manager.send_message_to_peer(node_id, message)
                    logger.info("Sent KNOWLEDGE_COMMIT_RESULT to %s", node_id[:20])
                except Exception as e:
                    logger.error("Failed to send result to %s: %s", node_id[:20], e, exc_info=True)
            else:
                logger.debug("Participant %s not connected, skipping result broadcast", node_id[:20])

        # Also emit to local UI
        await self.local_api.broadcast_event("knowledge_commit_result", result_payload)

    async def _request_context_from_peer(self, peer_id: str, query: str) -> PersonalContext:
        """
        Request context from a specific peer for the given query.

        Delegates to ContextCoordinator.
        """
        return await self.context_coordinator.request_peer_context(peer_id, query)

    async def _request_device_context_from_peer(self, peer_id: str) -> Dict:
        """
        Request device context from a specific peer.

        Delegates to ContextCoordinator.

        Args:
            peer_id: The node_id of the peer to request device context from

        Returns:
            Dict containing filtered device context, or None if request fails
        """
        return await self.context_coordinator.request_device_context(peer_id)

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

        logger.debug("Requesting inference from peer: %s", peer_id)

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
                logger.info("Received inference result from %s", peer_id)
                return result

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for inference from %s", peer_id)
                raise TimeoutError(f"Inference request to {peer_id} timed out after {timeout}s")
            finally:
                # Clean up pending request
                self._pending_inference_requests.pop(request_id, None)

        except Exception as e:
            logger.error("Error requesting inference from %s: %s", peer_id, e, exc_info=True)
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
                    logger.error("Error getting context from %s: %s", peer_id, result)

        return contexts

    # --- AI Query Methods ---

    async def execute_ai_query(self, command_id: str, prompt: str, context_ids: list = None, compute_host: str = None, model: str = None, provider: str = None, include_context: bool = True, ai_scope: str = None, **kwargs):
        """
        Orchestrates an AI query and sends the response back to the UI.

        Args:
            command_id: Unique ID for this command
            prompt: The user's prompt/query
            context_ids: Optional list of peer node_ids to fetch context from
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use
            include_context: If True, includes personal context, device context, and AI instructions (default: True)
            ai_scope: Optional AI scope name for filtering what the AI can access (None = no filtering)
            **kwargs: Additional arguments (including conversation_id)
        """
        logger.info("Orchestrating AI query for command_id %s: '%s...'", command_id, prompt[:50])
        logger.debug("Compute host: %s", compute_host or 'local')
        logger.debug("Model: %s", model or 'default')
        logger.debug("Include context: %s", include_context)
        logger.debug("AI Scope: %s", ai_scope or 'None (full access)')

        # Phase 7: Get or create conversation monitor early for history tracking
        conversation_id = kwargs.get("conversation_id", "local_ai")
        monitor = self._get_or_create_conversation_monitor(conversation_id)

        # Phase 7: Check if context window is full (hard limit enforcement)
        if monitor.token_limit > 0:
            usage_percent = monitor.current_token_count / monitor.token_limit
            if usage_percent >= 1.0:
                error_msg = (
                    f"Context window is full ({monitor.current_token_count}/{monitor.token_limit} tokens). "
                    "Please end the session to save knowledge and start a new conversation."
                )
                logger.warning("BLOCKED: %s", error_msg)
                raise RuntimeError(error_msg)

        # Simplified context inclusion: Always include when checkbox checked
        # Rationale: Modern AI models have large context windows (128K+ tokens)
        # Always including contexts ensures information is never lost and simplifies logic
        # User controls token usage via checkbox (checked = include, unchecked = exclude)
        aggregated_contexts = {}
        device_context_data = None

        if include_context:
            logger.debug("Including contexts (checkbox enabled)")
            local_context = self.p2p_manager.local_context

            # Apply AI Scope filtering if specified
            if ai_scope:
                logger.info("Applying AI Scope filtering: %s", ai_scope)
                try:
                    local_context = self.firewall.filter_personal_context_for_ai_scope(local_context, ai_scope)
                    logger.debug("AI Scope filtering applied successfully")
                except Exception as e:
                    logger.error("Error applying AI Scope filtering: %s", e, exc_info=True)
                    # Fall back to unfiltered context if filtering fails
                    logger.warning("Falling back to unfiltered context due to filtering error")

            aggregated_contexts = {'local': local_context}

            # Always include device context when checkbox is checked
            if self.device_context:
                device_context_data = self.device_context
        else:
            logger.debug("User disabled context inclusion")

        # Phase 7: Fetch remote contexts if context_ids provided (with caching)
        peer_device_contexts = {}
        if context_ids:
            logger.info("Processing contexts from %d peer(s)", len(context_ids))
            for node_id in context_ids:
                if node_id in self.p2p_manager.peers:
                    try:
                        # Phase 7: Check cache first (avoid network request if cached)
                        context = monitor.get_cached_peer_context(node_id)
                        device_ctx = monitor.get_cached_peer_device_context(node_id)

                        if context:
                            logger.debug("Using cached context for %s...", node_id[:20])
                        else:
                            # Cache miss: Fetch from network
                            logger.debug("Fetching context from %s...", node_id[:20])
                            context = await self._request_context_from_peer(node_id, prompt)

                        if context:
                            # Phase 7: Compute peer context hash
                            peer_hash = self._compute_peer_context_hash(context)

                            # Include peer context if:
                            # 1. This is first collaborative message, OR
                            # 2. Peer's context has changed

                            # Check if this is first fetch vs. actual change
                            is_first_fetch = node_id not in monitor.peer_context_hashes

                            if monitor.has_peer_context_changed(node_id, peer_hash):
                                aggregated_contexts[node_id] = context
                                monitor.update_peer_context_hash(node_id, peer_hash)

                                if is_first_fetch:
                                    logger.info("Context from %s... (included - first fetch)", node_id[:20])
                                else:
                                    logger.info("Context from %s... (included - context changed)", node_id[:20])
                                    # Phase 7: Only broadcast update event if context actually changed (not first fetch)
                                    await self.local_api.broadcast_event("peer_context_updated", {
                                        "node_id": node_id,
                                        "context_hash": peer_hash,
                                        "conversation_id": conversation_id
                                    })

                                # Phase 7: Cache the peer context (so we don't fetch again next time)
                                # Also fetch device context if we don't have it cached
                                if not device_ctx:
                                    device_ctx = await self._request_device_context_from_peer(node_id)
                                    if device_ctx:
                                        logger.info("Received device context from %s...", node_id[:20])

                                # Cache both personal and device contexts
                                monitor.cache_peer_context(node_id, context, device_ctx)

                                # Add device context to result if we have it
                                if device_ctx:
                                    peer_device_contexts[node_id] = device_ctx

                            else:
                                logger.debug("Context from %s... unchanged (using history)", node_id[:20])
                                # Still add cached device context if available
                                if device_ctx:
                                    peer_device_contexts[node_id] = device_ctx
                        else:
                            logger.warning("No context received from %s...", node_id[:20])
                    except Exception as e:
                        logger.error("Error fetching context from %s...: %s", node_id[:20], e)
                else:
                    logger.warning("Peer %s... not connected", node_id[:20])

        # Phase 7: Add user prompt to conversation history BEFORE assembling prompt
        monitor.add_message('user', prompt)

        # Phase 7: Get conversation history for message array
        message_history = monitor.get_message_history()

        # Assemble final prompt (with or without context, message history always included)
        final_prompt = self._assemble_final_prompt(
            contexts=aggregated_contexts,
            clean_prompt=prompt,
            device_context=device_context_data,
            peer_device_contexts=peer_device_contexts,
            message_history=message_history,
            include_full_context=include_context  # Simplified: just use checkbox state
        )

        response_payload = {}
        status = "OK"

        try:
            # Use send_ai_query to support both local and remote inference
            result = await self.send_ai_query(
                prompt=final_prompt,
                compute_host=compute_host,
                model=model,
                provider=provider
            )
            # result is a dict with 'response', 'model', 'provider', 'compute_host'
            # and potentially 'tokens_used', 'model_max_tokens' for local inference
            response_payload = {
                "content": result["response"],
                "model": result["model"],
                "provider": result["provider"],
                "compute_host": result["compute_host"]
            }

            # Phase 7: Add AI response to conversation history
            monitor.add_message('assistant', result["response"])
            logger.debug("AI response added to conversation history (total messages: %d)", len(message_history) + 1)

            # Update conversation monitor with inference settings used (for knowledge extraction)
            monitor.set_inference_settings(compute_host, model, provider)

            # Token tracking (Phase 2) - works for both local and remote inference
            if "tokens_used" in result:
                conversation_id = kwargs.get("conversation_id", "local_ai")
                monitor = self._get_or_create_conversation_monitor(conversation_id)

                # Set token limit based on model
                if "model_max_tokens" in result:
                    monitor.set_token_limit(result["model_max_tokens"])

                # Update token count
                monitor.update_token_count(result["tokens_used"])

                # Check if we should warn about approaching limit
                if monitor.should_suggest_extraction():
                    # Use the current model's max tokens (not the monitor's cached value)
                    # to ensure consistency with what the UI displays
                    current_limit = result.get("model_max_tokens", monitor.token_limit)
                    current_usage_percent = monitor.current_token_count / current_limit if current_limit > 0 else 0.0

                    await self.local_api.broadcast_event("token_limit_warning", {
                        "conversation_id": conversation_id,
                        "tokens_used": monitor.current_token_count,
                        "token_limit": current_limit,
                        "usage_percent": current_usage_percent
                    })
                    logger.warning("Token Warning - %s: %.1f%% of context window used (%d/%d tokens)",
                                 conversation_id, current_usage_percent * 100,
                                 monitor.current_token_count, current_limit)

                # Include token info in response (send cumulative conversation tokens, not per-query)
                response_payload["tokens_used"] = monitor.current_token_count  # Cumulative conversation tokens
                response_payload["token_limit"] = result.get("model_max_tokens", 0)
                response_payload["this_query_tokens"] = result["tokens_used"]  # Per-query tokens (for debugging)

        except Exception as e:
            logger.error("Error during inference: %s", e, exc_info=True)
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
        # Always buffer messages (even when auto-detection is OFF) to enable manual extraction
        if status == "OK":
            try:
                # BUG FIX: Use actual conversation_id instead of hardcoded "local_ai"
                # This ensures each AI chat (local_ai, ai_chat_xxx) maintains its own buffer and provider settings
                monitor = self._get_or_create_conversation_monitor(conversation_id)
                logger.debug("Monitor - buffering messages for %s (buffer size before: %d)",
                           conversation_id, len(monitor.message_buffer))

                # Update monitor's inference settings to match the query (for knowledge detection)
                # Note: set_inference_settings is also called after query completion (line 2701)
                # but we call it here too for buffering code path consistency
                monitor.set_inference_settings(compute_host, model, provider)

                # Feed user message
                # Group chat support: Include short node_id prefix for global uniqueness
                node_id_short = self.p2p_manager.node_id[-8:]
                user_message = ConvMessage(
                    message_id=f"{node_id_short}-{command_id}-user",
                    conversation_id=conversation_id,  # BUG FIX: Use actual conversation_id
                    sender_node_id=self.p2p_manager.node_id,  # BUG FIX: Use actual node ID
                    sender_name=self.p2p_manager.get_display_name() or "User",  # BUG FIX: Use actual display name
                    text=prompt,
                    timestamp=datetime.utcnow().isoformat()
                )
                await monitor.on_message(user_message)

                # Feed AI response
                # Include model name in sender_name for visibility
                model_name = response_payload.get("model", "unknown")
                ai_name = f"AI ({model_name})" if model_name != "unknown" else "AI Assistant"

                ai_message = ConvMessage(
                    message_id=f"{node_id_short}-{command_id}-ai",
                    conversation_id=conversation_id,  # BUG FIX: Use actual conversation_id
                    sender_node_id="ai",
                    sender_name=ai_name,
                    text=response_payload.get("content", ""),
                    timestamp=datetime.utcnow().isoformat()
                )
                proposal = await monitor.on_message(ai_message)

                logger.debug("Monitor - buffer size after: %d, Score: %.2f",
                           len(monitor.message_buffer), monitor.knowledge_score)

                # Only handle automatic proposals if auto-detection is enabled
                if self.auto_knowledge_detection_enabled:
                    # If proposal generated, broadcast to UI
                    if proposal:
                        logger.info("Auto-detect - knowledge proposal generated for %s chat", conversation_id)
                        await self.local_api.broadcast_event(
                            "knowledge_commit_proposed",
                            proposal.to_dict()
                        )
                        # Local AI - private conversation, don't broadcast to peers
                        logger.info("%s - private conversation, knowledge will not be shared with peers", conversation_id)
                        async def _no_op_broadcast(message: Dict[str, Any]) -> None:
                            pass  # Don't send to peers for private conversations

                        await self.consensus_manager.propose_commit(
                            proposal=proposal,
                            broadcast_func=_no_op_broadcast
                        )
                    else:
                        logger.debug("Monitor - no proposal yet (need 5 messages for auto-detect)")
                else:
                    logger.debug("Monitor - auto-detection is OFF, messages buffered for manual extraction")
            except Exception as e:
                logger.error("Error in local AI conversation monitoring: %s", e, exc_info=True)
                # Only broadcast extraction failure if auto-detection was enabled
                if self.auto_knowledge_detection_enabled:
                    await self.local_api.broadcast_event(
                        "knowledge_extraction_failed",
                        {
                            "conversation_id": "local_ai",
                            "error": str(e),
                            "reason": "JSON parsing failed or LLM extraction error"
                        }
                    )
        else:
            logger.debug("Monitor - query failed (status=%s), not buffering messages", status)

    def _compute_context_hash(self) -> str:
        """Compute SHA256 hash of personal.json + device_context.json for change detection

        Returns:
            SHA256 hex digest of combined context files
        """
        import hashlib
        import json
        from dataclasses import asdict

        hash_obj = hashlib.sha256()

        # Hash personal context
        if self.p2p_manager.local_context:
            context_dict = asdict(self.p2p_manager.local_context)
            context_json = json.dumps(context_dict, sort_keys=True)
            hash_obj.update(context_json.encode('utf-8'))

        # Hash device context
        if self.device_context:
            device_json = json.dumps(self.device_context, sort_keys=True)
            hash_obj.update(device_json.encode('utf-8'))

        return hash_obj.hexdigest()

    def _compute_peer_context_hash(self, context_obj) -> str:
        """Compute SHA256 hash of a peer's context for change detection

        Args:
            context_obj: PersonalContext object from peer

        Returns:
            SHA256 hex digest of peer context
        """
        import hashlib
        import json
        from dataclasses import asdict

        context_dict = asdict(context_obj)
        context_json = json.dumps(context_dict, sort_keys=True)
        return hashlib.sha256(context_json.encode('utf-8')).hexdigest()

    def _assemble_final_prompt(self, contexts: dict, clean_prompt: str, device_context: dict = None, peer_device_contexts: dict = None, message_history: list = None, include_full_context: bool = True) -> str:
        """Helper method to assemble the final prompt for the LLM with instruction processing.

        Phase 2: Incorporates InstructionBlock and bias mitigation from PCM v2.0
        Phase 7: Supports conversation history and context optimization

        Args:
            contexts: Dict of {source_id: PersonalContext} (only if include_full_context=True)
            clean_prompt: The user's query (current message)
            device_context: Local device context (optional, only if include_full_context=True)
            peer_device_contexts: Dict of {peer_id: device_context} for peers (optional)
            message_history: List of conversation messages (optional, for Phase 7)
            include_full_context: If True, include context blocks; if False, skip (Phase 7)
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
            cultural_contexts,
            include_full_context
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

        # Add device context if available (local)
        if device_context:
            # Extract special instructions if present (schema v1.1+)
            special_instructions_text = ""
            if "special_instructions" in device_context:
                instructions_obj = device_context["special_instructions"]
                special_instructions_text = "\nDEVICE CONTEXT INTERPRETATION RULES:\n"

                if "interpretation" in instructions_obj:
                    special_instructions_text += "\nInterpretation Guidelines:\n"
                    for key, value in instructions_obj["interpretation"].items():
                        special_instructions_text += f"  - {key}: {value}\n"

                if "privacy" in instructions_obj:
                    special_instructions_text += "\nPrivacy Rules:\n"
                    for key, value in instructions_obj["privacy"].items():
                        special_instructions_text += f"  - {key}: {value}\n"

                if "update_protocol" in instructions_obj:
                    special_instructions_text += "\nUpdate Protocol:\n"
                    for key, value in instructions_obj["update_protocol"].items():
                        special_instructions_text += f"  - {key}: {value}\n"

                if "usage_scenarios" in instructions_obj:
                    special_instructions_text += "\nUsage Scenarios:\n"
                    for key, value in instructions_obj["usage_scenarios"].items():
                        special_instructions_text += f"  - {key}: {value}\n"

                special_instructions_text += "\n"

            device_json = json.dumps(device_context, indent=2, ensure_ascii=False)
            device_block = f'<DEVICE_CONTEXT source="local">{special_instructions_text}{device_json}\n</DEVICE_CONTEXT>'
            context_blocks.append(device_block)

        # Add peer device contexts if available
        if peer_device_contexts:
            for peer_id, peer_device_ctx in peer_device_contexts.items():
                # Add peer name if available
                source_label = peer_id
                if peer_id in self.peer_metadata:
                    peer_name = self.peer_metadata[peer_id].get('name')
                    if peer_name:
                        source_label = f"{peer_name} ({peer_id})"

                # Extract special instructions if present (schema v1.1+)
                special_instructions_text = ""
                if "special_instructions" in peer_device_ctx:
                    instructions_obj = peer_device_ctx["special_instructions"]
                    special_instructions_text = "\nDEVICE CONTEXT INTERPRETATION RULES:\n"

                    if "interpretation" in instructions_obj:
                        special_instructions_text += "\nInterpretation Guidelines:\n"
                        for key, value in instructions_obj["interpretation"].items():
                            special_instructions_text += f"  - {key}: {value}\n"

                    if "privacy" in instructions_obj:
                        special_instructions_text += "\nPrivacy Rules:\n"
                        for key, value in instructions_obj["privacy"].items():
                            special_instructions_text += f"  - {key}: {value}\n"

                    if "update_protocol" in instructions_obj:
                        special_instructions_text += "\nUpdate Protocol:\n"
                        for key, value in instructions_obj["update_protocol"].items():
                            special_instructions_text += f"  - {key}: {value}\n"

                    if "usage_scenarios" in instructions_obj:
                        special_instructions_text += "\nUsage Scenarios:\n"
                        for key, value in instructions_obj["usage_scenarios"].items():
                            special_instructions_text += f"  - {key}: {value}\n"

                    special_instructions_text += "\n"

                peer_device_json = json.dumps(peer_device_ctx, indent=2, ensure_ascii=False)
                peer_device_block = f'<DEVICE_CONTEXT source="{source_label}">{special_instructions_text}{peer_device_json}\n</DEVICE_CONTEXT>'
                context_blocks.append(peer_device_block)

        # Phase 7: Build prompt with optional context and conversation history
        if include_full_context and context_blocks:
            # Include full context (first message or context changed)
            final_prompt = (
                f"{system_instruction}\n\n"
                f"--- CONTEXTUAL DATA ---\n"
                f'{"\n\n".join(context_blocks)}\n'
                f"--- END OF CONTEXTUAL DATA ---\n\n"
            )
        else:
            # No context or context already included in previous messages
            final_prompt = f"{system_instruction}\n\n"

        # Phase 7: Add conversation history if provided
        if message_history and len(message_history) > 0:
            # Build conversation history section
            history_lines = []
            for msg in message_history:
                role = msg.get('role', 'unknown').upper()
                content = msg.get('content', '')
                history_lines.append(f"{role}: {content}")

            final_prompt += "--- CONVERSATION HISTORY ---\n"
            final_prompt += "\n\n".join(history_lines)
            final_prompt += "\n--- END OF CONVERSATION HISTORY ---\n\n"
        else:
            # No history, just the current query
            final_prompt += f"USER QUERY: {clean_prompt}"

        return final_prompt

    def _build_bias_aware_system_instruction(
        self,
        instruction_blocks: List[Dict[str, Any]],
        bias_mitigation_settings: List[Dict[str, Any]],
        cultural_contexts: List[Dict[str, str]],
        include_full_context: bool = True
    ) -> str:
        """Build system instruction with bias mitigation and multi-perspective requirements.

        Phase 2: Implements cognitive bias mitigation strategies from KNOWLEDGE_ARCHITECTURE.md
        Phase 7: Context-aware instruction (adapts based on whether context is provided)
        """
        # Base instruction (context-aware)
        if include_full_context:
            base = (
                "You are a helpful AI assistant with strong bias-awareness training. "
                "Your task is to answer the user's query based on the provided JSON data blobs inside <CONTEXT> tags. "
                "The 'source' attribute of each tag indicates who the context belongs to. The source 'local' refers to the user asking the query. "
                "Other sources are peer nodes who have shared their context to help answer the query. "
                "Analyze all provided contexts to formulate your answer. When relevant, cite which source provided specific information."
            )
        else:
            base = (
                "You are a helpful AI assistant with strong bias-awareness training. "
                "Answer the user's query based on the conversation history and your general knowledge. "
                "The user has chosen not to share their personal context or device specifications for this conversation."
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

        logger.info("Successfully logged in to Hub")

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

        logger.info("Disconnected from Hub")
        await self.local_api.broadcast_event("status_update", await self.get_status())