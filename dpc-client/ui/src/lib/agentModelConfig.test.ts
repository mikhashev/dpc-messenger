import { describe, it, expect } from 'vitest';
import { buildAgentModelConfigPayload, type AgentModelConfigState } from './agentModelConfig';

/**
 * Reproduces what the dialog actually sent on 2026-08-18:
 *
 *   {'agent_id': 'agent_001', 'provider_alias': 'deepseek_flash',
 *    'sleep_provider_alias': None, 'snapshot_summarize_provider': None,
 *    'compaction_provider': None, ...}
 *
 * and `agent_001/config.json` kept `qwen3.8:latest` in exactly those three
 * fields, because the backend reads a missing value as «leave unchanged».
 */

const base: AgentModelConfigState = {
  providerAlias: 'deepseek_flash',
  sleepProvider: 'qwen3.8:latest',
  snapshotProvider: 'qwen3.8:latest',
  snapshotThreshold: 8000,
  compactionEnabled: true,
  compactionProvider: 'qwen3.8:latest',
  compactionThreshold: 0.3,
  knowledgeProvider: '',
  retrievalVector: 'native',
  retrievalText: 'native',
};

describe('agent model config payload', () => {
  it('sends the empty choice as an empty string, not as null', () => {
    // «Default (global)» is <option value=""> in all three dropdowns.
    const payload = buildAgentModelConfigPayload({
      ...base,
      sleepProvider: '',
      snapshotProvider: '',
      compactionProvider: '',
    });

    expect(payload.sleep_provider_alias).toBe('');
    expect(payload.snapshot_summarize_provider).toBe('');
    expect(payload.compaction_provider).toBe('');
    // The distinction that matters: none of them may be null, which the
    // backend would read as "the caller said nothing about this field".
    expect(payload.sleep_provider_alias).not.toBeNull();
    expect(payload.snapshot_summarize_provider).not.toBeNull();
    expect(payload.compaction_provider).not.toBeNull();
  });

  it('passes a chosen model through unchanged', () => {
    const payload = buildAgentModelConfigPayload(base);

    expect(payload.provider_alias).toBe('deepseek_flash');
    expect(payload.sleep_provider_alias).toBe('qwen3.8:latest');
    expect(payload.compaction_provider).toBe('qwen3.8:latest');
  });

  it('keeps null for the numeric fields, where there is no default to clear to', () => {
    const payload = buildAgentModelConfigPayload({
      ...base,
      snapshotThreshold: 0,
      compactionThreshold: '',
    });

    expect(payload.snapshot_summarize_threshold).toBeNull();
    expect(payload.compaction_threshold).toBeNull();
  });

  it('carries the flags and the retrieval backends verbatim', () => {
    const payload = buildAgentModelConfigPayload({
      ...base,
      compactionEnabled: false,
      retrievalVector: 'grafeo',
      retrievalText: 'grafeo',
    });

    expect(payload.compaction_enabled).toBe(false);
    expect(payload.retrieval_vector).toBe('grafeo');
    expect(payload.retrieval_text).toBe('grafeo');
  });
});

describe('the knowledge extraction field', () => {
  it('sends an empty choice as an empty string — «walk the chain», not «global default»', () => {
    const payload = buildAgentModelConfigPayload({ ...base, knowledgeProvider: '' });

    expect(payload.knowledge_provider).toBe('');
    expect(payload.knowledge_provider).not.toBeNull();
  });

  it('passes an explicit choice through unchanged', () => {
    const payload = buildAgentModelConfigPayload({ ...base, knowledgeProvider: 'llama.cpp' });

    expect(payload.knowledge_provider).toBe('llama.cpp');
  });
});
