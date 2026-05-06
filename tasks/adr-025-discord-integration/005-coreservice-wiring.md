# Task 005: CoreService Wiring + Config UI (Phase 1-2)

**Status:** TODO
**Phase:** 1-2
**Effort:** ~50 lines
**Depends on:** Task 002

## Description

Wire DiscordService into CoreService lifecycle and add config to settings UI.

## Files

- `dpc_client_core/service.py` — add discord_service init/start/stop (~20 lines)
- `dpc_client_core/settings.py` — add discord config getters (~15 lines)
- `dpc-client/ui/src/lib/panels/` — optional: Discord settings in sidebar (~15 lines)

## Implementation

- CoreService.__init__: create DiscordService if enabled
- CoreService._start_services: await discord_service.start()
- CoreService._shutdown: await discord_service.stop()
- Settings: get_discord_enabled, get_discord_bot_token_env, get_discord_allowed_channels
- UI: optional toggle in sidebar (can defer to Phase 2)

## Done criteria

- Discord bot starts/stops with CoreService
- Config reads from [discord] section in config.ini
- Backend restart enables/disables Discord bot based on config
