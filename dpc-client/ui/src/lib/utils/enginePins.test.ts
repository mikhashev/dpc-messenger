import { describe, it, expect } from 'vitest';
import src from '../components/ProvidersEditor.svelte?raw';
import { enginePinLines, runsRetainedPin, type EnginePins } from './enginePins';

const llama = (over: Partial<EnginePins> = {}): EnginePins => ({
    pin: 'b11669',
    retained_pin: 'b11429',
    running_pin: null,
    rolled_back_to: null,
    installed_pins: [
        { tag: 'b11429', platform: 'win-cuda-13.4-x64', start_passed_at: '2026-10-20T10:00:00+00:00' },
        { tag: 'b11669', platform: 'win-cuda-13.4-x64', start_passed_at: null },
    ],
    ...over,
});

describe('the Local engines block names each engine\'s pin', () => {
    it('one line per engine: the pin and the retained one', () => {
        expect(enginePinLines({ 'llama.cpp': llama() })).toEqual([
            { text: 'llama.cpp b11669 (retained b11429)', warn: false },
        ]);
    });

    it('says when the pin is not downloaded yet and the retained one runs', () => {
        const p = llama({ running_pin: 'b11429', installed_pins: [llama().installed_pins[0]] });
        expect(enginePinLines({ 'llama.cpp': p })[0].text)
            .toBe('llama.cpp b11669 (retained b11429) — b11669 not downloaded yet — running b11429');
    });

    it('adds a warning line after a rollback, worded per engine', () => {
        const rolled = { from: 'v0.3.0', to: 'v0.2.9', reason: 'the start smoke failed', at: 0 };
        const lines = enginePinLines({
            'llama.cpp': llama({ rolled_back_to: { ...rolled, from: 'b11669', to: 'b11429' }, running_pin: 'b11429' }),
            strata: { pin: 'v0.3.0', retained_pin: 'v0.2.9', running_pin: 'v0.2.9', rolled_back_to: rolled, installed_pins: [
                { tag: 'v0.3.0', platform: 'windows-x86_64', start_passed_at: null },
            ] },
        });
        expect(lines.map((l) => l.warn)).toEqual([false, true, false, true]);
        expect(lines[1].text).toBe(
            'llama.cpp b11669 failed its first start (the start smoke failed); running b11429 instead, until the backend restarts',
        );
        expect(lines[3].text).toContain('until the alias is Reset or the backend restarts');
    });

    it('an engine with nothing on disk gets no line, and no status gets none', () => {
        const none: EnginePins = { pin: 'v0.2.9', retained_pin: null, running_pin: null, rolled_back_to: null, installed_pins: [] };
        expect(enginePinLines({ strata: none })).toEqual([]);
        expect(enginePinLines(null)).toEqual([]);
    });

    it('the card-level block renders them, and the status load keeps `pins`', () => {
        const start = src.indexOf('{#snippet cardStatus(');
        const block = src.slice(start, src.indexOf('{/snippet}', start));
        expect(block).toContain('{#each enginePinLines(engineStatus?.pins) as line}');
        expect(block).toContain('class:warn={line.warn}');
        expect(src).toContain('pins: r.pins ?? null');
    });
});

describe('a Strata row running the retained pin', () => {
    const pin = { release_tag: 'v0.2.9', engine_version: '0.1.55' };
    it('is the retained pin, not a mismatch, while its engine matches it', () => {
        expect(runsRetainedPin({ running_pin: 'v0.2.8', version_matches: true, pin })).toBe(true);
        expect(runsRetainedPin({ running_pin: 'v0.2.8', version_matches: false, pin })).toBe(false);
        expect(runsRetainedPin({ running_pin: 'v0.2.9', version_matches: true, pin })).toBe(false);
        expect(runsRetainedPin({ running_pin: null, version_matches: true, pin })).toBe(false);
    });

    it('the row shows it before the mismatch badge', () => {
        const at = src.indexOf('{#if runsRetainedPin(row)}');
        expect(at).toBeGreaterThan(0);
        expect(at).toBeLessThan(src.indexOf('{:else if versionDiffers(row.engine_version, row.pin.engine_version)}'));
    });
});
