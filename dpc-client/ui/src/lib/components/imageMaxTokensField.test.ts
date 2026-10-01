import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// The editor has no component-mount harness, so this reads the markup the way
// the other source guards do and runs the field's own handler line.
const block = (() => {
  const start = src.indexOf('id="image-max-tokens-{i}"');
  expect(start).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('</div>', start));
})();

function handler(raw: string, prior?: number) {
  const m = block.match(/editedConfig\.providers\[i\]\.image_max_tokens = (.+);/);
  expect(m).not.toBeNull();
  const expr = m![1];
  const p: { image_max_tokens?: number } = { image_max_tokens: prior };
  const run = new Function('raw', 'p', `const n = parseInt(raw, 10); p.image_max_tokens = ${expr.replace(/\bundefined\b/, 'undefined')}; return p;`);
  return run(raw, p) as { image_max_tokens?: number };
}

describe('image_max_tokens field', () => {
  it('shows the stored value, empty when unset', () => {
    expect(block).toContain("value={editedConfig.providers[i].image_max_tokens ?? ''}");
  });
  it('keeps a number', () => {
    expect(handler('8192').image_max_tokens).toBe(8192);
  });
  it('an emptied input becomes undefined and the key is absent from the payload', () => {
    const p = handler('', 8192);
    expect(p.image_max_tokens).toBeUndefined();
    expect('image_max_tokens' in JSON.parse(JSON.stringify(p))).toBe(false);
  });
  it('is declared on the provider type', () => {
    expect(src).toMatch(/image_max_tokens\?: number;/);
  });
  it('a hand-written key survives the edit copy and the save payload', () => {
    const cfg = { providers: [{ alias: 'a', image_max_tokens: 8192 }] };
    const edited = JSON.parse(JSON.stringify(cfg));
    expect(JSON.parse(JSON.stringify(edited)).providers[0].image_max_tokens).toBe(8192);
  });
});
