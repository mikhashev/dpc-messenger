# dpc-client/core/dpc_client_core/service.py

import asyncio
from dataclasses import asdict
import json
import logging
import re
import uuid
import websockets
import socket
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

from .__version__ import __version__
from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer
from .file_server import FileServer
from .context_cache import ContextCache
from .settings import Settings
from .token_cache import TokenCache

from .connection_status import ConnectionStatus, OperationMode
from .consensus_manager import ConsensusManager
from .conversation_monitor import ConversationMonitor, Message as ConvMessage
from .stun_discovery import discover_external_ip
from .inference_orchestrator import InferenceOrchestrator
from .context_coordinator import ContextCoordinator
from .skill_coordinator import SkillCoordinator
from .p2p_coordinator import P2PCoordinator
from .coordinators.connection_orchestrator import ConnectionOrchestrator, ConnectionFailedError
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
from .message_handlers.transcription_handler import (
    RemoteTranscriptionRequestHandler, RemoteTranscriptionResponseHandler
)
from .message_handlers.provider_handler import GetProvidersHandler, ProvidersResponseHandler
from .message_handlers.knowledge_handler import (
    ContextUpdatedHandler, ProposeKnowledgeCommitHandler, VoteKnowledgeCommitHandler,
    KnowledgeCommitResultHandler, CommitSignedHandler, CommitAckHandler,
    ApplyKnowledgeCommitHandler
)
from .message_handlers.gossip_handler import GossipSyncHandler, GossipMessageHandler
from .message_handlers.relay_register_handler import RelayRegisterHandler
from .message_handlers.relay_message_handler import RelayMessageHandler
from .message_handlers.relay_disconnect_handler import RelayDisconnectHandler
from .message_handlers.relay_response_handler import RelayWaitingHandler, RelayReadyHandler
from .message_handlers.file_offer_handler import FileOfferHandler
from .message_handlers.file_accept_handler import FileAcceptHandler
from .message_handlers.file_chunk_handler import FileChunkHandler
from .message_handlers.file_complete_handler import FileCompleteHandler
from .message_handlers.file_cancel_handler import FileCancelHandler
from .message_handlers.file_chunk_retry_handler import FileChunkRetryHandler
from .message_handlers.voice_transcription_handler import VoiceTranscriptionHandler  # v0.13.2+ auto-transcription
from .message_handlers.chat_history_handlers import RequestChatHistoryHandler, ChatHistoryResponseHandler
from .message_handlers.session_handler import (
    ProposeNewSessionHandler, VoteNewSessionHandler, NewSessionResultHandler
)
from .message_handlers.telegram_handler import TelegramIncomingHandler  # v0.14.0+ Telegram integration
from .message_handlers.group_handler import (  # v0.19.0+ Group chat
    GroupCreateHandler, GroupTextHandler, GroupLeaveHandler, GroupDeleteHandler,
    GroupSyncHandler, GroupHistoryRequestHandler, GroupHistoryResponseHandler,
    GroupHistoryStatusHandler,  # v0.20.0 hash-based sync
    GroupDeletedStatusHandler,  # v0.20.0 offline deletion notification
)
from .message_handlers.skill_handler import (  # v0.21.0+ P2P skill sharing
    SkillSearchHandler, SkillsCatalogHandler, SkillRequestHandler,
    SkillDataHandler, SkillOfferHandler,
)
from .managers.file_transfer_manager import FileTransferManager
from .voice_service import VoiceService
from .knowledge_service import KnowledgeService
from .telegram_service import TelegramService
from .agent_service import AgentService
from .managers.group_manager import GroupManager
from .managers.prompt_manager import PromptManager
from .managers.instruction_manager import InstructionManager
from .session_manager import NewSessionProposalManager
from dpc_protocol.pcm_core import (
    PCMCore, PersonalContext, InstructionBlock, InstructionSet,
    InstructionSetManager, load_instructions, save_instructions
)
from dpc_protocol.utils import parse_dpc_uri
from datetime import datetime, timezone

# Define the path to the user's D-PC configuration directory
DPC_HOME_DIR = Path.home() / ".dpc"

# Configuration file names
PROVIDERS_CONFIG = "providers.json"
PRIVACY_RULES = "privacy_rules.json"
PERSONAL_CONTEXT = "personal.json"

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
        self.connection_status = ConnectionStatus()

        # Set up status change callback
        self.connection_status.set_on_status_change(self._on_connection_status_changed)

        # Initialize all major components
        self.firewall = ContextFirewall(DPC_HOME_DIR / PRIVACY_RULES)
        self.llm_manager = LLMManager(DPC_HOME_DIR / PROVIDERS_CONFIG)

        # Register callback for re-injecting CoreService after providers reload
        self.llm_manager.set_on_providers_reload(self._inject_service_into_providers)

        # Initial injection of CoreService reference into providers that need it
        self._inject_service_into_providers()

        self.hub_client = HubClient(
            api_base_url=self.settings.get_hub_url(),
            oauth_callback_host=self.settings.get_oauth_callback_host(),
            oauth_callback_port=self.settings.get_oauth_callback_port(),
            token_cache=self.token_cache  # Pass token cache for offline mode
        )
        self.p2p_manager = P2PManager(firewall=self.firewall, settings=self.settings)
        self.cache = ContextCache()

        self.local_api = LocalApiServer(core_service=self)
        self.file_server = FileServer(dpc_home_dir=DPC_HOME_DIR, host="127.0.0.1", port=9998)

        # Knowledge Architecture components (Phase 1-6)
        self.pcm_core = PCMCore(DPC_HOME_DIR / PERSONAL_CONTEXT)

        # Load AI instruction sets from instructions.json (v2.0)
        self.instruction_manager = InstructionManager(
            config_dir=DPC_HOME_DIR,
            event_broadcaster=None  # Will be set after LocalApiServer is initialized
        )
        self.instruction_set = self.instruction_manager.instruction_set
        logger.info("Loaded %d instruction set(s) from instructions.json", len(self.instruction_set.sets))

        # Set event broadcaster now that LocalApiServer is initialized
        self.instruction_manager.event_broadcaster = self.local_api

        # Update personal.json with instructions.json reference (if not already present)
        try:
            context = self.pcm_core.load_context()
            if not self._has_instructions_reference(context):
                context = self._add_instructions_reference(context)
                self.pcm_core.save_context(context)
                logger.info("Added instructions.json reference to personal.json metadata")
        except Exception as e:
            logger.warning("Failed to add instructions.json reference: %s", e)

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

        # consensus_manager and knowledge state are owned by KnowledgeService
        # (created below after group_manager and peer_metadata are ready)
        self.consensus_manager = None  # aliased after KnowledgeService.__init__

        # Session manager (mutual new session approval)
        self.session_manager = None

        # Phase 6 managers will be initialized in start() after DHT is created by P2PManager
        self.hole_punch_manager = None
        self.relay_manager = None
        self.gossip_manager = None
        self.connection_orchestrator = None

        # Inference orchestrator (coordinates local and remote AI inference)
        self.inference_orchestrator = InferenceOrchestrator(self)

        # Context coordinator (coordinates context requests and responses)
        self.context_coordinator = ContextCoordinator(self)

        # Skill coordinator (coordinates P2P skill sharing — Phase 5a Memento-Skills)
        self.skill_coordinator = SkillCoordinator(self)

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

        # Session manager (mutual new session approval)
        self.session_manager = NewSessionProposalManager(self)
        self.session_manager.on_proposal_received = self._on_session_proposal_received
        self.session_manager.on_result_broadcast = self._broadcast_session_result

        # Telegram bot integration (v0.14.0+)
        try:
            self.telegram_service = TelegramService(
                core_service_ref=self,
                settings=self.settings,
                llm_manager=self.llm_manager,
                p2p_manager=self.p2p_manager,
                dpc_home_dir=DPC_HOME_DIR,
            )
        except Exception as e:
            logger.error("TelegramService init failed, continuing without Telegram: %s", e)
            self.telegram_service = None
        self.telegram_manager = self.telegram_service.telegram_manager if self.telegram_service else None
        self.telegram_bridge = self.telegram_service.telegram_bridge if self.telegram_service else None

        # Group chat manager (v0.19.0)
        self.group_manager = GroupManager(Path.home() / ".dpc", self.p2p_manager.node_id)
        self.group_manager.load_from_disk()

        # Conversation monitors (per conversation/peer for knowledge extraction)
        # Owned by KnowledgeService but CoreService keeps a reference to the same dict.
        self.conversation_monitors: Dict[str, ConversationMonitor] = {}

        # External IP discovery (via STUN)
        self._external_ip: str | None = None  # Will be populated by STUN discovery

        self._is_running = False
        self._background_tasks = set()
        
        # Store peer metadata (names, profiles, etc.)
        self.peer_metadata: Dict[str, Dict[str, Any]] = {}

        # Prompt manager (assembles prompts with context and history) - v0.12.0 refactor
        self.prompt_manager = PromptManager(
            instruction_set=self.instruction_set,
            peer_metadata=self.peer_metadata
        )

        # Track pending inference requests (for request-response matching)
        self._pending_inference_requests: Dict[str, asyncio.Future] = {}

        # Track pending transcription requests (for request-response matching)
        self._pending_transcription_requests: Dict[str, asyncio.Future] = {}

        # Track pending providers requests (for on-demand provider discovery)
        self._pending_providers_requests: Dict[str, asyncio.Future] = {}

        # Voice transcription — managed by VoiceService (Phase 1a refactor)
        self.voice_service = VoiceService(
            llm_manager=self.llm_manager,
            settings=self.settings,
            local_api=self.local_api,
            on_transcription_enabled=self._retroactively_transcribe_conversation,
        )
        # Alias state refs for backward compat: methods that stay in CoreService
        # (_maybe_transcribe_voice_message, _retroactively_transcribe_conversation)
        # still access these via self._xxx — same dict objects, not copies.
        self._voice_transcriptions = self.voice_service._voice_transcriptions
        self._transcription_locks = self.voice_service._transcription_locks
        self._voice_transcription_settings = self.voice_service._voice_transcription_settings

        # Knowledge — managed by KnowledgeService (Phase 1b refactor)
        try:
            self.knowledge_service = KnowledgeService(
                pcm_core=self.pcm_core,
                llm_manager=self.llm_manager,
                local_api=self.local_api,
                p2p_manager=self.p2p_manager,
                settings=self.settings,
                dpc_home_dir=DPC_HOME_DIR,
                conversation_monitors=self.conversation_monitors,
                peer_metadata=self.peer_metadata,
                group_manager=self.group_manager,
                instruction_set=self.instruction_set,
                send_ai_query=self.send_ai_query,
                broadcast_to_peers=self._broadcast_to_peers,
                broadcast_to_group=self._broadcast_to_group,
                compute_context_hash=self._compute_context_hash,
            )
        except Exception as e:
            logger.error(
                "KnowledgeService init failed, continuing without knowledge features: %s", e
            )
            self.knowledge_service = None

        # Alias consensus_manager for backward compat (same object as KnowledgeService owns)
        if self.knowledge_service:
            self.consensus_manager = self.knowledge_service.consensus_manager

        # Agent lifecycle management (v0.20.0+ refactor Phase 1d)
        try:
            self.agent_service = AgentService(
                llm_manager=self.llm_manager,
                local_api=self.local_api,
                firewall=self.firewall,
                peer_metadata=self.peer_metadata,
            )
        except Exception as e:
            logger.error("AgentService init failed, continuing without agent management: %s", e)
            self.agent_service = None

        # Per-conversation chat provider selection (v0.20.0+)
        self._chat_providers: Dict[str, str] = {}  # conversation_id -> provider_alias

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
        self._history_requested_peers = set()  # Track peers we've requested history from (prevents infinite loops)
        # Note: _group_history_requested removed in v0.20.0 - now using hash-based sync

        # Initialize message router and register handlers
        self.message_router = MessageRouter()
        self._register_message_handlers()

    def _has_instructions_reference(self, context: PersonalContext) -> bool:
        """Check if personal.json has instructions.json reference in metadata."""
        if not hasattr(context, 'metadata') or context.metadata is None:
            return False
        external_contexts = context.metadata.get('external_contexts', {})
        return 'instructions' in external_contexts

    def _add_instructions_reference(self, context: PersonalContext) -> PersonalContext:
        """Add instructions.json reference to personal.json metadata."""
        from datetime import datetime, timezone

        if not hasattr(context, 'metadata') or context.metadata is None:
            context.metadata = {}

        if 'external_contexts' not in context.metadata:
            context.metadata['external_contexts'] = {}

        context.metadata['external_contexts']['instructions'] = {
            "file": "instructions.json",
            "description": "AI behavior instructions",
            "last_updated": datetime.now(timezone.utc).isoformat()
        }

        return context

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

        # Remote transcription (voice transcription sharing)
        self.message_router.register_handler(RemoteTranscriptionRequestHandler(self))
        self.message_router.register_handler(RemoteTranscriptionResponseHandler(self))

        # Provider discovery
        self.message_router.register_handler(GetProvidersHandler(self))
        self.message_router.register_handler(ProvidersResponseHandler(self))

        # Knowledge commits and context updates
        self.message_router.register_handler(ContextUpdatedHandler(self))
        self.message_router.register_handler(ProposeKnowledgeCommitHandler(self))
        self.message_router.register_handler(VoteKnowledgeCommitHandler(self))
        self.message_router.register_handler(KnowledgeCommitResultHandler(self))
        self.message_router.register_handler(CommitSignedHandler(self))
        self.message_router.register_handler(CommitAckHandler(self))
        self.message_router.register_handler(ApplyKnowledgeCommitHandler(self))

        # Peer handshake
        self.message_router.register_handler(HelloHandler(self))

        # Gossip protocol handlers
        self.message_router.register_handler(GossipSyncHandler(self))  # Anti-entropy sync
        self.message_router.register_handler(GossipMessageHandler(self))  # Epidemic routing

        # Volunteer relay handlers (server mode — relay node)
        self.message_router.register_handler(RelayRegisterHandler(self))   # Session registration
        self.message_router.register_handler(RelayMessageHandler(self))    # Forward / receive blobs
        self.message_router.register_handler(RelayDisconnectHandler(self)) # Session cleanup
        # Relay response handlers (client mode — connecting via relay)
        self.message_router.register_handler(RelayWaitingHandler(self))    # Waiting for other peer
        self.message_router.register_handler(RelayReadyHandler(self))      # Session ready

        # File transfer handlers (v0.13.0: FileOfferHandler handles images, voice messages, and regular files)
        self.message_router.register_handler(FileOfferHandler(self))
        self.message_router.register_handler(FileAcceptHandler(self))
        self.message_router.register_handler(FileChunkHandler(self))
        self.message_router.register_handler(FileCompleteHandler(self))
        self.message_router.register_handler(FileCancelHandler(self))
        self.message_router.register_handler(FileChunkRetryHandler(self))  # v0.11.1

        # Voice transcription (v0.13.2+)
        self.message_router.register_handler(VoiceTranscriptionHandler(self))

        # Chat history sync handlers (v0.11.2)
        self.message_router.register_handler(RequestChatHistoryHandler(self))
        self.message_router.register_handler(ChatHistoryResponseHandler(self))

        # Session management (mutual new session approval)
        self.message_router.register_handler(ProposeNewSessionHandler(self))
        self.message_router.register_handler(VoteNewSessionHandler(self))
        self.message_router.register_handler(NewSessionResultHandler(self))

        # Group chat (v0.19.0)
        self.message_router.register_handler(GroupCreateHandler(self))
        self.message_router.register_handler(GroupTextHandler(self))
        self.message_router.register_handler(GroupLeaveHandler(self))
        self.message_router.register_handler(GroupDeleteHandler(self))
        self.message_router.register_handler(GroupSyncHandler(self))
        self.message_router.register_handler(GroupHistoryRequestHandler(self))
        self.message_router.register_handler(GroupHistoryResponseHandler(self))
        self.message_router.register_handler(GroupHistoryStatusHandler(self))  # v0.20.0 hash-based sync
        self.message_router.register_handler(GroupDeletedStatusHandler(self))  # v0.20.0 offline deletion notification

        # P2P skill sharing (v0.21.0+ Phase 5a Memento-Skills)
        self.message_router.register_handler(SkillSearchHandler(self))
        self.message_router.register_handler(SkillsCatalogHandler(self))
        self.message_router.register_handler(SkillRequestHandler(self))
        self.message_router.register_handler(SkillDataHandler(self))
        self.message_router.register_handler(SkillOfferHandler(self))

        # Telegram bot integration (v0.14.0+)
        if self.telegram_manager:
            self.message_router.register_handler(TelegramIncomingHandler(self))

        logger.info("Registered %d message handlers", len(self.message_router.get_registered_commands()))

    def _inject_service_into_providers(self):
        """
        Inject CoreService reference into providers that need it.

        Called during initialization and after providers are reloaded from config.
        This ensures dpc_agent and remote_peer providers always have access to
        CoreService for LLMManager and other services.
        """
        # Inject into DpcAgentProvider if configured
        if "dpc_agent" in self.llm_manager.providers:
            dpc_agent_provider = self.llm_manager.providers["dpc_agent"]
            if hasattr(dpc_agent_provider, 'set_service'):
                dpc_agent_provider.set_service(self)
                logger.info("Injected CoreService into DpcAgentProvider")

        # Inject into RemotePeerProvider instances (v0.18.0+)
        for alias, provider in self.llm_manager.providers.items():
            if hasattr(provider, 'set_service') and provider.__class__.__name__ == 'RemotePeerProvider':
                provider.set_service(self)
                logger.info(f"Injected CoreService into RemotePeerProvider '{alias}'")

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
                "cached_peers_count": len(self.p2p_manager.peer_cache.get_all_peers())
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

        # CHECK 3: Cross-verify personal.json commit_history hashes against markdown frontmatter.
        # Detects independent tampering of either file (one can't be altered without breaking the other).
        for entry in context.commit_history:
            entry_commit_id = entry.get('commit_id', '')
            entry_commit_hash = entry.get('commit_hash', '')
            if not entry_commit_id or not entry_commit_hash:
                continue
            if not knowledge_dir.exists():
                break
            candidates = list(knowledge_dir.glob(f"*_{entry_commit_id}.md"))
            if not candidates:
                continue
            try:
                from dpc_protocol.commit_integrity import parse_markdown_with_frontmatter as _parse_fm
                md_frontmatter, _ = _parse_fm(candidates[0])
                md_commit_hash = md_frontmatter.get('commit_hash', '')
                if md_commit_hash and md_commit_hash != entry_commit_hash:
                    warnings.append({
                        'type': 'history_hash_mismatch',
                        'severity': 'error',
                        'commit_id': entry_commit_id,
                        'file': candidates[0].name,
                        'message': (
                            f'personal.json commit_history hash differs from markdown frontmatter '
                            f'for commit {entry_commit_id[:16]} — one of the files was tampered'
                        )
                    })
            except Exception as e:
                logger.debug("Could not cross-check commit %s: %s", entry_commit_id[:12], e)

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

        # Start file server for browser file access (v0.13.3+)
        self.file_server.start()
        logger.info("File server started on http://127.0.0.1:9998")

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
            # Announce shareable agent skills to DHT (Phase 5b) — non-blocking, best-effort
            asyncio.create_task(self.skill_coordinator.announce_skills_to_dht()).set_name(
                "skill_dht_announce"
            )
        else:
            logger.warning("Phase 6 managers unavailable (DHT not initialized)")

        # Start Telegram bot integration (v0.14.0+)
        if self.telegram_service:
            logger.info("Starting Telegram bot integration...")
            telegram_task = await self.telegram_service.start()
            if telegram_task:
                self._background_tasks.add(telegram_task)

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

        # Save all group chat histories to disk (v0.20.0)
        saved_count = 0
        for conversation_id, monitor in self.conversation_monitors.items():
            if conversation_id.startswith("group-") and monitor._history_dirty:
                if monitor.save_history():
                    saved_count += 1
        if saved_count > 0:
            logger.info("Saved %d group chat histories to disk", saved_count)

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

        # Shutdown Telegram bot integration (v0.14.0+)
        if hasattr(self, 'telegram_service') and self.telegram_service:
            logger.info("Stopping Telegram bot...")
            await self.telegram_service.stop()

        # Unload Whisper model if loaded (free VRAM before shutdown)
        try:
            for alias, provider in self.llm_manager.providers.items():
                if provider.config.get('type') == 'local_whisper':
                    if hasattr(provider, 'is_model_loaded') and provider.is_model_loaded():
                        logger.info(f"Unloading Whisper model during shutdown: {alias}")
                        await provider.unload_model_async()
        except Exception as e:
            logger.error(f"Error unloading Whisper model during shutdown: {e}", exc_info=True)

        # Shutdown LLMManager (close async HTTP clients to prevent event loop errors)
        await self.llm_manager.shutdown()

        # Shutdown core components
        await self.p2p_manager.shutdown_all()
        await self.local_api.stop()
        self.file_server.stop()  # Stop HTTP file server
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
            self.p2p_manager.peer_cache.add_or_update_peer(
                node_id=peer_id,
                display_name=display_name,
                direct_ip=direct_ip,
                supports_webrtc=(connection_type == "webrtc"),
                supports_direct=(connection_type == "direct_tls"),
                metadata={"last_connection_type": connection_type}
            )

            # Auto-request chat history if our history is empty (v0.11.2+)
            # This handles reconnection after app restart
            # Always create conversation monitor on connect to ensure both sides can sync
            conversation_monitor = self._get_or_create_conversation_monitor(peer_id)
            if conversation_monitor:
                history = conversation_monitor.get_message_history()
                # Only request if: (1) history is empty AND (2) we haven't already requested for this peer
                if len(history) == 0 and peer_id not in self._history_requested_peers:
                    # Our history is empty, request from peer
                    logger.info(f"Requesting chat history from {peer_id} (empty local history)")
                    import uuid
                    request_id = str(uuid.uuid4())
                    try:
                        await self.p2p_manager.send_message_to_peer(peer_id, {
                            "command": "REQUEST_CHAT_HISTORY",
                            "payload": {
                                "conversation_id": peer_id,
                                "request_id": request_id
                            }
                        })
                        # Mark as requested to prevent repeated requests
                        self._history_requested_peers.add(peer_id)
                    except Exception as e:
                        logger.error(f"Failed to request chat history from {peer_id}: {e}")

        # Auto-sync group metadata with newly connected peers (v0.19.0+)
        for peer_id in self.p2p_manager.peers:
            shared_groups = self.group_manager.get_groups_for_peer(peer_id)
            for group in shared_groups:
                try:
                    await self.p2p_manager.send_message_to_peer(peer_id, {
                        "command": "GROUP_SYNC",
                        "payload": group.to_dict()
                    })
                except Exception as e:
                    logger.debug("Failed to sync group %s with %s: %s", group.group_id, peer_id[:20], e)

                # v0.20.0: Send GROUP_HISTORY_STATUS for hash-based sync
                # This replaces the old "request if empty" logic with intelligent sync
                monitor = self.conversation_monitors.get(group.group_id)
                local_hash = monitor.compute_history_hash() if monitor and hasattr(monitor, "compute_history_hash") else "sha256:empty"
                local_count = len(monitor.message_history) if monitor else 0

                try:
                    await self.p2p_manager.send_message_to_peer(peer_id, {
                        "command": "GROUP_HISTORY_STATUS",
                        "payload": {
                            "group_id": group.group_id,
                            "history_hash": local_hash,
                            "message_count": local_count
                        }
                    })
                    logger.debug("Sent GROUP_HISTORY_STATUS for %s to %s (hash=%s, count=%d)",
                               group.group_id, peer_id[:20], local_hash[:16], local_count)
                except Exception as e:
                    logger.debug("Failed to send history status for %s: %s", group.group_id, e)

        # v0.20.0: Exchange deleted group IDs for offline deletion notification
        deleted_groups = self.group_manager.get_deleted_group_ids()
        if deleted_groups:
            for peer_id in self.p2p_manager.peers:
                try:
                    await self.p2p_manager.send_message_to_peer(peer_id, {
                        "command": "GROUP_DELETED_STATUS",
                        "payload": {"deleted_groups": deleted_groups}
                    })
                    logger.debug("Sent GROUP_DELETED_STATUS to %s (%d groups)",
                               peer_id[:20], len(deleted_groups))
                except Exception as e:
                    logger.debug("Failed to send deleted status to %s: %s", peer_id[:20], e)

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

        # Clear history request tracking for this peer
        # This allows us to request history again when they reconnect
        self._history_requested_peers.discard(peer_id)

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

    async def get_status(self, command_id: str = None, _websocket = None) -> Dict[str, Any]:
        """
        Aggregates status from all components.

        Args:
            command_id: Optional command_id for background execution (sends response via WebSocket)
            _websocket: Optional WebSocket for sending response in background mode

        Returns:
            Status dict if called synchronously, None if called in background mode
        """

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

        status_result = {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
            "peer_info": peer_info,
            "active_ai_model": active_model,
            # Offline mode status
            "operation_mode": self.connection_status.get_operation_mode().value,
            "connection_status": self.connection_status.get_status_message(),
            "available_features": self.connection_status.get_available_features(),
            "cached_peers_count": len(self.p2p_manager.peer_cache.get_all_peers()),
            # Direct TLS connection URIs
            "local_ips": local_ips,
            "dpc_uris": dpc_uris,
            # External URIs (from STUN server discovery)
            "external_ips": external_ips,
            "external_uris": external_uris,
            # Connection orchestrator stats (6-tier fallback metrics)
            "orchestrator_stats": orchestrator_stats,
        }

        # If running in background mode, send response via WebSocket
        if command_id is not None and _websocket is not None:
            try:
                response = {"id": command_id, "command": "get_status", "status": "OK", "payload": status_result}
                await _websocket.send(json.dumps(response))
                logger.debug("Sent get_status response in background mode")
            except Exception as e:
                logger.error("Failed to send get_status response: %s", e)
            return None

        return status_result

    @property
    def auto_knowledge_detection_enabled(self) -> bool:
        """Delegates to KnowledgeService (Phase 1b)."""
        if self.knowledge_service:
            return self.knowledge_service.auto_knowledge_detection_enabled
        return False

    @auto_knowledge_detection_enabled.setter
    def auto_knowledge_detection_enabled(self, value: bool) -> None:
        if self.knowledge_service:
            self.knowledge_service.auto_knowledge_detection_enabled = value

    def get_state(self) -> dict:
        """
        Agent-readable synchronous snapshot of current CoreService state.

        Unlike get_status() (async, UI-facing), this is synchronous and lightweight —
        intended for runtime introspection by agents and debugging tools.

        Returns a composite dict that will be extended as sub-services are extracted
        (VoiceService, KnowledgeService, TelegramService, AgentService).

        See: docs/decisions/001-service-split.md
        """
        services = {}
        if self.voice_service:
            services["voice"] = self.voice_service.get_state()
        if self.knowledge_service:
            services["knowledge"] = self.knowledge_service.get_state()
        if self.telegram_service:
            services["telegram"] = self.telegram_service.get_state()
        if self.agent_service:
            services["agent"] = self.agent_service.get_state()

        return {
            "is_running": self._is_running,
            "active_tasks": len(self._background_tasks),
            "p2p_peers": list(self.p2p_manager.peers.keys()) if self.p2p_manager else [],
            "services": services,
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

    async def get_default_providers(self) -> Dict[str, Any]:
        """
        Get default provider configuration for UI initialization.

        Returns:
            Dictionary with:
            - default_provider: str (text provider alias)
            - vision_provider: str (vision provider alias)
            - voice_provider: str (voice transcription provider alias) v0.13.0+
            - agent_provider: str (AI agent provider alias) v0.18.0+
        """
        return {
            "default_provider": self.llm_manager.default_provider or "",
            "vision_provider": self.llm_manager.vision_provider or "",
            "voice_provider": self.llm_manager.voice_provider or "",  # v0.13.0+
            "agent_provider": getattr(self.llm_manager, 'agent_provider', None) or ""  # v0.18.0+
        }

    async def get_providers_list(self) -> Dict[str, Any]:
        """
        Get list of available providers for UI dropdowns.

        Returns:
            Dictionary with:
            - providers: List of provider dicts with alias, model, type, supports_vision, supports_voice
            - default_provider: Default text provider alias
            - vision_provider: Default vision provider alias
            - voice_provider: Default voice transcription provider alias v0.13.0+
            - agent_provider: AI agent provider alias v0.18.0+
        """
        providers_info = []
        for alias, provider in self.llm_manager.providers.items():
            provider_dict = {
                "alias": alias,
                "model": provider.model,
                "type": provider.__class__.__name__.replace("Provider", ""),  # "Ollama", "OpenAICompatible", etc.
                "supports_vision": provider.supports_vision(),
                "context_window": self.llm_manager.get_context_window(provider.model),
            }
            # v0.13.0+: Add supports_voice flag for Whisper-capable providers
            provider_dict["supports_voice"] = self._provider_supports_voice(provider)

            # v0.18.1+: Add remote inference fields for dpc_agent provider
            if alias == "dpc_agent":
                provider_dict["peer_id"] = getattr(provider, 'peer_id', None)
                provider_dict["remote_model"] = getattr(provider, 'remote_model', None)
                provider_dict["remote_provider"] = getattr(provider, 'remote_provider', None)

            providers_info.append(provider_dict)

        return {
            "providers": providers_info,
            "default_provider": self.llm_manager.default_provider or "",
            "vision_provider": self.llm_manager.vision_provider or "",
            "voice_provider": self.llm_manager.voice_provider or "",  # v0.13.0+
            "agent_provider": getattr(self.llm_manager, 'agent_provider', None) or ""  # v0.18.0+
        }

    def _provider_supports_voice(self, provider: Any) -> bool:
        """Delegated to VoiceService."""
        return self.voice_service._provider_supports_voice(provider)

    async def set_voice_provider(self, provider_alias: str) -> Dict[str, Any]:
        """Delegated to VoiceService."""
        return await self.voice_service.set_voice_provider(provider_alias)

    async def prepare_agent(self) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.prepare_agent()

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

    async def query_remote_providers(self, peer_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Query a remote peer for their available AI providers.

        This sends a GET_PROVIDERS request to the peer and waits for their response.
        Used by the UI to populate dropdown lists when configuring a remote_peer provider.

        Args:
            peer_id: The node ID of the remote peer
            timeout: Maximum time to wait for response (default 10 seconds)

        Returns:
            Dictionary with:
            - status: "success" or "error"
            - providers: List of provider dicts with alias, model, type
            - message: Error message if failed
        """
        from dpc_protocol.protocol import create_get_providers_message

        logger.info("[DEBUG] query_remote_providers called with peer_id=%s, timeout=%s", peer_id, timeout)

        try:
            # Check if peer is connected
            logger.info("[DEBUG] Checking if peer %s is connected. Connected peers: %s", peer_id, list(self.p2p_manager.peers.keys()))
            if peer_id not in self.p2p_manager.peers:
                logger.info("[DEBUG] Peer %s is NOT connected", peer_id)
                return {
                    "status": "error",
                    "message": f"Peer '{peer_id}' is not connected. Connect to the peer first."
                }

            # Check if we already have cached providers
            if peer_id in self.peer_metadata and "providers" in self.peer_metadata[peer_id]:
                cached_providers = self.peer_metadata[peer_id]["providers"]
                if cached_providers:
                    logger.info("[DEBUG] Returning cached providers for %s (%d providers)", peer_id, len(cached_providers))
                    return {
                        "status": "success",
                        "providers": cached_providers,
                        "cached": True
                    }

            # Create Future to wait for response
            logger.info("[DEBUG] Creating Future and sending GET_PROVIDERS to %s", peer_id)
            response_future: asyncio.Future = asyncio.Future()
            self._pending_providers_requests[peer_id] = response_future

            # Send GET_PROVIDERS request
            await self.p2p_manager.send_message_to_peer(peer_id, create_get_providers_message())
            logger.info("[DEBUG] Sent GET_PROVIDERS request to %s, waiting for response...", peer_id)

            # Wait for response
            try:
                providers = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("[DEBUG] Received %d providers from %s", len(providers), peer_id)
                return {
                    "status": "success",
                    "providers": providers,
                    "cached": False
                }
            except asyncio.TimeoutError:
                self._pending_providers_requests.pop(peer_id, None)
                logger.info("[DEBUG] Timeout waiting for providers from %s after %ss", peer_id, timeout)
                return {
                    "status": "error",
                    "message": f"Timeout waiting for providers response from {peer_id} after {timeout}s"
                }

        except Exception as e:
            logger.error("[DEBUG] Error querying remote providers from %s: %s", peer_id, e, exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to query remote providers: {str(e)}"
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
            elif provider["type"] not in ["ollama", "openai_compatible", "anthropic", "zai", "local_whisper", "dpc_agent", "remote_peer"]:
                errors.append(f"{prefix}: Invalid type '{provider['type']}'")

            # Model is required for all types except dpc_agent and remote_peer
            provider_type = provider.get("type")
            if provider_type not in ["dpc_agent", "remote_peer"] and "model" not in provider:
                errors.append(f"{prefix}: Missing 'model'")

            # Type-specific required fields
            if provider_type == "ollama" and "host" not in provider:
                errors.append(f"{prefix}: Ollama provider missing 'host'")
            if provider_type == "openai_compatible" and "base_url" not in provider:
                errors.append(f"{prefix}: OpenAI provider missing 'base_url'")
            if provider_type == "remote_peer" and "peer_id" not in provider:
                errors.append(f"{prefix}: Remote peer provider missing 'peer_id'")

            # Optional context_window validation
            if "context_window" in provider:
                try:
                    cw = int(provider["context_window"])
                    if cw <= 0:
                        errors.append(f"{prefix}: context_window must be positive")
                except (ValueError, TypeError):
                    errors.append(f"{prefix}: context_window must be an integer")

            # Optional temperature validation (v0.19.0+)
            if "temperature" in provider:
                temp = provider["temperature"]
                if not isinstance(temp, (int, float)):
                    errors.append(f"{prefix}: temperature must be a number")
                elif temp < 0 or temp > 2:
                    errors.append(f"{prefix}: temperature must be between 0 and 2 (got {temp})")

        # Check default_provider exists
        default = config_dict.get("default_provider")
        if default and default not in provider_aliases:
            errors.append(f"Default provider '{default}' not found in providers list")

        # Check vision_provider exists (optional field)
        vision = config_dict.get("vision_provider")
        if vision and vision not in provider_aliases:
            errors.append(f"Vision provider '{vision}' not found in providers list")

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
        """Load and return all AI instruction sets for UI display (v2.0).

        UI Integration: Called when user opens InstructionsEditor component.
        Returns all instruction sets with schema version and default set.
        """
        try:
            instruction_sets = self.instruction_manager.get_all()
            return {
                "status": "success",
                "instruction_sets": instruction_sets
            }
        except Exception as e:
            logger.error("Error loading instruction sets: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_instructions(self, set_key: str, instructions_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Save updated AI instruction set from UI editor (v2.0).

        UI Integration: Called when user clicks 'Save' in InstructionsEditor.

        Args:
            set_key: Key of the instruction set to save
            instructions_dict: Dictionary representation of InstructionBlock

        Returns:
            Dict with status and message
        """
        try:
            success = self.instruction_manager.save_set(set_key, instructions_dict)

            if success:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                # Update PromptManager with new instruction set
                self.prompt_manager.instruction_set = self.instruction_set

                return {
                    "status": "success",
                    "message": f"Instruction set '{set_key}' saved successfully"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to save instruction set '{set_key}'"
                }

        except Exception as e:
            logger.error("Error saving instruction set '%s': %s", set_key, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reload_instructions(self) -> Dict[str, Any]:
        """Reload AI instruction sets from disk (v2.0).

        UI Integration: Called when user clicks 'Reload' or when external changes detected.

        Returns:
            Dict with status, message, and updated instruction sets
        """
        try:
            success = self.instruction_manager.reload()

            if success:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                # Update PromptManager with new instruction set
                self.prompt_manager.instruction_set = self.instruction_set

                return {
                    "status": "success",
                    "message": "AI instruction sets reloaded from disk",
                    "instruction_sets": self.instruction_manager.get_all()
                }
            else:
                return {
                    "status": "error",
                    "message": "Failed to reload instruction sets"
                }

        except Exception as e:
            logger.error("Error reloading instruction sets: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_instruction_set(self, set_key: str) -> Dict[str, Any]:
        """Get a specific instruction set.

        Args:
            set_key: Key of the instruction set to retrieve

        Returns:
            Dict with status and instruction set data
        """
        try:
            instruction_set = self.instruction_manager.get_set(set_key)

            if instruction_set:
                return {
                    "status": "success",
                    "instruction_set": instruction_set
                }
            else:
                return {
                    "status": "error",
                    "message": f"Instruction set '{set_key}' not found"
                }

        except Exception as e:
            logger.error("Error getting instruction set '%s': %s", set_key, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def create_instruction_set(self, set_key: str, name: str, description: str = "") -> Dict[str, Any]:
        """Create a new instruction set.

        UI Integration: Called when user creates a new instruction set.

        Args:
            set_key: Unique key for the instruction set (kebab-case)
            name: Display name for the instruction set
            description: Optional description

        Returns:
            Dict with status, message, and created instruction set
        """
        try:
            instruction_set = self.instruction_manager.create_set(set_key, name, description)

            if instruction_set:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                return {
                    "status": "success",
                    "message": f"Instruction set '{name}' created successfully",
                    "instruction_set": instruction_set
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to create instruction set '{name}'"
                }

        except Exception as e:
            logger.error("Error creating instruction set '%s': %s", set_key, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def delete_instruction_set(self, set_key: str) -> Dict[str, Any]:
        """Delete an instruction set.

        UI Integration: Called when user deletes an instruction set.

        Args:
            set_key: Key of the instruction set to delete

        Returns:
            Dict with status and message
        """
        try:
            success = self.instruction_manager.delete_set(set_key)

            if success:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                return {
                    "status": "success",
                    "message": f"Instruction set '{set_key}' deleted successfully"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to delete instruction set '{set_key}' (may be protected)"
                }

        except Exception as e:
            logger.error("Error deleting instruction set '%s': %s", set_key, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def rename_instruction_set(self, old_key: str, new_key: str, new_name: str) -> Dict[str, Any]:
        """Rename an instruction set.

        UI Integration: Called when user renames an instruction set.

        Args:
            old_key: Current key of the instruction set
            new_key: New key for the instruction set
            new_name: New display name

        Returns:
            Dict with status and message
        """
        try:
            success = self.instruction_manager.rename_set(old_key, new_key, new_name)

            if success:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                return {
                    "status": "success",
                    "message": f"Instruction set renamed from '{old_key}' to '{new_key}'"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to rename instruction set '{old_key}'"
                }

        except Exception as e:
            logger.error("Error renaming instruction set '%s': %s", old_key, e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def set_default_instruction_set(self, set_key: str) -> Dict[str, Any]:
        """Set the default instruction set.

        UI Integration: Called when user sets a default instruction set.

        Args:
            set_key: Key of the instruction set to make default

        Returns:
            Dict with status and message
        """
        try:
            success = self.instruction_manager.set_default(set_key)

            if success:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                return {
                    "status": "success",
                    "message": f"Default instruction set changed to '{set_key}'"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to set default instruction set to '{set_key}'"
                }

        except Exception as e:
            logger.error("Error setting default instruction set: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def import_instruction_template(self, template_file: str, set_key: str, set_name: str) -> Dict[str, Any]:
        """Import instruction template from file.

        UI Integration: Called when user imports a template.

        Args:
            template_file: Path to template JSON file
            set_key: Key for the new instruction set
            set_name: Display name for the new instruction set

        Returns:
            Dict with status, message, and imported instruction set
        """
        try:
            from pathlib import Path
            template_path = Path(template_file)

            if not template_path.exists():
                return {
                    "status": "error",
                    "message": f"Template file not found: {template_file}"
                }

            instruction_set = self.instruction_manager.import_template(template_path, set_key, set_name)

            if instruction_set:
                # Update in-memory reference
                self.instruction_set = self.instruction_manager.instruction_set

                return {
                    "status": "success",
                    "message": f"Template imported as '{set_name}'",
                    "instruction_set": instruction_set
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to import template from {template_file}"
                }

        except Exception as e:
            logger.error("Error importing template: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_available_templates(self) -> Dict[str, Any]:
        """Get list of available instruction set templates.

        UI Integration: Called to populate template picker dialog.

        Returns:
            Dict with status and list of templates with metadata
        """
        try:
            from pathlib import Path
            import json

            # Get templates directory (go up to dpc-client/core/)
            templates_dir = Path(__file__).parent.parent / "templates" / "instructions"

            if not templates_dir.exists():
                logger.warning("Templates directory not found: %s", templates_dir)
                return {
                    "status": "success",
                    "templates": []
                }

            templates = []

            # Scan for JSON template files (exclude README.md)
            for template_file in templates_dir.glob("*.json"):
                try:
                    with open(template_file, 'r', encoding='utf-8') as f:
                        template_data = json.load(f)

                    templates.append({
                        "file": str(template_file),
                        "filename": template_file.name,
                        "key": template_file.stem,  # filename without extension
                        "name": template_data.get("name", template_file.stem),
                        "description": template_data.get("description", ""),
                    })

                except Exception as e:
                    logger.warning("Error reading template %s: %s", template_file, e)
                    continue

            # Sort templates by name
            templates.sort(key=lambda t: t["name"])

            return {
                "status": "success",
                "templates": templates
            }

        except Exception as e:
            logger.error("Error listing templates: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
                "templates": []
            }

    async def get_wizard_template(self) -> Dict[str, Any]:
        """Get wizard template configuration for AI-assisted instruction creation.

        UI Integration: Called when user starts the AI wizard to get question sequence
        and system instructions.

        Returns:
            Dict with wizard configuration (system_instruction, question_sequence, etc.)
        """
        try:
            from pathlib import Path
            import json

            # Load wizard template (go up to dpc-client/core/)
            wizard_file = Path(__file__).parent.parent / "templates" / "wizard_template.json"

            if not wizard_file.exists():
                logger.error("Wizard template not found: %s", wizard_file)
                return {
                    "status": "error",
                    "message": "Wizard template configuration not found"
                }

            with open(wizard_file, 'r', encoding='utf-8') as f:
                wizard_config = json.load(f)

            return {
                "status": "success",
                "wizard": wizard_config
            }

        except Exception as e:
            logger.error("Error loading wizard template: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def ai_assisted_instruction_creation(
        self,
        user_responses: Dict[str, str],
        provider: str = "ollama",
        model: str = None
    ) -> Dict[str, Any]:
        """Generate instruction set using AI based on user responses.

        UI Integration: Called after wizard collects user's answers to questions.
        Uses AI to generate a custom instruction set tailored to user's needs.

        Args:
            user_responses: Dict mapping question IDs to user's answers
            provider: AI provider to use (ollama, openai, anthropic)
            model: Model name (optional, uses provider default if not specified)

        Returns:
            Dict with generated instruction set data or error
        """
        try:
            from pathlib import Path
            import json

            # Load wizard template for generation prompt (go up to dpc-client/core/)
            wizard_file = Path(__file__).parent.parent / "templates" / "wizard_template.json"
            with open(wizard_file, 'r', encoding='utf-8') as f:
                wizard_config = json.load(f)

            # Build prompt using template
            generation_prompt = wizard_config["generation_prompt_template"].format(
                use_case=user_responses.get("use_case", "general conversation"),
                learning_style=user_responses.get("learning_style", "adaptive"),
                ai_behaviors=user_responses.get("ai_behaviors", "helpful and clear"),
                challenges=user_responses.get("challenges", "none specified"),
                verification=user_responses.get("verification", "provide reasoning")
            )

            # Add system instruction
            system_instruction = wizard_config["system_instruction"]
            full_prompt = f"{system_instruction}\n\n{generation_prompt}"

            # Execute AI query to generate instruction set
            logger.info("Generating instruction set via AI wizard (provider=%s, model=%s)", provider, model)

            result = await self.llm_manager.query(
                prompt=full_prompt,
                provider_alias=provider,
                return_metadata=True,
                model=model,
                temperature=0.7
            )

            # Parse AI response (should be JSON)
            response_text = result.get("response", "")

            # Extract JSON from response (handle markdown code blocks)
            if "```json" in response_text:
                # Extract from markdown code block
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                json_text = response_text[start:end].strip()
            elif "```" in response_text:
                # Generic code block
                start = response_text.find("```") + 3
                end = response_text.find("```", start)
                json_text = response_text[start:end].strip()
            else:
                # Assume entire response is JSON
                json_text = response_text.strip()

            # Parse JSON
            try:
                instruction_data = json.loads(json_text)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse AI response as JSON: %s", e)
                logger.debug("AI response: %s", response_text[:500])
                return {
                    "status": "error",
                    "message": f"Failed to parse AI response: {e}",
                    "raw_response": response_text
                }

            # Validate required fields
            required_fields = ["name", "description", "primary"]
            missing_fields = [f for f in required_fields if f not in instruction_data]
            if missing_fields:
                return {
                    "status": "error",
                    "message": f"Generated instruction set missing required fields: {missing_fields}",
                    "instruction_data": instruction_data
                }

            return {
                "status": "success",
                "instruction_data": instruction_data,
                "message": "Instruction set generated successfully"
            }

        except Exception as e:
            logger.error("Error in AI-assisted instruction creation: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def ai_assisted_instruction_creation_remote(
        self,
        user_responses: Dict[str, str],
        peer_node_id: str
    ) -> Dict[str, Any]:
        """Generate instruction set using AI via remote inference.

        UI Integration: Called when user selects "Remote Inference" in wizard.
        Uses a peer's AI to generate the instruction set.

        Args:
            user_responses: Dict mapping question IDs to user's answers
            peer_node_id: Node ID of peer to use for remote inference

        Returns:
            Dict with generated instruction set data or error
        """
        try:
            from pathlib import Path
            import json

            # Load wizard template for generation prompt
            wizard_file = Path(__file__).parent.parent / "templates" / "wizard_template.json"
            with open(wizard_file, 'r', encoding='utf-8') as f:
                wizard_config = json.load(f)

            # Build prompt using template
            generation_prompt = wizard_config["generation_prompt_template"].format(
                use_case=user_responses.get("use_case", "general conversation"),
                learning_style=user_responses.get("learning_style", "adaptive"),
                ai_behaviors=user_responses.get("ai_behaviors", "helpful and clear"),
                challenges=user_responses.get("challenges", "none specified"),
                verification=user_responses.get("verification", "provide reasoning")
            )

            # Add system instruction
            system_instruction = wizard_config["system_instruction"]
            full_prompt = f"{system_instruction}\n\n{generation_prompt}"

            # Execute AI query via remote inference
            logger.info("Generating instruction set via remote AI wizard (peer=%s)", peer_node_id)

            result = await self.inference_orchestrator.execute_inference(
                prompt=full_prompt,
                compute_host=peer_node_id
            )

            # Parse AI response (should be JSON)
            response_text = result.get("response", "")

            # Extract JSON from response (handle markdown code blocks)
            if "```json" in response_text:
                # Extract from markdown code block
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                json_text = response_text[start:end].strip()
            elif "```" in response_text:
                # Generic code block
                start = response_text.find("```") + 3
                end = response_text.find("```", start)
                json_text = response_text[start:end].strip()
            else:
                # Assume entire response is JSON
                json_text = response_text.strip()

            # Parse JSON
            try:
                instruction_data = json.loads(json_text)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse AI response as JSON: %s", e)
                logger.debug("AI response: %s", response_text[:500])
                return {
                    "status": "error",
                    "message": f"Failed to parse AI response: {e}",
                    "raw_response": response_text
                }

            # Validate required fields
            required_fields = ["name", "description", "primary"]
            missing_fields = [f for f in required_fields if f not in instruction_data]
            if missing_fields:
                return {
                    "status": "error",
                    "message": f"Generated instruction set missing required fields: {missing_fields}",
                    "instruction_data": instruction_data
                }

            return {
                "status": "success",
                "instruction_data": instruction_data,
                "message": "Instruction set generated successfully via remote inference"
            }

        except Exception as e:
            logger.error("Error in AI-assisted instruction creation (remote): %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def send_p2p_image(self, node_id: str, image_base64: str, filename: str = None, text: str = "") -> dict:
        """
        Send screenshot/image to peer via file transfer with inline preview support.

        Args:
            node_id: Target peer node ID
            image_base64: Base64 data URL (data:image/png;base64,...)
            filename: Optional filename (auto-generated: screenshot_TIMESTAMP.png)
            text: Optional text caption to display with the image

        Returns:
            dict with transfer_id, file_path, thumbnail_base64, size_bytes, width, height, mime_type

        Raises:
            ValueError: Invalid data URL, peer not connected, or privacy rules violation
        """
        from datetime import datetime, timezone
        import base64
        import re
        from pathlib import Path
        from PIL import Image

        # 1. Validate peer connection
        connected_peers = self.p2p_coordinator.get_connected_peers()
        if node_id not in connected_peers:
            raise ValueError(f"Peer {node_id} not connected")

        # 2. Parse data URL
        if not image_base64.startswith("data:image/"):
            raise ValueError("Invalid data URL format (must start with 'data:image/')")

        match = re.match(r'data:([^;]+);base64,(.+)', image_base64)
        if not match:
            raise ValueError("Invalid data URL format (expected 'data:MIME;base64,DATA')")

        mime_type = match.group(1)  # e.g., "image/png"
        encoded_data = match.group(2)
        extension = mime_type.split("/")[1]  # e.g., "png"

        # 3. Decode image data
        try:
            image_data = base64.b64decode(encoded_data)
        except Exception as e:
            raise ValueError(f"Failed to decode base64 data: {e}")

        # 4. Check privacy rules
        img_rules = self.firewall.rules.get("image_transfer", {})
        max_size_mb = img_rules.get("max_size_mb", 100)
        allowed_sources = img_rules.get("allowed_sources", ["clipboard", "file", "camera"])

        # Validate size
        size_mb = len(image_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            raise ValueError(f"Image too large ({size_mb:.2f} MB > {max_size_mb} MB limit)")

        # Validate source (clipboard is the current source)
        if "clipboard" not in allowed_sources:
            raise ValueError("Clipboard image sharing disabled in privacy rules")

        # 5. Generate filename
        if not filename:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.{extension}"

        # 6. Create screenshots directory
        peer_files_dir = DPC_HOME_DIR / "conversations" / node_id / "files" / "screenshots"
        peer_files_dir.mkdir(parents=True, exist_ok=True)

        # 7. Save image file
        file_path = peer_files_dir / filename

        # Handle filename collisions
        if file_path.exists():
            stem = file_path.stem
            suffix = file_path.suffix
            counter = 1
            while file_path.exists():
                file_path = peer_files_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            with open(file_path, "wb") as f:
                f.write(image_data)
            logger.info(f"Saved P2P screenshot: {file_path} ({len(image_data)} bytes)")
        except Exception as e:
            raise ValueError(f"Failed to save image file: {e}")

        # 8. Validate image format and get dimensions
        try:
            with Image.open(file_path) as img:
                # Validate format
                if img.format not in ["PNG", "JPEG", "GIF", "WEBP"]:
                    file_path.unlink()  # Clean up
                    raise ValueError(f"Unsupported image format: {img.format}")

                width, height = img.size
        except Exception as e:
            # Clean up file if image is invalid
            if file_path.exists():
                file_path.unlink()
            raise ValueError(f"Invalid image file: {e}")

        # 9. Generate thumbnail (reuse existing utility)
        from .utils.image_utils import generate_thumbnail
        try:
            thumbnail_base64 = generate_thumbnail(file_path)
        except Exception as e:
            logger.warning(f"Failed to generate thumbnail: {e}")
            thumbnail_base64 = ""  # Continue without thumbnail

        # 10. Send via file transfer with image_metadata
        try:
            transfer_id = await self.file_transfer_manager.send_file(
                node_id=node_id,
                file_path=file_path,
                image_metadata={
                    "dimensions": {"width": width, "height": height},
                    "thumbnail_base64": thumbnail_base64,
                    "source": "clipboard",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "text": text  # Store user caption
                }
            )
        except Exception as e:
            # Clean up file if transfer fails to start
            if file_path.exists():
                file_path.unlink()
            raise ValueError(f"Failed to start file transfer: {e}")

        # 11. Return details for frontend
        return {
            "transfer_id": transfer_id,
            "file_path": str(file_path),
            "thumbnail_base64": thumbnail_base64,
            "size_bytes": len(image_data),
            "width": width,
            "height": height,
            "mime_type": mime_type
        }

    async def send_voice_message(
        self,
        node_id: str,
        audio_base64: str,
        duration_seconds: float,
        mime_type: str = "audio/webm"
    ) -> dict:
        """
        Send voice message to peer via file transfer with voice metadata.

        Args:
            node_id: Target peer node ID
            audio_base64: Base64-encoded audio data (raw, not data URL)
            duration_seconds: Recording duration in seconds
            mime_type: Audio MIME type (default: audio/webm)

        Returns:
            dict with transfer_id, file_path, size_bytes, voice_metadata

        Raises:
            ValueError: Invalid data, peer not connected, or privacy rules violation
        """
        from datetime import datetime, timezone
        import base64
        from pathlib import Path

        # 1. Validate peer connection
        connected_peers = self.p2p_coordinator.get_connected_peers()
        if node_id not in connected_peers:
            raise ValueError(f"Peer {node_id} not connected")

        # 2. Decode audio data
        try:
            audio_data = base64.b64decode(audio_base64)
        except Exception as e:
            raise ValueError(f"Failed to decode base64 audio data: {e}")

        # 3. Get voice settings
        max_duration = float(self.settings.get("voice_messages", "max_duration_seconds", "300"))
        max_size_mb = int(self.settings.get("voice_messages", "max_size_mb", "10"))
        supported_mime_types = self.settings.get("voice_messages", "mime_types", "audio/webm,audio/opus,audio/ogg,audio/mp4,audio/mpeg,audio/wav").split(",")

        # 4. Validate duration
        if duration_seconds > max_duration:
            raise ValueError(f"Voice message too long ({duration_seconds}s > {max_duration}s limit)")

        # 5. Validate size
        size_mb = len(audio_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            raise ValueError(f"Voice message too large ({size_mb:.2f} MB > {max_size_mb} MB limit)")

        # 6. Validate mime type (strip parameters like ";codecs=opus")
        mime_type_base = mime_type.split(";")[0].strip()
        if mime_type_base not in supported_mime_types:
            raise ValueError(f"Unsupported audio format: {mime_type}. Supported: {', '.join(supported_mime_types)}")

        # 7. Check privacy rules (file_transfer rules apply)
        rules = self.firewall.rules.get("file_transfer", {})
        max_size_mb_rules = rules.get("max_size_mb", 100)
        if size_mb > max_size_mb_rules:
            raise ValueError(f"File too large by firewall rules ({size_mb:.2f} MB > {max_size_mb_rules} MB limit)")

        # 8. Generate filename with timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Strip codec suffix from extension (e.g., "webm;codecs=opus" -> "webm")
        extension = mime_type.split("/")[-1].split(";")[0].strip()
        filename = f"voice_{timestamp}.{extension}"

        # 9. Create voice messages directory
        peer_files_dir = DPC_HOME_DIR / "conversations" / node_id / "files"
        peer_files_dir.mkdir(parents=True, exist_ok=True)

        # 10. Save voice file
        file_path = peer_files_dir / filename

        # Handle filename collisions
        if file_path.exists():
            stem = file_path.stem
            suffix = file_path.suffix
            counter = 1
            while file_path.exists():
                file_path = peer_files_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            with open(file_path, "wb") as f:
                f.write(audio_data)
            logger.info(f"Saved voice message: {file_path} ({len(audio_data)} bytes, {duration_seconds}s)")
        except Exception as e:
            raise ValueError(f"Failed to save voice file: {e}")

        # 11. Send via file transfer with voice_metadata
        voice_metadata = {
            "duration_seconds": duration_seconds,
            "sample_rate": int(self.settings.get("voice_messages", "default_sample_rate", "48000")),
            "channels": int(self.settings.get("voice_messages", "default_channels", "1")),
            "codec": self.settings.get("voice_messages", "default_codec", "opus"),
            "recorded_at": datetime.now(timezone.utc).isoformat()
        }

        try:
            transfer_id = await self.file_transfer_manager.send_file(
                node_id=node_id,
                file_path=file_path,
                voice_metadata=voice_metadata
            )
        except Exception as e:
            # Clean up file if transfer fails to start
            if file_path.exists():
                file_path.unlink()
            raise ValueError(f"Failed to start voice transfer: {e}")

        # 12. Return details for frontend
        return {
            "transfer_id": transfer_id,
            "file_path": str(file_path),
            "size_bytes": len(audio_data),
            "duration_seconds": duration_seconds,
            "voice_metadata": voice_metadata,
            "mime_type": mime_type
        }

    async def transcribe_audio(
        self,
        audio_base64: str = "",
        mime_type: str = "audio/webm",
        provider_alias: str | None = None,
        file_path: str | None = None
    ) -> dict:
        """
        Transcribe audio to text using local Whisper (primary), OpenAI API (fallback), or remote peer.

        v0.13.1+: Hybrid local/cloud/remote transcription with automatic fallback.
        v0.14.0+: Remote transcription support for P2P Whisper sharing.

        Priority:
        1. Remote peer (if provider_alias format is "remote:node_id:alias")
        2. LocalWhisperProvider (if enabled and configured)
        3. OpenAI/OpenAI-compatible provider (fallback)

        Args:
            audio_base64: Base64-encoded audio data (raw, not data URL)
            mime_type: Audio MIME type (default: audio/webm)
            provider_alias: Optional provider alias to use for transcription
                           Format: "local:alias" or "remote:node_id:alias"

        Returns:
            dict with transcription text and provider info

        Raises:
            ValueError: Invalid data or transcription failed
        """
        import base64
        import tempfile
        from pathlib import Path

        # 0. Check if this is a remote transcription request
        if provider_alias and provider_alias.startswith("remote:"):
            # Parse remote provider: "remote:node_id:alias"
            parts = provider_alias.split(":", 2)
            if len(parts) < 3:
                raise ValueError(f"Invalid remote provider format: {provider_alias}. Expected 'remote:node_id:alias'")

            node_id = parts[1]
            remote_alias = parts[2]

            logger.info(f"Remote transcription requested from peer {node_id[:20]}... using provider '{remote_alias}'")

            # If caller passed a file path, read and encode it for the remote peer
            if file_path and not audio_base64:
                import base64 as _b64
                from pathlib import Path as _Path
                audio_base64 = _b64.b64encode(_Path(file_path).read_bytes()).decode()

            try:
                # Call remote peer for transcription
                result = await self._request_transcription_from_peer(
                    peer_id=node_id,
                    audio_base64=audio_base64,
                    mime_type=mime_type,
                    provider=remote_alias,
                    timeout=120.0  # 2-minute timeout for remote transcription
                )

                # Format result to match local transcription response
                return {
                    "text": result.get("text", ""),
                    "language": result.get("language", "unknown"),
                    "duration": result.get("duration_seconds", 0),
                    "provider": f"remote_{result.get('provider', remote_alias)}",
                    "remote_node_id": node_id
                }

            except Exception as e:
                logger.error(f"Remote transcription failed: {e}", exc_info=True)
                raise ValueError(f"Remote transcription failed: {e}")

        # Handle local provider format from dropdown ("local:alias") - v0.15.1+
        if provider_alias and provider_alias.startswith("local:"):
            provider_alias = provider_alias[6:]  # Remove "local:" prefix

        # 1. Resolve audio to a local file path
        max_size_mb = int(self.settings.get("voice_messages", "max_size_mb", "10"))
        _temp_path_to_delete = None  # track temp files we created so we can clean up

        if file_path:
            # File already on disk (Tauri mode) — validate and use directly
            resolved = Path(file_path)
            if not resolved.exists():
                raise ValueError(f"Audio file not found: {file_path}")
            size_mb = resolved.stat().st_size / (1024 * 1024)
            if size_mb > max_size_mb:
                raise ValueError(f"Audio too large ({size_mb:.2f} MB > {max_size_mb} MB limit)")
            temp_path = str(resolved)
        else:
            # Decode base64 payload
            if not audio_base64:
                raise ValueError("Either file_path or audio_base64 must be provided")
            try:
                audio_data = base64.b64decode(audio_base64)
            except Exception as e:
                raise ValueError(f"Failed to decode base64 audio data: {e}")

            size_mb = len(audio_data) / (1024 * 1024)
            if size_mb > max_size_mb:
                raise ValueError(f"Audio too large ({size_mb:.2f} MB > {max_size_mb} MB limit)")

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=f".{mime_type.split('/')[-1]}"
            ) as temp_file:
                temp_file.write(audio_data)
                temp_path = temp_file.name
            _temp_path_to_delete = temp_path

        try:
            # 4. Determine transcription method (local vs cloud)
            transcription_method = None
            provider_config = None
            provider_obj = None

            if provider_alias and provider_alias in self.llm_manager.providers:
                # Use selected provider
                provider_obj = self.llm_manager.providers[provider_alias]
                provider_config = provider_obj.config
                provider_type = provider_obj.config.get("type", "")

                if provider_type == "local_whisper":
                    transcription_method = "local_whisper"
                elif provider_type == "openai" or provider_type == "openai_compatible":
                    transcription_method = "openai"
                else:
                    raise ValueError(f"Provider '{provider_alias}' does not support voice transcription. Type: {provider_type}")

                logger.info(f"Using selected voice provider: {provider_alias} ({transcription_method})")
            else:
                # Auto-detect: Try local first, then cloud
                local_enabled = self.settings.get_local_transcription_enabled()

                if local_enabled:
                    # Check for LocalWhisperProvider in providers
                    for provider in self.llm_manager.providers.values():
                        if provider.config.get("type") == "local_whisper":
                            provider_obj = provider
                            provider_config = provider.config
                            transcription_method = "local_whisper"

                            # Check file size limit for local transcription
                            local_max_mb = self.settings.get_local_transcription_max_file_size_mb()
                            if size_mb > local_max_mb:
                                logger.warning(f"Audio file ({size_mb:.2f} MB) exceeds local transcription limit ({local_max_mb} MB), skipping to cloud")
                                transcription_method = None
                            else:
                                break

                # Fallback to OpenAI if local not available or file too large
                if not transcription_method:
                    for provider in self.llm_manager.providers.values():
                        if provider.config.get("type") == "openai" or provider.config.get("type") == "openai_compatible":
                            provider_obj = provider
                            provider_config = provider.config
                            transcription_method = "openai"
                            break

            if not provider_config:
                raise ValueError(
                    "No voice transcription provider found. Please add a local_whisper or OpenAI-compatible provider.\n"
                    "Recommended: Add a local_whisper provider for privacy, with OpenAI as fallback."
                )

            logger.info(f"Transcribing audio using {transcription_method}: {temp_path}, mime_type={mime_type}")

            # 5. Perform transcription
            result = None

            if transcription_method == "local_whisper":
                try:
                    # Use local Whisper provider
                    result = await provider_obj.transcribe(temp_path)
                    logger.info(f"Local transcription successful: {result['text'][:100]}...")

                except Exception as local_error:
                    # Check if this is a model not cached error
                    from dpc_client_core.llm_manager import ModelNotCachedError
                    if isinstance(local_error.__cause__, ModelNotCachedError) or isinstance(local_error, ModelNotCachedError):
                        # Model needs to be downloaded - broadcast event to UI
                        error_details = local_error.__cause__ if isinstance(local_error.__cause__, ModelNotCachedError) else local_error

                        logger.info(f"Whisper model not cached, prompting user for download")

                        await self.local_api.broadcast_event("whisper_model_download_required", {
                            "model_name": error_details.model_name,
                            "cache_path": error_details.cache_path,
                            "download_size_gb": error_details.download_size_gb,
                            "provider_alias": provider_obj.alias
                        })

                        # Don't try fallback for this error - user needs to confirm download
                        raise ValueError(
                            f"Model '{error_details.model_name}' not cached locally. "
                            f"Please confirm download in the dialog to proceed."
                        ) from local_error

                    logger.warning(f"Local transcription failed: {local_error}")

                    # Check if fallback is enabled
                    fallback_enabled = self.settings.get_local_transcription_fallback_to_openai()

                    if fallback_enabled:
                        logger.info("Falling back to OpenAI API transcription...")

                        # Find OpenAI provider for fallback
                        fallback_provider = None
                        for provider in self.llm_manager.providers.values():
                            if provider.config.get("type") == "openai" or provider.config.get("type") == "openai_compatible":
                                fallback_provider = provider
                                break

                        if not fallback_provider:
                            raise ValueError("Local transcription failed and no OpenAI provider available for fallback") from local_error

                        # Use OpenAI API
                        result = await self._transcribe_with_openai(temp_path, fallback_provider.config)
                        result["fallback_reason"] = str(local_error)
                    else:
                        raise RuntimeError(f"Local transcription failed and fallback disabled: {local_error}") from local_error

            elif transcription_method == "openai":
                # Use OpenAI API
                result = await self._transcribe_with_openai(temp_path, provider_config)

            else:
                raise ValueError(f"Unknown transcription method: {transcription_method}")

            logger.info(f"Transcription successful ({result['provider']}): {len(result['text'])} characters")

            return {
                "status": "success",
                "text": result["text"],
                "provider": result["provider"],
                "language": result.get("language", "unknown"),
                "duration": result.get("duration", 0),
                "fallback_reason": result.get("fallback_reason")
            }

        except Exception as e:
            logger.error(f"Audio transcription failed: {e}", exc_info=True)

            # Broadcast error toast to UI for OOM errors (v0.14.1+)
            error_msg = str(e)
            if "VRAM" in error_msg or "too long" in error_msg or "CUDA out of memory" in error_msg:
                await self.local_api.broadcast_event("error_toast", {
                    "title": "Transcription Failed",
                    "message": error_msg,
                    "duration": 5000  # Show for 5 seconds
                })

            return {
                "status": "error",
                "error": str(e)
            }
        finally:
            # Only delete the temp file if we created it (not if the caller passed file_path)
            if _temp_path_to_delete:
                try:
                    Path(_temp_path_to_delete).unlink(missing_ok=True)
                except Exception:
                    pass

    async def preload_whisper_model(self, provider_alias: str | None = None) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.preload_whisper_model(provider_alias)

    async def download_whisper_model(self, provider_alias: str | None = None) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.download_whisper_model(provider_alias)

    async def _transcribe_with_openai(self, audio_path: str, provider_config: dict) -> dict:
        """
        Transcribe audio using OpenAI Whisper API.

        Args:
            audio_path: Path to audio file
            provider_config: Provider configuration dict

        Returns:
            Dict with transcription results
        """
        import os
        from openai import AsyncOpenAI

        # Get API key
        api_key = provider_config.get("api_key_env")
        if not api_key:
            raise ValueError("OpenAI API key not configured")

        api_key_value = os.environ.get(api_key)
        if not api_key_value:
            raise ValueError(f"OpenAI API key environment variable '{api_key}' not set")

        # Create client
        base_url = provider_config.get("base_url", "https://api.openai.com/v1")
        client = AsyncOpenAI(api_key=api_key_value, base_url=base_url)

        # Transcribe
        with open(audio_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )

        return {
            "text": transcript,
            "language": "unknown",  # OpenAI API doesn't return language in text format
            "duration": 0,  # OpenAI API doesn't return duration
            "provider": "openai"
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

    # Voice Transcription Methods (v0.13.2+ auto-transcription)

    async def _maybe_transcribe_voice_message(
        self,
        transfer_id: str,
        node_id: str,
        file_path: Path,
        voice_metadata: Dict[str, Any],
        is_sender: bool,
        allow_model_load: bool = False,
        group_id: Optional[str] = None
    ) -> None:
        """
        Conditionally trigger auto-transcription for a voice message.

        Implements distributed transcription protocol:
        - Sender: Optionally transcribes immediately (if sender_transcribes=true)
        - Recipients: Wait N seconds, then check if transcription exists. If not, first capable recipient transcribes.

        Args:
            transfer_id: Unique transfer identifier
            node_id: Peer node ID
            file_path: Path to voice file
            voice_metadata: Voice metadata dict (duration, codec, etc.)
            is_sender: True if this node sent the voice message, False if received
            allow_model_load: If True, allow model loading even for receivers (for retroactive transcription)
        """
        # 1. Check if auto-transcription is enabled globally
        if not self.settings.get_voice_transcription_enabled():
            logger.debug(f"Auto-transcription disabled globally, skipping transfer {transfer_id}")
            return

        # 2. Check per-conversation transcription setting (frontend checkbox)
        # Default to True if not set (backward compatibility)
        per_conversation_enabled = self._voice_transcription_settings.get(node_id, True)
        if not per_conversation_enabled:
            logger.debug(f"Auto-transcription disabled for conversation with {node_id}, skipping transfer {transfer_id}")
            return

        # 3. Check if already transcribed (deduplication)
        if transfer_id in self._voice_transcriptions:
            logger.debug(f"Transfer {transfer_id} already transcribed, skipping")
            return

        # 4. Check if this is a sender and sender_transcribes is disabled
        if is_sender and not self.settings.get_voice_transcription_sender_transcribes():
            logger.debug(f"Sender transcription disabled, skipping transfer {transfer_id}")
            return

        # 5. Acquire transcription lock to prevent race conditions
        if transfer_id not in self._transcription_locks:
            self._transcription_locks[transfer_id] = asyncio.Lock()

        async with self._transcription_locks[transfer_id]:
            # Double-check after acquiring lock
            if transfer_id in self._voice_transcriptions:
                logger.debug(f"Transfer {transfer_id} was transcribed while waiting for lock")
                return

            # 6. Recipients wait N seconds before attempting transcription (gives sender time to transcribe first)
            if not is_sender:
                delay = self.settings.get_voice_transcription_recipient_delay_seconds()
                logger.debug(f"Recipient waiting {delay}s before checking transcription for {transfer_id}")
                await asyncio.sleep(delay)

                # Check again after delay
                if transfer_id in self._voice_transcriptions:
                    logger.debug(f"Transfer {transfer_id} was transcribed during delay period")
                    return

            # 7. Check if this node has transcription capability
            # For receivers: check if model is loaded (enables passive wait mode if not ready)
            #   UNLESS allow_model_load=True (retroactive transcription case)
            # For senders: check if provider exists (allow lazy loading to handle model)
            check_loaded = not is_sender and not allow_model_load
            has_capability = await self._check_transcription_capability(check_model_loaded=check_loaded)
            if not has_capability:
                # No local capability - wait for peer's transcription (passive mode)
                if not is_sender:
                    timeout_seconds = self.settings.get_voice_transcription_timeout_seconds() or 240
                    # Log start of passive wait mode (v0.13.3+ - better UX logging)
                    logger.info(f"Starting passive wait mode for {transfer_id}: waiting {timeout_seconds}s for sender's transcription")
                    # Wait for peer to send transcription with progress logging
                    for i in range(timeout_seconds):
                        await asyncio.sleep(1)

                        # Log progress every 30 seconds (v0.13.3+)
                        if i > 0 and i % 30 == 0:
                            logger.info(f"Still waiting for sender's transcription for {transfer_id}: {i}/{timeout_seconds}s elapsed")

                        if transfer_id in self._voice_transcriptions:
                            logger.info(f"Received transcription from sender for {transfer_id} after {i}s")
                            return

                    # Timeout expired - transcribe locally (v0.13.3+ - clearer logging)
                    logger.warning(f"Timeout after {timeout_seconds}s: No transcription received from sender for {transfer_id}")
                    logger.info(f"Starting local transcription for {transfer_id} (will load Whisper model if needed)")

                    has_capability_lazy = await self._check_transcription_capability(check_model_loaded=False)
                    if not has_capability_lazy:
                        logger.error(f"No transcription provider configured at all for {transfer_id}, giving up")
                        return
                    # Fall through to transcribe locally
                    logger.info(f"Transcription capability found (lazy loading), proceeding to transcribe {transfer_id}")
                else:
                    logger.info(f"Sender has no transcription provider configured for {transfer_id}, skipping")
                    return

            # 7. Read audio file and convert to base64
            try:
                with open(file_path, "rb") as f:
                    audio_data = f.read()
                import base64
                audio_base64 = base64.b64encode(audio_data).decode('utf-8')
            except Exception as e:
                logger.error(f"Failed to read audio file for transcription: {e}")
                return

            # 8. Final check: Did peer send transcription while we were preparing?
            # This prevents duplicate work if sender took long to load model but finished before us
            if transfer_id in self._voice_transcriptions:
                logger.info(f"Transcription received from peer while preparing, cancelling local attempt for {transfer_id}")
                return

            # 9. Transcribe audio using configured provider
            try:
                mime_type = voice_metadata.get("mime_type", "audio/webm")

                # Use provider priority list
                provider_priority = self.settings.get_voice_transcription_provider_priority()
                result = None

                for provider_alias in provider_priority:
                    try:
                        logger.info(f"Attempting transcription with provider: {provider_alias}")
                        result = await self.transcribe_audio(audio_base64, mime_type, provider_alias)
                        break  # Success, stop trying providers
                    except Exception as e:
                        logger.warning(f"Transcription failed with {provider_alias}: {e}")
                        continue  # Try next provider

                if result is None:
                    logger.error(f"All transcription providers failed for transfer {transfer_id}")
                    return

                # 9. Validate transcription result (ensure not empty)
                transcription_text = result.get("text", "").strip()
                if not transcription_text:
                    logger.error(f"Transcription returned empty text for transfer {transfer_id}, result: {result}")
                    return

                # 9a. Final check before storing: Did peer send transcription while we were transcribing?
                # Prefer peer's transcription over ours if both completed (first-wins deduplication)
                existing = self._voice_transcriptions.get(transfer_id)
                if existing and existing.get("success", False):
                    logger.info(f"Peer's transcription arrived while we were transcribing {transfer_id}, discarding local result")
                    return

                # 10. Store transcription locally
                from datetime import datetime, timezone
                transcription_data = {
                    "text": transcription_text,
                    "transcriber_node_id": self.p2p_manager.node_id,  # Orchestrator
                    "provider": result.get("provider", "unknown"),
                    "confidence": result.get("confidence", 0.0),
                    "language": result.get("language", "unknown"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "success": True  # Mark as successful transcription
                }

                # Add remote provider credit if used (v0.13.2+ remote transcription)
                if "remote_node_id" in result:
                    transcription_data["remote_provider_node_id"] = result["remote_node_id"]

                self._voice_transcriptions[transfer_id] = transcription_data

                # 11. Update conversation monitor with transcribed text (v0.15.1 fix)
                # This ensures proposals include the actual transcription instead of placeholder
                monitor = self._get_or_create_conversation_monitor(node_id)
                if monitor:
                    # Find and update Message objects in buffer with transcription
                    for message_list in [monitor.message_buffer, monitor.full_conversation]:
                        for msg_obj in message_list:
                            if hasattr(msg_obj, 'attachment_transfer_id') and msg_obj.attachment_transfer_id == transfer_id:
                                # Replace placeholder with actual transcription
                                msg_obj.text = transcription_text
                                logger.debug(f"Updated local transcription in Message object: {transfer_id}")
                                break

                # 12. Broadcast transcription to peers (only if not empty)
                logger.info(f"Broadcasting transcription to peer {node_id}: {len(transcription_text)} chars")
                await self._broadcast_voice_transcription(node_id, transfer_id, transcription_data, group_id=group_id)

                # 13. Notify UI
                await self.local_api.broadcast_event("voice_transcription_complete", {
                    "transfer_id": transfer_id,
                    "node_id": node_id,
                    **transcription_data
                })

                logger.info(f"Successfully transcribed voice message {transfer_id} using {result.get('provider')} ({len(transcription_text)} chars)")

            except Exception as e:
                logger.error(f"Transcription failed for transfer {transfer_id}: {e}", exc_info=True)

    def _load_voice_transcription_settings(self) -> None:
        """Delegated to VoiceService."""
        self.voice_service._load_voice_transcription_settings()

    def _save_voice_transcription_settings(self) -> None:
        """Delegated to VoiceService."""
        self.voice_service._save_voice_transcription_settings()

    def _is_transcription_needed(self) -> bool:
        """Delegated to VoiceService."""
        return self.voice_service._is_transcription_needed()

    async def _retroactively_transcribe_conversation(self, node_id: str) -> None:
        """
        Retroactively transcribe all untranscribed voice messages in a conversation.

        Called when user enables auto-transcribe checkbox. Scans conversation history
        for voice messages without transcriptions and transcribes them in background.

        Args:
            node_id: Peer node ID for the conversation
        """
        try:
            # Get conversation monitor
            monitor = self.conversation_monitors.get(node_id)
            if not monitor:
                logger.debug(f"No conversation monitor for {node_id}, skipping retroactive transcription")
                return

            # Get message history
            history = monitor.get_message_history()

            # Find all voice messages without transcriptions
            untranscribed_voices = []
            for msg in history:
                if "attachments" in msg:
                    for attachment in msg["attachments"]:
                        if attachment.get("type") == "voice":
                            transfer_id = attachment.get("transfer_id")
                            file_path_str = attachment.get("file_path")
                            voice_metadata = attachment.get("voice_metadata")

                            # Check if already transcribed
                            if transfer_id and transfer_id not in self._voice_transcriptions:
                                if file_path_str and voice_metadata:
                                    file_path = Path(file_path_str)
                                    if file_path.exists():
                                        untranscribed_voices.append({
                                            "transfer_id": transfer_id,
                                            "file_path": file_path,
                                            "voice_metadata": voice_metadata
                                        })

            if not untranscribed_voices:
                logger.debug(f"No untranscribed voice messages found for {node_id}")
                return

            logger.info(f"Found {len(untranscribed_voices)} untranscribed voice messages for {node_id}, starting retroactive transcription")

            # Broadcast start event to UI
            await self.local_api.broadcast_event("retroactive_transcription_started", {
                "node_id": node_id,
                "total_count": len(untranscribed_voices)
            })

            # Transcribe each voice message
            for idx, voice_data in enumerate(untranscribed_voices):
                try:
                    # Broadcast progress to UI
                    await self.local_api.broadcast_event("retroactive_transcription_progress", {
                        "node_id": node_id,
                        "current": idx + 1,
                        "total": len(untranscribed_voices)
                    })

                    # Use the existing transcription logic
                    await self._maybe_transcribe_voice_message(
                        transfer_id=voice_data["transfer_id"],
                        node_id=node_id,
                        file_path=voice_data["file_path"],
                        voice_metadata=voice_data["voice_metadata"],
                        is_sender=False,  # Treat as receiver (no delay)
                        allow_model_load=True  # v0.13.3: Allow model loading for retroactive transcription
                    )

                    logger.debug(f"Retroactively transcribed {idx + 1}/{len(untranscribed_voices)} for {node_id}")

                except Exception as e:
                    logger.error(f"Failed to retroactively transcribe voice message {voice_data['transfer_id']}: {e}", exc_info=True)
                    continue  # Continue with next message

            # Broadcast completion event to UI
            await self.local_api.broadcast_event("retroactive_transcription_completed", {
                "node_id": node_id,
                "transcribed_count": len(untranscribed_voices)
            })

            logger.info(f"Retroactive transcription completed for {node_id}: {len(untranscribed_voices)} messages transcribed")

        except Exception as e:
            logger.error(f"Error during retroactive transcription for {node_id}: {e}", exc_info=True)
            # Broadcast error event to UI
            try:
                await self.local_api.broadcast_event("retroactive_transcription_error", {
                    "node_id": node_id,
                    "error": str(e)
                })
            except Exception:
                pass  # Ignore broadcast errors

    async def _check_transcription_capability(self, check_model_loaded: bool = True) -> bool:
        """Delegated to VoiceService."""
        return await self.voice_service._check_transcription_capability(check_model_loaded)

    async def _broadcast_voice_transcription(
        self,
        node_id: str,
        transfer_id: str,
        transcription_data: Dict[str, Any],
        group_id: Optional[str] = None
    ) -> None:
        """
        Broadcast VOICE_TRANSCRIPTION message to peer(s).

        Args:
            node_id: Peer node ID to send to (for P2P)
            transfer_id: Voice message transfer ID
            transcription_data: Transcription result
            group_id: If set, fan-out to all group members (v0.19.0)
        """
        message = {
            "command": "VOICE_TRANSCRIPTION",
            "payload": {
                "transfer_id": transfer_id,
                "transcription_text": transcription_data.get("text", ""),
                **transcription_data
            }
        }

        if group_id and hasattr(self, 'group_manager'):
            # Group chat: Fan-out to all connected group members
            group = self.group_manager.get_group(group_id)
            if group:
                connected = self.p2p_coordinator.get_connected_peers()
                for member_id in group.members:
                    if member_id != self.p2p_manager.node_id and member_id in connected:
                        try:
                            await self.p2p_manager.send_message_to_peer(member_id, message)
                            logger.debug(f"Broadcasted VOICE_TRANSCRIPTION for {transfer_id} to group member {member_id}")
                        except Exception as e:
                            logger.error(f"Failed to broadcast transcription to group member {member_id}: {e}")
        else:
            # P2P: Send to specific peer
            try:
                await self.p2p_manager.send_message_to_peer(node_id, message)
                logger.debug(f"Broadcasted VOICE_TRANSCRIPTION for {transfer_id} to {node_id}")
            except Exception as e:
                logger.error(f"Failed to broadcast transcription to {node_id}: {e}")

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

    async def get_voice_transcription_config(self) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.get_voice_transcription_config()

    async def save_voice_transcription_config(self, config: dict) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.save_voice_transcription_config(config)

    async def set_conversation_transcription(self, node_id: str, enabled: bool) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.set_conversation_transcription(node_id, enabled)

    async def get_conversation_transcription(self, node_id: str) -> dict:
        """Delegated to VoiceService."""
        return await self.voice_service.get_conversation_transcription(node_id)

    async def vote_knowledge_commit(
        self,
        proposal_id: str,
        vote: str,
        comment: str = None
    ) -> Dict[str, Any]:
        """Delegated to KnowledgeService."""
        return await self.knowledge_service.vote_knowledge_commit(proposal_id, vote, comment)

    async def _ai_agent_vote_on_proposal(self, proposal_id: str, ai_agent_node_id: str) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._ai_agent_vote_on_proposal(proposal_id, ai_agent_node_id)

    async def _evaluate_knowledge_proposal_for_ai_vote(
        self,
        proposal,
        provider_alias: Optional[str] = None,
        conversation_history: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Delegated to KnowledgeService."""
        return await self.knowledge_service._evaluate_knowledge_proposal_for_ai_vote(proposal, provider_alias=provider_alias, conversation_history=conversation_history)

    async def _broadcast_to_peers(self, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected peers.

        Used by ConsensusManager to broadcast votes and proposals.
        """
        await self.p2p_coordinator.broadcast_to_peers(message)

    async def _broadcast_to_group(self, group_id: str, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected members of a group (except self).

        Used for group chat fan-out messaging.
        """
        group = self.group_manager.get_group(group_id)
        if not group:
            logger.warning("Cannot broadcast: group %s not found", group_id)
            return

        for node_id in group.members:
            if node_id == self.p2p_manager.node_id:
                continue
            if node_id in self.p2p_manager.peers:
                try:
                    await self.p2p_manager.send_message_to_peer(node_id, message)
                except Exception as e:
                    logger.error("Failed to send group message to %s: %s", node_id[:20], e)

    def _get_or_create_conversation_monitor(self, conversation_id: str, instruction_set_name: str = None) -> ConversationMonitor:
        """Delegated to KnowledgeService."""
        return self.knowledge_service._get_or_create_conversation_monitor(conversation_id, instruction_set_name)

    async def end_conversation_session(
        self,
        conversation_id: str,
        initiated_by: str = "user_request",
    ) -> Dict[str, Any]:
        """Delegated to KnowledgeService."""
        return await self.knowledge_service.end_conversation_session(conversation_id, initiated_by)

    async def toggle_auto_knowledge_detection(self, enabled: bool = None) -> Dict[str, Any]:
        """Delegated to KnowledgeService."""
        return await self.knowledge_service.toggle_auto_knowledge_detection(enabled)

    def _resolve_agent_token_limit(self, agent_id: str) -> int:
        """Delegates to AgentService."""
        if not self.agent_service:
            return 0
        return self.agent_service._resolve_agent_token_limit(agent_id)

    async def get_conversation_history(self, conversation_id: str) -> Dict[str, Any]:
        """Get conversation history from backend (for frontend sync after page refresh).

        UI Integration: Called when frontend reconnects or switches to chat with empty history.

        Args:
            conversation_id: The conversation/chat ID to get history for

        Returns:
            Dict with messages list
        """
        try:
            monitor = self.conversation_monitors.get(conversation_id)

            # Special handling for agent conversations - their monitors are in AgentManager
            if not monitor and conversation_id.startswith("agent_"):
                # Get the dpc_agent provider to access the agent manager
                dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers'):
                    # Check if we have a manager for this specific agent
                    # conversation_id format is "agent_001", so we use it as the agent_id
                    if conversation_id in dpc_agent_provider._managers:
                        agent_manager = dpc_agent_provider._managers[conversation_id]
                        if hasattr(agent_manager, '_agent_monitors'):
                            monitor = agent_manager._agent_monitors.get(conversation_id)
                            if monitor:
                                # Monitor exists in memory — load history from disk if empty
                                # (Check file exists to prevent reloading after reset, where
                                # message_history is cleared but file is deleted)
                                if not monitor.message_history:
                                    history_path = monitor._get_history_path()
                                    if history_path.exists():
                                        logger.debug("Loading agent conversation history from disk for %s", conversation_id)
                                        monitor.load_history()
                                    else:
                                        logger.debug("No history file found for %s (likely reset), skipping load", conversation_id)
                                logger.debug("Found agent conversation monitor for %s in AgentManager", conversation_id)
                            else:
                                # Agent manager exists but monitor not created yet (no messages
                                # processed since service start) — load history from disk directly
                                logger.debug("Agent monitor not created for %s, loading history from disk", conversation_id)
                                history_path = DPC_HOME_DIR / "conversations" / conversation_id / "history.json"
                                if history_path.exists():
                                    try:
                                        import json as _json
                                        with open(history_path, encoding="utf-8") as f:
                                            data = _json.load(f)
                                        messages = data.get("messages", [])
                                        logger.info("Loaded %d messages from disk for %s", len(messages), conversation_id)
                                        token_stats = data.get("token_stats", {})
                                        history_tokens = sum(len(msg.get("content", "") or "") for msg in messages) // 4
                                        token_limit = token_stats.get("token_limit", 0) or self._resolve_agent_token_limit(conversation_id)
                                        context_estimated = token_stats.get("context_estimated", 0)
                                        return {
                                            "status": "success",
                                            "messages": messages,
                                            "message_count": len(messages),
                                            "tokens_used": token_stats.get("current_token_count", history_tokens),
                                            "token_limit": token_limit,
                                            "history_tokens": history_tokens,
                                            "context_estimated": context_estimated,
                                        }
                                    except Exception as e:
                                        logger.error("Failed to load agent history from disk: %s", e)
                                else:
                                    logger.debug("No history file found at %s", history_path)
                    else:
                        # Agent manager not created yet - try to load history directly from disk
                        logger.debug("Agent manager not created for %s, attempting to load history from disk", conversation_id)
                        history_path = DPC_HOME_DIR / "conversations" / conversation_id / "history.json"
                        if history_path.exists():
                            try:
                                import json
                                with open(history_path, encoding="utf-8") as f:
                                    data = json.load(f)
                                messages = data.get("messages", [])
                                logger.info("Loaded %d messages from disk for %s (agent manager not created yet)", len(messages), conversation_id)
                                token_stats = data.get("token_stats", {})
                                history_tokens = sum(len(msg.get("content", "") or "") for msg in messages) // 4
                                token_limit = token_stats.get("token_limit", 0) or self._resolve_agent_token_limit(conversation_id)
                                context_estimated = token_stats.get("context_estimated", 0)
                                # Return early with the loaded messages
                                return {
                                    "status": "success",
                                    "messages": messages,
                                    "message_count": len(messages),
                                    "tokens_used": token_stats.get("current_token_count", history_tokens),
                                    "token_limit": token_limit,
                                    "history_tokens": history_tokens,
                                    "context_estimated": context_estimated,
                                }
                            except Exception as e:
                                logger.error("Failed to load agent history from disk: %s", e)
                        else:
                            logger.debug("No history file found at %s", history_path)

            if not monitor:
                logger.debug("No conversation monitor found for %s, returning empty history", conversation_id)
                return {
                    "status": "success",
                    "messages": [],
                    "message_count": 0
                }

            # Get message history (same format as export_history)
            history = monitor.get_message_history()

            # Convert to frontend format (role, content, attachments)
            # v0.13.2+: Merge transcription data into voice attachments from _voice_transcriptions
            # v0.15.3+: Include timestamp and sender info for proper chat history display
            messages = []
            for msg in history:
                message_dict = {
                    "role": msg["role"],
                    "content": msg["content"]
                }
                # Add timestamp if present (v0.15.3)
                if "timestamp" in msg:
                    message_dict["timestamp"] = msg["timestamp"]
                # Add sender info if present (v0.15.3)
                if "sender_node_id" in msg:
                    message_dict["sender_node_id"] = msg["sender_node_id"]
                if "sender_name" in msg:
                    message_dict["sender_name"] = msg["sender_name"]
                if "attachments" in msg:
                    # Deep copy attachments to avoid mutating original
                    attachments = []
                    for attachment in msg["attachments"]:
                        attachment_copy = attachment.copy()

                        # Merge transcription data for voice messages
                        if attachment_copy.get("type") == "voice":
                            transfer_id = attachment_copy.get("transfer_id")
                            if transfer_id and transfer_id in self._voice_transcriptions:
                                transcription_data = self._voice_transcriptions[transfer_id]
                                attachment_copy["transcription"] = {
                                    "text": transcription_data.get("text", ""),
                                    "provider": transcription_data.get("provider", ""),
                                    "transcriber_node_id": transcription_data.get("transcriber_node_id", ""),
                                    "confidence": transcription_data.get("confidence"),
                                    "language": transcription_data.get("language", ""),
                                    "timestamp": transcription_data.get("timestamp", "")
                                }
                                logger.debug(f"Merged transcription for transfer_id={transfer_id} into history")

                        attachments.append(attachment_copy)

                    message_dict["attachments"] = attachments
                messages.append(message_dict)

            logger.info("Retrieved %d messages from backend for %s", len(messages), conversation_id)

            token_usage = monitor.get_token_usage()
            # history_tokens: dialog-only (chars ÷ 4), same basis as the token counter
            history_tokens = sum(len(msg.get("content", "") or "") for msg in messages) // 4
            # context_estimated: full LLM context from the last request.
            # For agent monitors, stored in _last_context_estimated.
            # For local AI monitors, current_token_count = prompt_tokens (already the full context).
            context_estimated = getattr(monitor, '_last_context_estimated', 0) or token_usage.get("tokens_used", 0)
            return {
                "status": "success",
                "messages": messages,
                "message_count": len(messages),
                "tokens_used": token_usage.get("tokens_used", 0),
                "token_limit": token_usage.get("token_limit", 0),
                "history_tokens": history_tokens,
                "context_estimated": context_estimated,
            }

        except Exception as e:
            logger.error("Error getting conversation history: %s", e, exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "messages": [],
                "message_count": 0
            }

    async def propose_new_session(self, conversation_id: str) -> Dict[str, Any]:
        """Propose a new session to connected peers (mutual approval flow).

        UI Integration: Called when user clicks "New Session" button.
        For P2P chats: Initiates voting process - history only cleared if all peers approve.
        For AI chats: Directly resets conversation (no voting needed).
        For Agent chats: Directly resets conversation via DpcAgentManager (no voting needed).
        For Telegram chats: Directly resets conversation (no voting needed).

        Args:
            conversation_id: The conversation/chat ID to reset

        Returns:
            Dict with status and proposal_id (for P2P) or status (for AI/Agent/Telegram)
        """
        try:
            # Check if this is an AI chat (local_ai or ai_chat_xxx), Telegram chat, or Agent chat
            if conversation_id == 'local_ai' or conversation_id.startswith('ai_') or conversation_id.startswith('telegram-') or conversation_id.startswith('agent_'):
                # AI chats, Telegram chats, and Agent chats: directly reset without proposal
                # These chats don't support peer voting (no peer connection)

                # Determine chat type for logging
                if conversation_id.startswith('agent_'):
                    chat_type = "Agent"
                elif conversation_id.startswith('ai_') or conversation_id == 'local_ai':
                    chat_type = "AI"
                else:
                    chat_type = "Telegram"

                # Handle agent conversations differently (use DpcAgentManager)
                if conversation_id.startswith('agent_'):
                    logger.info("Resetting %s conversation: %s", chat_type, conversation_id)

                    # Get the DPC agent provider
                    dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")

                    if not dpc_agent_provider or not hasattr(dpc_agent_provider, '_managers'):
                        return {
                            "status": "error",
                            "message": "Agent provider not available"
                        }

                    # Get the agent manager for this conversation
                    # conversation_id format is "agent_001", which matches the agent_id
                    agent_manager = dpc_agent_provider._managers.get(conversation_id)

                    if not agent_manager:
                        # Manager not initialized yet — history may still exist on disk, delete it directly
                        history_path = DPC_HOME_DIR / "conversations" / conversation_id / "history.json"
                        if history_path.exists():
                            history_path.unlink()
                            logger.info("Deleted history file for %s (agent manager not yet initialized)", conversation_id)
                        await self.local_api.broadcast_event("conversation_reset", {"conversation_id": conversation_id})
                        return {"status": "success", "message": f"Agent conversation reset: {conversation_id}"}

                    # Reset via agent manager (succeeds if monitor already exists in memory)
                    success = agent_manager.reset_conversation(conversation_id)

                    if not success:
                        # Monitor not in memory yet (user hasn't sent a message this session,
                        # but history file exists on disk from a previous session).
                        # Delete the history file directly so get_conversation_history returns 0.
                        history_path = DPC_HOME_DIR / "conversations" / conversation_id / "history.json"
                        if history_path.exists():
                            history_path.unlink()
                            logger.info("Deleted orphaned history file for %s (monitor not yet initialized)", conversation_id)
                            success = True
                        else:
                            logger.debug("No history file found for %s, nothing to reset", conversation_id)
                            success = True  # Nothing to clear — treat as success

                    if success:
                        logger.info("Successfully reset agent conversation: %s", conversation_id)

                        # Broadcast to UI (same as AI/Telegram chat reset flow)
                        await self.local_api.broadcast_event(
                            "conversation_reset",
                            {"conversation_id": conversation_id}
                        )

                        return {
                            "status": "success",
                            "message": f"Agent conversation reset: {conversation_id}"
                        }
                    else:
                        return {
                            "status": "error",
                            "message": f"Failed to reset agent conversation: {conversation_id}"
                        }

                # Handle AI and Telegram chats using the standard reset
                logger.info("Resetting %s conversation: %s", chat_type, conversation_id)
                result = await self.reset_conversation(conversation_id)
                return result

            # Group chats: participants from group members
            if conversation_id.startswith("group-"):
                group = self.group_manager.get_group(conversation_id)
                if not group:
                    return {"status": "error", "message": f"Group {conversation_id} not found"}

                participants = set(group.members)
                # Check at least one other member is online
                connected = self.p2p_coordinator.get_connected_peers()
                online_members = [m for m in group.members if m != self.p2p_manager.node_id and m in connected]
                if not online_members:
                    return {"status": "error", "message": "No group members are online"}
            else:
                # P2P chats: use proposal flow
                # For now, conversation_id is the peer_id in P2P mode
                participants = {self.p2p_manager.node_id, conversation_id}

                # Check if peer is connected
                if conversation_id not in self.p2p_manager.peers:
                    return {
                        "status": "error",
                        "message": "Peer must be online to propose new session"
                    }

            # Initiate proposal via session manager
            result = await self.session_manager.propose_new_session(
                conversation_id=conversation_id,
                participants=participants
            )

            return result

        except ValueError as e:
            # Duplicate proposal
            logger.warning("Cannot propose new session: %s", e)
            return {
                "status": "error",
                "message": str(e)
            }
        except Exception as e:
            logger.error("Error proposing new session: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def reset_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Reset conversation history and context tracking (internal method).

        NOTE: This is now an internal method called after proposal approval.
        UI should call propose_new_session() instead for mutual approval.

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

    async def get_conversation_settings(self, conversation_id: str) -> Dict[str, Any]:
        """Get per-conversation settings including history persistence.

        Args:
            conversation_id: The conversation/chat ID

        Returns:
            Dict with conversation settings
        """
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            settings = monitor._load_conversation_settings()
            return {
                "status": "success",
                "settings": settings
            }
        except Exception as e:
            logger.error("Error getting conversation settings: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def set_conversation_persist_history(self, conversation_id: str, persist: bool) -> Dict[str, Any]:
        """Set whether to persist history for a conversation.

        Args:
            conversation_id: The conversation/chat ID
            persist: True to persist history, False for ephemeral

        Returns:
            Dict with status
        """
        try:
            monitor = self._get_or_create_conversation_monitor(conversation_id)
            success = monitor.set_persist_history(persist)

            if success:
                # If enabling persistence and there's existing history, save it
                if persist and monitor.message_history:
                    monitor.save_history()
                    logger.info("Enabled history persistence for %s and saved %d messages",
                               conversation_id, len(monitor.message_history))
                elif not persist:
                    # If disabling persistence, delete any existing history file
                    monitor.clear_history()
                    logger.info("Disabled history persistence for %s and cleared history", conversation_id)

                # Broadcast to UI
                await self.local_api.broadcast_event(
                    "conversation_settings_changed",
                    {"conversation_id": conversation_id, "persist_history": persist}
                )

                return {
                    "status": "success",
                    "persist_history": persist
                }
            else:
                return {
                    "status": "error",
                    "message": "Failed to save settings"
                }
        except Exception as e:
            logger.error("Error setting conversation persistence: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def delete_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Delete an entire conversation including history, settings, and files.

        This is a complete deletion - removes the conversation folder.

        Args:
            conversation_id: The conversation/chat ID to delete

        Returns:
            Dict with status
        """
        try:
            # For groups, use the group manager
            if conversation_id.startswith("group-"):
                success = self.group_manager.leave_group(conversation_id)
                if not success:
                    return {
                        "status": "error",
                        "message": "Group not found or could not be deleted"
                    }
            else:
                # For P2P conversations, delete via conversation monitor
                if conversation_id in self.conversation_monitors:
                    monitor = self.conversation_monitors[conversation_id]
                    monitor.delete_conversation_folder()
                    del self.conversation_monitors[conversation_id]
                else:
                    # Even if no monitor exists, try to delete the folder
                    from pathlib import Path
                    import shutil
                    conv_dir = Path.home() / ".dpc" / "conversations" / conversation_id
                    if conv_dir.exists():
                        shutil.rmtree(conv_dir)

            logger.info("Deleted conversation: %s", conversation_id)

            # Broadcast to UI
            await self.local_api.broadcast_event(
                "conversation_deleted",
                {"conversation_id": conversation_id}
            )

            return {
                "status": "success",
                "message": "Conversation deleted successfully"
            }
        except Exception as e:
            logger.error("Error deleting conversation: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    # =========================================================================
    # Group Chat Commands (v0.19.0)
    # =========================================================================

    async def create_group_chat(self, name: str, topic: str = "", member_node_ids: list = None) -> Dict[str, Any]:
        """Create a new group chat and notify members.

        Args:
            name: Group display name
            topic: Group topic/description
            member_node_ids: List of peer node IDs to invite
        """
        try:
            members = member_node_ids or []
            group = self.group_manager.create_group(name, topic, members)

            # Send GROUP_CREATE to all members
            await self._broadcast_to_group(group.group_id, {
                "command": "GROUP_CREATE",
                "payload": group.to_dict()
            })

            return {"status": "success", "group": group.to_dict()}
        except Exception as e:
            logger.error("Error creating group: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_groups(self) -> Dict[str, Any]:
        """Get all groups this node belongs to."""
        groups = [g.to_dict() for g in self.group_manager.get_all_groups()]
        return {"status": "success", "groups": groups}

    def parse_mentions(self, text: str, group_members: List[str]) -> List[Dict[str, Any]]:
        """Parse @mentions in text and return list of mention objects.

        Args:
            text: Message text to parse
            group_members: List of node IDs in the group

        Returns:
            List of mention dicts with node_id, name, start, end
        """
        mentions = []
        # Pattern matches @Name or @Name-With-Dashes
        pattern = r'@([\w\-]+)'

        for match in re.finditer(pattern, text):
            name = match.group(1)
            # Resolve name to node_id from group members
            for node_id in group_members:
                peer_name = self.peer_metadata.get(node_id, {}).get("name", "")
                if peer_name and peer_name.lower() == name.lower():
                    mentions.append({
                        "node_id": node_id,
                        "name": peer_name,
                        "start": match.start(),
                        "end": match.end()
                    })
                    break

        return mentions

    async def send_group_message(self, group_id: str, text: str) -> Dict[str, Any]:
        """Send a text message to all group members.

        Args:
            group_id: Target group ID
            text: Message text
        """
        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            sender_name = "User"

            # Parse @mentions in the message text
            mentions = self.parse_mentions(text, group.members)

            # v0.20.0: Generate unique message ID on sender side
            import uuid
            message_id = str(uuid.uuid4())
            timestamp = datetime.now(timezone.utc).isoformat()

            # Fan-out GROUP_TEXT to all connected members
            await self._broadcast_to_group(group_id, {
                "command": "GROUP_TEXT",
                "payload": {
                    "group_id": group_id,
                    "text": text,
                    "sender_name": sender_name,
                    "mentions": mentions,
                    "message_id": message_id,  # v0.20.0: Include sender-generated ID
                    "timestamp": timestamp,
                }
            })

            # Track in conversation monitor for knowledge extraction
            monitor = self._get_or_create_conversation_monitor(group_id)
            from .conversation_monitor import Message as ConvMessage
            conv_message = ConvMessage(
                message_id=message_id,
                conversation_id=group_id,
                sender_node_id=self.p2p_manager.node_id,
                sender_name=sender_name,
                text=text,
                timestamp=timestamp,
            )
            await monitor.on_message(conv_message)

            return {"status": "success", "message_id": message_id}
        except Exception as e:
            logger.error("Error sending group message: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def add_group_member(self, group_id: str, node_id: str) -> Dict[str, Any]:
        """Add a member to a group and notify all members.

        Args:
            group_id: Target group ID
            node_id: Node ID to add
        """
        try:
            group = self.group_manager.add_member(group_id, node_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            # Notify existing members
            await self._broadcast_to_group(group_id, {
                "command": "GROUP_SYNC",
                "payload": group.to_dict()
            })

            # Send full group create to the new member (they need the full state)
            if node_id in self.p2p_manager.peers:
                await self.p2p_manager.send_message_to_peer(node_id, {
                    "command": "GROUP_CREATE",
                    "payload": group.to_dict()
                })

            return {"status": "success", "group": group.to_dict()}
        except Exception as e:
            logger.error("Error adding group member: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def remove_group_member(self, group_id: str, node_id: str) -> Dict[str, Any]:
        """Remove a member from a group and notify all members.

        Args:
            group_id: Target group ID
            node_id: Node ID to remove
        """
        try:
            group = self.group_manager.remove_member(group_id, node_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            # Notify all members (including the removed one)
            await self._broadcast_to_group(group_id, {
                "command": "GROUP_SYNC",
                "payload": group.to_dict()
            })

            return {"status": "success", "group": group.to_dict()}
        except Exception as e:
            logger.error("Error removing group member: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def leave_group(self, group_id: str) -> Dict[str, Any]:
        """Leave a group and notify remaining members."""
        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            # Notify other members before leaving
            await self._broadcast_to_group(group_id, {
                "command": "GROUP_LEAVE",
                "payload": {"group_id": group_id}
            })

            # Remove locally
            self.group_manager.leave_group(group_id)

            # Clean up conversation monitor
            if group_id in self.conversation_monitors:
                del self.conversation_monitors[group_id]

            return {"status": "success"}
        except Exception as e:
            logger.error("Error leaving group: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def delete_group(self, group_id: str) -> Dict[str, Any]:
        """Delete a group (creator-only) and notify all members."""
        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            if group.created_by != self.p2p_manager.node_id:
                return {"status": "error", "message": "Only the group creator can delete a group"}

            # Notify all members
            await self._broadcast_to_group(group_id, {
                "command": "GROUP_DELETE",
                "payload": {"group_id": group_id}
            })

            # Delete locally
            self.group_manager.delete_group(group_id, self.p2p_manager.node_id)

            # Clean up conversation monitor
            if group_id in self.conversation_monitors:
                del self.conversation_monitors[group_id]

            return {"status": "success"}
        except Exception as e:
            logger.error("Error deleting group: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def send_group_file(self, group_id: str, file_path: str) -> Dict[str, Any]:
        """Send a file to all group members via fan-out file transfer.

        Args:
            group_id: Target group ID
            file_path: Path to file to send
        """
        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            from pathlib import Path
            path = Path(file_path)
            if not path.exists():
                return {"status": "error", "message": f"File not found: {file_path}"}

            connected_peers = self.p2p_coordinator.get_connected_peers()
            transfer_ids = []

            for node_id in group.members:
                if node_id == self.p2p_manager.node_id:
                    continue
                if node_id not in connected_peers:
                    logger.debug("Skipping offline member %s for group file", node_id[:20])
                    continue

                try:
                    tid = await self.file_transfer_manager.send_file(
                        node_id=node_id,
                        file_path=path,
                        group_id=group_id
                    )
                    transfer_ids.append(tid)
                except Exception as e:
                    logger.warning("Failed to send file to %s: %s", node_id[:20], e)

            return {"status": "success", "transfer_ids": transfer_ids}
        except Exception as e:
            logger.error("Error sending group file: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def send_group_image(
        self, group_id: str, image_base64: str, filename: str = None, text: str = ""
    ) -> Dict[str, Any]:
        """Send screenshot/image to all group members via fan-out file transfer.

        Decodes and saves the image once, then sends to each connected member.

        Args:
            group_id: Target group ID
            image_base64: Base64 data URL (data:image/png;base64,...)
            filename: Optional filename
            text: Optional text caption
        """
        from datetime import datetime, timezone
        import base64 as b64
        import re
        from pathlib import Path
        from PIL import Image

        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            # Parse and decode image (once)
            if not image_base64.startswith("data:image/"):
                return {"status": "error", "message": "Invalid data URL format"}

            match = re.match(r'data:([^;]+);base64,(.+)', image_base64)
            if not match:
                return {"status": "error", "message": "Invalid data URL format"}

            mime_type = match.group(1)
            encoded_data = match.group(2)
            extension = mime_type.split("/")[1]

            try:
                image_data = b64.b64decode(encoded_data)
            except Exception as e:
                return {"status": "error", "message": f"Failed to decode base64: {e}"}

            # Validate size
            img_rules = self.firewall.rules.get("image_transfer", {})
            max_size_mb = img_rules.get("max_size_mb", 100)
            size_mb = len(image_data) / (1024 * 1024)
            if size_mb > max_size_mb:
                return {"status": "error", "message": f"Image too large ({size_mb:.2f} MB > {max_size_mb} MB)"}

            # Generate filename
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                filename = f"screenshot_{timestamp}.{extension}"

            # Save image once (use group_id in path)
            group_files_dir = DPC_HOME_DIR / "conversations" / group_id / "files" / "screenshots"
            group_files_dir.mkdir(parents=True, exist_ok=True)
            file_path = group_files_dir / filename
            if file_path.exists():
                stem, suffix = file_path.stem, file_path.suffix
                counter = 1
                while file_path.exists():
                    file_path = group_files_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

            with open(file_path, "wb") as f:
                f.write(image_data)

            # Validate and get dimensions
            with Image.open(file_path) as img:
                if img.format not in ["PNG", "JPEG", "GIF", "WEBP"]:
                    file_path.unlink()
                    return {"status": "error", "message": f"Unsupported format: {img.format}"}
                width, height = img.size

            # Generate thumbnail
            from .utils.image_utils import generate_thumbnail
            try:
                thumbnail_base64 = generate_thumbnail(file_path)
            except Exception:
                thumbnail_base64 = ""

            image_metadata = {
                "dimensions": {"width": width, "height": height},
                "thumbnail_base64": thumbnail_base64,
                "source": "clipboard",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "text": text
            }

            # Fan-out to connected members
            connected_peers = self.p2p_coordinator.get_connected_peers()
            transfer_ids = []

            for node_id in group.members:
                if node_id == self.p2p_manager.node_id:
                    continue
                if node_id not in connected_peers:
                    continue

                try:
                    tid = await self.file_transfer_manager.send_file(
                        node_id=node_id,
                        file_path=file_path,
                        image_metadata=image_metadata,
                        group_id=group_id
                    )
                    transfer_ids.append(tid)
                except Exception as e:
                    logger.warning("Failed to send group image to %s: %s", node_id[:20], e)

            return {
                "status": "success",
                "transfer_ids": transfer_ids,
                "file_path": str(file_path),
                "thumbnail_base64": thumbnail_base64,
                "size_bytes": len(image_data),
                "width": width,
                "height": height,
                "mime_type": mime_type
            }
        except Exception as e:
            logger.error("Error sending group image: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def send_group_voice_message(
        self, group_id: str, audio_base64: str, duration_seconds: float, mime_type: str = "audio/webm"
    ) -> Dict[str, Any]:
        """Send voice message to all group members via fan-out file transfer.

        Decodes and saves the audio once, then sends to each connected member.

        Args:
            group_id: Target group ID
            audio_base64: Base64-encoded audio data
            duration_seconds: Recording duration
            mime_type: Audio MIME type
        """
        from datetime import datetime, timezone
        import base64 as b64

        try:
            group = self.group_manager.get_group(group_id)
            if not group:
                return {"status": "error", "message": f"Group {group_id} not found"}

            # Decode audio
            try:
                audio_data = b64.b64decode(audio_base64)
            except Exception as e:
                return {"status": "error", "message": f"Failed to decode audio: {e}"}

            # Validate
            max_duration = float(self.settings.get("voice_messages", "max_duration_seconds", "300"))
            max_size_mb = int(self.settings.get("voice_messages", "max_size_mb", "10"))

            if duration_seconds > max_duration:
                return {"status": "error", "message": f"Voice too long ({duration_seconds}s > {max_duration}s)"}

            size_mb = len(audio_data) / (1024 * 1024)
            if size_mb > max_size_mb:
                return {"status": "error", "message": f"Voice too large ({size_mb:.2f} MB > {max_size_mb} MB)"}

            # Generate filename and save once
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            extension = mime_type.split("/")[-1].split(";")[0].strip()
            audio_filename = f"voice_{timestamp}.{extension}"

            group_files_dir = DPC_HOME_DIR / "conversations" / group_id / "files"
            group_files_dir.mkdir(parents=True, exist_ok=True)
            file_path = group_files_dir / audio_filename
            if file_path.exists():
                stem, suffix = file_path.stem, file_path.suffix
                counter = 1
                while file_path.exists():
                    file_path = group_files_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

            with open(file_path, "wb") as f:
                f.write(audio_data)

            voice_metadata = {
                "duration_seconds": duration_seconds,
                "sample_rate": int(self.settings.get("voice_messages", "default_sample_rate", "48000")),
                "channels": int(self.settings.get("voice_messages", "default_channels", "1")),
                "codec": self.settings.get("voice_messages", "default_codec", "opus"),
                "recorded_at": datetime.now(timezone.utc).isoformat()
            }

            # Fan-out to connected members
            connected_peers = self.p2p_coordinator.get_connected_peers()
            transfer_ids = []

            for node_id in group.members:
                if node_id == self.p2p_manager.node_id:
                    continue
                if node_id not in connected_peers:
                    continue

                try:
                    tid = await self.file_transfer_manager.send_file(
                        node_id=node_id,
                        file_path=file_path,
                        voice_metadata=voice_metadata,
                        group_id=group_id
                    )
                    transfer_ids.append(tid)
                except Exception as e:
                    logger.warning("Failed to send group voice to %s: %s", node_id[:20], e)

            return {
                "status": "success",
                "transfer_ids": transfer_ids,
                "file_path": str(file_path),
                "size_bytes": len(audio_data),
                "duration_seconds": duration_seconds,
                "voice_metadata": voice_metadata,
                "mime_type": mime_type
            }
        except Exception as e:
            logger.error("Error sending group voice: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # Telegram Bot Integration Commands (v0.14.0+)

    async def send_to_telegram(
        self,
        conversation_id: str,
        text: str,
        attachments: Optional[List[Dict]] = None,
        voice_audio_base64: Optional[str] = None,
        voice_duration_seconds: Optional[float] = None,
        voice_mime_type: Optional[str] = None,
        file_path: Optional[str] = None  # NEW: For sending files/images from UI
    ) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.send_to_telegram(
            conversation_id,
            text,
            attachments,
            voice_audio_base64,
            voice_duration_seconds,
            voice_mime_type,
            file_path,
        )

    async def link_telegram_chat(
        self,
        conversation_id: str,
        telegram_chat_id: str
    ) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.link_telegram_chat(conversation_id, telegram_chat_id)

    async def get_telegram_status(self) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.get_telegram_status()

    async def delete_telegram_conversation_link(self, conversation_id: str) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.delete_telegram_conversation_link(conversation_id)

    async def link_agent_telegram(
        self,
        agent_id: str,
        bot_token: str,
        chat_ids: List[str],
        event_filter: Optional[List[str]] = None,
        max_events_per_minute: int = 20,
        cooldown_seconds: float = 3.0,
        transcription_enabled: bool = True,
        unified_conversation: bool = False,
    ) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.link_agent_telegram(
            agent_id,
            bot_token,
            chat_ids,
            event_filter,
            max_events_per_minute,
            cooldown_seconds,
            transcription_enabled,
            unified_conversation,
        )

    async def _start_agent_telegram_bridges(self) -> None:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return
        await self.telegram_service._start_agent_telegram_bridges()

    def _get_telegram_bridge_for_conversation(self, conversation_id: str):
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return None
        return self.telegram_service._get_telegram_bridge_for_conversation(conversation_id)

    async def _restart_agent_telegram_bridge(self, agent_id: str) -> None:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return
        await self.telegram_service._restart_agent_telegram_bridge(agent_id)

    async def unlink_agent_telegram(self, agent_id: str) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.unlink_agent_telegram(agent_id)

    async def migrate_telegram_config(self) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.migrate_telegram_config()

    async def get_agent_telegram_status(self, agent_id: str) -> Dict[str, Any]:
        """Delegates to TelegramService."""
        if not self.telegram_service:
            return {"status": "error", "message": "Telegram integration not enabled"}
        return await self.telegram_service.get_agent_telegram_status(agent_id)

    async def vote_new_session(self, proposal_id: str, vote: bool) -> Dict[str, Any]:
        """Cast vote on a new session proposal.

        UI Integration: Called when user clicks Approve/Reject in NewSessionDialog.

        Args:
            proposal_id: UUID of the proposal
            vote: True for approve, False for reject

        Returns:
            Dict with status
        """
        try:
            # Get session
            session = self.session_manager.get_session(proposal_id)
            if not session:
                return {
                    "status": "error",
                    "message": "Proposal not found"
                }

            proposal = session.proposal

            # Record local vote
            await self.session_manager.record_vote(proposal_id, self.p2p_manager.node_id, vote)

            # Send VOTE_NEW_SESSION to all other participants
            message = {
                "command": "VOTE_NEW_SESSION",
                "payload": {
                    "proposal_id": proposal_id,
                    "vote": vote,
                    "voter_node_id": self.p2p_manager.node_id
                }
            }

            for node_id in proposal.participants:
                if node_id == self.p2p_manager.node_id:
                    continue

                if node_id in self.p2p_manager.peers:
                    try:
                        await self.p2p_manager.send_message_to_peer(node_id, message)
                        logger.debug("Sent VOTE_NEW_SESSION to %s", node_id[:20])
                    except Exception as e:
                        logger.error("Error sending vote to %s: %s", node_id[:20], e)

            vote_str = "approve" if vote else "reject"
            return {
                "status": "success",
                "message": f"Vote cast: {vote_str}"
            }

        except Exception as e:
            logger.error("Error voting on new session: %s", e, exc_info=True)
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
            if self.connection_orchestrator:
                # Use full 6-tier fallback hierarchy (IPv6 → IPv4 → WebRTC → hole punch → relay → gossip)
                connection = await self.connection_orchestrator.connect(node_id)
                return {
                    "status": "success",
                    "method": getattr(connection, "strategy_used", "unknown"),
                    "message": f"Connected to {node_id}"
                }
            else:
                # Fallback if orchestrator not yet initialized (early startup)
                success = await self.p2p_manager.connect_via_node_id(node_id)
                if success and node_id in self.p2p_manager.peers:
                    return {
                        "status": "success",
                        "method": "direct",
                        "message": f"Connected to {node_id}"
                    }
                return {
                    "status": "error",
                    "method": None,
                    "message": f"Failed to connect to {node_id} - peer not found"
                }

        except ConnectionFailedError as e:
            logger.error("All connection strategies failed for %s: %s", node_id, e)
            return {
                "status": "error",
                "method": None,
                "message": str(e)
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
                timestamp=datetime.now(timezone.utc).isoformat()
            )

            # Buffer the outgoing message (conversation monitor handles both directions)
            await monitor.on_message(outgoing_message)
        except Exception as e:
            logger.error("Error tracking outgoing message in conversation monitor: %s", e, exc_info=True)

    async def send_file(self, node_id: str, file_path: str, file_size_bytes: int = None):
        """
        Send a file to a peer via P2P file transfer.

        Args:
            node_id: Target peer's node ID
            file_path: Absolute path to file to send
            file_size_bytes: Optional file size (used by frontend for timeout calculation, ignored by backend)

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

    async def send_image(self, conversation_id: str, image_base64: str, filename: str, caption: str = "", provider_alias: str = None, compute_host: str = None, chat_provider: str = None):
        """
        Send an image from clipboard paste (Phase 2.3: Vision + Remote Vision).

        Handles two cases:
        - AI Chat (local_ai or ai_chat_xxx): Save image, run vision analysis (local or remote), broadcast result
        - P2P Chat: Generate thumbnail, send FILE_OFFER with image_metadata

        Args:
            conversation_id: 'local_ai' or 'ai_chat_xxx' for AI chat, or node_id for P2P chat
            image_base64: Data URL (e.g., "data:image/png;base64,...")
            filename: Suggested filename (e.g., "screenshot_1234567890.png")
            caption: Optional text caption (will be included in vision query for AI chat)
            provider_alias: Optional provider to use for vision analysis (overrides vision_provider config)
            compute_host: Optional node_id of peer to use for remote vision (Phase 2.3)
            chat_provider: Optional provider type for this chat (e.g., 'dpc_agent')

        Returns:
            Dict with status and metadata
        """
        import base64
        import tempfile
        import mimetypes
        from pathlib import Path
        from datetime import datetime, timezone
        from .utils.image_utils import generate_thumbnail, get_image_dimensions, validate_image_format

        # Parse data URL
        if not image_base64.startswith("data:"):
            raise ValueError("Invalid image data URL")

        # Extract mime type and base64 data
        header, data = image_base64.split(",", 1)
        mime_type = header.split(";")[0].split(":")[1]

        # Decode base64
        image_bytes = base64.b64decode(data)
        size_bytes = len(image_bytes)

        # Check size limit from config (vision.max_image_size_mb)
        max_size_mb = self.settings.get_vision_max_image_size_mb()
        if size_bytes > max_size_mb * 1024 * 1024:
            raise ValueError(f"Image too large ({round(size_bytes / (1024 * 1024), 2)}MB). Max: {max_size_mb}MB")

        # Save to temporary file for processing
        suffix = Path(filename).suffix or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            tmp_file.write(image_bytes)
            tmp_path = Path(tmp_file.name)

        try:
            # Validate image format
            if not validate_image_format(tmp_path):
                raise ValueError(f"Invalid image format: {filename}")

            # Extract dimensions
            dimensions = get_image_dimensions(tmp_path)

            # Check if this is a Dpc_agent conversation (chat_provider from frontend)
            # Note: agent_manager is on the DpcAgentProvider, not CoreService
            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            # Extract agent_id from conversation_id (e.g. "agent_001") for per-agent manager lookup
            _vision_agent_id = conversation_id if (conversation_id and conversation_id.startswith("agent_")) else None
            agent_manager = None

            if chat_provider == 'dpc_agent' and dpc_agent_provider:
                # Use _ensure_manager which handles both per-agent and singleton cases,
                # and avoids starting a duplicate Telegram bridge for the same token.
                logger.info("Initializing DPC Agent for vision query...")
                agent_manager = await dpc_agent_provider._ensure_manager(agent_id=_vision_agent_id)

                if agent_manager:
                    # Route to DPC Agent for vision analysis
                    logger.info(f"Agent vision analysis: {filename}")

                    response = await agent_manager.process_message(
                        message=caption or "Analyze this screenshot",
                        conversation_id=conversation_id,
                        include_context=True,
                        image_base64=data,  # Raw base64 without data URL prefix
                        image_mime=mime_type,
                        image_caption=caption,
                    )

                    # Broadcast result
                    await self.local_api.broadcast_event("ai_response_with_image", {
                        "conversation_id": conversation_id,
                        "query": caption or "Analyze this screenshot",
                        "response": response,
                        "provider": "dpc_agent",
                        "model": "agent",
                        "image_filename": filename,
                        "image_dimensions": dimensions,
                        "vision_used": True
                    })

                    return {
                        "status": "analyzed",
                        "conversation_id": conversation_id,
                        "filename": filename,
                        "dimensions": dimensions
                    }

            if conversation_id == "local_ai" or conversation_id.startswith("ai_"):
                # AI Chat: Run vision analysis (supports local_ai and ai_chat_xxx conversations)
                logger.info(f"AI chat vision analysis: {filename} ({dimensions['width']}x{dimensions['height']})")

                # Build query with caption (if provided)
                query = caption if caption else "What's in this image?"

                # Prepare image for vision API
                images = [{
                    "path": str(tmp_path),
                    "mime_type": mime_type,
                    "base64": data  # Pass base64 data directly (already encoded)
                }]

                # Run vision query (Phase 2.3: Support remote vision via compute_host)
                response_metadata = await self.inference_orchestrator.execute_inference(
                    prompt=query,
                    images=images,
                    provider=provider_alias,  # Pass UI selection (or None for auto-selection)
                    compute_host=compute_host  # Pass compute_host for remote vision
                )

                # Broadcast AI response to UI
                await self.local_api.broadcast_event("ai_response_with_image", {
                    "conversation_id": conversation_id,
                    "query": query,
                    "response": response_metadata["response"],
                    "provider": response_metadata["provider"],
                    "model": response_metadata["model"],
                    "image_filename": filename,
                    "image_dimensions": dimensions,
                    "vision_used": True
                })

                return {
                    "status": "analyzed",
                    "conversation_id": conversation_id,
                    "filename": filename,
                    "dimensions": dimensions
                }

            else:
                # P2P Chat: Send FILE_OFFER with image_metadata
                logger.info(f"P2P image transfer: {filename} to {conversation_id}")

                # Generate thumbnail
                thumbnail_base64 = generate_thumbnail(tmp_path)

                # Move file to peer-specific storage
                peer_storage = DPC_HOME_DIR / "conversations" / conversation_id / "files"
                peer_storage.mkdir(parents=True, exist_ok=True)
                final_path = peer_storage / filename

                # Handle duplicate filenames
                if final_path.exists():
                    stem = final_path.stem
                    suffix = final_path.suffix
                    counter = 1
                    while final_path.exists():
                        final_path = peer_storage / f"{stem}_{counter}{suffix}"
                        counter += 1

                tmp_path.rename(final_path)

                # Initiate file transfer with image_metadata
                transfer_id = await self.file_transfer_manager.send_file(
                    conversation_id,
                    final_path,
                    image_metadata={
                        "dimensions": dimensions,
                        "thumbnail_base64": thumbnail_base64,
                        "source": "clipboard",
                        "captured_at": datetime.now(timezone.utc).isoformat()
                    }
                )

                return {
                    "status": "sending",
                    "conversation_id": conversation_id,
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "dimensions": dimensions,
                    "size_bytes": size_bytes
                }

        finally:
            # Cleanup temp file (if not moved to peer storage)
            if tmp_path.exists():
                tmp_path.unlink()

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

    async def send_ai_query(self, prompt: str, compute_host: str = None, model: str = None, provider: str = None, conversation_id: str = None, agent_llm_provider: str = None):
        """
        Send an AI query, either to local LLM or to a remote peer for inference.

        Delegates to InferenceOrchestrator for execution.

        Args:
            prompt: The prompt to send to the AI
            compute_host: Optional node_id of peer to use for inference (None = local)
            model: Optional model name to use
            provider: Optional provider alias to use
            conversation_id: Optional conversation ID for progress tracking (DPC Agent)
            agent_llm_provider: Optional underlying LLM provider for DPC Agent (Phase 3)

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
            provider=provider,
            conversation_id=conversation_id,
            agent_llm_provider=agent_llm_provider  # Phase 3: per-agent provider selection
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

    async def _handle_inference_request(self, peer_id: str, request_id: str, prompt: str, model: str = None, provider: str = None, images: list = None):
        """
        Handle incoming remote inference request from a peer.
        Check firewall permissions, run inference, and send response.

        Args:
            peer_id: Node ID of the requesting peer
            request_id: Unique request identifier
            prompt: Text prompt for the model
            model: Optional model name
            provider: Optional provider alias
            images: Optional list of image dicts for vision queries (Phase 2: Remote Vision)
        """
        from dpc_protocol.protocol import create_remote_inference_response

        logger.debug("Handling inference request from %s (request_id: %s, images: %s)", peer_id, request_id, "yes" if images else "no")

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

            result = await self.llm_manager.query(prompt, provider_alias=provider_alias_to_use, images=images, return_metadata=True)
            logger.info("Inference completed successfully for %s", peer_id)

            # Send success response with token and model metadata
            # Use the actual model name from result if available (e.g., "claude-haiku-4.5"),
            # otherwise fall back to the original model parameter (e.g., "default")
            actual_model = result.get("model", model)

            success_response = create_remote_inference_response(
                request_id=request_id,
                response=result["response"],
                tokens_used=result.get("tokens_used"),
                prompt_tokens=result.get("prompt_tokens"),
                response_tokens=result.get("response_tokens"),
                model_max_tokens=result.get("model_max_tokens"),
                model=actual_model,
                provider=result.get("provider"),
                thinking=result.get("thinking"),
                thinking_tokens=result.get("thinking_tokens")
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

    async def _handle_transcription_request(self, peer_id: str, request_id: str, audio_base64: str, mime_type: str, model: str = None, provider: str = None, language: str = "auto", task: str = "transcribe"):
        """
        Handle incoming remote transcription request from a peer.
        Check firewall permissions, run transcription, and send response.

        Args:
            peer_id: Node ID of the requesting peer
            request_id: Unique request identifier
            audio_base64: Base64-encoded audio data
            mime_type: Audio MIME type (e.g., audio/webm)
            model: Optional model name
            provider: Optional provider alias
            language: Language code or "auto" for detection
            task: "transcribe" or "translate"
        """
        from dpc_protocol.protocol import create_remote_transcription_response
        import base64
        import tempfile
        import os

        logger.debug("Handling transcription request from %s (request_id: %s, mime_type: %s)", peer_id, request_id, mime_type)

        # Check if peer is allowed to request transcription
        if not self.firewall.can_request_transcription(peer_id, model):
            logger.warning("Access denied: %s cannot request transcription%s", peer_id, f" for model {model}" if model else "")
            error_response = create_remote_transcription_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request transcription" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending transcription error response to %s: %s", peer_id, e, exc_info=True)
            return

        # Run transcription
        temp_audio_path = None
        try:
            logger.info("Running transcription for %s (model: %s, provider: %s, language: %s, task: %s)",
                       peer_id, model or 'default', provider or 'default', language, task)

            # Decode base64 audio data and save to temporary file
            audio_data = base64.b64decode(audio_base64)

            # Determine file extension from MIME type
            ext_map = {
                "audio/webm": ".webm",
                "audio/opus": ".opus",
                "audio/ogg": ".ogg",
                "audio/wav": ".wav",
                "audio/mp3": ".mp3",
                "audio/mp4": ".mp4",
                "audio/mpeg": ".mp3"
            }
            file_ext = ext_map.get(mime_type, ".webm")

            # Create temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_audio_path = temp_file.name
                temp_file.write(audio_data)

            # Determine which provider to use
            provider_alias_to_use = provider

            if model and not provider:
                # Find provider by model name
                found_alias = self.llm_manager.find_provider_by_model(model)
                if found_alias:
                    provider_alias_to_use = found_alias
                    logger.debug("Found provider '%s' for model '%s'", found_alias, model)
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            # Get the provider instance
            provider_instance = self.llm_manager.providers.get(provider_alias_to_use)

            # Check if provider supports transcription
            if not hasattr(provider_instance, 'transcribe'):
                raise ValueError(f"Provider '{provider_alias_to_use}' does not support transcription")

            # Update provider settings for this transcription
            if language != "auto":
                provider_instance.language = language
            if task:
                provider_instance.task = task

            # Run transcription
            result = await provider_instance.transcribe(temp_audio_path)
            logger.info("Transcription completed successfully for %s", peer_id)

            # Send success response
            success_response = create_remote_transcription_response(
                request_id=request_id,
                text=result.get("text", ""),
                language=result.get("language"),
                duration_seconds=result.get("duration"),
                provider=result.get("provider") or provider_alias_to_use
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)
            logger.debug("Sent transcription result to %s", peer_id)

        except Exception as e:
            logger.error("Transcription failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_transcription_response(
                request_id=request_id,
                error=str(e)
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending transcription error response to %s: %s", peer_id, send_err, exc_info=True)

        finally:
            # Clean up temporary file
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.unlink(temp_audio_path)
                except Exception as cleanup_err:
                    logger.warning("Failed to delete temporary audio file %s: %s", temp_audio_path, cleanup_err)

    async def _handle_get_providers_request(self, peer_id: str):
        """
        Handle GET_PROVIDERS request from a peer.
        Check firewall permissions and send available providers that the peer can use.
        Supports both compute sharing (AI inference) and transcription sharing (Whisper).
        """
        from dpc_protocol.protocol import create_providers_response

        logger.debug("Handling GET_PROVIDERS request from %s", peer_id)

        # Check if peer has access to compute OR transcription resources
        has_compute_access = self.firewall.can_request_inference(peer_id)
        has_transcription_access = self.firewall.can_request_transcription(peer_id)

        if not has_compute_access and not has_transcription_access:
            logger.warning("Access denied: %s cannot access compute or transcription resources", peer_id)
            # Send empty provider list (no access)
            response = create_providers_response([])
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, response)
            except Exception as e:
                logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)
            return

        # Get all available providers
        all_providers = []
        compute_models = []
        transcription_models = []

        for alias, provider in self.llm_manager.providers.items():
            model = provider.model
            provider_type = provider.config.get("type", "unknown")

            provider_info = {
                "alias": alias,
                "model": model,
                "type": provider_type,
                "supports_vision": provider.supports_vision(),
                "supports_voice": self._provider_supports_voice(provider)  # Mark transcription providers
            }

            # Categorize by provider type
            if provider_type == "local_whisper":
                transcription_models.append(model)
            else:
                compute_models.append(model)

            all_providers.append(provider_info)

        # Filter providers based on firewall permissions
        filtered_providers = []

        for provider_info in all_providers:
            provider_type = provider_info["type"]
            model = provider_info["model"]

            # Check transcription providers
            if provider_type == "local_whisper":
                if has_transcription_access:
                    # Check if model is allowed for transcription
                    if self.firewall.can_request_transcription(peer_id, model):
                        filtered_providers.append(provider_info)
                        logger.debug("Including transcription provider '%s' for %s", provider_info["alias"], peer_id[:20])
            # Check compute providers (text/vision models)
            else:
                if has_compute_access:
                    # Check if model is allowed for compute
                    if self.firewall.can_request_inference(peer_id, model):
                        filtered_providers.append(provider_info)
                        logger.debug("Including compute provider '%s' for %s", provider_info["alias"], peer_id[:20])

        logger.debug("Sending %d providers to %s (filtered from %d total, compute_access=%s, transcription_access=%s)",
                    len(filtered_providers), peer_id[:20], len(all_providers), has_compute_access, has_transcription_access)

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
        Also resolves any pending query_remote_providers request.
        """
        logger.debug("Received %d providers from %s", len(providers), peer_id)

        # Update peer metadata with providers
        if peer_id not in self.peer_metadata:
            self.peer_metadata[peer_id] = {}

        self.peer_metadata[peer_id]["providers"] = providers

        # Resolve pending request if any (for on-demand provider queries)
        pending_future = self._pending_providers_requests.pop(peer_id, None)
        if pending_future and not pending_future.done():
            pending_future.set_result(providers)
            logger.debug("Resolved pending providers request for %s", peer_id)

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
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_proposal_received_from_peer(proposal)

    def _get_agent_telegram_bridge(self, conversation_id: str):
        """Delegated to KnowledgeService."""
        return self.knowledge_service._get_agent_telegram_bridge(conversation_id)

    async def _on_vote_received(self, vote) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_vote_received(vote)

    async def _on_commit_approved(self, commit) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_approved(commit)

    async def _on_commit_rejected(self, proposal, votes: Dict[str, Any]) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_rejected(proposal, votes)

    async def _on_commit_applied(self, commit):
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_applied(commit)

    async def _on_commit_revision_needed(self, proposal, votes: Dict[str, Any]) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_revision_needed(proposal, votes)

    async def _agent_auto_revise_proposal(self, agent_mgr, proposal, change_requests: list) -> None:
        """Delegated to KnowledgeService."""
        await self.knowledge_service._agent_auto_revise_proposal(agent_mgr, proposal, change_requests)

    async def _on_commit_signed(self, commit):
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_signed(commit)

    async def _on_commit_ack(self, commit):
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_ack(commit)

    async def _on_commit_apply_failed(self, commit, error_msg: str):
        """Delegated to KnowledgeService."""
        await self.knowledge_service._on_commit_apply_failed(commit, error_msg)

    async def _broadcast_context_updated_to_peers(self, context_hash: str):
        """Delegated to KnowledgeService."""
        await self.knowledge_service._broadcast_context_updated_to_peers(context_hash)

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
        """Delegated to KnowledgeService."""
        await self.knowledge_service._broadcast_commit_result(result_payload, participants)

    async def _on_session_proposal_received(self, payload: dict):
        """Callback when new session proposal received from peer.

        Broadcasts proposal to UI so user can review and vote.

        Args:
            payload: The new session proposal payload from peer
        """
        proposal_id = payload.get("proposal_id")
        initiator_id = payload.get("initiator_node_id")

        logger.info(
            "Broadcasting new session proposal to UI: %s from %s",
            proposal_id[:8] if proposal_id else "none",
            initiator_id[:20] if initiator_id else "none"
        )

        await self.local_api.broadcast_event(
            "new_session_proposed",
            payload
        )

    async def _broadcast_session_result(self, result_payload: dict, participants: List[str]):
        """Broadcast NEW_SESSION_RESULT to all participants.

        Args:
            result_payload: Complete voting result data (result, clear_history, vote_tally, etc.)
            participants: List of participant node_ids who should receive the notification
        """
        message = {
            "command": "NEW_SESSION_RESULT",
            "payload": result_payload
        }

        # Send to all participants except self (who are currently connected)
        for node_id in participants:
            if node_id == self.p2p_manager.node_id:
                continue  # Don't send to self

            if node_id in self.p2p_manager.peers:
                try:
                    await self.p2p_manager.send_message_to_peer(node_id, message)
                    logger.info("Sent NEW_SESSION_RESULT to %s", node_id[:20])
                except Exception as e:
                    logger.error("Failed to send session result to %s: %s", node_id[:20], e, exc_info=True)
            else:
                logger.debug("Participant %s not connected, skipping result broadcast", node_id[:20])

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

    async def search_peer_skills(
        self,
        peer_id: str,
        tags: Optional[List[str]] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search a connected peer's shareable skill catalog.

        WebSocket command: {"command": "search_peer_skills", "peer_id": "...", "tags": [...], "query": "..."}

        Returns {"status": "success", "skills": [...]} or {"status": "error", "message": "..."}
        """
        try:
            skills = await self.skill_coordinator.search_peer_skills(
                peer_id, tags=tags, query=query
            )
            return {"status": "success", "peer_id": peer_id, "skills": skills}
        except Exception as e:
            logger.error("search_peer_skills error: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def request_skill_from_peer(
        self,
        peer_id: str,
        skill_name: str,
    ) -> Dict[str, Any]:
        """
        Request and save a specific skill from a connected peer.

        WebSocket command: {"command": "request_skill_from_peer", "peer_id": "...", "skill_name": "..."}

        Firewall `accept_peer_skills` must be True.
        Returns {"status": "success"|"error", "message": "..."}
        """
        try:
            content = await self.skill_coordinator.request_skill_from_peer(peer_id, skill_name)
            if content is None:
                return {
                    "status": "error",
                    "message": f"Could not retrieve skill '{skill_name}' from {peer_id} (timeout, not found, or firewall blocked)",
                }
            status_msg = await self.skill_coordinator.save_received_skill(
                peer_id, skill_name, content
            )
            success = status_msg.startswith("✓")
            return {
                "status": "success" if success else "error",
                "message": status_msg,
            }
        except Exception as e:
            logger.error("request_skill_from_peer error: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _request_inference_from_peer(self, peer_id: str, prompt: str, model: str = None, provider: str = None, images: list = None, timeout: float = 240.0) -> str:
        """
        Request remote inference from a specific peer.
        Uses async request-response pattern with Future.

        Args:
            peer_id: The node_id of the peer to request inference from
            prompt: The prompt to send for inference
            model: Optional model name to use
            provider: Optional provider alias to use
            images: Optional list of image dicts for vision queries (Phase 2: Remote Vision)
            timeout: Timeout in seconds (default 240s for inference)

        Returns:
            The inference result as a string

        Raises:
            ConnectionError: If peer is not connected
            TimeoutError: If request times out
            RuntimeError: If inference fails on remote peer
        """
        from dpc_protocol.protocol import create_remote_inference_request

        logger.debug("Requesting inference from peer: %s (images: %s)", peer_id, 'yes' if images else 'no')

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_inference_requests[request_id] = response_future

            # Create inference request message (Phase 2: includes images)
            request_message = create_remote_inference_request(
                request_id=request_id,
                prompt=prompt,
                model=model,
                provider=provider,
                images=images
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

    async def _request_transcription_from_peer(
        self, peer_id: str, audio_base64: str, mime_type: str,
        model: str = None, provider: str = None, language: str = "auto",
        task: str = "transcribe", timeout: float = 120.0
    ) -> Dict[str, Any]:
        """
        Request remote transcription from a specific peer.
        Uses async request-response pattern with Future.

        Args:
            peer_id: The node_id of the peer to request transcription from
            audio_base64: Base64-encoded audio data
            mime_type: Audio MIME type (e.g., audio/webm)
            model: Optional model name to use
            provider: Optional provider alias to use
            language: Language code or "auto" for detection
            task: "transcribe" (default) or "translate" (to English)
            timeout: Timeout in seconds (default 120s for transcription)

        Returns:
            Dict containing transcription result with keys:
                - text: Transcribed text
                - language: Detected language
                - duration_seconds: Audio duration
                - provider: Provider used

        Raises:
            ConnectionError: If peer is not connected
            TimeoutError: If request times out
            RuntimeError: If transcription fails on remote peer
        """
        from dpc_protocol.protocol import create_remote_transcription_request

        logger.debug("Requesting transcription from peer: %s (mime_type: %s, language: %s)",
                    peer_id, mime_type, language)

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_transcription_requests[request_id] = response_future

            # Create transcription request message
            request_message = create_remote_transcription_request(
                request_id=request_id,
                audio_base64=audio_base64,
                mime_type=mime_type,
                model=model,
                provider=provider,
                language=language,
                task=task
            )

            # Send request
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            # Wait for response with timeout
            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("Received transcription result from %s: %d chars",
                           peer_id, len(result.get("text", "")))
                return result

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for transcription from %s", peer_id)
                raise TimeoutError(f"Transcription request to {peer_id} timed out after {timeout}s")
            finally:
                # Clean up pending request
                self._pending_transcription_requests.pop(request_id, None)

        except Exception as e:
            logger.error("Error requesting transcription from %s: %s", peer_id, e, exc_info=True)
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

    async def _execute_agent_query(self, command_id: str, prompt: str, conversation_id: str,
                                   include_context: bool, instruction_set_name: str,
                                   agent_llm_provider: str) -> None:
        """
        Execute an agent query (routes to DpcAgentManager).

        Agent conversations use their own prompt assembly without PromptManager formatting.
        This prevents "CONVERSATION HISTORY" and "USER QUERY" sections from appearing in chat.

        Args:
            command_id: Unique ID for this command
            prompt: The user's raw prompt (no formatting applied)
            conversation_id: Agent conversation ID (e.g., "agent_001")
            include_context: Whether to include DPC context
            instruction_set_name: Instruction set to use
            agent_llm_provider: Underlying LLM provider for the agent
        """
        try:
            # Get the dpc_agent provider
            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if not dpc_agent_provider:
                raise ValueError("DpcAgentProvider not configured")

            # Ensure agent manager exists for this conversation
            await dpc_agent_provider._ensure_manager(agent_id=conversation_id)
            agent_manager = dpc_agent_provider.get_manager(conversation_id)

            # Process message through agent (agent manager handles its own prompt assembly)
            response = await agent_manager.process_message(
                message=prompt,
                conversation_id=conversation_id,
                include_context=include_context,
                agent_llm_provider=agent_llm_provider or conversation_id,
                sender_name=self.p2p_manager.get_display_name() or "User",
            )

            # Get actual model and provider from agent's last usage.
            # process_message() may use a per-provider agent (stored in _agents[alias])
            # rather than the default self._agent, so we must resolve the same instance.
            _used_provider_alias = agent_llm_provider or conversation_id
            agent = agent_manager._get_or_create_agent_for_provider(_used_provider_alias) if _used_provider_alias else agent_manager.agent
            model_name = "dpc_agent"
            provider_name = agent_llm_provider or "dpc_agent"

            if hasattr(agent, '_last_usage') and agent._last_usage:
                # Try to get the actual model used
                if "model" in agent._last_usage:
                    model_name = agent._last_usage["model"]
                # Try to get the actual provider used
                if "provider" in agent._last_usage:
                    provider_name = agent._last_usage["provider"]
            else:
                # Fallback: get from LLM manager
                llm_manager = getattr(self, "llm_manager", None)
                if llm_manager:
                    model_name = llm_manager.get_active_model_name()
                    # If agent_llm_provider was specified, use that; otherwise use default
                    if agent_llm_provider and agent_llm_provider in llm_manager.providers:
                        provider_name = agent_llm_provider
                    elif llm_manager.default_provider in llm_manager.providers:
                        provider_name = llm_manager.default_provider

            # Get token usage from the agent monitor (set during process_message)
            session_state = agent_manager.get_session_state(conversation_id)
            # "history_tokens" is the canonical key (renamed from "tokens_used" in agent_manager)
            tokens_used = session_state.get("history_tokens", session_state.get("tokens_used", 0))
            token_limit = session_state.get("tokens_limit", 128000)

            # Token warning — mirror the same threshold check as local AI chat
            monitor = agent_manager._agent_monitors.get(conversation_id)
            if monitor and monitor.should_suggest_extraction():
                usage_percent = monitor.current_token_count / token_limit if token_limit > 0 else 0.0
                await self.local_api.broadcast_event("token_limit_warning", {
                    "conversation_id": conversation_id,
                    "tokens_used": monitor.current_token_count,
                    "token_limit": token_limit,
                    "usage_percent": usage_percent,
                    "history_tokens": session_state.get("history_tokens", 0),
                    "context_estimated": session_state.get("context_estimated", 0),
                })
                logger.warning("Token Warning - %s: %.1f%% of context window used (%d/%d tokens)",
                               conversation_id, usage_percent * 100,
                               monitor.current_token_count, token_limit)

            # Get thinking/reasoning from last LLM round if available
            thinking_text = None
            thinking_tokens = None
            if hasattr(agent, '_last_usage') and agent._last_usage:
                thinking_text = agent._last_usage.get("thinking")
                if thinking_text:
                    thinking_tokens = len(thinking_text) // 4  # rough token estimate

            # Send response to UI with actual metadata including token info
            await self.local_api.send_response_to_all(
                command_id=command_id,
                command="execute_ai_query",
                status="OK",
                payload={
                    "content": response,
                    "provider": provider_name,
                    "model": model_name,
                    "compute_host": "local",
                    "tokens_used": tokens_used,
                    "token_limit": token_limit,
                    "history_tokens": session_state.get("history_tokens", 0),
                    "context_estimated": session_state.get("context_estimated", 0),
                    "thinking": thinking_text,
                    "thinking_tokens": thinking_tokens,
                }
            )

            logger.info("Agent query completed for conversation %s", conversation_id)

        except Exception as e:
            logger.error("Agent query failed for %s: %s", conversation_id, e, exc_info=True)
            await self.local_api.send_response_to_all(
                command_id=command_id,
                command="execute_ai_query",
                status="ERROR",
                payload={"message": str(e)}
            )

    async def execute_ai_query(self, command_id: str, prompt: str, context_ids: list = None, compute_host: str = None, model: str = None, provider: str = None, include_context: bool = True, ai_scope: str = None, instruction_set_name: str = None, agent_llm_provider: str = None, **kwargs):
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
            instruction_set_name: Optional instruction set key to use for this query (None = use conversation's default or global default)
            agent_llm_provider: Optional underlying LLM provider for DPC Agent (Phase 3: per-agent provider selection)
            **kwargs: Additional arguments (including conversation_id)
        """
        logger.info("Orchestrating AI query for command_id %s: '%s...'", command_id, prompt[:50])
        logger.debug("Compute host: %s", compute_host or 'local')
        logger.debug("Model: %s", model or 'default')
        logger.debug("Include context: %s", include_context)
        logger.debug("AI Scope: %s", ai_scope or 'None (full access)')
        logger.debug("Instruction Set: %s", instruction_set_name or 'default')

        # Phase 7: Get or create conversation monitor early for history tracking
        conversation_id = kwargs.get("conversation_id", "local_ai")

        # Check if this is an agent conversation - route directly to agent manager
        # Agent conversations use their own prompt assembly, not PromptManager
        is_agent_conversation = (
            provider == 'dpc_agent' or
            conversation_id.startswith('agent_')
        )

        if is_agent_conversation:
            logger.debug("Routing to agent manager for conversation %s", conversation_id)
            return await self._execute_agent_query(
                command_id=command_id,
                prompt=prompt,
                conversation_id=conversation_id,
                include_context=include_context,
                instruction_set_name=instruction_set_name,
                agent_llm_provider=agent_llm_provider
            )

        monitor = self._get_or_create_conversation_monitor(conversation_id, instruction_set_name)

        # Phase 7: Check if context window is full (hard limit enforcement)
        if monitor.token_limit > 0:
            usage_percent = monitor.current_token_count / monitor.token_limit
            if usage_percent >= 1.0:
                error_msg = (
                    f"Context window is full ({monitor.current_token_count}/{monitor.token_limit} tokens). "
                    "Please end the session to save knowledge and start a new conversation."
                )
                logger.warning("BLOCKED: %s", error_msg)
                # Send error response immediately instead of raising exception
                await self.local_api.send_response_to_all(
                    command_id=command_id,
                    command="execute_ai_query",
                    status="ERROR",
                    payload={"message": error_msg}
                )
                return

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
                # Apply AI Scope filtering to device context if specified
                if ai_scope:
                    try:
                        device_context_data = self.firewall.filter_device_context_for_ai_scope(
                            device_context_data, ai_scope
                        )
                        logger.debug("AI Scope filtering applied to device context")
                    except Exception as e:
                        logger.error("Error applying AI Scope filtering to device context: %s", e, exc_info=True)
                        # Fall back to unfiltered device context if filtering fails
                        logger.warning("Falling back to unfiltered device context due to filtering error")
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
                            # Phase 7: ALWAYS add peer context when checkbox checked (matching local context behavior)
                            # This ensures contexts are included in EVERY message, not just when they change
                            aggregated_contexts[node_id] = context

                            # Compute peer context hash for change detection (for UI "Updated" badges)
                            peer_hash = self._compute_peer_context_hash(context)
                            is_first_fetch = node_id not in monitor.peer_context_hashes

                            # Track hash changes for UI badge updates
                            if monitor.has_peer_context_changed(node_id, peer_hash):
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
                            else:
                                # Context unchanged, but still included in prompt
                                logger.info("Context from %s... (included - unchanged)", node_id[:20])

                            # Phase 7: Fetch device context if we don't have it cached
                            if not device_ctx:
                                device_ctx = await self._request_device_context_from_peer(node_id)
                                if device_ctx:
                                    logger.info("Received device context from %s...", node_id[:20])

                            # Cache both personal and device contexts (for next time)
                            monitor.cache_peer_context(node_id, context, device_ctx)

                            # Add device context to result if we have it
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
        # Phase 7: Include context blocks if local checkbox OR any peer checkboxes are checked
        include_full_context = include_context or bool(aggregated_contexts)
        # Use instruction set from conversation monitor (which has the per-conversation instruction set)
        effective_instruction_set = instruction_set_name or monitor.instruction_set_name
        final_prompt = self.prompt_manager.assemble_prompt(
            query=prompt,
            contexts=aggregated_contexts,
            device_context=device_context_data,
            peer_device_contexts=peer_device_contexts,
            message_history=message_history,
            include_context=include_full_context,
            instruction_set_name=effective_instruction_set
        )

        # Phase 2: Pre-query validation - Count tokens BEFORE sending to LLM
        # REFACTORED (Phase 4 - v0.12.1): Uses TokenCountManager for validation
        if monitor.token_limit > 0:
            model_name = model or self.llm_manager.get_active_model_name()

            # Validate prompt fits in context window (with 20% response buffer)
            is_valid, error_msg = self.llm_manager.token_count_manager.validate_prompt(
                prompt=final_prompt,
                model=model_name,
                context_window=monitor.token_limit,
                buffer_percent=0.2
            )

            if not is_valid:
                logger.warning("BLOCKED (pre-query validation): %s", error_msg)
                # Send error response immediately instead of raising exception
                await self.local_api.send_response_to_all(
                    command_id=command_id,
                    command="execute_ai_query",
                    status="ERROR",
                    payload={"message": error_msg}
                )
                return

        response_payload = {}
        status = "OK"

        try:
            # Use send_ai_query to support both local and remote inference
            result = await self.send_ai_query(
                prompt=final_prompt,
                compute_host=compute_host,
                model=model,
                provider=provider,
                conversation_id=conversation_id,
                agent_llm_provider=agent_llm_provider  # Phase 3: per-agent provider selection
            )
            # result is a dict with 'response', 'model', 'provider', 'compute_host'
            # and potentially 'tokens_used', 'model_max_tokens' for local inference
            # and 'thinking', 'thinking_tokens' for reasoning models (v1.4+)
            response_payload = {
                "content": result["response"],
                "model": result["model"],
                "provider": result["provider"],
                "compute_host": result["compute_host"],
                # Include thinking fields if available (v1.4+)
                "thinking": result.get("thinking"),
                "thinking_tokens": result.get("thinking_tokens"),
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
                # IMPORTANT: Use set_token_count() with prompt_tokens, NOT update_token_count() with tokens_used!
                # prompt_tokens already includes the full conversation history, so we REPLACE the count (no double-counting)
                # Using update_token_count(tokens_used) would add prompt+response tokens cumulatively, causing exponential growth
                monitor.set_token_count(result["prompt_tokens"])

                # Compute breakdown for three-metric token display:
                # - context_estimated = full assembled prompt (system + contexts + history)
                # - history_tokens = rough estimate of conversation text only (chars ÷ 4)
                _context_estimated = result["prompt_tokens"]
                _history_tokens = sum(
                    len(msg.get("content", "")) for msg in message_history
                ) // 4

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
                        "usage_percent": current_usage_percent,
                        "history_tokens": _history_tokens,
                        "context_estimated": _context_estimated,
                    })
                    logger.warning("Token Warning - %s: %.1f%% of context window used (%d/%d tokens)",
                                 conversation_id, current_usage_percent * 100,
                                 monitor.current_token_count, current_limit)

                # Include token info in response (send cumulative conversation tokens, not per-query)
                response_payload["tokens_used"] = monitor.current_token_count  # Cumulative conversation tokens
                response_payload["token_limit"] = result.get("model_max_tokens", 0)
                response_payload["this_query_tokens"] = result["tokens_used"]  # Per-query tokens (for debugging)
                response_payload["history_tokens"] = _history_tokens
                response_payload["context_estimated"] = _context_estimated

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
                    timestamp=datetime.now(timezone.utc).isoformat()
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
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                proposal = await monitor.on_message(ai_message)

                logger.debug("Monitor - buffer size after: %d, Score: %.2f",
                           len(monitor.message_buffer), monitor.knowledge_score)

                # Only handle automatic proposals if auto-detection is enabled
                if self.knowledge_service.auto_knowledge_detection_enabled:
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
                if self.knowledge_service.auto_knowledge_detection_enabled:
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

    # --- DPC Agent Management Methods ---

    async def create_agent(
        self,
        name: str,
        provider_alias: str = "dpc_agent",
        profile_name: str = "",  # Empty = use agent_id as profile name
        instruction_set_name: str = "general",
        budget_usd: float = 50.0,
        max_rounds: int = 200,
        compute_host: str = "",  # Optional remote peer node_id for LLM inference
    ) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.create_agent(
            name=name,
            provider_alias=provider_alias,
            profile_name=profile_name,
            instruction_set_name=instruction_set_name,
            budget_usd=budget_usd,
            max_rounds=max_rounds,
            compute_host=compute_host,
        )

    async def list_agents(self) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.list_agents()

    async def get_agent_config(self, agent_id: str) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.get_agent_config(agent_id)

    async def update_agent_config(
        self,
        agent_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.update_agent_config(agent_id, updates)

    async def delete_agent(self, agent_id: str) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.delete_agent(agent_id)

    async def list_agent_profiles(self) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.list_agent_profiles()

    # --- Agent Task Board Methods (v0.20.0) ---

    async def get_agent_tasks(self, agent_id: str = "agent_001") -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.get_agent_tasks(agent_id)

    async def get_agent_task_result(self, agent_id: str = "agent_001", task_id: str = "") -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.get_agent_task_result(agent_id, task_id)

    async def get_agent_learning(self, agent_id: str = "agent_001") -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.get_agent_learning(agent_id)

    async def schedule_agent_task(
        self,
        agent_id: str = "agent_001",
        task_type: str = "chat",
        data: Dict[str, Any] = None,
        priority: str = "NORMAL",
        delay_seconds: int = 0,
    ) -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.schedule_agent_task(
            agent_id=agent_id,
            task_type=task_type,
            data=data,
            priority=priority,
            delay_seconds=delay_seconds,
        )

    async def cancel_agent_task(self, agent_id: str = "agent_001", task_id: str = "") -> Dict[str, Any]:
        """Delegates to AgentService."""
        if not self.agent_service:
            return {"status": "error", "message": "Agent service not available"}
        return await self.agent_service.cancel_agent_task(agent_id, task_id)

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