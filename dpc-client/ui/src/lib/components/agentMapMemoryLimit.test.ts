import { describe, it, expect } from 'vitest';
import src from './AgentMap.svelte?raw';

// No mount harness: the memory limit line of the D-PC backend card is checked in the
// source, the way the other component guards do. The wording and rules are tested in
// utils/shellMemoryLimit.test.ts.
const card = () => {
  const start = src.indexOf('<span class="node-title">D-PC backend</span>');
  expect(start, 'the D-PC backend card is in the markup').toBeGreaterThan(0);
  return src.slice(start, src.indexOf('<div class="branch">', start));
};
const fn = (name: string) => {
  const m = src.match(new RegExp(`(?:async )?function ${name}\\(([\\s\\S]*?)\\n  \\}\\n`));
  expect(m, `${name} is in the script`).not.toBeNull();
  return m![0];
};

describe('the D-PC backend card shows the memory limit per command', () => {
  it('builds the line from the list_processes fields through the util, inside the backend card', () => {
    expect(src).toContain('let limit = $derived(memoryLimitView(listing));');
    expect(src).toContain('shell_memory_limit_mb: r.shell_memory_limit_mb ?? null');
    expect(src).toContain('shell_memory_limit_source: r.shell_memory_limit_source ?? null');
    expect(card()).toContain('{limit.line}');
  });

  it('shows "(default)" through the suffix the util sets', () => {
    expect(card()).toContain('{#if limit.suffix}');
    expect(card()).toContain('{limit.suffix}');
  });

  it('from the environment it is read-only: the note, and no Edit', () => {
    const c = card();
    const guard = c.indexOf('{#if limit.editable}');
    expect(guard).toBeGreaterThan(0);
    const edit = c.indexOf('onclick={editLimit}');
    const note = c.indexOf('{limit.envNote}');
    expect(edit).toBeGreaterThan(guard);
    expect(c.indexOf('{:else}', edit)).toBeGreaterThan(edit);
    expect(note).toBeGreaterThan(c.indexOf('{:else}', edit));
    expect(fn('editLimit')).toContain('if (!limit?.editable) return;');
  });

  it('a limit below 1,024 MiB shows the warning line, and while editing it follows what is typed', () => {
    expect(card()).toContain('{#if limitWarning}');
    expect(card()).toContain('{limitWarning}');
    expect(src).toContain('memoryLimitWarning(limitTyped.mb)');
    expect(src).toContain('limit?.warning');
  });

  it('opens the input with 0 for no limit, and says that an empty field is the default', () => {
    expect(fn('editLimit')).toContain('memoryLimitInputText(limit.mb)');
    expect(card()).toContain('placeholder="8192 (default)"');
    expect(card()).toContain('0 = no limit, empty = back to the default 8,192 MiB');
    expect(fn('saveLimit')).toContain('parseMemoryLimitInput(limitDraft)');
    expect(fn('saveLimit')).toContain("sendCommand('set_shell_memory_limit', { mb: typed.mb })");
  });

  it('Save, Cancel and Enter / Escape are wired', () => {
    const c = card();
    expect(c).toContain('onclick={saveLimit}');
    expect(c).toContain('onclick={cancelLimit}');
    expect(c).toContain('onkeydown={limitKeydown}');
    expect(fn('limitKeydown')).toContain("e.key === 'Enter'");
    expect(fn('limitKeydown')).toContain("e.key === 'Escape'");
  });

  it('a refusal from the command, and a failed send, are shown inline and keep the editor open', () => {
    const save = fn('saveLimit');
    expect(save).toContain("limitError = r?.message || 'Could not set the memory limit'");
    expect(save).toContain('limitError = `Could not set the memory limit: ${e}`');
    expect(save).toContain('limitEditing = false;');
    expect(save.indexOf('limitEditing = false;')).toBeLessThan(save.indexOf('} else {'));
    expect(card()).toContain('{#if limitError}');
    expect(card()).toContain('{limitError}');
  });

  it('a typed value the backend would refuse is stopped before it is sent', () => {
    const save = fn('saveLimit');
    const parse = save.indexOf("if ('error' in typed)");
    expect(parse).toBeGreaterThan(0);
    expect(parse).toBeLessThan(save.indexOf("sendCommand('set_shell_memory_limit'"));
  });

  it('after a save the list is read again, so the line shows the effective value and source', () => {
    expect(fn('saveLimit')).toContain('untrack(refresh)');
    expect(fn('saveLimit')).toContain('r.shell_memory_limit_source');
  });
});
