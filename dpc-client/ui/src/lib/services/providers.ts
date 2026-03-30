// src/lib/services/providers.ts
// AI provider management: stores and reload triggers.

import { writable } from 'svelte/store';
import type { ProviderInfo, DefaultProvidersResponse } from '$lib/types';

// Legacy providers list (kept for backward compat)
export const availableProviders = writable<any>(null);

// Dual provider system (text + vision)
export const defaultProviders = writable<DefaultProvidersResponse | null>(null);
export const providersList = writable<ProviderInfo[]>([]);

// Peer node providers: node_id -> provider list
export const peerProviders = writable<Map<string, any[]>>(new Map());

// AI vision response
export const aiResponseWithImage = writable<any>(null);

// Firewall rules update store — triggers provider list reload in UI.
// See CLAUDE.md "UI Integration Pattern for New Firewall Fields" for usage pattern.
export const firewallRulesUpdated = writable<any>(null);
