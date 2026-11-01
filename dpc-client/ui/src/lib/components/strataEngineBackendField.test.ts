import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';
import { ENGINE_BACKEND_HELP, ENGINE_BACKEND_OWN_FOLDER, engineBackendOptions } from '../utils/strataFields';

// Same approach as strataSessionFilesField.test.ts: no mount harness, so read the
// markup and run the helper the select renders.
function block(id: string): string {
  const start = src.indexOf(`id="${id}-{i}"`);
  expect(start, `${id} is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('</div>', start));
}

describe('Strata engine backend select', () => {
  it("offers Strata's own pick first, then the backend's list for this machine", () => {
    const options = engineBackendOptions(['cuda', 'hip', 'hip-rdna2', 'hip-rdna3', 'hip-rx9070'], undefined);
    expect(options[0]).toEqual({ value: '', label: 'Strata picks for this card (default)' });
    expect(options.slice(1).map((o) => o.value)).toEqual(['cuda', 'hip', 'hip-rdna2', 'hip-rdna3', 'hip-rx9070']);
  });

  it('keeps a saved value the machine does not offer, so the save names why', () => {
    expect(engineBackendOptions(['metal'], 'hip-rdna3').map((o) => o.value)).toEqual(['', 'metal', 'hip-rdna3']);
    expect(engineBackendOptions(null, '').map((o) => o.value)).toEqual(['']);
  });

  it('says only AMD builds can be chosen', () => {
    expect(ENGINE_BACKEND_HELP).toBe('Only AMD builds can be chosen; CUDA 12 or 13 is picked by Strata from the driver.');
  });

  it('is a select fed by local_engine_status and shows the help', () => {
    const b = block('strata-engine-backend');
    expect(b).toContain("value={sp.strata_engine_backend ?? ''}");
    expect(b).toContain('engineBackendOptions(engineStatus?.engine_backends, sp.strata_engine_backend)');
    expect(b).toContain('setStrataEngineBackend(i, e.currentTarget.value)');
    expect(b).toContain('{ENGINE_BACKEND_HELP}');
    expect(src).toContain('<label for="strata-engine-backend-{i}">Engine backend</label>');
    expect(src).toContain('engine_backends: r.engine_backends ?? []');
  });

  it('is disabled with a note while a Strata folder is set, since it applies only to DPC\'s own install', () => {
    const b = block('strata-engine-backend');
    expect(b).toContain('disabled={!!sp.strata_install_dir}');
    const tail = src.slice(src.indexOf('id="strata-engine-backend-{i}"'));
    const note = tail.slice(tail.indexOf('</select>'), tail.indexOf('{/if}'));
    expect(note).toContain('{#if sp.strata_install_dir}');
    expect(note.indexOf('{ENGINE_BACKEND_OWN_FOLDER}')).toBeLessThan(note.indexOf('{:else}'));
    expect(note.indexOf('{ENGINE_BACKEND_HELP}')).toBeGreaterThan(note.indexOf('{:else}'));
    expect(ENGINE_BACKEND_OWN_FOLDER).toBe("Applies only to DPC's own install: your Strata folder installs its own engine.");
  });

  it('the setter drops the key for the default', () => {
    const m = src.match(/function setStrataEngineBackend\(i: number, value: string\) \{([\s\S]*?)\n  \}/);
    expect(m).not.toBeNull();
    const run = new Function('editedConfig', 'i', 'value', m![1].replace(/ as any/g, ''));
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    run({ providers }, 0, 'hip-rdna3');
    expect(providers[0].strata_engine_backend).toBe('hip-rdna3');
    run({ providers }, 0, '');
    expect('strata_engine_backend' in providers[0]).toBe(false);
  });
});
