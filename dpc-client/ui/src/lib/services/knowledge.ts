// src/lib/services/knowledge.ts
// Personal context, knowledge commits, firewall, and integrity stores.

import { writable } from 'svelte/store';

// Personal context (loaded on connect)
export const personalContext = writable<any>(null);

// Context update indicators (for "UPDATED" badge in UI)
export const contextUpdated = writable<any>(null);
export const peerContextUpdated = writable<any>(null);

// Knowledge commit voting
export const knowledgeCommitProposal = writable<any>(null);
export const knowledgeCommitResult = writable<any>(null);

// Knowledge extraction failure (Phase 4)
export const extractionFailure = writable<any>(null);

// Token limit warning (Phase 2)
export const tokenWarning = writable<any>(null);

// Knowledge integrity warnings (v0.19.2 - startup tamper/corruption detection)
// Payload: { count: number, warnings: Array<{file, type, severity, message, ...}>, dismissed: boolean }
export const integrityWarnings = writable<{ count: number; warnings: any[]; dismissed: boolean } | null>(null);
