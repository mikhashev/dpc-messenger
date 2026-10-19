/**
 * The UI's wait for a file-sending command, and the chat line for a refusal.
 *
 * A 47 GB file sent to a group ran on the 60 s default timeout, because only
 * `send_file` scaled with size; `send_group_file` and `send_group_image` did not.
 */

import { describe, it, expect } from 'vitest';
import { fileSendTimeoutMs, fileSendTimeoutFor, dataUrlBytes } from './fileSendTimeout';
import { cancelReasonText, refusalNotice } from './fileTransferNotice';

const GB = 1024 * 1024 * 1024;

describe('fileSendTimeoutMs', () => {
    it('keeps the formula send_file always had: 60 s + 40 s per GB + 20 s', () => {
        expect(fileSendTimeoutMs(0)).toBe(80_000);
        expect(fileSendTimeoutMs(1 * GB)).toBe(120_000);
        expect(fileSendTimeoutMs(10 * GB)).toBe(480_000);
    });

    it('scales a 47 GB file well past the 60 s default', () => {
        expect(fileSendTimeoutMs(47 * GB)).toBe(80_000 + 47 * 40_000);
    });

    it('treats a missing or nonsense size as zero', () => {
        expect(fileSendTimeoutMs(NaN)).toBe(80_000);
        expect(fileSendTimeoutMs(-5)).toBe(80_000);
    });
});

describe('fileSendTimeoutFor', () => {
    it('scales send_file and send_group_file by file_size_bytes', () => {
        expect(fileSendTimeoutFor('send_file', { file_size_bytes: 5 * GB })).toBe(280_000);
        expect(fileSendTimeoutFor('send_group_file', { file_size_bytes: 5 * GB })).toBe(280_000);
    });

    it('sizes send_group_image from the image it carries', () => {
        const oneMb = 'A'.repeat(Math.ceil((1024 * 1024 * 4) / 3));
        const t = fileSendTimeoutFor('send_group_image', { image_base64: `data:image/png;base64,${oneMb}` });
        expect(t).toBeGreaterThanOrEqual(80_000);
        expect(t).toBeLessThan(81_000);
    });

    it('leaves every other command to its own timeout', () => {
        expect(fileSendTimeoutFor('send_group_message', {})).toBeNull();
        expect(fileSendTimeoutFor('send_p2p_image', {})).toBeNull();
        expect(fileSendTimeoutFor('connect_to_peer', {})).toBeNull();
    });

    it('gives a group file with no measured size the 80 s floor, not the 60 s default', () => {
        expect(fileSendTimeoutFor('send_group_file', { group_id: 'g', file_path: 'x' })).toBe(80_000);
    });
});

describe('dataUrlBytes', () => {
    it('counts decoded bytes, padding included', () => {
        expect(dataUrlBytes('data:text/plain;base64,aGVsbG8=')).toBe(5);
        expect(dataUrlBytes('aGVsbG8=')).toBe(5);
        expect(dataUrlBytes('data:x;base64,YWJj')).toBe(3);
    });

    it('is zero for anything that is not a string', () => {
        expect(dataUrlBytes(undefined)).toBe(0);
        expect(dataUrlBytes(42)).toBe(0);
    });
});

describe('a refused upload', () => {
    const base = { transfer_id: 't1', reason: 'firewall_denied', filename: 'big.iso', node_id: 'dpc-node-b', direction: 'upload' };
    const names = (id: string) => (id === 'dpc-node-b' ? 'Boris' : undefined);

    it('puts the receiver sentence behind the reason', () => {
        expect(cancelReasonText({ ...base, denial_reason: 'File too large: 900.0 MB exceeds 100 MB limit' }))
            .toBe('firewall_denied: File too large: 900.0 MB exceeds 100 MB limit');
    });

    it('shows the bare reason for an older peer that sent no sentence', () => {
        expect(cancelReasonText(base)).toBe('firewall_denied');
        expect(refusalNotice(base, names)).toBeNull();
    });

    it('lands a chat line in the peer chat, naming the peer and the sentence', () => {
        const n = refusalNotice({ ...base, denial_reason: 'File transfer not permitted by firewall rules' }, names);
        expect(n).toEqual({
            chatId: 'dpc-node-b',
            text: 'Not sent: Boris refused big.iso (File transfer not permitted by firewall rules)',
        });
    });

    it('lands in the group chat when the transfer belonged to a group send', () => {
        const n = refusalNotice({ ...base, group_id: 'group-x', denial_reason: 'nope' }, names);
        expect(n?.chatId).toBe('group-x');
    });

    it('adds nothing for a download or a local cancel', () => {
        expect(refusalNotice({ ...base, direction: 'download', denial_reason: 'x' }, names)).toBeNull();
        expect(refusalNotice({ ...base, reason: 'user_cancelled' }, names)).toBeNull();
    });
});
