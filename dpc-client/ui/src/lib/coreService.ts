// dpc-client/ui/src/lib/coreService.ts

import { writable } from 'svelte/store';

// This store will hold the connection status
export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');

// This store will hold the last received status from the backend
export const nodeStatus = writable<any>(null);

let socket: WebSocket | null = null;

const API_URL = "ws://127.0.0.1:9999";

export function connectToCoreService() {
    if (socket && socket.readyState === WebSocket.OPEN) {
        console.log("Already connected.");
        return;
    }

    connectionStatus.set('connecting');
    console.log(`Attempting to connect to Core Service at ${API_URL}...`);

    socket = new WebSocket(API_URL);

    socket.onopen = () => {
        console.log("Successfully connected to Core Service.");
        connectionStatus.set('connected');
        // Automatically request status on connect
        sendCommand("get_status");
    };

    socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        console.log("Received message from Core Service:", message);

        if (message.event) {
            // Handle broadcast events
            if (message.event === "peer_status_changed") {
                // In the future, update a peer list store
            }
        } else if (message.id) {
            // Handle responses to our commands
            if (message.payload && message.payload.node_id) {
                nodeStatus.set(message.payload);
            }
        }
    };

    socket.onclose = () => {
        console.log("Disconnected from Core Service.");
        connectionStatus.set('disconnected');
        socket = null;
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        connectionStatus.set('error');
        socket = null;
    };
}

// A simple function to send commands
export function sendCommand(command: string, payload: any = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        console.error("Cannot send command: WebSocket is not connected.");
        return;
    }
    const message = {
        id: crypto.randomUUID(), // Generate a unique ID for each command
        command,
        payload,
    };
    console.log("Sending command to Core Service:", message);
    socket.send(JSON.stringify(message));
}