// src/lib/services/voice.ts
// Voice messages, transcription, and Whisper model lifecycle.

import { writable, get } from 'svelte/store';

// Voice message incoming
export const voiceOfferReceived = writable<any>(null);

// Transcription events
export const voiceTranscriptionReceived = writable<any>(null);
export const voiceTranscriptionComplete = writable<any>(null);
export const voiceTranscriptionConfig = writable<any>(null);

// Whisper model lifecycle
export const whisperModelLoadingStarted = writable<any>(null);
export const whisperModelLoaded = writable<any>(null);
export const whisperModelLoadingFailed = writable<any>(null);
export const whisperModelUnloaded = writable<any>(null);

// Whisper model download
export const whisperModelDownloadRequired = writable<any>(null);
export const whisperModelDownloadStarted = writable<any>(null);
export const whisperModelDownloadCompleted = writable<any>(null);
export const whisperModelDownloadFailed = writable<any>(null);

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
