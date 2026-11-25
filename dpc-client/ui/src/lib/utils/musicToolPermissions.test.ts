import { describe, it, expect } from 'vitest';
import { MUSIC_TOOL_CATEGORY, musicToolKeys, OWNER_ONLY_BADGE, categoryOwnerOnly } from './musicToolPermissions';
import { getToolLabel, getToolCategory, getCategoryLabel } from './toolDisplay';
import panel from '../components/AgentPermissionsPanel.svelte?raw';

const NAMES = [
  'music_capabilities', 'music_propose_job', 'music_check', 'music_wait', 'music_cancel_job',
  'music_list_takes', 'music_list_library', 'music_get_lyrics', 'music_playlist_add', 'chat_post_file',
];

describe('the Music category', () => {
  it('lists the nine music_* tools and chat_post_file, once each', () => {
    expect(musicToolKeys()).toEqual(NAMES);
    expect(new Set(musicToolKeys()).size).toBe(10);
  });
  it('is owner-only', () => {
    expect(MUSIC_TOOL_CATEGORY.ownerOnly).toBe(true);
  });
  it('gives every tool a label and a description', () => {
    for (const t of MUSIC_TOOL_CATEGORY.tools) {
      expect(t.label.trim()).not.toBe('');
      expect(t.desc.trim()).not.toBe('');
    }
  });
  it('is in the panel, with the badge, and sets no tool on by itself', () => {
    expect(panel).toContain('MUSIC_TOOL_CATEGORY,');
    expect(panel).toContain('{#if categoryOwnerOnly(category, allRegisteredTools)}');
    expect(panel).toContain('{OWNER_ONLY_BADGE}');
    expect(OWNER_ONLY_BADGE).toBe('asks you for others');
    // A tool is on only through the person's toggle (the registry default is off).
    expect(panel).not.toMatch(/music_[a-z_]+\s*:\s*true/);
  });
  it('is counted among the hardcoded tools, so none of it shows again under "Other Registered Tools"', () => {
    expect(panel).toContain('toolCategories.flatMap(cat => cat.tools.map');
  });
});

describe('the tool display names', () => {
  it('names each tool and puts it under MUSIC', () => {
    for (const name of NAMES) {
      expect(getToolCategory(name)).toBe('music');
      // A real label, not the fallback of the raw name with spaces.
      expect(getToolLabel(name)).not.toBe(name.replace(/_/g, ' '));
    }
    expect(getCategoryLabel('music')).toBe('MUSIC');
  });
});

describe('categoryOwnerOnly', () => {
  const all = (flag: boolean | undefined) => NAMES.map((name) => ({ name, owner_only: flag }));
  it('reads the backend flag over the hardcoded one', () => {
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, all(false))).toBe(false);
    expect(categoryOwnerOnly({ name: 'x', tools: [{ key: 'a', label: 'a', desc: 'a' }] }, [{ name: 'a', owner_only: true }])).toBe(true);
  });
  it('is not owner-only when one reported tool is not', () => {
    const mixed = all(true); mixed[2].owner_only = false;
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, mixed)).toBe(false);
  });
  it('reads owner_approval_for_others (ADR-053) before owner_only', () => {
    const flagged = NAMES.map((name) => ({ name, owner_only: false, owner_approval_for_others: true }));
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, flagged)).toBe(true);
    const unflagged = NAMES.map((name) => ({ name, owner_only: true, owner_approval_for_others: false }));
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, unflagged)).toBe(false);
  });
  it('falls back to the hardcoded flag when the field or the tools are missing', () => {
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, all(undefined))).toBe(true);
    expect(categoryOwnerOnly(MUSIC_TOOL_CATEGORY, [])).toBe(true);
  });
});
