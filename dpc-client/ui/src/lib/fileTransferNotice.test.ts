import { describe, it, expect } from 'vitest';
import { notOfferedNotice, type GroupSendResult } from './fileTransferNotice';

const SENTENCE = 'Upload limit reached: 3 uploads are open and the limit is 3 ([file_transfer] max_concurrent_transfers in config.ini, read at start). Open now: a.txt to Ann (waiting for an answer). Each stays open until its receiver answers and the transfer ends.';
const names: Record<string, string> = { 'dpc-node-d': 'Dora' };
const nameOf = (id: string) => names[id];

const capped: GroupSendResult = {
    transfer_ids: ['t1', 't2', 't3'],
    not_offered: [
        { node_id: 'dpc-node-d', name: null, reason: 'max_concurrent_transfers' },
        { node_id: 'dpc-node-e', name: 'Erin', reason: 'max_concurrent_transfers' },
    ],
    upload_limit: { limit: 3, sentence: SENTENCE },
};

describe('notOfferedNotice', () => {
    it('says nothing when everyone connected was offered the file', () => {
        expect(notOfferedNotice({ transfer_ids: ['t1'] }, nameOf)).toBeNull();
        expect(notOfferedNotice({ transfer_ids: [], not_offered: [] }, nameOf)).toBeNull();
        expect(notOfferedNotice(null, nameOf)).toBeNull();
        expect(notOfferedNotice(undefined, nameOf)).toBeNull();
    });

    it('names who was left out, how many of how many, and carries the backend sentence once', () => {
        const notice = notOfferedNotice(capped, nameOf)!;
        expect(notice.text).toBe(`Not offered to 2 of 5 connected members: Dora, Erin. ${SENTENCE}`);
        expect(notice.text.split('max_concurrent_transfers').length - 1).toBe(1);
    });

    it('names a member by the UI name first, then the name the backend knew, then the node id', () => {
        const notice = notOfferedNotice({
            transfer_ids: [],
            not_offered: [
                { node_id: 'dpc-node-d', name: 'Backend', reason: 'send_error', detail: 'x' },
                { node_id: 'dpc-node-e', name: 'Erin', reason: 'send_error', detail: 'x' },
                { node_id: 'dpc-node-f', name: null, reason: 'send_error', detail: 'x' },
            ],
        }, nameOf)!;
        expect(notice.text.startsWith('Not offered to 3 of 3 connected members: Dora, Erin, dpc-node-f.')).toBe(true);
    });

    it('gives another error its own reason beside the limit', () => {
        const notice = notOfferedNotice({
            ...capped,
            not_offered: [
                ...capped.not_offered!,
                { node_id: 'dpc-node-f', name: 'Finn', reason: 'send_error', detail: 'Peer closed the connection' },
            ],
        }, nameOf)!;
        expect(notice.text.endsWith(`${SENTENCE} Finn: Peer closed the connection.`)).toBe(true);
        expect(notice.toast).toBe('Not sent to Dora, Erin, Finn: upload limit of 3 reached; Peer closed the connection. Details in the chat.');
    });

    it('does not show the limit sentence when only another error left a member out', () => {
        const notice = notOfferedNotice({
            transfer_ids: ['t1'],
            not_offered: [{ node_id: 'dpc-node-f', name: 'Finn', reason: 'send_error', detail: 'boom' }],
        }, nameOf)!;
        expect(notice.text).toBe('Not offered to 1 of 2 connected members: Finn. Finn: boom.');
        expect(notice.toast).toBe('Not sent to Finn: boom. Details in the chat.');
    });

    it('keeps the toast short for many members', () => {
        const rows = ['a', 'b', 'c', 'd', 'e'].map((x) => ({ node_id: `dpc-node-${x}`, name: x.toUpperCase(), reason: 'max_concurrent_transfers' }));
        const notice = notOfferedNotice({ transfer_ids: [], not_offered: rows, upload_limit: { limit: 3, sentence: SENTENCE } }, () => undefined)!;
        expect(notice.toast).toBe('Not sent to A, B, C and 2 more: upload limit of 3 reached. Details in the chat.');
    });

    it('still warns if an older backend sends the rows without the limit block', () => {
        const notice = notOfferedNotice({ transfer_ids: [], not_offered: [{ node_id: 'dpc-node-d', reason: 'max_concurrent_transfers' }] }, nameOf)!;
        expect(notice.text).toBe('Not offered to 1 of 1 connected members: Dora. Upload limit reached.');
    });
});
