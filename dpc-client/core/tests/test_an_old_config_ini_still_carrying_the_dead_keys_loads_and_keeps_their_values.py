"""An old config.ini that still carries the dead keys loads, and their values stay readable.

The dead-key card (DEAD-CONFIG-INI-KEYS-ARE-STILL-WRITTEN-INTO-A-NEW-FILE-AND-CHANGE-NOTHING,
2026-11-27) removed 50 keys nothing reads from `Settings._create_default_config`: a person
who edited them saw no effect, so a new file no longer offers them. A file written before
that still carries them, and this pins the promise the card makes for it:

- Settings loads it without an exception (configparser parses every key it finds; unknown
  to the code is not unknown to the format);
- a live key keeps the non-default value its owner wrote, so the load reads the file;
- none of the 50 deleted keys is seeded into a freshly created file any more;
- `Settings.get(section, key, 'fallback')` answers the stored old value — `get` reads the
  config file before the fallback — so a path that still asks cannot crash and cannot be
  silently handed the fallback instead of what the owner wrote.

Every home is under tmp_path; no `DPC_*` variable may answer first, so each one of the 50
is cleared before `get` is called.
"""

import configparser

from dpc_client_core.settings import Settings

# Every key the card deleted, with the value an old file holds: the seed's old default.
# {(section, key): value}
DEAD_KEYS = {
    ("agent_telegram", "auto_link_on_create"): "true",
    ("agent_telegram", "require_confirmation"): "false",
    ("agent_telegram", "default_enabled"): "true",
    ("connection", "enable_ipv6"): "false",
    ("connection", "enable_ipv4"): "false",
    ("connection", "enable_hub_webrtc"): "true",
    ("connection", "enable_relays"): "false",
    ("connection", "enable_gossip"): "false",
    ("conversations", "storage_version"): "1",
    ("dpc_agent", "budget_usd"): "75",
    ("dpc_agent", "max_rounds"): "300",
    ("dpc_agent", "context_window"): "128000",
    ("dpc_agent", "enable_task_queue"): "false",
    ("dpc_agent", "billing_model"): "pay_per_use",
    ("file_transfer", "preparation_timeout_base"): "90",
    ("file_transfer", "preparation_timeout_per_gb"): "60",
    ("file_transfer", "preparation_progress_interval_mb"): "200",
    ("file_transfer", "preparation_progress_interval_chunks"): "20000",
    ("gossip", "cleanup_interval"): "1200",
    ("gossip", "priority"): "high",
    ("hole_punch", "nat_detection_enabled"): "false",
    ("hole_punch", "stun_timeout"): "10",
    ("hole_punch", "punch_attempts"): "5",
    ("hole_punch", "enable_dtls"): "false",
    ("hole_punch", "dtls_version"): "1.3",
    ("knowledge", "token_warning_threshold"): "0.9",
    ("knowledge", "auto_extraction_enabled"): "false",
    ("local_transcription", "model"): "openai/whisper-small",
    ("local_transcription", "device"): "cpu",
    ("local_transcription", "compile_model"): "false",
    ("local_transcription", "use_flash_attention"): "true",
    ("local_transcription", "chunk_length_s"): "20",
    ("local_transcription", "batch_size"): "8",
    ("local_transcription", "language"): "en",
    ("local_transcription", "task"): "translate",
    ("local_transcription", "lazy_loading"): "false",
    ("relay", "prefer_region"): "eu-central",
    ("relay", "cache_timeout"): "600",
    ("relay", "region"): "us-west",
    ("system", "collect_hardware_specs"): "false",
    ("system", "collect_dev_tools"): "false",
    ("telegram", "fetch_history_on_startup"): "false",
    ("telegram", "history_fetch_limit"): "50",
    ("telegram", "history_max_age_hours"): "12",
    ("telegram", "history_message_types"): "text,photo",
    ("telegram", "drop_pending_updates"): "true",
    ("vision", "enabled"): "false",
    ("vision", "default_provider"): "anthropic",
    ("vision", "thumbnail_quality"): "70",
    ("voice_messages", "enabled"): "false",
}
assert len(DEAD_KEYS) == 50, "the card deleted 50 keys; this file must carry them all"


def _write_an_old_config(home) -> None:
    """A config.ini from before the card: every dead key, plus one live key set off its
    default (`[gossip] enabled` is seeded true) to prove the load reads the file."""
    by_section: dict = {}
    for (section, key), value in DEAD_KEYS.items():
        by_section.setdefault(section, []).append(f"{key} = {value}")
    by_section["gossip"].insert(0, "enabled = false")
    lines = [line for section in by_section for line in (f"[{section}]", *by_section[section])]
    (home / "config.ini").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_an_old_file_with_every_dead_key_loads_and_keeps_their_values(tmp_path, monkeypatch):
    home = tmp_path / "old"
    home.mkdir()
    _write_an_old_config(home)
    for (section, key) in DEAD_KEYS:  # no DPC_* variable may answer before the file
        monkeypatch.delenv(f"DPC_{section.upper()}_{key.upper()}", raising=False)

    settings = Settings(home)  # no exception: the format knows every key the code forgot

    # The live key keeps the non-default value its owner wrote.
    assert settings.get_gossip_enabled() is False, "the live key did not read its written value"

    # A deleted key's stored value survives the load and answers before any fallback,
    # so a path that still asks for one neither crashes nor sees the fallback.
    for (section, key), value in DEAD_KEYS.items():
        assert settings.get(section, key, "fallback") == value, \
            f"[{section}] {key} did not read back its stored old value"


def test_a_fresh_file_seeds_none_of_the_dead_keys(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    Settings(fresh)
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(fresh / "config.ini", encoding="utf-8")
    written = {(section, key) for section in parser.sections() for key in parser[section]}
    seeded = sorted(f"[{section}] {key}" for key in DEAD_KEYS if key in written)
    assert not seeded, f"keys nothing reads are still written into a new file: {seeded}"
