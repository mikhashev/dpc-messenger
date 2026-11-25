// The read-only line an agent 1:1 chat's header shows in place of the AI Host,
// Text, Vision and Voice selectors (Mike, 2026-11-25): the agent's model, its
// reasoning level and its host, all set in Agent Configuration.
//
// What the selectors did in an agent 1:1 turn before they went (traced
// 2026-11-25, follow-up 13):
// - AI Host was already a statement of the agent's own compute_host.
// - Text: picking another alias stored it as the chat's provider and every next
//   turn ran a second DpcAgent on that alias (agent_llm_provider override) until
//   the configuration was saved again; it also broke images, which went to the
//   agent only while the chat's provider read `dpc_agent`.
// - Vision was already a statement: the agent's own model when it sees, else the
//   global vision provider; the backend ignored the header.
// - Voice reached only dictation in that chat (transcribe_audio provider_alias).
// The Main LLM, reasoning, Vision and Voice now live in the dialog.

import type { HostDisplay } from './agentComputeHost';
import type { EffectiveVisionModel } from './agentVisionModel';
import { providerPlace, type PlaceRow } from '../utils/providerPlace';

export type AgentLineInput = {
  /** The agent's Main LLM alias (chat meta `llm_provider`); empty when not known. */
  alias: string;
  /** The node's agent/default provider, said when the agent names none. */
  fallbackAlias: string;
  providers: PlaceRow[];
  /** The host peer's rows, when the agent runs on a peer. */
  peerRows: PlaceRow[];
  /** The agent's own level (config reasoning_effort); empty = the model's default. */
  reasoning: string;
  host: HostDisplay;
  vision: EffectiveVisionModel;
  /** True when the vision alias came from the agent's own configuration. */
  visionIsAgentOwn: boolean;
  voiceAlias: string;
  globalVoiceAlias: string;
};

export type AgentLine = { text: string; title: string };

export function agentConfigLine(input: AgentLineInput): AgentLine {
  const alias = input.alias || input.fallbackAlias;
  const onPeer = input.host.host !== 'local';
  const row = (onPeer ? input.peerRows : input.providers).find((p) => p.alias === alias)
    ?? input.providers.find((p) => p.alias === alias);
  const modelName = row?.model ? `${alias} (${row.model})` : alias || 'default model';
  const place = onPeer ? '' : row ? ` · ${providerPlace(row, 'local')}` : '';
  const reasoning = input.reasoning ? `reasoning ${input.reasoning}` : 'reasoning: model default';
  const host = `host ${input.host.label}`;
  const text = `${modelName}${place} · ${reasoning} · ${host}`;

  const vision = input.vision.isGlobal && input.visionIsAgentOwn
    ? input.vision.label.replace(/ — global$/, ' — agent config')
    : input.vision.label;
  const voice = input.voiceAlias
    ? `${input.voiceAlias} — agent config`
    : input.globalVoiceAlias ? `${input.globalVoiceAlias} — global` : 'automatic — global';
  const title = [
    `Main LLM: ${modelName}${input.alias ? '' : ' (no model of its own: the node default)'}`,
    `Reasoning: ${input.reasoning || "the model's own default"}`,
    `Host: ${input.host.label}${input.host.connected ? '' : ' — not connected now'}`,
    `Vision: ${vision}`,
    `Voice: ${voice}`,
    'Set in Agent Configuration (Configure).',
  ].join('\n');
  return { text, title };
}
