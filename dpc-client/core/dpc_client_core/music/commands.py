"""The `local_api` front door of the music library (ADR-051 D11).

`MusicCommandsMixin` is mixed into `CoreService`; `local_api` dispatches a command by
`getattr(core_service, command)`, so each `async def` here is one command, and each
must also be in `local_api.ALLOWED_COMMANDS`. The agent door (`music_*` tools) will
call the same `MusicLibrary` methods.

Every answer is `{"status": "ok", ...}` or `{"status": "error", "code", "message"}`.

The mixin needs `self.settings` (a `Settings`); the library lives under
`settings.dpc_home_dir / "music"`. The poll interval is `[music] poll_interval_s`
(`DPC_MUSIC_POLL_INTERVAL_S`), read with a default so `settings.py` is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..local_api import slow_command
from .library import DEFAULT_POLL_INTERVAL_S, MusicLibrary, MusicLibraryError

logger = logging.getLogger(__name__)


def _error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, MusicLibraryError):
        return {"status": "error", "code": exc.code, "message": exc.message}
    logger.exception("music command failed")
    return {"status": "error", "code": "internal", "message": str(exc)}


class MusicCommandsMixin:
    """Music library commands for `CoreService`."""

    _music_library_instance: Optional[MusicLibrary] = None

    def _music_poll_interval_s(self) -> float:
        raw = self.settings.get("music", "poll_interval_s", str(DEFAULT_POLL_INTERVAL_S))
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("[music] poll_interval_s %r is not a number; using %s", raw, DEFAULT_POLL_INTERVAL_S)
            return DEFAULT_POLL_INTERVAL_S

    async def _music(self) -> MusicLibrary:
        if self._music_library_instance is None:
            self._music_library_instance = MusicLibrary(
                self.settings.dpc_home_dir, poll_interval_s=self._music_poll_interval_s())
        await self._music_library_instance.start()
        return self._music_library_instance

    async def start_music_library(self) -> None:
        """Load the library and start watching its folders; called at service start."""
        try:
            await self._music()
        except Exception:
            logger.exception("music: the library did not start")

    async def stop_music_library(self) -> None:
        """Stop polling and hashing and write what is pending; called at shutdown."""
        if self._music_library_instance is not None:
            await self._music_library_instance.stop()

    # -- folders -----------------------------------------------------------------

    async def list_music_folders(self) -> Dict[str, Any]:
        try:
            return {"status": "ok", "folders": (await self._music()).list_folders()}
        except Exception as exc:
            return _error(exc)

    async def add_music_folder(self, path: str, watch: bool = True) -> Dict[str, Any]:
        """Add a watched folder; its first scan starts in the background."""
        try:
            folder = await (await self._music()).add_folder(path, watch=watch)
            return {"status": "ok", "folder": folder}
        except Exception as exc:
            return _error(exc)

    async def remove_music_folder(self, folder_id: str) -> Dict[str, Any]:
        """Remove a watched folder and its entries; the files are never touched."""
        try:
            removed = await (await self._music()).remove_folder(folder_id)
            return {"status": "ok", "removed_tracks": removed}
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def rescan_music_folder(self, folder_id: Optional[str] = None) -> Dict[str, Any]:
        """Rescan one folder, or every folder without `folder_id`. Returns when the walk
        is reconciled; hashing of new or changed files goes on in the background."""
        try:
            library = await self._music()
            if folder_id is None:
                return {"status": "ok", "folders": await library.rescan_all()}
            return {"status": "ok", "folder": await library.rescan_folder(folder_id)}
        except Exception as exc:
            return _error(exc)

    # -- library -----------------------------------------------------------------

    async def list_music_library(self, folder_id: Optional[str] = None, include_missing: bool = True) -> Dict[str, Any]:
        try:
            entries = (await self._music()).list_entries(folder_id=folder_id, include_missing=include_missing)
            return {"status": "ok", "entries": entries}
        except Exception as exc:
            return _error(exc)

    async def get_music_track(self, track_id: str) -> Dict[str, Any]:
        """The library entry and its `mp.track/1` (null while the duration is unknown)."""
        try:
            return {"status": "ok", **(await (await self._music()).get_track(track_id))}
        except Exception as exc:
            return _error(exc)

    # -- playlists ---------------------------------------------------------------

    async def list_music_playlists(self) -> Dict[str, Any]:
        try:
            return {"status": "ok", "playlists": (await self._music()).list_playlists()}
        except Exception as exc:
            return _error(exc)

    async def create_music_playlist(self, name: str, track_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        try:
            return {"status": "ok", "playlist": await (await self._music()).create_playlist(name, track_ids)}
        except Exception as exc:
            return _error(exc)

    async def update_music_playlist(self, playlist_id: str, name: Optional[str] = None,
                                    track_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Rename and/or set the full ordered `track_ids` (a reorder is one call)."""
        try:
            playlist = await (await self._music()).update_playlist(playlist_id, name=name, track_ids=track_ids)
            return {"status": "ok", "playlist": playlist}
        except Exception as exc:
            return _error(exc)

    async def delete_music_playlist(self, playlist_id: str) -> Dict[str, Any]:
        try:
            await (await self._music()).delete_playlist(playlist_id)
            return {"status": "ok", "deleted": playlist_id}
        except Exception as exc:
            return _error(exc)


MUSIC_COMMANDS = (
    "list_music_folders",
    "add_music_folder",
    "remove_music_folder",
    "rescan_music_folder",
    "list_music_library",
    "get_music_track",
    "list_music_playlists",
    "create_music_playlist",
    "update_music_playlist",
    "delete_music_playlist",
)
