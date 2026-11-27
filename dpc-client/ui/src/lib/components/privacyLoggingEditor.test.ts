import { describe, it, expect } from 'vitest';
import editor from './PrivacyLoggingSettingsEditor.svelte?raw';
import core from '../coreService.ts?raw';
import sidebar from './Sidebar.svelte?raw';
import page from '../../routes/+page.svelte?raw';

// No mount harness: the form is checked in the source, the way the other settings-form
// guards do (providersEditorIdleSwap.test.ts). The card is
// THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI.

describe('the privacy and logging form covers the two switches and the level keys', () => {
  it('has the two sections, each with its own save button', () => {
    expect(editor).toContain('<h3>Device information collection</h3>');
    expect(editor).toContain('<h3>Log levels</h3>');
    const saves = editor.match(/sendCommand\('save_privacy_logging_config', \{/g) ?? [];
    expect(saves.length).toBe(2);
  });

  it('carries the two live [system] switches and none of the dead ones', () => {
    for (const id of ['pl-auto-collect', 'pl-collect-ai-models']) {
      expect(editor).toContain(`id="${id}"`);
    }
    // collect_hardware_specs and collect_dev_tools are dead (another card).
    expect(editor).not.toContain('collect_hardware_specs');
    expect(editor).not.toContain('collect_dev_tools');
  });

  it('carries the level keys of [logging] as selects, and the non-level keys not at all', () => {
    for (const id of ['pl-level', 'pl-console-level', 'pl-new-module']) {
      expect(editor).toContain(`id="${id}"`);
    }
    // console/file/max_bytes/backup_count are live keys but neither switches nor
    // levels — a later step of this card, no field yet.
    for (const later of ['pl-console"', 'pl-file', 'pl-max-bytes', 'pl-backup-count']) {
      expect(editor).not.toContain(`id="${later}"`);
    }
  });

  it('says what each collection switch does — what is stored, and to whom it can go', () => {
    // The opt-in must name its scan, its store and its audience, not just "collect".
    expect(editor).toContain('ollama list');
    expect(editor).toContain('software.ai_models');
    expect(editor).toContain('device_context.json');
    expect(editor).toContain('peer that runs inference for you');
  });

  it('says honestly that no key here is applied live — every note names the next start', () => {
    const notes = editor.match(/next (backend start|collection|start)/g) ?? [];
    expect(notes.length).toBeGreaterThanOrEqual(5);
    expect(editor).not.toContain('without a restart');
  });
});

describe('each save sends only its section payload to the one command', () => {
  it('the collection payload has the two switches and nothing else', () => {
    const sends = editor.match(
      /sendCommand\('save_privacy_logging_config', \{\s*config: \{([\s\S]*?)\},\s*\}\)/g) ?? [];
    expect(sends.length).toBe(2);
    expect(sends[0]).toContain('auto_collect_device_info:');
    expect(sends[0]).toContain('collect_ai_models:');
    expect(sends[0]).not.toContain('level:');
  });

  it('the log levels payload has level, console_level and module_levels', () => {
    const sends = editor.match(
      /sendCommand\('save_privacy_logging_config', \{\s*config: \{([\s\S]*?)\},\s*\}\)/g) ?? [];
    expect(sends[1]).toContain('level:');
    expect(sends[1]).toContain('console_level:');
    expect(sends[1]).toContain('module_levels:');
    expect(sends[1]).not.toContain('collect_ai_models:');
  });

  it('checks a new module name client-side and refuses it inline', () => {
    expect(editor).toContain('if (!isModuleName(name)) {');
    expect(editor).toContain('addError = ');
  });

  it('a refused or failed save shows the backend reason beside its section', () => {
    expect((editor.match(/r\?\.message \|\| 'Could not save/g) ?? []).length).toBe(2);
  });
});

describe('the form loads its keys and opens from the sidebar', () => {
  it('loads the get when it opens', () => {
    expect(editor).toContain('$: if (open) loadAll();');
    expect(editor).toContain("'get_privacy_logging_config'");
  });

  it('coreService carries the two new command helpers', () => {
    for (const fn of ['getPrivacyLoggingConfig', 'savePrivacyLoggingConfig']) {
      expect(core).toContain(`export async function ${fn}(`);
    }
  });

  it('the sidebar has the button and the page mounts the editor', () => {
    expect(sidebar).toContain('onOpenPrivacyLogging');
    expect(sidebar).toContain('Privacy and Logging');
    expect(page).toContain('import PrivacyLoggingSettingsEditor from "$lib/components/PrivacyLoggingSettingsEditor.svelte"');
    expect(page).toContain('bind:open={showPrivacyLoggingEditor}');
  });
});
