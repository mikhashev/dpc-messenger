# Task 005: CoreService Wiring + Settings UI (Phase 1-2)

**Status:** TODO
**Phase:** 1-2
**Effort:** ~80 lines backend + ~50 lines frontend
**Depends on:** Task 002

## Description

Wire DiscordService into CoreService lifecycle and add Discord settings to UI Settings Panel — same pattern as Telegram settings (not manual config.ini editing).

## Files

- `dpc_client_core/service.py` — add discord_service init/start/stop (~20 lines)
- `dpc_client_core/settings.py` — add discord config getters/setters (~20 lines)
- `dpc_client_core/local_api.py` — add get/save_discord_config WS commands (~20 lines)
- `dpc-client/ui/src/lib/panels/` — Discord settings section in Settings Panel (~50 lines)

## Implementation

### Backend
- CoreService.__init__: create DiscordService if enabled
- CoreService._start_services: await discord_service.start()
- CoreService._shutdown: await discord_service.stop()
- Settings: get/set for discord_enabled, bot_token, server_id, allowed_channel_ids, morning_brief_channel_id
- WS commands: get_discord_config, save_discord_config (same pattern as Telegram)

### Frontend (Settings UI Panel)
- Discord section in Settings Panel, analogous to Telegram section
- Fields:
  - Bot Token (password input)
  - Server ID (text input)
  - Allowed Channel IDs (textarea, JSON array)
  - Morning Brief Channel ID (text input)
  - Enabled toggle
- Save triggers backend reload (hot-reload, same as Telegram)

## Done criteria

- Discord bot starts/stops with CoreService
- Settings configurable through UI (not manual config.ini)
- Hot-reload on save (no backend restart needed)
- Backend restart enables/disables Discord bot based on saved config
