# macOS Build Guide

Complete guide for building D-PC Messenger as a distributable macOS application.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Build (Development)](#quick-build-development)
- [Production Build with Bundled Backend](#production-build-with-bundled-backend)
- [Code Signing and Notarization](#code-signing-and-notarization)
- [Build Outputs](#build-outputs)
- [Distribution](#distribution)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Required Software

1. **macOS** (10.15 Catalina or later recommended)

2. **Xcode Command Line Tools**
   ```bash
   xcode-select --install
   ```

3. **Rust**
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   source ~/.cargo/env
   ```

4. **Node.js and npm** (v18+ recommended)
   ```bash
   # Using Homebrew
   brew install node

   # Or download from https://nodejs.org/
   ```

5. **Python 3.12+** and **Poetry**
   ```bash
   # Using Homebrew
   brew install python@3.12

   # Install Poetry
   curl -sSL https://install.python-poetry.org | python3 -
   ```

6. **PyInstaller** (for bundling Python backend)
   ```bash
   pip install pyinstaller
   ```

### Verify Installation

```bash
xcode-select -p         # Should show: /Library/Developer/CommandLineTools
rustc --version         # Should show: rustc 1.x.x
node --version          # Should show: v18.x.x or higher
poetry --version        # Should show: Poetry 1.x.x
pyinstaller --version   # Should show: 6.x.x or higher
```

---

## Quick Build (Development)

**Warning:** This builds the UI only. The Python backend must be run separately.

```bash
cd dpc-client/ui
npm install
npm run tauri build
```

**Output:** `src-tauri/target/release/bundle/dmg/dpc-messenger_0.1.0_x64.dmg`

**Limitation:** Users must manually:
1. Install Python and Poetry
2. Run `poetry run python run_service.py` in `dpc-client/core/`
3. Then launch the app

**Not recommended for distribution.**

---

## Production Build with Bundled Backend

This creates a standalone app with the Python backend included.

### Step 1: Build Python Backend Binary

```bash
cd dpc-client/core

# Install dependencies
poetry install

# Create standalone executable
pyinstaller --onefile run_service.py \
  --name dpc-backend \
  --hidden-import dpc_client_core \
  --hidden-import dpc_client_core.service \
  --hidden-import dpc_client_core.p2p_manager \
  --hidden-import dpc_client_core.webrtc_peer \
  --hidden-import dpc_client_core.hub_client \
  --hidden-import dpc_client_core.llm_manager \
  --hidden-import dpc_client_core.local_api \
  --hidden-import dpc_client_core.firewall \
  --hidden-import dpc_client_core.settings \
  --hidden-import dpc_protocol \
  --collect-all dpc_client_core \
  --collect-all dpc_protocol \
  --collect-all aiortc \
  --collect-all av
```

**Output:** `dist/dpc-backend` (single executable file)

### Step 2: Create Binaries Directory in Tauri

```bash
cd ../ui/src-tauri
mkdir -p binaries
cp ../../core/dist/dpc-backend binaries/dpc-backend-aarch64-apple-darwin
```

**Note:** For universal binaries supporting both Intel and Apple Silicon, create both:
- `dpc-backend-x86_64-apple-darwin` (Intel)
- `dpc-backend-aarch64-apple-darwin` (Apple Silicon)

### Step 3: Update Tauri Configuration

Edit `dpc-client/ui/src-tauri/tauri.conf.json`:

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "dpc-messenger",
  "version": "0.1.0",
  "identifier": "com.mike.dpc-messenger",
  "build": {
    "beforeDevCommand": "npm run dev",
    "devUrl": "http://localhost:1420",
    "beforeBuildCommand": "npm run build",
    "frontendDist": "../build"
  },
  "app": {
    "windows": [
      {
        "title": "dpc-messenger",
        "width": 1000,
        "height": 700,
        "minWidth": 800,
        "minHeight": 600,
        "resizable": true,
        "fullscreen": false,
        "maximizable": true
      }
    ],
    "security": {
      "csp": null
    }
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "externalBin": [
      "binaries/dpc-backend"
    ],
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ]
  }
}
```

**Key addition:** The `externalBin` array tells Tauri to bundle the Python backend.

### Step 4: Update Rust Code to Launch Backend

Edit `dpc-client/ui/src-tauri/src/main.rs`:

```rust
// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use std::sync::Mutex;

struct BackendProcess(Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendProcess(Mutex::new(None)))
        .setup(|app| {
            // Launch Python backend as sidecar
            let sidecar_command = app.shell().sidecar("dpc-backend").unwrap();

            match sidecar_command.spawn() {
                Ok(child) => {
                    println!("Backend process started with PID: {:?}", child.pid());

                    // Store the child process for cleanup
                    let backend_state = app.state::<BackendProcess>();
                    *backend_state.0.lock().unwrap() = Some(child);
                }
                Err(e) => {
                    eprintln!("Failed to start backend: {}", e);
                    // Optionally show error dialog to user
                }
            }

            #[cfg(debug_assertions)]
            {
                if let Some(window) = app.get_webview_window("main") {
                    window.open_devtools();
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Kill backend process when window closes
                if let Some(backend_state) = window.app_handle().try_state::<BackendProcess>() {
                    if let Some(mut child) = backend_state.0.lock().unwrap().take() {
                        let _ = child.kill();
                        println!("Backend process terminated");
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

### Step 5: Update Cargo.toml

Edit `dpc-client/ui/src-tauri/Cargo.toml` to add the shell plugin:

```toml
[dependencies]
tauri = { version = "2", features = [] }
tauri-plugin-opener = "2"
tauri-plugin-dialog = "2"
tauri-plugin-shell = "2"  # Add this line
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

### Step 6: Build the Complete App

```bash
cd dpc-client/ui
npm install
npm run tauri build
```

**This will:**
1. Build the SvelteKit frontend
2. Compile the Rust Tauri wrapper
3. Bundle the Python backend executable
4. Create the `.app` bundle and `.dmg` installer

---

## Build Outputs

After running `npm run tauri build`, find your distributable files:

```
dpc-client/ui/src-tauri/target/release/bundle/
├── dmg/
│   └── dpc-messenger_0.1.0_x64.dmg          # DMG installer (recommended)
│   └── dpc-messenger_0.1.0_aarch64.dmg      # Apple Silicon
└── macos/
    └── dpc-messenger.app                     # Standalone .app bundle
```

**File Sizes:**
- Expect 100-200 MB for the complete bundle (Python runtime included)

**Testing:**
```bash
# Test the .app bundle directly
open src-tauri/target/release/bundle/macos/dpc-messenger.app

# Or mount the DMG
open src-tauri/target/release/bundle/dmg/dpc-messenger_0.1.0_x64.dmg
```

---

## Code Signing and Notarization

**Required for distribution outside of development.**

### Prerequisites

1. **Apple Developer Account** ($99/year)
   - Sign up at https://developer.apple.com

2. **Developer ID Certificate**
   - Download from Apple Developer portal
   - Install in Keychain Access

### Step 1: Find Your Signing Identity

```bash
security find-identity -v -p codesigning
```

Look for: `Developer ID Application: Your Name (TEAM_ID)`

### Step 2: Update tauri.conf.json

```json
{
  "bundle": {
    "active": true,
    "targets": "all",
    "externalBin": ["binaries/dpc-backend"],
    "macOS": {
      "signing": {
        "identity": "Developer ID Application: Your Name (TEAM_ID)",
        "entitlements": null
      }
    },
    "icon": [...]
  }
}
```

### Step 3: Build with Signing

```bash
npm run tauri build
```

Tauri will automatically sign the app during build.

### Step 4: Notarize for macOS 10.15+

**Required** for users to open the app without Gatekeeper warnings.

```bash
# Submit for notarization
xcrun notarytool submit \
  src-tauri/target/release/bundle/dmg/dpc-messenger_0.1.0_x64.dmg \
  --apple-id your@email.com \
  --team-id TEAM_ID \
  --password APP_SPECIFIC_PASSWORD \
  --wait

# Staple the notarization ticket
xcrun stapler staple \
  src-tauri/target/release/bundle/dmg/dpc-messenger_0.1.0_x64.dmg
```

**Create App-Specific Password:**
1. Go to https://appleid.apple.com
2. Sign in → Security → App-Specific Passwords
3. Generate new password

### Step 5: Verify Signing and Notarization

```bash
# Check code signature
codesign -dv --verbose=4 \
  src-tauri/target/release/bundle/macos/dpc-messenger.app

# Check notarization
spctl -a -vv -t install \
  src-tauri/target/release/bundle/dmg/dpc-messenger_0.1.0_x64.dmg
```

Should show: `accepted` and `source=Notarized Developer ID`

---

## Distribution

### Option 1: DMG Installer (Recommended)

**Pros:**
- Standard macOS installation method
- Users drag app to Applications folder
- Can include license agreement, background image

**Upload to:**
- Your website
- GitHub Releases
- Mac App Store (requires additional steps)

### Option 2: ZIP Archive

```bash
cd src-tauri/target/release/bundle/macos/
zip -r dpc-messenger-macos.zip dpc-messenger.app
```

**Pros:**
- Smaller file size
- Easier to host

**Cons:**
- Less user-friendly than DMG

### Option 3: Homebrew Cask

Create a Homebrew formula for easy installation:

```ruby
cask "dpc-messenger" do
  version "0.1.0"
  sha256 "checksum_here"

  url "https://github.com/yourusername/dpc-messenger/releases/download/v#{version}/dpc-messenger_#{version}_x64.dmg"
  name "D-PC Messenger"
  desc "Privacy-first peer-to-peer messaging platform"
  homepage "https://github.com/yourusername/dpc-messenger"

  app "dpc-messenger.app"
end
```

---

## Troubleshooting

### Backend Process Not Starting

**Symptom:** App opens but can't connect to backend (localhost:9999)

**Solutions:**

1. **Check binary permissions:**
   ```bash
   chmod +x src-tauri/binaries/dpc-backend-aarch64-apple-darwin
   ```

2. **Test backend manually:**
   ```bash
   cd dpc-client/core
   ./dist/dpc-backend
   ```

3. **Check PyInstaller hidden imports:**
   - Add missing modules to `--hidden-import` flags
   - Use `--collect-all` for packages with data files

4. **View backend logs:**
   - Add logging to `run_service.py`
   - Check Console.app for crash reports

### App Won't Open (Gatekeeper Block)

**Symptom:** macOS says "App is damaged and can't be opened"

**Solutions:**

1. **Remove quarantine attribute (development only):**
   ```bash
   xattr -cr dpc-messenger.app
   ```

2. **Sign and notarize** (proper solution - see above)

3. **Allow in System Preferences:**
   - System Preferences → Security & Privacy
   - Click "Open Anyway"

### Build Fails with "Command not found: tauri"

**Solution:**
```bash
cd dpc-client/ui
npm install @tauri-apps/cli --save-dev
npm run tauri build
```

### DMG Creation Fails

**Symptom:** `error: failed to bundle project: error running bundle_dmg.sh`

**Solutions:**

1. **Install required tools:**
   ```bash
   brew install create-dmg
   ```

2. **Check disk space:**
   ```bash
   df -h
   ```

3. **Clean and rebuild:**
   ```bash
   rm -rf src-tauri/target
   npm run tauri build
   ```

### Universal Binary (Intel + Apple Silicon)

**To create a universal app:**

1. **Build Python backend for both architectures:**
   ```bash
   # On Intel Mac
   pyinstaller ... --name dpc-backend
   cp dist/dpc-backend binaries/dpc-backend-x86_64-apple-darwin

   # On Apple Silicon Mac
   pyinstaller ... --name dpc-backend
   cp dist/dpc-backend binaries/dpc-backend-aarch64-apple-darwin
   ```

2. **Tauri automatically bundles correct binary per architecture**

3. **Or use Rust cross-compilation** (advanced)

### Notarization Fails

**Common errors:**

1. **"Invalid signature"**
   - Ensure all binaries are signed (including Python backend)
   - Run: `codesign -s "Developer ID" binaries/dpc-backend-*`

2. **"Hardened runtime required"**
   - Add to `tauri.conf.json`:
     ```json
     "macOS": {
       "hardenedRuntime": true
     }
     ```

3. **"Invalid bundle"**
   - Check that `Info.plist` has required keys
   - Tauri handles this automatically

### Python Backend Missing Dependencies

**Symptom:** Backend crashes on launch with `ImportError`

**Solution:** Add to PyInstaller command:
```bash
pyinstaller --onefile run_service.py \
  --hidden-import missing_module \
  --collect-all package_with_data_files
```

**Find missing imports:**
```bash
# Run backend and check error
./dist/dpc-backend

# Add reported modules to PyInstaller command
```

---

## Automated Build Script

Create `dpc-client/scripts/build-macos.sh`:

```bash
#!/bin/bash
set -e

echo "Building D-PC Messenger for macOS..."

# Step 1: Build Python backend
echo "Step 1/3: Building Python backend..."
cd ../core
poetry install
pyinstaller --onefile run_service.py \
  --name dpc-backend \
  --hidden-import dpc_client_core \
  --collect-all dpc_client_core \
  --collect-all dpc_protocol \
  --collect-all aiortc

# Step 2: Copy to Tauri binaries
echo "Step 2/3: Copying backend binary..."
cd ../ui/src-tauri
mkdir -p binaries
cp ../../core/dist/dpc-backend binaries/dpc-backend-$(rustc -Vv | grep host | cut -d' ' -f2)

# Step 3: Build Tauri app
echo "Step 3/3: Building Tauri app..."
cd ..
npm install
npm run tauri build

echo "Build complete!"
echo "DMG: src-tauri/target/release/bundle/dmg/"
echo "App: src-tauri/target/release/bundle/macos/dpc-messenger.app"
```

**Usage:**
```bash
cd dpc-client/scripts
chmod +x build-macos.sh
./build-macos.sh
```

---

## Next Steps

1. **Test the build** on a clean Mac (no development tools)
2. **Set up CI/CD** (GitHub Actions for automated builds)
3. **Create release workflow** (version bumping, changelog)
4. **Consider Mac App Store** (additional requirements apply)

---

## References

- [Tauri Sidecar Documentation](https://tauri.app/v1/guides/building/sidecar)
- [PyInstaller Manual](https://pyinstaller.org/en/stable/)
- [Apple Notarization Guide](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution)
- [Tauri Bundle Configuration](https://tauri.app/v1/api/config/#bundleconfig)

---

## Support

For issues or questions:
- GitHub Issues: [dpc-messenger/issues](https://github.com/yourusername/dpc-messenger/issues)
- Documentation: See `CLAUDE.md` for development setup
