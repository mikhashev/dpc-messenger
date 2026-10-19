// Which model an agent chat actually shows a pasted image to.
//
// Mike's rule, 2026-09-18: if the model the agent runs supports vision, the
// image goes to it; otherwise to the global vision_provider. The backend
// decides on its own (llm_adapter, tools/vision.py) and ignores the header's
// Vision dropdown for agent chats, so the header states the outcome instead of
// offering a choice nothing reads.

export type VisionRow = { alias: string; model?: string; supports_vision?: boolean };

export type EffectiveVisionModel = {
  /** Alias that will see the image. */
  alias: string;
  /** Label for the header: the alias, plus "(global)" when it is not the agent's own. */
  label: string;
  /** True when the global vision_provider handles it. */
  isGlobal: boolean;
};

/**
 * @param computeHost the chat's `compute_host` (a peer's node_id; empty or
 *   "local" when the agent's text model is on this node).
 * @param peerRows the `peerProviders` store: node_id -> that peer's
 *   PROVIDERS_RESPONSE rows.
 * @param hostName display name for the host, for the label only.
 *
 * Mirrors llm_adapter.py (`_peer_alias_supports_vision`): when the agent's text
 * runs on a peer, the image goes to that peer if the peer's row for the agent's
 * alias says `supports_vision`; an unknown alias or an unanswered
 * PROVIDERS_RESPONSE reads as no and falls through to the local rule below.
 */
export function effectiveAgentVisionModel(
  agentAlias: string,
  providers: VisionRow[],
  globalVisionProvider: string,
  computeHost: string = "",
  peerRows: Map<string, VisionRow[]> = new Map(),
  hostName: string = ""
): EffectiveVisionModel {
  if (agentAlias && computeHost && computeHost !== "local") {
    const peerOwn = (peerRows.get(computeHost) || []).find((p) => p.alias === agentAlias);
    if (peerOwn?.supports_vision) {
      const where = hostName || `${computeHost.slice(0, 20)}...`;
      const name = peerOwn.model ? `${agentAlias} (${peerOwn.model})` : agentAlias;
      return { alias: agentAlias, label: `${name} - on ${where}`, isGlobal: false };
    }
  }

  const own = (providers || []).find((p) => p.alias === agentAlias);
  if (agentAlias && own?.supports_vision) {
    return {
      alias: agentAlias,
      label: own.model ? `${agentAlias} (${own.model})` : agentAlias,
      isGlobal: false
    };
  }

  const global = (providers || []).find((p) => p.alias === globalVisionProvider);
  const name = globalVisionProvider || "none configured";
  return {
    alias: globalVisionProvider || "",
    label: global?.model ? `${name} (${global.model}) — global` : `${name} — global`,
    isGlobal: true
  };
}
