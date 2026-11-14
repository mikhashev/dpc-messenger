import { describe, it, expect } from 'vitest';
import {
  describeEvent, formatBytes, formatMs, formatSpan, isUncPath, kvSnapshotBlocks, kvSnapshotLines,
  summaryLine, ttlHuman, ttlPlaceholder, KV_SNAPSHOTS_DISK_HINT, type KvSnapshotsRow,
} from './kvSnapshots';

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

const row = (over: Partial<KvSnapshotsRow> = {}): KvSnapshotsRow => ({
  folder: 'D:\\kv', files: 3, bytes: 12 * GIB, cap_bytes: 32 * GIB, oldest_expiry: 1000 + 23 * 3600,
  last_save: null, last_restore: null, off_reason: null, ...over,
});

describe('isUncPath', () => {
  it('is true for a \\\\server\\share path only', () => {
    expect(isUncPath('\\\\nas\\kv')).toBe(true);
    expect(isUncPath('  \\\\nas\\kv')).toBe(true);
    expect(isUncPath('D:\\kv')).toBe(false);
    expect(isUncPath('/mnt/kv')).toBe(false);
    expect(isUncPath('')).toBe(false);
    expect(isUncPath(undefined)).toBe(false);
  });
});

describe('the TTL words', () => {
  it('shows 86400 as 24 h', () => {
    expect(ttlHuman(86400)).toBe('24 h');
    expect(ttlHuman(3 * 86400)).toBe('3 d');
    expect(ttlHuman(7200)).toBe('2 h');
    expect(ttlHuman(90)).toBe('90 s');
    expect(ttlHuman(600)).toBe('10 min');
    expect(ttlHuman(-1)).toBe('');
  });

  it('the placeholder names the default in seconds and in words', () => {
    expect(ttlPlaceholder()).toBe('86400 (24 h)');
  });
});

describe('sizes and times', () => {
  it('prints binary steps under the plain GiB', () => {
    expect(formatBytes(32 * GIB)).toBe('32 GiB');
    expect(formatBytes(4.05 * GIB)).toBe('4.1 GiB');
    expect(formatBytes(236 * MIB)).toBe('236 MiB');
    expect(formatBytes(5000)).toBe('5 KiB');
    expect(formatBytes(12)).toBe('12 B');
    expect(formatBytes(NaN)).toBe('?');
  });

  it('prints milliseconds as ms or s', () => {
    expect(formatMs(850)).toBe('850 ms');
    expect(formatMs(2100)).toBe('2.1 s');
    expect(formatMs(38000)).toBe('38 s');
    expect(formatMs(120400)).toBe('120 s');
  });

  it('prints a span the way a person reads it', () => {
    expect(formatSpan(10)).toBe('1 min');
    expect(formatSpan(25 * 60)).toBe('25 min');
    expect(formatSpan(3 * 3600 + 20 * 60)).toBe('3 h 20 min');
    expect(formatSpan(3 * 3600)).toBe('3 h');
    expect(formatSpan(23 * 3600)).toBe('23 h');
    expect(formatSpan(5 * 86400)).toBe('5 d');
  });
});

describe('describeEvent', () => {
  it('says bytes and time for a save and a restore', () => {
    expect(describeEvent({ outcome: 'saved', bytes: 4.05 * GIB, ms: 2100, reason: null })).toBe('saved 4.1 GiB in 2.1 s');
    expect(describeEvent({ outcome: 'restored', bytes: 236 * MIB, ms: 900, reason: null })).toBe('restored 236 MiB in 900 ms');
  });

  it('says the reason when nothing was done, and a plain word without one', () => {
    expect(describeEvent({ outcome: 'skipped', bytes: null, ms: null, reason: 'below the size threshold' }))
      .toBe('skipped (below the size threshold)');
    expect(describeEvent({ outcome: 'no_room', bytes: null, ms: null, reason: null })).toBe('no room');
    expect(describeEvent({ outcome: 'engine_refuses', bytes: null, ms: null, reason: null })).toBe('the engine refuses saves');
  });

  it('shows an outcome it does not know as it came', () => {
    expect(describeEvent({ outcome: 'weird', bytes: null, ms: null, reason: null })).toBe('weird');
  });
});

describe('the Local engines lines', () => {
  const now = 1000;

  it('say files, bytes against the cap and when the oldest expires', () => {
    expect(summaryLine(row(), now)).toBe('KV snapshots (agent conversations): 3 files, 12 GiB of 32 GiB · oldest expires in 23 h');
    expect(summaryLine(row({ files: 1 }), now)).toContain('1 file,');
  });

  it('say so when nothing is held, and when the oldest is past its expiry', () => {
    expect(summaryLine(row({ files: 0, bytes: 0, oldest_expiry: null }), now)).toBe('KV snapshots (agent conversations): none held (cap 32 GiB)');
    expect(summaryLine(row({ oldest_expiry: 500 }), now)).toContain('oldest is past its expiry');
    expect(summaryLine(row({ oldest_expiry: null }), now)).not.toContain('oldest');
  });

  it('add the last save and restore, flagging the bad outcomes', () => {
    const lines = kvSnapshotLines(row({
      last_save: { outcome: 'saved', bytes: 4 * GIB, ms: 2000, reason: null },
      last_restore: { outcome: 'refused', bytes: null, ms: null, reason: 'engine version changed' },
    }), now);
    expect(lines.map((l) => l.text)).toEqual([
      'KV snapshots (agent conversations): 3 files, 12 GiB of 32 GiB · oldest expires in 23 h',
      'Last save: saved 4 GiB in 2 s',
      'Last restore: the engine refused it (engine version changed)',
    ]);
    expect(lines.map((l) => l.warn)).toEqual([false, false, true]);
  });

  it('say why saving is off this launch', () => {
    const lines = kvSnapshotLines(row({ off_reason: 'folder missing' }), now);
    expect(lines[lines.length - 1]).toEqual({ text: 'Off this launch: folder missing', warn: true });
  });

  it('list the aliases in order and nothing without a status', () => {
    const blocks = kvSnapshotBlocks({ zeta: row(), alpha: row({ files: 0, bytes: 0, oldest_expiry: null }) }, now);
    expect(blocks.map((b) => b.alias)).toEqual(['alpha', 'zeta']);
    expect(kvSnapshotBlocks(null, now)).toEqual([]);
    expect(kvSnapshotBlocks(undefined, now)).toEqual([]);
  });
});

describe('the disk hint', () => {
  it('is dated and carries the SSD and HDD figures', () => {
    expect(KV_SNAPSHOTS_DISK_HINT).toContain('2026-11-13');
    expect(KV_SNAPSHOTS_DISK_HINT).toContain('SSD');
    expect(KV_SNAPSHOTS_DISK_HINT).toContain('HDD');
  });
});
