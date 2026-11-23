"""ADR-052 S3 follow-up 2, Johnny #24 (question): where Whisper reads `idle_unload_s` in production.

A `LocalWhisperProvider` reads its idle-unload seconds from a module-level source; with none set it
reads 0.0, "never unload", by design for a provider built outside the service (tests, scripts). The
one production entry (`run_service.py` builds `CoreService`) registers the settings getter in
`CoreService.__init__`, once, before any provider can schedule a timer, and nothing in the package
unregisters it. The source is read on every schedule, so a provider built by a providers reload and
the successor of `hand_over_to` read it too: an existing config.ini without the key gives the 300 s
default (the seed writes it), not 0.
"""

import inspect
import re
from pathlib import Path

import pytest

import dpc_client_core
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import DEFAULT_IDLE_UNLOAD_S, LocalWhisperProvider
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

PACKAGE = Path(dpc_client_core.__file__).parent


def test_the_service_registers_the_settings_getter_before_the_providers_are_injected():
    source = inspect.getsource(CoreService.__init__)
    registered = source.index("set_idle_unload_source(self.settings.get_local_transcription_idle_unload_s)")
    assert registered < source.index("self._inject_service_into_providers()")
    assert source.index("self.settings = Settings(") < registered


def test_nothing_else_in_the_package_sets_or_clears_the_source():
    setters = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"set_idle_unload_source\(([^)]*)\)|_idle_unload_source\s*=(?!=)", text):
            setters.append((path.relative_to(PACKAGE).as_posix(), match.group(0)))
    assert sorted(setters) == sorted([
        ("service.py", "set_idle_unload_source(self.settings.get_local_transcription_idle_unload_s)"),
        ("providers/whisper_provider.py", "set_idle_unload_source(source: Optional[Callable[[], float]])"),
        ("providers/whisper_provider.py", "_idle_unload_source ="),  # inside the setter
    ]), setters


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The source as `CoreService.__init__` sets it, over a config.ini written before the key."""
    (tmp_path / "config.ini").write_text("[hub]\nurl = http://localhost:8000\n", encoding="utf-8")
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
    whisper_provider.set_idle_unload_source(Settings(tmp_path).get_local_transcription_idle_unload_s)
    yield
    whisper_provider.set_idle_unload_source(None)


def _provider():
    return LocalWhisperProvider("w", {"type": "local_whisper", "device": "cuda"})


def test_unwired_is_never_by_design(monkeypatch):
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
    assert _provider()._idle_unload_s() == 0.0


@pytest.mark.usefixtures("wired")
def test_a_provider_reads_the_300_s_default():
    assert _provider()._idle_unload_s() == DEFAULT_IDLE_UNLOAD_S == 300.0


@pytest.mark.usefixtures("wired")
def test_the_successor_of_a_providers_reload_reads_it_too():
    old, new = _provider(), _provider()
    old.hand_over_to(new)
    assert new._idle_unload_s() == 300.0
