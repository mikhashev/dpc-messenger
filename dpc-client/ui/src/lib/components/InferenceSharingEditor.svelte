<!-- InferenceSharingEditor.svelte -->
<!-- The Inference Sharing section of the firewall dialog: what this node
     serves, who may call, who calls for free, at what tariff, what was spent,
     the IDE door on this machine (5) and what one named peer is sent (6).
     Blocks (5) and (6) are read from the backend, not edited: the door is
     config.ini's and the menu is the firewall's answer about a peer.
     Same contract as AgentPermissionsPanel: a display object, an edit object
     mutated in place so the parent's save posts it as is, and editMode. -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { providersList, nodeStatus, sendCommand, firewallRulesUpdated } from '$lib/coreService';
  import {
    addAllowed,
    addAllowedModel,
    addServing,
    addTariffEntry,
    callerPriceBadge,
    clientLabel,
    computeBlockErrors,
    computeErrorsOf,
    doorAddress,
    foldServingAlias,
    gatewayVerdict,
    isFree,
    isIso4217,
    ISO_4217_CODES,
    knownGroups,
    knownNodes,
    maskedHeader,
    menuVerdict,
    offeredProviders,
    removeAllowed,
    removeAllowedModel,
    removeServing,
    removeTariffEntry,
    SERVES_NO_LOCAL_ALIAS,
    setCurrency,
    setFree,
    setVendorQuota,
    tariffAliases,
    tariffEntryErrors,
    tariffHistory,
    tariffState,
    unmatchedModels,
    utcToday,
    validationDraft,
    type CallerKind,
    type ClientLinesResult,
    type ComputeRules,
    type GatewayState,
    type PeerMenuResult,
    type ServingList,
    type TariffEntry,
  } from './inferenceSharing';
  import { priceLine } from './peerMenu';
  import InferenceUsage from './InferenceUsage.svelte';

  export let displayCompute: ComputeRules | null = null;
  export let editCompute: ComputeRules | null = null;
  export let editMode: boolean = false;
  /** The whole draft the parent would post to `save_firewall_rules`
   *  (`editedRules`), handed down so Validate can check exactly that object.
   *  Null when the component is used standalone, with no such draft to hand. */
  export let draftRules: Record<string, unknown> | null = null;
  /** `node_groups` of the rules being shown — the Node Groups tab's data. */
  export let nodeGroups: Record<string, unknown> | null = null;
  /** Keys of `nodes` — every peer with a per-node rule. */
  export let nodeRuleIds: string[] = [];
  /** The reasons the last save was refused with, if any; the compute ones are shown here. */
  export let saveErrors: string[] = [];

  // --- Folding on first open -------------------------------------------
  // The edit copy is normalised once per edit session (the parent makes a
  // fresh copy on every Edit), in place, so the object the parent saves is
  // the object edited here.
  let folded: ComputeRules | null = null;
  $: if (editMode && editCompute && folded !== editCompute) {
    Object.assign(editCompute, foldServingAlias(editCompute));
    folded = editCompute;
    editCompute = editCompute;
  }
  $: if (!editMode) folded = null;

  // What is rendered: the edit copy while editing, else a folded view of the
  // saved rules (so a file still carrying serving_alias reads the same way).
  $: view = editMode && editCompute ? editCompute : displayCompute ? foldServingAlias(displayCompute) : null;

  function apply(next: ComputeRules) {
    if (!editCompute || next === editCompute) return;
    Object.assign(editCompute, next);
    editCompute = editCompute;
  }

  // --- (1) What I share ------------------------------------------------
  $: offered = offeredProviders($providersList || []);
  $: providerByAlias = new Map(($providersList || []).map((p) => [p.alias, p]));
  let pickLocal = '';
  let pickVendor = '';

  function addPicked(list: ServingList) {
    const alias = list === 'local' ? pickLocal : pickVendor;
    if (!editCompute || !alias) return;
    apply(addServing(editCompute, list, alias));
    if (list === 'local') pickLocal = ''; else pickVendor = '';
  }

  function quotaInput(alias: string, raw: string) {
    if (!editCompute) return;
    const text = raw.trim();
    apply(setVendorQuota(editCompute, alias, text.length === 0 ? null : Number(text)));
  }

  // --- (1b) Which models the door accepts --------------------------------
  // The chips come from the same registry the alias pickers read; one model
  // may be carried by several aliases, so the option names them all.
  $: modelChoices = [...($providersList || []).reduce((byModel, p) => {
    if (p.model) byModel.set(p.model, [...(byModel.get(p.model) ?? []), p.alias]);
    return byModel;
  }, new Map<string, string[]>())]
    .filter(([model]) => !(view?.allowed_models ?? []).includes(model))
    .sort((a, b) => a[0].localeCompare(b[0]));
  $: unmatched = unmatchedModels(view, $providersList || []);
  let pickModel = '';
  let typingModel = false;
  let typedModel = '';

  function addModel(text: string) {
    if (!editCompute || !text.trim()) return;
    apply(addAllowedModel(editCompute, text));
  }

  function confirmTypedModel() {
    const model = typedModel.trim();
    if (!model) return;
    addModel(model);
    typedModel = '';
    typingModel = false;
  }

  // --- (2)+(3) Who may call, and who calls for free ----------------------
  $: peerInfo = $nodeStatus?.peer_info ?? ($nodeStatus?.p2p_peers ?? []).map((id) => ({ node_id: id, is_connected: true }));
  $: nodeChoices = knownNodes(peerInfo, nodeGroups, nodeRuleIds, view).filter((n) => !(view?.allow_nodes ?? []).includes(n.node_id));
  $: groupChoices = knownGroups(nodeGroups, view).filter((g) => !(view?.allow_groups ?? []).includes(g));
  $: nodeLabel = new Map(knownNodes(peerInfo, nodeGroups, nodeRuleIds, view).map((n) => [n.node_id, n]));
  // The same names the Usage block shows a peer under: one source for the tab.
  $: usageNodes = [...nodeLabel.values()];
  let pickNode = '';
  let pickGroup = '';
  let typingNode = false;
  let typedNode = '';

  function addCaller(kind: CallerKind, id: string) {
    if (!editCompute || !id.trim()) return;
    apply(addAllowed(editCompute, kind, id));
  }

  function confirmTypedNode() {
    const id = typedNode.trim();
    if (!id) return;
    addCaller('nodes', id);
    typedNode = '';
    typingNode = false;
  }

  // --- (4) Tariff ------------------------------------------------------
  $: today = utcToday();
  $: aliasesToPrice = view ? tariffAliases(view) : [];
  // Lines appended in this edit session, by alias: the only ones that may be
  // taken back. An older line is a declaration already made.
  let addedThisSession: Record<string, Set<string>> = {};
  $: if (!editMode) addedThisSession = {};
  let addingFor: string | null = null;
  let newFrom = '';
  let newIn = '';
  let newOut = '';
  let newEntryErrors: string[] = [];

  function startEntry(alias: string) {
    addingFor = alias;
    newFrom = today;
    newIn = '';
    newOut = '';
    newEntryErrors = [];
  }

  function confirmEntry() {
    if (!editCompute || !addingFor) return;
    const entry: TariffEntry = { from: newFrom, in: newIn.trim() === '' ? NaN : Number(newIn), out: newOut.trim() === '' ? NaN : Number(newOut) };
    newEntryErrors = tariffEntryErrors(editCompute.serving_tariff?.[addingFor], entry);
    if (newEntryErrors.length > 0) return;
    apply(addTariffEntry(editCompute, addingFor, entry));
    (addedThisSession[addingFor] ??= new Set()).add(entry.from);
    addedThisSession = addedThisSession;
    addingFor = null;
  }

  function takeBack(alias: string, from: string) {
    if (!editCompute || !addedThisSession[alias]?.has(from)) return;
    apply(removeTariffEntry(editCompute, alias, from));
    addedThisSession[alias].delete(from);
    addedThisSession = addedThisSession;
  }

  function currencyInput(raw: string) {
    if (!editCompute) return;
    apply(setCurrency(editCompute, raw));
  }

  // --- What the validator would say ------------------------------------
  $: preCheck = editMode && editCompute ? computeBlockErrors(editCompute) : [];
  $: refusedWith = computeErrorsOf(saveErrors);

  function rate(entry: TariffEntry, currency: string | null | undefined): string {
    return `${entry.in} in / ${entry.out} out ${currency ?? ''} per 1M tokens`;
  }

  // --- Talking to the local service --------------------------------------
  interface Reply { status?: string; message?: string }
  interface Answer<T> { value: T | null; error: string | null }

  async function ask<T>(
    command: string,
    payload: Record<string, unknown> = {},
  ): Promise<Answer<T & Reply>> {
    const sent = sendCommand(command, payload);
    if (sent === false) return { value: null, error: 'Not connected to the local service.' };
    try {
      const response = (await (sent as Promise<T & Reply>)) ?? null;
      if (!response) return { value: null, error: 'The local service answered nothing.' };
      if (response.status === 'error') return { value: null, error: response.message || 'The local service refused.' };
      return { value: response, error: null };
    } catch (e) {
      return { value: null, error: e instanceof Error ? e.message : String(e) };
    }
  }

  let copied: string | null = null;
  let copyError: string | null = null;

  async function copy(text: string, what: string) {
    copyError = null;
    try {
      await navigator.clipboard.writeText(text);
      copied = what;
      setTimeout(() => { if (copied === what) copied = null; }, 2000);
    } catch (e) {
      copied = null;
      copyError = `${what} could not be copied: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  // --- (5) The IDE door ---------------------------------------------------
  let gateway: GatewayState | null = null;
  let gatewayError: string | null = null;
  let clientLines: ClientLinesResult | null = null;
  let confirmingRotation = false;
  let rotating = false;
  let rotateError: string | null = null;
  /** The clear key, from the one answer that carries it; dropped on the next
   *  read of the door, so it is on screen for this rotation only. */
  let newKey: string | null = null;

  // The firewall in memory is what serves a call; the saved file's flag is
  // what the tab can say before the door has answered.
  $: doorVerdict = gatewayVerdict(gateway, gateway ? !!gateway.compute_enabled : !!displayCompute?.enabled);

  async function loadDoor() {
    const state = await ask<GatewayState>('get_gateway_state');
    gateway = state.value;
    gatewayError = state.error;
    const lines = await ask<ClientLinesResult>('get_gateway_client_lines');
    clientLines = lines.value;
  }

  async function rotate() {
    rotating = true;
    rotateError = null;
    const answer = await ask<{ key?: string; key_masked?: string }>('rotate_gateway_key');
    rotating = false;
    confirmingRotation = false;
    if (answer.error) { rotateError = answer.error; return; }
    newKey = answer.value?.key ?? null;
    await loadDoor();
  }

  // --- (6) What a peer sees ------------------------------------------------
  let peerPick = '';
  let menu: PeerMenuResult | null = null;
  let menuError: string | null = null;
  let menuLoading = false;
  // Plain, not reactive: the reload below must not depend on the pick, and a
  // slow answer about one peer must not overwrite a fast answer about another.
  let peerNow = '';
  let asked = 0;

  $: menuSeen = menuVerdict(menu);

  async function loadMenu(peerId: string) {
    const mine = ++asked;
    peerNow = peerId;
    if (!peerId) { menu = null; menuError = null; menuLoading = false; return; }
    menuLoading = true;
    menuError = null;
    const answer = await ask<PeerMenuResult>('get_peer_provider_menu', { peer_id: peerId });
    if (mine !== asked) return;
    menuLoading = false;
    menu = answer.value;
    menuError = answer.error;
  }

  // --- Validate without saving ---------------------------------------------
  let validating = false;
  let validation: { valid: boolean; errors: string[] } | null = null;
  let validationError: string | null = null;
  $: if (!editMode) { validation = null; validationError = null; }

  async function validateDraft() {
    if (!editCompute) return;
    validating = true;
    validation = null;
    validationError = null;
    let rules: Record<string, unknown>;
    if (draftRules) {
      rules = validationDraft(draftRules, null, null, null);
    } else {
      const saved = await ask<{ rules?: Record<string, unknown> }>('get_firewall_rules');
      if (saved.error) { validating = false; validationError = saved.error; return; }
      rules = validationDraft(null, saved.value?.rules ?? null, editCompute, nodeGroups);
    }
    const answer = await ask<{ valid?: boolean; errors?: string[] }>('validate_firewall_rules', { rules });
    validating = false;
    if (answer.error) { validationError = answer.error; return; }
    validation = { valid: !!answer.value?.valid, errors: answer.value?.errors ?? [] };
  }

  // A save rewrites the door's serving lists and every peer's menu; the guard
  // keeps the store's first value, which arrives before any save, from asking.
  let seenRules = false;
  $: onRulesSaved($firewallRulesUpdated);

  function onRulesSaved(rules: unknown) {
    if (!seenRules) { seenRules = true; return; }
    if (!rules) return;
    void loadDoor();
    if (peerNow) void loadMenu(peerNow);
  }

  onMount(() => { void loadDoor(); });
</script>

<div class="section">
  <h3>Inference Sharing (Remote Inference)</h3>
  <p class="help-text">
    What this node serves to peers, who may call it, who calls for free, and what a call costs.
    Every choice below is picked from data the application already holds.
  </p>

  <!-- The two doors in one sentence: the P2P one these rules govern, and the
       IDE one on this machine, which config.ini governs and a restart reads. -->
  <div class="verdict verdict-{doorVerdict.tone}" role="status">{doorVerdict.text}</div>
  {#if gatewayError}
    <div class="refusal" role="alert">The IDE door could not be read: {gatewayError}</div>
  {/if}

  {#if view}
    <div class="compute-settings">
      <div class="setting-item">
        <label>
          {#if editMode && editCompute}
            <input id="compute-enabled" name="compute-enabled" type="checkbox" bind:checked={editCompute.enabled} />
          {:else}
            <input id="compute-enabled-display" name="compute-enabled-display" type="checkbox" checked={view.enabled} disabled />
          {/if}
          <strong>Share my models with peers</strong>
        </label>
        <p class="help-text-small">
          Using a peer's model does not need this. Serving one does: peers' prompts arrive on
          this machine in plaintext, and this application shows, stores and logs none of them
          (ADR-041 D7, amendment 2026-09-14).
        </p>
      </div>

      {#if refusedWith.length > 0}
        <div class="refusal" role="alert">
          <strong>The last save was refused:</strong>
          <ul>{#each refusedWith as line}<li>{line}</li>{/each}</ul>
        </div>
      {/if}
      {#if preCheck.length > 0}
        <div class="precheck">
          <strong>This will be refused as it stands:</strong>
          <ul>{#each preCheck as line}<li>{line}</li>{/each}</ul>
        </div>
      {/if}

      {#if editMode && editCompute}
        <div class="inline-input-row validate-row">
          <button class="btn-small" disabled={validating} on:click={validateDraft}>
            {validating ? 'Validating…' : 'Validate'}
          </button>
          <span class="muted">
            Asks the backend's own validator and saves nothing. It checks the same rules Save would
            write, including any unsaved edit on another tab of this dialog.
          </span>
        </div>
        {#if validationError}
          <div class="refusal" role="alert">The rules could not be validated: {validationError}</div>
        {:else if validation?.valid}
          <div class="valid" role="status">Valid — the backend would accept these rules.</div>
        {:else if validation}
          <div class="refusal" role="alert">
            <strong>The backend would refuse these rules:</strong>
            <ul>{#each validation.errors as line}<li>{line}</li>{/each}</ul>
          </div>
        {/if}
      {/if}

      {#if view.enabled}
        <!-- (1) What I share -->
        <div class="subsection">
          <h4>1. What I share</h4>
          <p class="help-text-small">
            Two lists, by what an alias spends. A <strong>local</strong> alias (Ollama, llama.cpp) spends this
            card; the first one is what peers are served from over P2P. A <strong>vendor</strong> alias spends
            money and needs a daily ceiling per caller, in USD. A remote peer's model and an agent are never
            offered: what is shared is not shared onward (ADR-041 D7). Whisper is shared under Transcription Sharing.
          </p>

          <h5>Local (spends this card)</h5>
          <div class="rule-list">
            {#each view.serving_local ?? [] as alias, index (alias)}
              <div class="rule-row">
                <span class="alias-cell">
                  <code class="rule-path">{alias}</code>
                  {#if index === 0}<span class="badge badge-first">served over P2P</span>{/if}
                  {#if providerByAlias.has(alias)}
                    <span class="muted">({providerByAlias.get(alias)?.model})</span>
                  {:else}
                    <span class="badge badge-missing">not in providers.json</span>
                  {/if}
                </span>
                {#if editMode && editCompute}
                  <button class="btn-icon-small" title="Stop serving {alias}" on:click={() => editCompute && apply(removeServing(editCompute, 'local', alias))}>×</button>
                {/if}
              </div>
            {:else}
              <p class="empty-small">No local alias shared &mdash; peer inference is refused.</p>
            {/each}
          </div>
          {#if editMode}
            <div class="inline-input-row">
              <select id="compute-pick-local" name="compute-pick-local" class="inline-input" bind:value={pickLocal}>
                <option value="">&mdash; add a local alias &mdash;</option>
                {#each offered.local.filter((p) => !(view?.serving_local ?? []).includes(p.alias)) as p (p.alias)}
                  <option value={p.alias}>{p.alias} ({p.model})</option>
                {/each}
              </select>
              <button class="btn-small" disabled={!pickLocal} on:click={() => addPicked('local')}>Add</button>
            </div>
          {/if}

          <h5>Vendor (spends money; ceiling per caller, USD per day)</h5>
          <div class="rule-list">
            {#each view.serving_vendor ?? [] as alias (alias)}
              <div class="rule-row">
                <span class="alias-cell">
                  <code class="rule-path">{alias}</code>
                  {#if providerByAlias.has(alias)}
                    <span class="muted">({providerByAlias.get(alias)?.model})</span>
                  {:else}
                    <span class="badge badge-missing">not in providers.json</span>
                  {/if}
                </span>
                <span class="quota-cell">
                  {#if editMode && editCompute}
                    <label class="muted" for="compute-quota-{alias}">USD/day</label>
                    <input
                      id="compute-quota-{alias}"
                      name="compute-quota-{alias}"
                      class="inline-input quota-input"
                      type="number"
                      min="0"
                      step="0.01"
                      placeholder="required"
                      value={view.vendor_quotas?.[alias] ?? ''}
                      on:input={(e) => quotaInput(alias, e.currentTarget.value)}
                    />
                    <button class="btn-icon-small" title="Stop serving {alias}" on:click={() => editCompute && apply(removeServing(editCompute, 'vendor', alias))}>×</button>
                  {:else if typeof view.vendor_quotas?.[alias] === 'number'}
                    <span class="badge badge-quota">{view.vendor_quotas?.[alias]} USD/day per caller</span>
                  {:else}
                    <span class="badge badge-missing">no ceiling &mdash; refused</span>
                  {/if}
                </span>
              </div>
            {:else}
              <p class="empty-small">No vendor alias shared.</p>
            {/each}
          </div>
          {#if editMode}
            <div class="inline-input-row">
              <select id="compute-pick-vendor" name="compute-pick-vendor" class="inline-input" bind:value={pickVendor}>
                <option value="">&mdash; add a vendor alias &mdash;</option>
                {#each offered.vendor.filter((p) => !(view?.serving_vendor ?? []).includes(p.alias)) as p (p.alias)}
                  <option value={p.alias}>{p.alias} ({p.model}, {p.type})</option>
                {/each}
              </select>
              <button class="btn-small" disabled={!pickVendor} on:click={() => addPicked('vendor')}>Add</button>
            </div>
          {/if}

          <h5>1b. Models this door will accept</h5>
          <p class="help-text-small">
            A filter on what a caller may ask for, not a third list of what is served.
            <strong>An empty list accepts every model</strong> &mdash; the opposite of the two lists above,
            where an empty list serves nobody. Name one model and only that one may be asked for, on any
            alias shared above.
          </p>
          <div class="rule-list">
            {#each view.allowed_models ?? [] as model (model)}
              <div class="rule-row">
                <span class="alias-cell">
                  <code class="rule-path">{model}</code>
                  {#if unmatched.includes(model)}
                    <span class="badge badge-missing">matches no configured provider</span>
                  {/if}
                </span>
                {#if editMode && editCompute}
                  <button class="btn-icon-small" title="Stop accepting {model}" on:click={() => editCompute && apply(removeAllowedModel(editCompute, model))}>&times;</button>
                {/if}
              </div>
            {:else}
              <p class="empty-small">No model named &mdash; every model this node serves is accepted.</p>
            {/each}
          </div>
          {#if editMode}
            {#if typingModel}
              <div class="inline-input-row">
                <input
                  type="text"
                  class="inline-input"
                  name="compute-typed-model"
                  bind:value={typedModel}
                  placeholder="a model id (several: one per line, or comma-separated)"
                  on:keydown={(e) => { if (e.key === 'Enter') confirmTypedModel(); if (e.key === 'Escape') { typingModel = false; typedModel = ''; } }}
                />
                <button class="btn-small" on:click={confirmTypedModel}>Add</button>
                <button class="btn-small btn-cancel" on:click={() => { typingModel = false; typedModel = ''; }}>Cancel</button>
              </div>
            {:else}
              <div class="inline-input-row">
                <select id="compute-pick-model" name="compute-pick-model" class="inline-input" bind:value={pickModel}>
                  <option value="">&mdash; accept one named model &mdash;</option>
                  {#each modelChoices as [model, aliases] (model)}
                    <option value={model}>{model} ({aliases.join(', ')})</option>
                  {/each}
                </select>
                <button class="btn-small" disabled={!pickModel} on:click={() => { addModel(pickModel); pickModel = ''; }}>Add</button>
                <button class="btn-small btn-cancel" title="A model no configured provider carries yet" on:click={() => (typingModel = true)}>Type a model id</button>
              </div>
            {/if}
          {/if}
        </div>

        <!-- (2)+(3) Who may call, and who calls for free -->
        <div class="subsection">
          <h4>2. Who may call &mdash; and who calls for free</h4>
          <p class="help-text-small">
            Peers and groups admitted to the aliases above. <strong>Free</strong> marks an admitted caller who
            gets the tariff at zero; it distinguishes, it does not admit &mdash; removing a caller removes the
            mark with it (ADR-041 D3). Groups are the ones on the Node Groups tab.
            {#if !view.currency}
              <em>No tariff is declared, so today every call is a gift whatever the mark says.</em>
            {/if}
          </p>

          <h5>Nodes</h5>
          <div class="rule-list">
            {#each view.allow_nodes as nodeId (nodeId)}
              <div class="rule-row">
                <span class="alias-cell">
                  <code class="rule-path">{nodeId}</code>
                  {#if nodeLabel.get(nodeId)?.label && nodeLabel.get(nodeId)?.label !== nodeId}
                    <span class="muted">{nodeLabel.get(nodeId)?.label}</span>
                  {/if}
                  {#if nodeLabel.get(nodeId)?.connected}<span class="badge badge-live">connected</span>{/if}
                </span>
                <span class="quota-cell">
                  {#if editMode && editCompute}
                    <label class="free-toggle">
                      <input
                        type="checkbox"
                        name="compute-free-node-{nodeId}"
                        checked={isFree(view, 'nodes', nodeId)}
                        on:change={(e) => editCompute && apply(setFree(editCompute, 'nodes', nodeId, e.currentTarget.checked))}
                      />
                      free
                    </label>
                    <button class="btn-icon-small" title="Remove {nodeId}" on:click={() => editCompute && apply(removeAllowed(editCompute, 'nodes', nodeId))}>×</button>
                  {:else}
                    <span class="action-badge" class:allow={isFree(view, 'nodes', nodeId)} class:tariff={!isFree(view, 'nodes', nodeId)}>
                      {callerPriceBadge(isFree(view, 'nodes', nodeId), view.currency)}
                    </span>
                  {/if}
                </span>
              </div>
            {:else}
              <p class="empty-small">No specific nodes allowed.</p>
            {/each}
          </div>
          {#if editMode}
            {#if typingNode}
              <div class="inline-input-row">
                <input
                  type="text"
                  class="inline-input"
                  name="compute-typed-node"
                  bind:value={typedNode}
                  placeholder="dpc-node-... (several: one per line, or comma-separated)"
                  on:keydown={(e) => { if (e.key === 'Enter') confirmTypedNode(); if (e.key === 'Escape') { typingNode = false; typedNode = ''; } }}
                />
                <button class="btn-small" on:click={confirmTypedNode}>Add</button>
                <button class="btn-small btn-cancel" on:click={() => { typingNode = false; typedNode = ''; }}>Cancel</button>
              </div>
            {:else}
              <div class="inline-input-row">
                <select id="compute-pick-node" name="compute-pick-node" class="inline-input" bind:value={pickNode}>
                  <option value="">&mdash; add a known peer &mdash;</option>
                  {#each nodeChoices as n (n.node_id)}
                    <option value={n.node_id}>{n.label}{n.label !== n.node_id ? ` — ${n.node_id}` : ''}{n.connected ? ' (connected)' : ''}</option>
                  {/each}
                </select>
                <button class="btn-small" disabled={!pickNode} on:click={() => { addCaller('nodes', pickNode); pickNode = ''; }}>Add</button>
                <button class="btn-small btn-cancel" title="A peer this node has not met yet" on:click={() => (typingNode = true)}>Type an id</button>
              </div>
            {/if}
          {/if}

          <h5>Groups</h5>
          <div class="rule-list">
            {#each view.allow_groups as groupName (groupName)}
              <div class="rule-row">
                <span class="alias-cell">
                  <code class="rule-path">{groupName}</code>
                  {#if !(nodeGroups && groupName in nodeGroups)}
                    <span class="badge badge-missing">not on the Node Groups tab</span>
                  {/if}
                </span>
                <span class="quota-cell">
                  {#if editMode && editCompute}
                    <label class="free-toggle">
                      <input
                        type="checkbox"
                        name="compute-free-group-{groupName}"
                        checked={isFree(view, 'groups', groupName)}
                        on:change={(e) => editCompute && apply(setFree(editCompute, 'groups', groupName, e.currentTarget.checked))}
                      />
                      free
                    </label>
                    <button class="btn-icon-small" title="Remove {groupName}" on:click={() => editCompute && apply(removeAllowed(editCompute, 'groups', groupName))}>×</button>
                  {:else}
                    <span class="action-badge" class:allow={isFree(view, 'groups', groupName)} class:tariff={!isFree(view, 'groups', groupName)}>
                      {callerPriceBadge(isFree(view, 'groups', groupName), view.currency)}
                    </span>
                  {/if}
                </span>
              </div>
            {:else}
              <p class="empty-small">No groups allowed.</p>
            {/each}
          </div>
          {#if editMode}
            <div class="inline-input-row">
              <select id="compute-pick-group" name="compute-pick-group" class="inline-input" bind:value={pickGroup}>
                <option value="">&mdash; add a group &mdash;</option>
                {#each groupChoices as g (g)}
                  <option value={g}>{g}</option>
                {/each}
              </select>
              <button class="btn-small" disabled={!pickGroup} on:click={() => { addCaller('groups', pickGroup); pickGroup = ''; }}>Add</button>
            </div>
          {/if}
        </div>

        <!-- (4) Tariff -->
        <div class="subsection">
          <h4>3. Tariff</h4>
          <p class="help-text-small">
            Rates per 1M tokens in and out, in this node's currency, dated per alias. The newest line on or
            before the day of the call applies (UTC). Three states: <em>not declared</em> &mdash; the call is a
            gift; <em>declared 0</em> &mdash; free by decision; above zero &mdash; paid. A line once written is
            not edited: add a new one from a later date.
          </p>

          <div class="rule-row currency-row">
            <span class="alias-cell"><strong>Currency</strong> <span class="muted">(ISO 4217)</span></span>
            <span class="quota-cell">
              {#if editMode && editCompute}
                <input
                  id="compute-currency"
                  name="compute-currency"
                  class="inline-input currency-input"
                  list="compute-iso4217"
                  maxlength="3"
                  placeholder="none"
                  value={view.currency ?? ''}
                  on:change={(e) => currencyInput(e.currentTarget.value)}
                />
                <datalist id="compute-iso4217">
                  {#each ISO_4217_CODES as code}<option value={code}></option>{/each}
                </datalist>
                {#if view.currency && !isIso4217(view.currency)}
                  <span class="badge badge-missing">not an ISO 4217 code</span>
                {/if}
              {:else if view.currency}
                <span class="badge badge-quota">{view.currency}</span>
              {:else}
                <span class="badge badge-gift">none &mdash; no tariff declared</span>
              {/if}
            </span>
          </div>

          {#each aliasesToPrice as { alias, served } (alias)}
            {@const state = tariffState(view.serving_tariff?.[alias], view.currency, today)}
            <div class="peer-card tariff-card">
              <div class="group-header">
                <h5>
                  {alias}
                  {#if !served}<span class="badge badge-missing">priced, not served</span>{/if}
                </h5>
                {#if state.kind === 'gift'}
                  <span class="action-badge gift">
                    {#if state.reason === 'no-currency'}gift &mdash; no currency
                    {:else if state.reason === 'no-entry'}gift &mdash; nothing declared
                    {:else}gift until {state.upcoming[state.upcoming.length - 1]?.from}{/if}
                  </span>
                {:else if state.kind === 'free'}
                  <span class="action-badge allow">free by decision since {state.entry.from}</span>
                {:else}
                  <span class="action-badge tariff">{rate(state.entry, view.currency)} since {state.entry.from}</span>
                {/if}
              </div>

              <div class="rule-list">
                {#each tariffHistory(view.serving_tariff?.[alias]) as entry (entry.from)}
                  <div class="rule-row history-row" class:current={state.kind !== 'gift' && state.entry.from === entry.from} class:upcoming={entry.from > today}>
                    <span class="alias-cell">
                      <code class="rule-path">from {entry.from}</code>
                      <span class="muted">{rate(entry, view.currency)}</span>
                      {#if entry.from > today}<span class="badge badge-first">upcoming</span>{/if}
                      {#if entry.in === 0 && entry.out === 0}<span class="badge badge-gift">0 &mdash; free by decision</span>{/if}
                    </span>
                    {#if editMode && addedThisSession[alias]?.has(entry.from)}
                      <button class="btn-icon-small" title="Take back the line added just now" on:click={() => takeBack(alias, entry.from)}>×</button>
                    {/if}
                  </div>
                {:else}
                  <p class="empty-small">No line declared for {alias} &mdash; every call on it is a gift.</p>
                {/each}
              </div>

              {#if editMode}
                {#if addingFor === alias}
                  <div class="inline-input-row entry-row">
                    <label class="muted" for="compute-tariff-from-{alias}">from</label>
                    <input id="compute-tariff-from-{alias}" name="compute-tariff-from-{alias}" class="inline-input" type="date" bind:value={newFrom} />
                    <label class="muted" for="compute-tariff-in-{alias}">in</label>
                    <input id="compute-tariff-in-{alias}" name="compute-tariff-in-{alias}" class="inline-input rate-input" type="number" min="0" step="0.01" placeholder="per 1M" bind:value={newIn} />
                    <label class="muted" for="compute-tariff-out-{alias}">out</label>
                    <input id="compute-tariff-out-{alias}" name="compute-tariff-out-{alias}" class="inline-input rate-input" type="number" min="0" step="0.01" placeholder="per 1M" bind:value={newOut}
                      on:keydown={(e) => { if (e.key === 'Enter') confirmEntry(); if (e.key === 'Escape') addingFor = null; }} />
                    <button class="btn-small" on:click={confirmEntry}>Add line</button>
                    <button class="btn-small btn-cancel" on:click={() => (addingFor = null)}>Cancel</button>
                  </div>
                  {#if newEntryErrors.length > 0}
                    <ul class="entry-errors">{#each newEntryErrors as line}<li>{line}</li>{/each}</ul>
                  {/if}
                {:else}
                  <button class="btn-small" style="margin-top: 0.5rem;" on:click={() => startEntry(alias)}>+ New dated line</button>
                {/if}
              {/if}
            </div>
          {:else}
            <p class="empty-small">Nothing to price until an alias is shared above.</p>
          {/each}
        </div>
      {/if}

      <!-- Usage: the ledger these rules produced, read by role. Outside the
           enabled gate on purpose — a node that serves nobody still consumes
           from peers and still burns its own key. -->
      <InferenceUsage nodes={usageNodes} />

      <!-- (5) The IDE door. Outside the enabled gate as well: this door is
           config.ini's, not these rules', and it serves this machine even
           where no peer is served. -->
      <div class="subsection">
        <h4>5. IDE door</h4>
        <p class="help-text-small">
          An OpenAI-compatible listener for the clients on this machine &mdash; Continue, Cursor,
          Claude Code, curl. It is switched, bound and ported by <code>[gateway]</code> in
          <code>config.ini</code>, which is read once at start, so there is no switch here: what a
          restart would change is shown, not offered. The serving lists close this node's own
          aliases only: a call to <code>remote:&lt;peer&gt;:&lt;alias&gt;</code> is carried to the
          peer that serves it whatever those lists hold.
        </p>

        <div class="rule-list">
          <div class="rule-row">
            <span class="alias-cell"><strong>Address</strong></span>
            <span class="quota-cell"><code class="rule-path">http://{doorAddress(gateway)}</code></span>
          </div>
          <div class="rule-row">
            <span class="alias-cell"><strong>Configured</strong> <span class="muted">[gateway] enabled</span></span>
            <span class="quota-cell">
              {#if gateway?.enabled}<span class="badge badge-quota">true</span>
              {:else}<span class="badge badge-missing">false</span>{/if}
            </span>
          </div>
          <div class="rule-row">
            <span class="alias-cell"><strong>Listening</strong> <span class="muted">right now</span></span>
            <span class="quota-cell">
              {#if gateway?.running}<span class="badge badge-live">a listener holds the port</span>
              {:else}<span class="badge badge-missing">nothing is listening</span>{/if}
            </span>
          </div>
          <div class="rule-row">
            <span class="alias-cell">
              <strong>Key</strong>
              {#if gateway?.key_file}<span class="muted">{gateway.key_file}</span>{/if}
            </span>
            <span class="quota-cell">
              {#if gateway?.key_masked}<code class="rule-path">{gateway.key_masked}</code>
              {:else}<span class="badge badge-missing">none written yet</span>{/if}
            </span>
          </div>
          <div class="rule-row">
            <span class="alias-cell"><strong>Serves</strong> <span class="muted">the same two lists as block 1 &mdash; this node's own aliases</span></span>
            <span class="quota-cell">
              {#if gateway?.serving_error}
                <span class="badge badge-missing">the lists were refused</span>
              {:else if (gateway?.serving_local?.length ?? 0) + (gateway?.serving_vendor?.length ?? 0) > 0}
                {#each [...(gateway?.serving_local ?? []), ...(gateway?.serving_vendor ?? [])] as alias (alias)}
                  <code class="rule-path">{alias}</code>
                {/each}
              {:else}
                <span class="badge badge-missing">{SERVES_NO_LOCAL_ALIAS}</span>
              {/if}
            </span>
          </div>
        </div>

        <div class="inline-input-row">
          {#if confirmingRotation}
            <span class="warn-text">
              Rotate now? Every IDE on this machine still holding the old key gets 401 from its next
              request, and the old key cannot be brought back.
            </span>
            <button class="btn-small btn-danger" disabled={rotating} on:click={rotate}>
              {rotating ? 'Rotating…' : 'Rotate now'}
            </button>
            <button class="btn-small btn-cancel" disabled={rotating} on:click={() => (confirmingRotation = false)}>Cancel</button>
          {:else}
            <button class="btn-small" on:click={() => { confirmingRotation = true; newKey = null; }}>Rotate key</button>
            <span class="muted">Immediate and not part of Save: the file is rewritten when you confirm.</span>
          {/if}
        </div>

        {#if rotateError}
          <div class="refusal" role="alert">The key was not rotated: {rotateError}</div>
        {/if}

        {#if newKey}
          <div class="new-key">
            <strong>The new key, shown once:</strong>
            <code class="key-clear">{newKey}</code>
            <button class="btn-small" on:click={() => newKey && copy(newKey, 'The new key')}>Copy</button>
            <button class="btn-small btn-cancel" on:click={() => (newKey = null)}>Hide</button>
            <p class="help-text-small">
              It is on disk in the key file above, and the blocks below now carry it; this box is the
              one place it is spelled out here.
            </p>
          </div>
        {/if}

        <details class="client-lines">
          <summary>Client configuration &mdash; {maskedHeader(clientLines)}</summary>
          <p class="help-text-small">
            Paste-ready, with the key in clear: these go into another tool's configuration file.
          </p>
          {#each clientLines?.lines ?? [] as line (line.client)}
            <div class="client-block">
              <div class="group-header">
                <h5>{clientLabel(line.client)}</h5>
                <button class="btn-small" on:click={() => copy(line.text, clientLabel(line.client))}>Copy</button>
              </div>
              <pre>{line.text}</pre>
            </div>
          {:else}
            <p class="empty-small">Nothing to paste: the door has written no key yet.</p>
          {/each}
        </details>

        {#if copied}<p class="help-text-small">{copied} copied to the clipboard.</p>{/if}
        {#if copyError}<div class="refusal" role="alert">{copyError}</div>{/if}
      </div>

      <!-- (6) What a peer sees. Read-only, and built by the one selection
           that answers GET_PROVIDERS, so the preview and the wire cannot
           differ without one function differing from itself. -->
      <div class="subsection">
        <h4>6. What a peer sees</h4>
        <p class="help-text-small">
          The menu rows one named peer is sent &mdash; the same selection that answers its
          <code>GET_PROVIDERS</code>, so this is the exact row set it receives on connect and on
          every save of these rules. Nothing here is editable, and asking changes nothing.
        </p>

        <div class="inline-input-row">
          <select id="compute-preview-peer" name="compute-preview-peer" class="inline-input" bind:value={peerPick} on:change={() => loadMenu(peerPick)}>
            <option value="">&mdash; pick a known peer &mdash;</option>
            {#each usageNodes as n (n.node_id)}
              <option value={n.node_id}>{n.label}{n.label !== n.node_id ? ` — ${n.node_id}` : ''}{n.connected ? ' (connected)' : ''}</option>
            {/each}
          </select>
          <button class="btn-small" disabled={!peerPick || menuLoading} on:click={() => loadMenu(peerPick)}>Refresh</button>
          {#if menuLoading}<span class="muted">Asking the firewall&hellip;</span>{/if}
        </div>

        {#if menuError}
          <div class="refusal" role="alert">The menu could not be read: {menuError}</div>
        {/if}

        {#if menu}
          <div class="peer-card">
            <div class="group-header">
              <h5>
                {nodeLabel.get(menu.peer_id ?? '')?.label ?? menu.peer_id}
                {#if menu.connected}<span class="badge badge-live">connected</span>
                {:else if menu.known}<span class="badge badge-first">known, not connected</span>
                {:else}<span class="badge badge-missing">this node has not met it</span>{/if}
              </h5>
              <span class="action-badge" class:allow={menuSeen.kind === 'served'} class:gift={menuSeen.kind === 'empty'} class:tariff={menuSeen.kind === 'refused'}>
                {menuSeen.kind === 'served' ? 'served' : menuSeen.kind === 'empty' ? 'allowed, empty' : 'refused'}
              </span>
            </div>

            <p class="verdict-line">{menuSeen.text}</p>
            {#if menuSeen.detail}<p class="help-text-small">{menuSeen.detail}</p>{/if}

            <div class="rule-list">
              {#each menuSeen.rows as row (row.alias)}
                <div class="rule-row">
                  <span class="alias-cell">
                    <code class="rule-path">{row.alias}</code>
                    <span class="muted">{row.type ?? 'type not stated'}</span>
                    {#if row.supports_vision}<span class="badge badge-first">vision</span>{/if}
                    {#if row.supports_voice}<span class="badge badge-first">voice</span>{/if}
                    {#if row.supports_tools}<span class="badge badge-first">tools</span>{/if}
                  </span>
                  <span class="numbers-cell muted">
                    <span class="money">{priceLine(row.tariff).short}</span>
                    <span>ctx {row.context_window ?? 'not stated'}</span>
                    <span>effort {row.reasoning_default ?? 'not stated'}</span>
                  </span>
                </div>
              {/each}
            </div>
          </div>
        {/if}
      </div>
    </div>
  {:else}
    <p class="empty">Inference sharing not configured.</p>
  {/if}
</div>

<style>
  /* Scoped copies of the parent's classes: a child component does not see
     FirewallEditor's styles, and the section must read as one dialog. */
  .section h3 { margin: 0 0 0.5rem 0; font-size: 1.2rem; color: #333; }
  .help-text { color: #666; font-size: 0.9rem; margin: 0 0 1rem 0; }
  .help-text-small { color: #666; font-size: 0.85rem; margin: 0.25rem 0 0.5rem 0; }
  .compute-settings { display: flex; flex-direction: column; gap: 1rem; }
  .setting-item { display: flex; align-items: center; gap: 0.5rem; }
  .subsection { margin-top: 1rem; padding-left: 1rem; border-left: 3px solid #e0e0e0; }
  .subsection h4 { margin: 0 0 0.5rem 0; font-size: 1rem; color: #555; }
  .subsection h5 { margin: 0.75rem 0 0.25rem; font-size: 0.85rem; color: #555; }
  .peer-card { border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; background: #fafafa; }
  .peer-card h5 { margin: 0; color: #333; font-family: monospace; display: flex; gap: 0.5rem; align-items: center; }
  .group-header { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .rule-list { display: flex; flex-direction: column; gap: 0.5rem; }
  .rule-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; padding: 0.5rem; background: white; border-radius: 4px; flex-wrap: wrap; }
  .rule-path { font-size: 0.85rem; color: #555; }
  .empty { color: #999; font-style: italic; text-align: center; padding: 2rem; }
  .empty-small { color: #999; font-style: italic; font-size: 0.9rem; margin: 0.25rem 0; }
  .action-badge { padding: 0.25rem 0.75rem; border-radius: 12px; font-size: 0.85rem; font-weight: 500; }
  .action-badge.allow { background: #d4edda; color: #155724; }
  .action-badge.tariff { background: #e3f2fd; color: #0d47a1; }
  .action-badge.gift { background: #f3e5f5; color: #4a148c; }
  .btn-small { padding: 0.25rem 0.75rem; font-size: 0.85rem; background: #2196F3; color: white; border: none; border-radius: 4px; cursor: pointer; }
  .btn-small:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-small.btn-cancel { background: #757575; }
  .inline-input-row { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.5rem; flex-wrap: wrap; }
  .inline-input { flex: 1; min-width: 8rem; padding: 0.25rem 0.5rem; font-size: 0.85rem; border: 1px solid #555; border-radius: 4px; background: #2a2a2a; color: #e0e0e0; }
  .btn-icon-small { background: none; border: none; cursor: pointer; font-size: 1.5rem; color: #999; line-height: 1; }
  .btn-icon-small:hover { color: #f44336; }

  /* This section's own. */
  .alias-cell { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .quota-cell { display: flex; align-items: center; gap: 0.5rem; }
  .quota-input { flex: 0 0 7rem; min-width: 7rem; }
  .currency-input { flex: 0 0 5rem; min-width: 5rem; text-transform: uppercase; }
  .rate-input { flex: 0 0 6rem; min-width: 6rem; }
  .muted { color: #777; font-size: 0.85rem; }
  .badge { padding: 0.1rem 0.5rem; border-radius: 10px; font-size: 0.75rem; white-space: nowrap; }
  .badge-first { background: #e3f2fd; color: #0d47a1; }
  .badge-missing { background: #fff3cd; color: #856404; }
  .badge-quota { background: #e8f5e9; color: #1b5e20; }
  .badge-gift { background: #f3e5f5; color: #4a148c; }
  .badge-live { background: #d4edda; color: #155724; }
  .free-toggle { display: flex; align-items: center; gap: 0.25rem; font-size: 0.85rem; color: #333; cursor: pointer; }
  .currency-row { margin-bottom: 0.75rem; }
  .tariff-card { margin-top: 0.5rem; }
  .history-row.current { border-left: 3px solid #1976d2; }
  .history-row.upcoming { opacity: 0.8; }
  .entry-row label { flex: 0 0 auto; }
  .entry-errors, .refusal ul, .precheck ul { margin: 0.25rem 0 0 1rem; padding: 0; font-size: 0.85rem; }
  .refusal { background: #f8d7da; color: #721c24; border-left: 3px solid #dc3545; padding: 0.75rem; font-size: 0.9rem; white-space: pre-wrap; }
  .valid { background: #d4edda; color: #155724; border-left: 3px solid #28a745; padding: 0.75rem; font-size: 0.9rem; }

  /* The verdict at the top, and the two blocks the backend fills. */
  .verdict { padding: 0.75rem; border-radius: 4px; font-size: 0.95rem; margin-bottom: 0.75rem; border-left: 3px solid #999; background: #f5f5f5; color: #333; }
  .verdict-shared { background: #d4edda; color: #155724; border-left-color: #28a745; }
  .verdict-partial { background: #e3f2fd; color: #0d47a1; border-left-color: #1976d2; }
  .verdict-off { background: #f5f5f5; color: #555; border-left-color: #9e9e9e; }
  .verdict-error { background: #f8d7da; color: #721c24; border-left-color: #dc3545; }
  .verdict-line { font-size: 0.9rem; color: #333; margin: 0 0 0.5rem 0; }
  .validate-row { align-items: flex-start; }
  .btn-small.btn-danger { background: #dc3545; }
  .warn-text { flex: 1 1 18rem; font-size: 0.85rem; color: #721c24; }
  .new-key { background: #fff3cd; border-left: 3px solid #ffc107; padding: 0.75rem; margin-top: 0.5rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .key-clear { font-family: monospace; font-size: 0.9rem; word-break: break-all; background: white; padding: 0.25rem 0.5rem; border-radius: 4px; }
  .new-key .help-text-small { flex: 1 1 100%; margin: 0; }
  .client-lines { margin-top: 0.75rem; }
  .client-lines summary { cursor: pointer; font-size: 0.9rem; color: #555; }
  .client-block { margin-top: 0.5rem; }
  .client-block pre { background: #2a2a2a; color: #e0e0e0; padding: 0.5rem; border-radius: 4px; font-size: 0.8rem; overflow-x: auto; white-space: pre; margin: 0.25rem 0 0 0; }
  .numbers-cell { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; justify-content: flex-end; }
  .numbers-cell .money { color: #0d47a1; font-weight: 500; }
  .precheck { background: #fff3cd; color: #856404; border-left: 3px solid #ffc107; padding: 0.75rem; font-size: 0.9rem; }
  .entry-errors { color: #721c24; list-style: disc; }
</style>
