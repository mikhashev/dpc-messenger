// Where a provider's model runs, as a chat's model list says it.
//
// Every row of this node's providers list used to read "- local", so a cloud
// vendor alias (deepseek_flash, a NeuralDeep alias) looked like a model on this
// machine: "local" meant the compute host, the node that makes the call, not
// where the model runs (THE-CHAT-MODEL-DROPDOWN-CALLS-CLOUD-MODELS-LOCAL-...).
// The backend says the place on each row (`route`, `provider_kind`,
// `host_scope`, `base_url_host`, the words of the agent configuration dialog);
// this module turns them into the short tag of an option. A row from a backend
// that says nothing falls back to the provider type.

import { modelRoute, type RouteRow } from '../components/agentModelRoute';
import { vendorName } from '../components/peerMenu';

/** A provider row as any list carries it; a field a peer leaves out may be null. */
export type PlaceRow = {
  [K in keyof RouteRow]?: RouteRow[K] | null;
} & { alias: string; model?: string | null };

function routeRow(row: PlaceRow): RouteRow {
  const out: RouteRow = { alias: row.alias };
  for (const key of ['type', 'is_remote', 'peer_id', 'route', 'provider_kind', 'host_scope', 'base_url_host'] as const) {
    const value = row[key];
    if (value !== null && value !== undefined) (out as any)[key] = value;
  }
  return out;
}

// The provider types that run on this node's own hardware, and the vendor APIs:
// firewall.py LOCAL_PROVIDER_TYPES / VENDOR_PROVIDER_TYPES, for rows that carry
// no place of their own. openai_compatible is either, so it is left unknown.
const SELF_HOSTED_TYPES = new Set(['ollama', 'llamacpp_server', 'strata', 'local_whisper']);
const VENDOR_TYPES = new Set([
  'anthropic', 'zai', 'deepseek', 'neuraldeep', 'gemini', 'github_models', 'gigachat',
]);

function vendorWord(type: string | null | undefined): string {
  return vendorName(type).replace(/^an? /, '');
}

/**
 * The place tag of one row: "local model", "cloud: DeepSeek", "your network",
 * "via peer", "remote" (a peer's own row), or "place unknown".
 */
export function providerPlace(row: PlaceRow, source: 'local' | 'remote' = 'local'): string {
  if (source === 'remote') return 'remote';
  if (!row.route && !row.provider_kind) {
    if (row.type && SELF_HOSTED_TYPES.has(row.type)) return 'local model';
    if (row.type && VENDOR_TYPES.has(row.type)) return `cloud: ${vendorWord(row.type)}`;
    if (row.type === 'remote_peer' || row.type === 'dpc_agent') return 'via peer';
    return 'place unknown';
  }
  const route = modelRoute(routeRow(row), '', []);
  switch (route?.where) {
    case 'local':
      return 'local model';
    case 'lan':
      return 'your network';
    case 'vendor':
      return `cloud: ${vendorWord(row.type)}`;
    case 'peer':
      return 'via peer';
    default:
      return route?.tag === 'cloud?' && row.base_url_host
        ? `cloud? ${row.base_url_host}`
        : 'place unknown';
  }
}

/** The option text of a row: alias, model and place. */
export function providerOptionText(
  row: PlaceRow,
  source: 'local' | 'remote' = 'local',
  separator: string = ' - ',
): string {
  const model = row.model ? ` (${row.model})` : '';
  return `${row.alias}${model}${separator}${providerPlace(row, source)}`;
}
