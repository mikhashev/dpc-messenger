/**
 * Central notification service for D-PC Messenger
 *
 * Responsibilities:
 * - Check window focus/visibility state
 * - Request/check notification permissions
 * - Send system notifications when app is in background
 * - Provide fallback to in-app toasts when app is foreground
 */

export interface NotificationOptions {
  title: string;
  body: string;
}

let permissionGranted = false;
let notificationModule: any = null;
let windowModule: any = null;

// Check if we're running in Tauri context (Tauri 2.x uses window.isTauri)
function isTauriContext(): boolean {
  return typeof window !== 'undefined' && (
    (window as any).isTauri === true ||   // Tauri 2.x official detection
    !!(window as any).__TAURI__            // Fallback for older versions
  );
}

/**
 * Initialize Tauri modules on load
 */
async function initModules() {
  if (!isTauriContext()) {
    console.log('[Notifications] Not running in Tauri context');
    return;
  }

  if (notificationModule && windowModule) {
    return; // Already initialized
  }

  try {
    console.log('[Notifications] Loading Tauri notification module...');
    notificationModule = await import('@tauri-apps/plugin-notification');
    console.log('[Notifications] Notification module loaded:', Object.keys(notificationModule || {}));

    // Check permission state
    if (notificationModule.isPermissionGranted) {
      permissionGranted = await notificationModule.isPermissionGranted();
      console.log('[Notifications] Permission granted:', permissionGranted);
    }
  } catch (error) {
    console.error('[Notifications] Failed to load notification module:', error);
    notificationModule = null;
  }

  try {
    console.log('[Notifications] Loading Tauri window module...');
    windowModule = await import('@tauri-apps/api/window');
    console.log('[Notifications] Window module loaded');
  } catch (error) {
    console.error('[Notifications] Failed to load window module:', error);
    windowModule = null;
  }
}

// Don't auto-init - wait for explicit calls to ensure Tauri is ready
// initModules();

/**
 * Request notification permission from user
 * @returns true if granted, false if denied
 */
export async function requestNotificationPermission(): Promise<boolean> {
  await initModules(); // Ensure modules are loaded

  if (!notificationModule) {
    console.error('[Notifications] Tauri notification API not available - module failed to load');
    return false;
  }

  if (!notificationModule.requestPermission) {
    console.error('[Notifications] requestPermission function not found in module');
    console.log('[Notifications] Available functions:', Object.keys(notificationModule));
    return false;
  }

  try {
    console.log('[Notifications] Requesting permission...');
    const permission = await notificationModule.requestPermission();
    console.log('[Notifications] Permission result:', permission);
    permissionGranted = permission === 'granted';

    // Store preference in localStorage
    localStorage.setItem('notificationPermission', permission);
    localStorage.setItem('notificationPreference', permissionGranted ? 'enabled' : 'disabled');
    localStorage.setItem('permissionRequestedAt', new Date().toISOString());

    return permissionGranted;
  } catch (error) {
    console.error('[Notifications] Failed to request notification permission:', error);
    return false;
  }
}

/**
 * Check if app window is in background (minimized or not focused)
 */
async function isAppInBackground(): Promise<boolean> {
  await initModules(); // Ensure modules are loaded

  if (!windowModule) {
    // Not in Tauri context or module failed to load
    return false;
  }

  try {
    const appWindow = windowModule.getCurrentWindow();
    const [focused, minimized] = await Promise.all([
      appWindow.isFocused(),
      appWindow.isMinimized()
    ]);

    // App is in background if minimized OR not focused
    return minimized || !focused;
  } catch (error) {
    console.error('[Notifications] Failed to check window state:', error);
    return false;
  }
}

/**
 * Show system notification if app is in background
 *
 * @returns true if system notification was shown, false if app is foreground
 */
export async function showNotificationIfBackground(options: NotificationOptions): Promise<boolean> {
  await initModules(); // Ensure modules are loaded

  // Check user preference
  const preference = localStorage.getItem('notificationPreference');
  if (preference === 'disabled') {
    return false;
  }

  // Check if app is in background
  const inBackground = await isAppInBackground();
  if (!inBackground) {
    return false;
  }

  // Check permission
  if (!permissionGranted) {
    console.warn('[Notifications] Permission not granted');
    return false;
  }

  // Send system notification
  if (!notificationModule) {
    console.error('[Notifications] Tauri notification API not available');
    return false;
  }

  if (!notificationModule.sendNotification) {
    console.error('[Notifications] sendNotification function not found in module');
    return false;
  }

  try {
    await notificationModule.sendNotification({
      title: options.title,
      body: options.body
    });

    console.log(`[Notifications] System notification sent: ${options.title}`);
    return true;
  } catch (error) {
    console.error('[Notifications] Failed to send notification:', error);
    return false;
  }
}

/**
 * Bring app window to foreground
 */
export async function bringAppToForeground(): Promise<void> {
  await initModules(); // Ensure modules are loaded

  if (!windowModule) {
    console.warn('[Notifications] Tauri window API not available');
    return;
  }

  try {
    const appWindow = windowModule.getCurrentWindow();
    await appWindow.show();
    await appWindow.setFocus();
  } catch (error) {
    console.error('[Notifications] Failed to bring app to foreground:', error);
  }
}
