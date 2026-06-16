// src/lib/services/providers.ts
// AI provider management: stores and reload triggers.

import { writable } from 'svelte/store';
import type { ProviderInfo, DefaultProvidersResponse, AIResponseWithImageEvent } from '$lib/types';

// Legacy providers list (kept for backward compat) — used with $store.property access, needs any
export const availableProviders = writable<any>(null);

// Dual provider system (text + vision)
export const defaultProviders = writable<DefaultProvidersResponse | null>(null);
export const providersList = writable<ProviderInfo[]>([]);

// Peer node providers: node_id -> provider list
export const peerProviders = writable<Map<string, ProviderInfo[]>>(new Map());

// AI vision response
export const aiResponseWithImage = writable<AIResponseWithImageEvent | null>(null);

// Firewall rules update store — triggers provider list reload in UI.
// Free-form JSON (full privacy_rules.json object).
// See CLAUDE.md "UI Integration Pattern for New Firewall Fields" for usage pattern.
export const firewallRulesUpdated = writable<Record<string, any> | null>(null);

// Pay-per-use provider account balance (DeepSeek /user/balance), populated by
// getProviderBalance() in coreService. Free-form: the backend result dict
// { status: 'success'|'unsupported'|'error', alias?, balance?, message? } where
// balance = { is_available, balance_infos: [{currency, total_balance, ...}] }.
export const providerBalance = writable<any>(null);
