"""The core music service (ADR-051): library, watched folders, track identity, playlists.

`library.MusicLibrary` is the service; `commands.MusicCommandsMixin` is its `local_api`
door on `CoreService`. Engines, jobs, takes, lyrics and byte serving are later phases.
"""

from .library import MusicLibrary, MusicLibraryError

__all__ = ["MusicLibrary", "MusicLibraryError"]
