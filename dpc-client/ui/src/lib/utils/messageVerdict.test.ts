/**
 * verdictNote (messageVerdict.ts): the words a group record's signature verdict
 * turns into on a message row (DPC #111). The classes are the owner's split:
 * nothing for a record that checked out, for one that predates verdicts, or
 * for this node's own records; a quiet ask for a certificate that has not
 * arrived; a quiet age note for the pre-signature format. A verdict this UI
 * does not know draws nothing too: a note is drawn only for a verdict whose
 * meaning is known to be "not checked", so a word a later step stores for
 * this node's own records is never shown as unsigned (CC #122).
 */

import { describe, it, expect } from 'vitest';
import { verdictNote } from './messageVerdict';
import { mapBackendMessage } from './messageMapper';

describe('verdictNote', () => {
    it.each([
        ['unverified', { kind: 'awaiting-certificate', text: 'awaiting certificate' }],
        ['legacy', { kind: 'older-format', text: 'unsigned, older format' }],
    ] as const)('maps %s to its own quiet note', (value, note) => {
        expect(verdictNote(value)).toEqual(note);
    });

    it.each([undefined, '', 'verified'])('draws nothing for %s', (value) => {
        expect(verdictNote(value)).toEqual({ kind: 'none', text: '' });
    });

    it('draws nothing for a verdict it does not know', () => {
        // A later step stores a word for this node's own records; that word must
        // not reach the row as "unsigned", or this node's own words look suspect.
        expect(verdictNote('quarantined')).toEqual({ kind: 'none', text: '' });
        expect(verdictNote('own')).toEqual({ kind: 'none', text: '' });
    });
});

describe('the verdict on the record survives the mapping', () => {
    // The live group_text_received event and every history load path build
    // their rows through mapBackendMessage; the row reads the field off the
    // mapped message, so this is the wire the note hangs from.
    it('carries a verdict through mapBackendMessage', () => {
        const mapped = mapBackendMessage(
            { id: 'm9', sender_node_id: 'dpc-node-peer', text: 'hi', verification: 'unverified' });
        expect(mapped.verification).toBe('unverified');
    });

    it('leaves a record without a verdict unmarked', () => {
        const mapped = mapBackendMessage({ id: 'm10', sender_node_id: 'dpc-node-peer', text: 'hi' });
        expect(mapped.verification).toBeUndefined();
    });

    it('drops a value that is not a non-empty string rather than passing it on', () => {
        const mapped = mapBackendMessage(
            { id: 'm11', sender_node_id: 'dpc-node-peer', text: 'hi', verification: null });
        expect(mapped.verification).toBeUndefined();
    });
});
