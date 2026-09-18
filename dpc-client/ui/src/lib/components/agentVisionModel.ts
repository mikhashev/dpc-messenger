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

export function effectiveAgentVisionModel(
  agentAlias: string,
  providers: VisionRow[],
  globalVisionProvider: string
): EffectiveVisionModel {
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
