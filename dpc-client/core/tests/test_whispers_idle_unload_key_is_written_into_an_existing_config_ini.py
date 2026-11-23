"""ADR-052, Mike's decisions of 2026-11-23: two `[local_transcription]` keys.

Item 6A: the dictation bound, `queue_wait_s`, is 600 s: the default config writes it and the getter
reads 600 for a missing or unreadable value.

Item 7's carrier (Mike #9 and #15): `idle_unload_s` keeps its default in code (300) **and** the key
is written into `config.ini`, so the owner sees it; the value in the file wins over the code default.
A fresh config has had it since step 1; a config written before then gets the key at start, the
way the firewall seeds a missing tool default into an existing `privacy_rules.json`
(`_seed_missing_tools_into_rules`): only a missing key is written, a value the owner wrote is never
touched, and a file that already has it is not rewritten.
"""

import configparser

import pytest

from dpc_client_core.settings import Settings


def _read(home):
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(home / "config.ini", encoding="utf-8")
    return parser


def _old_config(home, local_transcription=None):
    """A config.ini written before the key existed."""
    lines = ["[hub]", "url = http://localhost:8000", ""]
    if local_transcription is not None:
        lines.append("[local_transcription]")
        lines += [f"{k} = {v}" for k, v in local_transcription.items()]
        lines.append("")
    (home / "config.ini").write_text("\n".join(lines), encoding="utf-8")


class TestQueueWaitS:
    def test_a_fresh_config_writes_600(self, tmp_path):
        Settings(tmp_path)
        assert _read(tmp_path)["local_transcription"]["queue_wait_s"] == "600"

    def test_the_getter_reads_600_by_default(self, tmp_path):
        _old_config(tmp_path)
        assert Settings(tmp_path).get_local_transcription_queue_wait_s() == 600

    def test_the_file_value_wins(self, tmp_path):
        _old_config(tmp_path, {"queue_wait_s": "120"})
        assert Settings(tmp_path).get_local_transcription_queue_wait_s() == 120

    @pytest.mark.parametrize("raw", ["nonsense", "-5", "1.5e"])
    def test_an_unreadable_value_reads_as_the_default(self, tmp_path, raw):
        _old_config(tmp_path, {"queue_wait_s": raw})
        assert Settings(tmp_path).get_local_transcription_queue_wait_s() == 600

    def test_zero_is_do_not_wait(self, tmp_path):
        _old_config(tmp_path, {"queue_wait_s": "0"})
        assert Settings(tmp_path).get_local_transcription_queue_wait_s() == 0


class TestIdleUnloadSIsWrittenForTheOwner:
    def test_a_fresh_config_has_it(self, tmp_path):
        Settings(tmp_path)
        assert _read(tmp_path)["local_transcription"]["idle_unload_s"] == "300"

    def test_an_old_config_without_the_section_gets_it_at_start(self, tmp_path):
        _old_config(tmp_path)
        s = Settings(tmp_path)
        parser = _read(tmp_path)
        assert parser["local_transcription"]["idle_unload_s"] == "300"
        assert parser["hub"]["url"] == "http://localhost:8000", "the rest of the file is kept"
        assert s.get_local_transcription_idle_unload_s() == 300.0

    def test_an_old_config_with_the_section_but_not_the_key_gets_it(self, tmp_path):
        _old_config(tmp_path, {"device": "cpu"})
        Settings(tmp_path)
        section = _read(tmp_path)["local_transcription"]
        assert section["idle_unload_s"] == "300" and section["device"] == "cpu"

    def test_the_owners_value_is_never_touched_and_wins(self, tmp_path):
        _old_config(tmp_path, {"idle_unload_s": "45"})
        s = Settings(tmp_path)
        assert _read(tmp_path)["local_transcription"]["idle_unload_s"] == "45"
        assert s.get_local_transcription_idle_unload_s() == 45.0

    def test_a_file_that_has_the_key_is_not_rewritten(self, tmp_path):
        _old_config(tmp_path, {"idle_unload_s": "300"})
        before = (tmp_path / "config.ini").read_bytes()
        Settings(tmp_path)
        assert (tmp_path / "config.ini").read_bytes() == before

    def test_the_seeded_value_is_the_code_default(self, tmp_path):
        from dpc_client_core.providers.whisper_provider import DEFAULT_IDLE_UNLOAD_S

        _old_config(tmp_path)
        Settings(tmp_path)
        assert float(_read(tmp_path)["local_transcription"]["idle_unload_s"]) == DEFAULT_IDLE_UNLOAD_S

    def test_every_other_line_and_comment_of_the_file_is_kept(self, tmp_path):
        text = ("# D-PC Client Configuration\n\n[hub]\nurl = http://localhost:8000  # the hub\n\n"
                "[local_transcription]  # Whisper\ndevice = cpu  # forced\n")
        (tmp_path / "config.ini").write_text(text, encoding="utf-8")
        Settings(tmp_path)
        after = (tmp_path / "config.ini").read_text(encoding="utf-8")
        for line in text.splitlines():
            assert line in after.splitlines()
        assert "idle_unload_s = 300  # " in after
        assert after.count("[local_transcription]") == 1
        assert _read(tmp_path)["local_transcription"]["idle_unload_s"] == "300"

    def test_the_file_still_parses_after_a_second_start(self, tmp_path):
        _old_config(tmp_path, {"device": "cpu"})
        Settings(tmp_path)
        once = (tmp_path / "config.ini").read_bytes()
        Settings(tmp_path)
        assert (tmp_path / "config.ini").read_bytes() == once

    def test_a_config_that_cannot_be_written_still_loads(self, tmp_path, monkeypatch):
        _old_config(tmp_path)
        real_open = open

        def refusing_open(path, mode="r", *args, **kwargs):
            if "w" in mode and str(path).endswith("config.ini"):
                raise PermissionError("read-only")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", refusing_open)
        s = Settings(tmp_path)
        assert s.get_local_transcription_idle_unload_s() == 300.0
