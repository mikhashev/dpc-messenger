// Which node an agent chat's text model runs on, as the header shows it.
//
// The AI Host dropdown binds one page-global value, which is set only when an
// agent chat is created from the management panel. The truth for an existing
// agent chat is its own `compute_host` (ChatPanel's send path reads it from the
// chat meta), so an agent chat's header states that instead of offering a choice.

export type HostPeer = {
  node_id: string;
  name?: string;
  display_name?: string;
  is_connected?: boolean;
};

export type HostDisplay = {
  /** "local" or the peer's node_id: the host the requests actually go to. */
  host: string;
  /** Text for the header. */
  label: string;
  /** False for a stored peer that is not among the connected ones. */
  connected: boolean;
  /** Short name for other labels (the vision line). Empty for local. */
  shortName: string;
};

export function agentHostDisplay(
  computeHost: string | undefined,
  peers: HostPeer[] | undefined | null
): HostDisplay {
  if (!computeHost || computeHost === "local") {
    return { host: "local", label: "Local", connected: true, shortName: "" };
  }
  const short = `${computeHost.slice(0, 20)}...`;
  const peer = (peers || []).find((p) => p.node_id === computeHost);
  const name = peer?.display_name || peer?.name || "";
  const connected = !!peer && peer.is_connected !== false;
  const base = name ? `${name} | ${short}` : short;
  return {
    host: computeHost,
    label: connected ? base : `${base} (not connected)`,
    connected,
    shortName: name || short
  };
}

/** What the configuration dialog and the agents list say of a model this node does not hold. */
export const NOT_AVAILABLE_NOW = "not available now";

/**
 * The text of a dropdown option for a model the agent has configured but the
 * lists do not carry now: the alias, where it lives, and why it is not offered.
 * A peer's alias is not in any list while that peer is offline, so without this
 * the select shows nothing and the stored value looks lost.
 */
export function heldModelLabel(
  alias: string,
  computeHost: string | undefined,
  peers: HostPeer[] | undefined | null
): string {
  const d = agentHostDisplay(computeHost, peers);
  if (d.host === "local") return `${alias} (${NOT_AVAILABLE_NOW})`;
  return `${alias} @ ${d.shortName} (${d.connected ? "not offered by it now" : "offline"})`;
}

// --- A deleted provider, as the agents list shows it ---------------------------------
// `list_agents` rows carry `missing_aliases` (role names whose alias is absent from
// this node's providers; empty for an agent that runs on a peer) and `alias_roles`
// ({role: alias} for every agent that runs its models here).

export type AgentAliasRow = {
  name?: string;
  provider_alias?: string;
  missing_aliases?: string[];
  alias_roles?: Record<string, string>;
};

const ROLE_ORDER = ["main", "sleep", "snapshot", "compaction"];

function byRole(a: string, b: string): number {
  const ia = ROLE_ORDER.indexOf(a);
  const ib = ROLE_ORDER.indexOf(b);
  return (ia < 0 ? ROLE_ORDER.length : ia) - (ib < 0 ? ROLE_ORDER.length : ib);
}

/**
 * The warning badge of an agent whose main model is not held here, or null.
 * Only the main role gets a badge; the title names every missing role.
 */
export function missingModelBadge(
  agent: AgentAliasRow
): { text: string; title: string } | null {
  const missing = agent.missing_aliases ?? [];
  if (!missing.includes("main")) return null;
  const alias = agent.alias_roles?.main || agent.provider_alias || "";
  const roles = [...missing].sort(byRole).join(", ");
  return {
    text: `⚠ ${NOT_AVAILABLE_NOW}`,
    title:
      `${alias ? heldModelLabel(alias, undefined, []) : `main model (${NOT_AVAILABLE_NOW})`}. ` +
      `Roles without a model: ${roles}. Click to choose another model.`
  };
}

/** Agents that name `alias`, each with the roles that do, as the delete confirmation prints them. */
export function agentsNamingAlias(
  agents: AgentAliasRow[] | undefined | null,
  alias: string
): { name: string; roles: string[] }[] {
  const found: { name: string; roles: string[] }[] = [];
  for (const agent of agents || []) {
    const roles = Object.entries(agent.alias_roles || {})
      .filter(([, a]) => a === alias)
      .map(([role]) => role)
      .sort(byRole);
    if (roles.length) found.push({ name: agent.name || "(unnamed agent)", roles });
  }
  return found;
}

/** "Delete provider 'X'? Used by: Kotler (main, sleep, snapshot); Muse (main)" */
export function deleteProviderPrompt(
  alias: string,
  agents: AgentAliasRow[] | undefined | null
): string {
  const users = agentsNamingAlias(agents, alias);
  const base = `Delete provider '${alias}'?`;
  if (!users.length) return base;
  return `${base} Used by: ${users.map((u) => `${u.name} (${u.roles.join(", ")})`).join("; ")}`;
}
