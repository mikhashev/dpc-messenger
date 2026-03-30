// src/lib/services/fileTransfer.ts
// File transfer stores and command functions.

import { writable } from 'svelte/store';
import type { FileTransfer } from '$lib/types';

// Incoming file offer / transfer lifecycle
export const fileTransferOffer = writable<any>(null);
export const fileTransferProgress = writable<any>(null);
export const fileTransferComplete = writable<any>(null);
export const fileTransferCancelled = writable<any>(null);

// Active transfers: transfer_id -> FileTransfer
export const activeFileTransfers = writable<Map<string, FileTransfer>>(new Map());

// File preparation progress (hashing/chunking before send)
export const filePreparationStarted = writable<any>(null);
export const filePreparationProgress = writable<any>(null);
export const filePreparationCompleted = writable<any>(null);

// --- Command functions ---
// sendCommand is passed in as a parameter to avoid circular imports.
type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

export async function sendFile(nodeId: string, filePath: string, sendCmd: SendCommandFn): Promise<any> {
    let fileSizeBytes = 0;
    try {
        const { invoke } = await import('@tauri-apps/api/core');
        const metadata = await invoke('get_file_metadata', { path: filePath });
        fileSizeBytes = (metadata as any).size;
    } catch (e) {
        console.warn('Could not get file size for timeout calculation:', e);
    }
    return sendCmd('send_file', {
        node_id: nodeId,
        file_path: filePath,
        file_size_bytes: fileSizeBytes
    });
}

export async function acceptFileTransfer(transferId: string, sendCmd: SendCommandFn): Promise<any> {
    return sendCmd('accept_file_transfer', { transfer_id: transferId });
}

export async function cancelFileTransfer(transferId: string, sendCmd: SendCommandFn, reason: string = 'user_cancelled'): Promise<any> {
    return sendCmd('cancel_file_transfer', { transfer_id: transferId, reason });
}
