import { describe, it, expect } from 'vitest';
import src from './FirewallEditor.svelte?raw';

// The editor has no mount harness, so this reads its source. A rules file with no
// value for these keys is read by the backend as False (firewall.py,
// _parse_dpc_agent_settings), so every fallback the editor writes must say false,
// or the screen claims an access the backend refuses.
const ACCESS_KEYS = ['personal_context_access', 'device_context_access', 'human_knowledge_access'];

function body(signature: string): string {
  const start = src.indexOf(signature);
  expect(start).toBeGreaterThan(0);
  const end = src.indexOf('\n  }\n', start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

const FALLBACKS = ['function startEditing()', 'function addAgentProfile()', 'function ensureAgentProfileExists()'];

describe('agent access fallbacks in the firewall editor', () => {
  for (const fn of FALLBACKS) {
    it(`${fn} writes every access key as false`, () => {
      const b = body(fn);
      for (const key of ACCESS_KEYS) {
        expect(b).toMatch(new RegExp(`\\b${key}: false,`));
        expect(b).not.toMatch(new RegExp(`\\b${key}: true\\b`));
      }
    });
  }

  it('a new profile copied from a global block without the keys gets off', () => {
    const b = body('function addAgentProfile()');
    const copy = b.slice(b.indexOf('editedRules.agent_profiles[profileName] = {'));
    for (const key of ACCESS_KEYS) {
      const m = copy.match(new RegExp(`${key}: (defaultSettings\\.${key}[^,]*),`));
      expect(m).not.toBeNull();
      const run = new Function('defaultSettings', `return ${m![1]};`);
      expect(run({ enabled: true })).toBe(false);
      expect(run({ [key]: true })).toBe(true);
      expect(run({ [key]: false })).toBe(false);
    }
  });
});
