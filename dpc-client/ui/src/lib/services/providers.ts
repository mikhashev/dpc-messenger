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

// The provider calls currently waiting out a backoff, by `retry_id`. A map
// rather than one slot because an agent and a chat can be waiting at the same
// time, and a single slot would let either one's closing notice clear the
// other's row. Entries arrive on `provider_retry` and leave on
// `provider_retry_finished`, so a strip bound to this empties itself.
export interface ProviderRetry {
    retry_id: string;        // the handle a cancel is sent with
    provider: string;        // "DeepSeek", "Z.AI", "llama-server"
    alias: string;
    attempt: number;
    waiting_seconds: number;
    elapsed_seconds: number;
    budget_seconds: number;
    error: string;
    unreachable: boolean;    // the connection never opened, vs the service said no
}

export const providerRetries = writable<Map<string, ProviderRetry>>(new Map());
