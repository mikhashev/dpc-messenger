import { describe, expect, it } from 'vitest';
import { aiChatWindow, chatEffortOptions, selectedModelRow } from './chatEffort';

const bonsai = { alias: 'bonsai', reasoning_words: ['xhigh', 'medium'], reasoning_default: 'xhigh', context_window: 262144 };
const plain = { alias: 'flash', context_window: null };

describe('the AI chat Reasoning list is the selected model\'s own', () => {
  it('lists the model\'s words plus Off, and the empty option is Model default, not Agent config', () => {
    const list = chatEffortOptions(bonsai, '');
    expect(list.emptyLabel).toBe('Model default (xhigh)');
    expect(list.options.map((o) => o.value)).toEqual(['off', 'xhigh', 'medium']);
    expect(list.options[1].label).toBe('Xhigh — default for this model');
    expect(list.fromModel).toBe(true);
    expect(JSON.stringify(list)).not.toContain('Agent config');
  });

  it('keeps a stored level the model does not know, marked', () => {
    const list = chatEffortOptions(bonsai, 'low');
    expect(list.options.at(-1)).toEqual({ value: 'low', label: 'Low — not in this model' });
  });

  it('falls back to the shared scale for a model that names no words', () => {
    const list = chatEffortOptions(plain, '');
    expect(list.emptyLabel).toBe('Model default');
    expect(list.options.map((o) => o.value)).toEqual(['off', 'low', 'medium', 'high', 'max']);
    expect(list.fromModel).toBe(false);
  });

  it('changes with the model', () => {
    const before = chatEffortOptions(plain, 'high').options.map((o) => o.value);
    const after = chatEffortOptions(bonsai, 'high').options.map((o) => o.value);
    expect(before).not.toEqual(after);
    expect(after).toEqual(['off', 'xhigh', 'medium', 'high']);
  });
});

describe('selectedModelRow', () => {
  const peers = new Map<string, any[]>([['dpc-node-x', [{ alias: 'llama:70b', context_window: 32768 }]]]);
  it('finds a local and a peer selection', () => {
    expect(selectedModelRow('local:bonsai', [bonsai, plain], peers)).toBe(bonsai);
    expect(selectedModelRow('remote:dpc-node-x:llama:70b', [bonsai], peers)?.context_window).toBe(32768);
    expect(selectedModelRow('', [bonsai], peers)).toBeNull();
    expect(selectedModelRow('local:gone', [bonsai], peers)).toBeNull();
  });
});

describe('the AI chat window before the engine reports one', () => {
  it('is the configured context_window, not a fixed 16,384', () => {
    expect(aiChatWindow(bonsai, 0)).toBe(262144);
    expect(aiChatWindow(bonsai, 16384)).toBe(262144);
  });
  it('is the reported number when the model has none, and 0 when nobody knows', () => {
    expect(aiChatWindow(plain, 8192)).toBe(8192);
    expect(aiChatWindow(plain, 0)).toBe(0);
    expect(aiChatWindow(null, 0)).toBe(0);
  });
});

describe('the page wires it', () => {
  it('the plain AI chat select lists the model levels; the agent and group selects keep Agent config', async () => {
    const page = (await import('../../routes/+page.svelte?raw')).default as string;
    expect(page).toContain('<option value="">{aiChatEfforts.emptyLabel}</option>');
    expect(page).toContain('{#if !chatHeaderCollapsed && isActuallyAIChat && !isAgentChatHeader}');
    expect(page).toContain('limit: aiChatLimit > 0 ? aiChatLimit : DEFAULT_TOKEN_LIMIT');
    const agentBranch = page.slice(page.indexOf('{#if !chatHeaderCollapsed && isActuallyAIChat && isAgentChatHeader}'));
    expect(agentBranch.slice(0, 3000)).toContain('<option value="">Agent config</option>');
  });
});
