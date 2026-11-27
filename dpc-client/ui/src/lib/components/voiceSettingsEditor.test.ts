import { describe, it, expect } from 'vitest';
import editor from './VoiceSettingsEditor.svelte?raw';
import core from '../coreService.ts?raw';
import sidebar from './Sidebar.svelte?raw';
import page from '../../routes/+page.svelte?raw';

// No mount harness: the form is checked in the source, the way the other settings-form
// guards do (providersEditorIdleSwap.test.ts). The card is
// THE-VOICE-AND-TRANSCRIPTION-SETTINGS-ARE-EDITABLE-ONLY-BY-HAND-IN-CONFIG-INI.

describe('the voice settings form covers the three sections and their live keys', () => {
  it('has the three sections with their save buttons', () => {
    expect(editor).toContain('<h3>Voice transcription</h3>');
    expect(editor).toContain('<h3>Local transcription</h3>');
    expect(editor).toContain('<h3>Voice messages</h3>');
    expect(editor).toContain("sendCommand('save_voice_transcription_config', {");
    expect(editor).toContain("sendCommand('save_local_transcription_config', {");
    expect(editor).toContain("sendCommand('save_voice_messages_config', {");
  });

  it('carries every live key of [voice_transcription] (the eight of the card)', () => {
    for (const id of ['vt-enabled', 'vt-sender-transcribes', 'vt-recipient-delay', 'vt-timeout',
                      'vt-provider-priority', 'vt-show-transcriber-name', 'vt-cache-transcriptions',
                      'vt-fallback-to-openai']) {
      expect(editor).toContain(`id="${id}"`);
    }
  });

  it('carries the nine live [local_transcription] keys and none of the nine dead ones', () => {
    for (const id of ['lt-enabled', 'lt-fallback-to-openai', 'lt-max-file-size', 'lt-max-duration',
                      'lt-idle-unload', 'lt-queue-wait', 'lt-cpu-max', 'lt-cpu-max-audio',
                      'lt-cpu-contention']) {
      expect(editor).toContain(`id="${id}"`);
    }
    // model/device/compile/flash/chunk/batch/language/task/lazy are dead (another card).
    for (const dead of ['lt-model', 'lt-device', 'lt-compile', 'lt-flash', 'lt-chunk',
                        'lt-batch', 'lt-language', 'lt-task', 'lt-lazy']) {
      expect(editor).not.toContain(`id="${dead}"`);
    }
  });

  it('carries the seven live [voice_messages] keys and not the dead enabled one', () => {
    for (const id of ['vm-max-duration', 'vm-max-size', 'vm-mime-types', 'vm-sample-rate',
                      'vm-channels', 'vm-codec', 'vm-auto-transcribe']) {
      expect(editor).toContain(`id="${id}"`);
    }
    expect(editor).not.toContain('id="vm-enabled"');
  });

  it('says a save takes effect without a restart, which the audit says of every covered key', () => {
    const hints = editor.match(/without a restart/g) ?? [];
    expect(hints.length).toBeGreaterThanOrEqual(3);
    expect(editor).not.toContain('restart is needed');
  });
});

describe('each save sends the parsed payload to its command', () => {
  it('the voice transcription payload has the eight config.ini keys', () => {
    const m = editor.match(/save_voice_transcription_config', \{\s*config: \{([\s\S]*?)\},\s*\}\)/);
    expect(m).not.toBeNull();
    for (const key of ['enabled', 'sender_transcribes', 'recipient_delay_seconds', 'timeout_seconds',
                      'provider_priority', 'show_transcriber_name', 'cache_transcriptions',
                      'fallback_to_openai']) {
      expect(m![1]).toContain(`${key}:`);
    }
  });

  it('the local transcription payload has the nine live keys', () => {
    const m = editor.match(/save_local_transcription_config', \{\s*config: \{([\s\S]*?)\},\s*\}\)/);
    expect(m).not.toBeNull();
    for (const key of ['enabled', 'fallback_to_openai', 'max_file_size_mb', 'max_duration_seconds',
                      'idle_unload_s', 'queue_wait_s', 'cpu_max_s', 'cpu_max_audio_s',
                      'cpu_contention_factor']) {
      expect(m![1]).toContain(`${key}:`);
    }
  });

  it('the voice messages payload has the seven live keys', () => {
    const m = editor.match(/save_voice_messages_config', \{\s*config: \{([\s\S]*?)\},\s*\}\)/);
    expect(m).not.toBeNull();
    for (const key of ['max_duration_seconds', 'max_size_mb', 'mime_types', 'default_sample_rate',
                      'default_channels', 'default_codec', 'auto_transcribe']) {
      expect(m![1]).toContain(`${key}:`);
    }
  });

  it('parses every number and list before the command is sent, refusing a bad one inline', () => {
    const busyOf: Record<string, string> = {
      saveVoiceTranscription: 'vtBusy', saveLocalTranscription: 'ltBusy', saveVoiceMessages: 'vmBusy',
    };
    for (const [fn, busy] of Object.entries(busyOf)) {
      const m = editor.match(new RegExp(`async function ${fn}\\(\\) \\{([\\s\\S]*?)\\n  \\}`));
      expect(m, fn).not.toBeNull();
      const body = m![1];
      const firstSend = body.indexOf('sendCommand');
      expect(firstSend).toBeGreaterThan(0);
      expect(body.indexOf(`${busy} = true`), fn).toBeLessThan(firstSend);
      expect((body.match(/parseWholeNumber|parseFigure|parseFigureAbove|parseNameList/g) ?? []).length)
        .toBeGreaterThan(0);
      // A refused field shows its reason and never reaches the command.
      const refusal = body.match(/Error = '[^']+'/g) ?? [];
      expect(refusal.length, fn).toBeGreaterThan(0);
    }
  });

  it('an empty max audio duration is sent as the legacy empty value, not as a refusal', () => {
    expect(editor).toContain("String(ltDraft.max_duration_seconds ?? '')");
    expect(editor).toContain("maxDurationText === '' ? '' : parseFigure(maxDurationText, 0)");
  });

  it('a refused or failed save shows the backend reason beside the section it belongs to', () => {
    expect((editor.match(/r\?\.message \|\| 'Could not save/g) ?? []).length).toBe(3);
  });
});

describe('the form loads its three sections and opens from the sidebar', () => {
  it('loads the three gets when it opens', () => {
    expect(editor).toContain("$: if (open) loadAll();");
    for (const cmd of ['get_voice_transcription_config', 'get_local_transcription_config',
                       'get_voice_messages_config']) {
      expect(editor).toContain(`'${cmd}'`);
    }
  });

  it('coreService carries the four new command helpers', () => {
    for (const fn of ['getLocalTranscriptionConfig', 'saveLocalTranscriptionConfig',
                      'getVoiceMessagesConfig', 'saveVoiceMessagesConfig']) {
      expect(core).toContain(`export async function ${fn}(`);
    }
  });

  it('the sidebar has the button and the page mounts the editor', () => {
    expect(sidebar).toContain('onOpenVoiceSettings');
    expect(sidebar).toContain('Voice and Transcription');
    expect(page).toContain('import VoiceSettingsEditor from "$lib/components/VoiceSettingsEditor.svelte"');
    expect(page).toContain('bind:open={showVoiceSettingsEditor}');
  });
});
