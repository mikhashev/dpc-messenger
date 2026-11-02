import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// No mount harness: the idle-swap controls are checked in the source, the way the
// other ProvidersEditor guards do.
const snippet = (name: string) => {
  const start = src.indexOf(`{#snippet ${name}(`);
  expect(start, `${name} snippet is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('{/snippet}', start));
};

describe('the Local engines block carries the idle-swap settings under the Model files folder', () => {
  it('has the checkbox and the wait field, after the folder row and before the card holder', () => {
    const top = snippet('cardStatus');
    expect(top).toContain('Swap local models when the card holder is idle');
    expect(top).toContain('Wait for a busy holder, s');
    expect(top).toContain('checked={engineStatus?.swap_when_idle ?? true}');
    expect(top).toContain('value={engineStatus?.swap_wait_s ?? \'\'}');
    const folder = top.indexOf('{@render modelFilesFolderRow()}');
    expect(folder).toBeGreaterThan(0);
    expect(top.indexOf('Swap local models')).toBeGreaterThan(folder);
    expect(top.indexOf('Wait for a busy holder, s')).toBeGreaterThan(top.indexOf('Swap local models'));
    expect(top.indexOf('Wait for a busy holder, s')).toBeLessThan(top.indexOf('Card holder'));
  });

  it('a change goes through set_local_engine_swap with enabled and wait_s, and the status is read again', () => {
    const m = src.match(/async function setLocalEngineSwap\(enabled: boolean, waitText: string \| number\) \{([\s\S]*?)\n  \}\n/);
    expect(m).not.toBeNull();
    expect(m![1]).toContain("sendCommand('set_local_engine_swap', { enabled, wait_s: wait })");
    expect(m![1]).toContain("r?.message || 'Could not set the idle swap'");
    const finallyAt = m![1].indexOf('finally');
    expect(finallyAt).toBeGreaterThan(0);
    expect(m![1].slice(finallyAt)).toContain('await loadEngineStatus()');
  });

  it('a wait that is not a whole number is refused inline before any command is sent', () => {
    const m = src.match(/async function setLocalEngineSwap\(enabled: boolean, waitText: string \| number\) \{([\s\S]*?)\n  \}\n/);
    const body = m![1];
    expect(body.indexOf('parseSwapWait(waitText)')).toBeGreaterThanOrEqual(0);
    expect(body.indexOf('engineStatusError = SWAP_WAIT_REFUSAL')).toBeLessThan(body.indexOf('sendCommand'));
  });

  it('local_engine_status feeds both settings into the status', () => {
    expect(src).toContain('swap_when_idle: r.swap_when_idle ?? true, swap_wait_s: r.swap_wait_s ?? 300');
  });
});
