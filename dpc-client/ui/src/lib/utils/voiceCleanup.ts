/**
 * Voice recording temp-file cleanup helpers (`~/.dpc/temp/voice_*.wav`).
 *
 * Keeps handler order explicit (capture `filePath` before clearing preview state),
 * guards against non-Tauri browser builds, and logs deletion failures instead of
 * silently swallowing them.
 */

export type VoiceRemoveFn = (path: string) => Promise<void>;

export interface VoiceCleanupDeps {
  isTauri?: () => boolean;
  removeFile?: VoiceRemoveFn;
  warn?: (message: string, error?: unknown) => void;
}

export interface VoicePreviewLike {
  filePath?: string;
}

export interface CancelTauriRecordingDeps extends VoiceCleanupDeps {
  stopRecording: () => Promise<string | null | undefined>;
  getStatus?: () => Promise<{ is_recording?: boolean; output_path?: string | null } | null | undefined>;
}

/** Detect whether the UI is running inside a Tauri desktop webview. */
export function isTauriEnvironment(): boolean {
  return (
    typeof window !== 'undefined' &&
    ((window as any).isTauri === true || !!(window as any).__TAURI__)
  );
}

function defaultLogError(message: string, error?: unknown): void {
  console.warn(message, error);
}

/**
 * Remove a temporary voice recording file when running in Tauri.
 * Returns true if `remove` succeeded, false when skipped or failed (logged).
 */
export async function removeVoiceTempFile(
  filePath: string | null | undefined,
  deps: VoiceCleanupDeps = {},
): Promise<boolean> {
  if (!filePath) return false;
  const inTauri = deps.isTauri ? deps.isTauri() : isTauriEnvironment();
  if (!inTauri) return false;

  try {
    const removeFile = deps.removeFile ?? (await import('@tauri-apps/plugin-fs')).remove;
    await removeFile(filePath);
    return true;
  } catch (error) {
    const log = deps.warn ?? defaultLogError;
    log(`[Voice] Failed to remove temp voice file "${filePath}":`, error);
    return false;
  }
}

/**
 * Capture `filePath` from `preview`, clear preview state synchronously via
 * `clearPreview()`, then delete the captured temp file if present.
 */
export function discardVoicePreview<T extends VoicePreviewLike>(
  preview: T | null | undefined,
  clearPreview: () => void,
  deps: VoiceCleanupDeps = {},
): Promise<boolean> {
  const filePath = preview?.filePath;
  clearPreview();
  return removeVoiceTempFile(filePath, deps);
}

/**
 * Stop an active Tauri voice recording on user cancel and remove its WAV file.
 * Falls back to `knownOutputPath` / `getStatus()` if `stopRecording` rejects
 * (e.g., when cancelling before enough audio frames were written).
 */
export async function cancelTauriRecording(
  knownOutputPath: string | null | undefined,
  deps: CancelTauriRecordingDeps,
): Promise<{ stopped: boolean; removed: boolean; path: string | null }> {
  const log = deps.warn ?? defaultLogError;
  let filePath = knownOutputPath ?? null;
  let stopped = false;

  try {
    const stoppedPath = await deps.stopRecording();
    stopped = true;
    if (stoppedPath) filePath = stoppedPath;
  } catch (error) {
    log('[VoiceRecorder] Error stopping cancelled recording:', error);
    if (!filePath && deps.getStatus) {
      try {
        const status = await deps.getStatus();
        if (status?.output_path) filePath = status.output_path;
      } catch (statusError) {
        log('[VoiceRecorder] Error querying cancelled recording status:', statusError);
      }
    }
  }

  const removed = filePath ? await removeVoiceTempFile(filePath, deps) : false;
  return { stopped, removed, path: filePath };
}
