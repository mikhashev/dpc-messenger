// src/lib/services/connection.ts
// WebSocket lifecycle, sendCommand, and connection/node status stores.
// NOTE: The actual WebSocket connection logic lives in coreService.ts during the
// migration period. This file holds the connection-related stores so they can be
// imported from here by new code, while coreService re-exports them for backward compat.

import { writable } from 'svelte/store';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<any>(null);

// Raw WebSocket message stream (diagnostic / catch-all)
export const coreMessages = writable<any>(null);
