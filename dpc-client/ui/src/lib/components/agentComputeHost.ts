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
