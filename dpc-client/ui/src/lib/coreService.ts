// dpc-client/ui/src/lib/coreService.ts

import { writable, get } from 'svelte/store';

// This store holds the general connection status to the backend service
export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');

// This store holds the full status object received from the backend (node_id, peers, etc.)
export const nodeStatus = writable<any>(null);

// This store is a "firehose" for all messages from the backend, used for command responses
export const coreMessages = writable<any>(null);

// This store is specifically for incoming P2P chat messages
export const p2pMessages = writable<any>(null);

let socket: WebSocket | null = null;
const API_URL = "ws://127.0.0.1:9999";

export function connectToCoreService() {
    // Prevent duplicate connection attempts if one is already in progress or open
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
        console.log("Connection attempt ignored: already connected or connecting.");
        return;
    }

    connectionStatus.set('connecting');
    console.log(`Attempting to connect to Core Service at ${API_URL}...`);

    socket = new WebSocket(API_URL);

    socket.onopen = () => {
        console.log("Successfully connected to Core Service.");
        connectionStatus.set('connected');
        // Automatically request the initial status as soon as we connect
        sendCommand("get_status");
    };

    socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        console.log("Received from Core Service:", message);
        
        // Push ALL messages to the coreMessages store for command-specific handling
        coreMessages.set(message);

        // Handle specific events and update dedicated stores
        if (message.event) {
            if (message.event === "status_update") {
                // By using the spread operator, we create a NEW object.
                // This guarantees that Svelte detects a change and re-renders the UI.
                nodeStatus.set({ ...message.payload });
            }
            else if (message.event === "new_p2p_message") {
                p2pMessages.set(message.payload);
            }
        } 
        // Also update status from direct command responses
        else if (message.id && message.command === "get_status" && message.status === "OK") {
            nodeStatus.set({ ...message.payload });
        }
    };

    socket.onclose = (event) => {
        console.log(`Disconnected from Core Service. Code: ${event.code}, Reason: ${event.reason}`);
        // Only set to 'disconnected' if it wasn't an explicit error.
        if (get(connectionStatus) !== 'error') {
            connectionStatus.set('disconnected');
        }
        nodeStatus.set(null); // Clear the status on disconnect
        socket = null; // CRITICAL: Set socket to null on close to allow reconnection
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        connectionStatus.set('error');
        // The onclose event will be called immediately after, so we don't need to nullify the socket here.
    };
}

export function sendCommand(command: string, payload: any = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        console.error(`Cannot send command '${command}': WebSocket is not connected.`);
        return;
    }
    
    const message = {
        id: crypto.randomUUID(), // Generate a unique ID for each command to track responses
        command,
        payload,
    };

    console.log("Sending command to Core Service:", message);
    socket.send(JSON.stringify(message));
}