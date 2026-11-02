import { describe, it, expect } from 'vitest';
import editor from './ProvidersEditor.svelte?raw';
import controls from './SessionControls.svelte?raw';
import page from '../../routes/+page.svelte?raw';
import panel from '../panels/ChatPanel.svelte?raw';
import { strataWindowHint } from '../utils/strataFields';
import { agentAliasToSend } from '../utils/rememberedProvider';

// Forge, 2026-11-02: an alias Add Provider made from a run config had no window, and the
// backend's answer for that was 4096. The backend now asks the engine; the form says so.

describe('the note under Context Window on a strata alias', () => {
  it('names the run config number and says it is the engine that decides, once read', () => {
    const hint = strataWindowHint(131072);
    expect(hint).toContain('--max-context');
    expect(hint).toContain((131072).toLocaleString());
    expect(hint).toContain('what the engine reports');
  });

  it('says plainly when the run config names none, and what follows', () => {
    const hint = strataWindowHint(null);
    expect(hint).toContain('names no --max-context');
    expect(hint).toContain('refuses');
  });

  it('says nothing until the run config has been read', () => {
    expect(strataWindowHint(undefined)).toBe('');
  });

  it('a value that is not a whole number is no window', () => {
    expect(strataWindowHint('auto')).toContain('names no --max-context');
  });
});

describe('the forms carry it', () => {
  const add = () => editor.slice(editor.indexOf('<!-- Add Provider Form -->'));

  it('Add reads the chosen run config and shows the note, and never writes a window for strata', () => {
    expect(editor).toContain('readNewStrataWindow(value);');
    expect(add()).toContain('strataWindowHint(newStrataMaxContext)');
    const branch = editor.slice(
      editor.indexOf("} else if (newProvider.type === 'strata') {"),
      editor.indexOf("} else if (newProvider.type === 'local_whisper') {"),
    );
    expect(branch).not.toContain('context_window');
  });

  it('Edit shows it beside the note about the agent window, from the alias or its base config', () => {
    expect(editor).toContain(
      "strataWindowHint(editedConfig.providers[i].max_context ?? strataBase(strataInspect, i, 'max_context'))",
    );
  });

  it('every other type keeps its prefill', () => {
    expect(editor).toContain('provider.context_window = 262144;');
    expect(editor).toContain('provider.context_window = 1000000;');
  });
});

describe('the token line after a switch', () => {
  it('takes the window the save answered, and an unset one is said, not the old number', () => {
    expect(page).toContain('const answered = Number(res?.context_window) || 0;');
    expect(page).toContain('limit: answered, windowUnknown: answered === 0');
    expect(page).toContain('windowUnknown={currentTokenUsage.windowUnknown ?? false}');
  });

  it('prints the words in place of a default number', () => {
    expect(controls).toContain("windowUnknown ? 'window not known' : effectiveLimit.toLocaleString()");
    expect(controls).toContain('tokens · window not known');
  });

  it('a switch in the Agent Configuration ends the pick in the chat\'s Text selector', () => {
    expect(page).toContain("chatProviders.update(m => m.has(agentId) ? new Map(m).set(agentId, 'dpc_agent') : m);");
  });
});

describe('what an agent chat sends for the Text selector', () => {
  it('the Agent row sends the alias the agent was configured with', () => {
    expect(agentAliasToSend('dpc_agent', 'flash', true)).toBe('flash');
    expect(agentAliasToSend('dpc_agent', undefined, true)).toBeNull();
  });

  it('another alias picked in an agent chat is the alias the message runs on', () => {
    expect(agentAliasToSend('small', 'flash', true)).toBe('small');
  });

  it('a chat with no agent names no agent alias', () => {
    expect(agentAliasToSend('small', undefined, false)).toBeNull();
  });

  it('ChatPanel sends it', () => {
    expect(panel).toContain('agentAliasToSend(chatSpecificProvider, chatMeta?.llm_provider, isAgentChat)');
  });
});
