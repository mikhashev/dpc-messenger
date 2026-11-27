<!-- VoiceSettingsEditor.svelte -->
<!-- Edit the voice transcription, local transcription and voice message keys of config.ini -->
<!-- (THE-VOICE-AND-TRANSCRIPTION-SETTINGS-ARE-EDITABLE-ONLY-BY-HAND-IN-CONFIG-INI). Every key -->
<!-- here is read live by the running backend (audit/config_ui_parity_2026-11-27.md), so a save -->
<!-- takes effect without a restart; the dead keys of the sections have no field. -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';
  import { parseFigure, parseFigureAbove, parseNameList, parseWholeNumber } from '$lib/utils/voiceSettings';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  // The drafts hold text for the number fields (a cleared field is a legal state for
  // max_duration_seconds), bools for the checkboxes, text for the comma lists.
  let vtDraft = {
    enabled: true, sender_transcribes: false, recipient_delay_seconds: '3', timeout_seconds: '240',
    provider_priority: '', show_transcriber_name: false, cache_transcriptions: true, fallback_to_openai: true,
  };
  let ltDraft = {
    enabled: true, fallback_to_openai: true, max_file_size_mb: '25', max_duration_seconds: '',
    idle_unload_s: '300', queue_wait_s: '600', cpu_max_s: '600', cpu_max_audio_s: '300',
    cpu_contention_factor: '1.0',
  };
  let vmDraft = {
    max_duration_seconds: '300', max_size_mb: '10', mime_types: '',
    default_sample_rate: '48000', default_channels: '1', default_codec: 'opus', auto_transcribe: true,
  };

  let loading = false;
  let loadError = '';
  let vtBusy = false, ltBusy = false, vmBusy = false;
  let vtError = '', ltError = '', vmError = '';
  let vtSaved = false, ltSaved = false, vmSaved = false;

  // Loads the three sections each time the editor opens (the only dependency named
  // is `open`, so the statement runs exactly when it flips).
  $: if (open) loadAll();

  async function loadAll() {
    loading = true;
    loadError = '';
    try {
      const [vt, lt, vm] = await Promise.all([
        sendCommand('get_voice_transcription_config', {}),
        sendCommand('get_local_transcription_config', {}),
        sendCommand('get_voice_messages_config', {}),
      ]);
      if (vt?.status === 'success') {
        // The get answers the cache switch as `cache_enabled`; the save writes the
        // config.ini key `cache_transcriptions`, which is what the draft holds.
        vtDraft = {
          enabled: vt.enabled !== false,
          sender_transcribes: vt.sender_transcribes === true,
          recipient_delay_seconds: String(vt.recipient_delay_seconds ?? 3),
          timeout_seconds: String(vt.timeout_seconds ?? 240),
          provider_priority: (vt.provider_priority ?? []).join(', '),
          show_transcriber_name: vt.show_transcriber_name === true,
          cache_transcriptions: vt.cache_enabled !== false,
          fallback_to_openai: vt.fallback_to_openai !== false,
        };
      }
      if (lt?.status === 'success') {
        ltDraft = {
          enabled: lt.enabled !== false,
          fallback_to_openai: lt.fallback_to_openai !== false,
          max_file_size_mb: String(lt.max_file_size_mb ?? 25),
          max_duration_seconds: lt.max_duration_seconds === null || lt.max_duration_seconds === undefined
            ? '' : String(lt.max_duration_seconds),
          idle_unload_s: String(lt.idle_unload_s ?? 300),
          queue_wait_s: String(lt.queue_wait_s ?? 600),
          cpu_max_s: String(lt.cpu_max_s ?? 600),
          cpu_max_audio_s: String(lt.cpu_max_audio_s ?? 300),
          cpu_contention_factor: String(lt.cpu_contention_factor ?? 1.0),
        };
      }
      if (vm?.status === 'success') {
        vmDraft = {
          max_duration_seconds: String(vm.max_duration_seconds ?? 300),
          max_size_mb: String(vm.max_size_mb ?? 10),
          mime_types: (vm.mime_types ?? []).join(', '),
          default_sample_rate: String(vm.default_sample_rate ?? 48000),
          default_channels: String(vm.default_channels ?? 1),
          default_codec: String(vm.default_codec ?? 'opus'),
          auto_transcribe: vm.auto_transcribe !== false,
        };
      }
      if (vt?.status === 'error' || lt?.status === 'error' || vm?.status === 'error') {
        loadError = vt?.message || lt?.message || vm?.message || 'Could not read the voice settings';
      }
      vtSaved = ltSaved = vmSaved = false;
      vtError = ltError = vmError = '';
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
    if (event.key === 'Escape' && !vtBusy && !ltBusy && !vmBusy) close();
  }

  async function saveVoiceTranscription() {
    const recipientDelay = parseWholeNumber(vtDraft.recipient_delay_seconds, 0);
    if (recipientDelay === null) {
      vtError = 'Recipient delay must be a whole number of seconds, 0 or more.';
      vtSaved = false;
      return;
    }
    const timeout = parseWholeNumber(vtDraft.timeout_seconds, 0);
    if (timeout === null) {
      vtError = 'Peer wait timeout must be a whole number of seconds, 0 or more.';
      vtSaved = false;
      return;
    }
    const priority = parseNameList(vtDraft.provider_priority);
    if (!priority.length) {
      vtError = 'Provider priority must list at least one provider alias, comma-separated.';
      vtSaved = false;
      return;
    }
    vtBusy = true;
    vtError = '';
    try {
      const r = await sendCommand('save_voice_transcription_config', {
        config: {
          enabled: vtDraft.enabled,
          sender_transcribes: vtDraft.sender_transcribes,
          recipient_delay_seconds: recipientDelay,
          timeout_seconds: timeout,
          provider_priority: priority,
          show_transcriber_name: vtDraft.show_transcriber_name,
          cache_transcriptions: vtDraft.cache_transcriptions,
          fallback_to_openai: vtDraft.fallback_to_openai,
        },
      });
      if (r?.status === 'success') {
        vtSaved = true;
      } else {
        vtError = r?.message || 'Could not save the voice transcription settings';
        vtSaved = false;
      }
    } catch (error) {
      vtError = `Error: ${error}`;
      vtSaved = false;
    } finally {
      vtBusy = false;
    }
  }

  async function saveLocalTranscription() {
    const maxFileSizeMb = parseWholeNumber(ltDraft.max_file_size_mb, 0);
    if (maxFileSizeMb === null) {
      ltError = 'Max file size must be a whole number of MB, 0 or more.';
      ltSaved = false;
      return;
    }
    // An empty duration field is the legacy fallback, not a refusal (a cleared
    // number input binds undefined, which reads as empty here).
    const maxDurationText = String(ltDraft.max_duration_seconds ?? '').trim();
    const maxDurationSeconds = maxDurationText === '' ? '' : parseFigure(maxDurationText, 0);
    if (maxDurationSeconds === null) {
      ltError = 'Max audio duration must be a number of seconds, 0 or more (0 = unlimited), or empty.';
      ltSaved = false;
      return;
    }
    const idleUnloadS = parseFigure(ltDraft.idle_unload_s, 0);
    if (idleUnloadS === null) {
      ltError = 'Idle unload must be a number of seconds, 0 or more (0 = never).';
      ltSaved = false;
      return;
    }
    const queueWaitS = parseWholeNumber(ltDraft.queue_wait_s, 0);
    if (queueWaitS === null) {
      ltError = 'Queue wait must be a whole number of seconds, 0 or more.';
      ltSaved = false;
      return;
    }
    const cpuMaxS = parseFigure(ltDraft.cpu_max_s, 0);
    if (cpuMaxS === null) {
      ltError = 'CPU max must be a number of seconds, 0 or more (0 = the CPU route off).';
      ltSaved = false;
      return;
    }
    const cpuMaxAudioS = parseFigure(ltDraft.cpu_max_audio_s, 0);
    if (cpuMaxAudioS === null) {
      ltError = 'CPU max audio must be a number of seconds, 0 or more (0 = the CPU route off).';
      ltSaved = false;
      return;
    }
    const cpuContentionFactor = parseFigureAbove(ltDraft.cpu_contention_factor, 0);
    if (cpuContentionFactor === null) {
      ltError = 'CPU contention factor must be a multiplier above 0.';
      ltSaved = false;
      return;
    }
    ltBusy = true;
    ltError = '';
    try {
      const r = await sendCommand('save_local_transcription_config', {
        config: {
          enabled: ltDraft.enabled,
          fallback_to_openai: ltDraft.fallback_to_openai,
          max_file_size_mb: maxFileSizeMb,
          max_duration_seconds: maxDurationSeconds,
          idle_unload_s: idleUnloadS,
          queue_wait_s: queueWaitS,
          cpu_max_s: cpuMaxS,
          cpu_max_audio_s: cpuMaxAudioS,
          cpu_contention_factor: cpuContentionFactor,
        },
      });
      if (r?.status === 'success') {
        ltSaved = true;
      } else {
        ltError = r?.message || 'Could not save the local transcription settings';
        ltSaved = false;
      }
    } catch (error) {
      ltError = `Error: ${error}`;
      ltSaved = false;
    } finally {
      ltBusy = false;
    }
  }

  async function saveVoiceMessages() {
    const maxDurationSeconds = parseFigureAbove(vmDraft.max_duration_seconds, 0);
    if (maxDurationSeconds === null) {
      vmError = 'Max recording duration must be a number of seconds above 0.';
      vmSaved = false;
      return;
    }
    const maxSizeMb = parseWholeNumber(vmDraft.max_size_mb, 1);
    if (maxSizeMb === null) {
      vmError = 'Max file size must be a whole number of MB above 0.';
      vmSaved = false;
      return;
    }
    const mimeTypes = parseNameList(vmDraft.mime_types);
    if (!mimeTypes.length) {
      vmError = 'Accepted mime types must list at least one audio mime type, comma-separated.';
      vmSaved = false;
      return;
    }
    const sampleRate = parseWholeNumber(vmDraft.default_sample_rate, 1);
    if (sampleRate === null) {
      vmError = 'Sample rate must be a whole number of Hz above 0.';
      vmSaved = false;
      return;
    }
    const channels = parseWholeNumber(vmDraft.default_channels, 1);
    if (channels === null) {
      vmError = 'Channels must be a whole number above 0 (1 = mono, 2 = stereo).';
      vmSaved = false;
      return;
    }
    const codec = vmDraft.default_codec.trim();
    if (!codec) {
      vmError = 'Codec must be a codec name, e.g. opus.';
      vmSaved = false;
      return;
    }
    vmBusy = true;
    vmError = '';
    try {
      const r = await sendCommand('save_voice_messages_config', {
        config: {
          max_duration_seconds: maxDurationSeconds,
          max_size_mb: maxSizeMb,
          mime_types: mimeTypes,
          default_sample_rate: sampleRate,
          default_channels: channels,
          default_codec: codec,
          auto_transcribe: vmDraft.auto_transcribe,
        },
      });
      if (r?.status === 'success') {
        vmSaved = true;
      } else {
        vmError = r?.message || 'Could not save the voice message settings';
        vmSaved = false;
      }
    } catch (error) {
      vmError = `Error: ${error}`;
      vmSaved = false;
    } finally {
      vmBusy = false;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" on:keydown={handleKeydown} role="presentation">
    <div class="modal" role="dialog" aria-labelledby="voice-settings-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="voice-settings-title">Voice and Transcription Settings</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        {#if loadError}
          <p class="section-error">{loadError}</p>
        {/if}

        <section class="settings-section">
          <h3>Voice transcription</h3>
          <p class="section-hint">
            Who transcribes a voice message and how the attempt is made. Read live by the running
            backend: a save takes effect without a restart.
          </p>
          {#if vtError}
            <p class="section-error">{vtError}</p>
          {:else if vtSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-group">
            <label for="vt-enabled">
              <input id="vt-enabled" type="checkbox" bind:checked={vtDraft.enabled} disabled={vtBusy} />
              Transcribe voice messages automatically
            </label>
            <label for="vt-sender-transcribes">
              <input id="vt-sender-transcribes" type="checkbox" bind:checked={vtDraft.sender_transcribes} disabled={vtBusy} />
              The sender transcribes its own voice messages
            </label>
            <label for="vt-show-transcriber-name">
              <input id="vt-show-transcriber-name" type="checkbox" bind:checked={vtDraft.show_transcriber_name} disabled={vtBusy} />
              Show who transcribed a message
            </label>
            <label for="vt-cache-transcriptions">
              <input id="vt-cache-transcriptions" type="checkbox" bind:checked={vtDraft.cache_transcriptions} disabled={vtBusy} />
              Cache transcriptions in memory
            </label>
            <label for="vt-fallback-to-openai">
              <input id="vt-fallback-to-openai" type="checkbox" bind:checked={vtDraft.fallback_to_openai} disabled={vtBusy} />
              Fall back to OpenAI when local Whisper is unavailable
            </label>
          </div>
          <div class="form-row">
            <label for="vt-recipient-delay">Recipient delay, s</label>
            <input id="vt-recipient-delay" type="number" min="0" step="1" bind:value={vtDraft.recipient_delay_seconds} disabled={vtBusy} />
            <span class="field-hint">Seconds a recipient waits before trying the transcription itself.</span>
          </div>
          <div class="form-row">
            <label for="vt-timeout">Peer wait timeout, s</label>
            <input id="vt-timeout" type="number" min="0" step="1" bind:value={vtDraft.timeout_seconds} disabled={vtBusy} />
            <span class="field-hint">How long to wait for a peer's transcription before trying locally.</span>
          </div>
          <div class="form-row">
            <label for="vt-provider-priority">Provider priority</label>
            <input id="vt-provider-priority" type="text" bind:value={vtDraft.provider_priority} disabled={vtBusy} />
            <span class="field-hint">Comma-separated provider aliases; the first one found is used.</span>
          </div>
          <button class="btn-save" on:click={saveVoiceTranscription} disabled={vtBusy || loading}>
            {vtBusy ? 'Saving...' : 'Save voice transcription'}
          </button>
        </section>

        <section class="settings-section">
          <h3>Local transcription</h3>
          <p class="section-hint">
            The local Whisper route's gates (ADR-052). Read live by the running backend: a save
            takes effect without a restart. The Whisper model itself is chosen in AI Providers.
          </p>
          {#if ltError}
            <p class="section-error">{ltError}</p>
          {:else if ltSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-group">
            <label for="lt-enabled">
              <input id="lt-enabled" type="checkbox" bind:checked={ltDraft.enabled} disabled={ltBusy} />
              Local Whisper transcription enabled
            </label>
            <label for="lt-fallback-to-openai">
              <input id="lt-fallback-to-openai" type="checkbox" bind:checked={ltDraft.fallback_to_openai} disabled={ltBusy} />
              Fall back to the OpenAI API when local fails
            </label>
          </div>
          <div class="form-row">
            <label for="lt-max-duration">Max audio duration, s</label>
            <input id="lt-max-duration" type="number" min="0" step="1" bind:value={ltDraft.max_duration_seconds} disabled={ltBusy} />
            <span class="field-hint">0 = unlimited; empty = the legacy max file size below.</span>
          </div>
          <div class="form-row">
            <label for="lt-max-file-size">Max file size, MB</label>
            <input id="lt-max-file-size" type="number" min="0" step="1" bind:value={ltDraft.max_file_size_mb} disabled={ltBusy} />
            <span class="field-hint">The legacy bound, used only while max duration is empty.</span>
          </div>
          <div class="form-row">
            <label for="lt-idle-unload">Idle unload, s</label>
            <input id="lt-idle-unload" type="number" min="0" step="1" bind:value={ltDraft.idle_unload_s} disabled={ltBusy} />
            <span class="field-hint">Unload the model (frees VRAM) after this many idle seconds; 0 = never.</span>
          </div>
          <div class="form-row">
            <label for="lt-queue-wait">Queue wait, s</label>
            <input id="lt-queue-wait" type="number" min="0" step="1" bind:value={ltDraft.queue_wait_s} disabled={ltBusy} />
            <span class="field-hint">How long a person's transcription may wait in line for the GPU; 0 = do not wait.</span>
          </div>
          <div class="form-row">
            <label for="lt-cpu-max">CPU max, s</label>
            <input id="lt-cpu-max" type="number" min="0" step="1" bind:value={ltDraft.cpu_max_s} disabled={ltBusy} />
            <span class="field-hint">The CPU twin takes a transcription only within this estimate; 0 = never use the CPU.</span>
          </div>
          <div class="form-row">
            <label for="lt-cpu-max-audio">CPU max audio, s</label>
            <input id="lt-cpu-max-audio" type="number" min="0" step="1" bind:value={ltDraft.cpu_max_audio_s} disabled={ltBusy} />
            <span class="field-hint">Audio longer than this never goes to the CPU; 0 = never use the CPU.</span>
          </div>
          <div class="form-row">
            <label for="lt-cpu-contention">CPU contention factor</label>
            <input id="lt-cpu-contention" type="number" min="0" step="0.1" bind:value={ltDraft.cpu_contention_factor} disabled={ltBusy} />
            <span class="field-hint">The CPU's estimate is multiplied by this while the CPU computes something else.</span>
          </div>
          <button class="btn-save" on:click={saveLocalTranscription} disabled={ltBusy || loading}>
            {ltBusy ? 'Saving...' : 'Save local transcription'}
          </button>
        </section>

        <section class="settings-section">
          <h3>Voice messages</h3>
          <p class="section-hint">
            The recording and sending bounds. Read live at each record and send: a save takes
            effect without a restart.
          </p>
          {#if vmError}
            <p class="section-error">{vmError}</p>
          {:else if vmSaved}
            <p class="section-saved">Saved.</p>
          {/if}
          <div class="form-row">
            <label for="vm-max-duration">Max recording duration, s</label>
            <input id="vm-max-duration" type="number" min="1" step="1" bind:value={vmDraft.max_duration_seconds} disabled={vmBusy} />
            <span class="field-hint">A recording longer than this is refused.</span>
          </div>
          <div class="form-row">
            <label for="vm-max-size">Max file size, MB</label>
            <input id="vm-max-size" type="number" min="1" step="1" bind:value={vmDraft.max_size_mb} disabled={vmBusy} />
            <span class="field-hint">A voice message larger than this is refused.</span>
          </div>
          <div class="form-row">
            <label for="vm-mime-types">Accepted mime types</label>
            <input id="vm-mime-types" type="text" bind:value={vmDraft.mime_types} disabled={vmBusy} />
            <span class="field-hint">Comma-separated audio formats a recording may be sent as.</span>
          </div>
          <div class="form-row">
            <label for="vm-sample-rate">Sample rate, Hz</label>
            <input id="vm-sample-rate" type="number" min="1" step="1" bind:value={vmDraft.default_sample_rate} disabled={vmBusy} />
            <span class="field-hint">The recording's sample rate (48000 for quality).</span>
          </div>
          <div class="form-row">
            <label for="vm-channels">Channels</label>
            <input id="vm-channels" type="number" min="1" step="1" bind:value={vmDraft.default_channels} disabled={vmBusy} />
            <span class="field-hint">1 = mono, 2 = stereo.</span>
          </div>
          <div class="form-row">
            <label for="vm-codec">Codec</label>
            <input id="vm-codec" type="text" bind:value={vmDraft.default_codec} disabled={vmBusy} />
            <span class="field-hint">The recording's codec (opus for web compatibility).</span>
          </div>
          <div class="form-group">
            <label for="vm-auto-transcribe">
              <input id="vm-auto-transcribe" type="checkbox" bind:checked={vmDraft.auto_transcribe} disabled={vmBusy} />
              Auto-transcribe voice messages (node-wide gate; off lets Whisper unload whatever a conversation asks)
            </label>
          </div>
          <button class="btn-save" on:click={saveVoiceMessages} disabled={vmBusy || loading}>
            {vmBusy ? 'Saving...' : 'Save voice messages'}
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

  .form-row input {
    grid-column: 2;
    padding: 0.45rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .form-row input:focus {
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

    .form-row input,
    .form-row .field-hint {
      grid-column: 1;
    }
  }
</style>
