// src/lib/services/groups.ts
// Group chat stores and command functions.

import { writable } from 'svelte/store';
import type {
    GroupChat,
    GroupMessageEvent,
    GroupFileEvent,
    GroupMemberLeftEvent,
    GroupDeletedEvent,
    GroupHistorySyncedEvent,
} from '$lib/types';

// Group state
export const groupChats = writable<Map<string, GroupChat>>(new Map());

// Group events
export const groupTextReceived = writable<GroupMessageEvent | null>(null);
export const groupFileReceived = writable<GroupFileEvent | null>(null);
export const groupInviteReceived = writable<GroupChat | null>(null);
export const groupUpdated = writable<GroupChat | null>(null);
export const groupMemberLeft = writable<GroupMemberLeftEvent | null>(null);
export const groupDeleted = writable<GroupDeletedEvent | null>(null);
export const groupHistorySynced = writable<GroupHistorySyncedEvent | null>(null);

// --- Command functions ---
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function createGroupChat(name: string, sendCmd: SendCommandFn, topic: string = '', memberNodeIds: string[] = []): Promise<any> {
    return sendCmd('create_group_chat', { name, topic, member_node_ids: memberNodeIds });
}

export async function sendGroupMessage(groupId: string, text: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('send_group_message', { group_id: groupId, text });
}

export async function sendGroupImage(groupId: string, imageBase64: string, sendCmd: SendCommandFn, filename?: string, text: string = ''): Promise<any> {
    return sendCmd('send_group_image', { group_id: groupId, image_base64: imageBase64, filename, text });
}

export async function sendGroupVoiceMessage(groupId: string, audioBase64: string, durationSeconds: number, sendCmd: SendCommandFn, mimeType: string = 'audio/webm'): Promise<any> {
    return sendCmd('send_group_voice_message', { group_id: groupId, audio_base64: audioBase64, duration_seconds: durationSeconds, mime_type: mimeType });
}

export async function sendGroupFile(groupId: string, filePath: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('send_group_file', { group_id: groupId, file_path: filePath });
}

export async function addGroupMember(groupId: string, nodeId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('add_group_member', { group_id: groupId, node_id: nodeId });
}

export async function removeGroupMember(groupId: string, nodeId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('remove_group_member', { group_id: groupId, node_id: nodeId });
}

export async function leaveGroup(groupId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('leave_group', { group_id: groupId });
}

export async function deleteGroup(groupId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('delete_group', { group_id: groupId });
}

export async function loadGroups(sendCmd: SendCommandFn): Promise<void> {
    try {
        const result = await sendCmd('get_groups', {});
        if (result && (result as any).status === 'success' && (result as any).groups) {
            const groupMap = new Map<string, GroupChat>();
            for (const group of (result as any).groups) {
                groupMap.set(group.group_id, group);
            }
            groupChats.set(groupMap);
        }
    } catch (e) {
        console.error('Failed to load groups:', e);
    }
}
