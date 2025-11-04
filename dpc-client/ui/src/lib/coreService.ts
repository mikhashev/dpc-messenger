// dpc-client/ui/src/lib/coreService.ts

import { writable, get } from 'svelte/store';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);
export const coreMessages = writable<any>(null);

let socket: WebSocket | null = null;
const API_URL = "ws://127.0.0.1:9999";

let isConnectingOrConnected = false;

export function connectToCoreService() {
    if (isConnectingOrConnected) return;
    isConnectingOrConnected = true;
    connectionStatus.set('connecting');

    socket = new WebSocket(API_URL);

    socket.onopen = () => {
        connectionStatus.set('connected');
        sendCommand("get_status");
    };

    socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        console.log("Received:", message);
        
        // Push ALL messages to the coreMessages store
        coreMessages.set(message);

        // Also update specific stores for convenience
        if (message.event === "status_update" || (message.command === "get_status" && message.status === "OK")) {
            nodeStatus.set(message.payload);
        }
    };

    socket.onclose = () => {
        connectionStatus.set('disconnected');
        nodeStatus.set(null);
        socket = null;
        isConnectingOrConnected = false;
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        connectionStatus.set('error');
        socket = null;
        isConnectingOrConnected = false;
    };
}

export function sendCommand(command: string, payload: any = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        if (!isConnectingOrConnected) connectToCoreService();
        console.error(`Cannot send command '${command}': WebSocket is not connected.`);
        return;
    }
    const message = {
        id: crypto.randomUUID(),
        command,
        payload,
    };
    socket.send(JSON.stringify(message));
}

if (typeof window !== 'undefined') {
    connectToCoreService();
}