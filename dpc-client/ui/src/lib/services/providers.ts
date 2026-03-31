// src/lib/services/providers.ts
// AI provider management: stores and reload triggers.

import { writable } from 'svelte/store';
import type { ProviderInfo, DefaultProvidersResponse, AIResponseWithImageEvent } from '$lib/types';

// Legacy providers list (kept for backward compat) — untyped, deprecated
export const availableProviders = writable<any>(null);

// Dual provider system (text + vision)
export const defaultProviders = writable<DefaultProvidersResponse | null>(null);
export const providersList = writable<ProviderInfo[]>([]);

// Peer node providers: node_id -> provider list
export const peerProviders = writable<Map<string, any[]>>(new Map());

// AI vision response
export const aiResponseWithImage = writable<AIResponseWithImageEvent | null>(null);

// Firewall rules update store — triggers provider list reload in UI.
// Free-form JSON (full privacy_rules.json object), intentionally untyped.
// See CLAUDE.md "UI Integration Pattern for New Firewall Fields" for usage pattern.
export const firewallRulesUpdated = writable<any>(null);
