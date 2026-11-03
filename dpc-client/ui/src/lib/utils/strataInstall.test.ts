import { describe, it, expect } from 'vitest';
import {
    STRATA_ESTIMATE_LABEL,
    cacheSuggestionMayNotFit,
    freeCommitMibOf,
    RESIDENT_SUGGESTION_MIN_GIB,
    RESIDENT_SUGGESTION_RESERVE_MIB,
    residentBudgetError,
    residentSuggestionMarkText,
    residentSuggestionWhy,
    runConfigId,
    runConfigOptions,
    sizeForRunConfig,
    strataSizeLine,
    suggestedConversationCacheMib,
    suggestedResidentBudget,
    suggestionMarkText,
    type StrataModelFamily,
} from './strataInstall';

// The owner's case of 2026-11-02: IQ3_S (ram_needed_gb 62, experts_gb 50.3 as Strata's table has
// them), 33.10 GiB available to the engine, 63168 MiB of RAM in all.
const IQ3_S = { ram_needed_gb: 62, experts_gb: 50.3 };
const NO_ARENA = { ram_needed_gb: 62 };
const WINDOWS = (physical: number, commit: number) => ({
    avail_page_file_mib: commit, total_page_file_mib: 108236, avail_phys_mib: physical, total_phys_mib: 63168,
    source: 'GlobalMemoryStatusEx',
});
const POSIX = (physical: number) => ({
    avail_page_file_mib: null, total_page_file_mib: null, avail_phys_mib: physical, total_phys_mib: 63168,
    source: 'psutil',
});

describe('the resident budget suggestion', () => {
    it('gives 24 on the worked example: 33.10 GiB free less 9 GiB, rounded down', () => {
        expect(RESIDENT_SUGGESTION_RESERVE_MIB).toBe(9 * 1024);
        const s = suggestedResidentBudget(WINDOWS(33894, 80000), IQ3_S);
        expect(s?.gib).toBe(24);
        expect(s?.cappedByArena).toBe(false);
        expect(s?.reading).toBe('free physical');
    });

    it('takes the lower of free physical and free commit', () => {
        const s = suggestedResidentBudget(WINDOWS(33894, 20480), IQ3_S);
        expect(s?.gib).toBe(11);
        expect(s?.freeMib).toBe(20480);
        expect(s?.reading).toBe('free commit');
    });

    it('uses physical alone where commit is not measured', () => {
        const s = suggestedResidentBudget(POSIX(33894), IQ3_S);
        expect(s?.gib).toBe(24);
        expect(s?.commitMeasured).toBe(false);
    });

    it('compares the size as GiB and is nothing when the size fits what is free', () => {
        expect(suggestedResidentBudget(WINDOWS(62 * 1024, 90000), IQ3_S)).toBeNull();
        expect(suggestedResidentBudget(WINDOWS(62 * 1024 - 1, 90000), NO_ARENA)?.gib).toBe(52);
        expect(suggestedResidentBudget(WINDOWS(90000, 62 * 1024 - 1), IQ3_S)?.reading).toBe('free commit');
    });

    it('is nothing when a figure is unknown', () => {
        expect(suggestedResidentBudget(null, IQ3_S)).toBeNull();
        expect(suggestedResidentBudget(undefined, IQ3_S)).toBeNull();
        expect(suggestedResidentBudget(WINDOWS(33894, 80000), null)).toBeNull();
        expect(suggestedResidentBudget(WINDOWS(33894, 80000), undefined)).toBeNull();
        expect(suggestedResidentBudget(WINDOWS(33894, 80000), { ram_needed_gb: NaN })).toBeNull();
        expect(suggestedResidentBudget(WINDOWS(33894, 80000), { experts_gb: 50.3 })).toBeNull();
    });

    it('is held to the arena, decimal GB taken to GiB and floored as setup.py does: 50.3 is 46', () => {
        const s = suggestedResidentBudget(WINDOWS(60 * 1024, 90000), IQ3_S);
        expect(s?.gib).toBe(46);
        expect(s?.arenaGib).toBe(46);
        expect(s?.cappedByArena).toBe(true);
    });

    it('calls it capped only when the arena is what decided', () => {
        const atTheArena = suggestedResidentBudget(POSIX(9216 + 46 * 1024), IQ3_S);
        expect(atTheArena?.gib).toBe(46);
        expect(atTheArena?.cappedByArena).toBe(false);
        const past = suggestedResidentBudget(POSIX(9216 + 47 * 1024), IQ3_S);
        expect(past?.gib).toBe(46);
        expect(past?.cappedByArena).toBe(true);
    });

    it('has no cap when the table gives no arena', () => {
        for (const size of [NO_ARENA, { ram_needed_gb: 62, experts_gb: null }]) {
            const s = suggestedResidentBudget(WINDOWS(60 * 1024, 90000), size);
            expect(s?.gib).toBe(51);
            expect(s?.arenaGib).toBeNull();
            expect(s?.cappedByArena).toBe(false);
        }
    });

    it('prefills no value below the 8 GiB setup.py writes at the least, and keeps the reading to say so', () => {
        expect(RESIDENT_SUGGESTION_MIN_GIB).toBe(8);
        for (const free of [9216 + 8 * 1024 - 1, 9216 + 1024, 9216, 4096, 0]) {
            const s = suggestedResidentBudget(POSIX(free), IQ3_S);
            expect(s?.gib, `free ${free}`).toBeNull();
            expect(s?.freeMib).toBe(free);
        }
        expect(suggestedResidentBudget(POSIX(9216 + 8 * 1024), IQ3_S)?.gib).toBe(8);
    });

    it('prints a reading of zero as a number, and an unread one is no suggestion and no text', () => {
        expect(suggestedResidentBudget(null, IQ3_S)).toBeNull();
        const zero = suggestedResidentBudget(WINDOWS(33894, 0), IQ3_S);
        expect(zero?.freeMib).toBe(0);
        expect(residentSuggestionWhy(zero!)).toContain('0.0 GiB is free now (free commit)');
    });
});

describe('the words beside the resident budget', () => {
    it('are marked as a suggestion, not a measurement, with the time', () => {
        expect(residentSuggestionMarkText(new Date(2026, 10, 2, 9, 5))).toBe('suggested from free memory at 09:05, not measured');
    });

    it('say why, whose figure the size is, and that the reserve is one example and not a rule', () => {
        const why = residentSuggestionWhy(suggestedResidentBudget(WINDOWS(33894, 80000), IQ3_S)!);
        expect(why).toContain(`${STRATA_ESTIMATE_LABEL}: this size needs 62 GB of RAM and 33.1 GiB is free now (free physical)`);
        expect(why).toContain('the lower of free physical and free commit, less a reserve of 9 GiB.');
        expect(why).toContain('one worked example (about 33 GiB free, a budget of 24 ran on the smaller IQ3_XXS), not from a rule');
        expect(why).not.toContain('held to');
    });

    it('name physical alone where commit is not measured', () => {
        const why = residentSuggestionWhy(suggestedResidentBudget(POSIX(33894), IQ3_S)!);
        expect(why).toContain('free physical (free commit is not measured on this OS)');
        expect(why).not.toContain('the lower of');
    });

    it('say so when the arena decided, and promise no rest that may not be there', () => {
        const why = residentSuggestionWhy(suggestedResidentBudget(WINDOWS(60 * 1024, 90000), IQ3_S)!);
        expect(why).toContain('less a reserve of 9 GiB, held to the 46 GiB of experts this size has.');
        expect(why).toContain('reads any others from the model files');
        expect(why).not.toContain('the rest');
    });

    it('say that nothing is suggested below the least, and that closing programs can help when the size can fit', () => {
        const why = residentSuggestionWhy(suggestedResidentBudget(POSIX(8192), { ram_needed_gb: 40, experts_gb: 34 })!);
        expect(why).toContain('8.0 GiB is free now');
        expect(why).toContain('After the reserve of 9 GiB that is less than 8 GiB, the least Strata\'s own setup writes, so no budget is suggested');
        expect(why).toContain('close other programs and choose the run config again, or type a budget');
        expect(why).not.toContain('worked example');
    });

    it('do not say close other programs when the size needs more than this PC has in all', () => {
        const why = residentSuggestionWhy(suggestedResidentBudget(POSIX(8192), IQ3_S)!);
        expect(why).toContain('no budget is suggested');
        expect(why).toContain('closing other programs will not make this size fit (this PC has 61.7 GiB of RAM in all)');
        expect(why).toContain('Type a budget');
        expect(why).not.toContain('close other programs and choose');
    });

    it("do not say it either when Strata's estimate says it does not fit", () => {
        const size = { ram_needed_gb: 40, on_this_pc: 'does not fit: this Mac has too little memory' };
        const why = residentSuggestionWhy(suggestedResidentBudget(POSIX(8192), size)!);
        expect(why).toContain(`closing other programs will not make this size fit (${STRATA_ESTIMATE_LABEL} for this PC: does not fit: this Mac has too little memory)`);
        for (const on_this_pc of ['tight', 'fits', null]) {
            expect(suggestedResidentBudget(POSIX(8192), { ram_needed_gb: 40, on_this_pc })?.neverFits, String(on_this_pc)).toBeNull();
        }
    });
});

describe('a typed resident budget', () => {
    it('is refused at zero or below, as the save refuses it; the min on the input is only a hint', () => {
        for (const bad of [0, -1, NaN, Infinity]) expect(residentBudgetError(bad), String(bad)).toContain('more than 0');
        for (const good of [0.5, 8, 24]) expect(residentBudgetError(good), String(good)).toBeNull();
    });

    it('may be empty, which keeps the base config', () => {
        expect(residentBudgetError(undefined)).toBeNull();
        expect(residentBudgetError(null)).toBeNull();
    });
});

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
