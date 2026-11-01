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
    const row = snippet('modelFilesFolderRow');
    expect(row).toContain('<label for="model-files-dir">Model files folder</label>');
    expect(row).toContain("value={engineStatus?.model_files_dir ?? ''}");
    const top = snippet('cardStatus');
    expect(top).toContain('{@render modelFilesFolderRow()}');
    expect(top.indexOf('modelFilesFolderRow')).toBeLessThan(top.indexOf('Card holder'));
    expect(src).toContain("model_files_dir: r.model_files_dir ?? ''");
  });

  it('Browse picks a directory and Default sends an empty path, both through set_model_files_dir', () => {
    const top = snippet('modelFilesFolderRow');
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
    expect(m![1].slice(finallyAt)).toContain('await Promise.all([loadEngineStatus(), loadModelFiles()])');
  });
});

describe('Add Provider and Edit pick a model from the model files folder', () => {
  const addForm = () => src.slice(src.indexOf('<!-- Add Provider Form -->'));

  it('the Add form shows the same Model files folder row for the local types, not a copy', () => {
    const add = addForm();
    expect(add).toContain("{#if newProvider.type === 'llamacpp_server' || newProvider.type === 'strata'}");
    expect(add).toContain('{@render modelFilesFolderRow()}');
    expect(src.match(/<label for="model-files-dir">/g)).toHaveLength(1);
  });

  it('list_model_files is called when the local form opens and after the folder changes', () => {
    expect(src).toContain("sendCommand('list_model_files', {})");
    expect(src).toMatch(/\$: wantsModelFiles = open && \(\s*\(selectedTab === 'add' && \(newProvider\.type === 'llamacpp_server' \|\| newProvider\.type === 'strata'\)\)/);
    expect(src).toMatch(/\$: if \(wantsModelFiles && !modelFilesLoadedForForm\) \{[\s\S]*?loadModelFiles\(\);/);
  });

  it('the Add form fills the GGUF path from the select and carries the chosen projector into the alias', () => {
    const add = addForm();
    expect(add).toContain("{@render modelFilePicker('new-model-file', newProvider.model ?? '', chooseNewModelFile)}");
    expect(add).toContain("{@render projectorPicker('new-mmproj', newProvider.model ?? '', newProvider.mmproj ?? ''");
    expect(src).toMatch(/function chooseNewModelFile\(path: string\) \{[\s\S]*?newProvider\.model = path;[\s\S]*?mmprojAfterChoosing\(modelFiles, path, newProvider\.mmproj\)/);
    expect(src).toContain('if (newProvider.mmproj) provider.mmproj = newProvider.mmproj;');
  });

  it('the Edit form of a llama-server alias has the same select under the GGUF path', () => {
    expect(src).toContain("{@render modelFilePicker(`gguf-file-${i}`, editedConfig.providers[i].gguf_path ?? ''");
    expect(src).toMatch(/function chooseEditedModelFile\(i: number, path: string\) \{[\s\S]*?provider\.gguf_path = path;[\s\S]*?mmprojAfterChoosing\(modelFiles, path, provider\.mmproj\)/);
  });

  it('the select lists the non-projector rows grouped by folder, by the util', () => {
    const picker = snippet('modelFilePicker');
    expect(picker).toContain('groupModelFiles(modelFiles)');
    expect(picker).toContain('<optgroup label={group.label}>');
    expect(snippet('projectorPicker')).toContain('projectorsBeside(modelFiles, modelPath)');
  });
});
