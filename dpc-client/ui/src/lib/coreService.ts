// dpc-client/ui/src/lib/coreService.ts

import { writable, get } from 'svelte/store';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);
export const coreMessages = writable<any>(null);
export const p2pMessages = writable<any>(null);

let socket: WebSocket | null = null;
const API_URL = "ws://127.0.0.1:9999";

export function connectToCoreService() {
    if (socket && socket.readyState === WebSocket.CONNECTING) {
        return; // Don't try to connect if already connecting
    }

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
        coreMessages.set(message);

        if (message.event === "status_update" || (message.id && message.command === "get_status" && message.status === "OK")) {
            nodeStatus.set({ ...message.payload });
        } else if (message.event === "new_p2p_message") {
            p2pMessages.set(message.payload);
        }
    };

    socket.onclose = () => {
        console.log("Disconnected from Core Service.");
        // Only set to 'disconnected' if it wasn't an error.
        if (get(connectionStatus) !== 'error') {
            connectionStatus.set('disconnected');
        }
        nodeStatus.set(null);
        socket = null;
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        connectionStatus.set('error');
    };
}

export function sendCommand(command: string, payload: any = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        console.error(`Cannot send command '${command}': WebSocket is not connected.`);
        return;
    }
    const message = { id: crypto.randomUUID(), command, payload };
    socket.send(JSON.stringify(message));
}

// We will no longer connect automatically. The UI component will do it.