import { describe, expect, it } from 'vitest';
import { providerOptionText, providerPlace } from './providerPlace';

// THE-CHAT-MODEL-DROPDOWN-CALLS-CLOUD-MODELS-LOCAL-...: a row is labelled by where
// its model runs, not by which node calls it.
describe('providerPlace', () => {
  it('calls a model on this machine a local model', () => {
    expect(providerPlace({ alias: 'q', type: 'llamacpp_server', route: 'local', provider_kind: 'self_hosted' }))
      .toBe('local model');
  });

  it('calls a vendor API cloud, naming the vendor', () => {
    expect(providerPlace({ alias: 'deepseek_flash', type: 'deepseek', route: 'local', provider_kind: 'vendor' }))
      .toBe('cloud: DeepSeek');
    expect(providerPlace({ alias: 'nd', type: 'neuraldeep', route: 'local', provider_kind: 'vendor' }))
      .toBe('cloud: NeuralDeep');
  });

  it('calls an OpenAI-compatible server on loopback local and one in the LAN your network', () => {
    expect(providerPlace({ alias: 'lm', type: 'openai_compatible', route: 'local', provider_kind: 'self_hosted' }))
      .toBe('local model');
    expect(providerPlace({ alias: 'nas', type: 'openai_compatible', route: 'local', provider_kind: 'unknown', host_scope: 'lan' }))
      .toBe('your network');
  });

  it('does not guess for a generic API at a host that names no vendor', () => {
    expect(providerPlace({ alias: 'o', type: 'openai_compatible', route: 'local', provider_kind: 'vendor', base_url_host: 'llm.example.org' }))
      .toBe('cloud? llm.example.org');
    expect(providerPlace({ alias: 'x', type: 'something_new', route: 'local', provider_kind: 'unknown' }))
      .toBe('place unknown');
  });

  it('says a local alias that dials a peer goes via a peer, and a peer row is remote', () => {
    expect(providerPlace({ alias: 'alice', type: 'remote_peer', route: 'peer', provider_kind: 'unknown' }))
      .toBe('via peer');
    expect(providerPlace({ alias: 'llama70', type: 'ollama' }, 'remote')).toBe('remote');
  });

  it('falls back to the type when the backend said nothing about the place', () => {
    expect(providerPlace({ alias: 'w', type: 'local_whisper' })).toBe('local model');
    expect(providerPlace({ alias: 'c', type: 'anthropic' })).toBe('cloud: Anthropic');
    expect(providerPlace({ alias: 'o', type: 'openai_compatible' })).toBe('place unknown');
    expect(providerPlace({ alias: 'p', type: 'remote_peer' })).toBe('via peer');
  });

  it('never says local for a cloud vendor', () => {
    for (const type of ['anthropic', 'zai', 'deepseek', 'neuraldeep', 'gemini', 'github_models', 'gigachat']) {
      expect(providerPlace({ alias: 'a', type, route: 'local', provider_kind: 'vendor' })).toMatch(/^cloud: /);
      expect(providerPlace({ alias: 'a', type })).toMatch(/^cloud: /);
    }
  });
});

describe('providerOptionText', () => {
  it('puts alias, model and place in one option', () => {
    expect(providerOptionText({ alias: 'deepseek_flash', model: 'deepseek-v4-flash', type: 'deepseek', route: 'local', provider_kind: 'vendor' }))
      .toBe('deepseek_flash (deepseek-v4-flash) - cloud: DeepSeek');
    expect(providerOptionText({ alias: 'llama70', model: 'llama-70b', type: 'ollama' }, 'remote'))
      .toBe('llama70 (llama-70b) - remote');
  });
});

describe('the chat model lists use it', () => {
  it('ProviderSelector and InstructionsEditor no longer write "- local" by hand', async () => {
    const selector = (await import('../components/ProviderSelector.svelte?raw')).default as string;
    const editor = (await import('../components/InstructionsEditor.svelte?raw')).default as string;
    for (const src of [selector, editor]) {
      expect(src).toContain('providerOptionText(');
      expect(src).not.toMatch(/\$\{p\.model\}\)? - local`/);
    }
  });
});
