#!/bin/bash
# Install WebKitGTK dependencies for Ubuntu 22.04

echo "=== Finding correct WebKitGTK packages for Ubuntu 22.04 ==="

# Check what webkit packages are available
echo "Available webkit2gtk packages:"
apt-cache search webkit2gtk | head -20

echo ""
echo "=== Checking what's already installed ==="
dpkg -l | grep webkit2gtk
dpkg -l | grep gstreamer

echo ""
echo "=== Installing required packages ==="

# Ubuntu 22.04 uses libwebkit2gtk-4.0-dev (not 4.1)
sudo apt update
sudo apt install -y \
  libwebkit2gtk-4.0-dev \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good \
  gstreamer1.0-pulseaudio \
  gstreamer1.0-libav \
  libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev

echo ""
echo "=== Verifying installation ==="
pkg-config --modversion webkit2gtk-4.0 || pkg-config --modversion webkit2gtk-4.1
gst-inspect-1.0 pulsesrc
gst-inspect-1.0 webrtcdsp

echo ""
echo "=== Installation complete! ==="
echo "Please restart the Tauri app to test microphone:"
echo "  cd dpc-client/ui"
echo "  npm run tauri dev"
