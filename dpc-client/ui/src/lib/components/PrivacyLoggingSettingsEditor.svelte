<!-- PrivacyLoggingSettingsEditor.svelte -->
<!-- Edit the device-information collection switches and the log levels of config.ini: -->
<!-- [system] auto_collect_device_info and collect_ai_models, [logging] level and -->
<!-- console_level, and the [logging.modules] overrides -->
<!-- (THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI, -->
<!-- audit/config_ui_parity_2026-11-27.md). None of these is applied live: the switches -->
<!-- are read when a collection runs (each backend start) and the levels once at start -->
<!-- (run_service.setup_logging) — every field's hint says so, and no live path exists. -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';
  import { allowedValue, boolValue, isModuleName, LOG_LEVELS } from '$lib/utils/privacyLogging';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  // The drafts hold bools for the checkboxes and one of their select's options for
  // each level; a load normalises whatever the command answered into one (the
  // getters answer values their readers already accept, so this guards the error
  // paths). The module map is keyed as [logging.modules] keys it.
  let plDraft = { auto_collect_device_info: true, collect_ai_models: false };
  let llDraft = { level: 'INFO', console_level: 'INFO', module_levels: {} as Record<string, string> };
  let newModule = '';
  let newModuleLevel = 'WARNING';
  let addError = '';

  let loading = false;
  let loadError = '';
  let plBusy = false, llBusy = false;
  let plError = '', llError = '';
  let plSaved = false, llSaved = false;

  // Loads the two sections each time the editor opens (the only dependency named
  // is `open`, so the statement runs exactly when it flips).
  $: if (open) loadAll();
  $: moduleNames = Object.keys(llDraft.module_levels).sort();

  async function loadAll() {
    loading = true;
    loadError = '';
    try {
      const pl = await sendCommand('get_privacy_logging_config', {});
      if (pl?.status === 'success') {
        plDraft = {
          auto_collect_device_info: boolValue(pl.auto_collect_device_info, true),
          collect_ai_models: boolValue(pl.collect_ai_models, false),
        };
        llDraft = {
          level: allowedValue(pl.level, LOG_LEVELS, 'INFO'),
          console_level: allowedValue(pl.console_level, LOG_LEVELS, 'INFO'),
          module_levels: (pl.module_levels && typeof pl.module_levels === 'object')
            ? Object.fromEntries(
                Object.entries(pl.module_levels)
                  .map(([name, level]): [string, string] => [name, allowedValue(level, LOG_LEVELS, 'WARNING')]))
            : {},
        };
      } else if (pl?.status === 'error') {
        loadError = pl.message || 'Could not read the privacy and logging settings';
      }
      plSaved = false;
      llSaved = false;
      plError = '';
      llError = '';
      addError = '';
    } catch (error) {
      loadError = `Error: ${error}`;
    } finally {
      loading = false;
    }
  }

  function close() {
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && !plBusy && !llBusy) close();
  }

  async function saveCollection() {
    plBusy = true;
    plError = '';
    try {
      const r = await sendCommand('save_privacy_logging_config', {
        config: {
          auto_collect_device_info: plDraft.auto_collect_device_info,
          collect_ai_models: plDraft.collect_ai_models,
        },
      });
      if (r?.status === 'success') {
        plSaved = true;
      } else {
        plError = r?.message || 'Could not save the collection switches';
        plSaved = false;
      }
    } catch (error) {
      plError = `Error: ${error}`;
      plSaved = false;
    } finally {
      plBusy = false;
    }
  }

  async function saveLogLevels() {
    llBusy = true;
    llError = '';
    try {
      const r = await sendCommand('save_privacy_logging_config', {
        config: {
          level: llDraft.level,
          console_level: llDraft.console_level,
          module_levels: llDraft.module_levels,
        },
      });
      if (r?.status === 'success') {
        llSaved = true;
      } else {
        llError = r?.message || 'Could not save the log levels';
        llSaved = false;
      }
    } catch (error) {
      llError = `Error: ${error}`;
      llSaved = false;
    } finally {
      llBusy = false;
    }
  }

  function addModule() {
    const name = newModule.trim();
    if (!isModuleName(name)) {
      addError = `"${name}" is not a dotted logger name (like httpx or telegram.ext).`;
      return;
    }
    if (llDraft.module_levels[name] !== undefined) {
      addError = `${name} is already in the list.`;
      return;
    }
    addError = '';
    llDraft.module_levels = { ...llDraft.module_levels, [name]: newModuleLevel };
    newModule = '';
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" on:keydown={handleKeydown} role="presentation">
    <div class="modal" role="dialog" aria-labelledby="privacy-logging-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="privacy-logging-title">Privacy and Logging</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        {#if loadError}
          <p class="section-error">{loadError}</p>
        {/if}

        <section class="settings-section">
          <h3>Device information collection</h3>
          <p class="section-hint">
            What this node writes into its device profile (device_context.json in the
            DPC home). The switches are read when a collection runs, and a collection
            runs at each backend start: a save applies from the next start, and turning
            collection off keeps the file a previous one wrote.
          </p>
          {#if plError}
            <p class="section-error">{plError}</p>
          {:else if plSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-group">
            <label for="pl-auto-collect">
              <input id="pl-auto-collect" type="checkbox" bind:checked={plDraft.auto_collect_device_info} disabled={plBusy} />
              Collect this device's profile (hardware and software)
            </label>
            <span class="field-hint wide">
              At each backend start the node writes CPU, RAM, GPU and disk facts plus
              OS, Python, shell, dev tools and package managers into device_context.json
              and references it from personal.json. This node's agents use it as
              context, and it can be sent — filtered by the privacy firewall — to a
              peer that runs inference for you. Applies from the next backend start.
            </span>
            <label for="pl-collect-ai-models">
              <input id="pl-collect-ai-models" type="checkbox" bind:checked={plDraft.collect_ai_models} disabled={plBusy} />
              Also list the local AI models in the profile (opt-in)
            </label>
            <span class="field-hint wide">
              Runs <code>ollama list</code> during a collection and stores the model
              names in the same profile (software.ai_models), so a peer that runs
              inference for you can see which models this node has — through the same
              privacy-firewall filtering as the rest of the profile. Off by default;
              while it is off, no model list is collected or sent. Applies from the
              next collection.
            </span>
          </div>
          <button class="btn-save" on:click={saveCollection} disabled={plBusy || loading}>
            {plBusy ? 'Saving...' : 'Save collection switches'}
          </button>
        </section>

        <section class="settings-section">
          <h3>Log levels</h3>
          <p class="section-hint">
            How much the node logs. Every level here is applied once, at backend start
            (run_service.setup_logging): a save lands in config.ini and takes effect
            from the next start — the loggers of the running process do not change.
          </p>
          {#if llError}
            <p class="section-error">{llError}</p>
          {:else if llSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-row">
            <label for="pl-level">Log level</label>
            <select id="pl-level" bind:value={llDraft.level} disabled={llBusy}>
              {#each LOG_LEVELS as level (level)}
                <option value={level}>{level}</option>
              {/each}
            </select>
            <span class="field-hint">
              The gate everything passes first; the log file keeps what passes it.
              Applies from the next backend start.
            </span>
          </div>
          <div class="form-row">
            <label for="pl-console-level">Console log level</label>
            <select id="pl-console-level" bind:value={llDraft.console_level} disabled={llBusy}>
              {#each LOG_LEVELS as level (level)}
                <option value={level}>{level}</option>
              {/each}
            </select>
            <span class="field-hint">
              The console output's own second gate; it can show less than the file.
              Applies from the next backend start.
            </span>
          </div>
          <div class="form-row">
            <label for="pl-new-module">Add a module override</label>
            <div class="add-row">
              <input id="pl-new-module" type="text" bind:value={newModule} disabled={llBusy}
                placeholder="logger name, e.g. httpx" />
              <select id="pl-new-module-level" bind:value={newModuleLevel} disabled={llBusy}>
                {#each LOG_LEVELS as level (level)}
                  <option value={level}>{level}</option>
                {/each}
              </select>
              <button class="btn-add" on:click={addModule} disabled={llBusy || loading}>Add</button>
            </div>
            {#if addError}
              <span class="field-hint error-text">{addError}</span>
            {/if}
          </div>
          {#each moduleNames as name (name)}
            <div class="form-row">
              <label for="pl-module-{name}">{name}</label>
              <select id="pl-module-{name}" bind:value={llDraft.module_levels[name]} disabled={llBusy}>
                {#each LOG_LEVELS as level (level)}
                  <option value={level}>{level}</option>
                {/each}
              </select>
            </div>
          {/each}
          <span class="field-hint wide">
            Per-module overrides, applied after the two levels above; a module not
            listed inherits them. config.ini lowercases every key, so a mixed-case
            logger name never reaches its logger (the seeded telegram.ext.application
            line does not). Applies from the next backend start.
          </span>
          <button class="btn-save" on:click={saveLogLevels} disabled={llBusy || loading}>
            {llBusy ? 'Saving...' : 'Save log levels'}
          </button>
        </section>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: white;
    border-radius: 8px;
    width: 95%;
    max-width: 640px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.3rem;
    color: #333;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: #999;
    line-height: 1;
  }

  .close-btn:hover {
    color: #333;
  }

  .modal-body {
    padding: 1rem 1.5rem;
    overflow-y: auto;
  }

  .settings-section {
    padding: 0.75rem 0;
    border-bottom: 1px solid #e0e0e0;
  }

  .settings-section:last-child {
    border-bottom: none;
  }

  .settings-section h3 {
    margin: 0 0 0.25rem 0;
    font-size: 1.05rem;
    color: #333;
  }

  .section-hint {
    margin: 0 0 0.5rem 0;
    color: #666;
    font-size: 0.85rem;
  }

  .field-hint {
    display: block;
    color: #888;
    font-size: 0.8rem;
  }

  .field-hint.wide {
    margin-bottom: 0.5rem;
  }

  .field-hint code {
    font-size: 0.78rem;
  }

  .section-error {
    padding: 0.5rem;
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .section-saved {
    padding: 0.5rem;
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .form-group {
    margin-bottom: 0.75rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    color: #444;
    cursor: pointer;
  }

  .form-row {
    display: grid;
    grid-template-columns: 170px 1fr;
    align-items: center;
    column-gap: 0.75rem;
    row-gap: 0.15rem;
    margin-bottom: 0.75rem;
  }

  .form-row label {
    font-size: 0.9rem;
    font-weight: 500;
    color: #555;
  }

  .form-row select,
  .add-row input {
    padding: 0.45rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .form-row select:focus,
  .add-row input:focus {
    outline: none;
    border-color: #1976d2;
    box-shadow: 0 0 0 3px rgba(25, 118, 210, 0.1);
  }

  .form-row .field-hint {
    grid-column: 2;
    margin-bottom: 0.25rem;
  }

  .add-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .add-row input {
    flex: 1;
  }

  .btn-add {
    padding: 0.45rem 0.9rem;
    background: #f0f0f0;
    color: #333;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-add:hover:not(:disabled) {
    background: #e0e0e0;
  }

  .btn-add:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .error-text {
    color: #a94442;
  }

  .btn-save {
    padding: 0.5rem 1rem;
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .btn-save:hover:not(:disabled) {
    background: #1258a0;
  }

  .btn-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 560px) {
    .form-row {
      grid-template-columns: 1fr;
    }

    .form-row select,
    .form-row .field-hint {
      grid-column: 1;
    }
  }
</style>
