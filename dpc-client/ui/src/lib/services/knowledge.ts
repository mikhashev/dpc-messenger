// src/lib/services/knowledge.ts
// Personal context, knowledge commits, firewall, and integrity stores.

import { writable } from 'svelte/store';
import type {
    KnowledgeCommit,
    KnowledgeCommitProposal,
    KnowledgeCommitResultEvent,
    ContextUpdatedEvent,
    TokenWarningEvent,
    ExtractionFailureEvent,
} from '$lib/types';

// Personal context (loaded on connect) — bound to ContextViewer's local PersonalContext type
export const personalContext = writable<any>(null);

// Context update indicators (for "UPDATED" badge in UI)
export const contextUpdated = writable<ContextUpdatedEvent | null>(null);
export const peerContextUpdated = writable<ContextUpdatedEvent | null>(null);

// Knowledge commit voting — full proposal payload from knowledge_commit_proposed event
export const knowledgeCommitProposal = writable<KnowledgeCommitProposal | null>(null);
export const knowledgeCommitResult = writable<KnowledgeCommitResultEvent | null>(null);

// Knowledge extraction failure (Phase 4)
export const extractionFailure = writable<ExtractionFailureEvent | null>(null);

// Token limit warning (Phase 2)
export const tokenWarning = writable<TokenWarningEvent | null>(null);

// Knowledge integrity warnings (v0.19.2 - startup tamper/corruption detection)
// Payload: { count: number, warnings: Array<{file, type, severity, message, ...}>, dismissed: boolean }
export const integrityWarnings = writable<{ count: number; warnings: any[]; dismissed: boolean } | null>(null);
