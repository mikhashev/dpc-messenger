"""The shapes of the music library's records, checked in code (no schema library).

The schemas are owned by the music player SPEC (`ai-studio/experiments/music-player/
SPEC.md` §4): `mp.library_entry/1`, `mp.music_folder/1`, `mp.playlist/1`,
`mp.track/1` and `mp.lyrics/1`. This module checks the subset of each that the core writes: required
keys, no undeclared keys, types, enums, id patterns, and the conditional rules
(`allOf`) that apply to entries the core creates.

Four keys on `mp.library_entry/1` are not in the SPEC yet and are declared here as
extensions, because ADR-051 Q4 and the metadata scope need them:

- `hash_kind`: how `content_hash` was computed (`HASH_KINDS`). Two hashes compare
  only when their kinds are equal: a whole-file hash and an audio-payload hash of the
  same file differ.
- `artist`, `album`: read with ffprobe when it is present.
- `hash_notes`: short strings saying why the hash is the kind it is (`"ffmpeg
  absent"`, `"ffmpeg could not read the audio"`); empty for a normal hash.

They are optional, so an entry without them is valid against both shapes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping

LIBRARY_ENTRY = "mp.library_entry/1"
MUSIC_FOLDER = "mp.music_folder/1"
PLAYLIST = "mp.playlist/1"
TRACK = "mp.track/1"
LYRICS = "mp.lyrics/1"
COMMENT = "mp.comment/1"

TRACK_ID_RE = re.compile(r"^trk_[0-9a-f]{16}$")
FOLDER_ID_RE = re.compile(r"^mf_[0-9a-f]{16}$")
PLAYLIST_ID_RE = re.compile(r"^pl_")

# How a content hash was made (ADR-051 Q4, layer 3).
HASH_KIND_FLAC = "flac_streaminfo"   # the MD5 a FLAC encoder stores over the decoded audio
HASH_KIND_AUDIO = "audio_stream"     # SHA-256 over an ffmpeg stream copy of the audio packets
HASH_KIND_FILE = "file"              # SHA-256 over the whole file: the fallback without ffmpeg
HASH_KINDS = (HASH_KIND_FLAC, HASH_KIND_AUDIO, HASH_KIND_FILE)

SOURCE_KINDS = ("local", "web", "generated")
ORIGINS = ("folder", "import", "web", "generated", "recording")
HASH_STATES = ("pending", "done", "error")
PERSIST_VALUES = ("full", "metadata_only")
WATCH_STATES = ("watching", "polling", "off", "unavailable")
PLAYLIST_KINDS = ("user", "smart")
SMART_RULES = ("all", "generated")

_SOURCE_KEYS = {"kind", "name", "uri", "size_bytes", "mime", "lineage"}
_SOURCE_REQUIRED = ("kind", "name", "uri")

_ENTRY_REQUIRED = (
    "schema", "id", "title", "source", "source_hash", "content_hash",
    "duration_s", "added_at", "persist", "placeholder",
)
_ENTRY_KEYS = set(_ENTRY_REQUIRED) | {
    "needs_readd", "origin", "path", "folder_id", "alt_paths", "file_stat",
    "hash_state", "missing", "hidden", "deletable",
    # extensions, see the module docstring
    "hash_kind", "artist", "album", "hash_notes",
}

_FOLDER_REQUIRED = ("schema", "id", "path", "added_at", "last_scan_at", "watch", "file_count")
_FOLDER_KEYS = set(_FOLDER_REQUIRED) | {"hashed_count", "watch_state", "state_reason"}

_PLAYLIST_REQUIRED = ("schema", "id", "name", "track_ids", "created_at", "updated_at", "kind")
_PLAYLIST_KEYS = set(_PLAYLIST_REQUIRED) | {"rule"}

_TRACK_REQUIRED = ("schema", "id", "source", "source_hash", "editable", "duration_s")
_TRACK_KEYS = set(_TRACK_REQUIRED) | {
    "content_hash", "sample_rate", "sample_rate_basis", "channels", "loaded_at",
}


COMMENT_ID_RE = re.compile(r"^cmt_[0-9a-f]{16}$")
COMMENT_KINDS = ("point", "region")
COMMENT_ACTIONS = ("note", "regenerate_region", "extend_ending", "fix_seam", "remove_region")
COMMENT_AUTHOR_KINDS = ("human", "agent")

_COMMENT_REQUIRED = ("schema", "id", "track_id", "version_id", "kind", "t_start", "t_end", "text",
                     "action", "author", "created_at")
_COMMENT_KEYS = set(_COMMENT_REQUIRED) | {"sample_rate", "anchor", "status", "updated_at"}


class SchemaError(ValueError):
    """A record does not have the shape its schema declares."""


def _fail(schema: str, message: str) -> None:
    raise SchemaError(f"{schema}: {message}")


def _check_keys(schema: str, record: Any, required: Iterable[str], allowed: set) -> None:
    if not isinstance(record, Mapping):
        _fail(schema, f"expected an object, got {type(record).__name__}")
    missing = [k for k in required if k not in record]
    if missing:
        _fail(schema, f"missing required key(s): {', '.join(missing)}")
    extra = sorted(set(record) - allowed)
    if extra:
        _fail(schema, f"undeclared key(s): {', '.join(extra)}")
    if record.get("schema") != schema:
        _fail(schema, f"'schema' must be {schema!r}, got {record.get('schema')!r}")


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _expect(schema: str, ok: bool, message: str) -> None:
    if not ok:
        _fail(schema, message)


def _check_source(schema: str, source: Any) -> None:
    _expect(schema, isinstance(source, Mapping), "'source' must be an object")
    missing = [k for k in _SOURCE_REQUIRED if k not in source]
    _expect(schema, not missing, f"'source' is missing {', '.join(missing)}")
    extra = sorted(set(source) - _SOURCE_KEYS)
    _expect(schema, not extra, f"'source' has undeclared key(s): {', '.join(extra)}")
    _expect(schema, source["kind"] in SOURCE_KINDS, f"'source.kind' {source['kind']!r} is not one of {SOURCE_KINDS}")
    _expect(schema, _is_str(source["name"]) and _is_str(source["uri"]), "'source.name' and 'source.uri' must be strings")
    if "size_bytes" in source:
        _expect(schema, _is_int(source["size_bytes"]) and source["size_bytes"] >= 0, "'source.size_bytes' must be an integer >= 0")
    if "mime" in source:
        _expect(schema, source["mime"] is None or _is_str(source["mime"]), "'source.mime' must be a string or null")
    if source["kind"] == "generated":
        _expect(schema, isinstance(source.get("lineage"), Mapping), "a generated source needs 'lineage'")


def validate_library_entry(entry: Any) -> None:
    """Raise `SchemaError` unless `entry` has the shape of `mp.library_entry/1`."""
    s = LIBRARY_ENTRY
    _check_keys(s, entry, _ENTRY_REQUIRED, _ENTRY_KEYS)
    _expect(s, _is_str(entry["id"]) and bool(TRACK_ID_RE.match(entry["id"])), f"'id' {entry['id']!r} does not match trk_ + 16 hex")
    _expect(s, _is_str(entry["title"]), "'title' must be a string")
    _check_source(s, entry["source"])
    _expect(s, _is_str(entry["source_hash"]), "'source_hash' must be a string")
    _expect(s, entry["content_hash"] is None or _is_str(entry["content_hash"]), "'content_hash' must be a string or null")
    d = entry["duration_s"]
    _expect(s, d is None or (_is_number(d) and d >= 0), "'duration_s' must be a number >= 0 or null")
    _expect(s, _is_str(entry["added_at"]), "'added_at' must be a string")
    _expect(s, entry["persist"] in PERSIST_VALUES, f"'persist' must be one of {PERSIST_VALUES}")
    _expect(s, entry["placeholder"] is None, "'placeholder' is prototype-only; the core writes null")
    if "needs_readd" in entry:
        _expect(s, isinstance(entry["needs_readd"], bool), "'needs_readd' must be a boolean")
    if "origin" in entry:
        _expect(s, entry["origin"] in ORIGINS, f"'origin' must be one of {ORIGINS}")
    if "path" in entry:
        _expect(s, entry["path"] is None or _is_str(entry["path"]), "'path' must be a string or null")
    if "folder_id" in entry and entry["folder_id"] is not None:
        _expect(s, _is_str(entry["folder_id"]) and bool(FOLDER_ID_RE.match(entry["folder_id"])), "'folder_id' does not match mf_ + 16 hex")
    if "alt_paths" in entry:
        _expect(s, isinstance(entry["alt_paths"], list) and all(_is_str(p) for p in entry["alt_paths"]), "'alt_paths' must be a list of strings")
    if "file_stat" in entry:
        fs = entry["file_stat"]
        _expect(s, isinstance(fs, Mapping) and set(fs) == {"size_bytes", "mtime"}, "'file_stat' must be {size_bytes, mtime}")
        _expect(s, _is_int(fs["size_bytes"]) and fs["size_bytes"] >= 0 and _is_str(fs["mtime"]), "'file_stat' has the wrong types")
    if "hash_state" in entry:
        _expect(s, entry["hash_state"] in HASH_STATES, f"'hash_state' must be one of {HASH_STATES}")
        if entry["hash_state"] == "pending":
            _expect(s, entry["content_hash"] is None, "a pending entry has content_hash null")
    if "missing" in entry:
        _expect(s, isinstance(entry["missing"], bool), "'missing' must be a boolean")
    if "hidden" in entry:
        _expect(s, isinstance(entry["hidden"], bool), "'hidden' must be a boolean")
    if "hash_kind" in entry:
        _expect(s, entry["hash_kind"] is None or entry["hash_kind"] in HASH_KINDS, f"'hash_kind' must be one of {HASH_KINDS} or null")
    if "hash_notes" in entry:
        _expect(s, isinstance(entry["hash_notes"], list) and all(_is_str(n) for n in entry["hash_notes"]),
                "'hash_notes' must be a list of strings")
    for key in ("artist", "album"):
        if key in entry:
            _expect(s, entry[key] is None or _is_str(entry[key]), f"'{key}' must be a string or null")
    origin = entry.get("origin")
    if origin == "folder":
        _expect(s, _is_str(entry.get("path")) and _is_str(entry.get("folder_id")), "a folder entry needs 'path' and 'folder_id'")
        _expect(s, entry["source"]["kind"] == "local", "a folder entry's source is local")
    elif origin in ("import", "recording"):
        _expect(s, entry["source"]["kind"] == "local", f"an {origin} entry's source is local")
    elif origin == "web":
        _expect(s, entry.get("path") is None and entry["content_hash"] is None and entry["source"]["kind"] == "web",
                "a web entry has path null, content_hash null and a web source")
    elif origin == "generated":
        _expect(s, entry["source"]["kind"] == "generated", "a generated entry's source is generated")


def validate_music_folder(folder: Any) -> None:
    """Raise `SchemaError` unless `folder` has the shape of `mp.music_folder/1`."""
    s = MUSIC_FOLDER
    _check_keys(s, folder, _FOLDER_REQUIRED, _FOLDER_KEYS)
    _expect(s, _is_str(folder["id"]) and bool(FOLDER_ID_RE.match(folder["id"])), f"'id' {folder['id']!r} does not match mf_ + 16 hex")
    _expect(s, _is_str(folder["path"]), "'path' must be a string")
    _expect(s, _is_str(folder["added_at"]), "'added_at' must be a string")
    _expect(s, folder["last_scan_at"] is None or _is_str(folder["last_scan_at"]), "'last_scan_at' must be a string or null")
    _expect(s, isinstance(folder["watch"], bool), "'watch' must be a boolean")
    _expect(s, _is_int(folder["file_count"]) and folder["file_count"] >= 0, "'file_count' must be an integer >= 0")
    if "hashed_count" in folder:
        _expect(s, _is_int(folder["hashed_count"]) and folder["hashed_count"] >= 0, "'hashed_count' must be an integer >= 0")
    if "watch_state" in folder:
        allowed = ("watching", "polling", "unavailable") if folder["watch"] else ("off", "unavailable")
        _expect(s, folder["watch_state"] in allowed, f"'watch_state' must be one of {allowed} when watch is {folder['watch']}")
    if "state_reason" in folder:
        _expect(s, folder["state_reason"] is None or _is_str(folder["state_reason"]), "'state_reason' must be a string or null")


def validate_playlist(playlist: Any) -> None:
    """Raise `SchemaError` unless `playlist` has the shape of `mp.playlist/1`."""
    s = PLAYLIST
    _check_keys(s, playlist, _PLAYLIST_REQUIRED, _PLAYLIST_KEYS)
    _expect(s, _is_str(playlist["id"]) and bool(PLAYLIST_ID_RE.match(playlist["id"])), "'id' must start with pl_")
    _expect(s, _is_str(playlist["name"]), "'name' must be a string")
    ids = playlist["track_ids"]
    _expect(s, isinstance(ids, list) and all(_is_str(t) and TRACK_ID_RE.match(t) for t in ids),
            "'track_ids' must be a list of trk_ + 16 hex ids")
    _expect(s, _is_str(playlist["created_at"]) and _is_str(playlist["updated_at"]), "'created_at' and 'updated_at' must be strings")
    _expect(s, playlist["kind"] in PLAYLIST_KINDS, f"'kind' must be one of {PLAYLIST_KINDS}")
    if "rule" in playlist:
        _expect(s, playlist["rule"] in SMART_RULES, f"'rule' must be one of {SMART_RULES}")
    if playlist["kind"] == "smart":
        _expect(s, "rule" in playlist, "a smart list needs 'rule'")


def validate_track(track: Any) -> None:
    """Raise `SchemaError` unless `track` has the shape of `mp.track/1`."""
    s = TRACK
    _check_keys(s, track, _TRACK_REQUIRED, _TRACK_KEYS)
    _expect(s, _is_str(track["id"]) and bool(TRACK_ID_RE.match(track["id"])), "'id' does not match trk_ + 16 hex")
    _check_source(s, track["source"])
    _expect(s, _is_str(track["source_hash"]), "'source_hash' must be a string")
    _expect(s, isinstance(track["editable"], bool), "'editable' must be a boolean")
    _expect(s, _is_number(track["duration_s"]) and track["duration_s"] >= 0, "'duration_s' must be a number >= 0")
    if "content_hash" in track:
        _expect(s, track["content_hash"] is None or _is_str(track["content_hash"]), "'content_hash' must be a string or null")
    for key in ("sample_rate", "channels"):
        if key in track:
            _expect(s, track[key] is None or _is_int(track[key]), f"'{key}' must be an integer or null")
    for key in ("sample_rate_basis", "loaded_at"):
        if key in track:
            _expect(s, _is_str(track[key]), f"'{key}' must be a string")


LYRICS_SOURCES = ("lrc", "tags", "lrclib", "transcribe", "manual")
_LYRICS_REQUIRED = ("schema", "track_hash", "source", "lines")
_LYRICS_KEYS = set(_LYRICS_REQUIRED) | {"language", "synced", "edited", "job_id", "lrclib", "transcription",
                                        "updated_at"}
_LYRICS_LINE_KEYS = {"t_start", "t_end", "text"}
_LRCLIB_KEYS = {"id", "artist_name", "track_name", "album_name", "duration", "synced"}
# What ran a `source: "transcribe"` record (Mike, 2026-11-20); optional, so older records load.
_TRANSCRIPTION_KEYS = {"provider", "model"}


def validate_lyrics(lyrics: Any) -> None:
    """Raise `SchemaError` unless `lyrics` has the shape of `mp.lyrics/1` (SPEC §4)."""
    s = LYRICS
    _check_keys(s, lyrics, _LYRICS_REQUIRED, _LYRICS_KEYS)
    _expect(s, _is_str(lyrics["track_hash"]), "'track_hash' must be a string")
    _expect(s, lyrics["source"] in LYRICS_SOURCES, f"'source' must be one of {LYRICS_SOURCES}")
    if "language" in lyrics:
        _expect(s, lyrics["language"] is None or _is_str(lyrics["language"]), "'language' must be a string or null")
    if "synced" in lyrics:
        _expect(s, isinstance(lyrics["synced"], bool), "'synced' must be a boolean")
    synced = lyrics.get("synced", True)
    lines = lyrics["lines"]
    _expect(s, isinstance(lines, list), "'lines' must be a list")
    for i, line in enumerate(lines):
        _expect(s, isinstance(line, Mapping), f"line {i} must be an object")
        _expect(s, "t_start" in line and "text" in line, f"line {i} needs 't_start' and 'text'")
        extra = sorted(set(line) - _LYRICS_LINE_KEYS)
        _expect(s, not extra, f"line {i} has undeclared key(s): {', '.join(extra)}")
        _expect(s, _is_str(line["text"]), f"line {i}: 'text' must be a string")
        t = line["t_start"]
        if t is None:
            _expect(s, not synced, f"line {i}: 't_start' is null only when 'synced' is false")
        else:
            _expect(s, _is_number(t) and t >= 0, f"line {i}: 't_start' must be a number >= 0")
        if "t_end" in line:
            _expect(s, _is_number(line["t_end"]) and t is not None and line["t_end"] > t,
                    f"line {i}: 't_end' must be a number greater than 't_start'")
    if not synced:
        _expect(s, all(line["t_start"] is None for line in lines), "unsynced lyrics have every 't_start' null")
    if "edited" in lyrics:
        _expect(s, isinstance(lyrics["edited"], bool), "'edited' must be a boolean")
    if "job_id" in lyrics:
        _expect(s, _is_str(lyrics["job_id"]), "'job_id' must be a string")
    if "updated_at" in lyrics:
        _expect(s, _is_str(lyrics["updated_at"]), "'updated_at' must be a string")
    if "lrclib" in lyrics:
        rec = lyrics["lrclib"]
        _expect(s, isinstance(rec, Mapping) and set(rec) == _LRCLIB_KEYS,
                f"'lrclib' must have exactly the keys {sorted(_LRCLIB_KEYS)}")
        _expect(s, _is_int(rec["id"]), "'lrclib.id' must be an integer")
        _expect(s, _is_str(rec["artist_name"]) and _is_str(rec["track_name"]), "'lrclib' names must be strings")
        _expect(s, rec["album_name"] is None or _is_str(rec["album_name"]), "'lrclib.album_name' must be a string or null")
        _expect(s, rec["duration"] is None or _is_number(rec["duration"]), "'lrclib.duration' must be a number or null")
        _expect(s, isinstance(rec["synced"], bool), "'lrclib.synced' must be a boolean")
    if "transcription" in lyrics:
        rec = lyrics["transcription"]
        _expect(s, lyrics["source"] == "transcribe", "'transcription' is only on lyrics with source 'transcribe'")
        _expect(s, isinstance(rec, Mapping) and set(rec) == _TRANSCRIPTION_KEYS,
                f"'transcription' must have exactly the keys {sorted(_TRANSCRIPTION_KEYS)}")
        _expect(s, all(rec[k] is None or _is_str(rec[k]) for k in _TRANSCRIPTION_KEYS),
                "'transcription.provider' and 'transcription.model' must be strings or null")


VALIDATORS: Dict[str, Any] = {
    LIBRARY_ENTRY: validate_library_entry,
    MUSIC_FOLDER: validate_music_folder,
    PLAYLIST: validate_playlist,
    TRACK: validate_track,
    LYRICS: validate_lyrics,
}


def validate_comment(comment: Any) -> None:
    """`mp.comment/1` (SPEC §4), as the core stores it: the `allOf` rule (a point has
    `t_end` null, a region a number) and the `author` object are checked too."""
    s = COMMENT
    _check_keys(s, comment, _COMMENT_REQUIRED, _COMMENT_KEYS)
    _expect(s, _is_str(comment["id"]) and bool(COMMENT_ID_RE.match(comment["id"])), "'id' does not match cmt_ + 16 hex")
    _expect(s, _is_str(comment["track_id"]) and bool(TRACK_ID_RE.match(comment["track_id"])), "'track_id' does not match trk_ + 16 hex")
    _expect(s, _is_str(comment["version_id"]), "'version_id' must be a string")
    _expect(s, comment["kind"] in COMMENT_KINDS, f"'kind' {comment['kind']!r} is not one of {COMMENT_KINDS}")
    _expect(s, _is_number(comment["t_start"]) and comment["t_start"] >= 0, "'t_start' must be a number >= 0")
    end = comment["t_end"]
    if comment["kind"] == "point":
        _expect(s, end is None, "a point comment has 't_end' null")
    else:
        _expect(s, _is_number(end) and end > comment["t_start"], "a region comment has 't_end' > 't_start'")
    _expect(s, _is_str(comment["text"]), "'text' must be a string")
    _expect(s, comment["action"] in COMMENT_ACTIONS, f"'action' {comment['action']!r} is not one of {COMMENT_ACTIONS}")
    author = comment["author"]
    _expect(s, isinstance(author, Mapping) and set(author) == {"kind", "name"}
            and author["kind"] in COMMENT_AUTHOR_KINDS and _is_str(author["name"]),
            "'author' must be {kind: human|agent, name: string}")
    _expect(s, _is_str(comment["created_at"]), "'created_at' must be a string")
    if "updated_at" in comment:
        _expect(s, _is_str(comment["updated_at"]), "'updated_at' must be a string")
