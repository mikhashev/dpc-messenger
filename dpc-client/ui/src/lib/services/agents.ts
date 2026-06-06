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
// Authoritative per-conversation tool-call snapshot (conversation_id -> tool_calls[]).
// Populated from agent_progress.tool_calls so the live collapsible renders the full
// list directly instead of accumulating a lossy event stream (no dropped results,
// survives chat switches).
export const agentLiveTools = writable<Record<string, any[]>>({});
export const agentTextChunk = writable<AgentTextChunkEvent | null>(null);

// CC agent chat message (injected by CC via send_cc_agent_response)
export const agentChatMessage = writable<Record<string, any> | null>(null);

// Backend confirms user message with msg_index before LLM starts thinking
export const userMessageConfirmed = writable<{ conversation_id: string; msg_index: number; command_id: string } | null>(null);

// Sleep state (ADR-014)
export const sleepStateChanged = writable<{ agent_id: string; group_id?: string; status: string; result?: string } | null>(null);
export const sleepProgress = writable<{ agent_id: string; group_id?: string; current: number; total: number; phase: string } | null>(null);
export type SleepAgentState = { agent_id: string; agent_name?: string; origin_chat_id?: string; status: string; current: number; total: number; phase: string };
export const sleepAgentStates = writable<Map<string, SleepAgentState>>(new Map());

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
    retrievalVector?: string,
    retrievalText?: string,
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
        ...(retrievalVector ? { retrieval_vector: retrievalVector } : {}),
        ...(retrievalText ? { retrieval_text: retrievalText } : {}),
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
