import { describe, it, expect } from 'vitest';
import { firstRound, roundJumpLabel, roundJumpTitle } from './roundJump';
import row from '../components/AgentProgressCollapsible.svelte?raw';

describe('the Round N start button names the first round of the run', () => {
    it('no tool call, no round, so no button', () => {
        expect(firstRound([])).toBeNull();
    });

    it('is the lowest round, whatever the order of the calls', () => {
        expect(firstRound([{ round: 1 }, { round: 1 }, { round: 2 }])).toBe(1);
        expect(firstRound([{ round: 3 }, { round: 1 }, { round: 2 }])).toBe(1);
    });

    it('a call with no round counts as round 0, as the round grouping does', () => {
        expect(firstRound([{}])).toBe(0);
        expect(firstRound([{ round: 2 }, {}])).toBe(0);
    });

    it('label and title carry the number', () => {
        expect(roundJumpLabel(0)).toBe('↑ Round 0 start');
        expect(roundJumpTitle(1)).toBe('Scroll to the start of the run (round 1)');
    });
});

describe('the button is wired into the live Stop bar only', () => {
    it('the button sits inside stop-footer and is drawn once', () => {
        const footer = row.slice(row.indexOf('<div class="stop-footer">'));
        expect(footer.slice(0, footer.indexOf('</div>'))).toContain('class="round-jump"');
        // the footer is the live branch; the button is not drawn anywhere else
        expect(row.split('class="round-jump"').length - 1).toBe(1);
        expect(row.slice(0, row.indexOf('<div class="stop-footer">')).slice(-700))
            .toContain('{#if isLive && conversationId}');
    });

    it('scrolls the block itself to its top, not the newest round and not the document', () => {
        expect(row).toContain("rootEl?.scrollIntoView({ block: 'start', behavior: 'smooth' })");
        expect(row).not.toContain('document.querySelector');
        expect(row).not.toContain('newestRound');
    });
});
