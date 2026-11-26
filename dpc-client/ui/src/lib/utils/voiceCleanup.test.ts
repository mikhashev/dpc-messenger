/**
 * Regression tests for THE-UI-CANNOT-DELETE-ANY-VOICE-RECORDING-BECAUSE-THE-TAURI-CAPABILITIES-GRANT-NO-FS-REMOVE:
 * - Tauri capabilities grant `fs:allow-remove` scoped strictly to `$HOME/.dpc/temp/**`
 *   (and global `fs:scope` does not widen `remove` to `$HOME/.dpc/**` or outside temp).
 * - `ChatPanel.svelte` captures `filePath` before nulling `voicePreview`, cleans up on
 *   chat switch and unmount, checks Tauri environment, and logs instead of swallowing errors.
 * - `VoiceRecorder.svelte` stops and deletes any in-progress WAV on cancel without calling
 *   `onRecordingComplete` or leaving orphaned recordings on disk.
 */

import { describe, it, expect, vi } from 'vitest';
import defaultCapText from '../../../src-tauri/capabilities/default.json?raw';
import chatPanelSrc from '../panels/ChatPanel.svelte?raw';
import voiceRecorderSrc from '../components/VoiceRecorder.svelte?raw';

type ScopeEntry = string | { path?: string };
type PermissionEntry = string | { identifier: string; allow?: ScopeEntry[]; deny?: ScopeEntry[] };

function entryPath(entry: ScopeEntry): string {
  return typeof entry === 'string' ? entry : (entry.path ?? '');
}

describe('Tauri capabilities: fs:allow-remove scoped to $HOME/.dpc/temp/**', () => {
  const cap = JSON.parse(defaultCapText) as { permissions: PermissionEntry[] };
  const permissions = cap.permissions ?? [];

  it('grants fs:allow-remove as a scoped permission for $HOME/.dpc/temp/** only', () => {
    expect(permissions).not.toContain('fs:allow-remove');

    const removeEntries = permissions.filter(
      (p): p is { identifier: string; allow?: ScopeEntry[] } =>
        typeof p === 'object' && p !== null && p.identifier === 'fs:allow-remove',
    );
    expect(removeEntries.length).toBe(1);

    const allowedPaths = (removeEntries[0].allow ?? []).map(entryPath);
    expect(allowedPaths).toEqual(['$HOME/.dpc/temp/**']);
  });

  it('does not widen fs:allow-remove via global fs:scope outside $HOME/.dpc/temp/**', () => {
    const globalScopes = permissions.filter(
      (p): p is { identifier: string; allow?: ScopeEntry[] } =>
        typeof p === 'object' && p !== null && p.identifier === 'fs:scope',
    );
    for (const scope of globalScopes) {
      const allowedPaths = (scope.allow ?? []).map(entryPath);
      for (const path of allowedPaths) {
        expect(path).toBe('$HOME/.dpc/temp/**');
      }
    }
  });

  it('keeps fs:allow-mkdir, fs:allow-read-file, and fs:allow-write-file allowed for $HOME/.dpc/**', () => {
    for (const id of ['fs:allow-mkdir', 'fs:allow-read-file', 'fs:allow-write-file']) {
      const obj = permissions.find(
        (p): p is { identifier: string; allow?: ScopeEntry[] } =>
          typeof p === 'object' && p !== null && p.identifier === id,
      );
      const hasBare = permissions.includes(id);
      const globalScope = permissions.find(
        (p): p is { identifier: string; allow?: ScopeEntry[] } =>
          typeof p === 'object' && p !== null && p.identifier === 'fs:scope',
      );
      const objPaths = (obj?.allow ?? []).map(entryPath);
      const globalPaths = (globalScope?.allow ?? []).map(entryPath);
      const allowed = obj ? objPaths : (hasBare ? globalPaths : []);
      expect(allowed).toContain('$HOME/.dpc/**');
    }
  });
});

describe('ChatPanel.svelte voice temp cleanup wiring', () => {
  it('captures filePath before clearing voicePreview in handleCancelVoicePreview and logs errors', () => {
    expect(chatPanelSrc).not.toContain('voicePreview!.filePath!');
    expect(chatPanelSrc).not.toMatch(/remove\([^)]*\)\.catch\(\(\)\s*=>\s*\{\s*\}\)/);
    expect(chatPanelSrc).not.toMatch(/await\s+remove\([^)]*\);\s*\}\s*catch\s*\{\s*\/\*\s*ignore\s*\*\/\s*\}/);

    const cancelStart = chatPanelSrc.indexOf('function handleCancelVoicePreview()');
    expect(cancelStart).toBeGreaterThan(-1);
    const cancelBlock = chatPanelSrc.slice(cancelStart, cancelStart + 260);
    expect(cancelBlock).toContain('discardVoicePreview');
  });

  it('removes pending voicePreview temp file on chat switch and component destroy', () => {
    expect(chatPanelSrc).not.toMatch(/if\s*\(voicePreview\s*!==\s*null\)\s*voicePreview\s*=\s*null;/);
    const switchStart = chatPanelSrc.indexOf('// Save / restore draft when chat switches');
    expect(switchStart).toBeGreaterThan(-1);
    const switchBlock = chatPanelSrc.slice(switchStart, switchStart + 650);
    expect(switchBlock).toContain('discardVoicePreview');

    const destroyStart = chatPanelSrc.indexOf('onDestroy(() =>');
    expect(destroyStart).toBeGreaterThan(-1);
    const destroyBlock = chatPanelSrc.slice(destroyStart, destroyStart + 350);
    expect(destroyBlock).toContain('discardVoicePreview');
  });

  it('routes all voice temp-file deletions through voiceCleanup with no silent catches', () => {
    expect(chatPanelSrc).not.toMatch(/import\(['"]@tauri-apps\/plugin-fs['"]\)[^;\n]*remove/);
    expect(chatPanelSrc).not.toContain('await remove(');
  });
});

describe('VoiceRecorder.svelte cancel during recording', () => {
  it('cancelling during recording does not call stopRecording() or emit onRecordingComplete', () => {
    const cancelStart = voiceRecorderSrc.indexOf('async function cancelRecording()');
    expect(cancelStart).toBeGreaterThan(-1);
    const cancelEnd = voiceRecorderSrc.indexOf('function getSupportedMimeType', cancelStart);
    const cancelBlock = voiceRecorderSrc.slice(cancelStart, cancelEnd);

    expect(cancelBlock).not.toMatch(/\bstopRecording\s*\(\s*\)/);
    expect(cancelBlock).toContain('cancelTauriRecording');
    expect(cancelBlock).toContain('discardBrowserRecording = true');
    expect(voiceRecorderSrc).toContain('if (!shouldDiscard)');
  });
});

describe('voiceCleanup helpers', () => {
  it('captures filePath BEFORE clearing preview state and passes captured path to remove()', async () => {
    const { discardVoicePreview } = await import('./voiceCleanup');
    const events: string[] = [];
    let preview: { blob: Blob; duration: number; filePath?: string } | null = {
      blob: new Blob(['wav']),
      duration: 3,
      filePath: '/home/user/.dpc/temp/voice_123.wav',
    };
    const removeFile = vi.fn(async (path: string) => {
      events.push(`remove:${path}`);
    });

    const promise = discardVoicePreview(
      preview,
      () => {
        events.push('clear');
        preview = null;
      },
      { isTauri: () => true, removeFile },
    );

    expect(preview).toBeNull();
    expect(events[0]).toBe('clear');

    const removed = await promise;
    expect(removed).toBe(true);
    expect(removeFile).toHaveBeenCalledTimes(1);
    expect(removeFile).toHaveBeenCalledWith('/home/user/.dpc/temp/voice_123.wav');
    expect(events).toEqual(['clear', 'remove:/home/user/.dpc/temp/voice_123.wav']);
  });

  it('logs errors rather than silently swallowing them when remove() rejects', async () => {
    const { discardVoicePreview } = await import('./voiceCleanup');
    const warn = vi.fn();
    const removeFile = vi.fn().mockRejectedValue(new Error('forbidden path'));

    const removed = await discardVoicePreview(
      { filePath: 'C:\\Users\\user\\.dpc\\temp\\voice_123.wav' },
      () => {},
      { isTauri: () => true, removeFile, warn },
    );

    expect(removed).toBe(false);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain('voice_123.wav');
    expect(warn.mock.calls[0][1]).toBeInstanceOf(Error);
  });

  it('skips fs remove() on non-Tauri browser builds and when filePath is absent', async () => {
    const { removeVoiceTempFile } = await import('./voiceCleanup');
    const removeFile = vi.fn();

    expect(await removeVoiceTempFile('/home/user/.dpc/temp/voice_1.wav', { isTauri: () => false, removeFile })).toBe(false);
    expect(await removeVoiceTempFile(undefined, { isTauri: () => true, removeFile })).toBe(false);
    expect(removeFile).not.toHaveBeenCalled();
  });

  it('cancelTauriRecording stops recording and removes the WAV file, even when stop rejects on a tiny WAV', async () => {
    const { cancelTauriRecording } = await import('./voiceCleanup');
    const removeFile = vi.fn().mockResolvedValue(undefined);
    const warn = vi.fn();

    const normal = await cancelTauriRecording('/home/user/.dpc/temp/voice_100.wav', {
      isTauri: () => true,
      stopRecording: async () => '/home/user/.dpc/temp/voice_100.wav',
      removeFile,
      warn,
    });
    expect(normal).toEqual({ stopped: true, removed: true, path: '/home/user/.dpc/temp/voice_100.wav' });
    expect(removeFile).toHaveBeenCalledWith('/home/user/.dpc/temp/voice_100.wav');

    removeFile.mockClear();
    const shortCancel = await cancelTauriRecording(null, {
      isTauri: () => true,
      stopRecording: async () => {
        throw new Error('Output file is too small (44 bytes): /home/user/.dpc/temp/voice_101.wav');
      },
      getStatus: async () => ({ is_recording: false, output_path: '/home/user/.dpc/temp/voice_101.wav' }),
      removeFile,
      warn,
    });
    expect(shortCancel).toEqual({ stopped: false, removed: true, path: '/home/user/.dpc/temp/voice_101.wav' });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(removeFile).toHaveBeenCalledWith('/home/user/.dpc/temp/voice_101.wav');
  });
});
