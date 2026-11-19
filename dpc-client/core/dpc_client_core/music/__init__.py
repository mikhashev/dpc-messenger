"""The core music service (ADR-051): library, watched folders, track identity, playlists,
and the lyrics chain.

`library.MusicLibrary` is the service and `lyrics.MusicLyrics` its lyrics chain;
`commands.MusicCommandsMixin` is their `local_api` door on `CoreService`. Engines, jobs,
takes and byte serving are later phases.
"""

from .library import MusicLibrary, MusicLibraryError

__all__ = ["MusicLibrary", "MusicLibraryError"]
