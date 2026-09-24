// src/lib/services/modelDownload.ts
// Generic model-download consent flow (2026-09-24). One event family for any
// model requiring a download (Whisper, the bge-m3 embedding model, ...),
// replacing the old whisper_model_download_* events. See CLAUDE.md /
// model_download_service.py for the backend contract.
//
// Deliberate asymmetry: whisperModelLoading*/whisperModelLoaded stay in
// voice.ts, named per model, because loading is Whisper-specific; the
// download-consent flow below is generic across models by design.

import { writable, get } from 'svelte/store';
import type {
    ModelDownloadRequiredEvent,
    ModelDownloadStartedEvent,
    ModelDownloadCompletedEvent,
    ModelDownloadFailedEvent,
    ModelDownloadStatusEntry,
} from '$lib/types';

export const modelDownloadRequired = writable<ModelDownloadRequiredEvent | null>(null);
export const modelDownloadStarted = writable<ModelDownloadStartedEvent | null>(null);
export const modelDownloadCompleted = writable<ModelDownloadCompletedEvent | null>(null);
export const modelDownloadFailed = writable<ModelDownloadFailedEvent | null>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function downloadModel(modelName: string, providerAlias: string | undefined, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('download_model', { model_name: modelName, provider_alias: providerAlias });
}

export async function declineModelDownload(modelName: string, remember: boolean, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('decline_model_download', { model_name: modelName, remember });
}

export async function resetModelDownloadDecline(modelName: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('reset_model_download_decline', { model_name: modelName });
}

export async function getModelDownloadStatus(sendCmd: SendCommandFn, modelName?: string): Promise<{ status: string; models: Record<string, ModelDownloadStatusEntry> }> {
    return sendCmd('get_model_download_status', modelName ? { model_name: modelName } : {});
}

// --- get_state() for agent introspection (Layer 3 per refactoring guidelines) ---
export function get_state(): Record<string, unknown> {
    return {
        download_required: get(modelDownloadRequired) !== null,
        downloading: get(modelDownloadStarted) !== null,
    };
}
