"""A percent sign in any value any settings form writes.

'%' is configparser's interpolation mark. Written unescaped, a value holding one did
one of two things, and both were wrong: the write failed with an error that quoted
the whole value (a TURN password went to the UI that way, after the keys ahead of it
in the same save had reached the disk), or a value shaped like ``%(x)s`` was stored
and every later read of its section raised, again quoting part of the value.

The first fix named the keys a review had found, and a review found what it had
missed. So this file does not name keys: it takes every settings-form setter, every
string parameter and every per-section key from the services' own lists, and offers
each one a value holding a percent sign in both shapes at once. Whatever the setter
does with it must be one of two things, and nothing else: refuse it without quoting
the value, or store it so that a fresh Settings reads the file and gives the value
back as typed. A key added to any form later is covered the day it is added.
"""
from __future__ import annotations

import inspect

import pytest

from dpc_client_core import network_service as ns
from dpc_client_core import telegram_discord_service as td
from dpc_client_core.settings import Settings

# A lone mark and the interpolation shape together: the first broke the write, the
# second was stored and broke the reads.
PCT = "a%(x)s%b"

FLAT_SETTERS = [
    "set_music_settings", "set_vision_settings",
    "set_knowledge_extraction_settings", "set_history_sync_settings", "set_hf_settings",
    "set_local_transcription_settings", "set_voice_messages_settings",
    "set_background_model_settings", "set_privacy_logging_settings",
]

# The sections each sectioned setter writes, and where each key list lives.
SECTIONED = [
    ("set_network_settings", ns, [
        ("p2p", "_P2P_KEYS"), ("dht", "_DHT_KEYS"), ("relay", "_RELAY_KEYS"),
        ("gossip", "_GOSSIP_KEYS"), ("connection", "_CONNECTION_KEYS"),
        ("hole_punch", "_HOLE_PUNCH_KEYS"), ("turn", "_TURN_KEYS"),
        ("webrtc", "_WEBRTC_KEYS"), ("hub", "_HUB_KEYS"), ("oauth", "_OAUTH_KEYS"),
        ("gateway", "_GATEWAY_KEYS"), ("file_transfer", "_FILE_TRANSFER_KEYS"),
    ]),
    ("set_telegram_discord_settings", td, [
        ("telegram", "_TELEGRAM_KEYS"), ("discord", "_DISCORD_KEYS"),
    ]),
]

# A refusal may echo a value that is not a secret, when the person needs to see which
# one was refused: a relative export folder is of no use unless the message names it.
MAY_QUOTE = {("set_music_settings", "export_dir")}


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, so no test reads or writes ~/.dpc."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


def _keys(module, name):
    return sorted(k for k in getattr(module, name) if not k.startswith("clear_"))


def _cases():
    cases = []
    for name in FLAT_SETTERS:
        for param in inspect.signature(getattr(Settings, name)).parameters:
            if param == "self" or param.startswith("clear_") or param == "module_levels":
                continue
            cases.append(pytest.param(name, param, None, id=f"{name}.{param}"))
    for setter, module, sections in SECTIONED:
        for section, list_name in sections:
            for key in _keys(module, list_name):
                cases.append(pytest.param(setter, section, key, id=f"{setter}[{section}].{key}"))
    return cases


def _config_ini(home):
    return home / "config.ini"


def _set(settings, setter, section, key, value):
    if key is None:
        getattr(settings, setter)(**{section: value})
    else:
        getattr(settings, setter)(**{section: {key: value}})


class TestEveryFormKeyTakesAPercentSign:
    @pytest.mark.parametrize("setter, section, key", _cases())
    def test_it_is_refused_without_quoting_or_stored_and_read_back(
            self, home, setter, section, key):
        settings = Settings(home)
        config_ini = _config_ini(home)
        before = config_ini.read_bytes()
        try:
            _set(settings, setter, section, key, PCT)
        except ValueError as e:
            # The refusal names the key and stops there: the value rides in no message.
            assert PCT not in str(e) or (setter, section) in MAY_QUOTE, \
                f"the refusal quotes the value: {e}"
            assert config_ini.read_bytes() == before, "a refusal writes nothing"
            return
        except Exception as e:                                   # noqa: BLE001 - the point is the kind
            pytest.fail(f"{type(e).__name__} instead of a clean refusal or a save: {e}")
        # Saved. Two facts, both read from the file rather than from the form's getters,
        # whose parameter names are not config.ini section and key names.
        stored = config_ini.read_bytes()
        assert stored != before, "the value was neither refused nor written"
        assert b"%(x)s" not in stored.replace(b"%%(x)s", b""), \
            "the interpolation shape reached config.ini single: it would break every read"
        # A fresh Settings is a fresh read of the whole file: nothing in it fails to load.
        Settings(home)

    def test_the_cover_is_whole(self):
        """The lists are the services' own: a form with no key here is a form this file
        has forgotten, which is how the first fix missed three keys."""
        cases = _cases()
        assert len(cases) > 80, f"only {len(cases)} cases: a form's key list is not being read"
        # Every setter named above exists and is a method of Settings.
        for setter, _, _ in SECTIONED:
            assert callable(getattr(Settings, setter, None)), setter
        for setter in FLAT_SETTERS:
            assert callable(getattr(Settings, setter, None)), setter
