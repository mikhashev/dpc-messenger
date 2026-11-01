import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// No mount harness: the card-level status is checked in the source, the way the
// other ProvidersEditor guards do.
const count = (needle: string) => src.split(needle).length - 1;
const snippet = (name: string) => {
  const start = src.indexOf(`{#snippet ${name}(`);
  expect(start, `${name} snippet is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('{/snippet}', start));
};

describe('the card holder sits once, at the top of the Providers list', () => {
  it('the holder line and the stop button each appear once', () => {
    expect(count('<strong>Card holder:</strong>')).toBe(1);
    expect(count("engineAction('stop_local_engine'")).toBe(1);
  });

  it('neither is inside the per-alias strataStatus snippet', () => {
    const alias = snippet('strataStatus');
    expect(alias).not.toContain('Card holder');
    expect(alias).not.toContain('stop_local_engine');
    const top = snippet('cardStatus');
    expect(top).toContain('Card holder');
    expect(top).toContain("engineAction('stop_local_engine'");
    expect(top).toContain('freeCommitLine(engineStatus.free_commit)');
  });

  it('the block is rendered above the provider cards for either local engine', () => {
    const at = src.indexOf('{@render cardStatus()}');
    expect(at).toBeGreaterThan(0);
    expect(at).toBeLessThan(src.indexOf('<div class="providers-list">'));
    const cond = src.slice(src.lastIndexOf('{#if', at), at);
    expect(cond).toContain("'strata'");
    expect(cond).toContain("'llamacpp_server'");
  });

  it('the status loads for a llama-server alias too', () => {
    const m = src.match(/\$: hasLocalEngine = ([^;]*);/);
    expect(m, 'hasLocalEngine is declared').not.toBeNull();
    expect(m![1]).toContain("'llamacpp_server'");
    expect(m![1]).toContain("'strata'");
    expect(src).toContain('if (open && hasLocalEngine && !engineStatusLoadedForOpen)');
  });
});
