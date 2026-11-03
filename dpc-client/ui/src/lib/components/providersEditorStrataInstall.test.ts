import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// No mount harness: the Add Provider form for type strata is checked in the source,
// the way providersEditorModelFilesFolder.test.ts does for the folder row.
const snippet = (name: string) => {
  const start = src.indexOf(`{#snippet ${name}(`);
  expect(start, `${name} snippet is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('{/snippet}', start));
};
const fn = (name: string) => {
  const m = src.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\)[^{]*\\{([\\s\\S]*?)\\n  \\}\\n`));
  expect(m, `${name} is a function of the component`).not.toBeNull();
  return m![1];
};
const addForm = () => src.slice(src.indexOf('<!-- Add Provider Form -->'));

describe('the Add form for strata keeps the Model files folder row and gains the run config select', () => {
  it('shows the one folder row, then the run config fields, the install block and the cache field', () => {
    const add = addForm();
    expect(add).toContain('{@render modelFilesFolderRow()}');
    expect(src.match(/<label for="model-files-dir">/g)).toHaveLength(1);
    const fields = add.indexOf('{@render strataRunConfigFields()}');
    const install = add.indexOf('{@render strataInstallBlock()}');
    const cache = add.indexOf('{@render strataCacheBudgetField()}');
    expect(fields).toBeGreaterThan(0);
    expect(install).toBeGreaterThan(fields);
    expect(cache).toBeGreaterThan(install);
    // all three sit in the branch of the strata type, not in a branch of another type
    expect(add.lastIndexOf("{#if newProvider.type === 'strata'}", fields)).toBeGreaterThan(add.lastIndexOf("{#if newProvider.type === 'llamacpp_server'}", fields));
  });

  it('the select "From the model files folder" lists run_configs by file name and fills strata_base_config', () => {
    const fields = snippet('strataRunConfigFields');
    expect(fields).toContain('<label for="new-strata-run-config">From the model files folder</label>');
    expect(fields).toContain('{#each newRunConfigs as o (o.path)}');
    expect(fields).toContain('<option value={o.path}>{o.label}</option>');
    expect(fields).toContain('on:change={(e) => chooseNewStrataConfig(e.currentTarget.value)}');
    expect(src).toContain('$: newRunConfigs = runConfigOptions(modelRunConfigs);');
    expect(src).toContain('modelRunConfigs = r.run_configs ?? [];');
    expect(fn('chooseNewStrataConfig')).toContain('newProvider.strata_base_config = value || undefined;');
  });

  it('the Add form has a Base config path with Browse (json), as the Edit form does', () => {
    const fields = snippet('strataRunConfigFields');
    expect(fields).toContain('<label for="new-strata-base">Base config</label>');
    expect(fields).toContain('on:click={pickNewStrataConfig}');
    const pick = fn('pickNewStrataConfig');
    expect(pick).toContain("extensions: ['json']");
    expect(pick).toContain('directory: false');
  });
});

describe('Install a Strata model', () => {
  it('reads list_strata_models when the Add form opens for strata, and on Refresh', () => {
    expect(src).toContain("sendCommand('list_strata_models', {})");
    expect(src).toContain("$: wantsStrataModels = open && selectedTab === 'add' && newProvider.type === 'strata';");
    expect(src).toMatch(/\$: if \(wantsStrataModels && !strataModelsLoadedForForm\) \{[\s\S]*?loadStrataModels\(\);/);
    const block = snippet('strataInstallBlock');
    expect(block).toContain('Install a Strata model');
    expect(block).toMatch(/on:click=\{loadStrataModels\}[^>]*>Refresh<\/button>/);
  });

  it('shows a line while loading and the command error text otherwise (Control not installed)', () => {
    const block = snippet('strataInstallBlock');
    expect(block).toContain('{#if strataModelsLoading}');
    expect(block).toContain('Reading Strata\'s model list…');
    expect(block).toContain('{:else if strataModelsError}');
    expect(block).toContain('<p class="help-text warn">{strataModelsError}</p>');
    expect(fn('loadStrataModels')).toContain("strataModelsError = r?.message || r?.error || 'Could not read the list of Strata models';");
  });

  it('every size shows download, RAM and Strata\'s estimate by the util', () => {
    const block = snippet('strataInstallBlock');
    expect(block).toContain('{strataSizeLine(size)}');
    expect(src).toContain('STRATA_ESTIMATE_LABEL');
    expect(block).toContain('{STRATA_ESTIMATE_LABEL} is Strata');
  });

  it('a size already installed shows "installed" and no buttons', () => {
    const block = snippet('strataInstallBlock');
    const installed = block.indexOf('{#if size.installed}');
    const other = block.indexOf('{:else}', installed);
    const end = block.indexOf('{/if}', other);
    expect(installed).toBeGreaterThan(0);
    expect(block.slice(installed, other)).toContain('installed</span>');
    expect(block.slice(installed, other)).not.toContain('<button');
    expect(block.slice(other, end)).toContain('>Install</button>');
    expect(block.slice(other, end)).toContain('>Use my files…</button>');
  });

  it("Strata's on_this_pc is advice: no button, select or field is disabled by it", () => {
    for (const name of ['strataInstallBlock', 'strataRunConfigFields', 'strataCacheBudgetField']) {
      const s = snippet(name);
      for (const m of s.matchAll(/disabled=\{([^}]*)\}/g)) expect(m[1]).not.toMatch(/on_this_pc/);
    }
    const block = snippet('strataInstallBlock');
    expect(block).toContain('disabled={strataInstallBusy}');
    expect(block).toContain('disabled={strataInstallBusy || !canBrowse}');
    expect(fn('installNewStrataModel')).not.toContain('on_this_pc');
  });

  it('Install sends install_strata_model with the family, the model and the size\'s images', () => {
    const block = snippet('strataInstallBlock');
    expect(block).toContain('on:click={() => installNewStrataModel(family, size)}');
    const install = fn('installNewStrataModel');
    expect(install).toContain('family: family.family, model: size.model, images: size.images ?? true');
    expect(install).toContain("sendCommand('install_strata_model', params)");
    expect(install).toContain('if (ggufDir) params.gguf_dir = ggufDir;');
  });

  it('Use my files… picks a directory with the Tauri dialog and sends it as gguf_dir', () => {
    const block = snippet('strataInstallBlock');
    expect(block).toContain('on:click={() => installNewStrataModelFromMyFiles(family, size)}');
    const mine = fn('installNewStrataModelFromMyFiles');
    expect(mine).toContain("import('@tauri-apps/plugin-dialog')");
    expect(mine).toContain('directory: true');
    expect(mine).toContain('await installNewStrataModel(family, size, picked)');
  });

  it('an error from the command shows in the block', () => {
    const install = fn('installNewStrataModel');
    expect(install).toContain("strataInstallError = r?.message || r?.error || 'Could not start the install';");
    expect(snippet('strataInstallBlock')).toContain('<p class="help-text warn">{strataInstallError}</p>');
  });

  it('an install answered "already installed" takes its run_config at once', () => {
    const install = fn('installNewStrataModel');
    expect(install).toMatch(/else if \(r\.run_config\) \{[\s\S]*?await useInstalledStrataConfig\(r\.run_config\);/);
  });
});

describe('a finished install fills the base config', () => {
  it('model_download_completed for strata-model with run_config, while the Add form is open for strata', () => {
    expect(src).toContain('modelDownloadCompleted.subscribe((done) => {');
    expect(src).toContain("import { STRATA_MODEL_DOWNLOAD } from '$lib/utils/strataModelDownload';");
    expect(src).toContain('if (!done || done.model_name !== STRATA_MODEL_DOWNLOAD || !done.run_config) return;');
    expect(src).toContain("if (!(open && selectedTab === 'add' && newProvider.type === 'strata')) return;");
    expect(src).toContain('useInstalledStrataConfig(done.run_config);');
    expect(src).toContain('onDestroy(stopWatchingStrataInstall);');
  });

  it('the folder is listed again before the run config is taken', () => {
    const use = fn('useInstalledStrataConfig');
    expect(use.indexOf('await loadModelFiles();')).toBeGreaterThanOrEqual(0);
    expect(use.indexOf('chooseNewStrataConfig(runConfig')).toBeGreaterThan(use.indexOf('await loadModelFiles();'));
  });
});

describe('the cache budget suggestion', () => {
  it('is the util\'s rule, from the free commit of the status read now, for the size of the choice', () => {
    const offer = fn('offerCacheSuggestion');
    expect(offer).toContain('await loadEngineStatus();');
    expect(offer).toContain('const free = freeCommitMibOf(engineStatus?.free_commit);');
    expect(offer).toContain('suggestedConversationCacheMib(free, ramNeededGb)');
    expect(offer).toContain('newCacheMayNotFit = cacheSuggestionMayNotFit(free, ramNeededGb);');
    expect(offer).toContain('newCacheSuggestedAt = new Date();');
    const choose = fn('chooseNewStrataConfig');
    expect(choose).toContain('sizeForRunConfig(strataFamilies, value) ?? fallbackSize ?? null');
    expect(choose).toContain('offerCacheSuggestion(size?.ram_needed_gb ?? null);');
    expect(fn('installNewStrataModel')).toContain('offerCacheSuggestion(size.ram_needed_gb)');
  });

  it('fills only an empty field or a suggestion, never a value typed by hand', () => {
    const offer = fn('offerCacheSuggestion');
    expect(offer).toContain('newProvider.conversation_cache_mib != null && !newCacheSuggestedAt');
    expect(offer).toContain('if (ramNeededGb == null) return;');
    expect(offer).toContain('if (suggested == null) return;');
  });

  it('typing in the field drops the mark; the mark text is the agreed one', () => {
    expect(fn('setNewCacheMib')).toContain('newCacheSuggestedAt = null;');
    const field = snippet('strataCacheBudgetField');
    expect(field).toContain('on:input={(e) => setNewCacheMib(e.currentTarget.value)}');
    expect(field).toContain('{#if newCacheSuggestedAt && newProvider.conversation_cache_mib != null}');
    expect(field).toContain('{suggestionMarkText(newCacheSuggestedAt)}');
  });
});

describe('the resident budget suggestion in the Add form', () => {
  it('is rendered after the cache budget, in the branch of the strata type', () => {
    const add = addForm();
    const resident = add.indexOf('{@render strataResidentBudgetField()}');
    expect(resident).toBeGreaterThan(add.indexOf('{@render strataCacheBudgetField()}'));
    expect(add.lastIndexOf("{#if newProvider.type === 'strata'}", resident)).toBeGreaterThan(add.lastIndexOf("{#if newProvider.type === 'llamacpp_server'}", resident));
  });

  it("comes from the status read now, for the chosen size, after the base config's own budget was read", () => {
    const offer = fn('offerResidentSuggestion');
    expect(offer).toContain('await loadEngineStatus();');
    expect(offer).toContain('suggestedResidentBudget(engineStatus?.free_commit, size)');
    const choose = fn('chooseNewStrataConfig');
    expect(choose).toContain('sizeForRunConfig(strataFamilies, value) ?? fallbackSize ?? null');
    expect(choose).toContain('baseRead.then(() => offerResidentSuggestion(value, size));');
    expect(fn('readNewStrataWindow')).toContain('newStrataBaseResident = r.values?.resident_budget_gib ?? null;');
  });

  it('reaches an install too, with the whole size it asked for, once the install has a run config', () => {
    expect(fn('installNewStrataModel')).toContain('strataInstallSize = size;');
    expect(fn('useInstalledStrataConfig')).toContain('chooseNewStrataConfig(runConfig, strataInstallSize);');
    expect(fn('resetNewProviderForm')).toContain('strataInstallSize = null;');
  });

  it('a budget the save would refuse is said beside the field, in the Add form and the Edit form', () => {
    expect(snippet('strataResidentBudgetField')).toContain('<p class="help-text warn">{residentBudgetError(newProvider.resident_budget_gib)}</p>');
    expect(src).toContain('<p class="help-text warn">{residentBudgetError(sp.resident_budget_gib)}</p>');
  });

  it('never replaces a typed value, a moved-on choice or a budget the base config names', () => {
    const offer = fn('offerResidentSuggestion');
    expect(offer).toContain('newProvider.resident_budget_gib != null && !newResidentSuggestedAt');
    expect(offer).toContain('if (typedByHand() || newProvider.strata_base_config !== path) return;');
    expect(offer).toContain('newStrataBaseResident === null');
    expect(fn('chooseNewStrataConfig')).toContain('newStrataBaseResident = undefined;');
  });

  it('prefills the one field and no other', () => {
    const offer = fn('offerResidentSuggestion');
    expect(offer).toContain('newProvider.resident_budget_gib = suggestion?.gib ?? undefined;');
    expect(offer).not.toMatch(/vram_reserve_mib|kv_resident/);
    expect(src).not.toMatch(/newProvider\.(vram_reserve_mib|kv_resident)/);
  });

  it('typing drops the mark and the reading; both texts come from the util', () => {
    const typed = fn('setNewResidentGib');
    expect(typed).toContain('newResidentSuggestedAt = null;');
    expect(typed).toContain('newResidentSuggestion = null;');
    const field = snippet('strataResidentBudgetField');
    expect(field).toContain('on:input={(e) => setNewResidentGib(e.currentTarget.value)}');
    expect(field).toContain('{residentSuggestionMarkText(newResidentSuggestedAt)}');
    expect(field).toContain('{residentSuggestionWhy(newResidentSuggestion)}');
    expect(field).toContain("basePlaceholder(newStrataBaseResident, 'not in the base config')");
    expect(field).toContain('--vram-reserve-mib and --kv-resident are other settings you may tune');
    expect(field).toContain('this form derives no value for them');
  });
});

describe('the new alias carries what the form collected', () => {
  it('addNewProvider copies strata_base_config and conversation_cache_mib for a strata alias', () => {
    const add = fn('addNewProvider');
    const strata = add.slice(add.indexOf("newProvider.type === 'strata'"));
    expect(strata).toContain('if (newProvider.strata_base_config) provider.strata_base_config = newProvider.strata_base_config;');
    expect(strata).toContain('if (newProvider.conversation_cache_mib != null) provider.conversation_cache_mib = newProvider.conversation_cache_mib;');
    expect(strata.indexOf('provider.conversation_cache_mib')).toBeLessThan(strata.indexOf("newProvider.type === 'local_whisper'"));
    expect(strata).toContain('if (newProvider.resident_budget_gib != null) provider.resident_budget_gib = newProvider.resident_budget_gib;');
    expect(strata.indexOf('provider.resident_budget_gib')).toBeLessThan(strata.indexOf("newProvider.type === 'local_whisper'"));
  });

  it('a reset of the form forgets the suggestion mark and the install messages', () => {
    const reset = fn('resetNewProviderForm');
    expect(reset).toContain('newCacheSuggestedAt = null;');
    expect(reset).toContain('newResidentSuggestedAt = null;');
    expect(reset).toContain('newResidentSuggestion = null;');
    expect(reset).toContain('newStrataBaseResident = undefined;');
    expect(reset).toContain("strataInstallError = '';");
  });

  it('the alias is still suggested before the draft is validated', () => {
    const suggest = src.indexOf('$: if (!aliasTouched) {');
    const validate = src.indexOf('$: addProviderValidation = validateProviderDraftForAdd(');
    expect(suggest).toBeGreaterThan(0);
    expect(validate).toBeGreaterThan(suggest);
  });
});

describe('a chosen run config names the model of the new alias', () => {
  it('every way of choosing one reads its model_name through strata_inspect_base_config', () => {
    expect(fn('chooseNewStrataConfig')).toContain('fillModelNameFromRunConfig(value);');
    const fill = fn('fillModelNameFromRunConfig');
    expect(fill).toContain("sendCommand('strata_inspect_base_config', { path, install_dir: '' })");
    expect(fill).toContain('newProvider.model = r.model_name;');
    // the install, the select and the typed path all end in chooseNewStrataConfig
    expect(fn('useInstalledStrataConfig')).toContain('chooseNewStrataConfig(runConfig');
  });

  it('fills an empty Model field or an earlier fill, never a value typed by hand', () => {
    const fill = fn('fillModelNameFromRunConfig');
    expect(fill).toContain('!newProvider.model || newProvider.model === newModelFilledFromConfig');
    expect(fill).toContain('newModelFilledFromConfig = r.model_name;');
    expect(fill).toContain('newProvider.strata_base_config !== path || !mayFill()');
    expect(fn('resetNewProviderForm')).toContain('newModelFilledFromConfig = null;');
  });
});
