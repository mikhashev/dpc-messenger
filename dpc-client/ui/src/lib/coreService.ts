// dpc-client/ui/src/lib/coreService.ts
// PRODUCTION VERSION - Clean, no excessive logging

import { get } from 'svelte/store';
import { setLogSender, clearLogSender } from '$lib/logger';

// Import and re-export all shared interfaces from types.ts for backward compatibility
import type {
    ProviderInfo,
    DefaultProvidersResponse,
    ProvidersListResponse,
    AgentInfo,
    AgentConfig,
    FileTransfer,
    GroupChat,
    NodeStatus,
    PeerInfo,
    P2PMessage,
    MessageAttachment,
    KnowledgeCommit,
    KnowledgeCommitProposal,
    KnowledgeEntry,
    VoteTally,
    VoiceTranscription,
    // Event payload types (v0.22+)
    AgentProgressEvent,
    AgentProgressClearEvent,
    AgentTextChunkEvent,
    AgentTelegramLinkedEvent,
    AgentTelegramUnlinkedEvent,
    AgentHistoryUpdatedEvent,
    FileTransferOfferEvent,
    FileTransferProgressEvent,
    FileTransferCompleteEvent,
    FileTransferCancelledEvent,
    FilePreparationStartedEvent,
    FilePreparationProgressEvent,
    FilePreparationCompletedEvent,
    WhisperModelEvent,
    WhisperModelFailedEvent,
    GroupMessageEvent,
    GroupFileEvent,
    GroupMemberLeftEvent,
    GroupDeletedEvent,
    GroupHistorySyncedEvent,
    TelegramStatusEvent,
    TelegramMessageEvent,
    TelegramVoiceEvent,
    TelegramImageEvent,
    TelegramFileEvent,
    HistoryRestoredEvent,
    NewSessionProposalEvent,
    NewSessionResultEvent,
    ConversationEvent,
    ConversationSettingsChangedEvent,
    ContextUpdatedEvent,
    TokenWarningEvent,
    ExtractionFailureEvent,
    KnowledgeCommitResultEvent,
    AIResponseWithImageEvent,
} from '$lib/types';

export type {
    ProviderInfo,
    DefaultProvidersResponse,
    ProvidersListResponse,
    AgentInfo,
    AgentConfig,
    FileTransfer,
    GroupChat,
    NodeStatus,
    PeerInfo,
    P2PMessage,
    MessageAttachment,
    KnowledgeCommit,
    KnowledgeCommitProposal,
    KnowledgeEntry,
    VoteTally,
    VoiceTranscription,
    // Event payload types (v0.22+)
    AgentProgressEvent,
    AgentProgressClearEvent,
    AgentTextChunkEvent,
    AgentTelegramLinkedEvent,
    AgentTelegramUnlinkedEvent,
    AgentHistoryUpdatedEvent,
    FileTransferOfferEvent,
    FileTransferProgressEvent,
    FileTransferCompleteEvent,
    FileTransferCancelledEvent,
    FilePreparationStartedEvent,
    FilePreparationProgressEvent,
    FilePreparationCompletedEvent,
    WhisperModelEvent,
    WhisperModelFailedEvent,
    GroupMessageEvent,
    GroupFileEvent,
    GroupMemberLeftEvent,
    GroupDeletedEvent,
    GroupHistorySyncedEvent,
    TelegramStatusEvent,
    TelegramMessageEvent,
    TelegramVoiceEvent,
    TelegramImageEvent,
    TelegramFileEvent,
    HistoryRestoredEvent,
    NewSessionProposalEvent,
    NewSessionResultEvent,
    ConversationEvent,
    ConversationSettingsChangedEvent,
    ContextUpdatedEvent,
    TokenWarningEvent,
    ExtractionFailureEvent,
    KnowledgeCommitResultEvent,
    AIResponseWithImageEvent,
};

// --- Service store imports (Step 2c: stores now live in services/) ---
// Imported here for use by the event handler and command functions; re-exported below for
// backward compat (8 components + +page.svelte import from '$lib/coreService').

import { connectionStatus, nodeStatus, coreMessages } from './services/connection';
import { p2pMessages, unreadMessageCounts } from './services/messaging';
import { availableProviders, defaultProviders, providersList, peerProviders, aiResponseWithImage, firewallRulesUpdated } from './services/providers';
import { fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, filePreparationStarted, filePreparationProgress, filePreparationCompleted } from './services/fileTransfer';
import { voiceOfferReceived, voiceTranscriptionReceived, voiceTranscriptionComplete, voiceTranscriptionConfig, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, whisperModelUnloaded, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed } from './services/voice';
import { groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced, groupMessageDeleted, tokenUsageUpdated } from './services/groups';
import { agentsList, agentCreated, agentUpdated, agentDeleted, agentProfiles, agentProgress, agentProgressClear, agentTextChunk, agentChatMessage, sleepStateChanged, sleepProgress, sleepAgentStates } from './services/agents';
import { telegramEnabled, telegramConnected, telegramStatus, telegramError, telegramLinkedChats, telegramMessages, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated } from './services/telegram';
import { personalContext, contextUpdated, peerContextUpdated, knowledgeCommitProposal, knowledgeCommitResult, extractionFailure, tokenWarning, integrityWarnings } from './services/knowledge';
import { historyRestored, newSessionProposal, newSessionResult, conversationReset, conversationSettings, conversationSettingsChanged, conversationDeleted } from './services/session';
import { webAuthPopupRequest } from './services/webAuth';

// Re-export all service stores for backward compatibility.
// NOTE: When adding new UI-reactive fields from privacy_rules.json, add the store in
// services/providers.ts and re-export it here. See CLAUDE.md "UI Integration Pattern".
export { connectionStatus, nodeStatus, coreMessages };
export { p2pMessages, unreadMessageCounts };
export { availableProviders, defaultProviders, providersList, peerProviders, aiResponseWithImage, firewallRulesUpdated };
export { fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, filePreparationStarted, filePreparationProgress, filePreparationCompleted };
export { voiceOfferReceived, voiceTranscriptionReceived, voiceTranscriptionComplete, voiceTranscriptionConfig, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, whisperModelUnloaded, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed };
export { groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced, groupMessageDeleted, tokenUsageUpdated };
export { agentsList, agentCreated, agentUpdated, agentDeleted, agentProfiles, agentProgress, agentProgressClear, agentTextChunk, agentChatMessage, sleepStateChanged, sleepProgress, sleepAgentStates };
export { telegramEnabled, telegramConnected, telegramStatus, telegramError, telegramLinkedChats, telegramMessages, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated };
export { personalContext, contextUpdated, peerContextUpdated, knowledgeCommitProposal, knowledgeCommitResult, extractionFailure, tokenWarning, integrityWarnings };
export { historyRestored, newSessionProposal, newSessionResult, conversationReset, conversationSettings, conversationSettingsChanged, conversationDeleted };
export { webAuthPopupRequest };

// Track currently active chat to prevent unread badges on open chats
let activeChat: string | null = null;

let socket: WebSocket | null = null;
let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
let reconnectAttempts = 0;
let pollingInterval: ReturnType<typeof setInterval> | null = null;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY = 3000;
const API_URL = "ws://127.0.0.1:9999";

// Map to track pending command responses
const pendingCommands = new Map<string, { resolve: (value: any) => void; reject: (reason: any) => void; resetTimeout?: () => void }>();

function startPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
    }

    pollingInterval = setInterval(() => {
        if (!socket) {
            if (pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
            }
            return;
        }

        // Connection opened: stop polling and let the 'open' event handler
        // do the auth handshake + initial commands. We must NOT sendCommand
        // from here — that would race the auth message and the backend would
        // close us with 1008 (auth required) before we sent the token.
        if (socket.readyState === WebSocket.OPEN) {
            if (pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
            }
        }

        // Check if connection closed
        if (socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
            console.log("❌ WebSocket connection closed");
            
            if (pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
            }

            const currentStatus = get(connectionStatus);
            if (currentStatus !== 'error') {
                connectionStatus.set('disconnected');
            }
            
            nodeStatus.set(null);
            socket = null;

            // Attempt reconnection
            if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS && currentStatus !== 'error') {
                reconnectAttempts++;
                console.log(`Reconnecting... (Attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
                
                reconnectTimeout = setTimeout(() => {
                    connectToCoreService();
                }, RECONNECT_DELAY);
            } else if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                console.error(`Failed to reconnect after ${MAX_RECONNECT_ATTEMPTS} attempts`);
                connectionStatus.set('error');
            }
        }
    }, 200); // Poll every 200ms
}

export async function connectToCoreService() {
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
        return;
    }

    if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
    }

    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }

    connectionStatus.set('connecting');
    console.log(`Connecting to Core Service at ${API_URL}...`);

    // Read the local API auth token BEFORE opening the socket. The backend
    // writes a fresh random token to ~/.dpc/.ws_token on every startup; we
    // present it as the first WebSocket message. We read it up-front (rather
    // than inside the open handler) because any await between socket creation
    // and the auth send opens a race window where polling or the close handler
    // can null `socket` out from under us.
    let authToken: string;
    try {
        const { invoke } = await import('@tauri-apps/api/core');
        authToken = await invoke<string>('get_ws_token');
    } catch (e) {
        console.error("❌ Failed to read WS auth token:", e);
        connectionStatus.set('error');
        return;
    }

    try {
        socket = new WebSocket(API_URL);

        // Start polling for state changes
        startPolling();

        // Set up event listeners (belt and suspenders)
        socket.addEventListener('open', () => {
            console.log("✅ WebSocket opened via event");

            // Send auth as the FIRST message — synchronously, no awaits.
            // Without this the backend closes us with code 1008 after a 5s timeout.
            // See local_api.py:_authenticate.
            socket!.send(JSON.stringify({ command: 'auth', token: authToken, id: 'auth-init' }));

            connectionStatus.set('connected');
            reconnectAttempts = 0;

            // Wire the frontend logger to relay messages to ui.log via the backend.
            setLogSender((level, context, message) => {
                sendCommand('ui_log', { level, context, message });
            });

            sendCommand("get_status");
            sendCommand("list_providers");
            sendCommand("get_default_providers");  // Fetch default text/vision providers
            sendCommand("get_providers_list");     // Fetch full provider list with vision flags
            sendCommand("get_telegram_status");    // Fetch Telegram status including conversation links
            loadGroups();                             // Fetch group chats (v0.19.0)

            // Stop polling
            if (pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
            }
        });

        socket.addEventListener('message', async (event) => {
            try {
                const message = JSON.parse(event.data);
                coreMessages.set(message);

                // Check if this is a response to a pending command
                if (message.id && pendingCommands.has(message.id)) {
                    const { resolve, reject } = pendingCommands.get(message.id)!;
                    pendingCommands.delete(message.id);

                    // Check for error responses
                    if (message.status === "ERROR") {
                        reject(new Error(message.payload?.message || "Command failed"));
                    } else {
                        resolve(message.payload);
                    }
                }

                if (message.event === "status_update" ||
                    (message.id && message.command === "get_status" && message.status === "OK")) {
                    if (message.event === "status_update" && message.payload.peer_info) {
                        console.log('[StatusUpdate] Received status_update with peer_info:', message.payload.peer_info);
                    }
                    nodeStatus.set({ ...message.payload });
                }
                // Handle get_telegram_status response to populate conversation links
                else if (message.id && message.command === "get_telegram_status" && message.status === "OK") {
                    const status = message.payload;
                    console.log("Telegram status loaded:", status);
                    if (status.enabled && status.conversation_links) {
                        // Populate telegramLinkedChats store with conversation_id -> telegram_chat_id mappings
                        telegramLinkedChats.set(new Map(Object.entries(status.conversation_links)));
                        console.log(`[Telegram] Loaded ${Object.keys(status.conversation_links).length} conversation links from backend`);
                    }
                } else if (message.event === "new_p2p_message") {
                    p2pMessages.set(message.payload);

                    // Track unread messages (v0.9.3) - only if this message is not from active chat
                    // Message payload contains: sender_node_id, sender_name, text, message_id
                    const senderId = message.payload.sender_node_id;
                    if (senderId && typeof window !== 'undefined') {
                        // Only increment unread count if this chat is NOT currently active
                        if (senderId !== activeChat) {
                            // Get current unread counts
                            const currentCounts = get(unreadMessageCounts);
                            const currentCount = currentCounts.get(senderId) || 0;
                            // Increment count
                            currentCounts.set(senderId, currentCount + 1);
                            unreadMessageCounts.set(new Map(currentCounts));
                        }
                    }
                } else if (message.event === "connection_status_changed") {
                    // Update node status with new connection status
                    const currentStatus = get(nodeStatus);
                    if (currentStatus) {
                        nodeStatus.set({
                            ...currentStatus,
                            ...message.payload.status
                        });
                    }
                    console.log(`Connection mode changed: ${message.payload.status.mode} - ${message.payload.status.message}`);
                }
                // Knowledge Architecture event handlers
                else if (message.event === "knowledge_commit_proposed") {
                    console.log("Knowledge commit proposal received:", message.payload);
                    knowledgeCommitProposal.set(message.payload);
                } else if (message.event === "knowledge_commit_approved") {
                    console.log("Knowledge commit approved:", message.payload);
                    // Refresh personal context after approval
                    sendCommand("get_personal_context");
                } else if (message.event === "knowledge_commit_result") {
                    console.log("Knowledge commit result received:", message.payload);
                    knowledgeCommitResult.set(message.payload);
                }
                // New session proposal handlers (v0.11.3)
                else if (message.event === "new_session_proposed") {
                    console.log("New session proposal received:", message.payload);
                    newSessionProposal.set(message.payload);
                } else if (message.event === "new_session_result") {
                    console.log("New session result received:", message.payload);
                    newSessionResult.set(message.payload);

                    // Clear proposal after result
                    newSessionProposal.set(null);

                    // Show toast notification
                    const result = message.payload.result;
                    if (result === "approved") {
                        console.log("✅ New session approved - conversation history cleared");
                    } else if (result === "rejected") {
                        console.log("❌ New session rejected");
                    } else if (result === "timeout") {
                        console.log("⏱️ New session request timed out");
                    }
                }
                // Conversation reset handler (v0.11.3 - for AI chats and approved P2P resets)
                else if (message.event === "conversation_reset") {
                    console.log("Conversation reset received:", message.payload);
                    conversationReset.set(message.payload);
                }
                // Conversation settings changed handler (v0.21.0 - persistence toggle)
                else if (message.event === "conversation_settings_changed") {
                    console.log("Conversation settings changed:", message.payload);
                    conversationSettingsChanged.set(message.payload);
                }
                // Conversation deleted handler (v0.21.0 - full conversation deletion)
                else if (message.event === "conversation_deleted") {
                    console.log("Conversation deleted:", message.payload);
                    conversationDeleted.set(message.payload);
                }
                // Handle token usage update (group chat counter)
                else if (message.event === "token_usage_updated") {
                    tokenUsageUpdated.set(message.payload);
                }
                // Handle token limit warning (Phase 2)
                else if (message.event === "token_limit_warning") {
                    console.log("Token limit warning:", message.payload);
                    tokenWarning.set(message.payload);
                }
                // Handle knowledge extraction failure (Phase 4)
                else if (message.event === "knowledge_extraction_failed") {
                    console.error("Knowledge extraction failed:", message.payload);
                    extractionFailure.set(message.payload);
                }
                // Phase 7: Handle personal context update (for status indicators)
                else if (message.event === "personal_context_updated") {
                    console.log("Personal context updated:", message.payload);
                    contextUpdated.set(message.payload);
                }
                // Phase 7: Handle peer context update (for status indicators)
                else if (message.event === "peer_context_updated") {
                    console.log("Peer context updated:", message.payload);
                    peerContextUpdated.set(message.payload);
                }
                // Knowledge integrity warnings (v0.19.2 - startup tamper/corruption detection)
                else if (message.event === "integrity_warnings") {
                    console.warn("Knowledge integrity issues detected:", message.payload);
                    integrityWarnings.set({ ...message.payload, dismissed: false });
                }
                // Handle get_personal_context response
                else if (message.command === "get_personal_context" && message.status === "OK") {
                    console.log("Personal context loaded:", message.payload);
                    if (message.payload.status === "success") {
                        personalContext.set(message.payload.context);
                    }
                }
                // Handle list_providers response (legacy)
                else if (message.command === "list_providers" && message.status === "OK") {
                    console.log("Available providers loaded:", message.payload);
                    availableProviders.set(message.payload);
                }
                // Handle get_default_providers response
                else if (message.command === "get_default_providers" && message.status === "OK") {
                    console.log("Default providers loaded:", message.payload);
                    defaultProviders.set(message.payload);
                }
                // Handle get_providers_list response
                else if (message.command === "get_providers_list" && message.status === "OK") {
                    console.log("Providers list loaded:", message.payload);
                    providersList.set(message.payload.providers);
                    // Also update defaults in case they changed
                    defaultProviders.set({
                        default_provider: message.payload.default_provider,
                        vision_provider: message.payload.vision_provider
                    });
                }
                // Handle peer_providers_updated event
                else if (message.event === "peer_providers_updated") {
                    const nodeId = message.payload.node_id.slice(0, 16);
                    console.log(`✓ Received ${message.payload.providers.length} providers from ${nodeId}...`);
                    peerProviders.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.node_id, message.payload.providers);
                        return newMap;
                    });
                }
                // Handle providers_updated event (when user edits their own providers)
                else if (message.event === "providers_updated") {
                    console.log("Providers configuration updated, reloading provider list");
                    sendCommand("get_providers_list");     // Reload full provider list
                    sendCommand("get_default_providers");  // Reload defaults
                }
                // Handle default_providers_updated event (v0.13.0+: voice/text/vision default changed)
                else if (message.event === "default_providers_updated") {
                    console.log("Default providers updated, reloading defaults");
                    sendCommand("get_default_providers");  // Reload defaults
                }
                // Handle firewall_rules_updated event
                // NOTE: This event is triggered when user saves firewall rules via FirewallEditor.
                // It allows UI components to reload data from privacy_rules.json without page refresh.
                // Example: AI scopes dropdown reloads when ai_scopes section is modified.
                // If you add more UI-reactive fields (compute settings, node groups, etc.),
                // update the corresponding store here (see pattern in store declarations above).
                else if (message.event === "firewall_rules_updated") {
                    console.log("Firewall rules updated, triggering AI scope and provider list reload");
                    firewallRulesUpdated.set(message.payload);
                    // Also reload provider list since allowed models may have changed
                    sendCommand("get_providers_list");
                    sendCommand("get_default_providers");
                }
                // Handle AI vision response (Phase 2)
                else if (message.event === "ai_response_with_image") {
                    console.log("AI vision response received:", message.payload);
                    aiResponseWithImage.set(message.payload);
                }
                // File transfer event handlers (Week 1)
                else if (message.event === "file_transfer_offered") {
                    console.log("File transfer offer received:", message.payload);
                    fileTransferOffer.set(message.payload);
                    // Add to active transfers (receiver side)
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.transfer_id, {
                            ...message.payload,
                            status: "pending",
                            progress: 0
                        });
                        return newMap;
                    });
                }
                else if (message.event === "file_transfer_started") {
                    console.log("File transfer started:", message.payload);
                    // Add to active transfers (sender side - v0.11.1)
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.transfer_id, {
                            ...message.payload,
                            status: "transferring",
                            progress: message.payload.progress_percent || 0  // Map progress_percent to progress for UI
                        });
                        return newMap;
                    });
                }
                else if (message.event === "file_transfer_progress") {
                    fileTransferProgress.set(message.payload);
                    // Update progress in active transfers
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        const transfer = newMap.get(message.payload.transfer_id);
                        if (transfer) {
                            newMap.set(message.payload.transfer_id, {
                                ...transfer,
                                ...message.payload,
                                progress: message.payload.progress_percent  // Map progress_percent to progress for UI
                            });
                        }
                        return newMap;
                    });
                }
                else if (message.event === "file_transfer_complete") {
                    console.log("File transfer completed:", message.payload);
                    fileTransferComplete.set(message.payload);
                    // Remove from active transfers after a delay
                    setTimeout(() => {
                        activeFileTransfers.update(map => {
                            const newMap = new Map(map);
                            newMap.delete(message.payload.transfer_id);
                            return newMap;
                        });
                    }, 3000);
                }
                else if (message.event === "file_transfer_cancelled") {
                    console.log("File transfer cancelled:", message.payload);
                    fileTransferCancelled.set(message.payload);
                    // Remove from active transfers
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        newMap.delete(message.payload.transfer_id);
                        return newMap;
                    });
                }
                else if (message.event === "image_offer_received") {
                    console.log("Image offer received:", message.payload);

                    // Backend auto-accepts images (v0.13.0+) - no dialog needed
                    // Just add to active transfers for progress tracking
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.transfer_id, {
                            ...message.payload,
                            status: "downloading",
                            progress: 0,
                            auto_accepted: true
                        });
                        return newMap;
                    });

                    console.log(`Auto-downloading image from ${message.payload.sender_name}: ${message.payload.filename}`);
                }
                else if (message.event === "voice_offer_received") {
                    console.log("Voice offer received:", message.payload);

                    // Backend auto-accepts voice messages (v0.13.0+) - no dialog needed
                    // Just add to active transfers for progress tracking
                    activeFileTransfers.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.transfer_id, {
                            ...message.payload,
                            status: "downloading",
                            progress: 0,
                            auto_accepted: true
                        });
                        return newMap;
                    });

                    console.log(`Auto-downloading voice message from ${message.payload.sender_name}: ${message.payload.filename} (${message.payload.duration_seconds}s)`);
                }
                // Voice transcription events (v0.13.2+ auto-transcription)
                else if (message.event === "voice_transcription_received") {
                    console.log("Voice transcription received:", message.payload);
                    voiceTranscriptionReceived.set(message.payload);
                }
                else if (message.event === "voice_transcription_complete") {
                    console.log("Voice transcription complete:", message.payload);
                    voiceTranscriptionComplete.set(message.payload);
                }
                else if (message.event === "voice_transcription_config_updated") {
                    console.log("Voice transcription config updated:", message.payload);
                    voiceTranscriptionConfig.set(message.payload);
                }
                // Telegram bot integration events (v0.14.0+)
                else if (message.event === "telegram_connected") {
                    console.log("Telegram bot connected");
                    telegramConnected.set(true);
                    telegramEnabled.set(true);
                    // Clear any previous errors on successful connection
                    telegramError.set(null);
                }
                else if (message.event === "telegram_disconnected") {
                    console.log("Telegram bot disconnected");
                    telegramConnected.set(false);
                }
                else if (message.event === "telegram_error") {
                    console.error("Telegram bot error:", message.payload);
                    const { title, message: errorMsg, timestamp } = message.payload;
                    telegramError.set({ title, message: errorMsg, timestamp });

                    // Show error toast using Tauri or fallback
                    if (import.meta.env.VITE_TAURI) {
                        const { invoke } = await import('@tauri-apps/api/core');
                        try {
                            await invoke('show_error', {
                                title: `Telegram: ${title}`,
                                message: errorMsg
                            });
                        } catch (err) {
                            console.error('Failed to show error toast:', err);
                        }
                    } else {
                        // Fallback for browser dev mode
                        console.error(`Telegram Error - ${title}: ${errorMsg}`);
                    }
                }
                else if (message.event === "telegram_message_received") {
                    console.log("Telegram message received:", message.payload);
                    const { conversation_id, telegram_chat_id, sender_name, text, timestamp } = message.payload;

                    // Track unread messages (v0.15.0) - only if this chat is not active
                    if (conversation_id && typeof window !== 'undefined') {
                        if (conversation_id !== activeChat) {
                            const currentCounts = get(unreadMessageCounts);
                            const currentCount = currentCounts.get(conversation_id) || 0;
                            currentCounts.set(conversation_id, currentCount + 1);
                            unreadMessageCounts.set(new Map(currentCounts));
                        }
                    }

                    // Store the mapping: conversation_id -> telegram_chat_id
                    const currentLinkedChats = get(telegramLinkedChats);
                    if (!currentLinkedChats.has(conversation_id)) {
                        telegramLinkedChats.set(new Map(currentLinkedChats).set(conversation_id, telegram_chat_id));
                        console.log(`[Telegram] Stored linked chat: ${conversation_id} -> ${telegram_chat_id}`);
                    }

                    // Add to conversation messages
                    const currentMessages = get(telegramMessages).get(conversation_id) || [];
                    telegramMessages.set(new Map(get(telegramMessages)).set(conversation_id, [
                        ...currentMessages,
                        {
                            id: `telegram-${Date.now()}`,
                            sender: `telegram-${telegram_chat_id}`,
                            senderName: sender_name,
                            text: text,
                            timestamp: Date.now(),
                            source: 'telegram'
                        }
                    ]));

                    // Trigger event for UI
                    telegramMessageReceived.set(message.payload);
                }
                else if (message.event === "telegram_voice_received") {
                    console.log("Telegram voice received:", message.payload);

                    // Track unread messages (v0.15.0)
                    const conversationId = message.payload.conversation_id;
                    if (conversationId && typeof window !== 'undefined') {
                        if (conversationId !== activeChat) {
                            const currentCounts = get(unreadMessageCounts);
                            const currentCount = currentCounts.get(conversationId) || 0;
                            currentCounts.set(conversationId, currentCount + 1);
                            unreadMessageCounts.set(new Map(currentCounts));
                        }
                    }

                    telegramVoiceReceived.set(message.payload);
                }
                else if (message.event === "telegram_image_received") {
                    console.log("Telegram image received:", message.payload);
                    const { conversation_id, telegram_chat_id, sender_name, filename, caption } = message.payload;

                    // Track unread messages (v0.15.0)
                    if (conversation_id && typeof window !== 'undefined') {
                        if (conversation_id !== activeChat) {
                            const currentCounts = get(unreadMessageCounts);
                            const currentCount = currentCounts.get(conversation_id) || 0;
                            currentCounts.set(conversation_id, currentCount + 1);
                            unreadMessageCounts.set(new Map(currentCounts));
                        }
                    }

                    // Add to telegram messages store
                    const currentMessages = get(telegramMessages).get(conversation_id) || [];
                    telegramMessages.set(new Map(get(telegramMessages)).set(conversation_id, [
                        ...currentMessages,
                        {
                            id: `telegram-${Date.now()}`,
                            sender: `telegram-${telegram_chat_id}`,
                            senderName: sender_name,
                            text: caption || "Image",
                            timestamp: Date.now(),
                            attachments: [{
                                type: 'image',
                                filename: filename,
                                file_path: message.payload.file_path
                            }]
                        }
                    ]));

                    // Set telegramImageReceived store for +page.svelte to update chatHistories
                    telegramImageReceived.set(message.payload);
                }
                else if (message.event === "telegram_file_received") {
                    console.log("Telegram file received:", message.payload);
                    const { conversation_id, telegram_chat_id, sender_name, filename, caption } = message.payload;

                    // Track unread messages (v0.15.0)
                    if (conversation_id && typeof window !== 'undefined') {
                        if (conversation_id !== activeChat) {
                            const currentCounts = get(unreadMessageCounts);
                            const currentCount = currentCounts.get(conversation_id) || 0;
                            currentCounts.set(conversation_id, currentCount + 1);
                            unreadMessageCounts.set(new Map(currentCounts));
                        }
                    }

                    // Add to telegram messages store
                    const currentMessages = get(telegramMessages).get(conversation_id) || [];
                    telegramMessages.set(new Map(get(telegramMessages)).set(conversation_id, [
                        ...currentMessages,
                        {
                            id: `telegram-${Date.now()}`,
                            sender: `telegram-${telegram_chat_id}`,
                            senderName: sender_name,
                            text: caption || filename,
                            timestamp: Date.now(),
                            attachments: [{
                                type: 'file',
                                filename: filename,
                                file_path: message.payload.file_path,
                                mime_type: message.payload.mime_type,
                                size_bytes: message.payload.size_bytes
                            }]
                        }
                    ]));

                    // Set telegramFileReceived store for +page.svelte to update chatHistories
                    telegramFileReceived.set(message.payload);
                }
                // Error toast notifications (v0.14.1+ - VRAM OOM warnings, etc.)
                else if (message.event === "error_toast") {
                    console.log("Error toast:", message.payload);
                    const { title, message: toastMessage, duration } = message.payload;

                    // Show alert for now - can be replaced with proper toast UI component later
                    if (typeof window !== 'undefined') {
                        alert(`${title}\n\n${toastMessage}`);
                    }

                    // Also log to console for debugging
                    console.error(`[ERROR TOAST] ${title}: ${toastMessage}`);
                }

                // Whisper model loading events (v0.13.3+ model pre-loading)
                else if (message.event === "whisper_model_loading_started") {
                    console.log("Whisper model loading started:", message.payload);
                    whisperModelLoadingStarted.set(message.payload);
                }
                else if (message.event === "whisper_model_loaded") {
                    console.log("Whisper model loaded successfully:", message.payload);
                    whisperModelLoaded.set(message.payload);
                }
                else if (message.event === "whisper_model_loading_failed") {
                    console.error("Whisper model loading failed:", message.payload);
                    whisperModelLoadingFailed.set(message.payload);
                }
                else if (message.event === "whisper_model_unloaded") {
                    console.log("Whisper model unloaded:", message.payload);
                    whisperModelUnloaded.set(message.payload);
                    // Optional: Show toast notification about VRAM freed
                    // console.log(`💾 Voice transcription model unloaded (~${message.payload.vram_freed_gb}GB VRAM freed)`);
                }
                else if (message.event === "whisper_model_download_required") {
                    console.log("Whisper model download required:", message.payload);
                    whisperModelDownloadRequired.set(message.payload);
                }
                else if (message.event === "whisper_model_download_started") {
                    console.log("Whisper model download started:", message.payload);
                    whisperModelDownloadStarted.set(message.payload);
                }
                else if (message.event === "whisper_model_download_completed") {
                    console.log("Whisper model download completed:", message.payload);
                    whisperModelDownloadCompleted.set(message.payload);
                }
                else if (message.event === "whisper_model_download_failed") {
                    console.error("Whisper model download failed:", message.payload);
                    whisperModelDownloadFailed.set(message.payload);
                }
                // Agent Telegram linking events (v0.15.0+)
                else if (message.event === "agent_telegram_linked") {
                    console.log("Agent linked to Telegram:", message.payload);
                    agentTelegramLinked.set(message.payload);
                }
                else if (message.event === "agent_telegram_unlinked") {
                    console.log("Agent unlinked from Telegram:", message.payload);
                    agentTelegramUnlinked.set(message.payload);
                }
                else if (message.event === "agent_history_updated") {
                    // Telegram bridge sent a message in unified_conversation mode — silently refresh chat
                    agentHistoryUpdated.set(message.payload);
                }
                // DPC Agent progress events (v0.15.0+ - real-time agent progress in chat)
                else if (message.event === "agent_progress") {
                    // Progress update from agent during tool execution
                    // payload: {conversation_id, message, round, tool_name, ts}
                    console.log("[AgentProgress]", message.payload?.tool_name || "thinking", `round ${message.payload?.round || "?"}`);
                    agentProgress.set(message.payload);
                }
                else if (message.event === "agent_progress_clear") {
                    // Signal to clear progress display (task completed/failed)
                    console.log("[AgentProgress] Clear for conversation:", message.payload?.conversation_id);
                    agentProgressClear.set(message.payload);
                }
                else if (message.event === "agent_text_chunk") {
                    // Streaming text chunk from agent LLM response
                    // payload: {conversation_id, chunk, ts}
                    agentTextChunk.set(message.payload);
                }
                else if (message.event === "agent_chat_message") {
                    // CC response injected into agent chat
                    // payload: {conversation_id, message_id, role, content, sender_name, timestamp}
                    console.log("[AgentChatMessage] CC response in", message.payload?.conversation_id);
                    agentChatMessage.set(message.payload);
                }
                // Group chat events (v0.19.0)
                else if (message.event === "group_text_received") {
                    groupTextReceived.set(message.payload);

                    // Track unread messages for group
                    const groupId = message.payload.group_id;
                    if (groupId && typeof window !== 'undefined' && groupId !== activeChat) {
                        const currentCounts = get(unreadMessageCounts);
                        const currentCount = currentCounts.get(groupId) || 0;
                        currentCounts.set(groupId, currentCount + 1);
                        unreadMessageCounts.set(new Map(currentCounts));
                    }
                }
                else if (message.event === "group_file_received") {
                    groupFileReceived.set(message.payload);

                    // Track unread messages for group
                    const groupId = message.payload.group_id;
                    if (groupId && typeof window !== 'undefined' && groupId !== activeChat) {
                        const currentCounts = get(unreadMessageCounts);
                        const currentCount = currentCounts.get(groupId) || 0;
                        currentCounts.set(groupId, currentCount + 1);
                        unreadMessageCounts.set(new Map(currentCounts));
                    }
                }
                else if (message.event === "group_invite_received") {
                    console.log("Group invite received:", message.payload);
                    groupInviteReceived.set(message.payload);

                    // Auto-add to groupChats store (can be refined with accept/decline later)
                    groupChats.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.group_id, message.payload);
                        return newMap;
                    });
                }
                else if (message.event === "group_updated") {
                    console.log("Group updated:", message.payload);
                    groupUpdated.set(message.payload);

                    // Update groupChats store
                    groupChats.update(map => {
                        const newMap = new Map(map);
                        newMap.set(message.payload.group_id, {
                            ...newMap.get(message.payload.group_id),
                            ...message.payload
                        });
                        return newMap;
                    });
                }
                else if (message.event === "group_member_left") {
                    console.log("Group member left:", message.payload);
                    groupMemberLeft.set(message.payload);

                    // Update members in groupChats store
                    groupChats.update(map => {
                        const newMap = new Map(map);
                        const group = newMap.get(message.payload.group_id);
                        if (group) {
                            newMap.set(message.payload.group_id, {
                                ...group,
                                members: message.payload.remaining_members
                            });
                        }
                        return newMap;
                    });
                }
                else if (message.event === "group_deleted") {
                    console.log("Group deleted:", message.payload);
                    groupDeleted.set(message.payload);

                    // Remove from groupChats store
                    groupChats.update(map => {
                        const newMap = new Map(map);
                        newMap.delete(message.payload.group_id);
                        return newMap;
                    });
                }
                else if (message.event === "group_history_synced") {
                    console.log("Group history synced:", message.payload);
                    groupHistorySynced.set(message.payload);
                }
                else if (message.event === "group_message_deleted") {
                    // Backend removed a message from group history (e.g. stale morning
                    // brief replaced on Sleep). +page.svelte removes from chatHistories.
                    groupMessageDeleted.set(message.payload);
                }
                else if (message.event === "file_preparation_progress") {
                    // Reset timeout on progress (keepalive mechanism for large file hash computation)
                    for (const [cmdId, cmd] of pendingCommands.entries()) {
                        if (cmd.resetTimeout) {
                            cmd.resetTimeout();
                        }
                    }

                    // Update store for UI progress indicator
                    filePreparationProgress.set(message.payload);
                    console.log(`File prep: ${message.payload.filename} - ${message.payload.phase} ${message.payload.percent}%`);
                }
                else if (message.event === "file_preparation_started") {
                    filePreparationStarted.set(message.payload);
                    console.log(`File preparation started: ${message.payload.filename} (${message.payload.size_mb} MB)`);
                }
                else if (message.event === "file_preparation_completed") {
                    filePreparationCompleted.set(message.payload);
                    console.log(`File preparation completed: ${message.payload.filename} (hash: ${message.payload.hash?.substring(0, 16)}...)`);
                }
                else if (message.event === "history_restored") {
                    // Chat history restored from peer (v0.11.2)
                    historyRestored.set(message.payload);
                    console.log(`Chat history restored: ${message.payload.message_count} messages from ${message.payload.conversation_id}`);
                }
                // DPC Agent events (v0.19.0+ - per-agent isolation)
                else if (message.event === "agent_created") {
                    console.log("Agent created:", message.payload);
                    agentCreated.set(message.payload);
                    // Refresh agents list
                    listAgents().then(result => {
                        if (result.status === 'success') {
                            agentsList.set(result.agents || []);
                        }
                    });
                }
                else if (message.event === "agent_updated") {
                    console.log("Agent updated:", message.payload);
                    agentUpdated.set(message.payload);
                    // Refresh agents list
                    listAgents().then(result => {
                        if (result.status === 'success') {
                            agentsList.set(result.agents || []);
                        }
                    });
                }
                else if (message.event === "agent_deleted") {
                    console.log("Agent deleted:", message.payload);
                    agentDeleted.set(message.payload);
                    // Refresh agents list
                    listAgents().then(result => {
                        if (result.status === 'success') {
                            agentsList.set(result.agents || []);
                        }
                    });
                }
                else if (message.event === "sleep_state_changed") {
                    console.log("[Sleep]", message.payload?.status, message.payload?.agent_id);
                    sleepStateChanged.set(message.payload);
                    const aid = message.payload?.agent_id;
                    const originChatId = message.payload?.group_id || aid;
                    if (aid) {
                        let aName = aid;
                        agentsList.subscribe(list => { const a = list?.find((x: any) => x.agent_id === aid); if (a?.name) aName = a.name; })();
                        sleepAgentStates.update(m => {
                            const nm = new Map(m);
                            if (message.payload?.status === "awake") {
                                nm.delete(aid);
                            } else {
                                nm.set(aid, { agent_id: aid, agent_name: aName, origin_chat_id: originChatId, status: message.payload?.status, current: 0, total: 0, phase: '' });
                            }
                            return nm;
                        });
                    }
                    if (message.payload?.status === "awake") {
                        sleepProgress.set(null);
                    }
                }
                else if (message.event === "sleep_progress") {
                    sleepProgress.set(message.payload);
                    const aid = message.payload?.agent_id;
                    const originChatId = message.payload?.group_id || aid;
                    if (aid) {
                        sleepAgentStates.update(m => {
                            const nm = new Map(m);
                            const existing = nm.get(aid);
                            nm.set(aid, { ...existing, agent_id: aid, agent_name: existing?.agent_name || aid, origin_chat_id: existing?.origin_chat_id || originChatId, status: 'sleeping', current: message.payload?.current ?? 0, total: message.payload?.total ?? 0, phase: message.payload?.phase ?? '' });
                            return nm;
                        });
                    }
                }
                else if (message.event === "web_auth_popup_request") {
                    // ADR-028 T9: backend agent hit an anti-bot challenge (or
                    // matched the always_popup whitelist) on an authenticated
                    // browse and is now awaiting a popup-extracted HTML.
                    // Surface the prompt — WebAuthPopupRequestPanel binds to
                    // this store and renders the "Open {domain}" button.
                    console.log("[web_auth] popup_request", message.payload?.url);
                    webAuthPopupRequest.set(message.payload);
                }
                else if (message.event === "web_auth_popup_force_close") {
                    // T10-FRONTEND-CLEANUP-ON-TIMEOUT: backend hit an error
                    // path on a popup-fallback session (timeout, unexpected
                    // failure). Without this, the modal + the Tauri popup
                    // window stay visible indefinitely from the user's
                    // perspective even though the agent already saw the
                    // error and moved on. Close both surfaces here so the
                    // user is not left staring at a stranded popup.
                    const rid = message.payload?.request_id;
                    const reason = message.payload?.reason;
                    if (!rid) {
                        console.warn('[web_auth] popup_force_close missing request_id');
                    } else {
                        console.log('[web_auth] popup_force_close', rid, `reason=${reason}`);
                        // Cancel any armed watchdog so it doesn't fire after cleanup.
                        cancelPopupCloseWatchdog(rid);
                        // Dismiss the in-app modal so the user is unblocked
                        // immediately. webAuthPopupRequest holds at most one
                        // outstanding request at a time (Q3 sequential).
                        webAuthPopupRequest.set(null);
                        // Best-effort: close the popup window via Tauri's
                        // WebviewWindow API. The popup window label matches
                        // the backend convention `web_auth_popup_{request_id}`.
                        (async () => {
                            try {
                                const { WebviewWindow } = await import('@tauri-apps/api/webviewWindow');
                                const popup = await WebviewWindow.getByLabel(`web_auth_popup_${rid}`);
                                if (popup) {
                                    await popup.close();
                                }
                            } catch (e) {
                                console.log('[web_auth] popup_force_close: nothing to close', e);
                            }
                        })();
                    }
                }
            } catch (error) {
                console.error("Error parsing message:", error);
            }
        });

        socket.addEventListener('error', (error) => {
            clearLogSender();
            console.error("WebSocket error:", error);
            connectionStatus.set('error');
        });

        socket.addEventListener('close', (event) => {
            clearLogSender();
            console.log("WebSocket closed:", event.code, event.reason);
            nodeStatus.set(null);
            socket = null;

            // Code 1008 = policy violation. The backend uses this exclusively for
            // local API auth failures (missing/invalid token, timeout). The token
            // won't change until the backend restarts, so retrying with the same
            // stale token would just spin forever.
            if (event.code === 1008) {
                console.error(`❌ Local API auth rejected: ${event.reason}`);
                connectionStatus.set('error');
                reconnectAttempts = MAX_RECONNECT_ATTEMPTS;
                return;
            }

            // Reconnect unless manually disconnected (disconnectFromCoreService sets reconnectAttempts = MAX)
            if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                connectionStatus.set('disconnected');
                reconnectAttempts++;
                const delay = Math.min(RECONNECT_DELAY * Math.pow(2, reconnectAttempts - 1), 30000);
                console.log(`Reconnecting in ${delay}ms... (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
                reconnectTimeout = setTimeout(() => connectToCoreService(), delay);
            } else {
                connectionStatus.set('error');
            }
        });

    } catch (error) {
        console.error("Failed to create WebSocket:", error);
        connectionStatus.set('error');
    }
}

export function disconnectFromCoreService() {
    console.log("Disconnecting from Core Service...");
    
    if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
    }

    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
    
    reconnectAttempts = MAX_RECONNECT_ATTEMPTS;
    
    if (socket) {
        socket.close(1000, "Manual disconnect");
        socket = null;
    }
    
    connectionStatus.set('disconnected');
    nodeStatus.set(null);
}

export function resetReconnection() {
    reconnectAttempts = 0;
    if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
    }
}


// ─────────────────────────────────────────────────────────────
// ADR-028 Web Auth — Tauri event → WebSocket forward
// ─────────────────────────────────────────────────────────────
// The Tauri Rust `web_auth_open_login_window` command spawns a WebView
// popup for user login. On close, it emits the `web_auth_login_complete`
// Tauri event carrying {domain, cookies} — but NOT the agent_id (the
// Rust side doesn't know which agent initiated the login).
//
// The UI panel that triggered the login knows the agent_id. Before
// calling invoke('web_auth_open_login_window', ...), it MUST call
// registerPendingWebAuthLogin(agent_id, domain) so this central
// listener can pair the incoming cookies with the right agent and
// forward to the Python vault via the web_auth_login_complete WS
// command.

const pendingWebAuthLogins = new Map<string, string>();
let webAuthListenerInstalled = false;

// ADR-028 T9 bug 1 — Path A defense. When the Rust popup CloseRequested
// handler fires, it emits `web_auth_popup_closing` and then tries to
// eval the JS extractor. If the WebView is torn down before the JS
// runs, no `web_auth_popup_extracted` event ever arrives — the modal
// would otherwise stay stuck until the 5-minute backend timeout. The
// closing-event listener arms a short watchdog for each request_id;
// the extracted listener cancels it on success.
const popupCloseWatchdogs = new Map<string, ReturnType<typeof setTimeout>>();
const POPUP_CLOSE_WATCHDOG_MS = 3000;

/**
 * Bug 6 (S143): cancel an armed popup-close watchdog for a given
 * request_id. Used by the WebAuthPopupRequest modal Cancel button to
 * prevent a double `web_auth_popup_complete` round (Cancel resolves
 * the backend future, then the watchdog would fire a second one and
 * crash the handler with InvalidStateError on the already-done future).
 */
export function cancelPopupCloseWatchdog(requestId: string): void {
    const timer = popupCloseWatchdogs.get(requestId);
    if (timer !== undefined) {
        clearTimeout(timer);
        popupCloseWatchdogs.delete(requestId);
    }
}

export function registerPendingWebAuthLogin(agentId: string, domain: string): void {
    const key = domain.toLowerCase();
    // Stale-entry guard (Ark S140 [#82] review): if a previous popup
    // for this domain never produced an event (user closed without
    // logging in, site crashed, browser hung), the old (agent_id,
    // domain) pair would still sit in the Map. Without overwriting,
    // a subsequent login for a DIFFERENT agent on the same domain
    // would route the new cookies to the OLD agent — privacy leak.
    // Overwrite-with-warning is the minimum defense; full TTL is a
    // Phase 2 nicety.
    const previous = pendingWebAuthLogins.get(key);
    if (previous && previous !== agentId) {
        console.warn(
            `[web_auth] overwriting pending login for ${key} — ` +
            `previous agent=${previous} did not complete login. ` +
            `Cookies from a future event for this domain will now be ` +
            `attributed to agent=${agentId}.`
        );
    }
    pendingWebAuthLogins.set(key, agentId);
}

async function ensureWebAuthListener(): Promise<void> {
    if (webAuthListenerInstalled) return;
    try {
        const { listen } = await import('@tauri-apps/api/event');
        await listen<{ domain: string; cookies: any[] }>('web_auth_login_complete', (event) => {
            const payload = event.payload;
            if (!payload || !payload.domain) {
                console.warn('[web_auth] received login_complete with no domain — ignoring');
                return;
            }
            const key = payload.domain.toLowerCase();
            const agentId = pendingWebAuthLogins.get(key);
            if (!agentId) {
                console.warn(
                    `[web_auth] received login_complete for ${payload.domain} ` +
                    `but no pending agent_id registered — ignoring. ` +
                    `UI panels must call registerPendingWebAuthLogin() before ` +
                    `invoking web_auth_open_login_window.`
                );
                return;
            }
            pendingWebAuthLogins.delete(key);
            sendCommand('web_auth_login_complete', {
                agent_id: agentId,
                domain: payload.domain,
                cookies: payload.cookies || [],
            });
        });

        // ADR-028 T9 bug 1 — Path A watchdog. Rust emits this BEFORE
        // calling popup.eval(). Arm a short timer here; the
        // `web_auth_popup_extracted` listener below cancels it on
        // either success or Rust-side eval-error. If neither arrives
        // (Path A — JS torn down before injected emit runs), the timer
        // fires, manually forwards an error to backend, and dismisses
        // the modal so the user is not stuck staring at "browser
        // window opened" after they already closed it.
        await listen<{ request_id: string }>('web_auth_popup_closing', (event) => {
            const payload = event.payload;
            if (!payload || !payload.request_id) return;
            const requestId = payload.request_id;
            // Idempotent: if Rust emits closing twice (shouldn't, but
            // defensive), the second timer overwrites the first cleanly.
            const existing = popupCloseWatchdogs.get(requestId);
            if (existing !== undefined) clearTimeout(existing);
            const timer = setTimeout(() => {
                popupCloseWatchdogs.delete(requestId);
                console.warn(
                    `[web_auth] popup_extracted did not arrive within ${POPUP_CLOSE_WATCHDOG_MS}ms ` +
                    `for request_id=${requestId} — assuming Path A (JS torn down before emit), ` +
                    `closing modal and reporting error to backend`,
                );
                sendCommand('web_auth_popup_complete', {
                    request_id: requestId,
                    error: 'popup close timeout — JS extraction event did not arrive',
                });
                webAuthPopupRequest.set(null);
            }, POPUP_CLOSE_WATCHDOG_MS);
            popupCloseWatchdogs.set(requestId, timer);
        });

        // ADR-028 T9: same listener-install gate handles the popup-fallback
        // companion event. Rust `web_auth_open_popup_for_content` emits this
        // when the user closes the popup; the payload already carries
        // request_id (closed-over in the init-script JS), so unlike the T8
        // login flow we don't need a pendingMap to recover the agent — the
        // Python Step-3 handler resolves the matching Future by id.
        await listen<{
            request_id: string;
            content_html?: string;
            current_url?: string;
            error?: string;
        }>('web_auth_popup_extracted', (event) => {
            const payload = event.payload;
            if (!payload || !payload.request_id) {
                console.warn('[web_auth] popup_extracted with no request_id — ignoring');
                return;
            }
            // Cancel any pending Path-A watchdog set by the closing listener.
            const watchdog = popupCloseWatchdogs.get(payload.request_id);
            if (watchdog !== undefined) {
                clearTimeout(watchdog);
                popupCloseWatchdogs.delete(payload.request_id);
            }
            console.log(
                '[web_auth] popup_extracted',
                payload.request_id,
                payload.error ? `error=${payload.error}` : `bytes=${payload.content_html?.length || 0}`,
            );
            sendCommand('web_auth_popup_complete', {
                request_id: payload.request_id,
                content_html: payload.content_html,
                current_url: payload.current_url,
                error: payload.error,
            });
            // Clear the pending-request store so the panel dismisses its UI.
            webAuthPopupRequest.set(null);
        });

        webAuthListenerInstalled = true;
        console.log('[web_auth] Tauri event listeners installed');
    } catch (e) {
        // Tauri not available (e.g. running outside Tauri shell) — the
        // web-auth flow won't work, but this isn't fatal for the rest
        // of the app. Log once and move on.
        console.warn('[web_auth] could not install Tauri event listener:', e);
    }
}

// Kick off listener installation at module load. Safe — no-ops outside
// Tauri context. Doing this lazily-at-import means panels don't need
// to coordinate timing.
void ensureWebAuthListener();


export function sendCommand(command: string, payload: any = {}, commandId?: string): Promise<any> | boolean {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        console.error(`Cannot send command '${command}': WebSocket not connected`);
        return false;
    }

    try {
        const id = commandId || crypto.randomUUID();
        const message = {
            id,
            command,
            payload
        };

        // Default = Promise. The backend's local_api dispatcher ALWAYS
        // sends a {status, payload} response after handler completes
        // (local_api.py:328-330), so any command we send is awaitable.
        // The previous design used an `expectsResponse` allowlist with
        // default = boolean — but missing entries silently failed when
        // callers `await`-ed them (the await resolved to `true`, not the
        // backend response, and callers fell into their error branch).
        //
        // Inverted to fail-safe: forgetting to register a new command
        // means the caller correctly gets a Promise. The only commands
        // that opt out are true fire-and-forget logging signals that
        // a caller would never await. Keep this list minimal — when in
        // doubt, leave the command off so it defaults to Promise.
        //
        // Tradeoff accepted: if a caller does NOT `await` a Promise-
        // returning command, the returned Promise is dropped without
        // a handler. The browser may log an unhandled rejection if the
        // command times out (60s) — visible noise rather than silent
        // failure, which matches our preferred debugging stance.
        //
        // Origin: Mike S141 / Bug A — `web_auth_*` commands were missing
        // from the old allowlist and the UI displayed "failed to add /
        // load domains" even though backend was returning success.
        const fireAndForgetCommands = new Set<string>([
            'ui_log',  // logging beacon — no semantic response, drop the await
            // Agent loop response is routed via event stream ($coreMessages →
            // MessageRouterPanel) rather than the pending-command promise.
            // ChatPanel calls sendCommand without await, so the unawaited
            // promise rejected at 60s when popup_fallback waits for user
            // (up to 5min), surfacing as `Uncaught (in promise) Error:
            // Command 'execute_ai_query' timed out` in DevTools.
            'execute_ai_query',
        ]);
        const expectsResponse = !fireAndForgetCommands.has(command);

        if (expectsResponse) {
            return new Promise((resolve, reject) => {
                // Calculate dynamic timeout for file operations and connections
                let timeout = 60000;  // Default: 60s (increased from 25s in v0.20.0 for slow systems)

                if (command === 'connect_to_peer' || command === 'connect_via_dht') {
                    // Connection timeout: 60s (includes pre-flight check + TLS handshake + HELLO)
                    timeout = 60000;
                } else if (command === 'ai_assisted_instruction_creation_remote') {
                    // AI instruction creation timeout: 60s (remote LLM processing can take time)
                    timeout = 60000;
                } else if (command === 'transcribe_audio') {
                    // Voice transcription timeout: 240s (v0.13.1+)
                    // First use: model download (~3GB, 1-2min) + load (~20s) + compile (~30s) + transcribe (~5s)
                    // Subsequent uses: ~5-10s
                    timeout = 240000;
                } else if (command === 'send_image') {
                    // Image vision analysis timeout: 240s (v0.20.0+)
                    // Local vision models can be slow, especially on first load or with large images
                    timeout = 240000;
                } else if (command === 'send_file') {
                    // Dynamic timeout based on file size (v0.11.2+)
                    const fileSizeBytes = payload.file_size_bytes || 0;
                    const fileSizeGB = fileSizeBytes / (1024 * 1024 * 1024);

                    // Formula: base (60s) + per_gb (40s/GB) + safety margin (20s)
                    // Examples: 1GB=120s, 5GB=280s, 10GB=480s
                    const baseTimeout = 60000;      // 60 seconds
                    const perGBTimeout = 40000;     // 40 seconds per GB
                    const safetyMargin = 20000;     // 20 seconds extra

                    timeout = baseTimeout + (fileSizeGB * perGBTimeout) + safetyMargin;

                    console.log(`File send timeout: ${Math.round(timeout/1000)}s for ${fileSizeGB.toFixed(2)}GB file`);
                }

                const timeoutSeconds = timeout / 1000;
                let timeoutHandle: ReturnType<typeof setTimeout>;

                // Create timeout handler
                const createTimeout = () => {
                    return setTimeout(() => {
                        if (pendingCommands.has(id)) {
                            pendingCommands.delete(id);
                            reject(new Error(`Command '${command}' timed out after ${timeoutSeconds} seconds`));
                        }
                    }, timeout);
                };

                timeoutHandle = createTimeout();

                // Store callbacks with timeout reset capability (keepalive mechanism)
                pendingCommands.set(id, {
                    resolve,
                    reject,
                    resetTimeout: () => {
                        // Clear existing timeout and create new one (keepalive)
                        clearTimeout(timeoutHandle);
                        timeoutHandle = createTimeout();
                        console.log(`Timeout reset for command ${id} (keepalive)`);
                    }
                });

                // Send the message
                socket!.send(JSON.stringify(message));
            });
        } else {
            // For commands that don't expect a response, just send
            socket.send(JSON.stringify(message));
            return true;
        }
    } catch (error) {
        console.error(`Error sending command '${command}':`, error);
        return false;
    }
}

// Helper function to set the currently active chat (prevents unread badges on open chats)
export function setActiveChat(chatId: string | null) {
    activeChat = chatId;
}

// File transfer helper functions (Week 1)
export async function sendFile(nodeId: string, filePath: string): Promise<any> {
    // Get file size for timeout calculation (v0.11.2+)
    let fileSizeBytes = 0;
    try {
        const { invoke } = await import('@tauri-apps/api/core');
        const metadata = await invoke('get_file_metadata', { path: filePath });
        fileSizeBytes = (metadata as any).size;
    } catch (e) {
        console.warn('Could not get file size for timeout calculation:', e);
        // Continue anyway - backend will use default timeout
    }

    return sendCommand('send_file', {
        node_id: nodeId,
        file_path: filePath,
        file_size_bytes: fileSizeBytes
    });
}

export async function acceptFileTransfer(transferId: string): Promise<any> {
    return sendCommand('accept_file_transfer', {
        transfer_id: transferId
    });
}

export async function cancelFileTransfer(transferId: string, reason: string = "user_cancelled"): Promise<any> {
    return sendCommand('cancel_file_transfer', {
        transfer_id: transferId,
        reason: reason
    });
}

// Voice message function (v0.13.0 - Voice Messages)
export async function sendVoiceMessage(
    nodeId: string,
    audioBlob: Blob,
    durationSeconds: number
): Promise<any> {
    // Convert blob to base64
    const arrayBuffer = await audioBlob.arrayBuffer();
    const base64 = btoa(
        new Uint8Array(arrayBuffer).reduce(
            (data, byte) => data + String.fromCharCode(byte),
            ''
        )
    );

    return sendCommand('send_voice_message', {
        node_id: nodeId,
        audio_base64: base64,
        duration_seconds: durationSeconds,
        mime_type: audioBlob.type || 'audio/webm'
    });
}

// Helper function to reset unread count when chat becomes active (v0.9.3)
export function resetUnreadCount(peerId: string) {
    const currentCounts = get(unreadMessageCounts);
    if (currentCounts.has(peerId)) {
        currentCounts.delete(peerId);
        unreadMessageCounts.set(new Map(currentCounts));
    }
}

// New session proposal helpers (v0.11.3)
export async function proposeNewSession(conversationId: string): Promise<any> {
    return sendCommand('propose_new_session', {
        conversation_id: conversationId
    });
}

export async function voteNewSession(proposalId: string, vote: boolean): Promise<any> {
    return sendCommand('vote_new_session', {
        proposal_id: proposalId,
        vote: vote
    });
}

// Voice transcription config commands (v0.13.2+)
export async function getVoiceTranscriptionConfig(): Promise<any> {
    return sendCommand('get_voice_transcription_config', {});
}

export async function saveVoiceTranscriptionConfig(config: any): Promise<any> {
    return sendCommand('save_voice_transcription_config', { config });
}

// Per-conversation transcription control (v0.13.2+ checkbox)
export async function setConversationTranscription(nodeId: string, enabled: boolean): Promise<any> {
    return sendCommand('set_conversation_transcription', { node_id: nodeId, enabled });
}

export async function getConversationTranscription(nodeId: string): Promise<any> {
    return sendCommand('get_conversation_transcription', { node_id: nodeId });
}

// Whisper model pre-loading (v0.13.3+ - load model before first transcription)
export async function preloadWhisperModel(providerAlias?: string): Promise<any> {
    return sendCommand('preload_whisper_model', {
        provider_alias: providerAlias  // Optional: specify provider, or use first local_whisper
    });
}

// Telegram bot integration (v0.14.0+)
export async function sendToTelegram(
    conversationId: string,
    text: string,
    attachments?: any[],
    voiceAudioBase64?: string,
    voiceDurationSeconds?: number,
    voiceMimeType?: string,
    filePath?: string  // NEW: For sending files/images to Telegram
): Promise<any> {
    const payload: any = {
        conversation_id: conversationId,
        text: text,
        attachments: attachments || []
    };

    // Add voice parameters if provided
    if (voiceAudioBase64 !== undefined) {
        payload.voice_audio_base64 = voiceAudioBase64;
    }
    if (voiceDurationSeconds !== undefined) {
        payload.voice_duration_seconds = voiceDurationSeconds;
    }
    if (voiceMimeType !== undefined) {
        payload.voice_mime_type = voiceMimeType;
    }
    // Add file path if provided (for sending files/images to Telegram)
    if (filePath !== undefined) {
        payload.file_path = filePath;
    }

    return sendCommand('send_to_telegram', payload);
}

export async function linkTelegramChat(conversationId: string, telegramChatId: string): Promise<any> {
    return sendCommand('link_telegram_chat', {
        conversation_id: conversationId,
        telegram_chat_id: telegramChatId
    });
}

export async function getTelegramStatus(): Promise<any> {
    return sendCommand('get_telegram_status', {});
}

// Group Chat API functions (v0.19.0)

export async function createGroupChat(name: string, topic: string = "", memberNodeIds: string[] = []): Promise<any> {
    return sendCommand('create_group_chat', {
        name,
        topic,
        member_node_ids: memberNodeIds
    });
}

export async function sendGroupMessage(groupId: string, text: string): Promise<any> {
    return sendCommand('send_group_message', {
        group_id: groupId,
        text
    });
}

export async function sendGroupImage(groupId: string, imageBase64: string, filename?: string, text: string = ""): Promise<any> {
    return sendCommand('send_group_image', {
        group_id: groupId,
        image_base64: imageBase64,
        filename,
        text
    });
}

export async function sendGroupVoiceMessage(groupId: string, audioBase64: string, durationSeconds: number, mimeType: string = "audio/webm"): Promise<any> {
    return sendCommand('send_group_voice_message', {
        group_id: groupId,
        audio_base64: audioBase64,
        duration_seconds: durationSeconds,
        mime_type: mimeType
    });
}

export async function sendGroupFile(groupId: string, filePath: string): Promise<any> {
    return sendCommand('send_group_file', {
        group_id: groupId,
        file_path: filePath
    });
}

export async function addGroupMember(groupId: string, nodeId: string): Promise<any> {
    return sendCommand('add_group_member', {
        group_id: groupId,
        node_id: nodeId
    });
}

export async function removeGroupMember(groupId: string, nodeId: string): Promise<any> {
    return sendCommand('remove_group_member', {
        group_id: groupId,
        node_id: nodeId
    });
}

export async function leaveGroup(groupId: string): Promise<any> {
    return sendCommand('leave_group', {
        group_id: groupId
    });
}

export async function deleteGroup(groupId: string): Promise<any> {
    return sendCommand('delete_group', {
        group_id: groupId
    });
}

export async function loadGroups(): Promise<void> {
    try {
        const result = await sendCommand('get_groups', {});
        if (result && result.status === "success" && result.groups) {
            const groupMap = new Map<string, any>();
            for (const group of result.groups) {
                groupMap.set(group.group_id, group);
            }
            groupChats.set(groupMap);
        }
    } catch (e) {
        console.error("Failed to load groups:", e);
    }
}

// Conversation settings (v0.21.0 - per-conversation persistence)
export async function getConversationSettings(conversationId: string): Promise<any> {
    return sendCommand('get_conversation_settings', {
        conversation_id: conversationId
    });
}

export async function setConversationPersistHistory(conversationId: string, persist: boolean): Promise<any> {
    return sendCommand('set_conversation_persist_history', {
        conversation_id: conversationId,
        persist: persist
    });
}

export async function updateGroupTopic(groupId: string, topic: string): Promise<any> {
    return sendCommand('update_group_topic', { group_id: groupId, topic });
}

export async function deleteConversation(conversationId: string): Promise<any> {
    return sendCommand('delete_conversation', {
        conversation_id: conversationId
    });
}

// --- DPC Agent Management (v0.19.0+) ---
// AgentInfo and AgentConfig interfaces are in src/lib/types.ts
// Agent stores (agentsList, agentCreated, agentUpdated, agentDeleted, agentProfiles) are in
// services/agents.ts and re-exported above.

/**
 * Create a new DPC Agent with isolated storage.
 */
export async function createAgent(
    name: string,
    providerAlias: string = "dpc_agent",
    profileName: string = "default",
    instructionSetName: string = "general",
    budgetUsd: number = 50.0,
    maxRounds: number = 200,
    computeHost?: string,
    contextWindow?: number,
    retrievalVector?: string,
    retrievalText?: string,
): Promise<any> {
    return sendCommand('create_agent', {
        name,
        provider_alias: providerAlias,
        profile_name: profileName,
        instruction_set_name: instructionSetName,
        budget_usd: budgetUsd,
        max_rounds: maxRounds,
        ...(computeHost ? { compute_host: computeHost } : {}),
        ...(contextWindow ? { context_window: contextWindow } : {}),
        ...(retrievalVector ? { retrieval_vector: retrievalVector } : {}),
        ...(retrievalText ? { retrieval_text: retrievalText } : {}),
    });
}

/**
 * List all registered DPC Agents.
 */
export async function listAgents(): Promise<any> {
    return sendCommand('list_agents', {});
}

/**
 * Get configuration for a specific agent.
 */
export async function getAgentConfig(agentId: string): Promise<any> {
    return sendCommand('get_agent_config', {
        agent_id: agentId
    });
}

/**
 * Update configuration for a specific agent.
 */
export async function updateAgentConfig(agentId: string, updates: Record<string, any>): Promise<any> {
    return sendCommand('update_agent_config', {
        agent_id: agentId,
        updates
    });
}

/**
 * Delete a DPC Agent and its storage.
 */
export async function deleteAgent(agentId: string): Promise<any> {
    return sendCommand('delete_agent', {
        agent_id: agentId
    });
}

/**
 * List available agent permission profiles.
 */
export async function listAgentProfiles(): Promise<any> {
    return sendCommand('list_agent_profiles', {});
}