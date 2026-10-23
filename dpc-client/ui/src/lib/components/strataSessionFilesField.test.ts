import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// Same approach as strataBudgetFields.test.ts: no mount harness, so read the
// markup and run the handler the field calls.
function block(id: string): string {
  const start = src.indexOf(`id="${id}-{i}"`);
  expect(start, `${id} is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('</p>', start));
}

// setStrataPath as the script defines it, with the base-config inspection stubbed out.
const setStrataPath = (() => {
  const m = src.match(/function setStrataPath\(i: number, key: StrataPathKey, raw: string\) \{([\s\S]*?)\n  \}/);
  expect(m, 'setStrataPath is in the script').not.toBeNull();
  const body = m![1].replace(/ as any/g, '');
  return (providers: Record<string, unknown>[], i: number, key: string, raw: string) => {
    const run = new Function('editedConfig', 'i', 'key', 'raw', 'pleAfterNativeChange', 'inspectStrataBase', body);
    run({ providers }, i, key, raw, () => null, () => undefined);
    return providers[i];
  };
})();

describe('Strata session files folder field', () => {
  it('is a text input with the alias value, the base config value as placeholder and a Browse button', () => {
    const b = block('strata-slot-save');
    expect(b).toContain('value={sp.slot_save_path ?? \'\'}');
    expect(b).toContain("setStrataPath(i, 'slot_save_path', e.currentTarget.value)");
    expect(b).toContain("strataBase(strataInspect, i, 'slot_save_path'), 'off — empty'");
    expect(b).toContain("pickStrataPath(i, 'slot_save_path')");
    expect(b).not.toContain('bind:value');
  });

  it('opens a folder dialog, not a file dialog', () => {
    expect(src).toMatch(/slot_save_path: \{ title: '[^']+', directory: true \}/);
  });

  it('is labelled with Strata\'s own key and sits after the server window, last of the cache section', () => {
    expect(src).toContain('Session files folder (slot_save_path)');
    const labels = [...src.matchAll(/<label for="([a-z-]+)-\{i\}">/g)].map((m) => m[1]);
    const at = labels.indexOf('strata-maxctx');
    expect(at).toBeGreaterThan(-1);
    expect(labels[at + 1]).toBe('strata-slot-save');
  });

  it('the handler writes the trimmed path and drops the key when emptied', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setStrataPath(providers, 0, 'slot_save_path', '  D:\strata-sessions ').slot_save_path).toBe('D:\strata-sessions');
    const p = setStrataPath(providers, 0, 'slot_save_path', '');
    expect('slot_save_path' in JSON.parse(JSON.stringify(p))).toBe(false);
  });
});
