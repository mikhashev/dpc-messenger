/**
 * Async-safe confirm dialog.
 *
 * Native `window.confirm()` is non-blocking in Tauri 2.x WebView2 on Windows —
 * the modal renders but JS does not wait for user input, and the call returns
 * truthy immediately. Result: `if (!confirm(...)) return;` guards silently
 * fail, destructive operations execute even when the user clicks Cancel.
 *
 * Use this helper for every destructive confirm. Tauri's plugin-dialog is
 * the primary path; `window.confirm` stays as a last-resort fallback for
 * non-Tauri environments (vite dev server, tests).
 */
export interface ConfirmOptions {
  title?: string;
  kind?: 'info' | 'warning' | 'error';
}

export async function confirmAsync(
  message: string,
  opts: ConfirmOptions = {},
): Promise<boolean> {
  const { title = 'dpc-messenger', kind = 'info' } = opts;
  try {
    const { ask } = await import('@tauri-apps/plugin-dialog');
    return await ask(message, { title, kind });
  } catch {
    return window.confirm(message);
  }
}
