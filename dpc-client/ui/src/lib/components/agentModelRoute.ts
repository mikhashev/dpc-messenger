// Where the calls of a model chosen in the agent configuration dialog go.
//
// The backend decides, from the provider's type and base_url and never from its
// alias: each row of the dialog's providers list carries `route` (`local` or
// `peer`) and `provider_kind` (`self_hosted`, `vendor`, `unknown`), the words of
// the agent's runtime budget. This module only turns them into the words on the
// screen. An agent pinned to a node (`compute_host`) sends every call there, so
// that outranks the row.

import { agentHostDisplay, type HostPeer } from './agentComputeHost';
import { vendorName } from './peerMenu';

export type RouteRow = {
  alias: string;
  type?: string;
  is_remote?: boolean;
  peer_id?: string;
  route?: string;
  provider_kind?: string;
  /** Where the URL host is, verbatim from the backend: this_machine, lan or unknown. Absent when it says nothing. */
  host_scope?: string;
  /** Host of a generic-type URL that names no vendor (`openai_compatible`); absent otherwise. */
  base_url_host?: string;
};

export type ModelRoute = {
  /**
   * Where the prompt goes: this machine, a vendor, another node, or not known.
   * Display only: billing and any redaction read `route`, `provider_kind` and `host_scope` in the backend.
   */
  where: 'local' | 'lan' | 'vendor' | 'peer' | 'unknown';
  /** Short word for an option of the list. Empty when nothing is known. */
  tag: string;
  /** The sentence shown under the select. */
  line: string;
};

/** What the screen says of a model whose place is not known. */
export const UNKNOWN_ROUTE_LINE =
  "Unknown: this node cannot tell where this model runs. Check the provider's settings before sending private data to it.";

const LOCAL_ROUTE: ModelRoute = {
  where: 'local',
  tag: 'local',
  line: 'Local: runs on this machine, your prompt is not sent to anyone else.'
};

export function modelRoute(
  row: RouteRow | null | undefined,
  computeHost: string | undefined,
  peers: HostPeer[] | undefined | null
): ModelRoute | null {
  const host = agentHostDisplay(computeHost, peers);
  const onPeer = host.host !== 'local';
  // A model no list carries says nothing about where it would run.
  if (!row && !onPeer) return null;

  const kind = row?.provider_kind;
  if (onPeer || row?.route === 'peer') {
    // A row of the node the agent is pinned to, or of any peer, has its host's own word on the kind.
    const peerId = onPeer ? host.host : row?.peer_id;
    const name = peerId ? agentHostDisplay(peerId, peers).shortName : '';
    const sameNode = !onPeer || (row?.is_remote === true && row.peer_id === host.host);
    const rowKind = sameNode ? kind : undefined;
    let line = `${name ? `Peer node ${name}` : 'Another node'}: your prompt is sent to that node, not kept on this machine.`;
    if (rowKind === 'vendor') {
      line += ` It is a vendor model there, so the prompt goes on to ${vendorName(row?.type)}.`;
    } else if (rowKind === 'self_hosted') {
      line += " It runs on that node's own hardware.";
    }
    return { where: 'peer', tag: 'peer node', line };
  }
  const scope = row?.route === 'local' ? row.host_scope : undefined;
  if (scope === 'unknown') return { where: 'unknown', tag: '', line: UNKNOWN_ROUTE_LINE };
  if (scope === 'lan') {
    return {
      where: 'lan',
      tag: 'your network',
      line: 'Your network: runs on a machine in your local network; this node cannot see what that machine does with the prompt.'
    };
  }
  if (scope === 'this_machine') return LOCAL_ROUTE;
  if (row?.route === 'local' && kind === 'self_hosted') {
    return LOCAL_ROUTE;
  }
  if (row?.route === 'local' && kind === 'vendor' && row.base_url_host) {
    // A generic type with a typed URL: a vendor and an office server look alike from here.
    return {
      where: 'unknown',
      tag: 'cloud?',
      line: `Cloud?: your prompt is sent to ${row.base_url_host}; this node cannot tell whether a vendor or a machine of yours answers there.`
    };
  }
  if (row?.route === 'local' && kind === 'vendor') {
    return {
      where: 'vendor',
      tag: 'cloud',
      line: `Cloud: vendor model, your prompt goes to ${vendorName(row.type)} and is not kept on this machine.`
    };
  }
  return { where: 'unknown', tag: '', line: UNKNOWN_ROUTE_LINE };
}

/** An option's text with its place appended, when the place is known. */
export function optionWithRoute(label: string, route: ModelRoute | null): string {
  return route && route.tag ? `${label} · ${route.tag}` : label;
}

/**
 * The line at the top of the dialog for an agent pinned to another node, or null
 * for one that runs here. Every role goes there, whichever model a select shows.
 */
export function computeHostNote(
  computeHost: string | undefined,
  peers: HostPeer[] | undefined | null
): string | null {
  const host = agentHostDisplay(computeHost, peers);
  if (host.host === 'local') return null;
  return `This agent runs on ${host.shortName}: every model call below goes to that node, whichever model is chosen, and its prompts are sent there.`;
}

/**
 * The route of the alias a role has chosen, looked up in the dialog's rows.
 * Null when no alias is set; an alias no row carries (a deleted provider) is unknown.
 */
export function routeForAlias(
  alias: string | undefined,
  rows: RouteRow[],
  computeHost: string | undefined,
  peers: HostPeer[] | undefined | null
): ModelRoute | null {
  if (!alias) return null;
  return modelRoute(rows.find((p) => p.alias === alias) ?? { alias }, computeHost, peers);
}

/** The one line shown for the compaction model while compaction is off. */
export function compactionOffLine(route: ModelRoute | null): string | null {
  if (!route) return null;
  return `Compaction is off. If it is turned on, its summaries go to: ${route.line}`;
}
