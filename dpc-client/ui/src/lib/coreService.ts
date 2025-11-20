// dpc-client/ui/src/lib/coreService.ts
// PRODUCTION VERSION - Clean, no excessive logging

import { writable, get } from 'svelte/store';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);
export const coreMessages = writable<any>(null);
export const p2pMessages = writable<any>(null);

// Knowledge Architecture stores (Phase 1-6 integration)
export const knowledgeCommitProposal = writable<any>(null);
export const personalContext = writable<any>(null);

// AI Providers store
export const availableProviders = writable<any>(null);

// Peer Providers store (node_id -> provider list)
export const peerProviders = writable<Map<string, any[]>>(new Map());

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
            'get_firewall_rules',
            'save_firewall_rules',
            'reload_firewall',
            'validate_firewall_rules'
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