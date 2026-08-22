<!-- ProvidersEditor.svelte -->
<!-- View and manage AI provider configuration -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand, peerProviders, providerBalance, getProviderBalance } from '$lib/coreService';
  import { confirmAsync } from '$lib/utils/dialog';
  import { trackRename } from '$lib/utils/aliasRenames';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type ProviderType = 'ollama' | 'openai_compatible' | 'anthropic' | 'zai' | 'zai_coding' | 'deepseek' | 'llamacpp_server' | 'local_whisper' | 'dpc_agent' | 'gemini' | 'github_models' | 'gigachat';

  type Provider = {
    alias: string;
    type: ProviderType;
    model?: string;          // Optional for dpc_agent type
    host?: string;           // Ollama only
    base_url?: string;       // OpenAI only
    api_key?: string;        // Plaintext (local providers only)
    api_key_env?: string;    // Environment variable (cloud providers)
    context_window?: number; // Optional override
    temperature?: number;    // Model creativity (0.0-2.0, default 0.7)
    max_tokens?: number;     // Max output tokens (zai/anthropic)
    top_p?: number;          // Nucleus sampling (zai, ollama)
    // Whether the model reasons before answering. Unset is not the same as
    // false: unset lets the capability decide, false says no to a model that
    // can (Ollama only).
    think?: boolean;
    // Ollama sampling params (unset = modelfile default)
    min_p?: number;
    presence_penalty?: number;
    repeat_penalty?: number;
    top_k?: number;
    num_predict?: number;
    // llamacpp_server specific: the model file the DPC-owned llama-server
    // child serves, the per-request thinking cap (ADR-040 route b2), and the
    // supervisor knobs the form exposes doors to
    gguf_path?: string;
    reasoning_budget_tokens?: number;
    mmproj?: string;         // vision projector; absent = text-only child
    n_ctx?: number;          // KV cells the child allocates (-c); unset = 262144
    cache_type_k?: string;   // KV quant; unset = the auto ladder (q8_0 -> q4_0)
    cache_type_v?: string;
    n_parallel?: number;     // unset = the server's own slot choice
    n_ubatch?: number;       // micro-batch; unset = the build's 512
    n_batch?: number;        // logical batch, the micro-batch's ceiling
    cache_reuse?: number;    // KV-shift reuse chunk; unset = the build's 0 (off)
    // The rest of the supervisor's DEFAULTS. They were reachable only by hand-
    // editing providers.json, which is how a measured MTP experiment came to be
    // set on the wrong field: `spec_draft_n_max` had no control, so "n=4" landed
    // on Parallel slots and the run measured nothing.
    binary_path?: string;       // overrides the ADR-040 pin; missing file = a loud refusal
    n_gpu_layers?: number;      // unset = 999, every context fully on the card
    flash_attn?: boolean;       // unset = false
    spec_type?: string;         // unset = draft-mtp
    spec_draft_n_max?: number;  // unset = 3
    ctx_checkpoints?: number;   // unset = the build's 32
    checkpoint_min_step?: number; // unset = the build's 8192
    kv_unified?: boolean;       // unset = true; only meaningful above one slot
    cache_ram_mib?: number;     // host RAM prompt cache, not VRAM
    slot_save_path?: string;    // where slot state is persisted
    jinja?: boolean;            // unset = true
    start_timeout_s?: number;   // unset = 300
    extra_args?: string[];      // raw flags appended last, one token per entry
    // Local Whisper specific (v0.13.1+)
    device?: string;         // 'cuda', 'cpu', or 'auto'
    compile_model?: boolean; // torch.compile optimization
    use_flash_attention?: boolean; // Flash Attention 2 (optional)
    chunk_length_s?: number; // Chunked transcription
    batch_size?: number;     // Batch size for processing
    language?: string;       // 'auto' or specific language code
    task?: string;           // 'transcribe' or 'translate'
    lazy_loading?: boolean;  // Load model on first use
    // Remote Peer specific (v0.18.0+)
    peer_id?: string;        // Remote peer's node ID (also used by dpc_agent for remote inference)
    provider?: string;       // Remote provider alias (optional)
    timeout?: number;        // Request timeout in seconds
    // DPC Agent remote inference (v0.18.1+ KISS approach)
    remote_model?: string;   // Model preference for remote peer
    remote_provider?: string; // Provider preference for remote peer
    // Thinking/reasoning (v0.15.0+)
    thinking?: {
      enabled?: boolean;
      budget_tokens?: number;
    };
    // GigaChat specific (v0.21.0+)
    scope?: string;        // 'GIGACHAT_API_PERS' | 'GIGACHAT_API_B2B' | 'GIGACHAT_API_CORP'
    verify_ssl?: boolean;
    ca_bundle_file?: string;
  };

  type ProvidersConfig = {
    default_provider: string;
    vision_provider?: string;  // Optional vision provider for image queries
    voice_provider?: string;   // v0.13.0+: Optional voice provider for transcription
    agent_provider?: string;   // v0.18.0+: Optional agent provider for AI agent
    knowledge_provider?: string;  // Provider that extracts knowledge from a conversation
    providers: Provider[];
  };

  let config: ProvidersConfig | null = null;
  let selectedTab: 'list' | 'add' = 'list';
  let editMode: boolean = false;
  let editedConfig: ProvidersConfig | null = null;
  let isSaving: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

  // Tauri only. The browser dev server has no file dialog, and a Browse button
  // that silently does nothing is worse than no button — so the control is not
  // rendered there at all. Re-checked whenever the modal opens rather than once
  // at init: `window.isTauri` is set during the page's onMount, which can land
  // after this component is constructed.
  $: canBrowse = open
    && typeof window !== 'undefined'
    && ((window as any).isTauri === true || !!(window as any).__TAURI__);

  // Three setters shared by the supervisor-flag controls below. Written once
  // rather than inlined twelve times: an empty control means "unset", and unset
  // has to delete the key, not write 0 / "" / false — the supervisor fills its
  // own DEFAULTS over whatever the alias omits, so a written falsy value is a
  // different instruction from an absent one.
  function setNum(i: number, key: keyof Provider, raw: string, float = false) {
    if (!editedConfig) return;
    const p = editedConfig.providers[i] as any;
    const n = float ? parseFloat(raw) : parseInt(raw);
    if (raw === '' || isNaN(n)) delete p[key];
    else p[key] = n;
    editedConfig = editedConfig;
  }

  function setStr(i: number, key: keyof Provider, raw: string) {
    if (!editedConfig) return;
    const p = editedConfig.providers[i] as any;
    if (raw === '') delete p[key];
    else p[key] = raw;
    editedConfig = editedConfig;
  }

  /** Tri-state: '' leaves the key out, 'true'/'false' write the boolean.
   *  A checkbox cannot express "unset", and for jinja and kv_unified the
   *  default is true — so an unchecked box would silently mean "off". */
  function setBool(i: number, key: keyof Provider, raw: string) {
    if (!editedConfig) return;
    const p = editedConfig.providers[i] as any;
    if (raw === '') delete p[key];
    else p[key] = raw === 'true';
    editedConfig = editedConfig;
  }

  /** `extra_args` is a flag array; the textarea holds one token per line so a
   *  path with spaces survives, which a space-split would tear in half. */
  function setExtraArgs(i: number, raw: string) {
    if (!editedConfig) return;
    const p = editedConfig.providers[i];
    const tokens = raw.split('\n').map((s) => s.trim()).filter((s) => s.length > 0);
    if (tokens.length === 0) delete p.extra_args;
    else p.extra_args = tokens;
    editedConfig = editedConfig;
  }

  /** Same dialog for the server executable, which has its own extension. */
  async function pickBinaryPath(i: number) {
    if (!editedConfig || !canBrowse) return;
    const provider = editedConfig.providers[i];
    const current = provider.binary_path || '';
    const startIn = current ? current.replace(/[\\/][^\\/]*$/, '') : undefined;
    try {
      const { open: openDialog } = await import('@tauri-apps/plugin-dialog');
      const picked = await openDialog({
        multiple: false,
        directory: false,
        defaultPath: startIn,
        title: 'Select a llama-server executable',
        filters: [
          { name: 'Executable', extensions: ['exe'] },
          { name: 'All files', extensions: ['*'] },
        ],
      });
      if (typeof picked !== 'string' || !picked) return;
      provider.binary_path = picked;
      editedConfig = editedConfig;
    } catch (err) {
      console.error('[ProvidersEditor] binary dialog failed:', err);
    }
  }

  /** Fill a model path from a file dialog, starting where the current value points. */
  async function pickModelPath(i: number, field: 'gguf_path' | 'mmproj') {
    if (!editedConfig || !canBrowse) return;
    const provider = editedConfig.providers[i];
    // For mmproj fall back to the GGUF's own folder: a projector almost always
    // ships beside the weights it belongs to.
    const current = provider[field] || provider.gguf_path || '';
    const startIn = current ? current.replace(/[\\/][^\\/]*$/, '') : undefined;
    try {
      // Named to avoid shadowing this component's own `open` prop.
      const { open: openDialog } = await import('@tauri-apps/plugin-dialog');
      const picked = await openDialog({
        multiple: false,
        directory: false,
        defaultPath: startIn,
        title: field === 'mmproj'
          ? 'Select the vision projector (mmproj)'
          : 'Select the GGUF model file',
        filters: [
          { name: 'GGUF model', extensions: ['gguf'] },
          { name: 'All files', extensions: ['*'] },
        ],
      });
      if (typeof picked !== 'string' || !picked) return;   // cancelled
      provider[field] = picked;
      editedConfig = editedConfig;
    } catch (err) {
      console.error('[ProvidersEditor] file dialog failed:', err);
    }
  }

  // Model info query state
  let showModelInfo: boolean = false;
  let modelInfoData: any = null;
  let modelInfoLoading: boolean = false;
  let modelInfoError: string = '';
  let queriedProviderAlias: string = '';

  // Remote peer providers state (for dropdown population)
  // Uses the shared peerProviders store from coreService for proper reactivity
  let remotePeerLoading: string = '';  // peer_id being fetched
  let remotePeerError: string = '';

  // Context window presets
  const CONTEXT_WINDOW_PRESETS = [
    { label: '2K tokens', value: 2048 },
    { label: '4K tokens', value: 4096 },
    { label: '8K tokens', value: 8192 },
    { label: '16K tokens', value: 16384 },
    { label: '32K tokens', value: 32768 },
    { label: '64K tokens', value: 65536 },
    { label: '128K tokens', value: 131072 },
    { label: '256K tokens', value: 262144 },
    { label: '512K tokens', value: 524288 },
    { label: '1M tokens', value: 1000000 },
  ];

  // Temperature presets for model creativity
  const TEMPERATURE_PRESETS = [
    { label: 'Precise (0.2)', value: 0.2, description: 'Deterministic, factual' },
    { label: 'Balanced (0.5)', value: 0.5, description: 'Consistent, focused' },
    { label: 'Default (0.7)', value: 0.7, description: 'Balanced creativity' },
    { label: 'Creative (1.0)', value: 1.0, description: 'More varied output' },
    { label: 'Random (1.5)', value: 1.5, description: 'High variation' },
  ];

  // Mirrors OLLAMA_SAMPLING_PARAMS in ollama_provider.py — unset field = key
  // absent from providers.json = Ollama modelfile default applies.
  const OLLAMA_SAMPLING_PARAMS = [
    { key: 'min_p', step: 0.01, min: 0, max: 1, isInt: false, hint: '0.0–1.0' },
    { key: 'presence_penalty', step: 0.1, min: -2, max: 2, isInt: false, hint: '-2.0–2.0' },
    { key: 'repeat_penalty', step: 0.05, min: 0, max: 2, isInt: false, hint: '0.0–2.0' },
    { key: 'top_k', step: 1, min: 0, max: 200, isInt: true, hint: 'integer' },
    { key: 'top_p', step: 0.05, min: 0, max: 1, isInt: false, hint: '0.0–1.0' },
    { key: 'num_predict', step: 1, min: -2, max: 1000000, isInt: true, hint: 'max tokens' },
  ];

  // The llamacpp_server sampling subset the model card actually prescribes
  // (thinking mode: temperature 1.0, top_p 0.95, top_k 20); temperature has
  // its own generic field above, so these two are the remainder.
  const LLAMA_SAMPLING_PARAMS = [
    { key: 'top_p', step: 0.05, min: 0, max: 1, isInt: false, hint: '0.0–1.0' },
    { key: 'top_k', step: 1, min: 0, max: 200, isInt: true, hint: 'integer' },
  ];

  // Unset temperature means different things per provider type: ollama omits
  // the key (modelfile default applies), deepseek/zai_coding fall back to 1.0,
  // the rest send self.temperature = 0.7.
  function temperatureDefaultLabel(type: ProviderType): string {
    if (type === 'ollama') return 'Model default (not sent)';
    if (type === 'deepseek' || type === 'zai_coding' || type === 'llamacpp_server') return 'Provider default (1.0)';
    return 'Default (0.7)';
  }

  // DeepSeek accepts the number and ignores it while thinking is on: measured
  // 2026-08-15 -- with thinking off, temperature 0.0 returned the same answer
  // five times out of five; with thinking on it returned four different ones.
  // The backend stops sending it there, so the field must stop looking like a
  // control the operator can use without turning thinking off first.
  function temperatureIsInert(p: { type: ProviderType; thinking?: { enabled?: boolean } }): boolean {
    return p.type === 'deepseek' && p.thinking?.enabled !== false;
  }

  // Selecting "Custom..." must show the manual input even while temperature is
  // still unset — tracked per provider index, reset on entering edit mode.
  let customTempMode: Record<number, boolean> = {};

  function tempSelectValue(i: number): string | number {
    const t = editedConfig?.providers[i]?.temperature;
    if (customTempMode[i] || (t !== undefined && !TEMPERATURE_PRESETS.some(p => p.value === t))) return 'custom';
    return t ?? '';
  }

  // Three states, and a checkbox can only hold two. Leaving it out is the
  // third: the daemon reports whether the model can think and that decides.
  // Saying no explicitly is what a checkbox cannot express, and it is the
  // case that matters — a model that spends its whole budget reasoning
  // answers with nothing at all.
  function thinkSelectValue(i: number): string {
    const t = editedConfig?.providers[i]?.think;
    return t === undefined ? '' : t ? 'on' : 'off';
  }

  function getSamplingParam(i: number, key: string): number | '' {
    const v = (editedConfig?.providers[i] as any)?.[key];
    return v ?? '';
  }

  function setSamplingParam(i: number, key: string, raw: string, isInt: boolean) {
    if (!editedConfig) return;
    const p = editedConfig.providers[i] as any;
    if (raw === '') {
      p[key] = undefined;
    } else {
      const n = isInt ? parseInt(raw) : parseFloat(raw);
      p[key] = isNaN(n) ? undefined : n;
    }
    editedConfig = editedConfig;
  }

  // New provider form
  let newProvider: Provider = {
    alias: '',
    type: 'ollama',
    model: '',
    peer_id: '',  // For dpc_agent remote inference
    think: false, // Reasoning is opt-in on a new provider — see the form's help text
  };

  // Load config when modal opens
  $: if (open && !config) {
    loadConfig();
  }

  async function loadConfig() {
    try {
      const result = await sendCommand('get_providers_config', {});
      if (result.status === 'success') {
        config = result.config;
      } else {
        console.error('Failed to load providers config:', result.message);
      }
    } catch (error) {
      console.error('Error loading providers config:', error);
    }
  }

  // Cancel editing
  function cancelEditing() {
    editMode = false;
    editedConfig = null;
    pendingRenames = {};
    selectedTab = 'list';
    saveMessage = '';
    saveMessageType = '';
    resetNewProviderForm();
  }

  // Save changes
  async function saveChanges() {
    if (!editedConfig) return;

    isSaving = true;
    saveMessage = '';
    saveMessageType = '';

    try {
      const result = await sendCommand('save_providers_config', {
        config_dict: editedConfig,
        alias_renames: pendingRenames
      });

      if (result.status === 'success') {
        saveMessage = result.message;
        if (result.warnings && result.warnings.length > 0) {
          saveMessage += '\nStill naming a provider that no longer exists:\n' + result.warnings.join('\n');
          saveMessageType = 'error';
        } else {
          saveMessageType = 'success';
        }

        // Update the displayed config
        config = JSON.parse(JSON.stringify(editedConfig));

        // Exit edit mode
        editMode = false;
        editedConfig = null;
        pendingRenames = {};
        selectedTab = 'list';

        // Clear success message after short delay — a warning stays until it is read
        if (saveMessageType === 'success') {
          setTimeout(() => {
            saveMessage = '';
            saveMessageType = '';
          }, 2000);
        }
      } else {
        saveMessage = result.message;
        if (result.errors && result.errors.length > 0) {
          saveMessage += ':\n' + result.errors.join('\n');
        }
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error saving providers config:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    } finally {
      isSaving = false;
    }
  }

  async function close() {
    if (editMode) {
      const confirmed = await confirmAsync('You have unsaved changes. Discard them and close?', { kind: 'warning' });
      if (!confirmed) return;
    }
    editMode = false;
    editedConfig = null;
    pendingRenames = {};
    config = null;
    selectedTab = 'list';
    resetNewProviderForm();
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (editMode) {
        cancelEditing();
      } else {
        close();
      }
    }
  }

  // Get the config to display (edited or original)
  $: displayConfig = editedConfig || config;

  // --- Account balance (pay-per-use providers, e.g. DeepSeek) — Phase 2b ---
  const LOW_BALANCE_USD = 3;       // early-warning threshold
  const CRITICAL_BALANCE_USD = 1;  // urgent threshold
  let balanceLoading = false;

  async function refreshBalance() {
    balanceLoading = true;
    try {
      await getProviderBalance();
    } finally {
      balanceLoading = false;
    }
  }

  $: hasPayPerUseProvider = !!displayConfig?.providers?.some((p) => p.type === 'deepseek');
  $: balResult = $providerBalance;
  $: balanceUnsupported = !!balResult && balResult.status === 'unsupported';
  $: balanceError = balResult && balResult.status === 'error' ? (balResult.message || 'error') : '';
  $: balanceInfo = balResult && balResult.status === 'success' && balResult.balance && Array.isArray(balResult.balance.balance_infos)
    ? balResult.balance.balance_infos[0] : null;
  $: balanceAlias = (balResult && balResult.alias) || '';
  $: balanceCurrency = balanceInfo ? (balanceInfo.currency || 'USD') : 'USD';
  $: balanceTotal = balanceInfo ? balanceInfo.total_balance : null;
  $: balanceAvailable = balResult && balResult.balance ? balResult.balance.is_available !== false : true;
  $: balanceNum = balanceTotal !== null && balanceTotal !== undefined ? parseFloat(balanceTotal) : NaN;
  $: balanceLevel = (!balanceAvailable || (!isNaN(balanceNum) && balanceNum < CRITICAL_BALANCE_USD)) ? 'critical'
    : (!isNaN(balanceNum) && balanceNum < LOW_BALANCE_USD) ? 'low'
    : 'ok';

  // Delete provider
  async function deleteProvider(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    const confirmed = await confirmAsync(`Delete provider '${provider.alias}'?`, { kind: 'warning' });
    if (confirmed) {
      editedConfig.providers.splice(index, 1);
      // If deleted provider was default, reset default
      if (editedConfig.default_provider === provider.alias) {
        editedConfig.default_provider = editedConfig.providers[0]?.alias || '';
      }
      // If deleted provider was vision default, reset vision default
      if (editedConfig.vision_provider === provider.alias) {
        editedConfig.vision_provider = editedConfig.providers[0]?.alias || '';
      }
      // v0.13.0+: If deleted provider was voice default, reset voice default
      if (editedConfig.voice_provider === provider.alias) {
        editedConfig.voice_provider = editedConfig.providers[0]?.alias || '';
      }
      // v0.18.0+: If deleted provider was agent default, reset agent default
      if (editedConfig.agent_provider === provider.alias) {
        editedConfig.agent_provider = editedConfig.providers[0]?.alias || '';
      }
      // Knowledge extraction clears rather than moving to another provider:
      // unset means «use the model that answered in the conversation», which is
      // a safe answer, and the first provider in the list may be a paid API.
      if (editedConfig.knowledge_provider === provider.alias) {
        editedConfig.knowledge_provider = '';
      }
      editedConfig = editedConfig; // Trigger reactivity
    }
  }

  // Set default provider
  function setDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.default_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Set vision default provider
  function setVisionDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.vision_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Set voice default provider (v0.13.0+)
  function setVoiceDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.voice_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Set agent default provider (v0.18.0+)
  function setAgentDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.agent_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // The provider that extracts knowledge. Clicking the active one clears it,
  // because «not set» is a real choice here: it means the model that answered
  // in the conversation does the extracting.
  function setKnowledgeDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.knowledge_provider = editedConfig.knowledge_provider === alias ? '' : alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Track original aliases to detect changes on blur
  let originalAliases = new Map<number, string>();

  // Renames to carry into everything else that names the alias — agent configs,
  // the registry, the firewall's serving alias, the voice priority list.
  let pendingRenames: Record<string, string> = {};

  // Handle alias change with auto-update of defaults (triggered on blur)
  function handleAliasBlur(index: number) {
    if (!editedConfig) return;
    const newAlias = editedConfig.providers[index].alias;
    const oldAlias = originalAliases.get(index);

    // Only update if alias actually changed
    if (!oldAlias || newAlias === oldAlias) {
      originalAliases.set(index, newAlias);
      return;
    }

    pendingRenames = trackRename(pendingRenames, oldAlias, newAlias);

    // Auto-update default_provider if this was the default
    if (editedConfig.default_provider === oldAlias) {
      editedConfig.default_provider = newAlias;
    }

    // Auto-update vision_provider if this was the vision default
    if (editedConfig.vision_provider === oldAlias) {
      editedConfig.vision_provider = newAlias;
    }

    // v0.13.0+: Auto-update voice_provider if this was the voice default
    if (editedConfig.voice_provider === oldAlias) {
      editedConfig.voice_provider = newAlias;
    }

    // v0.18.0+: Auto-update agent_provider if this was the agent default
    if (editedConfig.agent_provider === oldAlias) {
      editedConfig.agent_provider = newAlias;
    }

    // The extraction provider follows a rename like every other role.
    if (editedConfig.knowledge_provider === oldAlias) {
      editedConfig.knowledge_provider = newAlias;
    }

    // Update the tracked alias
    originalAliases.set(index, newAlias);
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Initialize original aliases when entering edit mode
  function startEditing() {
    if (!config) return;
    editMode = true;
    customTempMode = {};
    editedConfig = JSON.parse(JSON.stringify(config));
    if (!editedConfig) return; // Guard against null
    // Track original aliases
    pendingRenames = {};
    originalAliases.clear();
    editedConfig.providers.forEach((p, i) => {
      originalAliases.set(i, p.alias);
    });
  }

  // API key source switching
  function switchToEnv(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    delete provider.api_key;
    provider.api_key_env = provider.api_key_env || '';
    editedConfig = editedConfig; // Trigger reactivity
  }

  function switchToPlaintext(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    delete provider.api_key_env;
    provider.api_key = provider.api_key || '';
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Reset new provider form
  function resetNewProviderForm() {
    newProvider = {
      alias: '',
      type: 'ollama',
      model: '',
      peer_id: '',  // For dpc_agent remote inference
      think: false, // Reasoning is opt-in on a new provider — see the form's help text
    };
  }

  // Add new provider
  function addNewProvider() {
    if (!editedConfig) return;

    // Add type-specific defaults
    const provider: Provider = {
      alias: newProvider.alias,
      type: newProvider.type,
      model: newProvider.model,
    };

    if (newProvider.type === 'ollama') {
      provider.host = 'http://127.0.0.1:11434';
      // Only when the form says so: leaving the key out means the model decides,
      // and that is a third state rather than a synonym for off.
      if (newProvider.think !== undefined) provider.think = newProvider.think;
    } else if (newProvider.type === 'openai_compatible') {
      provider.base_url = 'https://api.openai.com/v1';
      provider.api_key_env = 'OPENAI_API_KEY';
    } else if (newProvider.type === 'anthropic') {
      provider.api_key_env = 'ANTHROPIC_API_KEY';
    } else if (newProvider.type === 'zai') {
      provider.api_key_env = 'ZAI_API_KEY';
      provider.model = newProvider.model || 'glm-5.2';
      provider.base_url = 'https://api.z.ai/api/anthropic';
    } else if (newProvider.type === 'zai_coding') {
      provider.api_key_env = 'ZAI_API_KEY';
      provider.model = newProvider.model || 'glm-5.2';
      provider.base_url = 'https://api.z.ai/api/coding/paas/v4';
      provider.context_window = 200000;
    } else if (newProvider.type === 'deepseek') {
      provider.api_key_env = 'DEEPSEEK_API_KEY';
      provider.model = newProvider.model || 'deepseek-v4-flash';
      provider.base_url = 'https://api.deepseek.com';
      provider.context_window = 1000000;
    } else if (newProvider.type === 'llamacpp_server') {
      // The form's single Model field carries the GGUF path — that is the one
      // thing this type cannot default. Everything else has a measured default
      // in the supervisor (n_ctx 262144, -ngl 999, MTP draft 3, --jinja).
      provider.gguf_path = newProvider.model || '';
      provider.context_window = 262144;
      // The card's thinking-mode sampling, prefilled so the alias is honest
      // from birth; the backend logs an advisory when these are missing.
      provider.temperature = 1.0;
      provider.top_p = 0.95;
      provider.top_k = 20;
    } else if (newProvider.type === 'local_whisper') {
      provider.device = 'auto';
      provider.compile_model = true;
      provider.use_flash_attention = false;
      provider.chunk_length_s = 30;
      provider.batch_size = 16;
      provider.language = 'auto';
      provider.task = 'transcribe';
      provider.lazy_loading = true;
    } else if (newProvider.type === 'dpc_agent') {
      // dpc_agent doesn't require model - it uses the default AI provider
      // Optionally can have peer_id for remote inference
      delete provider.model;
    } else if (newProvider.type === 'gemini') {
      provider.api_key_env = 'GEMINI_API_KEY';
      provider.context_window = 1000000;
    } else if (newProvider.type === 'github_models') {
      provider.api_key_env = 'GITHUB_TOKEN';
      provider.context_window = 128000;
    } else if (newProvider.type === 'gigachat') {
      provider.api_key_env = 'GIGACHAT_CREDENTIALS';
      provider.scope = 'GIGACHAT_API_PERS';
      provider.verify_ssl = true;
      provider.context_window = 128000;
    }

    // Carry over user-entered optional settings (override type defaults where set)
    if (newProvider.api_key_env) provider.api_key_env = newProvider.api_key_env;
    if (newProvider.context_window !== undefined) provider.context_window = newProvider.context_window;
    if (newProvider.temperature !== undefined) provider.temperature = newProvider.temperature;
    if (newProvider.max_tokens !== undefined) provider.max_tokens = newProvider.max_tokens;
    if (newProvider.top_p !== undefined) provider.top_p = newProvider.top_p;
    if (newProvider.thinking !== undefined) provider.thinking = newProvider.thinking;

    editedConfig.providers.push(provider);
    editedConfig = editedConfig; // Trigger reactivity

    // Reset form and switch to list tab
    resetNewProviderForm();
    selectedTab = 'list';
  }

  // Query Ollama model info
  async function queryModelInfo(providerAlias: string) {
    modelInfoLoading = true;
    modelInfoError = '';
    modelInfoData = null;
    queriedProviderAlias = providerAlias;
    showModelInfo = true;

    try {
      const result = await sendCommand('query_ollama_model_info', {
        provider_alias: providerAlias
      });

      if (result.status === 'success') {
        modelInfoData = result.model_info;
      } else {
        modelInfoError = result.message || 'Failed to query model info';
      }
    } catch (error) {
      modelInfoError = `Error: ${error}`;
    } finally {
      modelInfoLoading = false;
    }
  }

  // Close model info modal
  function closeModelInfo() {
    showModelInfo = false;
    modelInfoData = null;
    modelInfoError = '';
    queriedProviderAlias = '';
  }

  // Use detected context window value
  function useDetectedContextWindow(providerAlias: string, numCtx: number) {
    if (!editedConfig) {
      // Not in edit mode - enter edit mode first
      startEditing();
      // Wait for next tick to ensure editedConfig is set
      setTimeout(() => {
        const index = editedConfig?.providers.findIndex(p => p.alias === providerAlias);
        if (index !== undefined && index !== -1 && editedConfig) {
          editedConfig.providers[index].context_window = numCtx;
          editedConfig = editedConfig; // Trigger reactivity
          closeModelInfo();
        }
      }, 0);
    } else {
      const index = editedConfig.providers.findIndex(p => p.alias === providerAlias);
      if (index !== -1) {
        editedConfig.providers[index].context_window = numCtx;
        editedConfig = editedConfig; // Trigger reactivity
        closeModelInfo();
      }
    }
  }

  // Fetch remote peer's available providers
  // Uses shared peerProviders store from coreService (updated via peer_providers_updated event)
  async function fetchRemotePeerProviders(peerId: string) {
    if (!peerId || peerId.trim() === '') {
      return;
    }

    remotePeerLoading = peerId;
    remotePeerError = '';

    try {
      const result = await sendCommand('query_remote_providers', { peer_id: peerId });

      // Check if sendCommand returned false (WebSocket not connected)
      if (result === false) {
        remotePeerError = 'Backend not connected. Please check the service is running.';
        return;
      }

      if (result && result.status === 'success') {
        // The store is updated automatically via peer_providers_updated event in coreService.ts
      } else {
        remotePeerError = result?.message || 'Failed to fetch providers';
      }
    } catch (error) {
      console.error('Error fetching remote peer providers:', error);
      remotePeerError = `Error: ${error}`;
    } finally {
      remotePeerLoading = '';
    }
  }

  // Get providers for a remote peer (uses shared store)
  function getRemotePeerProviders(peerId: string | undefined): any[] {
    if (!peerId) return [];
    return $peerProviders.get(peerId) || [];
  }

  // Get unique models from remote peer providers
  function getRemotePeerModels(peerId: string | undefined): string[] {
    const providers = getRemotePeerProviders(peerId);
    const models = new Set<string>();
    providers.forEach((p: any) => {
      if (p.model) {
        models.add(p.model);
      }
    });
    return Array.from(models).sort();
  }

  // Check if peer providers are loading
  function isRemotePeerLoading(peerId: string | undefined): boolean {
    if (!peerId) return false;
    return remotePeerLoading === peerId;
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open && displayConfig}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" role="presentation">
    <div class="modal" role="dialog" aria-labelledby="modal-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="modal-title">AI Providers Configuration</h2>
        <div class="header-actions">
          {#if !editMode}
            <button class="btn btn-edit" on:click={startEditing}>Edit</button>
          {:else}
            <button class="btn btn-save" on:click={saveChanges} disabled={isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </button>
            <button class="btn btn-cancel" on:click={cancelEditing} disabled={isSaving}>Cancel</button>
          {/if}
        </div>
        <button class="close-btn" on:click={close} aria-label="Close">&times;</button>
      </div>

      <!-- Tabs -->
      <div class="tabs">
        <button
          class="tab"
          class:active={selectedTab === 'list'}
          on:click={() => selectedTab = 'list'}
        >
          Providers ({displayConfig.providers.length})
        </button>
        {#if editMode}
          <button
            class="tab"
            class:active={selectedTab === 'add'}
            on:click={() => selectedTab = 'add'}
          >
            Add Provider
          </button>
        {/if}
      </div>

      <div class="modal-body">
        {#if selectedTab === 'list'}
          <!-- Account balance (pay-per-use providers, e.g. DeepSeek) — Phase 2b -->
          {#if hasPayPerUseProvider}
            <div class="balance-card balance-{balanceLevel}">
              <div class="balance-row">
                <span class="balance-label">
                  Account balance{balanceAlias ? ` (${balanceAlias})` : ''}
                </span>
                <button class="btn btn-edit" on:click={refreshBalance} disabled={balanceLoading}>
                  {balanceLoading ? 'Checking…' : 'Check balance'}
                </button>
              </div>
              {#if balanceError}
                <div class="balance-value balance-err">⚠ {balanceError}</div>
              {:else if balanceUnsupported}
                <div class="balance-value balance-muted">No balance-capable provider</div>
              {:else if balanceTotal !== null && balanceTotal !== undefined}
                <div class="balance-value">
                  {balanceCurrency} {balanceTotal}
                  {#if balanceLevel === 'critical'}<span class="balance-flag">⚠ critical (&lt; ${CRITICAL_BALANCE_USD})</span>
                  {:else if balanceLevel === 'low'}<span class="balance-flag">low (&lt; ${LOW_BALANCE_USD})</span>{/if}
                  {#if !balanceAvailable}<span class="balance-flag">— insufficient</span>{/if}
                </div>
              {:else}
                <div class="balance-value balance-muted">Not checked yet — click “Check balance”.</div>
              {/if}
            </div>
          {/if}

          <!-- Provider Cards -->
          <div class="providers-list">
            {#if editMode}
              <p class="role-hint">
                🧠 <strong>Knowledge extraction</strong> — unset means the model
                that answered in a conversation extracts that conversation; the
                extraction prompt carries the whole transcript.
              </p>
            {/if}
            {#each displayConfig.providers as provider, i (i)}
              <div class="provider-card" class:default={provider.alias === displayConfig.default_provider}>
                <div class="provider-header">
                  <h3>
                    {provider.alias}
                    {#if provider.alias === displayConfig.default_provider}<span class="default-badge">⭐ Text Default</span>{/if}
                    {#if provider.alias === displayConfig.vision_provider}<span class="default-badge vision-badge">👁️ Vision Default</span>{/if}
                    {#if provider.alias === displayConfig.voice_provider}<span class="default-badge voice-badge">🎤 Voice Default</span>{/if}
                    {#if provider.alias === displayConfig.agent_provider}<span class="default-badge agent-badge">🤖 Agent Default</span>{/if}
                    {#if provider.alias === displayConfig.knowledge_provider}<span class="default-badge">🧠 Knowledge Extraction</span>{/if}
                  </h3>
                  {#if editMode}
                    <button class="btn-delete" on:click={() => deleteProvider(i)}>Delete</button>
                  {/if}
                </div>

                {#if editMode && editedConfig}
                  <!-- Edit Form -->
                  <div class="provider-form">
                    <div class="form-group">
                      <label for="alias-{i}">Alias</label>
                      <input
                        id="alias-{i}"
                        type="text"
                        bind:value={editedConfig.providers[i].alias}
                        on:blur={() => handleAliasBlur(i)}
                        placeholder="my_provider"
                      />
                      {#if editedConfig.default_provider === provider.alias || editedConfig.vision_provider === provider.alias || editedConfig.voice_provider === provider.alias}
                        <p class="help-text">💡 Renaming will automatically update default settings</p>
                      {/if}
                    </div>

                    <div class="form-group">
                      <label for="type-{i}">Type</label>
                      <select id="type-{i}" bind:value={editedConfig.providers[i].type}>
                        <option value="ollama">Ollama</option>
                        <option value="openai_compatible">OpenAI Compatible</option>
                        <option value="anthropic">Anthropic</option>
                        <option value="zai">Z.AI</option>
                        <option value="zai_coding">Z.AI Coding Plan</option>
                        <option value="deepseek">DeepSeek</option>
                        <option value="llamacpp_server">llama-server (local, DPC pin)</option>
                        <option value="local_whisper">Local Whisper</option>
                        <option value="dpc_agent">DPC Agent</option>
                        <option value="gemini">Google Gemini</option>
                        <option value="github_models">GitHub Models</option>
                        <option value="gigachat">GigaChat (Sberbank)</option>
                      </select>
                    </div>

                    {#if editedConfig.providers[i].type !== 'dpc_agent'}
                      <div class="form-group">
                        <label for="model-{i}">Model</label>
                        <input
                          id="model-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].model}
                          placeholder="llama3.1:8b"
                        />
                      </div>
                    {:else if editedConfig.providers[i].type === 'dpc_agent'}
                      <div class="form-info">
                        <p><strong>DPC Agent</strong> - Embedded autonomous AI agent for task automation.</p>
                        <p>Uses your configured default AI provider. Add peer_id below to use a remote peer's models instead.</p>
                      </div>

                      <!-- Remote Peer Configuration (optional) -->
                      <div class="form-group">
                        <label for="peer-id-{i}">Remote Peer ID (optional)</label>
                        <div class="input-with-button">
                          <input
                            id="peer-id-{i}"
                            type="text"
                            bind:value={editedConfig.providers[i].peer_id}
                            placeholder="Leave empty for local inference"
                          />
                          <button
                            class="btn-fetch"
                            on:click={() => fetchRemotePeerProviders(editedConfig!.providers[i].peer_id || '')}
                            disabled={!editedConfig.providers[i].peer_id || isRemotePeerLoading(editedConfig.providers[i].peer_id)}
                            title="Fetch available providers from this peer"
                          >
                            {isRemotePeerLoading(editedConfig.providers[i].peer_id) ? '⏳' : '🔍'}
                          </button>
                        </div>
                        <p class="help-text">If set, agent uses this peer's models instead of local</p>
                        {#if remotePeerError && remotePeerLoading === editedConfig.providers[i].peer_id}
                          <p class="help-text warn">{remotePeerError}</p>
                        {/if}
                        {#if editedConfig.providers[i].peer_id && getRemotePeerProviders(editedConfig.providers[i].peer_id).length > 0}
                          <p class="help-text success">✓ Found {getRemotePeerProviders(editedConfig.providers[i].peer_id).length} providers</p>
                        {/if}
                      </div>

                      {#if editedConfig.providers[i].peer_id}
                        <!-- Remote Model Selection -->
                        <div class="form-group">
                          <label for="remote-model-{i}">Remote Model</label>
                          {#if getRemotePeerModels(editedConfig.providers[i].peer_id).length > 0}
                            <select
                              id="remote-model-{i}"
                              bind:value={editedConfig.providers[i].remote_model}
                            >
                              <option value="">Any available model</option>
                              {#each getRemotePeerModels(editedConfig.providers[i].peer_id) as model}
                                <option value={model}>{model}</option>
                              {/each}
                            </select>
                            <p class="help-text">Select model from remote peer (fetched)</p>
                          {:else}
                            <input
                              id="remote-model-{i}"
                              type="text"
                              bind:value={editedConfig.providers[i].remote_model}
                              placeholder="llama3:70b"
                            />
                            <p class="help-text">Enter model name manually (or fetch providers first)</p>
                          {/if}
                        </div>

                        <!-- Remote Provider Selection -->
                        <div class="form-group">
                          <label for="remote-provider-{i}">Remote Provider</label>
                          {#if getRemotePeerProviders(editedConfig.providers[i].peer_id).length > 0}
                            <select
                              id="remote-provider-{i}"
                              bind:value={editedConfig.providers[i].remote_provider}
                            >
                              <option value="">Any available provider</option>
                              {#each getRemotePeerProviders(editedConfig.providers[i].peer_id) as prov}
                                <option value={prov.alias}>{prov.alias} ({prov.type})</option>
                              {/each}
                            </select>
                            <p class="help-text">Select provider from remote peer (fetched)</p>
                          {:else}
                            <input
                              id="remote-provider-{i}"
                              type="text"
                              bind:value={editedConfig.providers[i].remote_provider}
                              placeholder="ollama_text"
                            />
                            <p class="help-text">Enter provider alias manually (or fetch providers first)</p>
                          {/if}
                        </div>

                        <!-- Timeout for remote inference -->
                        <div class="form-group">
                          <label for="timeout-{i}">Timeout (seconds)</label>
                          <input
                            id="timeout-{i}"
                            type="number"
                            bind:value={editedConfig.providers[i].timeout}
                            placeholder="180"
                            min="30"
                            max="600"
                          />
                          <p class="help-text">Timeout for remote inference (default: 180s, max: 600s)</p>
                        </div>
                      {/if}
                    {/if}

                    <!-- Type-specific fields -->
                    {#if editedConfig.providers[i].type === 'ollama'}
                      <div class="form-group">
                        <label for="host-{i}">Host</label>
                        <input
                          id="host-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].host}
                          placeholder="http://127.0.0.1:11434"
                        />
                        <p class="help-text">No API key needed for Ollama</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'llamacpp_server'}
                      <div class="form-group">
                        <label for="gguf-{i}">GGUF path</label>
                        <div class="path-row">
                          <input
                            id="gguf-{i}"
                            type="text"
                            bind:value={editedConfig.providers[i].gguf_path}
                            placeholder="C:\models\qwen3.8-27b-Q4_K_M.gguf"
                          />
                          {#if canBrowse}
                            <button
                              type="button"
                              class="btn btn-browse"
                              on:click={() => pickModelPath(i, 'gguf_path')}
                              title="Pick the model file from disk"
                            >Browse…</button>
                          {/if}
                        </div>
                        <p class="help-text">
                          Absolute path to the model file. DPC starts its own llama-server on it
                          (ADR-040): first call fetch-verifies the pinned binary, then serves —
                          no host, no key.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="mmproj-{i}">mmproj (vision projector, optional)</label>
                        <div class="path-row">
                          <input
                            id="mmproj-{i}"
                            type="text"
                            bind:value={editedConfig.providers[i].mmproj}
                            placeholder="C:\models\qwen3.8-27b-mmproj.gguf"
                          />
                          {#if canBrowse}
                            <button
                              type="button"
                              class="btn btn-browse"
                              on:click={() => pickModelPath(i, 'mmproj')}
                              title="Pick the projector file from disk"
                            >Browse…</button>
                          {/if}
                        </div>
                        <p class="help-text">
                          The vision projector file passed to llama-server as --mmproj. With it
                          the server serves images (and video) at full context; without it the
                          alias is text-only. Needs KV headroom — q4_0 leaves it, q8_0 does not.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="nctx-{i}">KV pool size, n_ctx (optional)</label>
                        <input
                          id="nctx-{i}"
                          type="number"
                          min="4096"
                          step="4096"
                          value={editedConfig.providers[i].n_ctx ?? ''}
                          placeholder="262144 (default)"
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.currentTarget as HTMLInputElement).value;
                            const n = parseInt(raw, 10);
                            editedConfig.providers[i].n_ctx = raw === '' || isNaN(n) ? undefined : n;
                          }}
                        />
                        <p class="help-text">
                          How many KV cells llama-server allocates (-c). Unset = 262 144. This is
                          <strong>one pool shared by every slot</strong>, not a per-conversation
                          limit — «Context Window» below is what a single conversation may occupy,
                          and nothing derives one from the other. Two agents of one group carried
                          137 616 + 139 819 tokens here and did not fit the default pool: the
                          parked conversation could not be laid back down and was re-read from
                          zero. Costs VRAM — measured ≈18 KiB per cell with q4_0 KV on this card.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="ubatch-{i}">Micro-batch size (optional)</label>
                        <input
                          id="ubatch-{i}"
                          type="number"
                          min="64"
                          step="64"
                          value={editedConfig.providers[i].n_ubatch ?? ''}
                          placeholder="512 (build default)"
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.currentTarget as HTMLInputElement).value;
                            const n = parseInt(raw, 10);
                            editedConfig.providers[i].n_ubatch = raw === '' || isNaN(n) ? undefined : n;
                          }}
                        />
                        <p class="help-text">
                          How many tokens the server reads at once inside a prompt (-ub). The gain
                          needs depth: on one card and one build (RTX PRO 4500, b10472, a 27B at
                          Q4_K_M) 1024 beat the default 512 by 5.6 % at a 60 000-token prefill and
                          2048 added 2.8 %, while below ~8 000 tokens it changed nothing. Those are
                          our numbers, not yours — and it costs VRAM (683 MiB for that step here),
                          so measure on your own install before raising it.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="batch-{i}">Batch size (optional)</label>
                        <input
                          id="batch-{i}"
                          type="number"
                          min="64"
                          step="64"
                          value={editedConfig.providers[i].n_batch ?? ''}
                          placeholder="2048 (build default)"
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.currentTarget as HTMLInputElement).value;
                            const n = parseInt(raw, 10);
                            editedConfig.providers[i].n_batch = raw === '' || isNaN(n) ? undefined : n;
                          }}
                        />
                        <p class="help-text">
                          The logical batch (-b) the micro-batch is cut from, so it is the ceiling
                          on the field above: a micro-batch larger than this is silently clamped.
                          We measured no effect from changing it on its own — it is here so that
                          raising the micro-batch past 2048 is possible rather than quietly ignored.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="cache-reuse-{i}">Cache reuse chunk (optional)</label>
                        <input
                          id="cache-reuse-{i}"
                          type="number"
                          min="0"
                          step="64"
                          value={editedConfig.providers[i].cache_reuse ?? ''}
                          placeholder="0 — off (build default)"
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.currentTarget as HTMLInputElement).value;
                            const n = parseInt(raw, 10);
                            editedConfig.providers[i].cache_reuse = raw === '' || isNaN(n) ? undefined : n;
                          }}
                        />
                        <p class="help-text">
                          The smallest run of tokens the server will try to keep by shifting the KV
                          cache when a cached prompt diverges in the middle (--cache-reuse). Off by
                          default, and with it off one changed line early in a prompt costs a
                          re-read of everything behind it — which matters if your prompt rebuilds
                          anything ahead of the conversation. We have not measured a value on this
                          fleet yet; start around 256 and watch how much of each prompt the server
                          reports as already present.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="kv-type-{i}">KV cache type (optional)</label>
                        <select
                          id="kv-type-{i}"
                          value={editedConfig.providers[i].cache_type_k ?? ''}
                          on:change={(e) => {
                            if (!editedConfig) return;
                            const v = (e.target as HTMLSelectElement).value;
                            const p = editedConfig.providers[i];
                            if (v === '') {
                              delete p.cache_type_k;
                              delete p.cache_type_v;
                            } else {
                              p.cache_type_k = v;
                              p.cache_type_v = v;
                            }
                            editedConfig = editedConfig;
                          }}
                        >
                          <!-- The nine types `llama-server --help` accepts for -ctk/-ctv, in
                               cost order. Only four were offered before, which hid the three
                               rungs between q4_0 and q8_0 — and hid iq4_nl, which is free.
                               Bits per element rather than GiB: the GiB figure depends on the
                               model's attention layers and head width, so a number baked into
                               this label would be wrong for every model but one. -->
                          <option value="">Auto (q8_0 → q4_0 by free VRAM)</option>
                          <option value="f32">f32 — 32 bit, reference precision</option>
                          <option value="f16">f16 — 16 bit (needs headroom, check the card)</option>
                          <option value="bf16">bf16 — 16 bit, same size as f16</option>
                          <option value="q8_0">q8_0 — 8.5 bit, near-lossless</option>
                          <option value="q5_1">q5_1 — 6 bit</option>
                          <option value="q5_0">q5_0 — 5.5 bit</option>
                          <option value="q4_1">q4_1 — 5 bit</option>
                          <option value="iq4_nl">iq4_nl — 4.5 bit, same size as q4_0, non-linear</option>
                          <option value="q4_0">q4_0 — 4.5 bit</option>
                        </select>
                        <p class="help-text">
                          Auto never picks f16: on a full card Windows pages it into system RAM
                          and prefill collapses instead of failing. An explicit choice is loaded
                          as configured, with a warning in the log when the arithmetic says it
                          does not fit.
                          <br />
                          Cost scales with bits per element, so q8_0 is roughly twice q4_0 and
                          f16 roughly four times. <strong>iq4_nl occupies exactly as much as
                          q4_0</strong> — same 4.5 bits, non-linear spacing — so it is the one
                          change here with no memory cost. Whether that buys anything at your
                          context depth is unmeasured; it cannot cost you VRAM to find out.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="n-parallel-{i}">Parallel slots (optional)</label>
                        <input
                          id="n-parallel-{i}"
                          type="number"
                          min="1"
                          value={editedConfig.providers[i].n_parallel ?? ''}
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.target as HTMLInputElement).value;
                            const n = parseInt(raw);
                            const p = editedConfig.providers[i];
                            if (raw === '' || isNaN(n)) {
                              delete p.n_parallel;
                            } else {
                              p.n_parallel = n;
                            }
                            editedConfig = editedConfig;
                          }}
                          placeholder="auto — 4 slots here"
                        />
                        <p class="help-text">
                          Empty sends nothing, and the build's own default for <code>-np</code>
                          is <code>-1</code>, meaning auto — not a fixed number. On this fleet
                          auto has resolved to <strong>4</strong> unified slots; the child prints
                          <code>n_slots = 4</code> in its startup line, so the figure is one you
                          can check rather than one this form promises. An explicit value is
                          always sent — set 1 to serialize every request through one slot.
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="reasoning-budget-{i}">Reasoning budget (tokens, optional)</label>
                        <input
                          id="reasoning-budget-{i}"
                          type="number"
                          value={editedConfig.providers[i].reasoning_budget_tokens ?? ''}
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.target as HTMLInputElement).value;
                            const n = parseInt(raw);
                            editedConfig.providers[i].reasoning_budget_tokens = raw === '' || isNaN(n) ? undefined : n;
                            editedConfig = editedConfig;
                          }}
                          placeholder="e.g. 10000"
                        />
                        <p class="help-text">
                          Caps thinking per request. Without it the template's own default
                          effort (xhigh) is unbounded — on deep context it can spend the whole
                          window thinking and answer nothing.
                        </p>
                      </div>

                      <details class="supervisor-flags">
                        <summary>Supervisor flags — the rest of what the child is started with</summary>
                        <p class="help-text">
                          Every field here is optional and every one of them means the same
                          thing when left empty: <strong>the supervisor's own default is
                          used</strong>. Clearing a field removes it from the alias rather than
                          writing a zero — an explicit 0 and an absent key are different
                          instructions to the child.
                        </p>

                        <div class="form-group">
                          <label for="spec-type-{i}">Speculative decoding</label>
                          <select
                            id="spec-type-{i}"
                            value={editedConfig.providers[i].spec_type ?? ''}
                            on:change={(e) => setStr(i, 'spec_type', (e.target as HTMLSelectElement).value)}
                          >
                            <option value="">default (draft-mtp)</option>
                            <option value="none">none — plain decoding</option>
                            <option value="draft-mtp">draft-mtp — the head inside the model file</option>
                            <option value="draft-eagle3">draft-eagle3</option>
                            <option value="draft-simple">draft-simple</option>
                            <option value="draft-dflash">draft-dflash</option>
                            <option value="draft-dspark">draft-dspark</option>
                            <option value="ngram-simple">ngram-simple</option>
                            <option value="ngram-cache">ngram-cache</option>
                          </select>
                          <p class="help-text">
                            <code>draft-mtp</code> needs nothing else: the head ships inside the
                            GGUF. Everything beginning <code>draft-</code> other than that needs
                            a separate drafter file, named through <code>--spec-draft-model</code>
                            in Extra flags below — and a drafter beside an mmproj kills every
                            request carrying an image on this build.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="spec-n-max-{i}">Draft tokens per step (spec_draft_n_max)</label>
                          <input
                            id="spec-n-max-{i}"
                            type="number"
                            min="1"
                            value={editedConfig.providers[i].spec_draft_n_max ?? ''}
                            on:input={(e) => setNum(i, 'spec_draft_n_max', (e.target as HTMLInputElement).value)}
                            placeholder="default 3"
                          />
                          <p class="help-text">
                            How many tokens the drafter proposes before the model verifies.
                            Higher is not automatically worse: acceptance <em>ratio</em> falls
                            with it while accepted <em>length</em> — which is what throughput
                            follows — can still rise. Judge it by the child's
                            <code>mean len</code>, not by <code>draft acceptance</code>.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="ngl-{i}">GPU layers (n_gpu_layers)</label>
                          <input
                            id="ngl-{i}"
                            type="number"
                            value={editedConfig.providers[i].n_gpu_layers ?? ''}
                            on:input={(e) => setNum(i, 'n_gpu_layers', (e.target as HTMLInputElement).value)}
                            placeholder="default 999 (all)"
                          />
                          <p class="help-text">
                            999 puts every context fully on the card, which was measured worth
                            +11.3 % of prefill here. Lower it only for a card that cannot hold
                            the whole model — a partial split disables fused kernels on the
                            layers that land on the CPU side.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="flash-attn-{i}">Flash attention</label>
                          <select
                            id="flash-attn-{i}"
                            value={editedConfig.providers[i].flash_attn === undefined ? '' : String(editedConfig.providers[i].flash_attn)}
                            on:change={(e) => setBool(i, 'flash_attn', (e.target as HTMLSelectElement).value)}
                          >
                            <option value="">default (off)</option>
                            <option value="true">on</option>
                            <option value="false">off</option>
                          </select>
                          <p class="help-text">
                            Passed as <code>--flash-attn</code>. Its kernels exist only for some
                            KV types; with a type they do not cover, attention falls back to the
                            CPU and prefill collapses. Change one thing at a time and read the
                            child's <code>prompt processing</code> rate afterwards.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="kv-unified-{i}">Unified KV pool</label>
                          <select
                            id="kv-unified-{i}"
                            value={editedConfig.providers[i].kv_unified === undefined ? '' : String(editedConfig.providers[i].kv_unified)}
                            on:change={(e) => setBool(i, 'kv_unified', (e.target as HTMLSelectElement).value)}
                          >
                            <option value="">default (on)</option>
                            <option value="true">on — one pool shared by all slots</option>
                            <option value="false">off — the pool is split per slot</option>
                          </select>
                          <p class="help-text">
                            Only means anything above one slot: at a single slot unified and
                            split are the same pool. Sent as <code>--kv-unified</code> alongside
                            <code>-np</code>.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="ctx-checkpoints-{i}">Context checkpoints per slot</label>
                          <input
                            id="ctx-checkpoints-{i}"
                            type="number"
                            min="0"
                            value={editedConfig.providers[i].ctx_checkpoints ?? ''}
                            on:input={(e) => setNum(i, 'ctx_checkpoints', (e.target as HTMLInputElement).value)}
                            placeholder="default 32 (the build's)"
                          />
                          <p class="help-text">
                            A checkpoint snapshots the recurrent state and costs ~585–700 MiB
                            here, so the count decides how many parked conversations fit rather
                            than whether resuming works at all. Four checkpoints put a 150K state
                            near 5 GB.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="checkpoint-step-{i}">Minimum tokens between checkpoints</label>
                          <input
                            id="checkpoint-step-{i}"
                            type="number"
                            min="0"
                            value={editedConfig.providers[i].checkpoint_min_step ?? ''}
                            on:input={(e) => setNum(i, 'checkpoint_min_step', (e.target as HTMLInputElement).value)}
                            placeholder="default 8192 (the build's)"
                          />
                          <p class="help-text">
                            Read together with the count above: spacing times count is how far
                            back the child can resume from.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="cache-ram-{i}">Host prompt cache, MiB (cache_ram_mib)</label>
                          <input
                            id="cache-ram-{i}"
                            type="number"
                            min="0"
                            value={editedConfig.providers[i].cache_ram_mib ?? ''}
                            on:input={(e) => setNum(i, 'cache_ram_mib', (e.target as HTMLInputElement).value)}
                            placeholder="e.g. 24576"
                          />
                          <p class="help-text">
                            System RAM, <strong>not</strong> VRAM — it holds whole conversations
                            outside the card so a returning slot need not re-read its prompt.
                            Raising it costs nothing on the GPU.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="slot-save-{i}">Slot save path</label>
                          <input
                            id="slot-save-{i}"
                            type="text"
                            value={editedConfig.providers[i].slot_save_path ?? ''}
                            on:input={(e) => setStr(i, 'slot_save_path', (e.target as HTMLInputElement).value)}
                            placeholder="empty = slot state is not persisted"
                          />
                          <p class="help-text">
                            Directory the child writes slot state into
                            (<code>--slot-save-path</code>). Empty means state lives only for as
                            long as the process does.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="binary-path-{i}">llama-server binary (overrides the pin)</label>
                          <div class="path-row">
                            <input
                              id="binary-path-{i}"
                              type="text"
                              value={editedConfig.providers[i].binary_path ?? ''}
                              on:input={(e) => setStr(i, 'binary_path', (e.target as HTMLInputElement).value)}
                              placeholder="empty = the ADR-040 pinned build"
                            />
                            {#if canBrowse}
                              <button
                                type="button"
                                class="btn btn-browse"
                                on:click={() => pickBinaryPath(i)}
                                title="Pick a llama-server executable from disk"
                              >Browse…</button>
                            {/if}
                          </div>
                          <p class="help-text warn">
                            Empty is the right answer almost always: the pin is fetched and
                            hash-verified, a hand-picked build is neither. Set it only to test a
                            build the pin does not contain, and set it back afterwards. A path
                            that names no file is refused loudly rather than falling back.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="jinja-{i}">Jinja chat template</label>
                          <select
                            id="jinja-{i}"
                            value={editedConfig.providers[i].jinja === undefined ? '' : String(editedConfig.providers[i].jinja)}
                            on:change={(e) => setBool(i, 'jinja', (e.target as HTMLSelectElement).value)}
                          >
                            <option value="">default (on)</option>
                            <option value="true">on</option>
                            <option value="false">off</option>
                          </select>
                          <p class="help-text">
                            Uses the template baked into the GGUF. Off falls back to the
                            server's built-in formatting, which for most modern models is the
                            wrong one — turn it off only if the model ships no template.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="start-timeout-{i}">Start timeout, seconds</label>
                          <input
                            id="start-timeout-{i}"
                            type="number"
                            min="1"
                            value={editedConfig.providers[i].start_timeout_s ?? ''}
                            on:input={(e) => setNum(i, 'start_timeout_s', (e.target as HTMLInputElement).value, true)}
                            placeholder="default 300"
                          />
                          <p class="help-text">
                            How long the supervisor waits for the child to answer /health before
                            giving up with the child's last log lines attached. A very large
                            model on a cold disk can need more than 300 s.
                            <br />
                            The one field here that does <strong>not</strong> restart the child:
                            it is not part of the command line, so a save that changes nothing
                            else keeps the running model loaded. The new value is still recorded
                            and applies to the next start — you simply do not pay a re-load to
                            set it.
                          </p>
                        </div>

                        <div class="form-group">
                          <label for="extra-args-{i}">Extra flags (one token per line)</label>
                          <textarea
                            id="extra-args-{i}"
                            rows="4"
                            value={(editedConfig.providers[i].extra_args ?? []).join('\n')}
                            on:input={(e) => setExtraArgs(i, (e.target as HTMLTextAreaElement).value)}
                            placeholder={'--spec-draft-model\nD:\\models\\drafter.gguf\n--spec-draft-ngl\n999'}
                          ></textarea>
                          <p class="help-text warn">
                            Appended to the command line last, verbatim and unchecked. One token
                            per line — a flag and its value are separate lines, which is what
                            keeps a path containing spaces in one piece. Anything the child
                            rejects makes it exit before becoming healthy, and the supervisor
                            reports that with the child's own last lines.
                          </p>
                        </div>
                      </details>
                    {/if}

                    {#if editedConfig.providers[i].type === 'openai_compatible'}
                      <div class="form-group">
                        <label for="base-url-{i}">Base URL</label>
                        <input
                          id="base-url-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].base_url}
                          placeholder="https://api.openai.com/v1"
                        />
                      </div>

                      <div class="form-group">
                        <strong class="form-label">API Key Source</strong>
                        <div class="radio-group">
                          <label>
                            <input
                              type="radio"
                              name="key-{i}"
                              checked={!!editedConfig.providers[i].api_key_env}
                              on:change={() => switchToEnv(i)}
                            />
                            Environment Variable (Recommended)
                          </label>
                          <label>
                            <input
                              type="radio"
                              name="key-{i}"
                              checked={!!editedConfig.providers[i].api_key}
                              on:change={() => switchToPlaintext(i)}
                            />
                            Plaintext (Local providers only)
                          </label>
                        </div>

                        {#if editedConfig.providers[i].api_key_env}
                          <input
                            type="text"
                            bind:value={editedConfig.providers[i].api_key_env}
                            placeholder="OPENAI_API_KEY"
                          />
                          <p class="help-text">Set this environment variable before starting the service</p>
                        {:else if editedConfig.providers[i].api_key !== undefined}
                          <form on:submit|preventDefault>
                            <input
                              type="password"
                              bind:value={editedConfig.providers[i].api_key}
                              placeholder="sk-..."
                              autocomplete="off"
                            />
                          </form>
                          <p class="help-text warn">⚠️ Stored in plaintext - only use for local providers</p>
                        {/if}
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'anthropic'}
                      <div class="form-group">
                        <label for="api-key-env-{i}">API Key Environment Variable</label>
                        <input
                          id="api-key-env-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].api_key_env}
                          placeholder="ANTHROPIC_API_KEY"
                        />
                        <p class="help-text">Set this environment variable before starting the service</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'zai_coding'}
                      <div class="form-group">
                        <label for="base-url-{i}">Base URL (Coding Plan)</label>
                        <input
                          id="base-url-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].base_url}
                          placeholder="https://api.z.ai/api/coding/paas/v4"
                        />
                        <p class="help-text">GLM Coding Plan endpoint (OpenAI-compatible)</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'deepseek'}
                      <div class="form-group">
                        <label for="base-url-{i}">Base URL</label>
                        <input
                          id="base-url-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].base_url}
                          placeholder="https://api.deepseek.com"
                        />
                        <p class="help-text">DeepSeek OpenAI-compatible endpoint</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'zai' || editedConfig.providers[i].type === 'zai_coding' || editedConfig.providers[i].type === 'deepseek'}
                      <div class="form-group">
                        <label for="api-key-env-{i}">API Key Environment Variable</label>
                        <input
                          id="api-key-env-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].api_key_env}
                          placeholder="ZAI_API_KEY"
                        />
                        <p class="help-text">Recommended: Store API key in environment variable</p>
                      </div>

                      <div class="form-group">
                        <label for="api-key-{i}">API Key (plaintext, alternative)</label>
                        <form on:submit|preventDefault>
                          <input
                            id="api-key-{i}"
                            type="password"
                            bind:value={editedConfig.providers[i].api_key}
                            placeholder="Leave blank to use environment variable"
                            autocomplete="off"
                          />
                        </form>
                        <p class="help-text warn">⚠️ Not recommended: Stores key in config file</p>
                      </div>

                      <div class="form-group">
                        <label for="zai-max-tokens-{i}">Max Tokens (output)</label>
                        <input
                          id="zai-max-tokens-{i}"
                          type="number"
                          bind:value={editedConfig.providers[i].max_tokens}
                          placeholder="8192"
                        />
                        <p class="help-text">Max output tokens per response (model-dependent; GLM-5.x up to 131072)</p>
                      </div>

                      <div class="form-group">
                        <label for="zai-thinking-{i}">
                          <input
                            id="zai-thinking-{i}"
                            type="checkbox"
                            checked={editedConfig.providers[i].thinking?.enabled ?? false}
                            on:change={(e) => {
                              if (!editedConfig) return;
                              const p = editedConfig.providers[i];
                              if (!p.thinking) p.thinking = {};
                              p.thinking.enabled = (e.target as HTMLInputElement).checked;
                              editedConfig = editedConfig;
                            }}
                          />
                          Enable extended thinking
                        </label>
                      </div>

                      {#if editedConfig.providers[i].thinking?.enabled}
                        <div class="form-group">
                          <label for="zai-thinking-budget-{i}">Thinking Budget (tokens)</label>
                          <input
                            id="zai-thinking-budget-{i}"
                            type="number"
                            value={editedConfig.providers[i].thinking?.budget_tokens ?? ''}
                            on:input={(e) => {
                              if (!editedConfig) return;
                              const p = editedConfig.providers[i];
                              if (!p.thinking) p.thinking = {};
                              const v = (e.target as HTMLInputElement).value;
                              p.thinking.budget_tokens = v === '' ? undefined : parseInt(v);
                              editedConfig = editedConfig;
                            }}
                            placeholder="10000"
                          />
                          <p class="help-text">Reasoning budget; must be less than Max Tokens</p>
                        </div>
                      {/if}

                      <div class="form-group">
                        <label for="zai-top-p-{i}">Top P (optional)</label>
                        <input
                          id="zai-top-p-{i}"
                          type="number"
                          step="0.05"
                          min="0"
                          max="1"
                          bind:value={editedConfig.providers[i].top_p}
                          placeholder="e.g. 0.9"
                        />
                        <p class="help-text">Nucleus sampling; lower reduces language mixing</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'local_whisper'}
                      <div class="form-group">
                        <label for="device-{i}">Device</label>
                        <select id="device-{i}" bind:value={editedConfig.providers[i].device}>
                          <option value="auto">Auto (detect best available)</option>
                          <option value="mlx">MLX (Apple Silicon - M1/M2/M3/M4)</option>
                          <option value="cuda">CUDA (NVIDIA GPU)</option>
                          <option value="mps">MPS (macOS Metal)</option>
                          <option value="cpu">CPU</option>
                        </select>
                        <p class="help-text">GPU recommended for fast transcription (~10-15x real-time)</p>
                      </div>

                      <div class="form-group">
                        <label for="compile-model-{i}">
                          <input
                            id="compile-model-{i}"
                            type="checkbox"
                            bind:checked={editedConfig.providers[i].compile_model}
                          />
                          Enable torch.compile (4.5x speedup)
                        </label>
                        <p class="help-text">First transcription will be slower (model compilation)</p>
                      </div>

                      <div class="form-group">
                        <label for="use-flash-attention-{i}">
                          <input
                            id="use-flash-attention-{i}"
                            type="checkbox"
                            bind:checked={editedConfig.providers[i].use_flash_attention}
                          />
                          Use Flash Attention 2 (20% speedup)
                        </label>
                        <p class="help-text warn">⚠️ Requires flash-attn package (difficult to install on Windows)</p>
                      </div>

                      <div class="form-group">
                        <label for="chunk-length-{i}">Chunk Length (seconds)</label>
                        <input
                          id="chunk-length-{i}"
                          type="number"
                          bind:value={editedConfig.providers[i].chunk_length_s}
                          placeholder="30"
                          min="10"
                          max="60"
                        />
                        <p class="help-text">Smaller = faster but less accurate for long audio</p>
                      </div>

                      <div class="form-group">
                        <label for="batch-size-{i}">Batch Size</label>
                        <input
                          id="batch-size-{i}"
                          type="number"
                          bind:value={editedConfig.providers[i].batch_size}
                          placeholder="16"
                          min="1"
                          max="32"
                        />
                        <p class="help-text">Higher = faster processing (more VRAM required)</p>
                      </div>

                      <div class="form-group">
                        <label for="language-{i}">Language</label>
                        <input
                          id="language-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].language}
                          placeholder="auto"
                        />
                        <p class="help-text">'auto' or language code (e.g., 'en', 'ru', 'fr')</p>
                      </div>

                      <div class="form-group">
                        <label for="task-{i}">Task</label>
                        <select id="task-{i}" bind:value={editedConfig.providers[i].task}>
                          <option value="transcribe">Transcribe (same language)</option>
                          <option value="translate">Translate (to English)</option>
                        </select>
                      </div>

                      <div class="form-group">
                        <label for="lazy-loading-{i}">
                          <input
                            id="lazy-loading-{i}"
                            type="checkbox"
                            bind:checked={editedConfig.providers[i].lazy_loading}
                          />
                          Lazy Loading (load on first use)
                        </label>
                        <p class="help-text">Model loads on first transcription (~3GB download)</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'gigachat'}
                      <div class="form-group">
                        <label for="gigachat-scope-{i}">Scope</label>
                        <select id="gigachat-scope-{i}" bind:value={editedConfig.providers[i].scope}>
                          <option value="GIGACHAT_API_PERS">Personal / Free (GIGACHAT_API_PERS)</option>
                          <option value="GIGACHAT_API_B2B">Business (GIGACHAT_API_B2B)</option>
                          <option value="GIGACHAT_API_CORP">Corporate (GIGACHAT_API_CORP)</option>
                        </select>
                        <p class="help-text">Match your Sberbank account type</p>
                      </div>

                      <div class="form-group">
                        <label for="gigachat-verify-ssl-{i}">
                          <input
                            id="gigachat-verify-ssl-{i}"
                            type="checkbox"
                            bind:checked={editedConfig.providers[i].verify_ssl}
                          />
                          Verify SSL (requires Russian CA cert)
                        </label>
                        <p class="help-text">
                          Install cert once: <code>curl -k "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" -w "\n" >> $(python -m certifi)</code>
                        </p>
                      </div>

                      <div class="form-group">
                        <label for="gigachat-ca-bundle-{i}">CA Bundle File (optional)</label>
                        <input
                          id="gigachat-ca-bundle-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].ca_bundle_file}
                          placeholder="/path/to/russian_trusted_root_ca_pem.crt"
                        />
                        <p class="help-text">Alternative to installing cert via certifi</p>
                      </div>
                    {/if}

                    <div class="form-group">
                      <label for="context-window-{i}">Context Window (optional)</label>
                      <select
                        id="context-window-select-{i}"
                        value={editedConfig.providers[i].context_window || ''}
                        on:change={(e) => {
                          if (!editedConfig) return;
                          const val = (e.target as HTMLSelectElement).value;
                          editedConfig.providers[i].context_window = val === '' ? undefined : parseInt(val);
                          editedConfig = editedConfig;
                        }}
                      >
                        <option value="">Auto-detected</option>
                        {#each CONTEXT_WINDOW_PRESETS as preset}
                          <option value={preset.value}>{preset.label}</option>
                        {/each}
                      </select>

                      <input
                        id="context-window-custom-{i}"
                        type="number"
                        bind:value={editedConfig.providers[i].context_window}
                        placeholder="Or enter exact value (tokens)"
                        class="custom-context-input"
                      />
                    </div>

                    <!-- Temperature Setting -->
                    <div class="form-group">
                      <label for="temperature-{i}">Temperature (optional)</label>
                      <select
                        id="temperature-select-{i}"
                        value={tempSelectValue(i)}
                        on:change={(e) => {
                          if (!editedConfig) return;
                          const val = (e.target as HTMLSelectElement).value;
                          if (val === 'custom') {
                            customTempMode[i] = true;
                          } else if (val === '') {
                            editedConfig.providers[i].temperature = undefined;
                            customTempMode[i] = false;
                          } else {
                            editedConfig.providers[i].temperature = parseFloat(val);
                            customTempMode[i] = false;
                          }
                          customTempMode = customTempMode;
                          editedConfig = editedConfig;
                        }}
                      >
                        <option value="">{temperatureDefaultLabel(editedConfig.providers[i].type)}</option>
                        {#each TEMPERATURE_PRESETS as preset}
                          <option value={preset.value}>{preset.label} - {preset.description}</option>
                        {/each}
                        <option value="custom">Custom...</option>
                      </select>
                      {#if temperatureIsInert(editedConfig.providers[i])}
                        <p class="help-text warn">Not sent while thinking is enabled — DeepSeek ignores it. Turn thinking off for this alias to steer sampling.</p>
                      {:else}
                        <p class="help-text">Controls creativity: 0.2 = deterministic, 1.5 = creative</p>
                      {/if}

                      {#if tempSelectValue(i) === 'custom'}
                        <input
                          id="temperature-custom-{i}"
                          type="number"
                          value={editedConfig.providers[i].temperature ?? ''}
                          on:input={(e) => {
                            if (!editedConfig) return;
                            const raw = (e.target as HTMLInputElement).value;
                            const n = parseFloat(raw);
                            editedConfig.providers[i].temperature = raw === '' || isNaN(n) ? undefined : n;
                            editedConfig = editedConfig;
                          }}
                          placeholder="0.0 - 2.0"
                          min="0"
                          max="2"
                          step="0.1"
                          class="custom-context-input"
                        />
                      {/if}
                    </div>

                    {#if editedConfig.providers[i].type === 'ollama'}
                      <div class="form-group">
                        <label for="think-{i}">Thinking</label>
                        <select
                          id="think-{i}"
                          value={thinkSelectValue(i)}
                          on:change={(e) => {
                            if (!editedConfig) return;
                            const val = (e.target as HTMLSelectElement).value;
                            editedConfig.providers[i].think =
                              val === '' ? undefined : val === 'on';
                            editedConfig = editedConfig;
                          }}
                        >
                          <option value="">Model default (on if the model can)</option>
                          <option value="on">Always on</option>
                          <option value="off">Always off</option>
                        </select>
                        <p class="help-text">
                          Reasoning before the answer. Turn it off when a model
                          spends its whole output budget thinking and returns
                          nothing — that is a real failure we have seen, not a
                          preference about style.
                        </p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'ollama' || editedConfig.providers[i].type === 'llamacpp_server'}
                      <div class="form-group">
                        <label for="sampling-{i}">Sampling Parameters (optional)</label>
                        <p class="help-text">
                          {editedConfig.providers[i].type === 'llamacpp_server'
                            ? 'Empty = provider default. The model card prescribes top_p 0.95 and top_k 20 for thinking mode.'
                            : 'Empty = model default. Check actual values via the ⓘ Query Info button.'}
                        </p>
                        <div class="sampling-grid" id="sampling-{i}">
                          {#each (editedConfig.providers[i].type === 'llamacpp_server' ? LLAMA_SAMPLING_PARAMS : OLLAMA_SAMPLING_PARAMS) as param}
                            <div class="sampling-field">
                              <label for="sampling-{param.key}-{i}">{param.key}</label>
                              <input
                                id="sampling-{param.key}-{i}"
                                type="number"
                                step={param.step}
                                min={param.min}
                                max={param.max}
                                value={getSamplingParam(i, param.key)}
                                placeholder={param.hint}
                                on:input={(e) => setSamplingParam(i, param.key, (e.target as HTMLInputElement).value, param.isInt)}
                              />
                            </div>
                          {/each}
                        </div>
                      </div>
                    {/if}

                    <div class="default-buttons">
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.default_provider}
                        on:click={() => setDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.default_provider ? '✓ Text Default' : 'Set as Text Default'}
                      </button>
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.vision_provider}
                        on:click={() => setVisionDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.vision_provider ? '✓ Vision Default' : 'Set as Vision Default'}
                      </button>
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.voice_provider}
                        on:click={() => setVoiceDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.voice_provider ? '✓ Voice Default' : 'Set as Voice Default'}
                      </button>
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.agent_provider}
                        on:click={() => setAgentDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.agent_provider ? '✓ Agent Default' : 'Set as Agent Default'}
                      </button>
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.knowledge_provider}
                        title="Extracts knowledge from a conversation. Unset means the model that answered in that conversation does it."
                        on:click={() => setKnowledgeDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.knowledge_provider ? '✓ Knowledge Extraction' : 'Set knowledge extraction default'}
                      </button>
                    </div>
                  </div>
                {:else}
                  <!-- Display Mode -->
                  <div class="provider-info">
                    <p><strong>Type:</strong> {provider.type}</p>
                    <p><strong>Model:</strong> {provider.model}</p>

                    {#if provider.type === 'ollama'}
                      <p><strong>Host:</strong> {provider.host}</p>
                    {/if}

                    {#if provider.type === 'openai_compatible'}
                      <p><strong>Base URL:</strong> {provider.base_url}</p>
                      {#if provider.api_key_env}
                        <p><strong>API Key:</strong> ${provider.api_key_env} (env var)</p>
                      {:else if provider.api_key}
                        <p><strong>API Key:</strong> ••••••••</p>
                      {:else}
                        <p class="warn">⚠️ No API key configured</p>
                      {/if}
                    {/if}

                    {#if provider.type === 'anthropic'}
                      {#if provider.api_key_env}
                        <p><strong>API Key:</strong> ${provider.api_key_env} (env var)</p>
                      {:else}
                        <p class="warn">⚠️ No API key configured</p>
                      {/if}
                    {/if}

                    {#if provider.type === 'local_whisper'}
                      <p><strong>Device:</strong> {provider.device || 'auto'}</p>
                      <p><strong>Torch Compile:</strong> {provider.compile_model ? 'Enabled' : 'Disabled'}</p>
                      <p><strong>Flash Attention:</strong> {provider.use_flash_attention ? 'Enabled' : 'Disabled'}</p>
                      {#if provider.chunk_length_s}
                        <p><strong>Chunk Length:</strong> {provider.chunk_length_s}s</p>
                      {/if}
                      {#if provider.batch_size}
                        <p><strong>Batch Size:</strong> {provider.batch_size}</p>
                      {/if}
                      <p><strong>Language:</strong> {provider.language || 'auto'}</p>
                      <p><strong>Task:</strong> {provider.task || 'transcribe'}</p>
                      <p><strong>Lazy Loading:</strong> {provider.lazy_loading !== false ? 'Enabled' : 'Disabled'}</p>
                    {/if}

                    {#if provider.type === 'dpc_agent' && provider.peer_id}
                      <p><strong>Remote Peer ID:</strong> {provider.peer_id}</p>
                      {#if provider.remote_model}
                        <p><strong>Remote Model:</strong> {provider.remote_model}</p>
                      {/if}
                      {#if provider.remote_provider}
                        <p><strong>Remote Provider:</strong> {provider.remote_provider}</p>
                      {/if}
                    {/if}

                    {#if provider.context_window}
                      <p><strong>Context Window:</strong> {provider.context_window.toLocaleString()} tokens</p>
                    {/if}

                    {#if provider.temperature !== undefined}
                      <p><strong>Temperature:</strong> {provider.temperature}</p>
                    {/if}

                    {#if provider.think !== undefined}
                      <p><strong>Thinking:</strong> {provider.think ? 'always on' : 'always off'}</p>
                    {/if}

                    {#if [...OLLAMA_SAMPLING_PARAMS, ...LLAMA_SAMPLING_PARAMS].some(p => (provider as any)[p.key] !== undefined)}
                      <p><strong>Sampling:</strong> {[...OLLAMA_SAMPLING_PARAMS, ...LLAMA_SAMPLING_PARAMS].filter(p => (provider as any)[p.key] !== undefined).map(p => `${p.key}=${(provider as any)[p.key]}`).join(', ')}</p>
                    {/if}

                    {#if provider.type === 'ollama'}
                      <button class="btn-query-info" on:click={() => queryModelInfo(provider.alias)}>
                        🔍 Query Model Info
                      </button>
                    {/if}
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {:else if selectedTab === 'add'}
          <!-- Add Provider Form -->
          <div class="add-provider-form">
            <h3>Add New Provider</h3>

            <div class="form-group">
              <label for="new-alias">Alias</label>
              <input
                id="new-alias"
                type="text"
                bind:value={newProvider.alias}
                placeholder="my_provider"
              />
            </div>

            <div class="form-group">
              <label for="new-type">Type</label>
              <select id="new-type" bind:value={newProvider.type}>
                <option value="ollama">Ollama</option>
                <option value="openai_compatible">OpenAI Compatible</option>
                <option value="anthropic">Anthropic</option>
                <option value="zai">Z.AI</option>
                <option value="zai_coding">Z.AI Coding Plan</option>
                <option value="deepseek">DeepSeek</option>
                <option value="llamacpp_server">llama-server (local, DPC pin)</option>
                <option value="local_whisper">Local Whisper</option>
                <option value="dpc_agent">DPC Agent</option>
                <option value="gemini">Google Gemini</option>
                <option value="github_models">GitHub Models</option>
                <option value="gigachat">GigaChat (Sberbank)</option>
              </select>
            </div>

            {#if newProvider.type !== 'dpc_agent'}
              <div class="form-group">
                <label for="new-model">{newProvider.type === 'llamacpp_server' ? 'GGUF path' : 'Model'}</label>
                <input
                  id="new-model"
                  type="text"
                  bind:value={newProvider.model}
                  placeholder={
                    newProvider.type === 'ollama' ? 'llama3.1:8b' :
                    newProvider.type === 'llamacpp_server' ? 'C:\\models\\qwen3.8-27b-Q4_K_M.gguf' :
                    newProvider.type === 'openai_compatible' ? 'gpt-4o' :
                    newProvider.type === 'local_whisper' ? 'openai/whisper-large-v3' :
                    newProvider.type === 'zai' ? 'glm-4.7' :
                    newProvider.type === 'zai_coding' ? 'glm-5.2' :
                    newProvider.type === 'deepseek' ? 'deepseek-v4-flash' :
                    newProvider.type === 'gemini' ? 'gemini-2.0-flash' :
                    newProvider.type === 'github_models' ? 'gpt-4o' :
                    newProvider.type === 'gigachat' ? 'GigaChat-2-Pro' :
                    'claude-3-5-sonnet-20240620'
                  }
                />
                {#if newProvider.type === 'llamacpp_server'}
                  <p class="help-text">
                    Absolute path to the model file — DPC starts its own llama-server on it
                    (no host, no key). The Ollama blob can be named here directly.
                  </p>
                {/if}
              </div>
            {/if}

            {#if newProvider.type === 'anthropic' || newProvider.type === 'zai' || newProvider.type === 'zai_coding' || newProvider.type === 'deepseek' || newProvider.type === 'gemini' || newProvider.type === 'github_models' || newProvider.type === 'gigachat'}
              <div class="form-group">
                <label for="new-api-key-env">API Key Environment Variable</label>
                <input
                  id="new-api-key-env"
                  type="text"
                  bind:value={newProvider.api_key_env}
                  placeholder={
                    newProvider.type === 'zai' || newProvider.type === 'zai_coding' ? 'ZAI_API_KEY' :
                    newProvider.type === 'deepseek' ? 'DEEPSEEK_API_KEY' :
                    newProvider.type === 'anthropic' ? 'ANTHROPIC_API_KEY' :
                    newProvider.type === 'gemini' ? 'GEMINI_API_KEY' :
                    newProvider.type === 'github_models' ? 'GITHUB_TOKEN' :
                    'GIGACHAT_CREDENTIALS'
                  }
                />
              </div>
            {/if}

            {#if newProvider.type !== 'dpc_agent' && newProvider.type !== 'local_whisper'}
              <div class="form-group">
                <label for="new-context-window-select">Context Window (optional)</label>
                <select
                  id="new-context-window-select"
                  value={newProvider.context_window || ''}
                  on:change={(e) => {
                    const val = (e.target as HTMLSelectElement).value;
                    newProvider.context_window = val === '' ? undefined : parseInt(val);
                  }}
                >
                  <option value="">Auto-detected</option>
                  {#each CONTEXT_WINDOW_PRESETS as preset}
                    <option value={preset.value}>{preset.label}</option>
                  {/each}
                </select>
                <input
                  type="number"
                  bind:value={newProvider.context_window}
                  placeholder="Or enter exact value (tokens)"
                  class="custom-context-input"
                />
              </div>
            {/if}

            {#if newProvider.type === 'ollama'}
              <div class="form-group">
                <label for="new-think">Thinking</label>
                <select
                  id="new-think"
                  value={newProvider.think === undefined ? '' : newProvider.think ? 'on' : 'off'}
                  on:change={(e) => {
                    const val = (e.target as HTMLSelectElement).value;
                    newProvider.think = val === '' ? undefined : val === 'on';
                  }}
                >
                  <option value="off">Always off (default)</option>
                  <option value="on">Always on</option>
                  <option value="">Model default (on if the model can)</option>
                </select>
                <p class="help-text">
                  A new provider starts with reasoning off, because that is the
                  setting that cannot fail: a model which spends its whole output
                  budget thinking answers with nothing. Turn it on where the
                  reasoning is what you came for.
                </p>
              </div>
            {/if}

            {#if newProvider.type === 'zai'}
              <div class="form-group">
                <label for="new-max-tokens">Max Tokens (output)</label>
                <input id="new-max-tokens" type="number" bind:value={newProvider.max_tokens} placeholder="8192" />
              </div>

              <div class="form-group">
                <label for="new-thinking">
                  <input
                    id="new-thinking"
                    type="checkbox"
                    checked={newProvider.thinking?.enabled ?? false}
                    on:change={(e) => {
                      if (!newProvider.thinking) newProvider.thinking = {};
                      newProvider.thinking.enabled = (e.target as HTMLInputElement).checked;
                      newProvider = newProvider;
                    }}
                  />
                  Enable extended thinking
                </label>
              </div>

              {#if newProvider.thinking?.enabled}
                <div class="form-group">
                  <label for="new-thinking-budget">Thinking Budget (tokens)</label>
                  <input
                    id="new-thinking-budget"
                    type="number"
                    value={newProvider.thinking?.budget_tokens ?? ''}
                    on:input={(e) => {
                      if (!newProvider.thinking) newProvider.thinking = {};
                      const v = (e.target as HTMLInputElement).value;
                      newProvider.thinking.budget_tokens = v === '' ? undefined : parseInt(v);
                      newProvider = newProvider;
                    }}
                    placeholder="10000"
                  />
                </div>
              {/if}

              <div class="form-group">
                <label for="new-top-p">Top P (optional)</label>
                <input id="new-top-p" type="number" step="0.05" min="0" max="1" bind:value={newProvider.top_p} placeholder="e.g. 0.9" />
              </div>
            {/if}

            {#if newProvider.type !== 'dpc_agent' && newProvider.type !== 'local_whisper'}
              <div class="form-group">
                <label for="new-temperature">Temperature (optional)</label>
                <!-- The placeholder used to read 0.7 for every provider type, and an
                     example number in an empty field is an invitation: five Ollama
                     aliases on this machine carried a 0.7 nobody had chosen. It now
                     names what silence actually buys for the type being added. -->
                <input id="new-temperature" type="number" step="0.1" min="0" max="2" bind:value={newProvider.temperature} placeholder={temperatureDefaultLabel(newProvider.type)} />
                {#if temperatureIsInert(newProvider)}
                  <p class="help-text warn">Not sent while thinking is enabled — DeepSeek ignores it.</p>
                {/if}
              </div>
            {/if}

            <div class="form-info">
              <p><strong>Note:</strong> All settings can also be changed later in edit mode.</p>
              {#if newProvider.type === 'ollama'}
                <p>Default host will be: http://127.0.0.1:11434</p>
              {:else if newProvider.type === 'openai_compatible'}
                <p>Default base URL will be: https://api.openai.com/v1</p>
                <p>API key will use environment variable: OPENAI_API_KEY</p>
              {:else if newProvider.type === 'anthropic'}
                <p>API key will use environment variable: ANTHROPIC_API_KEY</p>
              {:else if newProvider.type === 'local_whisper'}
                <p>Device: Auto-detect (CUDA if available)</p>
                <p>Model will download on first use (~3GB)</p>
                <p>GPU acceleration recommended for fast transcription</p>
              {:else if newProvider.type === 'gemini'}
                <p>API key env var: GEMINI_API_KEY</p>
                <p>Get a free key at Google AI Studio (aistudio.google.com)</p>
                <p>All Gemini models support vision natively (1M token context)</p>
              {:else if newProvider.type === 'github_models'}
                <p>API key env var: GITHUB_TOKEN (needs models:read permission)</p>
                <p>Endpoint: https://models.inference.ai.azure.com (auto-configured)</p>
                <p>Free tier: 15 RPM / 150 RPD for low-complexity models</p>
              {:else if newProvider.type === 'gigachat'}
                <p>API key env var: GIGACHAT_CREDENTIALS (from developers.sber.ru/studio)</p>
                <p>Scope defaults to GIGACHAT_API_PERS (personal/free tier)</p>
                <p>Install Russian CA cert before use (see edit mode for command)</p>
              {:else if newProvider.type === 'dpc_agent'}
                <p>Embedded autonomous AI agent for task automation</p>
                <p>Uses your configured default AI provider</p>
                <p>No model or API key configuration required</p>
                <p>Optionally configure peer_id for remote inference</p>
              {/if}
            </div>

            <button
              class="btn btn-primary"
              on:click={addNewProvider}
              disabled={!newProvider.alias || (newProvider.type !== 'dpc_agent' && !newProvider.model)}
            >
              Add Provider
            </button>
          </div>
        {/if}
      </div>

      {#if saveMessage}
        <div class="save-message {saveMessageType}">{saveMessage}</div>
      {/if}
    </div>
  </div>
{/if}

<!-- Model Info Modal -->
{#if showModelInfo}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" role="presentation">
    <div class="modal model-info-modal" role="dialog" aria-labelledby="model-info-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="model-info-title">🔍 Model Information</h2>
        <button class="close-btn" on:click={closeModelInfo} aria-label="Close">×</button>
      </div>

      <div class="modal-body">
        {#if modelInfoLoading}
          <div class="loading-state">
            <p>Querying Ollama for model information...</p>
          </div>
        {:else if modelInfoError}
          <div class="error-state">
            <p class="error-text">❌ {modelInfoError}</p>
          </div>
        {:else if modelInfoData}
          <div class="model-info-content">
            <div class="info-section">
              <h3>Context Window</h3>
              {#if modelInfoData.num_ctx}
                <p class="detected-value">
                  <strong>Detected:</strong> {modelInfoData.num_ctx.toLocaleString()} tokens
                </p>
                <button class="btn-use-detected" on:click={() => useDetectedContextWindow(queriedProviderAlias, modelInfoData.num_ctx)}>
                  Use This Value
                </button>
              {:else}
                {@const providerCw = displayConfig?.providers.find(p => p.alias === queriedProviderAlias)?.context_window}
                {#if providerCw}
                  <div class="info-box ok-box">
                    <p class="info-title">✓ Set by provider config</p>
                    <p class="info-text">
                      The modelfile has no <code>num_ctx</code>, but this provider sends
                      <strong>{providerCw.toLocaleString()} tokens</strong> with every request
                      (Context Window setting), which overrides Ollama's default.
                    </p>
                  </div>
                {:else}
                  <div class="info-box warning-box">
                    <p class="info-title">⚠️ No Custom Context Window Detected</p>
                    <p class="info-text">
                      This model doesn't have a custom <code>num_ctx</code> parameter in its modelfile.
                      It will use Ollama's default context size.
                    </p>
                    <p class="info-text">
                      To increase it, use the dropdown in edit mode to select a larger context window.
                    </p>
                  </div>
                {/if}
              {/if}
            </div>

            {#if modelInfoData.details && Object.keys(modelInfoData.details).length > 0}
              <div class="info-section">
                <h3>Model Details</h3>
                <div class="details-grid">
                  {#if modelInfoData.details.family}
                    <p><strong>Family:</strong> {modelInfoData.details.family}</p>
                  {/if}
                  {#if modelInfoData.details.parameter_size}
                    <p><strong>Parameter Size:</strong> {modelInfoData.details.parameter_size}</p>
                  {/if}
                  {#if modelInfoData.details.quantization_level}
                    <p><strong>Quantization:</strong> {modelInfoData.details.quantization_level}</p>
                  {/if}
                  {#if modelInfoData.details.format}
                    <p><strong>Format:</strong> {modelInfoData.details.format}</p>
                  {/if}
                </div>
              </div>
            {/if}

            {#if modelInfoData.modelfile}
              <details class="modelfile-section">
                <summary><h3>Raw Modelfile</h3></summary>
                <pre class="modelfile-content">{modelInfoData.modelfile}</pre>
              </details>
            {/if}

            {#if modelInfoData.parameters}
              <details class="parameters-section">
                <summary><h3>Parameters</h3></summary>
                <pre class="parameters-content">{modelInfoData.parameters}</pre>
              </details>
            {/if}
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  /* The colour was inherited and then dimmed by opacity, which fades the text,
     the tint and the rule together — grey on dark navy, unreadable at 0.85em.
     An explicit foreground and a slightly stronger tint instead: opacity is the
     wrong instrument when only one of the three layers should be quiet. */
  .role-hint {
    margin: 0 0 12px;
    padding: 8px 12px;
    border-left: 3px solid #4a9eff;
    background: rgba(74, 158, 255, 0.12);
    color: #d6e4f5;
    font-size: 0.85em;
    line-height: 1.45;
  }

  .role-hint strong {
    color: #fff;
  }

  /* Account balance card (Phase 2b) — dark theme, matches .provider-card */
  .balance-card {
    margin: 0 0 1rem;
    padding: 0.6rem 0.85rem;
    border: 1px solid #333;
    border-radius: 8px;
    background: #252525;
    color: #fff;
  }
  .balance-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .balance-label { font-weight: 600; color: #fff; }
  .balance-value { margin-top: 0.4rem; font-size: 1.05rem; color: #fff; font-variant-numeric: tabular-nums; }
  .balance-flag { margin-left: 0.5rem; font-size: 0.85rem; color: #bbb; }
  .balance-muted { color: #aaa; font-size: 0.9rem; }
  .balance-err { color: #ef9a9a; }
  .balance-ok { border-left: 4px solid #4caf50; }
  .balance-low { border-left: 4px solid #ffb300; }
  .balance-low .balance-value { color: #ffc107; }
  .balance-critical { border-left: 4px solid #e53935; }
  .balance-critical .balance-value { color: #ff6b6b; }
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
    background: #1e1e1e;
    border-radius: 8px;
    width: 90%;
    max-width: 800px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px;
    border-bottom: 1px solid #333;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.5rem;
    color: #fff;
  }

  .header-actions {
    display: flex;
    gap: 10px;
  }

  .close-btn {
    background: none;
    border: none;
    color: #999;
    font-size: 2rem;
    cursor: pointer;
    padding: 0;
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .close-btn:hover {
    color: #fff;
  }

  .btn {
    padding: 8px 16px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
  }

  .btn-edit {
    background: #007acc;
    color: #fff;
  }

  .btn-edit:hover {
    background: #005a9e;
  }

  .btn-save {
    background: #28a745;
    color: #fff;
  }

  .btn-save:hover:not(:disabled) {
    background: #218838;
  }

  .btn-save:disabled {
    background: #666;
    cursor: not-allowed;
  }

  .btn-cancel {
    background: #6c757d;
    color: #fff;
  }

  .btn-cancel:hover:not(:disabled) {
    background: #5a6268;
  }

  .btn-primary {
    background: #007acc;
    color: #fff;
    padding: 10px 20px;
  }

  .btn-primary:hover:not(:disabled) {
    background: #005a9e;
  }

  .btn-primary:disabled {
    background: #666;
    cursor: not-allowed;
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid #333;
    padding: 0 20px;
  }

  .tab {
    background: none;
    border: none;
    color: #999;
    padding: 12px 20px;
    cursor: pointer;
    font-size: 1rem;
    border-bottom: 2px solid transparent;
  }

  .tab:hover {
    color: #fff;
  }

  .tab.active {
    color: #fff;
    border-bottom-color: #007acc;
  }

  .modal-body {
    padding: 20px;
    overflow-y: auto;
    flex: 1;
  }

  .providers-list {
    display: flex;
    flex-direction: column;
    gap: 15px;
  }

  .provider-card {
    background: #252525;
    border-radius: 6px;
    padding: 15px;
    border: 1px solid #333;
  }

  .provider-card.default {
    border-color: #007acc;
  }

  .provider-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }

  .provider-header h3 {
    margin: 0;
    color: #fff;
    font-size: 1.1rem;
  }

  .default-badge {
    color: #ffd700;
    margin-left: 8px;
  }

  .vision-badge {
    color: #00bcd4;
    margin-left: 8px;
  }

  .voice-badge {
    color: #9C27B0;
    margin-left: 8px;
  }

  .agent-badge {
    color: #4CAF50;
    margin-left: 8px;
  }

  .btn-delete {
    background: #dc3545;
    color: #fff;
    padding: 6px 12px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .btn-delete:hover {
    background: #c82333;
  }

  .btn-set-default {
    background: #007acc;
    color: #fff;
    padding: 8px 16px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    margin-top: 10px;
  }

  .btn-set-default:hover {
    background: #005a9e;
  }

  .provider-form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .provider-info {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .provider-info p {
    margin: 0;
    color: #ccc;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .form-group label {
    color: #fff;
    font-size: 0.9rem;
  }

  .form-label {
    display: block;
    color: #fff;
    font-size: 0.9rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
  }

  .form-group form {
    margin: 0;
    padding: 0;
  }

  .form-group input,
  .form-group select {
    background: #2a2a2a;
    border: 1px solid #444;
    color: #fff;
    padding: 8px;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .form-group select {
    cursor: pointer;
    min-height: 36px;
  }

  .form-group select option {
    background: #2a2a2a;
    color: #fff;
    padding: 8px;
  }

  .form-group input:focus,
  .form-group select:focus {
    outline: none;
    border-color: #007acc;
  }

  /* A path field and its Browse button on one line. The input keeps the
     .form-group styling above; only the layout changes, and min-width: 0 stops
     a long absolute path from pushing the button off the card. */
  .path-row {
    display: flex;
    gap: 6px;
    align-items: stretch;
  }

  .path-row input {
    flex: 1 1 auto;
    min-width: 0;
  }

  .btn-browse {
    flex: 0 0 auto;
    background: #3a3a3a;
    color: #fff;
    border: 1px solid #555;
    white-space: nowrap;
  }

  .btn-browse:hover {
    background: #4a4a4a;
    border-color: #007acc;
  }

  /* The twelve knobs that had no door. Folded away by default because most of
     them are rarely touched, but present — the alternative was hand-editing
     providers.json, and that is how a measured experiment came to be set on the
     wrong field. */
  .supervisor-flags {
    border: 1px solid #333;
    border-radius: 6px;
    padding: 8px 12px;
    background: #202020;
  }

  .supervisor-flags > summary {
    cursor: pointer;
    color: #cfcfcf;
    font-size: 0.9rem;
    padding: 2px 0;
  }

  .supervisor-flags > summary:hover {
    color: #fff;
  }

  .supervisor-flags[open] > summary {
    margin-bottom: 10px;
    border-bottom: 1px solid #333;
    padding-bottom: 8px;
  }

  .supervisor-flags .form-group {
    margin-bottom: 14px;
  }

  .supervisor-flags textarea {
    background: #2a2a2a;
    border: 1px solid #444;
    color: #fff;
    padding: 8px;
    border-radius: 4px;
    font-size: 0.85rem;
    font-family: 'Courier New', monospace;
    resize: vertical;
  }

  .supervisor-flags textarea:focus {
    outline: none;
    border-color: #007acc;
  }

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .radio-group label {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #ccc;
  }

  .help-text {
    margin: 0;
    font-size: 0.85rem;
    color: #999;
  }

  .help-text.warn {
    color: #ffc107;
  }

  /* Same treatment inline code and emphasis already get in .info-text, so a
     flag name reads as a flag name wherever it appears in this form. */
  .help-text code {
    background: rgba(255, 255, 255, 0.1);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
    color: #90caf9;
  }

  .help-text strong {
    color: #fff;
  }

  .help-text.success {
    color: #28a745;
  }

  .input-with-button {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .input-with-button input {
    flex: 1;
  }

  .btn-fetch {
    background: #007acc;
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 1rem;
    min-width: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .btn-fetch:hover:not(:disabled) {
    background: #005a9e;
  }

  .btn-fetch:disabled {
    background: #666;
    cursor: not-allowed;
  }

  .warn {
    color: #ffc107;
  }

  .add-provider-form {
    display: flex;
    flex-direction: column;
    gap: 15px;
    max-width: 500px;
  }

  .add-provider-form h3 {
    margin: 0;
    color: #fff;
  }

  .form-info {
    background: #252525;
    border-radius: 4px;
    padding: 12px;
    border: 1px solid #333;
  }

  .form-info p {
    margin: 0;
    margin-bottom: 8px;
    color: #ccc;
    font-size: 0.9rem;
  }

  .form-info p:last-child {
    margin-bottom: 0;
  }

  .save-message {
    padding: 12px 20px;
    margin: 0 20px 20px;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .save-message.success {
    background: #28a745;
    color: #fff;
  }

  .save-message.error {
    background: #dc3545;
    color: #fff;
    white-space: pre-line;
  }

  /* Query Model Info Button */
  .btn-query-info {
    background: #007bff;
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    margin-top: 10px;
  }

  .btn-query-info:hover {
    background: #0056b3;
  }

  /* Custom Context Window Input */
  .custom-context-input {
    margin-top: 8px;
    width: 100%;
    padding: 8px;
    border: 1px solid #444;
    background: #2a2a2a;
    color: #fff;
    border-radius: 4px;
  }

  .sampling-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 8px;
    margin-top: 6px;
  }
  .sampling-field label {
    display: block;
    font-size: 0.8rem;
    color: #aaa;
    margin-bottom: 3px;
    font-family: monospace;
  }
  .sampling-field input {
    width: 100%;
    padding: 6px 8px;
    border: 1px solid #444;
    background: #2a2a2a;
    color: #fff;
    border-radius: 4px;
  }

  /* Model Info Modal */
  .model-info-modal {
    max-width: 700px;
  }

  .model-info-content {
    padding: 0;
  }

  .info-section {
    padding: 20px;
    border-bottom: 1px solid #333;
  }

  .info-section:last-child {
    border-bottom: none;
  }

  .info-section h3 {
    margin: 0 0 15px 0;
    font-size: 1.1rem;
    color: #fff;
  }

  .detected-value {
    font-size: 1.1rem;
    margin: 10px 0;
    padding: 14px;
    background: rgba(0, 123, 255, 0.1);
    border-radius: 6px;
    border-left: 4px solid #007bff;
    color: #e0e0e0;
    line-height: 1.6;
  }

  .detected-value strong {
    color: #90caf9;
  }

  .btn-use-detected {
    background: #28a745;
    color: #fff;
    border: none;
    padding: 10px 20px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    margin-top: 10px;
  }

  .btn-use-detected:hover {
    background: #218838;
  }

  .details-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px;
  }

  .details-grid p {
    margin: 5px 0;
    padding: 12px;
    background: #2a2a2a;
    border-radius: 4px;
    color: #e0e0e0;
    line-height: 1.5;
  }

  .details-grid p strong {
    color: #90caf9;
  }

  .modelfile-section,
  .parameters-section {
    padding: 15px 20px;
    border-top: 1px solid #333;
  }

  .modelfile-section summary,
  .parameters-section summary {
    cursor: pointer;
    font-weight: bold;
    user-select: none;
    color: #e0e0e0;
  }

  .modelfile-section summary:hover,
  .parameters-section summary:hover {
    color: #90caf9;
  }

  .modelfile-section summary h3,
  .parameters-section summary h3 {
    display: inline;
    font-size: 1rem;
  }

  .modelfile-content,
  .parameters-content {
    background: #1a1a1a;
    padding: 15px;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 0.85rem;
    line-height: 1.6;
    margin-top: 10px;
    white-space: pre-wrap;
    word-wrap: break-word;
    color: #d0d0d0;
    border: 1px solid #333;
  }

  /* Info boxes */
  .info-box {
    padding: 16px;
    border-radius: 6px;
    margin: 10px 0;
    border: 2px solid;
  }

  .warning-box {
    background: rgba(255, 193, 7, 0.1);
    border-color: #ffc107;
  }

  .ok-box {
    background: rgba(76, 175, 80, 0.1);
    border-color: #4caf50;
  }
  .ok-box .info-title {
    color: #4caf50;
  }

  .info-title {
    font-weight: bold;
    font-size: 1rem;
    margin: 0 0 10px 0;
    color: #ffc107;
  }

  .info-text {
    margin: 8px 0;
    line-height: 1.6;
    color: #e0e0e0;
  }

  .info-text code {
    background: rgba(255, 255, 255, 0.1);
    padding: 2px 6px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
    color: #90caf9;
  }

  .info-text strong {
    color: #fff;
  }

  .loading-state,
  .error-state {
    padding: 40px 20px;
    text-align: center;
  }

  .loading-state p {
    color: #999;
    font-size: 1rem;
  }

  .error-text {
    color: #dc3545;
    font-size: 1rem;
  }
</style>
