#!/bin/bash
# Debug script to check WebRTC/microphone support on Linux for Tauri app

echo "=== Checking WebRTC Support on Linux ==="
echo ""

echo "1. Checking WebKitGTK version:"
pkg-config --modversion webkit2gtk-4.1 2>/dev/null || pkg-config --modversion webkit2gtk-4.0 2>/dev/null || echo "WebKitGTK not found"
echo ""

echo "2. Checking for WebRTC support in WebKitGTK:"
dpkg -l | grep webkit | grep -i webrtc || echo "webkit2gtk-webrtc not installed"
echo ""

echo "3. Checking xdg-desktop-portal (required for WebRTC on Wayland/X11):"
dpkg -l | grep xdg-desktop-portal || echo "xdg-desktop-portal not installed"
echo ""

echo "4. Checking PulseAudio/PipeWire:"
pactl info 2>/dev/null || echo "PulseAudio/PipeWire not running"
echo ""

echo "5. Checking GStreamer (used by WebKitGTK for media):"
gst-inspect-1.0 --version 2>/dev/null || echo "GStreamer not found"
echo ""

echo "6. Checking for required GStreamer plugins:"
gst-inspect-1.0 pulsesrc 2>/dev/null >/dev/null && echo "✓ pulsesrc (microphone input)" || echo "✗ pulsesrc missing"
gst-inspect-1.0 webrtcdsp 2>/dev/null >/dev/null && echo "✓ webrtcdsp (echo cancellation)" || echo "✗ webrtcdsp missing (optional)"
echo ""

echo "=== Recommended packages to install ==="
echo "sudo apt install -y \\"
echo "  webkit2gtk-4.1 \\"
echo "  gstreamer1.0-plugins-good \\"
echo "  gstreamer1.0-plugins-bad \\"
echo "  gstreamer1.0-pulseaudio \\"
echo "  xdg-desktop-portal \\"
echo "  xdg-desktop-portal-gtk"
echo ""

echo "=== After installing, restart the Tauri app ==="
