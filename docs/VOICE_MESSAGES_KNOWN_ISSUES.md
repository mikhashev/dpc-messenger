# Voice Messages - Known Issues & Platform Limitations

## Summary

Voice message recording works perfectly in **web browsers** but has platform-specific limitations in **Tauri desktop app** on Linux and macOS due to WebView restrictions.

**Status by Platform:**
- ✅ **Windows**: Works (uses Edge WebView2 with full WebRTC support)
- ⚠️ **Linux**: Partially works (getUserMedia permission issues with WebKitGTK)
- ❌ **macOS**: Blocked (`navigator.mediaDevices` is undefined in WKWebView)

---

## Platform-Specific Issues

### 1. macOS - `navigator.mediaDevices` is Undefined

**Error:**
```javascript
TypeError: undefined is not an object (evaluating 'navigator.mediaDevices.getUserMedia')
```

**Root Cause:**
macOS WKWebView does not expose the `navigator.mediaDevices` API at all, even with proper CSP and permissions configured. This is a fundamental limitation of Apple's WebKit on macOS.

**Why This Happens:**
- WKWebView on macOS has stricter sandboxing than iOS WKWebView
- Apple restricts WebRTC APIs in non-Safari contexts for privacy/security
- `getUserMedia()` requires Apple's MediaDevices entitlement which Tauri apps don't have

**Workarounds:**

**Option 1: Info.plist Configuration (REQUIRED - Added in v0.13.2)**
macOS requires explicit permission descriptions in `src-tauri/Info.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>NSMicrophoneUsageDescription</key>
    <string>D-PC Messenger needs microphone access...</string>
    <key>NSCameraUsageDescription</key>
    <string>D-PC Messenger needs camera access...</string>
</dict>
</plist>
```

**Important:** Tauri 2.x merges this Info.plist file with auto-generated values during build.

**Status:** ✅ Fixed in v0.13.2 - macOS builds now include Info.plist with microphone permissions.

**Note:** macOS 14 shows **double permission prompts** (app-level + webview-level). This is a known macOS behavior.

**Alternative Workarounds:**

**Option 2: Native Rust Plugin**
Create a Tauri plugin using native macOS audio APIs:

```toml
[dependencies]
cpal = "0.15"  # Cross-platform audio I/O
coreaudio-rs = "0.11"  # macOS-specific audio APIs
```

This bypasses WKWebView limitations entirely.

**Option 3: Open System Browser**
For macOS users, open a browser window for voice recording:

```javascript
// Detect macOS WKWebView limitation
if (isMacOS && !navigator.mediaDevices) {
  window.open('http://localhost:1420/voice-recorder', '_blank');
}
```

The browser window has full WebRTC access, records voice, then sends back to main app via WebSocket.

**Status:** Investigating native Rust plugin approach for future releases.

---

### 2. Linux - getUserMedia Permission Denied

**Error:**
```javascript
NotAllowedError: The request is not allowed by the user agent or the platform in the current context, possibly because the user denied permission.
```

**Root Cause:**
WebKitGTK on Linux requires specific system packages and configuration for WebRTC support.

**Required Packages:**
```bash
sudo apt install -y \
  libwebkit2gtk-4.0-dev \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good \
  gstreamer1.0-pulseaudio \
  xdg-desktop-portal-gtk
```

**Why These Are Needed:**
- WebKitGTK uses **GStreamer** for media capture (not native WebRTC like Chromium)
- `gstreamer1.0-pulseaudio` provides microphone input pipeline
- `xdg-desktop-portal-gtk` provides sandboxed permissions on modern Linux

**Verification:**
```bash
# Run debug script
./check_webrtc_support.sh

# Check GStreamer plugins
gst-inspect-1.0 pulsesrc  # Should show microphone input plugin
pactl info  # Should show PulseAudio/PipeWire running
```

**Status:** Works after installing packages, but requires manual setup.

**See:** [docs/LINUX_WEBRTC_LIMITATIONS.md](LINUX_WEBRTC_LIMITATIONS.md) for full troubleshooting guide.

---

## Other Issues Found

### 3. Missing `voice_provider` in Default providers.json

**Issue:**
When `~/.dpc/providers.json` is auto-generated on fresh install, `voice_provider` is set to empty string:

```json
{
  "voice_provider": ""  // Should be "local_whisper_large"
}
```

**Impact:**
Users must manually add local_whisper provider and set voice_provider field.

**Root Cause:**
Default config template in `llm_manager.py:864` doesn't include local_whisper in the providers array.

**Fix:**
Add local_whisper to default providers array when Whisper dependencies are available.

---

### 4. macOS - llvmlite Build Failure

**Error:**
```
CMake Error: Could not find a package configuration file provided by "LLVM"
```

**Root Cause:**
`llvmlite` (dependency of `librosa` → required for Whisper) needs LLVM development libraries, but macOS doesn't have them by default.

**Fix:**
```bash
# Install LLVM via Homebrew
brew install llvm

# Set CMake prefix path
export CMAKE_PREFIX_PATH="/usr/local/opt/llvm:$CMAKE_PREFIX_PATH"
export LLVM_DIR="/usr/local/opt/llvm/lib/cmake/llvm"

# Then retry poetry install
poetry install
```

**Alternative:**
Make Whisper dependencies optional and skip on macOS if LLVM is missing.

---

### 5. Missing Toast Notification for Firewall Errors

**Issue:**
When firewall blocks file transfer, backend logs `PermissionError` but UI doesn't show anything:

```python
WARNING  File transfer not permitted by firewall rules for dpc-node-964c31f280f7a1fd128b8ebb2328c7ad
ERROR    Error processing command: Failed to start voice transfer: File transfer not permitted by firewall rules
```

**Expected Behavior:**
Show toast notification to user: "Voice message blocked by firewall rules. Check Settings → Firewall."

**Fix:**
Catch `PermissionError` in frontend and show toast.

---

### 6. Infinity Loop After Token Limit

**Issue:**
(Need screenshot to analyze - mentioned by user but not provided yet)

**Status:** Pending investigation

---

## Recommendations

### Short-Term (v0.13.2)
1. ✅ **Document platform limitations** (this file)
2. 🔄 **Add local_whisper to default providers.json**
3. 🔄 **Add toast for firewall permission errors**
4. 🔄 **Make llvmlite optional on macOS**

### Medium-Term (v0.14.0)
1. **Create native Rust audio plugin for macOS**
   - Use `cpal` + `coreaudio-rs` for recording
   - Bypass WKWebView limitations entirely
   - Provides best UX

2. **Improve Linux auto-detection**
   - Check for GStreamer plugins on startup
   - Show helpful error if WebRTC unavailable
   - Auto-install script for Ubuntu/Fedora

### Long-Term (v1.0)
1. **Consider Electron build for macOS**
   - Only if Rust plugin is too complex
   - Full WebRTC support guaranteed
   - Trade-off: larger binary size

---

## Testing Matrix

| Platform | Browser | Tauri App | Notes |
|----------|---------|-----------|-------|
| Windows 10/11 | ✅ Chrome/Firefox | ✅ Edge WebView2 | Full support |
| Ubuntu 22.04 | ✅ Chrome/Firefox | ⚠️ WebKitGTK | Requires packages |
| macOS 12+ | ✅ Safari/Chrome | ❌ WKWebView | API undefined |

---

## Developer Notes

### Why Browser Works But Tauri Doesn't

**Browser:**
- Runs with full OS permissions
- Chrome/Firefox have complete WebRTC implementation
- User can explicitly grant microphone permission

**Tauri WebView:**
- Sandboxed application context
- Limited by WebView engine capabilities
- macOS WKWebView: No getUserMedia API
- Linux WebKitGTK: Limited WebRTC (requires GStreamer)

### CSP Configuration

Our CSP is correctly configured for media capture:

```json
{
  "csp": "media-src 'self' mediastream: blob: https://asset.localhost asset:;"
}
```

- `mediastream:` - For getUserMedia (microphone/camera)
- `blob:` - For MediaRecorder blob URLs
- `asset:` - For Tauri asset protocol (playback)

The issue is NOT CSP - it's the underlying WebView engine limitations.

---

## References

### Official Documentation
- [WebKitGTK WebRTC Support](https://webkitgtk.org/reference/webkit2gtk/stable/index.html)
- [Tauri v2 Security Configuration](https://v2.tauri.app/reference/config/)
- [Tauri v2 Permissions System](https://v2.tauri.app/security/permissions/)
- [WKWebView Limitations on macOS](https://developer.apple.com/documentation/webkit/wkwebview)
- [cpal - Cross-platform Audio I/O in Rust](https://github.com/RustAudio/cpal)
- [GStreamer Plugin Documentation](https://gstreamer.freedesktop.org/documentation/plugins_doc.html)

### Community Issues & Discussions
- [Tauri #11951 - macOS Microphone Permission not Prompted](https://github.com/tauri-apps/tauri/issues/11951) (Dec 2024)
- [Tauri #8851 - Linux Permission Problems](https://github.com/tauri-apps/tauri/issues/8851) (Jan 2025)
- [Tauri #8346 - Ubuntu 22.04 getUserMedia NotAllowedError](https://github.com/tauri-apps/tauri/issues/8346)
- [Tauri Discussion #8426 - Functional WebRTC in WebKitGTK](https://github.com/tauri-apps/tauri/discussions/8426)
- [Tauri #9573 - Blob Audio NotSupportedError](https://github.com/tauri-apps/tauri/issues/9573) (Apr 2024)
- [Tauri #5203 - Asset Server Wrong MIME Type](https://github.com/tauri-apps/tauri/issues/5203)

---

## Future Work

- [ ] Implement native Rust audio plugin for macOS
- [ ] Auto-detect WebRTC availability on Linux
- [ ] Add graceful fallback UI when voice unavailable
- [ ] Provide clear error messages for each platform
- [ ] Create platform-specific installation guides
