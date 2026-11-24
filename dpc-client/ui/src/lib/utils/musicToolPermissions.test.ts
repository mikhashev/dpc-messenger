import { describe, it, expect } from 'vitest';
import { MUSIC_TOOL_CATEGORY, musicToolKeys, OWNER_ONLY_BADGE } from './musicToolPermissions';
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
    expect(panel).toContain('{#if category.ownerOnly}');
    expect(panel).toContain('{OWNER_ONLY_BADGE}');
    expect(OWNER_ONLY_BADGE).toBe('owner-only');
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
