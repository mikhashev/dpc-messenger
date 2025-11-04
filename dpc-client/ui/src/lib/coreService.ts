// dpc-client/ui/src/lib/coreService.ts

import { writable, get } from 'svelte/store';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);

let socket: WebSocket | null = null;
const API_URL = "ws://127.0.0.1:9999";

// --- THE CORE FIX: Singleton Pattern ---
// This flag prevents multiple connection attempts.
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
        sendCommand("get_status");
    };

    socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        console.log("Received message from Core Service:", message);

        if (message.event) {
            if (message.event === "status_update") {
                nodeStatus.set(message.payload);
            }
        } else if (message.id) {
            // Check if the response is for a get_status command
            // This is a bit brittle, a better way would be to check the original command
            // but for now, we assume any response with a node_id is a status update.
            if (message.payload && message.payload.node_id) {
                nodeStatus.set(message.payload);
            }
        }
    };

    socket.onclose = () => {
        console.log("Disconnected from Core Service.");
        connectionStatus.set('disconnected');
        nodeStatus.set(null);
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
        // If not connected, try to connect first, then send the command.
        // This makes the system more resilient.
        if (!isConnectingOrConnected) {
            connectToCoreService();
        }
        // We can't send the command now, but the connection attempt has started.
        console.error(`Cannot send command '${command}': WebSocket is not connected.`);
        return;
    }
    const message = {
        id: crypto.randomUUID(),
        command,
        payload,
    };
    console.log("Sending command to Core Service:", message);
    socket.send(JSON.stringify(message));
}

// --- Automatically connect on application load ---
// This code runs once when the module is first imported.
if (typeof window !== 'undefined') { // Ensure this only runs in the browser
    connectToCoreService();
}