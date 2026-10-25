import { describe, expect, it } from 'vitest';
import {
    PROVED_CONNECTION_DEFAULT,
    PROVED_CONNECTION_KEY,
    offerOpensDialog,
    offerQuestion,
    provedConnectionEntry,
    setProvedConnectionRule,
} from './fileTransferPermission';

const PEER = 'dpc-node-' + 'b'.repeat(32);

describe('an offer from a sender no rule names', () => {
    it('opens the dialog even for a group file, which otherwise only toasts', () => {
        expect(offerOpensDialog({ node_id: PEER, group_id: 'group-x', awaiting_permission: true })).toBe(true);
        expect(offerOpensDialog({ node_id: PEER, group_id: 'group-x' })).toBe(false);
        expect(offerOpensDialog({ node_id: PEER })).toBe(true);
    });

    it('asks by the sender name and says what is coming', () => {
        const q = offerQuestion({ node_id: PEER, sender_name: 'Ann', kind: 'voice', awaiting_permission: true });
        expect(q?.title).toMatch(/^Allow files from Ann \(dpc-node-/);
        expect(q?.detail).toContain('a voice message');
        expect(q?.detail).toContain('not on a direct connection');
    });

    it('names a screenshot and a group', () => {
        const q = offerQuestion({ node_id: PEER, kind: 'image', group_id: 'g', awaiting_permission: true });
        expect(q?.detail).toContain('a screenshot in a group chat');
    });

    it('asks nothing about an ordinary offer', () => {
        expect(offerQuestion({ node_id: PEER, sender_name: 'Ann' })).toBeNull();
    });
});

describe('the direct-connection entry in the File Transfer tab', () => {
    it('shows the default while the file has no entry', () => {
        const shown = provedConnectionEntry({ file_transfer: { nodes: {}, groups: {} } });
        expect(shown.isDefault).toBe(true);
        expect(shown.entry['file_transfer.allow']).toBe('allow');
        expect(shown.entry['file_transfer.max_size_mb']).toBe(1000);
    });

    it('an edit writes the whole entry and leaves the default untouched', () => {
        const rules: any = { file_transfer: { nodes: {}, groups: {} } };
        setProvedConnectionRule(rules, 'file_transfer.allow', 'deny');
        expect(rules.file_transfer[PROVED_CONNECTION_KEY]).toEqual({
            'file_transfer.allow': 'deny',
            'file_transfer.max_size_mb': 1000,
            'file_transfer.allowed_mime_types': ['*'],
        });
        expect(PROVED_CONNECTION_DEFAULT['file_transfer.allow']).toBe('allow');
        rules.file_transfer[PROVED_CONNECTION_KEY]['file_transfer.allowed_mime_types'].push('x');
        expect(PROVED_CONNECTION_DEFAULT['file_transfer.allowed_mime_types']).toEqual(['*']);
    });

    it('an edit of an existing entry changes only that key', () => {
        const rules: any = { file_transfer: { [PROVED_CONNECTION_KEY]: { 'file_transfer.allow': 'allow', 'file_transfer.max_size_mb': 5 } } };
        setProvedConnectionRule(rules, 'file_transfer.max_size_mb', 50);
        expect(rules.file_transfer[PROVED_CONNECTION_KEY]).toEqual({ 'file_transfer.allow': 'allow', 'file_transfer.max_size_mb': 50 });
        expect(provedConnectionEntry(rules).isDefault).toBe(false);
    });

    it('a rules file with no file_transfer section gets one', () => {
        const rules: any = {};
        setProvedConnectionRule(rules, 'file_transfer.allow', 'deny');
        expect(rules.file_transfer[PROVED_CONNECTION_KEY]['file_transfer.allow']).toBe('deny');
    });
});
