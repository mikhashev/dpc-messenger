# Linux WebRTC Limitations in Tauri

## Summary

**Issue**: Microphone access (`navigator.mediaDevices.getUserMedia()`) may fail on Linux with `NotAllowedError` in Tauri apps using WebKitGTK, even though it works fine in system browsers like Firefox or Chromium.

**Root Cause**: WebKitGTK (the WebView engine Tauri uses on Linux) has more limited WebRTC support compared to Chromium. It requires additional system packages and proper configuration.

## Current Status (v0.13.1)

- ✅ **Web Browser**: Microphone works perfectly in Firefox/Chromium
- ✅ **Windows**: Tauri app works (uses Edge WebView2)
- ✅ **macOS**: Tauri app works (uses WKWebView)
- ❌ **Linux**: Tauri app may fail with WebKitGTK without proper system setup

## Required System Packages (Ubuntu/Debian)

Run the debug script first to check what's missing:

```bash
cd dpc-messenger
chmod +x check_webrtc_support.sh
./check_webrtc_support.sh
```

Then install required packages:

```bash
sudo apt install -y \
  webkit2gtk-4.1 \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-pulseaudio \
  xdg-desktop-portal \
  xdg-desktop-portal-gtk
```

**Why these packages:**
- `webkit2gtk-4.1` - The WebView engine with WebRTC support
- `gstreamer1.0-*` - Media pipelines for audio/video capture
- `xdg-desktop-portal-gtk` - Sandboxed access to system resources (required for modern Linux desktop permissions)

**After installing, restart the Tauri app.**

## Troubleshooting Steps

### 1. Check WebKitGTK Version
```bash
pkg-config --modversion webkit2gtk-4.1
```
Should be version 2.40+ for better WebRTC support.

### 2. Check GStreamer Plugins
```bash
gst-inspect-1.0 pulsesrc  # Microphone input plugin
gst-inspect-1.0 webrtcdsp  # Echo cancellation (optional)
```

### 3. Check Audio System
```bash
pactl info  # Should show PulseAudio or PipeWire running
```

### 4. Check Portal Status
```bash
systemctl --user status xdg-desktop-portal
```

### 5. Test in System Browser First
Before testing in Tauri app, verify microphone works in your system browser:

**Firefox:**
1. Go to https://mdn.github.io/dom-examples/media/getusermedia/
2. Click "Take photo"
3. Allow microphone permission
4. Verify audio visualizer works

**Chromium:**
1. Same test URL
2. Check chrome://settings/content/microphone shows your device

If microphone doesn't work in system browser, it's a system-level issue.

## Known Limitations

### 1. WebKitGTK WebRTC Support
- **getUserMedia**: Works with proper packages installed
- **Permissions**: Requires xdg-desktop-portal for sandboxed access
- **Audio Constraints**: Limited compared to Chromium (may ignore some MediaStreamConstraints)

### 2. CSP Configuration
Our `tauri.conf.json` CSP is correctly configured:

```json
{
  "csp": "media-src 'self' mediastream: blob: https://asset.localhost asset:;"
}
```

- `mediastream:` - For getUserMedia (microphone/camera)
- `blob:` - For MediaRecorder blob URLs
- `asset:` - For Tauri asset protocol (playback)

### 3. Flatpak/Snap Sandboxing
If using Flatpak or Snap packages, additional permissions may be required:

```bash
# Flatpak
flatpak permission-show

# Snap
snap connections dpc-messenger
```

## Alternative Solutions

If WebKitGTK microphone access continues to fail, consider these alternatives:

### Option 1: Native Audio Capture Plugin
Create a custom Tauri plugin using Rust's `cpal` library for native audio capture:

```toml
[dependencies]
cpal = "0.15"
```

This bypasses WebRTC entirely and uses native OS audio APIs.

### Option 2: Electron Build
As a last resort, consider an Electron build for Linux users where WebKitGTK is problematic. Electron uses Chromium with full WebRTC support.

### Option 3: Browser Extension
For Linux users who can't get Tauri working, provide a web version accessed via Firefox/Chromium.

## Testing Checklist

After installing packages and restarting:

- [ ] Run `check_webrtc_support.sh` - all checks pass
- [ ] Test microphone in Firefox - works
- [ ] Restart Tauri app with `npm run tauri dev`
- [ ] Click microphone button in D-PC Messenger
- [ ] Browser permission dialog appears
- [ ] Click "Allow"
- [ ] Recording starts (duration counter increments)
- [ ] Voice message sends successfully
- [ ] Playback works

## Environment Variables for Debug

Enable GStreamer debug logging:

```bash
export GST_DEBUG=3
export WEBKIT_DEBUG=all
npm run tauri dev
```

This will show detailed GStreamer pipeline errors if audio capture fails.

## Reporting Issues

If microphone still doesn't work after following this guide, please provide:

1. Output of `check_webrtc_support.sh`
2. Ubuntu/Debian version: `lsb_release -a`
3. Desktop environment: `echo $XDG_CURRENT_DESKTOP`
4. Tauri dev console output when clicking microphone button
5. Browser console errors (Right-click → Inspect → Console)
6. Whether microphone works in system Firefox/Chromium

## References

- [WebKitGTK WebRTC Support](https://webkitgtk.org/reference/webkit2gtk/stable/index.html)
- [GStreamer Plugin Documentation](https://gstreamer.freedesktop.org/documentation/plugins_doc.html)
- [xdg-desktop-portal Documentation](https://flatpak.github.io/xdg-desktop-portal/)
- [Tauri v2 Security Configuration](https://v2.tauri.app/reference/config/)
