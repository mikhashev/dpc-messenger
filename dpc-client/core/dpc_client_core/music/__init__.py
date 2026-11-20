"""The core music service (ADR-051): library, watched folders, track identity, playlists,
the lyrics chain, and the job store.

`library.MusicLibrary` is the service, `lyrics.MusicLyrics` its lyrics chain and
`jobs.MusicJobStore` the generation jobs and their takes (`capabilities.MusicCapabilities`
reports what can run: today no engine adapter is built, so a job is recorded `unsupported`).
`commands.MusicCommandsMixin` is their `local_api` door on `CoreService`. The engine
adapter and the job runner are later phases.
"""

from .library import MusicLibrary, MusicLibraryError

__all__ = ["MusicLibrary", "MusicLibraryError"]
