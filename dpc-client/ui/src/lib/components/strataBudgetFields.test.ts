import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// The editor has no component-mount harness, so this reads the markup the way
// the other source guards do and runs the handler the fields call.
function block(id: string): string {
  const start = src.indexOf(`id="${id}-{i}"`);
  expect(start, `${id} is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('</div>', start));
}

// setNum as the script defines it, run against a plain provider list.
const setNum = (() => {
  const m = src.match(/function setNum\(i: number, key: keyof Provider, raw: string, float = false\) \{([\s\S]*?)\n  \}/);
  expect(m, 'setNum is in the script').not.toBeNull();
  const body = m![1].replace(/ as any/g, '');
  return (providers: Record<string, unknown>[], i: number, key: string, raw: string, float = false) => {
    const run = new Function('editedConfig', 'i', 'key', 'raw', 'float', body);
    run({ providers }, i, key, raw, float);
    return providers[i];
  };
})();

const FIELDS = [
  { id: 'strata-resident-gib', key: 'resident_budget_gib', float: true },
  { id: 'strata-vram-reserve', key: 'vram_reserve_mib', float: false },
];

describe('Strata resident budget and VRAM reserve fields', () => {
  for (const f of FIELDS) {
    it(`${f.key} shows the alias value and the base config's as placeholder`, () => {
      const b = block(f.id);
      expect(b).toContain(`value={sp.${f.key} ?? ''}`);
      expect(b).toContain(`strataBase(strataInspect, i, '${f.key}')`);
      expect(b).toContain('<p class="help-text">');
    });

    it(`${f.key} is written through setNum (no bind:value inside the providers loop)`, () => {
      const b = block(f.id);
      const call = f.float
        ? `setNum(i, '${f.key}', e.currentTarget.value, true)`
        : `setNum(i, '${f.key}', e.currentTarget.value)`;
      expect(b).toContain(`on:input={(e) => ${call}}`);
      expect(b).not.toContain('bind:value');
    });

    it(`${f.key} is declared on the provider type`, () => {
      expect(src).toMatch(new RegExp(`${f.key}\\?: number;`));
    });
  }

  it('the handler keeps a fractional budget, a whole reserve, and drops an emptied field', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setNum(providers, 0, 'resident_budget_gib', '30.5', true).resident_budget_gib).toBe(30.5);
    expect(setNum(providers, 0, 'vram_reserve_mib', '2048').vram_reserve_mib).toBe(2048);
    const p = setNum(providers, 0, 'resident_budget_gib', '', true);
    expect('resident_budget_gib' in JSON.parse(JSON.stringify(p))).toBe(false);
  });

  it('both sit right after the conversation cache fields', () => {
    const labels = [...src.matchAll(/<label for="([a-z-]+)-\{i\}">/g)].map((m) => m[1]);
    const at = labels.indexOf('strata-cc-slots');
    expect(at).toBeGreaterThan(-1);
    expect(labels.slice(at + 1, at + 3)).toEqual(['strata-resident-gib', 'strata-vram-reserve']);
  });
});
