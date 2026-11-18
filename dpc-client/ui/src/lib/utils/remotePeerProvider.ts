// remotePeerProvider.ts — the pure half of the `remote_peer` form in ProvidersEditor.
//
// A `remote_peer` alias is somebody else's model reached over P2P. The backend
// reads three keys of it (providers/remote_peer_provider.py): `peer_id` (required),
// `model` (what to ask for) and `provider` (the alias on the peer that serves it).
// The form therefore never takes a typed node id: it picks the peer from the
// peers connected now (`get_status` -> `peer_info`), then the model from what that
// peer offers (`query_remote_providers` -> the peer's PROVIDERS_RESPONSE rows).
// Mike's call, 2026-11-18 (option b of REMOTE-PEER-PROVIDER-CANNOT-BE-ADDED-FROM-THE-UI).
//
// No DOM here; this is what the tests exercise.

import { agentHostDisplay, type HostPeer } from '../components/agentComputeHost';
import { classifyProviderType } from '../components/inferenceSharing';
import type { MenuRow } from '../components/peerMenu';

/** The provider type word, as `providers.json` and `llm_manager.PROVIDER_MAP` spell it. */
export const REMOTE_PEER_TYPE = 'remote_peer';

/** The Type dropdown's label for it. "Remote Peer" is the wording the provider
 *  selector's group already uses; the parenthesis is the editor's own habit
 *  (`Strata (local, DPC-supervised)`). */
export const REMOTE_PEER_TYPE_LABEL = 'Remote Peer (another DPC node)';

/** One option of the peer picker. */
export interface PeerChoice {
  node_id: string;
  /** `name | first-20-of-the-id...`, the form the host pickers already use. */
  label: string;
  connected: boolean;
}

/**
 * The peers the picker lists: every connected one, as `peer_info` carries them.
 * A peer already stored on the alias that is not connected now stays on the list,
 * marked, so opening an alias cannot silently drop its peer.
 */
export function peerChoices(
  peers: readonly HostPeer[] | null | undefined,
  storedPeerId?: string | null,
): PeerChoice[] {
  const list = (peers ?? []).filter((p) => p.node_id && p.is_connected !== false);
  const out: PeerChoice[] = list.map((p) => {
    const d = agentHostDisplay(p.node_id, list);
    return { node_id: p.node_id, label: d.label, connected: true };
  });
  if (storedPeerId && !out.some((c) => c.node_id === storedPeerId)) {
    const d = agentHostDisplay(storedPeerId, list);
    out.push({ node_id: storedPeerId, label: d.label, connected: false });
  }
  return out;
}

/**
 * The peer's name alone (no id), for the suggested alias `<model> <peer name>`:
 * the name `peer_info` carries, else the first 20 characters of the node id
 * (what the host pickers show without the dots). '' for no id.
 */
export function peerDisplayName(
  peers: readonly HostPeer[] | null | undefined,
  peerId: string | null | undefined,
): string {
  const id = (peerId ?? '').trim();
  if (!id) return '';
  const peer = (peers ?? []).find((p) => p.node_id === id);
  return (peer?.display_name || peer?.name || '').trim() || id.slice(0, 20);
}

/** One option of the model picker: a row the peer offers. */
export interface PeerModelChoice {
  /** The alias on the peer: what is saved as `provider`. */
  alias: string;
  /** What is saved as `model`; '' when the row names none. */
  model: string;
  label: string;
  row: MenuRow;
}

/**
 * The rows of a peer's menu that a `remote_peer` alias may point at. A row whose
 * type is somebody else's model (`remote_peer`, `dpc_agent`) is dropped: the host
 * never serves one onward (ADR-041 D7), and a stale or hostile menu must not make
 * this node chain through it. A transcription row is dropped too: this provider
 * sends text and pictures, never audio.
 */
export function peerModelChoices(rows: readonly MenuRow[] | null | undefined): PeerModelChoice[] {
  const out: PeerModelChoice[] = [];
  for (const row of rows ?? []) {
    if (!row || !row.alias) continue;
    const kind = classifyProviderType(row.type);
    if (kind === 'never' || kind === 'transcription') continue;
    const model = (row.model ?? '').trim();
    out.push({
      alias: row.alias,
      model,
      label: model && model !== row.alias ? `${model} (${row.alias})` : row.alias,
      row,
    });
  }
  return out;
}

/** What the picker area says instead of a list. */
export type RemotePeerModelState =
  | { kind: 'no-peers'; text: string }
  | { kind: 'no-peer-picked'; text: string }
  | { kind: 'peer-offline'; text: string }
  | { kind: 'loading'; text: string }
  | { kind: 'error'; text: string }
  | { kind: 'empty'; text: string }
  | { kind: 'ready'; text: '' };

export const NO_PEERS_TEXT = 'No peer is connected now. Connect to a node first, then add the provider.';
export const PEER_OFFLINE_TEXT = 'This peer is not connected now, so what it offers cannot be listed.';
export const PEER_OFFERS_NOTHING_TEXT =
  'This peer offers no model to this node. Its owner shares one under Inference Sharing.';

/**
 * Which of the six states the model picker is in. `peers` is `peerChoices` (the
 * connected list, plus a stored peer that is gone); `fetch` is the last
 * `query_remote_providers` for the picked peer, or undefined before one ran.
 */
export function remotePeerModelState(
  peers: readonly PeerChoice[],
  peerId: string | null | undefined,
  fetch: { loading: boolean; error: string } | undefined,
  choices: readonly PeerModelChoice[],
): RemotePeerModelState {
  const picked = peers.find((p) => p.node_id === peerId);
  if (!peerId) {
    return peers.some((p) => p.connected)
      ? { kind: 'no-peer-picked', text: 'Pick a peer to see what it offers.' }
      : { kind: 'no-peers', text: NO_PEERS_TEXT };
  }
  if (picked && !picked.connected) return { kind: 'peer-offline', text: PEER_OFFLINE_TEXT };
  if (fetch?.loading) return { kind: 'loading', text: 'Asking the peer what it offers…' };
  if (choices.length > 0) return { kind: 'ready', text: '' };
  if (fetch?.error) return { kind: 'error', text: fetch.error };
  if (fetch) return { kind: 'empty', text: PEER_OFFERS_NOTHING_TEXT };
  return { kind: 'loading', text: 'Asking the peer what it offers…' };
}

/** The keys a `remote_peer` provider carries because of the two pickers. */
export interface RemotePeerFields {
  peer_id: string;
  provider: string;
  model?: string;
}

/** The fields written when a peer and one of its rows are picked. `model` is left
 *  out when the row names none; the backend does not require it. */
export function remotePeerFields(peerId: string, choice: Pick<PeerModelChoice, 'alias' | 'model'>): RemotePeerFields {
  const out: RemotePeerFields = { peer_id: peerId, provider: choice.alias };
  if (choice.model) out.model = choice.model;
  return out;
}
