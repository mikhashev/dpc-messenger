import { describe, it, expect } from 'vitest';
import { compile, parse } from 'svelte/compiler';
import src from './ProvidersEditor.svelte?raw';

// The editor has no mount harness, so this compiles it and reads the output.
// A <select bind:value={editedConfig...}> inside the providers {#each} makes the
// legacy compiler put the loop's names (the row index i) into every script-side
// editedConfig mutation, which then throws "i is not defined" at runtime.
const SCRIPT_GLOBALS = new Set(['$', '$$props', '$$sanitized_props', '$$restProps']);

// Top-level names of the instance script only: a local `let i` inside some
// function must not make a stray `i` look declared.
function scriptNames(source: string): Set<string> {
  const ast = parse(source, { modern: true });
  const names = new Set<string>();
  for (const node of (ast.instance!.content as any).body) {
    const decl = node.type === 'ExportNamedDeclaration' ? node.declaration : node;
    if (!decl) continue;
    if (decl.type === 'ImportDeclaration') {
      for (const sp of decl.specifiers) names.add(sp.local.name);
    } else if (decl.type === 'VariableDeclaration') {
      for (const d of decl.declarations) {
        if (d.id.type === 'Identifier') names.add(d.id.name);
      }
    } else if (decl.id?.name) {
      names.add(decl.id.name);
    }
  }
  return names;
}

function invalidationBodies(dev: boolean): string[] {
  const js = compile(src, { dev, filename: 'ProvidersEditor.svelte' }).js.code;
  return [...js.matchAll(/invalidate_inner_signals\(\(\) => \{([^}]*)\}/g)].map((m) => m[1]);
}

describe('ProvidersEditor compiled output', () => {
  const declared = scriptNames(src);
  for (const dev of [true, false]) {
    it(`invalidates only script-scope names (dev=${dev})`, () => {
      const unknown = new Set<string>();
      for (const body of invalidationBodies(dev)) {
        // Strip getter wrappers like $.get(x) and store calls like $x(); keep the root names.
        const roots = body.replace(/\$\.\w+\(/g, '(').match(/[A-Za-z_$][\w$]*/g) ?? [];
        for (const r of roots) {
          const name = r.replace(/^\$(?=\w)/, '');
          if (!declared.has(name) && !SCRIPT_GLOBALS.has(r)) unknown.add(r);
        }
      }
      expect([...unknown]).toEqual([]);
    });
  }

  // The strata status row: in legacy mode a template call is wrapped in
  // $.untrack and depends only on its arguments, so a row read through the
  // closure stayed at its first (empty) read — "Not loaded yet" beside a card
  // holder that named the same running alias.
  function rowDerived(source: string): string {
    const js = compile(source, { dev: false, filename: 'ProvidersEditor.svelte' }).js.code;
    const m = js.match(/const row = \$\.derived_safe_equal\(\(\) => \(([\s\S]*?)\)\);/);
    expect(m, 'the strata row derived is in the output').not.toBeNull();
    return m![1];
  }
  const tracksStatus = (body: string) =>
    /\$\.get\(engineStatus\)/.test(body.replace(/\$\.untrack\([\s\S]*$/, ''));

  it('the strata status row re-reads when the engine status arrives', () => {
    expect(tracksStatus(rowDerived(src))).toBe(true);
  });

  it('the row guard sees the defect it is meant to catch', () => {
    const bad = src
      .replace('{@const row = strataRow(engineStatus, alias)}', '{@const row = strataRow(alias)}')
      .replace(
        'function strataRow(status: typeof engineStatus, alias: string): any | null {\n    return status?.strata',
        'function strataRow(alias: string): any | null {\n    return engineStatus?.strata',
      );
    expect(bad).not.toBe(src);
    expect(tracksStatus(rowDerived(bad))).toBe(false);
  });

  it('the guard sees the defect it is meant to catch', () => {
    const bad = src.replace(
      "value={editedConfig.providers[i].type} on:change={(e) => setProviderField(i, 'type', e.currentTarget.value)}",
      'bind:value={editedConfig.providers[i].type}',
    );
    expect(bad).not.toBe(src);
    const js = compile(bad, { dev: true, filename: 'ProvidersEditor.svelte' }).js.code;
    expect(js).toMatch(/invalidate_inner_signals\(\(\) => \{\s*i;/);
  });
});
