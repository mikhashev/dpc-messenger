// Putting text on the clipboard from a button click, shared by the sidebar's cached-peers
// list and the Agent map's process detail.
//
// navigator.clipboard.writeText is what the rest of the app uses; it needs a secure
// context and a user gesture, which the Tauri webviews on all three systems give a
// button click. The textarea + execCommand path is the fallback for a webview that
// refuses the first; if both refuse, the text is still selectable on screen.

/** True when the text reached the clipboard. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
