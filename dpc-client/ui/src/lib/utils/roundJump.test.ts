import { describe, it, expect } from 'vitest';
import { newestRound, roundJumpLabel, roundJumpTitle, findRoundHeader } from './roundJump';
import row from '../components/AgentProgressCollapsible.svelte?raw';

describe('the Round N start button names the newest round', () => {
    it('no tool call, no round, so no button', () => {
        expect(newestRound([])).toBeNull();
    });

    it('is the highest round, whatever the order of the calls', () => {
        expect(newestRound([{ round: 1 }, { round: 1 }, { round: 2 }])).toBe(2);
        expect(newestRound([{ round: 3 }, { round: 1 }, { round: 2 }])).toBe(3);
    });

    it('a call with no round counts as round 0, as the round grouping does', () => {
        expect(newestRound([{}])).toBe(0);
        expect(newestRound([{}, { round: 2 }])).toBe(2);
    });

    it('label and title carry the number', () => {
        expect(roundJumpLabel(4)).toBe('↑ Round 4 start');
        expect(roundJumpTitle(4)).toBe('Scroll to the start of round 4');
    });
});

describe('the header is looked up inside one block', () => {
    it('asks the block it was given for the round header of that round', () => {
        const asked: string[] = [];
        const marker = {} as Element;
        const root = { querySelector: (s: string) => { asked.push(s); return marker; } };
        expect(findRoundHeader(root, 7)).toBe(marker);
        expect(asked).toEqual(['.round-header[data-round="7"]']);
    });

    it('a missing root or header is null, not a throw', () => {
        expect(findRoundHeader(null, 1)).toBeNull();
        expect(findRoundHeader({ querySelector: () => null }, 1)).toBeNull();
    });
});

describe('the button is wired into the live Stop bar only', () => {
    it('round headers carry data-round and the button sits inside stop-footer', () => {
        expect(row).toContain('data-round={group.round}');
        const footer = row.slice(row.indexOf('<div class="stop-footer">'));
        expect(footer.slice(0, footer.indexOf('</div>'))).toContain('class="round-jump"');
        // the footer is the live branch; the button is not drawn anywhere else
        expect(row.split('class="round-jump"').length - 1).toBe(1);
        expect(row.slice(0, row.indexOf('<div class="stop-footer">')).slice(-700))
            .toContain('{#if isLive && conversationId}');
    });

    it('scrolls from the block root, not the document', () => {
        expect(row).toContain('findRoundHeader(rootEl, newest)');
        expect(row).toContain("scrollIntoView({ block: 'start', behavior: 'smooth' })");
        expect(row).not.toContain('document.querySelector');
    });
});
