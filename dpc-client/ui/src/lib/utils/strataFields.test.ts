import { describe, it, expect } from 'vitest';
import {
  STRATA_MODEL_PARAMS, suggestPleGguf, pleAfterNativeChange, parseModelParam, modelParamText,
  basePlaceholder, versionDiffers, cacheSummaryLine, formatMib, freeCommitLine,
  STRATA_CACHE_ADMISSION, admissionPlaceholder, cacheFlagsLine, engineAdmissionLine,
} from './strataFields';

const spec = (key: string) => STRATA_MODEL_PARAMS.find((s) => s.key === key)!;

describe('suggestPleGguf', () => {
  it('suggests the second shard beside the first', () => {
    expect(suggestPleGguf('D:\m\Flash-IQ3_XXS-00001-of-00002.gguf')).toBe('D:\m\Flash-IQ3_XXS-00002-of-00002.gguf');
    expect(suggestPleGguf('/m/x-00001-of-00004.GGUF')).toBe('/m/x-00002-of-00004.GGUF');
  });

  it('suggests nothing for another shard, a single shard or a plain file', () => {
    expect(suggestPleGguf('/m/x-00002-of-00002.gguf')).toBeNull();
    expect(suggestPleGguf('/m/x-00001-of-00001.gguf')).toBeNull();
    expect(suggestPleGguf('/m/single.gguf')).toBeNull();
    expect(suggestPleGguf(undefined)).toBeNull();
  });
});

describe('pleAfterNativeChange', () => {
  const a1 = 'D:/a-00001-of-00002.gguf';
  const a2 = 'D:/a-00002-of-00002.gguf';
  const b1 = 'D:/b-00001-of-00002.gguf';
  const b2 = 'D:/b-00002-of-00002.gguf';

  it('fills an empty field', () => {
    expect(pleAfterNativeChange('', undefined, a1)).toBe(a2);
  });

  it('replaces the suggestion made for the previous shard', () => {
    expect(pleAfterNativeChange(a2, a1, b1)).toBe(b2);
  });

  it('keeps what the user typed', () => {
    expect(pleAfterNativeChange('E:/own.gguf', a1, b1)).toBe('E:/own.gguf');
  });

  it('leaves an empty field empty when the new file has no sibling', () => {
    expect(pleAfterNativeChange('', a1, 'D:/single.gguf')).toBeUndefined();
  });
});

describe('parseModelParam', () => {
  it('reads an empty field as unset', () => {
    expect(parseModelParam(spec('sampling.top_k'), '  ')).toEqual({ value: undefined });
  });

  it('applies Strata ranges', () => {
    expect(parseModelParam(spec('sampling.top_k'), '20')).toEqual({ value: 20 });
    expect('error' in parseModelParam(spec('sampling.top_k'), '65')).toBe(true);
    expect('error' in parseModelParam(spec('sampling.top_k'), '2.5')).toBe(true);
    expect('error' in parseModelParam(spec('sampling.top_p'), '0')).toBe(true);
    expect(parseModelParam(spec('sampling.min_p'), '0')).toEqual({ value: 0 });
    expect('error' in parseModelParam(spec('idle_unload_s'), '-1')).toBe(true);
  });

  it('reads booleans, choices and name lists', () => {
    expect(parseModelParam(spec('lazy_load'), 'false')).toEqual({ value: false });
    expect(parseModelParam(spec('effort_position'), 'end')).toEqual({ value: 'end' });
    expect('error' in parseModelParam(spec('effort_position'), 'middle')).toBe(true);
    expect(parseModelParam(spec('aliases'), 'a, b,')).toEqual({ value: ['a', 'b'] });
  });

  it('round-trips through the control text', () => {
    expect(modelParamText(['a', 'b'])).toBe('a, b');
    expect(modelParamText(false)).toBe('false');
    expect(modelParamText(undefined)).toBe('');
  });
});

describe('the list itself', () => {
  it('leaves out open_browser, which DPC always writes false', () => {
    expect(STRATA_MODEL_PARAMS.some((s) => s.key === 'open_browser')).toBe(false);
    expect(STRATA_MODEL_PARAMS.length).toBe(14);
  });
});

describe('status formatting', () => {
  it('marks the base config value as such', () => {
    expect(basePlaceholder(131072, 'engine default')).toBe('base config: 131072');
    expect(basePlaceholder(null, 'engine default')).toBe('engine default');
  });

  it('compares versions only when both are known', () => {
    expect(versionDiffers('0.1.47', '0.1.46')).toBe(true);
    expect(versionDiffers('0.1.46', ' 0.1.46 ')).toBe(false);
    expect(versionDiffers(null, '0.1.46')).toBeNull();
  });

  it('summarises the conversation cache', () => {
    expect(cacheSummaryLine({ enabled: true, budget_mib: 10240, slots: 4, parked: 2, bytes: 3 << 20,
      parks: 5, restores: 3, evictions: 0 }))
      .toBe('budget 10,240 MiB / 4 slots; parked 2 (3 MiB); parks 5, restores 3, evictions 0');
    expect(cacheSummaryLine({ enabled: false })).toBe('off');
    expect(cacheSummaryLine(null)).toBe('not reported');
    expect(formatMib(undefined)).toBe('—');
  });

  it('names the free commit metric, commit first', () => {
    expect(freeCommitLine({ avail_page_file_mib: 3686, total_page_file_mib: 108236,
      avail_phys_mib: 11305, total_phys_mib: 63168 }))
      .toBe('free commit (GlobalMemoryStatusEx ullAvailPageFile) 3,686 of 108,236 MiB; '
        + 'free physical (ullAvailPhys) 11,305 of 63,168 MiB');
    expect(freeCommitLine(null)).toBe('free commit (GlobalMemoryStatusEx): not measured on this OS');
    expect(freeCommitLine(undefined)).toContain('not read yet');
  });
});

describe('the cache admission fields', () => {
  const by = (key: string) => STRATA_CACHE_ADMISSION.find((s) => s.key === key)!;

  it("carry Strata's own defaults and flags", () => {
    expect(STRATA_CACHE_ADMISSION.map((s) => [s.key, s.flag, s.strataDefault])).toEqual([
      ['conversation_cache_min_free_mib', '--conversation-cache-min-free-mib', 2560],
      ['conversation_cache_min_tokens', '--conversation-cache-min-tokens', 0],
      ['conversation_cache_park_gain', '--conversation-cache-park-gain', 0],
    ]);
  });

  it('say the RAM floor is physical RAM, not commit', () => {
    const help = by('conversation_cache_min_free_mib').help;
    expect(help).toContain('Physical RAM');
    expect(help).toContain('not free commit');
  });

  it('name the default that is written, and a base value it overrides', () => {
    const spec = by('conversation_cache_min_free_mib');
    expect(admissionPlaceholder(spec, undefined)).toBe('Strata default 2560, written');
    expect(admissionPlaceholder(spec, 2560)).toBe('Strata default 2560, written');
    expect(admissionPlaceholder(spec, 2048)).toBe("Strata default 2560, written — the base config's 2048 is not used");
  });

  it('summarise the start config flags and the engine report', () => {
    expect(cacheFlagsLine({ conversation_cache_mib: 10240, conversation_cache_slots: null,
      conversation_cache_min_free_mib: 2560, conversation_cache_min_tokens: 0, conversation_cache_park_gain: 4096 }))
      .toBe('budget 10,240 MiB, slots engine default 4, RAM floor 2,560 MiB (physical RAM), '
        + 'shortest parked 0 tokens, park gain 4,096 tokens');
    expect(cacheFlagsLine(null)).toContain('no start config');
    expect(engineAdmissionLine({ min_free_mib: 2560, min_tokens: 0, park_gain: null }))
      .toBe('RAM floor 2,560 MiB, shortest parked 0 tokens, park gain not reported');
    expect(engineAdmissionLine(undefined)).toBe('not reported');
  });
});
