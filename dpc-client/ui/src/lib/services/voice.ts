// src/lib/services/voice.ts
// Voice messages, transcription, and Whisper model lifecycle.

import { writable, get } from 'svelte/store';
import type { VoiceTranscription, FileTransferOfferEvent, WhisperModelEvent, WhisperModelFailedEvent } from '$lib/types';

// Voice message incoming (voice_offer_received event — file transfer with voice_metadata)
export const voiceOfferReceived = writable<FileTransferOfferEvent | null>(null);

// Transcription events
export const voiceTranscriptionReceived = writable<VoiceTranscription | null>(null);
export const voiceTranscriptionComplete = writable<VoiceTranscription | null>(null);
// Voice transcription config — free-form config object, intentionally untyped
export const voiceTranscriptionConfig = writable<any>(null);

// Whisper model lifecycle
export const whisperModelLoadingStarted = writable<WhisperModelEvent | null>(null);
export const whisperModelLoaded = writable<WhisperModelEvent | null>(null);
export const whisperModelLoadingFailed = writable<WhisperModelFailedEvent | null>(null);
export const whisperModelUnloaded = writable<WhisperModelEvent | null>(null);

// Whisper model download
export const whisperModelDownloadRequired = writable<WhisperModelEvent | null>(null);
export const whisperModelDownloadStarted = writable<WhisperModelEvent | null>(null);
export const whisperModelDownloadCompleted = writable<WhisperModelEvent | null>(null);
export const whisperModelDownloadFailed = writable<WhisperModelFailedEvent | null>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function sendVoiceMessage(
    nodeId: string,
    audioBlob: Blob,
    durationSeconds: number,
    sendCmd: SendCommandFn
): Promise<any> {
    const arrayBuffer = await audioBlob.arrayBuffer();
    const base64 = btoa(
        new Uint8Array(arrayBuffer).reduce(
            (data, byte) => data + String.fromCharCode(byte),
            ''
        )
    );
    return sendCmd('send_voice_message', {
        node_id: nodeId,
        audio_base64: base64,
        duration_seconds: durationSeconds,
        mime_type: audioBlob.type || 'audio/webm'
    });
}

export async function getVoiceTranscriptionConfig(sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('get_voice_transcription_config', {});
}

export async function saveVoiceTranscriptionConfig(config: any, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('save_voice_transcription_config', { config });
}

export async function setConversationTranscription(nodeId: string, enabled: boolean, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('set_conversation_transcription', { node_id: nodeId, enabled });
}

export async function getConversationTranscription(nodeId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('get_conversation_transcription', { node_id: nodeId });
}

export async function preloadWhisperModel(sendCmd: SendCommandFn, providerAlias?: string): Promise<any> {
    return sendCmd('preload_whisper_model', { provider_alias: providerAlias });
}

// --- get_state() for agent introspection (Layer 3 per refactoring guidelines) ---
export function get_state(): Record<string, unknown> {
    return {
        whisper_loaded: get(whisperModelLoaded) !== null,
        whisper_loading: get(whisperModelLoadingStarted) !== null,
        auto_transcribe: get(voiceTranscriptionConfig) !== null,
        download_required: get(whisperModelDownloadRequired) !== null,
    };
}
