import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';
import { SSD_TIER_LABEL, SSD_TIER_HINT, ssdTierExtraArgsNote } from '../utils/strataFields';

// Same approach as strataBudgetFields.test.ts: no mount harness, so read the markup and run
// the handler the field calls.
function block(id: string): string {
  const start = src.indexOf(`id="${id}-{i}"`);
  expect(start, `${id} is in the markup`).toBeGreaterThan(0);
  return src.slice(src.lastIndexOf('<div', start), src.indexOf('</div>', start));
}

const setNum = (() => {
  const m = src.match(/function setNum\(i: number, key: keyof Provider, raw: string, float = false\) \{([\s\S]*?)\n  \}/);
  expect(m, 'setNum is in the script').not.toBeNull();
  const body = m![1].replace(/ as any/g, '');
  return (providers: Record<string, unknown>[], i: number, key: string, raw: string) => {
    const run = new Function('editedConfig', 'i', 'key', 'raw', 'float', body);
    run({ providers }, i, key, raw, false);
    return providers[i];
  };
})();

describe('Strata SSD tier field', () => {
  it('sits in the conversation cache section, after the VRAM reserve and before the server window, with the label and hint', () => {
    const at = src.indexOf('id="strata-spill-mib-{i}"');
    expect(at).toBeGreaterThan(src.indexOf('id="strata-{spec.key}-{i}"'));
    expect(at).toBeGreaterThan(src.indexOf('id="strata-vram-reserve-{i}"'));
    expect(at).toBeLessThan(src.indexOf('id="strata-maxctx-{i}"'));
    const b = block('strata-spill-mib');
    expect(b).toContain('{SSD_TIER_LABEL}');
    expect(b).toContain("value={sp.conversation_spill_mib ?? ''}");
    expect(b).toContain("setNum(i, 'conversation_spill_mib', e.currentTarget.value)");
    expect(b).toContain('min="0"');
    expect(b).toContain('{SSD_TIER_HINT}');
  });

  it('shows the extra flags note inline, as a warning, under the field', () => {
    const b = block('strata-spill-mib');
    expect(b).toContain('<p class="help-text warn">{ssdTierExtraArgsNote(sp.extra_args)}</p>');
  });

  it('is typed on the provider', () => {
    expect(src).toContain('conversation_spill_mib?: number;');
  });

  it('stores a number, keeps 0 (off, written) and leaves the key out when emptied', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setNum(providers, 0, 'conversation_spill_mib', '32768').conversation_spill_mib).toBe(32768);
    expect(setNum(providers, 0, 'conversation_spill_mib', '0').conversation_spill_mib).toBe(0);
    const p = setNum(providers, 0, 'conversation_spill_mib', '');
    expect('conversation_spill_mib' in JSON.parse(JSON.stringify(p))).toBe(false);
  });

  it('carries the label and the hint the owner asked for', () => {
    expect(SSD_TIER_LABEL).toBe('SSD tier for parked conversations (conversation_spill_mib)');
    expect(SSD_TIER_HINT).toContain('0 = off');
    expect(SSD_TIER_HINT).toContain('inside the KV snapshots folder');
    expect(SSD_TIER_HINT).toContain('pictures');
    expect(SSD_TIER_HINT).toContain('only while the engine runs');
    expect(SSD_TIER_HINT).toContain('32,768');
  });
});

describe('ssdTierExtraArgsNote', () => {
  it('is empty when extra flags name neither flag, or are not a list', () => {
    expect(ssdTierExtraArgsNote(undefined)).toBe('');
    expect(ssdTierExtraArgsNote([])).toBe('');
    expect(ssdTierExtraArgsNote(['--spec', '4'])).toBe('');
    expect(ssdTierExtraArgsNote('--conversation-spill-mib')).toBe('');
  });

  it('names each flag found and says it overrides the field', () => {
    expect(ssdTierExtraArgsNote(['--conversation-spill-mib', '0'])).toContain('--conversation-spill-mib');
    const both = ssdTierExtraArgsNote([' --conversation-spill-dir ', 'D:/x', '--conversation-spill-mib', '0']);
    expect(both).toContain('--conversation-spill-mib and --conversation-spill-dir');
    expect(both).toContain('override it');
  });
});
