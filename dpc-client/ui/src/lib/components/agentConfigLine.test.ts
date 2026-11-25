import { describe, expect, it } from 'vitest';
import { agentConfigLine, type AgentLineInput } from './agentConfigLine';
import { agentHostDisplay } from './agentComputeHost';

const PEER = 'dpc-node-' + 'b'.repeat(32);

const base: AgentLineInput = {
  alias: 'deepseek_flash',
  fallbackAlias: 'qwen',
  providers: [
    { alias: 'deepseek_flash', model: 'deepseek-v4-flash', type: 'deepseek', route: 'local', provider_kind: 'vendor' },
    { alias: 'qwen', model: 'qwen3.8', type: 'llamacpp_server', route: 'local', provider_kind: 'self_hosted' },
  ],
  peerRows: [],
  reasoning: 'high',
  host: agentHostDisplay('', []),
  vision: { alias: 'strata', label: 'strata (flash-next) — global', isGlobal: true },
  visionIsAgentOwn: false,
  voiceAlias: '',
  globalVoiceAlias: 'whisper',
};

describe('agentConfigLine (the agent 1:1 header)', () => {
  it('states model, place, reasoning level and host on one line', () => {
    expect(agentConfigLine(base).text).toBe(
      'deepseek_flash (deepseek-v4-flash) · cloud: DeepSeek · reasoning high · host Local');
  });

  it('says the model default when the agent keeps no level', () => {
    expect(agentConfigLine({ ...base, reasoning: '' }).text).toContain('reasoning: model default');
  });

  it('names a peer host and the peer row of the model', () => {
    const line = agentConfigLine({
      ...base,
      alias: 'llama70',
      host: agentHostDisplay(PEER, [{ node_id: PEER, name: 'Ark box' }]),
      peerRows: [{ alias: 'llama70', model: 'llama-70b' }],
    });
    expect(line.text).toBe(`llama70 (llama-70b) · reasoning high · host Ark box | ${PEER.slice(0, 20)}...`);
  });

  it('falls back to the node default when the agent names no model', () => {
    const line = agentConfigLine({ ...base, alias: '' });
    expect(line.text.startsWith('qwen (qwen3.8) · local model')).toBe(true);
    expect(line.title).toContain('the node default');
  });

  it('tells Vision and Voice in the tooltip, marking the agent\'s own choice', () => {
    const own = agentConfigLine({
      ...base,
      vision: { alias: 'qwen-vl', label: 'qwen-vl (qwen3-vl) — global', isGlobal: true },
      visionIsAgentOwn: true,
      voiceAlias: 'whisper-big',
    });
    expect(own.title).toContain('Vision: qwen-vl (qwen3-vl) — agent config');
    expect(own.title).toContain('Voice: whisper-big — agent config');
    const global = agentConfigLine(base);
    expect(global.title).toContain('Vision: strata (flash-next) — global');
    expect(global.title).toContain('Voice: whisper — global');
  });
});

describe('the agent 1:1 header has no selectors', () => {
  it('ProviderSelector renders the line and a Configure button for an agent chat', async () => {
    const src = (await import('./ProviderSelector.svelte?raw')).default as string;
    const agentBranch = src.slice(src.indexOf('{#if isAIChat && isAgentChat}'), src.indexOf('{:else if isAIChat && providersList.length > 0}'));
    expect(agentBranch).toContain('agentLine().text');
    expect(agentBranch).toContain('onOpenAgentConfig');
    for (const id of ['ai-host-header', 'text-provider-header', 'vision-provider-header', 'voice-provider-header']) {
      expect(agentBranch).not.toContain(id);
    }
  });
});
