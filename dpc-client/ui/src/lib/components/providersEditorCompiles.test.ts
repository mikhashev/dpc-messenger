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
