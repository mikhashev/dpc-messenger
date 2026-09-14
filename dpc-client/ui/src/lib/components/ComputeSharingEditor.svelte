<!-- ComputeSharingEditor.svelte -->
<!-- The Compute Sharing section of the firewall dialog: what this node
     serves, who may call, who calls for free, and at what tariff. Blocks
     (1)-(4) of the board entry THE-COMPUTE-SHARING-TAB-DESIGNATES-ONE-ALIAS…;
     the IDE door (5) and the guest preview (6) are not here yet.
     Same contract as AgentPermissionsPanel: a display object, an edit object
     mutated in place so the parent's save posts it as is, and editMode. -->

<script lang="ts">
  import { providersList, nodeStatus } from '$lib/coreService';
  import {
    addAllowed,
    addServing,
    addTariffEntry,
    computeBlockErrors,
    computeErrorsOf,
    foldServingAlias,
    isFree,
    isIso4217,
    ISO_4217_CODES,
    knownGroups,
    knownNodes,
    offeredProviders,
    removeAllowed,
    removeServing,
    removeTariffEntry,
    setCurrency,
    setFree,
    setVendorQuota,
    tariffAliases,
    tariffEntryErrors,
    tariffHistory,
    tariffState,
    utcToday,
    type CallerKind,
    type ComputeRules,
    type ServingList,
    type TariffEntry,
  } from './computeSharing';

  export let displayCompute: ComputeRules | null = null;
  export let editCompute: ComputeRules | null = null;
  export let editMode: boolean = false;
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

  // --- (2)+(3) Who may call, and who calls for free ----------------------
  $: peerInfo = $nodeStatus?.peer_info ?? ($nodeStatus?.p2p_peers ?? []).map((id) => ({ node_id: id, is_connected: true }));
  $: nodeChoices = knownNodes(peerInfo, nodeGroups, nodeRuleIds, view).filter((n) => !(view?.allow_nodes ?? []).includes(n.node_id));
  $: groupChoices = knownGroups(nodeGroups, view).filter((g) => !(view?.allow_groups ?? []).includes(g));
  $: nodeLabel = new Map(knownNodes(peerInfo, nodeGroups, nodeRuleIds, view).map((n) => [n.node_id, n]));
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
</script>

<div class="section">
  <h3>Compute Sharing (Remote Inference)</h3>
  <p class="help-text">
    What this node serves to peers, who may call it, who calls for free, and what a call costs.
    Every choice below is picked from data the application already holds.
  </p>

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
        <p class="help-text-small">Using a peer's model does not need this.</p>
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
                      {isFree(view, 'nodes', nodeId) ? 'free' : 'at tariff'}
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
                      {isFree(view, 'groups', groupName) ? 'free' : 'at tariff'}
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
    </div>
  {:else}
    <p class="empty">Compute sharing not configured.</p>
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
  .precheck { background: #fff3cd; color: #856404; border-left: 3px solid #ffc107; padding: 0.75rem; font-size: 0.9rem; }
  .entry-errors { color: #721c24; list-style: disc; }
</style>
