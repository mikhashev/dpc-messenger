/**
 * Dictate in chat A, press "To input box", open chat B before the transcription
 * returns: the words belong to A's draft, and B's draft must not change. The
 * switch-back is part of the case, because A's box is refilled from the parked map.
 */

import { describe, it, expect } from 'vitest';
import { appendToChatDraft, switchChatDraft, type ChatDraftState } from './chatDraftInputs';

const A = 'dpc-node-aaaa';
const B = 'group-bbbb';

describe('switchChatDraft', () => {
    it('parks the outgoing text and loads the incoming draft', () => {
        const drafts = new Map([[B, 'half a sentence']]);
        const next = switchChatDraft({ drafts, currentInput: 'typed in A' }, A, B);
        expect(next.currentInput).toBe('half a sentence');
        expect(next.drafts.get(A)).toBe('typed in A');
    });

    it('opens a chat with no parked draft empty', () => {
        expect(switchChatDraft({ drafts: new Map(), currentInput: 'typed in A' }, A, B).currentInput).toBe('');
    });
});

describe('appendToChatDraft', () => {
    it('writes into the box when the chat is still the one open, as before', () => {
        const state: ChatDraftState = { drafts: new Map(), currentInput: 'hello' };
        const next = appendToChatDraft(state, A, A, 'world');
        expect(next.currentInput).toBe('hello world');
        expect(next.drafts).toBe(state.drafts);
    });

    it('does not put a space in front of an empty box', () => {
        const next = appendToChatDraft({ drafts: new Map(), currentInput: '' }, A, A, 'words');
        expect(next.currentInput).toBe('words');
    });

    it('lands in the originating chat when the user switched while it was transcribing', () => {
        // In A with a started draft; press the button; open B, which has its own draft.
        let state: ChatDraftState = { drafts: new Map([[B, 'draft of B']]), currentInput: 'draft of A' };
        state = switchChatDraft(state, A, B);
        expect(state.currentInput).toBe('draft of B');

        // The transcription returns while B is open.
        state = appendToChatDraft(state, B, A, 'dictated words');
        expect(state.currentInput).toBe('draft of B');
        expect(state.drafts.get(A)).toBe('draft of A dictated words');

        // B is left with exactly what it had, and going back to A shows the words.
        state = switchChatDraft(state, B, A);
        expect(state.drafts.get(B)).toBe('draft of B');
        expect(state.currentInput).toBe('draft of A dictated words');
    });

    it('creates the parked draft when the originating chat had none', () => {
        const state: ChatDraftState = { drafts: new Map(), currentInput: 'draft of B' };
        const next = appendToChatDraft(state, B, A, 'dictated words');
        expect(next.drafts.get(A)).toBe('dictated words');
        expect(next.currentInput).toBe('draft of B');
    });

    it('changes nothing for an empty transcription', () => {
        const state: ChatDraftState = { drafts: new Map(), currentInput: 'draft of B' };
        expect(appendToChatDraft(state, B, A, '')).toBe(state);
    });
});
