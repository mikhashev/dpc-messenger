import { describe, it, expect } from 'vitest';
import { isProviderDraftFilled, validateProviderDraftForAdd } from './providerDraft';

describe('isProviderDraftFilled', () => {
  it('is unfilled on the pristine form (default type, everything else empty)', () => {
    expect(isProviderDraftFilled({ alias: '', type: 'ollama', model: '' })).toBe(false);
  });

  it('changing only the type dropdown does not count as filled', () => {
    expect(isProviderDraftFilled({ alias: '', type: 'neuraldeep', model: '' })).toBe(false);
  });

  it('a typed alias counts as filled', () => {
    expect(isProviderDraftFilled({ alias: 'my-nd', type: 'neuraldeep', model: '' })).toBe(true);
  });

  it('a typed model with no alias still counts as filled', () => {
    expect(isProviderDraftFilled({ alias: '', type: 'neuraldeep', model: 'qwen3.8-27b' })).toBe(true);
  });

  it('a typed api key or key-env name counts as filled even with alias/model blank', () => {
    expect(isProviderDraftFilled({ alias: '', model: '', api_key: 'sk-abc' })).toBe(true);
    expect(isProviderDraftFilled({ alias: '', model: '', api_key_env: 'NEURALDEEP_API_KEY' })).toBe(true);
  });

  it('whitespace-only fields are still blank', () => {
    expect(isProviderDraftFilled({ alias: '   ', model: '\t' })).toBe(false);
  });
});

describe('validateProviderDraftForAdd', () => {
  it('accepts a filled, unique draft', () => {
    const result = validateProviderDraftForAdd(
      { alias: 'my-nd', type: 'neuraldeep', model: 'qwen3.8-27b' },
      ['ollama-default'],
    );
    expect(result.valid).toBe(true);
    expect(result.reason).toBeUndefined();
  });

  it('rejects an empty alias', () => {
    const result = validateProviderDraftForAdd({ alias: '', type: 'neuraldeep', model: 'x' }, []);
    expect(result.valid).toBe(false);
    expect(result.reason).toMatch(/alias/i);
  });

  it('rejects a missing model unless the type is dpc_agent', () => {
    const missing = validateProviderDraftForAdd({ alias: 'a', type: 'neuraldeep', model: '' }, []);
    expect(missing.valid).toBe(false);
    expect(missing.reason).toMatch(/model/i);

    const agent = validateProviderDraftForAdd({ alias: 'a', type: 'dpc_agent', model: '' }, []);
    expect(agent.valid).toBe(true);
  });

  it('rejects a raw key pasted into the env-name field', () => {
    const result = validateProviderDraftForAdd(
      { alias: 'a', type: 'neuraldeep', model: 'x', api_key_env: 'sk-abcDEF123' },
      [],
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toMatch(/key/i);
  });

  it('rejects an alias that already names a provider', () => {
    const result = validateProviderDraftForAdd(
      { alias: 'my-nd', type: 'neuraldeep', model: 'x' },
      ['my-nd', 'other'],
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toMatch(/already exists/i);
  });
});
