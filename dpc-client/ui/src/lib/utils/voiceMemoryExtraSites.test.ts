import { describe, it, expect } from 'vitest';
import page from '../../routes/+page.svelte?raw';
import groupSettings from '../components/GroupSettingsDialog.svelte?raw';
import agentPermissions from '../components/AgentPermissionsPanel.svelte?raw';
import sidebar from '../components/Sidebar.svelte?raw';
import providersEditor from '../components/ProvidersEditor.svelte?raw';
import helper from './voiceMemoryExtra.ts?raw';

// Every toggle that needs the voice-memory extra reads its note from the one helper,
// so the text lives once; the helper keys on the status field and on `false` only.
const sites: Array<[string, string]> = [
  ['+page.svelte (Auto Transcribe)', page],
  ['GroupSettingsDialog.svelte (Auto Transcribe)', groupSettings],
  ['AgentPermissionsPanel.svelte (Enable Memory System)', agentPermissions],
  ['Sidebar.svelte (Telegram voice transcription)', sidebar],
  ['ProvidersEditor.svelte (Ollama token counts)', providersEditor],
];

describe('the voice-memory note sits beside each toggle that needs the extra', () => {
  it.each(sites)('%s renders voiceMemoryNote', (_name, src) => {
    expect(src).toMatch(/import \{ voiceMemoryNote \} from ['"]\$lib\/utils\/voiceMemoryExtra['"]/);
    expect(src).toMatch(/\{#if voiceMemoryNote\(/);
  });

  it.each(sites)('%s does not spell the command itself', (_name, src) => {
    expect(src).not.toContain('uv sync --extra voice-memory');
  });

  it('the helper reads the status field and speaks only for an explicit false', () => {
    expect(helper).toContain('voice_memory_installed');
    expect(helper).toMatch(/voice_memory_installed !== false/);
  });
});
