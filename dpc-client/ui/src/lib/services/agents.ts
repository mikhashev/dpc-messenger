// src/lib/services/agents.ts
// DPC Agent management stores and command functions.

import { writable, get } from 'svelte/store';
import type {
    AgentInfo,
    AgentProgressEvent,
    AgentProgressClearEvent,
    AgentTextChunkEvent,
} from '$lib/types';

// Agent state
export const agentsList = writable<AgentInfo[]>([]);
export const agentCreated = writable<AgentInfo | null>(null);
export const agentUpdated = writable<AgentInfo | null>(null);
export const agentDeleted = writable<{ agent_id: string } | null>(null);
export const agentProfiles = writable<string[]>([]);

// Agent execution / streaming
export const agentProgress = writable<AgentProgressEvent | null>(null);
export const agentProgressClear = writable<AgentProgressClearEvent | null>(null);
export const agentTextChunk = writable<AgentTextChunkEvent | null>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function createAgent(
    name: string,
    sendCmd: SendCommandFn,
    providerAlias: string = 'dpc_agent',
    profileName: string = 'default',
    instructionSetName: string = 'general',
    budgetUsd: number = 50.0,
    maxRounds: number = 200,
    computeHost?: string,
    contextWindow?: number,
): Promise<any> {
    return sendCmd('create_agent', {
        name,
        provider_alias: providerAlias,
        profile_name: profileName,
        instruction_set_name: instructionSetName,
        budget_usd: budgetUsd,
        max_rounds: maxRounds,
        ...(computeHost ? { compute_host: computeHost } : {}),
        ...(contextWindow ? { context_window: contextWindow } : {}),
    });
}

export async function listAgents(sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('list_agents', {});
}

export async function getAgentConfig(agentId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('get_agent_config', { agent_id: agentId });
}

export async function updateAgentConfig(agentId: string, updates: Record<string, any>, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('update_agent_config', { agent_id: agentId, updates });
}

export async function deleteAgent(agentId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('delete_agent', { agent_id: agentId });
}

export async function listAgentProfiles(sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('list_agent_profiles', {});
}

// --- get_state() for agent introspection (Layer 3 per refactoring guidelines) ---
export function get_state(): Record<string, unknown> {
    return {
        agents: get(agentsList),
        agent_count: get(agentsList).length,
        profiles: get(agentProfiles),
        streaming_active: get(agentTextChunk) !== null,
        progress_active: get(agentProgress) !== null,
    };
}
