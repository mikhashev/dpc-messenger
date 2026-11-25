"""Web tracks' audio for the song jobs: the one place the Yandex Music / SoundCloud integration plugs in.

A cover, a same-style song with the source's audio as a reference, or a song from a track's lyrics with
an audio reference sends the source's audio to the engine (`job_schemas.song_needs_audio`). A local or
generated track has its file; a web track has none here until DPC can listen to and download web
tracks, which comes with the streaming integration (backlog card
THE-MUSIC-PLAYER-CANNOT-PLAY-OR-SAVE-SOUNDCLOUD-AND-YANDEX-MUSIC-TRACKS-SO-LISTENER-MODE-IS-LOCAL-ONLY, research in
ai-studio `experiments/music-player/docs/research-streaming-sources.md`). Until then such a job is not a
plain refusal: the door answers the named state `web_source_pending_integration` with `PENDING_MESSAGE`,
and capabilities report `features.web_source_audio` as planned with the same reason.

**The interface.** `can_fetch(entry)` says at the door whether a web track's audio can be had;
`fetch_audio(entry, dest_dir)` gives a local audio file of it at run time (the song job then decodes it
like a local file), raising `WebSourceNotAvailable` with `PENDING_MESSAGE` today. The integration
replaces these two bodies; nothing else in the song jobs changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

PENDING_CODE = "web_source_pending_integration"
PENDING_MESSAGE = (
    "listening to and downloading web tracks will come with the Yandex Music / SoundCloud integration; until "
    "then a web track's audio cannot go to the engine (a cover, or a song with the track's audio as a "
    "reference). A same-style song from the track's description works now"
)


class WebSourceNotAvailable(Exception):
    """A web track's audio cannot be had yet; `code` and `message` are what the door and the job say."""

    code = PENDING_CODE

    def __init__(self, message: str = PENDING_MESSAGE):
        super().__init__(message)
        self.message = message


def integration_status() -> Dict[str, str]:
    """`{status, reason}` of web tracks' audio in the capability report (SPEC §7's words)."""
    return {"status": "planned", "reason": PENDING_MESSAGE}


def can_fetch(entry: Mapping[str, Any]) -> bool:
    """Whether a web track's audio can be had now (always False until the streaming integration)."""
    return False


def fetch_audio(entry: Mapping[str, Any], dest_dir: Path) -> Path:
    """A local audio file of the web track `entry`, written under `dest_dir`; `WebSourceNotAvailable` until
    the streaming integration provides one."""
    raise WebSourceNotAvailable()
