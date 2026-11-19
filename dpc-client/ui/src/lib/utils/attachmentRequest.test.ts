import { describe, it, expect } from 'vitest';
import {
    applyRequestStatus, isMissingHere, patchAttachmentsById, requestFromAnswer, requestKey,
    requestStatusText, showsRequestButton, type AttachmentRequest,
} from './attachmentRequest';
import { offerOpensDialog } from '../fileTransferPermission';

const PEER = 'dpc-node-' + 'b'.repeat(32);
const present = { type: 'file' as const, filename: 'song.mp3', size_bytes: 3, status: 'completed',
    file_path: 'C:\\Users\\me\\.dpc\\conversations\\g\\files\\song.mp3' };
const missing = { ...present, file_path: '/home/someone/.dpc/files/song.mp3', available: false };

describe('the Request file button (Mike, 2026-11-19: only beside a file missing here)', () => {
    it('is never drawn beside an attachment whose file is here, whatever a request says', () => {
        expect(isMissingHere(present)).toBe(false);
        expect(showsRequestButton(present)).toBe(false);
        for (const state of ['requesting', 'receiving', 'received', 'not_held', 'no_answer', 'no_peers', 'failed', 'error'] as const) {
            expect(showsRequestButton(present, { state })).toBe(false);
        }
        // An attachment with no field at all (every record before this change) is a present one.
        expect(showsRequestButton({})).toBe(false);
        expect(showsRequestButton({ available: true })).toBe(false);
    });

    it('is drawn beside a missing file until a request is under way, and again when one ends without it', () => {
        expect(isMissingHere(missing)).toBe(true);
        expect(showsRequestButton(missing)).toBe(true);
        expect(showsRequestButton(missing, { state: 'requesting' })).toBe(false);
        expect(showsRequestButton(missing, { state: 'receiving' })).toBe(false);
        for (const state of ['not_held', 'no_answer', 'no_peers', 'failed', 'error'] as const) {
            expect(showsRequestButton(missing, { state })).toBe(true);
        }
    });
});

describe('what the row says', () => {
    it('names each state in plain words', () => {
        expect(requestStatusText(undefined)).toBe('Not on this node');
        expect(requestStatusText({ state: 'requesting' })).toBe('Requesting…');
        expect(requestStatusText({ state: 'receiving' })).toBe('Receiving…');
        expect(requestStatusText({ state: 'receiving' }, 42.6)).toBe('Receiving… 43%');
        expect(requestStatusText({ state: 'not_held' })).toBe('Nobody online has this file');
        expect(requestStatusText({ state: 'no_answer' })).toMatch(/^Nobody online answered/);
        expect(requestStatusText({ state: 'no_peers' })).toBe('No member of this chat is online');
        expect(requestStatusText({ state: 'failed', reason: 'cancelled' })).toBe('The transfer failed: cancelled');
    });

    it('reads the command answer', () => {
        expect(requestFromAnswer({ status: 'requesting', request_id: 'r1' })).toEqual({ state: 'requesting', requestId: 'r1' });
        expect(requestFromAnswer({ status: 'no_peers' }).state).toBe('no_peers');
        expect(requestFromAnswer({ status: 'error', message: 'No message m in this chat' }))
            .toEqual({ state: 'error', reason: 'No message m in this chat' });
        expect(requestFromAnswer(undefined).state).toBe('error');
    });
});

describe('the request state, from core events', () => {
    it('keeps one entry per chat, record and file, and remembers the transfer while it runs', () => {
        let map = new Map<string, AttachmentRequest>();
        const base = { request_id: 'r1', conversation_id: 'group-x', message_id: 'm1', filename: 'song.mp3' };
        map = applyRequestStatus(map, { ...base, state: 'requesting' });
        map = applyRequestStatus(map, { ...base, state: 'receiving', transfer_id: 't1' });
        const key = requestKey('group-x', 'm1', 'song.mp3');
        expect(map.get(key)).toEqual({ state: 'receiving', requestId: 'r1', transferId: 't1', reason: undefined });
        map = applyRequestStatus(map, { ...base, state: 'not_held' });
        expect(map.get(key)?.state).toBe('not_held');
        expect(map.size).toBe(1);
        expect(requestKey('group-x', 'm1', 'other.mp3')).not.toBe(key);
    });

    it('ignores an event that names no record', () => {
        const map = new Map<string, AttachmentRequest>();
        expect(applyRequestStatus(map, { request_id: 'r', conversation_id: '', message_id: 'm', state: 'requesting' })).toBe(map);
    });
});

describe('the arriving file completes the bubble already on screen', () => {
    it('replaces the attachment of the message with that id in place and drops `available`', () => {
        const history = [
            { id: 'm0', text: 'hi' },
            { id: 'm1', text: '', attachments: [missing] },
        ];
        const arrived = { ...present, file_path: '/home/me/.dpc/conversations/g/files/song.mp3', transfer_id: 't1' };
        const next = patchAttachmentsById(history, 'm1', [arrived]);
        expect(next).not.toBeNull();
        expect(next!.length).toBe(2);
        expect(next![1].attachments![0].file_path).toBe(arrived.file_path);
        expect('available' in next![1].attachments![0]).toBe(false);
        expect(showsRequestButton(next![1].attachments![0])).toBe(false);
        expect(history[1].attachments![0].available).toBe(false); // the old history is not mutated
    });

    it('says null for an id that is not on screen, so the caller appends as before', () => {
        expect(patchAttachmentsById([{ id: 'm0' }], 'm9', [present])).toBeNull();
        expect(patchAttachmentsById([{ id: 'm0' }], undefined, [present])).toBeNull();
    });
});

describe('an offer that answers the user\'s own request', () => {
    it('opens no accept dialog, unless the firewall asks', () => {
        expect(offerOpensDialog({ node_id: PEER, request_id: 'r1' })).toBe(false);
        expect(offerOpensDialog({ node_id: PEER, request_id: 'r1', awaiting_permission: true })).toBe(true);
        expect(offerOpensDialog({ node_id: PEER })).toBe(true);
    });
});
