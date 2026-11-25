import { describe, it, expect } from 'vitest';
import marqueeSource from '../components/MarqueeText.svelte?raw';
import {
    MARQUEE_HOLD_END,
    MARQUEE_HOLD_START,
    MARQUEE_MIN_CYCLE_S,
    MARQUEE_SPEED_PX_S,
    MARQUEE_TRAVEL_SHARE,
    marqueeCycleSeconds,
    marqueeOverflow,
    marqueeShouldRun,
} from './marquee';

describe('the ticker scrolls only a title that overflows its box', () => {
    it('does not scroll text that fits, or one that overflows by sub-pixel rounding', () => {
        expect(marqueeOverflow(120, 200)).toBe(0);
        expect(marqueeOverflow(200, 200)).toBe(0);
        expect(marqueeOverflow(200.6, 200)).toBe(0);
    });

    it('scrolls by exactly the overflow, rounded up to a whole pixel', () => {
        expect(marqueeOverflow(260, 200)).toBe(60);
        expect(marqueeOverflow(200.2 + 1, 200)).toBe(2);
    });

    it('never scrolls in a box that has no width yet, or on a measurement that is not a number', () => {
        expect(marqueeOverflow(300, 0)).toBe(0);
        expect(marqueeOverflow(0, 0)).toBe(0);
        expect(marqueeOverflow(NaN, 100)).toBe(0);
        expect(marqueeOverflow(300, Infinity)).toBe(0);
    });
});

describe('reduced motion keeps the ellipsis', () => {
    it('runs on an overflow unless the person asked for reduced motion', () => {
        expect(marqueeShouldRun(40, false)).toBe(true);
        expect(marqueeShouldRun(40, true)).toBe(false);
        expect(marqueeShouldRun(0, false)).toBe(false);
    });
});

describe('the cycle is steady and never twitches', () => {
    it('has no cycle without an overflow', () => {
        expect(marqueeCycleSeconds(0)).toBe(0);
        expect(marqueeCycleSeconds(-5)).toBe(0);
    });

    it('is at least the minimum for a small overflow', () => {
        expect(marqueeCycleSeconds(4)).toBe(MARQUEE_MIN_CYCLE_S);
    });

    it('grows with the overflow so the speed stays the same', () => {
        const a = 400;
        const seconds = marqueeCycleSeconds(a);
        const forwardSeconds = seconds * MARQUEE_TRAVEL_SHARE;
        expect(a / forwardSeconds).toBeGreaterThan(MARQUEE_SPEED_PX_S * 0.98);
        expect(a / forwardSeconds).toBeLessThan(MARQUEE_SPEED_PX_S * 1.02);
        expect(marqueeCycleSeconds(800)).toBeGreaterThan(seconds);
    });
});

describe('MarqueeText.svelte', () => {
    it('splits the cycle as utils/marquee.ts does: hold, travel, hold, travel back', () => {
        expect(MARQUEE_HOLD_END - MARQUEE_HOLD_START).toBe(50 - MARQUEE_HOLD_START + (MARQUEE_HOLD_END - 50));
        expect((50 - MARQUEE_HOLD_START) / 100).toBeCloseTo(MARQUEE_TRAVEL_SHARE);
        expect((100 - MARQUEE_HOLD_END) / 100).toBeCloseTo(MARQUEE_TRAVEL_SHARE);
        expect(marqueeSource).toContain(`0%, ${MARQUEE_HOLD_START}% { transform: translateX(0); }`);
        expect(marqueeSource).toContain(`50%, ${MARQUEE_HOLD_END}% { transform: translateX(var(--mq-shift, 0px)); }`);
    });

    it('moves only inside prefers-reduced-motion: no-preference, and pauses on hover and focus', () => {
        const motion = marqueeSource.slice(marqueeSource.indexOf('@media (prefers-reduced-motion: no-preference)'));
        expect(motion).toContain('animation: marquee-run');
        expect(motion).toContain('.marquee.run:hover .marquee-inner');
        expect(motion).toContain('animation-play-state: paused');
        // Outside that query the text is a plain inline run with an ellipsis.
        const before = marqueeSource.slice(0, marqueeSource.indexOf('@media (prefers-reduced-motion: no-preference)'));
        expect(before).toContain('text-overflow: ellipsis');
        expect(before).not.toContain('animation:');
    });

    it('measures scrollWidth against clientWidth, again on a new text and on a resize', () => {
        expect(marqueeSource).toContain('marqueeOverflow(box.scrollWidth, box.clientWidth)');
        expect(marqueeSource).toContain('new ResizeObserver');
        expect(marqueeSource).toContain('void text;');
    });

    it('keeps the whole text in the tooltip', () => {
        expect(marqueeSource).toContain('title={title ?? text}');
    });
});
