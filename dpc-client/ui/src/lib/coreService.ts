// dpc-client/ui/src/lib/coreService.ts

import { writable } from 'svelte/store';

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

// Singleton flag to prevent multiple connection attempts from HMR or component re-renders
let isConnectingOrConnected = false;

export function connectToCoreService() {
    // If we are already connected or in the process of connecting, do nothing.
    if (isConnectingOrConnected) {
        return;
    }
    isConnectingOrConnected = true;

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
                // --- THE CORE REACTIVITY FIX ---
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

    socket.onclose = () => {
        console.log("Disconnected from Core Service.");
        connectionStatus.set('disconnected');
        nodeStatus.set(null); // Clear the status on disconnect
        socket = null;
        isConnectingOrConnected = false; // Allow reconnection attempts
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        connectionStatus.set('error');
        socket = null;
        isConnectingOrConnected = false; // Allow reconnection attempts
    };
}

export function sendCommand(command: string, payload: any = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        // If not connected, try to connect first.
        if (!isConnectingOrConnected) {
            connectToCoreService();
        }
        console.error(`Cannot send command '${command}': WebSocket is not connected.`);
        // The command will fail, but a reconnection attempt has been started.
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

// --- Automatically connect on application load ---
// This code runs once when the module is first imported in the browser.
if (typeof window !== 'undefined') {
    connectToCoreService();
}