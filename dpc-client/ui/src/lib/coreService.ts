// dpc-client/ui/src/lib/coreService.ts
// PRODUCTION VERSION - Clean, no excessive logging

import { writable, get } from 'svelte/store';

// TypeScript types for dual provider system
export interface ProviderInfo {
    alias: string;
    model: string;
    type: string;
    supports_vision: boolean;
}

export interface DefaultProvidersResponse {
    default_provider: string;
    vision_provider: string;
}

export interface ProvidersListResponse {
    providers: ProviderInfo[];
    default_provider: string;
    vision_provider: string;
}

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);
export const coreMessages = writable<any>(null);
export const p2pMessages = writable<any>(null);

// Knowledge Architecture stores (Phase 1-6 integration)
export const knowledgeCommitProposal = writable<any>(null);
export const knowledgeCommitResult = writable<any>(null);
export const personalContext = writable<any>(null);

// Token tracking stores (Phase 2)
export const tokenWarning = writable<any>(null);

// Knowledge extraction failure store (Phase 4)
export const extractionFailure = writable<any>(null);

// AI Providers store (legacy)
export const availableProviders = writable<any>(null);

// Dual Provider Stores (Phase 1: Dual Dropdowns)
export const defaultProviders = writable<DefaultProvidersResponse | null>(null);
export const providersList = writable<ProviderInfo[]>([]);

// Peer Providers store (node_id -> provider list)
export const peerProviders = writable<Map<string, any[]>>(new Map());

// Phase 7: Context update stores (for status indicators)
export const contextUpdated = writable<any>(null);
export const peerContextUpdated = writable<any>(null);

// Firewall rules update store (for AI scope reload)
// IMPORTANT: If you add new UI-reactive fields from privacy_rules.json (like node_groups,
// compute settings, device_sharing, etc.), follow this pattern:
// 1. Create a writable store here (e.g., export const computeSettingsUpdated = writable<any>(null))
// 2. Add event handler below for 'firewall_rules_updated' event
// 3. In +page.svelte (or relevant component), add reactive statement to reload data
// This ensures UI updates immediately when firewall rules are saved via the editor.
export const firewallRulesUpdated = writable<any>(null);

// Unread message counter (v0.9.3)
export const unreadMessageCounts = writable<Map<string, number>>(new Map());

// File transfer stores (Week 1)
export const fileTransferOffer = writable<any>(null);  // Incoming file offer notifications
export const fileTransferProgress = writable<any>(null);  // Progress updates
export const fileTransferComplete = writable<any>(null);  // Completed transfers
export const fileTransferCancelled = writable<any>(null);  // Cancelled transfers

// Vision/Image stores (Phase 2)
export const aiResponseWithImage = writable<any>(null);  // AI vision analysis responses

// Track active file transfers (transfer_id -> {node_id, filename, direction, progress, status})
export const activeFileTransfers = writable<Map<string, any>>(new Map());

// File preparation stores (v0.11.2 - for Send File dialog progress indicator)
export const filePreparationStarted = writable<any>(null);  // {filename, size_bytes, size_mb}
export const filePreparationProgress = writable<any>(null);  // {filename, phase, percent, bytes_processed, total_size}
export const filePreparationCompleted = writable<any>(null);  // {filename, hash, total_chunks}

// Chat history restore store (v0.11.2 - for auto-restore on reconnect)
export const historyRestored = writable<any>(null);  // {conversation_id, message_count, messages}

// New session proposal store (v0.11.3 - mutual session approval)
export const newSessionProposal = writable<any>(null);  // {proposal_id, initiator_node_id, conversation_id, timestamp}
export const newSessionResult = writable<any>(null);  // {proposal_id, result, clear_history, vote_tally}

// Conversation reset store (v0.11.3 - for AI chats and approved P2P session resets)
export const conversationReset = writable<any>(null);  // {conversation_id}

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
            sendCommand("get_status");
            sendCommand("list_providers");
            sendCommand("get_default_providers");  // Fetch default text/vision providers
            sendCommand("get_providers_list");     // Fetch full provider list with vision flags

            // Stop polling
            if (pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
            }
        });

        socket.addEventListener('message', (event) => {
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
                // Handle firewall_rules_updated event
                // NOTE: This event is triggered when user saves firewall rules via FirewallEditor.
                // It allows UI components to reload data from privacy_rules.json without page refresh.
                // Example: AI scopes dropdown reloads when ai_scopes section is modified.
                // If you add more UI-reactive fields (compute settings, node groups, etc.),
                // update the corresponding store here (see pattern in store declarations above).
                else if (message.event === "firewall_rules_updated") {
                    console.log("Firewall rules updated, triggering AI scope reload");
                    firewallRulesUpdated.set(message.payload);
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

                    // Get auto-accept threshold from firewall rules (default 25MB)
                    const autoAcceptThresholdMB = 25; // TODO: Read from firewall rules store
                    const sizeMB = message.payload.size_bytes / (1024 * 1024);

                    if (sizeMB <= autoAcceptThresholdMB) {
                        // Auto-accept small images
                        console.log(`Auto-accepting image (${sizeMB.toFixed(2)} MB ≤ ${autoAcceptThresholdMB} MB)`);

                        // Immediately accept transfer
                        sendCommand("accept_file_transfer", {
                            transfer_id: message.payload.transfer_id
                        });

                        // Add to active transfers for progress tracking
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

                        // Log for user notification (optional)
                        console.log(`Auto-downloading image from ${message.payload.sender_name}: ${message.payload.filename}`);
                    } else {
                        // Large image: Show acceptance dialog
                        console.log(`Large image (${sizeMB.toFixed(2)} MB), prompting user`);
                        fileTransferOffer.set(message.payload); // Reuse existing dialog
                    }
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
            } catch (error) {
                console.error("Error parsing message:", error);
            }
        });

        socket.addEventListener('error', (error) => {
            console.error("WebSocket error:", error);
            connectionStatus.set('error');
        });

        socket.addEventListener('close', (event) => {
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
            'accept_file_transfer',
            'cancel_file_transfer',
            'get_conversation_history',  // v0.11.2 - backend→frontend sync
            'connect_to_peer',  // v0.12.0 - async connection with error handling
            'connect_via_dht'   // v0.12.0 - async connection with error handling
        ].includes(command);

        if (expectsResponse) {
            return new Promise((resolve, reject) => {
                // Calculate dynamic timeout for file operations
                let timeout = 10000;  // Default: 10s

                if (command === 'send_file') {
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