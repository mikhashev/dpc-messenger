<!-- BackgroundModelsSettingsEditor.svelte -->
<!-- Edit the node-level background model keys of config.ini: [memory] embedding_device, -->
<!-- [knowledge_graph] gliner_device and backend (A-NODE-HAS-NO-ONE-SWITCH-THAT-KEEPS-ITS- -->
<!-- BACKGROUND-MODELS-OFF-THE-GPU, audit/config_ui_parity_2026-11-27.md). Unlike the voice -->
<!-- form's keys, not all of these are live: each field's hint says when a save takes -->
<!-- effect, as the readers say it (settings.get_background_model_settings). -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';
  import { allowedValue, EMBEDDING_DEVICES, GLINER_DEVICES, KG_BACKENDS } from '$lib/utils/backgroundModels';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  // The drafts hold one of their select's options; a load normalises whatever the
  // command answered into one (the getters fall back on an unknown stored value,
  // so the answer is one already — this guards the error paths).
  let bmDraft = { embedding_device: 'auto', gliner_device: 'auto', backend: 'sqlite' };

  let loading = false;
  let loadError = '';
  let bmBusy = false;
  let bmError = '';
  let bmSaved = false;

  // Loads the section each time the editor opens (the only dependency named is
  // `open`, so the statement runs exactly when it flips).
  $: if (open) loadAll();

  async function loadAll() {
    loading = true;
    loadError = '';
    try {
      const bm = await sendCommand('get_background_model_config', {});
      if (bm?.status === 'success') {
        bmDraft = {
          embedding_device: allowedValue(bm.embedding_device, EMBEDDING_DEVICES, 'auto'),
          gliner_device: allowedValue(bm.gliner_device, GLINER_DEVICES, 'auto'),
          backend: allowedValue(bm.backend, KG_BACKENDS, 'sqlite'),
        };
      } else if (bm?.status === 'error') {
        loadError = bm.message || 'Could not read the background model settings';
      }
      bmSaved = false;
      bmError = '';
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
    if (event.key === 'Escape' && !bmBusy) close();
  }

  async function saveBackgroundModels() {
    bmBusy = true;
    bmError = '';
    try {
      const r = await sendCommand('save_background_model_config', {
        config: {
          embedding_device: bmDraft.embedding_device,
          gliner_device: bmDraft.gliner_device,
          backend: bmDraft.backend,
        },
      });
      if (r?.status === 'success') {
        bmSaved = true;
      } else {
        bmError = r?.message || 'Could not save the background model settings';
        bmSaved = false;
      }
    } catch (error) {
      bmError = `Error: ${error}`;
      bmSaved = false;
    } finally {
      bmBusy = false;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" on:keydown={handleKeydown} role="presentation">
    <div class="modal" role="dialog" aria-labelledby="background-models-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="background-models-title">Background Models</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        {#if loadError}
          <p class="section-error">{loadError}</p>
        {/if}

        <section class="settings-section">
          <h3>Background models</h3>
          <p class="section-hint">
            Where the background models of this node run, and which store the agent graphs
            use. The embedding device is read once at backend start; the knowledge graph
            keys at each graph open.
          </p>
          {#if bmError}
            <p class="section-error">{bmError}</p>
          {:else if bmSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-row">
            <label for="bm-embedding-device">Embedding device</label>
            <select id="bm-embedding-device" bind:value={bmDraft.embedding_device} disabled={bmBusy}>
              {#each EMBEDDING_DEVICES as device (device)}
                <option value={device}>{device}</option>
              {/each}
            </select>
            <span class="field-hint">
              The embedding model (indexing, Active Recall). auto = an agent profile's
              choice, else cuda, then mps, then cpu; any other value overrides every agent.
              Takes effect at the next backend start — the model loads once per run.
            </span>
          </div>
          <div class="form-row">
            <label for="bm-gliner-device">GLiNER entity-extraction device</label>
            <select id="bm-gliner-device" bind:value={bmDraft.gliner_device} disabled={bmBusy}>
              {#each GLINER_DEVICES as device (device)}
                <option value={device}>{device}</option>
              {/each}
            </select>
            <span class="field-hint">
              The GLiNER entity model of the knowledge graphs. auto = an agent's own
              config.json choice, else cuda when a card is seen, else cpu; any other value
              overrides the agents. Takes effect the next time GLiNER loads — at the latest
              after a backend restart.
            </span>
          </div>
          <div class="form-row">
            <label for="bm-backend">Knowledge graph backend</label>
            <select id="bm-backend" bind:value={bmDraft.backend} disabled={bmBusy}>
              {#each KG_BACKENDS as backend (backend)}
                <option value={backend}>{backend}</option>
              {/each}
            </select>
            <span class="field-hint">
              The agent graph store: sqlite is the stable default, grafeo the migration
              target (ADR-024). An agent's own kg_backend override still wins. Takes effect
              the next time a knowledge graph opens.
            </span>
          </div>
          <button class="btn-save" on:click={saveBackgroundModels} disabled={bmBusy || loading}>
            {bmBusy ? 'Saving...' : 'Save background models'}
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

  .form-row select {
    grid-column: 2;
    padding: 0.45rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .form-row select:focus {
    outline: none;
    border-color: #1976d2;
    box-shadow: 0 0 0 3px rgba(25, 118, 210, 0.1);
  }

  .form-row .field-hint {
    grid-column: 2;
    margin-bottom: 0.25rem;
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
