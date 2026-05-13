// src/lib/services/connection.ts
// WebSocket lifecycle, sendCommand, and connection/node status stores.
// NOTE: The actual WebSocket connection logic lives in coreService.ts during the
// migration period. This file holds the connection-related stores so they can be
// imported from here by new code, while coreService re-exports them for backward compat.

import { writable } from 'svelte/store';
import type { NodeStatus } from '$lib/types';

export const connectionStatus = writable<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected');
export const nodeStatus = writable<NodeStatus | null>(null);

// Raw WebSocket message stream (diagnostic / catch-all)
export const coreMessages = writable<Record<string, any> | null>(null);
