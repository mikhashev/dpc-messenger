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
    VoteTally,
    VoiceTranscription,
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
    VoteTally,
    VoiceTranscription,
};

// --- Service store imports (Step 2c: stores now live in services/) ---
// Imported here for use by the event handler and command functions; re-exported below for
// backward compat (8 components + +page.svelte import from '$lib/coreService').

import { connectionStatus, nodeStatus, coreMessages } from './services/connection';
import { p2pMessages, unreadMessageCounts } from './services/messaging';
import { availableProviders, defaultProviders, providersList, peerProviders, aiResponseWithImage, firewallRulesUpdated } from './services/providers';
import { fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, filePreparationStarted, filePreparationProgress, filePreparationCompleted } from './services/fileTransfer';
import { voiceOfferReceived, voiceTranscriptionReceived, voiceTranscriptionComplete, voiceTranscriptionConfig, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, whisperModelUnloaded, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed } from './services/voice';
import { groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced } from './services/groups';
import { agentsList, agentCreated, agentUpdated, agentDeleted, agentProfiles, agentProgress, agentProgressClear, agentTextChunk } from './services/agents';
import { telegramEnabled, telegramConnected, telegramStatus, telegramError, telegramLinkedChats, telegramMessages, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated } from './services/telegram';
import { personalContext, contextUpdated, peerContextUpdated, knowledgeCommitProposal, knowledgeCommitResult, extractionFailure, tokenWarning, integrityWarnings } from './services/knowledge';
import { historyRestored, newSessionProposal, newSessionResult, conversationReset, conversationSettings, conversationSettingsChanged, conversationDeleted } from './services/session';

// Re-export all service stores for backward compatibility.
// NOTE: When adding new UI-reactive fields from privacy_rules.json, add the store in
// services/providers.ts and re-export it here. See CLAUDE.md "UI Integration Pattern".
export { connectionStatus, nodeStatus, coreMessages };
export { p2pMessages, unreadMessageCounts };
export { availableProviders, defaultProviders, providersList, peerProviders, aiResponseWithImage, firewallRulesUpdated };
export { fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, filePreparationStarted, filePreparationProgress, filePreparationCompleted };
export { voiceOfferReceived, voiceTranscriptionReceived, voiceTranscriptionComplete, voiceTranscriptionConfig, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, whisperModelUnloaded, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed };
export { groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced };
export { agentsList, agentCreated, agentUpdated, agentDeleted, agentProfiles, agentProgress, agentProgressClear, agentTextChunk };
export { telegramEnabled, telegramConnected, telegramStatus, telegramError, telegramLinkedChats, telegramMessages, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated };
export { personalContext, contextUpdated, peerContextUpdated, knowledgeCommitProposal, knowledgeCommitResult, extractionFailure, tokenWarning, integrityWarnings };
export { historyRestored, newSessionProposal, newSessionResult, conversationReset, conversationSettings, conversationSettingsChanged, conversationDeleted };

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

        // Check if connection opened
        if (socket.readyState === WebSocket.OPEN && get(connectionStatus) !== 'connected') {
            console.log("✅ WebSocket connection established");
            connectionStatus.set('connected');
            reconnectAttempts = 0;
            sendCommand("get_status");
            
            // Stop polling once connected
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

export function connectToCoreService() {
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

    try {
        socket = new WebSocket(API_URL);

        // Start polling for state changes
        startPolling();

        // Set up event listeners (belt and suspenders)
        socket.addEventListener('open', () => {
            console.log("✅ WebSocket opened via event");
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

        // For commands that expect a response, return a Promise
        const expectsResponse = [
            'get_personal_context',
            'save_personal_context',
            'reload_personal_context',
            'get_instructions',
            'save_instructions',
            'reload_instructions',
            'get_firewall_rules',
            'save_firewall_rules',
            'reload_firewall',
            'validate_firewall_rules',
            'get_providers_config',
            'save_providers_config',
            'get_default_providers',  // Dual provider system
            'get_providers_list',     // Dual provider system
            'query_ollama_model_info',
            'toggle_auto_knowledge_detection',
            'send_file',
            'send_p2p_image',  // Screenshot sending
            'send_image',  // Vision analysis (clipboard paste)
            'accept_file_transfer',
            'cancel_file_transfer',
            'get_conversation_history',  // v0.11.2 - backend→frontend sync
            'connect_to_peer',  // v0.12.0 - async connection with error handling
            'connect_via_dht',   // v0.12.0 - async connection with error handling
            'get_wizard_template',  // AI wizard - load wizard configuration
            'ai_assisted_instruction_creation',  // AI wizard - generate instruction set (local)
            'ai_assisted_instruction_creation_remote',  // AI wizard - generate instruction set (remote)
            'get_available_templates',  // Template import - list templates
            'import_instruction_template',  // Template import - import template
            'create_instruction_set',  // Instruction management
            'delete_instruction_set',  // Instruction management
            'rename_instruction_set',  // Instruction management
            'set_default_instruction_set',  // Instruction management
            'get_instruction_set',  // Instruction management
            'transcribe_audio',  // v0.13.1 - voice message transcription
            'get_voice_transcription_config',  // v0.13.2 - auto-transcription config
            'save_voice_transcription_config',  // v0.13.2 - auto-transcription config
            'set_conversation_transcription',  // v0.13.2 - per-conversation transcription control
            'get_conversation_transcription',  // v0.13.2 - per-conversation transcription control
            'prepare_agent',  // Pre-initialize DPC Agent and Telegram bridge
            'query_remote_providers',  // v0.18.0 - fetch available providers from remote peer
            'get_groups',  // v0.19.0 - group chat management
            'create_group_chat',  // v0.19.0 - group chat creation
            'leave_group',  // v0.19.0 - leave group
            'delete_group',  // v0.19.0 - delete group
            'get_conversation_settings',  // v0.21.0 - per-conversation settings
            'set_conversation_persist_history',  // v0.21.0 - toggle history persistence
            'delete_conversation',  // v0.21.0 - delete entire conversation
            'create_agent',  // DPC Agent isolation - create agent with isolated storage
            'list_agents',  // DPC Agent isolation - list all agents
            'get_agent_config',  // DPC Agent isolation - get agent configuration
            'update_agent_config',  // DPC Agent isolation - update agent configuration
            'delete_agent',  // DPC Agent isolation - delete agent
            'list_agent_profiles',  // DPC Agent isolation - list permission profiles
            // Agent Task Board (v0.20.0)
            'get_agent_tasks',
            'get_agent_learning',
            'get_agent_task_result',
            'schedule_agent_task',
        ].includes(command);

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