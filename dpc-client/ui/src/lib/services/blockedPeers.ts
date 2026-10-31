// src/lib/services/blockedPeers.ts
// The block list: one store, and the two commands that change it.
//
// The list lives in privacy_rules.json (`blocked_nodes`). The store is derived from
// the existing `firewallRulesUpdated` store (every save, block and unblock broadcasts
// the rules through it), with the first `get_firewall_rules` load as the starting value.

import { derived, writable } from 'svelte/store';
import { firewallRulesUpdated } from './providers';
import { blockedIdsFromRules } from '$lib/utils/blockedPeers';

type SendCommandFn = (command: string, payload?: any) => Promise<any> | boolean;

// What the last `get_firewall_rules` said; null until a load has answered.
const loadedBlockedIds = writable<string[] | null>(null);

/** Node ids this node has blocked. Empty until the first load answers. */
export const blockedNodeIds = derived(
  [loadedBlockedIds, firewallRulesUpdated],
  ([loaded, event]) => blockedIdsFromRules(event) ?? loaded ?? [],
);

let loadState: 'idle' | 'loading' | 'done' = 'idle';

/** Forget that the list was loaded, so the next connection loads it again. */
export function resetBlockedNodesLoad(): void {
  loadState = 'idle';
}

/**
 * Read the block list once per connection. `force` re-reads it regardless; the event
 * handler uses that when a broadcast carries no `blocked_nodes` of its own.
 */
export async function loadBlockedNodes(sendCmd: SendCommandFn, force = false): Promise<void> {
  if (!force && loadState !== 'idle') return;
  loadState = 'loading';
  try {
    const result = await sendCmd('get_firewall_rules', {});
    if (result && result.status === 'success') {
      loadedBlockedIds.set(blockedIdsFromRules(result.rules) ?? []);
      loadState = 'done';
      return;
    }
  } catch (e) {
    console.error('Failed to load the block list:', e);
  }
  loadState = 'idle';
}

export async function blockPeer(nodeId: string, sendCmd: SendCommandFn): Promise<any> {
  return sendCmd('block_peer', { node_id: nodeId });
}

export async function unblockPeer(nodeId: string, sendCmd: SendCommandFn): Promise<any> {
  return sendCmd('unblock_peer', { node_id: nodeId });
}
