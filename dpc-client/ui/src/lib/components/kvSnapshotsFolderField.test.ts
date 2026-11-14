import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';
import { KV_SNAPSHOTS_FOLDER_LABEL } from '../utils/kvSnapshots';

// Same approach as strataBudgetFields.test.ts: no mount harness, so read the
// markup and run the handler the field calls.
const END_OF_GROUP = '\n                      </div>\n';

function block(id: string): string {
  const start = src.indexOf(`id="${id}-{i}"`);
  expect(start, `${id} is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf(END_OF_GROUP, start));
}

// A script function as the component defines it, with its parameters named from its signature.
function fromScript(name: string): (editedConfig: unknown, ...args: unknown[]) => void {
  const m = src.match(new RegExp('function ' + name + '\\(([^)]*)\\) \\{([\\s\\S]*?)\\n  \\}'));
  expect(m, `${name} is in the script`).not.toBeNull();
  const params = m![1].split(',').map((x) => x.trim().split(':')[0].replace(/\s*=.*$/, '').trim()).filter(Boolean);
  const body = m![2].replace(/ as any/g, '');
  return new Function('editedConfig', ...params, body) as (editedConfig: unknown, ...args: unknown[]) => void;
}
const setNum = (providers: Record<string, unknown>[], i: number, key: string, raw: string, float = false) => {
  fromScript('setNum')({ providers }, i, key, raw, float);
  return providers[i];
};
const setStr = (providers: Record<string, unknown>[], i: number, key: string, raw: string) => {
  fromScript('setStr')({ providers }, i, key, raw);
  return providers[i];
};

// setStrataPath as the script defines it, with the base-config inspection stubbed out.
const setStrataPath = (() => {
  const m = src.match(/function setStrataPath\(i: number, key: StrataPathKey, raw: string\) \{([\s\S]*?)\n  \}/);
  expect(m, 'setStrataPath is in the script').not.toBeNull();
  const body = m![1].replace(/ as any/g, '');
  return (providers: Record<string, unknown>[], i: number, key: string, raw: string) => {
    const run = new Function('editedConfig', 'i', 'key', 'raw', 'pleAfterNativeChange', 'inspectStrataBase', body);
    run({ providers }, i, key, raw, () => null, () => undefined);
    return providers[i];
  };
})();

describe('KV snapshots folder field, Strata', () => {
  it('is a text input with the alias value, the base config value as placeholder and a Browse button', () => {
    const b = block('strata-slot-save');
    expect(b).toContain('value={sp.slot_save_path ?? \'\'}');
    expect(b).toContain("setStrataPath(i, 'slot_save_path', e.currentTarget.value)");
    expect(b).toContain("strataBase(strataInspect, i, 'slot_save_path'), 'off — empty'");
    expect(b).toContain("pickStrataPath(i, 'slot_save_path')");
    expect(b).not.toContain('bind:value');
  });

  it('opens a folder dialog, not a file dialog', () => {
    expect(src).toMatch(/slot_save_path: \{ title: '[^']+', directory: true \}/);
  });

  it("is labelled with the shared KV snapshots label and sits after the server window, last of the cache section", () => {
    expect(KV_SNAPSHOTS_FOLDER_LABEL).toBe('KV snapshots folder (slot_save_path)');
    expect(src).toContain('<label for="strata-slot-save-{i}">{KV_SNAPSHOTS_FOLDER_LABEL}</label>');
    const labels = [...src.matchAll(/<label for="([a-z-]+)-\{i\}">/g)].map((m) => m[1]);
    const at = labels.indexOf('strata-maxctx');
    expect(at).toBeGreaterThan(-1);
    expect(labels[at + 1]).toBe('strata-slot-save');
  });

  it('carries the two alias fields through the shared snippet', () => {
    const b = block('strata-slot-save');
    expect(b).toContain('kvSnapshotExtras(i, sp.slot_save_path,');
    expect(b).toContain('sp.kv_snapshot_ttl_s, sp.kv_snapshot_max_gib');
  });

  it('the handler writes the trimmed path and drops the key when emptied', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setStrataPath(providers, 0, 'slot_save_path', '  D:\\kv-snapshots ').slot_save_path).toBe('D:\\kv-snapshots');
    const p = setStrataPath(providers, 0, 'slot_save_path', '');
    expect('slot_save_path' in JSON.parse(JSON.stringify(p))).toBe(false);
  });
});

describe('KV snapshots folder field, llama-server', () => {
  it('has the same label, a Browse button and the shared fields', () => {
    const b = block('slot-save');
    expect(src).toContain('<label for="slot-save-{i}">{KV_SNAPSHOTS_FOLDER_LABEL}</label>');
    expect(b).toContain('pickKvSnapshotsFolder(i)');
    expect(b).toContain("setStr(i, 'slot_save_path', e.currentTarget.value.trim())");
    expect(b).toContain('kvSnapshotExtras(i, editedConfig.providers[i].slot_save_path,');
    expect(b).toContain('editedConfig.providers[i].kv_snapshot_ttl_s, editedConfig.providers[i].kv_snapshot_max_gib');
  });

  it('picks a folder, with the same dialog title as Strata', () => {
    const m = src.match(/async function pickKvSnapshotsFolder[\s\S]*?\n  \}\n/);
    expect(m).not.toBeNull();
    expect(m![0]).toContain('directory: true');
    expect(m![0]).toContain('STRATA_PICK.slot_save_path.title');
  });

  it('an emptied folder writes no key', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'q', slot_save_path: 'D:\\kv' }];
    setStr(providers, 0, 'slot_save_path', '');
    expect('slot_save_path' in providers[0]).toBe(false);
  });
});

describe('the two KV snapshot alias fields', () => {
  it('are shown by one snippet for both engines, with the defaults as placeholders', () => {
    const start = src.indexOf('{#snippet kvSnapshotExtras');
    expect(start).toBeGreaterThan(0);
    const snippet = src.slice(start, src.indexOf('{/snippet}', start));
    expect(snippet).toContain("setNum(i, 'kv_snapshot_ttl_s', e.currentTarget.value)");
    expect(snippet).toContain("setNum(i, 'kv_snapshot_max_gib', e.currentTarget.value, true)");
    expect(snippet).toContain('placeholder={ttlPlaceholder()}');
    expect(snippet).toContain('KV_SNAPSHOT_DEFAULT_MAX_GIB');
    expect(snippet).toContain('isUncPath(folder)');
  });

  it('round-trip: a value is written, an empty field drops the key', () => {
    const providers: Record<string, unknown>[] = [{ alias: 'flash' }];
    expect(setNum(providers, 0, 'kv_snapshot_ttl_s', '3600').kv_snapshot_ttl_s).toBe(3600);
    expect(setNum(providers, 0, 'kv_snapshot_max_gib', '12.5', true).kv_snapshot_max_gib).toBe(12.5);
    setNum(providers, 0, 'kv_snapshot_ttl_s', '');
    setNum(providers, 0, 'kv_snapshot_max_gib', '', true);
    const saved = JSON.parse(JSON.stringify(providers[0]));
    expect('kv_snapshot_ttl_s' in saved).toBe(false);
    expect('kv_snapshot_max_gib' in saved).toBe(false);
  });
});

describe('the Local engines block', () => {
  it('renders the per-alias KV snapshot lines from local_engine_status', () => {
    expect(src).toContain('kv_snapshots: r.kv_snapshots ?? {}');
    expect(src).toContain('kvSnapshotBlocks(engineStatus?.kv_snapshots, Date.now() / 1000)');
  });
});

describe('the old name', () => {
  it('is gone from the editor', () => {
    // Built from parts so this file does not carry the string it forbids.
    const old = ['Session', 'files'].join(' ');
    expect(src).not.toContain(old);
    expect(src).not.toContain(old.toLowerCase());
  });
});
