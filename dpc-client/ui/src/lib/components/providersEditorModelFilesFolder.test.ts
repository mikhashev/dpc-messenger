import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// No mount harness: the "Model files folder" control is checked in the source, the
// way the other ProvidersEditor guards do.
const snippet = (name: string) => {
  const start = src.indexOf(`{#snippet ${name}(`);
  expect(start, `${name} snippet is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('{/snippet}', start));
};

describe('the Local engines block starts with the Model files folder', () => {
  it('shows the resolved path from local_engine_status, above the card holder', () => {
    const top = snippet('cardStatus');
    expect(top).toContain('<label for="model-files-dir">Model files folder</label>');
    expect(top).toContain("value={engineStatus?.model_files_dir ?? ''}");
    expect(top.indexOf('Model files folder')).toBeLessThan(top.indexOf('Card holder'));
    expect(src).toContain("model_files_dir: r.model_files_dir ?? ''");
  });

  it('Browse picks a directory and Default sends an empty path, both through set_model_files_dir', () => {
    const top = snippet('cardStatus');
    expect(top).toContain('on:click={pickModelFilesDir}');
    expect(top).toContain("on:click={() => setModelFilesDir('')}");
    const pick = src.match(/async function pickModelFilesDir\(\) \{([\s\S]*?)\n  \}\n/);
    expect(pick).not.toBeNull();
    expect(pick![1]).toContain('directory: true');
    expect(pick![1]).toContain('await setModelFilesDir(picked)');
    expect(src).toContain("sendCommand('set_model_files_dir', { path })");
  });

  it('a change reads the status again, whether the command succeeded or was refused', () => {
    const m = src.match(/async function setModelFilesDir\(path: string\) \{([\s\S]*?)\n  \}\n/);
    expect(m).not.toBeNull();
    expect(m![1]).toContain("r?.message || 'Could not set the model files folder'");
    const finallyAt = m![1].indexOf('finally');
    expect(finallyAt).toBeGreaterThan(0);
    expect(m![1].slice(finallyAt)).toContain('await loadEngineStatus()');
  });
});
