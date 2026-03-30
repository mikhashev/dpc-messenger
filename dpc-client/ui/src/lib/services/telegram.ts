// src/lib/services/telegram.ts
// Telegram bot integration stores and command functions.

import { writable, get } from 'svelte/store';

// Connection state
export const telegramEnabled = writable<boolean>(false);
export const telegramConnected = writable<boolean>(false);
export const telegramStatus = writable<any>(null);
export const telegramError = writable<{ title: string; message: string; timestamp: string } | null>(null);

// Conversation linking: conversation_id -> telegram_chat_id
export const telegramLinkedChats = writable<Map<string, string>>(new Map());

// Message store: conversation_id -> messages[]
export const telegramMessages = writable<Map<string, any[]>>(new Map());

// Incoming event stores
export const telegramMessageReceived = writable<any>(null);
export const telegramVoiceReceived = writable<any>(null);
export const telegramImageReceived = writable<any>(null);
export const telegramFileReceived = writable<any>(null);

// Agent <-> Telegram linking events (v0.15.0+)
export const agentTelegramLinked = writable<any>(null);
export const agentTelegramUnlinked = writable<any>(null);

// Silent history refresh from Telegram bridge (unified_conversation mode)
export const agentHistoryUpdated = writable<any>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function sendToTelegram(
    conversationId: string,
    text: string,
    sendCmd: SendCommandFn,
    attachments?: any[],
    voiceAudioBase64?: string,
    voiceDurationSeconds?: number,
    voiceMimeType?: string,
    filePath?: string,
): Promise<any> {
    const payload: any = {
        conversation_id: conversationId,
        text,
        attachments: attachments || [],
    };
    if (voiceAudioBase64 !== undefined) payload.voice_audio_base64 = voiceAudioBase64;
    if (voiceDurationSeconds !== undefined) payload.voice_duration_seconds = voiceDurationSeconds;
    if (voiceMimeType !== undefined) payload.voice_mime_type = voiceMimeType;
    if (filePath !== undefined) payload.file_path = filePath;
    return sendCmd('send_to_telegram', payload);
}

export async function linkTelegramChat(conversationId: string, telegramChatId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('link_telegram_chat', { conversation_id: conversationId, telegram_chat_id: telegramChatId });
}

export async function getTelegramStatus(sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('get_telegram_status', {});
}

// --- get_state() for agent introspection (Layer 3 per refactoring guidelines) ---
export function get_state(): Record<string, unknown> {
    return {
        enabled: get(telegramEnabled),
        connected: get(telegramConnected),
        linked_chat_count: get(telegramLinkedChats).size,
        has_error: get(telegramError) !== null,
    };
}
