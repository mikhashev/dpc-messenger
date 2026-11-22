import { describe, it, expect } from 'vitest';
import { addStopNote, clearStopNotes, isStopNote } from './stopNotes';
import panel from '../components/AgentProgressCollapsible.svelte?raw';
import list from '../components/ChatMessageList.svelte?raw';
import core from '../coreService.ts?raw';

const pressed = {
    conversation_id: 'agent_001',
    stop_note: 'pressed',
    message: 'Stop pressed by Mike — waiting for the current step to finish',
    ts: '2026-11-22T10:00:00Z',
};

describe('a Stop line is told apart from thinking and tool progress', () => {
    it('only a payload with a known stop_note is one', () => {
        expect(isStopNote(pressed)).toBe(true);
        expect(isStopNote({ stop_note: 'summary' })).toBe(true);
        expect(isStopNote({ stop_note: 'kill' })).toBe(true);
        expect(isStopNote({ stop_note: 'before_round_1' })).toBe(true);
        expect(isStopNote({ stop_note: 'nonsense' })).toBe(false);
        expect(isStopNote({ stop_note: 3 })).toBe(false);
        expect(isStopNote({})).toBe(false);
        expect(isStopNote(null)).toBe(false);
    });
});

describe('the lines of one conversation, in the order they came', () => {
    it('a line is added to its own conversation only', () => {
        const a = addStopNote({}, pressed);
        expect(a.agent_001.map((n) => n.kind)).toEqual(['pressed']);
        expect(a.agent_001[0].text).toContain('Stop pressed by Mike');
        const b = addStopNote(a, { ...pressed, conversation_id: 'group-1' });
        expect(Object.keys(b).sort()).toEqual(['agent_001', 'group-1']);
    });

    it('press, summary, kill stack in order; a kind already shown is not drawn twice', () => {
        let n = addStopNote({}, pressed);
        n = addStopNote(n, { conversation_id: 'agent_001', stop_note: 'summary', message: 'Writing a summary' });
        n = addStopNote(n, pressed);
        n = addStopNote(n, { conversation_id: 'agent_001', stop_note: 'kill', message: 'Kill pressed by Mike' });
        expect(n.agent_001.map((x) => x.kind)).toEqual(['pressed', 'summary', 'kill']);
    });

    it('a payload with no conversation, no text or no known kind adds nothing', () => {
        const base = {};
        expect(addStopNote(base, { stop_note: 'pressed', message: 'x' })).toBe(base);
        expect(addStopNote(base, { conversation_id: 'c', stop_note: 'pressed' })).toBe(base);
        expect(addStopNote(base, { conversation_id: 'c', stop_note: 'zzz', message: 'x' })).toBe(base);
    });

    it('the run ending clears its lines and leaves the others', () => {
        let n = addStopNote({}, pressed);
        n = addStopNote(n, { ...pressed, conversation_id: 'group-1' });
        const cleared = clearStopNotes(n, 'agent_001');
        expect(Object.keys(cleared)).toEqual(['group-1']);
        expect(clearStopNotes(cleared, 'agent_001')).toBe(cleared);
        expect(clearStopNotes(cleared, undefined)).toBe(cleared);
    });
});

describe('the lines are wired into the live panel', () => {
    it('the panel draws them as lines of their own, not as a round', () => {
        expect(panel).toContain('agentStopNotes');
        expect(panel).toContain('class="stop-notes"');
        expect(panel).toContain('class="stop-note-line {note.kind}"');
        // outside the rounds list, so they stay visible when the block is folded
        const lines = panel.indexOf('class="stop-notes"');
        const rounds = panel.indexOf('groupByRound(toolCalls)');
        expect(lines).toBeGreaterThan(rounds);
        expect(panel.slice(rounds, lines)).toContain('{/if}');
    });

    it('only the live block has them (it is the one given a conversation)', () => {
        expect(panel).toMatch(/isLive && conversationId \? \(\$agentStopNotes\[conversationId\]/);
    });

    it('colours come from the existing tokens', () => {
        const css = panel.slice(panel.indexOf('.stop-note-line {'));
        expect(css).toContain('var(--warning,');
        expect(css).toContain('var(--danger,');
    });

    it('the list keeps the live block on screen when only a Stop line exists', () => {
        expect(list).toContain('liveStopNotes.length > 0}');
    });

    it('the event is routed to the notes and never to agentProgress, the clear empties them', () => {
        const branch = core.slice(core.indexOf('isStopNote(message.payload)'));
        const own = branch.slice(0, branch.indexOf('else if (message.event === "agent_progress")'));
        expect(own).toContain('agentStopNotes.update');
        expect(own).not.toContain('agentProgress.set');
        expect(core).toContain('agentStopNotes.update((m) => clearStopNotes(m, _cid))');
    });
});
