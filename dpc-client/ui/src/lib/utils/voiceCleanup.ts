/**
 * Temp-file and recording lifecycle helpers (`~/.dpc/temp/*`).
 *
 * Keeps handler order explicit (capture `filePath` before clearing preview state),
 * guards against non-Tauri browser builds, tracks browser MediaRecorder cancellation
 * per instance, and logs deletion failures instead of silently swallowing them.
 */

export type VoiceRemoveFn = (path: string) => Promise<void>;

export interface VoiceCleanupDeps {
  isTauri?: () => boolean;
  removeFile?: VoiceRemoveFn;
  warn?: (message: string, error?: unknown) => void;
}

export interface DiscardVoicePreviewDeps extends VoiceCleanupDeps {
  /** When true, clears preview state without deleting the temp file (e.g. while transcription owns it). */
  keepTempFile?: boolean;
}

export interface VoicePreviewLike {
  filePath?: string;
}

export interface CancelTauriRecordingDeps extends VoiceCleanupDeps {
  stopRecording: () => Promise<string | null | undefined>;
  getStatus?: () => Promise<{ is_recording?: boolean; output_path?: string | null } | null | undefined>;
}

export interface BrowserRecorderLike {
  state: string;
  stop(): void;
}

export interface BrowserStreamTrackLike {
  stop(): void;
}

export interface BrowserStreamLike {
  getTracks(): BrowserStreamTrackLike[];
}

export interface BrowserRecordingSession<
  R extends BrowserRecorderLike = BrowserRecorderLike,
  S extends BrowserStreamLike = BrowserStreamLike,
> {
  recorder: R;
  stream: S;
  cancelled: boolean;
  duration: number;
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

/** Build a cross-platform path under `<homeDir>/.dpc/temp/<filename>`. */
export function joinDpcTempPath(homeDir: string, filename: string): string {
  const trimmed = homeDir.replace(/[\\/]+$/, '');
  const sep = trimmed.includes('\\') || /^[A-Za-z]:/.test(trimmed) ? '\\' : '/';
  return `${trimmed}${sep}.dpc${sep}temp${sep}${filename}`;
}

/**
 * Remove a temporary file under `~/.dpc/temp` when running in Tauri.
 * Returns true if `remove` succeeded, false when skipped or failed (logged).
 */
export async function removeTempFile(
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
    log(`[Voice] Failed to remove temp file "${filePath}":`, error);
    return false;
  }
}

/** Alias kept for voice-specific call sites. */
export const removeVoiceTempFile = removeTempFile;

/**
 * Capture `filePath` from `preview`, clear preview state synchronously via
 * `clearPreview()`, then delete the captured temp file unless `keepTempFile` is set.
 */
export function discardVoicePreview<T extends VoicePreviewLike>(
  preview: T | null | undefined,
  clearPreview: () => void,
  deps: DiscardVoicePreviewDeps = {},
): Promise<boolean> {
  const filePath = preview?.filePath;
  clearPreview();
  if (deps.keepTempFile) return Promise.resolve(false);
  return removeTempFile(filePath, deps);
}

/**
 * Clean up a transcribed recording after `transcribe_audio` finishes.
 * Removes the temp file on success (even if chat switching already cleared `voicePreview`)
 * and on failure if the preview is no longer active in the composer; preserves the file
 * only when transcription failed while the user still holds that preview for retry.
 */
export function cleanupTranscribedVoicePreview<T extends VoicePreviewLike>(
  preview: T,
  opts: {
    succeeded: boolean;
    isStillActivePreview: boolean;
    clearActivePreview?: () => void;
  },
  deps: VoiceCleanupDeps = {},
): Promise<boolean> {
  if (opts.succeeded && opts.isStillActivePreview) {
    opts.clearActivePreview?.();
  }
  if (opts.succeeded || !opts.isStillActivePreview) {
    return removeTempFile(preview.filePath, deps);
  }
  return Promise.resolve(false);
}

/** Stop every track on a MediaStream-like object. */
export function stopStreamTracks(stream: BrowserStreamLike | null | undefined): void {
  if (!stream) return;
  for (const track of stream.getTracks()) {
    track.stop();
  }
}

/** Create per-recorder state so delayed `onstop` callbacks never read a subsequent recording's flags. */
export function createBrowserRecordingSession<
  R extends BrowserRecorderLike,
  S extends BrowserStreamLike,
>(recorder: R, stream: S): BrowserRecordingSession<R, S> {
  return { recorder, stream, cancelled: false, duration: 0 };
}

/** Mark a browser recording session cancelled, detach active references, and stop its recorder/stream. */
export function cancelBrowserRecordingSession<
  R extends BrowserRecorderLike,
  S extends BrowserStreamLike,
>(
  session: BrowserRecordingSession<R, S> | null | undefined,
  clearActive: () => void,
): void {
  if (!session) {
    clearActive();
    return;
  }
  session.cancelled = true;
  clearActive();
  if (session.recorder.state !== 'inactive') {
    session.recorder.stop();
  } else {
    stopStreamTracks(session.stream);
  }
}

/** Finalize a browser recorder's `onstop` event for its own session; returns true iff emitted. */
export function finalizeBrowserRecordingSession<
  R extends BrowserRecorderLike,
  S extends BrowserStreamLike,
>(
  session: BrowserRecordingSession<R, S>,
  clearIfCurrent: () => void,
  onComplete: (duration: number) => void,
): boolean {
  const shouldDiscard = session.cancelled;
  stopStreamTracks(session.stream);
  clearIfCurrent();
  if (shouldDiscard) return false;
  onComplete(session.duration);
  return true;
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

  const removed = filePath ? await removeTempFile(filePath, deps) : false;
  return { stopped, removed, path: filePath };
}
