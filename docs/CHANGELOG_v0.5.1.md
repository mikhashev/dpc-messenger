# Changelog - Version 0.5.1

**Release Date:** 2025-11-11
**Status:** Bug Fixes & Configuration Enhancement

## What's New

### Configuration System
- ✨ **NEW:** Centralized configuration via `~/.dpc/config.ini`
- ✨ **NEW:** Environment variable support for all settings
- ✨ **NEW:** Automatic config migration from old formats
- 🔧 Hub URL now fully configurable (no more hardcoded ngrok URL!)
- 🔧 OAuth callback host/port now configurable

### Firewall Enhancements
- ✨ **NEW:** Group-based access rules now functional
- 🎯 Can now define node groups and assign access rules to groups
- ✅ Proper precedence: Node > Group > Hub/AI > Default

### Bug Fixes
- 🐛 Fixed hardcoded Hub URL (was pointing to development server)
- 🐛 Fixed OAuth redirect URL handling (port conflicts)
- 🐛 Fixed missing Group Rules evaluation in firewall
- ✅ Token refresh was already working correctly

---

## Configuration

### Environment Variables (NEW!)

All settings now support environment variable overrides:

```bash
export DPC_HUB_URL=https://hub.example.com
export DPC_OAUTH_CALLBACK_PORT=8080
export DPC_P2P_LISTEN_PORT=8888
```

### Config File (NEW!)

Default location: `~/.dpc/config.ini`

```ini
[hub]
url = http://localhost:8000
auto_connect = true

[oauth]
callback_port = 8080
callback_host = 127.0.0.1

[p2p]
listen_port = 8888
listen_host = 0.0.0.0

[api]
port = 9999
host = 127.0.0.1
```

📖 **Full Documentation:** [Configuration Guide](./CONFIGURATION.md)

---

## Group Rules (NEW!)

You can now define groups of nodes and assign access rules:

**`~/.dpc/.dpc_access`:**
```ini
# Define node groups
[node_groups]
colleagues = dpc-node-alice-123, dpc-node-bob-456
friends = dpc-node-charlie-789

# Define access rules for groups
[group:colleagues]
work_main.json:availability = allow
work_main.json:skills.* = allow

[group:friends]
personal.json:profile.* = allow
```

---

## Migration Guide

### From v0.5.0 to v0.5.1

#### 1. Configuration Migration (Automatic)

If you have an old `~/.dpc/config.ini` with just:
```
url = https://your-hub-url
```

It will be **automatically migrated** to the new format on first run:
- Old config backed up to `config.ini.bak`
- New config created with proper sections
- Your Hub URL preserved

#### 2. Using Environment Variables (Optional)

If you want to use environment variables instead:

```bash
# Add to your ~/.bashrc or ~/.zshrc
export DPC_HUB_URL=https://your-hub-url
```

#### 3. Group Rules (Optional)

If you want to use the new group rules feature, edit `~/.dpc/.dpc_access`:

1. Add `[node_groups]` section
2. Add `[group:groupname]` sections with rules

---

## Breaking Changes

**None** - This release is fully backward compatible.

Old configuration formats are automatically migrated.

---

## Files Changed

### New Files
- `dpc-client/core/dpc_client_core/settings.py` - Configuration management
- `docs/CONFIGURATION.md` - Configuration guide
- `FIXES_SUMMARY.md` - Technical details

### Modified Files
- `dpc-client/core/dpc_client_core/service.py` - Use Settings
- `dpc-client/core/dpc_client_core/hub_client.py` - Configurable OAuth
- `dpc-client/core/dpc_client_core/firewall.py` - Group rules implementation

---

## Testing

All tests pass:
```
[PASS] Hub tests passed.
[PASS] AI Scope tests passed.
[PASS] Node tests passed.
[PASS] Group tests passed.
[PASS] Default deny test passed.
[PASS] Settings tests passed.
```

---

## Upgrade Instructions

### For Users

```bash
# Pull latest code
git pull

# Restart the client - config will auto-migrate
cd dpc-client/core
poetry run python run_service.py
```

### For Developers

```bash
# Pull latest code
git pull

# Update dependencies (if any)
cd dpc-client/core
poetry install

# Run tests
poetry run python -m dpc_client_core.firewall
poetry run python dpc_client_core/settings.py
```

### For Hub Operators

No changes required - this is a client-side release only.

---

## Documentation Updates

- 📖 **NEW:** [Configuration Guide](./CONFIGURATION.md)
- 📝 Updated: [Fixes Summary](../FIXES_SUMMARY.md)
- 📝 Suggested: [Quick Start Guide](./QUICK_START.md) - Add configuration section
- 📝 Suggested: [Main README](../README.md) - Add configuration reference

---

## Known Issues

None reported.

---

## Contributors

- @mikhashev (Mike) - Configuration system, group rules, bug fixes
- AI Assistant (Claude) - Code analysis, implementation assistance

---

## Next Steps

**Recommended for v0.5.2:**
1. Add settings management UI in Tauri app
2. Add integration tests for Settings module
3. Implement Remote Inference (EPIC-15)
4. Add message persistence

---

## References

- [FIXES_SUMMARY.md](../FIXES_SUMMARY.md) - Technical details
- [Configuration Guide](./CONFIGURATION.md) - Full configuration docs
- [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)

---

**Status:** Production Ready ✅
