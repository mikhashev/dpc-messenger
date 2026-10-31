import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';
import { STRATA_MODEL_PARAMS } from '../utils/strataFields';

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
  { id: 'strata-resident-gib', key: 'resident_budget_gib', handler: "setNum(i, 'resident_budget_gib', e.currentTarget.value, true)" },
  { id: 'strata-vram-reserve', key: 'vram_reserve_mib', handler: 'setStrataVramReserve(i, e.currentTarget.value)' },
];

describe('Strata resident budget and VRAM reserve fields', () => {
  for (const f of FIELDS) {
    it(`${f.key} shows the alias value and the base config's as placeholder`, () => {
      const b = block(f.id);
      expect(b).toContain(`value={sp.${f.key} ?? ''}`);
      expect(b.includes(`strataBase(strataInspect, i, '${f.key}')`)
        || b.includes('strataVramReservePlaceholder(strataInspect, i)')).toBe(true);
      expect(b).toContain('<p class="help-text">');
    });

    it(`${f.key} is written through setNum (no bind:value inside the providers loop)`, () => {
      const b = block(f.id);
      expect(b).toContain(`on:input={(e) => ${f.handler}}`);
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

// A function of the script by name, rebuilt over plain values: the body only, its parameters named.
function script(name: string, params: string[], scope: string[]) {
  const m = src.match(new RegExp(String.raw`function ${name}\([^)]*\)[^{]*\{([\s\S]*?)\n  \}`));
  expect(m, name + ' is in the script').not.toBeNull();
  return new Function(...scope, ...params, m![1].replace(/ as any/g, ''));
}

describe('a Strata key with a typed alias field is not offered a second time', () => {
  it('the keys the editor leaves out of the model-parameters list are the ones with a typed field', () => {
    const m = src.match(/const TYPED_FIELD_PARAMS = new Set\(\[([^\]]*)\]\)/);
    expect(m, 'TYPED_FIELD_PARAMS is in the script').not.toBeNull();
    const left = [...m![1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
    // A typed field is an input of the Strata section above the model-parameters list whose label
    // names the key's engine flag, "(--vram-reserve-mib)".
    const start = src.indexOf("editedConfig.providers[i].type === 'strata'}");
    const section = src.slice(start, src.indexOf('Model parameters (Strata', start));
    expect(start).toBeGreaterThan(0);
    const typed = STRATA_MODEL_PARAMS.map((s) => s.key).filter((k) => section.includes(`(--${k.replace(/_/g, '-')})`));
    expect(typed).toEqual(['vram_reserve_mib']);
    expect(left).toEqual(typed);
  });

  it('the list the editor loops over is the mirror minus those keys', () => {
    expect(src).toContain('const STRATA_PARAMS_LISTED = STRATA_MODEL_PARAMS.filter((spec) => !TYPED_FIELD_PARAMS.has(spec.key));');
    expect(src).toContain('{#each STRATA_PARAMS_LISTED as spec}');
    expect(src).not.toContain('{#each STRATA_MODEL_PARAMS as spec}');
  });

  it('a typed reserve replaces an older model_params copy, and an emptied model_params goes', () => {
    const run = script('setStrataVramReserve', ['i', 'raw'], ['editedConfig', 'setNum']);
    const cfg: any = { providers: [{ alias: 'a', model_params: { vram_reserve_mib: 900, idle_unload_s: 60 } }] };
    const setNum = (i: number, k: string, r: string) => { cfg.providers[i][k] = parseInt(r); };
    run(cfg, setNum, 0, '1152');
    expect(cfg.providers[0].vram_reserve_mib).toBe(1152);
    expect(cfg.providers[0].model_params).toEqual({ idle_unload_s: 60 });
    const only: any = { providers: [{ alias: 'b', model_params: { vram_reserve_mib: 900 } }] };
    run(only, (i: number, k: string, r: string) => { only.providers[i][k] = parseInt(r); }, 0, '1152');
    expect('model_params' in only.providers[0]).toBe(false);
  });

  it('the empty field names what the next start sends: the older value, else the base, else 700', () => {
    const run = script('strataVramReservePlaceholder', ['inspect', 'i'], ['editedConfig', 'strataBase', 'basePlaceholder', 'modelParamText']);
    const basePlaceholder = (v: unknown, fallback: string) => (v === undefined || v === null || v === '' ? fallback : `base config: ${v}`);
    const at = (providers: any[], base: unknown) => run({ providers }, () => base, basePlaceholder, String, {}, 0);
    expect(at([{ alias: 'a' }], 1536)).toBe('base config: 1536');
    expect(at([{ alias: 'a' }], undefined)).toBe('engine default 700');
    expect(at([{ alias: 'a', model_params: { vram_reserve_mib: 900 } }], 1536)).toContain('900');
    expect(at([{ alias: 'a', model_params: { vram_reserve_mib: 900 } }], 1536)).not.toContain('1536');
  });
});

describe('Strata alias-level reasoning effort and max_tokens fields', () => {
  it('both are in the Strata section, written through the shared setters', () => {
    const effort = block('strata-reasoning-effort');
    expect(effort).toContain("on:change={(e) => setStr(i, 'reasoning_effort', e.currentTarget.value)}");
    expect(effort).toContain('value={sp.reasoning_effort ?? \'\'}');
    expect(effort).not.toContain('bind:value');
    const max = block('strata-max-tokens');
    expect(max).toContain("on:input={(e) => setNum(i, 'max_tokens', e.currentTarget.value)}");
    expect(max).toContain('value={sp.max_tokens ?? \'\'}');
    expect(max).toContain('placeholder="default 8192"');
    expect(max).not.toContain('bind:value');
  });

  it('they sit inside the Strata section, not the llama-server one', () => {
    const strata = src.indexOf("editedConfig.providers[i].type === 'strata'}");
    for (const id of ['strata-reasoning-effort', 'strata-max-tokens']) {
      expect(src.indexOf(`id="${id}-{i}"`)).toBeGreaterThan(strata);
    }
  });

  it('each is drawn once per alias, not once per entry of the model-parameters list', () => {
    const paramsLoop = src.indexOf('{#each STRATA_PARAMS_LISTED as spec}');
    expect(paramsLoop).toBeGreaterThan(-1);
    for (const id of ['strata-reasoning-effort', 'strata-max-tokens']) {
      expect(src.indexOf(`id="${id}-{i}"`)).toBeLessThan(paramsLoop);
    }
  });

  it('the empty select says what is sent, and the help says what unset means', () => {
    const b = block('strata-reasoning-effort');
    expect(b).toContain("Not set — the chat template's default is sent");
    expect(b).toContain('<p class="help-text">');
    expect(block('strata-max-tokens')).toContain('Empty sends');
  });

  it('an emptied effort or ceiling deletes the key instead of writing "" or 0', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'f', reasoning_effort: 'low', max_tokens: 16384 }];
    expect(setNum(providers, 0, 'max_tokens', '8192').max_tokens).toBe(8192);
    expect('max_tokens' in setNum(providers, 0, 'max_tokens', '')).toBe(false);
    const setStr = script('setStr', ['i', 'key', 'raw'], ['editedConfig']);
    const cfg: any = { providers };
    setStr(cfg, 0, 'reasoning_effort', 'xhigh');
    expect(cfg.providers[0].reasoning_effort).toBe('xhigh');
    setStr(cfg, 0, 'reasoning_effort', '');
    expect('reasoning_effort' in cfg.providers[0]).toBe(false);
  });
});
