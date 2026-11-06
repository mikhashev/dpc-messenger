import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// Same approach as strataSessionFilesField.test.ts: no mount harness, so read the
// markup and run the handlers the fields call.
function fn(name: string): string {
  const m = src.match(new RegExp(String.raw`function ${name}\([^)]*\) \{([\s\S]*?)\n  \}`));
  expect(m, `${name} is in the script`).not.toBeNull();
  return m![1].replace(/ as any/g, '');
}

const setStrataDrafting = (providers: Record<string, unknown>[], i: number, raw: string) => {
  const run = new Function('editedConfig', 'i', 'raw', 'draftingFromSelect', fn('setStrataDrafting'));
  const ec = { providers };
  run(ec, i, raw, (r: string) => (r === 'mtp' || r === 'dflash2' ? r : undefined));
  return providers[i];
};

const setBool = (providers: Record<string, unknown>[], i: number, key: string, raw: string) => {
  const run = new Function('editedConfig', 'i', 'key', 'raw', fn('setBool'));
  run({ providers }, i, key, raw);
  return providers[i];
};

describe('Strata drafter dropdown', () => {
  it('sits beside the Draft layer (mtp) field, lists auto and shows auto, never a blank', () => {
    const at = src.indexOf('id="strata-drafting-{i}"');
    expect(at).toBeGreaterThan(0);
    expect(src.indexOf("spec.key === 'mtp'")).toBeLessThan(at);
    const b = src.slice(at, src.indexOf('</p>', at));
    expect(b).toContain('value={draftingSelectValue(sp.drafting)}');
    expect(b).toContain('{#each STRATA_DRAFTING_CHOICES as choice}');
    expect(b).toContain('DRAFTING_AUTO_LABEL');
    expect(b).toContain("draftingHelp(strataBase(strataInspect, i, 'drafting'))");
    expect(b).not.toContain('<option value="">');
  });

  it('stores mtp or dflash2 and leaves the key out for auto', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setStrataDrafting(providers, 0, 'dflash2').drafting).toBe('dflash2');
    expect(setStrataDrafting(providers, 0, 'mtp').drafting).toBe('mtp');
    const p = setStrataDrafting(providers, 0, 'auto');
    expect('drafting' in JSON.parse(JSON.stringify(p))).toBe(false);
  });
});

describe('Strata picture reader dropdown', () => {
  it('lists the three options with auto selected by default, never a blank', () => {
    const at = src.indexOf('id="strata-reader-{i}"');
    expect(at).toBeGreaterThan(0);
    const b = src.slice(at, src.indexOf('</p>', at));
    expect(b).toContain('value={pictureReaderSelectValue(sp.vision_elastic)}');
    expect(b).toContain('{#each PICTURE_READER_OPTIONS as o}');
    expect(b).toContain("pictureReaderHelp(strataBase(strataInspect, i, 'vision_elastic'))");
    expect(b).not.toContain('<option value="">');
  });

  it('writes a boolean for the two choices and drops the key for auto', () => {
    expect(src).toContain("setBool(i, 'vision_elastic', raw === 'auto' ? '' : raw)");
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setBool(providers, 0, 'vision_elastic', 'true').vision_elastic).toBe(true);
    expect(setBool(providers, 0, 'vision_elastic', 'false').vision_elastic).toBe(false);
    const p = setBool(providers, 0, 'vision_elastic', '');
    expect('vision_elastic' in JSON.parse(JSON.stringify(p))).toBe(false);
  });
});

describe('the status panel', () => {
  it('shows the drafter the engine chose', () => {
    expect(src).toContain('engineDraftingLine(row.metrics.drafter, row.metrics.drafting_note)');
  });
});
