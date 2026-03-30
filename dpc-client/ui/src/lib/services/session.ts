// src/lib/services/session.ts
// Session management, conversation history restore, and per-conversation settings.

import { writable } from 'svelte/store';

// Chat history restore on reconnect
export const historyRestored = writable<any>(null);

// Mutual session approval (v0.11.3)
export const newSessionProposal = writable<any>(null);
export const newSessionResult = writable<any>(null);

// Conversation lifecycle
export const conversationReset = writable<any>(null);
export const conversationSettings = writable<any>(null);
export const conversationSettingsChanged = writable<any>(null);
export const conversationDeleted = writable<any>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function proposeNewSession(conversationId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('propose_new_session', { conversation_id: conversationId });
}

export async function voteNewSession(proposalId: string, vote: boolean, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('vote_new_session', { proposal_id: proposalId, vote });
}

export async function getConversationSettings(conversationId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('get_conversation_settings', { conversation_id: conversationId });
}

export async function setConversationPersistHistory(conversationId: string, persist: boolean, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('set_conversation_persist_history', { conversation_id: conversationId, persist });
}

export async function deleteConversation(conversationId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('delete_conversation', { conversation_id: conversationId });
}
