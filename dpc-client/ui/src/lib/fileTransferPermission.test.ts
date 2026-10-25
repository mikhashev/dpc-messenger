import { describe, expect, it } from 'vitest';
import {
    PROVED_CONNECTION_ALLOW_VALUES,
    PROVED_CONNECTION_DEFAULT,
    PROVED_CONNECTION_KEY,
    alwaysAllowLabel,
    alwaysAllowLimits,
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
        expect(q?.detail).toContain('No File Transfer rule names them');
        expect(q?.detail).not.toContain('direct connection');
    });

    it('names a screenshot and a group', () => {
        const q = offerQuestion({ node_id: PEER, kind: 'image', group_id: 'g', awaiting_permission: true });
        expect(q?.detail).toContain('a screenshot in a group chat');
    });

    it('names the author and the hop for a group file that came through another node', () => {
        const AUTHOR = 'dpc-node-' + 'c'.repeat(32);
        const offer = { node_id: PEER, sender_name: 'Bob', sender_node_id: AUTHOR, kind: 'file', group_id: 'g', awaiting_permission: true };
        const q = offerQuestion(offer, (id) => (id === AUTHOR ? 'Cara' : undefined));
        expect(q?.title).toMatch(/^Allow files from Cara \(dpc-node-c+.*\) via Bob \(dpc-node-b/);
        expect(q?.detail).toContain('not signed');
        expect(q?.detail).toContain('"Always allow" would remember Bob (dpc-node-');
        expect(q?.detail).toContain('not Cara');
    });

    it('names no second node when the author is the hop, or the offer is not a group file', () => {
        const same = offerQuestion({ node_id: PEER, sender_node_id: PEER, group_id: 'g', awaiting_permission: true });
        expect(same?.title).not.toContain(' via ');
        const notGroup = offerQuestion({ node_id: PEER, sender_node_id: 'dpc-node-x', awaiting_permission: true });
        expect(notGroup?.title).not.toContain(' via ');
    });

    it('asks nothing about an ordinary offer', () => {
        expect(offerQuestion({ node_id: PEER, sender_name: 'Ann' })).toBeNull();
    });
});

describe('the direct-connection entry in the File Transfer tab', () => {
    it('shows the default, ask, while the file has no entry', () => {
        const shown = provedConnectionEntry({ file_transfer: { nodes: {}, groups: {} } });
        expect(shown.isDefault).toBe(true);
        expect(shown.entry['file_transfer.allow']).toBe('ask');
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
        expect(PROVED_CONNECTION_DEFAULT['file_transfer.allow']).toBe('ask');
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

describe('the direct-connection entry offers allow, ask and deny', () => {
    it('lists the three values, ask among them', () => {
        expect([...PROVED_CONNECTION_ALLOW_VALUES]).toEqual(['allow', 'ask', 'deny']);
    });
});

describe('the "Always allow" box says what it writes', () => {
    const offer = { node_id: PEER, sender_name: 'Ann', awaiting_permission: true };

    it('names the size, the types and where to revoke, from the default entry', () => {
        const text = alwaysAllowLabel(offer, alwaysAllowLimits({ file_transfer: {} }));
        expect(text).toContain('Always allow files from Ann (dpc-node-');
        expect(text).toContain(': allow, up to 1000 MB, any type.');
        expect(text).toContain('Revoke in Firewall \u2192 File Transfer');
    });

    it("reads the owner's own limits", () => {
        const rules = { file_transfer: { [PROVED_CONNECTION_KEY]: {
            'file_transfer.allow': 'ask', 'file_transfer.max_size_mb': 50, 'file_transfer.allowed_mime_types': ['image/*', 'audio/*'],
        } } };
        expect(alwaysAllowLabel(offer, alwaysAllowLimits(rules))).toContain(': allow, up to 50 MB, image/*, audio/*.');
    });

    it('says any size when the entry has none, as the node entry it writes would', () => {
        const rules = { file_transfer: { [PROVED_CONNECTION_KEY]: { 'file_transfer.allow': 'deny', 'file_transfer.max_size_mb': null } } };
        expect(alwaysAllowLabel(offer, alwaysAllowLimits(rules))).toContain(': allow, any size, any type.');
    });

    it('says the box remembers the node a group file came through', () => {
        const via = { node_id: PEER, sender_name: 'Bob', sender_node_id: 'dpc-node-x', group_id: 'g', awaiting_permission: true };
        expect(alwaysAllowLabel(via, alwaysAllowLimits(null))).toContain(', the node it came through:');
    });
});
