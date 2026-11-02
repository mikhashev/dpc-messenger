import { describe, it, expect } from 'vitest';
import {
    STRATA_ESTIMATE_LABEL,
    cacheSuggestionMayNotFit,
    freeCommitMibOf,
    runConfigId,
    runConfigOptions,
    sizeForRunConfig,
    strataSizeLine,
    suggestedConversationCacheMib,
    suggestionMarkText,
    type StrataModelFamily,
} from './strataInstall';

describe('the conversation cache suggestion', () => {
    it('is unknown when the free commit or the RAM the model needs is unknown', () => {
        expect(suggestedConversationCacheMib(null, 20)).toBeNull();
        expect(suggestedConversationCacheMib(undefined, 20)).toBeNull();
        expect(suggestedConversationCacheMib(NaN, 20)).toBeNull();
        expect(suggestedConversationCacheMib(60000, null)).toBeNull();
        expect(suggestedConversationCacheMib(60000, undefined)).toBeNull();
    });

    it('is half of what is left after the model and 8192 MiB, floored to 1024', () => {
        // (30000 - 10*1024 - 8192) / 2 = 5784 -> 5120
        expect(suggestedConversationCacheMib(30000, 10)).toBe(5120);
        // (40960 - 20480 - 8192) / 2 = 6144 exactly
        expect(suggestedConversationCacheMib(40960, 20)).toBe(6144);
        // one MiB short of the next step stays on the lower one
        expect(suggestedConversationCacheMib(40960 + 2047, 20)).toBe(6144);
        expect(suggestedConversationCacheMib(40960 + 2048, 20)).toBe(7168);
    });

    it('takes a fractional RAM figure as it is', () => {
        // (50000 - 21.5*1024 - 8192) / 2 = 9896 -> 9216
        expect(suggestedConversationCacheMib(50000, 21.5)).toBe(9216);
    });

    it('is held to 1024 at the bottom, a negative remainder included', () => {
        expect(suggestedConversationCacheMib(12000, 10)).toBe(1024);
        expect(suggestedConversationCacheMib(1000, 30)).toBe(1024);
        expect(suggestedConversationCacheMib(0, 0)).toBe(1024);
    });

    it('is held to 16384 at the top', () => {
        expect(suggestedConversationCacheMib(200000, 10)).toBe(16384);
        // exactly at the ceiling, one step above it, one step below it
        expect(suggestedConversationCacheMib(8192 + 2 * 16384, 0)).toBe(16384);
        expect(suggestedConversationCacheMib(8192 + 2 * 17408, 0)).toBe(16384);
        expect(suggestedConversationCacheMib(8192 + 2 * 16384 - 2048, 0)).toBe(15360);
    });
});

describe('free commit from the engine status', () => {
    it('is the available page file (commit), never physical memory', () => {
        expect(freeCommitMibOf({
            avail_page_file_mib: 31000, total_page_file_mib: 90000, avail_phys_mib: 12000, total_phys_mib: 64000,
        })).toBe(31000);
    });

    it('is unknown off Windows, where the commit fields are null, and when nothing was read', () => {
        expect(freeCommitMibOf({
            avail_page_file_mib: null, total_page_file_mib: null, avail_phys_mib: 12000, total_phys_mib: 64000,
        })).toBeNull();
        expect(freeCommitMibOf(null)).toBeNull();
        expect(freeCommitMibOf(undefined)).toBeNull();
    });
});

describe('the mark beside a suggested value', () => {
    it('says where the figure comes from and that it was not measured, with the time as HH:MM', () => {
        expect(suggestionMarkText(new Date(2026, 10, 2, 9, 5))).toBe('suggested from free commit memory at 09:05, not measured');
        expect(suggestionMarkText(new Date(2026, 10, 2, 23, 59))).toBe('suggested from free commit memory at 23:59, not measured');
    });
});

describe('run configs as select options', () => {
    it('shows the file name and keeps the absolute path as the value, in the order given', () => {
        const opts = runConfigOptions([
            'C:\\m\\strata\\run-configs\\strata-IQ3_S.json',
            '/m/strata/run-configs/strata-q2_0.json',
        ]);
        expect(opts.map((o) => o.label)).toEqual(['strata-IQ3_S.json', 'strata-q2_0.json']);
        expect(opts.map((o) => o.path)).toEqual([
            'C:\\m\\strata\\run-configs\\strata-IQ3_S.json',
            '/m/strata/run-configs/strata-q2_0.json',
        ]);
    });

    it('names the size id a file is for, lower case, without the frame', () => {
        expect(runConfigId('/m/strata/run-configs/strata-IQ3_S.json')).toBe('iq3_s');
        expect(runConfigId('C:\\x\\strata-q2_0.json')).toBe('q2_0');
        expect(runConfigOptions(['/a/strata-q2_0.json'])[0].id).toBe('q2_0');
    });

    it('no folder, no options', () => {
        expect(runConfigOptions(null)).toEqual([]);
        expect(runConfigOptions(undefined)).toEqual([]);
        expect(runConfigOptions([])).toEqual([]);
    });
});

const FAMILIES: StrataModelFamily[] = [
    {
        family: 'qwen', title: 'Qwen', sizes: [
            { model: 'Q2_0', id: 'q2_0', download_gb: 34, ram_needed_gb: 20, images: true, on_this_pc: 'fits', installed: false },
            { model: 'IQ3_S', id: 'iq3_s', download_gb: 50.3, ram_needed_gb: 30.5, images: false, on_this_pc: 'does not fit', installed: true },
        ],
    },
];

describe('a run config matched to a listed size', () => {
    it('is found by the id in its file name, whatever the case', () => {
        expect(sizeForRunConfig(FAMILIES, '/m/strata/run-configs/strata-iq3_s.json')?.ram_needed_gb).toBe(30.5);
        expect(sizeForRunConfig(FAMILIES, '/m/strata/run-configs/strata-IQ3_S.json')?.model).toBe('IQ3_S');
    });

    it('is nothing when no size has that id, or the list is not loaded', () => {
        expect(sizeForRunConfig(FAMILIES, '/m/strata/run-configs/strata-mine.json')).toBeNull();
        expect(sizeForRunConfig([], '/m/strata/run-configs/strata-q2_0.json')).toBeNull();
        expect(sizeForRunConfig(null, '/m/strata/run-configs/strata-q2_0.json')).toBeNull();
    });
});

describe('the line under a size', () => {
    it("puts Strata's words for this PC under their label, with the sizes", () => {
        expect(STRATA_ESTIMATE_LABEL).toBe("Strata's estimate");
        expect(strataSizeLine(FAMILIES[0].sizes[0])).toBe("download 34 GB · needs 20 GB RAM · Strata's estimate: fits");
        expect(strataSizeLine(FAMILIES[0].sizes[1])).toContain("Strata's estimate: does not fit");
    });

    it('leaves out what the table did not give', () => {
        expect(strataSizeLine({ model: 'X' })).toBe('');
        expect(strataSizeLine({ model: 'X', download_gb: 3 })).toBe('download 3 GB');
    });
});

describe('a suggestion raised to the floor says the model may not fit', () => {
    it('is true only when the rule comes out below 1024 MiB before the clamp', () => {
        // (free - ram*1024 - 8192) / 2 : 63488 + 8192 + 2*1023 → just under; + 2*1024 → at the floor
        expect(cacheSuggestionMayNotFit(63488 + 8192 + 2046, 62)).toBe(true);
        expect(cacheSuggestionMayNotFit(63488 + 8192 + 2048, 62)).toBe(false);
        expect(cacheSuggestionMayNotFit(40000, 62)).toBe(true);
        expect(cacheSuggestionMayNotFit(null, 62)).toBe(false);
        expect(cacheSuggestionMayNotFit(89680, null)).toBe(false);
    });
});
