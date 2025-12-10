// dpc-client/ui/src/lib/coreService.ts
// PRODUCTION VERSION - Clean, no excessive logging

import { writable, get } from 'svelte/store';

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

// AI Providers store
export const availableProviders = writable<any>(null);

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
const pendingCommands = new Map<string, { resolve: (value: any) => void; reject: (reason: any) => void }>();

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
                    const { resolve } = pendingCommands.get(message.id)!;
                    pendingCommands.delete(message.id);
                    resolve(message.payload);
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
                // Handle list_providers response
                else if (message.command === "list_providers" && message.status === "OK") {
                    console.log("Available providers loaded:", message.payload);
                    availableProviders.set(message.payload);
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
            'query_ollama_model_info',
            'toggle_auto_knowledge_detection'
        ].includes(command);

        if (expectsResponse) {
            return new Promise((resolve, reject) => {
                // Store the promise callbacks
                pendingCommands.set(id, { resolve, reject });

                // Set timeout to reject if no response
                setTimeout(() => {
                    if (pendingCommands.has(id)) {
                        pendingCommands.delete(id);
                        reject(new Error(`Command '${command}' timed out after 10 seconds`));
                    }
                }, 10000);

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

// Helper function to reset unread count when chat becomes active (v0.9.3)
export function resetUnreadCount(peerId: string) {
    const currentCounts = get(unreadMessageCounts);
    if (currentCounts.has(peerId)) {
        currentCounts.delete(peerId);
        unreadMessageCounts.set(new Map(currentCounts));
    }
}