// src/lib/services/messaging.ts
// P2P direct messages and unread message counts.

import { writable, get } from 'svelte/store';
import type { P2PMessage } from '$lib/types';

export const p2pMessages = writable<P2PMessage | null>(null);
export const unreadMessageCounts = writable<Map<string, number>>(new Map());

// Track currently active chat to prevent unread badges on open chats
let _activeChat: string | null = null;

export function setActiveChat(chatId: string | null) {
    _activeChat = chatId;
}

export function getActiveChat(): string | null {
    return _activeChat;
}

export function resetUnreadCount(peerId: string) {
    const currentCounts = get(unreadMessageCounts);
    if (currentCounts.has(peerId)) {
        currentCounts.delete(peerId);
        unreadMessageCounts.set(new Map(currentCounts));
    }
}

/** Increment unread count for a conversation (skips if it's currently active). */
export function incrementUnread(conversationId: string) {
    if (!conversationId || typeof window === 'undefined') return;
    if (conversationId === _activeChat) return;
    const currentCounts = get(unreadMessageCounts);
    const currentCount = currentCounts.get(conversationId) || 0;
    currentCounts.set(conversationId, currentCount + 1);
    unreadMessageCounts.set(new Map(currentCounts));
}
