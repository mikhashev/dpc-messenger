/**
 * Central notification service for D-PC Messenger
 *
 * Responsibilities:
 * - Check window focus/visibility state
 * - Request/check notification permissions
 * - Send system notifications when app is in background
 * - Provide fallback to in-app toasts when app is foreground
 */

import { isPermissionGranted, requestPermission, sendNotification } from '@tauri-apps/plugin-notification';
import { getCurrentWindow } from '@tauri-apps/api/window';

export interface NotificationOptions {
  title: string;
  body: string;
}

let permissionGranted = false;

/**
 * Initialize permission state on module load
 */
(async () => {
  try {
    permissionGranted = await isPermissionGranted();
  } catch (error) {
    console.error('Failed to check notification permission:', error);
    permissionGranted = false;
  }
})();

/**
 * Request notification permission from user
 * @returns true if granted, false if denied
 */
export async function requestNotificationPermission(): Promise<boolean> {
  try {
    const permission = await requestPermission();
    permissionGranted = permission === 'granted';

    // Store preference in localStorage
    localStorage.setItem('notificationPermission', permission);
    localStorage.setItem('notificationPreference', permissionGranted ? 'enabled' : 'disabled');
    localStorage.setItem('permissionRequestedAt', new Date().toISOString());

    return permissionGranted;
  } catch (error) {
    console.error('Failed to request notification permission:', error);
    return false;
  }
}

/**
 * Check if app window is in background (minimized or not focused)
 */
async function isAppInBackground(): Promise<boolean> {
  try {
    const appWindow = getCurrentWindow();
    const [focused, minimized] = await Promise.all([
      appWindow.isFocused(),
      appWindow.isMinimized()
    ]);

    // App is in background if minimized OR not focused
    return minimized || !focused;
  } catch (error) {
    console.error('Failed to check window state:', error);
    // Assume foreground on error (safe fallback - show toast instead)
    return false;
  }
}

/**
 * Show system notification if app is in background
 *
 * @returns true if system notification was shown, false if app is foreground
 */
export async function showNotificationIfBackground(options: NotificationOptions): Promise<boolean> {
  // Check user preference
  const preference = localStorage.getItem('notificationPreference');
  if (preference === 'disabled') {
    return false; // User disabled notifications
  }

  // Check if app is in background
  const inBackground = await isAppInBackground();
  if (!inBackground) {
    return false; // App is foreground - caller should show toast instead
  }

  // Check permission
  if (!permissionGranted) {
    console.warn('Notification permission not granted');
    return false;
  }

  // Send system notification
  try {
    await sendNotification({
      title: options.title,
      body: options.body
    });

    console.log(`System notification sent: ${options.title}`);
    return true;
  } catch (error) {
    console.error('Failed to send notification:', error);
    return false;
  }
}

/**
 * Bring app window to foreground
 */
export async function bringAppToForeground(): Promise<void> {
  try {
    const appWindow = getCurrentWindow();
    await appWindow.show();
    await appWindow.setFocus();
  } catch (error) {
    console.error('Failed to bring app to foreground:', error);
  }
}
