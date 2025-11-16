# TURN Credentials Setup

This guide shows how to configure your Metered.ca TURN server credentials securely.

## Option 1: Environment Variables (Recommended)

Environment variables keep secrets out of your codebase and config files.

### Windows (PowerShell)

```powershell
# Set for current session
$env:DPC_TURN_USERNAME="your_metered_username_here"
$env:DPC_TURN_CREDENTIAL="your_metered_password_here"

# Or set permanently (system-wide)
[System.Environment]::SetEnvironmentVariable('DPC_TURN_USERNAME', 'your_metered_username_here', 'User')
[System.Environment]::SetEnvironmentVariable('DPC_TURN_CREDENTIAL', 'your_metered_password_here', 'User')
```

### Windows (Command Prompt)

```cmd
REM Set for current session
set DPC_TURN_USERNAME=your_metered_username_here
set DPC_TURN_CREDENTIAL=your_metered_password_here

REM Or set permanently
setx DPC_TURN_USERNAME "your_metered_username_here"
setx DPC_TURN_CREDENTIAL "your_metered_password_here"
```

### macOS/Linux (Bash/Zsh)

```bash
# Set for current session
export DPC_TURN_USERNAME="your_metered_username_here"
export DPC_TURN_CREDENTIAL="your_metered_password_here"

# Or add to ~/.bashrc or ~/.zshrc for permanent setup
echo 'export DPC_TURN_USERNAME="your_metered_username_here"' >> ~/.bashrc
echo 'export DPC_TURN_CREDENTIAL="your_metered_password_here"' >> ~/.bashrc
source ~/.bashrc
```

---

## Option 2: Config File

Add TURN credentials to your config file (less secure, but simpler).

**Windows:** `C:\Users\<YourUsername>\.dpc\config.ini`
**macOS/Linux:** `~/.dpc/config.ini`

```ini
[turn]
username = your_metered_username_here
credential = your_metered_password_here
```

**Security Warning:** The config file stores credentials in plain text. Use environment variables for better security.

---

## Getting Your Metered.ca Credentials

1. Go to https://www.metered.ca/
2. Sign up for a free account (100 GB/month free)
3. After signup, you'll receive:
   - **API Key** (use as `username`)
   - **Secret Key** (use as `credential`)

Example from Metered.ca dashboard:

```javascript
var myPeerConnection = new RTCPeerConnection({
  iceServers: [
      {
        urls: "turn:global.relay.metered.ca:80",
        username: "a1b2c3d4e5f6g7h8",  // ← Use this as DPC_TURN_USERNAME
        credential: "supersecretkey123",  // ← Use this as DPC_TURN_CREDENTIAL
      },
  ],
});
```

---

## Testing Your Setup

### Step 1: Set Credentials

Choose one method above and set your credentials.

### Step 2: Run TURN Test

```bash
cd dpc-client/core
poetry run python test_turn.py
```

**Expected output if credentials are configured:**

```
Testing TURN/STUN server connectivity...
============================================================

✓ TURN credentials found: a1b2c3d4...

TURN servers to test:
  • Metered.ca STUN: stun:stun.relay.metered.ca:80
  • Metered.ca TURN (UDP 80): turn:global.relay.metered.ca:80
  • Metered.ca TURN (TCP 80): turn:global.relay.metered.ca:80?transport=tcp
  • Metered.ca TURN (UDP 443): turn:global.relay.metered.ca:443
  • Metered.ca TURN (TLS 443): turns:global.relay.metered.ca:443?transport=tcp

ICE gathering complete - host:5 srflx:1 relay:2

✓ SUCCESS - 2 RELAY candidate(s) obtained!

Working TURN servers:
  ✓ Metered.ca TURN (UDP 80): 165.227.xxx.xxx
```

**Expected output if credentials are NOT configured:**

```
Testing TURN/STUN server connectivity...
============================================================

⚠ Warning: No TURN credentials configured!
  Set environment variables:
    DPC_TURN_USERNAME=your_username
    DPC_TURN_CREDENTIAL=your_password

TURN servers to test:
  • OpenRelay (UDP 80): turn:openrelay.metered.ca:80  (may fail)

❌ ALL TURN servers FAILED - No RELAY candidates!
```

---

## Troubleshooting

### "No TURN credentials configured" Warning

**Cause:** Environment variables not set or config file missing credentials.

**Fix:**
1. Verify you set the environment variables correctly
2. Restart your terminal/PowerShell after setting variables
3. Check the config file has the `[turn]` section

### "RELAY candidates: 0" Even with Credentials

**Possible causes:**
1. **Wrong credentials** - Double-check your Metered.ca dashboard
2. **Firewall blocking** - TURN needs UDP ports 80, 443
3. **Metered.ca quota exceeded** - Check your account usage
4. **Network restrictions** - Some corporate networks block TURN

**Debugging:**
```powershell
# Verify environment variables are set
echo $env:DPC_TURN_USERNAME  # Windows PowerShell
echo $DPC_TURN_USERNAME      # macOS/Linux

# Test UDP connectivity
Test-NetConnection global.relay.metered.ca -Port 80  # Windows
nc -vzu global.relay.metered.ca 80  # macOS/Linux
```

### Credentials Work in Test but Not in Client

**Cause:** Client may be using cached Settings instance.

**Fix:** Restart the dpc-client backend service:
```bash
cd dpc-client/core
# Stop the service (Ctrl+C)
# Start again
poetry run python run_service.py
```

You should see:
```
[WebRTC] Using configured TURN credentials (username: a1b2c3d4...)
```

---

## Security Best Practices

1. **Use environment variables** instead of config files
2. **Never commit credentials** to git
3. **Rotate credentials periodically** (change in Metered.ca dashboard)
4. **Use different credentials** for development vs. production
5. **Monitor usage** in Metered.ca dashboard to detect abuse

---

## Next Steps

After configuring credentials:

1. **Test TURN connectivity:**
   ```bash
   poetry run python test_turn.py
   ```

2. **Start the client:**
   ```bash
   poetry run python run_service.py
   ```

3. **Connect via WebRTC:**
   - Start both Windows and MacOS clients
   - Connect via Hub
   - Check logs for "Using configured TURN credentials"
   - Verify remote AI providers appear!

---

## Related Documentation

- [Metered.ca TURN Setup](https://www.metered.ca/docs/turn-stun/turn)
- [Hub + TURN Integration Guide](../../docs/HUB_TURN_INTEGRATION.md)
- [WebRTC Troubleshooting](../../docs/WEBRTC_SETUP_GUIDE.md)
